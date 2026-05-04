"""Tests for review_runner's post-reconnect stall detector.

Covers the four binary MVP gates declared in
wiki/concepts/feature_review-runner-reconnect-stall-detect.md.

Each test drives `run_one_reviewer` against a synthetic bash child
that emits a controlled stdout sequence, then asserts on the unified
log content and exit code.

Notes:
* `review_runner.KILL_GRACE_S` is patched down from 10s to 1s so the
  SIGTERM->SIGKILL grace doesn't dominate test wall-clock.
* These tests do not depend on the real Codex CLI or codex-companion.mjs;
  the fake child is a bash one-liner.
"""

from __future__ import annotations

import json
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.lib import review_runner as rr  # noqa: E402


def _read_log(p: Path) -> str:
    return p.read_text(encoding="utf-8", errors="replace")


def _read_state(p: Path) -> dict:
    return json.loads(p.read_text())


class ReconnectStallDetectorTests(unittest.TestCase):
    """Each gate matches a numbered MVP gate in the feature note."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.log_path = self.tmp / "review.log"
        self.state_path = self.tmp / "state.json"
        # Pre-create the log file the way cmd_run does, so run_one_reviewer
        # can append to it.
        self.log_path.write_text("[test] JOB_START\n")
        self.state = {
            "job_id": "test_job",
            "log_path": str(self.log_path),
            "state_path": str(self.state_path),
        }
        rr.write_state(self.state_path, self.state)

    def _run(self, bash_script: str, *, deadline_s: int = 30,
             heartbeat_s: int = 1, reconnect_stall_s: int = 3,
             kill_grace_s: int = 1) -> tuple[int, float]:
        cmd = ["bash", "-c", bash_script]
        with patch.object(rr, "KILL_GRACE_S", kill_grace_s):
            t0 = time.time()
            rc = rr.run_one_reviewer(
                "codex", cmd, "test_job", self.log_path, self.state_path,
                self.state, deadline_s=deadline_s, heartbeat_s=heartbeat_s,
                reconnect_stall_s=reconnect_stall_s,
            )
            return rc, time.time() - t0

    # Gate 1: synthetic flake -- mock companion emits "Reconnecting... 5/5"
    # then sleeps; runner logs RECONNECT_STALL_DETECTED and exits non-zero
    # within (threshold + 2 * heartbeat) seconds.
    def test_gate1_synthetic_flake_triggers_stall_detector(self):
        rc, elapsed = self._run(
            'echo "[codex] Codex error: Reconnecting... 5/5"; sleep 60',
            reconnect_stall_s=3, heartbeat_s=1, deadline_s=60,
        )
        log = _read_log(self.log_path)
        state = _read_state(self.state_path)
        self.assertIn("RECONNECT_STALL_DETECTED", log,
                      f"missing stall marker; log:\n{log}")
        self.assertNotEqual(rc, 0,
                            "runner returned 0 despite stall detection")
        self.assertEqual(rc, 126,
                         f"expected reconnect-stall rc=126, got {rc}")
        self.assertEqual(state.get("phase"), "reconnect_stall_detected")
        # threshold(3) + heartbeat(1) + kill_grace(1) + a couple seconds
        # of overhead -- give it a generous 15s ceiling.
        self.assertLess(elapsed, 15,
                        f"stall path took too long ({elapsed:.1f}s)")

    # Gate 2: reconnect-then-recover -- mock companion emits 5/5, waits below
    # threshold, emits a non-reconnect [codex] line; runner does NOT log
    # RECONNECT_STALL_DETECTED.
    def test_gate2_reconnect_then_recover_does_not_stall(self):
        rc, _elapsed = self._run(
            (
                'echo "[codex] Codex error: Reconnecting... 5/5"; '
                'sleep 1; '
                'echo "[codex] Assistant message captured: ok"; '
                'sleep 0.5'
            ),
            reconnect_stall_s=5, heartbeat_s=1, deadline_s=20,
        )
        log = _read_log(self.log_path)
        self.assertNotIn("RECONNECT_STALL_DETECTED", log,
                         f"stall fired despite recovery; log:\n{log}")
        # Real codex line refreshed last_activity[0]; child exited cleanly.
        # rc may be effective_rc=125 (verdict-marker quality gate) since the
        # synthetic stdout doesn't carry a verdict marker. That's expected
        # -- we only care that the stall detector did not fire.
        self.assertNotEqual(rc, 126)

    # Gate 2b (added after Kimi pass 1 found false-positive risk): reconnect,
    # recover with real codex output, THEN a legitimate long inference pause
    # exceeding the stall threshold. Detector must stay disarmed -- the real
    # codex line resets `saw_final` so the post-recovery silence cannot trip
    # the detector. Without this guard the detector would false-positive on
    # any post-recovery inference gap longer than the threshold.
    def test_gate2b_reconnect_recover_then_long_pause_does_not_stall(self):
        # threshold=2s, recovery line at ~1s, then 4s silent pause (> threshold).
        # If saw_final were not reset on recovery, the detector would fire.
        rc, _elapsed = self._run(
            (
                'echo "[codex] Codex error: Reconnecting... 5/5"; '
                'sleep 1; '
                'echo "[codex] Assistant message captured: working"; '
                'sleep 4; '
                'echo "[codex] Done"'
            ),
            reconnect_stall_s=2, heartbeat_s=1, deadline_s=15,
        )
        log = _read_log(self.log_path)
        self.assertNotIn(
            "RECONNECT_STALL_DETECTED", log,
            f"stall false-fired after recovery + legitimate pause; log:\n{log}")
        self.assertNotEqual(rc, 126,
                            "expected non-stall rc since detector disarmed")

    # Gate 3: no-reconnect baseline -- mock companion emits normal output
    # without reconnect strings; detector remains disarmed regardless of
    # stale time. Existing HEARTBEAT_STALE / WEDGE_SUSPECTED still apply.
    def test_gate3_no_reconnect_keeps_detector_disarmed(self):
        # Even with very aggressive threshold (1s), a child that never
        # emits "Reconnecting..." should not trigger the stall detector.
        rc, _elapsed = self._run(
            'echo "[codex] Starting"; sleep 5; echo "[codex] Done"',
            reconnect_stall_s=1, heartbeat_s=1, deadline_s=20,
        )
        log = _read_log(self.log_path)
        self.assertNotIn("RECONNECT_STALL_DETECTED", log)
        # Existing watchdog signal should still appear (5s of sleep > 30s? no).
        # We don't require HEARTBEAT_STALE since the silent gap is below 30s;
        # just confirm the detector did not fire.
        self.assertNotEqual(rc, 126)

    # Gate 4: threshold-zero kill-switch -- passing 0 disables the detector.
    # Child stalls forever; the existing HARD_DEADLINE path catches it.
    def test_gate4_threshold_zero_disables_detector(self):
        rc, elapsed = self._run(
            'echo "[codex] Codex error: Reconnecting... 5/5"; sleep 60',
            reconnect_stall_s=0, heartbeat_s=1, deadline_s=4,
            kill_grace_s=1,
        )
        log = _read_log(self.log_path)
        self.assertNotIn("RECONNECT_STALL_DETECTED", log,
                         "stall detector fired despite threshold=0")
        self.assertIn("HARD_DEADLINE", log,
                      "expected HARD_DEADLINE to fire as fallback path")
        self.assertNotEqual(rc, 0)
        self.assertNotEqual(rc, 126)
        # Should exit at ~deadline + kill_grace + small overhead.
        self.assertLess(elapsed, 12)


class CrossAttemptIsolationTests(unittest.TestCase):
    """Pass-2 hardening: ensure stall flag does not leak across attempts."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.log_path = self.tmp / "review.log"
        self.state_path = self.tmp / "state.json"
        self.log_path.write_text("[test] JOB_START\n")
        self.state = {
            "job_id": "test_job",
            "log_path": str(self.log_path),
            "state_path": str(self.state_path),
        }
        rr.write_state(self.state_path, self.state)

    def _run(self, bash_script: str, *, deadline_s: int = 30,
             heartbeat_s: int = 1, reconnect_stall_s: int = 3,
             kill_grace_s: int = 1) -> int:
        cmd = ["bash", "-c", bash_script]
        with patch.object(rr, "KILL_GRACE_S", kill_grace_s):
            return rr.run_one_reviewer(
                "codex", cmd, "test_job", self.log_path, self.state_path,
                self.state, deadline_s=deadline_s, heartbeat_s=heartbeat_s,
                reconnect_stall_s=reconnect_stall_s,
            )

    def test_stall_flag_does_not_corrupt_next_attempt_rc(self):
        # First attempt: stall fires (rc=126).
        rc1 = self._run(
            'echo "[codex] Codex error: Reconnecting... 5/5"; sleep 60',
            reconnect_stall_s=2, heartbeat_s=1, deadline_s=30,
        )
        self.assertEqual(rc1, 126)
        self.assertTrue(self.state.get("killed_by_reconnect_stall"))

        # Second attempt with the SAME state dict: child times out via
        # HARD_DEADLINE, no reconnect line at all. Without the per-attempt
        # flag reset, the stale flag would corrupt rc into 126.
        rc2 = self._run(
            'sleep 60',  # never emits anything; deadline kills it
            reconnect_stall_s=0,  # detector disabled for this attempt
            heartbeat_s=1, deadline_s=2, kill_grace_s=1,
        )
        self.assertEqual(rc2, 124,
                         f"deadline timeout misclassified as stall: rc={rc2}")
        # And the flag should not be set after this clean-deadline attempt.
        self.assertFalse(self.state.get("killed_by_reconnect_stall"))

    def test_clean_exit_immediately_after_5_5_does_not_sigterm(self):
        # Codex emits "5/5" then exits 0 immediately. Before the watchdog
        # can observe stale >= threshold, proc.poll() returns the exit code
        # and the watchdog returns without firing SIGTERM. rc should be 0
        # (or 125 from the verdict-marker quality gate, which is unrelated).
        # The point: rc must NOT be 126.
        rc = self._run(
            (
                'echo "[codex] Codex error: Reconnecting... 5/5"; '
                'echo "Review output captured"; '  # sat verdict-marker
                'exit 0'
            ),
            reconnect_stall_s=3, heartbeat_s=1, deadline_s=15,
        )
        log = _read_log(self.log_path)
        self.assertNotIn("RECONNECT_STALL_DETECTED", log,
                         f"stall fired on clean exit; log:\n{log}")
        self.assertNotEqual(rc, 126)
        self.assertFalse(self.state.get("killed_by_reconnect_stall"))


class ReconnectRegexParserTests(unittest.TestCase):
    """Direct tests on the reconnect-line regex used by reader_thread."""

    def test_matches_codex_companion_format(self):
        line = "[codex] Codex error: Reconnecting... 5/5"
        m = rr._RECONNECT_RE.search(line)
        self.assertIsNotNone(m)
        self.assertEqual(int(m.group(1)), 5)
        self.assertEqual(int(m.group(2)), 5)

    def test_matches_intermediate_attempt(self):
        line = "[codex] Codex error: Reconnecting... 2/5"
        m = rr._RECONNECT_RE.search(line)
        self.assertIsNotNone(m)
        self.assertEqual(int(m.group(1)), 2)
        self.assertEqual(int(m.group(2)), 5)

    def test_does_not_match_unrelated_lines(self):
        for line in (
            "[codex] Assistant message captured",
            "Reconnecting... soon",  # missing N/M
            "5/5 Reconnecting",      # different prefix order
            "[codex] retrying 5 of 5",
        ):
            self.assertIsNone(rr._RECONNECT_RE.search(line),
                              f"false-positive on: {line!r}")


if __name__ == "__main__":
    unittest.main()
