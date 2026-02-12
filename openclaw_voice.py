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
    load_dotenv()
    config_path = Path(__file__).parent / "config.yaml"
    with open(config_path) as f:
        config_str = f.read()
    for key in ['XAI_API_KEY', 'SIP_PASSWORD', 'OPENCLAW_GATEWAY_TOKEN', 'OPENCLAW_AUTH_PIN']:
        placeholder = f'${{{key}}}'
        if placeholder in config_str:
            config_str = config_str.replace(placeholder, os.getenv(key, placeholder))
    return yaml.safe_load(config_str)


# ---------------------------------------------------------------------------
# Tool definitions for xAI voice agent
# ---------------------------------------------------------------------------

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
]

INBOUND_UNAUTH_INSTRUCTIONS = """\
You are OpenClaw, a helpful voice assistant. The user is calling you on the phone.

The caller has NOT been authenticated yet. You can have a basic conversation, but \
you CANNOT use any tools until they provide their PIN.

Ask the caller for their PIN code to unlock full capabilities. When they give you \
a PIN, use the authenticate tool to verify it. Be friendly but firm — do not try \
to answer questions that would require tools until authenticated.

STYLE: Be natural, conversational, and concise. You're on a phone call."""

INBOUND_AUTH_INSTRUCTIONS = """\
You are OpenClaw, a helpful voice assistant. The user is calling you on the phone.

CAPABILITIES:
- Answer questions, have conversations
- Run commands and check system status via process_query
- Make outbound calls on the user's behalf via schedule_outbound_call
- Bridge calls together via connect_to_call

STYLE: Be natural, conversational, and concise. You're on a phone call.

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
- Identify yourself as OpenClaw, a voice assistant calling on behalf of your user
- Stay focused on the task
- Be polite and professional
- Confirm outcomes before hanging up
- Say goodbye when done"""


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
        auth_pin = self.config.get('auth', {}).get('pin', '')
        has_auth = bool(auth_pin and not auth_pin.startswith('${'))

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

        session.task_context = instructions

        if not self.call_manager.add(session):
            logger.warning("Max concurrent calls reached, rejecting inbound")
            await self.sip_client.hangup(call_id=call_id)
            return

        await self._run_call(session, instructions, is_inbound=True)

    # ---- Outbound call handling ------------------------------------------

    async def make_outbound_call(self, number: str, task: str) -> Optional[str]:
        """Initiate an outbound call. Returns call_id or None on failure."""
        instructions = OUTBOUND_TEMPLATE.format(task=task)
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

        # Wait briefly for call_id to be populated
        for _ in range(50):  # 5 seconds max
            await asyncio.sleep(0.1)
            if 'id' in call_id_holder:
                return call_id_holder['id']

        # Return a placeholder - the call might still connect
        return None

    # ---- Core call bridge ------------------------------------------------

    async def _run_call(self, session: CallSession, instructions: str, is_inbound: bool):
        """Run the audio bridge between SIP and xAI for one call."""
        # Pick tools based on auth state
        if not is_inbound:
            tools = []
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

        # Set up tool handler for inbound calls
        if is_inbound:
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
                        await bridge.send_audio(audio)
                    else:
                        await asyncio.sleep(0.005)
                except OSError:
                    break  # RTP socket closed (call ended)
                except Exception as e:
                    logger.error(f"sip_to_xai error: {e}")
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
                        rtp.send_audio(frame, payload_type=0)
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
                            peer.rtp_session.send_audio(frame, payload_type=0)
                        except OSError:
                            pass
                    await asyncio.sleep(RTP_FRAME_MS / 1000)
                else:
                    await asyncio.sleep(RTP_FRAME_MS / 1000)

        try:
            await asyncio.gather(xai_listener(), sip_to_xai(), xai_to_sip())
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
        logger.info(f"Tool call: {name}({args}) on call {session.call_id[:16]} auth={session.authenticated}")

        if name == "authenticate":
            pin = args.get("pin", "").strip()
            expected = self.config.get('auth', {}).get('pin', '')
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
                            "instructions": INBOUND_AUTH_INSTRUCTIONS,
                            "tools": TOOLS_AUTHENTICATED,
                        }
                    }
                    await session.bridge.ws.send(json.dumps(config))
                return "PIN accepted! You now have full access. How can I help you?"

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
            call_id = await self.make_outbound_call(number, task)
            if call_id:
                return f"Outbound call started (call_id: {call_id[:16]}). I'll handle the conversation."
            return "Failed to start the outbound call. The line may be busy or max calls reached."

        elif name == "connect_to_call":
            target_id = args.get("call_id", "")
            target = self.call_manager.get(target_id)
            if not target or target.state != 'connected':
                return "That call is not active."
            session.conference_peer = target
            target.conference_peer = session
            return "Connected! You can now hear the other call. I'm bridging audio between both parties."

        return f"Unknown tool: {name}"

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
        return web.json_response({
            'registered': registered,
            'active_calls': active_count,
            'max_concurrent': self.call_manager.max_concurrent,
            'uptime_s': int(time.time() - self._start_time) if hasattr(self, '_start_time') else 0,
        })

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

    config = load_config()

    # Validate
    xai_key = config.get('xai', {}).get('api_key', '')
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
