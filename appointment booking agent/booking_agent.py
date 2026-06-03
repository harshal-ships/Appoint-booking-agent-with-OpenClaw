"""HealthFirst Clinic inbound appointment agent (Claudia).

Responsibility split (do not mix these layers):
- AgenTao SDK: phone audio in/out only.
- Gemini Live: real-time voice conversation only (no Google ADK).
- OpenClaw: post-call extraction, WhatsApp delivery, orchestration.
- gog CLI: Google Calendar only (OAuth via `gog auth` — no service-account JSON in this repo).
- Internal CLI tools: gog calendar + bookings.json + OpenClaw WhatsApp (for exec fallback).

Run:
    python booking_agent.py

Prerequisites for WhatsApp: openclaw gateway running + channels login --channel whatsapp.
Patient confirmations use: openclaw message send --channel whatsapp --target <E.164> ...
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import re
import subprocess
import sys
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from google import genai
from google.genai import types
from AgenTao_sdk import ActiveCall, AgenTaoClient, AgenTaoClientConfig
import AgenTao_sdk.events as events

try:
    from dotenv import load_dotenv
except ImportError:
    load_dotenv = None

if load_dotenv is not None:
    load_dotenv()

# ---------------------------------------------------------------------------
# Runtime configuration
# ---------------------------------------------------------------------------

SCRIPT_PATH = Path(__file__).resolve()
AUDIO_MIME_TYPE = "audio/pcm;rate=24000"
BOOKINGS_PATH = Path(os.getenv("BOOKINGS_PATH", "bookings.json")).resolve()
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash-native-audio-preview-12-2025")
OPENCLAW_AGENT = os.getenv("OPENCLAW_AGENT", "main")
OPENCLAW_TIMEOUT_SECONDS = int(os.getenv("OPENCLAW_TIMEOUT_SECONDS", "900"))
LOG_TRANSCRIPTS = os.getenv("LOG_TRANSCRIPTS", "true").lower() in {"1", "true", "yes", "on"}
CLINIC_TIMEZONE = os.getenv("CLINIC_TIMEZONE", "Asia/Singapore")
APPOINTMENT_DURATION_MINUTES = int(os.getenv("APPOINTMENT_DURATION_MINUTES", "30"))
CLINIC_OPEN_HOUR = int(os.getenv("CLINIC_OPEN_HOUR", "9"))
CLINIC_CLOSE_HOUR = int(os.getenv("CLINIC_CLOSE_HOUR", "17"))
GOG_BIN = os.getenv("GOG_BIN", "gog").strip()
DEFAULT_PHONE_COUNTRY_CODE = os.getenv("DEFAULT_PHONE_COUNTRY_CODE", "91").strip()
WHATSAPP_CLINIC_NUMBER = os.getenv("WHATSAPP_CLINIC_NUMBER", "").strip()
WHATSAPP_OPENCLAW_ACCOUNT = os.getenv("WHATSAPP_OPENCLAW_ACCOUNT", "").strip()

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)

# Claudia's live voice instructions — conversation only; no calendar or messaging here.
CLAUDIA_SYSTEM_PROMPT = """Your name is Claudia. You are an appointment assistant for HealthFirst Clinic.
You are warm, calm, and professional at all times.
You greet every caller with: Hi, thank you for calling HealthFirst Clinic. I am Claudia, your appointment assistant. I can help you book, reschedule, or cancel an appointment. What would you like to do today?
You handle three intents: booking a new appointment, rescheduling an existing appointment, and cancelling an existing appointment.

For booking collect one step at a time: patient full name, contact phone number, preferred date, preferred time, appointment type (general checkup, specialist, or follow-up).
For rescheduling collect: patient name, existing appointment date, new preferred date, new preferred time.
For cancellation collect: patient name, existing appointment date, then confirm before ending.

WhatsApp (optional — never pressure the patient):
After you have the main appointment details, ask once in a friendly way: "Would you like a summary on WhatsApp? The number you're calling from — is that your WhatsApp number?"
- If yes, the calling number is their WhatsApp — note that and move on.
- If no to the calling number but they still want WhatsApp, say they can tell you the WhatsApp number to use.
- If they do not want WhatsApp, say "No worries at all" and do not ask again.

