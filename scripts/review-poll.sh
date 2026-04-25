#!/usr/bin/env bash
# Poll a review job and print structured JSON status.
#
# Usage:
#   scripts/review-poll.sh <job_id>
#
# Output JSON keys:
#   status                 starting|running|completed|both_failed|spawn_failed
#   phase                  running_commands|final_inference|wedge_suspected
#   elapsed_s              wall-clock seconds since review started
#   last_activity_s_ago    seconds since the vendor log last grew
#   deadline_remaining_s   seconds until hard deadline
#   reviewer_current       codex|minimax (current attempt)
#     (when 'minimax', the wrapper script runs whichever provider
#      K2B_LLM_PROVIDER selects -- Kimi K2.6 by default)
#   reviewer_attempts      per-attempt exit code + result
#   fallback_used          true if secondary reviewer ran
#   exit_code              final exit code (null while running)
#   log_path               repo-relative path to the unified log
#   tail                   last ~20 lines of the unified log
#   should_poll_again      true while status == running
#
# Recommended poll interval while running: 30s.

set -euo pipefail
if [ $# -ne 1 ]; then
  echo "usage: scripts/review-poll.sh <job_id>" >&2
  exit 3
fi
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec python3 "$SCRIPT_DIR/lib/review_runner.py" --poll "$1"
