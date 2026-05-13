#!/usr/bin/env bash
# Regression test for scripts/ssh-vps-transport.sh.
#
# The test creates a fake ssh binary that always exits 255, then launches a
# 50-caller dogpile through scripts/ssh-vps.sh. A correct circuit breaker lets
# exactly one caller perform the two transport attempts while the rest refuse
# on the single-flight lock, then refuses late callers on cooldown.

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RUNS=1
CALLERS=50
LATE_CALLERS=5
OBSERVE_SEC="${K2BI_SSH_TEST_OBSERVE_SEC:-1}"
RUN_SLOW=false

usage() {
    echo "usage: ssh_circuit_breaker_test.sh [--runs N] [--slow]" >&2
    exit 2
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --runs)
            [[ -n "${2:-}" ]] || usage
            RUNS="$2"
            shift 2
            ;;
        --slow)
            RUN_SLOW=true
            shift
            ;;
        *)
            usage
            ;;
    esac
done

assert_eq() {
    local expected="$1"
    local actual="$2"
    local label="$3"
    if [[ "$expected" != "$actual" ]]; then
        echo "FAIL: ${label}: expected ${expected}, got ${actual}" >&2
        exit 1
    fi
}

assert_between() {
    local min="$1"
    local max="$2"
    local actual="$3"
    local label="$4"
    if (( actual < min || actual > max )); then
        echo "FAIL: ${label}: expected ${min}-${max}, got ${actual}" >&2
        exit 1
    fi
}

read_counter() {
    local counter="$1"
    if [[ -f "$counter" ]]; then
        tr -d '[:space:]' < "$counter"
    else
        printf '0'
    fi
}

count_matching_files() {
    local pattern="$1"
    local dir="$2"
    grep -l "$pattern" "$dir"/err.* 2>/dev/null | wc -l | tr -d ' '
}

write_stubs() {
    local bin_dir="$1"
    cat > "${bin_dir}/ssh" <<'SH'
#!/usr/bin/env bash
set -euo pipefail

counter="${K2BI_SSH_STUB_COUNTER:?}"
lock="${counter}.lock"

while ! mkdir "$lock" 2>/dev/null; do
    /bin/sleep 0.01
done
count=0
if [[ -f "$counter" ]]; then
    count="$(cat "$counter")"
fi
count=$((count + 1))
printf '%s\n' "$count" > "$counter"
rmdir "$lock"

cooldown="${HOME}/.cache/k2bi/ssh/cooldown"
if [[ -f "$cooldown" ]]; then
    echo "ssh-stub: cooldown existed before ssh call ${count}" >&2
    exit 70
fi

echo "ssh-stub: call ${count}" >&2
echo "ssh-stub args: $*" >&2
/bin/sleep "${K2BI_SSH_STUB_DELAY:-0.2}"
exit "${K2BI_SSH_STUB_RC:-255}"
SH
    chmod +x "${bin_dir}/ssh"

    cat > "${bin_dir}/sleep" <<'SH'
#!/usr/bin/env bash
exit 0
SH
    chmod +x "${bin_dir}/sleep"
}

run_caller() {
    local output_dir="$1"
    local label="$2"
    local index="$3"
    set +e
    "${ROOT}/scripts/ssh-vps.sh" "echo ${label}-${index}" \
        > "${output_dir}/out.${index}" \
        2> "${output_dir}/err.${index}"
    local rc=$?
    set -e
    printf '%s\n' "$rc" > "${output_dir}/rc.${index}"
}

assert_all_exit_78() {
    local output_dir="$1"
    local label="$2"
    local count="$3"
    local non_78=0
    local i rc
    for i in $(seq 1 "$count"); do
        rc="$(cat "${output_dir}/rc.${i}")"
        if [[ "$rc" != "78" ]]; then
            echo "FAIL: ${label} caller ${i} exited ${rc}, stderr:" >&2
            cat "${output_dir}/err.${i}" >&2
            non_78=$((non_78 + 1))
        fi
    done
    assert_eq "0" "$non_78" "${label} non-78 exit count"
}

