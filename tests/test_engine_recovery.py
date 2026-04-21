"""Tests for execution.engine.recovery.

Covers the architect-specified Q3-refined catch-up + discrepancy
matrix: six catch-up cases (all should reconcile cleanly) + four
discrepancy cases (all should refuse to start unless
K2BI_ALLOW_RECOVERY_MISMATCH=1).
"""

from __future__ import annotations

import unittest
from datetime import datetime, timezone
from decimal import Decimal

from execution.connectors.types import (
    BrokerOpenOrder,
    BrokerOrderStatusEvent,
    BrokerPosition,
)
from execution.engine import recovery


NOW = datetime(2026, 5, 5, 12, 0, tzinfo=timezone.utc)
EARLIER = datetime(2026, 5, 5, 11, 30, tzinfo=timezone.utc)


def _journal_pending(
    *,
    trade_id: str = "T1",
    strategy: str = "spy-rotational",
    broker_order_id: str = "1000",
    broker_perm_id: str = "2000000",
    ticker: str = "SPY",
    side: str = "buy",
    qty: int = 10,
    limit_price: str = "500",
) -> list[dict]:
    return [
        {
            "ts": EARLIER.isoformat(),
            "event_type": "order_submitted",
            "trade_id": trade_id,
            "journal_entry_id": "J1",
            "strategy": strategy,
            "git_sha": "abc",
            "broker_order_id": broker_order_id,
            "broker_perm_id": broker_perm_id,
            "ticker": ticker,
            "side": side,
            "qty": qty,
            "payload": {
                "ticker": ticker,
                "side": side,
                "qty": qty,
                "limit_price": limit_price,
                "submitted_at": EARLIER.isoformat(),
            },
        }
    ]


class CatchUpTests(unittest.TestCase):
    def test_empty_state_is_clean(self):
        result = recovery.reconcile(
            journal_tail=[],
            broker_positions=[],
            broker_open_orders=[],
            broker_order_status=[],
            now=NOW,
        )
        self.assertEqual(result.status, recovery.RecoveryStatus.CLEAN)
        self.assertEqual(result.events, [])
        self.assertEqual(result.mismatch_reasons, [])

    def test_journal_pending_ibkr_filled(self):
        tail = _journal_pending()
        status = BrokerOrderStatusEvent(
            broker_order_id="1000",
            broker_perm_id="2000000",
            status="Filled",
            filled_qty=10,
            remaining_qty=0,
            avg_fill_price=Decimal("500.01"),
            last_update_at=EARLIER,
        )
        result = recovery.reconcile(
            journal_tail=tail,
            broker_positions=[BrokerPosition(ticker="SPY", qty=10, avg_price=Decimal("500.01"))],
            broker_open_orders=[],
            broker_order_status=[status],
            now=NOW,
        )
        self.assertEqual(result.status, recovery.RecoveryStatus.CATCH_UP)
        cases = [e.payload.get("case") for e in result.events]
        self.assertIn("pending_filled", cases)

    def test_journal_pending_ibkr_cancelled(self):
        tail = _journal_pending()
        status = BrokerOrderStatusEvent(
            broker_order_id="1000",
            broker_perm_id="2000000",
            status="Cancelled",
            filled_qty=0,
            remaining_qty=10,
            avg_fill_price=None,
            last_update_at=EARLIER,
        )
        result = recovery.reconcile(
            journal_tail=tail,
            broker_positions=[],
            broker_open_orders=[],
            broker_order_status=[status],
            now=NOW,
        )
        self.assertEqual(result.status, recovery.RecoveryStatus.CATCH_UP)
        cases = [e.payload.get("case") for e in result.events]
        self.assertIn("pending_cancelled", cases)

    def test_journal_pending_ibkr_partial_fill(self):
        tail = _journal_pending()
        status = BrokerOrderStatusEvent(
            broker_order_id="1000",
            broker_perm_id="2000000",
            status="Filled",
            filled_qty=7,
            remaining_qty=3,
            avg_fill_price=Decimal("500"),
            last_update_at=EARLIER,
        )
        result = recovery.reconcile(
            journal_tail=tail,
            broker_positions=[BrokerPosition(ticker="SPY", qty=7, avg_price=Decimal("500"))],
            broker_open_orders=[],
            broker_order_status=[status],
            now=NOW,
        )
        self.assertEqual(result.status, recovery.RecoveryStatus.CATCH_UP)
        cases = [e.payload.get("case") for e in result.events]
        self.assertIn("pending_partially_filled", cases)

    def test_journal_pending_ibkr_still_open(self):
        tail = _journal_pending()
        open_order = BrokerOpenOrder(
            broker_order_id="1000",
            broker_perm_id="2000000",
            ticker="SPY",
            side="buy",
            qty=10,
            filled_qty=0,
            limit_price=Decimal("500"),
            status="Submitted",
        )
        result = recovery.reconcile(
            journal_tail=tail,
            broker_positions=[],
            broker_open_orders=[open_order],
            broker_order_status=[],
            now=NOW,
        )
        self.assertEqual(result.status, recovery.RecoveryStatus.CATCH_UP)
        cases = [e.payload.get("case") for e in result.events]
        self.assertIn("pending_still_open", cases)

    def test_journal_pending_ibkr_rejected(self):
        tail = _journal_pending()
        status = BrokerOrderStatusEvent(
            broker_order_id="1000",
            broker_perm_id="2000000",
            status="Rejected",
            filled_qty=0,
            remaining_qty=10,
            avg_fill_price=None,
            last_update_at=EARLIER,
            reason="out of hours",
        )
        result = recovery.reconcile(
            journal_tail=tail,
            broker_positions=[],
            broker_open_orders=[],
            broker_order_status=[status],
            now=NOW,
        )
        self.assertEqual(result.status, recovery.RecoveryStatus.CATCH_UP)
        cases = [e.payload.get("case") for e in result.events]
        self.assertIn("pending_rejected", cases)
        rejection_event = next(
            e for e in result.events if e.payload.get("case") == "pending_rejected"
        )
        self.assertEqual(rejection_event.payload["broker_reason"], "out of hours")

    def test_avg_price_drift_on_qty_match(self):
        # Journal says we hold 10 SPY at $500; broker says 10 at $502.
        tail = [
            {
                "ts": EARLIER.isoformat(),
                "event_type": "order_filled",
                "trade_id": "T1",
                "journal_entry_id": "J1",
                "strategy": "spy-rotational",
                "git_sha": "abc",
                "ticker": "SPY",
                "side": "buy",
                "qty": 10,
                "payload": {"ticker": "SPY", "side": "buy", "qty": 10, "fill_price": "500"},
            }
        ]
        result = recovery.reconcile(
            journal_tail=tail,
            broker_positions=[BrokerPosition(ticker="SPY", qty=10, avg_price=Decimal("502"))],
            broker_open_orders=[],
            broker_order_status=[],
            now=NOW,
        )
        self.assertEqual(result.status, recovery.RecoveryStatus.CATCH_UP)
        drift_events = [e for e in result.events if e.event_type == "avg_price_drift"]
        self.assertEqual(len(drift_events), 1)
        self.assertEqual(drift_events[0].payload["ticker"], "SPY")
        self.assertEqual(drift_events[0].payload["journal_avg_price"], "500")
        self.assertEqual(drift_events[0].payload["broker_avg_price"], "502")


class DiscrepancyTests(unittest.TestCase):
    def test_phantom_position_refuses(self):
        # IBKR shows NVDA that journal never mentioned.
        result = recovery.reconcile(
            journal_tail=[],
            broker_positions=[
                BrokerPosition(ticker="NVDA", qty=5, avg_price=Decimal("600"))
            ],
            broker_open_orders=[],
            broker_order_status=[],
            now=NOW,
            override_env="",
        )
        self.assertEqual(result.status, recovery.RecoveryStatus.MISMATCH_REFUSED)
        cases = [m["case"] for m in result.mismatch_reasons]
        self.assertIn("phantom_position", cases)

    def test_oversized_position_refuses(self):
        tail = [
            {
                "ts": EARLIER.isoformat(),
                "event_type": "order_filled",
                "trade_id": "T1",
                "journal_entry_id": "J1",
                "strategy": "spy-rotational",
                "git_sha": "abc",
                "ticker": "SPY",
                "side": "buy",
                "qty": 5,
                "payload": {"ticker": "SPY", "side": "buy", "qty": 5, "fill_price": "500"},
            }
        ]
        result = recovery.reconcile(
            journal_tail=tail,
            broker_positions=[BrokerPosition(ticker="SPY", qty=20, avg_price=Decimal("500"))],
            broker_open_orders=[],
            broker_order_status=[],
            now=NOW,
            override_env="",
        )
        self.assertEqual(result.status, recovery.RecoveryStatus.MISMATCH_REFUSED)
        cases = [m["case"] for m in result.mismatch_reasons]
        self.assertIn("position_oversized_vs_journal", cases)

    def test_missing_position_refuses(self):
        tail = [
            {
                "ts": EARLIER.isoformat(),
                "event_type": "order_filled",
                "trade_id": "T1",
                "journal_entry_id": "J1",
                "strategy": "spy-rotational",
                "git_sha": "abc",
                "ticker": "SPY",
                "side": "buy",
                "qty": 10,
                "payload": {"ticker": "SPY", "side": "buy", "qty": 10, "fill_price": "500"},
            }
        ]
        result = recovery.reconcile(
            journal_tail=tail,
            broker_positions=[],  # IBKR no longer holds SPY
            broker_open_orders=[],
            broker_order_status=[],
            now=NOW,
            override_env="",
        )
        self.assertEqual(result.status, recovery.RecoveryStatus.MISMATCH_REFUSED)
        cases = [m["case"] for m in result.mismatch_reasons]
        self.assertIn("journal_position_missing_at_broker", cases)

    def test_phantom_open_order_refuses(self):
        phantom = BrokerOpenOrder(
            broker_order_id="9999",
            broker_perm_id="7777777",
            ticker="SPY",
            side="buy",
            qty=5,
            filled_qty=0,
            limit_price=Decimal("495"),
            status="Submitted",
        )
        result = recovery.reconcile(
            journal_tail=[],  # journal never proposed this order
            broker_positions=[],
            broker_open_orders=[phantom],
            broker_order_status=[],
            now=NOW,
            override_env="",
        )
        self.assertEqual(result.status, recovery.RecoveryStatus.MISMATCH_REFUSED)
        cases = [m["case"] for m in result.mismatch_reasons]
        self.assertIn("phantom_open_order", cases)

    def test_override_env_bypasses_refusal(self):
        result = recovery.reconcile(
            journal_tail=[],
            broker_positions=[
                BrokerPosition(ticker="NVDA", qty=5, avg_price=Decimal("600"))
            ],
            broker_open_orders=[],
            broker_order_status=[],
            now=NOW,
            override_env="1",
        )
        self.assertEqual(result.status, recovery.RecoveryStatus.MISMATCH_OVERRIDE)
        mismatch_events = [
            e for e in result.events if e.event_type == "recovery_state_mismatch"
        ]
        self.assertEqual(len(mismatch_events), 1)
        self.assertEqual(
            mismatch_events[0].payload["resolution"], "proceeding_with_override"
        )

    def test_mismatch_event_records_configured_env_name(self):
        # Codex round-7 P3: when a caller passes a custom env name, the
        # mismatch event must record that name in payload.override_env
        # so operators see the right remediation instruction.
        result = recovery.reconcile(
            journal_tail=[],
            broker_positions=[
                BrokerPosition(ticker="NVDA", qty=5, avg_price=Decimal("600"))
            ],
            broker_open_orders=[],
            broker_order_status=[],
            now=NOW,
            override_env="",
            override_env_name="K2BI_PAPER_ALLOW_MISMATCH",
        )
        self.assertEqual(result.status, recovery.RecoveryStatus.MISMATCH_REFUSED)
        mismatch_events = [
            e for e in result.events if e.event_type == "recovery_state_mismatch"
        ]
        self.assertEqual(len(mismatch_events), 1)
        self.assertEqual(
            mismatch_events[0].payload["override_env"],
            "K2BI_PAPER_ALLOW_MISMATCH",
        )

    def test_refused_status_still_emits_mismatch_event(self):
        result = recovery.reconcile(
            journal_tail=[],
            broker_positions=[
                BrokerPosition(ticker="NVDA", qty=5, avg_price=Decimal("600"))
            ],
            broker_open_orders=[],
            broker_order_status=[],
            now=NOW,
            override_env="",
        )
        mismatch_events = [
            e for e in result.events if e.event_type == "recovery_state_mismatch"
        ]
        self.assertEqual(len(mismatch_events), 1)
        self.assertEqual(
            mismatch_events[0].payload["resolution"], "engine_refuses_start"
        )


