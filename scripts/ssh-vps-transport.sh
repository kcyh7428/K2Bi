#!/usr/bin/env bash
# Drop-in ssh replacement with a single-flight circuit breaker.
# Used directly by rsync via -e and indirectly by scripts/ssh-vps.sh.

set -euo pipefail

STATE_DIR="${HOME}/.cache/k2bi/ssh"
LOCKDIR="${STATE_DIR}/lock.d"
LOCK_PID="${LOCKDIR}/pid"
COOLDOWN="${STATE_DIR}/cooldown"
COOLDOWN_SEC=600
cooldown_tmp=""

mkdir -p "$STATE_DIR"

if [[ -f "$COOLDOWN" ]]; then
    now="$(date +%s)"
    set_at="$(cat "$COOLDOWN" 2>/dev/null || printf '0')"
    case "$set_at" in
        ''|*[!0-9]*)
            echo "ssh-vps: cooldown timestamp unreadable. Refusing fail-closed; operator may remove ${COOLDOWN} after inspection." >&2
            exit 78
            ;;
    esac
    age=$((now - set_at))
    if (( age < COOLDOWN_SEC )); then
        echo "ssh-vps: circuit open (${age}s into ${COOLDOWN_SEC}s cooldown). Refusing." >&2
        exit 78
    fi
    rm -f "$COOLDOWN"
fi

write_lock_pid() {
    if ! printf '%s\n' "$$" > "$LOCK_PID"; then
        rm -f "$LOCK_PID" 2>/dev/null || true
        rmdir "$LOCKDIR" 2>/dev/null || true
        echo "ssh-vps: could not write lock pid; refusing fail-closed." >&2
        exit 78
    fi
}

read_lock_pid() {
    local pid
    pid="$(cat "$LOCK_PID" 2>/dev/null || true)"
    case "$pid" in
        ''|*[!0-9]*) return 1 ;;
        *) printf '%s\n' "$pid" ;;
    esac
}

pid_is_alive() {
    local pid="$1"
    kill -0 "$pid" 2>/dev/null
}

reclaim_lock() {
    local pid="${1:-unknown}"
    echo "ssh-vps: removing stale lock for pid=${pid}." >&2
    rm -f "$LOCK_PID" 2>/dev/null || true
    if rmdir "$LOCKDIR" 2>/dev/null && mkdir "$LOCKDIR" 2>/dev/null; then
        write_lock_pid
        return 0
    fi
    echo "ssh-vps: stale lock could not be reclaimed; refusing." >&2
    return 1
}

acquire_lock() {
    if mkdir "$LOCKDIR" 2>/dev/null; then
        write_lock_pid
        return 0
    fi

    local holder_pid
    if holder_pid="$(read_lock_pid)"; then
        if pid_is_alive "$holder_pid"; then
            echo "ssh-vps: another attempt in flight, refusing concurrent caller." >&2
            return 1
        fi
        reclaim_lock "$holder_pid"
        return $?
    fi

    # Grace window for the mkdir-then-write race in a just-started lock holder.
    sleep 1
    if holder_pid="$(read_lock_pid)"; then
        if pid_is_alive "$holder_pid"; then
            echo "ssh-vps: another attempt in flight, refusing concurrent caller." >&2
            return 1
        fi
        reclaim_lock "$holder_pid"
        return $?
    fi

    reclaim_lock "missing"
}

if ! acquire_lock; then
    exit 78
fi

cleanup() {
    rm -f "$LOCK_PID" 2>/dev/null || true
    rmdir "$LOCKDIR" 2>/dev/null || true
    if [[ -n "${cooldown_tmp:-}" ]]; then
        rm -f "$cooldown_tmp"
    fi
}
trap cleanup EXIT

if [[ "${K2BI_SSH_OVERRIDE:-}" == "human-debug" ]]; then
    reason="${K2BI_SSH_OVERRIDE_REASON:-NONE}"
    echo "ssh-vps: HUMAN OVERRIDE engaged; reason=${reason}; single attempt only" >&2
    set +e
    ssh "$@"
    rc=$?
    set -e
    exit "$rc"
fi

try_ssh() {
    ssh \
        -o ConnectTimeout=15 \
        -o BatchMode=yes \
        -o ServerAliveInterval=15 \
        -o ServerAliveCountMax=4 \
        "$@"
}

rc=0
try_ssh "$@" || rc=$?
if [[ "$rc" -ne 255 ]]; then
    exit "$rc"
fi

sleep 30

rc=0
try_ssh "$@" || rc=$?
if [[ "$rc" -ne 255 ]]; then
    exit "$rc"
fi

write_cooldown() {
    cooldown_tmp="${COOLDOWN}.$$"
    if ! date +%s > "$cooldown_tmp"; then
        rm -f "$cooldown_tmp"
        cooldown_tmp=""
        echo "ssh-vps: failed to write cooldown timestamp; refusing fail-closed." >&2
        exit 78
    fi
    if ! mv "$cooldown_tmp" "$COOLDOWN"; then
        rm -f "$cooldown_tmp"
        cooldown_tmp=""
        echo "ssh-vps: failed to publish cooldown timestamp; refusing fail-closed." >&2
        exit 78
    fi
    cooldown_tmp=""
}

write_cooldown
echo "ssh-vps: 2 transport failures (rc=255) with 30s gap. Tripping ${COOLDOWN_SEC}s cooldown. Operator: investigate sshd MaxStartups / firewall / VPS load." >&2
exit 78