run_one() {
    local run_id="$1"
    local work_dir home_dir bin_dir wave_dir late_dir counter cooldown
    work_dir="$(mktemp -d "${TMPDIR:-/tmp}/k2bi-ssh-cb.XXXXXX")"
    home_dir="${work_dir}/home"
    bin_dir="${work_dir}/bin"
    wave_dir="${work_dir}/wave"
    late_dir="${work_dir}/late"
    counter="${work_dir}/ssh-counter"
    cooldown="${home_dir}/.cache/k2bi/ssh/cooldown"
    mkdir -p "$home_dir" "$bin_dir" "$wave_dir" "$late_dir"
    write_stubs "$bin_dir"

    export HOME="$home_dir"
    export PATH="${bin_dir}:${PATH}"
    export K2BI_SSH_STUB_COUNTER="$counter"
    export K2BI_SSH_STUB_DELAY=0.2
    export K2BI_SSH_STUB_RC=255

    local start_ts
    start_ts="$(date +%s)"

    local i
    for i in $(seq 1 "$CALLERS"); do
        run_caller "$wave_dir" "wave" "$i" &
    done
    wait || true

    assert_all_exit_78 "$wave_dir" "first-wave" "$CALLERS"

    local ssh_calls lock_refusals trip_messages
    ssh_calls="$(read_counter "$counter")"
    lock_refusals="$(count_matching_files "another attempt in flight" "$wave_dir")"
    trip_messages="$(count_matching_files "Tripping" "$wave_dir")"

    assert_eq "2" "$ssh_calls" "run ${run_id} underlying ssh calls after first wave"
    assert_between 48 49 "$lock_refusals" "run ${run_id} lock refusals"
    assert_eq "1" "$trip_messages" "run ${run_id} breaker trip messages"

    if [[ ! -f "$cooldown" ]]; then
        echo "FAIL: run ${run_id} cooldown file was not written" >&2
        exit 1
    fi

    for i in $(seq 1 "$LATE_CALLERS"); do
        run_caller "$late_dir" "late" "$i"
    done
    assert_all_exit_78 "$late_dir" "late" "$LATE_CALLERS"

    local cooldown_refusals
    cooldown_refusals="$(count_matching_files "circuit open" "$late_dir")"
    assert_eq "$LATE_CALLERS" "$cooldown_refusals" "run ${run_id} late cooldown refusals"
    assert_eq "2" "$(read_counter "$counter")" "run ${run_id} underlying ssh calls after late callers"

    local now elapsed remaining
    now="$(date +%s)"
    elapsed=$((now - start_ts))
    remaining=$((OBSERVE_SEC - elapsed))
    if (( remaining > 0 )); then
        /bin/sleep "$remaining"
    fi
    assert_eq "2" "$(read_counter "$counter")" "run ${run_id} underlying ssh calls after observe window"

    rm -rf "$work_dir"
    echo "ok run ${run_id}: ssh_calls=2 lock_refusals=${lock_refusals} late_cooldown_refusals=${cooldown_refusals}"
}

run_non_transport_passthrough() {
    local work_dir home_dir bin_dir counter cooldown rc
    work_dir="$(mktemp -d "${TMPDIR:-/tmp}/k2bi-ssh-cb-pass.XXXXXX")"
    home_dir="${work_dir}/home"
    bin_dir="${work_dir}/bin"
    counter="${work_dir}/ssh-counter"
    cooldown="${home_dir}/.cache/k2bi/ssh/cooldown"
    mkdir -p "$home_dir" "$bin_dir"
    write_stubs "$bin_dir"

    export HOME="$home_dir"
    export PATH="${bin_dir}:${PATH}"
    export K2BI_SSH_STUB_COUNTER="$counter"
    export K2BI_SSH_STUB_DELAY=0
    export K2BI_SSH_STUB_RC=42

    set +e
    "${ROOT}/scripts/ssh-vps.sh" "remote exits nonzero" >/dev/null 2>"${work_dir}/err"
    rc=$?
    set -e

    assert_eq "42" "$rc" "non-transport exit pass-through"
    assert_eq "1" "$(read_counter "$counter")" "non-transport ssh call count"
    if [[ -f "$cooldown" ]]; then
        echo "FAIL: non-transport exit wrote cooldown" >&2
        exit 1
    fi

    rm -rf "$work_dir"
    echo "ok non-transport-pass-through: ssh_calls=1 rc=42"
}

