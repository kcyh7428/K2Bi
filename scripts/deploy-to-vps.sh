#!/bin/bash
# deploy-to-vps.sh -- Sync K2Bi project files from MacBook to Hostinger VPS.
#
# Usage:
#   deploy-to-vps.sh              # auto-detect what changed, sync those categories
#   deploy-to-vps.sh <category>   # force a single category from deploy-config.yml
#   deploy-to-vps.sh all          # sync every category
#   deploy-to-vps.sh --dry-run    # show what would sync without doing it
#
# The category list + the set of paths each category covers live in
# scripts/deploy-config.yml. Both this script and the /invest-ship step 12
# preflight read that file via scripts/lib/deploy_config.py. To add a new
# deployed path: append to deploy-config.yml's `targets:`. To add an
# intentionally-local path: append to `excludes:`. The preflight will block
# /ship until the drift is resolved.
#
# Phase 3.9 Stage 2 renamed this script from deploy-to-mini.sh to
# deploy-to-vps.sh and retargeted it from the Mac Mini to the Hostinger KL VPS.

set -euo pipefail

VPS="hostinger"
LOCAL_BASE="$HOME/Projects/K2Bi"
REMOTE_BASE="/home/k2bi/Projects/K2Bi"
CONFIG_HELPER="$LOCAL_BASE/scripts/lib/deploy_config.py"
DRY_RUN=false
MODE=""
RESTART_FAILED=false

GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m'

log()  { echo -e "${GREEN}[sync]${NC} $1"; }
warn() { echo -e "${YELLOW}[sync]${NC} $1"; }
err()  { echo -e "${RED}[sync]${NC} $1"; }

# --- arg parse ------------------------------------------------------------

for arg in "$@"; do
    case "$arg" in
        --dry-run) DRY_RUN=true ;;
        *) MODE="${MODE:-$arg}" ;;
    esac
done
MODE="${MODE:-auto}"

if [ ! -f "$CONFIG_HELPER" ]; then
    err "deploy-config helper missing at $CONFIG_HELPER"
    exit 2
fi

KNOWN_CATEGORIES=$(python3 "$CONFIG_HELPER" list-categories)

# --- helpers --------------------------------------------------------------

detect_changed_categories() {
    # Ask the config helper for the set of categories with pending changes
    # since the last successful sync (sentinel at .sync-state/last-synced-commit).
    # Canonical implementation unions uncommitted diffs, untracked files, and
    # committed-since-sentinel diffs. Replaces the cycle-2 `git diff HEAD~1
    # HEAD` fallback, which silently dropped earlier commits once a devlog
    # follow-up commit landed on top (the cycle-5 carry-over bug).
    #
    # $1 is the pinned baseline SHA (captured at run start). Passed through
    # to detect-categories + record-sync so the sentinel never advances past
    # content the rsync plan did not see (Codex R7 final-gate F1).
    cd "$LOCAL_BASE"
    local baseline="${1:-}"
    if [[ -n "$baseline" ]]; then
        python3 "$CONFIG_HELPER" detect-categories --head "$baseline"
    else
        python3 "$CONFIG_HELPER" detect-categories
    fi
}

