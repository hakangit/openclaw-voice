#!/usr/bin/env python3
"""
Minimal SIP registration test — FreePBX extension 100 on sip.customer1.example.

Uses only sip_client.py. Does NOT touch xAI. Just registers, holds, unregisters.

Usage:
    export SIP_EXT100_PASSWORD=***
    .venv/bin/python test_register.py

Reads SIP secret from env var. Delete .env / rotate secret after running.
"""
import asyncio
import logging
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from sip_client import SIPClient

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
)
logging.getLogger("websockets").setLevel(logging.WARNING)


async def main():
    pw = os.environ.get("SIP_EXT100_PASSWORD")
    if not pw:
        print("ERROR: SIP_EXT100_PASSWORD env var not set")
        sys.exit(1)

    config = {
        "sip_server": "sip.customer1.example",
        "sip_port": 5060,
        "extension": "100",
        "password": pw,
        "display_name": "OpenClaw-Test",
        "sip_local_port": 5060,
    }

    client = SIPClient(config)

    print(f"→ Registering {config['extension']}@{config['sip_server']}:{config['sip_port']}...")
    ok = await client.register()

    if not ok:
        print("✗ REGISTRATION FAILED")
        print("  Check: SIP secret correct? ext 100 exists on FreePBX?")
        print("  Check: UDP 5060 outbound to sip.customer1.example is allowed?")
        sys.exit(2)

    print("✓ REGISTERED — holding for 5s to inspect server-side bindings")
    await asyncio.sleep(5)

    print("→ Unregistering...")
    ok = await client.unregister_all()
    if ok:
        print("✓ UNREGISTERED cleanly")
    else:
        print("⚠ Unregister had issues (may still be expired by expires timer)")

    await client.stop() if hasattr(client, "stop") else None
    print("\nDONE.")


if __name__ == "__main__":
    asyncio.run(main())
