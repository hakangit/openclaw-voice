#!/usr/bin/env bash
#
# fleet_health.sh — health check across every OpenClaw Voice customer instance.
#
# Reads the systemd template instances, checks each one's service state and
# HTTP health endpoint, and prints a compact fleet view. Also reports total
# active calls, which is the first thing to look at when a customer asks
# "is the receptionist working today?".
#
# Usage:
#   fleet_health.sh              # summary table
#   fleet_health.sh --json       # machine-readable
#
set -euo pipefail

ROOT="/etc/openclaw-voice/customers"
TEMPLATE="openclaw-voice@"

JSON_OUT=0
[ "${1:-}" = "--json" ] && JSON_OUT=1

if [ ! -d "$ROOT" ]; then
    echo "No customer directory at $ROOT — nothing provisioned yet." >&2
    exit 0
fi

# Discover instances: enabled units plus any customer directory present.
INSTANCES="$(ls -1 "$ROOT" 2>/dev/null | tr '\n' ' ')"

if [ -z "$(echo "$INSTANCES" | tr -d ' ')" ]; then
    echo "No customer instances found in $ROOT." >&2
    exit 0
fi

if [ "$JSON_OUT" -eq 1 ]; then
    echo '['
    first=1
    for id in $INSTANCES; do
        unit="${TEMPLATE}${id}.service"
        active="$(systemctl is-active "$unit" 2>/dev/null | head -1 || true)"
        [ -n "$active" ] || active="unknown"
        enabled="$(systemctl is-enabled "$unit" 2>/dev/null | head -1 || true)"
        [ -n "$enabled" ] || enabled="unknown"
        port="$(grep -E '^OPENCLAW_VOICE_API_PORT=' "$ROOT/$id/.env" 2>/dev/null | cut -d= -f2 || true)"
        health="{}"
        if [ -n "$port" ] && [ "$active" = "active" ]; then
            health="$(curl -s --max-time 5 "http://127.0.0.1:${port}/health" 2>/dev/null || echo '{}')"
        fi
        [ $first -eq 1 ] || echo ','
        first=0
        printf '{"customer":"%s","unit":"%s","active":"%s","enabled":"%s","health":%s}' \
            "$id" "$unit" "$active" "$enabled" "$health"
    done
    echo
    echo ']'
    exit 0
fi

printf '%-16s %-10s %-10s %-9s %-8s %-8s %s\n' \
    CUSTOMER SERVICE ENABLED REGISTERED CALLS API "SIP EXT"
printf '%-16s %-10s %-10s %-9s %-8s %-8s %s\n' \
    "----------------" "----------" "----------" "---------" "--------" "--------" "--------"

TOTAL_CALLS=0
PROBLEMS=0

for id in $INSTANCES; do
    unit="${TEMPLATE}${id}.service"
    active="$(systemctl is-active "$unit" 2>/dev/null | head -1 || true)"
    [ -n "$active" ] || active="unknown"
    enabled="$(systemctl is-enabled "$unit" 2>/dev/null | head -1 || true)"
    [ -n "$enabled" ] || enabled="unknown"

    port="$(grep -E '^OPENCLAW_VOICE_API_PORT=' "$ROOT/$id/.env" 2>/dev/null | cut -d= -f2 || true)"
    registered="?"; calls="?"; ext="?"

    if [ -n "$port" ] && [ "$active" = "active" ]; then
        h="$(curl -s --max-time 5 "http://127.0.0.1:${port}/health" 2>/dev/null || echo '')"
        if [ -n "$h" ]; then
            registered="$(echo "$h" | grep -o '"registered": *[a-z]*' | awk '{print $2}')"
            calls="$(echo "$h" | grep -o '"active_calls": *[0-9]*' | awk '{print $2}')"
            ext="$(echo "$h" | grep -o '"extension": *"[^"]*"' | cut -d'"' -f4)"
            TOTAL_CALLS=$((TOTAL_CALLS + ${calls:-0}))
        else
            registered="no-reply"; PROBLEMS=$((PROBLEMS+1))
        fi
    else
        PROBLEMS=$((PROBLEMS+1))
    fi

    printf '%-16s %-10s %-10s %-9s %-8s %-8s %s\n' \
        "$id" "$active" "$enabled" "${registered:-?}" "${calls:-?}" "${port:--}" "${ext:--}"
done

echo
echo "Total active calls across fleet: $TOTAL_CALLS"
if [ "$PROBLEMS" -gt 0 ]; then
    echo "WARNING: $PROBLEMS instance(s) need attention." >&2
    exit 1
fi
echo "All instances healthy."
