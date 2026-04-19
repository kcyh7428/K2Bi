"""invest-thesis -- Bundle 4 cycle 1 (m2.11).

Python compute module for the invest-thesis skill. Consumes a
fully-structured `ThesisInput` (the skill body is responsible for
calling `/research` + reasoning the Ahern 4-phase content + scoring
the 5 thesis sub-dimensions + the 5 fundamental sub-dimensions; this
module owns validation + body assembly + atomic write + glossary-stub
maintenance).

Architecture (per spec §3.1 + MiniMax R2 LOCK to Python):

    Claude (skill body)
        -> gathers URLs from Keith
        -> invokes `/research --sources <urls>`
        -> reasons Ahern phases + scores sub-dimensions
        -> builds ThesisInput
        -> calls generate_thesis(thesis_input, vault_root, ...)
    invest_thesis.generate_thesis (this module)
        -> validates symbol + ticker_type + probabilities + sell_pct
        -> checks freshness (skip if within 30d + no --refresh)
        -> assembles body + frontmatter
        -> atomic writes via strategy_frontmatter.atomic_write_bytes
        -> updates glossary pending stubs
    Claude (skill body, post-return)
        -> invokes `scripts/wiki-log-append.sh` for the log entry

Keeping the Python pure (vault-in / vault-out, no subprocess) makes
the unit-test surface tight. The skill body owns side-effects (log
append, /research invocation) that aren't amenable to deterministic
unit testing.
"""

from __future__ import annotations

import datetime as _dt
import fcntl
import math
import re
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Optional

import yaml

from scripts.lib import strategy_frontmatter as sf


# ---------- enums + regexes ----------


# Symbol format: uppercase letters + digits, optionally with one `.`
# separator (for exchange suffixes like `0700.HK` or share classes like
# `BRK.B`). MUST contain at least one letter somewhere (prevents pure
# digit strings like `"123"` from being treated as tickers). Matched
# anchored.
_SYMBOL_RE = re.compile(r"^[A-Z0-9]+(?:\.[A-Z0-9]+)?$")


ALLOWED_TICKER_TYPES = frozenset({"equity", "etf", "pre_revenue", "penny"})
ALLOWED_ACTIONS = frozenset({"bull", "neutral", "bear"})
ALLOWED_LEARNING_STAGES = frozenset({"novice", "intermediate", "advanced"})
# Catalyst-timeline probability is a closed enum so downstream consumers
# (invest-alert routing in Bundle 5, Phase 4+ scoring) can rely on a
# stable vocabulary. Adding values requires a spec bump.
ALLOWED_CATALYST_PROBABILITIES = frozenset({"high", "medium", "low"})

# Target contract is locked to T1/T2/T3. Spec §2.1 frontmatter example +
# trade-thesis SKILL.md §5 Profit Targets table + §10 Action Plan
# Summary all assume exactly 3 targets. The body formatters would
# silently truncate a 4th target, leaving phantom sell_pct even when
# `validate_sell_pct` sees 100%. Enforce the contract at validation.
REQUIRED_TARGETS_COUNT = 3


# Conservative glossary-term list. First-occurrence detection in the
# assembled body; terms missing from the glossary get a pending stub
# appended in the same run. Add entries only when a body surface needs
# one. Lower-case form stored; matching is case-insensitive word-
# boundary.
TERM_LIST: tuple[str, ...] = (
    "moat",
    "tam",
    "rsi",
    "r/r",
    "200ma",
    "50ma",
    "p/e",
    "forward p/e",
    "drawdown",
    "sharpe",
)
# Note: `ev` omitted -- "EV" appears structurally as a column header in the
# Asymmetry Analysis table ("EV Contribution" = Expected Value), which is
# not the enterprise-value glossary concept. Auto-stubbing it would dump a
# noisy, ambiguous entry into the glossary on every thesis run. Add manually
# (or via a separate skill) when a body references enterprise-value as a
# valuation metric.


# Freshness window (inclusive). A thesis with `thesis-last-verified`
# within FRESH_DAYS of `now` is considered fresh; re-runs without
# `refresh=True` skip the rewrite. Matches the existing stub's 30-day
# window + spec Q8.
FRESH_DAYS = 30


# ---------- dataclasses ----------


@dataclass
class SubScores:
    """5-dim thesis scorecard per agents/trade-thesis.md (0-20 each)."""

    catalyst_clarity: int
    asymmetry: int
    timeline_precision: int
    edge_identification: int
    conviction_level: int

    def composite(self) -> int:
        return (
            self.catalyst_clarity
            + self.asymmetry
            + self.timeline_precision
            + self.edge_identification
            + self.conviction_level
        )


@dataclass
class FundamentalSubScores:
    """5-dim fundamental sub-scoring per agents/trade-fundamental.md
    (0-20 each). Plugs into Ahern phase 3 (financial quality) + phase 2
    (moat). NOT recomposed into thesis_score -- enrichment only."""

    valuation: int
    growth: int
    profitability: int
    financial_health: int
    moat_strength: int


@dataclass
class BullReason:
    reason: str
    evidence: str
    impact_estimate: str


@dataclass
class BearReason:
    reason: str
    evidence: str
    impact_estimate: str


@dataclass
class BaseCase:
    scenario: str
    probability: float
    target_price: float


@dataclass
class Target:
    """One row in the Profit Targets table. sell_pct total across
    T1/T2/T3 MUST equal 100 (validated at generate_thesis time)."""

    level: str
    price: float
    sell_pct: int
    reasoning: str


@dataclass
class EntryExitLevels:
    entry: float
    stop: float
    targets: list[Target]
    risk_reward_ratio: float


@dataclass
class TimeStop:
    max_hold_period: str
    reassessment_triggers: list[str]


@dataclass
class NextCatalyst:
    event: str
    date: str
    expected_impact: str


@dataclass
class CatalystTimelineEntry:
    date: str
    event: str
    expected_impact: str
    probability: str


@dataclass
class AsymmetryScenario:
    """One row of the EV-weighted scenario table. Probabilities across
    the 4-row default (Bull/Base/Neutral/Bear) MUST sum to 1.00
    ± 1e-3."""

    scenario: str
    probability: float
    target_price: float


