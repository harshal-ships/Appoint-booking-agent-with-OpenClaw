# HealthFirst Claudia — AgentDuet + Nova Sonic 2 + OpenClaw

Inbound appointment agent: **book**, **reschedule**, or **cancel**. 


## Architecture

```
Patient call → AgentDuet → Nova Sonic 2 (Claudia voice)
                                ↓
                      transcript → Nova text (Bedrock)
              ┌─────────────────┼─────────────────┐
              ▼                 ▼                 ▼
         gog (Calendar)   bookings.json    WhatsApp (OpenClaw)
```

| Layer | Tool |
| --- | --- |
| AgentDuet | Phone audio |
| Nova Sonic 2 | Claudia live voice |
| Nova Lite/Pro | Post-call JSON extraction |
| **gog** | Google Calendar (OAuth) |
| OpenClaw | WhatsApp delivery only |
| Python fallback | Calls `gog` + updates `bookings.json` + `openclaw message send` |

## Setup (required)

### 1. gog — Google Calendar (server-only OAuth)

No laptop required. On your VM (e.g. Lightsail over SSH):

```bash
# After uploading Desktop OAuth JSON from Google Cloud Console:
gog auth credentials set ~/client_secret.json --client healthfirst
gog auth add clinic@gmail.com --client healthfirst --services calendar --remote --step 1
# Open URL on phone → sign in → copy redirect URL from browser bar
gog auth add clinic@gmail.com --client healthfirst --services calendar --remote --step 2 --auth-url 'PASTE_URL'
gog calendar calendars --json
```

Full walkthrough: **[DEPLOYMENT.md §5](./DEPLOYMENT.md#5-authenticate-gog-on-the-server-no-laptop)**

Reference: [gog auth add](https://gogcli.sh/commands/gog-auth-add.html)

### 2. OpenClaw WhatsApp

```bash
openclaw channels login --channel whatsapp
openclaw gateway    # keep running
```

**Clinic** (`WHATSAPP_CLINIC_NUMBER`): always gets a full booking summary to review and confirm.

**Patient**: only if they opted in on the call. Claudia asks whether the calling number is their WhatsApp; if not, they may give another number; if they decline, no patient message is sent.

Set `DEFAULT_PHONE_COUNTRY_CODE` when callers omit `+`.

### 3. Run the agent

```bash
python3.12 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env

python booking_agent.py
```

## Environment variables

| Variable | Purpose |
| --- | --- |
| `WSS_API_KEY`, `WSS_CONNECTOR_UUID` | AgentDuet |
| `NOVA_MODEL_ID`, `NOVA_VOICE_ID` | Nova Sonic 2 live voice |
| `NOVA_TEXT_MODEL` | Nova text model for post-call extraction (default `amazon.nova-lite-v1:0`) |
| `AWS_REGION`, `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY` | Bedrock credentials |
| `GOOGLE_CALENDAR_ID` | gog calendar target (`primary` or email) |
| `GOG_BIN` | Path to `gog` binary (default `gog`) |
| `DEFAULT_PHONE_COUNTRY_CODE` | E.164 prefix when patient omits `+` |
| `WHATSAPP_CLINIC_NUMBER` | Optional staff WhatsApp (E.164) |
| `BOOKINGS_PATH` | Local booking ledger |

## Internal tools (gog-backed)

```bash
python booking_agent.py internal create-booking --payload '{"patient_name":"Jane",...}' --call-id abc --fallback-phone +1...
python booking_agent.py internal whatsapp --target +919322958608 --text "Hello"
```

## AWS Lightsail

Full step-by-step guide: **[DEPLOYMENT.md](./DEPLOYMENT.md)** (instance size, gog OAuth, WhatsApp QR, systemd, AgentDuet, troubleshooting).

Summary: run **`openclaw gateway`** and **`python booking_agent.py`** as two systemd services; complete `gog auth` and WhatsApp login before going live.

## bookings.json

Each confirmed booking stores `calendar_event_id` from `gog calendar create`.
