# Deploy HealthFirst Claudia on AWS Lightsail

This guide runs **two long-lived processes** on one Lightsail instance:

1. **OpenClaw gateway** — WhatsApp Web session (Baileys)
2. **`booking_agent.py`** — AgenTao inbound calls + Gemini Live (Claudia) + post-call OpenClaw/gog

AgenTao connects **outbound** from your instance to AgenTao’s servers (WebSocket). You do not need to open an inbound port for phone calls, but you need stable **outbound HTTPS (443)** and enough RAM for Node + Python + live audio.

---

## What you need before starting

| Item | Where to get it |
| --- | --- |
| AgenTao `WSS_API_KEY` + `WSS_CONNECTOR_UUID` | [AgenTao dashboard](https://docs.AgenTao.com) |
| `GOOGLE_API_KEY` | Google AI Studio / Cloud (Gemini Live + OpenClaw LLM) |
| Google account for clinic calendar | For `gog auth` (OAuth) |
| Dedicated WhatsApp number (recommended) | SIM or spare phone for OpenClaw |
| Clinic WhatsApp E.164 | `WHATSAPP_CLINIC_NUMBER` in `.env` |
| SSH access to Lightsail | AWS console |

**Headless note:** Both **gog** (Google Calendar) and **WhatsApp** can be set up **directly on the Lightsail server** — you do not need a laptop. Use a phone browser for OAuth and QR scan (see sections 5 and 6).

---

## 1. Create the Lightsail instance

1. AWS Console → **Lightsail** → **Create instance**.
2. **Platform:** Linux/Unix  
3. **Blueprint:** Ubuntu 22.04 LTS (or 24.04)  
4. **Plan:** at least **2 GB RAM** ($12/mo tier). 1 GB may OOM during Gemini Live + gateway.  
5. **Name:** e.g. `healthfirst-claudia`  
6. Create instance, attach a **static IP**, download the default SSH key.

**Networking**

- Default: outbound internet works.  
- No inbound firewall rule required for AgenTao.  
- Optional: restrict SSH (port 22) to your IP in Lightsail **Networking → Firewall**.

---

## 2. SSH and base packages

```bash
ssh -i ~/Downloads/LightsailDefaultKey.pem ubuntu@YOUR_STATIC_IP

sudo apt update && sudo apt upgrade -y
sudo apt install -y git python3.11 python3.11-venv python3-pip curl ca-certificates

# Node.js 22 (for OpenClaw + global gog if installed via npm ecosystem)
curl -fsSL https://deb.nodesource.com/setup_22.x | sudo -E bash -
sudo apt install -y nodejs
node -v && npm -v
```

---

## 3. Install OpenClaw and gog

```bash
sudo npm install -g openclaw

# gog CLI (Google Calendar) — install per https://gogcli.sh or:
# brew-style on Linux: download release from https://github.com/steipete/gogcli/releases
# Example (adjust version/arch for your instance):
# curl -fsSL -o /tmp/gog.tar.gz https://github.com/steipete/gogcli/releases/latest/download/gog_Linux_arm64.tar.gz
# sudo tar -xzf /tmp/gog.tar.gz -C /usr/local/bin gog

openclaw skills install gog
which gog && gog --version
```

Install `gog` so `which gog` succeeds. Set `GOG_BIN=/usr/local/bin/gog` in `.env` if not on `PATH`.

---

## 4. Deploy the agent code

```bash
sudo mkdir -p /opt/healthfirst
sudo chown ubuntu:ubuntu /opt/healthfirst
cd /opt/healthfirst

# Option A: git clone your repo
git clone https://github.com/YOUR_ORG/YOUR_REPO.git .
cd healthfirst_claudia

# Option B: scp from laptop
# scp -i key.pem -r ./healthfirst_claudia ubuntu@YOUR_IP:/opt/healthfirst/

python3.11 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
nano .env
```

### `.env` on Lightsail (example)

```bash
WSS_API_KEY=...
WSS_CONNECTOR_UUID=...
GOOGLE_API_KEY=...
GOOGLE_CALENDAR_ID=primary

DEFAULT_PHONE_COUNTRY_CODE=91
WHATSAPP_CLINIC_NUMBER=+919322958608
WHATSAPP_OPENCLAW_ACCOUNT=

OPENCLAW_AGENT=main
OPENCLAW_TIMEOUT_SECONDS=900
GOG_BIN=gog

BOOKINGS_PATH=/opt/healthfirst/healthfirst_claudia/data/bookings.json
CLINIC_TIMEZONE=Asia/Singapore
LOG_LEVEL=INFO
LOG_TRANSCRIPTS=true
```

```bash
mkdir -p /opt/healthfirst/healthfirst_claudia/data
touch /opt/healthfirst/healthfirst_claudia/data/bookings.json
echo '{"bookings":[]}' > /opt/healthfirst/healthfirst_claudia/data/bookings.json
chmod 600 .env
```

---

## 5. Authenticate gog on the server (no laptop)

`gog` supports a **remote / manual OAuth flow** designed for SSH servers. You only need a phone or any browser — not a local dev machine.

Reference: [gog auth add](https://gogcli.sh/commands/gog-auth-add.html) (`--remote`, `--manual`, `--step`).

### 5a. Create Google OAuth credentials (Google Cloud Console)

Do this once in a browser (phone is fine):

1. Open [Google Cloud Console](https://console.cloud.google.com/) → create or select a project.
2. **APIs & Services → Library** → enable **Google Calendar API**.
3. **APIs & Services → Credentials → Create credentials → OAuth client ID**.
4. If prompted, configure the **OAuth consent screen** (External or Internal for Workspace).
5. Application type: **Desktop app** (gog expects a desktop-style client).
6. Download the JSON file (e.g. `client_secret_....json`).

Upload it to Lightsail:

```bash
# From any machine that has the JSON file
scp -i key.pem client_secret_*.json ubuntu@YOUR_IP:/home/ubuntu/
```

On the server:

```bash
gog auth credentials set ~/client_secret_*.json --client healthfirst
gog auth credentials list
```

### 5b. Authorize your clinic Google account (remote flow)

Replace `clinic@gmail.com` with the Google account that owns the clinic calendar.

**Step 1 — print the sign-in URL (run on Lightsail over SSH):**

```bash
gog auth add clinic@gmail.com \
  --client healthfirst \
  --services calendar \
  --remote \
  --step 1
```

Copy the **URL** printed in the terminal.

**Step 2 — sign in on your phone (or any browser):**

1. Open the URL on your phone.
2. Sign in with the clinic Google account and approve Calendar access.
3. After consent, the browser redirects to `http://127.0.0.1:...` or `http://localhost:...` and may show “can’t connect” — that is normal on a phone.
4. **Copy the entire address bar URL** (it contains `code=` or tokens in the query string).  
   - On mobile Chrome: tap the address bar → copy all.  
   - If the page loaded an error, the URL in the bar is still what you need.

**Step 3 — paste the redirect URL back on the server:**

```bash
gog auth add clinic@gmail.com \
  --client healthfirst \
  --services calendar \
  --remote \
  --step 2 \
  --auth-url 'PASTE_THE_FULL_REDIRECT_URL_HERE'
```

Use single quotes around the URL so `&` in the query string is not eaten by the shell.

### 5c. Verify Calendar on the server

```bash
gog auth list
gog auth status
gog calendar calendars --json
```

Pick the calendar id from the output and set in `.env`:

```bash
GOOGLE_CALENDAR_ID=primary
# or e.g. clinic@gmail.com
```

Test free/busy:

```bash
gog --json calendar freebusy \
  --calendars "$GOOGLE_CALENDAR_ID" \
  --from "$(date -u -Iseconds)" \
  --to "$(date -u -d '+1 day' -Iseconds)"
```

### Alternative: Google Workspace service account (fully non-interactive)

If you use **Google Workspace** (not personal Gmail), an admin can use domain-wide delegation:

```bash
gog auth service-account --help
```

That path uses a service account JSON and admin console setup — no browser on the server. See [gog auth service-account](https://gogcli.sh/commands/gog-auth-service-account.html). Personal Gmail accounts should use the remote OAuth flow in 5b.

### If `gog auth add` fails

```bash
gog auth doctor
```

Common fixes:

- Wrong client type → must be **Desktop app** in Google Cloud.
- Consent screen in “Testing” → add your Google account as a **test user**, or publish the app.
- Expired step-1 URL → run step 1 again and complete step 2 within a few minutes.
- Keyring issues on minimal VMs → try `gog auth credentials set ... --insecure` (stores client secret in config; protect file permissions).

---

## 6. Configure OpenClaw and link WhatsApp

### Minimal OpenClaw config

```bash
mkdir -p ~/.openclaw /opt/healthfirst/openclaw-workspace

cat > ~/.openclaw/openclaw.json <<'EOF'
{
  "gateway": {
    "mode": "local",
    "port": 18789,
    "bind": "lan"
  },
  "agents": {
    "defaults": {
      "workspace": "/opt/healthfirst/openclaw-workspace",
      "model": { "primary": "google/gemini-2.5-flash" }
    },
    "list": [{ "id": "main", "default": true, "workspace": "/opt/healthfirst/openclaw-workspace" }]
  },
  "channels": {
    "whatsapp": {
      "dmPolicy": "allowlist",
      "allowFrom": ["+919322958608"]
    }
  }
}
EOF
```

Add patient numbers to `allowFrom` if you need inbound WhatsApp to the bot; outbound `openclaw message send` still requires the gateway to be up.

Set env for the booking agent user:

```bash
echo 'export GOOGLE_API_KEY=your_key' >> ~/.bashrc
echo 'export GEMINI_API_KEY=$GOOGLE_API_KEY' >> ~/.bashrc
source ~/.bashrc
```

### Link WhatsApp (QR) — on the server only

SSH into Lightsail with a terminal that can display the QR (most SSH clients do):

```bash
openclaw channels login --channel whatsapp
```

1. A **QR code** appears in the terminal (or a URL to open).
2. On the dedicated WhatsApp phone: **Linked devices → Link a device → Scan QR**.
3. Wait until the CLI reports success.

If the QR is too small over SSH:

- Maximize the terminal font / use a larger window, or  
- Use `openclaw channels login --channel whatsapp` from a machine with a bigger display **only for login**, then copy `~/.openclaw/` to the server (optional fallback).

Session data lives under `~/.openclaw/` — back it up before rebuilding the instance.

### Test gateway

```bash
openclaw gateway
# Leave running in one terminal; you should see WhatsApp connected
```

In another SSH session:

```bash
openclaw message send --channel whatsapp --target +919322958608 --message "HealthFirst test from Lightsail"
```

---

## 7. systemd — run on boot

### OpenClaw gateway

```bash
sudo tee /etc/systemd/system/openclaw-gateway.service <<'EOF'
[Unit]
Description=OpenClaw Gateway (WhatsApp)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=ubuntu
WorkingDirectory=/home/ubuntu
Environment=HOME=/home/ubuntu
Environment=GOOGLE_API_KEY=REPLACE_ME
Environment=GEMINI_API_KEY=REPLACE_ME
ExecStart=/usr/bin/openclaw gateway
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
EOF

sudo nano /etc/systemd/system/openclaw-gateway.service
# Set GOOGLE_API_KEY and GEMINI_API_KEY to the same value as .env
```

### Claudia booking agent

```bash
sudo tee /etc/systemd/system/healthfirst-claudia.service <<'EOF'
[Unit]
Description=HealthFirst Claudia AgenTao booking agent
After=network-online.target openclaw-gateway.service
Wants=network-online.target

[Service]
Type=simple
User=ubuntu
WorkingDirectory=/opt/healthfirst/healthfirst_claudia
EnvironmentFile=/opt/healthfirst/healthfirst_claudia/.env
Environment=HOME=/home/ubuntu
Environment=GEMINI_API_KEY=%GOOGLE_API_KEY%
ExecStart=/opt/healthfirst/healthfirst_claudia/.venv/bin/python booking_agent.py
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
EOF
```

Enable and start:

```bash
sudo systemctl daemon-reload
sudo systemctl enable openclaw-gateway healthfirst-claudia
sudo systemctl start openclaw-gateway
sleep 5
sudo systemctl start healthfirst-claudia

sudo systemctl status openclaw-gateway healthfirst-claudia
sudo journalctl -u healthfirst-claudia -f
sudo journalctl -u openclaw-gateway -f
```

**Order:** start **gateway first**, then **booking agent**. The agent calls `openclaw message send` and `openclaw agent` while processing calls.

---

## 8. AgenTao connector

In the AgenTao console, point your connector at this deployment:

- The agent uses **outbound** WSS with `WSS_API_KEY` / `WSS_CONNECTOR_UUID`.  
- Confirm the connector is in **sandbox** or **prod** to match `AgenTaoClientConfig.sandbox()` in code (change to `.production()` when you go live).  
- Place a test call; watch logs:

```bash
sudo journalctl -u healthfirst-claudia -f
```

You should see transcript lines, then OpenClaw extract/execute, then gog/WhatsApp activity.

---

## 9. Smoke tests

```bash
cd /opt/healthfirst/healthfirst_claudia
source .venv/bin/activate
export $(grep -v '^#' .env | xargs)

# gog
gog calendar calendars --json

# OpenClaw CLI (uses GOOGLE_API_KEY)
openclaw agent --agent main --local --message "Reply with OK" --json

# Internal booking path (optional)
python booking_agent.py internal whatsapp --target +919322958608 --text "Internal tool test"
```

---

## 10. Operations

| Task | Command |
| --- | --- |
| Restart agent | `sudo systemctl restart healthfirst-claudia` |
| Restart WhatsApp | `sudo systemctl restart openclaw-gateway` |
| View agent logs | `sudo journalctl -u healthfirst-claudia -f` |
| View gateway logs | `sudo journalctl -u openclaw-gateway -f` |
| Backup bookings | `cp data/bookings.json ~/bookings-backup-$(date +%F).json` |
| Update code | `git pull && source .venv/bin/activate && pip install -r requirements.txt && sudo systemctl restart healthfirst-claudia` |

**Persist on disk**

- `data/bookings.json`  
- `~/.openclaw/` (WhatsApp session + config)  
- `~/.config/gog/` (Calendar OAuth tokens)

Back up these before rebuilding the instance.

**WhatsApp session expiry**

If WhatsApp disconnects, re-run `openclaw channels login --channel whatsapp` and restart the gateway.

**gog token expiry**

Re-run `gog auth` locally and re-copy config, or refresh on the server.

---

## 11. Troubleshooting

| Symptom | Likely cause | Fix |
| --- | --- | --- |
| `gog is not ready` | OAuth missing | `gog auth` + copy config to server |
| `openclaw message send failed` | Gateway down | `sudo systemctl start openclaw-gateway` |
| No patient WhatsApp | Patient declined on call | Expected; check extraction `wants_patient_whatsapp` |
| No clinic WhatsApp | `WHATSAPP_CLINIC_NUMBER` empty | Set in `.env`, restart agent |
| OpenClaw agent timeout | Slow model / network | Raise `OPENCLAW_TIMEOUT_SECONDS` |
| Call connects, no voice | AgenTao / Gemini key | Check `WSS_*`, `GOOGLE_API_KEY`, logs |
| OOM / killed | 1 GB instance | Resize to 2 GB RAM |

---

## Architecture on Lightsail

```text
                    Internet (outbound 443)
                              │
         ┌────────────────────┼────────────────────┐
         │         Lightsail Ubuntu VM             │
         │                                       │
         │  openclaw-gateway.service             │
         │    └── WhatsApp Web (linked phone)    │
         │                                       │
         │  healthfirst-claudia.service          │
         │    ├── AgenTao WSS (inbound calls)  │
         │    ├── Gemini Live (Claudia voice)    │
         │    ├── openclaw agent (post-call)     │
         │    ├── gog calendar (OAuth)           │
         │    └── openclaw message send          │
         │                                       │
         │  /opt/.../data/bookings.json          │
         └───────────────────────────────────────┘
```

---

## Quick reference links

- [AgenTao docs](https://docs.AgenTao.com)
- [OpenClaw WhatsApp](https://docs.openclaw.ai/channels/whatsapp)
- [openclaw message send](https://docs.openclaw.ai/cli/message)
- [gog calendar](https://gogcli.sh/commands/gog-calendar.html)
- Project README: [README.md](./README.md)
