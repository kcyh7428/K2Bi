"""Spec B Section 8.1 tests for post-fill journal durability."""

from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import execution.engine.main as engine_main
from execution.connectors.mock import MockIBKRConnector
from execution.connectors.types import (
    BrokerExecution,
    BrokerOrderStatusEvent,
    BrokerPosition,
)
from execution.engine.main import (
    DEFAULT_TICK_SECONDS,
    AwaitingOrderState,
    Engine,
    EngineConfig,
    EngineState,
    TickResult,
)
from execution.journal.writer import JournalWriter
from execution.validators.types import Order


CONFIG = {
    "position_size": {
        "max_trade_risk_pct": 0.01,
        "max_ticker_concentration_pct": 0.20,
    },
    "trade_risk": {"max_open_risk_pct": 0.05},
    "leverage": {"cash_only": True, "max_leverage": 1.0},
    "market_hours": {
        "regular_open": "09:30",
        "regular_close": "16:00",
        "allow_pre_market": False,
        "allow_after_hours": False,
    },
    "instrument_whitelist": {"symbols": ["SPY"]},
}


def _now() -> datetime:
    return datetime(2026, 5, 12, 13, 30, tzinfo=timezone.utc)


def _pending_order(
    *,
    trade_id: str = "T-durable",
    broker_order_id: str = "77",
    broker_perm_id: str = "770001",
    filled_qty: int = 10,
) -> AwaitingOrderState:
    submitted_at = _now() - timedelta(seconds=5)
    order = Order(
        ticker="SPY",
        side="buy",
        qty=10,
        limit_price=Decimal("500.00"),
        stop_loss=Decimal("495.00"),
        strategy="spy-rotational",
        submitted_at=submitted_at,
    )
    return AwaitingOrderState(
        trade_id=trade_id,
        strategy="spy-rotational",
        order=order,
        broker_order_id=broker_order_id,
        broker_perm_id=broker_perm_id,
        submitted_at=submitted_at,
        filled_qty=filled_qty,
    )


class EngineJournalDurabilityTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.base = Path(self._tmp.name)
        self.journal_dir = self.base / "journal"
        self.journal_dir.mkdir()
        self.strategies_dir = self.base / "strategies"
        self.strategies_dir.mkdir()
        self.kill_path = self.base / ".killed"
        self.connector = MockIBKRConnector()
        await self.connector.connect()
        self.journal = JournalWriter(base_dir=self.journal_dir, git_sha="test08")
        self.engine = Engine(
            connector=self.connector,
            journal=self.journal,
            validator_config=CONFIG,
            engine_config=EngineConfig(
                tick_seconds=DEFAULT_TICK_SECONDS,
                strategies_dir=self.strategies_dir,
                kill_path=self.kill_path,
            ),
        )
        self.engine.state = EngineState.AWAITING_FILL
        self.engine._init_completed = True

    async def asyncTearDown(self) -> None:
        self._tmp.cleanup()

    def _events(self, event_type: str) -> list[dict]:
        return [
            event
            for event in self.journal.read_all()
            if event["event_type"] == event_type
        ]

    def _set_mismatched_read_back(self) -> None:
        self.journal.read_back_last_event = lambda: {  # type: ignore[attr-defined]
            "event_type": "engine_started",
            "trade_id": "T-stale",
        }

    def _set_consistent_read_back_fallback(self) -> None:
        if not hasattr(self.journal, "read_back_last_event"):
            self.journal.read_back_last_event = (  # type: ignore[attr-defined]
                lambda: self.journal.read_all()[-1]
            )

    def _durability_error_type(self) -> type[BaseException]:
        return getattr(engine_main, "JournalDurabilityError", RuntimeError)

    async def test_d8_1_1_durability_mismatch_keeps_pending_order(self) -> None:
        pending = _pending_order()
        self.engine._pending_order = pending
        self.connector.positions = [
            BrokerPosition(ticker="SPY", qty=10, avg_price=Decimal("500.00"))
        ]
        self._set_mismatched_read_back()

        with self.assertRaises(self._durability_error_type()):
            await self.engine._reconcile_fill(pending, TickResult(self.engine.state, self.engine.state))

        self.assertIs(self.engine._pending_order, pending)

    async def test_d8_1_2_consistent_terminal_write_clears_pending(self) -> None:
        pending = _pending_order()
        self.engine._pending_order = pending
        self.connector.positions = [
            BrokerPosition(ticker="SPY", qty=10, avg_price=Decimal("500.00"))
        ]
        self._set_consistent_read_back_fallback()

        await self.engine._reconcile_fill(
            pending,
            TickResult(self.engine.state, self.engine.state),
        )

        terminals = self._events("order_terminal")
        self.assertEqual(len(terminals), 1)
        self.assertEqual(terminals[0]["trade_id"], pending.trade_id)
        self.assertEqual(terminals[0]["payload"]["terminal_status"], "Filled")
        self.assertIsNone(self.engine._pending_order)

    async def test_d8_1_3_append_failure_still_preserves_pending(self) -> None:
        pending = _pending_order()
        self.engine._pending_order = pending
        self.connector.positions = [
            BrokerPosition(ticker="SPY", qty=10, avg_price=Decimal("500.00"))
        ]

        def fail_append(*args, **kwargs):  # type: ignore[no-untyped-def]
            raise RuntimeError("journal append failed")

        self.journal.append = fail_append  # type: ignore[method-assign]

        with self.assertRaisesRegex(RuntimeError, "journal append failed"):
            await self.engine._reconcile_fill(
                pending,
                TickResult(self.engine.state, self.engine.state),
            )

        self.assertIs(self.engine._pending_order, pending)

    async def test_d8_1_4_terminal_reconcile_mismatch_keeps_pending_order(self) -> None:
        pending = _pending_order(filled_qty=0)
        self.engine._pending_order = pending
        self.connector.order_status_history = [
            BrokerOrderStatusEvent(
                broker_order_id=pending.broker_order_id,
                broker_perm_id=pending.broker_perm_id,
                status="Filled",
                filled_qty=10,
                remaining_qty=0,
                avg_fill_price=Decimal("500.00"),
                last_update_at=_now(),
            )
        ]
        self._set_mismatched_read_back()

        with self.assertRaises(self._durability_error_type()):
            await self.engine._reconcile_terminal(
                pending,
                TickResult(self.engine.state, self.engine.state),
            )

        self.assertIs(self.engine._pending_order, pending)

    async def test_d8_1_5_durability_error_stops_engine_with_pending_visible(
        self,
    ) -> None:
        pending = _pending_order()
        self.engine._pending_order = pending
        self.connector.executions_history = [
            BrokerExecution(
                exec_id="E-durable",
                broker_order_id=pending.broker_order_id,
                broker_perm_id=pending.broker_perm_id,
                ticker="SPY",
                side="buy",
                qty=10,
                price=Decimal("500.00"),
                filled_at=_now(),
            )
        ]
        self.connector.positions = [
            BrokerPosition(ticker="SPY", qty=10, avg_price=Decimal("500.00"))
        ]
        self._set_mismatched_read_back()

        await self.engine.tick_once()

        stopped = self._events("engine_stopped")
        self.assertEqual(len(stopped), 1)
        self.assertEqual(
            stopped[0]["payload"]["reason"],
            "journal_durability_failure",
        )
        self.assertEqual(stopped[0]["payload"]["pending_order"], pending.trade_id)
        self.assertIs(self.engine._pending_order, pending)
