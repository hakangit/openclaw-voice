#!/usr/bin/env python3
"""
xAI Grok Voice Agent Bridge
============================

Real-time voice interface using xAI's Grok Voice Agent API.
Bridges phone calls (SIP/RTP) to xAI WebSocket.

KEY ADVANTAGE: xAI natively supports G.711 u-law at 8kHz - the exact format
SIP phones use. This eliminates the need for audio resampling entirely!

AUDIO PIPELINE (NO CONVERSION NEEDED):
======================================
  Phone (SIP/RTP)                         xAI Grok
  8kHz u-law                              8kHz u-law
       |                                       |
       |  Base64 encode ------------------>    |
       |                                       |
       |  <------------------ Base64 decode    |
       |                                       |

WebSocket URL: wss://api.x.ai/v1/realtime
Auth: Authorization: Bearer $XAI_API_KEY
"""

import asyncio
import base64
import json
import logging
from typing import Optional, Callable, Dict, Any

import websockets

logger = logging.getLogger(__name__)

# Audio constants - xAI uses same format as SIP!
RTP_FRAME_BYTES = 160  # 160 bytes u-law = 20ms @ 8kHz
RTP_FRAME_MS = 20


class XAIVoiceBridge:
    """
    Bridge between SIP/RTP audio and xAI Grok Voice Agent WebSocket.

    No audio conversion needed - both use 8kHz u-law!
    """

    def __init__(
        self,
        api_key: str,
        voice: str = "Rex",
        instructions: Optional[str] = None,
        tools: Optional[list] = None,
    ):
        """
        Initialize xAI Voice bridge.

        Args:
            api_key: xAI API key
            voice: Voice name (Rex, Ara, Sal, Eve, Leo)
            instructions: System instructions for the agent
            tools: List of tool definitions for function calling
        """
        self.api_key = api_key
        self.voice = voice
        self.instructions = instructions or "You are Mike, a helpful voice assistant."
        self.tools = tools or []

        self.ws: Optional[websockets.WebSocketClientProtocol] = None
        self.running = False

        # Audio queue for outbound (to phone)
        self.outbound_queue: asyncio.Queue[bytes] = asyncio.Queue()

        # State
        self.session_id: Optional[str] = None
        self.is_speaking = False
        self._session_ready = asyncio.Event()

        # Callbacks
        self.on_transcript: Optional[Callable[[str, str], None]] = None
        self.on_tool_call: Optional[Callable[[str, str, dict], asyncio.Future]] = None
        self.on_error: Optional[Callable[[str], None]] = None

    async def connect(self) -> bool:
        """Connect to xAI Voice Agent WebSocket."""
        try:
            url = "wss://api.x.ai/v1/realtime"
            headers = {
                "Authorization": f"Bearer {self.api_key}",
            }

            logger.info("Connecting to xAI Voice Agent...")
            self.ws = await websockets.connect(
                url,
                additional_headers=headers,
                ping_interval=20,
                ping_timeout=10,
                max_size=16 * 1024 * 1024,
            )

            self.running = True
            logger.info("Connected to xAI Voice Agent")

            # Start background listener so we can receive session.updated
            self._listen_task = asyncio.create_task(self.listen())

            # Send session config
            await self._configure_session()

            # Wait for session to be fully configured
            try:
                await asyncio.wait_for(self._session_ready.wait(), timeout=5.0)
                logger.info("Session ready, requesting initial greeting")
                await self.ws.send(json.dumps({
                    "type": "response.create",
                    "response": {"modalities": ["text", "audio"]}
                }))
            except asyncio.TimeoutError:
                logger.warning("Timed out waiting for session.updated, requesting greeting anyway")
                await self.ws.send(json.dumps({
                    "type": "response.create",
                    "response": {"modalities": ["text", "audio"]}
                }))

            return True

        except Exception as e:
            logger.error(f"Failed to connect to xAI: {e}")
            if self.on_error:
                self.on_error(str(e))
            return False

    async def _configure_session(self):
        """Send session configuration to xAI."""
        config = {
            "type": "session.update",
            "session": {
                "instructions": self.instructions,
                "voice": self.voice,
                "turn_detection": {"type": "server_vad"},
                "audio": {
                    "input": {"format": {"type": "audio/pcmu"}},
                    "output": {"format": {"type": "audio/pcmu"}},
                },
            }
        }

        # Add tools if provided
        if self.tools:
            config["session"]["tools"] = self.tools

        await self.ws.send(json.dumps(config))
        logger.info(f"Configured session: voice={self.voice}, format=pcmu (8kHz u-law)")

    async def disconnect(self):
        """Disconnect from xAI."""
        self.running = False
        if self.ws:
            await self.ws.close()
            self.ws = None
        logger.info("Disconnected from xAI")

    async def send_audio(self, ulaw_audio: bytes):
        """
        Send u-law audio from SIP to xAI.

        Args:
            ulaw_audio: G.711 u-law encoded audio bytes (160 bytes = 20ms)
        """
        if not self.ws or not self.running:
            return

        # Base64 encode and send - no conversion needed!
        audio_b64 = base64.b64encode(ulaw_audio).decode('utf-8')
        message = {
            "type": "input_audio_buffer.append",
            "audio": audio_b64
        }

        try:
            await self.ws.send(json.dumps(message))
        except Exception as e:
            logger.error(f"Error sending audio to xAI: {e}")

    async def receive_audio(self) -> Optional[bytes]:
        """
        Get u-law audio to send to SIP.

        Returns:
            G.711 u-law encoded audio bytes, or None if queue empty
        """
        try:
            return self.outbound_queue.get_nowait()
        except asyncio.QueueEmpty:
            return None

    async def listen(self):
        """Listen for messages from xAI."""
        if not self.ws:
            return

        try:
            async for message in self.ws:
                await self._handle_message(json.loads(message))
        except websockets.ConnectionClosed:
            logger.info("xAI connection closed")
        except Exception as e:
            logger.error(f"Error in xAI listener: {e}")
        finally:
            self.running = False

    async def _handle_message(self, msg: Dict[str, Any]):
        """Handle incoming message from xAI.

        Note: xAI realtime API uses 'response.output_audio.delta' (not 'response.audio.delta')
        and 'conversation.created' (not 'session.created').
        """
        msg_type = msg.get("type", "")

        if msg_type in ("session.created", "conversation.created"):
            self.session_id = msg.get("session", {}).get("id")
            logger.info(f"Session created: {self.session_id}")

        elif msg_type == "session.updated":
            logger.info("Session updated and ready")
            self._session_ready.set()

        elif msg_type == "input_audio_buffer.speech_started":
            logger.debug("User started speaking")

        elif msg_type == "input_audio_buffer.speech_stopped":
            logger.debug("User stopped speaking")

        elif msg_type == "conversation.item.input_audio_transcription.completed":
            transcript = msg.get("transcript", "")
            if transcript:
                logger.info(f"User: {transcript}")
                if self.on_transcript:
                    self.on_transcript("user", transcript)

        elif msg_type in ("response.audio_transcript.delta", "response.output_audio_transcript.delta"):
            delta = msg.get("delta", "")
            if delta:
                logger.debug(f"Assistant (delta): {delta}")

        elif msg_type in ("response.audio_transcript.done", "response.output_audio_transcript.done"):
            transcript = msg.get("transcript", "")
            if transcript:
                logger.info(f"Assistant: {transcript}")
                if self.on_transcript:
                    self.on_transcript("assistant", transcript)

        elif msg_type in ("response.audio.delta", "response.output_audio.delta"):
            # Audio chunk from assistant - already in u-law!
            audio_b64 = msg.get("delta", "")
            if audio_b64:
                self.is_speaking = True
                ulaw_audio = base64.b64decode(audio_b64)

                # Queue as individual RTP frames
                for i in range(0, len(ulaw_audio), RTP_FRAME_BYTES):
                    frame = ulaw_audio[i:i + RTP_FRAME_BYTES]
                    if len(frame) == RTP_FRAME_BYTES:
                        await self.outbound_queue.put(frame)

        elif msg_type in ("response.audio.done", "response.output_audio.done"):
            self.is_speaking = False
            logger.info("Assistant audio complete")

        elif msg_type == "response.done":
            self.is_speaking = False
            response_obj = msg.get("response", {})
            status = response_obj.get("status", "unknown")
            output = response_obj.get("output", [])
            logger.info(f"Response complete: status={status}, outputs={len(output)}")

        elif msg_type == "response.function_call_arguments.done":
            call_id = msg.get("call_id", "")
            name = msg.get("name", "")
            arguments = msg.get("arguments", "{}")

            logger.info(f"Tool call: {name}({arguments})")

            if self.on_tool_call:
                try:
                    args = json.loads(arguments)
                    result = await self.on_tool_call(call_id, name, args)
                    await self._send_tool_result(call_id, result)
                except Exception as e:
                    logger.error(f"Tool call error: {e}")
                    await self._send_tool_result(call_id, {"error": str(e)})

        elif msg_type == "error":
            error_msg = msg.get("error", {}).get("message", "Unknown error")
            logger.error(f"xAI error: {error_msg}")
            logger.error(f"xAI error full: {json.dumps(msg, indent=2)}")
            if self.on_error:
                self.on_error(error_msg)

        elif msg_type in ("ping", "response.created", "response.output_item.added",
                          "response.output_item.done", "conversation.item.added",
                          "response.content_part.added", "response.content_part.done"):
            logger.debug(f"xAI: {msg_type}")

        else:
            logger.info(f"Unhandled xAI message: {msg_type}")

    async def _send_tool_result(self, call_id: str, result: Any):
        """Send tool result back to xAI."""
        if not self.ws or not self.running:
            return

        message = {
            "type": "conversation.item.create",
            "item": {
                "type": "function_call_output",
                "call_id": call_id,
                "output": json.dumps(result) if not isinstance(result, str) else result
            }
        }

        await self.ws.send(json.dumps(message))

        # Request response after tool result
        await self.ws.send(json.dumps({
            "type": "response.create",
            "response": {"modalities": ["text", "audio"]}
        }))

    async def send_text(self, text: str):
        """Send text message to xAI (for testing/commands)."""
        if not self.ws or not self.running:
            return

        message = {
            "type": "conversation.item.create",
            "item": {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": text}]
            }
        }
        await self.ws.send(json.dumps(message))
        await self.ws.send(json.dumps({
            "type": "response.create",
            "response": {"modalities": ["text", "audio"]}
        }))

    async def inject_context(self, text: str):
        """
        Inject context/information for the assistant to use in response.
        Use this to provide tool results or system information.
        """
        if not self.ws or not self.running:
            return

        message = {
            "type": "conversation.item.create",
            "item": {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "text", "text": text}]
            }
        }
        await self.ws.send(json.dumps(message))
