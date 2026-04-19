"""Tests for execution.engine.main.

Exercises the state machine through the MockIBKRConnector. Strategy
files are written to a temp directory; the kill-switch file lives in
the same temp directory (not the default vault path).
"""

from __future__ import annotations

import asyncio
import tempfile
import unittest
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

from execution.connectors.mock import MockIBKRConnector
from execution.connectors.types import (
    AccountSummary,
    AuthRequiredError,
    BrokerExecution,
    BrokerOpenOrder,
    BrokerPosition,
    DisconnectedError,
)
from datetime import timedelta
from execution.engine.main import (
    DEFAULT_TICK_SECONDS,
    Engine,
    EngineConfig,
    EngineState,
    TickResult,
    _reconnect_delay,
)
from execution.journal.writer import JournalWriter
from execution.validators.types import Order


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
    # 10:30 ET on Tuesday 2026-04-21 (confirmed session day).
    return datetime(2026, 4, 21, 10, 30, tzinfo=ET).astimezone(timezone.utc)


def _write_strategy(dir: Path, name: str = "spy-rotational", side: str = "buy") -> Path:
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
        f"  side: {side}\n"
        "  qty: 10\n"
        "  limit_price: 500.00\n"
    )
    if side == "buy":
        text += "  stop_loss: 495.00\n"
    text += "  time_in_force: DAY\n"
    text += "---\n\n## How This Works\n\nPlain-English block.\n"
    path = dir / f"{name}.md"
    path.write_text(text, encoding="utf-8")
    return path


class _TestClock:
    def __init__(self, start: datetime) -> None:
        self._now = start

    def __call__(self) -> datetime:
        return self._now

    def advance(self, seconds: float) -> None:
        from datetime import timedelta

        self._now = self._now + timedelta(seconds=seconds)


class EngineStartupTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.base = Path(self._tmp.name)
        self.journal_dir = self.base / "journal"
        self.journal_dir.mkdir()
        self.strategies_dir = self.base / "strategies"
        self.strategies_dir.mkdir()
        self.kill_path = self.base / ".killed"
        _write_strategy(self.strategies_dir)

        self.connector = MockIBKRConnector()
        self.journal = JournalWriter(base_dir=self.journal_dir, git_sha="test01")

    async def asyncTearDown(self):
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

    async def test_clean_startup_transitions_to_connected_idle(self):
        engine = self._make_engine()
        tick = await engine.tick_once()
        self.assertEqual(tick.state_after, EngineState.CONNECTED_IDLE)
        events = self.journal.read_all()
        event_types = [e["event_type"] for e in events]
        self.assertIn("engine_started", event_types)

    async def test_startup_refuses_when_single_open_order_has_corrupt_journal(self):
        # Codex R20 P1: broker has 1 pending_still_open but the
        # journal_view field that would drive resume is corrupt, so
        # _pick_resumable_awaiting refuses. If the engine then fell
        # through to CONNECTED_IDLE, next tick could submit a
        # duplicate order for the same strategy. Refuse startup
        # instead; Keith investigates.
        self.connector.open_orders = [
            BrokerOpenOrder(
                broker_order_id="1001",
                broker_perm_id="2000001",
                ticker="SPY",
                side="buy",
                qty=10,
                filled_qty=0,
                limit_price=Decimal("500"),
                status="Submitted",
                tif="DAY",
            ),
        ]
        # Journal submission record with a CORRUPT side (both top-
        # level + payload so _pending_from_journal's fallback chain
        # can't substitute a valid value). This triggers the
        # refuse-resume path during recovery.
        now = datetime.now(timezone.utc)
        self.journal.append(
            "order_submitted",
            payload={
                "status": "Submitted",
                "limit_price": "500",
                "submitted_at": now.isoformat(),
                "ticker": "SPY",
                "side": "long",  # invalid: must be buy or sell
                "qty": 10,
            },
            strategy="spy-rotational",
            trade_id="T-corrupt",
            ticker="SPY",
            side="long",  # invalid at top level too
            qty=10,
            broker_order_id="1001",
            broker_perm_id="2000001",
        )

        engine = self._make_engine()
        tick = await engine.tick_once()
        self.assertEqual(tick.state_after, EngineState.HALTED)
        events = self.journal.read_all()
        stopped = [e for e in events if e["event_type"] == "engine_stopped"]
        self.assertGreaterEqual(len(stopped), 1)
        reasons = [e["payload"]["reason"] for e in stopped]
        self.assertIn("recovery_resume_refused_live_order", reasons)

    async def test_startup_refuses_on_multiple_still_open_orders(self):
        # Codex round-3 P1: MVP handles one in-flight order only.
        # Multiple still-open broker orders must refuse to start, not
        # silently drop them.
        self.connector.open_orders = [
            BrokerOpenOrder(
                broker_order_id="1001",
                broker_perm_id="2000001",
                ticker="SPY",
                side="buy",
                qty=5,
                filled_qty=0,
                limit_price=Decimal("500"),
                status="Submitted",
                tif="DAY",
            ),
            BrokerOpenOrder(
                broker_order_id="1002",
                broker_perm_id="2000002",
                ticker="SPY",
                side="buy",
                qty=3,
                filled_qty=0,
                limit_price=Decimal("495"),
                status="Submitted",
                tif="DAY",
            ),
        ]
        # Journal-pending entries so recovery classifies them as
        # pending_still_open (not phantom_open_order).
        now = datetime.now(timezone.utc)
        for i, (oid, perm) in enumerate([("1001", "2000001"), ("1002", "2000002")]):
            self.journal.append(
                "order_submitted",
                payload={
                    "status": "Submitted",
                    "limit_price": "500",
                    "submitted_at": now.isoformat(),
                    "ticker": "SPY",
                    "side": "buy",
                    "qty": 5,
                },
                strategy="spy-rotational",
                trade_id=f"T-{i}",
                ticker="SPY",
                side="buy",
                qty=5,
                broker_order_id=oid,
                broker_perm_id=perm,
            )

        engine = self._make_engine()
        tick = await engine.tick_once()
        self.assertEqual(tick.state_after, EngineState.HALTED)
        events = self.journal.read_all()
        stopped = [e for e in events if e["event_type"] == "engine_stopped"]
        self.assertEqual(len(stopped), 1)
        self.assertEqual(
            stopped[0]["payload"]["reason"],
            "recovery_multiple_still_open_orders",
        )

    async def test_custom_recovery_override_env_is_honored(self):
        # Codex round-3 P2: EngineConfig.allow_recovery_mismatch_env
        # must flow into reconcile(), not the hardcoded default.
        import os

        custom_env = "K2BI_PAPER_ALLOW_MISMATCH"
        os.environ[custom_env] = "1"
        try:
            self.connector.positions = [
                BrokerPosition(ticker="NVDA", qty=5, avg_price=Decimal("600"))
            ]
            engine = Engine(
                connector=self.connector,
                journal=self.journal,
                validator_config=CONFIG,
                engine_config=EngineConfig(
                    tick_seconds=DEFAULT_TICK_SECONDS,
                    strategies_dir=self.strategies_dir,
                    kill_path=self.kill_path,
                    allow_recovery_mismatch_env=custom_env,
                ),
            )
            tick = await engine.tick_once()
            # Phantom position would normally refuse; the custom env
            # should activate override and let us proceed.
            self.assertEqual(tick.state_after, EngineState.CONNECTED_IDLE)
            events = self.journal.read_all()
            mismatch_events = [
                e for e in events if e["event_type"] == "recovery_state_mismatch"
            ]
            self.assertEqual(len(mismatch_events), 1)
            self.assertEqual(
                mismatch_events[0]["payload"]["resolution"],
                "proceeding_with_override",
            )
        finally:
            os.environ.pop(custom_env, None)

    async def test_startup_refuse_emits_single_engine_stopped(self):
        # Codex round-9 P2: init-time refusal journals engine_stopped
        # with a specific reason; must not be followed by a second
        # graceful_shutdown engine_stopped record after _shutdown runs.
        self.connector.positions = [
            BrokerPosition(ticker="NVDA", qty=5, avg_price=Decimal("600"))
        ]
        engine = self._make_engine()
        await engine.tick_once()
        # Simulate graceful shutdown path (run_forever's finally).
        await engine._shutdown()
        events = self.journal.read_all()
        stopped = [e for e in events if e["event_type"] == "engine_stopped"]
        self.assertEqual(len(stopped), 1)
        self.assertEqual(
            stopped[0]["payload"]["reason"],
            "recovery_state_mismatch_refused",
        )

    async def test_startup_refuses_on_phantom_position(self):
        # Broker has a position the journal never knew about.
        self.connector.positions = [
            BrokerPosition(ticker="NVDA", qty=5, avg_price=Decimal("600"))
        ]
        engine = self._make_engine()
        tick = await engine.tick_once()
        self.assertEqual(tick.state_after, EngineState.HALTED)
        events = self.journal.read_all()
        event_types = [e["event_type"] for e in events]
        self.assertIn("recovery_state_mismatch", event_types)
        self.assertIn("engine_stopped", event_types)
        # engine_started must NOT be present when refusing.
        self.assertNotIn("engine_started", event_types)

    async def test_startup_with_kill_file_transitions_to_killed(self):
        self.kill_path.parent.mkdir(parents=True, exist_ok=True)
        self.kill_path.write_text(
            '{"ts": "2026-05-01T00:00:00Z", "reason": "manual", "source": "test", "detail": {}}',
            encoding="utf-8",
        )
        engine = self._make_engine()
        tick = await engine.tick_once()
        self.assertEqual(tick.state_after, EngineState.KILLED)
        events = self.journal.read_all()
        started = [e for e in events if e["event_type"] == "engine_started"]
        self.assertEqual(len(started), 1)
        self.assertTrue(started[0]["payload"]["kill_file_present_at_startup"])

    async def test_startup_auth_required_transitions_to_disconnected(self):
        self.connector.fail_connect_with_auth = True
        engine = self._make_engine()
        tick = await engine.tick_once()
        self.assertEqual(tick.state_after, EngineState.DISCONNECTED)
        self.assertTrue(tick.auth_required)
        events = self.journal.read_all()
        event_types = [e["event_type"] for e in events]
        self.assertIn("auth_required", event_types)


class OrderSubmissionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
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

    async def asyncTearDown(self):
        self._tmp.cleanup()

    async def _patch_now(self, patched: datetime) -> None:
        """Monkey-patch datetime.now in the engine module so the tick
        clock lines up with a mid-session time."""
        import execution.engine.main as main_mod
        from datetime import datetime as real_dt

        class _PatchedDT(real_dt):
            @classmethod
            def now(cls, tz=None):
                return patched if tz is None else patched.astimezone(tz)

        # asyncTearDown restores via unittest's implicit patch pattern;
        # we use a self-managed save/restore here.
        self._orig_dt = main_mod.datetime
        main_mod.datetime = _PatchedDT

    async def _unpatch_now(self) -> None:
        import execution.engine.main as main_mod

        main_mod.datetime = self._orig_dt

    async def test_order_flow_through_validators_and_submit(self):
        await self._patch_now(_mid_session_utc())
        try:
            tick1 = await self.engine.tick_once()  # INIT
            self.assertEqual(tick1.state_after, EngineState.CONNECTED_IDLE)

            tick2 = await self.engine.tick_once()  # processing
            self.assertEqual(tick2.orders_submitted, 1)
            self.assertEqual(tick2.state_after, EngineState.AWAITING_FILL)
            self.assertEqual(len(self.connector.submitted_orders), 1)
            submitted = self.connector.submitted_orders[0]
            self.assertEqual(submitted.ticker, "SPY")
            self.assertEqual(submitted.side, "buy")
            self.assertEqual(submitted.qty, 10)

            events = self.journal.read_all()
            event_types = [e["event_type"] for e in events]
            self.assertIn("order_proposed", event_types)
            self.assertIn("order_submitted", event_types)
            # Reconciliation identity: submitted event must have broker
            # IDs so recovery can match on perm_id.
            submitted_event = next(
                e for e in events if e["event_type"] == "order_submitted"
            )
            self.assertIn("broker_order_id", submitted_event)
            self.assertIn("broker_perm_id", submitted_event)
        finally:
            await self._unpatch_now()

    async def test_fill_transitions_to_connected_idle(self):
        await self._patch_now(_mid_session_utc())
        try:
            await self.engine.tick_once()  # INIT
            await self.engine.tick_once()  # submit
            self.assertEqual(self.engine.state, EngineState.AWAITING_FILL)

            # Simulate broker side: drop the order from open-orders
            # list and add an execution.
            submitted = self.connector.submitted_orders[0]
            self.connector.executions_history.append(
                BrokerExecution(
                    exec_id="E1",
                    broker_order_id=submitted.broker_order_id,
                    broker_perm_id=submitted.broker_perm_id,
                    ticker="SPY",
                    side="buy",
                    qty=10,
                    price=Decimal("500.05"),
                    filled_at=_mid_session_utc(),
                )
            )
            # Update broker positions after fill.
            self.connector.positions = [
                BrokerPosition(ticker="SPY", qty=10, avg_price=Decimal("500.05"))
            ]

            tick3 = await self.engine.tick_once()  # poll -> fill
            self.assertEqual(tick3.orders_filled, 1)
            self.assertEqual(tick3.state_after, EngineState.CONNECTED_IDLE)
            events = self.journal.read_all()
            fill_events = [e for e in events if e["event_type"] == "order_filled"]
            self.assertEqual(len(fill_events), 1)
            self.assertEqual(fill_events[0]["payload"]["fill_qty"], 10)
        finally:
            await self._unpatch_now()

    async def test_kill_during_submit_blocks_order(self):
        await self._patch_now(_mid_session_utc())
        try:
            await self.engine.tick_once()  # INIT

            # Write .killed BEFORE the submit tick.
            self.kill_path.parent.mkdir(parents=True, exist_ok=True)
            self.kill_path.write_text(
                '{"ts": "2026-05-01T00:00:00Z", "reason": "manual", '
                '"source": "test", "detail": {}}',
                encoding="utf-8",
            )

            tick = await self.engine.tick_once()
            self.assertEqual(tick.state_after, EngineState.KILLED)
            self.assertEqual(tick.orders_submitted, 0)
            self.assertEqual(len(self.connector.submitted_orders), 0)
        finally:
            await self._unpatch_now()

    async def test_retire_sentinel_blocks_submit_with_strategy_retired_journal(self):
        # Bundle 3 cycle 3 (spec §3.2 + Q7): if the per-strategy
        # retirement sentinel is present at submit time, the engine
        # must refuse the order synchronously in the submit path,
        # journal order_rejected with reason=strategy_retired, and
        # never touch the broker. No new event type is required;
        # order_rejected is already in schema v2.
        from execution.risk import kill_switch

        await self._patch_now(_mid_session_utc())
        try:
            await self.engine.tick_once()  # INIT

            # Retire the strategy mid-session. The retired_dir defaults
            # to kill_path.parent (= self.base) for test isolation.
            retired_path = kill_switch.write_retired(
                "spy-rotational",
                reason="cycle 3 synthetic retire",
                commit_sha="testsha",
                base_dir=self.base,
            )
            self.assertIsNotNone(retired_path)

            tick = await self.engine.tick_once()
            self.assertEqual(tick.state_after, EngineState.CONNECTED_IDLE)
            self.assertEqual(tick.orders_submitted, 0)
            self.assertEqual(tick.orders_rejected, 1)
            # Broker was never called -- the sentinel check is
            # synchronous and short-circuits before submit_order().
            self.assertEqual(len(self.connector.submitted_orders), 0)

            events = self.journal.read_all()
            # R2-minimax P2: retirement check runs BEFORE the runner,
            # so a retired strategy never reaches validators nor
            # journals order_proposed. The only order_* event for this
            # tick is a single order_rejected with reason=strategy_retired.
            event_types = [e["event_type"] for e in events]
            self.assertNotIn("order_proposed", event_types)
            self.assertNotIn("order_submitted", event_types)

            rejections = [
                e for e in events
                if e["event_type"] == "order_rejected"
                and e["payload"].get("reason") == "strategy_retired"
            ]
            self.assertEqual(len(rejections), 1)
            rejection = rejections[0]
            self.assertEqual(rejection["strategy"], "spy-rotational")
            retired_record = rejection["payload"]["retired_record"]
            self.assertEqual(retired_record["slug"], "spy-rotational")
            self.assertEqual(retired_record["commit_sha"], "testsha")
            # R1-minimax: the payload carries the snapshot's sha256 +
            # approval commit so replay can pin the rejection to an
            # exact approved snapshot without re-loading the file.
            self.assertIn("strategy_sha256", rejection["payload"])
            self.assertIn(
                "strategy_approved_commit", rejection["payload"]
            )
            self.assertEqual(
                rejection["payload"]["strategy_approved_commit"],
                "abc1234",
            )
        finally:
            await self._unpatch_now()

    async def test_invalid_strategy_slug_journals_rejection_without_crash(self):
        # R5-minimax P1 + Codex R4: the engine-level ValueError handler
        # is defense-in-depth for the narrow case where a strategy
        # snapshot carries a name that fails _validate_slug (empty,
        # non-str, or NUL byte). This test uses a monkey-patch to
        # force the sentinel check to raise ValueError, since YAML
        # frontmatter cannot realistically produce those inputs from
        # the loader and we only care that the engine doesn't crash
        # the tick when the exception surfaces.
        from execution.risk import kill_switch

        await self._patch_now(_mid_session_utc())

        original = kill_switch.assert_strategy_not_retired

        def _raise_value_error(slug, base_dir=None):
            raise ValueError(
                f"synthetic: invalid strategy slug {slug!r}"
            )

        kill_switch.assert_strategy_not_retired = _raise_value_error
        try:
            await self.engine.tick_once()  # INIT
            tick = await self.engine.tick_once()  # process
            # Tick did NOT crash -- it returned a valid TickResult.
            self.assertEqual(tick.state_after, EngineState.CONNECTED_IDLE)
            # No broker call occurred.
            self.assertEqual(len(self.connector.submitted_orders), 0)
            events = self.journal.read_all()
            invalid_rejections = [
                e for e in events
                if e["event_type"] == "order_rejected"
                and e["payload"].get("reason") == "strategy_name_invalid"
            ]
            self.assertEqual(len(invalid_rejections), 1)
            self.assertIn("error", invalid_rejections[0]["payload"])
        finally:
            kill_switch.assert_strategy_not_retired = original
            await self._unpatch_now()

    async def test_retire_sentinel_keyed_by_filename_slug_not_frontmatter_name(self):
        # Codex R5 P1: the cycle-4 post-commit hook derives its slug
        # from the `Retired-Strategy: strategy_<slug>` commit trailer,
        # which comes from the filename, not the `name:` frontmatter.
        # The engine MUST honor the same derivation so both sides key
        # sentinels identically. This test writes a strategy file where
        # the on-disk filename slug and the frontmatter `name:` differ,
        # then retires via the FILENAME slug, and asserts the engine
        # still blocks submits.
        from execution.risk import kill_switch

        # Remove the default fixture strategy; write a new one with a
        # mismatched filename vs. frontmatter-name.
        (self.strategies_dir / "spy-rotational.md").unlink()
        mismatched = self.strategies_dir / "strategy_spy-canonical-slug.md"
        mismatched.write_text(
            "---\n"
            "name: DisplayNameDiffers\n"
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
            "---\n\n## How This Works\n\nFilename-slug precedence test.\n",
            encoding="utf-8",
        )

        from execution.engine.main import Engine
        engine = Engine(
            connector=self.connector,
            journal=self.journal,
            validator_config=CONFIG,
            engine_config=EngineConfig(
                tick_seconds=DEFAULT_TICK_SECONDS,
                strategies_dir=self.strategies_dir,
                kill_path=self.kill_path,
            ),
        )

        # Retire via the FILENAME slug (what the cycle-4 hook would do).
        kill_switch.write_retired(
            "spy-canonical-slug",
            reason="filename-slug retire",
            commit_sha="deadbeef",
            base_dir=self.base,
        )

        await self._patch_now(_mid_session_utc())
        try:
            await engine.tick_once()  # INIT
            tick = await engine.tick_once()
            self.assertEqual(tick.orders_rejected, 1)
            self.assertEqual(tick.orders_submitted, 0)
            events = self.journal.read_all()
            retired = [
                e for e in events
                if e["event_type"] == "order_rejected"
                and e["payload"].get("reason") == "strategy_retired"
            ]
            self.assertEqual(
                len(retired), 1,
                "engine must key sentinels by filename slug so the cycle-4 "
                "hook's Retired-Strategy: strategy_<slug> trailer matches"
            )
        finally:
            await self._unpatch_now()

    async def test_retire_slug_is_filename_stem_not_frontmatter_name_flat_layout(self):
        # Codex R6: even in the flat layout `<slug>.md` (no `strategy_`
        # prefix), the sentinel must key by filename stem, NOT by
        # frontmatter `name:`. The loader doesn't enforce name==stem,
        # and the cycle-4 hook writes by filename, so falling back to
        # snap.name would let a name/filename drift silently bypass
        # the retirement gate.
        from execution.risk import kill_switch

        # Rewrite the default strategy so filename stem and
        # frontmatter name DIFFER in flat layout.
        (self.strategies_dir / "spy-rotational.md").unlink()
        drift_file = self.strategies_dir / "meanrev-v2.md"
        drift_file.write_text(
            "---\n"
            "name: meanrev\n"
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
            "---\n\n## How This Works\n\nFlat-layout name drift test.\n",
            encoding="utf-8",
        )

        from execution.engine.main import Engine
        engine = Engine(
            connector=self.connector,
            journal=self.journal,
            validator_config=CONFIG,
            engine_config=EngineConfig(
                tick_seconds=DEFAULT_TICK_SECONDS,
                strategies_dir=self.strategies_dir,
                kill_path=self.kill_path,
            ),
        )

        # Retire via the filename stem (what the hook would do).
        kill_switch.write_retired(
            "meanrev-v2",
            reason="drift test",
            commit_sha="deadbeef",
            base_dir=self.base,
        )

        # Sanity: a sentinel keyed by the frontmatter name would
        # NOT block submits (the reverse assertion).
        self.assertFalse(
            kill_switch.is_strategy_retired(
                "meanrev", base_dir=self.base
            )
        )

        await self._patch_now(_mid_session_utc())
        try:
            await engine.tick_once()  # INIT
            tick = await engine.tick_once()
            self.assertEqual(tick.orders_rejected, 1)
            self.assertEqual(tick.orders_submitted, 0)
        finally:
            await self._unpatch_now()

    async def test_retire_sentinel_is_per_strategy_not_global(self):
        # Cross-strategy isolation at the engine boundary: a sentinel for
        # a different slug must NOT affect spy-rotational's submits. Use
        # the same tick flow as the clean submit test but with a sentinel
        # for an unrelated strategy written beforehand.
        from execution.risk import kill_switch

        await self._patch_now(_mid_session_utc())
        try:
            # Write a sentinel for a strategy the engine doesn't have
            # loaded. This must be a no-op for the spy-rotational flow.
            kill_switch.write_retired(
                "atr-trail",
                reason="unrelated",
                commit_sha="ffffff",
                base_dir=self.base,
            )

            await self.engine.tick_once()  # INIT
            submit_tick = await self.engine.tick_once()
            self.assertEqual(submit_tick.orders_submitted, 1)
            self.assertEqual(submit_tick.orders_rejected, 0)
            self.assertEqual(submit_tick.state_after, EngineState.AWAITING_FILL)

            events = self.journal.read_all()
            event_types = [e["event_type"] for e in events]
            self.assertIn("order_submitted", event_types)
            rejections = [
                e for e in events
                if e["event_type"] == "order_rejected"
                and e["payload"].get("reason") == "strategy_retired"
            ]
            self.assertEqual(rejections, [])
        finally:
            await self._unpatch_now()

    async def test_kill_cleared_resumes(self):
        await self._patch_now(_mid_session_utc())
        try:
            await self.engine.tick_once()  # INIT
            # Kill.
            self.kill_path.parent.mkdir(parents=True, exist_ok=True)
            self.kill_path.write_text("{}", encoding="utf-8")
            killed_tick = await self.engine.tick_once()
            self.assertEqual(killed_tick.state_after, EngineState.KILLED)

            # Clear.
            self.kill_path.unlink()
            cleared_tick = await self.engine.tick_once()
            self.assertTrue(cleared_tick.kill_cleared)
            # After clearing, we're back to CONNECTED_IDLE -- the same
            # tick then runs strategy evaluation and submits.
            events = self.journal.read_all()
            event_types = [e["event_type"] for e in events]
            self.assertIn("kill_cleared", event_types)
        finally:
            await self._unpatch_now()

    async def test_partial_fill_timeout_refreshes_positions(self):
        # Codex round-1 P1: cancel after a partial must resync positions
        # from broker. Without the fix the engine would re-emit the
        # strategy on the next tick with stale _positions.
        await self._patch_now(_mid_session_utc())
        try:
            await self.engine.tick_once()  # INIT
            await self.engine.tick_once()  # submit
            self.assertEqual(self.engine.state, EngineState.AWAITING_FILL)
            submitted = self.connector.submitted_orders[0]

            # Simulate partial fill: 3 of 10 shares filled, remainder
            # sits in open orders.
            self.connector.executions_history.append(
                BrokerExecution(
                    exec_id="E1",
                    broker_order_id=submitted.broker_order_id,
                    broker_perm_id=submitted.broker_perm_id,
                    ticker="SPY",
                    side="buy",
                    qty=3,
                    price=Decimal("500"),
                    filled_at=_mid_session_utc(),
                )
            )
            # The remainder order stays in open_orders but we
            # manufacture a terminal disappearance via timeout: push
            # fill_timeout_seconds to 0 so next poll triggers cancel.
            self.engine.engine_config.fill_timeout_seconds = 0.0
            self.connector.open_orders = [
                BrokerOpenOrder(
                    broker_order_id=submitted.broker_order_id,
                    broker_perm_id=submitted.broker_perm_id,
                    ticker="SPY",
                    side="buy",
                    qty=10,
                    filled_qty=3,
                    limit_price=Decimal("500"),
                    status="Submitted",
                )
            ]
            # Seed broker positions so the post-cancel refresh can pick
            # up the 3 filled shares.
            self.connector.positions = [
                BrokerPosition(ticker="SPY", qty=3, avg_price=Decimal("500"))
            ]

            # Drop from open orders just before the next tick so the
            # timeout path (cancel-then-reconcile) fires.
            self.connector.open_orders = []

            tick = await self.engine.tick_once()
            self.assertEqual(tick.state_after, EngineState.CONNECTED_IDLE)
            # Engine must have refreshed from broker after the
            # terminal transition; our seeded 3-share SPY position
            # should now be in the engine's _positions.
            self.assertEqual(len(self.engine._positions), 1)
            self.assertEqual(self.engine._positions[0].qty, 3)
        finally:
            await self._unpatch_now()

    async def test_eod_cancels_day_orders_and_journals_eod_cancel(self):
        # Codex round-2 P2: EOD must actually cancel DAY orders + emit
        # one eod_cancel event per cancellation + one eod_complete.
        await self._patch_now(_mid_session_utc())
        try:
            await self.engine.tick_once()  # INIT
            self.assertEqual(self.engine.state, EngineState.CONNECTED_IDLE)

            # Seed open orders: one k2bi-tagged DAY (should cancel),
            # one k2bi GTC (kept), one manual DAY without k2bi tag
            # (should be left alone per Codex round-8 P2).
            self.connector.open_orders = [
                BrokerOpenOrder(
                    broker_order_id="1001",
                    broker_perm_id="2000001",
                    ticker="SPY",
                    side="buy",
                    qty=5,
                    filled_qty=0,
                    limit_price=Decimal("490"),
                    status="Submitted",
                    tif="DAY",
                    client_tag="k2bi:spy-rotational:T-EOD-1",
                ),
                BrokerOpenOrder(
                    broker_order_id="1002",
                    broker_perm_id="2000002",
                    ticker="AAPL",
                    side="buy",
                    qty=3,
                    filled_qty=0,
                    limit_price=Decimal("200"),
                    status="Submitted",
                    tif="GTC",
                    client_tag="k2bi:spy-rotational:T-EOD-2",
                ),
                BrokerOpenOrder(
                    broker_order_id="9999",
                    broker_perm_id="9999999",
                    ticker="NVDA",
                    side="buy",
                    qty=2,
                    filled_qty=0,
                    limit_price=Decimal("600"),
                    status="Submitted",
                    tif="DAY",
                    client_tag="",  # manual entry, no k2bi prefix
                ),
            ]

            # Force EOD by calling the internal hook directly. The tick
            # itself schedules EOD via wall-clock; directly invoking
            # the helper is the clean way to unit-test the cancel +
            # journal side-effects.
            result = TickResult(
                state_before=self.engine.state, state_after=self.engine.state
            )
            await self.engine._run_eod(result)
            self.assertTrue(result.eod_ran)

            # DAY order cancelled, GTC kept, manual DAY (no k2bi tag) skipped.
            self.assertEqual(self.connector.cancelled_order_ids, ["1001"])
            remaining_ids = {o.broker_order_id for o in self.connector.open_orders}
            self.assertEqual(remaining_ids, {"1002", "9999"})

            events = self.journal.read_all()
            event_types = [e["event_type"] for e in events]
            eod_cancels = [e for e in events if e["event_type"] == "eod_cancel"]
            eod_completes = [e for e in events if e["event_type"] == "eod_complete"]
            self.assertEqual(len(eod_cancels), 1)
            self.assertEqual(eod_cancels[0]["broker_order_id"], "1001")
            self.assertEqual(len(eod_completes), 1)
            eod_payload = eod_completes[0]["payload"]
            self.assertEqual(eod_payload["cancelled_orders"], 1)
            self.assertEqual(eod_payload["non_day_orders_retained"], 1)
            self.assertEqual(eod_payload["non_k2bi_orders_skipped"], 1)
        finally:
            await self._unpatch_now()

    async def test_cancel_request_defers_terminal_journal(self):
        # Codex round-8 P1: cancel is async at IBKR. _cancel_pending
        # must not journal order_timeout + clear pending before the
        # broker confirms terminal -- otherwise a crash in the cancel
        # window (order still live at broker, journal says closed)
        # causes phantom_open_order on restart.
        await self._patch_now(_mid_session_utc())
        try:
            await self.engine.tick_once()  # INIT
            await self.engine.tick_once()  # submit
            self.assertEqual(self.engine.state, EngineState.AWAITING_FILL)
            submitted = self.connector.submitted_orders[0]

            # Force a timeout path: broker still shows order open, no
            # fill yet, elapsed > 0s.
            self.engine.engine_config.fill_timeout_seconds = 0.0
            self.connector.open_orders = [
                BrokerOpenOrder(
                    broker_order_id=submitted.broker_order_id,
                    broker_perm_id=submitted.broker_perm_id,
                    ticker="SPY",
                    side="buy",
                    qty=10,
                    filled_qty=0,
                    limit_price=Decimal("500"),
                    status="Submitted",
                    tif="DAY",
                )
            ]

            tick = await self.engine.tick_once()
            # cancel_order was called once.
            self.assertEqual(
                self.connector.cancelled_order_ids,
                [submitted.broker_order_id],
            )
            # Pending MUST still be tracked -- broker hasn't confirmed yet.
            self.assertIsNotNone(self.engine._pending_order)
            self.assertTrue(self.engine._pending_order.cancel_requested)
            self.assertEqual(self.engine.state, EngineState.AWAITING_FILL)
            # No order_timeout journaled yet.
            events = self.journal.read_all()
            timeouts = [e for e in events if e["event_type"] == "order_timeout"]
            self.assertEqual(timeouts, [])
        finally:
            await self._unpatch_now()

    async def test_mid_session_disconnect_enters_disconnected(self):
        # Codex round-1 P1: breaker / market read failures during
        # tick body must downgrade to DISCONNECTED, not crash.
        await self._patch_now(_mid_session_utc())
        try:
            await self.engine.tick_once()  # INIT
            self.assertEqual(self.engine.state, EngineState.CONNECTED_IDLE)
            # Flip the mock connection off. The next tick's
            # account-summary call inside _evaluate_breakers will raise.
            self.connector.set_connected(False)

            tick = await self.engine.tick_once()
            self.assertEqual(tick.state_after, EngineState.DISCONNECTED)
        finally:
            await self._unpatch_now()

    async def test_submit_transport_failure_forces_reinit_on_reconnect(self):
        # Codex R22 P1: if submit_order raises AuthRequiredError or
        # DisconnectedError after IBKR may have already accepted the
        # order, the engine must NOT simply return to CONNECTED_IDLE
        # on reconnect -- the next tick would re-evaluate the strategy
        # and submit a duplicate. Force re-init so a full reconcile
        # runs before evaluating strategies again.
        await self._patch_now(_mid_session_utc())
        try:
            await self.engine.tick_once()  # INIT
            self.assertTrue(self.engine._init_completed)

            # Inject a failure on the next submit_order call.
            from execution.connectors.types import DisconnectedError

            self.connector.fail_next_submit = DisconnectedError(
                "socket dropped mid-submit"
            )

            tick = await self.engine.tick_once()  # attempt submit
            self.assertEqual(tick.state_after, EngineState.DISCONNECTED)
            # The proposal is journaled but not terminated; recovery
            # must re-run on reconnect, so init is marked incomplete.
            self.assertFalse(self.engine._init_completed)
        finally:
            await self._unpatch_now()

    async def test_validator_reject_journals_and_stays_idle(self):
        # Write a strategy with a non-whitelisted ticker so
        # instrument_whitelist rejects at the engine's validator cascade.
        bad_strategy = (
            "---\nname: bad-ticker\nstatus: approved\n"
            "strategy_type: hand_crafted\nrisk_envelope_pct: 0.01\n"
            "approved_at: 2026-05-01T10:00:00Z\napproved_commit_sha: abc1234\n"
            "order:\n  ticker: TSLA\n  side: buy\n  qty: 10\n"
            "  limit_price: 500.00\n  stop_loss: 495.00\n  time_in_force: DAY\n---\n\n"
            "## How This Works\n\nblock\n"
        )
        # Remove the good strategy, write the bad one.
        for p in self.strategies_dir.iterdir():
            p.unlink()
        (self.strategies_dir / "bad-ticker.md").write_text(bad_strategy, encoding="utf-8")

        await self._patch_now(_mid_session_utc())
        try:
            await self.engine.tick_once()  # INIT
            tick = await self.engine.tick_once()
            self.assertEqual(tick.orders_rejected, 1)
            self.assertEqual(tick.orders_submitted, 0)
            self.assertEqual(tick.state_after, EngineState.CONNECTED_IDLE)
            events = self.journal.read_all()
            rejects = [e for e in events if e["event_type"] == "order_rejected"]
            self.assertEqual(len(rejects), 1)
            self.assertEqual(
                rejects[0]["error"]["code"], "ticker_not_whitelisted"
            )
        finally:
            await self._unpatch_now()


