"""Spec B §1 tests for broker-position-aware BUY skips."""

from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

from execution.connectors.mock import MockIBKRConnector
from execution.connectors.types import (
    BrokerPosition,
    ConnectorError,
    POSITION_SOURCE_LIVE_REQ_POSITIONS,
    PositionSnapshot,
)
from execution.engine.main import DEFAULT_TICK_SECONDS, Engine, EngineConfig, EngineState
from execution.journal.schema import ABORT_PHASE_PRE_SUBMIT_RECHECK
from execution.journal.writer import JournalWriter


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
    return datetime(2026, 4, 21, 10, 30, tzinfo=ET).astimezone(timezone.utc)


def _write_strategy(dir: Path, *, ticker: str = "SPY") -> Path:
    text = (
        "---\n"
        "name: spy-rotational\n"
        "status: approved\n"
        "strategy_type: hand_crafted\n"
        "risk_envelope_pct: 0.01\n"
        "approved_at: 2026-05-01T10:00:00Z\n"
        "approved_commit_sha: abc1234\n"
        "order:\n"
        f"  ticker: {ticker}\n"
        "  side: buy\n"
        "  qty: 10\n"
        "  limit_price: 500.00\n"
        "  stop_loss: 495.00\n"
        "  time_in_force: DAY\n"
        "---\n\n## How This Works\n\nPlain-English block.\n"
    )
    path = dir / "spy-rotational.md"
    path.write_text(text, encoding="utf-8")
    return path


class PositionAwareSkipTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.base = Path(self._tmp.name)
        self.journal_dir = self.base / "journal"
        self.journal_dir.mkdir()
        self.strategies_dir = self.base / "strategies"
        self.strategies_dir.mkdir()
        self.kill_path = self.base / ".killed"
        _write_strategy(self.strategies_dir)

        self.connector = MockIBKRConnector()
        self.connector.marks = {"SPY": Decimal("500")}
        self.journal = JournalWriter(base_dir=self.journal_dir, git_sha="test01")
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

    async def _init_engine(self) -> None:
        await self._patch_now(_mid_session_utc())
        tick = await self.engine.tick_once()
        self.assertEqual(tick.state_after, EngineState.CONNECTED_IDLE)

    def _events(self, event_type: str) -> list[dict]:
        return [
            event
            for event in self.journal.read_all()
            if event["event_type"] == event_type
        ]

    async def test_g1_existing_position_blocks_buy(self) -> None:
        await self._init_engine()
        self.connector.positions = [
            BrokerPosition(ticker="SPY", qty=10, avg_price=Decimal("500"))
        ]

        tick = await self.engine.tick_once()

        self.assertEqual(tick.orders_submitted, 0)
        self.assertEqual(tick.state_after, EngineState.CONNECTED_IDLE)
        self.assertEqual(len(self.connector.submitted_orders), 0)
        skips = self._events("cycle_evaluated_skip_position_held")
        self.assertEqual(len(skips), 1)
        self.assertEqual(skips[0]["strategy"], "spy-rotational")
        self.assertEqual(skips[0]["payload"]["symbol"], "SPY")
        self.assertEqual(skips[0]["payload"]["current_qty"], 10)
        self.assertEqual(skips[0]["payload"]["target_qty"], 10)

    async def test_g2_zero_position_permits_buy(self) -> None:
        await self._init_engine()
        self.connector.positions = []

        tick = await self.engine.tick_once()

        self.assertEqual(tick.orders_submitted, 1)
        self.assertEqual(len(self.connector.submitted_orders), 1)
        self.assertEqual(self.connector.submitted_orders[0].ticker, "SPY")

    async def test_g3_partial_holdings_skips_without_top_up(self) -> None:
        """STRICT semantics: partial holdings emit cycle_skipped_existing_position."""
        await self._init_engine()
        self.connector.positions = [
            BrokerPosition(ticker="SPY", qty=3, avg_price=Decimal("500"))
        ]

        tick = await self.engine.tick_once()

        self.assertEqual(tick.orders_submitted, 0)
        self.assertEqual(len(self.connector.submitted_orders), 0)
        skips = self._events("cycle_evaluated_skip_position_held")
        self.assertEqual(len(skips), 1)
        self.assertEqual(skips[0]["payload"]["symbol"], "SPY")
        self.assertEqual(skips[0]["payload"]["current_qty"], 3)
        self.assertEqual(skips[0]["payload"]["target_qty"], 10)

    async def test_g4_position_query_failure_fails_closed(self) -> None:
        await self._init_engine()

        async def fail_positions() -> PositionSnapshot:
            raise ConnectorError("position query unavailable")

        self.connector.get_positions = fail_positions  # type: ignore[method-assign]

        tick = await self.engine.tick_once()

        self.assertEqual(tick.state_after, EngineState.DISCONNECTED)
        self.assertEqual(self.engine.state, EngineState.DISCONNECTED)
        self.assertEqual(tick.orders_submitted, 0)
        self.assertEqual(len(self.connector.submitted_orders), 0)
        self.assertIsNone(self.engine._pending_order)
        self.assertEqual(self._events("order_proposed"), [])
        self.assertEqual(self._events("order_submitted"), [])
        failures = self._events("cycle_skipped_position_query_failed")
        self.assertEqual(failures, [])

    async def test_position_change_between_check_and_submit_blocks_buy(self) -> None:
        await self._init_engine()
        position_snapshots = [
            [],
            [BrokerPosition(ticker="SPY", qty=10, avg_price=Decimal("500"))],
        ]

        async def sequence_positions() -> PositionSnapshot:
            if position_snapshots:
                positions = position_snapshots.pop(0)
            else:
                positions = []
            return PositionSnapshot(
                positions=positions,
                valid=True,
                source=POSITION_SOURCE_LIVE_REQ_POSITIONS,
                fetched_at=datetime.now(timezone.utc),
            )

        self.connector.get_positions = sequence_positions  # type: ignore[method-assign]

        tick = await self.engine.tick_once()

        self.assertEqual(tick.orders_submitted, 0)
        self.assertEqual(len(self.connector.submitted_orders), 0)
        skips = self._events("cycle_skipped_existing_position")
        self.assertEqual(len(skips), 1)
        self.assertEqual(skips[0]["payload"]["current_qty"], 10)
        self.assertEqual(
            skips[0]["payload"]["position_state"], "at_target"
        )

    async def test_cycle_top_position_refresh_mutates_engine_position_cache(self) -> None:
        await self._init_engine()
        self.assertEqual(self.engine._positions, [])
        self.connector.positions = [
            BrokerPosition(ticker="SPY", qty=10, avg_price=Decimal("500"))
        ]

        await self.engine.tick_once()

        self.assertEqual(self.engine._positions, self.connector.positions)

    async def test_existing_position_skip_journals_normalized_symbol(self) -> None:
        await self._unpatch_now()
        self.strategies_dir.joinpath("spy-rotational.md").unlink()
        _write_strategy(self.strategies_dir, ticker="spy")
        await self._init_engine()
        self.connector.positions = [
            BrokerPosition(ticker="SPY", qty=10, avg_price=Decimal("500"))
        ]

        tick = await self.engine.tick_once()

        self.assertEqual(tick.orders_submitted, 0)
        self.assertEqual(len(self.connector.submitted_orders), 0)
        skips = self._events("cycle_evaluated_skip_position_held")
        self.assertEqual(len(skips), 1)
        self.assertEqual(skips[0]["payload"]["symbol"], "SPY")
        self.assertEqual(skips[0]["ticker"], "SPY")

    async def test_g4b_pre_submit_position_query_fails_closed(self) -> None:
        await self._init_engine()
        position_snapshots: list[list[BrokerPosition] | Exception] = [
            [],
            [],
            ConnectorError("pre-submit position query unavailable"),
        ]

        async def sequence_positions() -> PositionSnapshot:
            next_snapshot = position_snapshots.pop(0)
            if isinstance(next_snapshot, Exception):
                raise next_snapshot
            return PositionSnapshot(
                positions=next_snapshot,
                valid=True,
                source=POSITION_SOURCE_LIVE_REQ_POSITIONS,
                fetched_at=datetime.now(timezone.utc),
            )

        self.connector.get_positions = sequence_positions  # type: ignore[method-assign]

        tick = await self.engine.tick_once()

        self.assertEqual(tick.state_after, EngineState.CONNECTED_IDLE)
        self.assertEqual(tick.orders_submitted, 0)
        self.assertEqual(len(self.connector.submitted_orders), 0)
        self.assertIsNone(self.engine._pending_order)
        proposals = self._events("order_proposed")
        failures = self._events("cycle_skipped_position_query_failed")
        submissions = self._events("order_submitted")
        self.assertEqual(len(proposals), 1)
        self.assertEqual(len(failures), 1)
        self.assertEqual(len(submissions), 0)
        self.assertEqual(failures[0]["trade_id"], proposals[0]["trade_id"])
        self.assertEqual(
            failures[0]["payload"]["abort_phase"], ABORT_PHASE_PRE_SUBMIT_RECHECK
        )
        self.assertEqual(failures[0]["payload"]["symbol"], "SPY")
        self.assertEqual(
            failures[0]["payload"]["position_source"],
            POSITION_SOURCE_LIVE_REQ_POSITIONS,
        )
        self.assertTrue(failures[0]["payload"]["position_visibility_valid"])
        self.assertIn(
            "pre-submit position query unavailable",
            failures[0]["payload"]["error"],
        )