run_option_passthrough() {
    local work_dir home_dir bin_dir counter calls rc
    work_dir="$(mktemp -d "${TMPDIR:-/tmp}/k2bi-ssh-cb-options.XXXXXX")"
    home_dir="${work_dir}/home"
    bin_dir="${work_dir}/bin"
    counter="${work_dir}/ssh-counter"
    calls="${work_dir}/ssh-calls"
    mkdir -p "$home_dir" "$bin_dir"
    write_stubs "$bin_dir"

    export HOME="$home_dir"
    export PATH="${bin_dir}:${PATH}"
    export K2BI_SSH_STUB_COUNTER="$counter"
    export K2BI_SSH_STUB_DELAY=0
    export K2BI_SSH_STUB_RC=42

    set +e
    "${ROOT}/scripts/ssh-vps-transport.sh" \
        -o ConnectTimeout=5 \
        -o ServerAliveInterval=15 \
        k2bi@hostinger \
        "echo options" \
        > /dev/null \
        2> "$calls"
    rc=$?
    set -e

    assert_eq "42" "$rc" "option pass-through exit"
    if ! grep -q -- "-o ConnectTimeout=5" "$calls"; then
        echo "FAIL: option pass-through missing caller ConnectTimeout option" >&2
        cat "$calls" >&2
        exit 1
    fi
    if ! grep -q -- "k2bi@hostinger" "$calls"; then
        echo "FAIL: option pass-through missing host" >&2
        cat "$calls" >&2
        exit 1
    fi

    rm -rf "$work_dir"
    echo "ok option-pass-through: ssh_calls=1 rc=42"
}

run_stale_lock_recovery() {
    local work_dir home_dir bin_dir counter lock_dir cooldown rc
    work_dir="$(mktemp -d "${TMPDIR:-/tmp}/k2bi-ssh-cb-stale.XXXXXX")"
    home_dir="${work_dir}/home"
    bin_dir="${work_dir}/bin"
    counter="${work_dir}/ssh-counter"
    lock_dir="${home_dir}/.cache/k2bi/ssh/lock.d"
    cooldown="${home_dir}/.cache/k2bi/ssh/cooldown"
    mkdir -p "$bin_dir" "$lock_dir"
    touch -t 202001010000 "$lock_dir"
    write_stubs "$bin_dir"

    export HOME="$home_dir"
    export PATH="${bin_dir}:${PATH}"
    export K2BI_SSH_STUB_COUNTER="$counter"
    export K2BI_SSH_STUB_DELAY=0
    export K2BI_SSH_STUB_RC=255

    set +e
    "${ROOT}/scripts/ssh-vps.sh" "echo stale" >/dev/null 2>"${work_dir}/err"
    rc=$?
    set -e

    assert_eq "78" "$rc" "stale lock recovery exit"
    assert_eq "2" "$(read_counter "$counter")" "stale lock recovered ssh call count"
    if [[ ! -f "$cooldown" ]]; then
        echo "FAIL: stale lock recovery did not reach cooldown write" >&2
        exit 1
    fi
    if ! grep -q "removing stale lock" "${work_dir}/err"; then
        echo "FAIL: stale lock recovery message missing" >&2
        cat "${work_dir}/err" >&2
        exit 1
    fi

    rm -rf "$work_dir"
    echo "ok stale-lock-recovery: ssh_calls=2 rc=78"
}

run_corrupt_cooldown_fails_closed() {
    local work_dir home_dir bin_dir counter cooldown rc
    work_dir="$(mktemp -d "${TMPDIR:-/tmp}/k2bi-ssh-cb-corrupt.XXXXXX")"
    home_dir="${work_dir}/home"
    bin_dir="${work_dir}/bin"
    counter="${work_dir}/ssh-counter"
    cooldown="${home_dir}/.cache/k2bi/ssh/cooldown"
    mkdir -p "$bin_dir" "$(dirname "$cooldown")"
    printf 'not-a-timestamp\n' > "$cooldown"
    write_stubs "$bin_dir"

    export HOME="$home_dir"
    export PATH="${bin_dir}:${PATH}"
    export K2BI_SSH_STUB_COUNTER="$counter"
    export K2BI_SSH_STUB_DELAY=0
    export K2BI_SSH_STUB_RC=255

    set +e
    "${ROOT}/scripts/ssh-vps.sh" "echo corrupt" >/dev/null 2>"${work_dir}/err"
    rc=$?
    set -e

    assert_eq "78" "$rc" "corrupt cooldown exit"
    assert_eq "0" "$(read_counter "$counter")" "corrupt cooldown ssh call count"
    if ! grep -q "cooldown timestamp unreadable" "${work_dir}/err"; then
        echo "FAIL: corrupt cooldown fail-closed message missing" >&2
        cat "${work_dir}/err" >&2
        exit 1
    fi

    rm -rf "$work_dir"
    echo "ok corrupt-cooldown-fails-closed: ssh_calls=0 rc=78"
}

hook_rc_for_command() {
    local command="$1"
    local err="$2"
    python3 - "$command" <<'PY' | "${ROOT}/.claude/hooks/ssh-guard.sh" >/dev/null 2>"$err"
import json
import sys

print(json.dumps({"tool_input": {"command": sys.argv[1]}}))
PY
}