class CliAccountWiringTests(unittest.TestCase):
    """Codex R18 P1 + architect's post-R18 type-level discipline
    ruling: the CLI must wire account_id from --account-id or
    K2BI_IB_ACCOUNT_ID into IBKRConnector, and missing the kwarg
    must fail at construction time, not silently bypass filters.
    """

    def test_direct_constructor_forbids_missing_account_id(self):
        from execution.connectors.ibkr import IBKRConnector

        # The kwarg is required + keyword-only; calling without it
        # is a TypeError at construction time. Guarantees that any
        # future call site that forgets the kwarg breaks loudly at
        # import/startup rather than silently defaulting to no
        # account filter.
        with self.assertRaises(TypeError):
            IBKRConnector()  # type: ignore[call-arg]

    def test_cli_resolves_account_from_argument(self):
        import argparse

        from execution.engine.main import _resolve_cli_account_id

        args = argparse.Namespace(account_id="DUQ12345")
        self.assertEqual(_resolve_cli_account_id(args), "DUQ12345")

    def test_cli_resolves_account_from_env_when_arg_absent(self):
        import argparse
        import os

        from execution.engine.main import _resolve_cli_account_id

        args = argparse.Namespace(account_id=None)
        prev = os.environ.get("K2BI_IB_ACCOUNT_ID")
        os.environ["K2BI_IB_ACCOUNT_ID"] = "DUQ-FROM-ENV"
        try:
            self.assertEqual(_resolve_cli_account_id(args), "DUQ-FROM-ENV")
        finally:
            if prev is None:
                os.environ.pop("K2BI_IB_ACCOUNT_ID", None)
            else:
                os.environ["K2BI_IB_ACCOUNT_ID"] = prev

    def test_cli_arg_overrides_env(self):
        import argparse
        import os

        from execution.engine.main import _resolve_cli_account_id

        args = argparse.Namespace(account_id="DUQ-FROM-ARG")
        prev = os.environ.get("K2BI_IB_ACCOUNT_ID")
        os.environ["K2BI_IB_ACCOUNT_ID"] = "DUQ-FROM-ENV"
        try:
            self.assertEqual(_resolve_cli_account_id(args), "DUQ-FROM-ARG")
        finally:
            if prev is None:
                os.environ.pop("K2BI_IB_ACCOUNT_ID", None)
            else:
                os.environ["K2BI_IB_ACCOUNT_ID"] = prev

    def test_cli_resolves_to_none_when_neither_arg_nor_env(self):
        import argparse
        import os

        from execution.engine.main import _resolve_cli_account_id

        args = argparse.Namespace(account_id=None)
        prev = os.environ.pop("K2BI_IB_ACCOUNT_ID", None)
        try:
            self.assertIsNone(_resolve_cli_account_id(args))
        finally:
            if prev is not None:
                os.environ["K2BI_IB_ACCOUNT_ID"] = prev

    def test_cli_constructs_connector_with_resolved_account(self):
        # End-to-end plumb-through check: given a CLI args namespace,
        # _construct_cli_connector builds an IBKRConnector whose
        # _account_id matches what _resolve_cli_account_id returned.
        import argparse

        from execution.engine.main import _construct_cli_connector

        args = argparse.Namespace(account_id="DUQ-WIRED")
        connector = _construct_cli_connector(args)
        self.assertEqual(connector._account_id, "DUQ-WIRED")

    def test_cli_constructs_connector_with_none_account_explicitly(self):
        # Single-account paper deployments pass None; still a
        # conscious choice (arg absent + env absent).
        import argparse
        import os

        from execution.engine.main import _construct_cli_connector

        args = argparse.Namespace(account_id=None)
        prev = os.environ.pop("K2BI_IB_ACCOUNT_ID", None)
        try:
            connector = _construct_cli_connector(args)
            self.assertIsNone(connector._account_id)
        finally:
            if prev is not None:
                os.environ["K2BI_IB_ACCOUNT_ID"] = prev


