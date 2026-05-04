---
tags: [k2bi-feature, infra, review-runner, codex, adversarial-review]
date: 2026-05-04
type: k2bi-feature
status: building
origin: k2b-architect
priority: high
effort: S
impact: high
mvp: "Given a Codex companion process that emits 'Codex error: Reconnecting... N/M' with N==M and then goes silent, the runner detects the stall within reconnect-stall-threshold-s seconds (default 45), logs RECONNECT_STALL_DETECTED, SIGTERMs the child, and triggers the existing Kimi fallback. The 5 binary gates are: (1) synthetic flake -- mock companion emits 'Reconnecting... 5/5' then sleeps; runner logs RECONNECT_STALL_DETECTED and exits with rc=126 within (threshold + 2 * heartbeat) seconds; (2) reconnect-then-recover -- mock companion emits 'Reconnecting... 5/5', waits below threshold, emits a non-reconnect '[codex] ...' line, runner does NOT log RECONNECT_STALL_DETECTED and does not SIGTERM; (2b) reconnect-recover-then-long-pause -- mock companion emits 'Reconnecting... 5/5', recovers with a real codex line, then sleeps longer than the threshold; detector must stay disarmed (false-positive guard); (3) no-reconnect baseline -- mock companion never emits a reconnect line, runner does NOT arm the detector regardless of stale time (existing HEARTBEAT_STALE / WEDGE_SUSPECTED still apply unchanged); (4) threshold-zero kill-switch -- passing --reconnect-stall-threshold-s 0 disables the detector and the runner falls through to HARD_DEADLINE as before."
proposed-by: "Phase 3.8a invest-coach build session diagnostic 2026-05-04T07:22Z"
accepted-by: "Keith / K2B architect handoff 2026-05-04"
priority-reason: "Cold-start Codex flake observed in production blocks K2Bi build sessions for 300+ seconds (full HARD_DEADLINE) when the existing Kimi fallback could trigger in ~65s. UX cliff during adversarial review is a top-3 friction source for Phase 3.8a kimi-handoff workflow per architect handoff."
depends-on: []
up: "[[index]]"
---

# Feature: review-runner reconnect-stall detector

## Goal

Close the cold-start Codex flake window in `scripts/lib/review_runner.py` by adding a targeted wedge-detector that fires on the specific failure pattern observed in production: Codex CLI completes its WebSocket reconnect cycle (typically `Reconnecting... 5/5`) and then goes silent indefinitely, leaving the runner staring at a dead connection until `HARD_DEADLINE` fires at the full `--deadline` (300-360s).

## Problem

Cold-start Codex flake on 2026-05-04 (job `2026-05-04T07-22-22Z_fad916`) wedged for the full 300s deadline. The same Codex CLI invoked via the Anthropic Agent path (`subagent_type=codex:codex-rescue`) succeeded in 176s. Pattern recognition across `.code-reviews/` archive: only two K2Bi runs ever showed the `Reconnecting...` pattern in their logs (both 2026-05-04); one resumed normally in ~20s after `5/5`, one never resumed. The K2B-side mirror has not exhibited the pattern in any logged run.

The runner already has a 2-tier fallback (Codex companion -> Kimi-backed reviewer). The fallback fires automatically on Codex non-zero exit, EISDIR pre-flight, or `HARD_DEADLINE`. The gap: between `Reconnecting... 5/5` going silent and `HARD_DEADLINE` firing, there is no signal that Codex is wedged. Real users abandon the run manually well before the fallback triggers, defeating the purpose of having a fallback.

## Non-goals

* Bumping the codex CLI version. Out-of-scope until the changelog confirms a fix; the current pin (`codex-cli 0.128.0`) is what was in production when the flake was observed.
* Rewriting the WebSocket transport or adding a third-tier "fresh subprocess" fallback. Both worth scoping later if the targeted detector proves insufficient; deferred to a separate feature note.
* Adding a new `--skip-codex` escape hatch. The runner already has `--skip-codex <reason>` with a documented contract; no new override flags. Per L-2026-04-30-001 anti-pattern (override-as-default).
* Changing the existing `HEARTBEAT_STALE` (30s) or `WEDGE_SUSPECTED` (120s) thresholds. The new detector adds a parallel signal scoped to post-reconnect-exhaustion only.