assert_hook_rc() {
    local expected="$1"
    local command="$2"
    local label="$3"
    local work_dir err rc
    work_dir="$(mktemp -d "${TMPDIR:-/tmp}/k2bi-ssh-guard.XXXXXX")"
    err="${work_dir}/err"

    set +e
    hook_rc_for_command "$command" "$err"
    rc=$?
    set -e

    if [[ "$rc" != "$expected" ]]; then
        echo "FAIL: ${label}: expected hook rc ${expected}, got ${rc}" >&2
        cat "$err" >&2
        rm -rf "$work_dir"
        exit 1
    fi
    rm -rf "$work_dir"
}

run_parser_bypass_coverage() {
    assert_hook_rc 2 "/usr/bin/ssh hostinger 'echo'" "absolute ssh path blocked"
    assert_hook_rc 2 "ssh -- hostinger 'echo'" "dashdash hostinger blocked"
    assert_hook_rc 2 "ssh -- k2bi@hostinger 'echo'" "dashdash user hostinger blocked"
    assert_hook_rc 0 "scripts/ssh-vps.sh 'echo'" "wrapper allowed"
    echo "ok parser-bypass-coverage"
}

run_long_hold_no_false_reclaim() {
    local work_dir home_dir bin_dir counter holder_pid rc
    local hold_sleep second_at
    hold_sleep="${K2BI_SSH_TEST_LONG_HOLD_SLEEP_SEC:-180}"
    second_at="${K2BI_SSH_TEST_LONG_HOLD_SECOND_AT_SEC:-130}"
    work_dir="$(mktemp -d "${TMPDIR:-/tmp}/k2bi-ssh-cb-long.XXXXXX")"
    home_dir="${work_dir}/home"
    bin_dir="${work_dir}/bin"
    counter="${work_dir}/ssh-counter"
    mkdir -p "$home_dir" "$bin_dir"
    write_stubs "$bin_dir"

    export HOME="$home_dir"
    export PATH="${bin_dir}:${PATH}"
    export K2BI_SSH_STUB_COUNTER="$counter"

    (
        export K2BI_SSH_STUB_DELAY="$hold_sleep"
        export K2BI_SSH_STUB_RC=42
        "${ROOT}/scripts/ssh-vps.sh" "echo long-hold" \
            > "${work_dir}/holder.out" \
            2> "${work_dir}/holder.err"
    ) &
    holder_pid=$!

    local lock_dir="${home_dir}/.cache/k2bi/ssh/lock.d"
    local waited=0
    while [[ ! -d "$lock_dir" && "$waited" -lt 20 ]]; do
        /bin/sleep 0.1
        waited=$((waited + 1))
    done
    if [[ ! -d "$lock_dir" ]]; then
        echo "FAIL: long-hold lock was not acquired" >&2
        kill "$holder_pid" 2>/dev/null || true
        wait "$holder_pid" 2>/dev/null || true
        rm -rf "$work_dir"
        exit 1
    fi

    /bin/sleep "$second_at"

    set +e
    K2BI_SSH_STUB_DELAY=0 K2BI_SSH_STUB_RC=42 \
        "${ROOT}/scripts/ssh-vps.sh" "echo second" \
        > "${work_dir}/second.out" \
        2> "${work_dir}/second.err"
    rc=$?
    set -e

    kill "$holder_pid" 2>/dev/null || true
    wait "$holder_pid" 2>/dev/null || true

    assert_eq "78" "$rc" "long-hold concurrent caller exit"
    if ! grep -q "another attempt in flight" "${work_dir}/second.err"; then
        echo "FAIL: long-hold concurrent caller was not refused by lock" >&2
        cat "${work_dir}/second.err" >&2
        rm -rf "$work_dir"
        exit 1
    fi
    assert_eq "1" "$(read_counter "$counter")" "long-hold ssh call count"

    rm -rf "$work_dir"
    echo "ok long-hold-no-false-reclaim: ssh_calls=1 rc=78"
}

for run_id in $(seq 1 "$RUNS"); do
    run_one "$run_id"
    run_non_transport_passthrough
    run_option_passthrough
    run_stale_lock_recovery
    run_corrupt_cooldown_fails_closed
    run_parser_bypass_coverage
done
if $RUN_SLOW; then
    run_long_hold_no_false_reclaim
else
    echo "skip long-hold-no-false-reclaim (use --slow)"
fi
