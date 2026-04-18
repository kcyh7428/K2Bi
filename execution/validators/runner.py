# cash-only invariant: runs leverage validator last, which delegates to
# execution.risk.cash_only.check_sell_covered for every sell. The runner
# itself does not encode the invariant -- it is the composition point
# only. When adding a new validator that touches sell-side, that
# validator must also import cash_only (NOT re-implement the rule).
"""Validator runner.

Runs the top 5 validators in sequence. Short-circuits on first rejection
so the journal records exactly one rejection reason per order. Passing
orders get one ValidatorResult per validator (all approved=True) so the
journal has full approval audit trail.

Per risk-controls.md: NO override flag. Runner does not accept --force.
"""

from __future__ import annotations

from typing import Any, Iterable

from . import (
    instrument_whitelist,
    leverage,
    market_hours,
    position_size,
    trade_risk,
)
from .types import Order, RiskContext, ValidatorResult


VALIDATORS = (
    instrument_whitelist,
    market_hours,
    position_size,
    trade_risk,
    leverage,
)


def run_all(
    order: Order,
    ctx: RiskContext,
    config: dict[str, Any],
) -> tuple[bool, list[ValidatorResult]]:
    results: list[ValidatorResult] = []
    for mod in VALIDATORS:
        res = mod.check(order, ctx, config)
        results.append(res)
        if not res.approved:
            return False, results
    return True, results


def as_journal_payload(results: Iterable[ValidatorResult]) -> list[dict[str, Any]]:
    return [r.as_journal_payload() for r in results]
