# cash-only invariant: delegates to execution.risk.cash_only.check_sell_covered
# for every sell. Do not reintroduce a local naked-short check -- the helper is
# the single canonical enforcement point (architect 2026-04-18).
"""Leverage validator.

Phase 2 ships cash-only. Margin mode (cash_only=False) lands in Phase 4+
with its own design. For now the engine refuses any order while margin
mode is toggled: shipping a half-correct margin path is worse than no
margin path, since a leaky leverage branch would let shorts bypass
every cap.

Rules in cash-only mode:
    - Sells must not exceed (filled long qty - pending sell qty).
      Enforced via execution.risk.cash_only.check_sell_covered.
    - Buys must leave enough cash after already-reserved pending-buy
      notional. Reject `cash_only_insufficient_funds` otherwise.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from ..risk import cash_only
from .types import Order, RiskContext, ValidatorResult


RULE = "leverage"
REASON_CASH_INSUFFICIENT = "cash_only_insufficient_funds"
REASON_APPROVED = "leverage_ok"

# Re-export for callers that still import these reasons off the leverage
# validator (the canonical home is execution.risk.cash_only).
REASON_NAKED_SHORT = cash_only.REASON_NAKED_SHORT
REASON_MARGIN_NOT_SUPPORTED = cash_only.REASON_MARGIN_NOT_SUPPORTED


def check(order: Order, ctx: RiskContext, config: dict[str, Any]) -> ValidatorResult:
    # Cash-only invariant + margin-mode refusal both come from the
    # canonical helper. The helper returns an approved result for
    # non-sells (in cash-only mode), letting us fall through to the
    # cash-coverage check below.
    cash_only_result = cash_only.check_sell_covered(order, ctx, config, rule=RULE)
    if not cash_only_result.approved:
        return cash_only_result
    if order.side == "sell":
        return cash_only_result  # approved sell: covered by inventory

    pending_buy_notional = sum(
        (o.notional for o in ctx.pending_orders if o.side == "buy"),
        Decimal("0"),
    )
    required_cash = order.notional + pending_buy_notional
    detail = {
        "cash_only": True,
        "cash": str(ctx.cash),
        "pending_buy_notional": str(pending_buy_notional),
        "order_notional": str(order.notional),
        "required_cash": str(required_cash),
    }
    if required_cash > ctx.cash:
        return ValidatorResult(
            approved=False,
            rule=RULE,
            reason=REASON_CASH_INSUFFICIENT,
            detail=detail,
        )
    return ValidatorResult(approved=True, rule=RULE, reason=REASON_APPROVED, detail=detail)
