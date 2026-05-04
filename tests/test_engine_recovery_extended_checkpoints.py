"""Tests for the Q42 +1 week carry-forward fix.

The +1 week persistence check on 2026-05-03 surfaced that a multi-day
engine-off gap empties the 48h `journal_tail` and re-flags both
previously-adopted positions (`phantom_position`) and previously-
adopted orphan STOPs (`phantom_open_order`) as mismatches. This test
file exercises the carry-forward fix:

1. Pure-function tests (T1-T6) feed older `engine_recovered` and
   `orphan_stop_adopted` records DIRECTLY into `recovery.reconcile()`
   to verify recovery's existing replay logic correctly carries the
   adoptions forward when those events are made available. This is
   the core invariant -- the smallest safe fix only widens the lookup
   window; it does NOT change recovery semantics, so recovery's
   handling of the older records IS what the fix relies on.

2. Helper tests (T7-T10) drive `_read_extended_checkpoints` against
   real on-disk journal day-files (via `JournalWriter`) to verify the
   lookup window expansion is correct: only the two checkpoint event
   types are picked up, only within the extended bound, only outside
   the narrow tail.

T4 explicitly preserves the Q31/Q32 invariant: with neither extended
checkpoints nor narrow events, a genuine fresh broker state still
refuses to start.
"""

from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

from execution.connectors.types import (
    BrokerOpenOrder,
    BrokerPosition,
)
from execution.engine import recovery
from execution.engine.main import _read_extended_checkpoints
from execution.journal.writer import JournalWriter


NOW = datetime(2026, 5, 10, 12, 0, tzinfo=timezone.utc)


def _engine_recovered_record(
    *,
    ts: datetime,
    adopted_positions: list[dict],
    expected_stop_children: list[dict] | None = None,
) -> dict:
    """Build a synthetic engine_recovered journal record."""
    return {
        "ts": ts.isoformat(),
        "schema_version": 2,
        "event_type": "engine_recovered",
        "trade_id": None,
        "journal_entry_id": "J-recovered",
        "strategy": None,
        "git_sha": "abc",
        "payload": {
            "status": "mismatch_override",
            "reconciled_event_count": 1,
            "adopted_positions": adopted_positions,
            "expected_stop_children": expected_stop_children or [],
        },
    }


def _orphan_stop_adopted_record(
    *,
    ts: datetime,
    perm_id: int,
    ticker: str = "SPY",
    qty: int = 2,
    stop_price: str = "697.13",
    justification: str = "Phase-3.6-Day-1-Portal-submitted",
) -> dict:
    """Build a synthetic orphan_stop_adopted journal record."""
    return {
        "ts": ts.isoformat(),
        "schema_version": 2,
        "event_type": "orphan_stop_adopted",
        "trade_id": None,
        "journal_entry_id": "J-adopted",
        "strategy": None,
        "git_sha": "abc",
        "payload": {
            "permId": perm_id,
            "ticker": ticker,
            "qty": qty,
            "stop_price": stop_price,
            "source": "operator-portal",
            "adopted_at": ts.isoformat(),
            "justification": justification,
        },
        "ticker": ticker,
        "broker_order_id": "",
        "broker_perm_id": str(perm_id),
    }


