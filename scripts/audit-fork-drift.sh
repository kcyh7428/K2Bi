#!/usr/bin/env bash
# scripts/audit-fork-drift.sh
# Finds residual K2B references in K2Bi that should have been swapped at fork time.
# Exit 0 if clean (given allowlist), exit 1 with report if drift found.
# Allowlist: scripts/fork-audit-allowlist.txt (fgrep-pattern lines to ignore).

set -euo pipefail

REPO_ROOT="$(git rev-parse --show-toplevel)"
cd "$REPO_ROOT"

ALLOWLIST=scripts/fork-audit-allowlist.txt
FINDINGS=0

check() {
  local label="$1"; shift
  local pattern="$1"; shift
  local paths=("$@")
  local hits
  hits=$(grep -rn --include='*.py' --include='*.md' --include='*.sh' --include='*.yml' \
    -E "$pattern" "${paths[@]}" 2>/dev/null || true)
  if [ -f "$ALLOWLIST" ]; then
    hits=$(echo "$hits" | grep -vFf "$ALLOWLIST" || true)
  fi
  if [ -n "$hits" ]; then
    echo "=== [$label] ==="
    echo "$hits"
    echo ""
    FINDINGS=$((FINDINGS + $(echo "$hits" | wc -l)))
  fi
}

check "K2B vault paths" "/Projects/K2B/|K2B-Vault/" .claude/ scripts/ execution/ tests/ CLAUDE.md
check "K2B hardcoded categories" "['\"]skills['\"].*['\"]code['\"].*['\"]dashboard['\"]" .claude/ scripts/
check "VALID_CATEGORIES hardcoded set" "VALID_CATEGORIES\s*=" .claude/ scripts/
check "K2B pm2 process names" "k2b-remote|k2b-dashboard|k2b-observer-loop" .claude/ scripts/ execution/
check "K2B skill invocations" "/k2b-(feedback|daily-capture|meeting-processor|tldr|review|improve|research|lint|observer|insight|compile|weave|autoresearch|youtube|linkedin|email|scheduler|ship|sync|vault-writer)" .claude/ scripts/
check "K2B GitHub remote" "kcyh7428/K2B[^i]" .claude/ scripts/ CLAUDE.md
check "K2B mailbox schema assumptions" "'code'|\"code\".*\"dashboard\"" .claude/skills/invest-*/SKILL.md

if [ "$FINDINGS" -eq 0 ]; then
  echo "fork-drift-audit: clean (allowlist: $ALLOWLIST)"
  exit 0
fi

echo "=== TOTAL: $FINDINGS drift hit(s) ==="
echo "For each hit: either (a) fix to K2Bi equivalent, or (b) add a fgrep-safe substring to $ALLOWLIST if intentionally kept."
exit 1
