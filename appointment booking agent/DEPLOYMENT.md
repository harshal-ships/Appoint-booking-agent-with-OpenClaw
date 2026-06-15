# Deploy HealthFirst Claudia on AWS Lightsail

This guide runs **two long-lived processes** on one Lightsail instance:

1. **OpenClaw gateway** — WhatsApp Web session
2. **`booking_agent.py`** — Telcoflow inbound calls + Nova Sonic 2 (Claudia) + Nova text extraction + gog/WhatsApp

Telcoflow connects **outbound** from your instance to Telcoflow’s servers (WebSocket). You do not need to open an inbound port for phone calls, but you need stable **outbound HTTPS (443)** and enough RAM for Node + Python + live audio.

---

## What you need before starting

| Item | Where to get it |
| --- | --- |
| Telcoflow `WSS_API_KEY` + `WSS_CONNECTOR_UUID` | [Telcoflow dashboard](https://www.telcoflow.com/) |
| AWS Bedrock creds | Nova Sonic 2 voice + Nova text post-call — **Python 3.12+** |
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
4. **Plan:** at least **2 GB RAM** ($12/mo tier). 1 GB may OOM during Nova Sonic + gateway.  
5. **Name:** e.g. `healthfirst-claudia`  
6. Create instance, attach a **static IP**, download the default SSH key.

**Networking**

- Default: outbound internet works.  
- No inbound firewall rule required for Telcoflow.  
- Optional: restrict SSH (port 22) to your IP in Lightsail **Networking → Firewall**.

---

## 2. SSH and base packages

```bash
ssh -i ~/Downloads/LightsailDefaultKey.pem ubuntu@YOUR_STATIC_IP

sudo apt update && sudo apt upgrade -y
sudo apt install -y git python3.12 python3.12-venv python3-pip curl ca-certificates

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
git clone your repo
git clone https://github.com/harshal-ships/Appoint-booking-agent-with-OpenClaw.git .
cd Appoint-booking-agent-with-OpenClaw

python3.12 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
nano .env
```

### `.env` on Lightsail (example)

```bash
WSS_API_KEY=...
WSS_CONNECTOR_UUID=...

NOVA_MODEL_ID=amazon.nova-2-sonic-v1:0
NOVA_TEXT_MODEL=amazon.nova-lite-v1:0
NOVA_VOICE_ID=matthew
AWS_REGION=us-east-1
AWS_ACCESS_KEY_ID=...
AWS_SECRET_ACCESS_KEY=...

GOOGLE_CALENDAR_ID=primary

DEFAULT_PHONE_COUNTRY_CODE=91
WHATSAPP_CLINIC_NUMBER=+919322958608
WHATSAPP_OPENCLAW_ACCOUNT=

GOG_BIN=gog

BOOKINGS_PATH=/opt/healthfirst/healthfirst_claudia/data/bookings.json
CLINIC_TIMEZONE=Asia/Singapore
LOG_LEVEL=INFO
LOG_TRANSCRIPTS=true
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
  "channels": {
    "whatsapp": {
      "dmPolicy": "disabled",
      "allowFrom": ["*"],
      "groupPolicy": "disabled",
      "enabled": true
    }
  }
}
EOF
```

`allowFrom: ["*"]` allows outbound sends to any patient number. `dmPolicy: "disabled"` blocks inbound DMs (Claudia is outbound-only). The gateway must be running for `openclaw message send` to work.

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
ExecStart=/usr/bin/openclaw gateway
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
EOF

```

Enable and start:

```bash
sudo systemctl daemon-reload
sudo systemctl enable openclaw-gateway
sudo systemctl start openclaw-gateway
sleep 5

sudo systemctl status openclaw-gateway
sudo journalctl -u healthfirst-claudia -f
```

**Order:** start **gateway first**, then **booking agent**. The agent calls `openclaw message send` while processing calls.

---

## Quick reference links

- [Telcoflow docs](https://docs.telcoflow.com)
- [OpenClaw WhatsApp](https://docs.openclaw.ai/channels/whatsapp)
- [openclaw message send](https://docs.openclaw.ai/cli/message)
- [gog calendar](https://gogcli.sh/commands/gog-calendar.html)
- Project README: [README.md](./README.md)
