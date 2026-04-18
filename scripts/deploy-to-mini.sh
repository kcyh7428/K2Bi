#!/bin/bash
# deploy-to-mini.sh -- Sync K2Bi project files from MacBook to Mac Mini
#
# Usage:
#   deploy-to-mini.sh              # auto-detect what changed, sync it
#   deploy-to-mini.sh skills       # sync .claude/skills/ + .claude/settings.json + top-level docs
#   deploy-to-mini.sh scripts      # sync scripts/
#   deploy-to-mini.sh execution    # sync execution/ (Trader tier engine, Phase 4+)
#   deploy-to-mini.sh pm2          # sync pm2/ (daemon manifest)
#   deploy-to-mini.sh all          # sync every covered category
#   deploy-to-mini.sh --dry-run    # show what would sync without doing it
#
# Excluded by design (per audit 2026-04-18): .claude/settings.local.json
# (gitignored, per-machine), .githooks/ (commit-only, Mini is Trader tier not
# dev), .pending-sync/ (per-machine consumer state), proposals/ (design docs,
# not runtime), .git/. Vault (K2Bi-Vault/) is Syncthing's job, not this script.

set -euo pipefail

MINI="macmini"
LOCAL_BASE="$HOME/Projects/K2Bi"
REMOTE_BASE="~/Projects/K2Bi"
DRY_RUN=false
MODE="${1:-auto}"

if [[ "$MODE" == "--dry-run" ]]; then
    DRY_RUN=true
    MODE="${2:-auto}"
fi

if [[ "${2:-}" == "--dry-run" ]]; then
    DRY_RUN=true
fi

GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m'

log()  { echo -e "${GREEN}[sync]${NC} $1"; }
warn() { echo -e "${YELLOW}[sync]${NC} $1"; }
err()  { echo -e "${RED}[sync]${NC} $1"; }

detect_changes() {
    local changes
    cd "$LOCAL_BASE"

    changes=$(git diff --name-only HEAD 2>/dev/null || true)

    if [[ -z "$changes" ]]; then
        changes=$(git diff --name-only HEAD~1 HEAD 2>/dev/null || true)
    fi

    # Include untracked files in all syncable categories so auto-detect picks up
    # brand-new files that have never been committed. Scope must match the
    # syncable-path list exactly -- scanning all of `.claude/` would pick up
    # runtime artifacts like `.claude/scheduled_tasks.lock` and trigger pointless
    # no-op skills deploys (Codex P3 finding 2026-04-18, third pass).
    local untracked
    untracked=$(git ls-files --others --exclude-standard \
        .claude/skills/ .claude/settings.json scripts/ execution/ pm2/ \
        CLAUDE.md DEVLOG.md README.md 2>/dev/null || true)
    changes="$changes"$'\n'"$untracked"

    echo "$changes"
}

needs_skills=false
needs_scripts=false
needs_execution=false
needs_pm2=false

categorize() {
    local changes="$1"
    # Skills category is exactly the set of paths sync_skills() actually deploys.
    # Do NOT match all of `.claude/*` -- that would trigger a pointless no-op
    # deploy every time Claude Code writes a local-only runtime file like
    # .claude/scheduled_tasks.lock (Codex P3 finding 2026-04-18, third pass).
    if echo "$changes" | grep -qE '^\.claude/skills/|^\.claude/settings\.json$|^CLAUDE\.md$|^DEVLOG\.md$|^README\.md$'; then
        needs_skills=true
    fi
    if echo "$changes" | grep -qE '^scripts/'; then
        needs_scripts=true
    fi
    if echo "$changes" | grep -qE '^execution/'; then
        needs_execution=true
    fi
    if echo "$changes" | grep -qE '^pm2/'; then
        needs_pm2=true
    fi
}

sync_singleton() {
    # Push a single tracked file from local to Mini, OR delete the remote copy
    # if it was deleted locally. rsync --delete only handles directory trees;
    # standalone files need explicit delete semantics (Codex P2 finding
    # 2026-04-18, second pass).
    local local_path="$1"
    local remote_rel="$2"
    local rsync_flag=""
    $DRY_RUN && rsync_flag="--dry-run"

    if [[ -f "$local_path" ]]; then
        rsync -av $rsync_flag "$local_path" "$MINI:$REMOTE_BASE/$remote_rel"
    else
        # Deleted locally. Mirror the deletion on the Mini so state stays
        # consistent. Run the existence-check + rm in one ssh call to avoid
        # a TOCTOU window + an extra round-trip.
        if $DRY_RUN; then
            warn "  (dry-run) would remove $MINI:$REMOTE_BASE/$remote_rel if present"
        else
            local result
            result=$(ssh "$MINI" "if [ -f $REMOTE_BASE/$remote_rel ]; then rm $REMOTE_BASE/$remote_rel && echo REMOVED; else echo ABSENT; fi")
            if [[ "$result" == "REMOVED" ]]; then
                log "  removed remote $remote_rel (deleted locally)"
            fi
        fi
    fi
}

