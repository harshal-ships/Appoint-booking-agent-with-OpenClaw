"""HealthFirst Clinic inbound appointment agent (Claudia).

Responsibility split (do not mix these layers):
- Telcoflow SDK: phone audio in/out only.
- AWS Nova 2 Sonic: real-time voice conversation only.
- OpenClaw: WhatsApp delivery only (no LLM extraction).
- Amazon Nova (Bedrock): Nova Sonic 2 for live voice; Nova text model for post-call JSON extraction.
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
import shutil
import subprocess
import sys
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from nova_sonic_bridge import NovaSonicBridge

import boto3
from botocore.exceptions import ClientError
try:
    from telcoflow_sdk import ActiveCall, TelcoflowClient, TelcoflowClientConfig
except ImportError as exc:
    raise ImportError(
        "telcoflow-sdk is missing or too old (need >= 0.5, recommended 0.27.1). "
        "In your venv run: pip install -r requirements.txt"
    ) from exc
import telcoflow_sdk.events as events

try:
    from dotenv import load_dotenv
except ImportError:
    load_dotenv = None

if load_dotenv is not None:
    load_dotenv()

# ---------------------------------------------------------------------------
# Runtime configuration
# ---------------------------------------------------------------------------

BOOKINGS_PATH = Path(os.getenv("BOOKINGS_PATH", "bookings.json")).resolve()
NOVA_TEXT_MODEL = os.getenv("NOVA_TEXT_MODEL", "amazon.nova-lite-v1:0").strip()
MAX_SLOT_SEARCH_ITERATIONS = int(os.getenv("MAX_SLOT_SEARCH_ITERATIONS", "200"))
LOG_TRANSCRIPTS = os.getenv("LOG_TRANSCRIPTS", "true").lower() in {"1", "true", "yes", "on"}
CLINIC_TIMEZONE = os.getenv("CLINIC_TIMEZONE", "Asia/Kolkata")
APPOINTMENT_DURATION_MINUTES = int(os.getenv("APPOINTMENT_DURATION_MINUTES", "30"))
CLINIC_OPEN_HOUR = int(os.getenv("CLINIC_OPEN_HOUR", "9"))
CLINIC_CLOSE_HOUR = int(os.getenv("CLINIC_CLOSE_HOUR", "17"))
GOG_BIN = os.getenv("GOG_BIN", "gog").strip()
GOG_ACCOUNT = os.getenv("GOG_ACCOUNT", "").strip()
GOG_CLIENT = os.getenv("GOG_CLIENT", "").strip()
GOG_KEYRING_BACKEND = os.getenv("GOG_KEYRING_BACKEND", "").strip()
GOG_KEYRING_PASSWORD = os.getenv("GOG_KEYRING_PASSWORD", "").strip()
DEFAULT_PHONE_COUNTRY_CODE = os.getenv("DEFAULT_PHONE_COUNTRY_CODE", "91").strip()
WHATSAPP_CLINIC_NUMBER = os.getenv("WHATSAPP_CLINIC_NUMBER", "").strip()
WHATSAPP_OPENCLAW_ACCOUNT = os.getenv("WHATSAPP_OPENCLAW_ACCOUNT", "").strip()
AWS_REGION = os.getenv("AWS_REGION", "us-east-1").strip()
NOVA_MODEL_ID = os.getenv("NOVA_MODEL_ID", "amazon.nova-2-sonic-v1:0").strip()
NOVA_VOICE_ID = os.getenv("NOVA_VOICE_ID", "matthew").strip()
NOVA_ROLE_TO_SPEAKER = {"USER": "PATIENT", "ASSISTANT": "CLAUDIA"}

_bedrock_runtime_client = None

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)

# Claudia's live voice instructions — conversation only; no calendar or messaging here.
CLAUDIA_SYSTEM_PROMPT = """Your name is Claudia. You are an administrative appointment assistant for HealthFirst Clinic.
You are warm, calm, professional, and concise. Keep replies to one or two short sentences when possible.

Greet every caller with: Hi, thanks for calling HealthFirst Clinic. I'm Claudia. I can help you book, reschedule, or cancel. What would you like to do?

