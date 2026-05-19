"""Spec B §9.2 tests for stopped-out strategy lifecycle."""

from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

from execution.connectors.mock import MockIBKRConnector
from execution.connectors.types import (
    BrokerExecution,
    BrokerOrderStatusEvent,
    BrokerPosition,
    POSITION_SOURCE_LIVE_REQ_POSITIONS,
    POSITION_SOURCE_TIMEOUT_FALLBACK,
    PositionSnapshot,
)
from execution.engine import main as engine_main
from execution.engine.main import DEFAULT_TICK_SECONDS, Engine, EngineConfig, EngineState
from execution.journal.writer import JournalWriter
from execution.risk import kill_switch
from execution.strategies import loader as strategy_loader


ET = ZoneInfo("US/Eastern")
STRATEGY_ID = "g-2026-05_2nd-wave-paper-trade"


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
    "instrument_whitelist": {"symbols": ["G"]},
}


def _mid_session_utc() -> datetime:
    return datetime(2026, 5, 15, 10, 30, tzinfo=ET).astimezone(timezone.utc)


def _write_strategy(dir_path: Path, *, status: str = "approved") -> Path:
    path = dir_path / f"strategy_{STRATEGY_ID}.md"
    metadata = ""
    if status == "stopped_out":
        metadata = (
            "stopped_out_at: '2026-05-15T14:26:23+00:00'\n"
            "stopped_out_fill_perm_id: 2000001\n"
            "stopped_out_fill_price: '30.00'\n"
            f"re_approve_path: '/invest-ship --re-approve {STRATEGY_ID}'\n"
        )
    path.write_text(
        "---\n"
        f"name: {STRATEGY_ID}\n"
        f"status: {status}\n"
        f"{metadata}"
        "strategy_type: hand_crafted\n"
        "risk_envelope_pct: 0.0025\n"
        "approved_at: 2026-05-01T10:00:00Z\n"
        "approved_commit_sha: abc1234\n"
        "order:\n"
        "  ticker: G\n"
        "  side: buy\n"
        "  qty: 71\n"
        "  limit_price: 34.50\n"
        "  stop_loss: 30.00\n"
        "  time_in_force: DAY\n"
        "---\n\n## How This Works\n\nPlain-English block.\n",
        encoding="utf-8",
    )
    return path


def _position(ticker: str, qty: int) -> BrokerPosition:
    return BrokerPosition(ticker=ticker, qty=qty, avg_price=Decimal("34.50"))


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


class _SequencedPositionConnector(MockIBKRConnector):
    def __init__(self, snapshots: list[PositionSnapshot]) -> None:
        super().__init__()
        self._snapshots = list(snapshots)

    async def get_positions(self) -> PositionSnapshot:
        self._require_connected()
        if self._snapshots:
            return self._snapshots.pop(0)
        return _snapshot([])


class ActiveProtectiveStopStateTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.base = Path(self._tmp.name)
        self.journal_dir = self.base / "journal"
        self.journal_dir.mkdir()
        self.strategies_dir = self.base / "strategies"
        self.strategies_dir.mkdir()
        self.strategy_path = _write_strategy(self.strategies_dir)
        self.kill_path = self.base / ".killed"
        self.connector = MockIBKRConnector()
        self.connector.marks = {"G": Decimal("34.50")}
        self.journal = JournalWriter(base_dir=self.journal_dir, git_sha="test92")

    async def asyncTearDown(self) -> None:
        await self._unpatch_now()
        self._tmp.cleanup()

    async def _patch_now(self, patched: datetime) -> None:
        from datetime import datetime as real_dt

        class _PatchedDT(real_dt):
            @classmethod
            def now(cls, tz=None):
                return patched if tz is None else patched.astimezone(tz)

        self._orig_main_dt = getattr(self, "_orig_main_dt", engine_main.datetime)
        engine_main.datetime = _PatchedDT

    async def _unpatch_now(self) -> None:
        if hasattr(self, "_orig_main_dt"):
            engine_main.datetime = self._orig_main_dt

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

    async def test_mock_bracket_submit_ack_exposes_protective_stop_child(self) -> None:
        await self.connector.connect()

        ack = await self.connector.submit_order(
            ticker="G",
            side="buy",
            qty=71,
            limit_price=Decimal("34.50"),
            stop_loss=Decimal("30.00"),
            time_in_force="DAY",
            client_tag="k2bi:g-2026-05_2nd-wave-paper-trade:T1",
            order_type="LMT",
        )

        self.assertEqual(ack.broker_perm_id, "2000000")
        self.assertEqual(ack.stop_broker_perm_id, "2000001")
        self.assertEqual(ack.stop_price, Decimal("30.00"))

    async def test_bracket_stop_ack_populates_active_protective_stop_record(self) -> None:
        await self._patch_now(_mid_session_utc())
        engine = self._make_engine()

        startup = await engine.tick_once()
        self.assertEqual(startup.state_after, EngineState.CONNECTED_IDLE)
        submitted = await engine.tick_once()
        self.assertEqual(submitted.state_after, EngineState.AWAITING_FILL)

        record = engine._active_protective_stops["G"]
        self.assertEqual(record.ticker, "G")
        self.assertEqual(record.strategy_id, STRATEGY_ID)
        self.assertEqual(record.parent_perm_id, 2000000)
        self.assertEqual(record.stop_perm_id, 2000001)
        self.assertEqual(record.stop_price, Decimal("30.00"))
        self.assertIsInstance(record.submitted_at, datetime)

    async def test_stop_terminal_statuses_clear_active_protective_stop_record(self) -> None:
        ProtectiveStopRecord = getattr(engine_main, "ProtectiveStopRecord", None)
        self.assertIsNotNone(ProtectiveStopRecord)
        engine = self._make_engine()
        record = ProtectiveStopRecord(
            ticker="G",
            stop_perm_id=2000001,
            stop_price=Decimal("30.00"),
            parent_perm_id=2000000,
            submitted_at=_mid_session_utc(),
            strategy_id=STRATEGY_ID,
        )

        engine._active_protective_stops["G"] = record
        engine._clear_active_protective_stop_on_terminal(
            ticker="G",
            stop_perm_id=2000001,
            terminal_status="Submitted",
        )
        self.assertEqual(engine._active_protective_stops["G"], record)

        engine._clear_active_protective_stop_on_terminal(
            ticker="G",
            stop_perm_id=9999999,
            terminal_status="Filled",
        )
        self.assertEqual(engine._active_protective_stops["G"], record)

        for status in ("Filled", "Cancelled", "Inactive"):
            engine._active_protective_stops["G"] = record
            engine._clear_active_protective_stop_on_terminal(
                ticker="G",
                stop_perm_id=2000001,
                terminal_status=status,
            )
            self.assertNotIn("G", engine._active_protective_stops)


class StoppedOutLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.base = Path(self._tmp.name)
        self.journal_dir = self.base / "journal"
        self.journal_dir.mkdir()
        self.strategies_dir = self.base / "strategies"
        self.strategies_dir.mkdir()
        self.kill_path = self.base / ".killed"
        self.strategy_path = _write_strategy(self.strategies_dir)
        self.connector = MockIBKRConnector()
        self.connector.marks = {"G": Decimal("34.50")}
        self.journal = JournalWriter(base_dir=self.journal_dir, git_sha="test92")

    async def asyncTearDown(self) -> None:
        await self._unpatch_now()
        self._tmp.cleanup()

    async def _patch_now(self, patched: datetime) -> None:
        from datetime import datetime as real_dt

        class _PatchedDT(real_dt):
            @classmethod
            def now(cls, tz=None):
                return patched if tz is None else patched.astimezone(tz)

        self._orig_main_dt = getattr(self, "_orig_main_dt", engine_main.datetime)
        engine_main.datetime = _PatchedDT

    async def _unpatch_now(self) -> None:
        if hasattr(self, "_orig_main_dt"):
            engine_main.datetime = self._orig_main_dt

    def _make_engine(self, connector: MockIBKRConnector | None = None) -> Engine:
        active_connector = connector or self.connector
        active_connector.marks = {"G": Decimal("34.50")}
        return Engine(
            connector=active_connector,
            journal=self.journal,
            validator_config=CONFIG,
            engine_config=EngineConfig(
                tick_seconds=DEFAULT_TICK_SECONDS,
                strategies_dir=self.strategies_dir,
                kill_path=self.kill_path,
            ),
        )

    def _load_strategy(self, engine: Engine) -> None:
        engine._strategies = strategy_loader.load_all_approved(self.strategies_dir)

    def _record(self, ticker: str = "G"):
        return engine_main.ProtectiveStopRecord(
            ticker=ticker,
            stop_perm_id=2000001,
            stop_price=Decimal("30.00"),
            parent_perm_id=2000000,
            submitted_at=_mid_session_utc(),
            strategy_id=STRATEGY_ID,
        )

    def _events(self, event_type: str) -> list[dict]:
        return [
            event
            for event in self.journal.read_all()
            if event["event_type"] == event_type
        ]

    def _stub_commit(self, engine: Engine) -> list[dict]:
        calls: list[dict] = []

        def fake_commit(**kwargs):
            calls.append(kwargs)
            return "deadbeefcafebabe"

        engine._git_commit_stopped_out_strategy = fake_commit  # type: ignore[method-assign]
        return calls

    def test_rewrite_stopped_out_frontmatter_preserves_nested_order_status(
        self,
    ) -> None:
        raw = (
            "---\n"
            "name: g-2026-05_2nd-wave-paper-trade\n"
            "status: approved\n"
            "strategy_type: hand_crafted\n"
            "order:\n"
            "  ticker: G\n"
            "  status: proposed\n"
            "  side: buy\n"
            "  qty: 71\n"
            "  order_type: MKT\n"
            "---\n\n"
            "## How This Works\n\nPlain-English block.\n"
        )

        rewritten = Engine._rewrite_stopped_out_frontmatter(
            raw,
            stopped_out_at="2026-05-19T13:59:18+00:00",
            fill_perm_id=1849923648,
            fill_price=Decimal("32.44"),
            re_approve_path=f"/invest-ship --re-approve {STRATEGY_ID}",
        )

        text = rewritten.decode("utf-8")
        self.assertIn("\nstatus: stopped_out\n", text)
        self.assertNotIn("\nstatus: proposed\n", text)
        self.assertIn(
            "\norder:\n"
            "  ticker: G\n"
            "  status: proposed\n"
            "  side: buy\n",
            text,
        )
        parsed = engine_main.strategy_frontmatter.parse(rewritten)
        self.assertEqual(parsed["status"], "stopped_out")
        self.assertEqual(parsed["order"]["status"], "proposed")
        self.assertEqual(parsed["order"]["side"], "buy")
        engine_main.strategy_frontmatter.validate_stopped_out_metadata(parsed)

    def test_rewrite_stopped_out_frontmatter_preserves_nested_yaml_structure(
        self,
    ) -> None:
        raw = (
            "---\n"
            "  name: g-2026-05_2nd-wave-paper-trade\n"
            "  status: approved\n"
            "  stopped_out_at: 'old-value'\n"
            "  strategy_type: hand_crafted\n"
            "  order:\n"
            "    ticker: G\n"
            "    status: proposed\n"
            "    side: buy\n"
            "    qty: 71\n"
            "    order_type: MKT\n"
            "  risk_controls:\n"
            "    primary:\n"
            "      status: proposed\n"
            "      side: buy\n"
            "    secondary:\n"
            "      status: active\n"
            "      note: keep\n"
            "  review_history:\n"
            "    - status: proposed\n"
            "      side: buy\n"
            "    - label: 'status: proposed'\n"
            "      status_like: 'status: proposed'\n"
            "  top_level_after_nested: keep-me\n"
            "---\n\n"
            "## How This Works\n\nPlain-English block.\n"
        )
        original = engine_main.strategy_frontmatter.parse(raw.encode("utf-8"))

        rewritten = Engine._rewrite_stopped_out_frontmatter(
            raw,
            stopped_out_at="2026-05-19T13:59:18+00:00",
            fill_perm_id=1849923648,
            fill_price=Decimal("32.44"),
            re_approve_path=f"/invest-ship --re-approve {STRATEGY_ID}",
        )

        text = rewritten.decode("utf-8")
        self.assertIn("\n  status: stopped_out\n", text)
        self.assertNotIn("old-value", text)
        self.assertIn("\n    status: proposed\n", text)
        self.assertIn("\n  top_level_after_nested: keep-me\n", text)
        parsed = engine_main.strategy_frontmatter.parse(rewritten)
        self.assertEqual(parsed["status"], "stopped_out")
        self.assertEqual(parsed["order"]["status"], "proposed")
        self.assertEqual(parsed["order"]["side"], "buy")
        self.assertEqual(parsed["risk_controls"]["primary"]["status"], "proposed")
        self.assertEqual(parsed["risk_controls"]["primary"]["side"], "buy")
        self.assertEqual(parsed["review_history"][0]["status"], "proposed")
        self.assertEqual(parsed["review_history"][0]["side"], "buy")
        self.assertEqual(parsed["review_history"][1]["label"], "status: proposed")
        self.assertEqual(parsed["top_level_after_nested"], "keep-me")
        engine_main.strategy_frontmatter.validate_stopped_out_metadata(parsed)

        metadata_keys = set(engine_main.strategy_frontmatter.STOPPED_OUT_ADDED_FIELDS)
        original_non_lifecycle = {
            key: value
            for key, value in original.items()
            if key not in metadata_keys and key != "status"
        }
        parsed_non_lifecycle = {
            key: value
            for key, value in parsed.items()
            if key not in metadata_keys and key != "status"
        }
        self.assertEqual(parsed_non_lifecycle, original_non_lifecycle)

    async def test_s1_stop_out_detection_flips_strategy_and_clears_record(self) -> None:
        engine = self._make_engine()
        self._load_strategy(engine)
        commits = self._stub_commit(engine)
        engine._positions_prev = [_position("G", 71)]
        engine._positions = []
        engine._position_visibility_valid = True
        engine._active_protective_stops["G"] = self._record()

        await engine._detect_strategy_stop_outs(cycle_id="cycle-s1")

        text = self.strategy_path.read_text(encoding="utf-8")
        self.assertIn("status: stopped_out", text)
        self.assertIn("stopped_out_at:", text)
        self.assertIn("stopped_out_fill_perm_id: 2000001", text)
        self.assertIn("stopped_out_fill_price:", text)
        self.assertIn(f"/invest-ship --re-approve {STRATEGY_ID}", text)
        self.assertNotIn("G", engine._active_protective_stops)
        event = self._events("strategy_stopped_out")[0]
        self.assertEqual(event["payload"]["strategy_id"], STRATEGY_ID)
        self.assertEqual(event["payload"]["ticker"], "G")
        self.assertEqual(event["payload"]["fill_perm_id"], 2000001)
        self.assertEqual(event["payload"]["fill_price"], "30.00")
        self.assertEqual(commits[0]["slug"], STRATEGY_ID)
        self.assertIn("Stopped-Out-Strategy: strategy_", commits[0]["message"])

    async def test_s2_stopped_out_strategy_is_skipped_and_journaled(self) -> None:
        _write_strategy(self.strategies_dir, status="stopped_out")
        await self._patch_now(_mid_session_utc())
        engine = self._make_engine()

        startup = await engine.tick_once()
        self.assertEqual(startup.state_after, EngineState.CONNECTED_IDLE)
        tick = await engine.tick_once()

        self.assertEqual(tick.orders_submitted, 0)
        self.assertEqual(self._events("order_submitted"), [])
        skip = self._events("cycle_skipped_strategy_stopped_out")[0]
        self.assertEqual(skip["payload"]["strategy_id"], STRATEGY_ID)
        self.assertEqual(
            skip["payload"]["stopped_out_at"],
            "2026-05-15T14:26:23+00:00",
        )

    async def test_s3_external_reapproval_status_restores_runtime_evaluation(self) -> None:
        _write_strategy(self.strategies_dir, status="stopped_out")
        stopped_engine = self._make_engine()
        await self._patch_now(_mid_session_utc())
        await stopped_engine.tick_once()
        await stopped_engine.tick_once()
        self.assertEqual(self._events("order_submitted"), [])

        self.journal = JournalWriter(base_dir=self.journal_dir, git_sha="test92b")
        _write_strategy(self.strategies_dir, status="approved")
        approved_engine = self._make_engine()
        await approved_engine.tick_once()
        tick = await approved_engine.tick_once()

        self.assertEqual(tick.orders_submitted, 1)
        self.assertEqual(self.connector.submitted_orders[0].ticker, "G")

    async def test_s4_external_flatten_without_active_stop_does_not_flip(self) -> None:
        engine = self._make_engine()
        self._load_strategy(engine)
        engine._positions_prev = [_position("G", 71)]
        engine._positions = []
        engine._position_visibility_valid = True

        await engine._detect_strategy_stop_outs(cycle_id="cycle-s4")

        self.assertIn("status: approved", self.strategy_path.read_text())
        self.assertEqual(self._events("strategy_stopped_out"), [])
        event = self._events("cycle_position_unexpectedly_zero")[0]
        self.assertEqual(event["payload"]["reason"], "no_active_protective_stop_record")

    async def test_s5_flip_uses_canonical_atomic_write_helper(self) -> None:
        engine = self._make_engine()
        self._load_strategy(engine)
        commits = self._stub_commit(engine)
        calls: list[Path] = []
        real_write = engine_main.strategy_frontmatter.atomic_write_bytes

        def tracking_write(path: Path, content: bytes) -> None:
            calls.append(path)
            real_write(path, content)

        with patch.object(
            engine_main.strategy_frontmatter,
            "atomic_write_bytes",
            side_effect=tracking_write,
        ):
            await engine._flip_strategy_to_stopped_out(
                strategy_id=STRATEGY_ID,
                ticker="G",
                fill_perm_id=2000001,
                fill_price=Decimal("30.00"),
                cycle_id="cycle-s5",
            )

        self.assertEqual(calls, [self.strategy_path])
        self.assertEqual(commits[0]["path"], self.strategy_path)
        decoy = self.base / "decoy.md"
        decoy.write_text("safe\n", encoding="utf-8")
        link = self.base / "link.md"
        link.symlink_to(decoy)
        with self.assertRaisesRegex(ValueError, "refusing to write through symlink"):
            real_write(link, b"unsafe\n")

    async def test_s6_invalid_position_snapshot_does_not_fire_stop_out(self) -> None:
        connector = _SequencedPositionConnector(
            [
                _snapshot([_position("G", 71)], fetched_at=_mid_session_utc()),
                _snapshot([], valid=False, source=POSITION_SOURCE_TIMEOUT_FALLBACK),
            ]
        )
        await self._patch_now(_mid_session_utc())
        engine = self._make_engine(connector)
        await engine.tick_once()
        engine._active_protective_stops["G"] = self._record()

        tick = await engine.tick_once()

        self.assertEqual(tick.orders_submitted, 0)
        self.assertIn("status: approved", self.strategy_path.read_text())
        self.assertEqual(self._events("strategy_stopped_out"), [])
        self.assertEqual(engine._active_protective_stops["G"], self._record())

    async def test_s7_partial_close_is_journaled_without_flip_or_clear(self) -> None:
        engine = self._make_engine()
        self._load_strategy(engine)
        record = self._record()
        engine._positions_prev = [_position("G", 71)]
        engine._positions = [_position("G", 50)]
        engine._position_visibility_valid = True
        engine._active_protective_stops["G"] = record

        await engine._detect_strategy_stop_outs(cycle_id="cycle-s7")

        self.assertIn("status: approved", self.strategy_path.read_text())
        self.assertEqual(self._events("strategy_stopped_out"), [])
        self.assertEqual(engine._active_protective_stops["G"], record)
        event = self._events("cycle_position_partial_close_observed")[0]
        self.assertEqual(event["payload"]["prev_qty"], 71)
        self.assertEqual(event["payload"]["curr_qty"], 50)

    async def test_s8_recovery_replay_detects_stop_fill_after_restart(self) -> None:
        engine = self._make_engine()
        self._load_strategy(engine)
        self._stub_commit(engine)
        self.journal.append(
            "order_submitted",
            payload={
                "status": "Submitted",
                "order_type": "LMT",
                "limit_price": "34.50",
                "stop_loss": "30.00",
                "stop_broker_perm_id": "2000001",
                "stop_price": "30.00",
            },
            strategy=STRATEGY_ID,
            trade_id="T-restart",
            ticker="G",
            side="buy",
            qty=71,
            broker_order_id="1000",
            broker_perm_id="2000000",
        )
        status = BrokerOrderStatusEvent(
            broker_order_id="1001",
            broker_perm_id="2000001",
            status="Filled",
            filled_qty=71,
            remaining_qty=0,
            avg_fill_price=Decimal("30.00"),
            last_update_at=_mid_session_utc(),
            client_tag=f"k2bi:{STRATEGY_ID}:T-restart:stop",
        )

        await engine._replay_stopped_out_stop_fills(
            journal_tail=self.journal.read_all(),
            broker_status=[status],
            cycle_id="recovery-replay",
        )

        event = self._events("strategy_stopped_out")[0]
        self.assertEqual(event["payload"]["source"], "recovery_replay")
        self.assertIn("status: stopped_out", self.strategy_path.read_text())

    async def test_s8b_recovery_replay_uses_execution_price_when_status_price_zero(
        self,
    ) -> None:
        engine = self._make_engine()
        self._load_strategy(engine)
        self._stub_commit(engine)
        await self.connector.connect()
        self.journal.append(
            "order_submitted",
            payload={
                "status": "Submitted",
                "order_type": "LMT",
                "limit_price": "34.50",
                "stop_loss": "30.00",
                "stop_broker_perm_id": "2000001",
                "stop_price": "30.00",
            },
            strategy=STRATEGY_ID,
            trade_id="T-restart",
            ticker="G",
            side="buy",
            qty=71,
            broker_order_id="1000",
            broker_perm_id="2000000",
        )
        self.connector.executions_history = [
            BrokerExecution(
                exec_id="0000dc8f.6a7433d6.01.01",
                broker_order_id="1001",
                broker_perm_id="2000001",
                ticker="G",
                side="sld",
                qty=71,
                price=Decimal("32.44"),
                filled_at=_mid_session_utc(),
            )
        ]
        status = BrokerOrderStatusEvent(
            broker_order_id="1001",
            broker_perm_id="2000001",
            status="Filled",
            filled_qty=71,
            remaining_qty=0,
            avg_fill_price=Decimal("0"),
            last_update_at=_mid_session_utc(),
            client_tag=f"k2bi:{STRATEGY_ID}:T-restart:stop",
        )

        await engine._replay_stopped_out_stop_fills(
            journal_tail=self.journal.read_all(),
            broker_status=[status],
            cycle_id="recovery-replay",
        )

        event = self._events("strategy_stopped_out")[0]
        self.assertEqual(event["payload"]["source"], "recovery_replay")
        self.assertEqual(event["payload"]["fill_perm_id"], 2000001)
        self.assertEqual(event["payload"]["fill_price"], "32.44")
        self.assertIn("stopped_out_fill_price: '32.44'", self.strategy_path.read_text())

    async def test_s9_new_long_entry_does_not_enter_stop_out_path(self) -> None:
        engine = self._make_engine()
        self._load_strategy(engine)
        engine._positions_prev = []
        engine._positions = [_position("G", 71)]
        engine._position_visibility_valid = True

        await engine._detect_strategy_stop_outs(cycle_id="cycle-s9")

        self.assertEqual(self._events("strategy_stopped_out"), [])
        self.assertEqual(self._events("cycle_position_unexpectedly_zero"), [])

    async def test_s10_resymbolized_position_goes_to_operator_reconciliation(self) -> None:
        engine = self._make_engine()
        self._load_strategy(engine)
        record = self._record()
        engine._positions_prev = [_position("G", 71)]
        engine._positions = [_position("GEN", 71)]
        engine._position_visibility_valid = True
        engine._active_protective_stops["G"] = record

        await engine._detect_strategy_stop_outs(cycle_id="cycle-s10")

        self.assertIn("status: approved", self.strategy_path.read_text())
        self.assertEqual(self._events("strategy_stopped_out"), [])
        self.assertEqual(engine._active_protective_stops["G"], record)
        event = self._events("cycle_position_unexpectedly_zero")[0]
        self.assertEqual(event["payload"]["reason"], "protective_stop_record_orphaned")

    async def test_loader_skips_approved_strategy_with_stopped_out_sentinel(self) -> None:
        sentinel = self.base / f".stopped-out-{STRATEGY_ID}-deadbeefdeadbeef"
        sentinel.write_text("{}\n", encoding="utf-8")

        snaps = strategy_loader.load_all_approved(
            self.strategies_dir,
            sentinel_dir=self.base,
        )

        self.assertEqual(snaps, [])

    async def test_submit_path_rejects_stopped_out_sentinel_defense_in_depth(self) -> None:
        sentinel = self.base / f".stopped-out-{STRATEGY_ID}-deadbeefdeadbeef"
        sentinel.write_text("{}\n", encoding="utf-8")
        engine = self._make_engine()
        snap = strategy_loader.load_approved(self.strategy_path)
        order = engine_main.Order(
            ticker="G",
            side="buy",
            qty=71,
            limit_price=Decimal("34.50"),
            stop_loss=Decimal("30.00"),
            strategy=STRATEGY_ID,
            submitted_at=_mid_session_utc(),
            order_type="LMT",
        )

        await engine._submit(
            snap=snap,
            order=order,
            trade_id="T-submit",
            tif="DAY",
            result=engine_main.TickResult(
                state_before=EngineState.CONNECTED_IDLE,
                state_after=EngineState.CONNECTED_IDLE,
            ),
        )

        self.assertEqual(self.connector.submitted_orders, [])
        event = self._events("order_rejected")[0]
        self.assertEqual(event["payload"]["reason"], "strategy_stopped_out")
