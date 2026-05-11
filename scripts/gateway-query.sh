#!/usr/bin/env bash
# gateway-query.sh -- run a python snippet on the VPS against the local IB Gateway.
#
# IB Gateway runs on the VPS at 127.0.0.1:4002. The engine (also on the VPS)
# connects to it natively. Operator one-off queries from the MacBook must go
# through this helper -- never tunnel or open the gateway port off-host.
#
# Usage:
#   scripts/gateway-query.sh "<python-snippet>"
#   scripts/gateway-query.sh -f path/to/snippet.py
#
# Example:
#   scripts/gateway-query.sh "from ib_async import IB
#   ib = IB(); ib.connect('127.0.0.1', 4002, clientId=99)
#   print(sum(float(v.value) for v in ib.accountValues()
#             if v.tag == 'NetLiquidation' and v.currency == 'BASE'))
#   ib.disconnect()"
#
# The snippet is piped to the remote python3 via stdin (no shell heredoc
# interpolation), so backticks, $-expansions, quotes, and EOF-shaped lines
# inside the snippet pass through unchanged and cannot escape into the remote
# shell.
#
# Operator one-off queries lease clientId values 90-99 before connecting.
# The engine owns clientId 1. This helper refuses clientId=1 and any explicit
# clientId outside the operator range.

set -euo pipefail

VPS="hostinger"
SSH_USER="k2bi"
REMOTE_REPO="/home/${SSH_USER}/Projects/K2Bi"
REMOTE_PYTHON="${REMOTE_REPO}/.venv/bin/python3"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LOCAL_PYTHON="${PYTHON:-python3}"
CLIENTID_ALLOCATOR="${REPO_ROOT}/scripts/lib/clientid_allocator.py"
CLIENTID_LEASE_DIR="${K2BI_GATEWAY_CLIENTID_LEASE_DIR:-${TMPDIR:-/tmp}/k2bi-gateway-clientids}"
LEASE_PATH=""
LEASE_TOKEN=""

# This guard is an accidental-misuse safety rail, not an authentication boundary.
# K2BI_GATEWAY_QUERY_OPERATOR_OVERRIDE is intentionally available to the operator.
assert_invoked_from_macbook() {
    if [[ "${K2BI_GATEWAY_QUERY_OPERATOR_OVERRIDE:-}" == "1" ]]; then
        return 0
    fi

    if [[ -n "${CLAUDE_CODE_SKILL_INVOCATION:-}" ]]; then
        echo "gateway-query.sh blocked: skill workflows must not call the live broker path without K2BI_GATEWAY_QUERY_OPERATOR_OVERRIDE=1" >&2
        exit 4
    fi

    local host_short
    local host_name
    host_short="$(hostname -s 2>/dev/null || true)"
    host_name="$(uname -n 2>/dev/null || true)"
    case "${host_short}:${host_name}" in
        Keiths-MacBook-Pro:*|*:Keiths-MacBook-Pro|*:Keiths-MacBook-Pro.local)
            return 0
            ;;
    esac

    echo "gateway-query.sh must run from Keiths-MacBook-Pro or set K2BI_GATEWAY_QUERY_OPERATOR_OVERRIDE=1" >&2
    exit 4
}

release_clientid_lease() {
    if [[ -n "${LEASE_PATH:-}" && -n "${LEASE_TOKEN:-}" ]]; then
        "$LOCAL_PYTHON" "$CLIENTID_ALLOCATOR" release \
            --lease-path "$LEASE_PATH" \
            --token "$LEASE_TOKEN" >/dev/null 2>&1 || true
    fi
}

extract_explicit_clientid() {
    printf '%s\n' "$SNIPPET" \
        | grep -Eo 'clientId[[:space:]]*=[[:space:]]*[0-9]+' \
        | head -n 1 \
        | grep -Eo '[0-9]+$' || true
}

SNIPPET=""
if [[ "${1:-}" == "-f" ]]; then
    [[ -n "${2:-}" ]] || { echo "usage: gateway-query.sh -f <path-to-snippet.py>" >&2; exit 2; }
    [[ -f "$2" ]] || { echo "snippet file not found: $2" >&2; exit 2; }
    SNIPPET=$(cat "$2")
elif [[ -n "${1:-}" ]]; then
    SNIPPET="$1"
else
    echo "usage: gateway-query.sh <python-snippet>" >&2
    echo "       gateway-query.sh -f <path-to-snippet.py>" >&2
    exit 2
fi

assert_invoked_from_macbook

PREFERRED_CLIENT_ID="$(extract_explicit_clientid)"
if [[ "${PREFERRED_CLIENT_ID:-}" == "1" ]]; then
    echo "gateway-query.sh refuses clientId=1 because clientId 1 is reserved for k2bi-engine" >&2
    exit 5
fi
if [[ -n "${PREFERRED_CLIENT_ID:-}" ]]; then
    if (( PREFERRED_CLIENT_ID < 90 || PREFERRED_CLIENT_ID > 99 )); then
        echo "gateway-query.sh refuses clientId=${PREFERRED_CLIENT_ID}; operator queries must use clientId 90-99" >&2
        exit 5
    fi
fi

ALLOCATOR_ARGS=(acquire --lease-dir "$CLIENTID_LEASE_DIR" --owner "gateway-query:${USER:-unknown}:$$" --owner-pid "$$" --format shell)
if [[ -n "${PREFERRED_CLIENT_ID:-}" ]]; then
    ALLOCATOR_ARGS+=(--preferred "$PREFERRED_CLIENT_ID")
fi
LEASE_OUTPUT="$("$LOCAL_PYTHON" "$CLIENTID_ALLOCATOR" "${ALLOCATOR_ARGS[@]}")"
GATEWAY_CLIENT_ID=""
while IFS='=' read -r key value; do
    case "$key" in
        client_id) GATEWAY_CLIENT_ID="$value" ;;
        lease_path) LEASE_PATH="$value" ;;
        token) LEASE_TOKEN="$value" ;;
    esac
done <<< "$LEASE_OUTPUT"

if [[ -z "$GATEWAY_CLIENT_ID" || -z "$LEASE_PATH" || -z "$LEASE_TOKEN" ]]; then
    echo "gateway-query.sh could not parse clientId allocator output" >&2
    exit 6
fi

trap release_clientid_lease EXIT

# Pipe snippet via stdin to ssh -> remote python3 (no shell interpolation).
# ConnectTimeout fails fast if VPS unreachable; ServerAliveInterval keeps long
# queries from hanging silently on a dropped connection.
printf '%s\n' "$SNIPPET" | ssh \
    -o ConnectTimeout=10 \
    -o ServerAliveInterval=15 \
    -o ServerAliveCountMax=4 \
    "${SSH_USER}@${VPS}" \
    "cd '${REMOTE_REPO}' && K2BI_GATEWAY_CLIENT_ID='${GATEWAY_CLIENT_ID}' '${REMOTE_PYTHON}' -"
