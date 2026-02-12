#!/usr/bin/env python3
"""
Minimal SIP Client for Voice Trading Agent
Direct socket implementation - no external SIP library dependencies
Connects to Asterisk server for voice trading agent
"""

import socket
import hashlib
import asyncio
import logging
import re
import uuid
import time
import struct
from typing import Optional, Callable, Dict, Any, Tuple
from datetime import datetime

logger = logging.getLogger(__name__)


class RTPSession:
    """Minimal RTP session for audio streaming"""

    def __init__(self, local_port: int = 0):
        self.socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.socket.bind(('0.0.0.0', local_port))
        self.local_port = self.socket.getsockname()[1]
        self.remote_addr = None
        self.remote_port = None
        self.sequence = 0
        self.timestamp = 0
        self.ssrc = hash(uuid.uuid4()) & 0xFFFFFFFF

    def set_remote(self, addr: str, port: int):
        """Set remote RTP endpoint"""
        self.remote_addr = addr
        self.remote_port = port

    def send_audio(self, payload: bytes, payload_type: int = 0):
        """
        Send RTP packet with audio payload
        payload_type: 0=PCMU (G.711 μ-law), 8=PCMA (G.711 A-law), 9=G.722
        """
        if not self.remote_addr:
            logger.warning(f"Cannot send audio: remote_addr not set")
            return

        # RTP header (12 bytes)
        version = 2
        padding = 0
        extension = 0
        csrc_count = 0
        marker = 0

        byte0 = (version << 6) | (padding << 5) | (extension << 4) | csrc_count
        byte1 = (marker << 7) | payload_type

        header = struct.pack('!BBHII',
            byte0,
            byte1,
            self.sequence & 0xFFFF,
            self.timestamp & 0xFFFFFFFF,
            self.ssrc
        )

        packet = header + payload

        # Send packet
        try:
            bytes_sent = self.socket.sendto(packet, (self.remote_addr, self.remote_port))
            if self.sequence % 50 == 0:  # Log every 50th packet (1 second)
                logger.debug(f"RTP #{self.sequence}: sent {bytes_sent} bytes to {self.remote_addr}:{self.remote_port}, ts={self.timestamp}")
        except Exception as e:
            logger.error(f"Failed to send RTP packet: {e}")

        self.sequence += 1
        # Timestamp increment depends on codec:
        # - G.711 (PCMU/PCMA): 1 byte = 1 sample @ 8kHz clock
        # - G.722: RTP clock is 8kHz (historical quirk), but encodes 16kHz audio
        #          80 bytes = 160 samples @ 8kHz RTP clock = 20ms
        if payload_type == 9:  # G.722
            # G.722 uses 8kHz RTP clock, 80 bytes = 160 timestamp units (20ms)
            self.timestamp += 160
        else:
            # G.711: 1 byte = 1 sample @ 8kHz
            self.timestamp += len(payload)

    def receive_audio(self, timeout: float = 0.1) -> Optional[bytes]:
        """Receive RTP packet and extract audio payload"""
        self.socket.settimeout(timeout)
        try:
            data, addr = self.socket.recvfrom(2048)
            if len(data) < 12:
                return None

            # Parse RTP header (skip it, return payload)
            return data[12:]
        except socket.timeout:
            return None
        except Exception as e:
            logger.error(f"RTP receive error: {e}")
            return None

    def close(self):
        """Close RTP socket"""
        self.socket.close()


class SIPProtocol(asyncio.DatagramProtocol):
    """Asyncio UDP protocol for SIP message dispatch.

    Routes incoming datagrams:
    - SIP responses -> per-Call-ID asyncio.Queue for register/make_call
    - INVITE -> _handle_invite task
    - ACK -> sets _pending_ack event
    - OPTIONS/BYE/NOTIFY -> synchronous handlers
    """

    def __init__(self, sip_client: 'SIPClient', call_handler: Callable):
        self.sip_client = sip_client
        self.call_handler = call_handler
        self.transport = None

    def connection_made(self, transport):
        self.transport = transport
        logger.info("SIPProtocol: datagram endpoint ready")

    def datagram_received(self, data: bytes, addr: tuple):
        try:
            message = data.decode('utf-8', errors='ignore')
        except Exception:
            return

        if not message.strip():
            return  # CRLF keepalive response or empty

        first_line = message.split('\r\n')[0] if '\r\n' in message else message.split('\n')[0]
        logger.debug(f"SIPProtocol recv from {addr}: {first_line}")

        if first_line.startswith('SIP/2.0'):
            # SIP response — route to per-Call-ID queue
            call_id = self.sip_client._extract_header(message, 'Call-ID')
            q = self.sip_client._response_queues.get(call_id)
            if q:
                q.put_nowait((message, addr))
            else:
                logger.debug(f"No queue for response Call-ID {call_id[:24]}")
        elif first_line.startswith('INVITE '):
            logger.info(f"Incoming INVITE from {addr}")
            asyncio.ensure_future(
                self.sip_client._handle_invite(message, addr, self.call_handler))
        elif first_line.startswith('ACK '):
            call_id = self.sip_client._extract_header(message, 'Call-ID')
            event = self.sip_client._pending_ack.get(call_id)
            if event:
                event.set()
            logger.debug(f"Received ACK for {call_id[:24]}")
        elif first_line.startswith('OPTIONS '):
            self.sip_client._handle_options(message, addr)
        elif first_line.startswith('BYE '):
            self.sip_client._handle_bye(message, addr)
        elif first_line.startswith('NOTIFY '):
            self.sip_client._handle_notify(message, addr)
        elif first_line.startswith('CANCEL '):
            logger.info(f"Received CANCEL from {addr} (not handled)")
        else:
            logger.debug(f"Unhandled SIP message: {first_line}")

    def error_received(self, exc):
        logger.error(f"SIPProtocol error: {exc}")

    def connection_lost(self, exc):
        if exc:
            logger.warning(f"SIPProtocol connection lost: {exc}")


