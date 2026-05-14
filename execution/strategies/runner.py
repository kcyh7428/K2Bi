"""Pure strategy evaluation.

Takes ApprovedStrategySnapshot + MarketSnapshot + engine context,
returns a CandidateOrder or None. The normal evaluation path contains
NO I/O, NO connector calls, NO validator invocation -- the engine's tick
owns those. Spec B §8.3 exposes a helper the engine can call after a
position-held skip decision to journal observability. The recovery-only
protective-stop repair verb at the bottom is an explicit Spec B §4
exception guarded by a private recovery token.

Why this lives separately from the engine (architect Q1-refined):
    - Bundle 4's invest-backtest reuses `evaluate()` against historical
      bars, so strategy logic cannot live inside the engine loop.
    - Unit-testing pure evaluation is trivial without spinning asyncio
      or mocking a connector -- a pure function over data classes.

cash_only invariant: sell-side orders are routed through
`execution.risk.cash_only.check_sell_covered` so the runner never
emits a sell that would become a naked short. The engine's validator
run on the returned CandidateOrder does the same check again -- the
runner's pre-check is a fast-path optimization, not the authoritative
gate.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

from ..engine.recovery_context import is_recovery_context_token
from ..journal.schema import (
    JournalReplayMalformedJsonError,
    validate_cycle_evaluated_skip_position_held_payload,
    validate_protective_stop_attached_payload,
    validate_protective_stop_attach_refused_drift_payload,
    validate_protective_stop_attach_refused_no_context_payload,
)
from ..journal.writer import JournalWriter
from ..risk import cash_only
from ..validators.types import Order as ValidatorOrder
from ..validators.types import Position as ValidatorPosition
from ..validators.types import RiskContext
from .types import (
    ApprovedStrategySnapshot,
    CandidateOrder,
    MarketSnapshot,
    STRATEGY_TYPE_HAND_CRAFTED,
)


@dataclass(frozen=True)
class EvaluationDecision:
    """Why the runner did or did not emit an order.

    Engine writes the `reason` into the journal on both paths so
    post-mortems can ask "why didn't the engine fire on 2026-05-03?"
    and get a one-line answer.
    """

    candidate: CandidateOrder | None
    reason: str
    detail: dict[str, Any]


SKIP_POSITION_HELD = "position_already_open_for_ticker"
SKIP_PENDING_ORDER = "pending_order_for_strategy"
SKIP_REGIME_MISMATCH = "regime_filter_mismatch"
SKIP_NAKED_SHORT = "would_open_naked_short"
SKIP_UNKNOWN_STRATEGY_TYPE = "unknown_strategy_type"
EMIT_HAND_CRAFTED = "hand_crafted_order_emitted"


class RecoveryContextError(PermissionError):
    """Raised when recovery-only broker repair is called without authority."""


class PositionDriftError(RuntimeError):
    """Raised when broker position qty differs from the requested stop qty."""


def evaluate(
    snapshot: ApprovedStrategySnapshot,
    market: MarketSnapshot,
    ctx: RiskContext,
    *,
    current_regime: str | None = None,
    cash_only_config: dict[str, Any] | None = None,
) -> EvaluationDecision:
    """Single-strategy evaluation entrypoint.

    Phase 2 MVP supports `hand_crafted` only. Phase 3+ introduces
    `rule_based` strategies; this dispatcher grows a new arm when that
    lands. Unknown strategy types return a `skip` decision, never an
    exception -- the engine journals the skip and continues.
    """
    if snapshot.strategy_type != STRATEGY_TYPE_HAND_CRAFTED:
        return EvaluationDecision(
            candidate=None,
            reason=SKIP_UNKNOWN_STRATEGY_TYPE,
            detail={"strategy_type": snapshot.strategy_type},
        )

    # Regime filter: engine caller passes the active regime; the
    # strategy's declared regime_filter is the AND set. Codex round-12
    # P1: a strategy that declares a regime_filter MUST NOT trade when
    # the regime is unknown (failure to pass a regime from engine /
    # regime skill not yet running). Block rather than silently bypass.
    if snapshot.regime_filter:
        if current_regime is None:
            return EvaluationDecision(
                candidate=None,
                reason=SKIP_REGIME_MISMATCH,
                detail={
                    "current_regime": None,
                    "required": list(snapshot.regime_filter),
                    "note": (
                        "regime_filter set on strategy but current "
                        "regime unknown; strategy blocked until regime "
                        "skill publishes wiki/regimes/current.md"
                    ),
                },
            )
        if current_regime not in snapshot.regime_filter:
            return EvaluationDecision(
                candidate=None,
                reason=SKIP_REGIME_MISMATCH,
                detail={
                    "current_regime": current_regime,
                    "required": list(snapshot.regime_filter),
                },
            )

    spec = snapshot.order_spec
    ticker = spec.ticker

    # Suppress duplicate orders: if the strategy already has an open
    # position in its ticker OR a pending order, skip. Hand_crafted
    # strategies are single-shot per "engine-lifetime or human reset";
    # the runner never stacks.
    current_qty = _position_qty(ticker, ctx)
    if current_qty != 0:
        return EvaluationDecision(
            candidate=None,
            reason=SKIP_POSITION_HELD,
            detail={
                "ticker": ticker,
                "current_qty": current_qty,
                "target_qty": spec.qty,
            },
        )
    if _any_pending_order_for_strategy(snapshot.name, ctx):
        return EvaluationDecision(
            candidate=None,
            reason=SKIP_PENDING_ORDER,
            detail={"strategy": snapshot.name},
        )

    # Pre-emit cash-only fast path for sell side. Engine re-runs the
    # full validator cascade (including cash_only via leverage) once
    # the CandidateOrder lands, so this is a short-circuit, not the
    # authoritative gate.
    if spec.side == "sell":
        pre_order = _to_validator_order(snapshot, market)
        pre_result = cash_only.check_sell_covered(
            pre_order, ctx, cash_only_config or {"leverage": {"cash_only": True}}
        )
        if not pre_result.approved:
            return EvaluationDecision(
                candidate=None,
                reason=SKIP_NAKED_SHORT,
                detail=pre_result.detail,
            )

    candidate = CandidateOrder(
        strategy=snapshot.name,
        ticker=ticker,
        side=spec.side,
        qty=spec.qty,
        limit_price=spec.limit_price,
        stop_loss=spec.stop_loss,
        time_in_force=spec.time_in_force,
        reason=EMIT_HAND_CRAFTED,
        order_type=spec.order_type,
    )
    return EvaluationDecision(
        candidate=candidate,
        reason=EMIT_HAND_CRAFTED,
        detail={
            "ticker": ticker,
            "side": spec.side,
            "qty": spec.qty,
            "strategy_type": snapshot.strategy_type,
        },
    )


def _position_qty(ticker: str, ctx: RiskContext) -> int:
    target = ticker.upper()
    return sum(p.qty for p in ctx.positions if p.ticker.upper() == target)


def journal_cycle_evaluated_skip_position_held(
    *,
    journal: JournalWriter,
    snapshot: ApprovedStrategySnapshot,
    ctx: RiskContext,
    market: MarketSnapshot,
    current_qty: int,
    cycle_id: str,
    position_source: str,
    position_age_seconds: float,
    position_visibility_valid: bool,
) -> None:
    spec = snapshot.order_spec
    symbol = spec.ticker.upper()
    evaluation_time = ctx.now or market.ts
    payload = {
        "strategy_id": snapshot.name,
        "symbol": symbol,
        "current_qty": current_qty,
        "target_qty": spec.qty,
        "cycle_id": cycle_id,
        "evaluation_timestamp": evaluation_time.isoformat(),
        "position_source": position_source,
        "position_age_seconds": position_age_seconds,
        "position_visibility_valid": position_visibility_valid,
    }
    validate_cycle_evaluated_skip_position_held_payload(payload)
    journal.append(
        "cycle_evaluated_skip_position_held",
        payload=payload,
        strategy=snapshot.name,
        trade_id=None,
        ticker=symbol,
        side=spec.side,
        qty=spec.qty,
    )


def _any_pending_order_for_strategy(name: str, ctx: RiskContext) -> bool:
    return any(o.strategy == name for o in ctx.pending_orders)


TERMINAL_JOURNAL_STATUSES = frozenset(
    {"Filled", "Cancelled", "ApiCancelled", "Rejected", "Inactive"}
)


def pending_order_map_from_journal(
    journal_records: Iterable[dict[str, Any]],
) -> dict[tuple[str, str], set[str]]:
    """Build pending broker order ids keyed by strategy and symbol."""
    pending: dict[tuple[str, str], set[str]] = {}
    order_keys: dict[str, tuple[str, str]] = {}
    order_qty: dict[str, int] = {}
    for record in journal_records:
        event_type = record.get("event_type")
        broker_order_id = record.get("broker_order_id")
        if not broker_order_id:
            payload = record.get("payload")
            if isinstance(payload, dict):
                broker_order_id = payload.get("broker_order_id")

        if event_type == "order_submitted":
            if not broker_order_id:
                raise JournalReplayMalformedJsonError(
                    "order_submitted missing broker_order_id"
                )
            strategy_id = record.get("strategy")
            symbol = str(record.get("ticker") or "").upper()
            if not isinstance(strategy_id, str) or not strategy_id or not symbol:
                raise JournalReplayMalformedJsonError(
                    "order_submitted missing strategy or ticker"
                )
            order_id = str(broker_order_id)
            key = (strategy_id, symbol)
            pending.setdefault(key, set()).add(order_id)
            order_keys[order_id] = key
            qty = record.get("qty")
            if not isinstance(qty, int) or isinstance(qty, bool):
                raise JournalReplayMalformedJsonError(
                    "order_submitted missing or invalid qty"
                )
            order_qty[order_id] = qty
            continue

        if event_type == "order_terminal":
            if not broker_order_id:
                raise JournalReplayMalformedJsonError(
                    "order_terminal missing broker_order_id"
                )
            payload = record.get("payload")
            terminal_status = (
                payload.get("terminal_status") if isinstance(payload, dict) else None
            )
            if terminal_status not in TERMINAL_JOURNAL_STATUSES:
                raise JournalReplayMalformedJsonError(
                    f"order_terminal unknown terminal_status: {terminal_status!r}"
                )
            order_id = str(broker_order_id)
            key = order_keys.pop(order_id, None)
            order_qty.pop(order_id, None)
            if key is not None:
                pending.get(key, set()).discard(order_id)
            continue

        if event_type in {"order_rejected", "order_timeout"}:
            if not broker_order_id:
                continue
            order_id = str(broker_order_id)
            key = order_keys.pop(order_id, None)
            order_qty.pop(order_id, None)
            if key is not None:
                pending.get(key, set()).discard(order_id)
            continue

        if event_type == "order_filled":
            if not broker_order_id:
                raise JournalReplayMalformedJsonError(
                    "order_filled missing broker_order_id"
                )
            payload = record.get("payload")
            remaining_qty = (
                payload.get("remaining_qty") if isinstance(payload, dict) else None
            )
            cumulative_filled_qty = (
                payload.get("cumulative_filled_qty")
                if isinstance(payload, dict)
                else None
            )
            if not (
                _quantity_is_zero(remaining_qty)
                or (
                    remaining_qty is None
                    and _quantity_at_least(
                        cumulative_filled_qty,
                        order_qty.get(str(broker_order_id)),
                    )
                )
            ):
                continue
            order_id = str(broker_order_id)
            key = order_keys.pop(order_id, None)
            order_qty.pop(order_id, None)
            if key is not None:
                pending.get(key, set()).discard(order_id)

    return {key: ids for key, ids in pending.items() if ids}


def _quantity_is_zero(value: Any) -> bool:
    if value is None or isinstance(value, bool):
        return False
    try:
        return Decimal(str(value).strip()) == 0
    except (InvalidOperation, ValueError):
        return False


def _quantity_at_least(value: Any, target: int | None) -> bool:
    if target is None or value is None or isinstance(value, bool):
        return False
    try:
        return Decimal(str(value).strip()) >= Decimal(target)
    except (InvalidOperation, ValueError):
        return False


def _pending_orders_for_strategy(
    strategy_id: str,
    symbol: str,
    journal_records: Iterable[dict[str, Any]],
) -> list[str]:
    """Return non-terminal broker order ids for one strategy/symbol."""
    target_symbol = symbol.upper()
    pending = pending_order_map_from_journal(journal_records)
    return sorted(pending.get((strategy_id, target_symbol), set()))


async def attach_protective_stop_to_existing_position(
    *,
    connector: Any,
    journal: JournalWriter,
    symbol: str,
    qty: int,
    stop_price: Decimal,
    strategy_id: str,
    recovery_context: object,
) -> Any:
    """Attach a standalone recovery STP to an already-held position.

    Normal strategy evaluation must never call this. The private recovery
    token makes the recovery/operator path explicit and prevents boolean
    flag drift from opening a standalone-stop path in the normal cycle.
    """

    symbol_norm = symbol.upper()
    if not is_recovery_context_token(recovery_context):
        payload = {
            "strategy_id": strategy_id,
            "symbol": symbol_norm,
            "qty": qty,
            "stop_price": str(stop_price),
            "reason": "missing_or_invalid_recovery_context",
        }
        validate_protective_stop_attach_refused_no_context_payload(payload)
        journal.append(
            "protective_stop_attach_refused_no_recovery_context",
            payload=payload,
            strategy=strategy_id,
            ticker=symbol_norm,
            side="sell",
            qty=qty,
        )
        raise RecoveryContextError(
            "attach_protective_stop_to_existing_position requires recovery context"
        )

    if qty <= 0:
        payload = {
            "strategy_id": strategy_id,
            "symbol": symbol_norm,
            "expected_qty": qty,
            "actual_qty": "0",
            "matching_position_count": 0,
            "stop_price": str(stop_price),
        }
        validate_protective_stop_attach_refused_drift_payload(payload)
        journal.append(
            "protective_stop_attach_refused_drift",
            payload=payload,
            strategy=strategy_id,
            ticker=symbol_norm,
            side="sell",
            qty=qty,
        )
        raise PositionDriftError(
            f"recovery stop qty for {symbol_norm} must be positive, got {qty}"
        )

    snapshot = await connector.get_positions()
    if not snapshot.valid:
        raise PositionDriftError(
            f"broker position visibility invalid for {symbol_norm}: {snapshot.source}"
        )
    positions = snapshot.positions
    matching_position_qtys = [
        Decimal(str(position.qty))
        for position in positions
        if str(position.ticker).upper() == symbol_norm
        and Decimal(str(position.qty)) != 0
    ]
    actual_qty = sum(matching_position_qtys, Decimal("0"))
    expected_qty = Decimal(qty)
    if len(matching_position_qtys) != 1 or actual_qty != expected_qty or actual_qty <= 0:
        payload = {
            "strategy_id": strategy_id,
            "symbol": symbol_norm,
            "expected_qty": qty,
            "actual_qty": format(actual_qty, "f"),
            "matching_position_count": len(matching_position_qtys),
            "stop_price": str(stop_price),
        }
        validate_protective_stop_attach_refused_drift_payload(payload)
        journal.append(
            "protective_stop_attach_refused_drift",
            payload=payload,
            strategy=strategy_id,
            ticker=symbol_norm,
            side="sell",
            qty=qty,
        )
        raise PositionDriftError(
            f"broker position drift for {symbol_norm}: expected one position "
            f"with qty {qty}, got {len(matching_position_qtys)} matching positions "
            f"totaling {actual_qty}"
        )

    ack = await connector.submit_standalone_stop_order(
        ticker=symbol_norm,
        side="sell",
        qty=qty,
        stop_price=stop_price,
        time_in_force="GTC",
        client_tag=f"k2bi:{strategy_id}:recovery-stop-{symbol_norm}:stop",
    )
    payload = {
        "strategy_id": strategy_id,
        "symbol": symbol_norm,
        "qty": qty,
        "stop_price": str(stop_price),
        "broker_order_id": ack.broker_order_id,
        "broker_perm_id": ack.broker_perm_id,
    }
    validate_protective_stop_attached_payload(payload)
    journal.append(
        "protective_stop_attached_to_existing_position",
        payload=payload,
        strategy=strategy_id,
        ticker=symbol_norm,
        side="sell",
        qty=qty,
        broker_order_id=ack.broker_order_id,
        broker_perm_id=ack.broker_perm_id,
    )
    return ack


def _to_validator_order(
    snapshot: ApprovedStrategySnapshot,
    market: MarketSnapshot,
) -> ValidatorOrder:
    """Build a transient ValidatorOrder for the runner's pre-emit
    cash-only fast path (sell-side only).

    For LMT orders, ``spec.limit_price`` is the authoritative reference.
    For MKT orders ``spec.limit_price`` may be None; we resolve a
    reference price from ``market.marks[ticker]`` so the validator's
    ``notional`` / ``per_share_risk`` math has a Decimal anchor.

    If a MKT order has no mark in this snapshot, the runner cannot
    evaluate the cash-only fast path safely. We return a synthetic
    Order with ``limit_price=Decimal('0')`` -- the cash-only check
    treats zero-notional sells as not-approved (notional > cash for
    the symbol), which fail-closes consistently with the engine-side
    behaviour at main._to_validator_order. The engine's authoritative
    pass downstream re-runs the full cascade with a re-pulled mark
    snapshot and journals the rejection cleanly.
    """
    spec = snapshot.order_spec
    limit_price = spec.limit_price
    if limit_price is None:
        # MKT/null path: try mark, else fail-closed sentinel (Decimal 0).
        limit_price = market.marks.get(spec.ticker, Decimal("0"))
    return ValidatorOrder(
        ticker=spec.ticker,
        side=spec.side,
        qty=spec.qty,
        limit_price=limit_price,
        stop_loss=spec.stop_loss,
        strategy=snapshot.name,
        submitted_at=market.ts,
        extended_hours=False,
        order_type=spec.order_type,
    )


__all__ = [
    "EMIT_HAND_CRAFTED",
    "EvaluationDecision",
    "PositionDriftError",
    "RecoveryContextError",
    "SKIP_NAKED_SHORT",
    "SKIP_PENDING_ORDER",
    "SKIP_POSITION_HELD",
    "SKIP_REGIME_MISMATCH",
    "SKIP_UNKNOWN_STRATEGY_TYPE",
    "attach_protective_stop_to_existing_position",
    "evaluate",
    "journal_cycle_evaluated_skip_position_held",
    "pending_order_map_from_journal",
    "_pending_orders_for_strategy",
]
