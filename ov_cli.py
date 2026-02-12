#!/usr/bin/env python3
"""
OpenClaw Voice CLI
==================

CLI wrapper for the OpenClaw Voice HTTP API.

USAGE:
    python ov_cli.py call +41321234567 "Reschedule my dentist to Thursday"
    python ov_cli.py calls
    python ov_cli.py hangup <call_id>
    python ov_cli.py status
    python ov_cli.py listen <call_id>
"""

import json
import sys
import urllib.request
import urllib.error

API_BASE = "http://127.0.0.1:8079"


def api_request(method: str, path: str, data: dict = None) -> dict:
    url = f"{API_BASE}{path}"
    body = json.dumps(data).encode() if data else None
    headers = {"Content-Type": "application/json"} if data else {}
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        return json.loads(e.read())
    except urllib.error.URLError:
        return {"error": "Cannot connect to daemon. Is openclaw-voice running?"}


def cmd_call(args):
    if len(args) < 1:
        print("Usage: ov call <number> [task]")
        sys.exit(1)
    number = args[0]
    task = " ".join(args[1:]) if len(args) > 1 else "Have a brief conversation"
    result = api_request("POST", "/call", {"number": number, "task": task})
    if "error" in result:
        print(f"Error: {result['error']}")
        sys.exit(1)
    print(f"Call started: {result.get('call_id', 'unknown')}")


def cmd_calls(args):
    result = api_request("GET", "/calls")
    calls = result.get("calls", [])
    if not calls:
        print("No active calls")
        return
    for c in calls:
        direction = "IN" if c["direction"] == "inbound" else "OUT"
        conf = f" [CONF:{c['conference_peer'][:8]}]" if c.get("conference_peer") else ""
        print(f"  {c['call_id'][:12]}  {direction}  {c['remote_number']:>16}  "
              f"{c['state']:>10}  {c['duration_s']:>4}s{conf}")
        if c.get("task"):
            print(f"    task: {c['task']}")


def cmd_hangup(args):
    if not args:
        print("Usage: ov hangup <call_id>")
        sys.exit(1)
    call_id = args[0]
    result = api_request("DELETE", f"/call/{call_id}")
    if "error" in result:
        print(f"Error: {result['error']}")
        sys.exit(1)
    print(f"Call {call_id[:12]} ended")


def cmd_status(args):
    result = api_request("GET", "/health")
    if "error" in result:
        print(f"Error: {result['error']}")
        sys.exit(1)
    reg = "registered" if result.get("registered") else "NOT registered"
    print(f"SIP: {reg}")
    print(f"Active calls: {result.get('active_calls', 0)}/{result.get('max_concurrent', 3)}")
    if result.get("uptime_s"):
        m, s = divmod(result["uptime_s"], 60)
        h, m = divmod(m, 60)
        print(f"Uptime: {h}h {m}m {s}s")


def cmd_listen(args):
    if not args:
        print("Usage: ov listen <call_id>")
        sys.exit(1)
    call_id = args[0]
    result = api_request("POST", f"/call/{call_id}/conference")
    if "error" in result:
        print(f"Error: {result['error']}")
        sys.exit(1)
    print(f"Conference active: inbound={result.get('inbound', '?')[:12]} <-> outbound={result.get('outbound', '?')[:12]}")


COMMANDS = {
    "call": cmd_call,
    "calls": cmd_calls,
    "hangup": cmd_hangup,
    "status": cmd_status,
    "listen": cmd_listen,
}


def main():
    if len(sys.argv) < 2 or sys.argv[1] in ("-h", "--help"):
        print("Usage: ov <command> [args]")
        print()
        print("Commands:")
        print("  call <number> [task]   Make an outbound call")
        print("  calls                  List active calls")
        print("  hangup <call_id>       Hang up a call")
        print("  status                 Health check")
        print("  listen <call_id>       Conference into an outbound call")
        sys.exit(0)

    cmd = sys.argv[1]
    if cmd not in COMMANDS:
        print(f"Unknown command: {cmd}")
        sys.exit(1)

    COMMANDS[cmd](sys.argv[2:])


if __name__ == "__main__":
    main()
