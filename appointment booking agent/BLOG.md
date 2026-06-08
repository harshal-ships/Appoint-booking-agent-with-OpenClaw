# Building a Clinic Phone Agent with AgenTao, OpenClaw, and WhatsApp

*How we wired inbound calls, post-call automation, and patient confirmations — without cramming everything into one model prompt.*

---

## The problem we were solving

HealthFirst Clinic wanted something simple on paper and messy in practice:

1. A patient calls and talks to a voice assistant (not a phone tree).
2. The assistant collects booking details — name, phone, date, time, appointment type.
3. After the call, something reliable creates a calendar event and notifies staff.
4. If the patient wants it, they get a WhatsApp confirmation. If not, staff know to call them.

No one wanted a monolith where the voice model also hits Google Calendar, parses JSON, and sends messages mid-sentence. That path breaks the moment latency spikes or the model hallucinates a slot.

So we split the system into layers. Each layer does one job.

---

## The stack at a glance

| Layer | Tool | Job |
|-------|------|-----|
| Phone | **AgenTao** (Telcoflow SDK) | Audio in/out, call events |
| Live voice | **Gemini Live** | Claudia speaks with the patient |
| Post-call extraction | **OpenClaw** (optional) or **Gemini text** | Turn transcript → structured JSON |
| Calendar | **gog CLI** | Check availability, create events |
| Ledger | **bookings.json** | Local record + `calendar_event_id` |
| Messaging | **OpenClaw WhatsApp** | Patient + clinic notifications |

```text
Patient call (PSTN)
        │
        ▼
   AgenTao WebSocket ──► Python agent (booking_agent.py)
        │                        │
        │                        ▼
        │                 Gemini Live (Claudia)
        │                 real-time voice only
        │                        │
        │                   call ends
        │                        ▼
        │                 transcript captured
        │                        │
        │              ┌─────────┴─────────┐
        │              ▼                   ▼
        │        OpenClaw extract    Gemini extract (fallback)
        │              └─────────┬─────────┘
        │                        ▼
        │              Python executor (deterministic)
        │              gog calendar + bookings.json
        │                        │
        │              ┌─────────┴─────────┐
        │              ▼                   ▼
        │        openclaw message      openclaw message
        │        (clinic staff)        (patient, if opted in)
        ▼
   hang up
```

The insight: **voice is probabilistic; booking is deterministic.** Keep them apart.

---

## Part 1: AgenTao — the phone layer

AgenTao sits at the telephony edge. Your Python process opens a long-lived WebSocket, listens for `INCOMING_CALL`, and gets a stream of 24 kHz PCM audio frames.

We use the Telcoflow SDK (`telcoflow-sdk`) — same family as AgenTao. The agent does not know about calendars or WhatsApp during the call. It only:

- `call.answer()` when someone rings
- Stream caller audio **to** Gemini Live
- Stream Claudia's audio **back** to the caller
- Fire post-call logic on `CALL_TERMINATED`

```python
async with TelcoflowClient(config) as client:
    @client.on(events.INCOMING_CALL)
    async def on_call(call: ActiveCall) -> None:
        transcript = await run_gemini_voice_call(call, gemini_client)
        result = await process_call_post_call(gemini_client, openclaw, call, transcript)
```

**What you need in `.env`:**

```env
WSS_API_KEY=your_AgenTao_api_key
WSS_CONNECTOR_UUID=your_connector_uuid
```

That's it for phone. No Google key here — AgenTao is transport, not intelligence.

**Practical note:** barge-in matters. When the patient talks over Claudia, the SDK should clear the outbound audio buffer so she stops speaking immediately. The Gemini Live bridge listens for `interrupted` events and calls `call.interrupt()` or `clear_send_audio_buffer()`.

---

## Part 2: Gemini Live — Claudia on the call

During the call, Claudia runs on Gemini Live native audio. She collects details, does a read-back (*"Is that correct?"*), asks about WhatsApp opt-in, and closes politely.

She explicitly does **not**:

- Hit Google Calendar
- Send WhatsApp
- Claim a slot is definitely free (that happens post-call)

