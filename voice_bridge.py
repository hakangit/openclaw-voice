#!/usr/bin/env python3
"""
Voice Bridge: SIP Phone <-> xAI Grok Voice Agent (OpenClaw Universal Tool)
==========================================================================

Bridges audio between a SIP phone call and xAI's Grok Voice Agent API.
Uses a universal 'process_query' tool that sends all queries to OpenClaw.

USAGE:
    python voice_bridge.py --call 288
"""

import argparse
import asyncio
import json
import logging
import os
import time
import tempfile
import uuid
from pathlib import Path

import aiohttp
import aiofiles

from audio import RTP_FRAME_BYTES, RTP_FRAME_MS
from sip_client import SIPClient, RTPSession
from xai_voice_bridge import XAIVoiceBridge

# ============================================================================
# CONFIGURATION
# ============================================================================

# Load from environment
XAI_API_KEY = os.getenv("XAI_API_KEY", "")

# SIP - from config.yaml or env
SIP_SERVER = os.getenv("SIP_SERVER", "free1.voipgateway.org")
SIP_PORT = int(os.getenv("SIP_PORT", "5060"))
SIP_EXTENSION = os.getenv("SIP_USER", "")
SIP_PASSWORD = os.getenv("SIP_PASSWORD", "")
SIP_DISPLAY_NAME = os.getenv("SIP_DISPLAY_NAME", "OpenClaw")

# OpenClaw integration
QUERY_DIR = Path("/tmp/openclaw_voice_queries")
RESPONSE_DIR = Path("/tmp/openclaw_voice_responses")
QUERY_TIMEOUT = 60  # seconds

# Logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    datefmt='%H:%M:%S'
)
logger = logging.getLogger(__name__)


# ============================================================================
# UNIVERSAL TOOL DEFINITION
# ============================================================================

TOOLS = [
    {
        "type": "function",
        "name": "process_query",
        "description": """Process any user query using OpenClaw tools and capabilities.

This is a universal tool that can handle ANY request:
- Run shell commands (df -h, docker ps, git status, etc.)
- Check system status (disk, memory, CPU)
- Send messages (Telegram, etc.)
- Search the web
- Manage files and code
- Execute Python scripts
- Control services (start/stop/restart)
- And anything else OpenClaw can do

The assistant should use this tool for ANY user request that requires action.""",
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
    }
]

SYSTEM_INSTRUCTIONS = """You are OpenClaw, a helpful voice assistant running on a server. You have an ongoing conversation with the user just like a chat.

CONVERSATION STYLE:
- Be natural, conversational, and helpful
- Feel free to ask follow-up questions to clarify
- Guide the user through multi-step tasks
- Remember context from earlier in the conversation
- Offer options and let the user choose

TOOL USAGE:
For ANY request that needs system info or actions, you MUST use the 'process_query' tool. The tool returns conversational responses that may include follow-up questions.

EXAMPLES:
User: "Check disk space" → process_query("Check disk space") → "You have 150GB free. That's plenty! Anything else you need?"
User: "Deploy the app" → process_query("Deploy the app") → "I can deploy. Should I pull latest changes first, or just restart what's there?"
User: "The server's slow" → process_query("Check why server is slow") → "CPU is at 95%. Top process is 'hfib'. Want me to restart it, or check logs first?"

The tool handles the full conversation flow - you just speak its responses naturally."""


# ============================================================================
# OPENCLAW INTEGRATION
# ============================================================================

def setup_communication_dirs():
    """Create directories for query/response communication."""
    QUERY_DIR.mkdir(exist_ok=True)
    RESPONSE_DIR.mkdir(exist_ok=True)
    # Clean old files
    for f in QUERY_DIR.glob("*.json"):
        f.unlink()
    for f in RESPONSE_DIR.glob("*.json"):
        f.unlink()


async def send_query_to_openclaw(query: str) -> str:
    """
    Send query to OpenClaw and wait for response.
    
    Uses file-based communication:
    1. Write query to QUERY_DIR/{id}.json
    2. Wait for response in RESPONSE_DIR/{id}.json
    3. Return response text
    """
    query_id = str(uuid.uuid4())
    query_file = QUERY_DIR / f"{query_id}.json"
    response_file = RESPONSE_DIR / f"{query_id}.json"
    
    # Write query
    query_data = {
        "id": query_id,
        "query": query,
        "timestamp": time.time()
    }
    
    async with aiofiles.open(query_file, 'w') as f:
        await f.write(json.dumps(query_data))
    
    logger.info(f"Query {query_id}: {query}")
    
    # Wait for response (polling)
    start_time = time.time()
    while time.time() - start_time < QUERY_TIMEOUT:
        if response_file.exists():
            async with aiofiles.open(response_file, 'r') as f:
                content = await f.read()
                response_data = json.loads(content)
                
            # Cleanup
            query_file.unlink(missing_ok=True)
            response_file.unlink(missing_ok=True)
            
            result = response_data.get("response", "No response received")
            logger.info(f"Response {query_id}: {result[:100]}...")
            return result
        
        await asyncio.sleep(0.1)
    
    # Timeout
    query_file.unlink(missing_ok=True)
    return "Sorry, the request timed out. Please try again."


# ============================================================================
# TOOL EXECUTION
# ============================================================================