@dataclass
class ThesisInput:
    """Complete structured input to invest_thesis.generate_thesis.

    Fields track spec §2.1 frontmatter + body content. The skill body
    populates this from /research output + Claude reasoning; this
    module does NO reasoning -- only validation + layout.
    """

    symbol: str
    ticker_type: str

    sub_scores: SubScores
    fundamental_sub_scores: FundamentalSubScores

    bull_reasons: list[BullReason]
    bear_reasons: list[BearReason]
    base_case: BaseCase

    entry_exit_levels: EntryExitLevels
    entry_triggers: list[str]
    entry_invalidation: list[str]
    exit_signals: list[str]
    time_stop: TimeStop

    recommended_action: str
    next_catalyst: NextCatalyst
    catalyst_timeline: list[CatalystTimelineEntry]

    asymmetry_scenarios: list[AsymmetryScenario]
    asymmetry_score: int
    asymmetry_score_rationale: str

    plain_english_summary: str

    phase_1_business_model: str
    phase_2_competitive_moat: str
    phase_3_financial_quality: str
    phase_4_risks_valuation: str

    primary_entry_rationale: str
    secondary_entry_aggressive: Optional[str] = None
    secondary_entry_conservative: Optional[str] = None
    initial_stop_rationale: str = ""
    trailing_stop_rationale: str = ""


@dataclass
class ThesisResult:
    path: Path
    written: bool
    skipped_reason: Optional[str] = None


# ---------- validation ----------


def validate_symbol(symbol: str) -> None:
    """Raise ValueError if `symbol` doesn't match K2Bi's ticker format.

    Accepts:
        - NVDA, GOOGL, SPY          (all letters)
        - 0700.HK                   (HKEX numeric + exchange suffix)
        - BRK.B, BRK.A              (US share-class variants)

    Rejects:
        - nvda (lowercase)
        - NVDA-B (dash)
        - 123 (digits-only, no letter anywhere)
        - "" (empty)
    """
    if not symbol:
        raise ValueError("symbol must be non-empty")
    if not _SYMBOL_RE.match(symbol):
        raise ValueError(
            f"symbol {symbol!r} does not match required format "
            f"[A-Z0-9]+(\\.[A-Z0-9]+)? (uppercase alphanumeric with "
            f"optional single `.` separator)"
        )
    if not any(ch.isalpha() for ch in symbol):
        raise ValueError(
            f"symbol {symbol!r} must contain at least one letter "
            f"(digits-only strings are not valid tickers)"
        )


def validate_ticker_type(ticker_type: str) -> None:
    if ticker_type not in ALLOWED_TICKER_TYPES:
        raise ValueError(
            f"ticker_type {ticker_type!r} not in allowed enum "
            f"{sorted(ALLOWED_TICKER_TYPES)}"
        )


def validate_action(action: str) -> None:
    if action not in ALLOWED_ACTIONS:
        raise ValueError(
            f"recommended_action {action!r} not in allowed enum "
            f"{sorted(ALLOWED_ACTIONS)}"
        )


def _validate_sub_score(name: str, val: int) -> None:
    if not isinstance(val, int) or val < 0 or val > 20:
        raise ValueError(
            f"sub_score {name} must be int in 0..20, got {val!r}"
        )


def validate_sub_scores(sub: SubScores) -> None:
    for name, val in asdict(sub).items():
        _validate_sub_score(name, val)


def validate_fundamental_sub_scores(f: FundamentalSubScores) -> None:
    for name, val in asdict(f).items():
        _validate_sub_score(name, val)


REQUIRED_TARGET_LABELS: tuple[str, ...] = ("T1", "T2", "T3")


def validate_targets(targets: list[Target]) -> None:
    """Enforce the T1/T2/T3 contract + sell_pct sum + positive prices.

    `validate_sell_pct` alone is insufficient: a caller passing 4
    targets with 25/25/25/25 would sum to 100 but the body formatters
    slice to 3, silently dropping the 4th target's sell_pct from the
    Action Plan. Locking the count at 3 matches spec §2.1 + trade-
    thesis SKILL.md §5.

    Also locks the positional label contract: index 0 is T1, index 1
    is T2, index 2 is T3. `_format_action_plan_summary` treats the
    first item as TARGET 1 while `_format_profit_targets_table` emits
    `t.level` verbatim -- a caller passing `[T2, T1, T3]` would
    produce contradictory exit instructions. Closes Codex R7 R3 #2.
    """
    if len(targets) != REQUIRED_TARGETS_COUNT:
        raise ValueError(
            f"targets must be exactly {REQUIRED_TARGETS_COUNT} (T1/T2/T3), "
            f"got {len(targets)}"
        )
    for i, (t, expected) in enumerate(zip(targets, REQUIRED_TARGET_LABELS)):
        if t.level != expected:
            raise ValueError(
                f"targets[{i}].level must be {expected!r} (positional "
                f"T1/T2/T3 contract); got {t.level!r}"
            )
    total = sum(t.sell_pct for t in targets)
    if total != 100:
        raise ValueError(
            f"targets sell_pct must sum to 100, got {total} "
            f"(individual: {[t.sell_pct for t in targets]})"
        )
    for t in targets:
        if t.price <= 0:
            raise ValueError(
                f"target {t.level} price must be positive, got {t.price!r}"
            )
        if t.sell_pct < 0:
            raise ValueError(
                f"target {t.level} sell_pct must be non-negative, "
                f"got {t.sell_pct!r}"
            )


def validate_sell_pct(targets: list[Target]) -> None:
    """Back-compat shim -- forwards to the combined target validator."""
    validate_targets(targets)


def validate_prices(ti: "ThesisInput") -> None:
    """Reject non-positive prices + non-positive risk_reward_ratio.

    Covers: entry_exit_levels.entry + .stop + .risk_reward_ratio, every
    target.price (via validate_targets), base_case.target_price, every
    asymmetry_scenario target_price. Skipped: catalyst_timeline dates
    (dates, not prices).

    Does NOT validate stop<entry or entry<T1<T2<T3 -- bear-biased
    theses can legitimately invert some of these relationships, and the
    skill emits signals, not trade orders (validator layer owns the
    trade-side checks per Q3). Does NOT validate risk_reward_ratio for
    internal consistency against entry/stop/targets because the caller
    (Claude) may have computed it from a different scenario than the
    T1 price implies (e.g. weighted R/R across scenarios).
    """
    if ti.entry_exit_levels.entry <= 0:
        raise ValueError(
            f"entry_exit_levels.entry must be positive, "
            f"got {ti.entry_exit_levels.entry!r}"
        )
    if ti.entry_exit_levels.stop <= 0:
        raise ValueError(
            f"entry_exit_levels.stop must be positive, "
            f"got {ti.entry_exit_levels.stop!r}"
        )
    if ti.entry_exit_levels.risk_reward_ratio <= 0:
        raise ValueError(
            f"entry_exit_levels.risk_reward_ratio must be positive, "
            f"got {ti.entry_exit_levels.risk_reward_ratio!r}"
        )
    if ti.base_case.target_price <= 0:
        raise ValueError(
            f"base_case.target_price must be positive, "
            f"got {ti.base_case.target_price!r}"
        )
    for s in ti.asymmetry_scenarios:
        if s.target_price <= 0:
            raise ValueError(
                f"asymmetry_scenario {s.scenario!r} target_price must "
                f"be positive, got {s.target_price!r}"
            )


