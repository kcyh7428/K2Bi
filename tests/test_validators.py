"""Unit tests for the top-5 pre-trade validators.

Each validator gets one rejecting order + one approving order, matching
the m2.3 verification criterion: "Each validator rejects a violating
order with named reason; passing order returns approved".
"""

from __future__ import annotations

import unittest
from datetime import datetime, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo

from execution.validators import (
    instrument_whitelist,
    leverage,
    market_hours,
    position_size,
    trade_risk,
)
from execution.validators.runner import run_all
from execution.validators.types import Order, Position, RiskContext


ET = ZoneInfo("US/Eastern")


CONFIG = {
    "position_size": {
        "max_trade_risk_pct": 0.01,
        "max_ticker_concentration_pct": 0.20,
    },
    "trade_risk": {"max_open_risk_pct": 0.05},
    "leverage": {"cash_only": True, "max_leverage": 1.0},
    "market_hours": {
        "regular_open": "09:30",
        "regular_close": "16:00",
        "allow_pre_market": False,
        "allow_after_hours": False,
    },
    "instrument_whitelist": {"symbols": ["SPY"]},
}


def _market_open_utc() -> datetime:
    # 10:30 ET on a Tuesday that was during regular session in 2026
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
        submitted_at=_market_open_utc(),
        extended_hours=False,
    )
    defaults.update(overrides)
    return Order(**defaults)


def _ctx(**overrides) -> RiskContext:
    # now=None by default so tests that set order.submitted_at exercise
    # the fallback path. Tests that specifically exercise ctx.now
    # precedence pass now= explicitly.
    defaults = dict(
        account_value=Decimal("1000000"),
        cash=Decimal("1000000"),
        positions=[],
        pending_orders=[],
        now=None,
    )
    defaults.update(overrides)
    return RiskContext(**defaults)


class InstrumentWhitelistTests(unittest.TestCase):
    def test_approves_whitelisted(self):
        r = instrument_whitelist.check(_order(), _ctx(), CONFIG)
        self.assertTrue(r.approved)
        self.assertEqual(r.rule, "instrument_whitelist")

    def test_rejects_unknown_ticker(self):
        r = instrument_whitelist.check(_order(ticker="TSLA"), _ctx(), CONFIG)
        self.assertFalse(r.approved)
        self.assertEqual(r.reason, "ticker_not_whitelisted")


