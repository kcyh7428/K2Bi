# cash-only invariant: no sell-side paths in this module (ticker
# membership, not position-based). Enforcement owned by
# execution.risk.cash_only via leverage validator.
"""Instrument whitelist validator.

Ticker must appear in config.instrument_whitelist.symbols. Empty list
means the engine refuses everything -- starting Phase 2 config has only
SPY (proof-of-pipeline); additional tickers land via
invest-propose-limits + /invest-ship.
"""

from __future__ import annotations

from typing import Any

from .types import Order, RiskContext, ValidatorResult


RULE = "instrument_whitelist"
REASON_NOT_WHITELISTED = "ticker_not_whitelisted"
REASON_APPROVED = "instrument_whitelist_ok"


def check(order: Order, ctx: RiskContext, config: dict[str, Any]) -> ValidatorResult:
    symbols = list(config.get("instrument_whitelist", {}).get("symbols", []))
    detail = {"ticker": order.ticker, "whitelist": symbols}
    if order.ticker in symbols:
        return ValidatorResult(approved=True, rule=RULE, reason=REASON_APPROVED, detail=detail)
    return ValidatorResult(
        approved=False,
        rule=RULE,
        reason=REASON_NOT_WHITELISTED,
        detail=detail,
    )