class SIPClient:
    """
    Minimal SIP client implementation
    Supports REGISTER, INVITE handling, and basic call management
    """

    def __init__(self, config: Dict[str, Any]):
        """
        Initialize SIP client

        Args:
            config: Dict with sip_server, sip_port, extension, password, display_name
        """
        self.server = config['sip_server']
        self.port = int(config.get('sip_port', 5060))
        self.extension = config['extension']
        self.password = config['password']
        self.display_name = config.get('display_name', 'Voice Agent')

        # SIP signaling port - must match Contact header for incoming INVITEs
        self.sip_port = int(config.get('sip_local_port', 5060))
        self.external_ip = config.get('external_ip', '')

        # RTP port range
        self.rtp_port_start = int(config.get('rtp_port', 40000))

        logger.info(f"SIP Client initialized:")
        logger.info(f"  Server: {self.server}:{self.port}")
        logger.info(f"  Extension: {self.extension}")
        logger.info(f"  Contact: {self.external_ip or 'auto'}:{self.sip_port}")

        self.socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            self.socket.bind(('0.0.0.0', self.sip_port))
            self.local_port = self.sip_port
        except OSError:
            # Port in use, fall back to random
            self.socket.bind(('0.0.0.0', 0))
            self.local_port = self.socket.getsockname()[1]
            logger.warning(f"Port {self.sip_port} in use, using {self.local_port}")

        # Get the actual IP that will be used to reach the SIP server
        self.local_ip = self._get_local_ip()

        self.call_id = None
        self.from_tag = None
        self.to_tag = None
        self.branch = None
        self.cseq = 1

        self.registered = False
        self.active_call = None
        self.rtp_session: Optional[RTPSession] = None
        self.negotiated_codec: int = 0  # 0=PCMU, 9=G.722
        self.contact_port = self.local_port

        # Per-call state for concurrent call support
        # Keys: SIP Call-ID, Values: dict with rtp_session, codec, from_tag, to_tag, etc.
        self._active_calls: Dict[str, dict] = {}

        self.running = False
        self._re_register_task: Optional[asyncio.Task] = None

        # Asyncio transport (set by listen_for_calls)
        self._transport = None
        self._protocol: Optional[SIPProtocol] = None
        self._response_queues: Dict[str, asyncio.Queue] = {}
        self._pending_ack: Dict[str, asyncio.Event] = {}
        self._keepalive_task: Optional[asyncio.Task] = None

    # ------------------------------------------------------------------
    # Transport abstraction
    # ------------------------------------------------------------------

    def _send(self, data: bytes, addr: tuple):
        """Send data via asyncio transport if available, else raw socket."""
        if self._transport:
            self._transport.sendto(data, addr)
        else:
            self.socket.sendto(data, addr)

    def _create_response_queue(self, call_id: str) -> asyncio.Queue:
        """Create a per-Call-ID response queue."""
        q = asyncio.Queue()
        self._response_queues[call_id] = q
        return q

    def _remove_response_queue(self, call_id: str):
        """Remove a per-Call-ID response queue."""
        self._response_queues.pop(call_id, None)

    async def _recv_filtered(self, call_id: str, timeout: float) -> Tuple[Optional[str], Optional[tuple]]:
        """Receive next SIP response for call_id.

        Transport mode: reads from per-call-id queue (protocol dispatches requests).
        Blocking mode: reads from socket, dispatches interleaved requests inline.
        """
        if self._transport:
            q = self._response_queues.get(call_id)
            if not q:
                return None, None
            try:
                return await asyncio.wait_for(q.get(), timeout)
            except asyncio.TimeoutError:
                return None, None
        else:
            # Blocking mode — handle interleaved server requests
            deadline = time.time() + timeout
            while time.time() < deadline:
                remaining = deadline - time.time()
                if remaining <= 0:
                    return None, None
                self.socket.settimeout(max(remaining, 0.1))
                try:
                    data, addr = self.socket.recvfrom(8192)
                    message = data.decode('utf-8', errors='ignore')
                    first_line = message.split('\r\n')[0] if '\r\n' in message else message.split('\n')[0]

                    # Dispatch interleaved requests
                    if first_line.startswith('OPTIONS '):
                        self._handle_options(message, addr)
                        continue
                    elif first_line.startswith('NOTIFY '):
                        self._handle_notify(message, addr)
                        continue
                    elif first_line.startswith('BYE '):
                        self._handle_bye(message, addr)
                        continue

                    # For responses, filter by Call-ID
                    if first_line.startswith('SIP/2.0'):
                        resp_cid = self._extract_header(message, 'Call-ID')
                        if resp_cid and resp_cid != call_id:
                            logger.debug(f"Ignoring stale response for Call-ID {resp_cid[:16]}...")
                            continue

                    return message, addr
                except socket.timeout:
                    return None, None
            return None, None

    # ------------------------------------------------------------------
    # Phase 1: Unregister all stale contacts
    # ------------------------------------------------------------------

    async def unregister_all(self) -> bool:
        """Send REGISTER with Contact: * and Expires: 0 to clear all bindings.

        RFC 3261 Section 10.2.2 — removes every Contact for this AOR so that
        voipgateway.org stops forking INVITEs to dead endpoints.
        """
        logger.info("Unregistering all contacts (Contact: *, Expires: 0)...")
        try:
            unreg_call_id = self._generate_call_id()
            from_tag = self._generate_tag()
            branch = self._generate_branch()
            cseq = 1

            sip_uri = f"sip:{self.server}"
            from_uri = f"sip:{self.extension}@{self.server}"

            def build_unreg(auth_header=None):
                lines = [
                    f"REGISTER {sip_uri} SIP/2.0",
                    f"Via: SIP/2.0/UDP {self.local_ip}:{self.local_port};branch={branch}",
                    f'From: "{self.display_name}" <{from_uri}>;tag={from_tag}',
                    f"To: <{from_uri}>",
                    f"Call-ID: {unreg_call_id}",
                    f"CSeq: {cseq} REGISTER",
                    "Contact: *",
                    "Expires: 0",
                    "Max-Forwards: 70",
                    "User-Agent: OpenClaw-Voice/1.0",
                ]
                if auth_header:
                    lines.append(auth_header)
                lines.append("Content-Length: 0")
                lines.append("")
                lines.append("")
                return "\r\n".join(lines)

            # Create response queue if transport is active
            if self._transport:
                self._create_response_queue(unreg_call_id)

            # Send initial unregister
            self._send(build_unreg().encode(), (self.server, self.port))

            # Wait for response
            response_text, addr = await self._recv_filtered(unreg_call_id, 5.0)
            if not response_text:
                logger.warning("No response to unregister request")
                self._remove_response_queue(unreg_call_id)
                return False

            # Handle 401 challenge
            if '401 Unauthorized' in response_text:
                realm, nonce, qop, opaque = self._parse_auth_challenge(response_text)
                if not realm.strip() or not nonce.strip():
                    logger.error("Failed to parse auth challenge for unregister")
                    self._remove_response_queue(unreg_call_id)
                    return False

                cnonce = uuid.uuid4().hex[:8]
                nc = "00000001"
                auth_response, cnonce_used = self._compute_auth_response(
                    'REGISTER', sip_uri, realm, nonce, qop=qop, nc=nc, cnonce=cnonce)

                auth_parts = [
                    f'username="{self.extension}"',
                    f'realm="{realm}"',
                    f'nonce="{nonce}"',
                    f'uri="{sip_uri}"',
                    f'response="{auth_response}"',
                    'algorithm=MD5',
                ]
                if qop:
                    auth_parts.extend([f'qop={qop}', f'nc={nc}', f'cnonce="{cnonce_used}"'])
                if opaque:
                    auth_parts.append(f'opaque="{opaque}"')

                auth_header = 'Authorization: Digest ' + ', '.join(auth_parts)

                cseq += 1
                branch = self._generate_branch()

                # Rebuild with auth — need to capture new cseq/branch in closure
                lines = [
                    f"REGISTER {sip_uri} SIP/2.0",
                    f"Via: SIP/2.0/UDP {self.local_ip}:{self.local_port};branch={branch}",
                    f'From: "{self.display_name}" <{from_uri}>;tag={from_tag}',
                    f"To: <{from_uri}>",
                    f"Call-ID: {unreg_call_id}",
                    f"CSeq: {cseq} REGISTER",
                    "Contact: *",
                    "Expires: 0",
                    "Max-Forwards: 70",
                    "User-Agent: OpenClaw-Voice/1.0",
                    auth_header,
                    "Content-Length: 0",
                    "",
                    "",
                ]
                self._send("\r\n".join(lines).encode(), (self.server, self.port))

                # Wait for 200 OK
                response_text, addr = await self._recv_filtered(unreg_call_id, 10.0)
                if not response_text:
                    logger.warning("No response to authenticated unregister")
                    self._remove_response_queue(unreg_call_id)
                    return False

            if 'SIP/2.0 200' in response_text:
                # Count Contact headers in response
                contacts = [l for l in response_text.replace('\r\n', '\n').split('\n')
                            if l.lower().startswith('contact:')]
                logger.info(f"Unregister OK — {len(contacts)} Contact(s) remaining in 200 OK")
                self._remove_response_queue(unreg_call_id)
                return True
            else:
                first_line = response_text.split('\r\n')[0]
                logger.warning(f"Unregister got unexpected response: {first_line}")
                self._remove_response_queue(unreg_call_id)
                return False

        except Exception as e:
            logger.error(f"Unregister error: {e}")
            self._remove_response_queue(unreg_call_id if 'unreg_call_id' in dir() else '')
            return False

    # ------------------------------------------------------------------
    # Phase 2: Parse server-granted Expires
    # ------------------------------------------------------------------

    def _parse_server_expires(self, response_text: str) -> int:
        """Parse Expires from 200 OK response.

        Checks (in order):
        1. expires= param on our Contact header
        2. Top-level Expires header
        3. Fallback: 1800s
        """
        # Check for expires= in Contact header
        for line in response_text.replace('\r\n', '\n').split('\n'):
            if line.lower().startswith('contact:') and self.extension in line:
                m = re.search(r'expires=(\d+)', line, re.IGNORECASE)
                if m:
                    return int(m.group(1))

        # Check top-level Expires header
        for line in response_text.replace('\r\n', '\n').split('\n'):
            if line.startswith('Expires:'):
                try:
                    return int(line.split(':', 1)[1].strip())
                except ValueError:
                    pass

        return 1800  # fallback

    # ------------------------------------------------------------------
    # Registration
    # ------------------------------------------------------------------

    async def register(self) -> bool:
        """Register to SIP server.

        Phase 1: Clears all stale contacts first via unregister_all().
        Phase 2: Parses server-granted Expires and schedules re-register at half.
        """
        try:
            # Phase 1: Clear stale registrations
            await self.unregister_all()

            # Small delay so server processes the unregister
            await asyncio.sleep(0.5)

            # Send initial REGISTER (generates new Call-ID and From tag)
            request = self._build_register_request(initial=True)

            # Set up response queue if transport is active
            if self._transport:
                self._create_response_queue(self.call_id)

            self._send(request.encode(), (self.server, self.port))

            # Wait for response
            response_text, addr = await self._recv_filtered(self.call_id, 5.0)
            if not response_text:
                logger.error("No response to REGISTER")
                self._remove_response_queue(self.call_id)
                return False

            logger.info(f"Received SIP response")
            logger.debug(f"Full response: {response_text}")

            # Check if we got 200 OK immediately (no auth required)
            if 'SIP/2.0 200 OK' in response_text:
                logger.info("Server accepted registration without authentication")
                self.registered = True
                server_expires = self._parse_server_expires(response_text)
                self._log_contacts(response_text)
                self._schedule_re_register(server_expires // 2)
                self._remove_response_queue(self.call_id)
                return True

            # Check for 401 Unauthorized
            if '401 Unauthorized' not in response_text:
                logger.error(f"Unexpected response: {response_text[:500]}")
                self._remove_response_queue(self.call_id)
                return False

            # Parse authentication challenge
            realm, nonce, qop, opaque = self._parse_auth_challenge(response_text)
            if not realm.strip() or not nonce.strip():
                logger.error("Failed to parse authentication challenge")
                self._remove_response_queue(self.call_id)
                return False

            # Generate cnonce and nc for qop=auth
            cnonce = uuid.uuid4().hex[:8]
            nc = "00000001"

            # Compute authentication response
            logger.info(f"Authenticating with realm={realm}, qop={qop}, opaque={opaque[:20] if opaque else None}")
            auth_response, cnonce_used = self._compute_auth_response(
                'REGISTER',
                f"sip:{self.server}",
                realm,
                nonce,
                qop=qop,
                nc=nc,
                cnonce=cnonce
            )
            cnonce = cnonce_used  # Use the cnonce from auth response

            # Build Authorization header
            auth_parts = [
                f'username="{self.extension}"',
                f'realm="{realm}"',
                f'nonce="{nonce}"',
                f'uri="sip:{self.server}"',
                f'response="{auth_response}"',
                'algorithm=MD5'
            ]

            if qop:
                auth_parts.append(f'qop={qop}')
                auth_parts.append(f'nc={nc}')
                auth_parts.append(f'cnonce="{cnonce}"')

            if opaque:
                auth_parts.append(f'opaque="{opaque}"')

            auth_header = 'Authorization: Digest ' + ', '.join(auth_parts)

            logger.info(f"Authorization header: {auth_header}")

            # Send authenticated REGISTER (keep same Call-ID and From tag)
            self.cseq += 1
            request = self._build_register_request(auth_header, initial=False)
            logger.info(f"Sending authenticated REGISTER")
            logger.debug(f"Request:\n{request}")
            self._send(request.encode(), (self.server, self.port))

            # Wait for 200 OK
            deadline = time.time() + 10.0
            while time.time() < deadline:
                remaining = deadline - time.time()
                response_text, addr = await self._recv_filtered(self.call_id, remaining)
                if not response_text:
                    break

                first_line = response_text.split('\r\n')[0] if '\r\n' in response_text else response_text.split('\n')[0]
                logger.info(f"Received during registration: {first_line}")

                if first_line.startswith('SIP/2.0 200'):
                    self.registered = True
                    server_expires = self._parse_server_expires(response_text)
                    self._log_contacts(response_text)
                    interval = max(server_expires // 2, 15)
                    logger.info(f"SIP registered as {self.extension} (server expires={server_expires}s, re-register in {interval}s)")
                    self._schedule_re_register(interval)
                    self._remove_response_queue(self.call_id)
                    return True
                elif '403' in first_line or '401' in first_line:
                    logger.error(f"Auth failed: {first_line}")
                    logger.error(f"Response: {response_text[:1000]}")
                    self._remove_response_queue(self.call_id)
                    return False
                else:
                    logger.debug(f"Ignoring during registration: {first_line}")

            logger.error("Registration timed out waiting for 200 OK")
            self._remove_response_queue(self.call_id)
            return False

        except Exception as e:
            logger.error(f"Registration error: {e}")
            if self.call_id:
                self._remove_response_queue(self.call_id)
            return False

    def _log_contacts(self, response_text: str):
        """Log Contact headers from a registration response."""
        contacts = []
        for line in response_text.replace('\r\n', '\n').split('\n'):
            if line.lower().startswith('contact:'):
                contacts.append(line)
        logger.info(f"Server returned {len(contacts)} Contact binding(s):")
        for c in contacts:
            logger.info(f"  {c}")

    # ------------------------------------------------------------------
    # Phase 3: CRLF Keepalive
    # ------------------------------------------------------------------

    async def _keepalive_loop(self):
        """Send CRLF keepalive every 30s to maintain NAT binding (RFC 5626 §3.5.1)."""
        logger.info("CRLF keepalive loop started (30s interval)")
        while self.running:
            try:
                self._send(b"\r\n\r\n", (self.server, self.port))
                logger.debug("Sent CRLF keepalive")
            except Exception as e:
                logger.warning(f"Keepalive send failed: {e}")
            await asyncio.sleep(30)

    # ------------------------------------------------------------------
    # Phase 4: Unified socket dispatch via asyncio transport
    # ------------------------------------------------------------------

    async def listen_for_calls(self, call_handler: Callable):
        """Listen for incoming SIP requests using asyncio datagram transport.

        Creates an asyncio DatagramProtocol from the existing socket, eliminating
        the run_in_executor threading race. All sends go through transport.sendto().
        """
        self.running = True
        loop = asyncio.get_event_loop()

        # Make socket non-blocking for asyncio
        self.socket.setblocking(False)

        # Create asyncio datagram endpoint from existing socket
        transport, protocol = await loop.create_datagram_endpoint(
            lambda: SIPProtocol(self, call_handler),
            sock=self.socket
        )
        self._transport = transport
        self._protocol = protocol

        logger.info(f"Listening for incoming calls on port {self.local_port} (asyncio transport)")

        # Start CRLF keepalive
        self._keepalive_task = loop.create_task(self._keepalive_loop())

        # Keep running until stopped
        try:
            while self.running:
                await asyncio.sleep(1)
        finally:
            if self._keepalive_task and not self._keepalive_task.done():
                self._keepalive_task.cancel()
            transport.close()
            self._transport = None
            self._protocol = None

    # ------------------------------------------------------------------
    # Re-registration
    # ------------------------------------------------------------------

    def _schedule_re_register(self, interval: int = 900):
        """Schedule periodic re-registration. Default 900s (half of typical 1800s expiry)."""
        if self._re_register_task and not self._re_register_task.done():
            self._re_register_task.cancel()
        try:
            loop = asyncio.get_event_loop()
            self._re_register_task = loop.create_task(self._re_register_loop(interval))
        except RuntimeError:
            logger.warning("No event loop available for re-registration scheduling")

    async def _re_register_loop(self, interval: int):
        """Periodically re-register to keep registration alive."""
        while self.running or self.registered:
            await asyncio.sleep(interval)
            if not self.registered:
                break
            logger.info(f"Re-registering (interval={interval}s)")
            try:
                result = await self.register()
                if not result:
                    logger.error("Re-registration failed")
            except Exception as e:
                logger.error(f"Re-registration error: {e}")

    # ------------------------------------------------------------------
    # Outbound calls
    # ------------------------------------------------------------------

    async def make_call(self, target_extension: str, call_handler: Callable) -> bool:
        """
        Make an outbound SIP call with authentication

        Args:
            target_extension: Extension to call (e.g., "299")
            call_handler: Async function to handle the call (receives RTPSession, codec, call_id)

        Returns:
            True if call connected, False otherwise
        """
        try:
            logger.info(f"Making outbound call to extension {target_extension}")

            # Generate call parameters
            call_id = self._generate_call_id()
            from_tag = self._generate_tag()
            branch = self._generate_branch()
            invite_cseq = self.cseq

            from_uri = f"sip:{self.extension}@{self.server}"
            to_uri = f"sip:{target_extension}@{self.server}"
            contact_uri = f"sip:{self.extension}@{self.external_ip or self.local_ip}:{self.contact_port}"

            # Create RTP session first so SDP has the correct port (per-call)
            rtp_session = RTPSession()
            self.rtp_session = rtp_session  # backwards compat

            # Build SDP for audio offer using the actual RTP port
            sdp = self._build_sdp(rtp_session.local_port)

            # Build initial INVITE request (without auth)
            invite_lines = [
                f"INVITE {to_uri} SIP/2.0",
                f"Via: SIP/2.0/UDP {self.local_ip}:{self.local_port};branch={branch}",
                f'From: "{self.display_name}" <{from_uri}>;tag={from_tag}',
                f"To: <{to_uri}>",
                f"Call-ID: {call_id}",
                f"CSeq: {invite_cseq} INVITE",
                f"Contact: <{contact_uri}>",
                "Max-Forwards: 70",
                "User-Agent: OpenClaw-Voice/1.0",
                "Content-Type: application/sdp",
                f"Content-Length: {len(sdp)}",
                "",
                sdp,
            ]
            invite_request = "\r\n".join(invite_lines)

            # Set up response queue if transport is active
            if self._transport:
                self._create_response_queue(call_id)

            # Send initial INVITE
            self._send(invite_request.encode(), (self.server, self.port))
            logger.debug(f"Sent initial INVITE to {target_extension}")

            # Wait for responses — 30s timeout to allow phone to ring
            auth_attempted = False
            deadline = time.time() + 30.0

            while time.time() < deadline:
                remaining = deadline - time.time()
                response_text, addr = await self._recv_filtered(call_id, min(remaining, 5.0))
                if not response_text:
                    if time.time() >= deadline:
                        break
                    continue

                first_line = response_text.split('\r\n')[0] if '\r\n' in response_text else response_text.split('\n')[0]
                logger.debug(f"make_call recv: {first_line}")

                if '100 Trying' in response_text:
                    logger.debug("Received 100 Trying")
                    continue
                elif '180 Ringing' in response_text:
                    logger.info("Call is ringing...")
                    continue
                elif '183 Session Progress' in response_text:
                    logger.info("Session progress (early media)")
                    continue
                elif '200 OK' in response_text:
                    logger.info("Call answered!")

                    # Extract To header with tag from 200 OK
                    to_with_tag = self._extract_header(response_text, 'To')

                    # ACK must use the same CSeq number as the INVITE
                    ack_branch = self._generate_branch()
                    ack_lines = [
                        f"ACK {to_uri} SIP/2.0",
                        f"Via: SIP/2.0/UDP {self.local_ip}:{self.local_port};branch={ack_branch}",
                        f'From: "{self.display_name}" <{from_uri}>;tag={from_tag}',
                        f"To: {to_with_tag}",
                        f"Call-ID: {call_id}",
                        f"CSeq: {invite_cseq} ACK",
                        f"Contact: <{contact_uri}>",
                        "Max-Forwards: 70",
                        "Content-Length: 0",
                        "",
                        "",
                    ]
                    self._send("\r\n".join(ack_lines).encode(), addr)
                    self.cseq = invite_cseq + 1

                    # Extract RTP port from SDP
                    rtp_port = self._extract_sdp_port(response_text)
                    if rtp_port == 0:
                        logger.error("Could not extract RTP port from 200 OK")
                        self._remove_response_queue(call_id)
                        return False

                    # Extract media IP from SDP (c= line)
                    rtp_ip = self._extract_sdp_ip(response_text) or addr[0]

                    # Extract negotiated codec from SDP answer
                    negotiated_codec = self._extract_sdp_codec(response_text)
                    codec_name = "G.722" if negotiated_codec == 9 else "PCMU"
                    logger.info(f"Negotiated codec: {codec_name} (payload type {negotiated_codec})")

                    # Set remote RTP endpoint
                    rtp_session.set_remote(rtp_ip, rtp_port)
                    logger.info(f"RTP ready: local port {rtp_session.local_port} -> {rtp_ip}:{rtp_port}")

                    to_tag = self._extract_tag(to_with_tag)

                    # Store per-call state
                    call_state = {
                        'call_id': call_id,
                        'direction': 'outbound',
                        'remote_number': target_extension,
                        'rtp_session': rtp_session,
                        'codec': negotiated_codec,
                        'from_tag': from_tag,
                        'to_tag': to_tag,
                        'from_uri': from_uri,
                        'to_uri': to_uri,
                        'addr': addr,
                    }
                    self._active_calls[call_id] = call_state

                    # Backwards compat
                    self.active_call = call_id
                    self.from_tag = from_tag
                    self.to_tag = to_tag
                    self.negotiated_codec = negotiated_codec
                    self._call_to_uri = to_uri
                    self._call_from_uri = from_uri

                    # Handle the call
                    self._remove_response_queue(call_id)
                    await call_handler(rtp_session, negotiated_codec, call_id)
                    return True

                elif ('401 Unauthorized' in response_text or '407 Proxy Authentication Required' in response_text) and not auth_attempted:
                    is_407 = '407' in first_line
                    logger.info(f"Received {'407' if is_407 else '401'}, sending ACK and re-INVITE with auth...")

                    # ACK the 401/407 (required by RFC 3261 §22.2)
                    to_from_challenge = self._extract_header(response_text, 'To')
                    ack_challenge = (
                        f"ACK {to_uri} SIP/2.0\r\n"
                        f"Via: SIP/2.0/UDP {self.local_ip}:{self.local_port};branch={branch}\r\n"
                        f"From: \"{self.display_name}\" <{from_uri}>;tag={from_tag}\r\n"
                        f"To: {to_from_challenge}\r\n"
                        f"Call-ID: {call_id}\r\n"
                        f"CSeq: {invite_cseq} ACK\r\n"
                        f"Content-Length: 0\r\n"
                        f"\r\n"
                    )
                    self._send(ack_challenge.encode(), addr)
                    logger.debug("Sent ACK for auth challenge")

                    # Parse authentication challenge
                    header_name = 'Proxy-Authenticate' if is_407 else 'WWW-Authenticate'
                    realm, nonce, qop, opaque = self._parse_auth_challenge(response_text, header_name)
                    if not realm or not nonce:
                        logger.error("Failed to parse authentication challenge")
                        self._remove_response_queue(call_id)
                        return False

                    # New CSeq and branch for authenticated request
                    self.cseq += 1
                    invite_cseq = self.cseq
                    branch = self._generate_branch()

                    # Generate cnonce and nc for qop=auth
                    cnonce = uuid.uuid4().hex[:8]
                    nc = "00000001"

                    # Compute authentication response
                    logger.debug(f"Authenticating with realm={realm}, qop={qop}")
                    auth_response, cnonce_used = self._compute_auth_response(
                        'INVITE',
                        to_uri,
                        realm,
                        nonce,
                        qop=qop,
                        nc=nc,
                        cnonce=cnonce
                    )

                    # Build Authorization header
                    auth_header_name = 'Proxy-Authorization' if is_407 else 'Authorization'
                    auth_parts = [
                        f'username="{self.extension}"',
                        f'realm="{realm}"',
                        f'nonce="{nonce}"',
                        f'uri="{to_uri}"',
                        f'response="{auth_response}"',
                        'algorithm=MD5'
                    ]

                    if qop:
                        auth_parts.append(f'qop={qop}')
                        auth_parts.append(f'nc={nc}')
                        auth_parts.append(f'cnonce="{cnonce_used}"')

                    if opaque:
                        auth_parts.append(f'opaque="{opaque}"')

                    auth_header = f'{auth_header_name}: Digest {", ".join(auth_parts)}'

                    # Rebuild SDP with same RTP port
                    sdp = self._build_sdp(rtp_session.local_port)

                    # Build authenticated INVITE with NEW branch
                    auth_invite_lines = [
                        f"INVITE {to_uri} SIP/2.0",
                        f"Via: SIP/2.0/UDP {self.local_ip}:{self.local_port};branch={branch}",
                        f'From: "{self.display_name}" <{from_uri}>;tag={from_tag}',
                        f"To: <{to_uri}>",
                        f"Call-ID: {call_id}",
                        f"CSeq: {invite_cseq} INVITE",
                        auth_header,
                        f"Contact: <{contact_uri}>",
                        "Max-Forwards: 70",
                        "User-Agent: OpenClaw-Voice/1.0",
                        "Content-Type: application/sdp",
                        f"Content-Length: {len(sdp)}",
                        "",
                        sdp,
                    ]
                    auth_invite = "\r\n".join(auth_invite_lines)

                    self._send(auth_invite.encode(), (self.server, self.port))
                    logger.debug("Sent authenticated INVITE")
                    auth_attempted = True

                elif '403 Forbidden' in response_text or '404 Not Found' in response_text or '486 Busy' in response_text or '503 Service Unavailable' in response_text:
                    # ACK non-2xx final response (RFC 3261 §17.1.1.3)
                    self._ack_non2xx(to_uri, from_uri, from_tag, call_id, invite_cseq, branch, response_text, addr)
                    logger.error(f"Call failed: {first_line}")
                    self._cleanup_rtp()
                    self._remove_response_queue(call_id)
                    return False
                elif ('401 Unauthorized' in response_text or '407' in response_text) and auth_attempted:
                    # ACK non-2xx final response (RFC 3261 §17.1.1.3)
                    self._ack_non2xx(to_uri, from_uri, from_tag, call_id, invite_cseq, branch, response_text, addr)
                    logger.error("Authentication failed - invalid credentials")
                    self._cleanup_rtp()
                    self._remove_response_queue(call_id)
                    return False
                else:
                    logger.warning(f"Unhandled SIP response in make_call: {first_line}")

            logger.error("Call timed out")
            self._cleanup_rtp()
            self._remove_response_queue(call_id)
            return False

        except Exception as e:
            logger.error(f"Error making outbound call: {e}")
            self._cleanup_rtp()
            self._remove_response_queue(call_id if 'call_id' in dir() else '')
            return False

    # ------------------------------------------------------------------
    # SDP helpers
    # ------------------------------------------------------------------

    def _build_sdp(self, rtp_port: int = 0) -> str:
        """Build SDP for audio call - offer PCMU for xAI compatibility"""
        # Use external IP for RTP so Asterisk can send audio to us
        sdp_ip = self.external_ip or self.local_ip

        # If no RTP port provided, create a session to get one
        if rtp_port == 0:
            if not self.rtp_session:
                self.rtp_session = RTPSession()
            rtp_port = self.rtp_session.local_port

        # Offer PCMU (0) first since xAI uses u-law natively, then G.722 as fallback
        sdp_lines = [
            "v=0",
            f"o=- {int(time.time())} 1 IN IP4 {sdp_ip}",
            "s=HFIB Voice Call",
            f"c=IN IP4 {sdp_ip}",
            "t=0 0",
            f"m=audio {rtp_port} RTP/AVP 0 9",
            "a=rtpmap:0 PCMU/8000",
            "a=rtpmap:9 G722/8000",
            "a=sendrecv",
            "a=ptime:20",
            "",
        ]
        return "\r\n".join(sdp_lines)

    # ------------------------------------------------------------------
    # SIP message building / parsing
    # ------------------------------------------------------------------

    def _generate_call_id(self) -> str:
        """Generate unique Call-ID"""
        return f"{uuid.uuid4()}@{self.server}"

    def _generate_branch(self) -> str:
        """Generate unique branch parameter"""
        return f"z9hG4bK{uuid.uuid4().hex[:8]}"

    def _generate_tag(self) -> str:
        """Generate unique tag"""
        return uuid.uuid4().hex[:8]

    def _get_local_ip(self) -> str:
        """
        Get the local IP address that will be used to reach the SIP server.
        This ensures the Contact header contains a reachable IP.
        """
        try:
            # Create a temporary socket to determine which interface would be used
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
                # Connect to SIP server (doesn't send data)
                s.connect((self.server, self.port))
                local_ip = s.getsockname()[0]
                logger.info(f"  Local IP for SIP: {local_ip}")
                return local_ip
        except Exception as e:
            logger.warning(f"Could not determine local IP, using hostname: {e}")
            return socket.gethostbyname(socket.gethostname())

    def _compute_auth_response(self, method: str, uri: str, realm: str, nonce: str,
                                qop: Optional[str] = None, nc: str = "00000001",
                                cnonce: Optional[str] = None) -> Tuple[str, str]:
        """
        Compute MD5 digest for authentication

        Args:
            method: SIP method (REGISTER, INVITE, etc.)
            uri: SIP URI
            realm: Authentication realm
            nonce: Server nonce
            qop: Quality of protection ("auth" or None)
            nc: Nonce count (hex string)
            cnonce: Client nonce

        Returns:
            Tuple of (response, cnonce_used)
        """
        # HA1 = MD5(username:realm:password)
        ha1_input = f"{self.extension}:{realm}:{self.password}"
        ha1 = hashlib.md5(ha1_input.encode()).hexdigest()

        # HA2 = MD5(method:uri)
        ha2_input = f"{method}:{uri}"
        ha2 = hashlib.md5(ha2_input.encode()).hexdigest()

        # Response calculation depends on qop
        if qop == "auth":
            # Response = MD5(HA1:nonce:nc:cnonce:qop:HA2)
            if not cnonce:
                cnonce = uuid.uuid4().hex[:8]
            response_input = f"{ha1}:{nonce}:{nc}:{cnonce}:{qop}:{ha2}"
            response = hashlib.md5(response_input.encode()).hexdigest()
            logger.debug(f"Auth calculation with qop:")
            logger.debug(f"  HA1 input: {ha1_input}")
            logger.debug(f"  HA1: {ha1}")
            logger.debug(f"  HA2 input: {ha2_input}")
            logger.debug(f"  HA2: {ha2}")
            logger.debug(f"  Response input: {response_input}")
            logger.debug(f"  Response: {response}")
        else:
            # Response = MD5(HA1:nonce:HA2)
            if not cnonce:
                cnonce = ""
            response_input = f"{ha1}:{nonce}:{ha2}"
            response = hashlib.md5(response_input.encode()).hexdigest()
            logger.debug(f"Auth calculation without qop:")
            logger.debug(f"  Response input: {response_input}")
            logger.debug(f"  Response: {response}")

        return response, cnonce

    def _build_register_request(self, auth_header: Optional[str] = None, initial: bool = True) -> str:
        """Build SIP REGISTER request"""
        # Only generate new Call-ID and From tag for initial request
        if initial or not self.call_id:
            self.call_id = self._generate_call_id()
        if initial or not self.from_tag:
            self.from_tag = self._generate_tag()

        # Always new branch for each request
        self.branch = self._generate_branch()

        sip_uri = f"sip:{self.server}"
        from_uri = f"sip:{self.extension}@{self.server}"
        contact_uri = f"sip:{self.extension}@{self.external_ip or self.local_ip}:{self.local_port}"

        headers = [
            f"REGISTER {sip_uri} SIP/2.0",
            f"Via: SIP/2.0/UDP {self.local_ip}:{self.local_port};branch={self.branch}",
            f'From: "{self.display_name}" <{from_uri}>;tag={self.from_tag}',
            f"To: <{from_uri}>",
            f"Call-ID: {self.call_id}",
            f"CSeq: {self.cseq} REGISTER",
            f"Contact: <{contact_uri}>;expires=3600",
            f"Max-Forwards: 70",
            f"User-Agent: OpenClaw-Voice/1.0",
            f"Allow: INVITE, ACK, CANCEL, BYE, OPTIONS, REGISTER",
        ]

        if auth_header:
            headers.append(auth_header)

        headers.append("Content-Length: 0")
        headers.append("")
        headers.append("")

        return "\r\n".join(headers)

    def _parse_auth_challenge(self, response: str, header_name: str = 'WWW-Authenticate') -> Tuple[str, str, str, str]:
        """Parse realm, nonce, qop, and opaque from auth challenge header"""
        realm = None
        nonce = None
        qop = None
        opaque = None

        # Normalize line endings to handle servers that send \n instead of \r\n
        for line in response.replace('\r\n', '\n').split('\n'):
            if line.startswith(f'{header_name}:'):
                realm_match = re.search(r'realm="([^"]+)"', line)
                nonce_match = re.search(r'nonce="([^"]+)"', line)
                qop_match = re.search(r'qop="([^"]+)"', line)
                opaque_match = re.search(r'opaque="([^"]+)"', line)

                if realm_match:
                    realm = realm_match.group(1)
                if nonce_match:
                    nonce = nonce_match.group(1)
                if qop_match:
                    qop = qop_match.group(1)
                if opaque_match:
                    opaque = opaque_match.group(1)

        return realm or "", nonce or "", qop or "", opaque or ""

    def _extract_header(self, message: str, header_name: str) -> str:
        """Extract header value from SIP message"""
        for line in message.replace('\r\n', '\n').split('\n'):
            if line.startswith(f"{header_name}:"):
                return line[len(header_name)+1:].strip()
        return ""

    def _extract_sdp_port(self, message: str) -> int:
        """Extract RTP port from SDP"""
        match = re.search(r'm=audio (\d+)', message)
        if match:
            return int(match.group(1))
        return 0

    def _extract_sdp_codec(self, message: str) -> int:
        """Extract negotiated codec payload type from SDP answer"""
        match = re.search(r'm=audio \d+ RTP/AVP (\d+)', message)
        if match:
            return int(match.group(1))
        return 0  # Default to PCMU

    def _extract_tag(self, header_value: str) -> Optional[str]:
        """Extract tag parameter from a SIP header value"""
        match = re.search(r'tag=([^\s;>]+)', header_value)
        if match:
            return match.group(1)
        return None

    def _extract_sdp_ip(self, message: str) -> Optional[str]:
        """Extract media IP from SDP c= line"""
        match = re.search(r'c=IN IP4 (\S+)', message)
        if match:
            return match.group(1)
        return None

    def _extract_number(self, header_value: str) -> str:
        """Extract phone number or extension from a SIP From/To header."""
        match = re.search(r'sip:([^@>]+)', header_value)
        return match.group(1) if match else ''

    # ------------------------------------------------------------------
    # Request handlers
    # ------------------------------------------------------------------

    def _handle_bye(self, message: str, addr: tuple):
        """Handle incoming BYE request - respond with 200 OK"""
        try:
            call_id = self._extract_header(message, 'Call-ID')
            from_header = self._extract_header(message, 'From')
            to_header = self._extract_header(message, 'To')
            via_header = self._extract_header(message, 'Via')
            cseq_header = self._extract_header(message, 'CSeq')

            bye_response = (
                f"SIP/2.0 200 OK\r\n"
                f"Via: {via_header}\r\n"
                f"From: {from_header}\r\n"
                f"To: {to_header}\r\n"
                f"Call-ID: {call_id}\r\n"
                f"CSeq: {cseq_header}\r\n"
                f"Content-Length: 0\r\n"
                f"\r\n"
            )
            self._send(bye_response.encode(), addr)
            logger.info(f"Sent 200 OK to BYE for {call_id[:16]}")

            # Clean up per-call state
            call_state = self._active_calls.pop(call_id, None)
            if call_state and call_state.get('rtp_session'):
                call_state['rtp_session'].close()

            # Legacy cleanup
            if self.active_call == call_id:
                self.rtp_session = None
                self.active_call = None
        except Exception as e:
            logger.error(f"Error handling BYE: {e}")

    def _cleanup_rtp(self):
        """Close RTP session if open (used on call failure paths)."""
        if self.rtp_session:
            self.rtp_session.close()
            self.rtp_session = None

    def _ack_non2xx(self, to_uri, from_uri, from_tag, call_id, cseq, branch, response_text, addr):
        """ACK a non-2xx final response (same transaction, same branch per RFC 3261 §17.1.1.3)."""
        to_header = self._extract_header(response_text, 'To')
        ack = (
            f"ACK {to_uri} SIP/2.0\r\n"
            f"Via: SIP/2.0/UDP {self.local_ip}:{self.local_port};branch={branch}\r\n"
            f'From: "{self.display_name}" <{from_uri}>;tag={from_tag}\r\n'
            f"To: {to_header}\r\n"
            f"Call-ID: {call_id}\r\n"
            f"CSeq: {cseq} ACK\r\n"
            f"Content-Length: 0\r\n"
            f"\r\n"
        )
        self._send(ack.encode(), addr)
        logger.debug(f"Sent ACK for non-2xx: {response_text.split(chr(13))[0]}")

    def _handle_notify(self, message: str, addr: tuple):
        """Handle incoming NOTIFY request - respond with 200 OK"""
        try:
            call_id = self._extract_header(message, 'Call-ID')
            from_header = self._extract_header(message, 'From')
            to_header = self._extract_header(message, 'To')
            via_header = self._extract_header(message, 'Via')
            cseq_header = self._extract_header(message, 'CSeq')

            response = (
                f"SIP/2.0 200 OK\r\n"
                f"Via: {via_header}\r\n"
                f"From: {from_header}\r\n"
                f"To: {to_header}\r\n"
                f"Call-ID: {call_id}\r\n"
                f"CSeq: {cseq_header}\r\n"
                f"Content-Length: 0\r\n"
                f"\r\n"
            )
            self._send(response.encode(), addr)
            logger.debug("Sent 200 OK to NOTIFY")
        except Exception as e:
            logger.error(f"Error handling NOTIFY: {e}")

    def _handle_options(self, options_message: str, addr: tuple):
        """Handle incoming OPTIONS request - respond with 200 OK

        Per RFC 3261, responses must echo Via, From, To, Call-ID, CSeq exactly.
        pjsip is strict about this — malformed responses cause qualify failures.
        """
        try:
            # Extract raw header lines (preserving exact values)
            via_lines = []
            from_line = ""
            to_line = ""
            call_id_line = ""
            cseq_line = ""

            for line in options_message.split('\r\n'):
                if line.startswith('Via:'):
                    via_lines.append(line)
                elif line.startswith('From:'):
                    from_line = line
                elif line.startswith('To:'):
                    to_line = line
                elif line.startswith('Call-ID:'):
                    call_id_line = line
                elif line.startswith('CSeq:'):
                    cseq_line = line

            # Add tag to To header if not present (required in responses)
            if 'tag=' not in to_line:
                to_line = f"{to_line};tag={self._generate_tag()}"

            # Build 200 OK response with raw headers
            via_block = '\r\n'.join(via_lines)
            options_response = (
                f"SIP/2.0 200 OK\r\n"
                f"{via_block}\r\n"
                f"{from_line}\r\n"
                f"{to_line}\r\n"
                f"{call_id_line}\r\n"
                f"{cseq_line}\r\n"
                f"Allow: INVITE, ACK, CANCEL, OPTIONS, BYE, REFER, NOTIFY, MESSAGE, SUBSCRIBE, INFO\r\n"
                f"Supported: replaces, timer\r\n"
                f"User-Agent: OpenClaw-Voice/1.0\r\n"
                f"Content-Length: 0\r\n"
                f"\r\n"
            )

            self._send(options_response.encode(), addr)
            logger.debug(f"OPTIONS response:\n{options_response}")
            logger.info(f"Sent 200 OK to OPTIONS from {addr}")

        except Exception as e:
            logger.error(f"Error handling OPTIONS: {e}")

    async def _handle_invite(self, invite_message: str, addr: tuple, call_handler: Callable):
        """Handle incoming INVITE - answer the call"""
        try:
            # Parse INVITE headers
            call_id = self._extract_header(invite_message, 'Call-ID')
            from_header = self._extract_header(invite_message, 'From')
            to_header = self._extract_header(invite_message, 'To')
            via_header = self._extract_header(invite_message, 'Via')
            cseq_header = self._extract_header(invite_message, 'CSeq')

            # Extract caller number from From header
            remote_number = self._extract_number(from_header)

            # Extract SDP for RTP info
            rtp_port = self._extract_sdp_port(invite_message)
            rtp_ip = self._extract_sdp_ip(invite_message) or addr[0]

            # Extract negotiated codec from SDP offer
            negotiated_codec = self._extract_sdp_codec(invite_message)
            codec_name = "G.722" if negotiated_codec == 9 else "PCMU"
            logger.info(f"Incoming call codec: {codec_name} (payload type {negotiated_codec})")

            # Create RTP session (per-call, not instance-level)
            rtp_session = RTPSession()
            rtp_session.set_remote(rtp_ip, rtp_port)

            # Send 100 Trying
            trying_response = (
                f"SIP/2.0 100 Trying\r\n"
                f"Via: {via_header}\r\n"
                f"From: {from_header}\r\n"
                f"To: {to_header}\r\n"
                f"Call-ID: {call_id}\r\n"
                f"CSeq: {cseq_header}\r\n"
                f"Content-Length: 0\r\n"
                f"\r\n"
            )
            self._send(trying_response.encode(), addr)

            # Send 180 Ringing
            to_tag = self._generate_tag()
            to_with_tag = f"{to_header};tag={to_tag}" if 'tag=' not in to_header else to_header
            contact_port = self.contact_port
            ringing_response = (
                f"SIP/2.0 180 Ringing\r\n"
                f"Via: {via_header}\r\n"
                f"From: {from_header}\r\n"
                f"To: {to_with_tag}\r\n"
                f"Call-ID: {call_id}\r\n"
                f"CSeq: {cseq_header}\r\n"
                f"Contact: <sip:{self.extension}@{self.external_ip or self.local_ip}:{contact_port}>\r\n"
                f"Content-Length: 0\r\n"
                f"\r\n"
            )
            self._send(ringing_response.encode(), addr)

            # Send 200 OK with per-call RTP session
            ok_response = self._build_200_ok(call_id, from_header, to_with_tag, via_header, cseq_header, rtp_session=rtp_session)
            self._send(ok_response.encode(), addr)

            logger.info(f"Call answered, RTP session established")

            # Wait for ACK using async event (transport mode) or blocking loop
            if self._transport:
                ack_event = asyncio.Event()
                self._pending_ack[call_id] = ack_event
                try:
                    await asyncio.wait_for(ack_event.wait(), 5.0)
                    logger.info("Received ACK, call established")
                except asyncio.TimeoutError:
                    logger.warning("No ACK received within 5s, proceeding anyway")
                finally:
                    self._pending_ack.pop(call_id, None)
            else:
                # Blocking fallback (shouldn't happen with new listen_for_calls)
                self.socket.settimeout(1.0)
                deadline = time.time() + 5.0
                ack_received = False
                while time.time() < deadline:
                    try:
                        ack_data, ack_addr = self.socket.recvfrom(8192)
                        ack_msg = ack_data.decode('utf-8', errors='ignore')
                        ack_first_line = ack_msg.split('\r\n')[0] if '\r\n' in ack_msg else ack_msg.split('\n')[0]
                        if 'ACK' in ack_first_line:
                            logger.info("Received ACK, call established")
                            ack_received = True
                            break
                        elif 'OPTIONS' in ack_first_line:
                            self._handle_options(ack_msg, ack_addr)
                        elif 'NOTIFY' in ack_first_line:
                            self._handle_notify(ack_msg, ack_addr)
                        else:
                            logger.debug(f"While waiting for ACK, got: {ack_first_line}")
                    except socket.timeout:
                        continue
                if not ack_received:
                    logger.warning("No ACK received within 5s, proceeding anyway")

            # Store per-call state
            from_tag = self._extract_tag(from_header)
            call_state = {
                'call_id': call_id,
                'direction': 'inbound',
                'remote_number': remote_number,
                'rtp_session': rtp_session,
                'codec': negotiated_codec,
                'from_tag': from_tag,
                'to_tag': to_tag,
                'from_uri': f"sip:{self.extension}@{self.server}",
                'to_uri': f"sip:{self.extension}@{self.server}",
                'addr': addr,
            }
            self._active_calls[call_id] = call_state

            # Backwards compat: also set instance attrs for legacy callers
            self.active_call = call_id
            self.rtp_session = rtp_session
            self.negotiated_codec = negotiated_codec
            self.from_tag = from_tag

            await call_handler(rtp_session, negotiated_codec, call_id)

        except Exception as e:
            logger.error(f"Error handling INVITE: {e}")
            # Clean up per-call state on error
            if 'call_id' in dir() and call_id in self._active_calls:
                cs = self._active_calls.pop(call_id)
                if cs.get('rtp_session'):
                    cs['rtp_session'].close()

    def _build_200_ok(self, call_id: str, from_header: str, to_header: str, via_header: str, cseq_header: str, rtp_session: Optional[RTPSession] = None) -> str:
        """Build 200 OK response for INVITE"""
        # Use external IP for RTP so Asterisk can reach us
        sdp_ip = self.external_ip or self.local_ip

        # Add to-tag if not present
        if 'tag=' not in to_header:
            to_tag = self._generate_tag()
            to_header = f"{to_header};tag={to_tag}"

        # Use provided per-call RTP session, fall back to instance
        rtp = rtp_session or self.rtp_session
        if not rtp:
            rtp = RTPSession()
            self.rtp_session = rtp

        ts = int(datetime.now().timestamp())
        sdp_lines = [
            "v=0",
            f"o=- {ts} {ts} IN IP4 {sdp_ip}",
            "s=HFIB Voice Agent",
            f"c=IN IP4 {sdp_ip}",
            "t=0 0",
            f"m=audio {rtp.local_port} RTP/AVP 0 8",
            "a=rtpmap:0 PCMU/8000",
            "a=rtpmap:8 PCMA/8000",
            "",
        ]
        sdp = "\r\n".join(sdp_lines)

        contact_port = self.contact_port
        response_lines = [
            "SIP/2.0 200 OK",
            f"Via: {via_header}",
            f"From: {from_header}",
            f"To: {to_header}",
            f"Call-ID: {call_id}",
            f"CSeq: {cseq_header}",
            f"Contact: <sip:{self.extension}@{self.external_ip or self.local_ip}:{contact_port}>",
            "Content-Type: application/sdp",
            f"Content-Length: {len(sdp)}",
            "",
            sdp,
        ]
        return "\r\n".join(response_lines)

    # ------------------------------------------------------------------
    # Call control
    # ------------------------------------------------------------------

    async def hangup(self, call_id: Optional[str] = None, target_extension: Optional[str] = None):
        """Send BYE to hangup a specific call (or the legacy active_call)."""
        # Resolve call state: prefer per-call dict, fall back to legacy
        cid = call_id or self.active_call
        if not cid:
            logger.warning("hangup: no active call")
            return

        call_state = self._active_calls.get(cid)

        if call_state:
            to_uri = call_state.get('to_uri', f"sip:{target_extension or self.extension}@{self.server}")
            from_uri = call_state.get('from_uri', f"sip:{self.extension}@{self.server}")
            from_tag = call_state.get('from_tag', self.from_tag)
            to_tag = call_state.get('to_tag', self.to_tag)
            rtp = call_state.get('rtp_session')
        else:
            to_uri = getattr(self, '_call_to_uri', None) or f"sip:{target_extension or self.extension}@{self.server}"
            from_uri = getattr(self, '_call_from_uri', None) or f"sip:{self.extension}@{self.server}"
            from_tag = self.from_tag
            to_tag = self.to_tag
            rtp = self.rtp_session

        if not rtp:
            logger.warning(f"hangup: no RTP session for {cid[:16]}")
            return

        contact_uri = f"sip:{self.extension}@{self.external_ip or self.local_ip}:{self.contact_port}"

        # Build To header with tag (required for in-dialog requests per RFC 3261)
        to_header = f"<{to_uri}>"
        if to_tag:
            to_header = f"{to_header};tag={to_tag}"

        bye_request = (
            f"BYE {to_uri} SIP/2.0\r\n"
            f"Via: SIP/2.0/UDP {self.local_ip}:{self.local_port};branch={self._generate_branch()}\r\n"
            f'From: "{self.display_name}" <{from_uri}>;tag={from_tag}\r\n'
            f"To: {to_header}\r\n"
            f"Call-ID: {cid}\r\n"
            f"CSeq: {self.cseq} BYE\r\n"
            f"Contact: <{contact_uri}>\r\n"
            f"Max-Forwards: 70\r\n"
            f"Content-Length: 0\r\n"
            f"\r\n"
        )

        try:
            self._send(bye_request.encode(), (self.server, self.port))
            logger.info(f"Sent BYE for {cid[:16]}")
            self.cseq += 1
        except Exception as e:
            logger.error(f"Error sending BYE: {e}")

        rtp.close()

        # Clean up per-call state
        self._active_calls.pop(cid, None)

        # Clean up legacy state
        if self.active_call == cid:
            self.rtp_session = None
            self.active_call = None
            self.to_tag = None
        logger.info(f"Call {cid[:16]} ended")

    async def stop(self):
        """Stop SIP client and hang up all active calls."""
        self.running = False
        self.registered = False
        if self._keepalive_task and not self._keepalive_task.done():
            self._keepalive_task.cancel()
        if self._re_register_task and not self._re_register_task.done():
            self._re_register_task.cancel()
        # Hang up all tracked calls
        for cid in list(self._active_calls.keys()):
            try:
                await self.hangup(call_id=cid)
            except Exception as e:
                logger.warning(f"Error hanging up {cid[:16]}: {e}")
        # Legacy fallback
        if self.rtp_session:
            await self.hangup()
        if self._transport:
            self._transport.close()
            self._transport = None
            self._protocol = None
        else:
            self.socket.close()
        logger.info("SIP client stopped")