class MarketHoursTests(unittest.TestCase):
    def test_approves_within_regular_session(self):
        r = market_hours.check(_order(), _ctx(), CONFIG)
        self.assertTrue(r.approved)

    def test_rejects_weekend(self):
        sunday_noon = datetime(2026, 4, 19, 16, 0, tzinfo=timezone.utc)
        r = market_hours.check(_order(submitted_at=sunday_noon), _ctx(), CONFIG)
        self.assertFalse(r.approved)
        self.assertEqual(r.reason, "weekend_blocked")

    def test_rejects_pre_market_without_flag(self):
        pre_market = datetime(2026, 4, 21, 8, 0, tzinfo=ET).astimezone(timezone.utc)
        r = market_hours.check(
            _order(submitted_at=pre_market, extended_hours=False),
            _ctx(),
            CONFIG,
        )
        self.assertFalse(r.approved)
        self.assertEqual(r.reason, "pre_market_not_allowed")

    def test_rejects_after_hours_without_flag(self):
        after = datetime(2026, 4, 21, 17, 0, tzinfo=ET).astimezone(timezone.utc)
        r = market_hours.check(
            _order(submitted_at=after, extended_hours=True),
            _ctx(),
            CONFIG,
        )
        self.assertFalse(r.approved)
        self.assertEqual(r.reason, "after_hours_not_allowed")

    def test_naive_datetime_raises(self):
        naive = datetime(2026, 4, 21, 14, 30)
        with self.assertRaises(ValueError):
            market_hours.check(_order(submitted_at=naive), _ctx(), CONFIG)

    def test_extended_hours_flag_does_not_unlock_overnight(self):
        # Codex round 4 P1: extended_hours=True must not approve 02:00 ET
        # even in a config that enables pre/after flags; that time is
        # outside any actual session window.
        cfg = dict(CONFIG)
        cfg["market_hours"] = {
            **CONFIG["market_hours"],
            "allow_pre_market": True,
            "allow_after_hours": True,
        }
        overnight = datetime(2026, 4, 21, 2, 0, tzinfo=ET).astimezone(timezone.utc)
        r = market_hours.check(
            _order(submitted_at=overnight, extended_hours=True),
            _ctx(),
            cfg,
        )
        self.assertFalse(r.approved)
        self.assertEqual(r.reason, "pre_market_not_allowed")

    def test_extended_hours_flag_does_not_unlock_late_night(self):
        cfg = dict(CONFIG)
        cfg["market_hours"] = {
            **CONFIG["market_hours"],
            "allow_pre_market": True,
            "allow_after_hours": True,
        }
        late = datetime(2026, 4, 21, 23, 0, tzinfo=ET).astimezone(timezone.utc)
        r = market_hours.check(
            _order(submitted_at=late, extended_hours=True),
            _ctx(),
            cfg,
        )
        self.assertFalse(r.approved)
        self.assertEqual(r.reason, "after_hours_not_allowed")

    def test_extended_hours_approves_real_pre_market(self):
        cfg = dict(CONFIG)
        cfg["market_hours"] = {**CONFIG["market_hours"], "allow_pre_market": True}
        pre = datetime(2026, 4, 21, 7, 0, tzinfo=ET).astimezone(timezone.utc)
        r = market_hours.check(
            _order(submitted_at=pre, extended_hours=True),
            _ctx(),
            cfg,
        )
        self.assertTrue(r.approved)

    def test_extended_hours_approves_real_after_hours(self):
        cfg = dict(CONFIG)
        cfg["market_hours"] = {**CONFIG["market_hours"], "allow_after_hours": True}
        after = datetime(2026, 4, 21, 18, 0, tzinfo=ET).astimezone(timezone.utc)
        r = market_hours.check(
            _order(submitted_at=after, extended_hours=True),
            _ctx(),
            cfg,
        )
        self.assertTrue(r.approved)

    def test_rejects_nyse_holiday_during_regular_session(self):
        # Codex round 7 P2: Dec 25 (Christmas) during regular session
        # hours must reject, not approve.
        christmas_noon = datetime(2026, 12, 25, 12, 0, tzinfo=ET).astimezone(timezone.utc)
        r = market_hours.check(_order(submitted_at=christmas_noon), _ctx(), CONFIG)
        self.assertFalse(r.approved)
        self.assertEqual(r.reason, "nyse_holiday_blocked")

    def test_rejects_observed_independence_day(self):
        # July 3 2026 (Friday, observed Independence Day since July 4 is Saturday).
        observed = datetime(2026, 7, 3, 11, 0, tzinfo=ET).astimezone(timezone.utc)
        r = market_hours.check(_order(submitted_at=observed), _ctx(), CONFIG)
        self.assertFalse(r.approved)
        self.assertEqual(r.reason, "nyse_holiday_blocked")

    def test_uses_ctx_now_over_order_submitted_at(self):
        # Codex round 8 P1: the authoritative clock is ctx.now. An order
        # with a stale submitted_at (pre-market queued) being validated
        # when ctx.now says regular session must approve as regular
        # session, not as pre-market.
        stale_submit = datetime(2026, 4, 21, 8, 0, tzinfo=ET).astimezone(timezone.utc)
        now = datetime(2026, 4, 21, 10, 30, tzinfo=ET).astimezone(timezone.utc)
        r = market_hours.check(
            _order(submitted_at=stale_submit),
            _ctx(now=now),
            CONFIG,
        )
        self.assertTrue(r.approved, msg=f"detail={r.detail}")
        self.assertEqual(r.detail["clock_source"], "ctx.now")

    def test_ctx_now_prevents_pre_market_bypass(self):
        # And the symmetric case: ctx.now at 08:00 ET + submitted_at
        # claiming 10:30 ET must reject as pre-market, regardless of
        # what the order object claims.
        fake_submit = datetime(2026, 4, 21, 10, 30, tzinfo=ET).astimezone(timezone.utc)
        now = datetime(2026, 4, 21, 8, 0, tzinfo=ET).astimezone(timezone.utc)
        r = market_hours.check(
            _order(submitted_at=fake_submit),
            _ctx(now=now),
            CONFIG,
        )
        self.assertFalse(r.approved)
        self.assertEqual(r.reason, "pre_market_not_allowed")


