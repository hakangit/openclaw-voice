#!/usr/bin/env python3
"""
OpenClaw Voice
==================

Permanent SIP + xAI Voice service that simultaneously receives and makes phone calls.

USAGE:
    python openclaw_voice.py                # Start daemon
    python openclaw_voice.py --no-api       # Start without HTTP API

Architecture:
    openclaw_voice.py
      |- SIPClient (always listening for inbound INVITEs)
      |    |- re-register + CRLF keepalive
      |    |- _active_calls dict (per-call state)
      |- HTTP API (aiohttp, localhost:8080)
      |    |- POST /call
      |    |- GET  /calls
      |    |- DELETE /call/{id}
      |    |- GET  /health
      |    |- POST /call/{id}/conference
      |- CallManager (tracks CallSession per concurrent call, max 3)
      |- Per call: CallSession
           |- RTPSession (own port)
           |- XAIVoiceBridge (own WebSocket)
           |- task_context (per-call instructions)
           |- 3 async tasks: sip->xai, xai->sip, xai_listener
"""

import argparse
import audioop
import asyncio
import base64
import json
import logging
import os
import signal
import sys
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional

import yaml
import aiohttp
from aiohttp import web
from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).parent))

RTP_FRAME_MS = 20  # 20ms per RTP frame (G.711 u-law @ 8kHz)
from sip_client import SIPClient, RTPSession
from xai_voice_bridge import XAIVoiceBridge

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logging.getLogger('websockets').setLevel(logging.INFO)
logging.getLogger('asyncio').setLevel(logging.INFO)
logging.getLogger('aiohttp').setLevel(logging.WARNING)
logger = logging.getLogger('openclaw_voice')


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

def load_config() -> dict:
    """Load configuration from config.yaml with env var substitution."""
    # Per-instance env file. Each customer instance has its own secrets file
    # (SIP password, xAI key, gateway token) so instances stay isolated from
    # each other. Falls back to the shared .env for the original single-tenant
    # deployment.
    env_override = os.getenv('OPENCLAW_VOICE_ENV', '').strip()
    if env_override:
        env_path = Path(env_override)
        if not env_path.is_absolute():
            env_path = Path(__file__).parent / env_path
        if env_path.exists():
            load_dotenv(env_path, override=True)
        else:
            logger.warning(
                f"OPENCLAW_VOICE_ENV={env_override} does not exist, "
                f"falling back to shared .env"
            )
    else:
        load_dotenv()

    config_path = Path(__file__).parent / "config.yaml"

    # Per-instance override: OPENCLAW_VOICE_CONFIG points at a customer-specific
    # config.yaml. This is what makes one daemon serve many customers -- each
    # systemd template instance gets its own file instead of sharing one.
    override = os.getenv('OPENCLAW_VOICE_CONFIG', '').strip()
    if override:
        override_path = Path(override)
        if not override_path.is_absolute():
            override_path = Path(__file__).parent / override_path
        if override_path.exists():
            config_path = override_path
        else:
            logger.warning(
                f"OPENCLAW_VOICE_CONFIG={override} does not exist, "
                f"falling back to {config_path}"
            )

    with open(config_path) as f:
        config_str = f.read()
    for key in ['XAI_API_KEY', 'SIP_PASSWORD', 'OPENCLAW_GATEWAY_TOKEN', 'OPENCLAW_AUTH_PIN']:
        placeholder = f'${{{key}}}'
        if placeholder in config_str:
            config_str = config_str.replace(placeholder, os.getenv(key, placeholder))
    config = yaml.safe_load(config_str)
    config['_config_path'] = str(config_path)
    return config


# ---------------------------------------------------------------------------
# Tool definitions for xAI voice agent
# ---------------------------------------------------------------------------

def _redact_tool_args(args: dict) -> dict:
    """Redact credential-bearing tool arguments before logging.

    The `authenticate` tool carries the caller's PIN in its `pin` argument, and
    logging tool args verbatim wrote the PIN to the journal on every successful
    authentication. Other secret-shaped fields are masked too, in case a
    provider starts passing one through a different tool.
    """
    secret_keys = {"pin", "password", "secret", "token", "api_key", "apikey", "key"}
    redacted = {}
    for key, value in (args or {}).items():
        if str(key).strip().lower() in secret_keys:
            redacted[key] = "***"
        else:
            redacted[key] = value
    return redacted


TOOL_AUTHENTICATE = {
    "type": "function",
    "name": "authenticate",
    "description": (
        "Verify the caller's PIN to unlock full capabilities. "
        "Call this when the user provides their PIN code."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "pin": {
                "type": "string",
                "description": "The PIN code the caller provided"
            }
        },
        "required": ["pin"]
    }
}

