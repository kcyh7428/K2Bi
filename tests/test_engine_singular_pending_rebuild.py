"""Spec B Section 8.2 tests for singular pending-order rebuild symmetry."""

from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from execution.connectors.mock import MockIBKRConnector
from execution.connectors.types import BrokerOpenOrder
from execution.engine.main import (
    DEFAULT_TICK_SECONDS,
    Engine,
    EngineConfig,
    EngineState,
)
from execution.journal.writer import JournalWriter


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


def _write_strategy(dir_path: Path) -> None:
    (dir_path / "spy-rotational.md").write_text(
        "---\n"
        "name: spy-rotational\n"
        "status: approved\n"
        "strategy_type: hand_crafted\n"
        "risk_envelope_pct: 0.01\n"
        "approved_at: 2026-05-01T10:00:00Z\n"
        "approved_commit_sha: abc1234\n"
        "order:\n"
        "  ticker: SPY\n"
        "  side: buy\n"
        "  qty: 10\n"
        "  limit_price: 500.00\n"
        "  stop_loss: 495.00\n"
        "---\n\n## How This Works\n\nPlain-English block.\n",
        encoding="utf-8",
    )


class EngineSingularPendingRebuildTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.base = Path(self._tmp.name)
        self.journal_dir = self.base / "journal"
        self.journal_dir.mkdir()
        self.strategies_dir = self.base / "strategies"
        self.strategies_dir.mkdir()
        _write_strategy(self.strategies_dir)
        self.kill_path = self.base / ".killed"
        self.connector = MockIBKRConnector()
        self.journal = JournalWriter(base_dir=self.journal_dir, git_sha="test82")

    async def asyncTearDown(self) -> None:
        self._tmp.cleanup()

    def _make_engine(self) -> Engine:
        return Engine(
            connector=self.connector,
            journal=self.journal,
            validator_config=CONFIG,
            engine_config=EngineConfig(
                tick_seconds=DEFAULT_TICK_SECONDS,
                strategies_dir=self.strategies_dir,
                kill_path=self.kill_path,
            ),
        )

    def _seed_submitted(
        self,
        *,
        trade_id: str = "T-singular",
        broker_order_id: str = "77",
        broker_perm_id: str = "770001",
    ) -> None:
        self.journal.append(
            "order_submitted",
            payload={
                "status": "Submitted",
                "limit_price": "500.00",
                "submitted_at": _now().isoformat(),
                "ticker": "SPY",
                "side": "buy",
                "qty": 10,
                "stop_loss": "495.00",
            },
            strategy="spy-rotational",
            trade_id=trade_id,
            ticker="SPY",
            side="buy",
            qty=10,
            broker_order_id=broker_order_id,
            broker_perm_id=broker_perm_id,
        )

    def _set_broker_parent_open(
        self,
        *,
        broker_order_id: str = "77",
        broker_perm_id: str = "770001",
    ) -> None:
        self.connector.open_orders = [
            BrokerOpenOrder(
                broker_order_id=broker_order_id,
                broker_perm_id=broker_perm_id,
                ticker="SPY",
                side="buy",
                qty=10,
                filled_qty=0,
                limit_price=Decimal("500.00"),
                status="Submitted",
                submitted_at=_now(),
                tif="DAY",
                client_tag="k2bi:spy-rotational:T-singular",
            )
        ]

    def _self_heal_events(self) -> list[dict]:
        return [
            event
            for event in self.journal.read_all()
            if event["event_type"] == "recovery_self_healed_pending_order"
        ]

    async def test_d8_2_1_order_terminal_self_heals_pending_order(self) -> None:
        self._seed_submitted()
        self.journal.append(
            "order_terminal",
            payload={"broker_order_id": "77", "terminal_status": "Filled"},
            strategy="spy-rotational",
            trade_id="T-singular",
            ticker="SPY",
            side="buy",
            qty=10,
            broker_order_id="77",
            broker_perm_id="770001",
        )
        self._set_broker_parent_open()

        engine = self._make_engine()
        tick = await engine.tick_once()

        self.assertEqual(tick.state_after, EngineState.CONNECTED_IDLE)
        self.assertIsNone(engine._pending_order)
        healed = self._self_heal_events()
        self.assertEqual(len(healed), 1)
        self.assertEqual(healed[0]["trade_id"], "T-singular")
        self.assertEqual(
            healed[0]["payload"]["terminal_event_type"],
            "order_terminal",
        )

    async def test_d8_2_2_order_filled_remaining_zero_self_heals(self) -> None:
        self._seed_submitted()
        self.journal.append(
            "order_filled",
            payload={"remaining_qty": 0},
            strategy="spy-rotational",
            trade_id="T-singular",
            ticker="SPY",
            side="buy",
            broker_order_id="77",
            broker_perm_id="770001",
        )
        self._set_broker_parent_open()

        engine = self._make_engine()
        tick = await engine.tick_once()

        self.assertEqual(tick.state_after, EngineState.CONNECTED_IDLE)
        self.assertIsNone(engine._pending_order)
        healed = self._self_heal_events()
        self.assertEqual(len(healed), 1)
        self.assertEqual(
            healed[0]["payload"]["terminal_event_type"],
            "order_filled",
        )

    async def test_d8_2_3_submitted_only_preserves_pending_order(self) -> None:
        self._seed_submitted()
        self._set_broker_parent_open()

        engine = self._make_engine()
        tick = await engine.tick_once()

        self.assertEqual(tick.state_after, EngineState.AWAITING_FILL)
        self.assertIsNotNone(engine._pending_order)
        self.assertEqual(engine._pending_order.trade_id, "T-singular")
        self.assertEqual(self._self_heal_events(), [])

    async def test_d8_2_4_order_timeout_self_heals_pending_order(self) -> None:
        self._seed_submitted()
        self.journal.append(
            "order_timeout",
            payload={"reason": "broker_status_Cancelled"},
            strategy="spy-rotational",
            trade_id="T-singular",
            ticker="SPY",
            side="buy",
            qty=10,
            broker_order_id="77",
            broker_perm_id="770001",
        )

        engine = self._make_engine()
        tick = await engine.tick_once()

        self.assertEqual(tick.state_after, EngineState.CONNECTED_IDLE)
        self.assertIsNone(engine._pending_order)
        healed = self._self_heal_events()
        self.assertEqual(len(healed), 1)
        self.assertEqual(
            healed[0]["payload"]["terminal_event_type"],
            "order_timeout",
        )

    async def test_d8_2_5_legacy_filled_shape_does_not_resume_pending(self) -> None:
        self._seed_submitted()
        self.journal.append(
            "order_filled",
            payload={"remaining_qty": 0},
            strategy="spy-rotational",
            trade_id="T-singular",
            ticker="SPY",
            side="buy",
            broker_order_id="77",
            broker_perm_id="770001",
        )

        engine = self._make_engine()
        tick = await engine.tick_once()

        self.assertEqual(tick.state_after, EngineState.CONNECTED_IDLE)
        self.assertIsNone(engine._pending_order)
        healed = self._self_heal_events()
        self.assertEqual(len(healed), 1)
        self.assertEqual(
            healed[0]["payload"]["heuristic_version"],
            "v1",
        )

        restart_engine = self._make_engine()
        restart_tick = await restart_engine.tick_once()

        self.assertEqual(restart_tick.state_after, EngineState.CONNECTED_IDLE)
        self.assertIsNone(restart_engine._pending_order)
        self.assertEqual(len(self._self_heal_events()), 1)
