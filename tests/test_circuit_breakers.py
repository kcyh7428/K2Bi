"""Unit tests for m2.4 circuit breakers + .killed lock file.

Verification per milestones.md#2.4:
    "Simulated drawdown triggers each breaker correctly; `.killed`
     written at -10%; engine refuses orders while file exists"
"""

from __future__ import annotations

import json
import tempfile
import unittest
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

from execution.risk import circuit_breakers as cb
from execution.risk import kill_switch as ks
from execution.risk.types import AccountState


def _state(
    current: str,
    day_open: str = "1000000",
    peak: str = "1000000",
    week_history=None,
) -> AccountState:
    return AccountState(
        current_value=Decimal(current),
        day_open_value=Decimal(day_open),
        peak_value=Decimal(peak),
        week_history=week_history or [],
    )


class BreakerTriggerTests(unittest.TestCase):
    def test_daily_soft_triggers_at_minus_2_pct(self):
        state = _state(current="980000")  # -2% intraday, 0% total
        results = cb.evaluate(state)
        soft = next(r for r in results if r.breaker == "daily_soft_stop")
        hard = next(r for r in results if r.breaker == "daily_hard_stop")
        self.assertTrue(soft.tripped)
        self.assertFalse(hard.tripped)
        self.assertEqual(soft.action, "halve_positions")

    def test_daily_hard_triggers_at_minus_3_pct(self):
        state = _state(current="970000")
        results = cb.evaluate(state)
        hard = next(r for r in results if r.breaker == "daily_hard_stop")
        self.assertTrue(hard.tripped)
        self.assertEqual(hard.action, "flatten_all")

    def test_weekly_triggers_at_minus_5_pct_rolling(self):
        week_start = Decimal("1000000")
        hist = [(date(2026, 4, 14) + timedelta(days=i), week_start) for i in range(5)]
        state = _state(current="950000", day_open="950000", peak="1000000", week_history=hist)
        results = cb.evaluate(state)
        weekly = next(r for r in results if r.breaker == "weekly_cap")
        self.assertTrue(weekly.tripped)
        self.assertEqual(weekly.action, "reduce_budget")

    def test_weekly_does_not_trip_before_full_5_session_window(self):
        # Codex round 6 P2: only 2 sessions of history -- the breaker
        # must NOT fire even if current is far below the first point.
        # Previously this would have read as -6% and tripped.
        hist = [
            (date(2026, 4, 14), Decimal("1000000")),
            (date(2026, 4, 15), Decimal("1000000")),
        ]
        state = _state(current="940000", day_open="940000", peak="1000000", week_history=hist)
        results = cb.evaluate(state)
        weekly = next(r for r in results if r.breaker == "weekly_cap")
        self.assertFalse(weekly.tripped)

    def test_weekly_engages_exactly_at_5_sessions(self):
        hist = [
            (date(2026, 4, 14) + timedelta(days=i), Decimal("1000000"))
            for i in range(5)
        ]
        state = _state(current="940000", day_open="940000", peak="1000000", week_history=hist)
        results = cb.evaluate(state)
        weekly = next(r for r in results if r.breaker == "weekly_cap")
        self.assertTrue(weekly.tripped)

    def test_total_drawdown_triggers_at_minus_10_pct(self):
        # -10% from peak (day_open shifted so intraday isn't also -10%)
        state = _state(current="900000", day_open="905000", peak="1000000")
        results = cb.evaluate(state)
        kill = next(r for r in results if r.breaker == "total_drawdown_kill")
        self.assertTrue(kill.tripped)
        self.assertEqual(kill.action, "write_killed")

    def test_no_breaker_trips_on_clean_state(self):
        state = _state(current="1005000", day_open="1000000", peak="1005000")
        results = cb.evaluate(state)
        self.assertFalse(any(r.tripped for r in results))


class KillSwitchTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / ".killed"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_write_and_detect_killed(self):
        self.assertFalse(ks.is_killed(self.path))
        ks.write_killed(
            reason="total_drawdown_kill",
            source="circuit_breaker",
            detail={"total_drawdown_pct": "-0.10"},
            path=self.path,
        )
        self.assertTrue(ks.is_killed(self.path))

        record = ks.read_kill_record(self.path)
        self.assertIsNotNone(record)
        self.assertEqual(record["reason"], "total_drawdown_kill")
        self.assertEqual(record["source"], "circuit_breaker")

    def test_assert_not_killed_raises_when_file_present(self):
        ks.write_killed(reason="manual", source="telegram", path=self.path)
        with self.assertRaises(ks.KillSwitchActiveError) as cm:
            ks.assert_not_killed(self.path)
        self.assertEqual(cm.exception.record["reason"], "manual")

    def test_no_delete_api_exposed(self):
        # Hard-rule surface: the module must not export a kill-clearing
        # function. Only a human filesystem operation can unlock.
        forbidden = {"delete_killed", "clear_kill", "unlock"}
        exported = set(dir(ks))
        self.assertTrue(forbidden.isdisjoint(exported), f"forbidden export found: {forbidden & exported}")

    def test_first_writer_wins_is_immutable(self):
        # Codex round 5 P2: once .killed is written, its contents are
        # frozen until a human deletes the file. A second write (e.g.
        # Telegram kill landing right after the breaker kill) must NOT
        # overwrite the record.
        first = ks.write_killed(reason="first", source="circuit_breaker", path=self.path)
        second = ks.write_killed(reason="second", source="telegram", path=self.path)
        self.assertIsNotNone(first)
        self.assertIsNone(second, "second write should have returned None (first-writer-wins)")
        record = ks.read_kill_record(self.path)
        self.assertEqual(record["reason"], "first")
        self.assertEqual(record["source"], "circuit_breaker")

    def test_write_killed_fsyncs_parent_directory(self):
        # Codex round 3 P2: the rename's durability depends on fsyncing
        # the parent directory -- without it a host crash can lose the
        # renamed entry and the bot would resume after reboot. We can't
        # easily observe fsync, but we can confirm the parent-directory
        # open is non-fatal and leaves no leaked temp file.
        import os as _os
        ks.write_killed(reason="total_drawdown_kill", source="circuit_breaker", path=self.path)
        self.assertTrue(self.path.exists())
        # No leftover temp files in the parent dir.
        leftovers = [
            p for p in self.path.parent.iterdir()
            if p.name.startswith(".killed.tmp.")
        ]
        self.assertEqual(leftovers, [])


class BreakerKillIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / ".killed"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_total_drawdown_writes_killed_and_blocks_orders(self):
        state = _state(current="900000", day_open="905000", peak="1000000")
        results = cb.evaluate(state)
        tripped = cb.apply_kill_on_trip(results, kill_path=self.path)
        self.assertIsNotNone(tripped)
        self.assertEqual(tripped.breaker, "total_drawdown_kill")
        self.assertTrue(self.path.exists())

        # Engine boundary: assert_not_killed must raise
        with self.assertRaises(ks.KillSwitchActiveError):
            ks.assert_not_killed(self.path)

    def test_soft_trip_does_not_write_killed(self):
        state = _state(current="980000")
        results = cb.evaluate(state)
        tripped = cb.apply_kill_on_trip(results, kill_path=self.path)
        self.assertIsNone(tripped)
        self.assertFalse(self.path.exists())

    def test_apply_kill_is_idempotent_across_ticks(self):
        # Codex round 4 P2: engine evaluates breakers every tick. If the
        # kill breaker stays tripped, we must not rewrite .killed (and
        # retrigger journal/Telegram logic) every tick -- the original
        # kill record must be preserved.
        state = _state(current="900000", day_open="905000", peak="1000000")
        first = cb.apply_kill_on_trip(cb.evaluate(state), kill_path=self.path)
        self.assertIsNotNone(first)
        original = self.path.read_bytes()

        # Simulate N more ticks with the same tripped state. Each should
        # return None and NOT overwrite the file.
        for _ in range(3):
            again = cb.apply_kill_on_trip(cb.evaluate(state), kill_path=self.path)
            self.assertIsNone(again)
        self.assertEqual(self.path.read_bytes(), original)

    def test_apply_kill_skips_even_on_unrelated_breaker_when_already_killed(self):
        # Manual kill already wrote .killed; a later breaker evaluation
        # with total-drawdown tripped must not overwrite it.
        ks.write_killed(reason="manual", source="telegram", path=self.path)
        original = self.path.read_bytes()
        state = _state(current="900000", day_open="905000", peak="1000000")
        result = cb.apply_kill_on_trip(cb.evaluate(state), kill_path=self.path)
        self.assertIsNone(result)
        self.assertEqual(self.path.read_bytes(), original)

    def test_atomic_first_writer_wins_under_race(self):
        # Codex round 5 P2: simulate the breaker + Telegram kill firing
        # in two processes concurrently. Both observe "not killed" at
        # t0; one links, the other EEXISTs. The file's content must
        # match exactly one writer.
        import threading
        results = []
        barrier = threading.Barrier(5)

        def _kill(source: str) -> None:
            barrier.wait()
            out = ks.write_killed(reason=source, source=source, path=self.path)
            results.append((source, out is not None))

        threads = [threading.Thread(target=_kill, args=(f"src-{i}",)) for i in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        winners = [s for s, ok in results if ok]
        self.assertEqual(len(winners), 1, f"exactly one winner; got {results}")
        rec = ks.read_kill_record(self.path)
        self.assertEqual(rec["source"], winners[0])


if __name__ == "__main__":
    unittest.main()