TOOLS_AUTHENTICATED = [
    {
        "type": "function",
        "name": "process_query",
        "description": (
            "Process any user query using OpenClaw tools and capabilities. "
            "Run shell commands, check system status, send messages, search the web, "
            "manage files, execute scripts, control services, etc."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "The complete user query or request to process"
                }
            },
            "required": ["query"]
        }
    },
    {
        "type": "function",
        "name": "schedule_outbound_call",
        "description": (
            "Schedule an outbound phone call to a number. Use this when the user "
            "asks you to call someone. Returns the call_id of the new call."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "number": {
                    "type": "string",
                    "description": "Phone number or extension to call (e.g. +85293233920 or 288)"
                },
                "task": {
                    "type": "string",
                    "description": "What to say/do on the call"
                }
            },
            "required": ["number", "task"]
        }
    },
    {
        "type": "function",
        "name": "connect_to_call",
        "description": (
            "Bridge the current inbound call's audio into an active outbound call "
            "so all parties can hear each other (3-way conference through xAI)."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "call_id": {
                    "type": "string",
                    "description": "The call_id of the outbound call to bridge into"
                }
            },
            "required": ["call_id"]
        }
    },
    {
        "type": "function",
        "name": "hangup_call",
        "description": (
            "End the current phone call. Use this when the conversation is over "
            "or the other party asks you to hang up. Without this you cannot "
            "end the call yourself — say goodbye, then call this tool."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "reason": {
                    "type": "string",
                    "description": "Why the call is ending (e.g. 'conversation complete')"
                }
            },
            "required": []
        }
    },
]

# Tools available on outbound calls. Outbound calls are dialled by us via the
# control API, so the caller has already been authorised by whoever placed
# the call. The agent needs hangup_call so it can end the call itself.
TOOLS_OUTBOUND = [
    tool for tool in TOOLS_AUTHENTICATED if tool["name"] == "hangup_call"
]

INBOUND_UNAUTH_INSTRUCTIONS = """\
You are a helpful voice assistant. The user is calling you on the phone.

The caller has NOT been authenticated yet. You can have a basic conversation, but \
you CANNOT use any tools until they provide their PIN.

Ask the caller for their PIN code to unlock full capabilities. When they give you \
a PIN, use the authenticate tool to verify it. Be friendly but firm — do not try \
to answer questions that would require tools until authenticated.

STYLE: Be natural, conversational, and concise. You're on a phone call."""

INBOUND_AUTH_INSTRUCTIONS = """\
You are a helpful voice assistant. The user is calling you on the phone.

CAPABILITIES:
- Answer questions, have conversations
- Run commands and check system status via process_query
- Make outbound calls on the user's behalf via schedule_outbound_call
- Bridge calls together via connect_to_call
- End the current call via hangup_call

STYLE: Be natural, conversational, and concise. You're on a phone call.

ENDING THE CALL: When the conversation is finished, or the caller asks you to
hang up, say your goodbye FIRST and then call the hangup_call tool. If you skip
the tool, nobody can end the call except the other party.

IMPORTANT: When you need to use a tool, ALWAYS say something first like "let me check \
on that", "one sec", or "hang on, let me look that up" BEFORE calling the tool. Tools \
take a few seconds to respond. While waiting, make brief small talk just like a real \
person would — comment on the topic, ask a follow-up, or chat casually. Never leave \
dead silence on the line.

When the user asks you to call someone, use schedule_outbound_call with their number \
and a description of what to do on the call."""

OUTBOUND_TEMPLATE = """\
You are OpenClaw, making a phone call on behalf of your user.

YOUR TASK: {task}

RULES:
- Identify yourself using the name given in your persona instructions, and say you are calling on behalf of your user
- Stay focused on the task
- Be polite and professional
- Confirm outcomes before hanging up
- Say goodbye when done

ENDING THE CALL: when you have finished the task and said goodbye, call the
hangup_call tool to end the call. If you do not call it, nobody can end the
call except the other party."""


# ---------------------------------------------------------------------------
# CallSession
# ---------------------------------------------------------------------------

@dataclass
class CallSession:
    call_id: str
    direction: str  # 'inbound' or 'outbound'
    remote_number: str
    state: str = 'ringing'  # ringing, connected, ended
    rtp_session: Optional[RTPSession] = None
    bridge: Optional[XAIVoiceBridge] = None
    task_context: str = ''
    started_at: float = field(default_factory=time.time)
    task: Optional[asyncio.Task] = None
    codec: int = 0
    authenticated: bool = False
    # Set once a hangup has been scheduled, so repeated hangup_call requests
    # (the model sometimes says goodbye over and over) do not queue extra
    # end-call tasks.
    hangup_scheduled: bool = False
    # Conference: when set, outbound RTP audio is also forwarded to this session
    conference_peer: Optional['CallSession'] = None


# ---------------------------------------------------------------------------
# CallManager
# ---------------------------------------------------------------------------

