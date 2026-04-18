"""Risk-layer types: account state + breaker result.

Mirror shape of validators.types.ValidatorResult so downstream journal
records are uniform.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Any


@dataclass
class AccountState:
    current_value: Decimal
    day_open_value: Decimal
    peak_value: Decimal
    # List of (session_date, close_value) for up to the last 5 sessions.
    week_history: list[tuple[date, Decimal]] = field(default_factory=list)

    def intraday_drawdown_pct(self) -> Decimal:
        if self.day_open_value <= 0:
            return Decimal("0")
        return (self.current_value - self.day_open_value) / self.day_open_value

    def total_drawdown_pct(self) -> Decimal:
        if self.peak_value <= 0:
            return Decimal("0")
        return (self.current_value - self.peak_value) / self.peak_value

    def rolling_week_drawdown_pct(self) -> Decimal:
        # The weekly cap is documented as a 5-session drawdown. With
        # fewer than 5 sessions loaded (bot just started, first Monday,
        # etc.) a single bad day can otherwise trip the breaker
        # prematurely -- one prior close of 1M and a current of 940K
        # would read as -6%. Return 0 until we have the full window.
        if len(self.week_history) < 5:
            return Decimal("0")
        opens = [v for _, v in self.week_history]
        start = opens[0]
        if start <= 0:
            return Decimal("0")
        return (self.current_value - start) / start

    def has_full_week_window(self) -> bool:
        return len(self.week_history) >= 5


@dataclass
class BreakerResult:
    tripped: bool
    breaker: str
    severity: str  # "soft" | "hard" | "weekly" | "kill"
    action: str    # "halve_positions" | "flatten_all" | "reduce_budget" | "write_killed"
    detail: dict[str, Any] = field(default_factory=dict)

    def as_journal_payload(self) -> dict[str, Any]:
        return {
            "tripped": self.tripped,
            "breaker": self.breaker,
            "severity": self.severity,
            "action": self.action,
            "detail": self.detail,
        }
