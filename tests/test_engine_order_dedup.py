"""Spec B §2 tests for journal-backed order deduplication."""

from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

from execution.connectors.types import BrokerExecution, BrokerPosition
from execution.connectors.mock import MockIBKRConnector
from execution.engine.main import DEFAULT_TICK_SECONDS, Engine, EngineConfig
from execution.journal.schema import (
    JournalReplayMalformedJsonError,
    JournalReplaySchemaVersionError,
    JournalReplayTruncatedLineError,
    JournalReplayUnknownEventTypeError,
)
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


def _write_strategy(dir: Path, *, name: str = "spy-rotational") -> Path:
    text = (
        "---\n"
        f"name: {name}\n"
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
        "---\n\n## How This Works\n\nPlain-English block.\n"
    )
    path = dir / f"{name}.md"
    path.write_text(text, encoding="utf-8")
    return path


def _raw_record(
    *,
    event_type: str,
    schema_version: int = 2,
    strategy: str | None = "spy-rotational",
    broker_order_id: str | None = "42",
    ticker: str | None = "SPY",
    qty: int | None = 10,
    payload: dict | None = None,
) -> str:
    record = {
        "ts": "2026-05-10T12:00:00.000000+00:00",
        "schema_version": schema_version,
        "event_type": event_type,
        "trade_id": "T-raw",
        "journal_entry_id": "J-raw",
        "strategy": strategy,
        "git_sha": "test",
        "payload": payload or {},
    }
    if ticker is not None:
        record["ticker"] = ticker
    if qty is not None:
        record["qty"] = qty
    if broker_order_id is not None:
        record["broker_order_id"] = broker_order_id
    return json.dumps(record)