class PartialThenCancelledTests(unittest.TestCase):
    """Codex round-2 P1: a Cancelled / Rejected terminal with
    filled_qty > 0 must be treated as a partial fill so the filled
    shares show up in reconciliation_deltas."""

    def test_partial_then_cancelled_includes_filled_in_delta(self):
        tail = _journal_pending(qty=10)
        status = BrokerOrderStatusEvent(
            broker_order_id="1000",
            broker_perm_id="2000000",
            status="Cancelled",
            filled_qty=4,
            remaining_qty=6,
            avg_fill_price=Decimal("500"),
            last_update_at=EARLIER,
        )
        result = recovery.reconcile(
            journal_tail=tail,
            broker_positions=[BrokerPosition(ticker="SPY", qty=4, avg_price=Decimal("500"))],
            broker_open_orders=[],
            broker_order_status=[status],
            now=NOW,
        )
        self.assertEqual(result.status, recovery.RecoveryStatus.CATCH_UP)
        cases = [e.payload.get("case") for e in result.events]
        # Classified as partial even though status is Cancelled.
        self.assertIn("pending_partially_filled", cases)
        # No phantom against the 4 filled shares.
        phantoms = [
            m for m in result.mismatch_reasons if m["case"] == "phantom_position"
        ]
        self.assertEqual(phantoms, [])

    def test_rejected_with_zero_fill_stays_rejected(self):
        tail = _journal_pending()
        status = BrokerOrderStatusEvent(
            broker_order_id="1000",
            broker_perm_id="2000000",
            status="Rejected",
            filled_qty=0,
            remaining_qty=10,
            avg_fill_price=None,
            last_update_at=EARLIER,
            reason="fat finger",
        )
        result = recovery.reconcile(
            journal_tail=tail,
            broker_positions=[],
            broker_open_orders=[],
            broker_order_status=[status],
            now=NOW,
        )
        cases = [e.payload.get("case") for e in result.events]
        self.assertIn("pending_rejected", cases)


class PendingKeyConsistencyTests(unittest.TestCase):
    """Codex round-2 P2: order_proposed keys on trade_id (no perm
    yet); downstream submitted/filled events must land on the SAME
    key so terminal cleanup clears the proposal entry."""

    def test_completed_trade_does_not_leave_phantom_proposal(self):
        # Full lifecycle in the tail: proposed -> submitted -> filled.
        tail = [
            {
                "ts": "2026-05-05T10:00:00+00:00",
                "event_type": "order_proposed",
                "trade_id": "T1",
                "journal_entry_id": "J1",
                "strategy": "spy-rotational",
                "git_sha": "abc",
                "ticker": "SPY",
                "side": "buy",
                "qty": 10,
                "payload": {
                    "ticker": "SPY",
                    "side": "buy",
                    "qty": 10,
                    "limit_price": "500",
                    "submitted_at": "2026-05-05T10:00:00+00:00",
                },
            },
            {
                "ts": "2026-05-05T10:00:01+00:00",
                "event_type": "order_submitted",
                "trade_id": "T1",
                "journal_entry_id": "J2",
                "strategy": "spy-rotational",
                "git_sha": "abc",
                "ticker": "SPY",
                "side": "buy",
                "qty": 10,
                "broker_order_id": "1000",
                "broker_perm_id": "2000000",
                "payload": {
                    "status": "Submitted",
                    "limit_price": "500",
                    "submitted_at": "2026-05-05T10:00:01+00:00",
                },
            },
            {
                "ts": "2026-05-05T10:00:05+00:00",
                "event_type": "order_filled",
                "trade_id": "T1",
                "journal_entry_id": "J3",
                "strategy": "spy-rotational",
                "git_sha": "abc",
                "ticker": "SPY",
                "side": "buy",
                "qty": 10,
                "broker_order_id": "1000",
                "broker_perm_id": "2000000",
                "payload": {
                    "ticker": "SPY",
                    "side": "buy",
                    "qty": 10,
                    "fill_price": "500",
                },
            },
        ]
        result = recovery.reconcile(
            journal_tail=tail,
            broker_positions=[
                BrokerPosition(ticker="SPY", qty=10, avg_price=Decimal("500"))
            ],
            broker_open_orders=[],
            broker_order_status=[],
            now=NOW,
        )
        # Completed trade must NOT leave a phantom pending entry.
        phantom_pendings = [
            e for e in result.events
            if e.payload.get("case") == "pending_no_broker_counterpart"
        ]
        self.assertEqual(phantom_pendings, [])
        self.assertEqual(result.status, recovery.RecoveryStatus.CATCH_UP)


class CorruptJournalFieldsTolerantTests(unittest.TestCase):
    """R16-minimax: corrupt Decimal / qty fields in journal payload
    must not crash the engine during reconcile. Graceful degradation:
    log + None/0 fallback so recovery can still classify by broker ID."""

    def test_corrupt_stop_loss_does_not_raise(self):
        tail = [
            {
                "ts": EARLIER.isoformat(),
                "event_type": "order_submitted",
                "trade_id": "T1",
                "journal_entry_id": "J1",
                "strategy": "spy-rotational",
                "git_sha": "abc",
                "ticker": "SPY",
                "side": "buy",
                "qty": 10,
                "broker_order_id": "1000",
                "broker_perm_id": "2000000",
                "payload": {
                    "status": "Submitted",
                    "limit_price": "500",
                    "stop_loss": "not-a-number",
                    "submitted_at": EARLIER.isoformat(),
                    "ticker": "SPY",
                    "side": "buy",
                    "qty": 10,
                },
            }
        ]
        open_order = BrokerOpenOrder(
            broker_order_id="1000",
            broker_perm_id="2000000",
            ticker="SPY",
            side="buy",
            qty=10,
            filled_qty=0,
            limit_price=Decimal("500"),
            status="Submitted",
            tif="DAY",
        )
        # Must not raise InvalidOperation.
        result = recovery.reconcile(
            journal_tail=tail,
            broker_positions=[],
            broker_open_orders=[open_order],
            broker_order_status=[],
            now=NOW,
        )
        still_open = [
            e for e in result.events
            if e.payload.get("case") == "pending_still_open"
        ]
        self.assertEqual(len(still_open), 1)
        # stop_loss degrades to None on corrupt input.
        self.assertIsNone(still_open[0].payload["journal_view"].get("stop_loss"))

    def test_corrupt_limit_price_does_not_raise(self):
        tail = [
            {
                "ts": EARLIER.isoformat(),
                "event_type": "order_submitted",
                "trade_id": "T1",
                "journal_entry_id": "J1",
                "strategy": "spy-rotational",
                "git_sha": "abc",
                "ticker": "SPY",
                "side": "buy",
                "qty": 10,
                "broker_order_id": "1000",
                "broker_perm_id": "2000000",
                "payload": {
                    "status": "Submitted",
                    "limit_price": "garbage",
                    "submitted_at": EARLIER.isoformat(),
                    "ticker": "SPY",
                    "side": "buy",
                    "qty": 10,
                },
            }
        ]
        result = recovery.reconcile(
            journal_tail=tail,
            broker_positions=[],
            broker_open_orders=[],
            broker_order_status=[],
            now=NOW,
        )
        # Q39-B (2026-04-21): broker_perm_id="2000000" in the journal
        # record makes this the assume-fill path, not the original
        # pending_no_broker_counterpart. The test's intent -- corrupt
        # limit_price does not raise -- stands; the case name reflects
        # the new branch's classification.
        cases = [e.payload.get("case") for e in result.events]
        self.assertIn(
            "pending_no_broker_counterpart_assumed_filled", cases
        )


class StopLossPreservedThroughRecoveryTests(unittest.TestCase):
    """R15-minimax finding: stop_loss must flow journal -> recovery ->
    AwaitingOrderState on resume. Broker's bracket child still holds
    the protective stop, but engine-internal tracking of the order
    needs the strategy-level stop reference for journaling + any
    engine-side risk re-evaluation mid-flight."""

    def test_pending_payload_includes_stop_loss(self):
        tail = [
            {
                "ts": EARLIER.isoformat(),
                "event_type": "order_submitted",
                "trade_id": "T1",
                "journal_entry_id": "J1",
                "strategy": "spy-rotational",
                "git_sha": "abc",
                "ticker": "SPY",
                "side": "buy",
                "qty": 10,
                "broker_order_id": "1000",
                "broker_perm_id": "2000000",
                "payload": {
                    "status": "Submitted",
                    "limit_price": "500",
                    "stop_loss": "495",
                    "submitted_at": EARLIER.isoformat(),
                    "ticker": "SPY",
                    "side": "buy",
                    "qty": 10,
                },
            }
        ]
        open_order = BrokerOpenOrder(
            broker_order_id="1000",
            broker_perm_id="2000000",
            ticker="SPY",
            side="buy",
            qty=10,
            filled_qty=0,
            limit_price=Decimal("500"),
            status="Submitted",
            tif="DAY",
        )
        result = recovery.reconcile(
            journal_tail=tail,
            broker_positions=[],
            broker_open_orders=[open_order],
            broker_order_status=[],
            now=NOW,
        )
        still_open_events = [
            e for e in result.events
            if e.payload.get("case") == "pending_still_open"
        ]
        self.assertEqual(len(still_open_events), 1)
        # journal_view in the event payload must carry stop_loss.
        journal_view = still_open_events[0].payload.get("journal_view", {})
        self.assertEqual(journal_view.get("stop_loss"), "495")


class KillBlockedClearsProposalTests(unittest.TestCase):
    """Codex round-9 P2: kill_blocked after order_proposed means the
    order never reached the broker. Must clear the proposal so recovery
    does not flag it as pending_no_broker_counterpart."""

    def test_kill_blocked_clears_proposal(self):
        tail = [
            {
                "ts": EARLIER.isoformat(),
                "event_type": "order_proposed",
                "trade_id": "T1",
                "journal_entry_id": "J1",
                "strategy": "spy-rotational",
                "git_sha": "abc",
                "ticker": "SPY",
                "side": "buy",
                "qty": 10,
                "payload": {
                    "ticker": "SPY",
                    "side": "buy",
                    "qty": 10,
                    "limit_price": "500",
                    "submitted_at": EARLIER.isoformat(),
                },
            },
            {
                "ts": EARLIER.isoformat(),
                "event_type": "kill_blocked",
                "trade_id": "T1",
                "journal_entry_id": "J2",
                "strategy": "spy-rotational",
                "git_sha": "abc",
                "ticker": "SPY",
                "side": "buy",
                "qty": 10,
                "payload": {
                    "reason": "kill_file_present_at_submit",
                    "ticker": "SPY",
                },
            },
        ]
        result = recovery.reconcile(
            journal_tail=tail,
            broker_positions=[],
            broker_open_orders=[],
            broker_order_status=[],
            now=NOW,
        )
        # No pending events -- kill_blocked cleared the proposal.
        pending_events = [
            e for e in result.events
            if e.event_type == "recovery_reconciled"
            and e.payload.get("case") == "pending_no_broker_counterpart"
        ]
        self.assertEqual(pending_events, [])
        self.assertEqual(result.status, recovery.RecoveryStatus.CLEAN)


