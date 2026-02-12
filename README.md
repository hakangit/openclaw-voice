# OpenClaw Voice

An [OpenClaw](https://github.com/hakangit/openclaw) plugin that bridges SIP telephone calls to xAI Grok Voice Agents. Registers as a SIP extension, answers inbound calls (with optional PIN auth), and connects callers to an AI voice agent in real time.

## Architecture

```
Phone Call ←→ SIP/RTP ←→ openclaw_voice.py ←→ xAI Grok Voice WebSocket
                              ↓
                         HTTP API (:8079)
                              ↓
                    OpenClaw Gateway ←→ Tools
```

### Components

| File | Purpose |
|------|---------|
| `openclaw_voice.py` | Main daemon — SIP registration, call routing, HTTP API |
| `sip_client.py` | Minimal SIP/RTP client (no external SIP library) |
| `xai_voice_bridge.py` | WebSocket client for xAI Grok Voice API |
| `voice_bridge.py` | Audio bridge between SIP RTP and xAI |
| `audio.py` | Voice Activity Detection (WebRTC VAD) and echo suppression |
| `ov_cli.py` | CLI for making calls and checking status via the HTTP API |

## Quick Start

1. **Install dependencies**:
   ```bash
   pip install -r requirements.txt
   ```

2. **Configure**:
   ```bash
   cp .env.example .env
   # Edit .env with your API keys and SIP credentials
   # Edit config.yaml to set your SIP extension, voice, and instructions
   ```

3. **Run**:
   ```bash
   python openclaw_voice.py
   ```

## Configuration

### `.env`

| Variable | Description |
|----------|-------------|
| `XAI_API_KEY` | xAI API key for Grok Voice |
| `SIP_PASSWORD` | SIP account password |
| `OPENCLAW_GATEWAY_TOKEN` | OpenClaw gateway auth token |
| `OPENCLAW_AUTH_PIN` | PIN for inbound call authentication |

### `config.yaml`

Covers SIP server, xAI voice/instructions, audio format, daemon settings, and auth. Environment variables are substituted using `${VAR}` syntax.

## CLI Usage

```bash
# Make an outbound call
python ov_cli.py call 5551234567

# List active calls
python ov_cli.py calls

# Hang up a call
python ov_cli.py hangup <call-id>

# Health check
python ov_cli.py status
```

## HTTP API

The daemon exposes a local HTTP API on the configured port (default 8079):

| Method | Endpoint | Description |
|--------|----------|-------------|
| `POST` | `/call` | Initiate outbound call |
| `GET` | `/calls` | List active calls |
| `DELETE` | `/call/{id}` | Hang up a call |
| `GET` | `/health` | Health check |
| `POST` | `/call/{id}/conference` | Bridge call into conference |

## OpenClaw Integration

When connected to an OpenClaw gateway, the voice agent can execute tools (queries, commands, API calls) through the gateway's tool-calling interface. Configure the gateway connection in `config.yaml`:

```yaml
openclaw:
  gateway_port: 18789
  gateway_token: ${OPENCLAW_GATEWAY_TOKEN}
  session_key: "openclaw-voice"
```

## Deployment (systemd)

```bash
sudo cp openclaw-voice.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now openclaw-voice
```

The service file expects the project at `/opt/openclaw_voice` with a virtualenv at `/opt/openclaw_voice/venv`. Adjust paths as needed.

## Auth

Inbound calls can require a DTMF PIN before connecting to the voice agent. Configure in `config.yaml`:

```yaml
auth:
  pin: ${OPENCLAW_AUTH_PIN}
  whitelist: ["+15551234567"]  # numbers that skip PIN
```

## Design

- **No audio resampling**: xAI Grok natively supports G.711 u-law at 8kHz, matching SIP exactly. Sub-700ms latency.
- **No SIP library dependency**: `sip_client.py` implements SIP/RTP directly on UDP sockets.
- **Concurrent calls**: Multiple simultaneous calls, each with its own RTP port and xAI WebSocket.

## License

MIT