Always confirm all details clearly with the patient before ending the call.
Never tell the patient their booking is fully confirmed during the call. Say our team will review the details and confirm shortly.
If the patient asks about insurance verification, let them know the team will follow up with them directly."""


@dataclass(frozen=True)
class TranscriptLine:
    speaker: str
    text: str


@dataclass(frozen=True)
class AppointmentRange:
    start: datetime
    end: datetime


def require_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


def ensure_gog_cli() -> None:
    """Verify the gog Google Calendar CLI is installed and authenticated."""
    try:
        result = subprocess.run(
            [GOG_BIN, "calendar", "calendars", "--json", "--no-input"],
            check=False,
            capture_output=True,
            text=True,
            timeout=60,
        )
    except FileNotFoundError as exc:
        raise RuntimeError(
            "gog CLI not found. Install: openclaw skills install gog  (or brew install steipete/tap/gogcli)"
        ) from exc

    if result.returncode != 0:
        raise RuntimeError(
            "gog is not ready for Calendar. Run: gog auth\n"
            f"stderr: {result.stderr.strip()}"
        )


def make_gemini_client() -> genai.Client:
    return genai.Client(api_key=require_env("GOOGLE_API_KEY"))


def make_AgenTao_config() -> AgenTaoClientConfig:
    return AgenTaoClientConfig.sandbox(
        api_key=require_env("WSS_API_KEY"),
        connector_uuid=require_env("WSS_CONNECTOR_UUID"),
        sample_rate=24000,
    )


# ---------------------------------------------------------------------------
# bookings.json persistence
# ---------------------------------------------------------------------------


class BookingStore:
    """Read and write the local bookings.json ledger."""

    def __init__(self, path: Path = BOOKINGS_PATH):
        self.path = path

    def load(self) -> dict[str, Any]:
        if not self.path.exists():
            return {"bookings": []}
        data = json.loads(self.path.read_text(encoding="utf-8"))
        if not isinstance(data.get("bookings"), list):
            raise RuntimeError(f"{self.path} must contain a top-level bookings list.")
        return data

    def save(self, data: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
        tmp.replace(self.path)

    def append(self, record: dict[str, Any]) -> None:
        data = self.load()
        data["bookings"].append(record)
        self.save(data)

    def find_active(
        self,
        patient_name: str,
        appointment_date: str,
    ) -> dict[str, Any] | None:
        target_name = patient_name.strip().lower()
        target_date = appointment_date.strip()
        for booking in self.load()["bookings"]:
            if booking.get("status") == "cancelled":
                continue
            if booking.get("patient_name", "").strip().lower() != target_name:
                continue
            if str(booking.get("appointment_date", "")).strip() != target_date:
                continue
            return booking
        return None

    def update_record(self, booking_id: str, updates: dict[str, Any]) -> dict[str, Any] | None:
        data = self.load()
        for booking in data["bookings"]:
            if booking.get("id") == booking_id:
                booking.update(updates)
                self.save(data)
                return booking
        return None


# ---------------------------------------------------------------------------
# Google Calendar via gog CLI (OpenClaw gog skill / OAuth)
# Docs: https://gogcli.sh/commands/gog-calendar.html
# ---------------------------------------------------------------------------


class GogCalendarClient:
    """Calendar access through the gog CLI (OAuth), not the Python Calendar API."""

    def __init__(self, calendar_id: str, timezone_name: str = CLINIC_TIMEZONE):
        self.calendar_id = calendar_id
        self.timezone_name = timezone_name
        self.timezone = ZoneInfo(timezone_name)

    def _run(self, *args: str) -> dict[str, Any]:
        command = [GOG_BIN, "--json", "--no-input", "calendar", *args]
        result = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            timeout=120,
        )
        if result.returncode != 0:
            raise RuntimeError(
                f"gog calendar failed ({' '.join(command)}): {result.stderr.strip() or result.stdout}"
            )
        stdout = result.stdout.strip()
        if not stdout:
            return {}
        parsed = json.loads(stdout)
        return parsed if isinstance(parsed, dict) else {"data": parsed}

    @staticmethod
    def _busy_blocks(freebusy_payload: dict[str, Any], calendar_id: str) -> list[Any]:
        calendars = freebusy_payload.get("calendars")
        if not isinstance(calendars, dict):
            return []
        cal = calendars.get(calendar_id)
        if not isinstance(cal, dict) and calendars:
            cal = next(iter(calendars.values()))
        if not isinstance(cal, dict):
            return []
        busy = cal.get("busy", [])
        return busy if isinstance(busy, list) else []

    @staticmethod
    def _event_id(create_payload: dict[str, Any]) -> str:
        for key in ("id", "eventId", "event_id"):
            value = create_payload.get(key)
            if isinstance(value, str) and value:
                return value
        event = create_payload.get("event")
        if isinstance(event, dict):
            value = event.get("id")
            if isinstance(value, str) and value:
                return value
        raise RuntimeError(f"gog calendar create returned no event id: {create_payload}")

    def appointment_range(self, appointment_date: str, appointment_time: str) -> AppointmentRange:
        time_value = appointment_time.strip()
        if re.fullmatch(r"\d{2}:\d{2}", time_value):
            time_value = f"{time_value}:00"
        start = datetime.fromisoformat(f"{appointment_date.strip()}T{time_value}")
        if start.tzinfo is None:
            start = start.replace(tzinfo=self.timezone)
        end = start + timedelta(minutes=APPOINTMENT_DURATION_MINUTES)
        return AppointmentRange(start=start, end=end)

    def is_available(self, appointment: AppointmentRange) -> bool:
        payload = self._run(
            "freebusy",
            "--calendars",
            self.calendar_id,
            "--from",
            appointment.start.isoformat(),
            "--to",
            appointment.end.isoformat(),
        )
        return not self._busy_blocks(payload, self.calendar_id)

    def create_event(
        self,
        booking: dict[str, str],
        appointment: AppointmentRange,
        call_id: str,
        intent_label: str,
    ) -> dict[str, Any]:
        patient_name = booking["patient_name"]
        appointment_type = booking.get("appointment_type", "appointment")
        phone_number = booking.get("phone_number", "")
        description = (
            f"{intent_label} via Claudia phone agent.\n"
            f"Patient: {patient_name}\n"
            f"Phone: {phone_number}\n"
            f"AgenTao call id: {call_id}"
        )
        payload = self._run(
            "create",
            self.calendar_id,
            "--summary",
            f"HealthFirst {appointment_type} - {patient_name}",
            "--description",
            description,
            "--from",
            appointment.start.isoformat(),
            "--to",
            appointment.end.isoformat(),
            "--start-timezone",
            self.timezone_name,
            "--end-timezone",
            self.timezone_name,
        )
        return {"id": self._event_id(payload)}

    def update_event(
        self,
        event_id: str,
        booking: dict[str, str],
        appointment: AppointmentRange,
    ) -> dict[str, Any]:
        patient_name = booking["patient_name"]
        appointment_type = booking.get("appointment_type", "appointment")
        self._run(
            "update",
            self.calendar_id,
            event_id,
            "--summary",
            f"HealthFirst {appointment_type} - {patient_name}",
            "--from",
            appointment.start.isoformat(),
            "--to",
            appointment.end.isoformat(),
            "--start-timezone",
            self.timezone_name,
            "--end-timezone",
            self.timezone_name,
        )
        return {"id": event_id}

    def delete_event(self, event_id: str) -> None:
        self._run("delete", self.calendar_id, event_id, "--force")

    def next_available_slots(
        self,
        requested: AppointmentRange,
        count: int = 3,
    ) -> list[dict[str, str]]:
        slots: list[dict[str, str]] = []
        candidate_start = requested.start + timedelta(minutes=APPOINTMENT_DURATION_MINUTES)

        while len(slots) < count:
            if candidate_start.hour < CLINIC_OPEN_HOUR:
                candidate_start = candidate_start.replace(
                    hour=CLINIC_OPEN_HOUR, minute=0, second=0, microsecond=0
                )
            if candidate_start.hour >= CLINIC_CLOSE_HOUR:
                next_day = candidate_start + timedelta(days=1)
                candidate_start = next_day.replace(
                    hour=CLINIC_OPEN_HOUR, minute=0, second=0, microsecond=0
                )

            candidate = AppointmentRange(
                start=candidate_start,
                end=candidate_start + timedelta(minutes=APPOINTMENT_DURATION_MINUTES),
            )
            if self.is_available(candidate):
                slots.append(
                    {
                        "appointment_date": candidate.start.date().isoformat(),
                        "appointment_time": candidate.start.strftime("%H:%M"),
                    }
                )
            candidate_start += timedelta(minutes=APPOINTMENT_DURATION_MINUTES)

        return slots


def make_gog_calendar() -> GogCalendarClient:
    return GogCalendarClient(require_env("GOOGLE_CALENDAR_ID"))


# ---------------------------------------------------------------------------
# WhatsApp via OpenClaw (WhatsApp Web / Baileys — not Meta Business API)
# Docs: https://docs.openclaw.ai/channels/whatsapp
#       https://docs.openclaw.ai/cli/message
# ---------------------------------------------------------------------------


def normalize_e164(phone: str, fallback: str = "") -> str:
    """Normalize a phone number to E.164 for openclaw message send --target."""
    raw = (phone or fallback or "").strip()
    if not raw or raw.lower() in {"unknown", "null", "none", "caller_number"}:
        raw = fallback.strip()
    if not raw:
        raise ValueError("No phone number available for WhatsApp delivery.")

    if raw.startswith("+"):
        digits = re.sub(r"\D", "", raw)
        return f"+{digits}"

    digits = re.sub(r"\D", "", raw)
    if digits.startswith("00"):
        digits = digits[2:]
    # Already includes country code (e.g. 919322958608 for India)
    if len(digits) >= 11:
        return f"+{digits}"
    return f"+{DEFAULT_PHONE_COUNTRY_CODE}{digits}"


def send_whatsapp_via_openclaw(target: str, message: str) -> dict[str, Any]:
    """Send WhatsApp using the OpenClaw channel you linked (requires gateway running)."""
    target_e164 = normalize_e164(target)
    command = [
        "openclaw",
        "message",
        "send",
        "--channel",
        "whatsapp",
        "--target",
        target_e164,
        "--message",
        message[:4096],
        "--json",
    ]
    if WHATSAPP_OPENCLAW_ACCOUNT:
        command.extend(["--account", WHATSAPP_OPENCLAW_ACCOUNT])

    result = subprocess.run(
        command,
        check=False,
        capture_output=True,
        text=True,
        timeout=120,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"openclaw message send failed (exit {result.returncode}): {result.stderr.strip()}"
        )

    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError:
        return {"raw_stdout": result.stdout.strip(), "target": target_e164}


def notify_patient_whatsapp(patient_phone: str, message: str, fallback_phone: str = "") -> bool:
    """Send booking confirmation/update to the patient on WhatsApp."""
    try:
        e164 = normalize_e164(patient_phone, fallback_phone)
        send_whatsapp_via_openclaw(e164, message)
        return True
    except Exception as exc:
        logger.exception("Patient WhatsApp failed: %s", exc)
        return False


def notify_clinic_whatsapp(message: str) -> bool:
    """Optional staff alert when WHATSAPP_CLINIC_NUMBER is configured."""
    if not WHATSAPP_CLINIC_NUMBER:
        return False
    try:
        send_whatsapp_via_openclaw(WHATSAPP_CLINIC_NUMBER, message)
        return True
    except Exception as exc:
        logger.exception("Clinic WhatsApp failed: %s", exc)
        return False


# ---------------------------------------------------------------------------
# Transcript helpers
# ---------------------------------------------------------------------------


def transcript_text(transcript: list[TranscriptLine]) -> str:
    return "\n".join(f"{line.speaker}: {line.text}" for line in transcript if line.text)


def record_transcript_line(transcript: list[TranscriptLine], speaker: str, text: str) -> None:
    clean_text = text.strip()
    if not clean_text:
        return

    if transcript and transcript[-1].speaker == speaker:
        previous = transcript[-1].text
        separator = "" if clean_text[:1] in {".", ",", "!", "?", ";", ":"} else " "
        transcript[-1] = TranscriptLine(speaker, f"{previous}{separator}{clean_text}")
    else:
        transcript.append(TranscriptLine(speaker, clean_text))

    if LOG_TRANSCRIPTS:
        logger.info("Transcript [%s]: %s", speaker, clean_text)


# ---------------------------------------------------------------------------
# Gemini Live voice bridge (AgenTao ↔ Gemini)
# ---------------------------------------------------------------------------


async def run_gemini_voice_call(
    call: ActiveCall,
    gemini_client: genai.Client,
    system_prompt: str = CLAUDIA_SYSTEM_PROMPT,
) -> list[TranscriptLine]:
    """Stream AgenTao PCM to Gemini Live and play Claudia's audio responses back."""
    transcript: list[TranscriptLine] = []
    call_ended = asyncio.Event()

    @call.on(events.CALL_TERMINATED)
    def on_terminated() -> None:
        call_ended.set()

    await call.answer()

    config = types.LiveConnectConfig(
        response_modalities=["AUDIO"],
        system_instruction=system_prompt,
        input_audio_transcription=types.AudioTranscriptionConfig(),
        output_audio_transcription=types.AudioTranscriptionConfig(),
        speech_config=types.SpeechConfig(
            language_code="en-US",
            voice_config=types.VoiceConfig(
                prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name="Kore")
            ),
        ),
    )

    async with gemini_client.aio.live.connect(model=GEMINI_MODEL, config=config) as session:
        await session.send_client_content(
            turns=types.Content(
                role="user",
                parts=[types.Part(text="The phone call is connected. Begin the conversation now.")],
            ),
            turn_complete=True,
        )

        async def stream_to_gemini() -> None:
            async for chunk in call.audio_stream():
                await session.send_realtime_input(
                    audio=types.Blob(data=chunk, mime_type=AUDIO_MIME_TYPE)
                )

        async def receive_from_gemini() -> None:
            while not call_ended.is_set():
                async for response in session.receive():
                    content = response.server_content
                    if not content:
                        continue

                    if content.input_transcription and content.input_transcription.text:
                        record_transcript_line(transcript, "PATIENT", content.input_transcription.text)

                    if content.output_transcription and content.output_transcription.text:
                        record_transcript_line(transcript, "CLAUDIA", content.output_transcription.text)

                    if content.interrupted:
                        if hasattr(call, "interrupt"):
                            await call.interrupt()
                        else:
                            await call.clear_send_audio_buffer()
                        break

                    if content.model_turn:
                        for part in content.model_turn.parts:
                            if part.inline_data and part.inline_data.data:
                                await call.send_audio(part.inline_data.data)

        async def wait_for_call_end() -> None:
            await call_ended.wait()

        tasks = [
            asyncio.create_task(stream_to_gemini()),
            asyncio.create_task(receive_from_gemini()),
            asyncio.create_task(wait_for_call_end()),
        ]
        done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        for task in done:
            task.result()

    return transcript