def validate_catalyst_timeline(
    entries: list[CatalystTimelineEntry],
) -> None:
    """Reject catalyst entries with disallowed probability values.

    The enum is high/medium/low per spec §2.1 example. Downstream
    consumers (invest-alert routing + Phase 4 scoring) depend on the
    closed vocabulary; silent drift breaks them.
    """
    for e in entries:
        if e.probability not in ALLOWED_CATALYST_PROBABILITIES:
            raise ValueError(
                f"catalyst_timeline entry {e.date!r} probability "
                f"{e.probability!r} not in allowed enum "
                f"{sorted(ALLOWED_CATALYST_PROBABILITIES)}"
            )


REQUIRED_ASYMMETRY_SCENARIOS: tuple[str, ...] = ("Bull", "Base", "Neutral", "Bear")


def validate_asymmetry_probabilities(
    scenarios: list[AsymmetryScenario], tol: float = 1e-3
) -> None:
    """Validate the EV-weighted scenario contract:

    1. Exactly the 4 required scenarios (Bull/Base/Neutral/Bear), case-
       insensitive, no duplicates, no omissions.
    2. Each probability in [0, 1] -- a standalone negative or >1 value
       can still sum to 1.0 with offsetting partners, making a
       sum-only check insufficient.
    3. Probabilities sum to 1.00 ± tolerance.

    Closes Codex R7 P2 #1.
    """
    labels = [s.scenario.strip().capitalize() for s in scenarios]
    if sorted(labels) != sorted(REQUIRED_ASYMMETRY_SCENARIOS):
        raise ValueError(
            f"asymmetry_scenarios must contain exactly one entry for each "
            f"of {list(REQUIRED_ASYMMETRY_SCENARIOS)} (case-insensitive), "
            f"got {labels}"
        )
    for s in scenarios:
        if not (0.0 <= s.probability <= 1.0):
            raise ValueError(
                f"asymmetry_scenario {s.scenario!r} probability must be "
                f"in [0, 1], got {s.probability!r}"
            )
    total = sum(s.probability for s in scenarios)
    if not math.isclose(total, 1.0, abs_tol=tol):
        raise ValueError(
            f"asymmetry probabilities must sum to 1.00 "
            f"(tolerance {tol}), got {total!r}"
        )


def validate_base_case(bc: BaseCase) -> None:
    """Validate BaseCase.probability is a real probability (0-1).

    A caller passing `55` instead of `0.55` (or a negative) would
    otherwise serialize semantically broken data into frontmatter.
    Closes Codex R7 P2 #2.
    """
    if not (0.0 <= bc.probability <= 1.0):
        raise ValueError(
            f"base_case.probability must be in [0, 1], "
            f"got {bc.probability!r} (did you mean a decimal fraction?)"
        )


def validate_next_catalyst_is_soonest(
    next_catalyst: NextCatalyst,
    timeline: list[CatalystTimelineEntry],
) -> None:
    """Spec §3.1 step 11: `next_catalyst` is the soonest-dated row in
    `catalyst_timeline`. Downstream alerting/scheduling trusts this
    invariant; a drift means the thesis advertises the wrong upcoming
    event. Closes Codex R7 P2 #3 + R7 R3 #1 (content-level check).

    When multiple rows share the soonest date, `next_catalyst` must
    match one of them by both event AND expected_impact (not just the
    date). Otherwise a caller could author a timeline with two same-
    day events and have `next_catalyst` advertise neither.
    """
    # Empty timeline makes `next_catalyst` undefined -- there is
    # nothing to derive from. Require at least one entry so
    # downstream consumers always have a concrete row to read.
    # Closes Codex R7 R5 #1.
    if not timeline:
        raise ValueError(
            "catalyst_timeline must have at least one entry; "
            "next_catalyst is derived from the soonest row and "
            "cannot be validated without rows"
        )
    soonest = min(entry.date for entry in timeline)
    soonest_rows = [entry for entry in timeline if entry.date == soonest]
    if next_catalyst.date != soonest:
        raise ValueError(
            f"next_catalyst.date {next_catalyst.date!r} must be the "
            f"soonest date in catalyst_timeline (soonest is {soonest!r})"
        )
    # Event match only, NOT expected_impact. In the spec §2.1 example
    # next_catalyst.expected_impact ("guidance for FY26 hyperscaler
    # capex") is deliberately different from timeline[0].expected_impact
    # ("Positive -- consensus 5% upside surprise potential"): one is
    # the user-facing angle on what we'll learn, the other is the
    # analytical angle on how the stock reacts. Requiring both to
    # match would break the spec's own template.
    soonest_events = {r.event for r in soonest_rows}
    if next_catalyst.event not in soonest_events:
        raise ValueError(
            f"next_catalyst.event {next_catalyst.event!r} does not "
            f"match any soonest-date row in catalyst_timeline "
            f"(soonest events on {soonest!s}: {sorted(soonest_events)})"
        )


def validate_asymmetry_score(score: int) -> None:
    """Closed 1-10 band per trade-thesis SKILL.md §8. Downstream
    signal consumers (invest-alert + Phase 4 scoring) rely on the
    bounded range."""
    if not isinstance(score, int) or score < 1 or score > 10:
        raise ValueError(
            f"asymmetry_score must be int in 1..10, got {score!r}"
        )


def _validate_iso_date_string(field_label: str, value: str) -> None:
    """Accept only YYYY-MM-DD. Prevents 'Q4 2025', 'yesterday', or
    typoed values from being written to frontmatter + rendered to
    body. Downstream parsers (invest-alert calendar logic, Phase 4
    scoring) depend on the format."""
    if not isinstance(value, str):
        raise ValueError(
            f"{field_label} must be a string (YYYY-MM-DD), "
            f"got {value!r}"
        )
    try:
        _dt.date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(
            f"{field_label} must be ISO-8601 YYYY-MM-DD, "
            f"got {value!r}: {exc}"
        ) from exc