class PositionSizeTests(unittest.TestCase):
    def test_approves_under_concentration(self):
        # $5000 notional on $1M NAV = 0.5% << 20%
        r = position_size.check(_order(), _ctx(), CONFIG)
        self.assertTrue(r.approved)

    def test_rejects_over_concentration(self):
        # 500 shares * $500 = $250K on $1M NAV = 25% > 20% cap
        r = position_size.check(_order(qty=500), _ctx(), CONFIG)
        self.assertFalse(r.approved)
        self.assertEqual(r.reason, "position_size_exceeded")

    def test_includes_existing_position_in_concentration(self):
        # Existing $180K position + $30K new = $210K > $200K cap
        existing = Position(ticker="SPY", qty=360, avg_price=Decimal("500"))
        ctx = _ctx(positions=[existing])
        new = _order(qty=60)  # $30K
        r = position_size.check(new, ctx, CONFIG)
        self.assertFalse(r.approved)

    def test_marks_existing_position_at_ctx_current_mark(self):
        # Codex round 5 P1 + round 6 P2: engine supplies current marks
        # via ctx.current_marks. Held at avg_price $300 with qty 500 =
        # $150K cost basis (15% of $1M). Current mark $500 -> true
        # exposure $250K (25%) is already over the 20% cap.
        existing = Position(ticker="SPY", qty=500, avg_price=Decimal("300"))
        ctx = _ctx(positions=[existing], current_marks={"SPY": Decimal("500")})
        new = _order(qty=1, limit_price=Decimal("500"))
        r = position_size.check(new, ctx, CONFIG)
        self.assertFalse(r.approved)
        self.assertEqual(r.reason, "position_size_exceeded")
        self.assertEqual(r.detail["mark_source"], "ctx.current_marks")

    def test_deep_below_market_limit_does_not_collapse_exposure(self):
        # Codex round 6 P2: a user-controlled limit_price cannot be used
        # as a mark for existing inventory. A limit of $1 on a 1-share
        # buy must NOT make the 500 held shares look like $500 of
        # exposure -- previously this slipped past the cap.
        existing = Position(ticker="SPY", qty=500, avg_price=Decimal("500"))
        ctx = _ctx(positions=[existing])  # no current_marks populated
        cheap = _order(qty=1, limit_price=Decimal("1"))
        r = position_size.check(cheap, ctx, CONFIG)
        # Fallback is avg_price: 500 * 500 = 250000 exposure = 25% > 20% cap
        self.assertFalse(r.approved)
        self.assertEqual(r.reason, "position_size_exceeded")
        self.assertEqual(r.detail["mark_source"], "avg_price_fallback")

    def test_skips_sell_side(self):
        r = position_size.check(_order(side="sell"), _ctx(), CONFIG)
        self.assertTrue(r.approved)