# ---------------------------------------------------------------------------
# OpenClaw CLI wrapper
# ---------------------------------------------------------------------------


class OpenClawClient:
    """Runs documented `openclaw agent` turns (OpenClaw has no API key of its own)."""

    def __init__(self, agent: str = OPENCLAW_AGENT, timeout: int = OPENCLAW_TIMEOUT_SECONDS):
        self.agent = agent
        self.timeout = timeout

    async def run_json(self, session_key: str, message: str) -> dict[str, Any]:
        return await asyncio.to_thread(self._run_json_sync, session_key, message)

    def _run_json_sync(self, session_key: str, message: str) -> dict[str, Any]:
        env = os.environ.copy()
        env["GEMINI_API_KEY"] = env.get("GEMINI_API_KEY") or require_env("GOOGLE_API_KEY")
        env["GOOGLE_API_KEY"] = require_env("GOOGLE_API_KEY")

        command = [
            "openclaw",
            "agent",
            "--agent",
            self.agent,
            "--local",
            "--session-key",
            session_key,
            "--message",
            message,
            "--json",
            "--timeout",
            str(self.timeout),
        ]
        result = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            timeout=self.timeout + 30,
            env=env,
        )
        if result.returncode != 0:
            raise RuntimeError(
                f"OpenClaw command failed (exit {result.returncode}): {result.stderr.strip()}"
            )
        return parse_openclaw_json(result.stdout)