def validate_dates(ti: "ThesisInput") -> None:
    """Validate date strings across frontmatter-bound fields:
    next_catalyst.date + every catalyst_timeline entry's date."""
    _validate_iso_date_string("next_catalyst.date", ti.next_catalyst.date)
    for i, entry in enumerate(ti.catalyst_timeline):
        _validate_iso_date_string(
            f"catalyst_timeline[{i}].date", entry.date
        )


# ---------- conviction band ----------


def conviction_band(composite_score: int) -> str:
    """Map thesis_score 0-100 -> one of {high, good, watchlist, pass,
    avoid}. Bands per spec §2.1 frontmatter comment + Q5 preemptive
    lock.
    """
    if composite_score >= 80:
        return "high"
    if composite_score >= 65:
        return "good"
    if composite_score >= 50:
        return "watchlist"
    if composite_score >= 35:
        return "pass"
    return "avoid"


# ---------- freshness check ----------


def _read_existing_thesis_last_verified(
    path: Path,
) -> Optional[_dt.date]:
    """Return the existing file's `thesis-last-verified` date, or None
    if the file does not exist / has no frontmatter / the field is
    missing or malformed.

    Malformed values fall through to None (treated as not-fresh); this
    keeps the skill resilient to hand-edits that might corrupt the
    date -- rather than refusing to refresh, we always prefer to write
    a fresh, well-formed thesis.
    """
    if not path.exists():
        return None
    try:
        fm = sf.parse(path.read_bytes())
    except ValueError:
        return None
    raw = fm.get("thesis-last-verified")
    if raw is None:
        return None
    # `datetime` is a subclass of `date`, so check the subclass first.
    # An existing thesis file with a timestamp value (e.g. hand-edited
    # `2026-04-19T10:00:00Z`) would otherwise crash `_is_fresh`'s
    # `date - datetime` subtraction with TypeError. Coerce to date.
    # Closes Codex R7 R4 #1.
    if isinstance(raw, _dt.datetime):
        return raw.date()
    if isinstance(raw, _dt.date):
        return raw
    if isinstance(raw, str):
        try:
            return _dt.date.fromisoformat(raw.strip())
        except ValueError:
            pass
        # Try parsing as an ISO-8601 datetime string and coerce to date
        try:
            return _dt.datetime.fromisoformat(raw.strip()).date()
        except ValueError:
            return None
    return None


def _is_fresh(
    path: Path, now: _dt.date, window_days: int = FRESH_DAYS
) -> bool:
    """Return True if the existing thesis at `path` is within the
    freshness window (inclusive) relative to `now`."""
    last = _read_existing_thesis_last_verified(path)
    if last is None:
        return False
    delta = (now - last).days
    return 0 <= delta <= window_days


# ---------- body assembly ----------


def _format_adaptation_note(ticker_type: str) -> str:
    """Top-of-body banner for non-equity ticker types. Equity returns
    empty string (no banner)."""
    if ticker_type == "equity":
        return ""
    if ticker_type == "etf":
        return (
            "> [!note] **ETF Adaptation:** Thesis focuses on sector / "
            "thematic outlook + tracking efficiency, not single-company "
            "fundamentals. Phase 2 'moat' reads as index-methodology "
            "durability; Phase 3 'financial quality' reads as the "
            "underlying holdings' aggregate financial quality.\n"
        )
    if ticker_type == "pre_revenue":
        return (
            "> [!warning] **Pre-Revenue Adaptation:** Phase 3 metrics "
            "substituted by runway + TAM + pipeline readouts. "
            "Speculative. Validator caps sizing separately; this note "
            "is advisory on analysis substitution.\n"
        )
    if ticker_type == "penny":
        return (
            "> [!warning] **Penny Stock Warning:** Market cap <$300M. "
            "Expect lower liquidity + wider bid-ask spreads + higher "
            "manipulation risk. Validator caps sizing regardless of "
            "the thesis score.\n"
        )
    return ""


def _format_teach_mode_preamble(
    learning_stage: str, plain_english_summary: str
) -> str:
    """Novice-only preamble. Intermediate + advanced skip.

    Per spec §4 pattern 6: novice prepend 2-3 sentences. Intermediate
    drops preamble on routine outputs (invest-thesis = routine).
    Advanced skip.
    """
    if learning_stage != "novice":
        return ""
    return (
        f"**Plain-English summary (Teach Mode novice):**\n"
        f"{plain_english_summary}\n\n"
    )


def _format_catalyst_timeline_table(
    entries: list[CatalystTimelineEntry],
) -> str:
    header = (
        "| Date | Catalyst | Expected Impact | Probability |\n"
        "|---|---|---|---|\n"
    )
    rows = "".join(
        f"| {e.date} | {e.event} | {e.expected_impact} | {e.probability} |\n"
        for e in entries
    )
    return header + rows


def _pct_from_entry(entry: float, target_price: float) -> float:
    return (target_price / entry - 1.0) * 100.0


def _pct_loss_to_stop(entry: float, stop: float) -> float:
    return (stop / entry - 1.0) * 100.0  # negative number


def _fmt_pct_signed(pct: float) -> str:
    """Render a percent-from-entry with a correct sign glyph.

    Positive -> "+14%"; negative -> "-17%"; zero -> "+0%".
    The unsigned f-string template "+{x:.0f}%" produced "+-17%" on
    bear theses (Codex R7 R2 #1); this helper keeps the leading `+`
    for positive values while letting Python's negative-number default
    render "-" for negatives. Zero is positive for our purposes (no
    sign inversion needed).
    """
    if pct >= 0:
        return f"+{pct:.0f}%"
    return f"{pct:.0f}%"


def _format_profit_targets_table(
    entry: float, targets: list[Target]
) -> str:
    header = (
        "| Target | Price | % Gain | Action | Reasoning |\n"
        "|---|---|---|---|---|\n"
    )
    rows = []
    last_idx = len(targets) - 1
    for i, t in enumerate(targets):
        gain = _pct_from_entry(entry, t.price)
        action = (
            f"Sell remaining {t.sell_pct}%" if i == last_idx
            else f"Sell {t.sell_pct}% of position"
        )
        # Preserve cents so penny / sub-dollar tickers don't render
        # rounded prices that disagree with the cents-preserving
        # frontmatter + Action Plan Summary. Closes Codex R7 R6 #3.
        rows.append(
            f"| {t.level} | ${t.price:,.2f} | {_fmt_pct_signed(gain)} | "
            f"{action} | {t.reasoning} |\n"
        )
    return header + "".join(rows)