class CarryForwardReplayTests(unittest.TestCase):
    """T1-T6: Pure-function tests proving recovery's existing replay
    correctly handles older checkpoint records once they are present
    in journal_tail. The carry-forward fix only widens the lookup
    window; these tests verify the downstream contract."""

    def test_T1_engine_recovered_carries_position_after_long_gap(self):
        """T1: Empty narrow tail + engine_recovered from 7 days ago.

        Broker holds the position the architect previously adopted.
        recovery._positions_from_journal must seed implied_positions
        from the older engine_recovered's adopted_positions, and
        Phase B must NOT flag phantom_position.
        """
        seven_days_ago = NOW - timedelta(days=7)
        journal_tail = [
            _engine_recovered_record(
                ts=seven_days_ago,
                adopted_positions=[
                    {"ticker": "SPY", "qty": 2, "avg_price": "707.72"}
                ],
            ),
        ]
        result = recovery.reconcile(
            journal_tail=journal_tail,
            broker_positions=[
                BrokerPosition(
                    ticker="SPY", qty=2, avg_price=Decimal("707.72")
                )
            ],
            broker_open_orders=[],
            broker_order_status=[],
            now=NOW,
        )
        self.assertEqual(
            result.status,
            recovery.RecoveryStatus.CATCH_UP,
            f"expected CATCH_UP, got {result.status} with mismatches "
            f"{result.mismatch_reasons}",
        )
        for m in result.mismatch_reasons:
            self.assertNotEqual(m.get("case"), "phantom_position")

    def test_T2_orphan_stop_adopted_carries_after_long_gap(self):
        """T2: Empty narrow tail + orphan_stop_adopted from 7 days ago.

        Broker holds the orphan STOP the architect previously adopted.
        recovery._adopted_orphan_perm_ids must extract the permId from
        the older event, and Phase B.1 must NOT flag phantom_open_order
        for that order.
        """
        seven_days_ago = NOW - timedelta(days=7)
        journal_tail = [
            # Engine_recovered to satisfy position adoption -- without
            # it the SPY broker position would itself flag phantom.
            # We're testing the orphan STOP path; position carry is T1.
            _engine_recovered_record(
                ts=seven_days_ago,
                adopted_positions=[
                    {"ticker": "SPY", "qty": 2, "avg_price": "707.72"}
                ],
            ),
            _orphan_stop_adopted_record(
                ts=seven_days_ago + timedelta(hours=1),
                perm_id=1888063981,
            ),
        ]
        result = recovery.reconcile(
            journal_tail=journal_tail,
            broker_positions=[
                BrokerPosition(
                    ticker="SPY", qty=2, avg_price=Decimal("707.72")
                )
            ],
            broker_open_orders=[
                BrokerOpenOrder(
                    broker_order_id="61226127",
                    broker_perm_id="1888063981",
                    ticker="SPY",
                    side="sell",
                    qty=2,
                    filled_qty=0,
                    limit_price=Decimal("0"),
                    status="PreSubmitted",
                    tif="GTC",
                    aux_price=Decimal("697.13"),
                    order_type="STP",
                )
            ],
            broker_order_status=[],
            now=NOW,
        )
        for m in result.mismatch_reasons:
            self.assertNotEqual(m.get("case"), "phantom_open_order")

    def test_T3_both_adoptions_carry_forward_clean_start(self):
        """T3: Both events from days 7+6 -> engine starts clean.

        This is the production scenario the +1 week persistence check
        exposed. After the fix, both adoptions should be visible to
        the replay and engine should reach CATCH_UP.
        """
        seven_days_ago = NOW - timedelta(days=7)
        six_days_ago = NOW - timedelta(days=6)
        journal_tail = [
            _engine_recovered_record(
                ts=seven_days_ago,
                adopted_positions=[
                    {"ticker": "SPY", "qty": 2, "avg_price": "707.72"}
                ],
            ),
            _orphan_stop_adopted_record(
                ts=six_days_ago,
                perm_id=1888063981,
            ),
        ]
        result = recovery.reconcile(
            journal_tail=journal_tail,
            broker_positions=[
                BrokerPosition(
                    ticker="SPY", qty=2, avg_price=Decimal("707.72")
                )
            ],
            broker_open_orders=[
                BrokerOpenOrder(
                    broker_order_id="61226127",
                    broker_perm_id="1888063981",
                    ticker="SPY",
                    side="sell",
                    qty=2,
                    filled_qty=0,
                    limit_price=Decimal("0"),
                    status="PreSubmitted",
                    tif="GTC",
                    aux_price=Decimal("697.13"),
                    order_type="STP",
                )
            ],
            broker_order_status=[],
            now=NOW,
        )
        self.assertEqual(
            result.status,
            recovery.RecoveryStatus.CATCH_UP,
            f"expected CATCH_UP, got {result.status} with mismatches "
            f"{result.mismatch_reasons}",
        )
        self.assertEqual(result.mismatch_reasons, [])

    def test_T4_no_checkpoints_still_refuses_start(self):
        """T4: Empty journal_tail (genuinely fresh) + broker holds
        unknown position -> MISMATCH_REFUSED.

        Preserves the Q31/Q32 invariant: the carry-forward fix only
        works when checkpoint events exist in the extended window. A
        truly fresh state with no prior adoption MUST still refuse to
        start so the architect explicitly engages
        K2BI_ALLOW_RECOVERY_MISMATCH=1 and documents the override.
        """
        result = recovery.reconcile(
            journal_tail=[],
            broker_positions=[
                BrokerPosition(
                    ticker="SPY", qty=2, avg_price=Decimal("707.72")
                )
            ],
            broker_open_orders=[],
            broker_order_status=[],
            now=NOW,
        )
        self.assertEqual(
            result.status,
            recovery.RecoveryStatus.MISMATCH_REFUSED,
        )
        cases = [m.get("case") for m in result.mismatch_reasons]
        self.assertIn("phantom_position", cases)

    def test_T5_multiple_engine_recovered_latest_wins(self):
        """T5: Two engine_recovered events with different positions.

        Snapshot-reset semantics in _positions_from_journal mean the
        per_ticker map is wiped on each engine_recovered. The most
        recent event in the replay sequence determines the seed.
        """
        ten_days_ago = NOW - timedelta(days=10)
        five_days_ago = NOW - timedelta(days=5)
        journal_tail = [
            # Older record claims SPY 5 @ 700 -- if this leaked
            # through, the broker's SPY 2 @ 707.72 would diff.
            _engine_recovered_record(
                ts=ten_days_ago,
                adopted_positions=[
                    {"ticker": "SPY", "qty": 5, "avg_price": "700.00"}
                ],
            ),
            # Newer record correctly claims SPY 2 @ 707.72.
            _engine_recovered_record(
                ts=five_days_ago,
                adopted_positions=[
                    {"ticker": "SPY", "qty": 2, "avg_price": "707.72"}
                ],
            ),
        ]
        result = recovery.reconcile(
            journal_tail=journal_tail,
            broker_positions=[
                BrokerPosition(
                    ticker="SPY", qty=2, avg_price=Decimal("707.72")
                )
            ],
            broker_open_orders=[],
            broker_order_status=[],
            now=NOW,
        )
        self.assertEqual(
            result.status,
            recovery.RecoveryStatus.CATCH_UP,
            f"latest engine_recovered must override; got "
            f"{result.status} with mismatches {result.mismatch_reasons}",
        )

    def test_T13_stale_orphan_with_closed_position_fires_phantom(self):
        """T13: orphan_stop_adopted from extended window, but the
        broker no longer holds any position in that ticker -- the
        live STOP order is now genuinely orphaned and phantom_open_order
        MUST fire.

        Capital-path safety gate per cross-mode adversarial review
        (Kimi + Codex 2026-05-03): the original Q42 unconditionally
        suppressed phantom detection for any adopted permId. With the
        carry-forward fix extending the lookup to 30 days, a stale
        adoption whose underlying position has since been closed
        could mask a dangling broker-side STOP. The position-aware
        gate at the recognition step keeps this safety property even
        when extended-window adoptions are present.
        """
        seven_days_ago = NOW - timedelta(days=7)
        journal_tail = [
            _orphan_stop_adopted_record(
                ts=seven_days_ago,
                perm_id=1888063981,
                ticker="SPY",
            ),
        ]
        result = recovery.reconcile(
            journal_tail=journal_tail,
            # Position closed -- broker holds NO SPY.
            broker_positions=[],
            # ...but the broker-side STOP for the OLD position is
            # still live (a real-world bracket-child that wasn't
            # cancelled when the parent fully closed externally).
            broker_open_orders=[
                BrokerOpenOrder(
                    broker_order_id="61226127",
                    broker_perm_id="1888063981",
                    ticker="SPY",
                    side="sell",
                    qty=2,
                    filled_qty=0,
                    limit_price=Decimal("0"),
                    status="PreSubmitted",
                    tif="GTC",
                    aux_price=Decimal("697.13"),
                    order_type="STP",
                )
            ],
            broker_order_status=[],
            now=NOW,
        )
        self.assertEqual(
            result.status,
            recovery.RecoveryStatus.MISMATCH_REFUSED,
            f"stale orphan adoption with closed position must NOT "
            f"suppress phantom_open_order; got {result.status} "
            f"with mismatches {result.mismatch_reasons}",
        )
        cases = [m.get("case") for m in result.mismatch_reasons]
        self.assertIn("phantom_open_order", cases)

    def test_T14_orphan_with_held_position_carries_forward(self):
        """T14: Companion to T13 -- when the position IS still held,
        the orphan adoption gates open and phantom_open_order is
        correctly suppressed.

        Together with T13 this proves the position-aware gate gates
        on the right axis: position-still-held passes; position-
        closed fails closed.
        """
        seven_days_ago = NOW - timedelta(days=7)
        journal_tail = [
            _engine_recovered_record(
                ts=seven_days_ago,
                adopted_positions=[
                    {"ticker": "SPY", "qty": 2, "avg_price": "707.72"}
                ],
            ),
            _orphan_stop_adopted_record(
                ts=seven_days_ago + timedelta(hours=1),
                perm_id=1888063981,
                ticker="SPY",
            ),
        ]
        result = recovery.reconcile(
            journal_tail=journal_tail,
            broker_positions=[
                BrokerPosition(
                    ticker="SPY", qty=2, avg_price=Decimal("707.72")
                )
            ],
            broker_open_orders=[
                BrokerOpenOrder(
                    broker_order_id="61226127",
                    broker_perm_id="1888063981",
                    ticker="SPY",
                    side="sell",
                    qty=2,
                    filled_qty=0,
                    limit_price=Decimal("0"),
                    status="PreSubmitted",
                    tif="GTC",
                    aux_price=Decimal("697.13"),
                    order_type="STP",
                )
            ],
            broker_order_status=[],
            now=NOW,
        )
        self.assertEqual(
            result.status,
            recovery.RecoveryStatus.CATCH_UP,
            f"position-held + orphan-adoption must reach CATCH_UP; "
            f"got {result.status} with mismatches {result.mismatch_reasons}",
        )
        for m in result.mismatch_reasons:
            self.assertNotEqual(m.get("case"), "phantom_open_order")

    def test_T6_phantom_when_broker_diverges_from_checkpoint(self):
        """T6: Older engine_recovered claims SPY 2, broker shows SPY 5.

        The carry-forward seeds implied_positions from the checkpoint,
        but Phase B still runs the position-vs-broker diff. A genuine
        divergence (broker holds different qty than the checkpoint
        records) must surface as a mismatch -- the fix must NOT
        suppress real divergence detection.
        """
        seven_days_ago = NOW - timedelta(days=7)
        journal_tail = [
            _engine_recovered_record(
                ts=seven_days_ago,
                adopted_positions=[
                    {"ticker": "SPY", "qty": 2, "avg_price": "707.72"}
                ],
            ),
        ]
        result = recovery.reconcile(
            journal_tail=journal_tail,
            broker_positions=[
                BrokerPosition(
                    ticker="SPY", qty=5, avg_price=Decimal("707.72")
                )
            ],
            broker_open_orders=[],
            broker_order_status=[],
            now=NOW,
        )
        self.assertEqual(
            result.status,
            recovery.RecoveryStatus.MISMATCH_REFUSED,
            "broker qty divergence from checkpoint must surface mismatch",
        )