def parse_openclaw_json(stdout: str) -> dict[str, Any]:
    outer = json.loads(stdout)
    candidate_texts: list[str] = []

    for container in (outer, outer.get("result") if isinstance(outer, dict) else None):
        if not isinstance(container, dict):
            continue
        for key in ("text", "message", "content", "output"):
            value = container.get(key)
            if isinstance(value, str):
                candidate_texts.append(value)
        payloads = container.get("payloads")
        if isinstance(payloads, list):
            for payload in payloads:
                if isinstance(payload, dict) and isinstance(payload.get("text"), str):
                    candidate_texts.append(payload["text"])

    for text in candidate_texts:
        parsed = extract_json_object(text)
        if parsed is not None:
            return parsed

    if isinstance(outer, dict):
        return outer
    raise RuntimeError("OpenClaw returned JSON that could not be interpreted as an object.")


def extract_json_object(text: str) -> dict[str, Any] | None:
    text = text.strip()
    if not text:
        return None
    try:
        parsed = json.loads(text)
        return parsed if isinstance(parsed, dict) else None
    except json.JSONDecodeError:
        pass

    match = re.search(r"\{.*\}", text, flags=re.DOTALL)
    if not match:
        return None
    parsed = json.loads(match.group(0))
    return parsed if isinstance(parsed, dict) else None


# ---------------------------------------------------------------------------
# Post-call: OpenClaw extraction + execution
# ---------------------------------------------------------------------------


async def extract_call_with_openclaw(
    openclaw: OpenClawClient,
    call: ActiveCall,
    rendered_transcript: str,
) -> dict[str, Any]:
    """Ask OpenClaw to extract intent and structured fields from the transcript."""
    today = datetime.now(ZoneInfo(CLINIC_TIMEZONE)).date().isoformat()
    message = f"""
You are Claudia's post-call extraction worker for HealthFirst Clinic.

Rules:
- Use OpenClaw with GOOGLE_API_KEY / GEMINI_API_KEY routing only. OpenClaw has no API key of its own.
- Read the transcript and extract structured data only.
- Do not access Google Calendar, bookings.json, or WhatsApp in this step.
- Today is {today}; resolve relative dates to YYYY-MM-DD.

Intents (each block must include WhatsApp fields below):
- book: patient_name, phone_number, appointment_date, appointment_time, appointment_type
- reschedule: patient_name, existing_appointment_date, new_appointment_date, new_appointment_time
- cancel: patient_name, existing_appointment_date

WhatsApp fields on book / reschedule / cancel (required on the active intent block):
- wants_patient_whatsapp: true if patient wants a WhatsApp summary; false if they declined or it was not discussed
- caller_is_whatsapp: true if patient said the calling number is their WhatsApp; false if they said it is not; null if unclear
- whatsapp_number: E.164 or local digits only when patient gave a different WhatsApp number; null otherwise

Return only one JSON object:
{{
  "status": "extracted" | "needs_human_review",
  "intent": "book" | "reschedule" | "cancel" | null,
  "book": null | {{
    "patient_name": "string",
    "phone_number": "string",
    "appointment_date": "YYYY-MM-DD",
    "appointment_time": "HH:MM",
    "appointment_type": "general checkup|specialist|follow-up",
    "wants_patient_whatsapp": true | false,
    "caller_is_whatsapp": true | false | null,
    "whatsapp_number": "string | null"
  }},
  "reschedule": null | {{
    "patient_name": "string",
    "existing_appointment_date": "YYYY-MM-DD",
    "new_appointment_date": "YYYY-MM-DD",
    "new_appointment_time": "HH:MM",
    "wants_patient_whatsapp": true | false,
    "caller_is_whatsapp": true | false | null,
    "whatsapp_number": "string | null"
  }},
  "cancel": null | {{
    "patient_name": "string",
    "existing_appointment_date": "YYYY-MM-DD",
    "wants_patient_whatsapp": true | false,
    "caller_is_whatsapp": true | false | null,
    "whatsapp_number": "string | null"
  }},
  "notes": "short operational note"
}}

AgenTao metadata:
- call_id: {call.call_id}
- caller_number: {call.caller_number}
- callee_number: {call.callee_number}

Transcript:
{rendered_transcript}
""".strip()
    return await openclaw.run_json(f"healthfirst-claudia-extract-{call.call_id}", message)