class CallManager:
    def __init__(self, max_concurrent: int = 3):
        self.calls: dict[str, CallSession] = {}
        self.max_concurrent = max_concurrent

    def add(self, session: CallSession) -> bool:
        active = sum(1 for s in self.calls.values() if s.state != 'ended')
        if active >= self.max_concurrent:
            return False
        self.calls[session.call_id] = session
        return True

    def remove(self, call_id: str):
        session = self.calls.get(call_id)
        if session:
            session.state = 'ended'

    def get(self, call_id: str) -> Optional[CallSession]:
        return self.calls.get(call_id)

    def active(self) -> list[CallSession]:
        return [s for s in self.calls.values() if s.state != 'ended']

    def to_list(self) -> list[dict]:
        result = []
        for s in self.calls.values():
            duration = int(time.time() - s.started_at)
            result.append({
                'call_id': s.call_id,
                'direction': s.direction,
                'remote_number': s.remote_number,
                'state': s.state,
                'duration_s': duration,
                'task': s.task_context[:100] if s.task_context else '',
                'conference_peer': s.conference_peer.call_id if s.conference_peer else None,
            })
        return result


# ---------------------------------------------------------------------------
# OpenClaw integration via Gateway HTTP API (OpenAI-compatible)
# ---------------------------------------------------------------------------

# Persistent aiohttp session for openclaw requests
_http_session: Optional[aiohttp.ClientSession] = None


async def get_http_session() -> aiohttp.ClientSession:
    global _http_session
    if _http_session is None or _http_session.closed:
        _http_session = aiohttp.ClientSession()
    return _http_session


async def run_openclaw_query(query: str, config: dict, timeout: int = 60) -> str:
    """Send a query to OpenClaw via its OpenAI-compatible chat completions API."""
    gw_port = config.get('openclaw', {}).get('gateway_port', 18789)
    gw_token = config.get('openclaw', {}).get('gateway_token', '')
    session_key = config.get('openclaw', {}).get('session_key', 'openclaw-voice')
    url = f"http://127.0.0.1:{gw_port}/v1/chat/completions"

    headers = {
        "Content-Type": "application/json",
        "X-OpenClaw-Session-Key": session_key,
    }
    if gw_token:
        headers["Authorization"] = f"Bearer {gw_token}"

    payload = {
        "model": "openclaw:main",
        "messages": [{"role": "user", "content": query}],
    }

    try:
        session = await get_http_session()
        async with session.post(url, json=payload, headers=headers, timeout=aiohttp.ClientTimeout(total=timeout)) as resp:
            if resp.status != 200:
                body = await resp.text()
                logger.warning(f"OpenClaw HTTP {resp.status}: {body[:200]}")
                return f"OpenClaw returned status {resp.status}"
            data = await resp.json()
            result = data["choices"][0]["message"]["content"]
            if len(result) > 500:
                result = result[:500] + "... [truncated for voice]"
            logger.info(f"OpenClaw reply ({len(result)} chars): {result[:100]}")
            return result
    except asyncio.TimeoutError:
        return "The request timed out. Please try again."
    except aiohttp.ClientConnectorError:
        return "Cannot reach OpenClaw gateway. Is it running?"
    except Exception as e:
        logger.error(f"OpenClaw query error: {e}")
        return f"Error contacting OpenClaw: {e}"


# ---------------------------------------------------------------------------
# OpenClawVoice
# ---------------------------------------------------------------------------