rsync_target() {
    # rsync one deploy-config.yml target (file or directory). Handles the
    # local-deleted-but-remote-present case so the VPS mirrors deletions.
    local local_rel="$1"    # path relative to LOCAL_BASE; from config verbatim
    local rsync_flag=""
    $DRY_RUN && rsync_flag="--dry-run"
    local stripped="${local_rel%/}"

    if [[ -d "$LOCAL_BASE/$stripped" ]]; then
        rsync -av $rsync_flag --delete \
            --exclude '__pycache__/' --exclude '*.pyc' --exclude '.venv/' \
            "$LOCAL_BASE/$stripped/" "k2bi@$VPS:$REMOTE_BASE/$stripped/"
    elif [[ -f "$LOCAL_BASE/$stripped" ]]; then
        rsync -av $rsync_flag "$LOCAL_BASE/$stripped" "k2bi@$VPS:$REMOTE_BASE/$stripped"
    else
        if $DRY_RUN; then
            warn "  (dry-run) would remove k2bi@$VPS:$REMOTE_BASE/$stripped if present"
            return 0
        fi
        # Mirror local deletion to remote so state stays consistent.
        local result
        result=$(ssh "k2bi@$VPS" "
            if [ -d $REMOTE_BASE/$stripped ]; then
                rm -rf $REMOTE_BASE/$stripped && echo REMOVED_DIR
            elif [ -f $REMOTE_BASE/$stripped ]; then
                rm $REMOTE_BASE/$stripped && echo REMOVED_FILE
            else
                echo ABSENT
            fi
        ")
        case "$result" in
            REMOVED_DIR)  log "  removed remote tree $stripped (deleted locally)" ;;
            REMOVED_FILE) log "  removed remote file $stripped (deleted locally)" ;;
        esac
    fi
}

sync_category() {
    local category="$1"
    log "Syncing category: $category"
    local targets
    targets=$(python3 "$CONFIG_HELPER" list-targets "$category")
    if [[ -z "$targets" ]]; then
        warn "  (no targets in category $category)"
        return 0
    fi
    while IFS= read -r target; do
        [[ -z "$target" ]] && continue
        rsync_target "$target"
    done <<< "$targets"

    # Skills category preserves the Phase 1 verify-count sanity check so a
    # drift between MacBook and VPS skill-folder counts surfaces loudly.
    if [[ "$category" == "skills" ]] && ! $DRY_RUN; then
        local remote_count local_count
        remote_count=$(ssh "k2bi@$VPS" "ls -d $REMOTE_BASE/.claude/skills/*/ 2>/dev/null | wc -l" | tr -d ' ')
        local_count=$(ls -d "$LOCAL_BASE/.claude/skills/"*/ 2>/dev/null | wc -l | tr -d ' ')
        if [[ "$remote_count" == "$local_count" ]]; then
            log "  skills verified: $remote_count skill folders on both machines"
        else
            warn "  skills count mismatch: local=$local_count remote=$remote_count"
        fi
    fi

    # After-sync service restart for categories that need it.
    # Phase 3.9 Stage 2: the VPS runs the engine under systemd, not pm2.
    if ! $DRY_RUN; then
        case "$category" in
            execution)
                log "  restarting k2bi-engine.service on VPS"
                ssh "k2bi@$VPS" "sudo systemctl restart k2bi-engine.service" || {
                    err "  RESTART FAILED for category '$category': k2bi-engine.service did not restart cleanly. Sync sentinel will NOT advance; re-run /sync after resolving the restart issue."
                    RESTART_FAILED=true
                }
                ;;
            pm2)
                # Historical category name: on the VPS this maps to the same
                # systemd service as execution. Kept as `pm2` in deploy-config.yml
                # so downstream readers (skills, preflight) need no rename.
                log "  restarting k2bi-engine.service on VPS (pm2 category -> systemd)"
                ssh "k2bi@$VPS" "sudo systemctl restart k2bi-engine.service" || {
                    err "  RESTART FAILED for category '$category': k2bi-engine.service did not restart cleanly. Sync sentinel will NOT advance; re-run /sync after resolving the restart issue."
                    RESTART_FAILED=true
                }
                ;;
        esac
    fi
}

# --- mode resolution ------------------------------------------------------

is_known_category() {
    local candidate="$1"
    local cat
    while IFS= read -r cat; do
        [[ "$cat" == "$candidate" ]] && return 0
    done <<< "$KNOWN_CATEGORIES"
    return 1
}