## Design

### Detection rule

In `reader_thread` (the thread relaying child stdout into the unified log), classify each `[codex] ...` line:
* If the line matches `Reconnecting\.\.\.\s+(\d+)\s*/\s*(\d+)` with `N == M` (cap exhausted, sane bounds), set `saw_final = True` and remember the timestamp.
* If the line is a reconnect line but `N != M` (intermediate attempt or malformed), set `saw_final = False`.
* If the line is anything else (real codex output), set `saw_final = False`. **This is the false-positive guard**: once codex recovers and emits real output, a later legitimate inference pause must not trip the detector.
* Every line continues to refresh `last_activity[0]` as today.

In `watchdog_thread`, add one new check before the existing `HEARTBEAT_STALE` / `WEDGE_SUSPECTED` branches:

```
if saw_final_reconnect AND reconnect_stall_s > 0 AND stale >= reconnect_stall_s:
    log RECONNECT_STALL_DETECTED ...
    SIGTERM the process group (existing kill path)
    set state.phase = "reconnect_stall_detected"
    return
```

The `stale` variable is already computed (`now - last_activity[0]`). Because every codex line refreshes `last_activity[0]`, a recovered codex run (`Reconnecting... 5/5` followed by `Assistant message captured`) will reset `stale` to ~0 and never trip the detector.

### Threshold

Default: `reconnect_stall_threshold_s = 45`. Rationale: the only observed success-after-`5/5` sample showed a ~20-second quiet gap between the final reconnect line and the first real codex output. 45s is roughly 2x that gap, giving real recoveries headroom while still catching wedges in well under HARD_DEADLINE. Configurable via `--reconnect-stall-threshold-s`. Setting 0 disables the detector and preserves prior runner behavior.

### Fallback wiring

When the detector fires, the existing watchdog SIGTERM path is reused (`os.killpg(os.getpgid(proc.pid), SIGTERM)` with the 10-second SIGKILL grace). `proc.wait()` returns a non-zero rc, `run_fallback_chain` records the attempt as `result: "reconnect_stalled"`, and the secondary reviewer (Kimi-backed) is invoked normally. No change to fallback contract, exit codes, or `state.json` schema beyond a new optional `phase: "reconnect_stall_detected"` value.

### Observability

* Log line: `[<utc>] RECONNECT_STALL_DETECTED elapsed=<E>s stale=<S>s threshold=<T>s; SIGTERM`
* `state.json` carries `phase: "reconnect_stall_detected"` while the SIGTERM grace runs.
* `reviewer_attempts[<idx>].result = "reconnect_stalled"` (new value alongside existing `ok` / `timed_out` / `error` / `unavailable`). Distinguishes this failure mode from generic deadline timeout for future post-mortem analysis.

## Binary MVP gates

Each gate is a unit test in `tests/test_review_runner_reconnect_stall.py` driving `run_one_reviewer` against a synthetic bash child. Tests pass on first run = MVP done.