INTAKE — all intents:
Listen carefully to everything the caller says from their very first response. Many callers introduce themselves and state their full request in one message, for example: "Hi, I'm Harshal, I want to book a general appointment today at 5 PM." When a caller already provides any of these details, treat them as collected and do not ask again:
- Patient full name
- Phone number
- Preferred appointment date and time (or existing appointment date for reschedule or cancel)
- Type of appointment: general checkup, specialist, or follow-up

Only ask for details that are still missing. If the caller gave name, appointment type, date, and time upfront, acknowledge what you heard (for example: "Got it, Harshal — a general checkup today at 5 PM") and ask only for what is missing, usually the phone number.
Never repeat a question for information the caller already clearly stated in this call.
The same upfront-intake rules apply to reschedule and cancel — do not re-ask name or dates already stated.

Required fields by intent:
- Book: patient full name, contact phone number, preferred date, preferred time, appointment type.
- Reschedule: patient name, existing appointment date, new preferred date, new preferred time.
- Cancel: patient name, existing appointment date.

Always map caller wording to one of these appointment types: general checkup, specialist, or follow-up.
If the patient says to use the number they are calling from as their contact phone, accept that.
When collecting a phone number, read it back clearly to confirm.

Do not promise a specific time is available during the call. Say the slot will be confirmed when we process the booking after this call.

READ-BACK (required before ending):
Summarize once in clear order — for booking: name, phone, date, time, and type; for reschedule: name, existing date, new date and time; for cancel: name and existing date.
Then ask: "Is that correct?" Wait for an affirmative response before continuing.

WHATSAPP (optional — never pressure the patient):
After read-back is confirmed, ask about WhatsApp in two short questions, not one combined question:
1. "Would you like your appointment confirmation sent on WhatsApp?"
2. Only if they say yes: "Is the number you're calling from your WhatsApp number?"
- If yes to both, the calling number is their WhatsApp — note that and move on.
- If they want WhatsApp but the calling number is not their WhatsApp, ask: "No problem — which WhatsApp number should I use?"
- If they decline WhatsApp, say "No problem at all" and do not ask again.
- Do not ask about WhatsApp before the read-back is confirmed.

CLOSING:
After they confirm the read-back, tell them their appointment is booked and will be processed after this call.
- If they opted in for WhatsApp, say a confirmation message will be sent shortly.
- If they declined WhatsApp, say our team will call them shortly to confirm.
Do not promise WhatsApp if they declined it.

BOUNDARIES:
You are administrative only. Do not provide medical advice, diagnoses, treatment recommendations, or clinical opinions. If asked medical questions, politely say you cannot help with clinical matters and suggest speaking with a doctor or nurse at the clinic.
If the caller describes an urgent medical emergency (for example chest pain, difficulty breathing, severe bleeding, or someone unconscious), stop the booking flow immediately. Tell them to hang up and call their local emergency number now. Do not continue scheduling until they confirm it is not an emergency.

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


def gog_global_flags() -> list[str]:
    """Global gog flags: --account / --client (see GOG_ACCOUNT, GOG_CLIENT)."""
    flags: list[str] = []
    if GOG_ACCOUNT:
        flags.extend(["--account", GOG_ACCOUNT])
    if GOG_CLIENT:
        flags.extend(["--client", GOG_CLIENT])
    return flags


def build_gog_command(*args: str) -> list[str]:
    """Build a gog argv list with shared global flags before the subcommand."""
    return [GOG_BIN, *gog_global_flags(), *args]


def gog_subprocess_env() -> dict[str, str]:
    """Env for gog subprocesses — headless Linux needs file keyring, not D-Bus."""
    env = os.environ.copy()
    backend = GOG_KEYRING_BACKEND or (
        "file" if sys.platform.startswith("linux") else ""
    )
    if backend:
        env["GOG_KEYRING_BACKEND"] = backend
    if GOG_KEYRING_PASSWORD:
        env["GOG_KEYRING_PASSWORD"] = GOG_KEYRING_PASSWORD
    return env