async def execute_with_openclaw(
    openclaw: OpenClawClient,
    call: ActiveCall,
    extraction: dict[str, Any],
) -> dict[str, Any]:
    """Ask OpenClaw to orchestrate gog Calendar, bookings.json, and WhatsApp."""
    calendar_id = require_env("GOOGLE_CALENDAR_ID")
    clinic_line = (
        f"Optional clinic ops number: {WHATSAPP_CLINIC_NUMBER}"
        if WHATSAPP_CLINIC_NUMBER
        else "No clinic ops number configured (patient WhatsApp only)."
    )

    message = f"""
You are Claudia's post-call execution worker for HealthFirst Clinic.

Rules:
- Use OpenClaw with GOOGLE_API_KEY / GEMINI_API_KEY routing only. OpenClaw has no API key of its own.
- Do NOT use Meta WhatsApp Business API tokens. WhatsApp is linked via OpenClaw (WhatsApp Web).
- Do NOT use Google service-account JSON or the Python google-api-python-client. Calendar is **gog only**.
- Return one final JSON object when finished.

Google Calendar — use **gog** CLI only (calendar id: "{calendar_id}"):
  gog --json calendar freebusy --calendars "{calendar_id}" --from <RFC3339> --to <RFC3339>
  gog --json calendar create {calendar_id} --summary "..." --from <RFC3339> --to <RFC3339> --description "..."
  gog --json calendar update {calendar_id} <eventId> --from <RFC3339> --to <RFC3339>
  gog --json calendar delete {calendar_id} <eventId> --force

Or run the bundled internal helpers (they call gog + update bookings.json + WhatsApp):
  python {SCRIPT_PATH} internal create-booking --payload '<json>' --call-id {call.call_id} --fallback-phone {call.caller_number}
  python {SCRIPT_PATH} internal reschedule-booking --payload '<json>' --fallback-phone {call.caller_number}
  python {SCRIPT_PATH} internal cancel-booking --payload '<json>' --fallback-phone {call.caller_number}

bookings.json path: {BOOKINGS_PATH} (read/write via file tools or internal commands above)

WhatsApp (gateway must be running):
  openclaw message send --channel whatsapp --target +<E164> --message "<text>"
- **Clinic ({clinic_line}):** ALWAYS send full booking details for team review and confirmation.
- **Patient:** ONLY if wants_patient_whatsapp is true in extraction. Use whatsapp_number, or caller if caller_is_whatsapp, or phone_number. Caller fallback: {call.caller_number}. Patient message is a receipt/summary — team confirms later.

Extraction JSON:
{json.dumps(extraction, indent=2)}

Execution rules by intent:
- book: gog freebusy → if available gog create + bookings.json (status pending_review) + clinic WhatsApp + patient WhatsApp only if wants_patient_whatsapp
- if busy: alternatives + clinic WhatsApp + optional patient WhatsApp
- reschedule / cancel: same patient WhatsApp rules; clinic always gets full details

Return only JSON:
{{
  "status": "confirmed" | "rescheduled" | "cancelled" | "unavailable" | "needs_human_review" | "failed",
  "booking": null | {{ "id", "patient_name", "phone_number", "appointment_date", "appointment_time", "appointment_type", "status", "calendar_event_id" }},
  "next_available_slots": [],
  "patient_whatsapp_sent": true | false,
  "clinic_whatsapp_sent": true | false,
  "notes": "short operational note"
}}
""".strip()
    return await openclaw.run_json(f"healthfirst-claudia-execute-{call.call_id}", message)


def normalize_phone(value: str, fallback: str) -> str:
    cleaned = value.strip()
    if not cleaned or cleaned.lower() in {"unknown", "null", "none", "caller_number"}:
        return fallback
    return cleaned


def parse_bool_field(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"true", "yes", "y", "1"}:
            return True
        if lowered in {"false", "no", "n", "0"}:
            return False
    return None


@dataclass(frozen=True)
class WhatsAppPrefs:
    wants_patient: bool
    patient_target: str | None


def resolve_whatsapp_prefs(block: dict[str, Any], caller_number: str) -> WhatsAppPrefs:
    """Map extraction WhatsApp fields to whether/where to send a patient receipt."""
    if parse_bool_field(block.get("wants_patient_whatsapp")) is not True:
        return WhatsAppPrefs(False, None)

    explicit = str(block.get("whatsapp_number") or "").strip()
    if explicit and explicit.lower() not in {"null", "none", "unknown"}:
        return WhatsAppPrefs(True, explicit)

    if parse_bool_field(block.get("caller_is_whatsapp")) is True:
        return WhatsAppPrefs(True, caller_number)

    phone = str(block.get("phone_number") or "").strip()
    if phone and phone.lower() not in {"unknown", "null", "none", "caller_number"}:
        return WhatsAppPrefs(True, phone)

    return WhatsAppPrefs(True, caller_number)


def maybe_notify_patient_whatsapp(
    prefs: WhatsAppPrefs,
    message: str,
    fallback_phone: str,
) -> bool:
    if not prefs.wants_patient or not prefs.patient_target:
        return False
    return notify_patient_whatsapp(prefs.patient_target, message, fallback_phone)


def format_clinic_review_message(
    action: str,
    *,
    patient_name: str,
    phone_number: str,
    call_id: str,
    outcome: str,
    booking_id: str | None = None,
    appointment_date: str | None = None,
    appointment_time: str | None = None,
    appointment_type: str | None = None,
    existing_date: str | None = None,
    new_date: str | None = None,
    new_time: str | None = None,
    calendar_event_id: str | None = None,
    wants_patient_whatsapp: bool = False,
    patient_whatsapp_target: str | None = None,
    alternatives: list[dict[str, str]] | None = None,
    notes: str = "",
) -> str:
    """Full details for clinic staff to review and confirm."""
    lines = [
        "HealthFirst Clinic — please review & confirm",
        f"Action: {action}",
        f"Outcome: {outcome}",
        f"Patient: {patient_name}",
        f"Contact phone: {phone_number}",
        f"AgenTao call: {call_id}",
    ]
    if appointment_type:
        lines.append(f"Appointment type: {appointment_type}")
    if appointment_date and appointment_time:
        lines.append(f"Requested slot: {appointment_date} at {appointment_time}")
    if existing_date:
        lines.append(f"Existing appointment date: {existing_date}")
    if new_date and new_time:
        lines.append(f"New slot: {new_date} at {new_time}")
    if booking_id:
        lines.append(f"Booking id: {booking_id}")
    if calendar_event_id:
        lines.append(f"Calendar event id: {calendar_event_id}")
    lines.append(
        f"Patient WhatsApp receipt: {'yes' if wants_patient_whatsapp else 'no'}"
        + (f" → {patient_whatsapp_target}" if wants_patient_whatsapp and patient_whatsapp_target else "")
    )
    if alternatives:
        lines.append("Suggested alternatives:")
        for slot in alternatives:
            lines.append(f"  - {slot['appointment_date']} at {slot['appointment_time']}")
    if notes:
        lines.append(f"Notes: {notes}")
    lines.append("Please confirm with the patient when ready.")
    return "\n".join(lines)