This keeps the live path fast. We tune turn-end detection so she responds sooner after the patient stops speaking — especially important when patients dictate phone numbers digit by digit.

```env
GEMINI_MODEL=gemini-2.5-flash-native-audio-preview-12-2025
GEMINI_SILENCE_DURATION_MS=300
GEMINI_PREFIX_PADDING_MS=20
```

Lower `GEMINI_SILENCE_DURATION_MS` = snappier replies. Too low and she may cut patients off mid-number. Start at 300ms and adjust.

---

## Part 3: OpenClaw — extraction and WhatsApp

OpenClaw shows up **after** the call, in two different roles.

### Role A: Post-call extraction (optional)

When the call ends, we have a transcript. We need structured data:

```json
{
  "status": "extracted",
  "intent": "book",
  "book": {
    "patient_name": "Harshal Revetkar",
    "phone_number": "+919322958608",
    "appointment_date": "2026-06-08",
    "appointment_time": "11:00",
    "appointment_type": "general checkup",
    "wants_patient_whatsapp": true,
    "caller_is_whatsapp": true,
    "whatsapp_number": null
  }
}
```

The Python agent shells out to OpenClaw:

```bash
openclaw agent --agent main --local \
  --session-key healthfirst-claudia-extract-<call_id> \
  --message "<extraction prompt + transcript>" \
  --json
```

If OpenClaw is down, misconfigured, or returns incomplete JSON, we fall back to **Gemini text** with the same prompt. If that still fails, we retry once focusing only on Claudia's confirmed read-back — the bit where the patient said *"yes, that's correct."*

OpenClaw is optional for extraction. WhatsApp is not optional if you want confirmations.

### Role B: WhatsApp delivery (required for messaging)

OpenClaw's WhatsApp channel uses **WhatsApp Web** (Baileys), not the Meta Business API. Setup is deliberately low-ceremony:

```bash
openclaw channels login --channel whatsapp   # scan QR once
openclaw gateway                             # keep this running
```

Every outbound message goes through:

```bash
openclaw message send \
  --channel whatsapp \
  --target +919322958608 \
  --message "Your appointment is confirmed..." \
  --json
```

In Python:

```python
def send_whatsapp_via_openclaw(target: str, message: str) -> dict[str, Any]:
    command = [
        "openclaw", "message", "send",
        "--channel", "whatsapp",
        "--target", normalize_e164(target),
        "--message", message[:4096],
        "--json",
    ]
    subprocess.run(command, ...)
```

**Important:** `openclaw gateway` must be running. If it isn't, voice calls still work — but no WhatsApp goes out and post-call extraction via `openclaw agent` fails too.

---

## Part 4: The WhatsApp flow — clinic vs patient

We designed two audiences with different rules.

### Clinic staff (`WHATSAPP_CLINIC_NUMBER`)

Always notified on every successful book or cancel. The message includes patient name, phone, slot, calendar event ID, and call ID.

If the patient **declined** WhatsApp on the call, the clinic message says:

> Patient WhatsApp: declined — please call +91… to confirm.

That closes the loop for patients who don't use WhatsApp.

### Patient (opt-in only)

Claudia asks on the call — never pressures:

1. *"Would you like your appointment confirmation sent on WhatsApp?"*
2. Only if yes: *"Is the number you're calling from your WhatsApp number?"*

If they opt in, post-call sends:

> Hi Harshal, your HealthFirst Clinic appointment is confirmed.  
> Date: 2026-06-08  
> Time: 11:00  
> We look forward to seeing you.

If they decline, **no patient message**. Clinic staff call them instead.

Contact phone (for the booking record) and WhatsApp number (for confirmation) are separate fields in extraction. The live prompt trains Claudia to keep them apart.

---

## Part 5: What happens after extraction

Once JSON lands, a **deterministic Python executor** runs. No more LLM guessing.

1. Check Google Calendar via `gog calendar freebusy`
2. If free → `gog calendar create` (real event, blocks the slot)
3. Append to `bookings.json` with `status: confirmed`
4. `openclaw message send` → clinic
5. `openclaw message send` → patient (only if `wants_patient_whatsapp: true`)

