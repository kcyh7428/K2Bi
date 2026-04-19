"""Build limits-proposal files for `/invest-propose-limits` -- Bundle 3 cycle 6.

This module is the testable core of the `invest-propose-limits` skill. The
skill body (`.claude/skills/invest-propose-limits/SKILL.md`) delegates the
safety-critical work here so it lives in deterministic Python instead of
LLM improvisation:

  1. Parse Keith's natural-language ask into a structured
     (rule, change_type, target field, value / ticker) tuple, OR return a
     clarification request when the ask is ambiguous / out of matrix.
  2. Read the current `execution/validators/config.yaml` (read-only) and
     extract the exact text slice the change would touch; build the
     matching after-slice with the requested value.
  3. Emit a deterministic safety-impact paragraph per §5.2 -- four
     templates keyed by (rule, change_type), NO LLM improvisation on
     safety-critical text.
  4. Render the limits-proposal markdown per spec §2.3 so cycle 5's
     `/invest-ship --approve-limits` handler can consume it verbatim.
  5. Write the proposal file atomically at
     `review/strategy-approvals/YYYY-MM-DD_limits-proposal_<slug>.md`.

**Hard rule (spec §5.4 / §5 preemptive decision):** this module NEVER
opens `execution/validators/config.yaml` in write mode. Only
`/invest-ship --approve-limits` applies the patch, and only after Keith
reviews the proposal file this module produced. Cycle 4's pre-commit
Check C is a backstop; this module's refusal to write config.yaml is the
first line of defence.

Python API:

    parse_nl(text, config_text)               -> ParsedDelta | Clarification
    compute_safety_impact(delta, config_text) -> str
    compute_slug(delta)                       -> str
    build_yaml_patch(delta, config_text)      -> tuple[str, str]
    render_proposal(...)                      -> str
    write_proposal(repo, ...)                 -> Path

CLI (consumed from the skill body via the Bash tool):

    python3 -m scripts.lib.propose_limits parse  --text "<ask>"
    python3 -m scripts.lib.propose_limits write  --text "<ask>" --rationale "..."
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tempfile
from dataclasses import dataclass, field
from datetime import date as _date
from pathlib import Path


# ---------- constants ----------

# Authoritative matrix -- kept in lockstep with cycle 5's
# invest_ship_strategy.VALID_LIMITS_RULES / VALID_CHANGE_TYPES. A
# parse result that falls outside this matrix routes to clarification.
VALID_RULES = frozenset(
    {
        "position_size",
        "trade_risk",
        "leverage",
        "market_hours",
        "instrument_whitelist",
    }
)
VALID_CHANGE_TYPES = frozenset({"widen", "tighten", "add", "remove"})

# Path the generated limits-proposal is written to (relative to repo root).
APPROVALS_DIR = Path("review") / "strategy-approvals"

# Path the proposal will later be applied to. This STRING is what the
# skill writes into `applies-to:` frontmatter; cycle 5's handler compares
# it byte-for-byte against this exact value.
CONFIG_APPLIES_TO = "execution/validators/config.yaml"

# Per preemptive decision #3: filename slug = `<rule>-<change_type>`
# with ticker suffix for ticker-specific changes. Deterministic; no
# content hashing required at MVP volume.
_SLUG_SEP = "-"

# Disambiguation thresholds for position_size (the only rule with two
# scalar sub-fields worth routing between automatically). Per-trade
# risk in prod is < 5% by convention; concentration is typically 10-30%
# of NAV. Any NL target value >= 5% with no explicit sub-field keyword
# routes to max_ticker_concentration_pct, < 5% routes to
# max_trade_risk_pct. Anything still ambiguous raises a Clarification.
_POSITION_SIZE_THRESHOLD = 0.05


# ---------- dataclasses ----------


@dataclass(frozen=True)
class ParsedDelta:
    """Structured result of NL parsing.

    `before` / `after` carry the scalar / boolean / string values we will
    serialize into the `## Change` block. The `## YAML Patch` before/
    after blocks are derived separately from `build_yaml_patch` because
    they need to match `config_text` byte-for-byte.
    """

    rule: str
    change_type: str
    field: str | None
    before: object
    after: object
    ticker: str | None = None
    summary: str = ""

    def validate(self) -> None:
        if self.rule not in VALID_RULES:
            raise ProposalError(
                f"rule {self.rule!r} not in {sorted(VALID_RULES)}"
            )
        if self.change_type not in VALID_CHANGE_TYPES:
            raise ProposalError(
                f"change_type {self.change_type!r} not in "
                f"{sorted(VALID_CHANGE_TYPES)}"
            )


@dataclass(frozen=True)
class Clarification:
    """NL input could not be resolved to a unique ParsedDelta.

    `question` is shown to Keith verbatim; `options` is a short list of
    suggested restatements he can pick from (may be empty when the asker
    needs a freeform answer).
    """

    question: str
    options: tuple[str, ...] = ()


# ---------- exceptions ----------


class ProposalError(ValueError):
    """Structural error in the proposal pipeline. Message surfaces to Keith."""


class ConfigReadError(ProposalError):
    """Could not locate the target slice in the current config.yaml.

    Distinct from the generic ProposalError so the skill body can surface
    a specific "drift" message: the proposal was authored against a
    config shape that does not exist on disk, so the skill declines to
    write a stale patch.
    """


# ---------- NL parsing ----------

# Rule keyword map. More-specific keywords first so a substring match on
# "daily risk" wins over "risk" (which would otherwise collide with
# position_size). The regex compiler treats the patterns as
# case-insensitive word matches.
_RULE_KEYWORDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "trade_risk",
        (
            "daily risk",
            "portfolio risk",
            "portfolio-level risk",
            "total risk",
            "open risk",
            "aggregate risk",
            "max_open_risk_pct",
        ),
    ),
    (
        "instrument_whitelist",
        (
            "whitelist",
            "instrument whitelist",
            "instrument_whitelist",
            "ticker list",
            "symbol list",
        ),
    ),
    (
        "market_hours",
        (
            "market hours",
            "market_hours",
            "regular hours",
            "trading hours",
            "pre-market",
            "pre market",
            "after-hours",
            "after hours",
            "extended hours",
            "overnight",
        ),
    ),
    (
        "leverage",
        (
            "leverage",
            "margin",
            "cash only",
            "cash-only",
            "max_leverage",
        ),
    ),
    (
        "position_size",
        (
            "position size",
            "position-size",
            "position_size",
            "size cap",
            "size limit",
            "per-trade risk",
            "per trade risk",
            "trade risk",
            "max_trade_risk_pct",
            "concentration",
            "max_ticker_concentration_pct",
            "ticker concentration",
        ),
    ),
)

# Change-type keyword map. Note: "allow <TICKER>" + whitelist context
# resolves to `add`; "drop" + market_hours context resolves to `remove`.
_CHANGE_TYPE_KEYWORDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("widen", ("widen", "loosen", "relax", "raise", "increase", "raise to", "bump")),
    ("tighten", ("tighten", "restrict", "lower", "reduce", "decrease", "cut")),
    ("add", ("add", "allow", "enable", "include", "permit")),
    ("remove", ("remove", "drop", "disallow", "disable", "exclude", "revoke")),
)

# Codex R3 P2: accept lowercase ticker input. Keith's NL asks are
# free-form; `allow aapl on the whitelist` is a common real-world
# pattern that the prior all-caps regex would silently reject. We
# match case-insensitively here and uppercase the result at the
# extraction site.
_TICKER_RE = re.compile(r"\b([A-Za-z]{1,5}(?:\.[A-Za-z]{1,3})?)\b")

# Shared stopword set for ticker extraction. English filler / command
# words that the `\b[A-Za-z]{1,5}\b` regex would otherwise capture as
# ticker candidates. Kept intentionally narrow: real 1-3 letter
# tickers (SPY, NVDA, AAPL, etc.) must survive this filter, so we only
# exclude words that function as commands, articles, or prepositions
# in the NL pattern Keith actually uses. Both `_extract_ticker` and
# `_extract_all_tickers` read from this set -- they used to maintain
# independent copies, which drifted (Codex R4 P2a: `FROM` landed in
# _extract_ticker's set but not _extract_all_tickers'). One set, one
# source of truth.
_TICKER_STOPWORDS: frozenset[str] = frozenset(
    {
        # Commands the skill may see in NL asks.
        "ADD", "USE", "SET", "ALLOW", "ENABLE", "DROP", "REMOVE",
        "TIGHTEN", "WIDEN", "RAISE", "CUT", "PERMIT", "INCLUDE",
        "LOWER", "REDUCE", "BUMP", "LOOSEN", "RELAX", "RESTRICT",
        "INCREASE", "DECREASE",
        # Domain / numeric units that shape up as candidate tokens.
        "NAV", "DAY", "DAYS", "PCT", "USD", "YEAR",
        # English prepositions / articles / conjunctions.
        "A", "AN", "ON", "TO", "FOR", "IN", "OF", "THE", "AND", "OR",
        "BUT", "FROM", "WITH", "AT", "BY", "AS", "ANY",
        # Pronouns.
        "I", "WE", "IT", "THAT", "THIS", "IS", "BE", "MY", "HIS",
        "HER", "ARE", "WAS", "HAS",
        # Domain vocabulary the skill routes via rule keywords, not
        # ticker extraction.
        "GUARD", "HOURS", "MARKET", "RISK", "SIZE", "CAP", "NEXT",
        "LIST", "LIMIT", "PER", "TRADE",
    }
)
_PCT_RE = re.compile(r"(\d+(?:\.\d+)?)\s*%")
_FLOAT_RE = re.compile(r"(\d+(?:\.\d+)?)")
_LEVERAGE_MULT_RE = re.compile(r"(\d+(?:\.\d+)?)\s*x\b", re.IGNORECASE)


def _detect_rule(text_lower: str) -> str | None:
    """Return the rule whose keyword set best matches `text_lower`.

    Ties broken by earlier position in `_RULE_KEYWORDS` (specificity
    order). Returns None if no keyword matched.
    """
    for rule, keywords in _RULE_KEYWORDS:
        for kw in keywords:
            if kw in text_lower:
                return rule
    return None


def _detect_change_type(text_lower: str) -> str | None:
    for change, keywords in _CHANGE_TYPE_KEYWORDS:
        for kw in keywords:
            if re.search(rf"\b{re.escape(kw)}\b", text_lower):
                return change
    return None


def _extract_ticker(text: str) -> str | None:
    """Extract the first ticker-shaped token, if any.

    Ignores the common English filler words that happen to be all-caps
    (ADD, USE, SET, ...). We conservatively check against a small
    stopword set rather than a dictionary to keep the dependency
    footprint zero.
    """
    for match in _TICKER_RE.finditer(text):
        # Codex R3 P2: normalise to uppercase before stopword check
        # and return. Keith can type `allow aapl`, `allow AAPL`, or
        # `allow Aapl` and land the same ticker.
        candidate = match.group(1).upper()
        if candidate in _TICKER_STOPWORDS:
            continue
        return candidate
    return None


def _extract_all_tickers(text: str) -> list[str]:
    """Extract every ticker-shaped token, not just the first.

    Codex R4 P2a: whitelist asks with multiple tickers (`allow AAPL and
    MSFT on the whitelist`) must clarify rather than silently author a
    single-ticker proposal. Callers use this helper to detect the
    multi-ticker case and route to a Clarification.
    """
    found: list[str] = []
    seen: set[str] = set()
    for match in _TICKER_RE.finditer(text):
        candidate = match.group(1).upper()
        if candidate in _TICKER_STOPWORDS or candidate in seen:
            continue
        seen.add(candidate)
        found.append(candidate)
    return found


def _detect_multi_rule(text_lower: str) -> list[str]:
    """Return every distinct rule whose keywords appear in the text.

    Codex R4 P2b: `widen leverage to 2x and daily risk to 8%` mentions
    TWO rules. Single-change MVP proposals must clarify instead of
    silently authoring only the last-parsed rule.

    Ordering mirrors `_RULE_KEYWORDS` so callers can present Keith the
    rules in the same specificity order the parser resolves them.
    """
    matched: list[str] = []
    for rule, keywords in _RULE_KEYWORDS:
        if rule in matched:
            continue
        for kw in keywords:
            if kw in text_lower:
                matched.append(rule)
                break
    return matched


def _extract_numeric_target(text: str, *, rule: str) -> float | None:
    """Parse a numeric target from the user's ask.

    - `N%` -> N/100 (percent)
    - `Nx` -> N (leverage multiplier)
    - `N.M` or `N` -> float
    - Prefers the FIRST match so "widen 1% to 2%" returns 0.01; the
      caller combines that with the "before" slot from config to detect
      a specific target. When the ask says "widen X to Y", the rightmost
      numeric is the intended AFTER value; we heuristically take the
      last match in "to Y" phrasing.

    Return None when no numeric token is present.
    """
    # Prefer the "to N%" / "to Nx" phrasing -- that's always the target.
    to_pct = re.search(r"\bto\s+(\d+(?:\.\d+)?)\s*%", text, re.IGNORECASE)
    if to_pct:
        return float(to_pct.group(1)) / 100.0
    to_x = re.search(r"\bto\s+(\d+(?:\.\d+)?)\s*x\b", text, re.IGNORECASE)
    if to_x:
        return float(to_x.group(1))
    to_plain = re.search(r"\bto\s+(\d+(?:\.\d+)?)\b", text, re.IGNORECASE)
    if to_plain:
        val = float(to_plain.group(1))
        # When the target is written "to 2" (plain) on a percent-valued
        # rule, assume it's a percent value with `%` elided. This is a
        # heuristic; tests pin the expected behaviour.
        if rule in {"position_size", "trade_risk"} and val > 1.0:
            return val / 100.0
        return val

    # No "to N" phrasing -- try a bare percent/multiplier.
    pct = _PCT_RE.search(text)
    if pct:
        return float(pct.group(1)) / 100.0
    mult = _LEVERAGE_MULT_RE.search(text)
    if mult:
        return float(mult.group(1))
    return None


def _disambiguate_position_size_field(
    text_lower: str, target: float | None
) -> str | None:
    """Pick between max_trade_risk_pct and max_ticker_concentration_pct.

    Rules (preemptive decision #2):
      - Explicit "concentration" / "ticker concentration" -> concentration
      - Explicit "trade risk" / "per-trade" -> trade_risk
      - Target value >= 0.05 (5%) -> concentration (per-trade rarely that high)
      - Target value < 0.05 and a target is given -> trade_risk
      - Otherwise: None (caller should ask)
    """
    if "concentration" in text_lower or "ticker concentration" in text_lower:
        return "max_ticker_concentration_pct"
    if (
        "trade risk" in text_lower
        or "per-trade" in text_lower
        or "per trade" in text_lower
        or "max_trade_risk_pct" in text_lower
    ):
        return "max_trade_risk_pct"
    if target is not None:
        if target >= _POSITION_SIZE_THRESHOLD:
            return "max_ticker_concentration_pct"
        return "max_trade_risk_pct"
    return None


def _extract_config_value(config_text: str, rule: str, field_name: str) -> object:
    """Extract the current value of `rule.field_name` from config_text.

    Naive line-based parse sufficient for the Phase-2 config shape.
    Returns the scalar (float, bool, int, or list) the field carries.
    Raises ConfigReadError if the field is missing, so the caller can
    surface a clear "config has drifted from the proposal matrix" error.
    """
    # Known-list fields get routed to list extraction directly -- their
    # scalar line carries an empty value on the `<field>:` line and the
    # items live on indented `-` lines below. Routing the scalar regex
    # at this case would return `''`, tripping the non-list guard.
    if (rule, field_name) == ("instrument_whitelist", "symbols"):
        return _extract_yaml_list(config_text, rule, field_name)

    lines = config_text.splitlines()
    in_rule = False
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            if in_rule and line and not line.startswith((" ", "\t")) and ":" in line:
                in_rule = False
            continue
        if not line.startswith((" ", "\t")):
            in_rule = stripped.rstrip(":") == rule
            continue
        if not in_rule:
            continue
        m = re.match(
            rf"\s+{re.escape(field_name)}\s*:\s*(.*?)\s*(?:#.*)?$", line
        )
        if m:
            raw = m.group(1).strip()
            return _coerce_yaml_scalar(raw)
    raise ConfigReadError(
        f"field {rule}.{field_name} not found in current config.yaml"
    )


def _coerce_yaml_scalar(raw: str) -> object:
    if raw.lower() == "true":
        return True
    if raw.lower() == "false":
        return False
    if raw.startswith('"') and raw.endswith('"'):
        return raw[1:-1]
    try:
        return int(raw)
    except ValueError:
        pass
    try:
        return float(raw)
    except ValueError:
        pass
    return raw


def _extract_yaml_list(
    config_text: str, rule: str, field_name: str
) -> list[str]:
    """Extract a YAML list value like `symbols:\n  - SPY\n  - AAPL`."""
    lines = config_text.splitlines()
    in_rule = False
    in_field = False
    out: list[str] = []
    for line in lines:
        if not line.startswith((" ", "\t")) and line.strip():
            in_rule = line.strip().rstrip(":") == rule
            in_field = False
            continue
        if in_rule:
            m = re.match(
                rf"\s+{re.escape(field_name)}\s*:\s*(?:#.*)?$", line
            )
            if m:
                in_field = True
                continue
            m_inline = re.match(
                rf"\s+{re.escape(field_name)}\s*:\s*\[(.*?)\]\s*(?:#.*)?$",
                line,
            )
            if m_inline:
                body = m_inline.group(1).strip()
                if not body:
                    return []
                return [t.strip().strip('"') for t in body.split(",") if t.strip()]
            if in_field:
                m_item = re.match(r"\s+-\s+(\S.*?)\s*(?:#.*)?$", line)
                if m_item:
                    out.append(m_item.group(1).strip().strip('"'))
                    continue
                if line.strip() and not line.strip().startswith("#"):
                    # End of list.
                    in_field = False
    return out


def parse_nl(text: str, config_text: str) -> ParsedDelta | Clarification:
    """Parse Keith's natural-language ask into a ParsedDelta or Clarification.

    Phase-2 MVP scope: the four supported (rule, change_type) matrices
    from §5.2 plus instrument_whitelist add/remove. Anything outside
    this matrix returns a Clarification naming the supported matrix so
    Keith can restate.
    """
    clean = text.strip()
    if not clean:
        return Clarification(
            question=(
                "Please describe the validator change you'd like to propose. "
                "Supported matrix: position_size | trade_risk | leverage | "
                "market_hours widen/tighten, instrument_whitelist add/remove."
            )
        )
    lower = clean.lower()

    # Codex R4 P2b: batched multi-rule asks land as a Clarification. A
    # single limits-proposal file encodes exactly one (rule, change_type)
    # pair; silently parsing one rule and dropping the others corrupts
    # Keith's mental model of what the approved file will apply.
    matched_rules = _detect_multi_rule(lower)
    if len(matched_rules) > 1:
        return Clarification(
            question=(
                f"The ask mentions more than one validator rule "
                f"({', '.join(matched_rules)}). A single limits-proposal "
                f"file encodes exactly one change. Please split the ask "
                f"into separate /propose-limits calls, one per rule."
            ),
            options=tuple(matched_rules),
        )

    rule = _detect_rule(lower)
    change_type = _detect_change_type(lower)

    # Codex R4 P2a: batched multi-ticker whitelist asks land as a
    # Clarification. `allow AAPL and MSFT on whitelist` would otherwise
    # silently author an AAPL-only proposal.
    if rule == "instrument_whitelist":
        tickers = _extract_all_tickers(clean)
        if len(tickers) > 1:
            return Clarification(
                question=(
                    f"The ask mentions more than one ticker "
                    f"({', '.join(tickers)}). A single limits-proposal "
                    f"file handles one ticker per add/remove; please "
                    f"split into separate /propose-limits calls."
                ),
                options=tuple(tickers),
            )

    # Fall-throughs / disambiguations.
    if rule is None and change_type is None:
        return Clarification(
            question=(
                "I couldn't resolve that to a supported validator change. "
                "Supported matrix: position_size widen/tighten, trade_risk "
                "widen/tighten, leverage widen/tighten, market_hours "
                "widen/remove, instrument_whitelist add/remove."
            )
        )
    if rule is None:
        return Clarification(
            question=(
                f"Which validator should I {change_type}? "
                f"Options: position_size, trade_risk, leverage, "
                f"market_hours, instrument_whitelist."
            ),
            options=tuple(sorted(VALID_RULES)),
        )

    # Heuristic: "tighten risk" (no qualifier) is structurally ambiguous
    # between position_size and trade_risk. Only resolve when the user
    # qualifies "daily/portfolio/total/open" or "per-trade/position".
    if (
        rule == "position_size"
        and change_type in {"widen", "tighten"}
        and "position" not in lower
        and "per-trade" not in lower
        and "per trade" not in lower
        and "concentration" not in lower
        and "size" not in lower
        and "trade risk" in lower
    ):
        return Clarification(
            question=(
                '"trade risk" is ambiguous: did you mean '
                "position_size.max_trade_risk_pct (per-trade) or "
                "trade_risk.max_open_risk_pct (portfolio-level daily risk)?"
            ),
            options=("position_size", "trade_risk"),
        )

    if change_type is None:
        return Clarification(
            question=(
                f"Should I widen, tighten, add to, or remove from "
                f"{rule}? Supported change types for this rule depend "
                f"on the rule itself."
            ),
            options=tuple(sorted(VALID_CHANGE_TYPES)),
        )

    # Rule-specific synonym remapping: "allow" reads as `add` by default
    # but should map to `widen` for market_hours (opens a window).
    # `widen` / `tighten` on the whitelist map to `add` / `remove`.
    #
    # Codex R1 F2: do NOT remap `tighten` -> `remove` for market_hours.
    # Those are semantically opposite: `tighten` means close windows
    # (flags from true to false), `remove` means open windows. Conflating
    # them inverted the user's intent into a risk-widening change.
    # `tighten` for market_hours is handled below as the close-windows
    # path.
    if rule == "market_hours" and change_type == "add":
        change_type = "widen"
    if rule == "instrument_whitelist" and change_type == "widen":
        change_type = "add"
    if rule == "instrument_whitelist" and change_type == "tighten":
        change_type = "remove"

    # Per-rule validity matrix -- narrow the change_type enum to the ops
    # that actually make sense for each rule. `tighten` on market_hours
    # is allowed and maps to closing currently-open window flags.
    allowed_by_rule = {
        "position_size": {"widen", "tighten"},
        "trade_risk": {"widen", "tighten"},
        "leverage": {"widen", "tighten"},
        "market_hours": {"widen", "remove", "tighten"},
        "instrument_whitelist": {"add", "remove"},
    }[rule]
    if change_type not in allowed_by_rule:
        return Clarification(
            question=(
                f"change_type {change_type!r} is not supported for "
                f"{rule}. Allowed: {sorted(allowed_by_rule)}."
            ),
            options=tuple(sorted(allowed_by_rule)),
        )

    target = _extract_numeric_target(clean, rule=rule)
    ticker = _extract_ticker(clean) if rule == "instrument_whitelist" else None

    # Dispatch per-rule.
    if rule == "position_size":
        return _build_position_size_delta(
            change_type, target, config_text, lower
        )
    if rule == "trade_risk":
        return _build_trade_risk_delta(change_type, target, config_text)
    if rule == "leverage":
        return _build_leverage_delta(change_type, target, config_text)
    if rule == "market_hours":
        return _build_market_hours_delta(change_type, lower, config_text)
    if rule == "instrument_whitelist":
        return _build_whitelist_delta(change_type, ticker, config_text)
    # Unreachable -- _detect_rule only yields known values.
    raise ProposalError(f"unreachable: unknown rule {rule!r}")


def _build_position_size_delta(
    change_type: str,
    target: float | None,
    config_text: str,
    text_lower: str,
) -> ParsedDelta | Clarification:
    field_name = _disambiguate_position_size_field(text_lower, target)
    if field_name is None:
        return Clarification(
            question=(
                "position_size has two caps: max_trade_risk_pct "
                "(per-trade risk) and max_ticker_concentration_pct "
                "(per-ticker concentration). Which did you mean?"
            ),
            options=("max_trade_risk_pct", "max_ticker_concentration_pct"),
        )
    try:
        before = _extract_config_value(config_text, "position_size", field_name)
    except ConfigReadError as exc:
        raise ProposalError(str(exc)) from exc
    if target is None:
        return Clarification(
            question=(
                f"What target value should I set {field_name} to? "
                f"Current value is {before!r}."
            )
        )
    after = target
    if not isinstance(before, (int, float)):
        raise ProposalError(
            f"position_size.{field_name} is non-numeric in config.yaml: "
            f"{before!r}"
        )
    if change_type == "widen" and after <= float(before):
        return Clarification(
            question=(
                f"widen requested but target {after} is not larger than "
                f"current {before}. Did you mean to tighten?"
            )
        )
    if change_type == "tighten" and after >= float(before):
        return Clarification(
            question=(
                f"tighten requested but target {after} is not smaller "
                f"than current {before}. Did you mean to widen?"
            )
        )
    return ParsedDelta(
        rule="position_size",
        change_type=change_type,
        field=field_name,
        before=before,
        after=after,
        summary=f"{field_name} {before} -> {after}",
    )


def _build_trade_risk_delta(
    change_type: str, target: float | None, config_text: str
) -> ParsedDelta | Clarification:
    field_name = "max_open_risk_pct"
    try:
        before = _extract_config_value(config_text, "trade_risk", field_name)
    except ConfigReadError as exc:
        raise ProposalError(str(exc)) from exc
    if target is None:
        return Clarification(
            question=(
                f"What target value should I set {field_name} to? "
                f"Current value is {before!r}."
            )
        )
    if not isinstance(before, (int, float)):
        raise ProposalError(
            f"trade_risk.{field_name} is non-numeric in config.yaml: "
            f"{before!r}"
        )
    if change_type == "widen" and target <= float(before):
        return Clarification(
            question=(
                f"widen requested but target {target} is not larger than "
                f"current {before}. Did you mean to tighten?"
            )
        )
    if change_type == "tighten" and target >= float(before):
        return Clarification(
            question=(
                f"tighten requested but target {target} is not smaller "
                f"than current {before}. Did you mean to widen?"
            )
        )
    return ParsedDelta(
        rule="trade_risk",
        change_type=change_type,
        field=field_name,
        before=before,
        after=target,
        summary=f"{field_name} {before} -> {target}",
    )


def _build_leverage_delta(
    change_type: str, target: float | None, config_text: str
) -> ParsedDelta | Clarification:
    field_name = "max_leverage"
    try:
        before = _extract_config_value(config_text, "leverage", field_name)
    except ConfigReadError as exc:
        raise ProposalError(str(exc)) from exc
    if target is None:
        return Clarification(
            question=(
                f"What target leverage multiplier should I set? "
                f"Current max_leverage is {before!r}. Note that widening "
                f"beyond 1.0 also flips cash_only to false."
            )
        )
    if not isinstance(before, (int, float)):
        raise ProposalError(
            f"leverage.{field_name} is non-numeric in config.yaml: {before!r}"
        )
    if change_type == "widen" and target <= float(before):
        return Clarification(
            question=(
                f"widen requested but target {target} is not larger than "
                f"current {before}."
            )
        )
    if change_type == "tighten" and target >= float(before):
        return Clarification(
            question=(
                f"tighten requested but target {target} is not smaller "
                f"than current {before}."
            )
        )
    return ParsedDelta(
        rule="leverage",
        change_type=change_type,
        field=field_name,
        before=before,
        after=target,
        summary=f"{field_name} {before} -> {target}",
    )


def _build_market_hours_delta(
    change_type: str, text_lower: str, config_text: str
) -> ParsedDelta | Clarification:
    # Map NL -> the two bools the market_hours section exposes.
    wants_pre = (
        "pre-market" in text_lower
        or "pre market" in text_lower
        or "allow_pre_market" in text_lower
    )
    wants_after = (
        "after-hours" in text_lower
        or "after hours" in text_lower
        or "allow_after_hours" in text_lower
    )
    wants_both = not wants_pre and not wants_after

    # Codex R1 F2: `tighten` closes currently-open windows; `widen` and
    # `remove` open them. Target value is derived from change_type, NOT
    # remapped across semantics.
    if change_type in {"widen", "remove"}:
        target_value = True
    elif change_type == "tighten":
        target_value = False
    else:
        raise ProposalError(
            f"internal: market_hours does not support {change_type!r}"
        )

    fields: list[tuple[str, bool, bool]] = []
    if wants_pre or wants_both:
        try:
            current = _extract_config_value(
                config_text, "market_hours", "allow_pre_market"
            )
        except ConfigReadError as exc:
            raise ProposalError(str(exc)) from exc
        if not isinstance(current, bool):
            raise ProposalError(
                f"market_hours.allow_pre_market is non-bool in "
                f"config.yaml: {current!r}"
            )
        fields.append(("allow_pre_market", bool(current), target_value))
    if wants_after or wants_both:
        try:
            current = _extract_config_value(
                config_text, "market_hours", "allow_after_hours"
            )
        except ConfigReadError as exc:
            raise ProposalError(str(exc)) from exc
        if not isinstance(current, bool):
            raise ProposalError(
                f"market_hours.allow_after_hours is non-bool in "
                f"config.yaml: {current!r}"
            )
        fields.append(("allow_after_hours", bool(current), target_value))

    # Collapse to a single ParsedDelta when only one field moves; emit a
    # composite when two fields move together. For both-fields case, we
    # encode "before" as a dict so render_proposal + build_yaml_patch
    # handle the multi-line slice.
    moving = [(name, before, after) for (name, before, after) in fields if before != after]
    if not moving:
        return Clarification(
            question=(
                f"market_hours fields are already at target "
                f"{'true' if target_value else 'false'}. Nothing to do."
            )
        )
    if len(moving) == 1:
        name, before, after = moving[0]
        return ParsedDelta(
            rule="market_hours",
            change_type=change_type,
            field=name,
            before=before,
            after=after,
            summary=f"{name} {before} -> {after}",
        )
    before_dict = {name: before for (name, before, _) in moving}
    after_dict = {name: after for (name, _, after) in moving}
    return ParsedDelta(
        rule="market_hours",
        change_type=change_type,
        field="+".join(name for (name, _, _) in moving),
        before=before_dict,
        after=after_dict,
        summary=", ".join(
            f"{name} {before} -> {after}" for (name, before, after) in moving
        ),
    )


def _build_whitelist_delta(
    change_type: str, ticker: str | None, config_text: str
) -> ParsedDelta | Clarification:
    if ticker is None:
        return Clarification(
            question=(
                f"Which ticker should I {change_type} to/from the "
                f"instrument_whitelist? Example: 'allow AAPL on whitelist'."
            )
        )
    try:
        before_list = _extract_config_value(
            config_text, "instrument_whitelist", "symbols"
        )
    except ConfigReadError as exc:
        raise ProposalError(str(exc)) from exc
    if not isinstance(before_list, list):
        raise ProposalError(
            f"instrument_whitelist.symbols is non-list in config.yaml: "
            f"{before_list!r}"
        )
    normalized = [str(t).upper() for t in before_list]
    ticker_upper = ticker.upper()
    if change_type == "add":
        if ticker_upper in normalized:
            return Clarification(
                question=(
                    f"{ticker_upper} is already on the whitelist. "
                    f"Nothing to do."
                )
            )
        after_list = normalized + [ticker_upper]
    else:
        if ticker_upper not in normalized:
            return Clarification(
                question=(
                    f"{ticker_upper} is not currently on the whitelist. "
                    f"Nothing to remove."
                )
            )
        after_list = [t for t in normalized if t != ticker_upper]
    return ParsedDelta(
        rule="instrument_whitelist",
        change_type=change_type,
        field="symbols",
        before=normalized,
        after=after_list,
        ticker=ticker_upper,
        summary=(
            f"{change_type} {ticker_upper} "
            f"{'to' if change_type == 'add' else 'from'} symbols"
        ),
    )


# ---------- safety-impact templates (§5.2, deterministic) ----------


def compute_safety_impact(delta: ParsedDelta, config_text: str = "") -> str:
    """Return the deterministic safety-impact paragraph for this delta.

    Four templates map to §5.2's four heuristic categories:
      A. Widening size / risk / leverage caps
      B. Adding to instrument_whitelist
      C. Dropping / widening market_hours guard
      D. Tightening limits
      E. Removing from instrument_whitelist (tightens access)

    No LLM improvisation on safety-critical text (preemptive decision #1).
    """
    delta.validate()
    rule = delta.rule
    change = delta.change_type

    if rule == "instrument_whitelist" and change == "add":
        return (
            f"Neutral on aggregate risk. This only ENABLES trading "
            f"{delta.ticker}; no order fires until the strategy-approval "
            f"flow signs off on a strategy that references it. Existing "
            f"validators (position_size, trade_risk, leverage, "
            f"market_hours) still apply."
        )

    if rule == "instrument_whitelist" and change == "remove":
        return (
            f"Tightens access. Removes {delta.ticker} from the traded "
            f"universe; any open positions remain but no top-ups or new "
            f"entries on this ticker will pass validation. Existing "
            f"strategy specs referencing {delta.ticker} will fail at "
            f"next engine load until the ticker is re-added or the "
            f"strategy is retired."
        )

    if rule == "market_hours" and change in {"widen", "remove"}:
        opened: list[str] = []
        if isinstance(delta.after, dict):
            for k, v in delta.after.items():
                if v is True:
                    opened.append(k)
        elif delta.after is True and isinstance(delta.field, str):
            opened.append(delta.field)
        opened_label = ", ".join(opened) or "extended-hours windows"
        return (
            f"RISKY. Opens {opened_label}. Phase 2 default is cash-only "
            f"regular hours for a reason: overnight / extended-hours "
            f"fills on gap-ups or thin liquidity can blow through "
            f"stop_loss levels. Engine will place orders during the "
            f"newly-opened windows; validators no longer block them."
        )

    # Numeric widening (position_size / trade_risk / leverage)
    if change == "widen":
        try:
            before_f = float(delta.before)  # type: ignore[arg-type]
            after_f = float(delta.after)  # type: ignore[arg-type]
            factor = after_f / before_f if before_f else float("inf")
        except (TypeError, ValueError):
            before_f = after_f = factor = float("nan")

        if rule == "position_size" and delta.field == "max_trade_risk_pct":
            return (
                f"Increases per-trade loss ceiling from {_fmt_pct(before_f)} "
                f"to {_fmt_pct(after_f)} of NAV ({_fmt_factor(factor)}). "
                f"Max simultaneous exposure at portfolio-wide adverse "
                f"co-movement scales proportionally. Review alongside "
                f"trade_risk.max_open_risk_pct to confirm the aggregate "
                f"ceiling still clears this per-trade cap at the number "
                f"of concurrent open positions you actually run."
            )
        if (
            rule == "position_size"
            and delta.field == "max_ticker_concentration_pct"
        ):
            return (
                f"Increases per-ticker concentration cap from "
                f"{_fmt_pct(before_f)} to {_fmt_pct(after_f)} of NAV "
                f"({_fmt_factor(factor)}). A single-ticker adverse move "
                f"now impacts a larger share of NAV; diversification "
                f"across tickers is correspondingly reduced."
            )
        if rule == "trade_risk":
            return (
                f"Increases portfolio-level open-risk ceiling from "
                f"{_fmt_pct(before_f)} to {_fmt_pct(after_f)} of NAV "
                f"({_fmt_factor(factor)}). More concurrent positions "
                f"can sit at full-stop-loss exposure simultaneously; "
                f"a coordinated drawdown event now hurts proportionally "
                f"more."
            )
        if rule == "leverage":
            # Codex R1 F4: only claim a `cash_only` flip when the current
            # config actually has cash_only=true and the widen would push
            # max_leverage past 1.0 (the threshold at which _patch_leverage_widen
            # also flips cash_only). Otherwise the safety text lies.
            cash_only_state: object = None
            try:
                cash_only_state = _extract_config_value(
                    config_text, "leverage", "cash_only"
                )
            except ConfigReadError:
                cash_only_state = None
            if cash_only_state is True and after_f > 1.0:
                cash_only_note = (
                    "This also flips cash_only from true to false, so "
                    "margin utilization enters the picture. "
                )
            elif cash_only_state is False:
                cash_only_note = (
                    "cash_only is already false; this only raises the "
                    "ceiling, it does not introduce margin use. "
                )
            else:
                # MiniMax R4 F5: cash_only field absent from config.
                # Silently omitting the note would mislead Keith into
                # thinking the flip is settled; flag the gap instead.
                cash_only_note = (
                    "[cash_only field absent from config; the YAML "
                    "Patch will not touch it, and the engine default "
                    "applies. Audit config.yaml before landing.] "
                )
            return (
                f"RAISES leverage ceiling from {before_f}x to {after_f}x "
                f"({_fmt_factor(factor)}). {cash_only_note}Phase 2 "
                f"default is cash-only (leverage=1.0); widening past "
                f"1.0 departs from the MVP-safety posture. Revisit the "
                f"broker's initial + maintenance margin rules before "
                f"landing this."
            )

    if change == "tighten":
        # Numeric tightening template.
        return (
            f"Safer by definition. {delta.field} moves from "
            f"{delta.before!r} to {delta.after!r}. Existing open "
            f"positions are NOT force-closed by a tightening change; "
            f"validators reject any top-ups that would push a position "
            f"above the new cap, so a portfolio currently at the old "
            f"limit will drift into compliance as positions are closed "
            f"or rebalanced. Review any active strategies that sized "
            f"against the looser cap."
        )

    # Fall-through: shouldn't happen for the MVP matrix, but keep honest.
    return (
        f"{rule}:{change} delta recorded. No pre-canned safety template "
        f"matches this (rule, change_type); surface to Keith for a "
        f"manual safety review before landing."
    )


def _fmt_pct(value: float) -> str:
    if value != value:  # NaN
        return "unknown"
    return f"{value * 100:.2f}%"


def _fmt_factor(factor: float) -> str:
    if factor != factor or factor == float("inf"):
        return "ratio undefined"
    return f"{factor:.2f}x larger"


# ---------- slug + YAML patch ----------


def compute_slug(delta: ParsedDelta) -> str:
    """Deterministic slug for the proposal filename (preemptive decision #3)."""
    delta.validate()
    parts = [delta.rule, delta.change_type]
    if delta.ticker:
        parts.append(delta.ticker)
    return _SLUG_SEP.join(parts)


def build_yaml_patch(
    delta: ParsedDelta, config_text: str
) -> tuple[str, str]:
    """Compute (before_text, after_text) YAML-patch blocks.

    Extracts the minimal unique slice of `config_text` covering the
    target field(s), returns it as `before_text` (byte-for-byte), and
    synthesises `after_text` by substituting the new value(s).

    For whitelist add/remove and market_hours multi-field moves, the
    slice covers the entire mapping body so the textual replacement
    remains uniquely identifiable in config.yaml.
    """
    delta.validate()

    if delta.rule == "instrument_whitelist":
        return _patch_whitelist(delta, config_text)
    if delta.rule == "market_hours":
        return _patch_market_hours(delta, config_text)
    if delta.rule == "leverage" and delta.change_type == "widen":
        return _patch_leverage_widen(delta, config_text)

    # Numeric single-field rules (position_size, trade_risk, leverage
    # tighten).
    section = _extract_rule_body(config_text, delta.rule)
    if delta.field is None:
        raise ProposalError(
            f"internal: ParsedDelta.field is None for numeric rule {delta.rule}"
        )
    line = _find_field_line(section, delta.field)
    if line is None:
        raise ConfigReadError(
            f"field {delta.rule}.{delta.field} line not found for patching"
        )
    before_val = _format_value_matching(line, delta.before)
    after_val = _format_value_matching(line, delta.after)
    if before_val is None or after_val is None:
        raise ProposalError(
            f"could not format value(s) for {delta.field}"
        )
    after_line = _swap_value_in_line(line, before_val, after_val)
    if line == after_line:
        raise ProposalError(
            f"before and after lines are identical for {delta.field}"
        )
    return line, after_line


def _patch_whitelist(
    delta: ParsedDelta, config_text: str
) -> tuple[str, str]:
    section = _extract_rule_body(config_text, "instrument_whitelist")
    before_slice = _extract_symbols_block(section)
    if before_slice is None:
        raise ConfigReadError(
            "instrument_whitelist.symbols block not found for patching"
        )
    indent = _detect_symbols_indent(before_slice)
    after_list = delta.after
    if not isinstance(after_list, list):
        raise ProposalError(
            f"whitelist after is not a list: {after_list!r}"
        )
    after_slice = _render_symbols_block(after_list, indent)
    if before_slice == after_slice:
        raise ProposalError(
            "whitelist patch is a no-op (before==after)"
        )
    return before_slice, after_slice


def _patch_market_hours(
    delta: ParsedDelta, config_text: str
) -> tuple[str, str]:
    if isinstance(delta.after, dict) and isinstance(delta.before, dict):
        fields = list(delta.before.keys())
    elif isinstance(delta.field, str):
        fields = [delta.field]
    else:
        raise ProposalError(
            "market_hours patch requires field or dict payload"
        )
    section = _extract_rule_body(config_text, "market_hours")
    before_lines: list[str] = []
    after_lines: list[str] = []
    for f in fields:
        line = _find_field_line(section, f)
        if line is None:
            raise ConfigReadError(
                f"market_hours.{f} line not found for patching"
            )
        if isinstance(delta.before, dict):
            b_val = delta.before[f]
            a_val = delta.after[f]
        else:
            b_val = delta.before
            a_val = delta.after
        before_val = _format_value_matching(line, b_val)
        after_val = _format_value_matching(line, a_val)
        if before_val is None or after_val is None:
            raise ProposalError(
                f"could not format value(s) for market_hours.{f}"
            )
        before_lines.append(line)
        after_lines.append(_swap_value_in_line(line, before_val, after_val))
    # Concatenate contiguous field lines; the handler does textual
    # find-and-replace, so we need a slice that actually appears as-is
    # in config_text. Use the raw config text between the first and
    # last field to preserve comments / blank lines.
    first = before_lines[0]
    last = before_lines[-1]
    first_idx = config_text.find(first)
    last_idx = config_text.find(last, first_idx)
    if first_idx < 0 or last_idx < 0:
        raise ConfigReadError(
            "market_hours field lines not located uniquely in config.yaml"
        )
    slice_end = last_idx + len(last)
    before_slice = config_text[first_idx:slice_end]
    after_slice = before_slice
    # Order matters: apply the longest before-line first to avoid
    # partial collisions. Here each before_line is distinct per our
    # field extraction, so the order is irrelevant.
    for b, a in zip(before_lines, after_lines):
        after_slice = after_slice.replace(b, a, 1)
    if before_slice == after_slice:
        raise ProposalError("market_hours patch is a no-op")
    return before_slice, after_slice


def _patch_leverage_widen(
    delta: ParsedDelta, config_text: str
) -> tuple[str, str]:
    """Build the YAML patch for a leverage widen.

    Codex R2 F5: the cash_only flip is conditional on BOTH the current
    config state (cash_only must be true) AND the widen target crossing
    above 1.0. A widen that stays at or below 1.0 (e.g. from 0.5 to
    0.8) must NOT flip cash_only, matching what the safety-impact text
    promises in that branch.
    """
    section = _extract_rule_body(config_text, "leverage")
    max_line = _find_field_line(section, "max_leverage")
    if max_line is None:
        raise ConfigReadError(
            "leverage.max_leverage not found for patching"
        )
    before_max_val = _format_value_matching(max_line, delta.before)
    after_max_val = _format_value_matching(max_line, delta.after)
    if before_max_val is None or after_max_val is None:
        raise ProposalError("could not format leverage values")
    after_max = _swap_value_in_line(max_line, before_max_val, after_max_val)

    # Decide whether to flip cash_only. Only when the current config
    # has `cash_only: true` AND the widen crosses strictly above 1.0.
    try:
        current_cash = _extract_config_value(
            config_text, "leverage", "cash_only"
        )
    except ConfigReadError:
        current_cash = None
    try:
        target_f = float(delta.after)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        target_f = float("nan")
    should_flip_cash = (
        current_cash is True and target_f > 1.0
    )

    if not should_flip_cash:
        # Patch max_leverage only. Keep the slice narrow so the search-
        # and-replace locates it uniquely.
        before_slice = max_line
        after_slice = after_max
        if before_slice == after_slice:
            raise ProposalError("leverage widen patch is a no-op")
        return before_slice, after_slice

    # Multi-line slice covering both cash_only and max_leverage.
    cash_line = _find_field_line(section, "cash_only")
    if cash_line is None:
        raise ConfigReadError(
            "leverage.cash_only not found for patching (required for "
            "widen past 1.0 from cash-only)"
        )
    cash_idx = config_text.find(cash_line)
    max_idx = config_text.find(max_line, cash_idx)
    if cash_idx < 0 or max_idx < 0 or max_idx < cash_idx:
        raise ConfigReadError(
            "leverage field lines not in expected order in config.yaml"
        )
    slice_end = max_idx + len(max_line)
    before_slice = config_text[cash_idx:slice_end]
    # Codex R5 P2: route the bool tokens through _format_value_matching
    # so the casing of the cash_only literal on the existing line (true
    # | True | TRUE) is preserved on both sides of the swap. Hard-coded
    # "true"/"false" would produce a no-op patch on a valid but
    # non-lowercase config.
    cash_before_tok = _format_value_matching(cash_line, True) or "true"
    cash_after_tok = _format_value_matching(cash_line, False) or "false"
    after_cash = _swap_value_in_line(
        cash_line, cash_before_tok, cash_after_tok
    )
    after_slice = before_slice.replace(cash_line, after_cash, 1).replace(
        max_line, after_max, 1
    )
    if before_slice == after_slice:
        raise ProposalError("leverage widen patch is a no-op")
    return before_slice, after_slice


def _extract_rule_body(config_text: str, rule: str) -> str:
    """Return the text between `<rule>:` and the next top-level key or EOF."""
    lines = config_text.splitlines(keepends=True)
    start_idx = -1
    for i, line in enumerate(lines):
        if line.startswith((" ", "\t")):
            continue
        if line.strip().rstrip(":") == rule:
            start_idx = i
            break
    if start_idx < 0:
        raise ConfigReadError(
            f"rule {rule!r} not found in config.yaml"
        )
    end_idx = len(lines)
    for j in range(start_idx + 1, len(lines)):
        line = lines[j]
        if line and not line.startswith((" ", "\t")) and line.strip():
            end_idx = j
            break
    return "".join(lines[start_idx:end_idx])


def _find_field_line(section: str, field_name: str) -> str | None:
    """Return the full line (including trailing newline if present) for `field_name`."""
    for line in section.splitlines(keepends=True):
        stripped = line.strip()
        if stripped.startswith(f"{field_name}:") or stripped.startswith(
            f"{field_name} :"
        ):
            return line.rstrip("\n").rstrip("\r")
    return None


def _format_value_matching(line: str, value: object) -> str | None:
    """Format `value` so it substitutes cleanly into `line`.

    For floats whose textual representation in `line` uses `0.01`-style
    decimal form, return a matching decimal. Bool values match the
    existing token's casing so `_swap_value_in_line` (case-sensitive
    replace) lands cleanly against `True`/`False`/`TRUE`/`FALSE` as well
    as the canonical lowercase. String values quoted with `"..."` pick
    up quotes.
    """
    if isinstance(value, bool):
        # Codex R5 P2: preserve the existing token's casing when the
        # config line uses a non-lowercase YAML boolean. YAML accepts
        # `true`/`True`/`TRUE` (same for false); emitting a lowercase
        # token while `_swap_value_in_line` does a case-sensitive
        # replace would produce a no-op patch against a valid but
        # capitalised config.
        m = re.search(r":\s*(True|TRUE|False|FALSE|true|false)\b", line)
        if m:
            token = m.group(1)
            truthy = token.lower() == "true"
            if truthy == value:
                return token
            if token.istitle():
                return "True" if value else "False"
            if token.isupper():
                return "TRUE" if value else "FALSE"
        return "true" if value else "false"
    if isinstance(value, float):
        # Prefer the raw config token when we can locate it; tests pin
        # the match to config_text.
        m = re.search(r":\s*([^#\s]+)", line)
        if m:
            token = m.group(1).strip()
            # Use the token's decimal precision when the current value
            # matches it numerically; else fall back to a short format.
            try:
                cur = float(token)
                if abs(cur - value) < 1e-12:
                    return token
            except ValueError:
                pass
        # Canonical decimal; avoids scientific notation for MVP ranges.
        formatted = f"{value:.10g}"
        return formatted
    if isinstance(value, int):
        return str(value)
    if isinstance(value, str):
        if line.count('"') >= 2:
            return f'"{value}"'
        return value
    return None


def _swap_value_in_line(line: str, before_val: str, after_val: str) -> str:
    """Replace the value token in `line` and update a trailing percent comment.

    A single `line.replace(before_val, after_val, 1)` handles the YAML
    scalar itself. When the comment contains `N%` and `before_val` is
    a decimal, the percent text is also rewritten so comments don't lie
    after the swap.
    """
    out = line.replace(before_val, after_val, 1)
    # Percent comment rewrite, best-effort.
    try:
        bf = float(before_val)
        af = float(after_val)
    except ValueError:
        return out
    pct_re = re.compile(r"(\d+(?:\.\d+)?)\s*%")
    def _sub(m: re.Match[str]) -> str:
        current = float(m.group(1))
        if abs(current - bf * 100) < 1e-6:
            return f"{_fmt_comment_pct(af * 100)}%"
        return m.group(0)
    out = pct_re.sub(_sub, out, count=1)
    return out


def _fmt_comment_pct(value: float) -> str:
    """Render a percent value for a comment (strip trailing zeroes)."""
    if value == int(value):
        return str(int(value))
    return f"{value:g}"


def _extract_symbols_block(section: str) -> str | None:
    """Find the `symbols:` key + its nested list in `section`."""
    lines = section.splitlines(keepends=True)
    start_idx = -1
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped == "symbols:" or stripped.startswith("symbols:"):
            start_idx = i
            break
    if start_idx < 0:
        return None
    # Inline form: `symbols: [SPY]`
    if re.match(r"\s+symbols\s*:\s*\[", lines[start_idx]):
        return lines[start_idx].rstrip("\n").rstrip("\r")
    # Block form: following `  - ITEM` lines, same indent or deeper.
    base_indent = len(lines[start_idx]) - len(lines[start_idx].lstrip(" "))
    end_idx = start_idx + 1
    while end_idx < len(lines):
        line = lines[end_idx]
        if not line.strip():
            break
        leading = len(line) - len(line.lstrip(" "))
        if leading <= base_indent:
            break
        end_idx += 1
    block = "".join(lines[start_idx:end_idx])
    return block.rstrip("\n")


def _detect_symbols_indent(symbols_block: str) -> str:
    """Infer the `- ITEM` indent from a block-form symbols block."""
    for line in symbols_block.splitlines():
        m = re.match(r"^( +)-\s", line)
        if m:
            return m.group(1)
    return "    "


def _render_symbols_block(items: list[str], indent: str) -> str:
    if not items:
        # Use an inline empty list; keeps YAML valid and avoids dangling
        # indentation that might collide with the next top-level key.
        return "  symbols: []"
    lines = ["  symbols:"]
    for sym in items:
        lines.append(f"{indent}- {sym}")
    return "\n".join(lines)


# ---------- markdown rendering ----------


def render_proposal(
    *,
    delta: ParsedDelta,
    rationale: str,
    safety_impact: str,
    yaml_patch: tuple[str, str],
    date_iso: str,
    summary: str | None = None,
) -> str:
    """Render the full limits-proposal markdown per spec §2.3."""
    delta.validate()
    heading_summary = summary or delta.summary or compute_slug(delta)
    before_block, after_block = yaml_patch
    change_yaml = _render_change_yaml(delta)
    lines = [
        "---",
        "tags: [review, strategy-approvals, limits-proposal]",
        f"date: {date_iso}",
        "type: limits-proposal",
        "origin: keith",
        "status: proposed",
        f"applies-to: {CONFIG_APPLIES_TO}",
        'up: "[[index]]"',
        "---",
        "",
        f"# Limits Proposal: {heading_summary}",
        "",
        "## Change",
        "",
        "```yaml",
        change_yaml.rstrip("\n"),
        "```",
        "",
        "## Rationale (Keith's)",
        "",
        rationale.strip() or "_not provided_",
        "",
        "## Safety Impact (skill's assessment)",
        "",
        safety_impact.strip(),
        "",
        "## YAML Patch",
        "",
        "before:",
        "",
        "```yaml",
        before_block.rstrip("\n"),
        "```",
        "",
        "after:",
        "",
        "```yaml",
        after_block.rstrip("\n"),
        "```",
        "",
        "## Approval",
        "",
        "Pending Keith's review. Apply via "
        "`/invest-ship --approve-limits <path>`.",
        "",
    ]
    return "\n".join(lines)


def _render_change_yaml(delta: ParsedDelta) -> str:
    """Serialize the `## Change` block body."""
    lines = [f"rule: {delta.rule}", f"change_type: {delta.change_type}"]
    if delta.ticker:
        lines.append(f"ticker: {delta.ticker}")
    if delta.field:
        lines.append(f"field: {delta.field}")
    lines.append(f"before: {_render_yaml_scalar(delta.before)}")
    lines.append(f"after: {_render_yaml_scalar(delta.after)}")
    return "\n".join(lines)


def _render_yaml_scalar(value: object) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return f"{value:g}" if isinstance(value, float) else str(value)
    if isinstance(value, list):
        if not value:
            return "[]"
        return "[" + ", ".join(str(v) for v in value) + "]"
    if isinstance(value, dict):
        if not value:
            return "{}"
        return (
            "{"
            + ", ".join(f"{k}: {_render_yaml_scalar(v)}" for k, v in value.items())
            + "}"
        )
    if isinstance(value, str):
        return value
    return repr(value)


# ---------- file writer ----------


def write_proposal(
    repo: Path,
    content: str,
    *,
    slug: str,
    date_iso: str | None = None,
    overwrite: bool = False,
) -> Path:
    """Write the proposal to `review/strategy-approvals/` atomically.

    Returns the path written. Does NOT touch config.yaml.

    Codex R1 F3: refuse to overwrite an existing proposal file by
    default. Same-day reruns on the same slug would otherwise clobber
    the earlier proposal -- including any manual rationale edits, or an
    already-approved audit record. Pass `overwrite=True` to allow
    replacement (CLI exposes this via --overwrite).
    """
    if date_iso is None:
        date_iso = _date.today().isoformat()
    dest = repo / APPROVALS_DIR
    dest.mkdir(parents=True, exist_ok=True)
    path = dest / f"{date_iso}_limits-proposal_{slug}.md"
    data_bytes = (
        content if content.endswith("\n") else content + "\n"
    ).encode("utf-8")
    # Codex R2 F6: use O_CREAT|O_EXCL-equivalent semantics (os.link
    # inside _atomic_write) when overwrite is False. This closes the
    # TOCTOU window between `path.exists()` and `os.replace` that would
    # otherwise let two concurrent writers each clobber the other.
    try:
        _atomic_write(path, data_bytes, exclusive=not overwrite)
    except FileExistsError as exc:
        raise ProposalError(
            f"proposal file already exists: {path}. Refusing to "
            f"overwrite (either a prior proposal under the same date + "
            f"slug, or a concurrent writer beat this call). Delete the "
            f"existing file, pass --overwrite, or re-run with a "
            f"different date or slug."
        ) from exc
    return path


def _atomic_write(path: Path, data: bytes, *, exclusive: bool = False) -> None:
    """Atomic file write via tempfile + os.replace (or os.link).

    Hard-rule invariant check: reject any write target under
    `execution/validators/config.yaml`. The skill MUST NOT touch that
    file; if caller somehow routed a config-path through here, fail
    loudly before the fs touches the target. Belt-and-braces vs. the
    skill-level discipline; catches refactors that silently reroute
    the write path.

    R1-minimax F5: guard is a three-component tail check
    (`execution/validators/config.yaml`), not two, so a symlink at
    `execution/validators -> /tmp/evil/validators` where `config.yaml`
    resolves through `/tmp/evil/validators/config.yaml` does not
    bypass. The canonical resolved path must contain all three parts
    at the tail.
    """
    # Codex R1 F1: macOS + Windows filesystems are case-insensitive;
    # `Path.resolve()` preserves case, and `os.path.normcase` on macOS
    # is a no-op (only Windows lowercases). A mixed-case spelling like
    # `Execution/Validators/config.yaml` would therefore pass a
    # case-sensitive tail check while still pointing at the protected
    # file. Force-lowercase the tail comparison so the guard fires on
    # ANY spelling, regardless of OS. K2Bi never legitimately writes to
    # a path ending in the tail tuple under any casing, so the extra
    # strictness on case-sensitive filesystems has no false-positive
    # surface in practice.
    abs_path = path.resolve()
    parts_cf = tuple(p.lower() for p in abs_path.parts)
    tail = ("execution", "validators", "config.yaml")
    if len(parts_cf) >= 3 and parts_cf[-3:] == tail:
        raise ProposalError(
            f"invest-propose-limits refuses to write to {path}: "
            f"config.yaml is owned by /invest-ship --approve-limits, "
            f"never by this skill (spec §5.4)."
        )
    parent = path.parent
    parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{path.name}.tmp.", dir=str(parent)
    )
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        if exclusive:
            # Codex R2 F6: atomic create-if-not-exists via os.link.
            # os.link raises FileExistsError if the target already
            # exists; no TOCTOU window. Caller (write_proposal) is
            # responsible for catching FileExistsError and surfacing
            # a skill-level ProposalError.
            #
            # MiniMax R4 F4: on filesystems that do not support hard
            # links (FAT32 USB drives, some network mounts), os.link
            # raises OSError with errno EXDEV/EPERM/ENOTSUP. K2Bi's
            # MacBook + Mac Mini both run APFS so this branch is
            # defensive; falling through to os.replace + a pre-check
            # loses the TOCTOU guarantee but beats a cryptic crash.
            import errno as _errno

            try:
                os.link(str(tmp), str(path))
                tmp.unlink()
            except FileExistsError:
                raise
            except OSError as link_exc:
                if link_exc.errno in (
                    _errno.EXDEV,
                    _errno.EPERM,
                    _errno.ENOTSUP,
                    _errno.EOPNOTSUPP,
                ):
                    if path.exists():
                        raise FileExistsError(
                            f"[Errno {_errno.EEXIST}] File exists: {path}"
                        ) from link_exc
                    os.replace(tmp, path)
                else:
                    raise
        else:
            os.replace(tmp, path)
    except Exception:
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass
        raise


# ---------- top-level pipeline ----------


def build_and_write(
    repo: Path,
    text: str,
    *,
    rationale: str,
    date_iso: str | None = None,
    summary: str | None = None,
    config_path: Path | None = None,
    overwrite: bool = False,
) -> dict:
    """Parse NL, build delta + safety impact, render markdown, write file.

    Returns a dict summary of what was written. On Clarification, raises
    ProposalError with the clarification message so the CLI surfaces it
    to Keith via stderr + exit code 1.
    """
    resolved_config = config_path or (
        repo / "execution" / "validators" / "config.yaml"
    )
    if not resolved_config.is_absolute():
        resolved_config = (repo / resolved_config).resolve()
    if not resolved_config.exists():
        raise ProposalError(
            f"config.yaml not found at {resolved_config}"
        )
    config_text = resolved_config.read_text(encoding="utf-8")
    parsed = parse_nl(text, config_text)
    if isinstance(parsed, Clarification):
        raise ProposalError(f"clarification-needed: {parsed.question}")
    parsed.validate()
    slug = compute_slug(parsed)
    safety_impact = compute_safety_impact(parsed, config_text)
    yaml_patch = build_yaml_patch(parsed, config_text)
    date_iso = date_iso or _date.today().isoformat()
    markdown = render_proposal(
        delta=parsed,
        rationale=rationale,
        safety_impact=safety_impact,
        yaml_patch=yaml_patch,
        date_iso=date_iso,
        summary=summary,
    )
    path = write_proposal(
        repo, markdown, slug=slug, date_iso=date_iso, overwrite=overwrite
    )
    return {
        "path": str(path),
        "slug": slug,
        "rule": parsed.rule,
        "change_type": parsed.change_type,
        "field": parsed.field,
        "ticker": parsed.ticker,
        "before": parsed.before,
        "after": parsed.after,
        "summary": parsed.summary,
        "safety_impact": safety_impact,
    }


# ---------- CLI ----------


def _cli_parse(args: argparse.Namespace) -> int:
    repo = Path(args.repo).resolve()
    # Codex R3 P3: rebase relative --config-path overrides onto --repo,
    # matching build_and_write's behaviour. `parse --repo /x --config-path
    # execution/validators/config.yaml` should resolve the same way
    # `write --repo /x` does with no override.
    if args.config_path:
        config_path = Path(args.config_path)
        if not config_path.is_absolute():
            config_path = (repo / config_path).resolve()
    else:
        config_path = repo / "execution" / "validators" / "config.yaml"
    if not config_path.exists():
        print(f"error: config.yaml not found at {config_path}", file=sys.stderr)
        return 1
    config_text = config_path.read_text(encoding="utf-8")
    parsed = parse_nl(args.text, config_text)
    if isinstance(parsed, Clarification):
        payload = {
            "kind": "clarification",
            "question": parsed.question,
            "options": list(parsed.options),
        }
        json.dump(payload, sys.stdout, indent=2, sort_keys=True)
        sys.stdout.write("\n")
        return 0
    try:
        parsed.validate()
    except ProposalError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    slug = compute_slug(parsed)
    safety_impact = compute_safety_impact(parsed, config_text)
    try:
        yaml_patch = build_yaml_patch(parsed, config_text)
    except ProposalError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    payload = {
        "kind": "parsed",
        "rule": parsed.rule,
        "change_type": parsed.change_type,
        "field": parsed.field,
        "ticker": parsed.ticker,
        "before": _jsonable(parsed.before),
        "after": _jsonable(parsed.after),
        "summary": parsed.summary,
        "slug": slug,
        "safety_impact": safety_impact,
        "yaml_patch_before": yaml_patch[0],
        "yaml_patch_after": yaml_patch[1],
    }
    json.dump(payload, sys.stdout, indent=2, sort_keys=True)
    sys.stdout.write("\n")
    return 0


def _cli_write(args: argparse.Namespace) -> int:
    repo = Path(args.repo).resolve()
    try:
        result = build_and_write(
            repo,
            args.text,
            rationale=args.rationale,
            date_iso=args.date,
            summary=args.summary,
            overwrite=args.overwrite,
        )
    except ProposalError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    json.dump(
        {"kind": "written", **{k: _jsonable(v) for k, v in result.items()}},
        sys.stdout,
        indent=2,
        sort_keys=True,
    )
    sys.stdout.write("\n")
    return 0


def _jsonable(value: object) -> object:
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if isinstance(value, list):
        return [_jsonable(v) for v in value]
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    return repr(value)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="propose_limits",
        description=(
            "invest-propose-limits helper: parse NL -> structured delta, "
            "render + write a limits-proposal markdown under "
            "review/strategy-approvals/."
        ),
    )
    parser.add_argument(
        "--repo",
        default=".",
        help="repo root (defaults to cwd)",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_parse = sub.add_parser(
        "parse", help="parse NL input; print JSON result"
    )
    p_parse.add_argument("--text", required=True, help="Keith's NL ask")
    p_parse.add_argument(
        "--config-path",
        default=None,
        help="override config.yaml path (default: execution/validators/config.yaml)",
    )

    p_write = sub.add_parser(
        "write", help="parse + write the limits-proposal file"
    )
    p_write.add_argument("--text", required=True, help="Keith's NL ask")
    p_write.add_argument(
        "--rationale",
        required=True,
        help="Keith's stated reason for the change (goes under ## Rationale)",
    )
    p_write.add_argument("--summary", default=None)
    p_write.add_argument("--date", default=None, help="override YYYY-MM-DD")
    p_write.add_argument(
        "--overwrite",
        action="store_true",
        help=(
            "Replace an existing same-day same-slug proposal. Default: "
            "refuse and tell caller to move/rename the stale file."
        ),
    )

    args = parser.parse_args(argv)
    if args.cmd == "parse":
        return _cli_parse(args)
    if args.cmd == "write":
        return _cli_write(args)
    parser.print_usage(sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
