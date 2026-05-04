# cash-only invariant: the submit path below invokes
# execution.validators.runner.run_all, which ends with the leverage
# validator, which delegates to execution.risk.cash_only.check_sell_covered.
# The engine's own pre-submit backstop additionally calls
# cash_only.check_sell_covered directly for every sell before touching
# the connector -- never add a shortcut that skips either check.
"""Engine main loop -- Bundle 2 m2.6.

Single-process, single-event-loop. State machine per
wiki/planning/m2.6-engine-state-machine.md. All state transitions are
logged via the decision journal; the state variable in memory is
secondary to the journal record.

Composition:
    - IBKRConnectorProtocol (live or mock) owns broker I/O
    - JournalWriter owns the audit trail
    - strategies.loader + runner own strategy logic
    - validators.runner owns pre-trade enforcement
    - risk.circuit_breakers owns account-level breakers
    - risk.kill_switch owns the .killed gate
    - engine.recovery owns crash-restart reconciliation

Phase 2 scope is intentionally narrow:
    - Single strategy at a time (first real strategy = SPY rotational
      in Phase 3). Multi-strategy concurrency is Phase 4+.
    - At most one in-flight order. Partial fills stay in AWAITING_FILL
      until terminal.
    - Order-timeout cancels via broker + journals order_timeout.
    - EOD at configured UTC wall-clock time cancels DAY orders.
    - Reconnect backoff: 5s start, 2x, 300s cap, infinite (architect Q4).
    - Per-attempt reconnect journaling is suppressed; disconnect_status
      emits every 5 min during continuous outage (architect Q4-refined).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import signal
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from enum import Enum
from pathlib import Path
from typing import Any

from ..connectors.ibkr import ConnectorImportError
from ..connectors.types import (
    AuthRequiredError,
    BrokerOrderAck,
    BrokerPosition,
    ConnectorError,
    DisconnectedError,
    IBKRConnectorProtocol,
    LIVE_ORDER_STATUSES,
    TERMINAL_ORDER_STATUSES,
)
from ..journal.schema import reject_non_finite_json_constant
from ..journal.ulid import new_ulid
from ..journal.writer import JournalWriter
from ..risk import cash_only, kill_switch
from ..risk.circuit_breakers import BreakerConfig, any_hard_tripped, apply_kill_on_trip, evaluate
from ..risk.types import AccountState
from ..strategies import loader as strategy_loader
from ..strategies import runner as strategy_runner
from ..strategies.types import (
    ApprovedStrategySnapshot,
    CandidateOrder,
    MarketSnapshot,
    STATUS_APPROVED,
)
from ..validators.config import load_config
from ..validators.runner import as_journal_payload, run_all
from ..validators.types import Order, Position, RiskContext
from . import recovery as recovery_mod

LOG = logging.getLogger("k2bi.engine")

# ---------- constants ----------

RECONNECT_START_SECONDS = 5.0
RECONNECT_MULT = 2.0
RECONNECT_CAP_SECONDS = 300.0
DISCONNECT_STATUS_INTERVAL = timedelta(minutes=5)
DEFAULT_TICK_SECONDS = 30.0
DEFAULT_FILL_TIMEOUT_SECONDS = 60.0
# Q33 (MiniMax R1 finding #3, 2026-04-21): cap the pre-exit barrier
# wait so a misconfigured deployment cannot hang --once indefinitely.
# 300s is generous vs the 10s default but firmly rules out accidental
# 86400 or float('inf') values surfacing in production.
ONCE_EXIT_WAIT_SECONDS_MAX = 300.0
# Codex round-11 P1: the EOD cutoff must be DST-safe. NYSE closes at
# 16:00 US/Eastern year-round, which is 20:00 UTC in summer (EDT) and
# 21:00 UTC in winter (EST). A naive UTC-only default is wrong for
# half the year -- 20:30 UTC during EST fires 30 minutes BEFORE
# close. The cutoff string is now interpreted as US/Eastern local
# time and converted at check-time via zoneinfo, so 16:30 ET always
# means "30 min after close" regardless of season.
DEFAULT_EOD_ET = "16:30"  # US/Eastern local time, ~30 min after NYSE close

DEFAULT_STRATEGIES_DIR = (
    Path.home() / "Projects" / "K2Bi-Vault" / "wiki" / "strategies"
)

DEFAULT_REGIME_FILE = (
    Path.home() / "Projects" / "K2Bi-Vault" / "wiki" / "regimes" / "current.md"
)


class EngineState(str, Enum):
    INIT = "init"
    CONNECTED_IDLE = "connected_idle"
    PROCESSING_TICK = "processing_tick"
    SUBMITTING = "submitting"
    AWAITING_FILL = "awaiting_fill"
    RECONCILING = "reconciling"
    KILLED = "killed"
    DISCONNECTED = "disconnected"
    # Engine refused to operate because a recovery-time safety check
    # failed (phantom position, multi-still-open, refuse-resume with
    # live broker order, strategy load failure, missing ib_async).
    # Distinct from SHUTDOWN which is a graceful exit via SIGTERM /
    # SIGINT / manual stop. Invest-execute status surfaces the
    # distinction so Keith knows whether to investigate vs just
    # relaunch.
    HALTED = "halted"
    SHUTDOWN = "shutdown"


# States the tick loop treats as "engine no longer operates; early
# return". Added HALTED alongside SHUTDOWN so the refuse-to-start
# paths get a distinct operational label without changing tick
# control flow.
_TERMINAL_STATES = frozenset({EngineState.HALTED, EngineState.SHUTDOWN})


@dataclass
class EngineConfig:
    """Engine-specific tunables. Validator config is a separate file
    (execution/validators/config.yaml) already owned by Bundle 1.

    Fields not present in validator config.yaml's top level live here;
    defaults are Phase 2 MVP sensible, overridable via config.yaml's
    `engine:` section or direct keyword arguments when constructing
    for tests.
    """

    tick_seconds: float = DEFAULT_TICK_SECONDS
    fill_timeout_seconds: float = DEFAULT_FILL_TIMEOUT_SECONDS
    # US/Eastern local time, HH:MM. Converted to today's UTC instant
    # each tick via zoneinfo so EDT/EST transitions are handled.
    eod_et_time: str = DEFAULT_EOD_ET
    strategies_dir: Path = field(default_factory=lambda: DEFAULT_STRATEGIES_DIR)
    kill_path: Path | None = None  # None -> kill_switch.DEFAULT_KILL_PATH
    # Base dir holding per-strategy `.retired-<slug>` sentinels (Bundle 3
    # m2.17, Q7). None falls back to kill_path.parent when a custom
    # kill_path is set -- so test fixtures that scope .killed to a tmp
    # dir automatically scope retirement sentinels to the same tmp dir
    # and cannot accidentally read the real vault's sentinels. With no
    # custom kill_path either, falls back to kill_switch.DEFAULT_RETIRED_DIR.
    retired_dir: Path | None = None
    allow_recovery_mismatch_env: str = recovery_mod.RECOVERY_OVERRIDE_ENV
    # Q42 (2026-04-26): per-permId orphan-STOP adoption env var name.
    # Configurable for testability (mirrors allow_recovery_mismatch_env).
    # When os.environ[<this name>] is set to "<permId>:<justification>"
    # AND a broker open order matches that permId AND the order is a
    # STOP, recovery writes an orphan_stop_adopted journal event and
    # the matching mismatch is removed (engine starts cleanly without
    # K2BI_ALLOW_RECOVERY_MISMATCH=1). Malformed input is fatal at
    # startup with sys.exit(78). See execution.engine.recovery for
    # the parser + adoption logic.
    adopt_orphan_stop_env: str = recovery_mod.ADOPT_ORPHAN_STOP_ENV
    # Q33 (2026-04-21): wall-clock window `run_once()` waits for
    # broker terminal status after the submit body leaves the engine
    # in AWAITING_FILL. Covers the Session F fill-callback race where
    # --once exited before IBKR's fill callback reached the journal.
    # On timeout, the engine journals `once_exit_barrier_timeout`
    # with the pending-order trade_ids so Q39-B's recovery can
    # promote evidence to barrier_timeout on the next restart.
    once_exit_wait_seconds: float = 10.0

    def __post_init__(self) -> None:
        # Q33 MiniMax R1 finding #3 (2026-04-21): clamp once_exit_wait_seconds
        # at ONCE_EXIT_WAIT_SECONDS_MAX. Zero / negative values keep
        # their meaning (barrier disabled); anything above the cap is
        # clamped down. __post_init__ runs whether EngineConfig was
        # built by hand or by _engine_config_from_dict, so both paths
        # are covered.
        if self.once_exit_wait_seconds > ONCE_EXIT_WAIT_SECONDS_MAX:
            self.once_exit_wait_seconds = ONCE_EXIT_WAIT_SECONDS_MAX

    # Codex round-12 P1: regime source for strategy gating. The file
    # is vault-side + populated by invest-regime (Phase 2 manual
    # classification, Phase 4 auto). Missing file -> current_regime
    # stays None and strategies with regime_filter are BLOCKED rather
    # than silently bypassing the filter.
    regime_file: Path = field(default_factory=lambda: DEFAULT_REGIME_FILE)


@dataclass
class AwaitingOrderState:
    """Tracks one in-flight order until the broker terminates it."""

    trade_id: str
    strategy: str
    order: Order
    broker_order_id: str
    broker_perm_id: str
    submitted_at: datetime
    filled_qty: int = 0
    last_poll_at: datetime | None = None
    # Codex round-5 P2: IBKR's ExecutionFilter only has second-level
    # precision, so repolling within the same wall-clock second can
    # return executions the engine has already journaled. Track
    # exec_ids we have applied so the poll loop dedupes.
    applied_exec_ids: set[str] = field(default_factory=set)
    # Codex round-8 P1: cancel_order() is asynchronous at IBKR; the
    # order can remain in "PendingCancel" / "Submitted" for a moment
    # after the API returns. Marking the order terminal before broker
    # confirms risks a restart-window where recovery sees the journal
    # as closed and the broker as still open (phantom_open_order).
    # Track cancel intent here so _poll_awaiting keeps polling until
    # the broker confirms terminal.
    cancel_requested: bool = False
    cancel_reason: str | None = None
    cancel_requested_at: datetime | None = None


@dataclass
class TickResult:
    """Structured view of what one tick did. Tests assert against this
    so the engine's behavior is observable without parsing the journal
    file."""

    state_before: EngineState
    state_after: EngineState
    killed: bool = False
    kill_cleared: bool = False
    reconnected: bool = False
    auth_required: bool = False
    disconnect_status_emitted: bool = False
    strategies_evaluated: int = 0
    orders_submitted: int = 0
    orders_filled: int = 0
    orders_rejected: int = 0
    orders_timed_out: int = 0
    breaker_trips: list[str] = field(default_factory=list)
    eod_ran: bool = False
    notes: list[str] = field(default_factory=list)


@dataclass
class Engine:
    """K2Bi execution engine.

    Construct with explicit dependencies so tests can swap in:
        - MockIBKRConnector for connector
        - A temp-dir JournalWriter
        - An in-memory config dict (bypassing YAML)

    Production wires real IBKRConnector + vault-path JournalWriter via
    `Engine.from_environment()`.
    """

    connector: IBKRConnectorProtocol
    journal: JournalWriter
    validator_config: dict[str, Any]
    engine_config: EngineConfig = field(default_factory=EngineConfig)
    breaker_config: BreakerConfig = field(default_factory=BreakerConfig)

    # runtime state
    state: EngineState = EngineState.INIT
    _strategies: list[ApprovedStrategySnapshot] = field(default_factory=list)
    _strategy_drift_warned: set[str] = field(default_factory=set)
    _positions: list[BrokerPosition] = field(default_factory=list)
    _pending_order: AwaitingOrderState | None = None

    # connection tracking
    _reconnect_attempts: int = 0
    _outage_started_at: datetime | None = None
    _last_disconnect_status_at: datetime | None = None
    _last_error_class: str | None = None
    # True once _run_init() has completed all init steps (connect,
    # reconcile, engine_started journal, strategy load). A reconnect
    # that follows an INIT-time disconnect must re-run init rather
    # than jumping straight to CONNECTED_IDLE with empty state
    # (Codex round-6 P1).
    _init_completed: bool = False

    # EOD tracking
    _last_eod_date_utc: str | None = None

    # shutdown
    _shutdown_requested: bool = False
    # Codex round-9 P2: init paths can emit engine_stopped with a
    # specific reason (recovery_state_mismatch_refused,
    # strategy_load_failed, recovery_multiple_still_open_orders) before
    # setting _shutdown_requested. The unconditional _shutdown() in
    # run_forever's finally would then emit a SECOND engine_stopped
    # with reason=graceful_shutdown, masking the true terminal
    # reason for downstream readers. Track whether stopping was
    # already recorded.
    _engine_stopped_journaled: bool = False

    @classmethod
    def from_environment(
        cls,
        connector: IBKRConnectorProtocol,
        *,
        journal_base: Path | None = None,
        validator_config_path: Path | None = None,
        engine_config: EngineConfig | None = None,
    ) -> "Engine":
        cfg_path = validator_config_path
        validator_config = load_config(cfg_path)
        eng_cfg = engine_config or _engine_config_from_dict(
            validator_config.get("engine", {})
        )
        journal = JournalWriter(base_dir=journal_base)
        return cls(
            connector=connector,
            journal=journal,
            validator_config=validator_config,
            engine_config=eng_cfg,
        )

    # ---------- lifecycle ----------

    async def run_forever(self) -> None:
        """Continuous mode. pm2 cron keeps this alive on the Mac Mini.

        Installs SIGTERM / SIGINT handlers for graceful shutdown.
        On shutdown, emits engine_stopped + disconnects cleanly.

        Codex round-13 P1: DISCONNECTED ticks already sleep inside
        _attempt_reconnect() for the full backoff window. Adding the
        normal tick_seconds on top of that would turn the documented
        5s start / 10s / 20s reconnect schedule into ~35s / 40s / 50s
        -- a materially delayed recovery. Skip the tick-spacing sleep
        when the last tick was a DISCONNECTED reconnect attempt; the
        backoff inside _attempt_reconnect is the authoritative delay.
        """
        self._install_signal_handlers()
        try:
            while not self._shutdown_requested:
                result = await self.tick_once()
                if self._shutdown_requested:
                    break
                # _attempt_reconnect already slept for the backoff
                # window; adding tick_seconds here would stack delays.
                if result.state_before == EngineState.DISCONNECTED:
                    continue
                await asyncio.sleep(self.engine_config.tick_seconds)
        finally:
            await self._shutdown()

    async def run_once(self) -> TickResult:
        """One full tick of useful work from a fresh process.

        Codex round-12 P1: a fresh Engine starts in INIT and the first
        tick_once() returns immediately after _run_init() with no
        strategy evaluation. `/execute run` and `--once` would then be
        init-only, defeating the purpose. When state is still INIT or
        DISCONNECTED after the first tick, run the next tick so the
        caller gets at least one trip through _run_tick_body() (or a
        clean reason the engine cannot proceed).

        Caps at 3 ticks so a repeat-disconnect doesn't loop forever.
        Tests that want finer control call tick_once() directly.

        Q33 (2026-04-21): if the body tick leaves the engine in
        AWAITING_FILL, enter a bounded pre-exit barrier before
        returning. Covers the Session F fill-callback race where
        --once exited between broker fill and journal write.
        """
        result = await self.tick_once()
        for _ in range(2):
            if result.state_after in {
                EngineState.CONNECTED_IDLE,
                EngineState.KILLED,
                EngineState.AWAITING_FILL,
                EngineState.SHUTDOWN,
                EngineState.HALTED,
            }:
                # Init complete OR terminal-refused; run body once
                # (if still alive) then exit.
                if result.state_after in _TERMINAL_STATES:
                    return result
                body_result = await self.tick_once()
                if body_result.state_after == EngineState.AWAITING_FILL:
                    await self._once_exit_barrier(body_result)
                    # Q33 MiniMax R2 finding #2 (2026-04-21): the
                    # barrier can transition self.state (kill, fill,
                    # disconnect). Refresh body_result.state_after so
                    # callers see the post-barrier state rather than
                    # the stale AWAITING_FILL snapshot taken before
                    # the barrier ran.
                    body_result.state_after = self.state
                return body_result
            if result.state_after == EngineState.DISCONNECTED:
                # Still trying to reconnect -- caller knows from state.
                return result
            # state is still INIT (shouldn't normally happen, but be
            # defensive): run again.
            result = await self.tick_once()
        return result

    async def _once_exit_barrier(self, result: TickResult) -> None:
        """Q33 (2026-04-21) pre-exit wait for pending-order resolution.

        Called by run_once() after the submit body returns with
        state_after=AWAITING_FILL. Polls broker state via tick_once
        (which routes through _poll_awaiting) until either:
            - the pending resolves (state transitions out of
              AWAITING_FILL, _pending_order is cleared), or
            - wall-time reaches EngineConfig.once_exit_wait_seconds.

        On timeout, journals `once_exit_barrier_timeout` with the
        architect-specified payload so Q39-B's next-restart recovery
        can promote evidence to `barrier_timeout`. The pending is NOT
        cancelled by the barrier -- it stays at the broker and
        recovery will reconcile it on the next run.
        """
        if (
            self._pending_order is None
            or self.state != EngineState.AWAITING_FILL
        ):
            return

        wait_seconds = float(self.engine_config.once_exit_wait_seconds)
        if wait_seconds <= 0:
            # Explicitly disabled; behave like pre-Q33 and just exit.
            return

        # Use monotonic wall-clock for the barrier window. datetime.now
        # can be monkey-patched in tests for session-time simulation
        # (see test_engine_main._PatchedDT); the barrier's timeout is
        # real wall time, not simulated time.
        start = time.monotonic()
        deadline = start + wait_seconds
        # Brief inner-poll spacing: tick_once() has its own i/o waits,
        # but the broker mock path can resolve synchronously. A small
        # sleep between polls keeps cpu usage sane while staying well
        # under the barrier window.
        poll_interval = min(0.25, wait_seconds / 4.0)

        # Pin the original pending trade_id. If _poll_awaiting clears
        # this pending and the engine's tick body re-entered and
        # submitted a fresh one, the barrier is done: we waited on the
        # specific order that run_once submitted, not on arbitrary
        # future orders.
        original_trade_id = self._pending_order.trade_id
        while (
            self._pending_order is not None
            and self._pending_order.trade_id == original_trade_id
            and self.state == EngineState.AWAITING_FILL
            and time.monotonic() < deadline
        ):
            # Q33 MiniMax R1 finding #2 (2026-04-21): kill-file check
            # inside the barrier loop. _poll_awaiting bypasses
            # tick_once's kill check, so a human writing .killed
            # during the barrier would be ignored for the full wait
            # window without this poll. Delegating to tick_once honors
            # the kill transition naturally, without disturbing the
            # barrier's narrow scope (no strategy submission because
            # the pending is still set at that point).
            if self._kill_file_present():
                kill_result = TickResult(
                    state_before=self.state, state_after=self.state
                )
                await self._transition_to_killed(kill_result)
                break
            # _poll_awaiting handles disconnect via _enter_disconnected
            # (state transitions out of AWAITING_FILL on failure); we
            # intentionally skip strategy-eval since the barrier's job
            # is narrow: poll the one pending order.
            poll_result = TickResult(
                state_before=self.state, state_after=self.state
            )
            await self._poll_awaiting(poll_result)
            if (
                self._pending_order is None
                or self._pending_order.trade_id != original_trade_id
                or self.state != EngineState.AWAITING_FILL
            ):
                break
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            await asyncio.sleep(min(poll_interval, remaining))

        if (
            self._pending_order is None
            or self._pending_order.trade_id != original_trade_id
            or self.state != EngineState.AWAITING_FILL
        ):
            return

        elapsed = time.monotonic() - start
        pending = self._pending_order
        self.journal.append(
            "once_exit_barrier_timeout",
            payload={
                "barrier_seconds_elapsed": elapsed,
                "last_known_state": self.state.value,
                "pending_orders": [
                    {
                        "trade_id": pending.trade_id,
                        "broker_order_id": pending.broker_order_id,
                        "broker_perm_id": pending.broker_perm_id,
                        "ticker": pending.order.ticker,
                        "side": pending.order.side,
                        "qty": pending.order.qty,
                        "limit_price": str(pending.order.limit_price),
                        "stop_loss": (
                            str(pending.order.stop_loss)
                            if pending.order.stop_loss is not None
                            else None
                        ),
                    }
                ],
            },
        )

    async def tick_once(self) -> TickResult:
        """One unit of engine work.

        Order of operations:
            1. INIT must run first if state is INIT -- we need to
               connect + reconcile + journal engine_started BEFORE we
               honor .killed. Init itself is defensive (reads only);
               no orders leave the engine during init even if kill is
               absent. Init closes by transitioning to KILLED itself
               if .killed is present.
            2. For non-INIT states: .killed check (spec: "ANY state ->
               KILLED on .killed detect"). Clearing also detected.
            3. Connection recovery.
            4. Tick body (strategy evaluation + submit).
        """
        state_before = self.state
        result = TickResult(state_before=state_before, state_after=state_before)

        if self.state == EngineState.INIT:
            await self._run_init(result)
            # _run_init transitions state itself (CONNECTED_IDLE,
            # AWAITING_FILL on resume, KILLED if .killed present, or
            # SHUTDOWN on mismatch-refused). Init is a full tick.
            result.state_after = self.state
            return result

        # kill transitions (applies to every non-INIT tick).
        if self._kill_file_present():
            if self.state != EngineState.KILLED:
                await self._transition_to_killed(result)
            result.killed = True
        elif self.state == EngineState.KILLED:
            await self._handle_kill_cleared(result)

        if self.state == EngineState.DISCONNECTED:
            await self._attempt_reconnect(result)
            result.state_after = self.state
            return result

        if self.state == EngineState.KILLED:
            # Killed but connected: still poll awaiting orders so fills
            # that complete under kill are journaled. Do NOT submit new
            # orders.
            if self._pending_order is not None:
                await self._poll_awaiting(result)
            result.state_after = self.state
            return result

        if self.state in _TERMINAL_STATES:
            # HALTED or SHUTDOWN: engine no longer operates. Early
            # return keeps the state label stable while pm2 / Keith
            # decides whether to restart.
            result.state_after = self.state
            return result

        # Main body: state is CONNECTED_IDLE or AWAITING_FILL (resume).
        await self._run_tick_body(result)
        result.state_after = self.state
        return result

    # ---------- init + recovery ----------

    async def _run_init(self, result: TickResult) -> None:
        # Codex round-14 P2: if _attempt_reconnect already established
        # a live session before init completed, calling connect() here
        # would double-connect (ib_async can fail or open a duplicate
        # client session against the same IB Gateway). Skip the
        # reconnect when the connector reports a healthy session.
        conn_status = self.connector.connection_status()
        if not conn_status.connected:
            try:
                await self.connector.connect()
            except ConnectorImportError as exc:
                # Codex R21 P2: missing ib_async is a permanent
                # condition on this host. Reconnect loop cannot
                # resolve it -- halt with a clean journal entry so
                # invest-execute status surfaces "install ib_async"
                # rather than a silent DISCONNECTED loop.
                LOG.error(
                    "engine: cannot start, ib_async import failed: %s",
                    exc,
                )
                self.journal.append(
                    "engine_stopped",
                    payload={
                        "reason": "connector_import_failed",
                        "error": str(exc),
                        "terminal_state": "halted",
                        "note": (
                            "ib_async missing; install via "
                            "`pip install ib_async==2.1.0` before "
                            "running the live engine."
                        ),
                    },
                )
                self._engine_stopped_journaled = True
                self.state = EngineState.HALTED
                self._shutdown_requested = True
                return
            except AuthRequiredError as exc:
                await self._enter_disconnected(result, exc, auth=True)
                return
            except (DisconnectedError, ConnectorError) as exc:
                await self._enter_disconnected(result, exc)
                return

        narrow_lookback_start = (
            datetime.now(timezone.utc) - recovery_mod.DEFAULT_LOOKBACK
        )
        ext_lookback_start = (
            datetime.now(timezone.utc)
            - recovery_mod.EXTENDED_CHECKPOINT_LOOKBACK
        )
        try:
            broker_positions = await self.connector.get_positions()
            broker_open_orders = await self.connector.get_open_orders()
            # broker_status uses the narrow window: order-status history
            # is recent broker activity, not a state checkpoint, and
            # widening it would conflict with the existing
            # reconciliation contract for terminal-state classification.
            broker_status = await self.connector.get_order_status_history(
                narrow_lookback_start
            )
        except (AuthRequiredError, DisconnectedError) as exc:
            await self._enter_disconnected(result, exc, auth=isinstance(exc, AuthRequiredError))
            return

        narrow_journal_tail = _read_recent_journal(
            self.journal, narrow_lookback_start
        )
        # Q42 +1 week carry-forward fix: prepend state-checkpoint
        # events from the extended window so multi-day engine-off gaps
        # don't re-flag previously-adopted positions / orphan STOPs as
        # phantoms. Older events first, narrow tail second -- recovery
        # replay walks forward and snapshot-resets per_ticker on each
        # `engine_recovered`, so the most recent in the combined
        # sequence wins.
        extended_checkpoints = _read_extended_checkpoints(
            self.journal,
            ext_since=ext_lookback_start,
            narrow_since=narrow_lookback_start,
        )
        journal_tail = extended_checkpoints + narrow_journal_tail

        # Codex round-3 P2: the EngineConfig-configurable override env
        # name must actually be consulted by reconcile(); otherwise a
        # deploy that remaps the name to, say, K2BI_PAPER_ALLOW_* would
        # still be ignored. Pass it through explicitly.
        override_env_value = os.environ.get(
            self.engine_config.allow_recovery_mismatch_env, ""
        )

        # Q42: parse K2BI_ADOPT_ORPHAN_STOP if set. Malformed input
        # is fatal -- silently ignoring it would let the operator
        # believe adoption is happening when it is not, and the
        # orphan would re-flag on the next cold start. sys.exit(78)
        # matches sysexits.h EX_CONFIG (operator config error before
        # any engine state mutates).
        try:
            adopt_request = recovery_mod._parse_adopt_orphan_stop(
                os.environ.get(self.engine_config.adopt_orphan_stop_env)
            )
        except ValueError as exc:
            LOG.error("FATAL: %s", exc)
            sys.exit(78)

        reco = recovery_mod.reconcile(
            journal_tail=journal_tail,
            broker_positions=broker_positions,
            broker_open_orders=broker_open_orders,
            broker_order_status=broker_status,
            now=datetime.now(timezone.utc),
            override_env=override_env_value,
            override_env_name=self.engine_config.allow_recovery_mismatch_env,
            adopt_orphan_stop=adopt_request,
            adopt_orphan_stop_env_name=self.engine_config.adopt_orphan_stop_env,
        )

        for event in reco.events:
            self._journal_recovery_event(event)

        if reco.status == recovery_mod.RecoveryStatus.MISMATCH_REFUSED:
            LOG.error(
                "engine: refusing to start due to recovery state mismatch. "
                "Set %s=1 after manual review to override.",
                self.engine_config.allow_recovery_mismatch_env,
            )
            self.journal.append(
                "engine_stopped",
                payload={
                    "reason": "recovery_state_mismatch_refused",
                    "mismatch_count": len(reco.mismatch_reasons),
                    "override_env": self.engine_config.allow_recovery_mismatch_env,
                    "terminal_state": "halted",
                },
            )
            self._engine_stopped_journaled = True
            self.state = EngineState.HALTED
            self._shutdown_requested = True
            return

        # Adopt broker state as engine state.
        self._positions = reco.adopted_positions
        # If there was a journal-pending order still open at broker,
        # resume in AWAITING_FILL. MVP handles at most one in-flight,
        # so multiple still-open orders must refuse-to-start -- silently
        # dropping them from engine state while IBKR still holds them
        # would let the engine emit a second order for the same strategy
        # next tick (Codex round-3 P1).
        still_open_count = sum(
            1
            for e in reco.events
            if e.event_type == "recovery_reconciled"
            and e.payload.get("case") == "pending_still_open"
        )
        if still_open_count > 1:
            LOG.error(
                "engine: %d still-open broker orders found during "
                "recovery; MVP handles at most one in-flight. "
                "Refusing to start.",
                still_open_count,
            )
            self.journal.append(
                "engine_stopped",
                payload={
                    "reason": "recovery_multiple_still_open_orders",
                    "still_open_count": still_open_count,
                    "adopted_positions": [
                        {"ticker": p.ticker, "qty": p.qty, "avg_price": str(p.avg_price)}
                        for p in self._positions
                    ],
                    "terminal_state": "halted",
                },
            )
            self._engine_stopped_journaled = True
            self.state = EngineState.HALTED
            self._shutdown_requested = True
            return

        resumed = _pick_resumable_awaiting(reco, journal_tail)
        if resumed is not None:
            self._pending_order = resumed
            self.state = EngineState.AWAITING_FILL
        elif still_open_count == 1:
            # Codex R20 P1: a broker-live order exists (1 pending_still_open
            # classified by recovery) but _pick_resumable_awaiting refused
            # to resume it (corrupt journal_view field). Falling through to
            # CONNECTED_IDLE would leave the engine trading with no record
            # of the live order, so the next tick could submit a duplicate
            # for the same strategy. Refuse startup like we do for >1
            # still-open or a state mismatch -- Keith investigates the
            # journal + the broker side before re-launching.
            LOG.error(
                "engine: broker has 1 still-open order but journal_view "
                "is corrupt and resume refused; refusing to start."
            )
            self.journal.append(
                "engine_stopped",
                payload={
                    "reason": "recovery_resume_refused_live_order",
                    "still_open_count": still_open_count,
                    "terminal_state": "halted",
                    "note": (
                        "broker shows a live order for this engine but "
                        "its journal_view payload is corrupt; resume "
                        "refused to avoid duplicate submission on next "
                        "tick. Investigate vault journal + broker before "
                        "clearing."
                    ),
                },
            )
            self._engine_stopped_journaled = True
            self.state = EngineState.HALTED
            self._shutdown_requested = True
            return
        else:
            self.state = EngineState.CONNECTED_IDLE

        # Load approved strategies. Parse failures raise; engine bails.
        try:
            self._strategies = strategy_loader.load_all_approved(
                self.engine_config.strategies_dir
            )
        except strategy_loader.StrategyLoaderError as exc:
            LOG.error("engine: strategy load failed: %s", exc)
            self.journal.append(
                "engine_stopped",
                payload={
                    "reason": "strategy_load_failed",
                    "error": str(exc),
                    "terminal_state": "halted",
                },
            )
            self._engine_stopped_journaled = True
            self.state = EngineState.HALTED
            self._shutdown_requested = True
            return

        self.journal.append(
            "engine_started",
            payload={
                "pid": os.getpid(),
                "tick_seconds": self.engine_config.tick_seconds,
                "recovery_status": reco.status.value,
                "reconciled_event_count": len(reco.events),
                "mismatch_count": len(reco.mismatch_reasons),
                "strategies_loaded": [s.name for s in self._strategies],
                # Bundle 3 cycle 5: richer per-strategy metadata so
                # `--diagnose-approved` can show the operator the exact
                # approved_commit_sha + regime_filter + risk_envelope_pct
                # the engine booted with. `strategies_loaded` is kept for
                # backwards-compat with journals / tests that already
                # expect it. Decimals stringify so JSON round-trips.
                "strategies": [
                    {
                        "name": s.name,
                        "approved_commit_sha": s.approved_commit_sha,
                        "regime_filter": list(s.regime_filter),
                        "risk_envelope_pct": str(s.risk_envelope_pct),
                    }
                    for s in self._strategies
                ],
                "resumed_awaiting": (
                    resumed.trade_id if resumed is not None else None
                ),
                "validator_config_hash": _hash_config(self.validator_config),
                "kill_file_present_at_startup": self._kill_file_present(),
                # R5-minimax: surface the resolved retired_dir so
                # operators can diff it against the cycle-4 post-commit
                # hook's write target. A mismatch = silently-disabled
                # retirement gate; publishing the engine's view in the
                # journal makes the coupling visible at deploy time.
                "retired_dir": str(self._retired_dir()),
            },
        )
        if reco.status in (
            recovery_mod.RecoveryStatus.CATCH_UP,
            recovery_mod.RecoveryStatus.MISMATCH_OVERRIDE,
        ):
            # Q32: checkpoint the expected broker-held stop children so
            # that multi-day holds restart cleanly even after the
            # parent's order_submitted / order_filled records scroll
            # out of the 48h journal lookback. Zero-length list when
            # no adopted position has a journaled stop_loss -- always
            # present as a list so downstream readers can treat
            # absence and empty identically. Recovery events from THIS
            # pass are included so recovery-discovered fills (crash
            # between order_proposed and order_submitted) seed the
            # checkpoint too -- they're already being journaled above
            # but live in _journal_recovery_event's freshly-appended
            # records, not in the stale journal_tail passed to
            # reconcile() earlier (Codex R5 P1).
            expected_stop_children = recovery_mod.build_expected_stop_children(
                positions=self._positions,
                journal_tail=journal_tail,
                recovery_events=reco.events,
            )
            self.journal.append(
                "engine_recovered",
                payload={
                    "status": reco.status.value,
                    "reconciled_event_count": len(reco.events),
                    "adopted_positions": [
                        {"ticker": p.ticker, "qty": p.qty, "avg_price": str(p.avg_price)}
                        for p in self._positions
                    ],
                    "expected_stop_children": expected_stop_children,
                },
            )

        # Init fully completed: any later reconnect jumps straight to
        # CONNECTED_IDLE. Before this point, a reconnect must re-enter
        # INIT (Codex round-6 P1).
        self._init_completed = True

        # If .killed was already present at startup, we entered this
        # function via run_init but _transition_to_killed wasn't called
        # (INIT != KILLED). Apply now so post-init we honor the kill.
        if self._kill_file_present():
            await self._transition_to_killed(result)

    # ---------- connection failure handling ----------

    async def _enter_disconnected(
        self,
        result: TickResult,
        exc: Exception,
        *,
        auth: bool = False,
    ) -> None:
        self.state = EngineState.DISCONNECTED
        self._outage_started_at = datetime.now(timezone.utc)
        self._reconnect_attempts = 0
        self._last_disconnect_status_at = None
        self._last_error_class = type(exc).__name__
        if auth:
            result.auth_required = True
            self.journal.append(
                "auth_required",
                payload={
                    "error": str(exc),
                    "telegram_alert_required": True,
                    "note": (
                        "IB Gateway requires human re-login per broker-research.md#15"
                    ),
                },
            )
        else:
            # Connection failure: no per-attempt event; the next tick's
            # disconnect_status covers cumulative outage reporting.
            LOG.warning("engine: disconnect at init -> %s", exc)

    async def _attempt_reconnect(self, result: TickResult) -> None:
        delay = _reconnect_delay(self._reconnect_attempts)
        # The runner lives on a single event loop: asyncio.sleep is the
        # correct wait primitive here.
        await asyncio.sleep(delay)

        try:
            await self.connector.connect()
        except AuthRequiredError as exc:
            self._reconnect_attempts += 1
            prior = self._last_error_class
            self._last_error_class = "AuthRequiredError"
            # Codex round-9 P2: an outage can start as a plain
            # disconnect + transition to auth-required on a later
            # reconnect attempt. Without this explicit journal, the
            # operator alert path for "human re-login required" never
            # fires for that pattern -- disconnect_status alone is
            # advisory, not actionable. Emit once per transition into
            # auth-required, then fall through to the summary cadence.
            if prior != "AuthRequiredError":
                self.journal.append(
                    "auth_required",
                    payload={
                        "error": str(exc),
                        "telegram_alert_required": True,
                        "transitioned_from": prior,
                        "note": (
                            "IB Gateway requires human re-login per broker-research.md#15"
                        ),
                    },
                )
                result.auth_required = True
            await self._maybe_emit_disconnect_status(result)
            return
        except (DisconnectedError, ConnectorError) as exc:
            self._reconnect_attempts += 1
            self._last_error_class = type(exc).__name__
            await self._maybe_emit_disconnect_status(result)
            return

        outage_started = self._outage_started_at or datetime.now(timezone.utc)
        outage_duration = (
            datetime.now(timezone.utc) - outage_started
        ).total_seconds()
        was_auth = self._last_error_class == "AuthRequiredError"
        self._reconnect_attempts = 0
        self._outage_started_at = None
        self._last_disconnect_status_at = None

        if was_auth:
            self.journal.append(
                "auth_recovered",
                payload={
                    "outage_seconds": outage_duration,
                },
            )
        self.journal.append(
            "reconnected",
            payload={
                "outage_seconds": outage_duration,
                "prior_error_class": self._last_error_class,
                "init_completed_before_outage": self._init_completed,
            },
        )
        self._last_error_class = None

        # Codex round-6 P1: if the outage happened before init
        # finished (reconcile + engine_started never ran), re-enter
        # INIT on the next tick rather than CONNECTED_IDLE. Without
        # this, startup-time auth/disconnect failures would silently
        # skip recovery + strategy load and the engine would run with
        # empty state.
        if not self._init_completed:
            self.state = EngineState.INIT
        else:
            self.state = EngineState.CONNECTED_IDLE
        result.reconnected = True

    async def _maybe_emit_disconnect_status(self, result: TickResult) -> None:
        """Summary journaling during long outages (architect Q4-refined).

        Emits one disconnect_status event per DISCONNECT_STATUS_INTERVAL
        during a continuous outage, with cumulative attempt count, last
        error class, and outage duration so invest-execute has a clean
        surface."""
        now = datetime.now(timezone.utc)
        last = self._last_disconnect_status_at
        if last is not None and now - last < DISCONNECT_STATUS_INTERVAL:
            return
        outage_started = self._outage_started_at or now
        duration = (now - outage_started).total_seconds()
        self.journal.append(
            "disconnect_status",
            payload={
                "attempts": self._reconnect_attempts,
                "outage_seconds": duration,
                "last_error_class": self._last_error_class,
            },
        )
        self._last_disconnect_status_at = now
        result.disconnect_status_emitted = True

    # ---------- kill switch ----------

    def _kill_file_present(self) -> bool:
        return kill_switch.is_killed(self.engine_config.kill_path)

    def _retired_dir(self) -> Path:
        """Base dir for per-strategy `.retired-<slug>` sentinels.

        Thin delegation to `kill_switch.resolve_retired_dir` so the
        engine (reader) and the cycle-4 post-commit hook (writer)
        share a single resolver; any path-derivation change lands in
        one place.
        """
        return kill_switch.resolve_retired_dir(
            self.engine_config.retired_dir,
            self.engine_config.kill_path,
        )

    def _retire_slug(self, snap: ApprovedStrategySnapshot) -> str:
        return derive_retire_slug(snap.source_path)

    async def _transition_to_killed(self, result: TickResult) -> None:
        """Observe an externally-written .killed and transition KILLED.

        No journal event here: the .killed file itself is the primary
        record (who wrote it + why, per kill_switch.write_killed). The
        engine just honors it. Individual order attempts during kill
        get kill_blocked events at the attempt site, and the
        circuit-breaker path separately emits kill_switch_written when
        IT wrote the file.
        """
        self.state = EngineState.KILLED
        result.killed = True

    async def _handle_kill_cleared(self, result: TickResult) -> None:
        self.journal.append(
            "kill_cleared",
            payload={
                "note": "human removed .killed file -- engine resuming",
            },
        )
        self.state = EngineState.CONNECTED_IDLE
        result.kill_cleared = True

    # ---------- main tick body ----------

    async def _run_tick_body(self, result: TickResult) -> None:
        # Poll awaiting-fill order first: a fill frees up the engine to
        # consider new candidates this tick.
        if self._pending_order is not None:
            await self._poll_awaiting(result)

        # If polling dropped us to DISCONNECTED (auth/socket), bail.
        if self.state == EngineState.DISCONNECTED:
            return

        # EOD boundary (once per UTC day).
        if self._eod_due():
            await self._run_eod(result)
            if self.state == EngineState.DISCONNECTED:
                return
            # EOD does not block the rest of tick; fall through.

        # Drift-check approved strategies (file-level hash comparison).
        for snap in self._strategies:
            if snap.name in self._strategy_drift_warned:
                continue
            if strategy_loader.detect_drift(snap):
                self._strategy_drift_warned.add(snap.name)
                self.journal.append(
                    "strategy_file_modified_post_approval",
                    payload={
                        "source_path": snap.source_path,
                        "approved_sha256": snap.source_sha256,
                        "approved_at": snap.approved_at.isoformat(),
                        "note": (
                            "on-disk file differs from approved snapshot. "
                            "Snapshot remains the authoritative runtime config "
                            "until re-approved via /invest-ship."
                        ),
                    },
                    strategy=snap.name,
                )

        # Breaker check against current account state. Codex round-1 P1:
        # _evaluate_breakers does a broker read, which can raise
        # AuthRequiredError / DisconnectedError mid-session. Catch here
        # so the engine enters DISCONNECTED cleanly rather than
        # crashing the tick.
        try:
            trips = await self._evaluate_breakers()
        except AuthRequiredError as exc:
            await self._enter_disconnected(result, exc, auth=True)
            return
        except (DisconnectedError, ConnectorError) as exc:
            await self._enter_disconnected(result, exc)
            return
        result.breaker_trips.extend(t.breaker for t in trips if t.tripped)
        if any_hard_tripped(trips):
            # A hard breaker fired: do not submit new orders this tick.
            # Note: .killed may now be set by the total-drawdown trip;
            # the next tick's kill-check picks it up.
            return

        if self._pending_order is not None:
            return  # serialized: at most one in-flight

        if self.state == EngineState.KILLED:
            return  # defense-in-depth: kill re-checked, no new submits

        # Evaluate strategies and submit if a candidate survives the
        # full validator cascade.
        self.state = EngineState.PROCESSING_TICK
        try:
            await self._process_strategies(result)
        except AuthRequiredError as exc:
            await self._enter_disconnected(result, exc, auth=True)
            return
        except (DisconnectedError, ConnectorError) as exc:
            await self._enter_disconnected(result, exc)
            return
        finally:
            if self.state == EngineState.PROCESSING_TICK:
                self.state = EngineState.CONNECTED_IDLE

    async def _process_strategies(self, result: TickResult) -> None:
        if not self._strategies:
            return

        account = await self.connector.get_account_summary()
        marks = await self.connector.get_marks([s.order_spec.ticker for s in self._strategies])
        now = datetime.now(timezone.utc)
        current_regime = _read_current_regime(self.engine_config.regime_file)

        ctx = RiskContext(
            account_value=account.net_liquidation,
            cash=account.cash,
            positions=[
                Position(ticker=p.ticker, qty=p.qty, avg_price=p.avg_price)
                for p in self._positions
            ],
            pending_orders=[],  # MVP: one-in-flight serialization
            now=now,
            current_marks=marks,
        )
        market = MarketSnapshot(ts=now, marks=marks, account_value=account.net_liquidation)

        for snap in self._strategies:
            # R2-minimax P2: retirement sentinel check runs BEFORE the
            # runner so a retired strategy never emits order_proposed.
            # The _submit-level check remains as defense-in-depth for
            # the narrow window where a sentinel lands mid-tick between
            # this iteration's check and the broker call.
            # R5-minimax P1: a ValueError from _validate_slug (name that
            # bypassed the pre-commit hook) must not crash the whole
            # tick. Fail-closed on this strategy by journaling
            # strategy_name_invalid and skipping it; other strategies
            # in self._strategies continue to be evaluated.
            try:
                kill_switch.assert_strategy_not_retired(
                    self._retire_slug(snap), base_dir=self._retired_dir()
                )
            except kill_switch.StrategyRetiredError as exc:
                self.journal.append(
                    "order_rejected",
                    payload={
                        "reason": "strategy_retired",
                        "retired_record": exc.record,
                        "strategy_sha256": snap.source_sha256,
                        "strategy_approved_commit": snap.approved_commit_sha,
                    },
                    strategy=snap.name,
                    trade_id=new_ulid(),
                )
                result.strategies_evaluated += 1
                result.orders_rejected += 1
                continue
            except ValueError as exc:
                self.journal.append(
                    "order_rejected",
                    payload={
                        "reason": "strategy_name_invalid",
                        "error": str(exc),
                        "strategy_sha256": snap.source_sha256,
                        "strategy_approved_commit": snap.approved_commit_sha,
                    },
                    strategy=snap.name,
                    trade_id=new_ulid(),
                )
                result.strategies_evaluated += 1
                result.orders_rejected += 1
                continue

            result.strategies_evaluated += 1
            decision = strategy_runner.evaluate(
                snap,
                market,
                ctx,
                current_regime=current_regime,
                cash_only_config=self.validator_config,
            )
            if decision.candidate is None:
                continue
            trade_id = new_ulid()
            order = _to_validator_order(decision.candidate, now)

            # Engine-level pre-submit cash_only backstop for sell orders.
            # Bundle 1 architect flag: non-negotiable to call this BEFORE
            # the general runner, independent of the leverage validator.
            if order.side == "sell":
                backstop = cash_only.check_sell_covered(
                    order, ctx, self.validator_config
                )
                if not backstop.approved:
                    self._journal_validator_reject(
                        trade_id,
                        snap.name,
                        order,
                        [backstop],
                        note="engine_pre_submit_cash_only_backstop",
                    )
                    result.orders_rejected += 1
                    continue

            ok, validator_results = run_all(order, ctx, self.validator_config)
            if not ok:
                self._journal_validator_reject(
                    trade_id, snap.name, order, validator_results
                )
                result.orders_rejected += 1
                continue

            # All validators passed: journal the proposal + submit.
            self.journal.append(
                "order_proposed",
                payload={
                    "ticker": order.ticker,
                    "side": order.side,
                    "qty": order.qty,
                    "limit_price": str(order.limit_price),
                    "stop_loss": str(order.stop_loss) if order.stop_loss else None,
                    "time_in_force": decision.candidate.time_in_force,
                    "validators": as_journal_payload(validator_results),
                    "strategy_sha256": snap.source_sha256,
                    "strategy_approved_commit": snap.approved_commit_sha,
                },
                strategy=snap.name,
                trade_id=trade_id,
                ticker=order.ticker,
                side=order.side,
                qty=order.qty,
            )

            await self._submit(
                snap=snap,
                order=order,
                trade_id=trade_id,
                tif=decision.candidate.time_in_force,
                result=result,
            )
            # Phase 2 MVP: one order per tick. Break after the first
            # successful submit; next tick handles subsequent strategies.
            if self._pending_order is not None:
                break

    async def _submit(
        self,
        *,
        snap: ApprovedStrategySnapshot,
        order: Order,
        trade_id: str,
        tif: str,
        result: TickResult,
    ) -> None:
        self.state = EngineState.SUBMITTING

        # Re-check .killed right before talking to broker (spec: "check
        # at every tick AND before every order submission").
        if self._kill_file_present():
            self.journal.append(
                "kill_blocked",
                payload={
                    "reason": "kill_file_present_at_submit",
                    "ticker": order.ticker,
                    "side": order.side,
                    "qty": order.qty,
                },
                strategy=snap.name,
                trade_id=trade_id,
            )
            self.state = EngineState.CONNECTED_IDLE
            return

        # Bundle 3 m2.17, Q7: retirement sentinel check. The per-
        # strategy `.retired-<slug>` file is written atomically by the
        # post-commit hook (cycle 4) when /invest-ship --retire-strategy
        # lands. Checking it synchronously here closes the one-tick
        # exposure window left by the file-hash drift detection, which
        # only fires at the next tick boundary. strategy_retired is a
        # payload.reason on order_rejected (no new event type needed;
        # order_rejected is already in schema v2).
        try:
            kill_switch.assert_strategy_not_retired(
                self._retire_slug(snap), base_dir=self._retired_dir()
            )
        except kill_switch.StrategyRetiredError as exc:
            # R1-minimax: mirror order_proposed's snapshot metadata so
            # replay can pin the rejection to the exact approved
            # snapshot that was blocked (by sha256 + approval commit)
            # without having to re-load the strategy file.
            self.journal.append(
                "order_rejected",
                payload={
                    "reason": "strategy_retired",
                    "ticker": order.ticker,
                    "side": order.side,
                    "qty": order.qty,
                    "retired_record": exc.record,
                    "strategy_sha256": snap.source_sha256,
                    "strategy_approved_commit": snap.approved_commit_sha,
                },
                strategy=snap.name,
                trade_id=trade_id,
                ticker=order.ticker,
                side=order.side,
                qty=order.qty,
            )
            self.state = EngineState.CONNECTED_IDLE
            result.orders_rejected += 1
            return

        try:
            ack = await self.connector.submit_order(
                ticker=order.ticker,
                side=order.side,
                qty=order.qty,
                limit_price=order.limit_price,
                stop_loss=order.stop_loss,
                time_in_force=tif,
                client_tag=f"k2bi:{snap.name}:{trade_id}",
            )
        except AuthRequiredError as exc:
            # Codex round-10 P1: transport failures are NOT proof that
            # IBKR refused the order. The order may already be live at
            # the broker with the placeOrder call having succeeded on
            # their side before the socket dropped. Journaling
            # order_rejected would terminate the proposal in recovery
            # replay and make restart-matching by trade_id impossible.
            # Leave the order_proposed record as the trailing state.
            # Codex round-22 P1: force re-init on reconnect so a full
            # journal-vs-broker reconcile runs before the engine
            # evaluates strategies again. Without this, the next
            # tick's runner would see no pending order in memory and
            # could submit a duplicate against the still-live broker
            # order. Flipping _init_completed=False routes the next
            # successful reconnect back through INIT, which resumes
            # the pending if broker has it or halts on mismatch.
            self._init_completed = False
            await self._enter_disconnected(result, exc, auth=True)
            return
        except DisconnectedError as exc:
            self._init_completed = False
            await self._enter_disconnected(result, exc)
            return
        except ConnectorError as exc:
            self.journal.append(
                "order_rejected",
                payload={
                    "reason": "submit_failed_broker_rejected",
                    "error": str(exc),
                },
                strategy=snap.name,
                trade_id=trade_id,
                ticker=order.ticker,
                side=order.side,
                qty=order.qty,
            )
            self.state = EngineState.CONNECTED_IDLE
            result.orders_rejected += 1
            return

        submitted_payload: dict[str, Any] = {
            "status": ack.status,
            "limit_price": str(order.limit_price),
            "stop_loss": str(order.stop_loss) if order.stop_loss else None,
            "time_in_force": tif,
        }
        if ack.warnings:
            # Codex round-9 P1: connector-side warnings (e.g. stop
            # child rejected after parent filled) are captured on the
            # submit record AND escalated to kill so a live-but-
            # unprotected position does not silently linger.
            submitted_payload["warnings"] = list(ack.warnings)
        self.journal.append(
            "order_submitted",
            payload=submitted_payload,
            strategy=snap.name,
            trade_id=trade_id,
            ticker=order.ticker,
            side=order.side,
            qty=order.qty,
            broker_order_id=ack.broker_order_id,
            broker_perm_id=ack.broker_perm_id,
        )
        if ack.warnings:
            # Write .killed so no further orders go out until a human
            # reviews + re-protects the position.
            killed = kill_switch.write_killed(
                reason="protective_stop_child_failed_parent_live",
                source="engine_submit",
                detail={
                    "trade_id": trade_id,
                    "broker_order_id": ack.broker_order_id,
                    "broker_perm_id": ack.broker_perm_id,
                    "warnings": list(ack.warnings),
                },
                path=self.engine_config.kill_path,
            )
            if killed is not None:
                self.journal.append(
                    "kill_switch_written",
                    payload={
                        "source": "engine_submit_warning",
                        "trade_id": trade_id,
                        "warnings": list(ack.warnings),
                    },
                    strategy=snap.name,
                    trade_id=trade_id,
                    broker_order_id=ack.broker_order_id,
                    broker_perm_id=ack.broker_perm_id,
                )
        self._pending_order = AwaitingOrderState(
            trade_id=trade_id,
            strategy=snap.name,
            order=order,
            broker_order_id=ack.broker_order_id,
            broker_perm_id=ack.broker_perm_id,
            submitted_at=ack.submitted_at,
            filled_qty=0,
        )
        self.state = EngineState.AWAITING_FILL
        result.orders_submitted += 1

    # ---------- awaiting-fill polling ----------

    async def _poll_awaiting(self, result: TickResult) -> None:
        pending = self._pending_order
        assert pending is not None  # poll is only called when set

        since = pending.last_poll_at or pending.submitted_at
        try:
            executions = await self.connector.get_executions_since(since)
            open_orders = await self.connector.get_open_orders()
        except AuthRequiredError as exc:
            await self._enter_disconnected(result, exc, auth=True)
            return
        except (DisconnectedError, ConnectorError) as exc:
            await self._enter_disconnected(result, exc)
            return

        # Match by perm_id first, then order_id. Dedupe by exec_id
        # since get_executions_since() rounds to second-precision in
        # IBKR's ExecutionFilter -- same-second polls can return a
        # fill the engine already journaled (Codex round-5 P2).
        new_fills = [
            e
            for e in executions
            if (
                (pending.broker_perm_id and e.broker_perm_id == pending.broker_perm_id)
                or e.broker_order_id == pending.broker_order_id
            )
            and e.exec_id not in pending.applied_exec_ids
        ]
        for fill in new_fills:
            pending.applied_exec_ids.add(fill.exec_id)
            pending.filled_qty += fill.qty
            self.journal.append(
                "order_filled",
                payload={
                    "exec_id": fill.exec_id,
                    "fill_qty": fill.qty,
                    "fill_price": str(fill.price),
                    "filled_at": fill.filled_at.isoformat(),
                    "cumulative_filled_qty": pending.filled_qty,
                    "remaining_qty": pending.order.qty - pending.filled_qty,
                    "ticker": pending.order.ticker,
                    "side": pending.order.side,
                    # Q32 precondition: recovery's expected_stop_children
                    # checkpoint can source the protective-stop trigger
                    # from the fill record in the window where the
                    # original order_submitted aged out but order_filled
                    # still sits in journal_tail.
                    "stop_loss": (
                        str(pending.order.stop_loss)
                        if pending.order.stop_loss is not None
                        else None
                    ),
                },
                strategy=pending.strategy,
                trade_id=pending.trade_id,
                ticker=pending.order.ticker,
                side=pending.order.side,
                qty=fill.qty,
                broker_order_id=pending.broker_order_id,
                broker_perm_id=pending.broker_perm_id,
            )
            result.orders_filled += 1

        pending.last_poll_at = datetime.now(timezone.utc)

        # Did we terminate?
        still_open = any(
            (o.broker_perm_id and o.broker_perm_id == pending.broker_perm_id)
            or o.broker_order_id == pending.broker_order_id
            for o in open_orders
        )
        if still_open and pending.filled_qty < pending.order.qty:
            # Timeout check.
            elapsed = (datetime.now(timezone.utc) - pending.submitted_at).total_seconds()
            if elapsed >= self.engine_config.fill_timeout_seconds:
                await self._cancel_pending(pending, reason="fill_timeout", result=result)
            return

        if pending.filled_qty >= pending.order.qty:
            await self._reconcile_fill(pending, result)
            return

        # Not in open orders AND not fully filled = broker terminated
        # (cancelled / rejected) while we weren't looking. Poll status
        # history to classify.
        await self._reconcile_terminal(pending, result)

    async def _reconcile_fill(
        self, pending: AwaitingOrderState, result: TickResult
    ) -> None:
        self.state = EngineState.RECONCILING
        # Refresh positions from broker (broker is authoritative).
        try:
            self._positions = await self.connector.get_positions()
        except (AuthRequiredError, DisconnectedError) as exc:
            await self._enter_disconnected(
                result,
                exc,
                auth=isinstance(exc, AuthRequiredError),
            )
            return
        self._pending_order = None
        self.state = EngineState.CONNECTED_IDLE

    async def _reconcile_terminal(
        self, pending: AwaitingOrderState, result: TickResult
    ) -> None:
        """Broker dropped the order without filling (cancelled /
        rejected). Pull status history and journal accordingly."""
        try:
            history = await self.connector.get_order_status_history(
                pending.submitted_at - timedelta(seconds=10)
            )
        except (AuthRequiredError, DisconnectedError) as exc:
            await self._enter_disconnected(
                result,
                exc,
                auth=isinstance(exc, AuthRequiredError),
            )
            return

        terminal = None
        for row in history:
            if (
                pending.broker_perm_id and row.broker_perm_id == pending.broker_perm_id
            ) or row.broker_order_id == pending.broker_order_id:
                terminal = row
                break

        if terminal is None:
            # Ambiguous: not open, no terminal record. Treat as timeout
            # and journal so the audit trail has a resolution.
            self.journal.append(
                "order_timeout",
                payload={
                    "reason": "vanished_from_open_and_status_history",
                    "ticker": pending.order.ticker,
                    "side": pending.order.side,
                    "qty": pending.order.qty,
                    "filled_qty": pending.filled_qty,
                },
                strategy=pending.strategy,
                trade_id=pending.trade_id,
                broker_order_id=pending.broker_order_id,
                broker_perm_id=pending.broker_perm_id,
            )
            result.orders_timed_out += 1
        else:
            if terminal.status == "Rejected":
                self.journal.append(
                    "order_rejected",
                    payload={
                        "reason": "broker_rejected",
                        "broker_status": terminal.status,
                        "broker_reason": terminal.reason,
                    },
                    strategy=pending.strategy,
                    trade_id=pending.trade_id,
                    ticker=pending.order.ticker,
                    side=pending.order.side,
                    qty=pending.order.qty,
                    broker_order_id=pending.broker_order_id,
                    broker_perm_id=pending.broker_perm_id,
                )
                result.orders_rejected += 1
            elif terminal.status == "Filled" and terminal.filled_qty > 0:
                # Codex round-11 P2: broker confirms the order filled
                # (get_executions_since may have missed the exec rows
                # across a disconnect/restart gap). Journal as a real
                # fill so audit trail stays consistent with broker
                # inventory instead of mis-recording the trade as a
                # timeout.
                newly_filled = max(
                    0, terminal.filled_qty - pending.filled_qty
                )
                self.journal.append(
                    "order_filled",
                    payload={
                        "source": "status_history_recovery",
                        "fill_qty": newly_filled,
                        "fill_price": (
                            str(terminal.avg_fill_price)
                            if terminal.avg_fill_price is not None
                            else None
                        ),
                        "cumulative_filled_qty": terminal.filled_qty,
                        "remaining_qty": terminal.remaining_qty,
                        "broker_status": terminal.status,
                        # Q32 precondition (mirror of primary fill path).
                        "stop_loss": (
                            str(pending.order.stop_loss)
                            if pending.order.stop_loss is not None
                            else None
                        ),
                    },
                    strategy=pending.strategy,
                    trade_id=pending.trade_id,
                    ticker=pending.order.ticker,
                    side=pending.order.side,
                    qty=newly_filled,
                    broker_order_id=pending.broker_order_id,
                    broker_perm_id=pending.broker_perm_id,
                )
                result.orders_filled += 1
            else:
                self.journal.append(
                    "order_timeout",
                    payload={
                        "reason": f"broker_status_{terminal.status}",
                        "broker_status": terminal.status,
                        "filled_qty": terminal.filled_qty,
                        "remaining_qty": terminal.remaining_qty,
                    },
                    strategy=pending.strategy,
                    trade_id=pending.trade_id,
                    ticker=pending.order.ticker,
                    side=pending.order.side,
                    qty=pending.order.qty,
                    broker_order_id=pending.broker_order_id,
                    broker_perm_id=pending.broker_perm_id,
                )
                result.orders_timed_out += 1

        # Codex round-1 P1: a terminal cancel/rejection after a partial
        # fill would otherwise leave _positions stale (the partial share
        # count already left the broker), letting the next tick re-emit
        # the strategy as if nothing happened. Always refresh.
        await self._refresh_positions_after_terminal(pending, result)
        # Codex round-5 P1: if the refresh itself failed and pushed us
        # to DISCONNECTED, respect that state. Clearing _pending_order
        # is still correct (the order terminated at the broker), but
        # flipping back to CONNECTED_IDLE would make the next tick run
        # order flow against a broken session with stale positions.
        self._pending_order = None
        if self.state != EngineState.DISCONNECTED:
            self.state = EngineState.CONNECTED_IDLE

    async def _cancel_pending(
        self,
        pending: AwaitingOrderState,
        *,
        reason: str,
        result: TickResult,
    ) -> None:
        """Request broker cancel; do NOT mark terminal until broker
        confirms.

        Codex round-8 P1: IBKR cancel_order is async -- the broker can
        keep the order in Submitted/PendingCancel for a brief window
        after the API call returns. Prior implementation journaled
        order_timeout + cleared _pending_order immediately, so a
        crash in that window (redeploy, SIGTERM, OS hiccup) would
        cause recovery to see the journal as closed + broker as still
        open and flag phantom_open_order + refuse to start.

        This path now issues the cancel request, flips a cancel_
        requested flag on the pending, and leaves the next tick's
        _poll_awaiting to observe the broker terminal status and
        journal order_timeout at that point via the existing
        _reconcile_terminal flow.
        """
        if pending.cancel_requested:
            # Cancel already requested on a prior tick; let the poll
            # loop observe the broker terminal.
            return

        try:
            await self.connector.cancel_order(pending.broker_order_id)
        except (AuthRequiredError, DisconnectedError) as exc:
            await self._enter_disconnected(
                result, exc, auth=isinstance(exc, AuthRequiredError)
            )
            return
        except ConnectorError as exc:
            LOG.warning("cancel_order raised: %s", exc)

        pending.cancel_requested = True
        pending.cancel_reason = reason
        pending.cancel_requested_at = datetime.now(timezone.utc)
        # Do NOT journal order_timeout yet and do NOT clear
        # _pending_order. The next _poll_awaiting tick sees the
        # broker terminal + calls _reconcile_terminal which emits
        # order_timeout with the observed broker status.

    async def _refresh_positions_after_terminal(
        self,
        pending: AwaitingOrderState,
        result: TickResult,
    ) -> None:
        """Resync _positions from broker when a pending terminates in a
        non-clean-fill state (cancel / reject / timeout).

        Cheap to call when filled_qty == 0 (broker returns the same
        snapshot), but mandatory when filled_qty > 0 because the
        partial fill already moved inventory. Failure to refresh gets
        logged but does not stop the transition back to
        CONNECTED_IDLE; the next tick's own broker read will catch up
        (engine is tick-driven, not event-driven for recovery)."""
        try:
            self._positions = await self.connector.get_positions()
        except (AuthRequiredError, DisconnectedError) as exc:
            await self._enter_disconnected(
                result, exc, auth=isinstance(exc, AuthRequiredError)
            )
        except ConnectorError as exc:  # pragma: no cover - broker edge
            LOG.warning(
                "refresh_positions after terminal raised: %s", exc
            )

    # ---------- breakers ----------

    async def _evaluate_breakers(self):
        account = await self.connector.get_account_summary()
        # Phase 2 MVP: breakers use broker's net liquidation as the
        # current value + session-open snapshot. Day-open tracking is
        # Bundle 5's invest-journal P&L stub; for now we feed current
        # value as both to keep breakers conservative (zero drawdown
        # reads as zero, never trips). Full wiring lands in Bundle 5.
        state = AccountState(
            current_value=account.net_liquidation,
            day_open_value=account.net_liquidation,
            peak_value=account.net_liquidation,
        )
        trips = evaluate(state, self.breaker_config)
        for trip in trips:
            if trip.tripped:
                self.journal.append(
                    "breaker_triggered",
                    payload=trip.as_journal_payload(),
                )
        tripping_kill = apply_kill_on_trip(trips, self.engine_config.kill_path)
        if tripping_kill is not None:
            self.journal.append(
                "kill_switch_written",
                payload={
                    "source": "circuit_breaker",
                    "breaker": tripping_kill.breaker,
                    "detail": tripping_kill.detail,
                },
            )
        return trips

    # ---------- EOD ----------

    def _eod_due(self) -> bool:
        """True at most once per US/Eastern session date, on the first
        tick past the configured EOD time in Eastern local.

        Codex round-11 P1: a UTC cutoff is wrong across EDT/EST
        transitions. The cutoff is specified in US/Eastern local, so
        16:30 ET consistently means "30 min after NYSE close" without
        per-deployment overrides for DST.
        """
        from zoneinfo import ZoneInfo

        eastern = ZoneInfo("US/Eastern")
        now_et = datetime.now(timezone.utc).astimezone(eastern)
        today_et = now_et.strftime("%Y-%m-%d")
        if self._last_eod_date_utc == today_et:
            return False
        hh, mm = [int(x) for x in self.engine_config.eod_et_time.split(":")]
        eod_today_et = now_et.replace(
            hour=hh, minute=mm, second=0, microsecond=0
        )
        return now_et >= eod_today_et

    async def _run_eod(self, result: TickResult) -> None:
        """Cancel DAY orders still open at the configured EOD UTC time.

        Codex round-2 P2: the module docstring and journal event
        schema both promise DAY-order EOD cancellation. Earlier MVP
        just stubbed it; this is the real wiring. GTC / GTD orders
        remain live across sessions per IBKR conventions.
        """
        try:
            open_orders = await self.connector.get_open_orders()
        except (AuthRequiredError, DisconnectedError) as exc:
            await self._enter_disconnected(
                result, exc, auth=isinstance(exc, AuthRequiredError)
            )
            return

        cancelled_count = 0
        non_day_seen = 0
        non_k2bi_seen = 0
        for o in open_orders:
            tif_upper = (o.tif or "DAY").upper()
            if tif_upper != "DAY":
                non_day_seen += 1
                continue
            # Codex round-8 P2: get_open_orders returns ALL open
            # orders on the account -- including manual orders + other
            # API clients. EOD must not cancel unrelated activity.
            # Filter to k2bi-managed orders by client_tag prefix so the
            # sweep never touches what it doesn't own.
            if not (o.client_tag or "").startswith("k2bi:"):
                non_k2bi_seen += 1
                continue
            try:
                await self.connector.cancel_order(o.broker_order_id)
            except (AuthRequiredError, DisconnectedError) as exc:
                await self._enter_disconnected(
                    result, exc, auth=isinstance(exc, AuthRequiredError)
                )
                # Already-cancelled entries persist in audit; the next
                # tick's reconcile-or-retry handles remainders.
                return
            except ConnectorError as exc:
                # Broker-side refusal on a specific cancel (order just
                # terminated concurrently, etc.) is non-fatal -- log
                # per-order and carry on to the next.
                LOG.warning(
                    "eod cancel raised for %s: %s", o.broker_order_id, exc
                )
                continue
            self.journal.append(
                "eod_cancel",
                payload={
                    "broker_status_before_cancel": o.status,
                    "qty": o.qty,
                    "filled_qty": o.filled_qty,
                    "remaining_qty": o.qty - o.filled_qty,
                    "limit_price": str(o.limit_price),
                    "tif": tif_upper,
                    "client_tag": o.client_tag,
                },
                ticker=o.ticker,
                side=o.side,
                qty=o.qty,
                broker_order_id=o.broker_order_id,
                broker_perm_id=o.broker_perm_id or None,
            )
            cancelled_count += 1

        self.journal.append(
            "eod_complete",
            payload={
                "cancelled_orders": cancelled_count,
                "open_orders_seen": len(open_orders),
                "non_day_orders_retained": non_day_seen,
                "non_k2bi_orders_skipped": non_k2bi_seen,
            },
        )
        # Track the Eastern session date so the next tick won't re-
        # fire EOD within the same US/Eastern calendar day.
        from zoneinfo import ZoneInfo

        eastern = ZoneInfo("US/Eastern")
        self._last_eod_date_utc = (
            datetime.now(timezone.utc).astimezone(eastern).strftime("%Y-%m-%d")
        )
        result.eod_ran = True

    # ---------- shutdown ----------

    def _install_signal_handlers(self) -> None:
        loop = asyncio.get_running_loop()

        def _request_stop() -> None:
            self._shutdown_requested = True

        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                loop.add_signal_handler(sig, _request_stop)
            except NotImplementedError:
                # Windows / restricted runtimes can't add signal handlers
                # to asyncio loops. The pm2-hosted production path is
                # macOS, so we never hit this branch on the Mac Mini.
                pass  # pragma: no cover

    async def _shutdown(self) -> None:
        # Don't overwrite HALTED with SHUTDOWN -- the distinction
        # matters for invest-execute status reporting. HALTED means
        # engine refused to operate (needs investigation); SHUTDOWN
        # means clean exit.
        if self.state != EngineState.HALTED:
            self.state = EngineState.SHUTDOWN
        # Codex round-9 P2: don't double-journal engine_stopped when an
        # init path already emitted a specific terminal reason. Only
        # write the graceful_shutdown record if no engine_stopped has
        # been journaled in this lifecycle.
        if not self._engine_stopped_journaled:
            self.journal.append(
                "engine_stopped",
                payload={
                    "reason": "graceful_shutdown",
                    "pending_order": (
                        self._pending_order.trade_id
                        if self._pending_order is not None
                        else None
                    ),
                },
            )
            self._engine_stopped_journaled = True
        try:
            await self.connector.disconnect()
        except Exception as exc:  # pragma: no cover - shutdown path
            LOG.warning("connector disconnect raised during shutdown: %s", exc)

    # ---------- journal helpers ----------

    def _journal_recovery_event(
        self, event: recovery_mod.ReconciliationEvent
    ) -> None:
        kwargs: dict[str, Any] = {"payload": event.payload}
        if event.ticker is not None:
            kwargs["ticker"] = event.ticker
        if event.broker_order_id is not None:
            kwargs["broker_order_id"] = event.broker_order_id
        if event.broker_perm_id is not None:
            kwargs["broker_perm_id"] = event.broker_perm_id
        if event.trade_id is not None:
            kwargs["trade_id"] = event.trade_id
        if event.strategy is not None:
            kwargs["strategy"] = event.strategy
        self.journal.append(event.event_type, **kwargs)

    def _journal_validator_reject(
        self,
        trade_id: str,
        strategy: str,
        order: Order,
        results,
        *,
        note: str | None = None,
    ) -> None:
        last = results[-1]
        payload: dict[str, Any] = {
            "ticker": order.ticker,
            "side": order.side,
            "qty": order.qty,
            "limit_price": str(order.limit_price),
            "stop_loss": str(order.stop_loss) if order.stop_loss else None,
            "validators": as_journal_payload(results),
        }
        if note is not None:
            payload["note"] = note
        self.journal.append(
            "order_rejected",
            payload=payload,
            strategy=strategy,
            trade_id=trade_id,
            ticker=order.ticker,
            side=order.side,
            qty=order.qty,
            error={"code": last.reason, "message": last.rule},
        )


# ---------- module-level helpers ----------


def _to_validator_order(cand: CandidateOrder, now: datetime) -> Order:
    return Order(
        ticker=cand.ticker,
        side=cand.side,
        qty=cand.qty,
        limit_price=cand.limit_price,
        stop_loss=cand.stop_loss,
        strategy=cand.strategy,
        submitted_at=now,
        extended_hours=False,
    )


def _reconnect_delay(attempt: int) -> float:
    """5s -> 10s -> 20s -> ... capped at 300s.

    Architect Q4-refined defaults. The first attempt (attempt == 0)
    waits the base delay; attempts grow 2x up to the cap.

    Guards against overflow on week-scale outages (IB weekly re-auth
    can produce thousands of attempts while waiting for Keith); once
    we'd clear the cap we short-circuit to the cap directly so we
    never compute 2**large-int.
    """
    if attempt < 0:
        attempt = 0
    # log2(300/5) = log2(60) ~= 5.9, so attempt >= 6 is guaranteed
    # to be at or above the cap.
    if attempt >= 6:
        return RECONNECT_CAP_SECONDS
    delay = RECONNECT_START_SECONDS * (RECONNECT_MULT ** attempt)
    return min(delay, RECONNECT_CAP_SECONDS)


def _hash_config(config: dict[str, Any]) -> str:
    """Stable hash of validator config so engine_started records which
    limit set was in force. Changes to config.yaml therefore show up
    as a new hash in the journal on next startup."""
    import json

    payload = json.dumps(config, sort_keys=True, default=str).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()[:16]


def derive_retire_slug(source_path: str) -> str:
    """Filesystem slug used to key this strategy's retirement sentinel.

    Codex R5 + R6: the cycle-4 post-commit hook keys sentinels by the
    slug extracted from the `Retired-Strategy: strategy_<slug>` commit
    trailer -- which comes from the FILENAME, not the `name:`
    frontmatter. The engine MUST use the same derivation so both
    sides always agree, and MUST NEVER fall back to `snap.name`
    (which can drift from the filename in flat-layout or manually-
    edited files: e.g. `meanrev-v2.md` with `name: meanrev` would
    retire under one key and check under another, silently bypassing
    the gate).

    In the K2Bi convention (`wiki/strategies/strategy_<slug>.md`),
    strip the `strategy_` prefix from the filename stem. For the flat
    `<slug>.md` layout (test fixtures + legacy), use the stem
    directly. Either way: filename is authoritative.

    Exported as a module-level function (not just an Engine method)
    so tests can exercise the derivation in isolation and the cycle-4
    post-commit hook can import it directly, closing the hook/engine
    parity gap that MiniMax R11 flagged.
    """
    stem = Path(source_path).stem
    prefix = "strategy_"
    if stem.startswith(prefix):
        return stem[len(prefix):]
    return stem


def _engine_config_from_dict(raw: dict[str, Any]) -> EngineConfig:
    if not isinstance(raw, dict):
        return EngineConfig()
    strategies_dir = raw.get("strategies_dir")
    regime_file = raw.get("regime_file")
    # Codex round-7 P2: propagate the override env name through YAML
    # too. Prior implementation dropped `allow_recovery_mismatch_env`
    # and reverted to the default, making the config ineffective in
    # from_environment() deploys.
    override_env_name = raw.get(
        "allow_recovery_mismatch_env",
        recovery_mod.RECOVERY_OVERRIDE_ENV,
    )
    return EngineConfig(
        tick_seconds=float(raw.get("tick_seconds", DEFAULT_TICK_SECONDS)),
        fill_timeout_seconds=float(
            raw.get("fill_timeout_seconds", DEFAULT_FILL_TIMEOUT_SECONDS)
        ),
        eod_et_time=str(raw.get("eod_et_time", DEFAULT_EOD_ET)),
        strategies_dir=(
            Path(strategies_dir) if strategies_dir else DEFAULT_STRATEGIES_DIR
        ),
        kill_path=(Path(raw["kill_path"]) if raw.get("kill_path") else None),
        # Bundle 3 cycle 3 R1-minimax: YAML-configured retired_dir must
        # flow through so deployments can override where per-strategy
        # `.retired-<slug>` sentinels land (otherwise the field is
        # silently dropped and always falls through to kill_path.parent
        # or DEFAULT_RETIRED_DIR).
        retired_dir=(
            Path(raw["retired_dir"]) if raw.get("retired_dir") else None
        ),
        allow_recovery_mismatch_env=str(override_env_name),
        # Q33 (2026-04-21): deployments can tune the --once pre-exit
        # barrier via YAML. EngineConfig.__post_init__ clamps any
        # oversized value at ONCE_EXIT_WAIT_SECONDS_MAX so a typo in
        # config cannot hang --once indefinitely.
        once_exit_wait_seconds=float(
            raw.get("once_exit_wait_seconds", 10.0)
        ),
        # Codex round-13 P2: deployments that remap the regime file
        # now flow through to the engine instead of silently reverting
        # to the vault default.
        regime_file=(
            Path(regime_file) if regime_file else DEFAULT_REGIME_FILE
        ),
    )


def _safe_decimal_optional(raw: Any) -> Decimal | None:
    """Parse an optional Decimal from journal data; None on corruption
    or missing.

    R16-minimax: defensive helper for journal-view reconstruction.
    """
    if raw in (None, "", "None"):
        return None
    try:
        return Decimal(str(raw))
    except (InvalidOperation, ValueError, TypeError):
        LOG.warning(
            "resume: corrupt Decimal in journal_view (%r); using None",
            raw,
        )
        return None


def _safe_decimal_or_zero(raw: Any) -> Decimal:
    """Parse a required Decimal from journal data; Decimal(0) on
    corruption. Used for limit_price where the Order dataclass wants
    a non-None value."""
    value = _safe_decimal_optional(raw)
    return value if value is not None else Decimal("0")


def _read_current_regime(path: Path) -> str | None:
    """Read the active regime name from the vault's regime file.

    Expected shape (invest-regime writes this): YAML frontmatter with
    a `regime:` field. Missing file OR missing field returns None;
    callers that care (strategies with regime_filter) MUST treat
    None as "block, regime unknown" rather than bypass.
    """
    if not path.exists():
        return None
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return None
    lines = raw.splitlines()
    if not lines or lines[0].strip() != "---":
        return None
    for line in lines[1:]:
        stripped = line.strip()
        if stripped == "---":
            break
        if stripped.startswith("regime:"):
            value = stripped.split(":", 1)[1].strip()
            # Strip YAML quote chars if present.
            if value and value[0] in "'\"" and value[-1] in "'\"":
                value = value[1:-1].strip()
            return value or None
    return None


def _read_recent_journal(
    journal: JournalWriter,
    since: datetime,
) -> list[dict[str, Any]]:
    """Read the journal day-files covering the DEFAULT_LOOKBACK window.

    Codex round-1 P2: DEFAULT_LOOKBACK is 48 hours, so a restart that
    lands shortly after midnight UTC still needs records from the file
    that rotated out two days back. Reading three days (today,
    yesterday, day-before-yesterday) covers the full window regardless
    of when during the day the restart occurs. Records outside the
    since-timestamp are filtered out below.

    Lookback uses the writer's own read_all() so sidecar flock
    discipline is preserved even during concurrent writes.
    """
    out: list[dict[str, Any]] = []
    for day_offset in (2, 1, 0):
        when = datetime.now(timezone.utc) - timedelta(days=day_offset)
        try:
            out.extend(journal.read_all(when))
        except Exception as exc:  # pragma: no cover - shouldn't raise
            LOG.warning("journal read_all(%s) raised: %s", when, exc)
    since_iso = since.isoformat()
    return [r for r in out if str(r.get("ts", "")) >= since_iso]


def _read_extended_checkpoints(
    journal: JournalWriter,
    *,
    ext_since: datetime,
    narrow_since: datetime,
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    """Read state-checkpoint events from [ext_since, narrow_since).

    Q42 +1 week persistence FAIL (2026-05-03) carry-forward fix: a
    multi-day engine-off gap leaves journal_tail empty under the 48h
    DEFAULT_LOOKBACK, so the replay logic in
    `recovery._positions_from_journal` and
    `recovery._adopted_orphan_perm_ids` has nothing to seed from --
    previously-adopted SPY positions and orphan STOPs re-flag as
    phantoms.

    This helper extends ONLY the lookup window for the event types in
    `recovery.EXTENDED_CHECKPOINT_EVENT_TYPES`. Recovery's replay logic
    is unchanged. Returns events in oldest-to-newest order so the
    snapshot-reset semantics in `_positions_from_journal` apply
    correctly (most recent `engine_recovered` wins).

    Bounded by `recovery.EXTENDED_CHECKPOINT_LOOKBACK` (30 days).
    File-system errors per day are logged-and-skipped, mirroring the
    `_read_recent_journal` pattern -- a missing day file just means no
    checkpoint that day, not a recovery failure.

    `narrow_since` is the boundary that `_read_recent_journal` already
    covers; events with `ts >= narrow_since` are filtered out here so
    the caller can append the result to the narrow tail without
    duplication.

    `now` is injectable for tests; production passes the default
    (`datetime.now(timezone.utc)`).
    """
    if now is None:
        now = datetime.now(timezone.utc)
    if ext_since >= narrow_since:
        # Defensive: caller misconfigured the window. Return empty so
        # behavior degrades to the current 48h-only lookup.
        return []
    out: list[dict[str, Any]] = []
    days_back_ext = (now.date() - ext_since.date()).days
    days_back_narrow = (now.date() - narrow_since.date()).days
    # Walk day files from (narrow_since, ext_since] inclusive on the
    # ext side; +1 because day_offset == days_back_narrow is already
    # covered by `_read_recent_journal`.
    for day_offset in range(days_back_narrow + 1, days_back_ext + 1):
        when = now - timedelta(days=day_offset)
        try:
            day_records = journal.read_all(when)
        except Exception as exc:  # pragma: no cover - shouldn't raise
            LOG.warning(
                "extended-checkpoint read_all(%s) raised: %s", when, exc
            )
            continue
        for rec in day_records:
            if (
                rec.get("event_type")
                not in recovery_mod.EXTENDED_CHECKPOINT_EVENT_TYPES
            ):
                continue
            ts = str(rec.get("ts", ""))
            if not ts:
                continue
            if ts < ext_since.isoformat():
                continue
            if ts >= narrow_since.isoformat():
                continue  # already covered by narrow tail
            out.append(rec)
    out.sort(key=lambda r: str(r.get("ts", "")))
    return out


def _validate_journal_view(journal_view: dict[str, Any]) -> dict[str, Any] | None:
    """Validate every field the resume path will consume. Return a
    validated dict or None if ANY field is corrupt.

    Architect post-R19 ruling: rounds R16 (corrupt Decimal), R18
    (corrupt qty), R19 (corrupt datetime) all found defensive-parsing
    gaps in _pick_resumable_awaiting on DIFFERENT fields of the same
    journal_view payload. Instead of patching each field's call site,
    validate the whole payload at a single seam. If anything is
    corrupt, refuse resume and let the broker's still-open order
    surface as phantom_open_order on next reconcile -- Keith gets an
    explicit mismatch signal rather than silently degraded state.
    """
    ticker = journal_view.get("ticker", "")
    if not isinstance(ticker, str) or not ticker.strip():
        LOG.error("resume: missing/invalid ticker (%r); refusing resume", ticker)
        return None

    side_raw = journal_view.get("side", "")
    side = str(side_raw).lower()
    if side not in ("buy", "sell"):
        LOG.error("resume: invalid side (%r); refusing resume", side_raw)
        return None

    try:
        qty = int(journal_view.get("qty", 0))
    except (TypeError, ValueError):
        LOG.error(
            "resume: corrupt qty (%r); refusing resume",
            journal_view.get("qty"),
        )
        return None
    if qty <= 0:
        LOG.error(
            "resume: non-positive qty (%d); refusing resume", qty,
        )
        return None

    # limit_price: corrupt value is NOT a refuse-resume condition (we
    # only use it for journaling + Order reconstruction, broker holds
    # the authoritative price). Degrade to 0.
    limit_price = _safe_decimal_or_zero(journal_view.get("limit_price"))
    stop_loss = _safe_decimal_optional(journal_view.get("stop_loss"))

    submitted_iso = journal_view.get("submitted_at")
    if submitted_iso is None:
        submitted_at = datetime.now(timezone.utc)
    else:
        try:
            submitted_at = datetime.fromisoformat(
                str(submitted_iso).replace("Z", "+00:00")
            )
        except (ValueError, TypeError):
            LOG.error(
                "resume: corrupt submitted_at (%r); refusing resume",
                submitted_iso,
            )
            return None
        # Codex R20 P2: fromisoformat returns naive for values like
        # "2026-05-05T10:00:00". A naive datetime later collides with
        # _poll_awaiting's `datetime.now(timezone.utc) - submitted_at`
        # (raises TypeError). Normalize to UTC -- if the journal
        # writer disciplines tz-aware, this is a no-op; if a manual
        # edit dropped the offset, we recover it as UTC.
        if submitted_at.tzinfo is None:
            submitted_at = submitted_at.replace(tzinfo=timezone.utc)
        else:
            submitted_at = submitted_at.astimezone(timezone.utc)

    return {
        "ticker": ticker.strip(),
        "side": side,
        "qty": qty,
        "limit_price": limit_price,
        "stop_loss": stop_loss,
        "submitted_at": submitted_at,
    }


def _pick_resumable_awaiting(
    reco: recovery_mod.ReconciliationResult,
    journal_tail: list[dict[str, Any]],
) -> AwaitingOrderState | None:
    """If exactly one order was classified as pending_still_open during
    reconciliation, resume it in AWAITING_FILL. MVP handles one in-
    flight only; multiple still-open orders means either we restarted
    into an unusual state (more than one strategy got off a submit
    before crash) or manual pre-engine activity -- either way we refuse
    to auto-resume and let Keith sort it out.

    Codex round-10 P1: when resuming, seed applied_exec_ids + the poll
    watermark from the journal history for this trade_id so the first
    post-restart poll does not re-apply executions that were already
    journaled pre-crash (which would double-count fills + emit dup
    order_filled records).

    Corruption safety: journal_view fields are validated up-front by
    _validate_journal_view. Any corrupt field refuses resume rather
    than degrading silently (architect post-R19 ruling).
    """
    candidates = [
        e for e in reco.events
        if e.event_type == "recovery_reconciled"
        and e.payload.get("case") == "pending_still_open"
    ]
    if len(candidates) != 1:
        return None
    event = candidates[0]
    journal_view = event.payload.get("journal_view", {})
    if not isinstance(journal_view, dict):
        LOG.error(
            "resume: journal_view is not a mapping (%r); refusing resume",
            type(journal_view).__name__,
        )
        return None

    validated = _validate_journal_view(journal_view)
    if validated is None:
        return None

    ticker = event.ticker or validated["ticker"]
    submitted_at = validated["submitted_at"]
    order = Order(
        ticker=ticker,
        side=validated["side"],
        qty=validated["qty"],
        limit_price=validated["limit_price"],
        stop_loss=validated["stop_loss"],
        strategy=event.strategy or "",
        submitted_at=submitted_at,
        extended_hours=False,
    )
    trade_id = event.trade_id or new_ulid()

    # Walk journal_tail for order_filled events tied to this trade_id
    # or its broker IDs and extract their exec_ids + last ts. The next
    # poll then starts from that watermark and dedupes any exec_id that
    # already made it to disk.
    applied_ids: set[str] = set()
    last_fill_ts: datetime | None = None
    for rec in journal_tail:
        if rec.get("event_type") != "order_filled":
            continue
        if rec.get("trade_id") == trade_id or (
            event.broker_perm_id
            and rec.get("broker_perm_id") == event.broker_perm_id
        ) or (
            event.broker_order_id
            and rec.get("broker_order_id") == event.broker_order_id
        ):
            payload = rec.get("payload", {}) or {}
            exec_id = payload.get("exec_id")
            if exec_id:
                applied_ids.add(str(exec_id))
            ts_raw = rec.get("ts")
            if ts_raw:
                try:
                    parsed = datetime.fromisoformat(
                        str(ts_raw).replace("Z", "+00:00")
                    )
                    if last_fill_ts is None or parsed > last_fill_ts:
                        last_fill_ts = parsed
                except ValueError:
                    pass

    return AwaitingOrderState(
        trade_id=trade_id,
        strategy=event.strategy or "",
        order=order,
        broker_order_id=event.broker_order_id or "",
        broker_perm_id=event.broker_perm_id or "",
        submitted_at=submitted_at,
        filled_qty=int(event.payload.get("filled_qty", 0)),
        last_poll_at=last_fill_ts,
        applied_exec_ids=applied_ids,
    )


# ---------- CLI entry point ----------


def _parse_args(argv: list[str] | None = None):
    import argparse

    parser = argparse.ArgumentParser(
        prog="execution.engine.main",
        description="K2Bi execution engine (m2.6).",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Run a single tick and exit (used by invest-execute).",
    )
    parser.add_argument(
        "--diagnose-approved",
        action="store_true",
        help=(
            "Diagnostic mode: read the newest `engine_started` event "
            "from today's (falling back to yesterday's) decision journal "
            "and print the approved-strategy set the engine booted "
            "with (name, approved_commit_sha, regime_filter, "
            "risk_envelope_pct). Does not connect to IBKR, does not "
            "touch any file. Exits 0 even when no recent event exists."
        ),
    )
    parser.add_argument(
        "--validator-config",
        type=Path,
        default=None,
        help="Path to validator config.yaml. Default: execution/validators/config.yaml",
    )
    parser.add_argument(
        "--journal-dir",
        type=Path,
        default=None,
        help="Override journal base directory (default: K2Bi-Vault/raw/journal).",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        help="Logging level (default: INFO).",
    )
    parser.add_argument(
        "--account-id",
        default=None,
        help=(
            "IBKR account id for scoping positions / orders / "
            "executions. Falls back to K2BI_IB_ACCOUNT_ID env var. "
            "Leave empty for a single-account paper Gateway."
        ),
    )
    return parser.parse_args(argv)


def _resolve_cli_account_id(args) -> str | None:
    """Resolve the IBKR account id the CLI will scope to.

    Priority: --account-id arg > K2BI_IB_ACCOUNT_ID env > None.
    Exposed as a module-level function so tests can assert the
    plumb-through discipline directly (Codex R18 P1 + architect's
    post-R18 type-level discipline ruling).
    """
    return args.account_id or os.environ.get("K2BI_IB_ACCOUNT_ID") or None


def _construct_cli_connector(args):
    """Build the live IBKRConnector from CLI args + env.

    Factored out so tests can verify the CLI path wires account_id
    through correctly without spinning ib_async. IBKRConnector's
    constructor is pure Python; the ib_async import only happens
    on connect().
    """
    from ..connectors.ibkr import IBKRConnector

    account_id = _resolve_cli_account_id(args)
    return IBKRConnector(account_id=account_id)


async def _run_from_cli(args) -> None:
    logging.basicConfig(level=getattr(logging, args.log_level.upper(), logging.INFO))
    connector = _construct_cli_connector(args)
    engine = Engine.from_environment(
        connector,
        journal_base=args.journal_dir,
        validator_config_path=args.validator_config,
    )
    if args.once:
        await engine.run_once()
    else:
        await engine.run_forever()


def _resolve_journal_dir_for_diagnose(
    cli_journal_dir: Path | None,
    validator_config_path: Path | None,
) -> Path | None:
    """Diagnose-specific journal-dir resolution.

    Precedence (same shape as cycle-4's retired_dir resolver):
      1. `--journal-dir` CLI flag.
      2. `engine.journal_dir` in config.yaml if present.
      3. None (JournalWriter falls back to its DEFAULT_BASE_DIR).

    Returning None on the fall-through case lets JournalWriter own the
    default; we do not hardcode the vault path here.
    """
    if cli_journal_dir is not None:
        return cli_journal_dir
    cfg_path = validator_config_path or (
        Path("execution") / "validators" / "config.yaml"
    )
    if not cfg_path.exists():
        return None
    try:
        raw = load_config(cfg_path)
    except Exception:  # noqa: BLE001 -- best-effort; diagnose never crashes
        return None
    engine_cfg = raw.get("engine", {}) if isinstance(raw, dict) else {}
    if not isinstance(engine_cfg, dict):
        return None
    jd = engine_cfg.get("journal_dir")
    return Path(str(jd)).expanduser() if jd else None


def _find_newest_engine_started(
    journal_base: Path | None,
    *,
    now: datetime | None = None,
) -> dict[str, Any] | None:
    """Read today's and yesterday's journal files; return the newest
    engine_started event. Returns None if no such event appears in the
    48h window.

    Codex R7 P1 #2: this path must be strictly read-only. The earlier
    implementation instantiated `JournalWriter(base_dir=journal_base)`,
    which calls `mkdir(parents=True, exist_ok=True)` AND
    `recover_trailing_partial()` in `__init__` -- both are on-disk
    mutations that contradict the diagnose CLI's stated contract
    ("does not touch any file"; see `--diagnose-approved` --help). For
    example, pointing `--diagnose-approved --journal-dir /does/not/
    exist` would create that directory as a side effect.

    Instead, glob the jsonl files directly and parse one line at a
    time. Use a shared advisory lock on the `.lock` sidecar while
    reading each file so we still coexist safely with a concurrent
    JournalWriter without taking any write action ourselves.
    """
    base = _resolve_diagnose_base_dir(journal_base)
    if base is None or not base.exists() or not base.is_dir():
        return None
    cursor = now if now is not None else datetime.now(timezone.utc)
    if cursor.tzinfo is None:
        cursor = cursor.replace(tzinfo=timezone.utc)
    best: dict[str, Any] | None = None
    best_ts: str = ""
    for day_offset in (0, 1):
        when = cursor - timedelta(days=day_offset)
        day_path = base / f"{when.strftime('%Y-%m-%d')}.jsonl"
        if not day_path.exists():
            continue
        for record in _iter_journal_read_only(day_path):
            # Codex R7 round 2 [medium]: `json.loads` can legitimately
            # yield non-dict values (a scalar or list written by a
            # recovery artifact, a manual edit, or a schema drift). The
            # diagnose contract is "exits 0 always"; calling .get() on
            # a non-dict would raise AttributeError before the
            # formatter's isinstance guards ever run. Skip silently --
            # the `-diagnose-approved` output will still render
            # correctly for any well-formed engine_started records that
            # appear elsewhere in the day's file, and `(none)` if
            # everything is corrupt.
            if not isinstance(record, dict):
                continue
            if record.get("event_type") != "engine_started":
                continue
            ts = str(record.get("ts", ""))
            if ts > best_ts:
                best_ts = ts
                best = record
    return best


def _resolve_diagnose_base_dir(journal_base: Path | None) -> Path | None:
    """Return the journal base dir to read.

    Mirrors the JournalWriter default when journal_base is None but
    does NOT create the directory -- the diagnose path is read-only by
    contract (Codex R7 P1 #2).
    """
    if journal_base is not None:
        return journal_base
    # Same default JournalWriter uses; imported lazily to keep this
    # function cheap when the caller supplies an override.
    from ..journal.writer import DEFAULT_BASE_DIR

    return DEFAULT_BASE_DIR


def _iter_journal_read_only(path: Path):
    """Yield each parsed JSON record from `path` under a shared flock.

    Holding the same sidecar `.lock` file JournalWriter uses with
    LOCK_SH means an in-flight concurrent write (LOCK_EX) blocks us
    until it finishes, so we never read a half-written line. We
    never take LOCK_EX ourselves: this entire function is strictly
    read-only.
    """
    import fcntl

    lock_path = path.with_suffix(path.suffix + ".lock")
    lock_fd: int | None = None
    if lock_path.exists():
        try:
            lock_fd = os.open(str(lock_path), os.O_RDONLY)
            fcntl.flock(lock_fd, fcntl.LOCK_SH)
        except OSError:
            if lock_fd is not None:
                os.close(lock_fd)
                lock_fd = None
    try:
        try:
            with path.open("r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        yield json.loads(
                            line,
                            parse_constant=reject_non_finite_json_constant,
                        )
                    except (json.JSONDecodeError, ValueError):
                        # Skip corrupt / non-RFC-8259 lines. JSONDecodeError
                        # covers structural breakage; ValueError is raised
                        # by the parse_constant hook when a NaN / Infinity
                        # token appears. Both are "don't consume this
                        # record" outcomes -- the writer's recovery path
                        # cleans them up on its next append; the diagnose
                        # read must never touch disk (Codex R7 P1 #2).
                        continue
        except OSError as exc:  # pragma: no cover -- advisory diagnose
            LOG.warning("diagnose: could not read %s: %s", path, exc)
            return
    finally:
        if lock_fd is not None:
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_UN)
            finally:
                os.close(lock_fd)


def _format_diagnose_table(record: dict[str, Any]) -> str:
    """Pretty-print a single engine_started record for operator eyes.

    Codex R7 P1 #3: every accessor on `record` / `payload` / `strategies`
    must defend against non-dict / non-list shapes. A corrupt journal
    record (manual edit, schema drift, pre-cycle-5 payload with a
    different shape) must cause graceful degradation, not an
    AttributeError traceback. `record` may not even be a dict if the
    caller hands us a list or scalar; guard at every hop.
    """
    if not isinstance(record, dict):
        return "engine_started record is malformed (not a mapping)."
    payload = record.get("payload")
    if not isinstance(payload, dict):
        # Top-level metadata is still useful to surface even when the
        # payload is unreadable -- Keith can at least see the record
        # exists and its ts.
        return (
            f"engine_started at {record.get('ts', '(no ts)')}: "
            "payload is malformed (not a mapping); rerun the engine "
            "or inspect the journal file manually."
        )

    lines: list[str] = []
    lines.append(
        f"Most recent engine_started: {record.get('ts', '(no ts)')}"
    )
    pid = payload.get("pid")
    if pid is not None:
        lines.append(f"PID: {pid}")
    cfg_hash = payload.get("validator_config_hash")
    if cfg_hash:
        lines.append(f"Validator config hash: {cfg_hash}")
    retired_dir = payload.get("retired_dir")
    if retired_dir:
        lines.append(f"Retired sentinel dir: {retired_dir}")
    lines.append("")

    strategies = payload.get("strategies")
    if not isinstance(strategies, list) or not strategies:
        # Fall back to legacy `strategies_loaded` names if the richer
        # block is missing (e.g. journal from before cycle 5 rolled
        # out). Operator still gets the names; the cycle-5 additional
        # fields are not reconstructable after the fact.
        legacy_raw = payload.get("strategies_loaded")
        legacy = legacy_raw if isinstance(legacy_raw, list) else []
        if legacy:
            lines.append(f"Approved strategies loaded ({len(legacy)}):")
            for name in legacy:
                lines.append(
                    f"  - {name}  (pre-cycle-5 journal; rerun engine "
                    "for full metadata)"
                )
        else:
            lines.append("Approved strategies loaded: (none)")
        return "\n".join(lines)

    # Codex R7 P1 #3: each entry in strategies must be dict-typed
    # before we call .get on it. Skip malformed entries with a warning
    # line so the rest still renders.
    well_formed = []
    malformed = 0
    for item in strategies:
        if isinstance(item, dict):
            well_formed.append(item)
        else:
            malformed += 1

    lines.append(
        f"Approved strategies loaded ({len(well_formed)}"
        + (f"; {malformed} malformed entry/entries skipped" if malformed else "")
        + "):"
    )
    header = (
        f"  {'name':<24} {'approved_commit_sha':<20} "
        f"{'regime_filter':<24} {'risk_envelope_pct':<18}"
    )
    lines.append(header)
    lines.append(
        "  " + "-" * 22 + "   " + "-" * 18 + "   "
        + "-" * 22 + "   " + "-" * 16
    )
    for s in well_formed:
        name = str(s.get("name", ""))[:24]
        sha = str(s.get("approved_commit_sha") or "")[:20]
        rf_raw = s.get("regime_filter")
        rf = rf_raw if isinstance(rf_raw, list) else []
        rf_str = "[" + ", ".join(str(x) for x in rf) + "]" if rf else "[]"
        rf_str = rf_str[:24]
        risk = str(s.get("risk_envelope_pct") or "")[:18]
        lines.append(f"  {name:<24} {sha:<20} {rf_str:<24} {risk:<18}")
    return "\n".join(lines)


def _run_diagnose_approved(args) -> int:
    """--diagnose-approved entry point.

    Reads the journal, finds the newest engine_started, and prints the
    approved-strategy set the engine booted with. Returns 0 on all
    outcomes (diagnostic tool is non-blocking per spec §3.2 Step F);
    absence of a recent event prints an operator hint.
    """
    journal_base = _resolve_journal_dir_for_diagnose(
        args.journal_dir, args.validator_config
    )
    newest = _find_newest_engine_started(journal_base)
    if newest is None:
        print(
            "engine not started in last 24h; run `--once` or "
            "restart the daemon"
        )
        return 0
    print(_format_diagnose_table(newest))
    return 0


def main() -> None:
    args = _parse_args()
    if args.diagnose_approved:
        # Diagnose path: no connector, no engine instantiation, no event
        # loop. Just journal reads + table print.
        logging.basicConfig(
            level=getattr(logging, args.log_level.upper(), logging.INFO)
        )
        sys.exit(_run_diagnose_approved(args))
    asyncio.run(_run_from_cli(args))


if __name__ == "__main__":  # pragma: no cover - CLI entry
    main()


__all__ = [
    "AwaitingOrderState",
    "DEFAULT_EOD_ET",
    "DEFAULT_FILL_TIMEOUT_SECONDS",
    "DEFAULT_TICK_SECONDS",
    "Engine",
    "EngineConfig",
    "EngineState",
    "RECONNECT_CAP_SECONDS",
    "RECONNECT_MULT",
    "RECONNECT_START_SECONDS",
    "TickResult",
    "main",
]