def _gog_keyring_hint(stderr: str) -> str:
    if not any(
        token in stderr
        for token in ("keyring", "SecretService", "D-Bus", "GOG_KEYRING")
    ):
        return ""
    return (
        " Headless servers cannot use D-Bus SecretService. Add to .env:\n"
        "  GOG_KEYRING_BACKEND=file\n"
        "  GOG_KEYRING_PASSWORD=<choose-a-strong-password>\n"
        "Then on the server (with those exports set):\n"
        "  gog auth keyring file\n"
        "  gog auth credentials set ~/client_secret.json --client healthfirst\n"
        "  gog auth add clinic@gmail.com --client healthfirst --services calendar --remote --step 1\n"
        "  # complete OAuth step 2 with the redirect URL"
    )


def ensure_gog_cli() -> None:
    """Verify the gog Google Calendar CLI is installed and authenticated."""
    try:
        result = subprocess.run(
            build_gog_command("--json", "--no-input", "calendar", "calendars"),
            check=False,
            capture_output=True,
            text=True,
            timeout=60,
            env=gog_subprocess_env(),
        )
    except FileNotFoundError as exc:
        raise RuntimeError(
            "gog CLI not found. Install: openclaw skills install gog  (or brew install steipete/tap/gogcli)"
        ) from exc

    if result.returncode != 0:
        stderr = result.stderr.strip()
        hint = _gog_keyring_hint(stderr)
        if not hint and ("missing --account" in stderr or "GOG_ACCOUNT" in stderr):
            hint = (
                " Set GOG_ACCOUNT in .env to the clinic Google account you authenticated "
                "(e.g. clinic@gmail.com). If you used a named OAuth client during "
                "`gog auth add`, also set GOG_CLIENT. Or run: "
                "`gog auth alias set default <email>` on the server."
            )
        elif not hint and ("auth" in stderr.lower() or "token" in stderr.lower()):
            hint = " Run: gog auth add <clinic@gmail.com> --services calendar"
        raise RuntimeError(
            "gog is not ready for Calendar." + hint + f"\nstderr: {stderr}"
        )