class EodTimezoneTests(unittest.TestCase):
    """Codex round-11 P1: EOD cutoff must be interpreted as US/Eastern
    local so it stays 30 min after close across EDT/EST transitions."""

    def test_eod_et_default_is_1630(self):
        from execution.engine.main import DEFAULT_EOD_ET

        self.assertEqual(DEFAULT_EOD_ET, "16:30")

    def test_engine_config_field_name_is_eod_et_time(self):
        cfg = EngineConfig()
        self.assertEqual(cfg.eod_et_time, "16:30")


class EngineConfigFromDictTests(unittest.TestCase):
    """Codex round-7 P2: YAML-configured override env name must flow
    through _engine_config_from_dict into EngineConfig."""

    def test_yaml_override_env_name_preserved(self):
        from execution.engine.main import _engine_config_from_dict

        cfg = _engine_config_from_dict(
            {"allow_recovery_mismatch_env": "K2BI_PAPER_ALLOW_MISMATCH"}
        )
        self.assertEqual(
            cfg.allow_recovery_mismatch_env, "K2BI_PAPER_ALLOW_MISMATCH"
        )

    def test_yaml_override_env_defaults_to_canonical(self):
        from execution.engine.main import _engine_config_from_dict

        cfg = _engine_config_from_dict({})
        self.assertEqual(
            cfg.allow_recovery_mismatch_env,
            "K2BI_ALLOW_RECOVERY_MISMATCH",
        )

    def test_yaml_regime_file_preserved(self):
        # Codex round-13 P2: YAML-configured regime_file must reach
        # EngineConfig so a remapped publisher path is honored.
        from execution.engine.main import _engine_config_from_dict

        cfg = _engine_config_from_dict({"regime_file": "/tmp/custom-regime.md"})
        self.assertEqual(str(cfg.regime_file), "/tmp/custom-regime.md")

    def test_yaml_retired_dir_preserved(self):
        # Bundle 3 cycle 3 R1-minimax: YAML-configured retired_dir must
        # reach EngineConfig. Without the wiring, deployments that
        # configure a non-default sentinel base silently fall back to
        # kill_path.parent / DEFAULT_RETIRED_DIR and the config is inert.
        from execution.engine.main import _engine_config_from_dict

        cfg = _engine_config_from_dict({"retired_dir": "/tmp/custom-retired"})
        self.assertEqual(str(cfg.retired_dir), "/tmp/custom-retired")

    def test_yaml_retired_dir_defaults_to_none(self):
        # Absent key -> None so the engine's _retired_dir() chain picks
        # kill_path.parent (test fixtures) or DEFAULT_RETIRED_DIR (prod).
        from execution.engine.main import _engine_config_from_dict

        cfg = _engine_config_from_dict({})
        self.assertIsNone(cfg.retired_dir)

    def test_yaml_retired_dir_empty_string_treated_as_none(self):
        # R11-minimax claim check: an explicit empty-string
        # retired_dir must not resolve to Path("") (which equals CWD
        # and would silently disable the gate). The truthiness guard
        # in _engine_config_from_dict handles this.
        from execution.engine.main import _engine_config_from_dict

        cfg = _engine_config_from_dict({"retired_dir": ""})
        self.assertIsNone(cfg.retired_dir)