Terminal output looks like:

```json
{
  "call_id": "019ea558-b556-7ab3-a425-18aedd1f3831",
  "post_call_result": {
    "status": "confirmed",
    "booking": { "patient_name": "...", "calendar_event_id": "..." },
    "patient_whatsapp_sent": true,
    "clinic_whatsapp_sent": true
  }
}
```

If extraction is ambiguous → `needs_human_review` and clinic gets an alert. If the slot is busy → `unavailable` with suggested alternatives. The executor owns those outcomes; the voice model never does.

---

## Deployment: two processes on one VM

On a typical Lightsail box we run **two systemd services**:

| Service | Command | Why |
|---------|---------|-----|
| `openclaw-gateway` | `openclaw gateway` | WhatsApp session + message routing |
| `healthfirst-claudia` | `python booking_agent.py` | AgenTao listener + voice + post-call |

Plus one-time setup:

- `gog auth` for Google Calendar (OAuth on the server)
- `openclaw channels login --channel whatsapp` (QR scan)
- `.env` with API keys and `WHATSAPP_CLINIC_NUMBER`

The agent reads `GOOGLE_API_KEY` from its own `.env`, or falls back to OpenClaw's onboarding config at `~/.openclaw/openclaw.json`. One key can serve both Gemini Live and extraction.

---

## Environment variables cheat sheet

```env
# Phone
WSS_API_KEY=
WSS_CONNECTOR_UUID=

# Voice + extraction
GOOGLE_API_KEY=
GEMINI_MODEL=gemini-2.5-flash-native-audio-preview-12-2025
GEMINI_SILENCE_DURATION_MS=300

# Calendar (gog)
GOOGLE_CALENDAR_ID=primary
GOG_ACCOUNT=clinic@gmail.com

# WhatsApp
WHATSAPP_CLINIC_NUMBER=+91...
DEFAULT_PHONE_COUNTRY_CODE=91

# OpenClaw
OPENCLAW_AGENT=main
```

---

## Lessons we learned the hard way

**1. Don't book during the call.**  
Patients hear *"your appointment is booked"* on the call, but the calendar write happens after hang-up. If you promise live availability without a calendar check, you'll confirm slots that post-call rejects.

**2. OpenClaw gateway is infrastructure, not a nice-to-have.**  
Treat it like your database — if it's down, confirmations stop. Monitor it.

**3. WhatsApp Web ≠ Meta Business API.**  
OpenClaw's channel is faster to set up (QR login) but tied to a phone's WhatsApp session. Plan for session expiry and re-login.

**4. Separate contact phone from WhatsApp.**  
Patients often assume they're the same. They're not in your data model. Collect contact phone during intake; ask about WhatsApp only after read-back.

**5. Extraction trusts Claudia's read-back, not noisy STT.**  
The patient saying digits often garbles in transcription. The extraction prompt says: if Claudia read back details and the patient said *"yes"*, use Claudia's version.

**6. Voice latency is tuning, not magic.**  
`GEMINI_SILENCE_DURATION_MS` and a prompt that says *"acknowledge immediately"* help. Some delay is inherent to native audio models — design short replies.

---

## When to use this pattern

This architecture fits when you need:

- Real phone calls (not just web voice)
- Reliable calendar writes after the conversation
- WhatsApp confirmations without Meta Business API paperwork
- A path to swap voice models without rewriting booking logic

It is probably overkill if you only need a chat widget. It shines when **the phone call is the product** and everything after it must be boring, testable, and repeatable.

---

## Try it yourself

The reference implementation lives in `examples/healthfirst_claudia/`:

```bash
cd healthfirst_claudia
python3.11 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
# configure AgenTao, gog, OpenClaw WhatsApp
python booking_agent.py
```

Full production walkthrough: [DEPLOYMENT.md](./DEPLOYMENT.md).

---

*Built with AgenTao for telephony, Gemini Live for voice, OpenClaw for WhatsApp, and a stubborn belief that booking logic belongs in Python — not in a system prompt.*
