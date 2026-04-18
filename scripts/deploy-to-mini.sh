#!/bin/bash
# deploy-to-mini.sh -- Sync K2Bi project files from MacBook to Mac Mini.
#
# Usage:
#   deploy-to-mini.sh              # auto-detect what changed, sync those categories
#   deploy-to-mini.sh <category>   # force a single category from deploy-config.yml
#   deploy-to-mini.sh all          # sync every category
#   deploy-to-mini.sh --dry-run    # show what would sync without doing it
#
# The category list + the set of paths each category covers live in
# scripts/deploy-config.yml. Both this script and the /invest-ship step 12
# preflight read that file via scripts/lib/deploy_config.py. To add a new
# deployed path: append to deploy-config.yml's `targets:`. To add an
# intentionally-local path: append to `excludes:`. The preflight will block
# /ship until the drift is resolved.

set -euo pipefail

MINI="macmini"
LOCAL_BASE="$HOME/Projects/K2Bi"
REMOTE_BASE="~/Projects/K2Bi"
CONFIG_HELPER="$LOCAL_BASE/scripts/lib/deploy_config.py"
DRY_RUN=false
MODE=""

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

detect_changes() {
    # Tracked modified files since HEAD, plus untracked files anywhere within
    # a deployed tree. Covers new-files-never-committed so /ship catches them
    # before the first commit that lands them.
    cd "$LOCAL_BASE"
    local changes
    changes=$(git diff --name-only HEAD 2>/dev/null || true)
    if [[ -z "$changes" ]]; then
        changes=$(git diff --name-only HEAD~1 HEAD 2>/dev/null || true)
    fi
    local untracked
    untracked=$(git ls-files --others --exclude-standard 2>/dev/null || true)
    if [[ -n "$untracked" ]]; then
        if [[ -n "$changes" ]]; then
            changes=$(printf '%s\n%s\n' "$changes" "$untracked")
        else
            changes="$untracked"
        fi
    fi
    printf '%s\n' "$changes" | awk 'NF'
}

classify_changes() {
    # Echo the set of distinct categories touched by $1 (newline-separated file list).
    # Uses the config helper so the category semantics stay in one place.
    local files="$1"
    if [[ -z "$files" ]]; then
        return 0
    fi
    printf '%s\n' "$files" | python3 "$CONFIG_HELPER" classify \
        | awk -F '\t' '$1 != "uncovered" {print $1}' \
        | sort -u
}

rsync_target() {
    # rsync one deploy-config.yml target (file or directory). Handles the
    # local-deleted-but-remote-present case so the Mini mirrors deletions.
    local local_rel="$1"    # path relative to LOCAL_BASE; from config verbatim
    local rsync_flag=""
    $DRY_RUN && rsync_flag="--dry-run"
    local stripped="${local_rel%/}"

    if [[ -d "$LOCAL_BASE/$stripped" ]]; then
        rsync -av $rsync_flag --delete \
            --exclude '__pycache__/' --exclude '*.pyc' --exclude '.venv/' \
            "$LOCAL_BASE/$stripped/" "$MINI:$REMOTE_BASE/$stripped/"
    elif [[ -f "$LOCAL_BASE/$stripped" ]]; then
        rsync -av $rsync_flag "$LOCAL_BASE/$stripped" "$MINI:$REMOTE_BASE/$stripped"
    else
        if $DRY_RUN; then
            warn "  (dry-run) would remove $MINI:$REMOTE_BASE/$stripped if present"
            return 0
        fi
        # Mirror local deletion to remote so state stays consistent.
        local result
        result=$(ssh "$MINI" "
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
    # drift between MacBook and Mini skill-folder counts surfaces loudly.
    if [[ "$category" == "skills" ]] && ! $DRY_RUN; then
        local remote_count local_count
        remote_count=$(ssh "$MINI" "ls -d $REMOTE_BASE/.claude/skills/*/ 2>/dev/null | wc -l" | tr -d ' ')
        local_count=$(ls -d "$LOCAL_BASE/.claude/skills/"*/ 2>/dev/null | wc -l | tr -d ' ')
        if [[ "$remote_count" == "$local_count" ]]; then
            log "  skills verified: $remote_count skill folders on both machines"
        else
            warn "  skills count mismatch: local=$local_count remote=$remote_count"
        fi
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

RUN_CATEGORIES=""
case "$MODE" in
    auto)
        changes=$(detect_changes)
        if [[ -z "$changes" ]]; then
            warn "No changes detected. Use 'all' to force full sync."
            exit 0
        fi
        RUN_CATEGORIES=$(classify_changes "$changes")
        if [[ -z "$RUN_CATEGORIES" ]]; then
            warn "Changes detected but none map to deploy-config.yml categories."
            echo "$changes"
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

if ! ssh -o ConnectTimeout=5 "$MINI" "echo ok" &>/dev/null; then
    err "Cannot reach Mac Mini (ssh $MINI). Is it on?"
    exit 1
fi

# Ensure the remote repo root + each category's directory prefix exist.
# The rsync commands build directory structure as they go, but a first-time
# deploy into a virgin REMOTE_BASE needs the parent there.
REMOTE_MKDIRS=$(python3 "$CONFIG_HELPER" list-targets | awk -F/ '{if ($1 != $0) print $1}' | sort -u)
ssh "$MINI" "mkdir -p $REMOTE_BASE $(echo "$REMOTE_MKDIRS" | awk -v base="$REMOTE_BASE" '{print base"/"$0}' | tr '\n' ' ')" || {
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
if $DRY_RUN; then
    log "Dry run complete. Run without --dry-run to sync."
else
    log "Sync complete."
fi