class ReadExtendedCheckpointsTests(unittest.TestCase):
    """T7-T10: Helper tests for `_read_extended_checkpoints` against
    real on-disk journal day-files."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmpdir = Path(self._tmp.name)
        self.journal = JournalWriter(base_dir=self.tmpdir, git_sha="abc")
        # Fixed "now" for deterministic day-offset arithmetic.
        self.now = datetime(2026, 5, 10, 12, 0, tzinfo=timezone.utc)

    def tearDown(self):
        self._tmp.cleanup()

    def _write(self, *, ts: datetime, event_type: str, payload: dict):
        self.journal.append(event_type, payload, ts=ts)

    def test_T7_helper_returns_only_checkpoint_event_types(self):
        """T7: Helper must filter to engine_recovered + orphan_stop_adopted.

        Other event types written in the same window MUST NOT appear
        in the returned list.
        """
        five_days_ago = self.now - timedelta(days=5)
        self._write(
            ts=five_days_ago,
            event_type="engine_recovered",
            payload={
                "status": "catch_up",
                "reconciled_event_count": 0,
                "adopted_positions": [
                    {"ticker": "SPY", "qty": 2, "avg_price": "707.72"}
                ],
                "expected_stop_children": [],
            },
        )
        self._write(
            ts=five_days_ago + timedelta(minutes=1),
            event_type="orphan_stop_adopted",
            payload={
                "permId": 1888063981,
                "ticker": "SPY",
                "qty": 2,
                "stop_price": "697.13",
                "source": "operator-portal",
                "adopted_at": (
                    five_days_ago + timedelta(minutes=1)
                ).isoformat(),
                "justification": "test",
            },
        )
        # Noise events that must NOT appear in extended checkpoints.
        self._write(
            ts=five_days_ago + timedelta(minutes=2),
            event_type="engine_started",
            payload={
                "pid": 999,
                "tick_seconds": 30.0,
                "recovery_status": "catch_up",
                "reconciled_event_count": 0,
                "mismatch_count": 0,
                "strategies_loaded": [],
                "strategies": [],
                "resumed_awaiting": None,
                "validator_config_hash": "hash",
                "kill_file_present_at_startup": False,
                "retired_dir": str(self.tmpdir),
            },
        )
        self._write(
            ts=five_days_ago + timedelta(minutes=3),
            event_type="engine_stopped",
            payload={
                "reason": "test",
                "terminal_state": "halted",
            },
        )

        out = _read_extended_checkpoints(
            self.journal,
            ext_since=self.now - timedelta(days=30),
            narrow_since=self.now - timedelta(days=2),
            now=self.now,
        )
        types = [r["event_type"] for r in out]
        self.assertEqual(
            sorted(types),
            ["engine_recovered", "orphan_stop_adopted"],
            f"unexpected event types in helper output: {types}",
        )

    def test_T8_helper_excludes_events_within_narrow_window(self):
        """T8: An event whose ts is within [narrow_since, now] must be
        excluded -- it's covered by `_read_recent_journal` and would
        double-count if returned here."""
        one_day_ago = self.now - timedelta(days=1)
        self._write(
            ts=one_day_ago,
            event_type="engine_recovered",
            payload={
                "status": "catch_up",
                "reconciled_event_count": 0,
                "adopted_positions": [],
                "expected_stop_children": [],
            },
        )
        out = _read_extended_checkpoints(
            self.journal,
            ext_since=self.now - timedelta(days=30),
            narrow_since=self.now - timedelta(days=2),
            now=self.now,
        )
        self.assertEqual(out, [])

    def test_T9_helper_excludes_events_older_than_extended_window(self):
        """T9: An event whose ts is older than `ext_since` must be
        excluded -- the 30-day bound is an explicit policy choice."""
        thirty_one_days_ago = self.now - timedelta(days=31)
        self._write(
            ts=thirty_one_days_ago,
            event_type="engine_recovered",
            payload={
                "status": "catch_up",
                "reconciled_event_count": 0,
                "adopted_positions": [
                    {"ticker": "SPY", "qty": 2, "avg_price": "707.72"}
                ],
                "expected_stop_children": [],
            },
        )
        out = _read_extended_checkpoints(
            self.journal,
            ext_since=self.now - timedelta(days=30),
            narrow_since=self.now - timedelta(days=2),
            now=self.now,
        )
        self.assertEqual(out, [])

    def test_T10_helper_returns_events_sorted_oldest_first(self):
        """T10: Records returned must be sorted by ts ascending so the
        recovery replay's snapshot-reset semantics apply correctly
        (most recent engine_recovered wins)."""
        # Write out-of-order: newer first, then older.
        five_days_ago = self.now - timedelta(days=5)
        ten_days_ago = self.now - timedelta(days=10)
        self._write(
            ts=five_days_ago,
            event_type="engine_recovered",
            payload={
                "status": "catch_up",
                "reconciled_event_count": 0,
                "adopted_positions": [
                    {"ticker": "SPY", "qty": 2, "avg_price": "707.72"}
                ],
                "expected_stop_children": [],
            },
        )
        self._write(
            ts=ten_days_ago,
            event_type="engine_recovered",
            payload={
                "status": "catch_up",
                "reconciled_event_count": 0,
                "adopted_positions": [
                    {"ticker": "SPY", "qty": 5, "avg_price": "700.00"}
                ],
                "expected_stop_children": [],
            },
        )
        out = _read_extended_checkpoints(
            self.journal,
            ext_since=self.now - timedelta(days=30),
            narrow_since=self.now - timedelta(days=2),
            now=self.now,
        )
        self.assertEqual(len(out), 2)
        self.assertLess(out[0]["ts"], out[1]["ts"])

    def test_T11_helper_handles_missing_day_files(self):
        """T11: Days with no journal file must not raise -- helper
        treats them as "no checkpoint that day" and continues."""
        # Only write one day file in the extended window; other days
        # have no file at all (gap, mirroring the 2026-04-28..05-02
        # production gap).
        five_days_ago = self.now - timedelta(days=5)
        self._write(
            ts=five_days_ago,
            event_type="engine_recovered",
            payload={
                "status": "catch_up",
                "reconciled_event_count": 0,
                "adopted_positions": [
                    {"ticker": "SPY", "qty": 2, "avg_price": "707.72"}
                ],
                "expected_stop_children": [],
            },
        )
        out = _read_extended_checkpoints(
            self.journal,
            ext_since=self.now - timedelta(days=30),
            narrow_since=self.now - timedelta(days=2),
            now=self.now,
        )
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["event_type"], "engine_recovered")

    def test_T12_helper_returns_empty_when_window_inverted(self):
        """T12: Defensive check -- ext_since >= narrow_since should
        return an empty list, not crash."""
        out = _read_extended_checkpoints(
            self.journal,
            ext_since=self.now - timedelta(days=2),
            narrow_since=self.now - timedelta(days=2),
            now=self.now,
        )
        self.assertEqual(out, [])

        out = _read_extended_checkpoints(
            self.journal,
            ext_since=self.now - timedelta(days=1),
            narrow_since=self.now - timedelta(days=2),
            now=self.now,
        )
        self.assertEqual(out, [])


if __name__ == "__main__":
    unittest.main()
