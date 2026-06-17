"""Bridge one Telcoflow phone call to Amazon Nova 2 Sonic (Bedrock bidirectional stream).

Nova Sonic expects 16 kHz LPCM input and returns 24 kHz LPCM output.
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import uuid
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from aws_sdk_bedrock_runtime.client import BedrockRuntimeClient
from aws_sdk_bedrock_runtime.config import Config, HTTPAuthSchemeResolver, SigV4AuthScheme
from aws_sdk_bedrock_runtime.models import (
    BidirectionalInputPayloadPart,
    InvokeModelWithBidirectionalStreamInputChunk,
    InvokeModelWithBidirectionalStreamOperationInput,
)
from smithy_aws_core.identity import EnvironmentCredentialsResolver
from telcoflow_sdk import ActiveCall
import telcoflow_sdk.events as events

logger = logging.getLogger(__name__)

TELECOFLOW_SAMPLE_RATE = 24000
NOVA_INPUT_SAMPLE_RATE = 16000
NOVA_OUTPUT_SAMPLE_RATE = 24000
MAX_CALL_DURATION_SECONDS = 600

ToolHandler = Callable[[str, dict[str, Any]], Awaitable[str]]


@dataclass
class TranscriptLine:
    role: str
    text: str


@dataclass
class NovaCallResult:
    call_id: str
    caller_number: str | None
    transcript: list[TranscriptLine] = field(default_factory=list)
    tool_calls: list[dict[str, Any]] = field(default_factory=list)


class NovaSonicBridge:
    def __init__(
        self,
        *,
        model_id: str,
        region: str,
        voice_id: str,
        system_prompt: str,
        tools: list[dict[str, Any]] | None = None,
        tool_handlers: dict[str, ToolHandler] | None = None,
    ):
        self.model_id = model_id
        self.region = region
        self.voice_id = voice_id
        self.system_prompt = system_prompt
        self.tools = tools or []
        self.tool_handlers = tool_handlers or {}

        self._client: BedrockRuntimeClient | None = None
        self._stream = None
        self._response_task: asyncio.Task | None = None
        self._is_active = False
        self._barge_in = False
        self._role = "ASSISTANT"
        self._display_assistant_text = False

        self.prompt_name = str(uuid.uuid4())
        self.system_content_name = str(uuid.uuid4())
        self.audio_content_name = str(uuid.uuid4())

        self.transcript: list[TranscriptLine] = []
        self.tool_calls: list[dict[str, Any]] = []
        self._resample_remainder = b""

        self._pending_tool_name: str | None = None
        self._pending_tool_use_id: str | None = None
        self._pending_tool_input: dict[str, Any] | None = None
        self._call: ActiveCall | None = None

    def _initialize_client(self) -> None:
        config = Config(
            endpoint_uri=f"https://bedrock-runtime.{self.region}.amazonaws.com",
            region=self.region,
            aws_credentials_identity_resolver=EnvironmentCredentialsResolver(),
            auth_scheme_resolver=HTTPAuthSchemeResolver(),
            auth_schemes={"aws.auth#sigv4": SigV4AuthScheme(service="bedrock")},
        )
        self._client = BedrockRuntimeClient(config=config)

    async def send_event(self, event: dict[str, Any]) -> None:
        payload = json.dumps(event).encode("utf-8")
        chunk = InvokeModelWithBidirectionalStreamInputChunk(
            value=BidirectionalInputPayloadPart(bytes_=payload)
        )
        await self._stream.input_stream.send(chunk)

    def _record_text(self, role: str, text: str) -> None:
        clean = text.strip()
        if not clean or '{ "interrupted" : true }' in clean:
            return
        if self.transcript and self.transcript[-1].role == role:
            self.transcript[-1].text = f"{self.transcript[-1].text} {clean}".strip()
        else:
            self.transcript.append(TranscriptLine(role=role, text=clean))
        logger.info("Nova transcript [%s]: %s", role, clean)

    def _downsample_to_nova(self, chunk: bytes) -> bytes:
        """Downsample 24 kHz → 16 kHz. Uses audioop on ≤3.12, pure-Python on 3.13+."""
        try:
            import audioop
            converted, self._resample_remainder = audioop.ratecv(
                chunk, 2, 1,
                TELECOFLOW_SAMPLE_RATE, NOVA_INPUT_SAMPLE_RATE,
                self._resample_remainder if self._resample_remainder else None,
            )
            return converted
        except ImportError:
            pass

        import struct
        data = self._resample_remainder + chunk
        self._resample_remainder = b""
        frame_size = 2
        n_samples = len(data) // frame_size
        if n_samples == 0:
            return b""
        samples = struct.unpack(f"<{n_samples}h", data[: n_samples * frame_size])
        self._resample_remainder = data[n_samples * frame_size :]
        ratio = TELECOFLOW_SAMPLE_RATE / NOVA_INPUT_SAMPLE_RATE
        out_len = int(n_samples / ratio)
        out = []
        for i in range(out_len):
            src = i * ratio
            idx = int(src)
            frac = src - idx
            s0 = samples[idx]
            s1 = samples[min(idx + 1, n_samples - 1)]
            out.append(int(s0 + frac * (s1 - s0)))
        return struct.pack(f"<{len(out)}h", *out)

    async def start_session(self) -> None:
        if not self._client:
            self._initialize_client()

        self._stream = await self._client.invoke_model_with_bidirectional_stream(
            InvokeModelWithBidirectionalStreamOperationInput(model_id=self.model_id)
        )
        self._is_active = True

        await self.send_event(
            {
                "event": {
                    "sessionStart": {
                        "inferenceConfiguration": {
                            "maxTokens": 1024,
                            "topP": 0.9,
                            "temperature": 0.7,
                        },
                        "turnDetectionConfiguration": {"endpointingSensitivity": "HIGH"},
                    }
                }
            }
        )

        prompt_start: dict[str, Any] = {
            "event": {
                "promptStart": {
                    "promptName": self.prompt_name,
                    "textOutputConfiguration": {"mediaType": "text/plain"},
                    "audioOutputConfiguration": {
                        "mediaType": "audio/lpcm",
                        "sampleRateHertz": NOVA_OUTPUT_SAMPLE_RATE,
                        "sampleSizeBits": 16,
                        "channelCount": 1,
                        "voiceId": self.voice_id,
                        "encoding": "base64",
                        "audioType": "SPEECH",
                    },
                }
            }
        }
        if self.tools:
            prompt_start["event"]["promptStart"]["toolUseOutputConfiguration"] = {
                "mediaType": "application/json"
            }
            prompt_start["event"]["promptStart"]["toolConfiguration"] = {
                "tools": self.tools,
                "toolChoice": {"auto": {}},
            }
        await self.send_event(prompt_start)

        await self.send_event(
            {
                "event": {
                    "contentStart": {
                        "promptName": self.prompt_name,
                        "contentName": self.system_content_name,
                        "type": "TEXT",
                        "interactive": True,
                        "role": "SYSTEM",
                        "textInputConfiguration": {"mediaType": "text/plain"},
                    }
                }
            }
        )
        await self.send_event(
            {
                "event": {
                    "textInput": {
                        "promptName": self.prompt_name,
                        "contentName": self.system_content_name,
                        "content": self.system_prompt,
                    }
                }
            }
        )
        await self.send_event(
            {
                "event": {
                    "contentEnd": {
                        "promptName": self.prompt_name,
                        "contentName": self.system_content_name,
                    }
                }
            }
        )

        self._response_task = asyncio.create_task(self._process_responses())

    async def start_audio_input(self) -> None:
        await self.send_event(
            {
                "event": {
                    "contentStart": {
                        "promptName": self.prompt_name,
                        "contentName": self.audio_content_name,
                        "type": "AUDIO",
                        "interactive": True,
                        "role": "USER",
                        "audioInputConfiguration": {
                            "mediaType": "audio/lpcm",
                            "sampleRateHertz": NOVA_INPUT_SAMPLE_RATE,
                            "sampleSizeBits": 16,
                            "channelCount": 1,
                            "audioType": "SPEECH",
                            "encoding": "base64",
                        },
                    }
                }
            }
        )

    async def send_audio_chunk(self, audio_bytes: bytes) -> None:
        if not self._is_active:
            return
        encoded = base64.b64encode(audio_bytes).decode("utf-8")
        await self.send_event(
            {
                "event": {
                    "audioInput": {
                        "promptName": self.prompt_name,
                        "contentName": self.audio_content_name,
                        "content": encoded,
                    }
                }
            }
        )

    async def end_audio_input(self) -> None:
        await self.send_event(
            {
                "event": {
                    "contentEnd": {
                        "promptName": self.prompt_name,
                        "contentName": self.audio_content_name,
                    }
                }
            }
        )

    async def _send_tool_result(self, tool_use_id: str, result: str) -> None:
        tool_content_name = str(uuid.uuid4())
        await self.send_event(
            {
                "event": {
                    "contentStart": {
                        "promptName": self.prompt_name,
                        "contentName": tool_content_name,
                        "type": "TOOL",
                        "role": "TOOL",
                        "toolUseId": tool_use_id,
                        "toolUseOutputConfiguration": {"mediaType": "application/json"},
                    }
                }
            }
        )
        await self.send_event(
            {
                "event": {
                    "toolResult": {
                        "promptName": self.prompt_name,
                        "contentName": tool_content_name,
                        "content": result,
                    }
                }
            }
        )
        await self.send_event(
            {
                "event": {
                    "contentEnd": {
                        "promptName": self.prompt_name,
                        "contentName": tool_content_name,
                    }
                }
            }
        )

    async def _handle_tool_use(self, tool_name: str, tool_use_id: str, tool_input: dict[str, Any]) -> None:
        handler = self.tool_handlers.get(tool_name)
        if handler is None:
            result = json.dumps({"error": f"Unknown tool: {tool_name}"})
        else:
            try:
                result = await handler(tool_name, tool_input)
            except Exception as exc:
                logger.exception("Tool %s failed", tool_name)
                result = json.dumps({"error": str(exc)})
        self.tool_calls.append(
            {"tool": tool_name, "input": tool_input, "result": result}
        )
        await self._send_tool_result(tool_use_id, result)

    async def _process_responses(self) -> None:
        try:
            while self._is_active:
                output = await self._stream.await_output()
                result = await output[1].receive()
                if not result.value or not result.value.bytes_:
                    continue

                payload = json.loads(result.value.bytes_.decode("utf-8"))
                event = payload.get("event", {})

                if "contentStart" in event:
                    content_start = event["contentStart"]
                    self._role = content_start.get("role", self._role)
                    additional = content_start.get("additionalModelFields")
                    if additional:
                        try:
                            fields = json.loads(additional)
                            self._display_assistant_text = (
                                fields.get("generationStage") == "SPECULATIVE"
                            )
                        except json.JSONDecodeError:
                            self._display_assistant_text = False

                elif "textOutput" in event:
                    text = event["textOutput"]["content"]
                    role = event["textOutput"].get("role", self._role)
                    if '{ "interrupted" : true }' in text:
                        self._barge_in = True
                        if self._call:
                            await self._call.interrupt()
                    if role == "USER":
                        self._record_text("USER", text)
                    elif role == "ASSISTANT" and self._display_assistant_text:
                        self._record_text("ASSISTANT", text)

                elif "audioOutput" in event:
                    if self._barge_in and self._call:
                        await self._call.interrupt()
                        self._barge_in = False
                        continue
                    audio_bytes = base64.b64decode(event["audioOutput"]["content"])
                    if self._call:
                        await self._call.send_audio(audio_bytes)

                elif "toolUse" in event:
                    tool_event = event["toolUse"]
                    self._pending_tool_name = tool_event.get("toolName")
                    self._pending_tool_use_id = tool_event.get("toolUseId")
                    raw_content = tool_event.get("content", {})
                    if isinstance(raw_content, dict):
                        self._pending_tool_input = raw_content
                    else:
                        try:
                            self._pending_tool_input = json.loads(raw_content or "{}")
                        except json.JSONDecodeError:
                            self._pending_tool_input = {}

                elif "contentEnd" in event:
                    content_end = event["contentEnd"]
                    if (
                        content_end.get("type") == "TOOL"
                        and self._pending_tool_name
                        and self._pending_tool_use_id is not None
                    ):
                        await self._handle_tool_use(
                            self._pending_tool_name,
                            self._pending_tool_use_id,
                            self._pending_tool_input or {},
                        )
                        self._pending_tool_name = None
                        self._pending_tool_use_id = None
                        self._pending_tool_input = None

        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Nova Sonic response loop failed")
        finally:
            self._is_active = False
            if hasattr(self, "_call_ended") and self._call_ended:
                self._call_ended.set()

    async def end_session(self) -> None:
        if not self._is_active:
            return
        self._is_active = False
        await self.send_event(
            {"event": {"promptEnd": {"promptName": self.prompt_name}}}
        )
        await self.send_event({"event": {"sessionEnd": {}}})
        await self._stream.input_stream.close()
        if self._response_task and not self._response_task.done():
            self._response_task.cancel()
            await asyncio.gather(self._response_task, return_exceptions=True)

    async def run_call(self, call: ActiveCall) -> NovaCallResult:
        """Bridge a single Telcoflow call through Nova Sonic until hangup."""
        self._call = call
        call_ended = asyncio.Event()
        self._call_ended = call_ended

        @call.on(events.CALL_TERMINATED)
        def on_terminated() -> None:
            call_ended.set()

        await call.answer()
        await self.start_session()
        await self.start_audio_input()

        async def stream_caller_audio() -> None:
            try:
                async for chunk in call.audio_stream():
                    if call_ended.is_set() or not self._is_active:
                        break
                    await self.send_audio_chunk(self._downsample_to_nova(chunk))
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Caller audio stream failed")

        stream_task = asyncio.create_task(stream_caller_audio())
        try:
            await asyncio.wait_for(call_ended.wait(), timeout=MAX_CALL_DURATION_SECONDS)
        except asyncio.TimeoutError:
            logger.warning("Call %s exceeded max duration (%ds), ending", call.call_id, MAX_CALL_DURATION_SECONDS)
        finally:
            stream_task.cancel()
            await asyncio.gather(stream_task, return_exceptions=True)
            await self.end_audio_input()
            await self.end_session()
            self._call = None
            self._call_ended = None

        return NovaCallResult(
            call_id=call.call_id,
            caller_number=call.caller_number,
            transcript=list(self.transcript),
            tool_calls=list(self.tool_calls),
        )
