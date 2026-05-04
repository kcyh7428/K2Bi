# cash-only invariant: no sell-side paths in this module (reconciliation
# reads broker state; does not generate orders). Sell-side enforcement
# is owned by execution.risk.cash_only and called by runner.py + engine
# main pre-submit hook.
"""Crash-restart reconciliation between the journal and the broker.

Architect Q3-refined contract:

    Broker is authoritative for positions + order status. The journal
    is authoritative for INTENT (what the engine meant to do). On
    restart we:
        1. Walk the journal back to the last engine_stopped event and
           derive "journal-implied" position + pending-order state.
        2. Pull current broker state (positions, open orders, recent
           executions, completed-order status history).
        3. Classify each journal-pending order against its broker
           counterpart: FILLED / CANCELLED / REJECTED / PARTIALLY_FILLED
           / STILL_OPEN. All allowed; log recovery_reconciled for each.
        4. Check for discrepancies (phantom position, oversized
           position, missing position, phantom open order). Any hit =
           refuse-to-start unless K2BI_ALLOW_RECOVERY_MISMATCH=1.

Identity: match by broker_perm_id first, broker_order_id second.
permId is stable across IB Gateway restarts; orderId re-issues on each
new session (architect-mandated in journal v2 payload).
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from enum import Enum
from typing import Any, Iterable


LOG = logging.getLogger("k2bi.engine.recovery")


def _safe_decimal(raw: Any) -> Decimal | None:
    """Parse a Decimal from journal data; return None on corruption.

    R16-minimax: journal payload fields like stop_loss / limit_price
    are written as stringified Decimals, but a partial-write / manual
    edit / future-writer bug could land a non-numeric value. Raising
    InvalidOperation from inside _pending_from_journal would crash the
    engine before engine_started could be journaled. Degrade
    gracefully: log the corruption and return None so the rest of
    recovery can still classify the order by broker ID.
    """
    if raw in (None, "", "None"):
        return None
    try:
        return Decimal(str(raw))
    except (InvalidOperation, ValueError, TypeError):
        LOG.warning(
            "recovery: corrupt Decimal in journal payload (%r); using None",
            raw,
        )
        return None

from ..connectors.types import (
    BrokerOpenOrder,
    BrokerOrderStatusEvent,
    BrokerPosition,
    CLIENT_TAG_PREFIX,
    CLIENT_TAG_STOP_SUFFIX,
    LIVE_ORDER_STATUSES,
    TERMINAL_ORDER_STATUSES,
    parse_client_tag,
)


RECOVERY_OVERRIDE_ENV = "K2BI_ALLOW_RECOVERY_MISMATCH"
# Q42: per-permId adoption of an orphan STOP (operator-portal-submitted
# pre-engine-awareness). Format: "<permId>:<justification>". When set
# AND a broker open order matches the permId AND that order is a STOP
# (aux_price > 0), the orphan_stop_adopted journal event is written
# and the matching mismatch is removed -- adoption resolves THAT
# permId only. OTHER unknown broker state still trips MISMATCH_REFUSED.
# Wire-up lives in engine/main.py; engine refuses to start with
# sys.exit(78) on malformed input (fail-closed config error).
ADOPT_ORPHAN_STOP_ENV = "K2BI_ADOPT_ORPHAN_STOP"
# How far back to walk journal + broker history on restart. A full
# calendar day covers overnight crashes; longer outages trip the
# weekly_cap breaker first anyway.
DEFAULT_LOOKBACK = timedelta(hours=48)
# Q42 +1 week persistence FAIL (2026-05-03) carry-forward fix:
# state-checkpoint events MUST survive multi-day engine-off gaps so a
# cold-start after >48h doesn't re-flag previously-adopted positions
# / orphan STOPs as phantoms. 30 days covers any plausible operator-
# attended outage window without unbounded scan growth (each day file
# is small KB-scale; checkpoint events are sparse). Recovery REPLAY
# semantics are unchanged -- the existing _positions_from_journal
# snapshot-reset and _adopted_orphan_perm_ids extraction handle older
# events correctly once they are present in journal_tail. Only the
# LOOKUP WINDOW for the two checkpoint event types widens. See
# K2Bi-Vault/wiki/planning/q42-carryforward-fix-kickoff.md.
EXTENDED_CHECKPOINT_LOOKBACK = timedelta(days=30)
# Event types whose payload represents a state checkpoint that must
# survive multi-day engine-off gaps. Limited to these two because they
# are the only journal events that encode broker-side state the
# architect has already adopted: engine_recovered.adopted_positions
# (post-recovery snapshot) and orphan_stop_adopted (per-permId
# adoption). Other events (order_filled, recovery_reconciled) are
# intermediate transitions whose effect is already absorbed by
# engine_recovered's snapshot-reset semantics on the next cold start.
EXTENDED_CHECKPOINT_EVENT_TYPES: frozenset[str] = frozenset(
    {"engine_recovered", "orphan_stop_adopted"}
)


@dataclass(frozen=True)
class OrphanStopAdoptionRequest:
    """Architect-issued instruction to adopt a specific broker permId
    as a known STOP order. Constructed by `_parse_adopt_orphan_stop()`
    from the K2BI_ADOPT_ORPHAN_STOP env var; passed to `reconcile()`
    as a pure-function input (mirrors the override_env pattern)."""

    perm_id: int
    justification: str


def _parse_adopt_orphan_stop(raw: str | None) -> OrphanStopAdoptionRequest | None:
    """Parse K2BI_ADOPT_ORPHAN_STOP=<permId>:<justification>.

    Returns None if env var is unset/empty (engine proceeds normally,
    K2BI_ALLOW_RECOVERY_MISMATCH stays as the general escape hatch).
    Raises ValueError if set but malformed -- engine main treats this
    as fatal at startup with sys.exit(78), same fail-closed posture as
    the existing mismatch-refused path. A silently-ignored parse error
    would let the operator believe adoption is happening when it is
    not, and the orphan would re-flag on the next cold start.
    """
    if not raw or not raw.strip():
        return None
    if ":" not in raw:
        raise ValueError(
            f"{ADOPT_ORPHAN_STOP_ENV} format is <permId>:<justification>; "
            f"got {raw!r} (no colon)"
        )
    # Split on FIRST colon so a justification containing colons is
    # preserved verbatim.
    perm_str, _, just = raw.partition(":")
    perm_str = perm_str.strip()
    just = just.strip()
    try:
        perm_id = int(perm_str)
    except ValueError:
        raise ValueError(
            f"{ADOPT_ORPHAN_STOP_ENV} permId must be int, got {perm_str!r}"
        )
    if perm_id <= 0:
        raise ValueError(
            f"{ADOPT_ORPHAN_STOP_ENV} permId must be positive, got {perm_id}"
        )
    if not just:
        raise ValueError(
            f"{ADOPT_ORPHAN_STOP_ENV} justification must be non-empty"
        )
    return OrphanStopAdoptionRequest(perm_id=perm_id, justification=just)


def _adopted_orphan_perm_ids(records: Iterable[dict[str, Any]]) -> set[str]:
    """Permanent IDs adopted by any prior orphan_stop_adopted journal event.

    The orphan check at the Phase B.1 loop treats these permIds as
    KNOWN broker state so future cold starts don't re-flag them after
    the architect has explicitly adopted them. Values are stringified
    so they collide cleanly with the `f"perm:{...}"` namespace used
    throughout reconcile().

    NOTE: bounded by journal_tail's lookback window. The Q42 +1 week
    carry-forward fix (2026-05-03) extends the lookup window via
    `_read_extended_checkpoints` for these events specifically, so
    adoptions survive multi-day engine-off gaps. The recognition is
    GATED on the underlying position still being held at the broker
    -- see `_adopted_orphan_perm_id_to_ticker` and the call site in
    `reconcile()` -- so a stale adoption whose position has since
    been closed does NOT suppress phantom_open_order detection.
    """
    out: set[str] = set()
    for rec in records:
        if rec.get("event_type") != "orphan_stop_adopted":
            continue
        payload = rec.get("payload") or {}
        perm = payload.get("permId")
        if perm is not None:
            out.add(str(perm))
    return out


def _adopted_orphan_perm_id_to_ticker(
    records: Iterable[dict[str, Any]],
) -> dict[str, str]:
    """Map adopted-orphan permId -> ticker from orphan_stop_adopted events.

    Q42 +1 week carry-forward review (2026-05-03, Kimi+Codex
    cross-mode): the original Q42 recognition unconditionally
    suppressed `phantom_open_order` for any permId in
    `_adopted_orphan_perm_ids`. With the carry-forward fix extending
    the lookup to 30 days, a stale adoption could mask a genuinely-
    orphaned STOP whose underlying position has been closed in the
    interim. The reconcile() call site uses this mapping to gate the
    suppression: an adopted permId is only honored when the broker
    still holds a position in the order's ticker.

    Same fix tightens the existing narrow-window path too -- the gap
    was always present, the carry-forward just widened the time
    window over which it could manifest.

    Returns dict[permId_str -> ticker]. Multiple records for the same
    permId would be unusual (operator should adopt at most once per
    orphan), but if present the LATEST event's ticker wins via the
    natural dict-overwrite semantics during forward replay.
    """
    out: dict[str, str] = {}
    for rec in records:
        if rec.get("event_type") != "orphan_stop_adopted":
            continue
        payload = rec.get("payload") or {}
        perm = payload.get("permId")
        ticker = payload.get("ticker")
        if perm is not None and ticker:
            out[str(perm)] = str(ticker)
    return out


class RecoveryStatus(str, Enum):
    CLEAN = "clean"                      # empty journal + empty broker
    CATCH_UP = "catch_up"                # reconciled cleanly, may include drift/partials
    MISMATCH_REFUSED = "mismatch_refused"
    MISMATCH_OVERRIDE = "mismatch_override"  # K2BI_ALLOW_RECOVERY_MISMATCH=1


@dataclass(frozen=True)
class PendingFromJournal:
    """Extracted view of an order the journal last saw as in-flight.

    `stop_loss` is preserved here (R15-minimax finding) so that a
    resumed AwaitingOrderState keeps the strategy-level stop reference
    across restart. Broker still holds the protective stop child via
    the bracket, but engine-internal tracking of the order loses the
    stop context if we don't carry it through recovery.
    """

    trade_id: str | None
    strategy: str | None
    broker_order_id: str | None
    broker_perm_id: str | None
    ticker: str
    side: str
    qty: int
    limit_price: Decimal | None
    submitted_at: datetime
    stop_loss: Decimal | None = None


@dataclass(frozen=True)
class PositionFromJournal:
    """Positions implied by the journal's fill history + last engine
    snapshot."""

    ticker: str
    qty: int
    avg_price: Decimal


@dataclass(frozen=True)
class ReconciliationEvent:
    """One reconciliation outcome, ready to be handed to JournalWriter.
    The engine's recovery step iterates these and appends each to the
    journal so audit trail is complete regardless of how many orders
    we caught up."""

    event_type: str
    payload: dict[str, Any]
    ticker: str | None = None
    broker_order_id: str | None = None
    broker_perm_id: str | None = None
    trade_id: str | None = None
    strategy: str | None = None


@dataclass
class ReconciliationResult:
    """Outcome of a single reconcile() pass.

    engine/main.py treats `status == MISMATCH_REFUSED` as refuse-to-start
    unless the K2BI_ALLOW_RECOVERY_MISMATCH env flag is set. Regardless
    of status, `events` must be journaled -- mismatch audit trail is
    exactly as important as catch-up audit trail."""

    status: RecoveryStatus
    events: list[ReconciliationEvent] = field(default_factory=list)
    mismatch_reasons: list[dict[str, Any]] = field(default_factory=list)
    adopted_positions: list[BrokerPosition] = field(default_factory=list)
    adopted_open_orders: list[BrokerOpenOrder] = field(default_factory=list)


def reconcile(
    *,
    journal_tail: list[dict[str, Any]],
    broker_positions: list[BrokerPosition],
    broker_open_orders: list[BrokerOpenOrder],
    broker_order_status: list[BrokerOrderStatusEvent],
    now: datetime,
    override_env: str | None = None,
    override_env_name: str = RECOVERY_OVERRIDE_ENV,
    adopt_orphan_stop: OrphanStopAdoptionRequest | None = None,
    adopt_orphan_stop_env_name: str = ADOPT_ORPHAN_STOP_ENV,
) -> ReconciliationResult:
    """Classify broker state against journal-implied state.

    Pure function: takes all inputs, returns a result. Engine is the
    only caller that turns the result into journal writes + state
    machine transition.

    Algorithm in two phases:

        Phase A classifies each journal-pending order against its
        broker counterpart (filled/cancelled/rejected/partial/still-
        open/vanished). Catch-up fills are used to project a delta
        against journal-implied positions so the Phase B position-diff
        doesn't mistake a legitimate catch-up fill for a phantom
        position.

        Phase B diffs broker positions + open orders against the
        projected journal-implied state and flags discrepancies.

    `override_env`: explicit value for the override flag (tests pass
    "" or "1"; production caller reads os.environ and passes through).
    `override_env_name`: the NAME of the env var that was consulted,
    so mismatch records report the actual remediation instruction to
    operators (Codex round-7 P3).
    """
    override_raw = (
        override_env
        if override_env is not None
        else os.environ.get(override_env_name, "")
    )
    override_active = override_raw.strip() == "1"

    implied_positions = _positions_from_journal(journal_tail)
    implied_pending = _pending_from_journal(journal_tail)

    events: list[ReconciliationEvent] = []
    mismatches: list[dict[str, Any]] = []

    # ---- Phase A: classify journal-pending against broker fate ----

    status_index = _index_status_events(broker_order_status)
    open_index = _index_open_orders(broker_open_orders)
    seen_broker_ids: set[str] = set()
    # Q42: orphan_stop_adopted events from prior recovery passes pre-mark
    # their permIds as known so the Phase B.1 orphan loop skips them.
    # Same recognition mechanism Phase A's perm/oid matching uses.
    # Q42 +1 week carry-forward (2026-05-03) extends the lookup window
    # via `_read_extended_checkpoints` so multi-day engine-off gaps
    # don't re-flag adopted orphans as phantoms. Capital-path safety
    # gate per cross-mode adversarial review (Kimi + Codex 2026-05-03):
    # an adopted permId only suppresses phantom_open_order when the
    # broker STILL HOLDS a position in the order's ticker. A stale
    # adoption whose underlying position has been closed must fall
    # through to phantom detection -- the orphan STOP would otherwise
    # be live at the broker without an associated long position to
    # protect, and engine state cannot reason about it safely.
    adopted_perm_to_ticker = _adopted_orphan_perm_id_to_ticker(journal_tail)
    broker_position_tickers = {p.ticker for p in broker_positions}
    for adopted_perm in _adopted_orphan_perm_ids(journal_tail):
        ticker = adopted_perm_to_ticker.get(str(adopted_perm))
        if ticker is not None and ticker not in broker_position_tickers:
            # Stale orphan adoption: ticker no longer at broker.
            # Skip the carry-forward; phantom_open_order will fire
            # for the live STOP order and surface the divergence.
            continue
        seen_broker_ids.add(f"perm:{adopted_perm}")
    # Per-ticker (signed_qty_delta, last_broker_avg_fill_price).
    # Positive qty means the pending was a buy that filled, growing
    # inventory; negative means a sell that filled, reducing it.
    reconciliation_deltas: dict[str, list[tuple[int, Decimal | None]]] = {}

    # Build the trade-id fallback map BEFORE classification so
    # _match_broker_order can also match by client_tag when the
    # journal never captured broker IDs (crash between submit_order
    # success and order_submitted journal write -- Codex round-4 P1).
    trade_id_to_open = _index_open_orders_by_trade_id(broker_open_orders)
    trade_id_to_status = _index_status_events_by_trade_id(broker_order_status)

    # Q39-B: the --once pre-exit barrier (Q33) writes
    # once_exit_barrier_timeout events when it times out waiting on a
    # pending order's terminal status. When recovery sees such an event
    # referencing a trade_id that later turns up with no broker
    # counterpart, it is the strongest-tier evidence we have that the
    # order was live at the broker before the engine exited. Recorded
    # as evidence=barrier_timeout in the assumed-fill event; absence
    # defaults to crash_gap.
    #
    # MiniMax R3 finding #3 (2026-04-21): the evidence field is AUDIT
    # ONLY. Recovery does NOT branch on barrier_timeout vs crash_gap
    # for delta computation, phantom detection, or any other capital-
    # path decision. The field exists so operators can distinguish
    # deliberate-exit from crash-cause when reviewing mismatch
    # journals; it must NOT be relied upon as an operational trust
    # signal. A future architect decision to strengthen the
    # barrier_timeout path (e.g., reduced-position delta, different
    # mismatch severity) requires an explicit design change, not a
    # drive-by branch added here.
    barrier_timeout_trade_ids = _barrier_timeout_trade_ids(journal_tail)

    for pending in implied_pending:
        match = _match_broker_order(
            pending,
            status_index,
            open_index,
            trade_id_open_index=trade_id_to_open,
            trade_id_status_index=trade_id_to_status,
        )
        if match is None:
            # Q39-B / Q36: journal said submitted; broker knows nothing.
            # Branch on broker_perm_id:
            #   present -> perm_id is broker's accept-ack. Engine either
            #              crashed post-ack (crash_gap) or deliberately
            #              exited during the Q33 --once barrier wait
            #              (barrier_timeout). Hybrid rule (Q39 Option 1
            #              + Option 3): assume filled, synthesize a
            #              fill into reconciliation_deltas at the
            #              journal's limit_price so projected positions
            #              match broker's actual state (post-Gateway-
            #              restart visibility gap). If the assumption
            #              is wrong, Phase B.2 fires
            #              journal_position_missing_at_broker on the
            #              next reconnect -- divergence detection.
            #   absent  -> no evidence broker ever accepted the order.
            #              Could be pre-ack engine crash, transport
            #              failure, or reject-before-perm-id. Preserve
            #              the original pending_no_broker_counterpart
            #              case so projected positions stay unchanged.
            if pending.broker_perm_id:
                evidence = (
                    "barrier_timeout"
                    if pending.trade_id
                    and pending.trade_id in barrier_timeout_trade_ids
                    else "crash_gap"
                )
                # Q39-B (MiniMax R2 2026-04-21): a None limit_price
                # means the journal has no price signal for the
                # synthetic fill. Silently substituting Decimal("0")
                # would corrupt the projected avg_price and mask real
                # divergence behind a bogus zero-cost basis. Flag the
                # unknown price in the audit payload AND skip the
                # synthetic delta so Phase B.2 surfaces the divergence
                # via phantom_position or journal_position_missing_at_broker.
                fill_price_unknown = pending.limit_price is None
                assumed_fill_price = (
                    pending.limit_price
                    if pending.limit_price is not None
                    else Decimal("0")
                )
                payload: dict[str, Any] = {
                    "case": "pending_no_broker_counterpart_assumed_filled",
                    "evidence": evidence,
                    "note": (
                        "journal had broker_perm_id; broker "
                        "visibility limited (Q39); defaulting "
                        "to assume fill"
                    ),
                    "journal_view": _pending_payload(pending),
                    "filled_qty": pending.qty,
                }
                if fill_price_unknown:
                    # MiniMax R2 finding #2 (2026-04-21): omit the
                    # avg_fill_price field entirely rather than
                    # persisting a "0" sentinel that could surface as
                    # real data under an override bypass path. The
                    # fill_price_unknown flag is sufficient for audit.
                    payload["fill_price_unknown"] = True
                else:
                    payload["avg_fill_price"] = str(assumed_fill_price)
                events.append(
                    ReconciliationEvent(
                        event_type="recovery_reconciled",
                        payload=payload,
                        trade_id=pending.trade_id,
                        strategy=pending.strategy,
                        broker_order_id=pending.broker_order_id,
                        broker_perm_id=pending.broker_perm_id,
                        ticker=pending.ticker,
                    )
                )
                if pending.qty > 0 and not fill_price_unknown:
                    sign = 1 if pending.side == "buy" else -1
                    reconciliation_deltas.setdefault(
                        pending.ticker, []
                    ).append((sign * pending.qty, assumed_fill_price))
            else:
                events.append(
                    ReconciliationEvent(
                        event_type="recovery_reconciled",
                        payload={
                            "case": "pending_no_broker_counterpart",
                            "note": (
                                "journal-pending order never reached "
                                "broker or broker-side record missing"
                            ),
                            "journal_view": _pending_payload(pending),
                        },
                        trade_id=pending.trade_id,
                        strategy=pending.strategy,
                        broker_order_id=pending.broker_order_id,
                        broker_perm_id=pending.broker_perm_id,
                        ticker=pending.ticker,
                    )
                )
            continue

        kind, obj = match
        broker_perm = _perm_id_of(obj)
        broker_oid = _order_id_of(obj)
        if broker_perm:
            seen_broker_ids.add(f"perm:{broker_perm}")
        if broker_oid:
            seen_broker_ids.add(f"oid:{broker_oid}")

        if kind == "open":
            assert isinstance(obj, BrokerOpenOrder)
            events.append(
                ReconciliationEvent(
                    event_type="recovery_reconciled",
                    payload={
                        "case": "pending_still_open",
                        "broker_status": obj.status,
                        "filled_qty": obj.filled_qty,
                        "remaining_qty": obj.qty - obj.filled_qty,
                        "journal_view": _pending_payload(pending),
                    },
                    trade_id=pending.trade_id,
                    strategy=pending.strategy,
                    broker_order_id=broker_oid,
                    broker_perm_id=broker_perm,
                    ticker=obj.ticker,
                )
            )
            continue

        assert kind == "status"
        assert isinstance(obj, BrokerOrderStatusEvent)
        case = _classify_terminal(obj)
        events.append(
            ReconciliationEvent(
                event_type="recovery_reconciled",
                payload={
                    "case": case,
                    "broker_status": obj.status,
                    "filled_qty": obj.filled_qty,
                    "remaining_qty": obj.remaining_qty,
                    "avg_fill_price": (
                        str(obj.avg_fill_price)
                        if obj.avg_fill_price is not None
                        else None
                    ),
                    "broker_reason": obj.reason,
                    "journal_view": _pending_payload(pending),
                },
                trade_id=pending.trade_id,
                strategy=pending.strategy,
                broker_order_id=broker_oid,
                broker_perm_id=broker_perm,
                ticker=pending.ticker,
            )
        )

        if case in {"pending_filled", "pending_partially_filled"} and obj.filled_qty > 0:
            sign = 1 if pending.side == "buy" else -1
            delta = sign * obj.filled_qty
            reconciliation_deltas.setdefault(pending.ticker, []).append(
                (delta, obj.avg_fill_price)
            )

    # ---- Phase B prep: apply deltas to implied positions ----

    projected_by_ticker: dict[str, tuple[int, Decimal]] = {
        p.ticker: (p.qty, p.avg_price) for p in implied_positions
    }
    for ticker, deltas in reconciliation_deltas.items():
        for delta_qty, fill_avg in deltas:
            old_qty, old_avg = projected_by_ticker.get(
                ticker, (0, Decimal("0"))
            )
            new_qty = old_qty + delta_qty
            if new_qty <= 0:
                projected_by_ticker.pop(ticker, None)
                continue
            if delta_qty > 0 and fill_avg is not None:
                old_cost = old_avg * Decimal(old_qty) if old_qty > 0 else Decimal("0")
                new_cost = fill_avg * Decimal(delta_qty)
                new_avg = (old_cost + new_cost) / Decimal(new_qty)
            else:
                new_avg = old_avg
            projected_by_ticker[ticker] = (new_qty, new_avg)

    # ---- Phase B.1: phantom open orders ----

    # Track trade_ids seen among journal-pending orders so we can
    # recognize stop-loss child orders (Codex round-4 P1). Stop
    # children carry the parent's trade_id in their client_tag with a
    # :stop suffix; they are expected open orders after the parent
    # fills, not phantoms.
    pending_trade_ids: set[str] = set()
    for pending in implied_pending:
        if pending.trade_id:
            pending_trade_ids.add(pending.trade_id)
    # Also treat trade_ids observed in journal fills (parent already
    # filled + cleared) as "ours" so their surviving stop children do
    # not flag phantoms.
    #
    # Q39-B (MiniMax R1 + R2 2026-04-21): assume-fill trade_ids are
    # tracked SEPARATELY. R2 finding #1: silently recognizing a stop
    # child via pending_trade_ids when the assume-fill assumption was
    # wrong leaves an orphaned stop live at the broker, ready to
    # trigger on unrelated price movement. Recognition for an
    # assume-fill trade_id requires the broker to still hold the
    # corresponding position; otherwise the stop is orphaned and must
    # fall through to phantom detection.
    assumed_fill_trade_ids: set[str] = set()
    for rec in journal_tail:
        event_type = rec.get("event_type")
        if event_type in {
            "order_submitted",
            "order_filled",
            "order_proposed",
        }:
            tid = rec.get("trade_id")
            if tid:
                pending_trade_ids.add(tid)
        elif event_type == "recovery_reconciled":
            payload = rec.get("payload") or {}
            if (
                payload.get("case")
                == "pending_no_broker_counterpart_assumed_filled"
            ):
                tid = rec.get("trade_id")
                if tid:
                    assumed_fill_trade_ids.add(tid)
    # Phase A's freshly-synthesized assume-fill events in this pass
    # also feed assumed_fill_trade_ids so the orphan gate covers the
    # Run-1-emission case, not only the Run-2-replay case.
    for ev in events:
        if ev.event_type != "recovery_reconciled":
            continue
        ev_payload = ev.payload or {}
        if (
            ev_payload.get("case")
            == "pending_no_broker_counterpart_assumed_filled"
            and ev.trade_id
        ):
            assumed_fill_trade_ids.add(ev.trade_id)

    # Q32: seed from the most recent engine_recovered checkpoint's
    # expected_stop_children. Rationale: a spec that allows multi-day
    # holds (e.g. 5-session window) will see the parent's
    # order_submitted / order_filled records scroll out of the 48h
    # lookback on day-3 restarts while the GTC stop child is still live
    # at the broker. Without this seed, the stop would be flagged as a
    # phantom_open_order and block startup. The checkpoint is written
    # at every prior engine_recovered moment, so even a day-7 restart
    # finds the most recent hop's record inside journal_tail as long as
    # the engine has been bounced at least once per lookback window.
    #
    # Checkpoint seeding is INTENTIONALLY separated from
    # pending_trade_ids so that a corrupt or historical checkpoint
    # cannot silently reclassify arbitrary broker-side managed orders
    # as recognized (MiniMax R2 Finding 2). The restricted set below
    # only suppresses the phantom-open-order flag for STOP children
    # (is_stop=True after parse_client_tag) whose trade_id appears in
    # the checkpoint. Non-stop broker orders never match this path --
    # they still flow through pending_trade_ids (journal-backed) or
    # fall through to phantom detection.
    #
    # Scope note (Q31/Commit 2 follow-on): seeding ONLY expands stop-
    # child RECOGNITION for broker-present orders. It does NOT detect
    # "checkpoint expects stop, broker has no stop" -- that gap is
    # closed by Commit 2's missing_protective_stop / price_drift /
    # tag_mismatch invariants in Phase B.2. Q32 alone is necessary but
    # not sufficient for full protective-stop safety; Decision 1 of the
    # Session A kickoff commits both commits together.
    #
    # Operational constraint: the checkpoint is only written in
    # main.py::_run_init() after recovery. An engine that opens a
    # position, runs continuously >48h, then crashes, will find neither
    # the original fill nor a post-fill checkpoint in journal_tail.
    # IBKR HK's ~24h re-auth cycle forces reconnects but not engine
    # restarts; a longer outage + crash is the edge case. In that
    # case the broker-side stop is flagged as phantom_open_order and
    # the position as phantom_position, so recovery returns
    # MISMATCH_REFUSED and the engine REFUSES TO START -- not a silent
    # failure. The broker's GTC stop remains live and protects the
    # position; operator must investigate before relaunching.
    # Mitigation (mid-session checkpoint writes or longer retention
    # for stop_loss-bearing records) is architect-scope beyond this
    # commit and is on the Session-A-follow-on list alongside Q31.
    checkpoint_stop_children = _latest_checkpoint_stop_children(journal_tail)
    # Map trade_id -> expected ticker for cross-check at recognition
    # time. Defense against the rare case where a checkpoint entry
    # outlives its position and a broker-side stop child on a different
    # ticker happens to carry a matching trade_id (broker reuse, replay,
    # or manual edit). Recognition requires ticker agreement, not just
    # trade_id agreement.
    checkpoint_stop_trade_ids: set[str] = set()
    checkpoint_stop_tickers: dict[str, str] = {}
    for entry in checkpoint_stop_children:
        tid = entry.get("parent_trade_id")
        ticker = entry.get("ticker")
        if tid:
            checkpoint_stop_trade_ids.add(tid)
            if ticker:
                checkpoint_stop_tickers[tid] = ticker

    for open_order in broker_open_orders:
        matched = False
        perm = open_order.broker_perm_id
        oid = open_order.broker_order_id
        if perm and f"perm:{perm}" in seen_broker_ids:
            matched = True
        if oid and f"oid:{oid}" in seen_broker_ids:
            matched = True
        if matched:
            continue

        # Client-tag based recognition for k2bi-managed orders.
        strategy, trade_id, is_stop = parse_client_tag(open_order.client_tag)
        # Codex R3 P1: checkpoint-seeded recognition additionally
        # requires the broker to still hold a position in the order's
        # ticker. If the position was closed after the checkpoint was
        # written but the broker's GTC stop was left behind, the stop
        # is orphaned and MUST be flagged as phantom -- otherwise the
        # engine could restart with a live unintended sell stop that
        # might open a short on trigger.
        broker_position_tickers = {p.ticker for p in broker_positions}
        checkpoint_recognized = (
            is_stop
            and trade_id is not None
            and trade_id in checkpoint_stop_trade_ids
            # Ticker cross-check: a broker-side stop child with a
            # matching trade_id but a different ticker is NOT the stop
            # the checkpoint expected. Let it fall through to phantom
            # detection rather than silently recognize.
            and checkpoint_stop_tickers.get(trade_id, open_order.ticker)
            == open_order.ticker
            # Position-still-open gate (Codex R3 P1): the checkpoint
            # only protects positions we currently hold.
            and open_order.ticker in broker_position_tickers
        )
        # Q39-B MiniMax R2 finding #1 (2026-04-21): assume-fill trade
        # ids are eligible for recognition ONLY when the broker still
        # holds the corresponding position. Without this gate, a
        # stop child whose assumed-filled parent never materialized at
        # broker would be silently recognized, leaving a live GTC stop
        # ready to trigger on unrelated price movement.
        #
        # The orphan check runs BEFORE pending_trade_ids recognition
        # because a trade_id can be in both sets (order_submitted
        # journaled in Run 1 + assume-fill emitted in this pass); the
        # pending-path recognition would otherwise fire before the
        # assume-fill gate has a say.
        is_assume_fill_trade = (
            trade_id is not None and trade_id in assumed_fill_trade_ids
        )
        if (
            is_stop
            and is_assume_fill_trade
            and open_order.ticker not in broker_position_tickers
        ):
            mismatches.append(
                {
                    "case": "phantom_open_order",
                    "broker_order_id": oid,
                    "broker_perm_id": perm,
                    "ticker": open_order.ticker,
                    "side": open_order.side,
                    "qty": open_order.qty,
                    "status": open_order.status,
                    "client_tag": open_order.client_tag,
                    "reason": (
                        "stop_child_orphan_after_assumed_fill: "
                        "broker has no matching position for the "
                        "trade_id that recovery assumed-filled; this "
                        "stop would trigger on unrelated price "
                        "movement. Operator must review before "
                        "relaunching."
                    ),
                }
            )
            continue
        # Recognition paths:
        #   - pending_trade_ids: standard journal-pending bracket
        #     recognition (covers bracket child placed before fill).
        #   - is_assume_fill_trade + position present: aged-out
        #     assume-fill where order_submitted no longer in
        #     journal_tail; recognition holds only when broker
        #     position confirms the assumption.
        #   - checkpoint_recognized: Q32 expected_stop_children
        #     seeding (existing behavior).
        assume_fill_recognized = (
            is_assume_fill_trade
            and open_order.ticker in broker_position_tickers
        )
        if strategy is not None and trade_id is not None and (
            trade_id in pending_trade_ids
            or assume_fill_recognized
            or checkpoint_recognized
        ):
            # Emit a reconciled event so the audit trail records the
            # child's presence; continue -- not a phantom.
            # Q32 checkpoint-seeded recognition is STOP-ONLY by design
            # (see comments above where checkpoint_stop_trade_ids is
            # built): a regular-order trade_id from the checkpoint
            # cannot reclassify arbitrary broker orders, and a
            # ticker-mismatched stop or a stop without a current
            # position do not count as recognition.
            events.append(
                ReconciliationEvent(
                    event_type="recovery_reconciled",
                    payload={
                        "case": (
                            "stop_child_recognized"
                            if is_stop
                            else "managed_order_recognized"
                        ),
                        "client_tag": open_order.client_tag,
                        "broker_status": open_order.status,
                        "qty": open_order.qty,
                        "filled_qty": open_order.filled_qty,
                        "remaining_qty": open_order.qty - open_order.filled_qty,
                        "tif": open_order.tif,
                    },
                    strategy=strategy,
                    trade_id=trade_id,
                    broker_order_id=oid,
                    broker_perm_id=perm,
                    ticker=open_order.ticker,
                )
            )
            continue

        mismatches.append(
            {
                "case": "phantom_open_order",
                "broker_order_id": oid,
                "broker_perm_id": perm,
                "ticker": open_order.ticker,
                "side": open_order.side,
                "qty": open_order.qty,
                "status": open_order.status,
                "client_tag": open_order.client_tag,
            }
        )

    # ---- Phase B.2: position diff against projected state ----

    broker_by_ticker = {p.ticker: p for p in broker_positions}

    for ticker, broker_pos in broker_by_ticker.items():
        projected = projected_by_ticker.get(ticker)
        if projected is None:
            mismatches.append(
                {
                    "case": "phantom_position",
                    "ticker": ticker,
                    "broker_qty": broker_pos.qty,
                    "broker_avg_price": str(broker_pos.avg_price),
                }
            )
            continue
        implied_qty, implied_avg = projected
        if broker_pos.qty > implied_qty:
            mismatches.append(
                {
                    "case": "position_oversized_vs_journal",
                    "ticker": ticker,
                    "broker_qty": broker_pos.qty,
                    "journal_implied_qty": implied_qty,
                }
            )
            continue
        if broker_pos.qty < implied_qty:
            mismatches.append(
                {
                    "case": "position_undersized_vs_journal",
                    "ticker": ticker,
                    "broker_qty": broker_pos.qty,
                    "journal_implied_qty": implied_qty,
                }
            )
            continue
        if broker_pos.avg_price != implied_avg:
            events.append(
                ReconciliationEvent(
                    event_type="avg_price_drift",
                    payload={
                        "ticker": ticker,
                        "journal_avg_price": str(implied_avg),
                        "broker_avg_price": str(broker_pos.avg_price),
                        "delta": str(broker_pos.avg_price - implied_avg),
                    },
                    ticker=ticker,
                )
            )

    for ticker, (implied_qty, _) in projected_by_ticker.items():
        if ticker not in broker_by_ticker:
            mismatches.append(
                {
                    "case": "journal_position_missing_at_broker",
                    "ticker": ticker,
                    "journal_implied_qty": implied_qty,
                }
            )

    # ---- Phase B.3: protective-stop invariants (Q31) ----
    #
    # Consumes Q32's expected_stop_children checkpoint AND the current
    # journal_tail (for freshly-opened positions without a prior
    # checkpoint entry) + broker open orders to enforce three safety
    # invariants on adopted positions with a journaled protective
    # stop. Any mismatch emits into `mismatches` (existing path), so
    # recovery fails with MISMATCH_REFUSED per kickoff Decision 4.
    # No auto-recovery, no auto-reattach -- operator investigates
    # before relaunching.
    #
    # Three cases, ordered by specificity:
    #   - missing_protective_stop: broker has NO stop child at all
    #     for this ticker whose trade_id matches the expected one.
    #   - protective_stop_tag_mismatch: broker has a stop on the
    #     ticker but its parsed (strategy, trade_id) pair differs
    #     from the expected entry.
    #   - protective_stop_price_drift: broker has a matching-tag stop
    #     but its trigger (aux_price for STP orders) differs from
    #     the expected trigger_price via exact Decimal equality
    #     (kickoff Decision 6: strict MVP, no tolerance).
    #
    # Intentionally-cancelled stops fail with missing_protective_stop
    # per Decision 5. There is no journal event recording "operator
    # cancelled intent" today; fail-closed is the correct default
    # until a Phase 4 `/invest unprotect-position` command exists.
    #
    # Only evaluates positions that are currently held at the broker
    # (ticker in broker_by_ticker). An expected entry whose ticker
    # has no broker position is irrelevant here (the position was
    # closed; any orphaned stop at broker is caught by Phase B.1's
    # broker_position_tickers gate).
    #
    # Union source: prior-checkpoint entries (Q32 persistence across
    # restart hops) + journal-tail parents (first-time protection for
    # positions created in the current window that haven't been
    # checkpointed yet). Fresh journal parents WIN when trade_ids
    # collide -- the journal is always more authoritative than a
    # stale checkpoint for the same trade_id. MiniMax R1 Finding 2
    # called out this defense-in-depth requirement.
    expected_entries: list[dict[str, Any]] = []
    covered_trade_ids: set[str] = set()
    # Step 1: walk journal_tail for recent parents with stop_loss on
    # adopted-position tickers. For each unique (ticker, trade_id)
    # with the latest non-null stop_loss, synthesize an entry in the
    # checkpoint shape so Phase B.3 can validate it uniformly.
    # MVP one-parent-per-ticker means at most one journal entry per
    # ticker here; newer records overwrite older.
    position_tickers_now = set(broker_by_ticker)
    journal_parent_by_ticker: dict[str, dict[str, Any]] = {}
    for rec in journal_tail:
        et = rec.get("event_type")
        payload = rec.get("payload") or {}
        # Q39-B (MiniMax R1 2026-04-21): Phase B.3 must also pick up
        # stop_loss from a prior run's assumed-fill event when the
        # original order_submitted has aged out of journal_tail.
        # Without this, Q31 spuriously fires missing_protective_stop
        # on a correctly-protected assumed-filled position after one
        # hop.
        is_assumed_fill_record = (
            et == "recovery_reconciled"
            and payload.get("case")
            == "pending_no_broker_counterpart_assumed_filled"
        )
        if et not in {"order_submitted", "order_filled"} and not is_assumed_fill_record:
            continue
        if is_assumed_fill_record:
            # Stop metadata on assume-fill events lives in
            # payload.journal_view (mirrors Phase A's _pending_payload
            # output). Side/ticker likewise.
            journal_view = payload.get("journal_view") or {}
            ticker = rec.get("ticker") or journal_view.get("ticker")
        else:
            ticker = rec.get("ticker") or payload.get("ticker")
        if not ticker or ticker not in position_tickers_now:
            continue
        trade_id = rec.get("trade_id")
        if not trade_id:
            continue
        if is_assumed_fill_record:
            journal_view = payload.get("journal_view") or {}
            side = str(journal_view.get("side") or "").lower()
            stop_loss = journal_view.get("stop_loss")
        else:
            side = (rec.get("side") or payload.get("side") or "").lower()
            stop_loss = payload.get("stop_loss")
        if side and side != "buy":
            continue
        strategy = rec.get("strategy")
        prior = journal_parent_by_ticker.get(ticker)
        if (
            prior is not None
            and prior.get("trade_id") == trade_id
            and stop_loss in (None, "None", "")
            and prior.get("stop_loss") not in (None, "None", "")
        ):
            stop_loss = prior["stop_loss"]
        journal_parent_by_ticker[ticker] = {
            "ticker": ticker,
            "strategy": strategy,
            "trade_id": trade_id,
            "stop_loss": stop_loss,
        }
    # Also scan the Phase A synthesized events for recovery_reconciled
    # events that carry stop metadata in journal_view:
    #   - pending_filled / pending_partially_filled: engine crashed
    #     between order_proposed and order_submitted; the only
    #     surviving stop metadata lives in journal_view (Codex
    #     Commit-2 R1 P1).
    #   - pending_no_broker_counterpart_assumed_filled: fresh assume-
    #     fill emitted in this pass; journal_view holds stop_loss
    #     from the pending (Q39-B, MiniMax R1 2026-04-21). Without
    #     the scan, the checkpoint written in this pass' engine_recovered
    #     would be empty for assume-filled positions.
    parent_fill_cases = {
        "pending_filled",
        "pending_partially_filled",
        "pending_no_broker_counterpart_assumed_filled",
    }
    for ev in events:
        if ev.event_type != "recovery_reconciled":
            continue
        payload = ev.payload or {}
        if payload.get("case") not in parent_fill_cases:
            continue
        ticker = ev.ticker
        trade_id = ev.trade_id
        if not ticker or ticker not in position_tickers_now or not trade_id:
            continue
        journal_view = payload.get("journal_view") or {}
        side = str(journal_view.get("side") or "").lower()
        if side and side != "buy":
            continue
        stop_loss = journal_view.get("stop_loss")
        prior = journal_parent_by_ticker.get(ticker)
        if (
            prior is not None
            and prior.get("trade_id") == trade_id
            and stop_loss in (None, "None", "")
            and prior.get("stop_loss") not in (None, "None", "")
        ):
            stop_loss = prior["stop_loss"]
        journal_parent_by_ticker[ticker] = {
            "ticker": ticker,
            "strategy": ev.strategy,
            "trade_id": trade_id,
            "stop_loss": stop_loss,
        }

    for parent in journal_parent_by_ticker.values():
        strategy = parent.get("strategy")
        trade_id = parent.get("trade_id")
        stop_loss = parent.get("stop_loss")
        if not strategy or not trade_id:
            continue
        if stop_loss in (None, "None", ""):
            continue
        expected_entries.append(
            {
                "ticker": parent["ticker"],
                "strategy": strategy,
                "parent_trade_id": trade_id,
                "client_tag": (
                    f"{strategy}:{trade_id}{CLIENT_TAG_STOP_SUFFIX}"
                ),
                "trigger_price": str(stop_loss),
            }
        )
        covered_trade_ids.add(trade_id)
    # Step 2: add prior-checkpoint entries whose TICKER has no fresh
    # journal parent at all. Codex Commit-2 R2 P1: MVP one-parent-
    # per-ticker means a fresh journal parent on a ticker supersedes
    # every stale checkpoint entry for that ticker, not just ones
    # with the matching trade_id. This mirrors the supersede-by-
    # ticker rule in build_expected_stop_children and prevents a
    # legitimate same-ticker exit-and-reenter from failing recovery
    # due to the stale stop's missing tag/trigger at broker.
    if checkpoint_stop_children:
        fresh_journal_tickers = set(journal_parent_by_ticker)
        for entry in checkpoint_stop_children:
            tid = entry.get("parent_trade_id")
            entry_ticker = entry.get("ticker")
            if not tid or tid in covered_trade_ids:
                continue
            if entry_ticker in fresh_journal_tickers:
                # Fresh journal parent exists for this ticker; the
                # checkpoint's stale entry is superseded.
                continue
            expected_entries.append(entry)
            covered_trade_ids.add(tid)

    if expected_entries:
        stops_by_ticker: dict[str, list[BrokerOpenOrder]] = {}
        for open_order in broker_open_orders:
            _strategy, _tid, is_stop_order = parse_client_tag(
                open_order.client_tag
            )
            if not is_stop_order:
                continue
            stops_by_ticker.setdefault(open_order.ticker, []).append(
                open_order
            )
        for entry in expected_entries:
            ticker = entry.get("ticker")
            expected_trade_id = entry.get("parent_trade_id")
            expected_strategy = entry.get("strategy")
            expected_client_tag = entry.get("client_tag")
            expected_trigger_raw = entry.get("trigger_price")
            if (
                not ticker
                or not expected_trade_id
                or not expected_strategy
                or not expected_client_tag
                or expected_trigger_raw in (None, "", "None")
            ):
                # Malformed checkpoint entry; nothing reliable to
                # validate against. Skip.
                continue
            if ticker not in broker_by_ticker:
                # Position closed; orphan stop (if any) is Phase B.1's
                # concern. No Q31 invariant applies.
                continue
            try:
                expected_trigger = Decimal(str(expected_trigger_raw))
            except (InvalidOperation, ValueError, TypeError):
                # Corrupt trigger_price in checkpoint; treat as
                # missing (fail-closed).
                mismatches.append(
                    {
                        "case": "missing_protective_stop",
                        "ticker": ticker,
                        "expected_trade_id": expected_trade_id,
                        "expected_strategy": expected_strategy,
                        "expected_client_tag": expected_client_tag,
                        "note": "checkpoint trigger_price is corrupt",
                    }
                )
                continue
            ticker_stops = stops_by_ticker.get(ticker, [])
            if not ticker_stops:
                # No broker-side stop child at all for this ticker.
                # Could be: operator cancelled, broker dropped, order
                # never placed. Fail-closed per Decision 5.
                mismatches.append(
                    {
                        "case": "missing_protective_stop",
                        "ticker": ticker,
                        "expected_trade_id": expected_trade_id,
                        "expected_strategy": expected_strategy,
                        "expected_client_tag": expected_client_tag,
                        "expected_trigger_price": str(expected_trigger),
                    }
                )
                continue
            matched_stop: BrokerOpenOrder | None = None
            tag_mismatch_stops: list[BrokerOpenOrder] = []
            for stop in ticker_stops:
                parsed_strategy, parsed_tid, _ = parse_client_tag(
                    stop.client_tag
                )
                if (
                    parsed_strategy == expected_strategy
                    and parsed_tid == expected_trade_id
                ):
                    matched_stop = stop
                    break
                tag_mismatch_stops.append(stop)
            if matched_stop is None:
                # Broker has stops on this ticker but none with the
                # expected (strategy, trade_id) pair. Report each
                # non-matching stop's actual tag so the operator can
                # investigate what's really protecting the position.
                mismatches.append(
                    {
                        "case": "protective_stop_tag_mismatch",
                        "ticker": ticker,
                        "expected_trade_id": expected_trade_id,
                        "expected_strategy": expected_strategy,
                        "expected_client_tag": expected_client_tag,
                        "broker_stop_tags": [
                            stop.client_tag for stop in tag_mismatch_stops
                        ],
                    }
                )
                continue
            # Tag match confirmed; validate trigger price via exact
            # Decimal equality (Decision 6). Stop orders carry trigger
            # in aux_price (auxPrice on IBKR's side); a Decimal("0")
            # default from a non-STP order or a connector that can't
            # surface auxPrice is treated as drift so the safety
            # property holds even when trigger info is missing.
            broker_trigger = matched_stop.aux_price
            if broker_trigger != expected_trigger:
                mismatches.append(
                    {
                        "case": "protective_stop_price_drift",
                        "ticker": ticker,
                        "expected_trade_id": expected_trade_id,
                        "expected_strategy": expected_strategy,
                        "expected_client_tag": expected_client_tag,
                        "expected_trigger_price": str(expected_trigger),
                        "broker_trigger_price": str(broker_trigger),
                        "broker_order_id": matched_stop.broker_order_id,
                        "broker_perm_id": matched_stop.broker_perm_id,
                    }
                )

    # ---- Q42: orphan-STOP adoption resolution ----
    #
    # Architect-issued K2BI_ADOPT_ORPHAN_STOP request resolves the
    # SPECIFIC permId mismatch when the broker holds a STOP order the
    # engine has never seen before. Adoption fails closed: malformed
    # request, no matching permId, or matched-order-is-not-a-STOP all
    # emit a NEW mismatch entry that keeps status=MISMATCH_REFUSED.
    # Defense in depth: adoption removes ONLY the matching mismatch
    # entry; OTHER unknown broker state still trips MISMATCH_REFUSED.
    if adopt_orphan_stop is not None:
        target_perm = str(adopt_orphan_stop.perm_id)
        matched_open: BrokerOpenOrder | None = None
        for o in broker_open_orders:
            if str(o.broker_perm_id or "") == target_perm:
                matched_open = o
                break
        if matched_open is None:
            mismatches.append(
                {
                    "case": "adopt_orphan_stop_perm_not_found",
                    "requested_perm_id": adopt_orphan_stop.perm_id,
                    "env": adopt_orphan_stop_env_name,
                    "reason": (
                        f"{adopt_orphan_stop_env_name} requested adoption of "
                        f"permId {adopt_orphan_stop.perm_id} but no matching "
                        "broker open order was found. Refusing to silently "
                        "swallow the request -- operator must verify the "
                        "permId or unset the env var."
                    ),
                }
            )
        else:
            # Codex R1 P1: aux_price > 0 alone is too weak -- TRAIL and
            # TRAIL LIMIT orders also populate auxPrice (with the trail
            # amount/percent, NOT a stop trigger). Require an explicit
            # STP order type so a TRAIL cannot sneak through. An empty
            # order_type (connector that doesn't surface this field)
            # also fails closed -- adoption refuses rather than guessing.
            order_type_norm = (matched_open.order_type or "").upper().strip()
            is_stop_order = order_type_norm in ("STP", "STP LMT")
            if not is_stop_order:
                mismatches.append(
                    {
                        "case": "adopt_orphan_stop_not_a_stop",
                        "requested_perm_id": adopt_orphan_stop.perm_id,
                        "matched_order_type": matched_open.order_type,
                        "matched_aux_price": str(matched_open.aux_price),
                        "matched_client_tag": matched_open.client_tag,
                        "matched_limit_price": str(matched_open.limit_price),
                        "reason": (
                            f"Broker order at permId "
                            f"{adopt_orphan_stop.perm_id} is not a STOP "
                            f"order (order_type={matched_open.order_type!r}; "
                            "adoption is restricted to STP / STP LMT this "
                            "ship). Operator must verify the permId targets "
                            "the intended broker-side STOP."
                        ),
                    }
                )
            else:
                # Codex R1 P1: adoption resolves the phantom-open-order
                # mismatch for THIS permId only. The previous filter
                # also removed protective_stop_price_drift entries
                # carrying the same broker_perm_id, which would silently
                # erase a Q31 safety failure if the operator pointed
                # K2BI_ADOPT_ORPHAN_STOP at an existing broker stop with
                # the wrong trigger. Narrow to case=phantom_open_order
                # so all OTHER cases (Q31 protective-stop integrity, etc.)
                # remain in mismatches and continue to trip
                # MISMATCH_REFUSED.
                mismatches = [
                    m
                    for m in mismatches
                    if not (
                        m.get("case") == "phantom_open_order"
                        and str(m.get("broker_perm_id") or "") == target_perm
                    )
                ]
                from ..journal.schema import (
                    validate_orphan_stop_adopted_payload,
                )

                adoption_payload = {
                    "permId": adopt_orphan_stop.perm_id,
                    "ticker": matched_open.ticker,
                    "qty": matched_open.qty,
                    "stop_price": str(matched_open.aux_price),
                    "source": "operator-portal",
                    "adopted_at": now.isoformat(),
                    "justification": adopt_orphan_stop.justification,
                }
                # Programmer-error guard: a malformed payload built by
                # our own code is a bug, not operator error -- raise so
                # the test suite catches it and the journal stays clean.
                validate_orphan_stop_adopted_payload(adoption_payload)
                events.append(
                    ReconciliationEvent(
                        event_type="orphan_stop_adopted",
                        payload=adoption_payload,
                        ticker=matched_open.ticker,
                        broker_order_id=matched_open.broker_order_id,
                        broker_perm_id=str(adopt_orphan_stop.perm_id),
                    )
                )

    # ---- 4. status assembly ----

    if not mismatches and not events and not broker_positions and not broker_open_orders:
        status = RecoveryStatus.CLEAN
    elif not mismatches:
        status = RecoveryStatus.CATCH_UP
    else:
        status = (
            RecoveryStatus.MISMATCH_OVERRIDE
            if override_active
            else RecoveryStatus.MISMATCH_REFUSED
        )

    # Record override usage explicitly so the audit trail shows why a
    # session started despite discrepancies.
    if status == RecoveryStatus.MISMATCH_OVERRIDE:
        events.append(
            ReconciliationEvent(
                event_type="recovery_state_mismatch",
                payload={
                    "override_env": override_env_name,
                    "override_value": override_raw,
                    "mismatch_count": len(mismatches),
                    "mismatches": mismatches,
                    "resolution": "proceeding_with_override",
                    "ts": now.isoformat(),
                },
            )
        )
    elif status == RecoveryStatus.MISMATCH_REFUSED:
        events.append(
            ReconciliationEvent(
                event_type="recovery_state_mismatch",
                payload={
                    "override_env": override_env_name,
                    "override_value": override_raw,
                    "mismatch_count": len(mismatches),
                    "mismatches": mismatches,
                    "resolution": "engine_refuses_start",
                    "ts": now.isoformat(),
                },
            )
        )

    return ReconciliationResult(
        status=status,
        events=events,
        mismatch_reasons=mismatches,
        adopted_positions=list(broker_positions),
        adopted_open_orders=list(broker_open_orders),
    )


# ---------- journal-implied state extraction ----------


def _positions_from_journal(
    records: Iterable[dict[str, Any]],
) -> list[PositionFromJournal]:
    """Walk records forward, tracking (qty, avg_price) per ticker.

    Sources of position state, in replay order:
        - engine_recovered.adopted_positions    (snapshot from prior
                                                 reconciliation / override)
        - order_filled                          (live fill event)
        - recovery_reconciled with case=pending_filled /
          pending_partially_filled             (fill discovered during
                                                 recovery)

    Engine_recovered acts as a "checkpoint" -- after any recovery pass
    writes it, subsequent startups can seed from that snapshot even if
    the original order_filled events have since aged out of the
    lookback window. Without this, a restart that overrode a
    recovery_state_mismatch and adopted broker positions would see the
    same broker positions flagged as phantoms on the next restart once
    the original fills scrolled out of the 48-hour window (Codex
    round-1 P2).

    All other events (validator_pass, order_proposed, order_submitted,
    order_rejected, breaker_triggered, kill_*, auth_*, disconnect_*,
    eod_*) do not change positions. This keeps the extraction
    predictable -- one event type, one effect.
    """
    per_ticker: dict[str, tuple[int, Decimal]] = {}
    for rec in records:
        event_type = rec.get("event_type")
        payload = rec.get("payload", {}) or {}
        if event_type == "engine_recovered":
            # Snapshot reset: the recovery that wrote this record made
            # the engine's view of the world consistent with the
            # broker at that moment, so we REPLACE the accumulated
            # per_ticker rather than add to it. Any later order_filled
            # or recovery_reconciled events in the same replay window
            # still accumulate on top.
            per_ticker = {}
            for adopted in payload.get("adopted_positions", []) or []:
                ticker = adopted.get("ticker")
                qty = adopted.get("qty")
                avg = adopted.get("avg_price")
                if not ticker or qty is None or avg is None:
                    continue
                try:
                    qty_int = int(qty)
                    avg_dec = Decimal(str(avg))
                except (ValueError, TypeError, InvalidOperation):
                    continue
                if qty_int == 0:
                    continue
                per_ticker[ticker] = (qty_int, avg_dec)
        elif event_type == "order_filled":
            ticker = rec.get("ticker") or payload.get("ticker")
            side = rec.get("side") or payload.get("side")
            qty = rec.get("qty") or payload.get("qty")
            price_raw = payload.get("fill_price")
            if not ticker or not side or qty is None or price_raw is None:
                continue
            per_ticker[ticker] = _apply_fill(
                per_ticker.get(ticker, (0, Decimal("0"))),
                side=str(side).lower(),
                qty=int(qty),
                price=Decimal(str(price_raw)),
            )
        elif event_type == "recovery_reconciled":
            case = payload.get("case")
            # Q39-A: pending_no_broker_counterpart_assumed_filled is
            # treated as a synthetic fill -- journal says we submitted
            # with a broker_perm_id, broker visibility can't see it,
            # so the hybrid rule assumes fill and projects the
            # position. If the assumption is wrong, Phase B.2's
            # journal_position_missing_at_broker fires on the next
            # reconcile (Q39 divergence detection).
            if case not in {
                "pending_filled",
                "pending_partially_filled",
                "pending_no_broker_counterpart_assumed_filled",
            }:
                continue
            ticker = rec.get("ticker") or payload.get("ticker")
            side = payload.get("journal_view", {}).get("side")
            qty = payload.get("filled_qty")
            price_raw = payload.get("avg_fill_price")
            if not ticker or not side or qty is None or price_raw is None:
                continue
            per_ticker[ticker] = _apply_fill(
                per_ticker.get(ticker, (0, Decimal("0"))),
                side=str(side).lower(),
                qty=int(qty),
                price=Decimal(str(price_raw)),
            )

    out: list[PositionFromJournal] = []
    for ticker, (qty, avg) in per_ticker.items():
        if qty == 0:
            continue
        out.append(PositionFromJournal(ticker=ticker, qty=qty, avg_price=avg))
    return out


def _apply_fill(
    state: tuple[int, Decimal],
    *,
    side: str,
    qty: int,
    price: Decimal,
) -> tuple[int, Decimal]:
    held, avg = state
    if side == "buy":
        new_held = held + qty
        if new_held <= 0:
            return new_held, Decimal("0")
        total_cost = avg * Decimal(held) + price * Decimal(qty)
        return new_held, total_cost / Decimal(new_held)
    # sell: reduce inventory at the same cost basis (no realized-P&L
    # tracking at the journal-replay level; that is invest-journal's
    # concern).
    new_held = held - qty
    if new_held <= 0:
        return 0, Decimal("0")
    return new_held, avg


def _pending_from_journal(
    records: Iterable[dict[str, Any]],
) -> list[PendingFromJournal]:
    """Scan for orders whose last event is submitted-but-not-terminal.

    Algorithm: walk forward, keeping a map keyed by the most stable
    identifier available (trade_id > perm_id > order_id). Terminal
    events clear the entry; NON-terminal events (partial fills) must
    leave it in place so a crash mid-sequence correctly classifies the
    remainder as still-pending.

    Codex round-6 P1: `order_filled` is emitted on every fill,
    including partials -- the engine's payload carries
    `cumulative_filled_qty` + `remaining_qty`. Treating any
    `order_filled` as terminal loses track of partially filled live
    orders across restart. The correct predicate is:
        full-fill  -> cumulative_filled_qty >= order qty (terminal)
        partial    -> cumulative_filled_qty < order qty (still pending)

    When a journal record lacks cumulative_filled_qty (old v1 record
    or an external producer), fall back to accumulating `qty` from the
    record itself, which the engine sets to the SINGLE-FILL qty on
    order_filled events.
    """
    per_key: dict[str, PendingFromJournal] = {}
    per_key_filled: dict[str, int] = {}
    terminal_cases = {
        "pending_filled",
        "pending_cancelled",
        "pending_rejected",
        "pending_partially_filled",  # remainder-only reconciliation
        "pending_no_broker_counterpart",
        # Q39-A: once recovery has ASSUMED a fill for a pending order
        # (journal had broker_perm_id; broker visibility can't see it),
        # subsequent restarts must treat the decision as final. Without
        # this, the same order would be re-reconciled every restart
        # and either re-emit pending_no_broker_counterpart or stack
        # synthetic fills into projected positions.
        "pending_no_broker_counterpart_assumed_filled",
    }
    for rec in records:
        event_type = rec.get("event_type")
        payload = rec.get("payload", {}) or {}
        key = _pending_key(rec)
        if key is None:
            continue
        if event_type in {"order_proposed", "order_submitted"}:
            try:
                qty_int = int(rec.get("qty") or payload.get("qty") or 0)
            except (TypeError, ValueError):
                LOG.warning(
                    "recovery: corrupt qty in journal payload (%r); using 0",
                    rec.get("qty") or payload.get("qty"),
                )
                qty_int = 0
            per_key[key] = PendingFromJournal(
                trade_id=rec.get("trade_id"),
                strategy=rec.get("strategy"),
                broker_order_id=rec.get("broker_order_id"),
                broker_perm_id=rec.get("broker_perm_id"),
                ticker=rec.get("ticker") or payload.get("ticker", ""),
                side=(rec.get("side") or payload.get("side", "")).lower(),
                qty=qty_int,
                limit_price=_safe_decimal(payload.get("limit_price")),
                submitted_at=_parse_ts(rec.get("ts")) or datetime.now(timezone.utc),
                stop_loss=_safe_decimal(payload.get("stop_loss")),
            )
            per_key_filled.setdefault(key, 0)
        elif event_type == "order_filled":
            # Track cumulative fill. Prefer the engine-authored
            # `cumulative_filled_qty` when present (accurate across
            # partials); otherwise accumulate the per-record fill qty.
            cumulative_raw = payload.get("cumulative_filled_qty")
            if cumulative_raw is not None:
                try:
                    per_key_filled[key] = int(cumulative_raw)
                except (TypeError, ValueError):
                    pass
            else:
                fill_raw = rec.get("qty") or payload.get("fill_qty")
                if fill_raw is not None:
                    try:
                        per_key_filled[key] = (
                            per_key_filled.get(key, 0) + int(fill_raw)
                        )
                    except (TypeError, ValueError):
                        pass
            pending = per_key.get(key)
            if pending is not None and per_key_filled.get(key, 0) >= pending.qty:
                per_key.pop(key, None)
        elif event_type in {"order_rejected", "order_timeout"}:
            per_key.pop(key, None)
        elif event_type == "kill_blocked":
            # Codex round-9 P2: kill_blocked can fire between an
            # order_proposed record and any broker call -- the order
            # was intentionally never sent. Leaving the proposal in
            # per_key would misclassify every kill-during-submit case
            # as pending_no_broker_counterpart on restart.
            per_key.pop(key, None)
        elif event_type == "recovery_reconciled":
            case = payload.get("case")
            if case in terminal_cases:
                per_key.pop(key, None)

    return list(per_key.values())


def _pending_key(record: dict[str, Any]) -> str | None:
    """Pick the lifecycle-stable identifier for a pending-order entry.

    Codex round-2 P2: trade_id is the FIRST identifier every event in
    a trade lifecycle carries (engine emits it on order_proposed,
    before any broker call), and it remains on every subsequent
    order_submitted / order_filled / order_rejected. Keying on
    trade_id first means the proposal event + terminal event land on
    the same map entry and terminal cleanup actually clears the
    pending. Keying on perm_id first (as the prior implementation
    did) stranded the trade-keyed proposal entry because perm_id
    isn't assigned until submit, so every completed trade left a
    phantom "pending_no_broker_counterpart" after recovery.

    perm_id / order_id remain as fallback so records emitted by a
    process that didn't stamp trade_id can still be tracked.
    """
    trade_id = record.get("trade_id")
    if trade_id:
        return f"trade:{trade_id}"
    perm = record.get("broker_perm_id")
    if perm:
        return f"perm:{perm}"
    oid = record.get("broker_order_id")
    if oid:
        return f"oid:{oid}"
    return None


# ---------- broker-side matching ----------


def _index_open_orders(
    rows: Iterable[BrokerOpenOrder],
) -> dict[str, BrokerOpenOrder]:
    idx: dict[str, BrokerOpenOrder] = {}
    for row in rows:
        if row.broker_perm_id:
            idx[f"perm:{row.broker_perm_id}"] = row
        if row.broker_order_id:
            idx[f"oid:{row.broker_order_id}"] = row
    return idx


def _index_status_events(
    rows: Iterable[BrokerOrderStatusEvent],
) -> dict[str, BrokerOrderStatusEvent]:
    """Most-recent-wins if the broker returned multiple status updates
    for the same order.  IBKR's reqCompletedOrdersAsync() can include
    intermediate states; we keep only the terminal one."""
    idx: dict[str, BrokerOrderStatusEvent] = {}
    for row in rows:
        if row.status not in TERMINAL_ORDER_STATUSES:
            continue
        for k in _status_keys(row):
            prior = idx.get(k)
            if prior is None or row.last_update_at >= prior.last_update_at:
                idx[k] = row
    return idx


def _status_keys(row: BrokerOrderStatusEvent) -> list[str]:
    out: list[str] = []
    if row.broker_perm_id:
        out.append(f"perm:{row.broker_perm_id}")
    if row.broker_order_id:
        out.append(f"oid:{row.broker_order_id}")
    return out


def _match_broker_order(
    pending: PendingFromJournal,
    status_index: dict[str, BrokerOrderStatusEvent],
    open_index: dict[str, BrokerOpenOrder],
    *,
    trade_id_open_index: dict[str, BrokerOpenOrder] | None = None,
    trade_id_status_index: dict[str, BrokerOrderStatusEvent] | None = None,
) -> tuple[str, BrokerOrderStatusEvent | BrokerOpenOrder] | None:
    # Broker-ID match first: strongest signal.
    for key_fn in (
        lambda: f"perm:{pending.broker_perm_id}" if pending.broker_perm_id else None,
        lambda: f"oid:{pending.broker_order_id}" if pending.broker_order_id else None,
    ):
        key = key_fn()
        if key is None:
            continue
        if key in status_index:
            return "status", status_index[key]
        if key in open_index:
            return "open", open_index[key]

    # Trade-id fallback (Codex round-4 P1): when the journal never
    # recorded broker IDs because order_submitted write was lost, we
    # can still match via the orderRef/client_tag that ib_async sent
    # to the broker at submit-time. The caller passes indexes built
    # from BrokerOpenOrder.client_tag and the status-event side
    # equivalent; missing indexes skip this branch.
    if pending.trade_id:
        if trade_id_status_index is not None:
            hit = trade_id_status_index.get(pending.trade_id)
            if hit is not None:
                return "status", hit
        if trade_id_open_index is not None:
            hit = trade_id_open_index.get(pending.trade_id)
            if hit is not None:
                return "open", hit
    return None


def _index_open_orders_by_trade_id(
    rows: Iterable[BrokerOpenOrder],
) -> dict[str, BrokerOpenOrder]:
    """Map trade_id -> parent open order.

    Codex round-14 P1: stop children MUST be excluded from this
    index. Crash-window scenario: order_proposed journaled, parent
    filled before restart, only the :stop child remains open at
    broker. If we let a child slot into the trade_id fallback,
    _match_broker_order would classify the parent as
    pending_still_open (wrongly) and either resume the wrong order
    or flag phantom position on next tick. The stop child is
    recognized separately via stop_child_recognized; this map only
    tracks parents.
    """
    parents: dict[str, BrokerOpenOrder] = {}
    for row in rows:
        strategy, trade_id, is_stop = parse_client_tag(row.client_tag)
        if not trade_id or is_stop:
            continue
        parents[trade_id] = row
    return parents


def _index_status_events_by_trade_id(
    rows: Iterable[BrokerOrderStatusEvent],
) -> dict[str, BrokerOrderStatusEvent]:
    """Map trade_id -> most-recent-terminal status event.

    Codex round-11 P1: crash-window orders (submit succeeded, journal
    had only order_proposed) can finish at the broker before restart.
    BrokerOrderStatusEvent now carries client_tag (populated by the
    connector from ib_async's orderRef on completed orders), so
    recovery can match by trade_id and classify the terminal fate
    rather than leaving the proposal as pending_no_broker_counterpart.
    """
    idx: dict[str, BrokerOrderStatusEvent] = {}
    for row in rows:
        if row.status not in TERMINAL_ORDER_STATUSES:
            continue
        strategy, trade_id, _ = parse_client_tag(row.client_tag)
        if not trade_id:
            continue
        prior = idx.get(trade_id)
        if prior is None or row.last_update_at >= prior.last_update_at:
            idx[trade_id] = row
    return idx


def _classify_terminal(status: BrokerOrderStatusEvent) -> str:
    """Map a terminal broker status to a reconciliation case.

    Codex round-2 P1: the dimension that matters for position
    reconciliation is `filled_qty`, not the status string. A Cancelled
    or Rejected order with filled_qty > 0 still moved inventory, and
    the reconciliation_deltas pass must see it as a partial fill so
    the implied position includes those shares. Otherwise the next
    restart sees the broker position as phantom.
    """
    if status.filled_qty > 0:
        if status.remaining_qty == 0:
            return "pending_filled"
        return "pending_partially_filled"
    if status.status == "Rejected":
        return "pending_rejected"
    if status.status in {"Cancelled", "ApiCancelled", "Inactive"}:
        return "pending_cancelled"
    # Any other terminal-ish status (shouldn't happen given the filter
    # in _index_status_events) falls through to cancelled semantics.
    return "pending_cancelled"


def _pending_payload(pending: PendingFromJournal) -> dict[str, Any]:
    return {
        "ticker": pending.ticker,
        "side": pending.side,
        "qty": pending.qty,
        "limit_price": (
            str(pending.limit_price)
            if pending.limit_price is not None
            else None
        ),
        "stop_loss": (
            str(pending.stop_loss)
            if pending.stop_loss is not None
            else None
        ),
        "submitted_at": pending.submitted_at.isoformat(),
    }


def _perm_id_of(obj: Any) -> str | None:
    value = getattr(obj, "broker_perm_id", None)
    return value or None


def _order_id_of(obj: Any) -> str | None:
    value = getattr(obj, "broker_order_id", None)
    return value or None


def _parse_ts(raw: Any) -> datetime | None:
    if raw is None:
        return None
    try:
        return datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return None


def _barrier_timeout_trade_ids(
    journal_tail: Iterable[dict[str, Any]],
) -> set[str]:
    """Collect trade_ids referenced by any once_exit_barrier_timeout
    event in the journal tail.

    Q33 (architect 2026-04-21): the engine's --once pre-exit barrier
    writes this event when it times out waiting on a pending order's
    terminal status. Q39-B (this file) reads the set to decide the
    evidence tier for the assumed-fill path: barrier_timeout ->
    strongest (engine deliberately exited mid-wait); absent ->
    crash_gap (engine crashed without the barrier running, weaker).

    Defensive parsing: malformed records contribute nothing. Callers
    must treat an empty set as "no barrier evidence anywhere" (same as
    no event at all).
    """
    out: set[str] = set()
    for rec in journal_tail:
        if rec.get("event_type") != "once_exit_barrier_timeout":
            continue
        payload = rec.get("payload") or {}
        orders = payload.get("pending_orders")
        if not isinstance(orders, list):
            continue
        for order in orders:
            if not isinstance(order, dict):
                continue
            tid = order.get("trade_id")
            if isinstance(tid, str) and tid:
                out.add(tid)
    return out


def _latest_checkpoint_stop_children(
    journal_tail: Iterable[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Return the expected_stop_children list from the most recent
    engine_recovered event in journal_tail, or an empty list if absent.

    Q32: engine_recovered is the recovery checkpoint. Its payload now
    carries expected_stop_children so that stop-child recognition can
    span multiple restarts even after the original order_submitted /
    order_filled records age out of the 48h lookback. Multiple
    engine_recovered events can exist in journal_tail across bounces;
    the latest wins.

    Defensive defaults: any shape other than a list resolves to empty
    (old v1-style checkpoints emitted before this field existed, or a
    corrupt write). Callers must treat an empty list as "no prior
    stop-child expectations" -- same as the pre-Q32 behavior.
    """
    latest: list[dict[str, Any]] = []
    for rec in journal_tail:
        if rec.get("event_type") != "engine_recovered":
            continue
        payload = rec.get("payload") or {}
        entries = payload.get("expected_stop_children")
        if isinstance(entries, list):
            latest = [e for e in entries if isinstance(e, dict)]
        else:
            latest = []
    return latest


def build_expected_stop_children(
    *,
    positions: list[BrokerPosition],
    journal_tail: list[dict[str, Any]],
    recovery_events: list[ReconciliationEvent] | None = None,
) -> list[dict[str, Any]]:
    """Compute the expected_stop_children checkpoint for engine_recovered.

    Q32 (LOCKED shape per Session A kickoff Design Decision 2):

        [
            {
                "ticker": str,
                "strategy": str,
                "parent_trade_id": str,
                "client_tag": f"{strategy}:{parent_trade_id}:stop",
                "trigger_price": str(Decimal),
            },
            ...
        ]

    For each adopted position, locate the parent buy order in
    journal_tail and, if that order carried a non-null stop_loss,
    emit a canonical expected-stop-child entry. When the parent has
    aged out of journal_tail, carry forward the matching entry from
    the most recent prior engine_recovered checkpoint so the
    multi-day chain does not lose protective-stop identity after
    one hop (Codex R1 P1).

    Source priority:
        1. Latest order_submitted/order_filled in journal_tail for
           this ticker (fresh journal view wins, even if it drops the
           stop -- trust current intent).
        2. Prior engine_recovered checkpoint entry for this ticker
           (carry-forward path, unreachable when the journal has any
           current parent record for the ticker).

    Sources for stop_loss within the journal path, in walk order
    (later wins):
        - order_submitted.payload.stop_loss (primary; written by
          engine/main.py:_submit_order)
        - order_filled.payload.stop_loss (Q32 precondition; mirrors
          the parent's stop_loss onto the fill record so a window
          where order_submitted scrolled out but order_filled didn't
          still yields the correct trigger)

    Never emit None trigger_price: if stop_loss resolves to None/"None"
    on the latest record, treat the position as "no expected stop"
    (the same outcome as a strategy that never configured one).
    """
    if not positions:
        return []
    position_tickers = {p.ticker for p in positions}

    # Per-ticker, track ONLY the most recently-journaled buy parent.
    # MVP enforces one live parent per ticker (engine single-in-flight
    # guard); multi-parent layered buys are architect-escalation per
    # the Session A kickoff Decision notes. Taking the newest-wins
    # view also handles the same-ticker exit-and-reenter case cleanly
    # (Codex R6 P1): an older buy parent whose position was closed
    # and replaced with a new buy on the same ticker is superseded,
    # and its stop (if orphaned at broker) falls through to phantom
    # detection rather than being silently recognized.
    latest_parent_by_ticker: dict[str, dict[str, Any]] = {}
    # Per-ticker, the trade_id of the newest journaled buy parent.
    # Used to decide prior-checkpoint carry-forward: only carry
    # forward a prior entry when the ticker has NO newer journaled
    # parent (aged-out single-parent case).
    newest_journal_trade_id_by_ticker: dict[str, str] = {}

    def _consider_record(
        *,
        ticker: str | None,
        trade_id: str | None,
        strategy: str | None,
        side: str,
        stop_loss: Any,
    ) -> None:
        """Shared intake for journal records and
        recovery-event-synthesized records. Later calls OVERWRITE
        earlier ones per ticker -- callers pass records in journal
        order so newest wins."""
        if not ticker or ticker not in position_tickers:
            return
        if not trade_id:
            return
        if side and side.lower() != "buy":
            # Sell-side reduces inventory; not a parent for stop-child
            # identity. But still mark the ticker as having a fresh
            # journal record so the carry-forward check can decide
            # based on "any activity".
            return
        # Preserve prior stop_loss when the newer record for the SAME
        # trade_id has None (e.g. order_filled written pre-Q32
        # precondition without stop_loss). Stop_loss travels forward
        # with its trade_id through the parent's lifecycle. Only
        # applies within the same trade_id; a new trade_id supersedes
        # regardless of stop_loss comparison (Codex R6 P1).
        prior = latest_parent_by_ticker.get(ticker)
        effective_stop_loss = stop_loss
        if (
            prior is not None
            and prior.get("trade_id") == trade_id
            and effective_stop_loss in (None, "None", "")
            and prior.get("stop_loss") not in (None, "None", "")
        ):
            effective_stop_loss = prior["stop_loss"]
        latest_parent_by_ticker[ticker] = {
            "trade_id": trade_id,
            "strategy": strategy,
            "stop_loss": effective_stop_loss,
        }
        newest_journal_trade_id_by_ticker[ticker] = trade_id

    for rec in journal_tail:
        event_type = rec.get("event_type")
        if event_type not in {"order_submitted", "order_filled"}:
            continue
        payload = rec.get("payload") or {}
        _consider_record(
            ticker=rec.get("ticker") or payload.get("ticker"),
            trade_id=rec.get("trade_id"),
            strategy=rec.get("strategy"),
            side=(rec.get("side") or payload.get("side") or ""),
            stop_loss=payload.get("stop_loss"),
        )

    # Codex R5 P1: include recovery-discovered fills. When recovery
    # finds an in-flight parent that actually filled at the broker
    # during the outage, the ONLY stop metadata may live in the
    # freshly-emitted recovery_reconciled event's journal_view (for
    # crashes between order_proposed and order_submitted, that's
    # where stop_loss survives). Without this pass, the engine_recovered
    # checkpoint written immediately after recovery would be empty for
    # such positions, and the next restart would flag the live stop
    # child as phantom_open_order.
    if recovery_events:
        # Q39-B (MiniMax R1 2026-04-21): include the assume-fill case
        # so a fresh recovery pass' engine_recovered checkpoint carries
        # the assumed-filled position's protective-stop identity
        # forward to subsequent restarts.
        parent_fill_cases = {
            "pending_filled",
            "pending_partially_filled",
            "pending_no_broker_counterpart_assumed_filled",
        }
        for ev in recovery_events:
            if ev.event_type != "recovery_reconciled":
                continue
            payload = ev.payload or {}
            if payload.get("case") not in parent_fill_cases:
                continue
            journal_view = payload.get("journal_view") or {}
            _consider_record(
                ticker=ev.ticker or journal_view.get("ticker"),
                trade_id=ev.trade_id,
                strategy=ev.strategy,
                side=str(journal_view.get("side") or ""),
                stop_loss=journal_view.get("stop_loss"),
            )

    # Codex R1 P1 carry-forward: when journal_tail no longer contains
    # the parent for an adopted position, fall back to the prior
    # checkpoint's entries so successive restarts do not lose stop-
    # child identity. Without this, recovery hop #1 reads the prior
    # checkpoint to seed recognition (correct), but writes a fresh
    # engine_recovered with EMPTY expected_stop_children (because
    # journal_tail has no parent records). Hop #2 then has no
    # checkpoint to seed from and falls back to phantom_open_order --
    # the multi-day chain breaks after one hop.
    #
    # The prior-checkpoint map is list-per-ticker to preserve the
    # multi-parent case (Codex R2 P1): an older checkpoint with two
    # stop children on the same ticker must carry both forward when
    # the underlying parents have aged out.
    prior_checkpoint_entries = _latest_checkpoint_stop_children(journal_tail)
    prior_by_ticker: dict[str, list[dict[str, Any]]] = {}
    for entry in prior_checkpoint_entries:
        t = entry.get("ticker")
        if t and t in position_tickers:
            prior_by_ticker.setdefault(t, []).append(entry)

    required_fields = (
        "ticker",
        "strategy",
        "parent_trade_id",
        "client_tag",
        "trigger_price",
    )

    result: list[dict[str, Any]] = []
    for pos in positions:
        # Step 1: if journal_tail (or recovery events) carried a buy
        # parent for this ticker, emit a fresh entry from the newest
        # one. That supersedes any prior-checkpoint entry even if the
        # prior one had a stop and the fresh one doesn't (Codex R6 P1:
        # an exit-and-reenter cycle on the same ticker drops the old
        # parent's orphan stop from the checkpoint).
        parent = latest_parent_by_ticker.get(pos.ticker)
        newest_journal_tid = newest_journal_trade_id_by_ticker.get(pos.ticker)
        if parent is not None:
            strategy = parent.get("strategy")
            parent_trade_id = parent.get("trade_id")
            stop_loss = parent.get("stop_loss")
            if (
                strategy
                and parent_trade_id
                and stop_loss not in (None, "None", "")
            ):
                trigger = str(stop_loss)
                # Session A Design Decision 2 locks the canonical
                # client_tag format: f"{strategy}:{trade_id}:stop".
                # Semantic identity, not the full broker-on-wire tag
                # (which includes CLIENT_TAG_PREFIX). Matchers parse
                # broker tags via parse_client_tag and compare
                # components; the stored form is prefix-free.
                client_tag = (
                    f"{strategy}:{parent_trade_id}{CLIENT_TAG_STOP_SUFFIX}"
                )
                result.append(
                    {
                        "ticker": pos.ticker,
                        "strategy": strategy,
                        "parent_trade_id": parent_trade_id,
                        "client_tag": client_tag,
                        "trigger_price": trigger,
                    }
                )
            # Whether or not we emitted, the fresh journal is the
            # authoritative view for this ticker. Do NOT fall through
            # to prior-checkpoint carry-forward.
            continue
        # Step 2: no fresh journal parent for this ticker. Carry
        # forward the prior checkpoint's entry (if any) so the
        # multi-day restart chain does not lose stop-child identity
        # after the parent records age out (Codex R1 P1). MVP one-
        # parent-per-ticker means at most one prior entry per ticker
        # should exist; if the prior has multiple, we still carry
        # only the one matching the newest-journaled trade_id (which
        # is None here since there's no fresh journal parent), which
        # collapses to "carry the first valid entry for this ticker".
        prior_entries = prior_by_ticker.get(pos.ticker, [])
        for prior in prior_entries:
            if any(not prior.get(k) for k in required_fields):
                continue
            # Shallow copy: decouple from journal_tail record identity.
            result.append({k: prior[k] for k in required_fields})
            break  # MVP: one entry per ticker
    return result


__all__ = [
    "ADOPT_ORPHAN_STOP_ENV",
    "DEFAULT_LOOKBACK",
    "OrphanStopAdoptionRequest",
    "PendingFromJournal",
    "PositionFromJournal",
    "RECOVERY_OVERRIDE_ENV",
    "ReconciliationEvent",
    "ReconciliationResult",
    "RecoveryStatus",
    "build_expected_stop_children",
    "reconcile",
]