class TradeRiskTests(unittest.TestCase):
    def test_approves_within_both_caps(self):
        # risk = 10 * (500-495) = 50 on $1M NAV (0.005%)
        r = trade_risk.check(_order(), _ctx(), CONFIG)
        self.assertTrue(r.approved)

    def test_rejects_per_trade_cap(self):
        # risk = 10000 * 5 = 50000 on $1M = 5% > 1% per-trade cap
        r = trade_risk.check(_order(qty=10000), _ctx(), CONFIG)
        self.assertFalse(r.approved)
        self.assertEqual(r.reason, "trade_risk_exceeded")

    def test_rejects_open_risk_cap(self):
        # Three existing positions each carrying $20K open risk = $60K
        # open risk already. Even a small new $1000 order brings aggregated
        # risk to $61K = 6.1% NAV > 5% open_risk cap.
        existing = [
            Position(
                ticker=f"TST{i}",
                qty=100,
                avg_price=Decimal("500"),
                stop_loss=Decimal("300"),
            )
            for i in range(3)
        ]
        ctx = _ctx(positions=existing)
        r = trade_risk.check(_order(), ctx, CONFIG)
        self.assertFalse(r.approved)
        self.assertEqual(r.reason, "open_risk_exceeded")

    def test_rejects_missing_stop(self):
        r = trade_risk.check(_order(stop_loss=None), _ctx(), CONFIG)
        self.assertFalse(r.approved)
        self.assertEqual(r.reason, "missing_stop_loss")

    def test_pending_sell_does_not_add_to_open_risk(self):
        # Codex round 4 P2: a pending sell-to-close is risk-reducing,
        # not risk-adding, in cash-only mode. It must not count against
        # the portfolio open-risk cap.
        existing = Position(
            ticker="SPY",
            qty=100,
            avg_price=Decimal("500"),
            stop_loss=Decimal("300"),
        )  # open_risk = 100 * 200 = 20000 (2% NAV)
        pending_exit = _order(side="sell", qty=100)  # would have been counted as extra risk
        ctx = _ctx(positions=[existing], pending_orders=[pending_exit])
        # A fresh $250 buy-side risk should leave us at 2.025% aggregated,
        # well under the 5% cap. Previously the pending sell would have
        # double-counted and broken the calc.
        new = _order(qty=50, limit_price=Decimal("500"), stop_loss=Decimal("495"))
        r = trade_risk.check(new, ctx, CONFIG)
        self.assertTrue(r.approved, msg=f"rejected: {r.reason} detail={r.detail}")