1. **Synthetic flake.** Mock child emits `[codex] Codex error: Reconnecting... 5/5\n` then `sleep 60`. Threshold = 3s. Assert: log contains `RECONNECT_STALL_DETECTED`, `rc == 126` (distinguished from rc=124 deadline), total wall-clock < threshold + 2 * heartbeat + small kill-grace overhead.
2. **Reconnect-then-recover.** Mock child emits `Reconnecting... 5/5`, waits 1s, emits `[codex] Assistant message captured`, exits 0. Threshold = 5s. Assert: log does NOT contain `RECONNECT_STALL_DETECTED`.
2b. **Reconnect-recover-then-long-pause** (false-positive guard). Mock child emits `Reconnecting... 5/5`, recovers with a real codex line at ~1s, then sleeps 4s (longer than the 2s threshold). Assert: log does NOT contain `RECONNECT_STALL_DETECTED`. This gate exists because without resetting `saw_final` on real codex output, any legitimate post-recovery inference pause longer than the threshold would false-fire the detector.
3. **No-reconnect baseline.** Mock child emits normal `[codex] Starting` lines without any reconnect string, runs to completion. Assert: log does NOT contain `RECONNECT_STALL_DETECTED` and existing watchdog signals fire as before.
4. **Threshold-zero kill-switch.** Mock child same as gate 1. Threshold = 0. Assert: log does NOT contain `RECONNECT_STALL_DETECTED`; child runs until parent kills via the existing HARD_DEADLINE path.

All five gates must pass before this feature note can transition `status: building -> shipped`.

## Accepted trade-offs (architect-overruled review concerns)

These were surfaced during adversarial review and judged out-of-scope or low-impact for this feature. Documented here so a future reviewer does not re-litigate.

* **Reader-thread stale-undershoot under heavy scheduling jitter.** The watchdog reads `last_activity[0]` which is updated by the reader thread when a line is processed. If the reader is briefly delayed (e.g. the kernel pipe buffer holds a line for milliseconds before `for line in proc.stdout:` advances), `stale` may report a value microseconds smaller than wall-clock truth. At our 45-second default threshold this is irrelevant; the detector is heuristic and does not need millisecond accuracy. Stress-test infrastructure to surface scheduler-dependent races is out-of-scope.
* **Phase popping between fallback attempts** (`state.pop('phase', None)` in `run_one_reviewer`). The forensic record of "first attempt was stall-killed" lives in `reviewer_attempts[0].result = "reconnect_stalled"`, not in the transient `state.phase` field. Popping `phase` is correct because that field describes the *current* attempt's watchdog state, not history.
* **Verdict-marker quality gate vs. reconnect-stall rc.** If Codex emits `Reconnecting... 5/5` then exits 0 cleanly without producing a verdict marker (e.g. the WebSocket closed and the companion chose to exit rather than hang), the existing `effective_rc=125` quality gate fires, not the new `rc=126` stall path. This is correct behavior: both paths trigger fallback, with different forensic labels. The detector targets the *hang* case; clean-exit-after-exhaustion is already handled by the quality gate.

## Out-of-scope follow-ups (queued, not scoped here)

* Subprocess-fallback as Tier 3 (`Codex-companion -> Codex-fresh-subprocess -> Kimi`): would address the case where `Reconnecting... 5/5` actually reflects a stale broker that a fresh `node codex-companion.mjs` would re-init cleanly. Spec separately if reconnect-stall recurs after this fix lands.
* Backport K2B-side runner improvements (`process_group=0` for Python 3.12+, defensive `MINIMAX_API_KEY` injection): hygiene drift identified during this diagnostic; separate follow-up so this feature stays single-purpose.
* Codex CLI version bump audit: out-of-scope per "do not bump without changelog" architect rule.

## Adversarial review path

Per architect rule and chicken-and-egg avoidance: this fix targets the runner's Codex-side wedge handling, so primary review by Codex via the runner is unsafe. Ship gate is **Kimi fast-pass via `scripts/minimax-review.sh --scope diff --files ...`** as the second reviewer; no Codex pass attempted on this commit.

## Reproduction context

* Failed job: `.code-reviews/2026-05-04T07-22-22Z_fad916.log` (300s deadline, frozen at elapsed=55s).
* Successful contemporaneous job: `.code-reviews/2026-05-04T07-08-49Z_8038b5.log` (360s deadline, completed in 110.7s after the same `5/5` reconnect cycle).
* Architect handoff: 2026-05-04 K2B build-session paste captured under L-2026-04-30-001 sibling.
