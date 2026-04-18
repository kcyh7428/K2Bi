"""Tests for the canonical cash-only invariant helper.

The architect flag after Codex round 7: cash-only enforcement was
scattered across leverage/position_size/trade_risk, which is why rounds
1-3 each found a different naked-short path. Consolidation target:
`execution.risk.cash_only`. These tests pin the canonical helper's
contract so Bundle 2's engine main loop cannot regress it without a
failing test.
"""

from __future__ import annotations

import unittest
from datetime import datetime, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo

from execution.risk import cash_only
from execution.validators.types import Order, Position, RiskContext


ET = ZoneInfo("US/Eastern")


CASH_ONLY_CONFIG = {"leverage": {"cash_only": True, "max_leverage": 1.0}}
MARGIN_CONFIG = {"leverage": {"cash_only": False, "max_leverage": 1.0}}


def _at_market_open() -> datetime:
    local = datetime(2026, 4, 21, 10, 30, tzinfo=ET)
    return local.astimezone(timezone.utc)


def _order(**overrides) -> Order:
    defaults = dict(
        ticker="SPY",
        side="buy",
        qty=10,
        limit_price=Decimal("500"),
        stop_loss=Decimal("495"),
        strategy="spy-rotational",
        submitted_at=_at_market_open(),
        extended_hours=False,
    )
    defaults.update(overrides)
    return Order(**defaults)


def _ctx(**overrides) -> RiskContext:
    defaults = dict(
        account_value=Decimal("1000000"),
        cash=Decimal("1000000"),
        positions=[],
        pending_orders=[],
        now=_at_market_open(),
    )
    defaults.update(overrides)
    return RiskContext(**defaults)


class ModeTests(unittest.TestCase):
    def test_mode_from_config_reads_cash_only_flag(self):
        self.assertTrue(cash_only.mode_from_config(CASH_ONLY_CONFIG).cash_only)
        self.assertFalse(cash_only.mode_from_config(MARGIN_CONFIG).cash_only)


class AvailableLongQtyTests(unittest.TestCase):
    def test_filled_position_counted(self):
        pos = Position(ticker="SPY", qty=100, avg_price=Decimal("500"))
        self.assertEqual(cash_only.available_long_qty("SPY", _ctx(positions=[pos])), 100)

    def test_pending_sells_subtract(self):
        pos = Position(ticker="SPY", qty=100, avg_price=Decimal("500"))
        pending = _order(side="sell", qty=30)
        self.assertEqual(
            cash_only.available_long_qty(
                "SPY", _ctx(positions=[pos], pending_orders=[pending])
            ),
            70,
        )

    def test_pending_buys_do_not_add(self):
        pending_buy = _order(side="buy", qty=40)
        self.assertEqual(
            cash_only.available_long_qty("SPY", _ctx(pending_orders=[pending_buy])),
            0,
        )


class CheckSellCoveredTests(unittest.TestCase):
    def test_margin_mode_refuses_everything(self):
        r = cash_only.check_sell_covered(_order(), _ctx(), MARGIN_CONFIG)
        self.assertFalse(r.approved)
        self.assertEqual(r.reason, "margin_mode_not_supported_phase2")

    def test_buys_pass_through_in_cash_only(self):
        r = cash_only.check_sell_covered(_order(side="buy"), _ctx(), CASH_ONLY_CONFIG)
        self.assertTrue(r.approved)

    def test_naked_sell_rejected(self):
        r = cash_only.check_sell_covered(_order(side="sell", qty=10), _ctx(), CASH_ONLY_CONFIG)
        self.assertFalse(r.approved)
        self.assertEqual(r.reason, "naked_short_not_allowed")

    def test_covered_sell_approved(self):
        pos = Position(ticker="SPY", qty=10, avg_price=Decimal("500"))
        r = cash_only.check_sell_covered(
            _order(side="sell", qty=10), _ctx(positions=[pos]), CASH_ONLY_CONFIG
        )
        self.assertTrue(r.approved)

    def test_rule_kwarg_is_surfaced(self):
        # leverage validator calls with rule="leverage" so the journal
        # shows which validator caught the violation while the rule
        # itself is centrally defined here.
        r = cash_only.check_sell_covered(
            _order(side="sell", qty=10),
            _ctx(),
            CASH_ONLY_CONFIG,
            rule="leverage",
        )
        self.assertEqual(r.rule, "leverage")
        self.assertEqual(r.reason, "naked_short_not_allowed")


if __name__ == "__main__":
    unittest.main()
