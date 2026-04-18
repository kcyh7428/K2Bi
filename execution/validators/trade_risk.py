# cash-only invariant: sell-side orders short-circuit to approve here;
# naked-short enforcement is owned by execution.risk.cash_only (called
# via leverage validator). This module is not a sell-side enforcement
# point.
"""Per-trade and portfolio-level risk caps.

Per-trade cap: abs(limit_price - stop_loss) * qty must not exceed
max_trade_risk_pct * account_value.

Portfolio cap: sum(open position open_risk + pending order trade_risk +
this order's trade_risk) must not exceed max_open_risk_pct *
account_value.

Rejection reason `missing_stop_loss` is distinct from `trade_risk_exceeded`
so the journal can distinguish "bad strategy math" from "strategy
forgot a stop".
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from .types import Order, RiskContext, ValidatorResult


RULE = "trade_risk"
REASON_EXCEEDED = "trade_risk_exceeded"
REASON_OPEN_RISK = "open_risk_exceeded"
REASON_MISSING_STOP = "missing_stop_loss"
REASON_APPROVED = "trade_risk_ok"


def check(order: Order, ctx: RiskContext, config: dict[str, Any]) -> ValidatorResult:
    if order.side == "sell":
        return ValidatorResult(
            approved=True,
            rule=RULE,
            reason=REASON_APPROVED,
            detail={"note": "sell-side skipped by trade_risk"},
        )

    if order.stop_loss is None:
        return ValidatorResult(
            approved=False,
            rule=RULE,
            reason=REASON_MISSING_STOP,
            detail={"ticker": order.ticker, "strategy": order.strategy},
        )

    per_trade_pct = Decimal(str(config["position_size"]["max_trade_risk_pct"]))
    open_risk_pct = Decimal(str(config["trade_risk"]["max_open_risk_pct"]))
    per_trade_cap = per_trade_pct * ctx.account_value
    open_risk_cap = open_risk_pct * ctx.account_value

    trade_risk = order.trade_risk
    aggregated_risk = trade_risk
    for p in ctx.positions:
        aggregated_risk += p.open_risk
    # Only count pending BUYS as new open risk. In cash-only mode a
    # pending sell is an exit -- it reduces, not adds to, open risk.
    # Counting sells here causes open_risk_exceeded to fire during
    # normal de-risking flows (position with pending sell-to-close).
    for o in ctx.pending_orders:
        if o.side == "buy":
            aggregated_risk += o.trade_risk

    detail = {
        "ticker": order.ticker,
        "account_value": str(ctx.account_value),
        "trade_risk": str(trade_risk),
        "per_trade_cap": str(per_trade_cap),
        "aggregated_open_risk": str(aggregated_risk),
        "open_risk_cap": str(open_risk_cap),
    }

    if trade_risk > per_trade_cap:
        return ValidatorResult(
            approved=False,
            rule=RULE,
            reason=REASON_EXCEEDED,
            detail=detail,
        )
    if aggregated_risk > open_risk_cap:
        return ValidatorResult(
            approved=False,
            rule=RULE,
            reason=REASON_OPEN_RISK,
            detail=detail,
        )
    return ValidatorResult(approved=True, rule=RULE, reason=REASON_APPROVED, detail=detail)
