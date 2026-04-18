# cash-only invariant: data types only; no enforcement. Sell-side gates
# live in execution.risk.cash_only (called by leverage validator).
"""Normalized order + risk context + validator result types.

Shared across every pre-trade validator. Defined once here so the engine
main loop (m2.6) and tests produce comparable shapes.

All monetary quantities use Decimal (not float) to avoid rounding drift
on repeated risk-percent calculations.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any


Side = str  # "buy" or "sell"


@dataclass
class Order:
    ticker: str
    side: Side
    qty: int
    limit_price: Decimal
    stop_loss: Decimal | None
    strategy: str
    submitted_at: datetime
    extended_hours: bool = False

    @property
    def notional(self) -> Decimal:
        return self.limit_price * Decimal(self.qty)

    @property
    def per_share_risk(self) -> Decimal:
        if self.stop_loss is None:
            return Decimal("0")
        if self.side == "buy":
            return max(Decimal("0"), self.limit_price - self.stop_loss)
        return max(Decimal("0"), self.stop_loss - self.limit_price)

    @property
    def trade_risk(self) -> Decimal:
        return self.per_share_risk * Decimal(self.qty)


@dataclass
class Position:
    ticker: str
    qty: int
    avg_price: Decimal
    stop_loss: Decimal | None = None

    @property
    def notional(self) -> Decimal:
        return self.avg_price * Decimal(self.qty)

    @property
    def open_risk(self) -> Decimal:
        if self.stop_loss is None:
            return Decimal("0")
        per_share = max(Decimal("0"), self.avg_price - self.stop_loss)
        return per_share * Decimal(self.qty)


@dataclass
class RiskContext:
    account_value: Decimal
    cash: Decimal
    positions: list[Position] = field(default_factory=list)
    pending_orders: list[Order] = field(default_factory=list)
    now: datetime | None = None
    # Per-ticker current market marks (bid/mid/last) fed by the engine
    # from an IBKR snapshot before every validator pass. Validators that
    # need to compute current exposure (position_size concentration cap)
    # read from here. If a ticker is missing, validators must fall back
    # to a safe non-user-controlled value (typically position.avg_price);
    # never fall back to the caller's limit_price -- that lets a deep-
    # below-market order collapse existing exposure.
    current_marks: dict[str, Decimal] = field(default_factory=dict)


@dataclass
class ValidatorResult:
    approved: bool
    rule: str
    reason: str
    detail: dict[str, Any] = field(default_factory=dict)

    def as_journal_payload(self) -> dict[str, Any]:
        return {
            "approved": self.approved,
            "rule": self.rule,
            "reason": self.reason,
            "detail": self.detail,
        }