def _format_entry_strategy(ti: ThesisInput) -> str:
    lines = ["## Entry Strategy\n"]
    lines.append(f"- **Primary entry:** {ti.primary_entry_rationale}\n")
    if ti.secondary_entry_aggressive:
        lines.append(
            f"- **Secondary entry (aggressive):** "
            f"{ti.secondary_entry_aggressive}\n"
        )
    if ti.secondary_entry_conservative:
        lines.append(
            f"- **Secondary entry (conservative):** "
            f"{ti.secondary_entry_conservative}\n"
        )
    lines.append("\n### Entry Triggers (conditions that MUST be met)\n")
    for i, trig in enumerate(ti.entry_triggers, start=1):
        lines.append(f"{i}. {trig}\n")
    lines.append("\n### Entry Invalidation (do NOT enter if)\n")
    for i, inv in enumerate(ti.entry_invalidation, start=1):
        lines.append(f"{i}. {inv}\n")
    return "".join(lines) + "\n"


def _format_exit_strategy(ti: ThesisInput) -> str:
    lines = ["## Exit Strategy\n\n### Profit Targets\n\n"]
    lines.append(
        _format_profit_targets_table(
            ti.entry_exit_levels.entry, ti.entry_exit_levels.targets
        )
    )
    stop_pct = _pct_loss_to_stop(
        ti.entry_exit_levels.entry, ti.entry_exit_levels.stop
    )
    lines.append(
        f"\n### Stop Loss\n"
        f"- **Initial stop:** ${ti.entry_exit_levels.stop:,.2f} "
        f"({_fmt_pct_signed(stop_pct)} from entry) -- {ti.initial_stop_rationale}\n"
        f"- **Trailing stop:** {ti.trailing_stop_rationale}\n"
    )
    lines.append(
        f"\n### Time Stop\n"
        f"- **Maximum hold:** {ti.time_stop.max_hold_period}\n"
        f"- **Reassessment triggers:**\n"
    )
    for trig in ti.time_stop.reassessment_triggers:
        lines.append(f"  - {trig}\n")
    lines.append("\n### Exit Signals (sell regardless of price)\n")
    for i, sig in enumerate(ti.exit_signals, start=1):
        lines.append(f"{i}. {sig}\n")
    return "".join(lines) + "\n"


def _format_asymmetry_analysis(ti: ThesisInput) -> str:
    header = (
        "## Asymmetry Analysis\n\n"
        "| Scenario | Probability | Target Price | EV Contribution |\n"
        "|---|---|---|---|\n"
    )
    rows = []
    ev_total = 0.0
    for s in ti.asymmetry_scenarios:
        ev = s.probability * s.target_price
        ev_total += ev
        rows.append(
            f"| {s.scenario} | {s.probability:.2f} | ${s.target_price:,.2f} | {ev:.2f} |\n"
        )
    total_row = (
        f"| **Total EV** | **1.00** | | **${ev_total:.2f}** |\n"
    )
    score_line = (
        f"\n**Asymmetry Score:** {ti.asymmetry_score}/10 -- "
        f"{ti.asymmetry_score_rationale}\n"
    )
    return header + "".join(rows) + total_row + score_line + "\n"


def _format_thesis_scorecard(sub: SubScores) -> str:
    return (
        "## Thesis Scorecard\n\n"
        "| Sub-Dimension | Score |\n"
        "|---|---|\n"
        f"| Catalyst Clarity | {sub.catalyst_clarity}/20 |\n"
        f"| Asymmetry | {sub.asymmetry}/20 |\n"
        f"| Timeline Precision | {sub.timeline_precision}/20 |\n"
        f"| Edge Identification | {sub.edge_identification}/20 |\n"
        f"| Conviction Level | {sub.conviction_level}/20 |\n"
        f"| **Total** | **{sub.composite()}/100** |\n\n"
    )


def _format_fundamental_subscoring(f: FundamentalSubScores) -> str:
    return (
        "## Fundamental Sub-Scoring\n\n"
        "Enrichment only -- NOT recomposed into thesis_score. Plugs "
        "into Phase 3 (financial quality) + Phase 2 (moat).\n\n"
        "| Sub-Dimension | Score |\n"
        "|---|---|\n"
        f"| Valuation | {f.valuation}/20 |\n"
        f"| Growth | {f.growth}/20 |\n"
        f"| Profitability | {f.profitability}/20 |\n"
        f"| Financial Health | {f.financial_health}/20 |\n"
        f"| Moat Strength | {f.moat_strength}/20 |\n\n"
    )


POSITION_LITERAL = "validator-owned (see config.yaml position_size cap)"


def _format_action_plan_summary(ti: ThesisInput) -> str:
    """Code-block format from trade-thesis SKILL.md §10. POSITION line
    is ALWAYS the literal validator-owned string (Q3 validator-isolation)
    -- never compute a size here."""
    e = ti.entry_exit_levels.entry
    s = ti.entry_exit_levels.stop
    t1, t2, t3 = ti.entry_exit_levels.targets[:3]
    stop_pct = _pct_loss_to_stop(e, s)
    g1 = _pct_from_entry(e, t1.price)
    g2 = _pct_from_entry(e, t2.price)
    g3 = _pct_from_entry(e, t3.price)
    return (
        "## Action Plan Summary\n\n"
        "```\n"
        f"TICKER:        {ti.symbol}\n"
        f"DIRECTION:     {ti.recommended_action}\n"
        f"ENTRY:         ${e:,.2f} (limit order)\n"
        f"STOP LOSS:     ${s:,.2f} ({_fmt_pct_signed(stop_pct)})\n"
        f"TARGET 1:      ${t1.price:,.2f} ({_fmt_pct_signed(g1)}) -- sell {t1.sell_pct}%\n"
        f"TARGET 2:      ${t2.price:,.2f} ({_fmt_pct_signed(g2)}) -- sell {t2.sell_pct}%\n"
        f"TARGET 3:      ${t3.price:,.2f} ({_fmt_pct_signed(g3)}) -- sell remaining\n"
        f"RISK/REWARD:   {ti.entry_exit_levels.risk_reward_ratio}:1\n"
        f"POSITION:      {POSITION_LITERAL}\n"
        f"TIMEFRAME:     {ti.time_stop.max_hold_period}\n"
        f"NEXT CATALYST: {ti.next_catalyst.event} on {ti.next_catalyst.date}\n"
        "```\n"
    )


