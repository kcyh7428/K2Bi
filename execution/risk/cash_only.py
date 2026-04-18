"""Canonical cash-only invariant helpers.

The cash-only mode is K2Bi's Phase 2 MVP posture: no margin, no shorts.
Rounds 1/2/3 of Codex review on Bundle 1 each surfaced a different
naked-short scenario scattered across validators, so this module owns
the invariant in ONE place. Every order path must call through
`check_sell_covered()` rather than re-implementing the rule -- that's
the load-bearing architectural decision.

When Bundle 2 ships the engine main loop (m2.6), it imports from here
directly rather than poking at individual validators. The runner's
leverage validator also defers to this module for its sell branch, so
the same rule runs regardless of whether the caller came through
`run_all()` or the engine's own pre-submit hook.

Cash-only invariant (strict):
    For every ticker T: outstanding_sells(T) + new_sell(T) <= filled_long_qty(T)
    where:
      - outstanding_sells = sum of pending sell qty for T
      - filled_long_qty   = sum of open long position qty for T
      - pending BUYS are NOT counted (they may cancel or fail to fill)
    Violating this opens a short. No override.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..validators.types import Order, RiskContext, ValidatorResult


RULE = "cash_only"
REASON_NAKED_SHORT = "naked_short_not_allowed"
REASON_MARGIN_NOT_SUPPORTED = "margin_mode_not_supported_phase2"
REASON_APPROVED = "cash_only_ok"


@dataclass(frozen=True)
class ModeDecision:
    cash_only: bool
    reason: str


def mode_from_config(config: dict[str, Any]) -> ModeDecision:
    """Resolve the active leverage mode from the validator config."""
    cash_only = bool(config.get("leverage", {}).get("cash_only", True))
    return ModeDecision(
        cash_only=cash_only,
        reason="cash_only" if cash_only else "margin",
    )


def available_long_qty(ticker: str, ctx: RiskContext) -> int:
    """Filled long inventory minus reserved pending sells.

    Callers: leverage validator (sell branch), engine main loop pre-
    submit hook, propose-limits skill when projecting what a validator
    change would allow.
    """
    long_qty = 0
    for p in ctx.positions:
        if p.ticker == ticker:
            long_qty += p.qty
    for o in ctx.pending_orders:
        if o.ticker == ticker and o.side == "sell":
            long_qty -= o.qty
    return long_qty


def check_sell_covered(
    order: Order,
    ctx: RiskContext,
    config: dict[str, Any],
    *,
    rule: str = RULE,
) -> ValidatorResult:
    """Reject any sell that would open (or grow) a short position.

    Returns an approved ValidatorResult for non-sell orders so callers
    can uniformly chain this into existing pipelines without branching.
    The `rule` kwarg lets the leverage validator surface its own rule
    name in the result while the semantics stay shared.
    """
    mode = mode_from_config(config)
    if not mode.cash_only:
        return ValidatorResult(
            approved=False,
            rule=rule,
            reason=REASON_MARGIN_NOT_SUPPORTED,
            detail={
                "cash_only": False,
                "phase": "Phase 2 MVP is cash-only; margin lands in Phase 4+",
            },
        )

    if order.side != "sell":
        return ValidatorResult(
            approved=True,
            rule=rule,
            reason=REASON_APPROVED,
            detail={"note": "non-sell skipped by cash_only.check_sell_covered"},
        )

    available = available_long_qty(order.ticker, ctx)
    detail = {
        "cash_only": True,
        "ticker": order.ticker,
        "available_long_qty": available,
        "sell_qty": order.qty,
    }
    if order.qty > available:
        return ValidatorResult(
            approved=False,
            rule=rule,
            reason=REASON_NAKED_SHORT,
            detail=detail,
        )
    return ValidatorResult(approved=True, rule=rule, reason=REASON_APPROVED, detail=detail)