sync_skills() {
    log "Syncing skills + settings + top-level docs..."
    local rsync_flag=""
    $DRY_RUN && rsync_flag="--dry-run"

    for doc in CLAUDE.md README.md DEVLOG.md; do
        sync_singleton "$LOCAL_BASE/$doc" "$doc"
    done

    # .claude/settings.json ships; settings.local.json is per-machine (gitignored, never sync)
    sync_singleton "$LOCAL_BASE/.claude/settings.json" ".claude/settings.json"

    rsync -av $rsync_flag --delete "$LOCAL_BASE/.claude/skills/" "$MINI:$REMOTE_BASE/.claude/skills/"

    if ! $DRY_RUN; then
        log "Verifying skills on Mini..."
        local remote_count
        remote_count=$(ssh "$MINI" "ls -d $REMOTE_BASE/.claude/skills/*/ 2>/dev/null | wc -l" | tr -d ' ')
        local local_count
        local_count=$(ls -d "$LOCAL_BASE/.claude/skills/"*/ 2>/dev/null | wc -l | tr -d ' ')
        if [[ "$remote_count" == "$local_count" ]]; then
            log "Skills verified: $remote_count skill folders on both machines"
        else
            warn "Skill count mismatch: local=$local_count remote=$remote_count"
        fi
    fi
}

sync_scripts() {
    log "Syncing scripts/..."
    local rsync_flag=""
    $DRY_RUN && rsync_flag="--dry-run"

    rsync -av $rsync_flag "$LOCAL_BASE/scripts/" "$MINI:$REMOTE_BASE/scripts/"
}

sync_tree_or_delete() {
    # Mirror a local directory tree to the Mini, OR delete the remote tree if
    # the local one has been removed. Prevents a "rsync: No such file or
    # directory" abort when Keith renames or removes a top-level dir locally
    # and then runs /sync -- auto-detect still flags the deleted path, so we
    # need to turn that flag into an actual remote removal (Codex P2 finding
    # 2026-04-18, third pass).
    local local_dir="$1"    # absolute path
    local remote_rel="$2"    # relative to REMOTE_BASE
    local label="$3"         # human-readable label for log output
    local rsync_flag=""
    $DRY_RUN && rsync_flag="--dry-run"

    if [[ -d "$local_dir" ]]; then
        # Python-oriented excludes are harmless for non-Python trees (pm2/).
        rsync -av $rsync_flag --delete \
            --exclude '__pycache__/' --exclude '*.pyc' --exclude '.venv/' \
            "$local_dir/" "$MINI:$REMOTE_BASE/$remote_rel/"
    else
        if $DRY_RUN; then
            warn "  (dry-run) would remove $MINI:$REMOTE_BASE/$remote_rel/ if present"
        else
            local result
            result=$(ssh "$MINI" "if [ -d $REMOTE_BASE/$remote_rel ]; then rm -rf $REMOTE_BASE/$remote_rel && echo REMOVED; else echo ABSENT; fi")
            if [[ "$result" == "REMOVED" ]]; then
                log "  removed remote $label tree (deleted locally)"
            fi
        fi
    fi
}

sync_execution() {
    log "Syncing execution/ (Trader tier engine)..."
    sync_tree_or_delete "$LOCAL_BASE/execution" "execution" "execution"
}

sync_pm2() {
    log "Syncing pm2/ (daemon manifest)..."
    sync_tree_or_delete "$LOCAL_BASE/pm2" "pm2" "pm2"
}

case "$MODE" in
    skills)
        needs_skills=true
        ;;
    scripts)
        needs_scripts=true
        ;;
    execution)
        needs_execution=true
        ;;
    pm2)
        needs_pm2=true
        ;;
    all)
        needs_skills=true
        needs_scripts=true
        needs_execution=true
        needs_pm2=true
        ;;
    auto)
        changes=$(detect_changes)
        if [[ -z "$changes" || "$changes" == $'\n' ]]; then
            warn "No changes detected. Use 'all' to force full sync."
            exit 0
        fi
        categorize "$changes"
        if ! $needs_skills && ! $needs_scripts && ! $needs_execution && ! $needs_pm2; then
            warn "Changes detected but none in syncable categories."
            echo "$changes"
            exit 0
        fi
        ;;
    *)
        err "Unknown mode: $MODE"
        echo "Usage: deploy-to-mini.sh [auto|skills|scripts|execution|pm2|all] [--dry-run]"
        exit 1
        ;;
esac

if ! ssh -o ConnectTimeout=5 "$MINI" "echo ok" &>/dev/null; then
    err "Cannot reach Mac Mini (ssh macmini). Is it on?"
    exit 1
fi

# Ensure remote base exists (K2Bi's first deploy will need this)
ssh "$MINI" "mkdir -p $REMOTE_BASE/.claude/skills $REMOTE_BASE/scripts $REMOTE_BASE/execution $REMOTE_BASE/pm2" || {
    err "Failed to create remote base directories"
    exit 1
}

$DRY_RUN && warn "DRY RUN -- no files will be changed"

echo ""
log "Sync plan:"
$needs_skills && log "  - .claude/skills/ + .claude/settings.json + CLAUDE.md + DEVLOG.md + README.md"
$needs_scripts && log "  - scripts/"
$needs_execution && log "  - execution/"
$needs_pm2 && log "  - pm2/"
echo ""

$needs_skills && sync_skills
$needs_scripts && sync_scripts
$needs_execution && sync_execution
$needs_pm2 && sync_pm2

echo ""
if $DRY_RUN; then
    log "Dry run complete. Run without --dry-run to sync."
else
    log "Sync complete."
fi