def _assemble_body(ti: ThesisInput, learning_stage: str) -> str:
    parts = [
        "> [!robot] K2Bi analysis -- Phase 2 MVP via one-shot /research\n\n",
    ]
    adapt = _format_adaptation_note(ti.ticker_type)
    if adapt:
        parts.append(adapt + "\n")
    preamble = _format_teach_mode_preamble(
        learning_stage, ti.plain_english_summary
    )
    if preamble:
        parts.append(preamble)
    parts.append(
        f"## Phase 1: Business Model\n{ti.phase_1_business_model}\n\n"
    )
    parts.append(
        f"## Phase 2: Competitive Position / Moat\n"
        f"{ti.phase_2_competitive_moat}\n\n"
    )
    parts.append(
        f"## Phase 3: Financial Quality\n{ti.phase_3_financial_quality}\n\n"
    )
    parts.append(
        f"## Phase 4: Risks + Valuation\n{ti.phase_4_risks_valuation}\n\n"
    )
    parts.append(
        "## Catalyst Timeline\n\n"
        + _format_catalyst_timeline_table(ti.catalyst_timeline)
        + "\n"
    )
    parts.append(_format_entry_strategy(ti))
    parts.append(_format_exit_strategy(ti))
    parts.append(_format_asymmetry_analysis(ti))
    parts.append(_format_thesis_scorecard(ti.sub_scores))
    parts.append(_format_fundamental_subscoring(ti.fundamental_sub_scores))
    parts.append(_format_action_plan_summary(ti))
    return "".join(parts)


# ---------- frontmatter assembly ----------


def _build_frontmatter(
    ti: ThesisInput,
    now: _dt.date,
    composite: int,
    band: str,
) -> dict[str, Any]:
    """Ordered dict for YAML serialization; order matches spec §2.1."""
    iso = now.isoformat()
    return {
        "tags": ["ticker", ti.symbol, "thesis"],
        "date": iso,
        "type": "ticker",
        "origin": "k2bi-extract",
        "up": "[[tickers/index]]",
        "symbol": ti.symbol,
        "confidence-last-verified": iso,
        "thesis-last-verified": iso,
        "thesis_score": composite,
        "sub_scores": asdict(ti.sub_scores),
        "fundamental_sub_scores": asdict(ti.fundamental_sub_scores),
        "bull_case": {
            "reasons": [asdict(r) for r in ti.bull_reasons],
        },
        "bear_case": {
            "reasons": [asdict(r) for r in ti.bear_reasons],
        },
        "base_case": asdict(ti.base_case),
        "entry_exit_levels": {
            "entry": ti.entry_exit_levels.entry,
            "stop": ti.entry_exit_levels.stop,
            "targets": [asdict(t) for t in ti.entry_exit_levels.targets],
            "risk_reward_ratio": ti.entry_exit_levels.risk_reward_ratio,
        },
        "entry_triggers": list(ti.entry_triggers),
        "entry_invalidation": list(ti.entry_invalidation),
        "exit_signals": list(ti.exit_signals),
        "time_stop": {
            "max_hold_period": ti.time_stop.max_hold_period,
            "reassessment_triggers": list(ti.time_stop.reassessment_triggers),
        },
        "recommended_action": ti.recommended_action,
        "conviction_band": band,
        "next_catalyst": asdict(ti.next_catalyst),
        "catalyst_timeline": [asdict(c) for c in ti.catalyst_timeline],
        "ticker_type": ti.ticker_type,
    }


def _serialize_file(frontmatter: dict[str, Any], body: str) -> bytes:
    """Emit `---\\nYAML\\n---\\n\\nbody\\n` bytes. sort_keys=False so
    the dict insertion order from `_build_frontmatter` is preserved;
    this makes the on-disk layout stable + human-readable."""
    yaml_text = yaml.safe_dump(
        frontmatter,
        sort_keys=False,
        allow_unicode=True,
        default_flow_style=False,
    )
    text = f"---\n{yaml_text}---\n\n{body}"
    if not text.endswith("\n"):
        text += "\n"
    return text.encode("utf-8")


# ---------- glossary stub maintenance ----------


def _parse_glossary_headings(content_text: str) -> set[str]:
    """Extract lowercase `## heading` labels from glossary text.

    Shared between the pre-lock check (cheap + best-effort) and the
    post-lock re-check (authoritative) in `_update_glossary`.
    """
    headings: set[str] = set()
    for line in content_text.splitlines():
        stripped = line.strip()
        if stripped.startswith("## "):
            headings.add(stripped[3:].strip().lower())
    return headings


def _read_glossary_headings(path: Path) -> set[str]:
    """Return the set of lowercase heading labels already in the
    glossary. Used to de-dupe pending-stub appends."""
    if not path.exists():
        return set()
    return _parse_glossary_headings(path.read_text())


def _detect_body_terms(body: str) -> list[str]:
    """Return TERM_LIST entries that appear in `body` (case-insensitive
    word-boundary match), preserving TERM_LIST order. First-occurrence
    only -- we emit one pending stub per term per run."""
    found: list[str] = []
    lower = body.lower()
    for term in TERM_LIST:
        # Word-boundary match that also accepts adjacent punctuation
        # (e.g. "Moat:" should count). Using a coarse re with lookaround.
        pattern = re.compile(
            r"(?<![A-Za-z0-9])" + re.escape(term) + r"(?![A-Za-z0-9])",
            re.IGNORECASE,
        )
        if pattern.search(body):
            if term not in found:
                found.append(term)
    return found


def _ensure_glossary_exists(path: Path) -> None:
    if path.exists():
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    sf.atomic_write_bytes(
        path,
        (
            "---\n"
            "tags: [glossary, k2bi, reference]\n"
            "type: glossary\n"
            "origin: k2bi-generate\n"
            "up: \"[[index]]\"\n"
            "---\n"
            "\n"
            "# K2Bi Trading Glossary\n"
            "\n"
            "Living glossary of trading terms used by K2Bi skills. "
            "Grows organically as new concepts appear in skill outputs. "
            "Skills auto-stub new terms; definitions filled by Keith "
            "or by `/invest-compile`.\n"
        ).encode("utf-8"),
    )