def format_patient_receipt(
    patient_name: str,
    *,
    action: str,
    appointment_date: str | None = None,
    appointment_time: str | None = None,
    appointment_type: str | None = None,
    existing_date: str | None = None,
    new_date: str | None = None,
    new_time: str | None = None,
    alternatives: list[dict[str, str]] | None = None,
) -> str:
    """Patient-facing WhatsApp summary — receipt only; team confirms later."""
    lines = [
        f"Hi {patient_name}, thank you for calling HealthFirst Clinic.",
        f"Here is a summary of your {action} request:",
    ]
    if appointment_type:
        lines.append(f"Type: {appointment_type}")
    if appointment_date and appointment_time:
        lines.append(f"Date: {appointment_date}")
        lines.append(f"Time: {appointment_time}")
    if existing_date:
        lines.append(f"Previous date: {existing_date}")
    if new_date and new_time:
        lines.append(f"New date: {new_date}")
        lines.append(f"New time: {new_time}")
    if alternatives:
        lines.append("Suggested times:")
        for slot in alternatives:
            lines.append(f"- {slot['appointment_date']} at {slot['appointment_time']}")
    lines.append("Our team will review and send a final confirmation shortly.")
    return "\n".join(lines)


def build_booking_record(
    fields: dict[str, str],
    event_id: str,
    prefs: WhatsAppPrefs,
    status: str = "pending_review",
) -> dict[str, str]:
    return {
        "id": str(uuid.uuid4()),
        "patient_name": fields["patient_name"],
        "phone_number": fields.get("phone_number", ""),
        "appointment_date": fields["appointment_date"],
        "appointment_time": fields["appointment_time"][:5],
        "appointment_type": fields.get("appointment_type", "general checkup"),
        "status": status,
        "calendar_event_id": event_id,
        "wants_patient_whatsapp": prefs.wants_patient,
        "whatsapp_number": prefs.patient_target or "",
    }


def _whatsapp_result(patient_sent: bool, clinic_sent: bool) -> dict[str, Any]:
    return {
        "patient_whatsapp_sent": patient_sent,
        "clinic_whatsapp_sent": clinic_sent,
        "whatsapp_sent": patient_sent or clinic_sent,
    }


def execute_booking_intent(
    fields: dict[str, str],
    whatsapp_block: dict[str, Any],
    calendar: GogCalendarClient,
    store: BookingStore,
    call_id: str,
    fallback_phone: str = "",
) -> dict[str, Any]:
    """Deterministic booking via gog + bookings.json + OpenClaw WhatsApp."""
    prefs = resolve_whatsapp_prefs(whatsapp_block, fallback_phone)
    appointment = calendar.appointment_range(fields["appointment_date"], fields["appointment_time"])
    if not calendar.is_available(appointment):
        alternatives = calendar.next_available_slots(appointment)
        patient_msg = format_patient_receipt(
            fields["patient_name"],
            action="appointment",
            appointment_date=fields["appointment_date"],
            appointment_time=fields["appointment_time"],
            appointment_type=fields.get("appointment_type"),
            alternatives=alternatives,
        )
        clinic_msg = format_clinic_review_message(
            "book — slot unavailable",
            patient_name=fields["patient_name"],
            phone_number=fields.get("phone_number", ""),
            call_id=call_id,
            outcome="unavailable",
            appointment_date=fields["appointment_date"],
            appointment_time=fields["appointment_time"],
            appointment_type=fields.get("appointment_type"),
            wants_patient_whatsapp=prefs.wants_patient,
            patient_whatsapp_target=prefs.patient_target,
            alternatives=alternatives,
        )
        wa = _whatsapp_result(
            maybe_notify_patient_whatsapp(prefs, patient_msg, fallback_phone),
            notify_clinic_whatsapp(clinic_msg),
        )
        return {
            "status": "unavailable",
            "booking": None,
            "next_available_slots": alternatives,
            "notes": "Slot busy; clinic notified. Patient WhatsApp only if requested on the call.",
            **wa,
        }

    event = calendar.create_event(fields, appointment, call_id, "New booking")
    record = build_booking_record(fields, str(event["id"]), prefs)
    store.append(record)

    patient_msg = format_patient_receipt(
        record["patient_name"],
        action="appointment",
        appointment_date=record["appointment_date"],
        appointment_time=record["appointment_time"],
        appointment_type=record["appointment_type"],
    )
    clinic_msg = format_clinic_review_message(
        "book — new appointment",
        patient_name=record["patient_name"],
        phone_number=record["phone_number"],
        call_id=call_id,
        outcome="pending_review",
        booking_id=record["id"],
        appointment_date=record["appointment_date"],
        appointment_time=record["appointment_time"],
        appointment_type=record["appointment_type"],
        calendar_event_id=record["calendar_event_id"],
        wants_patient_whatsapp=prefs.wants_patient,
        patient_whatsapp_target=prefs.patient_target,
    )
    wa = _whatsapp_result(
        maybe_notify_patient_whatsapp(prefs, patient_msg, fallback_phone),
        notify_clinic_whatsapp(clinic_msg),
    )
    return {
        "status": "pending_review",
        "booking": record,
        "next_available_slots": [],
        "notes": "Calendar hold created; clinic notified to confirm. Patient receipt only if they opted in.",
        **wa,
    }


