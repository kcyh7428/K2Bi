#!/usr/bin/env bash
# One-shot alert tick: classify journal events and send any alerts via Telegram.
# Designed to run from cron every minute.
#
# Logs to ~/Projects/K2Bi/logs/invest-alert-YYYY-MM-DD.log

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
LOG_DIR="$PROJECT_DIR/logs"
mkdir -p "$LOG_DIR"

TODAY="$(date +%Y-%m-%d)"
LOG_FILE="$LOG_DIR/invest-alert-${TODAY}.log"

# Load env vars from .env if present
if [[ -f "$PROJECT_DIR/.env" ]]; then
  set -a
  # shellcheck source=/dev/null
  source "$PROJECT_DIR/.env"
  set +a
fi

# IMPORTANT: ${VAR-default} (NO COLON) is correct here under `set -u`.
# Per Stage 1 finding (cc) at K2Bi-Vault/wiki/planning/upcoming-sessions.md:
# the no-colon form falls back to default ONLY when the variable is UNSET,
# preserving an explicit empty value (e.g. cron line `HTTPS_PROXY=` to disable
# the proxy on VPS where Clash is not used). Reviewers may flag this as
# "unbound under set -u" -- that is a misreading of bash semantics; the `-`
# operator IS the documented mechanism for safe default-handling. Verified
# 2026-04-25 evening via executable test (architect ruling per L-2026-04-20-002).
# DO NOT revert to ${VAR:-default} -- that re-introduces the cron-env trap.

# VPS cron may set HTTPS_PROXY= (empty) to disable proxy use in the KL
# datacentre. Use non-colon default form so an explicit empty override wins.
# Cron does NOT source ~/.zshenv where this is set for interactive shells,
# so export it here. Idempotent on hosts that already have HTTPS_PROXY set.
export HTTPS_PROXY="${HTTPS_PROXY-http://127.0.0.1:7897}"
export HTTP_PROXY="${HTTP_PROXY-http://127.0.0.1:7897}"
export NO_PROXY="${NO_PROXY-localhost,127.0.0.1}"

# Run classifier WITHOUT saving state yet.
# State is only committed after all Telegram sends succeed.
ALERTS_JSON="$(mktemp -t invest-alert-json.XXXXXX)"
STATE_JSON="$(mktemp -t invest-alert-state.XXXXXX)"
trap 'rm -f "$ALERTS_JSON" "$STATE_JSON"' EXIT

python3 "$SCRIPT_DIR/invest_alert_lib.py" --no-save-state --state-json-out "$STATE_JSON" > "$ALERTS_JSON" 2>>"$LOG_FILE" || {
  echo "$(date -Iseconds) ERROR: classifier failed" >> "$LOG_FILE"
  exit 1
}

# If no alerts, silent success (no state to commit)
if [[ ! -s "$ALERTS_JSON" ]]; then
  exit 0
fi

# Send each alert via Telegram. Fail hard on first failure so state
# is NOT committed and cron will retry on next tick.
FAILED=0
while IFS= read -r line; do
  MSG="$(echo "$line" | python3 -c "import sys, json; print(json.load(sys.stdin)['message'])")"
  if echo "$MSG" | "$SCRIPT_DIR/send-telegram.sh" >> "$LOG_FILE" 2>&1; then
    EVT="$(echo "$line" | python3 -c "import sys, json; d=json.load(sys.stdin); print(f\"{d['event_type']} tier={d['tier']}\")")"
    echo "$(date -Iseconds) SENT: $EVT" >> "$LOG_FILE"
  else
    echo "$(date -Iseconds) ERROR: telegram send failed for: $MSG" >> "$LOG_FILE"
    FAILED=1
    break
  fi
done < "$ALERTS_JSON"

if [[ "$FAILED" -ne 0 ]]; then
  exit 1
fi

# All sends succeeded: commit state
if [[ -f "$STATE_JSON" ]]; then
  STATE_DIR="${K2BI_ALERT_STATE_DIR:-$HOME/.k2bi}"
  mkdir -p "$STATE_DIR"
  mv "$STATE_JSON" "$STATE_DIR/alert-state.json"
fi

exit 0
