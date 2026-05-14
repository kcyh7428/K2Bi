"""Spec B §8.3 tests for runner-side position-held observability."""

from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

from execution.connectors.mock import MockIBKRConnector
from execution.connectors.types import BrokerPosition
from execution.engine.main import DEFAULT_TICK_SECONDS, Engine, EngineConfig, EngineState
from execution.journal.writer import JournalWriter
from execution.strategies import runner as strategy_runner
from execution.strategies.types import MarketSnapshot
from execution.validators.types import Position, RiskContext


ET = ZoneInfo("US/Eastern")

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


def _mid_session_utc() -> datetime:
    return datetime(2026, 5, 12, 10, 30, tzinfo=ET).astimezone(timezone.utc)


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


class RunnerObservabilityTests(unittest.IsolatedAsyncioTestCase):
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
        self.connector.marks = {"SPY": Decimal("500")}
        self.journal = JournalWriter(base_dir=self.journal_dir, git_sha="test83")
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
        await self._patch_now(_mid_session_utc())
        tick = await self.engine.tick_once()
        self.assertEqual(tick.state_after, EngineState.CONNECTED_IDLE)

    async def asyncTearDown(self) -> None:
        await self._unpatch_now()
        self._tmp.cleanup()

    async def _patch_now(self, patched: datetime) -> None:
        import execution.connectors.mock as mock_mod
        import execution.engine.main as main_mod
        from datetime import datetime as real_dt

        class _PatchedDT(real_dt):
            @classmethod
            def now(cls, tz=None):
                return patched if tz is None else patched.astimezone(tz)

        self._orig_main_dt = getattr(self, "_orig_main_dt", main_mod.datetime)
        self._orig_mock_dt = getattr(self, "_orig_mock_dt", mock_mod.datetime)
        main_mod.datetime = _PatchedDT
        mock_mod.datetime = _PatchedDT

    async def _unpatch_now(self) -> None:
        import execution.connectors.mock as mock_mod
        import execution.engine.main as main_mod

        if hasattr(self, "_orig_main_dt"):
            main_mod.datetime = self._orig_main_dt
        if hasattr(self, "_orig_mock_dt"):
            mock_mod.datetime = self._orig_mock_dt

    def _events(self, event_type: str) -> list[dict]:
        return [
            event
            for event in self.journal.read_all()
            if event["event_type"] == event_type
        ]

    async def test_d8_3_1_position_held_emits_observability_event(self) -> None:
        self.connector.positions = [
            BrokerPosition(ticker="SPY", qty=10, avg_price=Decimal("500"))
        ]

        tick = await self.engine.tick_once()

        self.assertEqual(tick.orders_submitted, 0)
        self.assertEqual(tick.state_after, EngineState.CONNECTED_IDLE)
        self.assertEqual(self._events("order_proposed"), [])
        events = self._events("cycle_evaluated_skip_position_held")
        self.assertEqual(len(events), 1)
        event = events[0]
        self.assertEqual(event["strategy"], "spy-rotational")
        self.assertEqual(event["ticker"], "SPY")
        self.assertEqual(event["payload"]["strategy_id"], "spy-rotational")
        self.assertEqual(event["payload"]["symbol"], "SPY")
        self.assertEqual(event["payload"]["current_qty"], 10)
        self.assertEqual(event["payload"]["target_qty"], 10)
        self.assertTrue(event["payload"]["cycle_id"])
        self.assertIsNone(event["trade_id"])
        self.assertEqual(
            event["payload"]["evaluation_timestamp"],
            _mid_session_utc().isoformat(),
        )
        self.assertEqual(event["payload"]["position_source"], "live_reqPositions")
        self.assertEqual(event["payload"]["position_age_seconds"], 0.0)
        self.assertTrue(event["payload"]["position_visibility_valid"])

        second_tick = await self.engine.tick_once()

        self.assertEqual(second_tick.orders_submitted, 0)
        events = self._events("cycle_evaluated_skip_position_held")
        self.assertEqual(len(events), 2)
        self.assertNotEqual(
            events[0]["payload"]["cycle_id"],
            events[1]["payload"]["cycle_id"],
        )
        self.assertTrue(all(event["trade_id"] is None for event in events))

    async def test_d8_3_2_zero_position_does_not_emit_event(self) -> None:
        self.engine._positions = []

        tick = await self.engine.tick_once()

        self.assertEqual(tick.orders_submitted, 1)
        self.assertEqual(self._events("cycle_evaluated_skip_position_held"), [])

    async def test_observability_write_failure_logs_and_continues(self) -> None:
        self.connector.positions = [
            BrokerPosition(ticker="SPY", qty=10, avg_price=Decimal("500"))
        ]
        original_append = self.engine.journal.append

        def fail_skip_event(event_type: str, *args, **kwargs):
            if event_type == "cycle_evaluated_skip_position_held":
                raise OSError("disk full")
            return original_append(event_type, *args, **kwargs)

        self.engine.journal.append = fail_skip_event  # type: ignore[method-assign]

        with self.assertLogs("k2bi.engine", level="ERROR") as logs:
            tick = await self.engine.tick_once()

        self.assertEqual(tick.orders_submitted, 0)
        self.assertEqual(tick.state_after, EngineState.CONNECTED_IDLE)
        self.assertEqual(self._events("cycle_evaluated_skip_position_held"), [])
        self.assertTrue(
            any(
                "runner position-held observability write failed: disk full" in line
                for line in logs.output
            )
        )

    async def test_malformed_skip_detail_logs_and_writes_no_event(self) -> None:
        snap = self.engine._strategies[0]
        ctx = RiskContext(
            account_value=Decimal("1000000"),
            cash=Decimal("1000000"),
            positions=[
                Position(ticker="SPY", qty=10, avg_price=Decimal("500"))
            ],
            now=_mid_session_utc(),
        )
        market = MarketSnapshot(
            ts=_mid_session_utc(),
            marks={"SPY": Decimal("500")},
            account_value=Decimal("1000000"),
        )
        decision = strategy_runner.EvaluationDecision(
            candidate=None,
            reason=strategy_runner.SKIP_POSITION_HELD,
            detail={},
        )

        with self.assertLogs("k2bi.engine", level="ERROR") as logs:
            ok = self.engine._journal_runner_position_held_skip(
                snap=snap,
                ctx=ctx,
                market=market,
                decision=decision,
            )

        self.assertFalse(ok)
        self.assertEqual(self._events("cycle_evaluated_skip_position_held"), [])
        self.assertTrue(
            any(
                "runner position-held skip detail malformed" in line
                for line in logs.output
            )
        )

    async def test_d8_3_3_partial_position_emits_observability_event(self) -> None:
        self.connector.positions = [
            BrokerPosition(ticker="SPY", qty=3, avg_price=Decimal("500"))
        ]

        tick = await self.engine.tick_once()

        self.assertEqual(tick.orders_submitted, 0)
        events = self._events("cycle_evaluated_skip_position_held")
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["payload"]["current_qty"], 3)
        self.assertEqual(events[0]["payload"]["target_qty"], 10)