class FillDedupeTests(unittest.IsolatedAsyncioTestCase):
    """Codex round-5 P2: same-second polls of IBKR's ExecutionFilter
    can return a previously-seen fill. Engine must dedupe by exec_id."""

    async def asyncSetUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.base = Path(self._tmp.name)
        self.journal_dir = self.base / "journal"
        self.journal_dir.mkdir()
        self.strategies_dir = self.base / "strategies"
        self.strategies_dir.mkdir()
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
                kill_path=self.base / ".killed",
            ),
        )

    async def asyncTearDown(self):
        self._tmp.cleanup()

    async def _patch_now(self, patched: datetime) -> None:
        import execution.engine.main as main_mod
        from datetime import datetime as real_dt

        class _PatchedDT(real_dt):
            @classmethod
            def now(cls, tz=None):
                return patched if tz is None else patched.astimezone(tz)

        self._orig_dt = main_mod.datetime
        main_mod.datetime = _PatchedDT

    async def _unpatch_now(self) -> None:
        import execution.engine.main as main_mod

        main_mod.datetime = self._orig_dt

    async def test_same_exec_id_not_counted_twice(self):
        await self._patch_now(_mid_session_utc())
        try:
            await self.engine.tick_once()  # INIT
            await self.engine.tick_once()  # submit
            submitted = self.connector.submitted_orders[0]

            fill = BrokerExecution(
                exec_id="E-once",
                broker_order_id=submitted.broker_order_id,
                broker_perm_id=submitted.broker_perm_id,
                ticker="SPY",
                side="buy",
                qty=10,
                price=Decimal("500"),
                filled_at=_mid_session_utc(),
            )
            # The same fill is visible on TWO consecutive polls
            # (simulates second-precision ExecutionFilter returning
            # the same row on same-second poll).
            self.connector.executions_history.append(fill)
            self.connector.positions = [
                BrokerPosition(ticker="SPY", qty=10, avg_price=Decimal("500"))
            ]
            # First poll -> journal fill once, transition to idle.
            tick3 = await self.engine.tick_once()
            self.assertEqual(tick3.orders_filled, 1)

            # Reset engine to AWAITING_FILL to simulate a second poll of
            # the same execution. More realistic: inject a second fill
            # with the SAME exec_id and verify engine ignores it.
            # Force the engine to re-enter AWAITING to retest dedupe.
            from execution.engine.main import AwaitingOrderState

            self.engine._pending_order = AwaitingOrderState(
                trade_id="T2",
                strategy="spy-rotational",
                order=Order(
                    ticker="SPY",
                    side="buy",
                    qty=10,
                    limit_price=Decimal("500"),
                    stop_loss=Decimal("495"),
                    strategy="spy-rotational",
                    submitted_at=_mid_session_utc(),
                ),
                broker_order_id=submitted.broker_order_id,
                broker_perm_id=submitted.broker_perm_id,
                submitted_at=_mid_session_utc(),
                applied_exec_ids={"E-once"},  # already applied
            )
            from execution.engine.main import EngineState

            self.engine.state = EngineState.AWAITING_FILL
            baseline_fills = len([
                e for e in self.journal.read_all()
                if e["event_type"] == "order_filled"
            ])

            tick4 = await self.engine.tick_once()
            # The duplicated execution must NOT be counted again.
            post_fills = len([
                e for e in self.journal.read_all()
                if e["event_type"] == "order_filled"
            ])
            self.assertEqual(post_fills, baseline_fills)
        finally:
            await self._unpatch_now()


