# cash-only invariant: sell-side orders short-circuit to approve here;
# naked-short enforcement is owned by execution.risk.cash_only (called
# via leverage validator). This module is not a sell-side enforcement
# point.
"""Per-ticker concentration cap.

Rule: (existing ticker notional + this order's notional) must not exceed
max_ticker_concentration_pct * account_value. Buys only add; sells net
against existing exposure.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from .types import Order, RiskContext, ValidatorResult


RULE = "position_size"
REASON_EXCEEDED = "position_size_exceeded"
REASON_APPROVED = "position_size_ok"


def _mark_for(ticker: str, ctx: RiskContext, fallback: Decimal) -> Decimal:
    """Current mark for a ticker, with a safe non-user-controlled fallback.

    The engine populates ctx.current_marks from an IBKR snapshot before
    each validator pass. When missing, fall back to the position's own
    avg_price (cost basis) -- imperfect, but not under the caller's
    control. Using order.limit_price as a mark for existing inventory
    lets a deep-below-market buy collapse the concentration calculation.
    """
    return ctx.current_marks.get(ticker, fallback)


def _current_exposure(ticker: str, ctx: RiskContext) -> Decimal:
    exposure = Decimal("0")
    for p in ctx.positions:
        if p.ticker == ticker:
            mark = _mark_for(ticker, ctx, p.avg_price)
            exposure += mark * Decimal(p.qty)
    # Pending buys use the order's own limit_price (it's the notional
    # the engine has already committed to).
    for o in ctx.pending_orders:
        if o.ticker == ticker and o.side == "buy":
            exposure += o.notional
    return exposure


def check(order: Order, ctx: RiskContext, config: dict[str, Any]) -> ValidatorResult:
    if order.side == "sell":
        return ValidatorResult(
            approved=True,
            rule=RULE,
            reason=REASON_APPROVED,
            detail={"note": "sell-side skipped by position_size"},
        )

    cap_pct = Decimal(str(config["position_size"]["max_ticker_concentration_pct"]))
    cap_value = cap_pct * ctx.account_value
    existing = _current_exposure(order.ticker, ctx)
    projected = existing + order.notional
    mark_source = "ctx.current_marks" if order.ticker in ctx.current_marks else "avg_price_fallback"

    detail = {
        "ticker": order.ticker,
        "account_value": str(ctx.account_value),
        "mark_source": mark_source,
        "current_mark": str(ctx.current_marks.get(order.ticker, "")),
        "existing_exposure": str(existing),
        "order_notional": str(order.notional),
        "projected_exposure": str(projected),
        "cap_pct": str(cap_pct),
        "cap_value": str(cap_value),
    }

    if projected > cap_value:
        return ValidatorResult(
            approved=False,
            rule=RULE,
            reason=REASON_EXCEEDED,
            detail=detail,
        )
    return ValidatorResult(approved=True, rule=RULE, reason=REASON_APPROVED, detail=detail)
