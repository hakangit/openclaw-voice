# Multi-customer OpenClaw Voice — architecture & onboarding

**Last updated:** 2026-11-10

## The model

One daemon instance per customer. Each instance is identical code pointed at a
different PBX, extension, SIP secret, and persona. Nothing is shared except
the code itself.

```
                          ┌──────────────────────────────┐
                          │  /opt/openclaw_voice/         │
                          │  (code — one copy, all insts)  │
                          └───────────────┬──────────────┘
                                          │
        ┌─────────────────────────────────┼─────────────────────────────────┐
        │                                 │                                 │
┌───────▼────────┐              ┌────────▼───────┐              ┌──────────▼───────┐
│ @customer1         │              │ @acme          │              │ @globex          │
│ config.yaml    │              │ config.yaml    │              │ config.yaml      │
│ .env (600)     │              │ .env (600)     │              │ .env (600)       │
│ persona.md     │              │ persona.md     │              │ persona.md       │
│                │              │                │              │                  │
│ SIP :5060      │              │ SIP :5070      │              │ SIP :5080        │
│ API :8079      │              │ API :8099      │              │ API :8100        │
└───────┬────────┘              └────────┬───────┘              └──────────┬───────┘
        │                                 │                                 │
┌───────▼────────┐              ┌────────▼───────┐              ┌──────────▼───────┐
│ sip.customer1 │              │ sip.acme.com   │              │ pbx.globex.com   │
│ ext 100        │              │ ext 299        │              │ ext 399          │
└────────────────┘              └────────────────┘              └──────────────────┘
```

Extension numbers may repeat across customers because each customer runs
their own PBX. What must be unique **on one host** is:

- the SIP listener port
- the HTTP API port
- the instance directory name

## On-disk layout

```
/etc/openclaw-voice/customers/
└── <customer_id>/
    ├── config.yaml      # non-secret settings: PBX host, extension, ports, persona
    ├── .env             # secrets: SIP password, xAI key, gateway token  (mode 600)
    └── persona.md       # the customer's voice identity and behaviour

/opt/openclaw_voice/    # the code, shared by all instances
```

Secrets never go in `config.yaml`. `config.yaml` references them as
`${SIP_PASSWORD}` / `${XAI_API_KEY}` and the daemon substitutes from the
instance's own `.env` at startup.

## How an instance starts

`openclaw-voice@.service` is a systemd **template**. Starting
`openclaw-voice@acme` sets:

| Variable | Value | Purpose |
|---|---|---|
| `OPENCLAW_VOICE_INSTANCE` | `acme` | identity in logs + health output |
| `OPENCLAW_VOICE_CONFIG` | `/etc/openclaw-voice/customers/acme/config.yaml` | which config to load |
| `OPENCLAW_VOICE_ENV` | `/etc/openclaw-voice/customers/acme/.env` | which secrets to load |
| `OPENCLAW_VOICE_API_PORT` | from that `.env` | HTTP API port |
| `OPENCLAW_VOICE_SIP_PORT` | from that `.env` | SIP listener port |

`load_config()` in `openclaw_voice.py` honours `OPENCLAW_VOICE_CONFIG` and
`OPENCLAW_VOICE_ENV`; without them it falls back to the original single-tenant
layout (`/opt/openclaw_voice/config.yaml` and `/opt/openclaw_voice/.env`), so
the existing deployment is unaffected.

## Onboarding a customer

```bash
sudo /opt/openclaw_voice/scripts/onboard_customer.sh \
    acme sip.acme.com 299 8099 5070
```

Arguments: `customer_id pbx_host extension [api_port] [sip_port]`

This creates the customer directory with config, secrets placeholder, and a
persona template. Then:

1. Add the SIP password and xAI key (script prints the exact commands — they
   use `read -rs` so the value never enters shell history).
2. `systemctl enable --now openclaw-voice@acme`
3. Verify: `curl -s http://127.0.0.1:8099/health`

The SIP extension and its password must already exist on the customer's PBX.
For FreePBX that is Extensions → add extension; the daemon registers as a
standard SIP endpoint and does not need any special module.

## Monitoring the fleet

```bash
/opt/openclaw_voice/scripts/fleet_health.sh          # table
/opt/openclaw_voice/scripts/fleet_health.sh --json   # machine-readable
```

Output includes per-instance service state, SIP registration state, active
call count, API port, and extension. The `--json` form is suitable for
feeding a monitoring system or the billing/cost-attribution layer.

## The control API

Each instance exposes its own localhost-only HTTP API:

| Method | Path | Purpose |
|---|---|---|
| GET | `/health` | registration, active calls, instance identity, extension |
| GET | `/calls` | list active and recent calls |
| POST | `/call` | place an outbound call → returns `call_id` immediately |
| DELETE | `/call/{call_id}` | hang up |
| POST | `/conference` | bridge an inbound call to an active outbound call |

Because each instance has its own port, the fleet can be driven from one
orchestrator that knows the customer→port mapping.

## Persona

`config.yaml` holds `xai.instructions` — the identity and behaviour of the
voice agent. This is per-customer and is the single source of identity. The
daemon prepends a capability block (tools available, style guidance, how to
end a call) at runtime, so customer instructions only need to describe *who
the business is and how it sounds*.

## Cost attribution

Each instance's xAI usage is attributable per customer because every customer
has a separate xAI realtime session stream in the daemon logs, keyed by
`OPENCLAW_VOICE_INSTANCE` via the systemd `SyslogIdentifier`. Call transcripts
are logged per instance, so a billing layer can aggregate by instance without
cross-talk.
