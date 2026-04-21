"""Tests for Q39 hybrid recovery primitives.

Q39 (architect scope 2026-04-21): IB Gateway's broker-API has limited
historical visibility after Gateway restart. Recovery cannot rely on
`reqExecutions` / `reqCompletedOrders` alone to discover historical
fills. The hybrid rule:

    1. Journal is authoritative for last-known broker state.
    2. orderRef/client_tag joins broker-visible records to journal
       trade_ids when broker visibility allows.
    3. When journal has a submitted order with broker_perm_id but broker
       visibility shows nothing, assume the order filled. Emit a new
       reconciliation case `pending_no_broker_counterpart_assumed_filled`
       so the next restart's recovery treats it as terminal AND
       projects the fill into the adopted-position ledger.
    4. Divergence detection: if broker later contradicts the assumed
       fill (no position, no trace), Phase B flags it as a genuine
       mismatch -- operator decides.

Q39-A primitive layer (this file, first cluster):
    - `_pending_from_journal.terminal_cases` recognizes the new case.
    - `_positions_from_journal` projects the assumed fill using
      top-level `filled_qty` + `avg_fill_price` in the event payload.

Q39-B emission layer + Q39-C divergence detection live in follow-up
test classes/commits (same file) to keep the Codex review chunked.
"""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from execution.connectors.types import (
    BrokerOpenOrder,
    BrokerOrderStatusEvent,
    BrokerPosition,
)
from execution.engine import recovery


NOW = datetime(2026, 5, 5, 12, 0, tzinfo=timezone.utc)
EARLIER = datetime(2026, 5, 5, 11, 30, tzinfo=timezone.utc)
LATER = datetime(2026, 5, 5, 11, 45, tzinfo=timezone.utc)


def _order_submitted_record(
    *,
    trade_id: str = "T1",
    strategy: str = "spy-rotational",
    broker_order_id: str = "1000",
    broker_perm_id: str = "2000000",
    ticker: str = "SPY",
    side: str = "buy",
    qty: int = 10,
    limit_price: str | None = "500",
    stop_loss: str | None = None,
    ts: datetime = EARLIER,
) -> dict:
    return {
        "ts": ts.isoformat(),
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
            "stop_loss": stop_loss,
            "submitted_at": ts.isoformat(),
        },
    }


def _assumed_filled_record(
    *,
    trade_id: str = "T1",
    strategy: str = "spy-rotational",
    broker_order_id: str = "1000",
    broker_perm_id: str = "2000000",
    ticker: str = "SPY",
    side: str = "buy",
    qty: int = 10,
    assumed_fill_price: str = "500",
    evidence: str = "crash_gap",
    ts: datetime = LATER,
    parent_stop_loss: str | None = None,
) -> dict:
    return {
        "ts": ts.isoformat(),
        "event_type": "recovery_reconciled",
        "trade_id": trade_id,
        "journal_entry_id": "J2",
        "strategy": strategy,
        "git_sha": "abc",
        "broker_order_id": broker_order_id,
        "broker_perm_id": broker_perm_id,
        "ticker": ticker,
        "payload": {
            "case": "pending_no_broker_counterpart_assumed_filled",
            "evidence": evidence,
            "note": (
                "journal had broker_perm_id; broker visibility limited "
                "(Q39); defaulting to assume fill"
            ),
            "journal_view": {
                "ticker": ticker,
                "side": side,
                "qty": qty,
                "limit_price": assumed_fill_price,
                "stop_loss": parent_stop_loss,
                "submitted_at": (ts - timedelta(minutes=15)).isoformat(),
            },
            "filled_qty": qty,
            "avg_fill_price": assumed_fill_price,
        },
    }