def execute_reschedule_intent(
    fields: dict[str, str],
    whatsapp_block: dict[str, Any],
    calendar: GogCalendarClient,
    store: BookingStore,
    call_id: str = "",
    fallback_phone: str = "",
) -> dict[str, Any]:
    prefs = resolve_whatsapp_prefs(whatsapp_block, fallback_phone)
    existing = store.find_active(fields["patient_name"], fields["existing_appointment_date"])
    if not existing:
        return {
            "status": "needs_human_review",
            "booking": None,
            "next_available_slots": [],
            "patient_whatsapp_sent": False,
            "clinic_whatsapp_sent": False,
            "whatsapp_sent": False,
            "notes": "No matching booking found for reschedule.",
        }

    new_range = calendar.appointment_range(
        fields["new_appointment_date"],
        fields["new_appointment_time"],
    )
    if not calendar.is_available(new_range):
        alternatives = calendar.next_available_slots(new_range)
        patient_msg = format_patient_receipt(
            fields["patient_name"],
            action="reschedule",
            existing_date=fields["existing_appointment_date"],
            new_date=fields["new_appointment_date"],
            new_time=fields["new_appointment_time"],
            alternatives=alternatives,
        )
        clinic_msg = format_clinic_review_message(
            "reschedule — slot unavailable",
            patient_name=fields["patient_name"],
            phone_number=existing.get("phone_number", ""),
            call_id=call_id,
            outcome="unavailable",
            booking_id=existing["id"],
            existing_date=fields["existing_appointment_date"],
            new_date=fields["new_appointment_date"],
            new_time=fields["new_appointment_time"],
            wants_patient_whatsapp=prefs.wants_patient,
            patient_whatsapp_target=prefs.patient_target,
            alternatives=alternatives,
        )
        wa = _whatsapp_result(
            maybe_notify_patient_whatsapp(prefs, patient_msg, fallback_phone),
            notify_clinic_whatsapp(clinic_msg),
        )
        return {
            "status": "unavailable",
            "booking": existing,
            "next_available_slots": alternatives,
            "notes": "Reschedule slot busy; clinic notified.",
            **wa,
        }

    event_id = str(existing["calendar_event_id"])
    updated_fields = {
        "patient_name": existing["patient_name"],
        "phone_number": existing.get("phone_number", ""),
        "appointment_type": existing.get("appointment_type", "general checkup"),
        "appointment_date": fields["new_appointment_date"],
        "appointment_time": fields["new_appointment_time"],
    }
    calendar.update_event(event_id, updated_fields, new_range)
    updated = store.update_record(
        existing["id"],
        {
            "appointment_date": fields["new_appointment_date"],
            "appointment_time": fields["new_appointment_time"][:5],
            "status": "pending_review",
            "wants_patient_whatsapp": prefs.wants_patient,
            "whatsapp_number": prefs.patient_target or "",
        },
    )

    patient_msg = format_patient_receipt(
        updated_fields["patient_name"],
        action="reschedule",
        existing_date=fields["existing_appointment_date"],
        new_date=fields["new_appointment_date"],
        new_time=fields["new_appointment_time"],
    )
    clinic_msg = format_clinic_review_message(
        "reschedule",
        patient_name=updated_fields["patient_name"],
        phone_number=updated_fields["phone_number"],
        call_id=call_id,
        outcome="pending_review",
        booking_id=existing["id"],
        existing_date=fields["existing_appointment_date"],
        new_date=fields["new_appointment_date"],
        new_time=fields["new_appointment_time"],
        calendar_event_id=event_id,
        wants_patient_whatsapp=prefs.wants_patient,
        patient_whatsapp_target=prefs.patient_target,
    )
    wa = _whatsapp_result(
        maybe_notify_patient_whatsapp(prefs, patient_msg, fallback_phone),
        notify_clinic_whatsapp(clinic_msg),
    )
    return {
        "status": "pending_review",
        "booking": updated,
        "next_available_slots": [],
        "notes": "Reschedule recorded; clinic to confirm. Patient receipt only if opted in.",
        **wa,
    }


def execute_cancel_intent(
    fields: dict[str, str],
    whatsapp_block: dict[str, Any],
    calendar: GogCalendarClient,
    store: BookingStore,
    call_id: str = "",
    fallback_phone: str = "",
) -> dict[str, Any]:
    prefs = resolve_whatsapp_prefs(whatsapp_block, fallback_phone)
    existing = store.find_active(fields["patient_name"], fields["existing_appointment_date"])
    if not existing:
        return {
            "status": "needs_human_review",
            "booking": None,
            "next_available_slots": [],
            "patient_whatsapp_sent": False,
            "clinic_whatsapp_sent": False,
            "whatsapp_sent": False,
            "notes": "No matching booking found for cancellation.",
        }

    calendar.delete_event(str(existing["calendar_event_id"]))
    updated = store.update_record(
        existing["id"],
        {
            "status": "cancelled",
            "wants_patient_whatsapp": prefs.wants_patient,
            "whatsapp_number": prefs.patient_target or "",
        },
    )

    patient_msg = format_patient_receipt(
        fields["patient_name"],
        action="cancellation",
        existing_date=fields["existing_appointment_date"],
    )
    clinic_msg = format_clinic_review_message(
        "cancel",
        patient_name=fields["patient_name"],
        phone_number=existing.get("phone_number", ""),
        call_id=call_id,
        outcome="cancelled",
        booking_id=existing["id"],
        existing_date=fields["existing_appointment_date"],
        calendar_event_id=str(existing.get("calendar_event_id", "")),
        wants_patient_whatsapp=prefs.wants_patient,
        patient_whatsapp_target=prefs.patient_target,
    )
    wa = _whatsapp_result(
        maybe_notify_patient_whatsapp(prefs, patient_msg, fallback_phone),
        notify_clinic_whatsapp(clinic_msg),
    )
    return {
        "status": "cancelled",
        "booking": updated,
        "next_available_slots": [],
        "notes": "Cancellation processed; clinic notified. Patient receipt only if opted in.",
        **wa,
    }


def execute_extraction_locally(
    extraction: dict[str, Any],
    call: ActiveCall,
) -> dict[str, Any]:
    """Run gog Calendar / bookings.json / WhatsApp when OpenClaw execute turn fails."""
    if extraction.get("status") != "extracted":
        return {
            "status": "needs_human_review",
            "booking": None,
            "next_available_slots": [],
            "patient_whatsapp_sent": False,
            "clinic_whatsapp_sent": False,
            "whatsapp_sent": False,
            "notes": extraction.get("notes", "Extraction incomplete."),
        }

    calendar = make_gog_calendar()
    store = BookingStore()
    intent = extraction.get("intent")

    if intent == "book":
        book = extraction.get("book")
        if not isinstance(book, dict):
            return {
                "status": "needs_human_review",
                "booking": None,
                "next_available_slots": [],
                "whatsapp_sent": False,
                "notes": "Missing book payload.",
            }
        fields = {
            "patient_name": str(book["patient_name"]).strip(),
            "phone_number": normalize_phone(str(book.get("phone_number", "")), call.caller_number),
            "appointment_date": str(book["appointment_date"]).strip(),
            "appointment_time": str(book["appointment_time"]).strip(),
            "appointment_type": str(book.get("appointment_type", "general checkup")).strip(),
        }
        return execute_booking_intent(
            fields, book, calendar, store, call.call_id, call.caller_number
        )

    if intent == "reschedule":
        payload = extraction.get("reschedule")
        if not isinstance(payload, dict):
            return {
                "status": "needs_human_review",
                "booking": None,
                "next_available_slots": [],
                "whatsapp_sent": False,
                "notes": "Missing reschedule payload.",
            }
        fields = {
            key: str(payload[key]).strip()
            for key in payload
            if key
            not in {"wants_patient_whatsapp", "caller_is_whatsapp", "whatsapp_number"}
        }
        return execute_reschedule_intent(
            fields, payload, calendar, store, call.call_id, call.caller_number
        )

    if intent == "cancel":
        payload = extraction.get("cancel")
        if not isinstance(payload, dict):
            return {
                "status": "needs_human_review",
                "booking": None,
                "next_available_slots": [],
                "whatsapp_sent": False,
                "notes": "Missing cancel payload.",
            }
        fields = {
            key: str(payload[key]).strip()
            for key in payload
            if key
            not in {"wants_patient_whatsapp", "caller_is_whatsapp", "whatsapp_number"}
        }
        return execute_cancel_intent(
            fields, payload, calendar, store, call.call_id, call.caller_number
        )

    return {
        "status": "needs_human_review",
        "booking": None,
        "next_available_slots": [],
        "whatsapp_sent": False,
        "notes": "Unknown intent.",
    }