async def execute_tool(call_id: str, name: str, args: dict, bridge: XAIVoiceBridge) -> str:
    """Execute a tool and return the result."""
    logger.info(f"Tool call: {name}({args})")
    
    try:
        if name == "process_query":
            query = args.get("query", "")
            if not query:
                return "I didn't catch that. Could you repeat your request?"
            
            result = await send_query_to_openclaw(query)
            
            # Check if result is a conversation directive (JSON)
            try:
                result_data = json.loads(result)
                if isinstance(result_data, dict):
                    # Handle conversation mode directives
                    response = result_data.get("response", "")
                    follow_up = result_data.get("follow_up")
                    ask_user = result_data.get("ask_user")
                    choices = result_data.get("choices")
                    
                    # Build conversational response
                    full_response = response
                    if ask_user:
                        full_response += f" {ask_user}"
                    if choices:
                        full_response += " Options: " + ", ".join(choices)
                    if follow_up:
                        full_response += f" {follow_up}"
                    
                    return full_response
            except json.JSONDecodeError:
                # Plain text response
                pass
            
            return result
        else:
            return f"Unknown tool: {name}"
    
    except Exception as e:
        logger.error(f"Tool execution error: {e}")
        return f"Sorry, there was an error: {str(e)}"


# ============================================================================
# VOICE BRIDGE
# ============================================================================

async def voice_bridge(rtp_session: RTPSession, negotiated_codec: int = 0, call_id: str = ''):
    """
    Bridge audio between SIP phone and xAI Grok Voice Agent.
    """
    
    if not XAI_API_KEY:
        logger.error("XAI_API_KEY not set!")
        return
    
    # Setup communication
    setup_communication_dirs()
    
    # Create xAI bridge
    bridge = XAIVoiceBridge(
        api_key=XAI_API_KEY,
        voice="Rex",
        instructions=SYSTEM_INSTRUCTIONS,
        tools=TOOLS,
    )
    
    # Set up tool handler - pass bridge for conversation injection
    async def tool_handler(call_id: str, name: str, args: dict):
        return await execute_tool(call_id, name, args, bridge)
    
    bridge.on_tool_call = tool_handler
    
    # Connect to xAI
    if not await bridge.connect():
        logger.error("Failed to connect to xAI")
        return
    
    logger.info("Connected to xAI Grok Voice Agent!")
    
    # Shared state
    running = True
    
    # ------------------------------------------------------------------
    # TASK 1: Listen to xAI messages
    # ------------------------------------------------------------------
    async def rx_xai():
        nonlocal running
        try:
            await bridge.listen()
        finally:
            running = False
    
    # ------------------------------------------------------------------
    # TASK 2: Receive from phone, send to xAI
    # ------------------------------------------------------------------
    async def tx_xai():
        loop = asyncio.get_event_loop()
        await asyncio.sleep(2)  # Wait for AI greeting
        logger.info("Now listening to phone...")
        
        while running:
            ulaw = await loop.run_in_executor(
                None, lambda: rtp_session.receive_audio(timeout=0.02))
            
            if ulaw:
                if bridge.is_speaking:
                    continue
                await bridge.send_audio(ulaw)
            else:
                await asyncio.sleep(0.005)
    
    # ------------------------------------------------------------------
    # TASK 3: Send queued audio to phone with precise timing
    # ------------------------------------------------------------------
    async def play_sip():
        start_time = time.monotonic()
        frames_sent = 0
        
        while running:
            expected_time = start_time + (frames_sent * RTP_FRAME_MS / 1000)
            now = time.monotonic()
            
            wait_time = expected_time - now
            if wait_time > 0:
                await asyncio.sleep(wait_time)
            
            frame = await bridge.receive_audio()
            if frame:
                rtp_session.send_audio(frame, payload_type=0)
                frames_sent += 1
                
                if frames_sent % 50 == 0:
                    logger.info(f"Sent {frames_sent} frames, queue: {bridge.outbound_queue.qsize()}")
            else:
                await asyncio.sleep(RTP_FRAME_MS / 1000)
    
    # ------------------------------------------------------------------
    # Run all tasks
    # ------------------------------------------------------------------
    logger.info("Voice bridge active! Waiting for calls...")
    try:
        await asyncio.gather(rx_xai(), tx_xai(), play_sip())
    finally:
        await bridge.disconnect()


# ============================================================================
# MAIN
# ============================================================================

async def main(target_extension: str = None):
    """Register and optionally call a target extension."""
    
    logger.info("Starting OpenClaw Voice Bridge (Universal Tool Mode)...")
    
    if not XAI_API_KEY:
        logger.error("XAI_API_KEY environment variable not set!")
        logger.error("Set it with: export XAI_API_KEY=your_api_key")
        return
    
    if not SIP_PASSWORD:
        logger.error("SIP_PASSWORD not set!")
        return
    
    # Create SIP client - bind to port 5060 for SIP signaling
    client = SIPClient({
        'sip_server': SIP_SERVER,
        'sip_port': SIP_PORT,
        'extension': SIP_EXTENSION,
        'password': SIP_PASSWORD,
        'display_name': SIP_DISPLAY_NAME,
        'sip_local_port': 5060,  # Bind SIP socket to this port
        'rtp_port': 40000,
        'external_ip': ''
    })
    
    # Register
    logger.info(f"Registering as {SIP_EXTENSION}@{SIP_SERVER}...")
    if not await client.register():
        logger.error("SIP registration failed")
        return
    
    logger.info("SIP registration successful!")
    
    if target_extension:
        # Make outbound call
        logger.info(f"Calling extension {target_extension}...")
        await client.make_call(target_extension, voice_bridge)
    else:
        # Wait for incoming calls
        logger.info("Waiting for incoming calls...")
        await client.listen_for_calls(voice_bridge)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Voice Bridge: SIP <-> xAI Grok")
    parser.add_argument("--call", help="Extension to call (outbound)")
    args = parser.parse_args()
    
    try:
        asyncio.run(main(args.call))
    except KeyboardInterrupt:
        logger.info("Interrupted")