class Q39APrimitiveRecognitionTests(unittest.TestCase):
    """Q39-A: once a recovery_reconciled(case=
    pending_no_broker_counterpart_assumed_filled) event is written to
    the journal, subsequent recovery replays MUST:

        1. Treat it as terminal for its trade_id -- do not re-emit
           pending_no_broker_counterpart for the same order.
        2. Project the assumed fill into _positions_from_journal so
           the engine's adopted-position ledger matches what the broker
           actually holds (assuming the hypothesis stands).

    Neither helper is public, so we drive both via reconcile() with
    inputs crafted to isolate the primitive behavior.
    """

    def test_terminal_case_clears_pending_on_replay(self):
        """After the assumed-filled event is journaled, a subsequent
        reconcile must NOT emit pending_no_broker_counterpart (or the
        new case itself) for the same trade_id -- the prior terminal
        decision stands."""
        tail = [
            _order_submitted_record(),
            _assumed_filled_record(),
        ]
        result = recovery.reconcile(
            journal_tail=tail,
            broker_positions=[
                BrokerPosition(ticker="SPY", qty=10, avg_price=Decimal("500")),
            ],
            broker_open_orders=[],
            broker_order_status=[],
            now=NOW,
        )
        cases = [e.payload.get("case") for e in result.events]
        self.assertNotIn("pending_no_broker_counterpart", cases)
        self.assertNotIn(
            "pending_no_broker_counterpart_assumed_filled", cases
        )

    def test_assumed_fill_projects_position_for_diff(self):
        """The assumed-filled event must feed through
        _positions_from_journal so Phase B.2 sees the broker's matching
        position as consistent -- no phantom_position mismatch."""
        tail = [
            _order_submitted_record(),
            _assumed_filled_record(),
        ]
        result = recovery.reconcile(
            journal_tail=tail,
            broker_positions=[
                BrokerPosition(ticker="SPY", qty=10, avg_price=Decimal("500")),
            ],
            broker_open_orders=[],
            broker_order_status=[],
            now=NOW,
        )
        self.assertEqual(result.mismatch_reasons, [])
        self.assertEqual(result.status, recovery.RecoveryStatus.CATCH_UP)

    def test_assumed_fill_with_broker_flat_surfaces_divergence(self):
        """If broker is flat but journal says we assumed a fill, Phase
        B.2's journal_position_missing_at_broker must fire -- recovery
        refuses to start without operator review. This is Q39's
        divergence detection (the next-reconnect case where the
        optimistic assumption was wrong)."""
        tail = [
            _order_submitted_record(),
            _assumed_filled_record(),
        ]
        result = recovery.reconcile(
            journal_tail=tail,
            broker_positions=[],
            broker_open_orders=[],
            broker_order_status=[],
            now=NOW,
        )
        self.assertEqual(
            result.status, recovery.RecoveryStatus.MISMATCH_REFUSED
        )
        cases = [m.get("case") for m in result.mismatch_reasons]
        self.assertIn("journal_position_missing_at_broker", cases)

    def test_evidence_barrier_timeout_recognized_identically(self):
        """evidence=barrier_timeout vs evidence=crash_gap both
        recognize the same case-name terminal. The evidence field is
        audit-trail metadata, not a branch point in recovery logic."""
        tail = [
            _order_submitted_record(),
            _assumed_filled_record(evidence="barrier_timeout"),
        ]
        result = recovery.reconcile(
            journal_tail=tail,
            broker_positions=[
                BrokerPosition(ticker="SPY", qty=10, avg_price=Decimal("500")),
            ],
            broker_open_orders=[],
            broker_order_status=[],
            now=NOW,
        )
        self.assertEqual(result.mismatch_reasons, [])
        self.assertEqual(result.status, recovery.RecoveryStatus.CATCH_UP)

    def test_assumed_fill_avg_price_feeds_position_ledger(self):
        """_positions_from_journal must source avg_fill_price from the
        event's top-level payload so multi-leg averages compute
        correctly. A later real fill on the same ticker should average
        against the assumed-fill price, not treat it as zero-cost."""
        # Two trades on same ticker: T1 assumed filled @ 500, T2 a
        # live fill @ 510. Expected projected avg = (500*10 + 510*10)/20
        # = 505.
        tail = [
            _order_submitted_record(trade_id="T1", broker_perm_id="P1"),
            _assumed_filled_record(trade_id="T1", broker_perm_id="P1"),
            {
                "ts": (LATER + timedelta(minutes=5)).isoformat(),
                "event_type": "order_filled",
                "trade_id": "T2",
                "journal_entry_id": "J3",
                "strategy": "spy-rotational",
                "git_sha": "abc",
                "broker_order_id": "1001",
                "broker_perm_id": "P2",
                "ticker": "SPY",
                "side": "buy",
                "qty": 10,
                "payload": {
                    "ticker": "SPY",
                    "side": "buy",
                    "fill_qty": 10,
                    "cumulative_filled_qty": 10,
                    "fill_price": "510",
                    "filled_at": (LATER + timedelta(minutes=5)).isoformat(),
                    "remaining_qty": 0,
                    "exec_id": "E2",
                },
            },
        ]
        # Broker has 20 shares matching the combined projection.
        result = recovery.reconcile(
            journal_tail=tail,
            broker_positions=[
                BrokerPosition(ticker="SPY", qty=20, avg_price=Decimal("505")),
            ],
            broker_open_orders=[],
            broker_order_status=[],
            now=NOW,
        )
        # No phantom / undersize / oversize -- quantity matches.
        undersize_or_oversize = [
            m.get("case")
            for m in result.mismatch_reasons
            if m.get("case")
            in {
                "position_oversized_vs_journal",
                "position_undersized_vs_journal",
                "phantom_position",
                "journal_position_missing_at_broker",
            }
        ]
        self.assertEqual(undersize_or_oversize, [])
        # avg_price drift is an event, not a mismatch. Confirm no drift.
        drift_events = [
            e for e in result.events if e.event_type == "avg_price_drift"
        ]
        self.assertEqual(drift_events, [])