class HaltedStateTests(unittest.IsolatedAsyncioTestCase):
    """State-machine completeness per Keith's post-R21 ruling:
    refuse-to-start paths use a distinct HALTED state, not SHUTDOWN,
    so invest-execute can surface the operational distinction
    (investigate vs just restart) + later ticks early-return without
    touching state."""

    async def asyncSetUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.base = Path(self._tmp.name)
        self.journal_dir = self.base / "journal"
        self.journal_dir.mkdir()
        self.strategies_dir = self.base / "strategies"
        self.strategies_dir.mkdir()
        _write_strategy(self.strategies_dir)
        self.connector = MockIBKRConnector()
        self.journal = JournalWriter(base_dir=self.journal_dir, git_sha="test01")

    async def asyncTearDown(self):
        self._tmp.cleanup()

    def _make_engine(self) -> Engine:
        return Engine(
            connector=self.connector,
            journal=self.journal,
            validator_config=CONFIG,
            engine_config=EngineConfig(
                tick_seconds=DEFAULT_TICK_SECONDS,
                strategies_dir=self.strategies_dir,
                kill_path=self.base / ".killed",
            ),
        )

    async def test_halted_state_is_terminal_for_tick_loop(self):
        # Seed phantom position to trigger recovery_state_mismatch_refused.
        self.connector.positions = [
            BrokerPosition(ticker="NVDA", qty=5, avg_price=Decimal("600"))
        ]
        engine = self._make_engine()
        tick1 = await engine.tick_once()
        self.assertEqual(tick1.state_after, EngineState.HALTED)
        # Subsequent ticks stay HALTED (no state drift, no duplicate
        # stop records).
        tick2 = await engine.tick_once()
        self.assertEqual(tick2.state_after, EngineState.HALTED)
        events = self.journal.read_all()
        stopped = [e for e in events if e["event_type"] == "engine_stopped"]
        self.assertEqual(len(stopped), 1)
        self.assertEqual(stopped[0]["payload"]["terminal_state"], "halted")

    async def test_shutdown_preserves_halted_state(self):
        # Running _shutdown after halt must NOT relabel state as
        # SHUTDOWN -- that would lose the audit trail distinction.
        self.connector.positions = [
            BrokerPosition(ticker="NVDA", qty=5, avg_price=Decimal("600"))
        ]
        engine = self._make_engine()
        await engine.tick_once()
        self.assertEqual(engine.state, EngineState.HALTED)
        await engine._shutdown()
        self.assertEqual(engine.state, EngineState.HALTED)

    async def test_missing_ib_async_halts_with_clear_reason(self):
        # Codex R21 P2: ConnectorImportError previously escaped the
        # state machine. Now it halts cleanly.
        from execution.connectors.ibkr import ConnectorImportError

        async def fail_connect():
            raise ConnectorImportError(
                "ib_async is not installed in this environment."
            )

        self.connector.connect = fail_connect  # type: ignore[assignment]
        self.connector.set_connected(False)
        engine = self._make_engine()
        tick = await engine.tick_once()
        self.assertEqual(tick.state_after, EngineState.HALTED)
        events = self.journal.read_all()
        stopped = [e for e in events if e["event_type"] == "engine_stopped"]
        self.assertEqual(len(stopped), 1)
        self.assertEqual(
            stopped[0]["payload"]["reason"],
            "connector_import_failed",
        )