def _append_glossary_stubs_under_lock(
    path: Path, candidate_terms: list[str], now: _dt.date
) -> None:
    """Append `## term\\n\\n_definition pending ..._\\n` for each
    candidate term that is still missing when we read the glossary
    under an exclusive lock. The lock closes the TOCTOU window
    between pre-check (in the caller) and write here -- two concurrent
    `generate_thesis` runs cannot clobber each other's stub appends or
    double-add the same term.

    Re-checks `candidate_terms` against the lock-held glossary so the
    pre-lock cheap read is treated as a hint, not a contract. This
    matches the canonical "optimistic check outside the lock, authority
    inside" pattern.
    """
    if not candidate_terms:
        return
    # Dot-prefix keeps the lock file hidden from casual `ls`; living
    # next to the glossary is fine because flock is per-file-handle,
    # not per-path-string, and the glossary's parent directory already
    # exists (guaranteed by `_ensure_glossary_exists`).
    lock_path = path.parent / f".{path.name}.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with open(lock_path, "a") as lock_f:
        fcntl.flock(lock_f.fileno(), fcntl.LOCK_EX)
        try:
            existing_bytes = path.read_bytes()
            existing_headings = _parse_glossary_headings(
                existing_bytes.decode("utf-8", errors="replace")
            )
            still_missing = [
                t for t in candidate_terms if t not in existing_headings
            ]
            if not still_missing:
                return
            addition_parts: list[str] = []
            if not existing_bytes.endswith(b"\n"):
                addition_parts.append("\n")
            for term in still_missing:
                addition_parts.append(
                    f"\n## {term}\n\n"
                    f"_definition pending -- added by invest-thesis "
                    f"{now.isoformat()}_\n"
                )
            new_content = existing_bytes + "".join(addition_parts).encode(
                "utf-8"
            )
            sf.atomic_write_bytes(path, new_content)
        finally:
            fcntl.flock(lock_f.fileno(), fcntl.LOCK_UN)


def _update_glossary(
    vault_root: Path, body: str, now: _dt.date
) -> None:
    """Create glossary if missing; append pending stubs for TERM_LIST
    terms that appear in body but are not already headings.

    Uses a two-phase check:
        1. Pre-lock cheap read + term filter (fast path; avoids taking
           the lock when nothing needs adding).
        2. Lock-held re-read + authoritative filter + atomic write (the
           only path that mutates the glossary).

    The pre-lock read is optimistic; the under-lock read is the source
    of truth. MiniMax R2 TOCTOU finding closed.
    """
    path = vault_root / "wiki" / "reference" / "glossary.md"
    # Mirror the vault-containment check we do for ticker_path: a
    # symlinked `wiki/reference` could otherwise redirect the glossary
    # write. Codex R7 R2 #2 defence-in-depth applied here too.
    _assert_path_within_vault(path, vault_root)
    _ensure_glossary_exists(path)
    terms_in_body = _detect_body_terms(body)
    if not terms_in_body:
        return
    pre_lock_existing = _read_glossary_headings(path)
    candidate_missing = [
        t for t in terms_in_body if t not in pre_lock_existing
    ]
    if not candidate_missing:
        return
    _append_glossary_stubs_under_lock(path, candidate_missing, now)


# ---------- main entry ----------


def _normalize_learning_stage(stage: str) -> str:
    """Unknown stages fall back to `advanced` (no preamble). Matches
    CLAUDE.md 'skills should never fail because the dial is unset'
    guidance -- absent / malformed dial = skip the lightest layer."""
    if stage in ALLOWED_LEARNING_STAGES:
        return stage
    return "advanced"


