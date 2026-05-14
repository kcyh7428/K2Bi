"""Spec B §9.1 tests for position cache coherence."""

from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

from execution.connectors.mock import MockIBKRConnector
from execution.connectors.types import (
    BrokerPosition,
    DisconnectedError,
    POSITION_SOURCE_DISCONNECTED,
    POSITION_SOURCE_LIVE_REQ_POSITIONS,
    POSITION_SOURCE_TIMEOUT_FALLBACK,
    PositionSnapshot,
)
from execution.engine import recovery
from execution.engine.main import DEFAULT_TICK_SECONDS, Engine, EngineConfig, EngineState
from execution.journal.schema import ABORT_PHASE_PRE_SUBMIT_RECHECK
from execution.journal.writer import JournalWriter
from execution.strategies import loader as strategy_loader


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
    return datetime(2026, 5, 14, 10, 30, tzinfo=ET).astimezone(timezone.utc)


def _position(qty: int) -> BrokerPosition:
    return BrokerPosition(ticker="SPY", qty=qty, avg_price=Decimal("500.00"))


def _snapshot(
    positions: list[BrokerPosition],
    *,
    valid: bool = True,
    source: str = POSITION_SOURCE_LIVE_REQ_POSITIONS,
    fetched_at: datetime | None = None,
) -> PositionSnapshot:
    return PositionSnapshot(
        positions=positions,
        valid=valid,
        source=source,  # type: ignore[arg-type]
        fetched_at=fetched_at if valid else None,
    )


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
        "  time_in_force: DAY\n"
        "---\n\n## How This Works\n\nPlain-English block.\n",
        encoding="utf-8",
    )


async def _no_sleep(_: float) -> None:
    return None


class _SequencedPositionConnector(MockIBKRConnector):
    def __init__(self, snapshots: list[PositionSnapshot]) -> None:
        super().__init__()
        self._snapshots = list(snapshots)
        self.position_calls = 0

    async def get_positions(self) -> PositionSnapshot:
        self._require_connected()
        self.position_calls += 1
        if self._snapshots:
            return self._snapshots.pop(0)
        return _snapshot([])


class PositionCacheCoherenceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.base = Path(self._tmp.name)
        self.journal_dir = self.base / "journal"
        self.journal_dir.mkdir()
        self.strategies_dir = self.base / "strategies"
        self.strategies_dir.mkdir()
        _write_strategy(self.strategies_dir)
        self.kill_path = self.base / ".killed"

    async def asyncTearDown(self) -> None:
        await self._unpatch_now()
        self._tmp.cleanup()

    async def _patch_now(self, patched: datetime) -> None:
        import execution.engine.main as main_mod
        from datetime import datetime as real_dt

        class _PatchedDT(real_dt):
            @classmethod
            def now(cls, tz=None):
                return patched if tz is None else patched.astimezone(tz)

        self._orig_main_dt = getattr(self, "_orig_main_dt", main_mod.datetime)
        main_mod.datetime = _PatchedDT

    async def _unpatch_now(self) -> None:
        import execution.engine.main as main_mod

        if hasattr(self, "_orig_main_dt"):
            main_mod.datetime = self._orig_main_dt

    def _make_engine(self, connector: MockIBKRConnector) -> Engine:
        connector.marks = {"SPY": Decimal("500.00")}
        journal = JournalWriter(base_dir=self.journal_dir, git_sha="test91")
        self.journal = journal
        return Engine(
            connector=connector,
            journal=journal,
            validator_config=CONFIG,
            engine_config=EngineConfig(
                tick_seconds=DEFAULT_TICK_SECONDS,
                strategies_dir=self.strategies_dir,
                kill_path=self.kill_path,
            ),
        )

    def _strategy_snapshot(self):
        return strategy_loader.load_all_approved(self.strategies_dir)[0]

    async def _init_engine(self, engine: Engine) -> None:
        await self._patch_now(_mid_session_utc())
        tick = await engine.tick_once()
        self.assertEqual(tick.state_after, EngineState.CONNECTED_IDLE)

    def _events(self, event_type: str) -> list[dict]:
        return [
            event
            for event in self.journal.read_all()
            if event["event_type"] == event_type
        ]

    async def _assert_invalid_pre_submit_snapshot_clears_order_proposal(
        self,
        *,
        position_source: str,
    ) -> None:
        trade_id = "pre-submit-invalid-position-visibility"
        connector = _SequencedPositionConnector(
            [_snapshot([], valid=False, source=position_source)]
        )
        connector.set_connected(True)
        engine = self._make_engine(connector)
        snap = self._strategy_snapshot()
        self.journal.append(
            "order_proposed",
            payload={
                "ticker": "SPY",
                "side": "buy",
                "qty": 10,
                "order_type": "LMT",
                "limit_price": "500.00",
                "stop_loss": "495.00",
                "time_in_force": "DAY",
                "validators": [],
                "strategy_sha256": snap.source_sha256,
                "strategy_approved_commit": snap.approved_commit_sha,
            },
            strategy=snap.name,
            trade_id=trade_id,
            ticker="SPY",
            side="buy",
            qty=10,
            ts=_mid_session_utc(),
        )

        with self.assertRaises(DisconnectedError):
            await engine._skip_buy_for_existing_position(
                snap=snap,
                symbol="SPY",
                side="buy",
                target_qty=10,
                trade_id=trade_id,
                abort_phase=ABORT_PHASE_PRE_SUBMIT_RECHECK,
            )

        failures = self._events("cycle_skipped_position_query_failed")
        self.assertEqual(len(failures), 1)
        payload = failures[0]["payload"]
        self.assertEqual(payload["abort_phase"], ABORT_PHASE_PRE_SUBMIT_RECHECK)
        self.assertEqual(payload["position_source"], position_source)
        self.assertFalse(payload["position_visibility_valid"])
        result = recovery.reconcile(
            journal_tail=self.journal.read_all(),
            broker_positions=[],
            broker_open_orders=[],
            broker_order_status=[],
            now=_mid_session_utc(),
        )
        pending_events = [
            event
            for event in result.events
            if event.event_type == "recovery_reconciled"
            and event.payload.get("case") == "pending_no_broker_counterpart"
        ]
        self.assertEqual(pending_events, [])
        self.assertEqual(result.status, recovery.RecoveryStatus.CLEAN)

    async def test_round4_invalid_timeout_fallback_pre_submit_snapshot_clears_proposal(
        self,
    ) -> None:
        await self._assert_invalid_pre_submit_snapshot_clears_order_proposal(
            position_source=POSITION_SOURCE_TIMEOUT_FALLBACK
        )

    async def test_round4_invalid_disconnected_pre_submit_snapshot_clears_proposal(
        self,
    ) -> None:
        await self._assert_invalid_pre_submit_snapshot_clears_order_proposal(
            position_source=POSITION_SOURCE_DISCONNECTED
        )

    def test_p7_timeout_fallback_snapshot_cannot_claim_valid_visibility(self) -> None:
        with self.assertRaisesRegex(
            ValueError,
            "PositionSnapshot invariant violated",
        ):
            PositionSnapshot(
                positions=[],
                valid=True,
                source=POSITION_SOURCE_TIMEOUT_FALLBACK,
                fetched_at=_mid_session_utc(),
            )

    async def test_p1_cycle_top_refresh_replaces_stale_position_cache(self) -> None:
        connector = _SequencedPositionConnector(
            [
                _snapshot([], fetched_at=_mid_session_utc()),
                _snapshot([], fetched_at=_mid_session_utc()),
                _snapshot([], fetched_at=_mid_session_utc()),
            ]
        )
        engine = self._make_engine(connector)
        await self._init_engine(engine)
        engine._positions = [_position(10)]

        tick = await engine.tick_once()

        self.assertEqual(engine._positions, [])
        self.assertEqual(tick.orders_submitted, 1)
        self.assertEqual(len(connector.submitted_orders), 1)

    async def test_p2_invalid_snapshot_journals_visibility_lost_and_skips(self) -> None:
        connector = _SequencedPositionConnector(
            [
                _snapshot([], fetched_at=_mid_session_utc()),
                _snapshot(
                    [],
                    valid=False,
                    source=POSITION_SOURCE_TIMEOUT_FALLBACK,
                ),
            ]
        )
        engine = self._make_engine(connector)
        await self._init_engine(engine)
        engine._positions = [_position(10)]

        tick = await engine.tick_once()

        self.assertEqual(tick.orders_submitted, 0)
        self.assertEqual(engine._positions, [_position(10)])
        self.assertEqual(connector.submitted_orders, [])
        events = self._events("position_visibility_lost")
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["payload"]["source"], POSITION_SOURCE_TIMEOUT_FALLBACK)
        self.assertIsNotNone(events[0]["payload"]["last_valid_age_seconds"])

    async def test_p3_invalid_snapshot_does_not_crash_and_next_valid_cycle_retries(
        self,
    ) -> None:
        connector = _SequencedPositionConnector(
            [
                _snapshot([], fetched_at=_mid_session_utc()),
                _snapshot([], valid=False, source=POSITION_SOURCE_TIMEOUT_FALLBACK),
                _snapshot([], fetched_at=_mid_session_utc() + timedelta(seconds=30)),
                _snapshot([], fetched_at=_mid_session_utc() + timedelta(seconds=30)),
            ]
        )
        engine = self._make_engine(connector)
        await self._init_engine(engine)

        invalid_tick = await engine.tick_once()
        next_tick = await engine.tick_once()

        self.assertEqual(invalid_tick.state_after, EngineState.CONNECTED_IDLE)
        self.assertEqual(invalid_tick.orders_submitted, 0)
        self.assertEqual(next_tick.orders_submitted, 1)
        self.assertEqual(len(connector.submitted_orders), 1)

    async def test_p3b_disconnected_snapshot_enters_reconnect_path(self) -> None:
        connector = _SequencedPositionConnector(
            [
                _snapshot([], fetched_at=_mid_session_utc()),
                _snapshot([], valid=False, source=POSITION_SOURCE_DISCONNECTED),
                _snapshot([], fetched_at=_mid_session_utc() + timedelta(seconds=30)),
            ]
        )
        engine = self._make_engine(connector)
        await self._init_engine(engine)

        disconnected_tick = await engine.tick_once()
        with patch("execution.engine.main.asyncio.sleep", _no_sleep):
            reconnect_tick = await engine.tick_once()

        self.assertEqual(disconnected_tick.state_after, EngineState.DISCONNECTED)
        self.assertEqual(engine.state, EngineState.CONNECTED_IDLE)
        self.assertTrue(reconnect_tick.reconnected)
        events = self._events("position_visibility_lost")
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["payload"]["source"], POSITION_SOURCE_DISCONNECTED)
        self.assertEqual(connector.submitted_orders, [])

    async def test_p4_position_held_payload_includes_visibility_metadata(self) -> None:
        fetched = _mid_session_utc() - timedelta(seconds=12)
        connector = _SequencedPositionConnector(
            [
                _snapshot([], fetched_at=fetched),
                _snapshot([_position(10)], fetched_at=fetched),
            ]
        )
        engine = self._make_engine(connector)
        await self._init_engine(engine)
        engine._positions = [_position(10)]

        tick = await engine.tick_once()

        self.assertEqual(tick.orders_submitted, 0)
        events = self._events("cycle_evaluated_skip_position_held")
        self.assertEqual(len(events), 1)
        payload = events[0]["payload"]
        self.assertEqual(payload["position_source"], POSITION_SOURCE_LIVE_REQ_POSITIONS)
        self.assertEqual(payload["position_age_seconds"], 12.0)
        self.assertTrue(payload["position_visibility_valid"])

    async def test_p5_broker_flatten_between_cycles_is_seen_before_evaluation(self) -> None:
        connector = _SequencedPositionConnector(
            [
                _snapshot([], fetched_at=_mid_session_utc()),
                _snapshot([], fetched_at=_mid_session_utc() + timedelta(seconds=30)),
                _snapshot([], fetched_at=_mid_session_utc() + timedelta(seconds=30)),
            ]
        )
        engine = self._make_engine(connector)
        await self._init_engine(engine)
        engine._positions = [_position(10)]

        tick = await engine.tick_once()

        self.assertEqual(engine._positions, [])
        self.assertEqual(tick.orders_submitted, 1)
        self.assertEqual(connector.submitted_orders[0].ticker, "SPY")

    async def test_p6_timeout_snapshot_skips_submission_preserves_cache_and_retries(
        self,
    ) -> None:
        connector = _SequencedPositionConnector(
            [
                _snapshot([], fetched_at=_mid_session_utc()),
                _snapshot([], valid=False, source=POSITION_SOURCE_TIMEOUT_FALLBACK),
                _snapshot([], fetched_at=_mid_session_utc() + timedelta(seconds=30)),
                _snapshot([], fetched_at=_mid_session_utc() + timedelta(seconds=30)),
            ]
        )
        engine = self._make_engine(connector)
        await self._init_engine(engine)
        engine._positions = [_position(10)]

        invalid_tick = await engine.tick_once()
        positions_after_invalid = list(engine._positions)
        next_tick = await engine.tick_once()

        self.assertEqual(invalid_tick.orders_submitted, 0)
        self.assertEqual(positions_after_invalid, [_position(10)])
        self.assertEqual(engine._positions, [])
        self.assertEqual(self._events("strategy_stopped_out"), [])
        self.assertEqual(
            [
                event
                for event in self._events("order_submitted")
                if event["ts"] <= self._events("position_visibility_lost")[0]["ts"]
            ],
            [],
        )
        self.assertEqual(next_tick.orders_submitted, 1)
        self.assertEqual(len(connector.submitted_orders), 1)
