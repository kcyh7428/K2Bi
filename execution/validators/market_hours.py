# cash-only invariant: no sell-side paths in this module (the gate is
# time-based, not position-based). Enforcement owned by
# execution.risk.cash_only via leverage validator.
"""Market hours validator.

Converts order.submitted_at (UTC) into US/Eastern and compares against
the regular session window (default 09:30-16:00 ET). Extended-hours
orders require both the config flag (`allow_pre_market` /
`allow_after_hours`) and the order's own `extended_hours=True` flag.

Uses zoneinfo from the stdlib so no third-party tz dependency.
"""

from __future__ import annotations

from datetime import datetime, time
from typing import Any
from zoneinfo import ZoneInfo

from ..risk.market_calendar import is_nyse_holiday
from .types import Order, RiskContext, ValidatorResult


RULE = "market_hours"
REASON_CLOSED = "market_closed"
REASON_PRE_MARKET_BLOCKED = "pre_market_not_allowed"
REASON_AFTER_HOURS_BLOCKED = "after_hours_not_allowed"
REASON_WEEKEND = "weekend_blocked"
REASON_HOLIDAY = "nyse_holiday_blocked"
REASON_APPROVED = "market_hours_ok"

ET = ZoneInfo("US/Eastern")

# US equity extended-hours sessions. Windows are outside the regular
# 09:30-16:00 ET session but still represent actual liquidity. Orders
# outside BOTH regular and these extended windows (e.g. 02:00 ET, 21:00
# ET, overnight) must reject even when extended-hours flags are on.
PRE_MARKET_OPEN = time(4, 0)
AFTER_HOURS_CLOSE = time(20, 0)


def _parse_hm(value: str) -> time:
    hh, mm = value.split(":")
    return time(int(hh), int(mm))


def check(order: Order, ctx: RiskContext, config: dict[str, Any]) -> ValidatorResult:
    mh = config["market_hours"]
    regular_open = _parse_hm(mh.get("regular_open", "09:30"))
    regular_close = _parse_hm(mh.get("regular_close", "16:00"))
    allow_pre = bool(mh.get("allow_pre_market", False))
    allow_after = bool(mh.get("allow_after_hours", False))
    pre_open = _parse_hm(mh.get("pre_market_open", PRE_MARKET_OPEN.strftime("%H:%M")))
    after_close = _parse_hm(mh.get("after_hours_close", AFTER_HOURS_CLOSE.strftime("%H:%M")))

    # Authoritative clock is ctx.now (engine's current time). Using
    # order.submitted_at would let a stale or spoofed queued timestamp
    # bypass the window: a pre-market-queued order validated at 10:00 ET
    # with submitted_at=08:00 ET would appear "pre-market" instead of
    # "regular session". Fall back to order.submitted_at only if the
    # context lacks a clock (test harness path).
    clock = ctx.now if ctx.now is not None else order.submitted_at
    if clock.tzinfo is None:
        raise ValueError("ctx.now / order.submitted_at must be timezone-aware")
    et_time = clock.astimezone(ET)
    weekday = et_time.weekday()  # Mon=0 ... Sun=6

    detail = {
        "clock_utc": clock.astimezone(ZoneInfo("UTC")).isoformat(),
        "clock_source": "ctx.now" if ctx.now is not None else "order.submitted_at",
        "et_time": et_time.isoformat(),
        "weekday": weekday,
        "regular_open": regular_open.isoformat(),
        "regular_close": regular_close.isoformat(),
        "pre_market_open": pre_open.isoformat(),
        "after_hours_close": after_close.isoformat(),
        "extended_hours": order.extended_hours,
    }

    if weekday >= 5:
        return ValidatorResult(
            approved=False,
            rule=RULE,
            reason=REASON_WEEKEND,
            detail=detail,
        )
    if is_nyse_holiday(et_time.date()):
        return ValidatorResult(
            approved=False,
            rule=RULE,
            reason=REASON_HOLIDAY,
            detail={**detail, "session_date": et_time.date().isoformat()},
        )

    now_time = et_time.time()
    if regular_open <= now_time < regular_close:
        return ValidatorResult(approved=True, rule=RULE, reason=REASON_APPROVED, detail=detail)

    if now_time < regular_open:
        # Pre-market window only covers pre_open..regular_open. Times
        # before pre_open (overnight / early morning) are market-closed
        # regardless of the extended-hours flag.
        if (
            order.extended_hours
            and allow_pre
            and pre_open <= now_time < regular_open
        ):
            return ValidatorResult(approved=True, rule=RULE, reason=REASON_APPROVED, detail=detail)
        return ValidatorResult(
            approved=False,
            rule=RULE,
            reason=REASON_PRE_MARKET_BLOCKED,
            detail=detail,
        )

    # now_time >= regular_close; after-hours window is regular_close..after_close
    if (
        order.extended_hours
        and allow_after
        and regular_close <= now_time < after_close
    ):
        return ValidatorResult(approved=True, rule=RULE, reason=REASON_APPROVED, detail=detail)
    return ValidatorResult(
        approved=False,
        rule=RULE,
        reason=REASON_AFTER_HOURS_BLOCKED,
        detail=detail,
    )
