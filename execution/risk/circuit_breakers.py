"""3-layer circuit breakers + total-drawdown kill.

Layer triggers (from risk-controls.md):
    Daily soft stop   intraday drawdown <= -2%  -> halve pending sizes + pause new entries
    Daily hard stop   intraday drawdown <= -3%  -> flatten all + block new orders until next session
    Weekly cap        5-session drawdown <= -5% -> reduce risk budget 50% + mandatory strategy review
    Total drawdown    peak drawdown     <= -10% -> write .killed + Telegram ping (human-only unlock)
    Manual kill       Telegram /invest kill     -> flatten all + write .killed

Thresholds are in risk-controls.md, not duplicated in a config file yet --
changing them is rare enough that a source edit + /invest-ship review is
the right process.

Per agent-topology.md: breakers are deterministic Python. No LLM agent is
in the decision path. Engine calls `evaluate()` on every tick + before
every order.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Iterable

from .kill_switch import write_killed
from .types import AccountState, BreakerResult


DAILY_SOFT_THRESHOLD = Decimal("-0.02")
DAILY_HARD_THRESHOLD = Decimal("-0.03")
WEEKLY_THRESHOLD = Decimal("-0.05")
TOTAL_KILL_THRESHOLD = Decimal("-0.10")


@dataclass
class BreakerConfig:
    daily_soft: Decimal = DAILY_SOFT_THRESHOLD
    daily_hard: Decimal = DAILY_HARD_THRESHOLD
    weekly: Decimal = WEEKLY_THRESHOLD
    total_kill: Decimal = TOTAL_KILL_THRESHOLD


def _daily_soft(state: AccountState, cfg: BreakerConfig) -> BreakerResult:
    dd = state.intraday_drawdown_pct()
    tripped = dd <= cfg.daily_soft and dd > cfg.daily_hard
    return BreakerResult(
        tripped=tripped,
        breaker="daily_soft_stop",
        severity="soft",
        action="halve_positions",
        detail={"intraday_drawdown_pct": str(dd), "threshold": str(cfg.daily_soft)},
    )


def _daily_hard(state: AccountState, cfg: BreakerConfig) -> BreakerResult:
    dd = state.intraday_drawdown_pct()
    tripped = dd <= cfg.daily_hard
    return BreakerResult(
        tripped=tripped,
        breaker="daily_hard_stop",
        severity="hard",
        action="flatten_all",
        detail={"intraday_drawdown_pct": str(dd), "threshold": str(cfg.daily_hard)},
    )


def _weekly(state: AccountState, cfg: BreakerConfig) -> BreakerResult:
    dd = state.rolling_week_drawdown_pct()
    tripped = dd <= cfg.weekly
    return BreakerResult(
        tripped=tripped,
        breaker="weekly_cap",
        severity="weekly",
        action="reduce_budget",
        detail={"rolling_week_drawdown_pct": str(dd), "threshold": str(cfg.weekly)},
    )


def _total_kill(state: AccountState, cfg: BreakerConfig) -> BreakerResult:
    dd = state.total_drawdown_pct()
    tripped = dd <= cfg.total_kill
    return BreakerResult(
        tripped=tripped,
        breaker="total_drawdown_kill",
        severity="kill",
        action="write_killed",
        detail={"total_drawdown_pct": str(dd), "threshold": str(cfg.total_kill)},
    )


def evaluate(
    state: AccountState,
    cfg: BreakerConfig | None = None,
) -> list[BreakerResult]:
    """Return one BreakerResult per breaker, tripped or not."""
    cfg = cfg or BreakerConfig()
    return [
        _daily_soft(state, cfg),
        _daily_hard(state, cfg),
        _weekly(state, cfg),
        _total_kill(state, cfg),
    ]


def apply_kill_on_trip(
    results: Iterable[BreakerResult],
    kill_path: Path | None = None,
) -> BreakerResult | None:
    """Side-effect: if the total-drawdown breaker tripped AND .killed does
    not already exist, write .killed.

    Idempotent: if .killed is already present from an earlier trip or
    from a manual Telegram kill, this function returns None so downstream
    journaling / Telegram alerts do not fire repeatedly on every tick
    while the breaker remains tripped. The original kill record
    (timestamp, reason, source) is preserved.

    Returns the tripping BreakerResult only on the FIRST observed kill
    transition; subsequent ticks with the same breaker still tripped
    return None. Caller is responsible for surfacing that first kill to
    Telegram + the decision journal.
    """
    for r in results:
        if r.tripped and r.action == "write_killed":
            # write_killed is first-writer-wins: returns None if .killed
            # already existed (earlier tick, or a concurrent Telegram
            # kill from another process). That guarantees at most one
            # kill notification per shutdown even under cross-process
            # races, with no TOCTOU window.
            wrote = write_killed(
                reason=r.breaker,
                source="circuit_breaker",
                detail=r.detail,
                path=kill_path,
            )
            return r if wrote is not None else None
    return None


def any_hard_tripped(results: Iterable[BreakerResult]) -> bool:
    return any(r.tripped and r.severity in {"hard", "kill"} for r in results)
