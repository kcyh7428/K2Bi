#!/usr/bin/env bash
# Test primitive: send a synthetic test alert via Telegram.
# Usage: scripts/invest-alert-test.sh [--chat-id <id>]
# Does NOT append to journal; sends directly via send-telegram.sh.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"

# Load env vars from .env if present
if [[ -f "$PROJECT_DIR/.env" ]]; then
  set -a
  # shellcheck source=/dev/null
  source "$PROJECT_DIR/.env"
  set +a
fi

CHAT_ID="${K2BI_TELEGRAM_CHAT_ID:-}"

# Parse optional --chat-id flag
while [[ $# -gt 0 ]]; do
  case "$1" in
    --chat-id)
      CHAT_ID="${2:-}"
      shift 2
      ;;
    *)
      echo "Unknown argument: $1" >&2
      exit 1
      ;;
  esac
done

[[ -n "$CHAT_ID" ]] || { echo "K2BI_TELEGRAM_CHAT_ID env var not set and --chat-id not passed" >&2; exit 1; }

HOSTNAME="$(hostname -s 2>/dev/null || hostname)"
TIMESTAMP="$(date -Iseconds)"

MSG="[TEST] invest-alert pipeline ok ${HOSTNAME} ${TIMESTAMP}"

echo "$MSG" | "$SCRIPT_DIR/send-telegram.sh" --chat-id "$CHAT_ID"
echo "Test alert sent to $CHAT_ID"