def _once_exit_barrier_timeout_record(
    *,
    trade_ids: list[str],
    barrier_seconds_elapsed: float = 10.0,
    last_known_state: str = "AWAITING_FILL",
    ts: datetime = LATER,
) -> dict:
    """Build an `once_exit_barrier_timeout` journal record per architect
    spec (2026-04-21): payload carries pending_orders list; trade_id
    identifies each order the barrier was waiting on."""
    return {
        "ts": ts.isoformat(),
        "event_type": "once_exit_barrier_timeout",
        "journal_entry_id": "J-BARRIER",
        "git_sha": "abc",
        "payload": {
            "barrier_seconds_elapsed": barrier_seconds_elapsed,
            "last_known_state": last_known_state,
            "pending_orders": [
                {
                    "trade_id": tid,
                    "broker_order_id": "1000",
                    "broker_perm_id": "2000000",
                    "ticker": "SPY",
                    "side": "buy",
                    "qty": 10,
                    "limit_price": "500",
                    "stop_loss": None,
                }
                for tid in trade_ids
            ],
        },
    }


class Q39BEmissionTests(unittest.TestCase):
    """Q39-B: reconcile() must emit the new case at the right branch
    AND feed a synthetic fill into projected positions so Phase B.2 is
    consistent with the journal's last-known intent.

    Decision tree for a journal-pending order with no broker match:

        perm_id absent              -> pending_no_broker_counterpart
                                       (original case; order never
                                        reached broker; no position
                                        projected)
        perm_id present, no barrier -> _assumed_filled, evidence=crash_gap
                                       (engine crashed after broker ack;
                                        best-guess fill at limit_price;
                                        position projected)
        perm_id present, barrier    -> _assumed_filled, evidence=barrier_timeout
                                       (engine deliberately exited mid-
                                        wait per Q33; strongest evidence
                                        the order was live at broker)
    """

    def test_assumed_filled_emitted_when_perm_id_present_and_broker_flat(self):
        """Broker-perm-id presence is the evidence threshold for the
        assume-fill path. Broker position added to avoid Phase B.2
        divergence (that path is tested in the primitive class)."""
        tail = [_order_submitted_record(broker_perm_id="2000000")]
        result = recovery.reconcile(
            journal_tail=tail,
            broker_positions=[
                BrokerPosition(ticker="SPY", qty=10, avg_price=Decimal("500")),
            ],
            broker_open_orders=[],
            broker_order_status=[],
            now=NOW,
        )
        cases = [e.payload.get("case") for e in result.events]
        self.assertIn(
            "pending_no_broker_counterpart_assumed_filled", cases
        )
        self.assertNotIn("pending_no_broker_counterpart", cases)

    def test_original_case_preserved_when_perm_id_absent(self):
        """Without broker_perm_id, the journal has no evidence the
        broker accepted the order -- engine might have crashed pre-
        submit or the submit transport failed. Keep the original
        pending_no_broker_counterpart case (clean catch-up)."""
        tail = [_order_submitted_record(broker_perm_id="")]
        result = recovery.reconcile(
            journal_tail=tail,
            broker_positions=[],
            broker_open_orders=[],
            broker_order_status=[],
            now=NOW,
        )
        cases = [e.payload.get("case") for e in result.events]
        self.assertIn("pending_no_broker_counterpart", cases)
        self.assertNotIn(
            "pending_no_broker_counterpart_assumed_filled", cases
        )
        self.assertEqual(result.status, recovery.RecoveryStatus.CATCH_UP)

    def test_evidence_crash_gap_when_no_barrier_event_for_trade_id(self):
        """Default evidence is crash_gap: engine terminated outside the
        Q33 barrier path, so the only signal we have is that the order
        was journaled with a broker_perm_id."""
        tail = [_order_submitted_record(broker_perm_id="2000000")]
        result = recovery.reconcile(
            journal_tail=tail,
            broker_positions=[
                BrokerPosition(ticker="SPY", qty=10, avg_price=Decimal("500")),
            ],
            broker_open_orders=[],
            broker_order_status=[],
            now=NOW,
        )
        assumed_events = [
            e for e in result.events
            if e.payload.get("case")
            == "pending_no_broker_counterpart_assumed_filled"
        ]
        self.assertEqual(len(assumed_events), 1)
        self.assertEqual(
            assumed_events[0].payload.get("evidence"), "crash_gap"
        )

    def test_evidence_barrier_timeout_when_barrier_event_references_trade_id(
        self,
    ):
        """Q33 pairs with Q39-B: when the engine's --once barrier
        timed out waiting on this specific trade_id, the journal
        carries the strongest evidence possible for assume-fill."""
        tail = [
            _order_submitted_record(trade_id="T1", broker_perm_id="2000000"),
            _once_exit_barrier_timeout_record(trade_ids=["T1"]),
        ]
        result = recovery.reconcile(
            journal_tail=tail,
            broker_positions=[
                BrokerPosition(ticker="SPY", qty=10, avg_price=Decimal("500")),
            ],
            broker_open_orders=[],
            broker_order_status=[],
            now=NOW,
        )
        assumed_events = [
            e for e in result.events
            if e.payload.get("case")
            == "pending_no_broker_counterpart_assumed_filled"
        ]
        self.assertEqual(len(assumed_events), 1)
        self.assertEqual(
            assumed_events[0].payload.get("evidence"),
            "barrier_timeout",
        )

    def test_barrier_event_for_different_trade_id_does_not_promote_evidence(
        self,
    ):
        """Barrier must reference THIS trade_id to count as evidence.
        A barrier event for a different pending order leaves evidence
        at crash_gap."""
        tail = [
            _order_submitted_record(trade_id="T1", broker_perm_id="2000000"),
            _once_exit_barrier_timeout_record(trade_ids=["T99"]),
        ]
        result = recovery.reconcile(
            journal_tail=tail,
            broker_positions=[
                BrokerPosition(ticker="SPY", qty=10, avg_price=Decimal("500")),
            ],
            broker_open_orders=[],
            broker_order_status=[],
            now=NOW,
        )
        assumed_events = [
            e for e in result.events
            if e.payload.get("case")
            == "pending_no_broker_counterpart_assumed_filled"
        ]
        self.assertEqual(len(assumed_events), 1)
        self.assertEqual(
            assumed_events[0].payload.get("evidence"), "crash_gap"
        )

    def test_emitted_event_carries_synthetic_fill_top_level_fields(self):
        """The synthetic-fill triple (filled_qty, avg_fill_price,
        journal_view) must be present so downstream replays feed the
        same shape the primitive layer recognizes."""
        tail = [_order_submitted_record(broker_perm_id="2000000")]
        result = recovery.reconcile(
            journal_tail=tail,
            broker_positions=[
                BrokerPosition(ticker="SPY", qty=10, avg_price=Decimal("500")),
            ],
            broker_open_orders=[],
            broker_order_status=[],
            now=NOW,
        )
        assumed = [
            e for e in result.events
            if e.payload.get("case")
            == "pending_no_broker_counterpart_assumed_filled"
        ][0]
        self.assertEqual(assumed.payload.get("filled_qty"), 10)
        self.assertEqual(assumed.payload.get("avg_fill_price"), "500")
        journal_view = assumed.payload.get("journal_view", {})
        self.assertEqual(journal_view.get("ticker"), "SPY")
        self.assertEqual(journal_view.get("side"), "buy")
        self.assertEqual(journal_view.get("qty"), 10)

    def test_emitted_event_journal_view_preserves_stop_loss(self):
        """Protective-stop context must travel journal -> assumed-fill
        event so Q31 invariants fire correctly on the next reconnect."""
        tail = [
            _order_submitted_record(
                broker_perm_id="2000000", stop_loss="495"
            )
        ]
        result = recovery.reconcile(
            journal_tail=tail,
            broker_positions=[
                BrokerPosition(ticker="SPY", qty=10, avg_price=Decimal("500")),
            ],
            broker_open_orders=[],
            broker_order_status=[],
            now=NOW,
        )
        assumed = [
            e for e in result.events
            if e.payload.get("case")
            == "pending_no_broker_counterpart_assumed_filled"
        ][0]
        self.assertEqual(
            assumed.payload.get("journal_view", {}).get("stop_loss"),
            "495",
        )

    def test_synthetic_fill_propagates_to_reconciliation_deltas(self):
        """The assume-fill must not just emit an event; it must also
        feed reconciliation_deltas so Phase B.2's projected position
        matches the broker. Without the delta pass, broker shows SPY
        but projected has nothing -> phantom_position mismatch."""
        tail = [_order_submitted_record(broker_perm_id="2000000")]
        result = recovery.reconcile(
            journal_tail=tail,
            broker_positions=[
                BrokerPosition(ticker="SPY", qty=10, avg_price=Decimal("500")),
            ],
            broker_open_orders=[],
            broker_order_status=[],
            now=NOW,
        )
        self.assertEqual(result.mismatch_reasons, [])
        self.assertEqual(result.status, recovery.RecoveryStatus.CATCH_UP)

    def test_assume_filled_with_broker_flat_refuses_start(self):
        """Q39 divergence: journal projects an assumed fill, broker is
        flat. Phase B.2 fires journal_position_missing_at_broker and
        recovery refuses to start. Operator reviews before relaunch."""
        tail = [_order_submitted_record(broker_perm_id="2000000")]
        result = recovery.reconcile(
            journal_tail=tail,
            broker_positions=[],
            broker_open_orders=[],
            broker_order_status=[],
            now=NOW,
        )
        cases = [e.payload.get("case") for e in result.events]
        self.assertIn(
            "pending_no_broker_counterpart_assumed_filled", cases
        )
        self.assertEqual(
            result.status, recovery.RecoveryStatus.MISMATCH_REFUSED
        )
        mismatch_cases = [
            m.get("case") for m in result.mismatch_reasons
        ]
        self.assertIn(
            "journal_position_missing_at_broker", mismatch_cases
        )