# Codex R7 final-gate F1: capture the baseline SHA ONCE at run start and
# thread it through detection + record-sync. Without this, a commit that
# lands locally while rsync is copying files would advance the sentinel
# even though its content was never part of the sync plan.
#
# MiniMax R7 R2 F1: a silent capture failure (empty BASELINE_SHA) causes
# detect + record-sync to each re-resolve HEAD independently and
# potentially disagree. For a real sync run, that is a correctness gap
# we must surface loudly. --dry-run bypasses record-sync entirely, so
# baseline inconsistency there is observational; still require git so
# the dry-run reflects what the real run would do.
BASELINE_SHA="$(cd "$LOCAL_BASE" && git rev-parse HEAD 2>/dev/null || true)"
if [[ -z "$BASELINE_SHA" ]]; then
    err "Cannot capture baseline SHA: \`git rev-parse HEAD\` failed in $LOCAL_BASE."
    err "Deploy requires an initialised git repo with at least one commit."
    err "If this is a fresh clone, finish \`git clone\` before running /sync."
    exit 1
fi

RUN_CATEGORIES=""
case "$MODE" in
    auto)
        RUN_CATEGORIES=$(detect_changed_categories "$BASELINE_SHA")
        if [[ -z "$RUN_CATEGORIES" ]]; then
            warn "No pending changes since last sync. Use 'all' to force full sync."
            exit 0
        fi
        ;;
    all)
        RUN_CATEGORIES="$KNOWN_CATEGORIES"
        ;;
    *)
        if ! is_known_category "$MODE"; then
            err "Unknown mode: $MODE"
            echo "Valid modes: auto | all | --dry-run | $(echo "$KNOWN_CATEGORIES" | tr '\n' ' ')"
            exit 1
        fi
        RUN_CATEGORIES="$MODE"
        ;;
esac

# --- execute --------------------------------------------------------------

if ! ssh -o ConnectTimeout=5 "k2bi@$VPS" "echo ok" &>/dev/null; then
    err "Cannot reach Hostinger VPS (ssh k2bi@$VPS). Is it on?"
    exit 1
fi

# Ensure the remote repo root + each category's directory prefix exist.
# The rsync commands build directory structure as they go, but a first-time
# deploy into a virgin REMOTE_BASE needs the parent there.
REMOTE_MKDIRS=$(python3 "$CONFIG_HELPER" list-targets | awk -F/ '{if ($1 != $0) print $1}' | sort -u)
ssh "k2bi@$VPS" "mkdir -p $REMOTE_BASE $(echo "$REMOTE_MKDIRS" | awk -v base="$REMOTE_BASE" '{print base"/"$0}' | tr '\n' ' ')" || {
    err "Failed to create remote base directories"
    exit 1
}

$DRY_RUN && warn "DRY RUN -- no files will be changed"

echo ""
log "Sync plan:"
while IFS= read -r cat; do
    [[ -z "$cat" ]] && continue
    log "  category: $cat"
done <<< "$RUN_CATEGORIES"
echo ""

while IFS= read -r cat; do
    [[ -z "$cat" ]] && continue
    sync_category "$cat"
done <<< "$RUN_CATEGORIES"

echo ""
if $RESTART_FAILED; then
    err "Deploy NOT recorded -- one or more systemctl restarts failed during sync. Working tree is rsync'd but engine state is uncertain. Re-run after resolving the restart issue (likely missing sudoers rule on VPS for k2bi user; see K2Bi-Vault/wiki/planning/feature_vps-migration.md gotcha #9)."
    exit 3
fi

if $DRY_RUN; then
    log "Dry run complete. Run without --dry-run to sync."
else
    # Record the post-sync HEAD so the next auto-detect can diff from here.
    # Pass the pinned baseline SHA so the sentinel matches the snapshot we
    # actually synced even if HEAD advanced mid-run (Codex R7 final-gate F1).
    # A sentinel write failure is not fatal -- the sync itself succeeded; we
    # just warn so Keith knows the next auto run may over-sync.
    record_sync_args=()
    if [[ -n "$BASELINE_SHA" ]]; then
        record_sync_args+=(--sha "$BASELINE_SHA")
    fi
    if ! python3 "$CONFIG_HELPER" record-sync "${record_sync_args[@]}"; then
        warn "Sync succeeded but sentinel write failed -- next auto run may over-sync."
    fi
    log "Sync complete."
fi
