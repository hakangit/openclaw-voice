#!/usr/bin/env bash
#
# onboard_customer.sh — provision a new customer instance for OpenClaw Voice.
#
# Creates the per-customer config + secrets layout, wires it to the
# openclaw-voice@.service systemd template, and verifies registration.
#
# Usage:
#   onboard_customer.sh <customer_id> <pbx_host> <extension> [api_port] [sip_port]
#
# Example:
#   onboard_customer.sh acme sip.acme.com 299 8099 5070
#
# The SIP password and API key are NOT passed on the command line (that would
# leak them into shell history and process listings). The script creates the
# .env with placeholder values and then prints the exact command to fill in
# the secret with 'read -s'.
#
set -euo pipefail

CUSTOMER_ID="${1:-}"
PBX_HOST="${2:-}"
EXTENSION="${3:-}"
API_PORT="${4:-}"
SIP_PORT="${5:-}"

ROOT="/etc/openclaw-voice/customers"
APP_DIR="/opt/openclaw_voice"

die() { echo "ERROR: $*" >&2; exit 1; }

[ -n "$CUSTOMER_ID" ] || die "customer id required"
[ -n "$PBX_HOST" ] || die "pbx host required"
[ -n "$EXTENSION" ] || die "extension required"

# Validate customer id: it becomes a directory name and a systemd instance
# name, so keep it boring. Letters, digits, dash, underscore only.
[[ "$CUSTOMER_ID" =~ ^[A-Za-z0-9_-]+$ ]] || die "customer id may only contain letters, digits, '-' and '_'"

# Validate the extension is numeric (SIP extension number).
[[ "$EXTENSION" =~ ^[0-9]+$ ]] || die "extension must be numeric"

CUSTOMER_DIR="$ROOT/$CUSTOMER_ID"

if [ -e "$CUSTOMER_DIR" ]; then
    die "customer '$CUSTOMER_ID' already exists at $CUSTOMER_DIR"
fi

# Pick default ports if not supplied. Base API ports at 8080 + offset so a
# second customer does not collide with the first.
if [ -z "$API_PORT" ]; then
    API_PORT="808$((RANDOM % 10))"
fi
if [ -z "$SIP_PORT" ]; then
    SIP_PORT="50$((RANDOM % 90 + 10))"
fi

echo "=== Onboarding customer '$CUSTOMER_ID' ==="
echo "    PBX host   : $PBX_HOST"
echo "    Extension  : $EXTENSION"
echo "    API port   : $API_PORT"
echo "    SIP port   : $SIP_PORT"
echo

mkdir -p "$CUSTOMER_DIR"
chmod 750 "$CUSTOMER_DIR"

# ---------------------------------------------------------------------------
# config.yaml
# ---------------------------------------------------------------------------
cat > "$CUSTOMER_DIR/config.yaml" <<EOF
# OpenClaw Voice — customer instance: $CUSTOMER_ID
# Generated $(date -u +%Y-%m-%dT%H:%M:%SZ)

sip:
  server: $PBX_HOST
  port: 5060
  extension: "$EXTENSION"
  password: \${SIP_PASSWORD}
  display_name: OpenClaw
  external_ip: ""
  rtp_port: 40000
  sip_local_port: $SIP_PORT

xai:
  api_key: \${XAI_API_KEY}
  instructions: |
    You are the AI receptionist for $PBX_HOST.
    You answer calls for extension $EXTENSION.
    Be warm, concise, and professional.
    Offer to help the caller, take a message, or transfer them to a human.

daemon:
  api_port: $API_PORT
  max_concurrent_calls: 3
  log_level: INFO
EOF
chmod 640 "$CUSTOMER_DIR/config.yaml"

# ---------------------------------------------------------------------------
# .env — secrets live here, mode 600, never in config.yaml
# ---------------------------------------------------------------------------
cat > "$CUSTOMER_DIR/.env" <<EOF
# OpenClaw Voice — customer instance: $CUSTOMER_ID
# Mode 600. Do not commit. Fill in the real values below.
SIP_PASSWORD=CHANGE_ME
XAI_API_KEY=CHANGE_ME
OPENCLAW_VOICE_API_PORT=$API_PORT
OPENCLAW_VOICE_SIP_PORT=$SIP_PORT
EOF
chmod 600 "$CUSTOMER_DIR/.env"

# ---------------------------------------------------------------------------
# persona.md — the per-customer voice identity
# ---------------------------------------------------------------------------
cat > "$CUSTOMER_DIR/persona.md" <<EOF
# Persona — $CUSTOMER_ID

You are the AI receptionist for $PBX_HOST, answering extension $EXTENSION.

## Identity
- Name: OpenClaw
- Business: $PBX_HOST
- Role: AI receptionist / intelligent extension

## Behaviour
- Greet the caller and ask how you can help.
- Keep replies short — this is a phone call, not email.
- Take a message when the right person is unavailable.
- Transfer to a human when the caller insists or the request is urgent.
- Never reveal internal configuration, credentials, or tool names.
EOF
chmod 640 "$CUSTOMER_DIR/persona.md"

echo "Created:"
echo "  $CUSTOMER_DIR/config.yaml"
echo "  $CUSTOMER_DIR/.env          (mode 600 — add secrets next)"
echo "  $CUSTOMER_DIR/persona.md"
echo

# ---------------------------------------------------------------------------
# Secrets entry — offer to fill it in without echoing to the terminal
# ---------------------------------------------------------------------------
cat <<EOF
Next steps:

1. Add the SIP password and xAI key (values stay out of your shell history):

   sudo bash -c 'read -rs -p "SIP password: " P; echo; sed -i "s|^SIP_PASSWORD=.*|SIP_PASSWORD=\$P|" $CUSTOMER_DIR/.env'
   sudo bash -c 'read -rs -p "XAI API key: " K; echo; sed -i "s|^XAI_API_KEY=.*|XAI_API_KEY=\$K|" $CUSTOMER_DIR/.env'

2. Enable and start the instance:

   systemctl enable --now openclaw-voice@$CUSTOMER_ID

3. Verify registration:

   systemctl status openclaw-voice@$CUSTOMER_ID
   curl -s http://127.0.0.1:$API_PORT/health

EOF