class OpenClawVoice:
    def __init__(self, config: dict):
        self.config = config
        self.sip_client: Optional[SIPClient] = None
        self.call_manager = CallManager(
            max_concurrent=config.get('daemon', {}).get('max_concurrent_calls', 3)
        )
        self.api_port = config.get('daemon', {}).get('api_port', 8080)

        # Multi-instance overrides. Two instances on one host cannot share a
        # SIP listener or an HTTP API port, so each customer instance is
        # started with its own. Config file is the source of truth; these
        # env vars let the systemd template inject the per-instance values.
        instance = os.getenv('OPENCLAW_VOICE_INSTANCE', '').strip()
        self.instance = instance or None
        if self.instance:
            logger.info(f"Running as customer instance '{self.instance}'")

        env_api_port = os.getenv('OPENCLAW_VOICE_API_PORT', '').strip()
        if env_api_port:
            self.api_port = int(env_api_port)

        env_sip_port = os.getenv('OPENCLAW_VOICE_SIP_PORT', '').strip()
        if env_sip_port:
            self.config.setdefault('sip', {})['sip_local_port'] = int(env_sip_port)

        self.running = False
        self._app: Optional[web.Application] = None
        self._runner: Optional[web.AppRunner] = None

    # ---- Lifecycle --------------------------------------------------------

    async def start(self, enable_api: bool = True):
        """Register SIP, start HTTP API, listen for calls."""
        self.running = True
        logger.info("Starting OpenClaw Voice...")

        # Build SIP config
        sip_cfg = self.config['sip']
        sip_config = {
            'sip_server': sip_cfg['server'],
            'sip_port': sip_cfg['port'],
            'extension': sip_cfg['extension'],
            'password': sip_cfg['password'],
            'display_name': sip_cfg.get('display_name', 'OpenClaw'),
            'external_ip': sip_cfg.get('external_ip', ''),
            'rtp_port': sip_cfg.get('rtp_port', 40000),
            'sip_local_port': sip_cfg.get('sip_local_port', 5060),
        }
        self.sip_client = SIPClient(sip_config)

        if not await self.sip_client.register():
            logger.error("SIP registration failed")
            return

        logger.info("SIP registered successfully")

        # Pre-warm OpenClaw session
        asyncio.create_task(self._prewarm_openclaw())

        # Start HTTP API
        if enable_api:
            await self._start_api()
            logger.info(f"HTTP API listening on 127.0.0.1:{self.api_port}")

        # Listen for inbound calls (blocks until stopped)
        try:
            await self.sip_client.listen_for_calls(self._on_inbound_call)
        finally:
            await self.stop()

    async def stop(self):
        """Shut down all calls, SIP client, and HTTP API."""
        if not self.running:
            return
        self.running = False
        logger.info("Stopping daemon...")

        # End all active calls
        for session in self.call_manager.active():
            await self._end_call(session)

        # Stop SIP
        if self.sip_client:
            await self.sip_client.stop()

        # Stop HTTP API
        if self._runner:
            await self._runner.cleanup()

        # Close shared HTTP session
        global _http_session
        if _http_session and not _http_session.closed:
            await _http_session.close()

        logger.info("Daemon stopped")

    async def _prewarm_openclaw(self):
        """Send a warmup query to OpenClaw so the first real call doesn't pay cold-start."""
        try:
            result = await run_openclaw_query("ping", self.config, timeout=15)
            logger.info(f"OpenClaw pre-warm done: {result[:60]}")
        except Exception as e:
            logger.warning(f"OpenClaw pre-warm failed (non-fatal): {e}")

    # ---- Inbound call handling -------------------------------------------

    def _is_whitelisted(self, number: str) -> bool:
        """Check if a caller number is in the auth whitelist."""
        whitelist = self.config.get('auth', {}).get('whitelist', [])
        if not whitelist:
            return False
        # Normalize: strip spaces, compare with/without leading +
        normalized = number.replace(' ', '').replace('-', '')
        for entry in whitelist:
            entry_norm = entry.replace(' ', '').replace('-', '')
            if normalized == entry_norm or normalized.lstrip('+') == entry_norm.lstrip('+'):
                return True
        return False

    async def _on_inbound_call(self, rtp_session: RTPSession, negotiated_codec: int, call_id: str):
        """Called by SIPClient when an inbound INVITE is answered."""
        call_state = self.sip_client._active_calls.get(call_id, {})
        remote = call_state.get('remote_number', 'unknown')
        logger.info(f"Inbound call {call_id[:16]} from {remote}")

        # Check if auth is configured
        # A numeric PIN can parse as a YAML int, so coerce to str before
        # calling string methods on it.
        auth_pin = str(self.config.get('auth', {}).get('pin', '') or '')
        # `auth.require_pin` is the explicit switch. It defaults to FALSE so
        # that merely having OPENCLAW_AUTH_PIN in .env no longer silently
        # strips every tool from the model. Previously a non-empty PIN meant
        # the model was given ONLY the `authenticate` tool, so it could never
        # schedule an outbound call -- and instead hallucinated "I can only
        # place one call at a time". The receptionist product intent is "no
        # inbound PIN", so callers get the full toolset by default.
        require_pin = bool((self.config.get('auth', {}) or {}).get('require_pin', False))
        has_auth = require_pin and bool(auth_pin and not auth_pin.startswith('${'))

        # Auto-authenticate whitelisted callers or if no PIN configured
        pre_authenticated = not has_auth or self._is_whitelisted(remote)
        if pre_authenticated and has_auth:
            logger.info(f"Caller {remote} is whitelisted, skipping PIN")

        session = CallSession(
            call_id=call_id,
            direction='inbound',
            remote_number=remote,
            state='connected',
            rtp_session=rtp_session,
            codec=negotiated_codec,
            authenticated=pre_authenticated,
        )

        if pre_authenticated:
            instructions = INBOUND_AUTH_INSTRUCTIONS
        else:
            instructions = INBOUND_UNAUTH_INSTRUCTIONS

        instructions = self._compose_instructions(instructions)
        session.task_context = instructions

        if not self.call_manager.add(session):
            logger.warning("Max concurrent calls reached, rejecting inbound")
            await self.sip_client.hangup(call_id=call_id)
            return

        await self._run_call(session, instructions, is_inbound=True)

    def _compose_instructions(self, base: str) -> str:
        """Prepend the configured persona to the session instructions.

        `xai.instructions` in config.yaml was previously dead config — it was
        never read — so the spoken identity drifted between calls on the same
        daemon ("OpenClaw" on one call, "Grok Voice" on the next).
        """
        persona = (self.config.get('xai') or {}).get('instructions') or ''
        persona = persona.strip()
        if not persona:
            return base
        return f"{persona}\n\n{base}"

    # ---- Outbound call handling ------------------------------------------

    async def make_outbound_call(self, number: str, task: str) -> Optional[str]:
        """Initiate an outbound call. Returns call_id or None on failure."""
        instructions = self._compose_instructions(OUTBOUND_TEMPLATE.format(task=task))
        call_id_holder = {}

        async def outbound_handler(rtp_session: RTPSession, codec: int, call_id: str):
            call_id_holder['id'] = call_id
            call_state = self.sip_client._active_calls.get(call_id, {})

            session = CallSession(
                call_id=call_id,
                direction='outbound',
                remote_number=number,
                state='connected',
                rtp_session=rtp_session,
                codec=codec,
                task_context=task,
            )

            if not self.call_manager.add(session):
                logger.warning("Max concurrent calls reached")
                await self.sip_client.hangup(call_id=call_id)
                return

            await self._run_call(session, instructions, is_inbound=False)

        # Run in background task
        async def do_call():
            try:
                await self.sip_client.make_call(number, outbound_handler)
            except Exception as e:
                logger.error(f"Outbound call error: {e}")

        task_obj = asyncio.create_task(do_call())

        # Wait briefly for make_call to assign a call_id. sip_client sets
        # `last_call_id` as soon as it builds the INVITE, so we get the id
        # within milliseconds instead of waiting for the call to be ANSWERED
        # (up to 30s). Previously the API reported "call failed" for a call
        # that was actually ringing and connecting.
        for _ in range(30):  # 3 seconds max
            await asyncio.sleep(0.1)
            if call_id_holder.get('id'):
                return call_id_holder['id']
            pending = getattr(self.sip_client, 'last_call_id', None)
            if pending:
                return pending

        return None

    # ---- Core call bridge ------------------------------------------------

    async def _run_call(self, session: CallSession, instructions: str, is_inbound: bool):
        """Run the audio bridge between SIP and xAI for one call."""
        # Pick tools based on auth state
        if not is_inbound:
            # Outbound calls are initiated by us (from the control API), so
            # they are already trusted. They still need at least hangup_call,
            # otherwise the agent can never end its own outbound call -- the
            # party had to hang up manually ("I can't disconnect from my
            # side" was the symptom).
            tools = list(TOOLS_OUTBOUND)
        elif session.authenticated:
            tools = TOOLS_AUTHENTICATED
        else:
            tools = [TOOL_AUTHENTICATE]

        bridge = XAIVoiceBridge(
            api_key=self.config['xai']['api_key'],
            voice=self.config['xai'].get('voice', 'Rex'),
            instructions=instructions,
            tools=tools,
        )

        # Set up tool handler
        async def tool_handler(tool_call_id: str, name: str, args: dict):
            return await self._handle_tool(session, tool_call_id, name, args)
        bridge.on_tool_call = tool_handler

        if not await bridge.connect():
            logger.error(f"Failed to connect xAI for call {session.call_id[:16]}")
            session.state = 'ended'
            self.call_manager.remove(session.call_id)
            return

        session.bridge = bridge
        logger.info(f"xAI connected for call {session.call_id[:16]}")

        rtp = session.rtp_session
        running = True
        last_frame_sent_at = 0.0  # monotonic time of last xai->sip frame

        async def xai_listener():
            nonlocal running
            try:
                await bridge._listen_task
            except asyncio.CancelledError:
                pass
            except Exception as e:
                logger.error(f"xAI listener error ({session.call_id[:16]}): {e}")
            finally:
                running = False

        async def sip_to_xai():
            """Forward audio from SIP to xAI."""
            nonlocal running
            loop = asyncio.get_event_loop()
            await asyncio.sleep(2)
            logger.info(f"sip_to_xai active ({session.call_id[:16]})")
            frames_in = 0
            ECHO_HOLD_S = 0.5  # suppress mic for 500ms after last xAI frame sent
            while running and bridge.running and self.running:
                try:
                    audio = await loop.run_in_executor(
                        None, lambda: rtp.receive_audio(timeout=0.02))
                    if audio:
                        frames_in += 1
                        if frames_in == 1:
                            logger.info(f"sip_to_xai: first RTP frame ({len(audio)}b)")
                        elif frames_in % 250 == 0:
                            logger.info(f"sip_to_xai: {frames_in} frames from phone")
                        # Echo suppression: don't send mic audio while playing or shortly after
                        if bridge.is_speaking:
                            continue
                        if (time.monotonic() - last_frame_sent_at) < ECHO_HOLD_S:
                            continue
                        # The bridge speaks G.711 u-law (PCMU) only. If the
                        # caller negotiated PCMA, convert on the way in so the
                        # bridge receives the format it expects.
                        #
                        # G.722 (PT 9) is deliberately NOT converted: it is
                        # ADPCM, not resampled u-law, so a rate conversion
                        # produces noise. We never offer G.722, and if a peer
                        # forces it we drop the frames rather than send garbage.
                        if session.codec == 8:
                            audio = audioop.lin2ulaw(audioop.ulaw2lin(audio, 2), 2)
                        elif session.codec not in (0, 8):
                            continue
                        await bridge.send_audio(audio)
                    else:
                        await asyncio.sleep(0.005)
                except OSError:
                    # RTP socket closed — the remote party hung up. End the whole
                    # call so the session slot and the xAI websocket are released
                    # instead of leaking until the next call.
                    logger.info(f"RTP session closed for call {session.call_id[:16]}; ending call")
                    running = False
                    break
                except Exception as e:
                    logger.error(f"sip_to_xai error: {e}")
                    running = False
                    break

        async def xai_to_sip():
            """Forward audio from xAI to SIP, with optional conference fork."""
            logger.info(f"xai_to_sip active ({session.call_id[:16]})")
            frames_sent = 0
            while running and bridge.running and self.running:
                frame = await bridge.receive_audio()
                if frame:
                    nonlocal last_frame_sent_at
                    try:
                        # xAI emits PCMU. Re-encode only for PCMA; for anything
                        # else (e.g. G.722) skip the frame instead of sending
                        # undecodable noise.
                        out_frame = frame
                        if session.codec == 8:
                            out_frame = audioop.lin2alaw(audioop.ulaw2lin(frame, 2), 2)
                        elif session.codec not in (0, 8):
                            await asyncio.sleep(RTP_FRAME_MS / 1000)
                            continue
                        rtp.send_audio(out_frame, payload_type=session.codec)
                    except OSError:
                        break  # RTP socket closed
                    last_frame_sent_at = time.monotonic()
                    frames_sent += 1
                    if frames_sent == 1:
                        logger.info(f"xai_to_sip: first frame to phone ({len(frame)}b) -> {rtp.remote_addr}:{rtp.remote_port}")
                    elif frames_sent % 250 == 0:
                        logger.info(f"xai_to_sip: {frames_sent} frames sent, queue={bridge.outbound_queue.qsize()}")
                    # Conference: also send to peer's RTP
                    peer = session.conference_peer
                    if peer and peer.state == 'connected' and peer.rtp_session:
                        try:
                            peer_frame = frame
                            if peer.codec == 8:
                                peer_frame = audioop.lin2alaw(audioop.ulaw2lin(frame, 2), 2)
                            elif peer.codec not in (0, 8):
                                continue
                            peer.rtp_session.send_audio(peer_frame, payload_type=peer.codec)
                        except OSError:
                            pass
                    await asyncio.sleep(RTP_FRAME_MS / 1000)
                else:
                    await asyncio.sleep(RTP_FRAME_MS / 1000)

        # Wait for the FIRST loop to finish, then cancel the rest.
        # asyncio.gather() waits for ALL of them, and xai_listener() blocks on
        # the still-open xAI websocket — so a caller hangup left gather()
        # pending forever and _end_call() never ran, leaking the call slot.
        loops = [
            asyncio.create_task(xai_listener()),
            asyncio.create_task(sip_to_xai()),
            asyncio.create_task(xai_to_sip()),
        ]
        try:
            done, pending = await asyncio.wait(loops, return_when=asyncio.FIRST_COMPLETED)
            for t in pending:
                t.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
        except asyncio.CancelledError:
            pass
        finally:
            await self._end_call(session)

    async def _end_call(self, session: CallSession):
        """Clean up a call session."""
        if session.state == 'ended':
            return
        session.state = 'ended'
        logger.info(f"Ending call {session.call_id[:16]}")

        # Disconnect xAI bridge
        if session.bridge:
            try:
                if session.bridge._listen_task:
                    session.bridge._listen_task.cancel()
                await session.bridge.disconnect()
            except Exception as e:
                logger.warning(f"Bridge disconnect error: {e}")

        # Remove conference peer reference
        if session.conference_peer:
            if session.conference_peer.conference_peer == session:
                session.conference_peer.conference_peer = None
            session.conference_peer = None

        # Send SIP BYE
        if self.sip_client and session.call_id in self.sip_client._active_calls:
            try:
                await self.sip_client.hangup(call_id=session.call_id)
            except Exception as e:
                logger.warning(f"Hangup error: {e}")

        # Cancel background task
        if session.task and not session.task.done():
            session.task.cancel()

        self.call_manager.remove(session.call_id)

    # ---- Tool execution --------------------------------------------------

    async def _handle_tool(self, session: CallSession, tool_call_id: str, name: str, args: dict) -> str:
        """Handle a tool call from xAI during an inbound call."""
        logger.info(f"Tool call: {name}({_redact_tool_args(args)}) on call {session.call_id[:16]} auth={session.authenticated}")

        if name == "authenticate":
            pin = str(args.get("pin", "") or "").strip()
            expected = str(self.config.get('auth', {}).get('pin', '') or '')
            if not expected or expected.startswith('${'):
                # No PIN configured, auto-pass
                session.authenticated = True
            elif pin == expected:
                session.authenticated = True
            else:
                logger.warning(f"Bad PIN attempt from {session.remote_number}")
                return "Wrong PIN. Please try again."

            if session.authenticated:
                logger.info(f"Caller {session.remote_number} authenticated on {session.call_id[:16]}")
                # Reconfigure xAI session with full tools and instructions
                if session.bridge and session.bridge.ws:
                    config = {
                        "type": "session.update",
                        "session": {
                            "instructions": self._compose_instructions(INBOUND_AUTH_INSTRUCTIONS),
                            "tools": TOOLS_AUTHENTICATED,
                        }
                    }
                    await session.bridge.ws.send(json.dumps(config))
                return "PIN accepted! You now have full access. How can I help you?"

        # hangup_call is never gated: ending a call must always be possible,
        # even for a caller who has not authenticated. Otherwise an
        # unauthenticated caller is trapped on the line with no way out.
        if name == "hangup_call":
            return await self._handle_hangup(session, args)

        # Gate all other tools behind authentication
        if not session.authenticated:
            return "You need to authenticate first. Please provide your PIN."

        if name == "process_query":
            query = args.get("query", "")
            if not query:
                return "I didn't catch that. Could you repeat?"
            return await run_openclaw_query(query, self.config)

        elif name == "schedule_outbound_call":
            number = args.get("number", "")
            task = args.get("task", "Say hello")
            if not number:
                return "I need a phone number to call."
            logger.info(f"schedule_outbound_call requested for '{number}' (task: {str(task)[:80]})")
            call_id = await self.make_outbound_call(number, task)
            if call_id:
                return (
                    f"Outbound call started (call_id: {call_id}). "
 f"The call to {number} is connecting. I'll handle the conversation."
                )
            return "Failed to start the outbound call. The line may be busy or max calls reached."

        elif name == "connect_to_call":
            target_id = args.get("call_id", "")
            target = self.call_manager.get(target_id)
            if not target or target.state != 'connected':
                return "That call is not active."
            session.conference_peer = target
            target.conference_peer = session
            return "Connected! You can now hear the other call. I'm bridging audio between both parties."

        elif name == "hangup_call":
            return await self._handle_hangup(session, args)

        return f"Unknown tool: {name}"

    async def _handle_hangup(self, session: CallSession, args: dict) -> str:
        """End the call after the agent has finished speaking.

        The agent says its goodbye first and then calls hangup_call, so we
        must not cut the line mid-sentence. Wait until the bridge reports it
        has stopped speaking, give the final RTP frames a moment to flush,
        then send BYE.
        """
        reason = str(args.get("reason", "") or "conversation complete")
        logger.info(
            f"hangup_call requested on call {session.call_id[:16]} "
            f"(reason: {reason})"
        )

        # Guard against a goodbye loop. The model sometimes calls hangup_call
        # repeatedly, and each tool result nudges it to say goodbye again.
        if session.hangup_scheduled:
            return "Already ending. Say nothing further."
        session.hangup_scheduled = True

        async def _hangup_when_done():
            try:
                deadline = time.monotonic() + 30.0
                while time.monotonic() < deadline:
                    bridge = session.bridge
                    if bridge and bridge.is_speaking:
                        await asyncio.sleep(0.2)
                        continue
                    # The model can emit its message AND the hangup_call tool
                    # call in the same response. In that case is_speaking is
                    # already False but the audio is still queued, and
                    # hanging up immediately cut the message off mid-sentence
                    # (the party only heard the first few words).
                    #
                    # So: wait for the outbound queue to drain as well, then
                    # give the final RTP frames a moment to flush.
                    if bridge and bridge.outbound_queue.qsize() > 0:
                        await asyncio.sleep(0.2)
                        continue
                    # Queue is empty and the model is not speaking. Wait a
                    # beat for the last frames to reach the phone.
                    await asyncio.sleep(1.5)
                    if bridge and bridge.outbound_queue.qsize() > 0:
                        # More audio arrived during the flush wait.
                        continue
                    break
                await self._end_call(session)
                logger.info(
                    f"hangup_call completed on call {session.call_id[:16]}"
                )
            except Exception as e:
                logger.warning(f"hangup_call failed: {e}")

        asyncio.create_task(_hangup_when_done())
        # The wording matters: "say your goodbye first" made the model repeat
        # the goodbye several times before the call actually dropped.
        return "Ending the call now. Do not speak again."

    # ---- HTTP API --------------------------------------------------------

    async def _start_api(self):
        """Start the aiohttp HTTP API server."""
        app = web.Application()
        app.router.add_post('/call', self._api_make_call)
        app.router.add_get('/calls', self._api_list_calls)
        app.router.add_delete('/call/{call_id}', self._api_hangup)
        app.router.add_get('/health', self._api_health)
        app.router.add_post('/call/{call_id}/conference', self._api_conference)

        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, '127.0.0.1', self.api_port)
        await site.start()

        self._app = app
        self._runner = runner

    async def _api_make_call(self, request: web.Request) -> web.Response:
        try:
            data = await request.json()
        except Exception:
            return web.json_response({'error': 'invalid JSON'}, status=400)

        number = data.get('number', '')
        task = data.get('task', 'Say hello and have a brief conversation')
        if not number:
            return web.json_response({'error': 'number is required'}, status=400)

        call_id = await self.make_outbound_call(number, task)
        if call_id:
            return web.json_response({'call_id': call_id, 'status': 'connecting'})
        return web.json_response({'error': 'call failed or max concurrent reached'}, status=503)

    async def _api_list_calls(self, request: web.Request) -> web.Response:
        return web.json_response({'calls': self.call_manager.to_list()})

    async def _api_hangup(self, request: web.Request) -> web.Response:
        call_id = request.match_info['call_id']
        session = self.call_manager.get(call_id)
        if not session or session.state == 'ended':
            return web.json_response({'error': 'call not found'}, status=404)
        await self._end_call(session)
        return web.json_response({'status': 'ended', 'call_id': call_id})

    async def _api_health(self, request: web.Request) -> web.Response:
        registered = self.sip_client.registered if self.sip_client else False
        active_count = len(self.call_manager.active())
        health = {
            'registered': registered,
            'active_calls': active_count,
            'max_concurrent': self.call_manager.max_concurrent,
            'uptime_s': int(time.time() - self._start_time) if hasattr(self, '_start_time') else 0,
        }

        # Per-customer identity, so a fleet health check can tell which
        # instance is which and which extension it answers on.
        if self.instance:
            health['instance'] = self.instance
        sip_cfg = self.config.get('sip', {})
        if sip_cfg.get('server'):
            health['sip_server'] = sip_cfg['server']
        if sip_cfg.get('extension'):
            health['extension'] = str(sip_cfg['extension'])
        health['api_port'] = self.api_port

        return web.json_response(health)

    async def _api_conference(self, request: web.Request) -> web.Response:
        call_id = request.match_info['call_id']
        target = self.call_manager.get(call_id)
        if not target or target.state != 'connected':
            return web.json_response({'error': 'target call not active'}, status=404)

        # Find the inbound call to bridge
        inbound = None
        for s in self.call_manager.active():
            if s.direction == 'inbound' and s.call_id != call_id:
                inbound = s
                break

        if not inbound:
            return web.json_response({'error': 'no inbound call to bridge'}, status=404)

        inbound.conference_peer = target
        target.conference_peer = inbound
        return web.json_response({
            'status': 'conference_active',
            'inbound': inbound.call_id,
            'outbound': target.call_id,
        })


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def main():
    parser = argparse.ArgumentParser(description="OpenClaw Voice")
    parser.add_argument('--no-api', action='store_true', help='Disable HTTP API')
    args = parser.parse_args()

    # Allow SIP-only registration without an xAI key (for smoke tests / receptionist
    # wiring before the voice backend is selected). Set SKIP_XAI_VALIDATION=1 in .env.
    skip_xai = os.getenv('SKIP_XAI_VALIDATION', '').lower() in ('1', 'true', 'yes')
    if skip_xai:
        logger.warning("SKIP_XAI_VALIDATION set — SIP registration only, no audio bridge")

    config = load_config()

    # Validate
    xai_key = config.get('xai', {}).get('api_key', '')
    if not skip_xai:
        if not xai_key or xai_key.startswith('${'):
            logger.error("XAI_API_KEY not set. export XAI_API_KEY=...")
            sys.exit(1)
    sip_pw = config.get('sip', {}).get('password', '')
    if not sip_pw or sip_pw.startswith('${'):
        logger.error("SIP_PASSWORD not set. export SIP_PASSWORD=...")
        sys.exit(1)

    daemon = OpenClawVoice(config)
    daemon._start_time = time.time()

    # Graceful shutdown on SIGTERM/SIGINT
    loop = asyncio.get_event_loop()

    def handle_signal():
        logger.info("Signal received, shutting down...")
        asyncio.ensure_future(daemon.stop())

    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, handle_signal)

    # SIGHUP: reload config (re-register SIP)
    def handle_sighup():
        logger.info("SIGHUP received")

    loop.add_signal_handler(signal.SIGHUP, handle_sighup)

    await daemon.start(enable_api=not args.no_api)


if __name__ == "__main__":
    asyncio.run(main())