class PartialFillMidstreamTests(unittest.TestCase):
    """Codex round-6 P1: a partial fill in the journal must NOT clear
    the pending entry. The order only terminates when
    cumulative_filled_qty >= order qty."""

    def test_partial_fill_leaves_pending_open(self):
        tail = [
            {
                "ts": EARLIER.isoformat(),
                "event_type": "order_submitted",
                "trade_id": "T1",
                "journal_entry_id": "J1",
                "strategy": "spy-rotational",
                "git_sha": "abc",
                "ticker": "SPY",
                "side": "buy",
                "qty": 10,
                "broker_order_id": "1000",
                "broker_perm_id": "2000000",
                "payload": {
                    "status": "Submitted",
                    "limit_price": "500",
                    "submitted_at": EARLIER.isoformat(),
                },
            },
            {
                "ts": EARLIER.isoformat(),
                "event_type": "order_filled",
                "trade_id": "T1",
                "journal_entry_id": "J2",
                "strategy": "spy-rotational",
                "git_sha": "abc",
                "ticker": "SPY",
                "side": "buy",
                "qty": 3,  # this record's fill qty
                "broker_order_id": "1000",
                "broker_perm_id": "2000000",
                "payload": {
                    "ticker": "SPY",
                    "side": "buy",
                    "fill_qty": 3,
                    "fill_price": "500",
                    "cumulative_filled_qty": 3,  # only 3 of 10 so far
                    "remaining_qty": 7,
                },
            },
        ]
        # Broker shows the order still open with 3 filled, 7 remaining.
        open_order = BrokerOpenOrder(
            broker_order_id="1000",
            broker_perm_id="2000000",
            ticker="SPY",
            side="buy",
            qty=10,
            filled_qty=3,
            limit_price=Decimal("500"),
            status="Submitted",
            tif="DAY",
        )
        result = recovery.reconcile(
            journal_tail=tail,
            broker_positions=[BrokerPosition(ticker="SPY", qty=3, avg_price=Decimal("500"))],
            broker_open_orders=[open_order],
            broker_order_status=[],
            now=NOW,
        )
        self.assertEqual(result.status, recovery.RecoveryStatus.CATCH_UP)
        # The remaining live order should be classified pending_still_open
        # (not phantom_open_order).
        cases = [e.payload.get("case") for e in result.events]
        self.assertIn("pending_still_open", cases)
        phantoms = [
            m for m in result.mismatch_reasons if m["case"] == "phantom_open_order"
        ]
        self.assertEqual(phantoms, [])

    def test_full_fill_clears_pending(self):
        tail = [
            {
                "ts": EARLIER.isoformat(),
                "event_type": "order_submitted",
                "trade_id": "T1",
                "journal_entry_id": "J1",
                "strategy": "spy-rotational",
                "git_sha": "abc",
                "ticker": "SPY",
                "side": "buy",
                "qty": 10,
                "broker_order_id": "1000",
                "broker_perm_id": "2000000",
                "payload": {
                    "status": "Submitted",
                    "limit_price": "500",
                    "submitted_at": EARLIER.isoformat(),
                },
            },
            {
                "ts": EARLIER.isoformat(),
                "event_type": "order_filled",
                "trade_id": "T1",
                "journal_entry_id": "J2",
                "strategy": "spy-rotational",
                "git_sha": "abc",
                "ticker": "SPY",
                "side": "buy",
                "qty": 10,
                "broker_order_id": "1000",
                "broker_perm_id": "2000000",
                "payload": {
                    "ticker": "SPY",
                    "side": "buy",
                    "fill_qty": 10,
                    "fill_price": "500",
                    "cumulative_filled_qty": 10,
                    "remaining_qty": 0,
                },
            },
        ]
        result = recovery.reconcile(
            journal_tail=tail,
            broker_positions=[BrokerPosition(ticker="SPY", qty=10, avg_price=Decimal("500"))],
            broker_open_orders=[],
            broker_order_status=[],
            now=NOW,
        )
        # No pending events -- the full-fill clears the entry.
        pending_events = [
            e for e in result.events
            if e.event_type == "recovery_reconciled"
        ]
        self.assertEqual(pending_events, [])
        self.assertEqual(result.status, recovery.RecoveryStatus.CATCH_UP)


class StopChildRecognitionTests(unittest.TestCase):
    """Codex round-4 P1: after a parent with stop_loss fills, the GTC
    stop child remains open at broker. Recovery must recognize it via
    client_tag rather than flag it as phantom_open_order."""

    def test_stop_child_after_parent_fill_is_not_phantom(self):
        tail = [
            {
                "ts": EARLIER.isoformat(),
                "event_type": "order_submitted",
                "trade_id": "T1",
                "journal_entry_id": "J1",
                "strategy": "spy-rotational",
                "git_sha": "abc",
                "ticker": "SPY",
                "side": "buy",
                "qty": 10,
                "broker_order_id": "1000",
                "broker_perm_id": "2000000",
                "payload": {
                    "status": "Submitted",
                    "limit_price": "500",
                    "submitted_at": EARLIER.isoformat(),
                },
            },
            {
                "ts": EARLIER.isoformat(),
                "event_type": "order_filled",
                "trade_id": "T1",
                "journal_entry_id": "J2",
                "strategy": "spy-rotational",
                "git_sha": "abc",
                "ticker": "SPY",
                "side": "buy",
                "qty": 10,
                "broker_order_id": "1000",
                "broker_perm_id": "2000000",
                "payload": {
                    "ticker": "SPY",
                    "side": "buy",
                    "qty": 10,
                    "fill_price": "500",
                },
            },
        ]
        stop_child = BrokerOpenOrder(
            broker_order_id="1001",
            broker_perm_id="2000001",
            ticker="SPY",
            side="sell",
            qty=10,
            filled_qty=0,
            limit_price=Decimal("0"),
            status="Submitted",
            tif="GTC",
            client_tag="k2bi:spy-rotational:T1:stop",
        )
        result = recovery.reconcile(
            journal_tail=tail,
            broker_positions=[
                BrokerPosition(ticker="SPY", qty=10, avg_price=Decimal("500"))
            ],
            broker_open_orders=[stop_child],
            broker_order_status=[],
            now=NOW,
        )
        self.assertEqual(result.status, recovery.RecoveryStatus.CATCH_UP)
        phantoms = [
            m for m in result.mismatch_reasons if m["case"] == "phantom_open_order"
        ]
        self.assertEqual(phantoms, [])
        recognized = [
            e for e in result.events
            if e.payload.get("case") == "stop_child_recognized"
        ]
        self.assertEqual(len(recognized), 1)
        self.assertEqual(recognized[0].payload["client_tag"], "k2bi:spy-rotational:T1:stop")


class StopChildExcludedFromTradeIdIndexTests(unittest.TestCase):
    """Codex round-14 P1: crash-window scenario where parent filled
    before restart and only the :stop child remains open at broker.
    The stop child MUST NOT be used as a fallback match for the
    parent's trade_id -- that would classify the parent as
    pending_still_open when it actually already filled.
    """

    def test_stop_child_alone_does_not_match_parent_trade_id(self):
        tail = [
            {
                "ts": EARLIER.isoformat(),
                "event_type": "order_proposed",
                "trade_id": "T1",
                "journal_entry_id": "J1",
                "strategy": "spy-rotational",
                "git_sha": "abc",
                "ticker": "SPY",
                "side": "buy",
                "qty": 10,
                "payload": {
                    "ticker": "SPY",
                    "side": "buy",
                    "qty": 10,
                    "limit_price": "500",
                    "submitted_at": EARLIER.isoformat(),
                },
            }
        ]
        # Parent already filled; broker status history confirms.
        parent_fill_status = BrokerOrderStatusEvent(
            broker_order_id="1000",
            broker_perm_id="2000000",
            status="Filled",
            filled_qty=10,
            remaining_qty=0,
            avg_fill_price=Decimal("500"),
            last_update_at=EARLIER,
            client_tag="k2bi:spy-rotational:T1",
        )
        # Only the stop child is still open.
        stop_child = BrokerOpenOrder(
            broker_order_id="1001",
            broker_perm_id="2000001",
            ticker="SPY",
            side="sell",
            qty=10,
            filled_qty=0,
            limit_price=Decimal("0"),
            status="Submitted",
            tif="GTC",
            client_tag="k2bi:spy-rotational:T1:stop",
        )
        result = recovery.reconcile(
            journal_tail=tail,
            broker_positions=[
                BrokerPosition(ticker="SPY", qty=10, avg_price=Decimal("500"))
            ],
            broker_open_orders=[stop_child],
            broker_order_status=[parent_fill_status],
            now=NOW,
        )
        self.assertEqual(result.status, recovery.RecoveryStatus.CATCH_UP)
        # Parent classified via the status (pending_filled), NOT as
        # pending_still_open on the stop child.
        cases = [e.payload.get("case") for e in result.events]
        self.assertIn("pending_filled", cases)
        self.assertNotIn("pending_still_open", cases)
        # Stop child separately recognized.
        self.assertIn("stop_child_recognized", cases)


class TradeIdStatusMatchTests(unittest.TestCase):
    """Codex round-11 P1: when a process crashes after submit_order
    succeeded (journal has only order_proposed, no broker IDs) and
    the broker terminates the order before restart, recovery must
    match the status event by trade_id via client_tag."""

    def test_terminal_status_matched_by_trade_id(self):
        tail = [
            {
                "ts": EARLIER.isoformat(),
                "event_type": "order_proposed",
                "trade_id": "T1",
                "journal_entry_id": "J1",
                "strategy": "spy-rotational",
                "git_sha": "abc",
                "ticker": "SPY",
                "side": "buy",
                "qty": 10,
                "payload": {
                    "ticker": "SPY",
                    "side": "buy",
                    "qty": 10,
                    "limit_price": "500",
                    "submitted_at": EARLIER.isoformat(),
                },
            }
        ]
        status = BrokerOrderStatusEvent(
            broker_order_id="1000",
            broker_perm_id="2000000",
            status="Filled",
            filled_qty=10,
            remaining_qty=0,
            avg_fill_price=Decimal("500"),
            last_update_at=EARLIER,
            client_tag="k2bi:spy-rotational:T1",
        )
        result = recovery.reconcile(
            journal_tail=tail,
            broker_positions=[
                BrokerPosition(ticker="SPY", qty=10, avg_price=Decimal("500"))
            ],
            broker_open_orders=[],
            broker_order_status=[status],
            now=NOW,
        )
        self.assertEqual(result.status, recovery.RecoveryStatus.CATCH_UP)
        cases = [e.payload.get("case") for e in result.events]
        self.assertIn("pending_filled", cases)
        # Must NOT be classified as pending_no_broker_counterpart.
        self.assertNotIn("pending_no_broker_counterpart", cases)


class TradeIdFallbackMatchTests(unittest.TestCase):
    """Codex round-4 P1: crash between submit_order success and
    order_submitted journal write. Journal has only order_proposed
    (no perm/order_id yet); broker has live open order with our
    client_tag. Recovery must match via trade_id fallback."""

    def test_proposed_only_journal_matches_via_client_tag(self):
        tail = [
            {
                "ts": EARLIER.isoformat(),
                "event_type": "order_proposed",
                "trade_id": "T1",
                "journal_entry_id": "J1",
                "strategy": "spy-rotational",
                "git_sha": "abc",
                "ticker": "SPY",
                "side": "buy",
                "qty": 10,
                # note: no broker_order_id / broker_perm_id yet --
                # crash happened before order_submitted wrote them
                "payload": {
                    "ticker": "SPY",
                    "side": "buy",
                    "qty": 10,
                    "limit_price": "500",
                    "submitted_at": EARLIER.isoformat(),
                },
            }
        ]
        broker_open = BrokerOpenOrder(
            broker_order_id="1000",
            broker_perm_id="2000000",
            ticker="SPY",
            side="buy",
            qty=10,
            filled_qty=0,
            limit_price=Decimal("500"),
            status="Submitted",
            tif="DAY",
            client_tag="k2bi:spy-rotational:T1",
        )
        result = recovery.reconcile(
            journal_tail=tail,
            broker_positions=[],
            broker_open_orders=[broker_open],
            broker_order_status=[],
            now=NOW,
        )
        self.assertEqual(result.status, recovery.RecoveryStatus.CATCH_UP)
        phantoms = [
            m for m in result.mismatch_reasons if m["case"] == "phantom_open_order"
        ]
        self.assertEqual(phantoms, [])
        # The order should be classified as pending_still_open (matched
        # via trade_id fallback) rather than phantom or no-counterpart.
        cases = [e.payload.get("case") for e in result.events]
        self.assertIn("pending_still_open", cases)
        no_counterpart = [e for e in result.events
                          if e.payload.get("case") == "pending_no_broker_counterpart"]
        self.assertEqual(no_counterpart, [])