class RunOnceTests(unittest.IsolatedAsyncioTestCase):
    """Codex round-12 P1: --once must exercise a real tick body from
    a fresh process, not just init."""

    async def asyncSetUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.base = Path(self._tmp.name)
        self.journal_dir = self.base / "journal"
        self.journal_dir.mkdir()
        self.strategies_dir = self.base / "strategies"
        self.strategies_dir.mkdir()
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
                kill_path=self.base / ".killed",
            ),
        )

    async def asyncTearDown(self):
        self._tmp.cleanup()

    async def _patch_now(self, patched: datetime) -> None:
        import execution.engine.main as main_mod
        from datetime import datetime as real_dt

        class _PatchedDT(real_dt):
            @classmethod
            def now(cls, tz=None):
                return patched if tz is None else patched.astimezone(tz)

        self._orig_dt = main_mod.datetime
        main_mod.datetime = _PatchedDT

    async def _unpatch_now(self) -> None:
        import execution.engine.main as main_mod

        main_mod.datetime = self._orig_dt

    async def test_run_once_reaches_strategy_evaluation(self):
        await self._patch_now(_mid_session_utc())
        try:
            result = await self.engine.run_once()
            # With the fix, run_once's second tick exercises
            # _run_tick_body() and submits the approved strategy.
            self.assertEqual(result.orders_submitted, 1)
            self.assertEqual(len(self.connector.submitted_orders), 1)
            events = self.journal.read_all()
            event_types = [e["event_type"] for e in events]
            self.assertIn("engine_started", event_types)
            self.assertIn("order_submitted", event_types)
        finally:
            await self._unpatch_now()


class ReconnectBackoffTests(unittest.TestCase):
    def test_backoff_progression(self):
        self.assertEqual(_reconnect_delay(0), 5.0)
        self.assertEqual(_reconnect_delay(1), 10.0)
        self.assertEqual(_reconnect_delay(2), 20.0)
        self.assertEqual(_reconnect_delay(3), 40.0)
        self.assertEqual(_reconnect_delay(4), 80.0)
        self.assertEqual(_reconnect_delay(5), 160.0)
        self.assertEqual(_reconnect_delay(6), 300.0)
        self.assertEqual(_reconnect_delay(7), 300.0)
        # Sanity: large attempts don't overflow.
        self.assertEqual(_reconnect_delay(100_000), 300.0)

    def test_negative_attempt_is_clamped(self):
        self.assertEqual(_reconnect_delay(-1), 5.0)


class InitReentryAfterDisconnectTests(unittest.IsolatedAsyncioTestCase):
    """Codex round-6 P1: disconnect during INIT -> reconnect must
    re-enter INIT, not jump to CONNECTED_IDLE with empty state."""

    async def asyncSetUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.base = Path(self._tmp.name)
        self.journal_dir = self.base / "journal"
        self.journal_dir.mkdir()
        self.strategies_dir = self.base / "strategies"
        self.strategies_dir.mkdir()
        _write_strategy(self.strategies_dir)

        self.connector = MockIBKRConnector()
        self.connector.fail_connect_with_disconnect = True
        self.journal = JournalWriter(base_dir=self.journal_dir, git_sha="test01")
        self.engine = Engine(
            connector=self.connector,
            journal=self.journal,
            validator_config=CONFIG,
            engine_config=EngineConfig(
                tick_seconds=DEFAULT_TICK_SECONDS,
                strategies_dir=self.strategies_dir,
                kill_path=self.base / ".killed",
            ),
        )
        # Patch asyncio.sleep to avoid real waits.
        async def fake_sleep(_delay):
            return None

        import execution.engine.main as main_mod

        self._orig_sleep = main_mod.asyncio.sleep
        main_mod.asyncio.sleep = fake_sleep  # type: ignore[assignment]

    async def asyncTearDown(self):
        import execution.engine.main as main_mod

        main_mod.asyncio.sleep = self._orig_sleep  # type: ignore[assignment]
        self._tmp.cleanup()

    async def test_reconnect_before_init_returns_to_init(self):
        # First tick fails during INIT -> DISCONNECTED.
        tick1 = await self.engine.tick_once()
        self.assertEqual(tick1.state_after, EngineState.DISCONNECTED)
        self.assertFalse(self.engine._init_completed)

        # Clear the failure; next tick reconnects.
        self.connector.fail_connect_with_disconnect = False
        tick2 = await self.engine.tick_once()
        # Must re-enter INIT (not CONNECTED_IDLE) because init never
        # completed.
        self.assertEqual(tick2.state_after, EngineState.INIT)

        # Third tick runs init fully.
        tick3 = await self.engine.tick_once()
        self.assertEqual(tick3.state_after, EngineState.CONNECTED_IDLE)
        self.assertTrue(self.engine._init_completed)
        events = self.journal.read_all()
        event_types = [e["event_type"] for e in events]
        self.assertIn("engine_started", event_types)

    async def test_resume_normalizes_naive_submitted_at_to_utc(self):
        # Codex R20 P2: fromisoformat returns naive for values without
        # offset. _poll_awaiting later subtracts a tz-aware now() from
        # it, which raises TypeError. Normalize to UTC in validation.
        from execution.engine.main import _pick_resumable_awaiting
        from execution.engine.recovery import ReconciliationEvent, ReconciliationResult, RecoveryStatus

        event = ReconciliationEvent(
            event_type="recovery_reconciled",
            payload={
                "case": "pending_still_open",
                "filled_qty": 0,
                "remaining_qty": 10,
                "journal_view": {
                    "ticker": "SPY",
                    "side": "buy",
                    "qty": 10,
                    "limit_price": "500",
                    "submitted_at": "2026-05-05T10:00:00",  # no tz
                },
            },
            trade_id="T1",
            strategy="spy-rotational",
            broker_order_id="1000",
            broker_perm_id="2000000",
            ticker="SPY",
        )
        reco = ReconciliationResult(
            status=RecoveryStatus.CATCH_UP,
            events=[event],
        )
        resumed = _pick_resumable_awaiting(reco, journal_tail=[])
        self.assertIsNotNone(resumed)
        self.assertIsNotNone(resumed.submitted_at.tzinfo)
        self.assertEqual(resumed.submitted_at.utcoffset(), timedelta(0))

    async def test_resume_refuses_on_corrupt_submitted_at(self):
        # Codex R19 P2 + architect post-R19 type-level ruling: after
        # three rounds of scattered defensive wraps (R16 Decimal, R18
        # qty, R19 datetime) the resume path validates every
        # journal_view field at a single seam. Corrupt submitted_at
        # now refuses resume like corrupt qty does.
        from execution.engine.main import _pick_resumable_awaiting
        from execution.engine.recovery import ReconciliationEvent, ReconciliationResult, RecoveryStatus

        event = ReconciliationEvent(
            event_type="recovery_reconciled",
            payload={
                "case": "pending_still_open",
                "filled_qty": 0,
                "remaining_qty": 10,
                "journal_view": {
                    "ticker": "SPY",
                    "side": "buy",
                    "qty": 10,
                    "limit_price": "500",
                    "submitted_at": "not-an-iso-timestamp",
                },
            },
            trade_id="T1",
            strategy="spy-rotational",
            broker_order_id="1000",
            broker_perm_id="2000000",
            ticker="SPY",
        )
        reco = ReconciliationResult(
            status=RecoveryStatus.CATCH_UP,
            events=[event],
        )
        resumed = _pick_resumable_awaiting(reco, journal_tail=[])
        self.assertIsNone(resumed)

    async def test_resume_refuses_on_invalid_side(self):
        # Validation also catches bogus side values (defense against
        # a journal-payload schema drift or manual edit).
        from execution.engine.main import _pick_resumable_awaiting
        from execution.engine.recovery import ReconciliationEvent, ReconciliationResult, RecoveryStatus

        event = ReconciliationEvent(
            event_type="recovery_reconciled",
            payload={
                "case": "pending_still_open",
                "filled_qty": 0,
                "remaining_qty": 10,
                "journal_view": {
                    "ticker": "SPY",
                    "side": "long",  # invalid: must be buy or sell
                    "qty": 10,
                    "limit_price": "500",
                    "submitted_at": "2026-05-05T10:00:00+00:00",
                },
            },
            trade_id="T1",
            strategy="spy-rotational",
            broker_order_id="1000",
            broker_perm_id="2000000",
            ticker="SPY",
        )
        reco = ReconciliationResult(
            status=RecoveryStatus.CATCH_UP,
            events=[event],
        )
        resumed = _pick_resumable_awaiting(reco, journal_tail=[])
        self.assertIsNone(resumed)

    async def test_resume_refuses_on_corrupt_qty(self):
        # Codex R18 P2: resuming with qty=0 (from corrupt journal)
        # would trigger filled_qty >= order.qty on the next poll and
        # clear _pending_order while the broker order is still live.
        # Refuse resume instead -- broker phantom_open_order will
        # surface on next reconcile to give Keith the explicit signal.
        from execution.engine.main import _pick_resumable_awaiting
        from execution.engine.recovery import ReconciliationEvent, ReconciliationResult, RecoveryStatus

        for bad_qty in ("garbage", None, 0, -5):
            event = ReconciliationEvent(
                event_type="recovery_reconciled",
                payload={
                    "case": "pending_still_open",
                    "filled_qty": 0,
                    "remaining_qty": 10,
                    "journal_view": {
                        "ticker": "SPY",
                        "side": "buy",
                        "qty": bad_qty,
                        "limit_price": "500",
                        "submitted_at": "2026-05-05T10:00:00+00:00",
                    },
                },
                trade_id="T1",
                strategy="spy-rotational",
                broker_order_id="1000",
                broker_perm_id="2000000",
                ticker="SPY",
            )
            reco = ReconciliationResult(
                status=RecoveryStatus.CATCH_UP,
                events=[event],
            )
            resumed = _pick_resumable_awaiting(reco, journal_tail=[])
            self.assertIsNone(
                resumed,
                f"should refuse resume when qty={bad_qty!r}",
            )

    async def test_resume_degrades_gracefully_on_corrupt_decimals(self):
        # R16-minimax: _pick_resumable_awaiting must not crash when
        # journal_view has garbage in stop_loss or limit_price fields.
        from execution.engine.main import _pick_resumable_awaiting
        from execution.engine.recovery import ReconciliationEvent, ReconciliationResult, RecoveryStatus

        event = ReconciliationEvent(
            event_type="recovery_reconciled",
            payload={
                "case": "pending_still_open",
                "broker_status": "Submitted",
                "filled_qty": 0,
                "remaining_qty": 10,
                "journal_view": {
                    "ticker": "SPY",
                    "side": "buy",
                    "qty": 10,
                    "limit_price": "corrupted",
                    "stop_loss": "also-bad",
                    "submitted_at": "2026-05-05T10:00:00+00:00",
                },
            },
            trade_id="T1",
            strategy="spy-rotational",
            broker_order_id="1000",
            broker_perm_id="2000000",
            ticker="SPY",
        )
        reco = ReconciliationResult(
            status=RecoveryStatus.CATCH_UP,
            events=[event],
        )
        # Must not raise.
        resumed = _pick_resumable_awaiting(reco, journal_tail=[])
        self.assertIsNotNone(resumed)
        # Graceful degradation: limit_price -> 0, stop_loss -> None.
        self.assertEqual(resumed.order.limit_price, Decimal("0"))
        self.assertIsNone(resumed.order.stop_loss)

    async def test_resumed_awaiting_preserves_stop_loss(self):
        # R15-minimax finding: _pick_resumable_awaiting must extract
        # stop_loss from journal_view so the resumed AwaitingOrderState
        # carries it. Broker's bracket child still holds the protective
        # stop, but engine-internal tracking also needs the reference.
        from execution.engine.main import _pick_resumable_awaiting
        from execution.engine.recovery import ReconciliationEvent, ReconciliationResult, RecoveryStatus

        # Synthesize a reco result with one pending_still_open whose
        # journal_view includes stop_loss.
        event = ReconciliationEvent(
            event_type="recovery_reconciled",
            payload={
                "case": "pending_still_open",
                "broker_status": "Submitted",
                "filled_qty": 0,
                "remaining_qty": 10,
                "journal_view": {
                    "ticker": "SPY",
                    "side": "buy",
                    "qty": 10,
                    "limit_price": "500",
                    "stop_loss": "495",
                    "submitted_at": "2026-05-05T10:00:00+00:00",
                },
            },
            trade_id="T1",
            strategy="spy-rotational",
            broker_order_id="1000",
            broker_perm_id="2000000",
            ticker="SPY",
        )
        reco = ReconciliationResult(
            status=RecoveryStatus.CATCH_UP,
            events=[event],
        )
        resumed = _pick_resumable_awaiting(reco, journal_tail=[])
        self.assertIsNotNone(resumed)
        self.assertEqual(resumed.order.stop_loss, Decimal("495"))

    async def test_init_after_reconnect_does_not_double_connect(self):
        # Codex round-14 P2: if the previous reconnect established a
        # live session before init completed, _run_init must NOT call
        # connect() again (would double-connect against the same IB
        # Gateway). We simulate by seeding a connected mock + flagging
        # the NEXT connect as a disconnect failure. If _run_init
        # skips the connect call, init completes; if it calls connect
        # it trips the injected failure and enters DISCONNECTED.
        self.connector.set_connected(True)
        self.connector.fail_connect_with_disconnect = True  # would fail IF called
        self.engine.state = EngineState.INIT
        tick = await self.engine.tick_once()
        self.assertEqual(tick.state_after, EngineState.CONNECTED_IDLE)
        self.assertTrue(self.engine._init_completed)


class ReconnectIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.base = Path(self._tmp.name)
        self.journal_dir = self.base / "journal"
        self.journal_dir.mkdir()
        self.strategies_dir = self.base / "strategies"
        self.strategies_dir.mkdir()
        _write_strategy(self.strategies_dir)

        self.connector = MockIBKRConnector()
        self.connector.fail_connect_with_disconnect = True
        self.journal = JournalWriter(base_dir=self.journal_dir, git_sha="test01")
        self.engine = Engine(
            connector=self.connector,
            journal=self.journal,
            validator_config=CONFIG,
            engine_config=EngineConfig(
                tick_seconds=DEFAULT_TICK_SECONDS,
                strategies_dir=self.strategies_dir,
                kill_path=self.base / ".killed",
            ),
        )
        # Patch asyncio.sleep used inside _attempt_reconnect so tests
        # don't wait 5+ seconds.
        self._sleep_calls: list[float] = []

        async def fake_sleep(delay):
            self._sleep_calls.append(delay)

        import execution.engine.main as main_mod

        self._orig_sleep = main_mod.asyncio.sleep
        main_mod.asyncio.sleep = fake_sleep  # type: ignore[assignment]

    async def asyncTearDown(self):
        import execution.engine.main as main_mod

        main_mod.asyncio.sleep = self._orig_sleep  # type: ignore[assignment]
        self._tmp.cleanup()

    async def test_disconnect_then_auth_emits_auth_required(self):
        # Codex round-9 P2: outage starts as a plain disconnect but a
        # later reconnect surfaces AuthRequired. The engine must
        # journal auth_required on the transition, not just
        # disconnect_status.
        tick1 = await self.engine.tick_once()
        self.assertEqual(tick1.state_after, EngineState.DISCONNECTED)
        # Initial disconnect was not auth -- verify.
        events_after_init = self.journal.read_all()
        auth_events_initial = [
            e for e in events_after_init if e["event_type"] == "auth_required"
        ]
        self.assertEqual(auth_events_initial, [])

        # Next reconnect attempt flips to auth-required.
        self.connector.fail_connect_with_disconnect = False
        self.connector.fail_connect_with_auth = True

        tick2 = await self.engine.tick_once()
        self.assertEqual(tick2.state_after, EngineState.DISCONNECTED)
        self.assertTrue(tick2.auth_required)
        events = self.journal.read_all()
        auth_events = [
            e for e in events if e["event_type"] == "auth_required"
        ]
        self.assertEqual(len(auth_events), 1)
        self.assertEqual(
            auth_events[0]["payload"]["transitioned_from"],
            "DisconnectedError",
        )

    async def test_disconnect_then_reconnect(self):
        # First tick: INIT raises DisconnectedError during connect, so
        # engine enters DISCONNECTED.
        tick1 = await self.engine.tick_once()
        self.assertEqual(tick1.state_after, EngineState.DISCONNECTED)

        # Flip the mock to succeed next connect attempt.
        self.connector.fail_connect_with_disconnect = False

        tick2 = await self.engine.tick_once()
        # Codex round-6 P1: reconnect-before-init-complete must return
        # to INIT so recovery + engine_started still run on the next
        # tick. Previous behavior jumped to CONNECTED_IDLE with empty
        # state.
        self.assertEqual(tick2.state_after, EngineState.INIT)
        self.assertTrue(tick2.reconnected)
        # Reconnect delay applied once.
        self.assertGreaterEqual(len(self._sleep_calls), 1)
        self.assertEqual(self._sleep_calls[0], 5.0)

        events = self.journal.read_all()
        event_types = [e["event_type"] for e in events]
        self.assertIn("reconnected", event_types)


if __name__ == "__main__":
    unittest.main()