def make_telcoflow_config() -> TelcoflowClientConfig:
    return TelcoflowClientConfig.sandbox(
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
        command = build_gog_command("--json", "--no-input", "calendar", *args)
        result = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            timeout=120,
            env=gog_subprocess_env(),
        )
        if result.returncode != 0:
            stderr = result.stderr.strip() or result.stdout
            hint = _gog_keyring_hint(stderr)
            raise RuntimeError(
                f"gog calendar failed ({' '.join(command)}): {stderr}{hint}"
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
            "--cal",
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
            f"Telcoflow call id: {call_id}"
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

    def _next_clinic_open(self, after: datetime) -> datetime:
        """Next Mon–Fri slot at or after `after`, within clinic hours."""
        candidate = after
        for _ in range(MAX_SLOT_SEARCH_ITERATIONS):
            if candidate.hour < CLINIC_OPEN_HOUR:
                candidate = candidate.replace(
                    hour=CLINIC_OPEN_HOUR, minute=0, second=0, microsecond=0
                )
            elif candidate.hour >= CLINIC_CLOSE_HOUR:
                candidate = (candidate + timedelta(days=1)).replace(
                    hour=CLINIC_OPEN_HOUR, minute=0, second=0, microsecond=0
                )
            if candidate.weekday() < 5:
                return candidate
            days_ahead = 7 - candidate.weekday()
            candidate = (candidate + timedelta(days=days_ahead)).replace(
                hour=CLINIC_OPEN_HOUR, minute=0, second=0, microsecond=0
            )
        return candidate

    def next_available_slots(
        self,
        requested: AppointmentRange,
        count: int = 3,
    ) -> list[dict[str, str]]:
        slots: list[dict[str, str]] = []
        candidate_start = self._next_clinic_open(
            requested.start + timedelta(minutes=APPOINTMENT_DURATION_MINUTES)
        )

        iterations = 0
        while len(slots) < count and iterations < MAX_SLOT_SEARCH_ITERATIONS:
            iterations += 1
            candidate_start = self._next_clinic_open(candidate_start)

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
# Nova Sonic voice bridge (Telcoflow ↔ Bedrock)
# ---------------------------------------------------------------------------


async def run_nova_voice_call(
    call: ActiveCall,
    system_prompt: str = CLAUDIA_SYSTEM_PROMPT,
) -> list[TranscriptLine]:
    """Stream Telcoflow PCM to Nova 2 Sonic and play Claudia's audio responses back."""
    nova_prompt = (
        f"{system_prompt}\n\n"
        "The phone call is connected. Begin the conversation now."
    )
    bridge = NovaSonicBridge(
        model_id=NOVA_MODEL_ID,
        region=AWS_REGION,
        voice_id=NOVA_VOICE_ID,
        system_prompt=nova_prompt,
    )
    result = await bridge.run_call(call)

    transcript: list[TranscriptLine] = []
    for line in result.transcript:
        speaker = NOVA_ROLE_TO_SPEAKER.get(line.role, line.role)
        record_transcript_line(transcript, speaker, line.text)
    return transcript


# ---------------------------------------------------------------------------
# Bedrock Nova text extraction (post-call)
# ---------------------------------------------------------------------------


def _get_bedrock_runtime_client():
    global _bedrock_runtime_client
    if _bedrock_runtime_client is None:
        _bedrock_runtime_client = boto3.client("bedrock-runtime", region_name=AWS_REGION)
    return _bedrock_runtime_client


def _bedrock_nova_extract_json(prompt: str, *, temperature: float) -> dict[str, Any]:
    """Call Amazon Nova text on Bedrock Converse API and parse a JSON object."""
    client = _get_bedrock_runtime_client()
    try:
        response = client.converse(
            modelId=NOVA_TEXT_MODEL,
            messages=[{"role": "user", "content": [{"text": prompt}]}],
            system=[
                {
                    "text": (
                        "You extract structured booking data from phone transcripts. "
                        "Respond with a single valid JSON object only. "
                        "No markdown fences or commentary."
                    )
                }
            ],
            inferenceConfig={"maxTokens": 4096, "temperature": temperature, "topP": 0.9},
        )
    except ClientError as exc:
        raise RuntimeError(f"Bedrock Nova extraction failed: {exc}") from exc

    content = response.get("output", {}).get("message", {}).get("content", [])
    text = ""
    for block in content:
        if isinstance(block, dict) and block.get("text"):
            text = str(block["text"]).strip()
            break
    if not text:
        raise RuntimeError("Bedrock Nova extraction returned empty text.")

    parsed = extract_json_object(text)
    if parsed is None:
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError as exc:
            raise RuntimeError("Bedrock Nova extraction did not return valid JSON.") from exc
    if not isinstance(parsed, dict):
        raise RuntimeError("Bedrock Nova extraction did not return a JSON object.")
    return parsed


# ---------------------------------------------------------------------------
# Post-call: extraction + deterministic execution
# ---------------------------------------------------------------------------


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
    try:
        parsed = json.loads(match.group(0))
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def build_extraction_prompt(call: ActiveCall, rendered_transcript: str) -> str:
    """Extraction instructions for Bedrock Nova text post-call."""
    today = datetime.now(ZoneInfo(CLINIC_TIMEZONE)).date().isoformat()
    return f"""
You are Claudia's post-call extraction worker for HealthFirst Clinic.

Rules:
- Read the transcript and extract structured data only.
- Do not access Google Calendar, bookings.json, or WhatsApp in this step.
- Today is {today}; resolve relative dates to YYYY-MM-DD.
- Claudia's final read-back before ending is the authoritative source when the patient agrees.
- If Claudia summarizes the appointment and asks "Is that correct?" (or similar), and the patient replies with any affirmative phrase such as yes, yeah, correct, all right, alright, okay, that's right, perfect, sounds good, or equivalent, use Claudia's summarized details and return status "extracted".
- Prefer Claudia's final confirmed values over earlier noisy patient speech-to-text, especially for phone numbers and appointment times.
- Only return needs_human_review when a required field is missing or the patient explicitly contradicts or corrects Claudia's final read-back.
- Normalize phone_number to digits only, optionally with a leading +. If Claudia confirmed a specific phone number and the patient agreed, use Claudia's number.
- Use caller_number metadata only when Claudia said the patient is calling from that number or did not state any specific phone number: {call.caller_number or "unknown"}
- Map appointment_type to exactly one of: general checkup, specialist, follow-up.
- If the caller intent is unclear or required fields are missing, set status to needs_human_review.

Intents (each block must include WhatsApp fields below):
- book: patient_name, phone_number, appointment_date, appointment_time, appointment_type
- reschedule: patient_name, existing_appointment_date, new_appointment_date, new_appointment_time
- cancel: patient_name, existing_appointment_date

WhatsApp fields on book / reschedule / cancel (required on the active intent block):
- wants_patient_whatsapp: true if patient agreed to receive confirmation on WhatsApp; false if they declined; if Claudia never asked about WhatsApp, set false and note it in notes
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

Telcoflow metadata:
- call_id: {call.call_id}
- caller_number: {call.caller_number}
- callee_number: {call.callee_number}

Transcript:
{rendered_transcript}
""".strip()


def build_extraction_retry_prompt(call: ActiveCall, rendered_transcript: str) -> str:
    """Second pass when the patient agreed to Claudia's read-back but the first pass was incomplete."""
    today = datetime.now(ZoneInfo(CLINIC_TIMEZONE)).date().isoformat()
    return f"""
The caller already confirmed Claudia's final read-back with yes, all right, okay, or similar.

Extract ONLY from Claudia's final confirmation summary in the transcript.
Use Claudia's values for name, phone, dates, times, appointment type, and intent.
Do not return needs_human_review because of earlier garbled patient speech if the patient agreed to Claudia's summary.
Today is {today}; resolve relative dates to YYYY-MM-DD.
Use caller_number only when Claudia said the patient is calling from that number: {call.caller_number or "unknown"}

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

Transcript:
{rendered_transcript}
""".strip()


def extract_call_with_nova_sync(call: ActiveCall, rendered_transcript: str) -> dict[str, Any]:
    """Extract structured booking fields from the transcript via Bedrock Nova text."""
    prompt = build_extraction_prompt(call, rendered_transcript)
    return normalize_extraction(_bedrock_nova_extract_json(prompt, temperature=0.1))


async def extract_call_with_nova(call: ActiveCall, rendered_transcript: str) -> dict[str, Any]:
    return await asyncio.to_thread(extract_call_with_nova_sync, call, rendered_transcript)


def retry_extract_call_with_nova_sync(call: ActiveCall, rendered_transcript: str) -> dict[str, Any]:
    """Re-extract from Claudia's confirmed read-back when the first pass was incomplete."""
    prompt = build_extraction_retry_prompt(call, rendered_transcript)
    return normalize_extraction(_bedrock_nova_extract_json(prompt, temperature=0))


async def retry_extract_call_with_nova(call: ActiveCall, rendered_transcript: str) -> dict[str, Any]:
    return await asyncio.to_thread(retry_extract_call_with_nova_sync, call, rendered_transcript)


def normalize_extraction(extraction: dict[str, Any]) -> dict[str, Any]:
    """Ensure extraction payloads are usable by the deterministic executor."""
    if extraction.get("status") != "extracted":
        return extraction

    intent = extraction.get("intent")
    if intent not in {"book", "reschedule", "cancel"}:
        extraction["status"] = "needs_human_review"
        extraction["notes"] = extraction.get("notes", "Intent missing or unsupported.")
        return extraction

    block = extraction.get(intent)
    if not isinstance(block, dict):
        extraction["status"] = "needs_human_review"
        extraction["notes"] = extraction.get("notes", f"Missing {intent} payload.")
        return extraction

    required_by_intent = {
        "book": {
            "patient_name",
            "phone_number",
            "appointment_date",
            "appointment_time",
            "appointment_type",
        },
        "reschedule": {
            "patient_name",
            "existing_appointment_date",
            "new_appointment_date",
            "new_appointment_time",
        },
        "cancel": {"patient_name", "existing_appointment_date"},
    }
    missing = [key for key in required_by_intent[intent] if not str(block.get(key, "")).strip()]
    if missing:
        extraction["status"] = "needs_human_review"
        extraction["notes"] = (
            extraction.get("notes", "")
            + f" Missing fields: {', '.join(missing)}"
        ).strip()

    return extraction


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
    """Full booking details for clinic staff."""
    if outcome == "needs_human_review":
        header = "HealthFirst Clinic — needs manual follow-up"
    elif outcome == "unavailable":
        header = "HealthFirst Clinic — action needed"
    else:
        header = "HealthFirst Clinic — booking update"

    lines = [
        header,
        f"Action: {action}",
        f"Outcome: {outcome}",
        f"Patient: {patient_name}",
        f"Contact phone: {phone_number}",
        f"Telcoflow call: {call_id}",
    ]
    if appointment_type:
        lines.append(f"Appointment type: {appointment_type}")
    if appointment_date and appointment_time:
        lines.append(f"Slot: {appointment_date} at {appointment_time}")
    if existing_date:
        lines.append(f"Existing appointment date: {existing_date}")
    if new_date and new_time:
        lines.append(f"New slot: {new_date} at {new_time}")
    if booking_id:
        lines.append(f"Booking id: {booking_id}")
    if calendar_event_id:
        lines.append(f"Calendar event id: {calendar_event_id}")
    if wants_patient_whatsapp:
        lines.append(
            "Patient WhatsApp confirmation: sent"
            + (f" → {patient_whatsapp_target}" if patient_whatsapp_target else "")
        )
    else:
        lines.append(f"Patient WhatsApp: declined — please call {phone_number} to confirm.")
    if alternatives:
        lines.append("Suggested alternatives:")
        for slot in alternatives:
            lines.append(f"  - {slot['appointment_date']} at {slot['appointment_time']}")
    if notes:
        lines.append(f"Notes: {notes}")
    if outcome == "needs_human_review":
        lines.append("Please follow up with the patient manually.")
    elif outcome == "unavailable":
        lines.append("Please contact the patient with alternative times.")
    elif outcome in {"confirmed", "cancelled"}:
        lines.append("Booking is on the calendar. Call the patient only if they declined WhatsApp.")
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
    """Patient-facing WhatsApp confirmation or update."""
    if alternatives:
        lines = [
            f"Hi {patient_name}, thank you for calling HealthFirst Clinic.",
            f"We could not complete your {action} request for the time you wanted.",
        ]
        if appointment_type:
            lines.append(f"Type: {appointment_type}")
        if appointment_date and appointment_time:
            lines.append(f"Requested: {appointment_date} at {appointment_time}")
        if existing_date:
            lines.append(f"Previous date: {existing_date}")
        if new_date and new_time:
            lines.append(f"Requested new time: {new_date} at {new_time}")
        lines.append("Suggested times:")
        for slot in alternatives:
            lines.append(f"- {slot['appointment_date']} at {slot['appointment_time']}")
        lines.append("Please call us back or reply to choose another time.")
        return "\n".join(lines)

    if action == "cancellation":
        lines = [
            f"Hi {patient_name}, your HealthFirst Clinic appointment",
            f"on {existing_date} has been cancelled as requested.",
            "If you would like to rebook, please call us anytime.",
        ]
        return "\n".join(lines)

    if action == "reschedule":
        lines = [
            f"Hi {patient_name}, your HealthFirst Clinic appointment is rescheduled.",
            f"Previous date: {existing_date}",
            f"New date: {new_date}",
            f"New time: {new_time}",
            "We look forward to seeing you.",
        ]
        return "\n".join(lines)

    lines = [
        f"Hi {patient_name}, your HealthFirst Clinic appointment is confirmed.",
    ]
    if appointment_type:
        lines.append(f"Type: {appointment_type}")
    if appointment_date and appointment_time:
        lines.append(f"Date: {appointment_date}")
        lines.append(f"Time: {appointment_time}")
    lines.append("We look forward to seeing you.")
    return "\n".join(lines)


def build_booking_record(
    fields: dict[str, str],
    event_id: str,
    prefs: WhatsAppPrefs,
    status: str = "confirmed",
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


def _needs_review_result(notes: str) -> dict[str, Any]:
    return {
        "status": "needs_human_review",
        "booking": None,
        "next_available_slots": [],
        "patient_whatsapp_sent": False,
        "clinic_whatsapp_sent": False,
        "whatsapp_sent": False,
        "notes": notes,
    }


def _patient_name_from_extraction(extraction: dict[str, Any] | None) -> str:
    if not extraction:
        return "Unknown"
    for key in ("book", "reschedule", "cancel"):
        block = extraction.get(key)
        if isinstance(block, dict):
            name = str(block.get("patient_name") or "").strip()
            if name:
                return name
    return "Unknown"


def notify_clinic_human_review(
    call: ActiveCall,
    rendered_transcript: str,
    notes: str,
    extraction: dict[str, Any] | None = None,
) -> bool:
    """Alert clinic staff when a call needs manual follow-up."""
    excerpt = rendered_transcript.strip()
    if len(excerpt) > 2000:
        excerpt = excerpt[:2000] + "…"
    message = format_clinic_review_message(
        "needs human review",
        patient_name=_patient_name_from_extraction(extraction),
        phone_number=call.caller_number,
        call_id=call.call_id,
        outcome="needs_human_review",
        notes=(notes or "Manual follow-up required.") + (f"\n\nTranscript:\n{excerpt}" if excerpt else ""),
    )
    return notify_clinic_whatsapp(message)


def attach_clinic_review_alert(
    result: dict[str, Any],
    call: ActiveCall,
    rendered_transcript: str,
    extraction: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Send a clinic WhatsApp alert once when post-call processing needs a human."""
    if result.get("status") != "needs_human_review":
        return result
    if result.get("clinic_whatsapp_sent"):
        return result
    clinic_sent = notify_clinic_human_review(
        call,
        rendered_transcript,
        str(result.get("notes") or ""),
        extraction,
    )
    result["clinic_whatsapp_sent"] = clinic_sent
    result["whatsapp_sent"] = bool(result.get("patient_whatsapp_sent")) or clinic_sent
    return result


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
        outcome="confirmed",
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
        "status": "confirmed",
        "booking": record,
        "next_available_slots": [],
        "notes": "Appointment confirmed on calendar. Patient WhatsApp sent if opted in; otherwise clinic should call.",
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
        return _needs_review_result("No matching booking found for reschedule.")

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
            "status": "confirmed",
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
        outcome="confirmed",
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
        "status": "confirmed",
        "booking": updated,
        "next_available_slots": [],
        "notes": "Reschedule confirmed on calendar. Patient WhatsApp sent if opted in; otherwise clinic should call.",
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
        return _needs_review_result("No matching booking found for cancellation.")

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
        "notes": "Cancellation processed; clinic notified. Patient WhatsApp sent if opted in; otherwise clinic should call.",
        **wa,
    }


def execute_extraction_locally(
    extraction: dict[str, Any],
    call: ActiveCall,
) -> dict[str, Any]:
    """Run gog Calendar / bookings.json / WhatsApp after transcript extraction."""
    if extraction.get("status") != "extracted":
        return _needs_review_result(extraction.get("notes", "Extraction incomplete."))

    calendar = make_gog_calendar()
    store = BookingStore()
    intent = extraction.get("intent")

    if intent == "book":
        book = extraction.get("book")
        if not isinstance(book, dict):
            return _needs_review_result("Missing book payload.")
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
            return _needs_review_result("Missing reschedule payload.")
        fields = {
            "patient_name": str(payload["patient_name"]).strip(),
            "existing_appointment_date": str(payload["existing_appointment_date"]).strip(),
            "new_appointment_date": str(payload["new_appointment_date"]).strip(),
            "new_appointment_time": str(payload["new_appointment_time"]).strip(),
        }
        return execute_reschedule_intent(
            fields, payload, calendar, store, call.call_id, call.caller_number
        )

    if intent == "cancel":
        payload = extraction.get("cancel")
        if not isinstance(payload, dict):
            return _needs_review_result("Missing cancel payload.")
        fields = {
            "patient_name": str(payload["patient_name"]).strip(),
            "existing_appointment_date": str(payload["existing_appointment_date"]).strip(),
        }
        return execute_cancel_intent(
            fields, payload, calendar, store, call.call_id, call.caller_number
        )

    return _needs_review_result("Unknown intent.")


async def process_call_post_call(
    call: ActiveCall,
    transcript: list[TranscriptLine],
) -> dict[str, Any]:
    """Extract intent from transcript via Bedrock Nova text, then execute via gog + bookings.json."""
    rendered = transcript_text(transcript)
    if not rendered.strip():
        raise RuntimeError("No transcript captured for post-call processing.")

    logger.info("Call %s: Nova text extraction (%s)", call.call_id, NOVA_TEXT_MODEL)
    try:
        extraction = await extract_call_with_nova(call, rendered)
    except Exception as exc:
        logger.exception("Call %s: Nova extraction failed", call.call_id)
        extraction = _needs_review_result(f"Extraction failed: {exc}")

    if extraction.get("status") == "needs_human_review":
        logger.info(
            "Call %s: retrying extraction using Claudia's confirmed read-back",
            call.call_id,
        )
        try:
            retry = await retry_extract_call_with_nova(call, rendered)
            if retry.get("status") == "extracted":
                extraction = retry
        except Exception as exc:
            logger.warning("Call %s: extraction retry failed (%s)", call.call_id, exc)

    logger.info("Call %s: local execution (%s)", call.call_id, extraction.get("intent"))
    result = execute_extraction_locally(extraction, call)
    return attach_clinic_review_alert(result, call, rendered, extraction)


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
    resched.add_argument("--call-id", default="manual")
    resched.add_argument("--fallback-phone", default="")

    cancel = sub.add_parser("cancel-booking")
    cancel.add_argument("--payload", required=True)
    cancel.add_argument("--call-id", default="manual")
    cancel.add_argument("--fallback-phone", default="")

    wa = sub.add_parser("whatsapp")
    wa.add_argument("--target", required=True, help="E.164 patient or clinic number")
    wa.add_argument("--text", required=True)

    args, _unknown = parser.parse_known_args(argv)
    calendar = make_gog_calendar()
    store = BookingStore()

    def intent_fields(payload: dict[str, Any], keys: list[str]) -> dict[str, str]:
        return {key: str(payload[key]).strip() for key in keys}

    if args.tool == "create-booking":
        payload = json.loads(args.payload)
        fields = intent_fields(
            payload,
            [
                "patient_name",
                "phone_number",
                "appointment_date",
                "appointment_time",
                "appointment_type",
            ],
        )
        result = execute_booking_intent(
            fields, payload, calendar, store, args.call_id, args.fallback_phone
        )
        print(json.dumps(result))
        return 0

    if args.tool == "reschedule-booking":
        payload = json.loads(args.payload)
        fields = intent_fields(
            payload,
            [
                "patient_name",
                "existing_appointment_date",
                "new_appointment_date",
                "new_appointment_time",
            ],
        )
        result = execute_reschedule_intent(
            fields, payload, calendar, store, args.call_id, args.fallback_phone
        )
        print(json.dumps(result))
        return 0

    if args.tool == "cancel-booking":
        payload = json.loads(args.payload)
        fields = intent_fields(
            payload,
            ["patient_name", "existing_appointment_date"],
        )
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
# Telcoflow inbound handler
# ---------------------------------------------------------------------------


async def safe_disconnect(call: ActiveCall) -> None:
    """Disconnect if still connected; ignore errors when the call already ended."""
    try:
        await call.disconnect()
    except Exception as exc:
        logger.debug("Call %s disconnect skipped (already ended): %s", call.call_id, exc)


async def handle_incoming_call(call: ActiveCall) -> None:
    """Answer the call with Claudia (Nova Sonic), then extract and execute post-call."""
    transcript = await run_nova_voice_call(call)
    result = await process_call_post_call(call, transcript)
    print(json.dumps({"call_id": call.call_id, "post_call_result": result}, indent=2))


async def main() -> None:
    ensure_gog_cli()
    config = make_telcoflow_config()

    async with TelcoflowClient(config) as client:
        @client.on(events.INCOMING_CALL)
        async def on_call(call: ActiveCall) -> None:
            try:
                await handle_incoming_call(call)
            except Exception as exc:
                logger.exception("Call %s failed", call.call_id)
                print(f"Call {call.call_id} failed: {exc}", file=sys.stderr)
                notify_clinic_whatsapp(
                    "\n".join(
                        [
                            "HealthFirst Clinic — call processing failed",
                            f"Telcoflow call: {call.call_id}",
                            f"Caller: {call.caller_number}",
                            f"Error: {exc}",
                            "Please follow up with the patient manually.",
                        ]
                    )
                )
                await safe_disconnect(call)

        logger.info(
            "HealthFirst Claudia agent listening (voice=Nova Sonic 2, post-call=%s)",
            NOVA_TEXT_MODEL,
        )
        await client.run_forever()


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "internal":
        raise SystemExit(run_internal_tool(sys.argv[2:]))
    asyncio.run(main())