class AdoptedPositionsReplayTests(unittest.TestCase):
    """Codex round-1 P2: engine_recovered.adopted_positions must seed
    journal-implied positions so a post-override restart does not
    re-flag the same broker holdings as phantoms."""

    def test_adopted_positions_seed_implied_state(self):
        # Earlier session adopted SPY via mismatch_override; the
        # recovery wrote an engine_recovered event with adopted
        # positions. Fresh restart sees only that event + broker
        # reports the same position -> CLEAN catch-up, not phantom.
        tail = [
            {
                "ts": EARLIER.isoformat(),
                "event_type": "engine_recovered",
                "trade_id": None,
                "journal_entry_id": "J1",
                "strategy": None,
                "git_sha": "abc",
                "payload": {
                    "status": "mismatch_override",
                    "adopted_positions": [
                        {"ticker": "SPY", "qty": 10, "avg_price": "500"}
                    ],
                },
            }
        ]
        result = recovery.reconcile(
            journal_tail=tail,
            broker_positions=[
                BrokerPosition(ticker="SPY", qty=10, avg_price=Decimal("500"))
            ],
            broker_open_orders=[],
            broker_order_status=[],
            now=NOW,
        )
        self.assertEqual(result.status, recovery.RecoveryStatus.CATCH_UP)
        phantoms = [
            m for m in result.mismatch_reasons if m["case"] == "phantom_position"
        ]
        self.assertEqual(phantoms, [])

    def test_adopted_positions_supersede_prior_fills(self):
        # Replay order: earlier order_filled for 5 SPY -> later
        # engine_recovered with adopted_positions 10 SPY. The
        # engine_recovered is the authoritative checkpoint and should
        # replace the accumulated state.
        tail = [
            {
                "ts": "2026-05-05T10:00:00+00:00",
                "event_type": "order_filled",
                "trade_id": "T1",
                "journal_entry_id": "J1",
                "strategy": "spy-rotational",
                "git_sha": "abc",
                "ticker": "SPY",
                "side": "buy",
                "qty": 5,
                "payload": {
                    "ticker": "SPY",
                    "side": "buy",
                    "qty": 5,
                    "fill_price": "500",
                },
            },
            {
                "ts": "2026-05-05T11:00:00+00:00",
                "event_type": "engine_recovered",
                "trade_id": None,
                "journal_entry_id": "J2",
                "strategy": None,
                "git_sha": "abc",
                "payload": {
                    "status": "mismatch_override",
                    "adopted_positions": [
                        {"ticker": "SPY", "qty": 10, "avg_price": "505"}
                    ],
                },
            },
        ]
        result = recovery.reconcile(
            journal_tail=tail,
            broker_positions=[
                BrokerPosition(ticker="SPY", qty=10, avg_price=Decimal("505"))
            ],
            broker_open_orders=[],
            broker_order_status=[],
            now=NOW,
        )
        # qty matches the adopted snapshot (10 not 5); no mismatch.
        self.assertEqual(result.status, recovery.RecoveryStatus.CATCH_UP)
        phantoms = [
            m for m in result.mismatch_reasons
            if m["case"] in {
                "phantom_position",
                "position_oversized_vs_journal",
                "position_undersized_vs_journal",
            }
        ]
        self.assertEqual(phantoms, [])


class IdentityMatchingTests(unittest.TestCase):
    def test_perm_id_preferred_over_order_id(self):
        # Journal records perm_id=2000000 + order_id=1000; broker-side
        # has a status for perm_id=2000000 but order_id reissued to
        # 2000 (simulating an IB Gateway restart). Recovery should
        # still match on perm.
        tail = _journal_pending(broker_order_id="1000", broker_perm_id="2000000")
        status = BrokerOrderStatusEvent(
            broker_order_id="2000",         # reissued
            broker_perm_id="2000000",       # stable
            status="Filled",
            filled_qty=10,
            remaining_qty=0,
            avg_fill_price=Decimal("500"),
            last_update_at=EARLIER,
        )
        result = recovery.reconcile(
            journal_tail=tail,
            broker_positions=[BrokerPosition(ticker="SPY", qty=10, avg_price=Decimal("500"))],
            broker_open_orders=[],
            broker_order_status=[status],
            now=NOW,
        )
        self.assertEqual(result.status, recovery.RecoveryStatus.CATCH_UP)
        cases = [e.payload.get("case") for e in result.events]
        self.assertIn("pending_filled", cases)

    def test_pending_no_broker_counterpart(self):
        # Q39-B (2026-04-21): broker_perm_id="" signals broker never
        # acknowledged the order -- no evidence broker accepted, keep
        # the original pending_no_broker_counterpart case. With perm_id
        # set, the assume-fill path fires instead (covered in the Q39
        # suite at tests/test_engine_recovery_q39.py).
        tail = _journal_pending(broker_perm_id="")
        result = recovery.reconcile(
            journal_tail=tail,
            broker_positions=[],
            broker_open_orders=[],
            broker_order_status=[],
            now=NOW,
        )
        self.assertEqual(result.status, recovery.RecoveryStatus.CATCH_UP)
        cases = [e.payload.get("case") for e in result.events]
        self.assertIn("pending_no_broker_counterpart", cases)
        self.assertNotIn(
            "pending_no_broker_counterpart_assumed_filled", cases
        )