class OrderDedupTests(unittest.IsolatedAsyncioTestCase):
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
        self.journal = JournalWriter(base_dir=self.journal_dir, git_sha="test02")
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
        import execution.journal.writer as writer_mod
        from datetime import datetime as real_dt

        class _PatchedDT(real_dt):
            @classmethod
            def now(cls, tz=None):
                return patched if tz is None else patched.astimezone(tz)

        self._orig_main_dt = getattr(self, "_orig_main_dt", main_mod.datetime)
        self._orig_mock_dt = getattr(self, "_orig_mock_dt", mock_mod.datetime)
        self._orig_writer_dt = getattr(
            self, "_orig_writer_dt", writer_mod.datetime
        )
        main_mod.datetime = _PatchedDT
        mock_mod.datetime = _PatchedDT
        writer_mod.datetime = _PatchedDT

    async def _unpatch_now(self) -> None:
        import execution.connectors.mock as mock_mod
        import execution.engine.main as main_mod
        import execution.journal.writer as writer_mod

        if hasattr(self, "_orig_main_dt"):
            main_mod.datetime = self._orig_main_dt
        if hasattr(self, "_orig_mock_dt"):
            mock_mod.datetime = self._orig_mock_dt
        if hasattr(self, "_orig_writer_dt"):
            writer_mod.datetime = self._orig_writer_dt

    async def _init_engine(self) -> None:
        await self._patch_now(_mid_session_utc())
        tick = await self.engine.tick_once()
        self.assertEqual(tick.state_after.value, "connected_idle")

    def _append_prior_submission(
        self,
        *,
        strategy: str = "spy-rotational",
        symbol: str = "SPY",
        broker_order_id: str = "42",
    ) -> None:
        self.journal.append(
            "order_submitted",
            payload={
                "status": "Submitted",
                "order_type": "LMT",
                "limit_price": "500.00",
                "stop_loss": "495.00",
                "time_in_force": "DAY",
            },
            strategy=strategy,
            trade_id=f"T-prior-{broker_order_id}",
            ticker=symbol,
            side="buy",
            qty=10,
            broker_order_id=broker_order_id,
            broker_perm_id=f"42{broker_order_id}",
        )

    def _append_terminal(
        self,
        *,
        strategy: str = "spy-rotational",
        symbol: str = "SPY",
        broker_order_id: str = "42",
        terminal_status: str = "Filled",
    ) -> None:
        self.journal.append(
            "order_terminal",
            payload={
                "broker_order_id": broker_order_id,
                "terminal_status": terminal_status,
            },
            strategy=strategy,
            trade_id=f"T-terminal-{broker_order_id}",
            ticker=symbol,
            side="buy",
            qty=10,
            broker_order_id=broker_order_id,
        )

    def _events(self, event_type: str) -> list[dict]:
        return [
            event
            for event in self.journal.read_all()
            if event["event_type"] == event_type
        ]

    async def test_d1_pending_prior_submission_blocks_new_submit(self) -> None:
        await self._init_engine()
        self._append_prior_submission()

        tick = await self.engine.tick_once()

        self.assertEqual(tick.orders_submitted, 0)
        self.assertEqual(len(self.connector.submitted_orders), 0)
        skips = self._events("cycle_skipped_pending_prior_submission")
        self.assertEqual(len(skips), 1)
        self.assertEqual(skips[0]["payload"]["strategy_id"], "spy-rotational")
        self.assertEqual(skips[0]["payload"]["symbol"], "SPY")
        self.assertEqual(skips[0]["payload"]["pending_order_id"], "42")

    async def test_d1b_multiple_pending_ids_are_fully_journaled(self) -> None:
        await self._init_engine()
        self._append_prior_submission(broker_order_id="42")
        self._append_prior_submission(broker_order_id="43")

        tick = await self.engine.tick_once()

        self.assertEqual(tick.orders_submitted, 0)
        skips = self._events("cycle_skipped_pending_prior_submission")
        self.assertEqual(len(skips), 1)
        self.assertEqual(skips[0]["payload"]["pending_order_id"], "42")
        self.assertEqual(skips[0]["payload"]["pending_order_ids"], ["42", "43"])
        self.assertEqual(skips[0]["payload"]["pending_order_count"], 2)

    async def test_d2_terminal_filled_order_does_not_block_submit(self) -> None:
        await self._init_engine()
        self._append_prior_submission()
        self._append_terminal(terminal_status="Filled")

        tick = await self.engine.tick_once()

        self.assertEqual(tick.orders_submitted, 1)
        self.assertEqual(len(self.connector.submitted_orders), 1)
        self.assertEqual(self._events("cycle_skipped_pending_prior_submission"), [])

    async def test_d2b_full_fill_emits_order_terminal_and_clears_pending_map(
        self,
    ) -> None:
        await self._init_engine()
        submit_tick = await self.engine.tick_once()
        self.assertEqual(submit_tick.orders_submitted, 1)
        pending = self.engine._pending_order
        self.assertIsNotNone(pending)
        assert pending is not None
        self.connector.executions_history.append(
            BrokerExecution(
                exec_id="E-fill",
                broker_order_id=pending.broker_order_id,
                broker_perm_id=pending.broker_perm_id,
                ticker="SPY",
                side="buy",
                qty=10,
                price=Decimal("500.00"),
                filled_at=_mid_session_utc() + timedelta(seconds=1),
            )
        )
        self.connector.positions = [
            BrokerPosition(ticker="SPY", qty=10, avg_price=Decimal("500.00"))
        ]

        poll_tick = await self.engine.tick_once()

        self.assertEqual(poll_tick.orders_filled, 1)
        terminals = self._events("order_terminal")
        self.assertEqual(len(terminals), 1)
        self.assertEqual(terminals[0]["payload"]["terminal_status"], "Filled")
        self.assertEqual(terminals[0]["broker_order_id"], pending.broker_order_id)
        self.engine._refresh_pending_orders_from_journal()
        self.assertEqual(self.engine._pending_orders, {})

    async def test_d3_terminal_rejected_order_does_not_block_submit(self) -> None:
        await self._init_engine()
        self._append_prior_submission()
        self._append_terminal(terminal_status="Rejected")

        tick = await self.engine.tick_once()

        self.assertEqual(tick.orders_submitted, 1)
        self.assertEqual(len(self.connector.submitted_orders), 1)
        self.assertEqual(self._events("cycle_skipped_pending_prior_submission"), [])

    async def test_d3b_legacy_filled_zero_float_string_does_not_block_submit(
        self,
    ) -> None:
        await self._init_engine()
        self._append_prior_submission()
        self.journal.append(
            "order_filled",
            payload={
                "fill_qty": 10,
                "remaining_qty": "0.0",
                "cumulative_filled_qty": 10,
            },
            strategy="spy-rotational",
            trade_id="T-fill-42",
            ticker="SPY",
            side="buy",
            qty=10,
            broker_order_id="42",
        )

        tick = await self.engine.tick_once()

        self.assertEqual(tick.orders_submitted, 1)
        self.assertEqual(self._events("cycle_skipped_pending_prior_submission"), [])

    async def test_d3c_legacy_filled_missing_remaining_uses_cumulative_qty(
        self,
    ) -> None:
        await self._init_engine()
        self._append_prior_submission()
        self.journal.append(
            "order_filled",
            payload={
                "fill_qty": 10,
                "cumulative_filled_qty": 10,
            },
            strategy="spy-rotational",
            trade_id="T-fill-42",
            ticker="SPY",
            side="buy",
            qty=10,
            broker_order_id="42",
        )

        tick = await self.engine.tick_once()

        self.assertEqual(tick.orders_submitted, 1)
        self.assertEqual(self._events("cycle_skipped_pending_prior_submission"), [])

    async def test_d4_pending_map_rebuilds_on_engine_startup(self) -> None:
        await self._patch_now(_mid_session_utc())
        self._append_prior_submission(broker_order_id="41")
        self._append_terminal(broker_order_id="41", terminal_status="Filled")
        self._append_prior_submission(broker_order_id="43")

        with patch.dict("os.environ", {"K2BI_ALLOW_RECOVERY_MISMATCH": "1"}):
            tick = await self.engine.tick_once()

        self.assertEqual(tick.state_after.value, "connected_idle")
        self.assertEqual(
            self.engine._pending_orders,
            {("spy-rotational", "SPY"): {"43"}},
        )

    async def test_d4a_malformed_json_fails_closed_on_replay(self) -> None:
        await self._patch_now(_mid_session_utc())
        self.journal.path_for_today().write_text("{not json}\n", encoding="utf-8")

        with self.assertRaises(JournalReplayMalformedJsonError):
            await self.engine.tick_once()

    async def test_d4b_unknown_event_type_fails_closed_on_replay(self) -> None:
        await self._patch_now(_mid_session_utc())
        self.journal.path_for_today().write_text(
            _raw_record(event_type="unknown_spec_b_event") + "\n",
            encoding="utf-8",
        )

        with self.assertRaises(JournalReplayUnknownEventTypeError):
            await self.engine.tick_once()

    async def test_d4c_truncated_final_line_fails_closed_on_replay(self) -> None:
        await self._patch_now(_mid_session_utc())
        self.journal.path_for_today().write_text(
            _raw_record(event_type="order_submitted"),
            encoding="utf-8",
        )

        with self.assertRaises(JournalReplayTruncatedLineError):
            await self.engine.tick_once()

    async def test_d4d_schema_version_mismatch_fails_closed_on_replay(self) -> None:
        await self._patch_now(_mid_session_utc())
        self.journal.path_for_today().write_text(
            _raw_record(event_type="order_submitted", schema_version=999) + "\n",
            encoding="utf-8",
        )

        with self.assertRaises(JournalReplaySchemaVersionError):
            await self.engine.tick_once()

    async def test_d4e_order_submitted_missing_ticker_fails_closed_on_replay(
        self,
    ) -> None:
        await self._patch_now(_mid_session_utc())
        self.journal.path_for_today().write_text(
            _raw_record(event_type="order_submitted", ticker=None) + "\n",
            encoding="utf-8",
        )

        with self.assertRaises(JournalReplayMalformedJsonError):
            await self.engine.tick_once()

    async def test_d4f_order_terminal_unknown_status_fails_closed_on_replay(
        self,
    ) -> None:
        await self._patch_now(_mid_session_utc())
        self.journal.path_for_today().write_text(
            _raw_record(event_type="order_submitted") + "\n"
            + _raw_record(
                event_type="order_terminal",
                payload={
                    "broker_order_id": "42",
                    "terminal_status": "MysteryStatus",
                },
            )
            + "\n",
            encoding="utf-8",
        )

        with self.assertRaises(JournalReplayMalformedJsonError):
            await self.engine.tick_once()

    async def test_d4g_order_submitted_missing_qty_fails_closed_on_replay(
        self,
    ) -> None:
        await self._patch_now(_mid_session_utc())
        self.journal.path_for_today().write_text(
            _raw_record(event_type="order_submitted", qty=None) + "\n",
            encoding="utf-8",
        )

        with self.assertRaises(JournalReplayMalformedJsonError):
            await self.engine.tick_once()

    async def test_d4h_startup_pending_replay_uses_init_clock(self) -> None:
        import execution.engine.main as main_mod
        from datetime import datetime as real_dt

        startup_now = datetime(2026, 5, 10, 23, 59, 59, tzinfo=timezone.utc)
        later_now = datetime(2026, 5, 11, 0, 0, 1, tzinfo=timezone.utc)
        now_calls = 0
        captured: list[datetime] = []

        class _PatchedDT(real_dt):
            @classmethod
            def now(cls, tz=None):
                nonlocal now_calls
                now_calls += 1
                current = startup_now if now_calls == 1 else later_now
                return current if tz is None else current.astimezone(tz)

        def _capture_refresh(engine_self, now: datetime) -> None:
            captured.append(now)
            engine_self._pending_orders = {}

        original_datetime = main_mod.datetime
        original_refresh = Engine._refresh_pending_orders_from_journal
        try:
            main_mod.datetime = _PatchedDT
            Engine._refresh_pending_orders_from_journal = _capture_refresh
            await self.engine.tick_once()
        finally:
            main_mod.datetime = original_datetime
            Engine._refresh_pending_orders_from_journal = original_refresh

        self.assertEqual(captured, [startup_now])

    async def test_d5_cross_strategy_pending_order_does_not_block_submit(self) -> None:
        _write_strategy(self.strategies_dir, name="spy-secondary")
        await self._init_engine()
        self._append_prior_submission(strategy="spy-rotational")

        tick = await self.engine.tick_once()

        self.assertEqual(tick.orders_submitted, 1)
        self.assertEqual(len(self.connector.submitted_orders), 1)
        self.assertIn(":spy-secondary:", self.connector.submitted_orders[0].client_tag)
        self.assertEqual(len(self._events("cycle_skipped_pending_prior_submission")), 1)

    async def test_d6_pending_replay_refreshes_once_per_tick(self) -> None:
        _write_strategy(self.strategies_dir, name="spy-secondary")
        await self._init_engine()
        self._append_prior_submission(strategy="spy-rotational")
        calls = []
        original = self.journal.read_all_strict

        def counting_read_all_strict(when=None):
            calls.append(when)
            return original(when)

        self.journal.read_all_strict = counting_read_all_strict

        tick = await self.engine.tick_once()

        self.assertEqual(tick.orders_submitted, 1)
        self.assertEqual(len(calls), 3)