def generate_thesis(
    thesis_input: ThesisInput,
    vault_root: Path,
    *,
    refresh: bool = False,
    learning_stage: str = "advanced",
    now: Optional[_dt.date] = None,
) -> ThesisResult:
    """Write `wiki/tickers/<SYMBOL>.md` with Ahern 4-phase thesis +
    structured frontmatter (per spec §2.1) under `vault_root`.

    Validation order (fail-fast before any I/O):
        1. symbol format
        2. ticker_type enum
        3. recommended_action enum
        4. sub_scores / fundamental_sub_scores ranges
        5. targets sell_pct == 100
        6. asymmetry probabilities sum == 1.00 (±1e-3)

    Freshness check (after validation):
        - If `wiki/tickers/<SYMBOL>.md` has `thesis-last-verified` within
          FRESH_DAYS (30) of `now` AND `refresh is False`: skip with an
          informational ThesisResult. No write; no glossary mutation.

    On write:
        - Atomic file write via `sf.atomic_write_bytes`.
        - Glossary stub maintenance (append pending stubs for TERM_LIST
          terms that newly appear).

    Args:
        thesis_input: structured ThesisInput.
        vault_root: K2Bi vault root (has `wiki/`, `wiki/reference/`).
        refresh: if True, force rewrite even if existing thesis fresh.
        learning_stage: `novice` | `intermediate` | `advanced` (unknown
            defaults to `advanced`). Controls Teach Mode preamble.
        now: date to stamp into frontmatter + freshness check. Defaults
            to `date.today()` but tests pin this deterministically.

    Returns:
        ThesisResult with `written: bool`, `path`, and optional
        `skipped_reason` when a freshness skip triggers.

    Raises:
        ValueError on any validation failure.
    """
    # 1. Validation (before any I/O)
    validate_symbol(thesis_input.symbol)
    validate_ticker_type(thesis_input.ticker_type)
    validate_action(thesis_input.recommended_action)
    validate_sub_scores(thesis_input.sub_scores)
    validate_fundamental_sub_scores(thesis_input.fundamental_sub_scores)
    validate_targets(thesis_input.entry_exit_levels.targets)
    validate_prices(thesis_input)
    validate_base_case(thesis_input.base_case)
    validate_catalyst_timeline(thesis_input.catalyst_timeline)
    validate_asymmetry_probabilities(thesis_input.asymmetry_scenarios)
    validate_asymmetry_score(thesis_input.asymmetry_score)
    validate_dates(thesis_input)
    validate_next_catalyst_is_soonest(
        thesis_input.next_catalyst, thesis_input.catalyst_timeline
    )

    if now is None:
        now = _dt.date.today()
    learning_stage = _normalize_learning_stage(learning_stage)

    # Defence-in-depth: reject obviously wrong vault_root values before
    # we start creating directories. `Path('/')`, empty paths, or stale
    # Path objects pointing at non-existent / non-directory entries
    # would otherwise cascade into directory-creation side effects
    # outside the vault.
    if not vault_root.is_dir():
        raise ValueError(
            f"vault_root {vault_root!s} is not an existing directory; "
            f"refusing to write"
        )

    ticker_path = (
        vault_root / "wiki" / "tickers" / f"{thesis_input.symbol}.md"
    )

    # Closes Codex R7 R2 #2: even though atomic_write_bytes rejects a
    # symlinked final path, a symlinked ancestor (e.g. vault_root/wiki
    # pointing outside the vault) would redirect writes out of the
    # vault tree. Resolve the intended write path and check it still
    # lives under the resolved vault_root. This is narrow enough to
    # pass through benign macOS-level symlinks (/var -> /private/var)
    # because both sides resolve identically.
    _assert_path_within_vault(ticker_path, vault_root)

    # Clean up orphaned tempfiles in the ticker directory scoped to
    # this symbol. A SIGKILL between `os.fsync` and `os.replace` in a
    # prior run would leave `.<symbol>.md.tmp.<suffix>` behind; over
    # many crashes the directory accumulates garbage. Scoped to the
    # current symbol to keep the scan O(1).
    _cleanup_orphan_tempfiles(
        ticker_path.parent, f".{ticker_path.name}.tmp."
    )

    # 2. Freshness check
    if not refresh and _is_fresh(ticker_path, now):
        return ThesisResult(
            path=ticker_path,
            written=False,
            skipped_reason=(
                f"thesis is fresh (thesis-last-verified within "
                f"{FRESH_DAYS} days); pass refresh=True to force re-run"
            ),
        )

    # 3. Body + frontmatter
    composite = thesis_input.sub_scores.composite()
    band = conviction_band(composite)
    body = _assemble_body(thesis_input, learning_stage)
    frontmatter = _build_frontmatter(thesis_input, now, composite, band)
    file_bytes = _serialize_file(frontmatter, body)

    # 4. Atomic write
    sf.atomic_write_bytes(ticker_path, file_bytes)

    # 5. Glossary stub maintenance (after the main write is durable --
    #    the thesis file is the primary output; a glossary-write
    #    hiccup should not invalidate the thesis). Errors surface to
    #    stderr so disk-full / permission-denied don't get silently
    #    swallowed, but they don't abort the skill because the thesis
    #    itself is already on disk (no point in raising after the
    #    primary side-effect succeeded).
    #
    # ValueError is included because `sf.atomic_write_bytes` rejects
    # symlinked destinations (Bundle 4 R3 HIGH #1 defence-in-depth),
    # and a symlinked glossary file should degrade gracefully in the
    # same way as an OSError -- the thesis itself has nothing to do
    # with the glossary layout.
    try:
        _update_glossary(vault_root, body, now)
    except (OSError, ValueError) as exc:
        print(
            f"invest-thesis warning: glossary update failed for "
            f"{thesis_input.symbol} (thesis itself was written to "
            f"{ticker_path}): {exc.__class__.__name__}: {exc}",
            file=sys.stderr,
        )

    return ThesisResult(path=ticker_path, written=True)


def _assert_path_within_vault(path: Path, vault_root: Path) -> None:
    """Raise ValueError if `path` (after symlink resolution) is not a
    descendant of `vault_root` (after symlink resolution).

    Uses `.resolve(strict=False)` so missing leaf files / missing
    parent directories don't bubble up before we've had a chance to
    create them -- only existing symlink traversals inside the path
    matter. Both sides are resolved so benign platform-level symlinks
    (macOS `/var -> /private/var`) don't trigger a false positive.
    """
    resolved = path.resolve(strict=False)
    root_resolved = vault_root.resolve(strict=False)
    try:
        resolved.relative_to(root_resolved)
    except ValueError:
        raise ValueError(
            f"path {path!s} resolves to {resolved!s}, outside vault "
            f"root {root_resolved!s}; refusing to write"
        ) from None


# Age threshold (seconds) below which a dot-prefixed tempfile is
# considered potentially in-flight and must NOT be unlinked. Covers
# the atomic-write window (tempfile.mkstemp -> write -> fsync ->
# os.replace), which on a modern SSD completes in low milliseconds
# but can stretch under heavy I/O. 60s is generous enough to never
# touch an active peer writer, short enough to reliably garbage-
# collect SIGKILL leftovers on the next invocation.
ORPHAN_TEMPFILE_MIN_AGE_SECONDS = 60.0


def _cleanup_orphan_tempfiles(directory: Path, prefix: str) -> None:
    """Unlink dot-prefixed tempfiles left behind by a prior SIGKILL
    between `os.fsync` and `os.replace` in `atomic_write_bytes`.

    Scoped to a single `prefix` so we only touch leftovers from the
    current symbol's writes; never a vault-wide scan. Silent on
    `FileNotFoundError` (raced with another cleanup) and `OSError`
    (permission / I/O) -- the cleanup is best-effort. The main write
    immediately follows; if the directory is unusable it will surface
    there with a clear error.

    Only unlinks tempfiles whose mtime is older than
    `ORPHAN_TEMPFILE_MIN_AGE_SECONDS`. Closes Codex R7 R5 #2: a
    parallel `/thesis NVDA` run racing with this one would otherwise
    have its freshly-created tempfile swept away before
    `os.replace` could commit it, causing the peer write to fail or
    silently lose output.
    """
    if not directory.exists() or not directory.is_dir():
        return
    now_s = time.time()
    try:
        for entry in directory.iterdir():
            if not entry.name.startswith(prefix):
                continue
            try:
                age = now_s - entry.stat().st_mtime
            except FileNotFoundError:
                continue
            except OSError:
                continue
            if age < ORPHAN_TEMPFILE_MIN_AGE_SECONDS:
                # Potentially a live peer writer's tempfile -- skip.
                continue
            try:
                entry.unlink()
            except FileNotFoundError:
                pass
            except OSError:
                # Log-and-continue: best-effort cleanup must not
                # itself crash the skill.
                print(
                    f"invest-thesis warning: failed to clean "
                    f"orphan tempfile {entry!s}",
                    file=sys.stderr,
                )
    except OSError:
        # Directory iteration failed; skip cleanup, let the main write
        # surface the underlying issue.
        pass