class LeverageTests(unittest.TestCase):
    def test_approves_with_cash(self):
        r = leverage.check(_order(), _ctx(), CONFIG)
        self.assertTrue(r.approved)

    def test_rejects_over_cash(self):
        # $5000 order, only $1000 cash
        r = leverage.check(_order(), _ctx(cash=Decimal("1000")), CONFIG)
        self.assertFalse(r.approved)
        self.assertEqual(r.reason, "cash_only_insufficient_funds")

    def test_considers_pending_buys(self):
        # Account has $5000 cash. A pending $4500 buy already reserves cash.
        # New $1000 buy puts us over.
        pending = _order(qty=9, limit_price=Decimal("500"))  # $4500
        ctx = _ctx(cash=Decimal("5000"), pending_orders=[pending])
        new = _order(qty=2, limit_price=Decimal("500"))  # $1000
        r = leverage.check(new, ctx, CONFIG)
        self.assertFalse(r.approved)

    def test_margin_mode_refused_in_phase2(self):
        cfg = dict(CONFIG)
        cfg["leverage"] = {"cash_only": False, "max_leverage": 1.0}
        r = leverage.check(_order(), _ctx(), cfg)
        self.assertFalse(r.approved)
        self.assertEqual(r.reason, "margin_mode_not_supported_phase2")

    def test_margin_mode_refused_even_for_sell(self):
        cfg = dict(CONFIG)
        cfg["leverage"] = {"cash_only": False, "max_leverage": 1.0}
        r = leverage.check(_order(side="sell"), _ctx(), cfg)
        self.assertFalse(r.approved)
        self.assertEqual(r.reason, "margin_mode_not_supported_phase2")

    def test_cash_only_rejects_naked_short(self):
        # No existing long in SPY; sell order in cash-only mode is a
        # naked short and must be rejected.
        r = leverage.check(_order(side="sell", qty=10), _ctx(), CONFIG)
        self.assertFalse(r.approved)
        self.assertEqual(r.reason, "naked_short_not_allowed")

    def test_cash_only_allows_sell_to_close(self):
        existing = Position(ticker="SPY", qty=20, avg_price=Decimal("500"))
        ctx = _ctx(positions=[existing])
        r = leverage.check(_order(side="sell", qty=10), ctx, CONFIG)
        self.assertTrue(r.approved)

    def test_cash_only_rejects_oversell(self):
        # Long 10 SPY; order to sell 20 would open a 10-share short.
        existing = Position(ticker="SPY", qty=10, avg_price=Decimal("500"))
        ctx = _ctx(positions=[existing])
        r = leverage.check(_order(side="sell", qty=20), ctx, CONFIG)
        self.assertFalse(r.approved)
        self.assertEqual(r.reason, "naked_short_not_allowed")

    def test_pending_buy_does_not_count_as_long_inventory(self):
        # Codex round 2 P1: a pending buy is not filled inventory -- if the
        # sell executes first or the buy is canceled, counting the pending
        # buy would have allowed a naked short.
        pending_buy = _order(qty=20)  # not yet filled
        ctx = _ctx(positions=[], pending_orders=[pending_buy])
        sell = _order(side="sell", qty=10)
        r = leverage.check(sell, ctx, CONFIG)
        self.assertFalse(r.approved)
        self.assertEqual(r.reason, "naked_short_not_allowed")
        self.assertEqual(r.detail["available_long_qty"], 0)

    def test_pending_sells_are_reserved_against_long_inventory(self):
        # Codex round 3 P1: 100 long + pending sell 90 should leave only
        # 10 available. A new sell for 20 must reject or the two fills
        # combine into a 10-share naked short.
        long_pos = Position(ticker="SPY", qty=100, avg_price=Decimal("500"))
        pending_sell = _order(side="sell", qty=90)
        ctx = _ctx(positions=[long_pos], pending_orders=[pending_sell])
        new_sell = _order(side="sell", qty=20)
        r = leverage.check(new_sell, ctx, CONFIG)
        self.assertFalse(r.approved)
        self.assertEqual(r.reason, "naked_short_not_allowed")
        self.assertEqual(r.detail["available_long_qty"], 10)

    def test_pending_sells_reservation_allows_residual(self):
        # 100 long + pending sell 90 leaves 10; a fresh sell for 10
        # should still be approved (exact fit).
        long_pos = Position(ticker="SPY", qty=100, avg_price=Decimal("500"))
        pending_sell = _order(side="sell", qty=90)
        ctx = _ctx(positions=[long_pos], pending_orders=[pending_sell])
        new_sell = _order(side="sell", qty=10)
        r = leverage.check(new_sell, ctx, CONFIG)
        self.assertTrue(r.approved)


class RunnerNakedShortTests(unittest.TestCase):
    def test_runner_rejects_naked_short_in_cash_only(self):
        # Regression for Codex P1: previously all three sell-skipping
        # validators approved a whitelisted in-hours sell with no long
        # position, letting a naked short through run_all().
        ok, results = run_all(_order(side="sell", qty=10), _ctx(), CONFIG)
        self.assertFalse(ok)
        self.assertEqual(results[-1].rule, "leverage")
        self.assertEqual(results[-1].reason, "naked_short_not_allowed")


class RunnerTests(unittest.TestCase):
    def test_short_circuits_on_first_rejection(self):
        # Non-whitelisted ticker is the first gate.
        ok, results = run_all(_order(ticker="TSLA"), _ctx(), CONFIG)
        self.assertFalse(ok)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].rule, "instrument_whitelist")

    def test_clean_order_runs_all_five(self):
        ok, results = run_all(_order(), _ctx(), CONFIG)
        self.assertTrue(ok)
        self.assertEqual(len(results), 5)
        self.assertTrue(all(r.approved for r in results))


if __name__ == "__main__":
    unittest.main()
