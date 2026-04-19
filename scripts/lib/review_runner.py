"""Unified code-review runner with guaranteed progress.

Three guarantees:
  1. Deadline: no single review exceeds --deadline wall-clock seconds.
     Soft warning at 0.67 * deadline, hard SIGTERM at deadline, SIGKILL
     10s later if the child still hasn't exited.
  2. Fallback: if the primary reviewer (Codex by default) exits non-zero
     or hits the deadline, automatically retry on the secondary reviewer
     (MiniMax) for the same scope. If both fail, exit code 2.
  3. Visibility: a watchdog thread injects synthetic HEARTBEAT lines into
     the unified log every --heartbeat-interval seconds (default 5s)
     regardless of vendor-side activity, and escalates to HEARTBEAT_STALE
     after 30s of no log growth and WEDGE_SUSPECTED after 120s. This is
     what makes `scripts/review-poll.sh` always show *something* new, so
     Claude can never mistake "in final inference" for "wedged".

Nothing in this file calls the Bash tool; Codex and MiniMax are spawned
via subprocess.Popen, so the .claude PreToolUse guard hook does not block
them -- the hook only fires on direct user-invoked Bash calls.
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import signal
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(
    subprocess.check_output(
        ["git", "rev-parse", "--show-toplevel"], text=True
    ).strip()
)
ARCHIVE_DIR = REPO_ROOT / ".code-reviews"
CODEX_PLUGIN_DEFAULT = (
    Path.home() / ".claude" / "plugins" / "marketplaces"
    / "openai-codex" / "plugins" / "codex"
)

DEFAULT_DEADLINE_S = 360
DEFAULT_HEARTBEAT_S = 5
HEARTBEAT_STALE_AFTER_S = 30
WEDGE_SUSPECTED_AFTER_S = 120
KILL_GRACE_S = 10

_log_lock = threading.Lock()


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def job_id() -> str:
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%SZ")
    return f"{ts}_{secrets.token_hex(3)}"


def log_line(logf, text: str) -> None:
    if not text.endswith("\n"):
        text += "\n"
    with _log_lock:
        logf.write(text)
        logf.flush()


def write_state(state_path: Path, state: dict) -> None:
    tmp = state_path.with_suffix(state_path.suffix + ".tmp")
    tmp.write_text(json.dumps(state, indent=2))
    tmp.replace(state_path)


def _working_tree_eisdir_hazard(repo_root: Path) -> str | None:
    """Return the first path that would crash Codex's working-tree walk
    with EISDIR, or None if the tree is safe for Codex.

    Codex's `--scope working-tree` walks the dirty tree and calls read()
    on every path. On a directory, that raises
    `EISDIR: illegal operation on a directory, read` and Codex exits in
    <1s. Observed during Cycle 7: nested git worktrees (gitignored but
    physically present) and untracked top-level directories both trigger
    this. We pre-detect both shapes so the wrapper can skip Codex and
    route to MiniMax immediately instead of logging a failed attempt
    on every call.
    """
    # Case 1: untracked directories visible to git as `??` (not gitignored).
    # --directory collapses each entirely-untracked dir into a single
    # trailing-slash entry.
    try:
        result = subprocess.run(
            ["git", "ls-files", "--others", "--exclude-standard", "--directory"],
            cwd=repo_root, capture_output=True, text=True, check=True,
        )
        for line in result.stdout.splitlines():
            if line.endswith("/"):
                return line.rstrip("/")
    except (subprocess.CalledProcessError, FileNotFoundError, OSError):
        pass

    # Case 2: nested git worktrees (physically present on disk but
    # gitignored; the Cycle 7 failure was .claude/worktrees/<slug>/).
    try:
        result = subprocess.run(
            ["git", "worktree", "list", "--porcelain"],
            cwd=repo_root, capture_output=True, text=True, check=True,
        )
        for line in result.stdout.splitlines():
            if not line.startswith("worktree "):
                continue
            wt_path = Path(line[len("worktree "):].strip())
            try:
                rel = wt_path.relative_to(repo_root)
            except ValueError:
                continue
            if str(rel) != ".":
                return str(rel)
    except (subprocess.CalledProcessError, FileNotFoundError, OSError):
        pass

    return None


def codex_unavailable_reason(scope: str, repo_root: Path,
                             codex_plugin: Path) -> str | None:
    """Return a short reason string if Codex cannot review this scope, else None.

    The reason is written verbatim to the job's state.json under
    reviewer_attempts[].reason and to the unified log as REVIEWER_SKIP so
    the fallback path is observable in review-poll output.
    """
    if scope == "plan":
        return ("plan scope requires --path which current codex-companion.mjs "
                "dropped; plan reviews always route to MiniMax")
    companion = codex_plugin / "scripts" / "codex-companion.mjs"
    if not companion.is_file():
        return f"codex-companion.mjs not found at {companion}"
    hazard = _working_tree_eisdir_hazard(repo_root)
    if hazard is not None:
        return (f"codex --scope working-tree would EISDIR on '{hazard}'; "
                f"routing to MiniMax until the path is removed or committed")
    return None


def build_codex_cmd(scope: str, files: list[str] | None, plan: str | None,
                    focus: str, codex_plugin: Path) -> list[str] | None:
    """Return argv for Codex companion, or None when Codex can't handle scope.

    Skip conditions are centralized in codex_unavailable_reason(); if that
    returns a string the wrapper logs the reason and falls back to MiniMax.
    Verified against live `codex-companion.mjs --help` on 2026-04-19:

      Usage:
        review              [--wait|--background] [--base <ref>] [--scope <auto|working-tree|branch>]
        adversarial-review  [--wait|--background] [--base <ref>] [--scope <auto|working-tree|branch>] [focus text]

    Key constraints:
      * `review` does not accept --focus. Use `adversarial-review` whenever
        a focus string is supplied.
      * `adversarial-review` takes the focus as a POSITIONAL argument, not
        a --focus flag. Passing --focus errors out.
      * Neither subcommand supports --path or --files, so Codex cannot scope
        to a single plan file or to an explicit subset of the working tree.
      * Codex walks the dirty tree and read()s each path, EISDIRing on any
        untracked or worktree directory -- pre-detected above.

    K2Bi scope -> Codex argv:
      "diff"           -> adversarial-review --wait --scope working-tree [focus]
      "working-tree"   -> adversarial-review --wait --scope working-tree [focus]
      "files"          -> adversarial-review --wait --scope working-tree [focus]
                          (Codex loses the subset; callers wanting subset
                          fidelity should use --primary minimax.)
      "plan"           -> None  (forces fallback to MiniMax)
    """
    if codex_unavailable_reason(scope, REPO_ROOT, codex_plugin) is not None:
        return None
    subcmd = "adversarial-review" if focus else "review"
    cmd = ["node", str(codex_plugin / "scripts" / "codex-companion.mjs"),
           subcmd, "--wait", "--scope", "working-tree"]
    if focus and subcmd == "adversarial-review":
        cmd.append(focus)
    return cmd


def build_minimax_cmd(scope: str, files: list[str] | None, plan: str | None,
                      focus: str) -> list[str]:
    script = str(REPO_ROOT / "scripts" / "minimax-review.sh")
    cmd = [script, "--scope", scope]
    if files:
        cmd += ["--files", ",".join(files)]
    if plan:
        cmd += ["--plan", plan]
    if focus:
        cmd += ["--focus", focus]
    return cmd


def spawn_child(cmd: list[str], logf, extra_env: dict | None = None
                ) -> subprocess.Popen:
    env = os.environ.copy()
    if extra_env:
        env.update(extra_env)
    env["CLAUDE_PLUGIN_ROOT"] = str(CODEX_PLUGIN_DEFAULT)
    log_line(logf, f"[{utc_now_iso()}] SPAWN argv={cmd!r}")
    return subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        bufsize=1,
        text=True,
        env=env,
        cwd=str(REPO_ROOT),
        preexec_fn=os.setsid,
    )


def reader_thread(proc: subprocess.Popen, logf,
                  last_activity: list[float]) -> threading.Thread:
    def run():
        if proc.stdout is None:
            return
        for line in proc.stdout:
            last_activity[0] = time.time()
            log_line(logf, line.rstrip("\n"))
        try:
            proc.stdout.close()
        except Exception:
            pass

    t = threading.Thread(target=run, daemon=True)
    t.start()
    return t


def watchdog_thread(proc: subprocess.Popen, logf, state_path: Path,
                    state: dict, deadline_s: int, heartbeat_s: int,
                    last_activity: list[float],
                    stop_event: threading.Event) -> threading.Thread:
    def run():
        start = time.time()
        soft_at = start + (deadline_s * 2 // 3)
        hard_at = start + deadline_s
        warned_soft = False
        warned_wedge = False
        while not stop_event.is_set():
            if proc.poll() is not None:
                return
            now = time.time()
            elapsed = now - start
            stale = now - last_activity[0]

            phase = "running_commands"
            if stale >= WEDGE_SUSPECTED_AFTER_S:
                phase = "wedge_suspected"
                if not warned_wedge:
                    log_line(logf, f"[{utc_now_iso()}] WEDGE_SUSPECTED "
                             f"elapsed={elapsed:.0f}s stale={stale:.0f}s "
                             f"(no progress in >{WEDGE_SUSPECTED_AFTER_S}s)")
                    warned_wedge = True
            elif stale >= HEARTBEAT_STALE_AFTER_S:
                phase = "final_inference"
                log_line(logf, f"[{utc_now_iso()}] HEARTBEAT_STALE "
                         f"elapsed={elapsed:.0f}s stale={stale:.0f}s "
                         f"(reviewer in pure inference; no log activity)")
            else:
                log_line(logf, f"[{utc_now_iso()}] HEARTBEAT "
                         f"elapsed={elapsed:.0f}s stale={stale:.0f}s")

            state.update({
                "status": "running",
                "phase": phase,
                "elapsed_s": round(elapsed, 1),
                "last_activity_s_ago": round(stale, 1),
                "deadline_remaining_s": max(0, round(hard_at - now, 1)),
                "updated_at": utc_now_iso(),
            })
            write_state(state_path, state)

            if not warned_soft and now >= soft_at:
                log_line(logf, f"[{utc_now_iso()}] SOFT_DEADLINE "
                         f"elapsed={elapsed:.0f}s/{deadline_s}s "
                         f"(approaching hard deadline)")
                warned_soft = True

            if now >= hard_at:
                log_line(logf, f"[{utc_now_iso()}] HARD_DEADLINE "
                         f"elapsed={elapsed:.0f}s; SIGTERM")
                try:
                    os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
                except ProcessLookupError:
                    pass
                time.sleep(KILL_GRACE_S)
                if proc.poll() is None:
                    log_line(logf, f"[{utc_now_iso()}] SIGKILL after "
                             f"{KILL_GRACE_S}s grace")
                    try:
                        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                return

            stop_event.wait(heartbeat_s)

    t = threading.Thread(target=run, daemon=True)
    t.start()
    return t


def run_one_reviewer(
    reviewer: str,
    cmd: list[str],
    job: str,
    log_path: Path,
    state_path: Path,
    state: dict,
    deadline_s: int,
    heartbeat_s: int,
) -> int:
    """Run a single reviewer end-to-end with the three guarantees.

    Returns the child's exit code; 124 if killed by the deadline.
    """
    attempt_start = time.time()
    with log_path.open("a", buffering=1) as logf:
        log_line(logf, f"[{utc_now_iso()}] REVIEWER_START reviewer={reviewer} "
                 f"job={job} deadline={deadline_s}s heartbeat={heartbeat_s}s")
        try:
            proc = spawn_child(cmd, logf)
        except FileNotFoundError as e:
            log_line(logf, f"[{utc_now_iso()}] SPAWN_FAILED {e}")
            state.update({"status": "spawn_failed", "error": str(e),
                          "reviewer_current": reviewer})
            write_state(state_path, state)
            return 127

        state["reviewer_current"] = reviewer
        state["pid"] = proc.pid
        write_state(state_path, state)

        last_activity = [time.time()]
        stop_event = threading.Event()
        reader = reader_thread(proc, logf, last_activity)
        watchdog = watchdog_thread(proc, logf, state_path, state, deadline_s,
                                   heartbeat_s, last_activity, stop_event)

        rc = proc.wait()
        stop_event.set()
        reader.join(timeout=5)
        watchdog.join(timeout=5)

        elapsed = time.time() - attempt_start
        hit_deadline = elapsed >= deadline_s - 1
        effective_rc = 124 if hit_deadline and rc != 0 else rc

        if effective_rc == 0:
            try:
                log_text = log_path.read_text()
            except OSError:
                log_text = ""
            verdict_markers = (
                "# Codex Review", "# MiniMax",
                "APPROVE", "NEEDS-ATTENTION",
                '"verdict"', "Review output captured",
            )
            if not any(m in log_text for m in verdict_markers):
                log_line(logf, f"[{utc_now_iso()}] QUALITY_GATE_FAIL "
                         f"reviewer={reviewer} rc=0 but no verdict marker "
                         f"in log; forcing fallback")
                effective_rc = 125

        log_line(logf, f"[{utc_now_iso()}] REVIEWER_END reviewer={reviewer} "
                 f"rc={rc} effective_rc={effective_rc} elapsed={elapsed:.1f}s")
        return effective_rc


def run_fallback_chain(args: argparse.Namespace, job: str, log_path: Path,
                       state_path: Path, state: dict) -> int:
    primary = args.primary
    secondary = "minimax" if primary == "codex" else "codex"
    files = ([p.strip() for p in args.files.split(",") if p.strip()]
             if args.files else None)

    def cmd_for(reviewer: str) -> list[str] | None:
        if reviewer == "codex":
            return build_codex_cmd(args.scope, files, args.plan, args.focus,
                                   Path(args.codex_plugin))
        return build_minimax_cmd(args.scope, files, args.plan, args.focus)

    for idx, reviewer in enumerate([primary, secondary]):
        cmd = cmd_for(reviewer)
        state["reviewer_attempts"] = state.get("reviewer_attempts", [])
        if cmd is None:
            if reviewer == "codex":
                reason = codex_unavailable_reason(
                    args.scope, REPO_ROOT, Path(args.codex_plugin)
                ) or "codex plugin/script not found"
            else:
                reason = "minimax command not buildable"
            state["reviewer_attempts"].append(
                {"reviewer": reviewer, "result": "unavailable",
                 "reason": reason})
            with log_path.open("a") as logf:
                log_line(logf, f"[{utc_now_iso()}] REVIEWER_SKIP "
                         f"reviewer={reviewer} reason={reason}")
            continue
        rc = run_one_reviewer(reviewer, cmd, job, log_path, state_path, state,
                              args.deadline, args.heartbeat_interval)
        state["reviewer_attempts"].append(
            {"reviewer": reviewer, "exit_code": rc,
             "result": "ok" if rc == 0 else
                       "timed_out" if rc == 124 else "error"})
        if rc == 0:
            state.update({"status": "completed", "primary_used": primary,
                          "fallback_used": idx > 0, "exit_code": 0,
                          "ended_at": utc_now_iso()})
            write_state(state_path, state)
            return 0
        with log_path.open("a") as logf:
            why = "deadline" if rc == 124 else f"exit_{rc}"
            if idx == 0:
                log_line(logf, f"[{utc_now_iso()}] FALLBACK triggering "
                         f"{secondary} ({reviewer} failed: {why})")

    state.update({"status": "both_failed", "exit_code": 2,
                  "ended_at": utc_now_iso()})
    write_state(state_path, state)
    return 2


def cmd_poll(args: argparse.Namespace) -> int:
    state_path = ARCHIVE_DIR / f"{args.poll}.json"
    if not state_path.is_file():
        print(json.dumps({"error": "unknown_job_id", "job_id": args.poll}))
        return 1
    state = json.loads(state_path.read_text())
    tail_lines: list[str] = []
    log_path = Path(state.get("log_path", ""))
    if log_path.is_file():
        with log_path.open("rb") as f:
            try:
                f.seek(-4096, os.SEEK_END)
            except OSError:
                f.seek(0)
            tail_lines = f.read().decode("utf-8", errors="replace").splitlines()[-20:]
    out = {
        "job_id": state.get("job_id"),
        "status": state.get("status"),
        "phase": state.get("phase"),
        "elapsed_s": state.get("elapsed_s"),
        "last_activity_s_ago": state.get("last_activity_s_ago"),
        "deadline_remaining_s": state.get("deadline_remaining_s"),
        "reviewer_current": state.get("reviewer_current"),
        "reviewer_attempts": state.get("reviewer_attempts", []),
        "primary_used": state.get("primary_used"),
        "fallback_used": state.get("fallback_used"),
        "exit_code": state.get("exit_code"),
        "log_path": state.get("log_path"),
        "tail": tail_lines,
    }
    should_poll = state.get("status") == "running"
    out["should_poll_again"] = should_poll
    out["recommended_poll_interval_s"] = 30 if should_poll else 0
    print(json.dumps(out, indent=2))
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
    job = job_id()
    log_path = ARCHIVE_DIR / f"{job}.log"
    state_path = ARCHIVE_DIR / f"{job}.json"
    state = {
        "job_id": job, "scope": args.scope, "primary_requested": args.primary,
        "focus": args.focus, "files": args.files, "plan": args.plan,
        "deadline_s": args.deadline, "heartbeat_interval_s": args.heartbeat_interval,
        "log_path": str(log_path), "state_path": str(state_path),
        "started_at": utc_now_iso(), "started_at_ts": time.time(),
        "status": "starting",
    }
    write_state(state_path, state)
    log_path.write_text(
        f"[{utc_now_iso()}] JOB_START job={job} scope={args.scope} "
        f"primary={args.primary} deadline={args.deadline}s\n"
    )

    if not args.wait:
        pid = os.fork()
        if pid > 0:
            rel = log_path.relative_to(REPO_ROOT)
            rel_state = state_path.relative_to(REPO_ROOT)
            print(json.dumps({
                "job_id": job,
                "log_path": str(rel),
                "state_path": str(rel_state),
                "pid": pid,
                "hint_poll_cmd": f"scripts/review-poll.sh {job}",
                "hint_poll_interval_s": 30,
            }, indent=2))
            return 0
        os.setsid()
        for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
            signal.signal(sig, signal.SIG_DFL)
        devnull = os.open(os.devnull, os.O_RDWR)
        os.dup2(devnull, 0)
        os.dup2(devnull, 1)
        os.dup2(devnull, 2)
        os.close(devnull)

    rc = run_fallback_chain(args, job, log_path, state_path, state)
    if args.wait:
        print(json.dumps({
            "job_id": job, "exit_code": rc,
            "status": state.get("status"),
            "log_path": str(log_path.relative_to(REPO_ROOT)),
        }, indent=2))
    sys.exit(rc)


def main() -> int:
    p = argparse.ArgumentParser(
        description="Unified code-review runner (Codex + MiniMax fallback)."
    )
    p.add_argument("scope", nargs="?",
                   choices=["diff", "working-tree", "files", "plan"],
                   default="diff")
    p.add_argument("--primary", choices=["codex", "minimax"], default="codex")
    p.add_argument("--files", default=None,
                   help="Comma-separated file list (diff/files scope)")
    p.add_argument("--plan", default=None,
                   help="Plan file path (plan scope)")
    p.add_argument("--focus", default="")
    p.add_argument("--deadline", type=int, default=DEFAULT_DEADLINE_S,
                   help="Hard wall-clock deadline per reviewer, seconds")
    p.add_argument("--heartbeat-interval", type=int, default=DEFAULT_HEARTBEAT_S)
    p.add_argument("--codex-plugin", default=str(CODEX_PLUGIN_DEFAULT))
    p.add_argument("--wait", action="store_true",
                   help="Block until review finishes; default is background+poll")
    p.add_argument("--poll", default=None,
                   help="Poll an existing job_id and print its JSON status")

    args = p.parse_args()
    if args.poll:
        return cmd_poll(args)
    return cmd_run(args)


if __name__ == "__main__":
    sys.exit(main())