class ExpectedStopChildrenCheckpointTests(unittest.TestCase):
    """Q32: the engine_recovered checkpoint carries the expected
    broker-held stop-child identities so that multi-day holds restart
    cleanly even after the parent order_submitted / order_filled
    records age out of the 48h journal lookback window.

    Covers:
        - build_expected_stop_children payload shape (LOCKED schema)
        - round-trip through a journal record
        - day-3 / day-7 restart: prior checkpoint alone seeds stop-child
          recognition when the parent records have scrolled out
        - zero-length list when no adopted position has a journaled stop
        - no prior checkpoint: behavior unchanged from pre-Q32
    """

    def test_build_from_submit_has_locked_shape(self):
        tail = [
            {
                "ts": EARLIER.isoformat(),
                "event_type": "order_submitted",
                "trade_id": "T-2026-04-21-0001",
                "journal_entry_id": "J1",
                "strategy": "spy-first-paper-smoke",
                "git_sha": "abc",
                "ticker": "SPY",
                "side": "buy",
                "qty": 10,
                "broker_order_id": "1000",
                "broker_perm_id": "2000000",
                "payload": {
                    "status": "Submitted",
                    "limit_price": "700.00",
                    "stop_loss": "697.13",
                    "submitted_at": EARLIER.isoformat(),
                },
            },
        ]
        positions = [
            BrokerPosition(ticker="SPY", qty=10, avg_price=Decimal("700.00"))
        ]
        out = recovery.build_expected_stop_children(
            positions=positions,
            journal_tail=tail,
        )
        self.assertEqual(len(out), 1)
        entry = out[0]
        self.assertEqual(
            set(entry.keys()),
            {
                "ticker",
                "strategy",
                "parent_trade_id",
                "client_tag",
                "trigger_price",
            },
        )
        self.assertEqual(entry["ticker"], "SPY")
        self.assertEqual(entry["strategy"], "spy-first-paper-smoke")
        self.assertEqual(entry["parent_trade_id"], "T-2026-04-21-0001")
        # Decision 2 canonical form: no CLIENT_TAG_PREFIX here. The
        # checkpoint stores the semantic identity; broker matchers parse
        # on-wire tags via parse_client_tag and compare components.
        self.assertEqual(
            entry["client_tag"],
            "spy-first-paper-smoke:T-2026-04-21-0001:stop",
        )
        self.assertEqual(entry["trigger_price"], "697.13")

    def test_build_skips_positions_without_stop(self):
        """A ticker whose journaled parent had no stop_loss is absent
        from the checkpoint -- there is no protective stop to expect."""
        tail = [
            {
                "ts": EARLIER.isoformat(),
                "event_type": "order_submitted",
                "trade_id": "T1",
                "journal_entry_id": "J1",
                "strategy": "no-stop-strategy",
                "git_sha": "abc",
                "ticker": "AAPL",
                "side": "buy",
                "qty": 10,
                "broker_order_id": "1000",
                "broker_perm_id": "2000000",
                "payload": {
                    "status": "Submitted",
                    "limit_price": "200",
                    "stop_loss": None,
                    "submitted_at": EARLIER.isoformat(),
                },
            },
        ]
        positions = [
            BrokerPosition(ticker="AAPL", qty=10, avg_price=Decimal("200"))
        ]
        out = recovery.build_expected_stop_children(
            positions=positions,
            journal_tail=tail,
        )
        self.assertEqual(out, [])

    def test_build_empty_positions_returns_empty_list(self):
        self.assertEqual(
            recovery.build_expected_stop_children(
                positions=[],
                journal_tail=[{"event_type": "order_submitted"}],
            ),
            [],
        )

    def test_build_prefers_submit_stop_when_fill_lacks_it(self):
        """Q32 precondition: order_filled now carries stop_loss, but an
        older pre-precondition order_filled without the field must not
        clobber the stop_loss that the earlier order_submitted carried."""
        tail = [
            {
                "ts": EARLIER.isoformat(),
                "event_type": "order_submitted",
                "trade_id": "T1",
                "journal_entry_id": "J1",
                "strategy": "spy-strat",
                "git_sha": "abc",
                "ticker": "SPY",
                "side": "buy",
                "qty": 10,
                "broker_order_id": "1000",
                "broker_perm_id": "2000000",
                "payload": {
                    "status": "Submitted",
                    "limit_price": "700",
                    "stop_loss": "697.13",
                    "submitted_at": EARLIER.isoformat(),
                },
            },
            {
                "ts": EARLIER.isoformat(),
                "event_type": "order_filled",
                "trade_id": "T1",
                "journal_entry_id": "J2",
                "strategy": "spy-strat",
                "git_sha": "abc",
                "ticker": "SPY",
                "side": "buy",
                "qty": 10,
                "broker_order_id": "1000",
                "broker_perm_id": "2000000",
                "payload": {
                    "ticker": "SPY",
                    "side": "buy",
                    "qty": 10,
                    "fill_price": "700",
                    # Intentionally no stop_loss: simulates a v2-additive
                    # record written before this commit's precondition.
                },
            },
        ]
        positions = [BrokerPosition(ticker="SPY", qty=10, avg_price=Decimal("700"))]
        out = recovery.build_expected_stop_children(
            positions=positions,
            journal_tail=tail,
        )
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["trigger_price"], "697.13")

    def test_checkpoint_round_trip_through_journal_record(self):
        """The engine_recovered payload we write at checkpoint time
        must round-trip through the journal back to the same structure
        the seeding path reads."""
        positions = [
            BrokerPosition(ticker="SPY", qty=10, avg_price=Decimal("500"))
        ]
        tail = [
            {
                "ts": EARLIER.isoformat(),
                "event_type": "order_submitted",
                "trade_id": "T1",
                "journal_entry_id": "J1",
                "strategy": "spy-strat",
                "git_sha": "abc",
                "ticker": "SPY",
                "side": "buy",
                "qty": 10,
                "broker_order_id": "1000",
                "broker_perm_id": "2000000",
                "payload": {
                    "status": "Submitted",
                    "limit_price": "500",
                    "stop_loss": "497.25",
                    "submitted_at": EARLIER.isoformat(),
                },
            },
        ]
        built = recovery.build_expected_stop_children(
            positions=positions,
            journal_tail=tail,
        )
        # Emulate the engine_recovered write from main.py.
        checkpoint_record = {
            "ts": EARLIER.isoformat(),
            "event_type": "engine_recovered",
            "trade_id": None,
            "journal_entry_id": "J2",
            "strategy": None,
            "git_sha": "abc",
            "payload": {
                "status": "catch_up",
                "reconciled_event_count": 0,
                "adopted_positions": [
                    {"ticker": "SPY", "qty": 10, "avg_price": "500"}
                ],
                "expected_stop_children": built,
            },
        }
        # Downstream reader pulls the same list back out.
        recovered = recovery._latest_checkpoint_stop_children([checkpoint_record])
        self.assertEqual(recovered, built)

    def test_day3_restart_stop_child_recognized_via_checkpoint(self):
        """Parent records aged out of the 48h lookback; only the prior
        engine_recovered checkpoint and the broker-held stop child
        remain. Recovery must recognize the stop child via the
        checkpoint, not flag it as phantom."""
        # Simulated state: a day-3 restart. Tail contains only the prior
        # engine_recovered (checkpoint written on day-0) -- no
        # order_submitted / order_filled records survive.
        checkpoint_payload = {
            "status": "catch_up",
            "reconciled_event_count": 0,
            "adopted_positions": [
                {"ticker": "SPY", "qty": 10, "avg_price": "500"}
            ],
            "expected_stop_children": [
                {
                    "ticker": "SPY",
                    "strategy": "spy-strat",
                    "parent_trade_id": "T1",
                    "client_tag": "spy-strat:T1:stop",
                    "trigger_price": "497.25",
                }
            ],
        }
        tail = [
            {
                "ts": EARLIER.isoformat(),
                "event_type": "engine_recovered",
                "trade_id": None,
                "journal_entry_id": "J1",
                "strategy": None,
                "git_sha": "abc",
                "payload": checkpoint_payload,
            },
        ]
        stop_child = BrokerOpenOrder(
            broker_order_id="1001",
            broker_perm_id="2000001",
            ticker="SPY",
            side="sell",
            qty=10,
            filled_qty=0,
            limit_price=Decimal("0"),
            status="Submitted",
            tif="GTC",
            client_tag="k2bi:spy-strat:T1:stop",
            # Q31: matches checkpoint's trigger_price; required for
            # happy-path CATCH_UP under the protective-stop invariant.
            aux_price=Decimal("497.25"),
        )
        result = recovery.reconcile(
            journal_tail=tail,
            broker_positions=[
                BrokerPosition(ticker="SPY", qty=10, avg_price=Decimal("500"))
            ],
            broker_open_orders=[stop_child],
            broker_order_status=[],
            now=NOW,
        )
        self.assertEqual(result.status, recovery.RecoveryStatus.CATCH_UP)
        phantoms = [
            m for m in result.mismatch_reasons if m["case"] == "phantom_open_order"
        ]
        self.assertEqual(phantoms, [])
        cases = [e.payload.get("case") for e in result.events]
        self.assertIn("stop_child_recognized", cases)

    def test_day7_restart_uses_most_recent_checkpoint(self):
        """When multiple engine_recovered events exist across bounces,
        the MOST RECENT one's expected_stop_children wins."""
        older_checkpoint = {
            "ts": EARLIER.isoformat(),
            "event_type": "engine_recovered",
            "trade_id": None,
            "journal_entry_id": "J1",
            "strategy": None,
            "git_sha": "abc",
            "payload": {
                "status": "catch_up",
                "reconciled_event_count": 0,
                "adopted_positions": [
                    {"ticker": "SPY", "qty": 10, "avg_price": "500"}
                ],
                "expected_stop_children": [
                    # Stale trade_id from 7 days ago; no longer current.
                    {
                        "ticker": "SPY",
                        "strategy": "spy-strat",
                        "parent_trade_id": "T-old",
                        "client_tag": "spy-strat:T-old:stop",
                        "trigger_price": "480.00",
                    }
                ],
            },
        }
        newer_checkpoint = {
            "ts": datetime(2026, 5, 5, 10, 0, tzinfo=timezone.utc).isoformat(),
            "event_type": "engine_recovered",
            "trade_id": None,
            "journal_entry_id": "J2",
            "strategy": None,
            "git_sha": "abc",
            "payload": {
                "status": "catch_up",
                "reconciled_event_count": 0,
                "adopted_positions": [
                    {"ticker": "SPY", "qty": 10, "avg_price": "500"}
                ],
                "expected_stop_children": [
                    {
                        "ticker": "SPY",
                        "strategy": "spy-strat",
                        "parent_trade_id": "T-current",
                        "client_tag": "spy-strat:T-current:stop",
                        "trigger_price": "497.25",
                    }
                ],
            },
        }
        tail = [older_checkpoint, newer_checkpoint]
        stop_child = BrokerOpenOrder(
            broker_order_id="1001",
            broker_perm_id="2000001",
            ticker="SPY",
            side="sell",
            qty=10,
            filled_qty=0,
            limit_price=Decimal("0"),
            status="Submitted",
            tif="GTC",
            client_tag="k2bi:spy-strat:T-current:stop",
            # Q31: matches newer checkpoint's trigger_price (497.25).
            aux_price=Decimal("497.25"),
        )
        result = recovery.reconcile(
            journal_tail=tail,
            broker_positions=[
                BrokerPosition(ticker="SPY", qty=10, avg_price=Decimal("500"))
            ],
            broker_open_orders=[stop_child],
            broker_order_status=[],
            now=NOW,
        )
        self.assertEqual(result.status, recovery.RecoveryStatus.CATCH_UP)
        phantoms = [
            m for m in result.mismatch_reasons if m["case"] == "phantom_open_order"
        ]
        self.assertEqual(phantoms, [])
        cases = [e.payload.get("case") for e in result.events]
        self.assertIn("stop_child_recognized", cases)

    def test_no_prior_checkpoint_falls_back_to_journal_tail(self):
        """When no engine_recovered checkpoint exists in the tail, the
        pre-Q32 journal-tail-only recognition path still applies."""
        tail = [
            {
                "ts": EARLIER.isoformat(),
                "event_type": "order_submitted",
                "trade_id": "T1",
                "journal_entry_id": "J1",
                "strategy": "spy-strat",
                "git_sha": "abc",
                "ticker": "SPY",
                "side": "buy",
                "qty": 10,
                "broker_order_id": "1000",
                "broker_perm_id": "2000000",
                "payload": {
                    "status": "Submitted",
                    "limit_price": "500",
                    "stop_loss": "497.25",
                    "submitted_at": EARLIER.isoformat(),
                },
            },
            {
                "ts": EARLIER.isoformat(),
                "event_type": "order_filled",
                "trade_id": "T1",
                "journal_entry_id": "J2",
                "strategy": "spy-strat",
                "git_sha": "abc",
                "ticker": "SPY",
                "side": "buy",
                "qty": 10,
                "broker_order_id": "1000",
                "broker_perm_id": "2000000",
                "payload": {
                    "ticker": "SPY",
                    "side": "buy",
                    "qty": 10,
                    "fill_price": "500",
                    "stop_loss": "497.25",
                },
            },
        ]
        stop_child = BrokerOpenOrder(
            broker_order_id="1001",
            broker_perm_id="2000001",
            ticker="SPY",
            side="sell",
            qty=10,
            filled_qty=0,
            limit_price=Decimal("0"),
            status="Submitted",
            tif="GTC",
            client_tag="k2bi:spy-strat:T1:stop",
            # Q31 defensive Phase B.3 now also validates journal-tail
            # parents (not just the prior checkpoint). aux_price must
            # match the journal's stop_loss for the happy CATCH_UP
            # path; otherwise price_drift would fire.
            aux_price=Decimal("497.25"),
        )
        result = recovery.reconcile(
            journal_tail=tail,
            broker_positions=[
                BrokerPosition(ticker="SPY", qty=10, avg_price=Decimal("500"))
            ],
            broker_open_orders=[stop_child],
            broker_order_status=[],
            now=NOW,
        )
        self.assertEqual(result.status, recovery.RecoveryStatus.CATCH_UP)
        phantoms = [
            m for m in result.mismatch_reasons if m["case"] == "phantom_open_order"
        ]
        self.assertEqual(phantoms, [])

    def test_latest_checkpoint_stop_children_empty_when_payload_missing(self):
        """A pre-Q32 engine_recovered without expected_stop_children
        must resolve to an empty list, not crash or raise."""
        tail = [
            {
                "ts": EARLIER.isoformat(),
                "event_type": "engine_recovered",
                "trade_id": None,
                "journal_entry_id": "J1",
                "strategy": None,
                "git_sha": "abc",
                "payload": {
                    "status": "catch_up",
                    "reconciled_event_count": 0,
                    "adopted_positions": [],
                    # no expected_stop_children key
                },
            },
        ]
        self.assertEqual(
            recovery._latest_checkpoint_stop_children(tail),
            [],
        )

    def test_latest_checkpoint_stop_children_corrupt_payload_yields_empty(self):
        tail = [
            {
                "ts": EARLIER.isoformat(),
                "event_type": "engine_recovered",
                "trade_id": None,
                "journal_entry_id": "J1",
                "strategy": None,
                "git_sha": "abc",
                "payload": {
                    "expected_stop_children": "not-a-list",
                },
            },
        ]
        self.assertEqual(
            recovery._latest_checkpoint_stop_children(tail),
            [],
        )

    def test_build_carries_forward_prior_checkpoint_when_parent_aged_out(self):
        """Codex R1 P1: when the parent records have scrolled out of
        journal_tail but the prior engine_recovered still appears in
        the tail, the freshly-built checkpoint must carry forward the
        prior entry for adopted positions. Without this, recovery hop
        #2 loses stop-child identity and falls back to phantom."""
        # Simulated state at recovery hop #2: journal_tail has a prior
        # engine_recovered (hop #1's checkpoint) but no order_submitted
        # or order_filled for SPY (aged out).
        prior_checkpoint = {
            "ts": EARLIER.isoformat(),
            "event_type": "engine_recovered",
            "trade_id": None,
            "journal_entry_id": "J1",
            "strategy": None,
            "git_sha": "abc",
            "payload": {
                "status": "catch_up",
                "reconciled_event_count": 0,
                "adopted_positions": [
                    {"ticker": "SPY", "qty": 10, "avg_price": "500"}
                ],
                "expected_stop_children": [
                    {
                        "ticker": "SPY",
                        "strategy": "spy-strat",
                        "parent_trade_id": "T1",
                        "client_tag": "spy-strat:T1:stop",
                        "trigger_price": "497.25",
                    }
                ],
            },
        }
        positions = [
            BrokerPosition(ticker="SPY", qty=10, avg_price=Decimal("500"))
        ]
        out = recovery.build_expected_stop_children(
            positions=positions,
            journal_tail=[prior_checkpoint],
        )
        self.assertEqual(len(out), 1)
        entry = out[0]
        self.assertEqual(entry["ticker"], "SPY")
        self.assertEqual(entry["strategy"], "spy-strat")
        self.assertEqual(entry["parent_trade_id"], "T1")
        self.assertEqual(entry["client_tag"], "spy-strat:T1:stop")
        self.assertEqual(entry["trigger_price"], "497.25")

    def test_build_newest_parent_wins_on_same_ticker_reentry(self):
        """Codex R6 P1 + MVP one-parent-per-ticker: when the same
        ticker has been traded more than once within the lookback
        window (exit-then-reenter), only the NEWEST buy parent is
        emitted in the checkpoint. Older parents are assumed closed;
        their orphaned stops (if any) fall through to phantom
        detection rather than being silently recognized as protective
        stops for the current position."""
        tail = [
            # T-old: the earlier buy (later closed).
            {
                "ts": EARLIER.isoformat(),
                "event_type": "order_submitted",
                "trade_id": "T-old",
                "journal_entry_id": "J1",
                "strategy": "spy-strat",
                "git_sha": "abc",
                "ticker": "SPY",
                "side": "buy",
                "qty": 10,
                "broker_order_id": "1000",
                "broker_perm_id": "2000000",
                "payload": {
                    "status": "Submitted",
                    "limit_price": "500",
                    "stop_loss": "495.00",
                    "submitted_at": EARLIER.isoformat(),
                },
            },
            # T-new: the current buy.
            {
                "ts": datetime(2026, 5, 5, 11, 45, tzinfo=timezone.utc).isoformat(),
                "event_type": "order_submitted",
                "trade_id": "T-new",
                "journal_entry_id": "J2",
                "strategy": "spy-strat",
                "git_sha": "abc",
                "ticker": "SPY",
                "side": "buy",
                "qty": 10,
                "broker_order_id": "1010",
                "broker_perm_id": "2000010",
                "payload": {
                    "status": "Submitted",
                    "limit_price": "502",
                    "stop_loss": "498.50",
                    "submitted_at": datetime(2026, 5, 5, 11, 45, tzinfo=timezone.utc).isoformat(),
                },
            },
        ]
        positions = [
            BrokerPosition(ticker="SPY", qty=10, avg_price=Decimal("502"))
        ]
        out = recovery.build_expected_stop_children(
            positions=positions,
            journal_tail=tail,
        )
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["parent_trade_id"], "T-new")
        self.assertEqual(out[0]["trigger_price"], "498.50")

    def test_build_captures_recovery_discovered_fill(self):
        """Codex R5 P1: a fill discovered during recovery
        (recovery_reconciled with case=pending_filled) contributes its
        stop_loss to the checkpoint via journal_view. Without this, a
        crash between order_proposed and order_submitted leaves the
        stop metadata ONLY in the recovery event, and subsequent
        restarts would flag the live stop as phantom after aging out."""
        # journal_tail has only order_proposed (crash before submit
        # wrote broker IDs). Recovery will discover the filled status
        # via broker order status history -- not in this test but
        # simulated via a pre-built ReconciliationEvent.
        tail = [
            {
                "ts": EARLIER.isoformat(),
                "event_type": "order_proposed",
                "trade_id": "T1",
                "journal_entry_id": "J1",
                "strategy": "spy-strat",
                "git_sha": "abc",
                "ticker": "SPY",
                "side": "buy",
                "qty": 10,
                "payload": {
                    "ticker": "SPY",
                    "side": "buy",
                    "qty": 10,
                    "limit_price": "500",
                    "stop_loss": "497.25",
                    "submitted_at": EARLIER.isoformat(),
                },
            }
        ]
        reconciled_event = recovery.ReconciliationEvent(
            event_type="recovery_reconciled",
            payload={
                "case": "pending_filled",
                "broker_status": "Filled",
                "filled_qty": 10,
                "remaining_qty": 0,
                "avg_fill_price": "500.00",
                "broker_reason": None,
                "journal_view": {
                    "ticker": "SPY",
                    "side": "buy",
                    "qty": 10,
                    "limit_price": "500",
                    "stop_loss": "497.25",
                    "submitted_at": EARLIER.isoformat(),
                },
            },
            trade_id="T1",
            strategy="spy-strat",
            ticker="SPY",
            broker_order_id="1000",
            broker_perm_id="2000000",
        )
        positions = [
            BrokerPosition(ticker="SPY", qty=10, avg_price=Decimal("500.00"))
        ]
        out = recovery.build_expected_stop_children(
            positions=positions,
            journal_tail=tail,
            recovery_events=[reconciled_event],
        )
        self.assertEqual(len(out), 1)
        entry = out[0]
        self.assertEqual(entry["ticker"], "SPY")
        self.assertEqual(entry["parent_trade_id"], "T1")
        self.assertEqual(entry["trigger_price"], "497.25")
        self.assertEqual(entry["client_tag"], "spy-strat:T1:stop")

    def test_build_fresh_journal_supersedes_prior_checkpoint_different_trade_id(self):
        """When journal_tail carries a NEW buy parent for a ticker
        (different trade_id than the prior checkpoint), the fresh
        journal is authoritative and the prior entry does not carry
        forward. This is the exit-then-reenter case under MVP one-
        parent-per-ticker: the old parent is assumed closed."""
        prior_checkpoint = {
            "ts": EARLIER.isoformat(),
            "event_type": "engine_recovered",
            "trade_id": None,
            "journal_entry_id": "J0",
            "strategy": None,
            "git_sha": "abc",
            "payload": {
                "status": "catch_up",
                "reconciled_event_count": 0,
                "adopted_positions": [
                    {"ticker": "SPY", "qty": 10, "avg_price": "500"}
                ],
                "expected_stop_children": [
                    {
                        "ticker": "SPY",
                        "strategy": "spy-strat",
                        "parent_trade_id": "T-old",
                        "client_tag": "spy-strat:T-old:stop",
                        "trigger_price": "495.00",
                    }
                ],
            },
        }
        fresh_parent = {
            "ts": datetime(2026, 5, 5, 11, 45, tzinfo=timezone.utc).isoformat(),
            "event_type": "order_submitted",
            "trade_id": "T-new",
            "journal_entry_id": "J1",
            "strategy": "spy-strat",
            "git_sha": "abc",
            "ticker": "SPY",
            "side": "buy",
            "qty": 10,
            "broker_order_id": "2000",
            "broker_perm_id": "3000000",
            "payload": {
                "status": "Submitted",
                "limit_price": "502",
                "stop_loss": "498.50",
                "submitted_at": datetime(2026, 5, 5, 11, 45, tzinfo=timezone.utc).isoformat(),
            },
        }
        out = recovery.build_expected_stop_children(
            positions=[
                BrokerPosition(ticker="SPY", qty=10, avg_price=Decimal("502"))
            ],
            journal_tail=[prior_checkpoint, fresh_parent],
        )
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["parent_trade_id"], "T-new")
        self.assertEqual(out[0]["trigger_price"], "498.50")

    def test_build_journal_overrides_prior_checkpoint_for_same_trade_id(self):
        """When the fresh journal's order_submitted / order_filled for
        a trade_id has stop_loss=None, the journal's "no stop" view
        wins over the prior checkpoint's entry for that same trade_id.
        Carry-forward only applies to trade_ids that do NOT appear in
        the current journal (they've aged out, so the prior checkpoint
        is the only surviving signal)."""
        prior_checkpoint = {
            "ts": EARLIER.isoformat(),
            "event_type": "engine_recovered",
            "trade_id": None,
            "journal_entry_id": "J1",
            "strategy": None,
            "git_sha": "abc",
            "payload": {
                "status": "catch_up",
                "reconciled_event_count": 0,
                "adopted_positions": [
                    {"ticker": "SPY", "qty": 10, "avg_price": "500"}
                ],
                "expected_stop_children": [
                    {
                        "ticker": "SPY",
                        "strategy": "spy-strat",
                        "parent_trade_id": "T1",
                        "client_tag": "spy-strat:T1:stop",
                        "trigger_price": "497.25",
                    }
                ],
            },
        }
        # Same trade_id T1, explicit stop_loss=None in the fresh journal
        # (e.g. operator cleared the stop).
        journal_removes_stop = {
            "ts": EARLIER.isoformat(),
            "event_type": "order_submitted",
            "trade_id": "T1",
            "journal_entry_id": "J2",
            "strategy": "spy-strat",
            "git_sha": "abc",
            "ticker": "SPY",
            "side": "buy",
            "qty": 10,
            "broker_order_id": "1000",
            "broker_perm_id": "2000000",
            "payload": {
                "status": "Submitted",
                "limit_price": "500",
                "stop_loss": None,
                "submitted_at": EARLIER.isoformat(),
            },
        }
        out = recovery.build_expected_stop_children(
            positions=[
                BrokerPosition(ticker="SPY", qty=10, avg_price=Decimal("500"))
            ],
            journal_tail=[prior_checkpoint, journal_removes_stop],
        )
        # Journal's T1 view (no stop) overrides the prior checkpoint's
        # T1 entry; no entries emitted.
        self.assertEqual(out, [])

    def test_checkpoint_seeding_requires_position_still_open(self):
        """Codex R3 P1: a stop child left behind at the broker after
        the position was closed must NOT be recognized via checkpoint
        seeding. The checkpoint only protects currently-held
        positions; an orphaned sell stop on a closed ticker could
        open an unintended short on trigger and must be flagged."""
        tail = [
            {
                "ts": EARLIER.isoformat(),
                "event_type": "engine_recovered",
                "trade_id": None,
                "journal_entry_id": "J1",
                "strategy": None,
                "git_sha": "abc",
                "payload": {
                    "status": "catch_up",
                    "reconciled_event_count": 0,
                    "adopted_positions": [],
                    "expected_stop_children": [
                        {
                            "ticker": "SPY",
                            "strategy": "spy-strat",
                            "parent_trade_id": "T1",
                            "client_tag": "spy-strat:T1:stop",
                            "trigger_price": "497.25",
                        }
                    ],
                },
            },
        ]
        orphan_stop = BrokerOpenOrder(
            broker_order_id="1001",
            broker_perm_id="2000001",
            ticker="SPY",
            side="sell",
            qty=10,
            filled_qty=0,
            limit_price=Decimal("0"),
            status="Submitted",
            tif="GTC",
            client_tag="k2bi:spy-strat:T1:stop",
        )
        result = recovery.reconcile(
            journal_tail=tail,
            broker_positions=[],  # position closed; nothing to protect
            broker_open_orders=[orphan_stop],
            broker_order_status=[],
            now=NOW,
        )
        phantoms = [
            m for m in result.mismatch_reasons
            if m["case"] == "phantom_open_order"
        ]
        self.assertEqual(len(phantoms), 1)
        self.assertEqual(phantoms[0]["ticker"], "SPY")
        self.assertEqual(
            result.status, recovery.RecoveryStatus.MISMATCH_REFUSED
        )

    def test_checkpoint_seeding_requires_ticker_match(self):
        """Checkpoint seeds stop-child recognition only when the broker
        open order's ticker matches the checkpoint entry. A trade_id
        collision across tickers (e.g. replayed checkpoint vs a new
        stop on a different symbol) must not silently recognize the
        wrong-ticker stop."""
        tail = [
            {
                "ts": EARLIER.isoformat(),
                "event_type": "engine_recovered",
                "trade_id": None,
                "journal_entry_id": "J1",
                "strategy": None,
                "git_sha": "abc",
                "payload": {
                    "status": "catch_up",
                    "reconciled_event_count": 0,
                    "adopted_positions": [
                        {"ticker": "SPY", "qty": 10, "avg_price": "500"}
                    ],
                    "expected_stop_children": [
                        {
                            "ticker": "SPY",
                            "strategy": "spy-strat",
                            "parent_trade_id": "T1",
                            "client_tag": "spy-strat:T1:stop",
                            "trigger_price": "497.25",
                        }
                    ],
                },
            },
        ]
        # Broker has a stop child with matching trade_id T1 but on QQQ,
        # not SPY. Must flag as phantom, not recognize.
        wrong_ticker_stop = BrokerOpenOrder(
            broker_order_id="1001",
            broker_perm_id="2000001",
            ticker="QQQ",  # mismatched ticker
            side="sell",
            qty=10,
            filled_qty=0,
            limit_price=Decimal("0"),
            status="Submitted",
            tif="GTC",
            client_tag="k2bi:spy-strat:T1:stop",
        )
        result = recovery.reconcile(
            journal_tail=tail,
            broker_positions=[
                BrokerPosition(ticker="SPY", qty=10, avg_price=Decimal("500"))
            ],
            broker_open_orders=[wrong_ticker_stop],
            broker_order_status=[],
            now=NOW,
        )
        phantoms = [
            m for m in result.mismatch_reasons
            if m["case"] == "phantom_open_order"
        ]
        self.assertEqual(len(phantoms), 1)
        self.assertEqual(phantoms[0]["ticker"], "QQQ")
        self.assertEqual(
            result.status, recovery.RecoveryStatus.MISMATCH_REFUSED
        )

    def test_checkpoint_seeding_does_not_recognize_non_stop_order(self):
        """MiniMax R2 Finding 2: checkpoint-seeded trade_ids must NOT
        suppress phantom detection on non-stop broker orders. A
        corrupt / replayed checkpoint entry with trade_id T1 cannot
        reclassify a k2bi-tagged parent order with the same trade_id
        as recognized -- parent recognition lives in pending_trade_ids
        (journal-backed) only."""
        tail = [
            {
                "ts": EARLIER.isoformat(),
                "event_type": "engine_recovered",
                "trade_id": None,
                "journal_entry_id": "J1",
                "strategy": None,
                "git_sha": "abc",
                "payload": {
                    "status": "catch_up",
                    "reconciled_event_count": 0,
                    "adopted_positions": [
                        {"ticker": "SPY", "qty": 10, "avg_price": "500"}
                    ],
                    "expected_stop_children": [
                        {
                            "ticker": "SPY",
                            "strategy": "spy-strat",
                            "parent_trade_id": "T1",
                            "client_tag": "spy-strat:T1:stop",
                            "trigger_price": "497.25",
                        }
                    ],
                },
            },
        ]
        # A broker-side order tagged with T1 but NOT a stop (e.g. a
        # replayed parent tag somehow surfacing). Must be flagged as
        # phantom, not suppressed via checkpoint seeding.
        non_stop_with_matching_trade_id = BrokerOpenOrder(
            broker_order_id="9999",
            broker_perm_id="8888",
            ticker="SPY",
            side="buy",
            qty=5,
            filled_qty=0,
            limit_price=Decimal("499"),
            status="Submitted",
            tif="DAY",
            client_tag="k2bi:spy-strat:T1",  # parent tag, no :stop suffix
        )
        result = recovery.reconcile(
            journal_tail=tail,
            broker_positions=[
                BrokerPosition(ticker="SPY", qty=10, avg_price=Decimal("500"))
            ],
            broker_open_orders=[non_stop_with_matching_trade_id],
            broker_order_status=[],
            now=NOW,
        )
        phantoms = [
            m for m in result.mismatch_reasons
            if m["case"] == "phantom_open_order"
        ]
        self.assertEqual(len(phantoms), 1)
        self.assertEqual(phantoms[0]["client_tag"], "k2bi:spy-strat:T1")
        # And the engine refuses to start on this mismatch.
        self.assertEqual(
            result.status, recovery.RecoveryStatus.MISMATCH_REFUSED
        )


