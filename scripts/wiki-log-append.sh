#!/usr/bin/env bash
# Single writer for ~/Projects/K2B-Investment-Vault/wiki/log.md
# Usage: wiki-log-append.sh <skill> <action> <summary>
# Example: wiki-log-append.sh /compile raw/research/foo.md "updated 3 wiki pages"
#
# Format written: "YYYY-MM-DD HH:MM  <skill>  <action>  <summary>\n"
# Locking: flock -x if available, mkdir fallback for macOS.
#
# Ported from K2B (Tier 1 audit fix #2) on Phase 1 Session 2.

set -euo pipefail

SKILL="${1:?wiki-log-append: skill arg required}"
ACTION="${2:?wiki-log-append: action arg required}"
SUMMARY="${3:?wiki-log-append: summary arg required}"

LOG="${K2BI_WIKI_LOG:-$HOME/Projects/K2B-Investment-Vault/wiki/log.md}"
LOCK="${K2BI_WIKI_LOG_LOCK:-/tmp/k2bi-wiki-log.lock}"
TS="$(date '+%Y-%m-%d %H:%M')"
LINE="${TS}  ${SKILL}  ${ACTION}  ${SUMMARY}"

if [ ! -f "$LOG" ]; then
  echo "wiki-log-append: log file not found: $LOG" >&2
  exit 2
fi

if command -v flock >/dev/null 2>&1; then
  exec 9>"$LOCK"
  flock -x 9
  printf '%s\n' "$LINE" >> "$LOG"
else
  # macOS fallback: mkdir is atomic
  LOCK_DIR="${LOCK}.d"
  TRIES=0
  while ! mkdir "$LOCK_DIR" 2>/dev/null; do
    TRIES=$((TRIES + 1))
    if [ "$TRIES" -gt 200 ]; then
      echo "wiki-log-append: could not acquire $LOCK_DIR after 10s" >&2
      exit 3
    fi
    sleep 0.05
  done
  trap 'rmdir "$LOCK_DIR" 2>/dev/null || true' EXIT
  printf '%s\n' "$LINE" >> "$LOG"
fi