async def process_call_with_openclaw(
    openclaw: OpenClawClient,
    call: ActiveCall,
    transcript: list[TranscriptLine],
) -> dict[str, Any]:
    """Full post-call pipeline: OpenClaw extract → OpenClaw execute (with local fallback)."""
    rendered = transcript_text(transcript)
    if not rendered.strip():
        raise RuntimeError("Gemini did not return a transcript for OpenClaw to process.")

    logger.info("Call %s: OpenClaw extraction", call.call_id)
    extraction = await extract_call_with_openclaw(openclaw, call, rendered)

    try:
        logger.info("Call %s: OpenClaw execution", call.call_id)
        result = await execute_with_openclaw(openclaw, call, extraction)
        if result.get("status") in {
            "confirmed",
            "pending_review",
            "rescheduled",
            "cancelled",
            "unavailable",
            "needs_human_review",
        }:
            return result
        logger.warning("Call %s: OpenClaw execution returned unexpected payload; using local fallback", call.call_id)
    except Exception as exc:
        logger.warning("Call %s: OpenClaw execution failed (%s); using local fallback", call.call_id, exc)

    return execute_extraction_locally(extraction, call)


# ---------------------------------------------------------------------------
# Internal CLI tools (gog + bookings.json + WhatsApp — for OpenClaw exec)
# ---------------------------------------------------------------------------


def run_internal_tool(argv: list[str]) -> int:
    """Entry point for `python booking_agent.py internal <tool> ...`."""
    ensure_gog_cli()
    parser = argparse.ArgumentParser(description="HealthFirst internal post-call tools (gog)")
    sub = parser.add_subparsers(dest="tool", required=True)

    create = sub.add_parser("create-booking")
    create.add_argument("--payload", required=True)
    create.add_argument("--call-id", default="manual")
    create.add_argument("--fallback-phone", default="")

    resched = sub.add_parser("reschedule-booking")
    resched.add_argument("--payload", required=True)
    resched.add_argument("--fallback-phone", default="")

    cancel = sub.add_parser("cancel-booking")
    cancel.add_argument("--payload", required=True)
    cancel.add_argument("--fallback-phone", default="")

    wa = sub.add_parser("whatsapp")
    wa.add_argument("--target", required=True, help="E.164 patient or clinic number")
    wa.add_argument("--text", required=True)

    args, _unknown = parser.parse_known_args(argv)
    calendar = make_gog_calendar()
    store = BookingStore()

    if args.tool == "create-booking":
        payload = json.loads(args.payload)
        fields = {
            k: str(v).strip()
            for k, v in payload.items()
            if k not in {"wants_patient_whatsapp", "caller_is_whatsapp", "whatsapp_number"}
        }
        result = execute_booking_intent(
            fields, payload, calendar, store, args.call_id, args.fallback_phone
        )
        print(json.dumps(result))
        return 0

    if args.tool == "reschedule-booking":
        payload = json.loads(args.payload)
        fields = {
            k: str(v).strip()
            for k, v in payload.items()
            if k not in {"wants_patient_whatsapp", "caller_is_whatsapp", "whatsapp_number"}
        }
        result = execute_reschedule_intent(
            fields, payload, calendar, store, args.call_id, args.fallback_phone
        )
        print(json.dumps(result))
        return 0

    if args.tool == "cancel-booking":
        payload = json.loads(args.payload)
        fields = {
            k: str(v).strip()
            for k, v in payload.items()
            if k not in {"wants_patient_whatsapp", "caller_is_whatsapp", "whatsapp_number"}
        }
        result = execute_cancel_intent(
            fields, payload, calendar, store, args.call_id, args.fallback_phone
        )
        print(json.dumps(result))
        return 0

    if args.tool == "whatsapp":
        response = send_whatsapp_via_openclaw(args.target, args.text)
        print(json.dumps({"whatsapp_sent": True, "response": response}))
        return 0

    parser.error(f"Unknown internal tool: {args.tool}")
    return 1


# ---------------------------------------------------------------------------
# AgenTao inbound handler
# ---------------------------------------------------------------------------


async def handle_incoming_call(
    call: ActiveCall,
    gemini_client: genai.Client,
    openclaw: OpenClawClient,
) -> None:
    """Answer the call with Claudia (Gemini Live), then hand off to OpenClaw."""
    transcript = await run_gemini_voice_call(call, gemini_client)
    result = await process_call_with_openclaw(openclaw, call, transcript)
    print(json.dumps({"call_id": call.call_id, "openclaw_result": result}, indent=2))


async def main() -> None:
    ensure_gog_cli()
    gemini_client = make_gemini_client()
    openclaw = OpenClawClient()
    config = make_AgenTao_config()

    async with AgenTaoClient(config) as client:
        @client.on(events.INCOMING_CALL)
        async def on_call(call: ActiveCall) -> None:
            try:
                await handle_incoming_call(call, gemini_client, openclaw)
            except Exception as exc:
                logger.exception("Call %s failed", call.call_id)
                print(f"Call {call.call_id} failed: {exc}", file=sys.stderr)
                await call.disconnect()

        logger.info("HealthFirst Claudia agent listening for inbound AgenTao calls")
        await client.run_forever()


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "internal":
        raise SystemExit(run_internal_tool(sys.argv[2:]))
    asyncio.run(main())