class ProtectiveStopInvariantTests(unittest.TestCase):
    """Q31: missing_protective_stop / protective_stop_price_drift /
    protective_stop_tag_mismatch invariants in Phase B.2. All emit
    into the mismatches list, causing recovery to fail with
    MISMATCH_REFUSED per the existing pattern. No auto-recovery or
    auto-reattach -- operator investigates before relaunching.

    Consumes Q32's expected_stop_children checkpoint. Kickoff
    Decisions 4-6 apply:
      - Decision 4: mismatches fail recovery (no auto-recovery).
      - Decision 5: intentionally-cancelled stops FAIL with
        missing_protective_stop (no operator-intent signal).
      - Decision 6: trigger price match is EXACT Decimal equality
        (no tolerance).
    """

    def _checkpoint_tail(
        self,
        *,
        ticker: str = "SPY",
        strategy: str = "spy-strat",
        trade_id: str = "T1",
        trigger_price: str = "497.25",
    ) -> list[dict]:
        return [
            {
                "ts": EARLIER.isoformat(),
                "event_type": "engine_recovered",
                "trade_id": None,
                "journal_entry_id": "J1",
                "strategy": None,
                "git_sha": "abc",
                "payload": {
                    "status": "catch_up",
                    "reconciled_event_count": 0,
                    "adopted_positions": [
                        {"ticker": ticker, "qty": 10, "avg_price": "500"}
                    ],
                    "expected_stop_children": [
                        {
                            "ticker": ticker,
                            "strategy": strategy,
                            "parent_trade_id": trade_id,
                            "client_tag": f"{strategy}:{trade_id}:stop",
                            "trigger_price": trigger_price,
                        }
                    ],
                },
            }
        ]

    def test_missing_protective_stop_fails_recovery(self):
        """Broker has the position but NO stop child. Recovery must
        fail with missing_protective_stop."""
        tail = self._checkpoint_tail()
        result = recovery.reconcile(
            journal_tail=tail,
            broker_positions=[
                BrokerPosition(ticker="SPY", qty=10, avg_price=Decimal("500"))
            ],
            broker_open_orders=[],
            broker_order_status=[],
            now=NOW,
        )
        self.assertEqual(
            result.status, recovery.RecoveryStatus.MISMATCH_REFUSED
        )
        missing = [
            m
            for m in result.mismatch_reasons
            if m["case"] == "missing_protective_stop"
        ]
        self.assertEqual(len(missing), 1)
        self.assertEqual(missing[0]["ticker"], "SPY")
        self.assertEqual(missing[0]["expected_trade_id"], "T1")
        self.assertEqual(missing[0]["expected_trigger_price"], "497.25")

    def test_intentionally_cancelled_stop_fails_recovery_same_as_missing(self):
        """Decision 5: intentionally-cancelled stop is indistinguishable
        from a dropped/missing stop at the journal level -- the engine
        has no operator-intent signal to read. Fail-closed with
        missing_protective_stop is the CORRECT MVP behavior; Phase 4
        `/invest unprotect-position` is the un-defer trigger."""
        tail = self._checkpoint_tail()
        result = recovery.reconcile(
            journal_tail=tail,
            broker_positions=[
                BrokerPosition(ticker="SPY", qty=10, avg_price=Decimal("500"))
            ],
            broker_open_orders=[],  # Operator cancelled the stop.
            broker_order_status=[],
            now=NOW,
        )
        self.assertEqual(
            result.status, recovery.RecoveryStatus.MISMATCH_REFUSED
        )
        missing = [
            m
            for m in result.mismatch_reasons
            if m["case"] == "missing_protective_stop"
        ]
        self.assertEqual(len(missing), 1)

    def test_protective_stop_price_drift_fails_recovery(self):
        """Checkpoint expects trigger 497.25; broker shows 497.24.
        Decision 6: exact Decimal equality, no tolerance."""
        tail = self._checkpoint_tail(trigger_price="497.25")
        drifted_stop = BrokerOpenOrder(
            broker_order_id="1001",
            broker_perm_id="2000001",
            ticker="SPY",
            side="sell",
            qty=10,
            filled_qty=0,
            limit_price=Decimal("0"),
            status="Submitted",
            tif="GTC",
            client_tag="k2bi:spy-strat:T1:stop",
            aux_price=Decimal("497.24"),  # drift of 1 cent
        )
        result = recovery.reconcile(
            journal_tail=tail,
            broker_positions=[
                BrokerPosition(ticker="SPY", qty=10, avg_price=Decimal("500"))
            ],
            broker_open_orders=[drifted_stop],
            broker_order_status=[],
            now=NOW,
        )
        self.assertEqual(
            result.status, recovery.RecoveryStatus.MISMATCH_REFUSED
        )
        drifts = [
            m
            for m in result.mismatch_reasons
            if m["case"] == "protective_stop_price_drift"
        ]
        self.assertEqual(len(drifts), 1)
        self.assertEqual(drifts[0]["ticker"], "SPY")
        self.assertEqual(drifts[0]["expected_trigger_price"], "497.25")
        self.assertEqual(drifts[0]["broker_trigger_price"], "497.24")

    def test_protective_stop_tag_mismatch_fails_recovery(self):
        """Broker has a stop on the ticker but with a different trade_id
        than the checkpoint expected. Tag mismatch means recovery
        cannot confirm the current stop is the one journalled; fail
        closed so operator investigates."""
        tail = self._checkpoint_tail(trade_id="T-expected")
        wrong_tag_stop = BrokerOpenOrder(
            broker_order_id="1001",
            broker_perm_id="2000001",
            ticker="SPY",
            side="sell",
            qty=10,
            filled_qty=0,
            limit_price=Decimal("0"),
            status="Submitted",
            tif="GTC",
            # Same trigger but different trade_id (e.g. operator
            # cancelled the expected stop and placed a new one with
            # a different tag).
            client_tag="k2bi:spy-strat:T-other:stop",
            aux_price=Decimal("497.25"),
        )
        result = recovery.reconcile(
            journal_tail=tail,
            broker_positions=[
                BrokerPosition(ticker="SPY", qty=10, avg_price=Decimal("500"))
            ],
            broker_open_orders=[wrong_tag_stop],
            broker_order_status=[],
            now=NOW,
        )
        self.assertEqual(
            result.status, recovery.RecoveryStatus.MISMATCH_REFUSED
        )
        tag_mismatches = [
            m
            for m in result.mismatch_reasons
            if m["case"] == "protective_stop_tag_mismatch"
        ]
        self.assertEqual(len(tag_mismatches), 1)
        self.assertEqual(tag_mismatches[0]["expected_trade_id"], "T-expected")
        self.assertEqual(
            tag_mismatches[0]["broker_stop_tags"],
            ["k2bi:spy-strat:T-other:stop"],
        )

    def test_happy_path_stop_tag_and_trigger_match(self):
        """Checkpoint and broker state agree on stop tag + trigger.
        Recovery proceeds CATCH_UP, no mismatch."""
        tail = self._checkpoint_tail()
        expected_stop = BrokerOpenOrder(
            broker_order_id="1001",
            broker_perm_id="2000001",
            ticker="SPY",
            side="sell",
            qty=10,
            filled_qty=0,
            limit_price=Decimal("0"),
            status="Submitted",
            tif="GTC",
            client_tag="k2bi:spy-strat:T1:stop",
            aux_price=Decimal("497.25"),
        )
        result = recovery.reconcile(
            journal_tail=tail,
            broker_positions=[
                BrokerPosition(ticker="SPY", qty=10, avg_price=Decimal("500"))
            ],
            broker_open_orders=[expected_stop],
            broker_order_status=[],
            now=NOW,
        )
        self.assertEqual(result.status, recovery.RecoveryStatus.CATCH_UP)
        for case in {
            "missing_protective_stop",
            "protective_stop_tag_mismatch",
            "protective_stop_price_drift",
        }:
            hits = [
                m for m in result.mismatch_reasons if m["case"] == case
            ]
            self.assertEqual(
                hits, [], f"expected no {case}, got {hits}"
            )

    def test_checkpoint_without_position_skips_q31(self):
        """Checkpoint references a ticker the broker no longer holds.
        Q31 does not fire (no position to protect); any orphan stop
        is Phase B.1's job via phantom_open_order."""
        tail = self._checkpoint_tail()
        # Broker has no position for SPY. An orphan stop would
        # phantom under Phase B.1, but here we give a clean empty
        # state to isolate the Q31 skip.
        result = recovery.reconcile(
            journal_tail=tail,
            broker_positions=[],
            broker_open_orders=[],
            broker_order_status=[],
            now=NOW,
        )
        q31_cases = {
            "missing_protective_stop",
            "protective_stop_tag_mismatch",
            "protective_stop_price_drift",
        }
        q31_hits = [
            m for m in result.mismatch_reasons if m["case"] in q31_cases
        ]
        self.assertEqual(q31_hits, [])

    def test_journal_parent_without_prior_checkpoint_triggers_q31(self):
        """MiniMax R1 Finding 2: Phase B.3 also checks journal-tail
        parents, not just the prior checkpoint. A freshly-opened
        position with journaled stop_loss but NO prior checkpoint
        (first recovery after position creation + crash) must still
        fail recovery when the broker-side stop is missing."""
        tail = [
            {
                "ts": EARLIER.isoformat(),
                "event_type": "order_submitted",
                "trade_id": "T-new",
                "journal_entry_id": "J1",
                "strategy": "spy-strat",
                "git_sha": "abc",
                "ticker": "SPY",
                "side": "buy",
                "qty": 10,
                "broker_order_id": "1000",
                "broker_perm_id": "2000000",
                "payload": {
                    "status": "Submitted",
                    "limit_price": "500",
                    "stop_loss": "497.25",
                    "submitted_at": EARLIER.isoformat(),
                },
            },
            {
                "ts": EARLIER.isoformat(),
                "event_type": "order_filled",
                "trade_id": "T-new",
                "journal_entry_id": "J2",
                "strategy": "spy-strat",
                "git_sha": "abc",
                "ticker": "SPY",
                "side": "buy",
                "qty": 10,
                "broker_order_id": "1000",
                "broker_perm_id": "2000000",
                "payload": {
                    "ticker": "SPY",
                    "side": "buy",
                    "qty": 10,
                    "fill_price": "500",
                    "stop_loss": "497.25",
                },
            },
        ]
        # Broker: position exists but the stop was cancelled /
        # dropped. No prior engine_recovered checkpoint in tail.
        result = recovery.reconcile(
            journal_tail=tail,
            broker_positions=[
                BrokerPosition(ticker="SPY", qty=10, avg_price=Decimal("500"))
            ],
            broker_open_orders=[],
            broker_order_status=[],
            now=NOW,
        )
        self.assertEqual(
            result.status, recovery.RecoveryStatus.MISMATCH_REFUSED
        )
        missing = [
            m
            for m in result.mismatch_reasons
            if m["case"] == "missing_protective_stop"
        ]
        self.assertEqual(len(missing), 1)
        self.assertEqual(missing[0]["expected_trade_id"], "T-new")

    def test_default_aux_price_zero_fails_closed_as_price_drift(self):
        """Contract test: BrokerOpenOrder.aux_price defaults to
        Decimal('0') as a FAIL-CLOSED sentinel. A connector that
        returns a stop child without populating aux_price will
        trigger protective_stop_price_drift on any non-zero
        checkpointed trigger. This documents the default behavior
        so future mock/connector authors know the contract."""
        tail = self._checkpoint_tail(trigger_price="497.25")
        # Stop child with no aux_price argument -- relies on the
        # Decimal("0") default.
        unset_aux_stop = BrokerOpenOrder(
            broker_order_id="1001",
            broker_perm_id="2000001",
            ticker="SPY",
            side="sell",
            qty=10,
            filled_qty=0,
            limit_price=Decimal("0"),
            status="Submitted",
            tif="GTC",
            client_tag="k2bi:spy-strat:T1:stop",
            # aux_price omitted intentionally.
        )
        result = recovery.reconcile(
            journal_tail=tail,
            broker_positions=[
                BrokerPosition(ticker="SPY", qty=10, avg_price=Decimal("500"))
            ],
            broker_open_orders=[unset_aux_stop],
            broker_order_status=[],
            now=NOW,
        )
        self.assertEqual(
            result.status, recovery.RecoveryStatus.MISMATCH_REFUSED
        )
        drifts = [
            m
            for m in result.mismatch_reasons
            if m["case"] == "protective_stop_price_drift"
        ]
        self.assertEqual(len(drifts), 1)
        # Default was Decimal("0"), reported as "0" in the mismatch.
        self.assertEqual(drifts[0]["broker_trigger_price"], "0")

    def test_operator_adjusted_stop_at_broker_fails_recovery(self):
        """Operator-adjusted stop price at broker: operator tightens
        the stop from the checkpoint's 497.25 to 495.00 directly at
        IBKR. On restart, Phase B.3 fires protective_stop_price_drift
        and refuses startup. The resolution path is operator-action:
        either revert the stop to 497.25 (match checkpoint), or set
        K2BI_ALLOW_RECOVERY_MISMATCH=1 to accept the drift and
        proceed. MiniMax R2 Finding 2 documentation test."""
        tail = self._checkpoint_tail(trigger_price="497.25")
        adjusted_stop = BrokerOpenOrder(
            broker_order_id="1001",
            broker_perm_id="2000001",
            ticker="SPY",
            side="sell",
            qty=10,
            filled_qty=0,
            limit_price=Decimal("0"),
            status="Submitted",
            tif="GTC",
            client_tag="k2bi:spy-strat:T1:stop",
            # Operator manually tightened the stop at IBKR.
            aux_price=Decimal("495.00"),
        )
        # Without override: refuse-to-start.
        result = recovery.reconcile(
            journal_tail=tail,
            broker_positions=[
                BrokerPosition(ticker="SPY", qty=10, avg_price=Decimal("500"))
            ],
            broker_open_orders=[adjusted_stop],
            broker_order_status=[],
            now=NOW,
            override_env="",
        )
        self.assertEqual(
            result.status, recovery.RecoveryStatus.MISMATCH_REFUSED
        )
        drifts = [
            m
            for m in result.mismatch_reasons
            if m["case"] == "protective_stop_price_drift"
        ]
        self.assertEqual(len(drifts), 1)
        self.assertEqual(drifts[0]["broker_trigger_price"], "495.00")
        # With override: MISMATCH_OVERRIDE (operator accepts drift).
        result_override = recovery.reconcile(
            journal_tail=tail,
            broker_positions=[
                BrokerPosition(ticker="SPY", qty=10, avg_price=Decimal("500"))
            ],
            broker_open_orders=[adjusted_stop],
            broker_order_status=[],
            now=NOW,
            override_env="1",
        )
        self.assertEqual(
            result_override.status,
            recovery.RecoveryStatus.MISMATCH_OVERRIDE,
        )

    def test_q31_same_ticker_reentry_suppresses_stale_checkpoint(self):
        """Codex Commit-2 R2 P1: same-ticker exit-and-reenter within
        lookback. Prior checkpoint expected T-old stop. Journal-tail
        has a fresh T-new parent for same ticker. Broker shows
        T-new's stop correctly -- the old T-old entry is stale and
        must NOT validate (would spuriously fire missing/tag_mismatch
        and block startup).

        2026-04-21: adopted_positions cleared in the checkpoint so
        that Q39's synthetic-fill delta for T-new (pending_filled
        case) does not stack on top of a stale 10-share snapshot that
        has no matching broker position. Q31 suppression is driven by
        expected_stop_children, not adopted_positions."""
        prior_checkpoint = {
            "ts": EARLIER.isoformat(),
            "event_type": "engine_recovered",
            "trade_id": None,
            "journal_entry_id": "J0",
            "strategy": None,
            "git_sha": "abc",
            "payload": {
                "status": "catch_up",
                "reconciled_event_count": 0,
                "adopted_positions": [],
                "expected_stop_children": [
                    {
                        "ticker": "SPY",
                        "strategy": "spy-strat",
                        "parent_trade_id": "T-old",
                        "client_tag": "spy-strat:T-old:stop",
                        "trigger_price": "495.00",
                    }
                ],
            },
        }
        fresh_parent = {
            "ts": datetime(2026, 5, 5, 11, 45, tzinfo=timezone.utc).isoformat(),
            "event_type": "order_submitted",
            "trade_id": "T-new",
            "journal_entry_id": "J1",
            "strategy": "spy-strat",
            "git_sha": "abc",
            "ticker": "SPY",
            "side": "buy",
            "qty": 10,
            "broker_order_id": "2000",
            "broker_perm_id": "3000000",
            "payload": {
                "status": "Submitted",
                "limit_price": "502",
                "stop_loss": "498.50",
                "submitted_at": datetime(2026, 5, 5, 11, 45, tzinfo=timezone.utc).isoformat(),
            },
        }
        # Broker: T-new parent filled + T-new's stop at broker (no
        # T-old stop left). T-new's Filled status is provided via
        # broker_order_status so recovery classifies as pending_filled
        # rather than Q39-B's assume-fill path (2026-04-21 update).
        new_stop = BrokerOpenOrder(
            broker_order_id="2001",
            broker_perm_id="3000001",
            ticker="SPY",
            side="sell",
            qty=10,
            filled_qty=0,
            limit_price=Decimal("0"),
            status="Submitted",
            tif="GTC",
            client_tag="k2bi:spy-strat:T-new:stop",
            aux_price=Decimal("498.50"),
        )
        t_new_parent_filled = BrokerOrderStatusEvent(
            broker_order_id="2000",
            broker_perm_id="3000000",
            status="Filled",
            filled_qty=10,
            remaining_qty=0,
            avg_fill_price=Decimal("502"),
            last_update_at=datetime(
                2026, 5, 5, 11, 46, tzinfo=timezone.utc
            ),
        )
        result = recovery.reconcile(
            journal_tail=[prior_checkpoint, fresh_parent],
            broker_positions=[
                BrokerPosition(ticker="SPY", qty=10, avg_price=Decimal("502"))
            ],
            broker_open_orders=[new_stop],
            broker_order_status=[t_new_parent_filled],
            now=NOW,
        )
        self.assertEqual(result.status, recovery.RecoveryStatus.CATCH_UP)
        q31_cases = {
            "missing_protective_stop",
            "protective_stop_tag_mismatch",
            "protective_stop_price_drift",
        }
        q31_hits = [
            m for m in result.mismatch_reasons if m["case"] in q31_cases
        ]
        self.assertEqual(q31_hits, [])

    def test_q31_covers_crash_window_recovery_discovered_fill(self):
        """Codex Commit-2 R1 P1: a crash between order_proposed and
        order_submitted leaves the only stop_loss reference inside
        the recovery_reconciled event Phase A synthesizes from
        broker-status-history. Q31 must scan those events so the
        resulting adopted position is not left unprotected when the
        broker lost the stop."""
        tail = [
            {
                "ts": EARLIER.isoformat(),
                "event_type": "order_proposed",
                "trade_id": "T-crash",
                "journal_entry_id": "J1",
                "strategy": "spy-strat",
                "git_sha": "abc",
                "ticker": "SPY",
                "side": "buy",
                "qty": 10,
                "payload": {
                    "ticker": "SPY",
                    "side": "buy",
                    "qty": 10,
                    "limit_price": "500",
                    "stop_loss": "497.25",
                    "submitted_at": EARLIER.isoformat(),
                },
            }
        ]
        # Broker's terminal status for the parent (engine missed the
        # fill during the crash; Phase A will reconcile this to
        # pending_filled with journal_view carrying stop_loss).
        status = BrokerOrderStatusEvent(
            broker_order_id="1000",
            broker_perm_id="2000000",
            status="Filled",
            filled_qty=10,
            remaining_qty=0,
            avg_fill_price=Decimal("500.00"),
            last_update_at=EARLIER,
            client_tag="k2bi:spy-strat:T-crash",
        )
        # Broker state at restart: position exists, but stop child is
        # MISSING (e.g. cancelled during outage).
        result = recovery.reconcile(
            journal_tail=tail,
            broker_positions=[
                BrokerPosition(ticker="SPY", qty=10, avg_price=Decimal("500"))
            ],
            broker_open_orders=[],
            broker_order_status=[status],
            now=NOW,
        )
        self.assertEqual(
            result.status, recovery.RecoveryStatus.MISMATCH_REFUSED
        )
        missing = [
            m
            for m in result.mismatch_reasons
            if m["case"] == "missing_protective_stop"
        ]
        self.assertEqual(len(missing), 1)
        self.assertEqual(missing[0]["expected_trade_id"], "T-crash")

    def test_corrupt_checkpoint_trigger_price_fails_closed(self):
        """A non-Decimal value in checkpoint's trigger_price is
        treated as missing (fail-closed). The engine refuses to
        start; operator investigates the journal corruption."""
        tail = [
            {
                "ts": EARLIER.isoformat(),
                "event_type": "engine_recovered",
                "trade_id": None,
                "journal_entry_id": "J1",
                "strategy": None,
                "git_sha": "abc",
                "payload": {
                    "status": "catch_up",
                    "reconciled_event_count": 0,
                    "adopted_positions": [
                        {"ticker": "SPY", "qty": 10, "avg_price": "500"}
                    ],
                    "expected_stop_children": [
                        {
                            "ticker": "SPY",
                            "strategy": "spy-strat",
                            "parent_trade_id": "T1",
                            "client_tag": "spy-strat:T1:stop",
                            "trigger_price": "not-a-number",
                        }
                    ],
                },
            }
        ]
        # Even with a matching stop at broker, corrupt checkpoint
        # trigger fails closed.
        present_stop = BrokerOpenOrder(
            broker_order_id="1001",
            broker_perm_id="2000001",
            ticker="SPY",
            side="sell",
            qty=10,
            filled_qty=0,
            limit_price=Decimal("0"),
            status="Submitted",
            tif="GTC",
            client_tag="k2bi:spy-strat:T1:stop",
            aux_price=Decimal("497.25"),
        )
        result = recovery.reconcile(
            journal_tail=tail,
            broker_positions=[
                BrokerPosition(ticker="SPY", qty=10, avg_price=Decimal("500"))
            ],
            broker_open_orders=[present_stop],
            broker_order_status=[],
            now=NOW,
        )
        self.assertEqual(
            result.status, recovery.RecoveryStatus.MISMATCH_REFUSED
        )
        missing = [
            m
            for m in result.mismatch_reasons
            if m["case"] == "missing_protective_stop"
        ]
        self.assertEqual(len(missing), 1)
        self.assertIn("corrupt", missing[0].get("note", ""))


if __name__ == "__main__":
    unittest.main()
