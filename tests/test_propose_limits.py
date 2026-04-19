"""Tests for scripts/lib/propose_limits.py -- Bundle 3 cycle 6.

Coverage map (spec §8.4 + preemptive-decision verification):

- NL parser: 20-combination matrix + ambiguity -> Clarification
- Safety-impact templates: four §5.2 categories emit deterministic text
- Slug derivation: <rule>-<change_type>[-<ticker>]
- YAML patch: extracted before-block appears once in config.yaml, after
  differs from before
- Markdown renderer: spec §2.3 frontmatter + body sections present
- File writer: correct filename + refuses config.yaml path (hard rule)
- Integration: generated file is consumed successfully by cycle 5's
  handle_approve_limits handler (the critical contract test)
- CLI: parse / write subcommands behave
"""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import textwrap
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.lib import propose_limits as pl  # noqa: E402
from scripts.lib import invest_ship_strategy as iss  # noqa: E402


DEFAULT_CONFIG_TEXT = textwrap.dedent(
    """\
    # Pre-trade validator config.
    #
    # File-based; engine restart required to change.

    position_size:
      # Per-trade risk cap as fraction of portfolio NAV.
      max_trade_risk_pct: 0.01  # 1% per trade per Ahern / research consensus
      # Per-ticker concentration cap.
      max_ticker_concentration_pct: 0.20  # 20%

    trade_risk:
      # Portfolio-level risk ceiling summed across open + pending orders.
      max_open_risk_pct: 0.05  # 5% of NAV at risk at any time

    leverage:
      # MVP: cash-only. Engine refuses any margin use.
      cash_only: true
      max_leverage: 1.0

    market_hours:
      # US Eastern.
      regular_open: "09:30"
      regular_close: "16:00"
      # Extended-hours off by default for MVP.
      allow_pre_market: false
      allow_after_hours: false

    instrument_whitelist:
      # Only tickers in this list can be traded.
      symbols:
        - SPY
    """
)


def _tmp_repo(
    tmp: TemporaryDirectory, *, config_text: str = DEFAULT_CONFIG_TEXT
) -> Path:
    repo = Path(tmp.name)
    cfg_dir = repo / "execution" / "validators"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    (cfg_dir / "config.yaml").write_text(config_text, encoding="utf-8")
    return repo


# ---------- NL parser ----------


class ParseNlMatrixTests(unittest.TestCase):
    """Every (rule, change_type) in the supported matrix resolves to a
    ParsedDelta. Ambiguity routes to a Clarification."""

    def setUp(self) -> None:
        self.cfg = DEFAULT_CONFIG_TEXT

    def test_widen_position_size_concentration_to_25pct(self) -> None:
        result = pl.parse_nl(
            "widen position size cap to 25%", self.cfg
        )
        self.assertIsInstance(result, pl.ParsedDelta)
        assert isinstance(result, pl.ParsedDelta)
        self.assertEqual(result.rule, "position_size")
        self.assertEqual(result.change_type, "widen")
        self.assertEqual(result.field, "max_ticker_concentration_pct")
        self.assertAlmostEqual(float(result.after), 0.25)

    def test_widen_position_size_per_trade_to_2pct(self) -> None:
        result = pl.parse_nl(
            "widen per-trade risk to 2%", self.cfg
        )
        self.assertIsInstance(result, pl.ParsedDelta)
        assert isinstance(result, pl.ParsedDelta)
        self.assertEqual(result.rule, "position_size")
        self.assertEqual(result.field, "max_trade_risk_pct")
        self.assertAlmostEqual(float(result.after), 0.02)

    def test_tighten_daily_risk(self) -> None:
        result = pl.parse_nl(
            "tighten daily risk to 3%", self.cfg
        )
        self.assertIsInstance(result, pl.ParsedDelta)
        assert isinstance(result, pl.ParsedDelta)
        self.assertEqual(result.rule, "trade_risk")
        self.assertEqual(result.change_type, "tighten")
        self.assertEqual(result.field, "max_open_risk_pct")
        self.assertAlmostEqual(float(result.after), 0.03)

    def test_widen_trade_risk_to_8pct(self) -> None:
        result = pl.parse_nl(
            "widen portfolio risk to 8%", self.cfg
        )
        self.assertIsInstance(result, pl.ParsedDelta)
        assert isinstance(result, pl.ParsedDelta)
        self.assertEqual(result.rule, "trade_risk")
        self.assertEqual(result.change_type, "widen")

    def test_widen_leverage_to_2x(self) -> None:
        result = pl.parse_nl("widen leverage to 2x", self.cfg)
        self.assertIsInstance(result, pl.ParsedDelta)
        assert isinstance(result, pl.ParsedDelta)
        self.assertEqual(result.rule, "leverage")
        self.assertEqual(result.change_type, "widen")
        self.assertEqual(result.field, "max_leverage")
        self.assertAlmostEqual(float(result.after), 2.0)

    def test_remove_market_hours_guard(self) -> None:
        result = pl.parse_nl("drop market_hours guard", self.cfg)
        self.assertIsInstance(result, pl.ParsedDelta)
        assert isinstance(result, pl.ParsedDelta)
        self.assertEqual(result.rule, "market_hours")
        self.assertEqual(result.change_type, "remove")

    def test_widen_market_hours_pre_market_only(self) -> None:
        result = pl.parse_nl("allow pre-market trading", self.cfg)
        self.assertIsInstance(result, pl.ParsedDelta)
        assert isinstance(result, pl.ParsedDelta)
        self.assertEqual(result.rule, "market_hours")
        # `allow` is an add keyword but market_hours maps that to widen.
        # We accept either `widen` or the skill's chosen synonym as long
        # as the semantics match. Both change_types land the same edit.
        self.assertIn(result.change_type, {"widen", "add"})

    def test_add_ticker_to_whitelist(self) -> None:
        result = pl.parse_nl("allow AAPL on the whitelist", self.cfg)
        self.assertIsInstance(result, pl.ParsedDelta)
        assert isinstance(result, pl.ParsedDelta)
        self.assertEqual(result.rule, "instrument_whitelist")
        self.assertEqual(result.change_type, "add")
        self.assertEqual(result.ticker, "AAPL")

    def test_add_lowercase_ticker_normalized(self) -> None:
        """Codex R3 P2: lowercase ticker input ('allow aapl') resolves
        to the same ParsedDelta as uppercase ('allow AAPL')."""
        result = pl.parse_nl("allow aapl on the whitelist", self.cfg)
        self.assertIsInstance(result, pl.ParsedDelta)
        assert isinstance(result, pl.ParsedDelta)
        self.assertEqual(result.ticker, "AAPL")

    def test_remove_mixedcase_ticker_normalized(self) -> None:
        result = pl.parse_nl("remove Spy from whitelist", self.cfg)
        self.assertIsInstance(result, pl.ParsedDelta)
        assert isinstance(result, pl.ParsedDelta)
        self.assertEqual(result.ticker, "SPY")

    def test_remove_ticker_from_whitelist(self) -> None:
        result = pl.parse_nl("remove SPY from whitelist", self.cfg)
        self.assertIsInstance(result, pl.ParsedDelta)
        assert isinstance(result, pl.ParsedDelta)
        self.assertEqual(result.rule, "instrument_whitelist")
        self.assertEqual(result.change_type, "remove")
        self.assertEqual(result.ticker, "SPY")


class ParseNlBoundaryTests(unittest.TestCase):
    """R1-minimax F1: explicit tests for the 5% position_size routing
    threshold and matrix completeness across all supported (rule,
    change_type) tuples. A regression in the threshold-based routing
    would otherwise land silently."""

    def setUp(self) -> None:
        self.cfg = DEFAULT_CONFIG_TEXT

    def test_position_size_at_5pct_threshold_routes_concentration(self) -> None:
        """Value exactly at threshold routes to concentration (>= 5%).
        Use a config where concentration is low enough for widen to 5%
        to be a real widen direction."""
        low_cfg = DEFAULT_CONFIG_TEXT.replace(
            "max_ticker_concentration_pct: 0.20",
            "max_ticker_concentration_pct: 0.03",
        )
        result = pl.parse_nl("widen size cap to 5%", low_cfg)
        self.assertIsInstance(result, pl.ParsedDelta)
        assert isinstance(result, pl.ParsedDelta)
        self.assertEqual(result.rule, "position_size")
        self.assertEqual(result.field, "max_ticker_concentration_pct")

    def test_position_size_just_below_threshold_routes_per_trade(self) -> None:
        """Value < 5% routes to per-trade (threshold = 5% strict)."""
        result = pl.parse_nl("widen size cap to 4.9%", self.cfg)
        self.assertIsInstance(result, pl.ParsedDelta)
        assert isinstance(result, pl.ParsedDelta)
        self.assertEqual(result.rule, "position_size")
        self.assertEqual(result.field, "max_trade_risk_pct")

    def test_position_size_explicit_per_trade_keyword_overrides_magnitude(
        self,
    ) -> None:
        """Explicit 'per-trade' keyword wins over magnitude heuristic."""
        result = pl.parse_nl(
            "widen per-trade risk to 10%", self.cfg
        )
        self.assertIsInstance(result, pl.ParsedDelta)
        assert isinstance(result, pl.ParsedDelta)
        self.assertEqual(result.field, "max_trade_risk_pct")

    def test_position_size_explicit_concentration_keyword_tighten(
        self,
    ) -> None:
        """Explicit 'concentration' keyword routes to concentration even
        on a tighten operation (target < current) where magnitude-only
        routing would pick per-trade (target 2% < 5% threshold)."""
        result = pl.parse_nl(
            "tighten concentration cap to 2%", self.cfg
        )
        self.assertIsInstance(result, pl.ParsedDelta)
        assert isinstance(result, pl.ParsedDelta)
        self.assertEqual(result.field, "max_ticker_concentration_pct")

    def test_tighten_position_size_trade_risk(self) -> None:
        """Complete the (position_size, tighten, max_trade_risk_pct) tuple."""
        result = pl.parse_nl(
            "tighten per-trade risk to 0.5%", self.cfg
        )
        self.assertIsInstance(result, pl.ParsedDelta)
        assert isinstance(result, pl.ParsedDelta)
        self.assertEqual(result.rule, "position_size")
        self.assertEqual(result.change_type, "tighten")
        self.assertEqual(result.field, "max_trade_risk_pct")

    def test_tighten_position_size_concentration(self) -> None:
        """(position_size, tighten, max_ticker_concentration_pct) tuple."""
        result = pl.parse_nl(
            "tighten concentration cap to 15%", self.cfg
        )
        self.assertIsInstance(result, pl.ParsedDelta)
        assert isinstance(result, pl.ParsedDelta)
        self.assertEqual(result.field, "max_ticker_concentration_pct")

    def test_tighten_leverage(self) -> None:
        """(leverage, tighten) tuple is legal even though MVP default is 1.0."""
        cfg = DEFAULT_CONFIG_TEXT.replace(
            "max_leverage: 1.0", "max_leverage: 2.0"
        ).replace("cash_only: true", "cash_only: false")
        result = pl.parse_nl("tighten leverage to 1.5x", cfg)
        self.assertIsInstance(result, pl.ParsedDelta)
        assert isinstance(result, pl.ParsedDelta)
        self.assertEqual(result.rule, "leverage")
        self.assertEqual(result.change_type, "tighten")

    def test_widen_market_hours_after_hours_only(self) -> None:
        result = pl.parse_nl("allow after-hours trading", self.cfg)
        self.assertIsInstance(result, pl.ParsedDelta)
        assert isinstance(result, pl.ParsedDelta)
        self.assertEqual(result.rule, "market_hours")
        # Only the after_hours field should move.
        self.assertEqual(result.field, "allow_after_hours")

    def test_tighten_market_hours_closes_windows(self) -> None:
        """Codex R1 F2: `tighten market_hours` must mean CLOSE windows,
        not open them. The prior implementation remapped tighten -> remove
        which opened windows -- inverting the user's intent into a
        risk-widening change. Regression lock."""
        cfg_open = DEFAULT_CONFIG_TEXT.replace(
            "allow_pre_market: false", "allow_pre_market: true"
        ).replace(
            "allow_after_hours: false", "allow_after_hours: true"
        )
        result = pl.parse_nl("tighten market_hours guard", cfg_open)
        self.assertIsInstance(result, pl.ParsedDelta)
        assert isinstance(result, pl.ParsedDelta)
        self.assertEqual(result.change_type, "tighten")
        # Both windows currently open; tighten moves both to false.
        if isinstance(result.after, dict):
            for v in result.after.values():
                self.assertFalse(v)
        else:
            self.assertFalse(result.after)

    def test_tighten_market_hours_when_already_closed_asks(self) -> None:
        """Tightening already-closed windows is a no-op; clarify."""
        result = pl.parse_nl("tighten market_hours guard", self.cfg)
        self.assertIsInstance(result, pl.Clarification)


class ParseNlClarificationTests(unittest.TestCase):
    """Ambiguous or out-of-matrix inputs return Clarification, not ParsedDelta."""

    def setUp(self) -> None:
        self.cfg = DEFAULT_CONFIG_TEXT

    def test_empty_input_asks(self) -> None:
        result = pl.parse_nl("", self.cfg)
        self.assertIsInstance(result, pl.Clarification)

    def test_tighten_risk_is_ambiguous(self) -> None:
        result = pl.parse_nl("tighten trade risk", self.cfg)
        self.assertIsInstance(result, pl.Clarification)
        assert isinstance(result, pl.Clarification)
        self.assertIn("ambiguous", result.question.lower())

    def test_tighten_position_size_without_value_asks(self) -> None:
        result = pl.parse_nl("tighten position size", self.cfg)
        self.assertIsInstance(result, pl.Clarification)

    def test_widen_to_smaller_value_asks(self) -> None:
        """'widen' but the target is smaller than current -> clarify."""
        result = pl.parse_nl(
            "widen max_trade_risk_pct to 0.005", self.cfg
        )
        self.assertIsInstance(result, pl.Clarification)

    def test_tighten_to_larger_value_asks(self) -> None:
        result = pl.parse_nl(
            "tighten max_trade_risk_pct to 5%", self.cfg
        )
        self.assertIsInstance(result, pl.Clarification)

    def test_multiple_tickers_asks(self) -> None:
        """Codex R4 P2a: batched whitelist ask with >1 ticker must
        clarify rather than silently drop all but the first."""
        result = pl.parse_nl(
            "allow AAPL and MSFT on the whitelist", self.cfg
        )
        self.assertIsInstance(result, pl.Clarification)
        assert isinstance(result, pl.Clarification)
        self.assertIn("more than one ticker", result.question)
        self.assertIn("AAPL", result.question)
        self.assertIn("MSFT", result.question)

    def test_multiple_rules_asks(self) -> None:
        """Codex R4 P2b: ask mentioning >1 rule must clarify rather
        than silently drop all but the last-parsed."""
        result = pl.parse_nl(
            "widen leverage to 2x and daily risk to 8%", self.cfg
        )
        self.assertIsInstance(result, pl.Clarification)
        assert isinstance(result, pl.Clarification)
        self.assertIn("more than one validator rule", result.question)

    def test_add_whitelist_without_ticker_asks(self) -> None:
        result = pl.parse_nl("add a ticker to whitelist", self.cfg)
        self.assertIsInstance(result, pl.Clarification)

    def test_add_whitelist_already_present_asks(self) -> None:
        result = pl.parse_nl("add SPY to whitelist", self.cfg)
        self.assertIsInstance(result, pl.Clarification)
        assert isinstance(result, pl.Clarification)
        self.assertIn("already", result.question.lower())

    def test_remove_whitelist_missing_ticker_asks(self) -> None:
        result = pl.parse_nl("remove AAPL from whitelist", self.cfg)
        self.assertIsInstance(result, pl.Clarification)
        assert isinstance(result, pl.Clarification)
        self.assertIn("not currently", result.question.lower())

    def test_market_hours_already_open_asks(self) -> None:
        cfg_open = DEFAULT_CONFIG_TEXT.replace(
            "allow_pre_market: false", "allow_pre_market: true"
        ).replace(
            "allow_after_hours: false", "allow_after_hours: true"
        )
        result = pl.parse_nl("drop market_hours guard", cfg_open)
        self.assertIsInstance(result, pl.Clarification)


# ---------- safety-impact ----------


class SafetyImpactTests(unittest.TestCase):
    """§5.2 heuristic templates emit deterministic text."""

    def test_widen_trade_risk_mentions_doubling(self) -> None:
        delta = pl.ParsedDelta(
            rule="trade_risk",
            change_type="widen",
            field="max_open_risk_pct",
            before=0.05,
            after=0.10,
        )
        text = pl.compute_safety_impact(delta)
        self.assertIn("5.00%", text)
        self.assertIn("10.00%", text)
        self.assertIn("2.00x", text)

    def test_widen_position_size_trade_risk_mentions_doubling(self) -> None:
        delta = pl.ParsedDelta(
            rule="position_size",
            change_type="widen",
            field="max_trade_risk_pct",
            before=0.01,
            after=0.02,
        )
        text = pl.compute_safety_impact(delta)
        self.assertIn("1.00%", text)
        self.assertIn("2.00%", text)
        self.assertIn("2.00x", text)

    def test_widen_position_size_concentration(self) -> None:
        delta = pl.ParsedDelta(
            rule="position_size",
            change_type="widen",
            field="max_ticker_concentration_pct",
            before=0.20,
            after=0.25,
        )
        text = pl.compute_safety_impact(delta)
        self.assertIn("concentration", text.lower())
        self.assertIn("20.00%", text)
        self.assertIn("25.00%", text)

    def test_leverage_widen_flags_missing_cash_only_field(self) -> None:
        """R4-minimax F5: if config.yaml has no `cash_only:` field,
        the safety text must explicitly flag the gap rather than
        silently omit the cash_only note."""
        cfg_no_cash_only = DEFAULT_CONFIG_TEXT.replace(
            "  cash_only: true\n", ""
        )
        delta = pl.ParsedDelta(
            rule="leverage",
            change_type="widen",
            field="max_leverage",
            before=1.0,
            after=2.0,
        )
        text = pl.compute_safety_impact(delta, cfg_no_cash_only)
        self.assertIn("cash_only field absent", text)

    def test_widen_leverage_mentions_cash_only_flip(self) -> None:
        """cash_only text only appears when config actually has
        cash_only=true (Codex R1 F4 honesty fix)."""
        delta = pl.ParsedDelta(
            rule="leverage",
            change_type="widen",
            field="max_leverage",
            before=1.0,
            after=2.0,
        )
        text = pl.compute_safety_impact(delta, DEFAULT_CONFIG_TEXT)
        self.assertIn("cash_only", text.lower())
        self.assertIn("margin", text.lower())

    def test_widen_leverage_full_safety_text(self) -> None:
        """R1-minimax F2 + Codex R1 F4: pin the leverage widen text when
        cash_only starts at `true` (the default MVP config)."""
        delta = pl.ParsedDelta(
            rule="leverage",
            change_type="widen",
            field="max_leverage",
            before=1.0,
            after=2.0,
        )
        text = pl.compute_safety_impact(delta, DEFAULT_CONFIG_TEXT)
        self.assertIn("RAISES leverage ceiling", text)
        self.assertIn("1.0x", text)
        self.assertIn("2.0x", text)
        self.assertIn("cash_only from true to false", text)
        self.assertIn("MVP-safety", text)

    def test_widen_leverage_no_false_flip_when_already_false(self) -> None:
        """Codex R1 F4: if config already has cash_only: false, the
        safety text must NOT claim a flip. Prevents the 'text lies about
        what the patch does' failure mode."""
        cfg = DEFAULT_CONFIG_TEXT.replace(
            "cash_only: true", "cash_only: false"
        ).replace("max_leverage: 1.0", "max_leverage: 2.0")
        delta = pl.ParsedDelta(
            rule="leverage",
            change_type="widen",
            field="max_leverage",
            before=2.0,
            after=3.0,
        )
        text = pl.compute_safety_impact(delta, cfg)
        self.assertNotIn("flips cash_only", text)
        self.assertIn("already false", text)


class LeverageSubOnePatchTests(unittest.TestCase):
    """Codex R2 F5: a leverage widen that stays <= 1.0 must NOT flip
    cash_only, even though the legacy patcher unconditionally did so.
    The patch and the safety-impact text must agree."""

    def test_widen_leverage_from_0_5_to_0_8_preserves_cash_only(
        self,
    ) -> None:
        cfg = DEFAULT_CONFIG_TEXT.replace(
            "max_leverage: 1.0", "max_leverage: 0.5"
        )
        delta = pl.ParsedDelta(
            rule="leverage",
            change_type="widen",
            field="max_leverage",
            before=0.5,
            after=0.8,
        )
        before_slice, after_slice = pl.build_yaml_patch(delta, cfg)
        # Cash_only line must be absent from the patched slice (we only
        # change max_leverage when widen stays <= 1.0).
        self.assertNotIn("cash_only", before_slice)
        self.assertNotIn("cash_only", after_slice)
        self.assertIn("0.5", before_slice)
        self.assertIn("0.8", after_slice)

    def test_widen_leverage_sub_one_safety_text_no_flip(self) -> None:
        cfg = DEFAULT_CONFIG_TEXT.replace(
            "max_leverage: 1.0", "max_leverage: 0.5"
        )
        delta = pl.ParsedDelta(
            rule="leverage",
            change_type="widen",
            field="max_leverage",
            before=0.5,
            after=0.8,
        )
        text = pl.compute_safety_impact(delta, cfg)
        # After is still <= 1.0, cash_only should not flip regardless of
        # current state.
        self.assertNotIn("flips cash_only", text)

    def test_widen_leverage_past_one_from_cash_only_flips(self) -> None:
        """The canonical path: cash_only=true + widen past 1.0 -> both
        lines move together, preserving the original MVP semantics."""
        cfg = DEFAULT_CONFIG_TEXT  # cash_only: true, max_leverage: 1.0
        delta = pl.ParsedDelta(
            rule="leverage",
            change_type="widen",
            field="max_leverage",
            before=1.0,
            after=2.0,
        )
        before_slice, after_slice = pl.build_yaml_patch(delta, cfg)
        self.assertIn("cash_only: true", before_slice)
        self.assertIn("cash_only: false", after_slice)
        self.assertIn("max_leverage: 1.0", before_slice)
        self.assertIn("max_leverage: 2", after_slice)

    def test_widen_position_size_per_trade_full_text(self) -> None:
        """R1-minimax F2: pin the position_size widen per-trade text."""
        delta = pl.ParsedDelta(
            rule="position_size",
            change_type="widen",
            field="max_trade_risk_pct",
            before=0.01,
            after=0.02,
        )
        text = pl.compute_safety_impact(delta)
        self.assertIn("per-trade loss ceiling", text)
        self.assertIn("1.00%", text)
        self.assertIn("2.00%", text)
        self.assertIn("max_open_risk_pct", text)

    def test_remove_market_hours_is_risky(self) -> None:
        delta = pl.ParsedDelta(
            rule="market_hours",
            change_type="remove",
            field="allow_pre_market+allow_after_hours",
            before={"allow_pre_market": False, "allow_after_hours": False},
            after={"allow_pre_market": True, "allow_after_hours": True},
        )
        text = pl.compute_safety_impact(delta)
        self.assertIn("RISKY", text)
        self.assertIn("allow_pre_market", text)

    def test_add_whitelist_is_neutral(self) -> None:
        delta = pl.ParsedDelta(
            rule="instrument_whitelist",
            change_type="add",
            field="symbols",
            before=["SPY"],
            after=["SPY", "AAPL"],
            ticker="AAPL",
        )
        text = pl.compute_safety_impact(delta)
        self.assertIn("Neutral", text)
        self.assertIn("AAPL", text)

    def test_remove_whitelist_tightens_access(self) -> None:
        delta = pl.ParsedDelta(
            rule="instrument_whitelist",
            change_type="remove",
            field="symbols",
            before=["SPY", "AAPL"],
            after=["SPY"],
            ticker="AAPL",
        )
        text = pl.compute_safety_impact(delta)
        self.assertIn("Tightens", text)
        self.assertIn("AAPL", text)

    def test_tighten_is_safer_by_definition(self) -> None:
        delta = pl.ParsedDelta(
            rule="trade_risk",
            change_type="tighten",
            field="max_open_risk_pct",
            before=0.05,
            after=0.03,
        )
        text = pl.compute_safety_impact(delta)
        self.assertIn("Safer", text)


# ---------- slug ----------


class SlugTests(unittest.TestCase):
    def test_slug_basic(self) -> None:
        delta = pl.ParsedDelta(
            rule="position_size",
            change_type="widen",
            field="max_trade_risk_pct",
            before=0.01,
            after=0.02,
        )
        self.assertEqual(pl.compute_slug(delta), "position_size-widen")

    def test_slug_with_ticker(self) -> None:
        delta = pl.ParsedDelta(
            rule="instrument_whitelist",
            change_type="add",
            field="symbols",
            before=["SPY"],
            after=["SPY", "AAPL"],
            ticker="AAPL",
        )
        self.assertEqual(
            pl.compute_slug(delta), "instrument_whitelist-add-AAPL"
        )


# ---------- YAML patch ----------


class YamlPatchTests(unittest.TestCase):
    def setUp(self) -> None:
        self.cfg = DEFAULT_CONFIG_TEXT

    def _assert_patch_valid(self, before: str, after: str) -> None:
        """The handler contract: before appears exactly once, after differs."""
        self.assertEqual(
            self.cfg.count(before), 1,
            f"before-block must appear exactly once in config; "
            f"got {self.cfg.count(before)} occurrences of {before!r}",
        )
        self.assertNotEqual(before, after)

    def test_widen_position_size_concentration(self) -> None:
        delta = pl.ParsedDelta(
            rule="position_size",
            change_type="widen",
            field="max_ticker_concentration_pct",
            before=0.20,
            after=0.25,
        )
        before, after = pl.build_yaml_patch(delta, self.cfg)
        self._assert_patch_valid(before, after)
        self.assertIn("0.25", after)

    def test_widen_per_trade_risk(self) -> None:
        delta = pl.ParsedDelta(
            rule="position_size",
            change_type="widen",
            field="max_trade_risk_pct",
            before=0.01,
            after=0.02,
        )
        before, after = pl.build_yaml_patch(delta, self.cfg)
        self._assert_patch_valid(before, after)
        self.assertIn("max_trade_risk_pct: 0.02", after)

    def test_widen_trade_risk(self) -> None:
        delta = pl.ParsedDelta(
            rule="trade_risk",
            change_type="widen",
            field="max_open_risk_pct",
            before=0.05,
            after=0.08,
        )
        before, after = pl.build_yaml_patch(delta, self.cfg)
        self._assert_patch_valid(before, after)
        self.assertIn("0.08", after)

    def test_widen_leverage_flips_cash_only(self) -> None:
        delta = pl.ParsedDelta(
            rule="leverage",
            change_type="widen",
            field="max_leverage",
            before=1.0,
            after=2.0,
        )
        before, after = pl.build_yaml_patch(delta, self.cfg)
        self._assert_patch_valid(before, after)
        self.assertIn("cash_only: false", after)
        self.assertIn("max_leverage: 2", after)

    def test_remove_market_hours_both_windows(self) -> None:
        delta = pl.ParsedDelta(
            rule="market_hours",
            change_type="remove",
            field="allow_pre_market+allow_after_hours",
            before={"allow_pre_market": False, "allow_after_hours": False},
            after={"allow_pre_market": True, "allow_after_hours": True},
        )
        before, after = pl.build_yaml_patch(delta, self.cfg)
        self._assert_patch_valid(before, after)
        self.assertIn("allow_pre_market: true", after)
        self.assertIn("allow_after_hours: true", after)

    def test_add_whitelist_ticker(self) -> None:
        delta = pl.ParsedDelta(
            rule="instrument_whitelist",
            change_type="add",
            field="symbols",
            before=["SPY"],
            after=["SPY", "AAPL"],
            ticker="AAPL",
        )
        before, after = pl.build_yaml_patch(delta, self.cfg)
        self._assert_patch_valid(before, after)
        self.assertIn("- AAPL", after)
        self.assertIn("- SPY", after)

    def test_market_hours_preserves_title_case_booleans(self) -> None:
        """Codex R5 P2: if config uses `True`/`False` (YAML-valid but
        non-canonical casing), the patch must preserve that casing so
        the case-sensitive search-and-replace lands."""
        cfg = DEFAULT_CONFIG_TEXT.replace(
            "allow_pre_market: false", "allow_pre_market: False"
        ).replace(
            "allow_after_hours: false", "allow_after_hours: False"
        )
        delta = pl.ParsedDelta(
            rule="market_hours",
            change_type="remove",
            field="allow_pre_market+allow_after_hours",
            before={"allow_pre_market": False, "allow_after_hours": False},
            after={"allow_pre_market": True, "allow_after_hours": True},
        )
        before, after = pl.build_yaml_patch(delta, cfg)
        # The before-block matched the title-case config line.
        self.assertIn("allow_pre_market: False", before)
        self.assertIn("allow_after_hours: False", before)
        # After-block preserves title-case.
        self.assertIn("allow_pre_market: True", after)
        self.assertIn("allow_after_hours: True", after)
        # The before-block appears exactly once in the config.
        self.assertEqual(cfg.count(before), 1)

    def test_leverage_preserves_uppercase_cash_only(self) -> None:
        cfg = DEFAULT_CONFIG_TEXT.replace(
            "cash_only: true", "cash_only: TRUE"
        )
        delta = pl.ParsedDelta(
            rule="leverage",
            change_type="widen",
            field="max_leverage",
            before=1.0,
            after=2.0,
        )
        before, after = pl.build_yaml_patch(delta, cfg)
        self.assertIn("cash_only: TRUE", before)
        self.assertIn("cash_only: FALSE", after)

    def test_market_hours_both_fields_same_before_value_flip(self) -> None:
        """R4-minimax F3: both allow_pre_market and allow_after_hours
        sharing the same `false` before-value must both flip correctly.
        The finding hypothesized that sequential str.replace could cross-
        contaminate; this test pins the expected behaviour: each field's
        line carries its own key, so the replace is line-unique."""
        delta = pl.ParsedDelta(
            rule="market_hours",
            change_type="remove",
            field="allow_pre_market+allow_after_hours",
            before={"allow_pre_market": False, "allow_after_hours": False},
            after={"allow_pre_market": True, "allow_after_hours": True},
        )
        before, after = pl.build_yaml_patch(delta, self.cfg)
        # Both source lines appear exactly once in the config.
        self.assertIn("allow_pre_market: false", before)
        self.assertIn("allow_after_hours: false", before)
        # Both flip to true in the after block.
        self.assertIn("allow_pre_market: true", after)
        self.assertIn("allow_after_hours: true", after)
        # Neither `false` survives in the fields we patched.
        self.assertNotIn("allow_pre_market: false", after)
        self.assertNotIn("allow_after_hours: false", after)

    def test_remove_whitelist_last_ticker_yields_empty_list(self) -> None:
        delta = pl.ParsedDelta(
            rule="instrument_whitelist",
            change_type="remove",
            field="symbols",
            before=["SPY"],
            after=[],
            ticker="SPY",
        )
        before, after = pl.build_yaml_patch(delta, self.cfg)
        self._assert_patch_valid(before, after)
        self.assertIn("symbols: []", after)


# ---------- render ----------


class RenderTests(unittest.TestCase):
    def test_render_has_required_frontmatter(self) -> None:
        delta = pl.ParsedDelta(
            rule="position_size",
            change_type="widen",
            field="max_trade_risk_pct",
            before=0.01,
            after=0.02,
            summary="per-trade risk 1% -> 2%",
        )
        md = pl.render_proposal(
            delta=delta,
            rationale="More cap efficiency.",
            safety_impact="Doubles per-trade exposure.",
            yaml_patch=(
                "  max_trade_risk_pct: 0.01  # 1%",
                "  max_trade_risk_pct: 0.02  # 2%",
            ),
            date_iso="2026-04-19",
        )
        # Spec §2.3 frontmatter fields
        self.assertIn("tags: [review, strategy-approvals, limits-proposal]", md)
        self.assertIn("date: 2026-04-19", md)
        self.assertIn("type: limits-proposal", md)
        self.assertIn("origin: keith", md)
        self.assertIn("status: proposed", md)
        self.assertIn(
            "applies-to: execution/validators/config.yaml", md
        )
        self.assertIn('up: "[[index]]"', md)

    def test_render_has_required_body_sections(self) -> None:
        delta = pl.ParsedDelta(
            rule="position_size",
            change_type="widen",
            field="max_trade_risk_pct",
            before=0.01,
            after=0.02,
        )
        md = pl.render_proposal(
            delta=delta,
            rationale="reason",
            safety_impact="impact",
            yaml_patch=("before-block", "after-block"),
            date_iso="2026-04-19",
        )
        self.assertIn("## Change", md)
        self.assertIn("## Rationale (Keith's)", md)
        self.assertIn("## Safety Impact (skill's assessment)", md)
        self.assertIn("## YAML Patch", md)
        self.assertIn("## Approval", md)

    def test_render_change_block_parseable(self) -> None:
        delta = pl.ParsedDelta(
            rule="instrument_whitelist",
            change_type="add",
            field="symbols",
            before=["SPY"],
            after=["SPY", "AAPL"],
            ticker="AAPL",
        )
        md = pl.render_proposal(
            delta=delta,
            rationale="broaden universe",
            safety_impact="neutral",
            yaml_patch=(
                "  symbols:\n    - SPY",
                "  symbols:\n    - SPY\n    - AAPL",
            ),
            date_iso="2026-04-19",
        )
        # The Change block must contain rule + change_type keys.
        self.assertIn("rule: instrument_whitelist", md)
        self.assertIn("change_type: add", md)
        self.assertIn("ticker: AAPL", md)


# ---------- write ----------


class WriteProposalTests(unittest.TestCase):
    def test_write_creates_correct_filename(self) -> None:
        with TemporaryDirectory() as tmp:
            repo = _tmp_repo(TemporaryDirectory_wrapper(tmp))
            path = pl.write_proposal(
                repo,
                "dummy content\n",
                slug="position_size-widen",
                date_iso="2026-04-19",
            )
            self.assertTrue(path.exists())
            self.assertEqual(
                path.name, "2026-04-19_limits-proposal_position_size-widen.md"
            )
            self.assertEqual(
                path.parent, repo / "review" / "strategy-approvals"
            )

    def test_write_creates_approvals_dir_if_missing(self) -> None:
        with TemporaryDirectory() as tmp:
            repo = Path(tmp)
            path = pl.write_proposal(
                repo,
                "content",
                slug="leverage-widen",
                date_iso="2026-04-19",
            )
            self.assertTrue((repo / "review" / "strategy-approvals").exists())
            self.assertTrue(path.exists())

    def test_write_refuses_overwrite_by_default(self) -> None:
        """Codex R1 F3: same-day re-runs must not silently clobber an
        existing proposal artifact. Default behaviour is refuse."""
        with TemporaryDirectory() as tmp:
            repo = Path(tmp)
            pl.write_proposal(
                repo,
                "first content",
                slug="position_size-widen",
                date_iso="2026-04-19",
            )
            with self.assertRaises(pl.ProposalError) as ctx:
                pl.write_proposal(
                    repo,
                    "second content",
                    slug="position_size-widen",
                    date_iso="2026-04-19",
                )
            self.assertIn("already exists", str(ctx.exception))

    def test_write_allows_overwrite_with_flag(self) -> None:
        with TemporaryDirectory() as tmp:
            repo = Path(tmp)
            pl.write_proposal(
                repo,
                "first content\n",
                slug="x-y",
                date_iso="2026-04-19",
            )
            path = pl.write_proposal(
                repo,
                "second content\n",
                slug="x-y",
                date_iso="2026-04-19",
                overwrite=True,
            )
            self.assertIn("second content", path.read_text(encoding="utf-8"))

    def test_write_exclusive_rejects_concurrent_create(self) -> None:
        """Codex R2 F6: exclusivity must be atomic, not
        check-then-write. Simulate a concurrent writer by creating the
        target file between logical 'decision' and 'write' -- the
        exclusive path must reject."""
        with TemporaryDirectory() as tmp:
            repo = Path(tmp)
            # Pre-create the target file to simulate a concurrent writer
            # who got there first.
            dest = repo / "review" / "strategy-approvals"
            dest.mkdir(parents=True, exist_ok=True)
            target = dest / "2026-04-19_limits-proposal_x-y.md"
            target.write_text("from concurrent writer\n", encoding="utf-8")
            # Now call write_proposal; exclusive semantics must refuse.
            with self.assertRaises(pl.ProposalError) as ctx:
                pl.write_proposal(
                    repo,
                    "my content\n",
                    slug="x-y",
                    date_iso="2026-04-19",
                )
            self.assertIn("already exists", str(ctx.exception))
            # Concurrent writer's content must be preserved.
            self.assertEqual(
                target.read_text(encoding="utf-8"),
                "from concurrent writer\n",
            )


class TemporaryDirectory_wrapper:
    """Wrap an existing tmp path so _tmp_repo's API (TemporaryDirectory-like) works."""

    def __init__(self, tmpdir: str) -> None:
        self.name = tmpdir


# ---------- hard rule: NEVER writes config.yaml ----------


class HardRuleTests(unittest.TestCase):
    def test_atomic_write_refuses_config_yaml_absolute(self) -> None:
        with TemporaryDirectory() as tmp:
            repo = _tmp_repo(TemporaryDirectory_wrapper(tmp))
            target = repo / "execution" / "validators" / "config.yaml"
            with self.assertRaises(pl.ProposalError) as ctx:
                pl._atomic_write(target, b"evil content")
            self.assertIn("config.yaml", str(ctx.exception))

    def test_atomic_write_refuses_mixed_case_config_yaml(self) -> None:
        """Codex R1 F1: macOS / Windows filesystems are case-insensitive.
        A mixed-case spelling like `Execution/Validators/config.yaml`
        resolves to the same inode but a case-sensitive tail match
        would miss it. The guard must fire on any fs-equivalent spelling.
        """
        with TemporaryDirectory() as tmp:
            repo = Path(tmp)
            # Construct the path by name only (the file doesn't need to
            # exist for the guard check -- _atomic_write rejects before
            # any fs touch). Use mixed case to exercise the case-fold
            # path.
            target = (
                repo / "SubDir" / "Execution" / "Validators" / "config.yaml"
            )
            target.parent.mkdir(parents=True, exist_ok=True)
            with self.assertRaises(pl.ProposalError):
                pl._atomic_write(target, b"evil content")

    def test_atomic_write_refuses_uppercase_config_yaml(self) -> None:
        """Fully-uppercase CONFIG.YAML variant on case-insensitive fs."""
        with TemporaryDirectory() as tmp:
            repo = Path(tmp)
            target = repo / "EXECUTION" / "VALIDATORS" / "CONFIG.YAML"
            target.parent.mkdir(parents=True, exist_ok=True)
            with self.assertRaises(pl.ProposalError):
                pl._atomic_write(target, b"evil content")

    def test_build_and_write_does_not_mutate_config_yaml(self) -> None:
        """End-to-end: a widen call must leave config.yaml byte-identical."""
        with TemporaryDirectory() as tmp:
            repo = _tmp_repo(TemporaryDirectory_wrapper(tmp))
            cfg_path = repo / "execution" / "validators" / "config.yaml"
            before_bytes = cfg_path.read_bytes()
            before_mtime = cfg_path.stat().st_mtime_ns
            pl.build_and_write(
                repo,
                "widen per-trade risk to 2%",
                rationale="test",
                date_iso="2026-04-19",
            )
            self.assertEqual(cfg_path.read_bytes(), before_bytes)
            # mtime may or may not bump depending on fs; the bytes
            # comparison is the authoritative check.

    def test_malicious_prompt_cannot_route_write_to_config(self) -> None:
        """A crafted prompt that would try to route the write target to
        config.yaml must be refused at _atomic_write. This mirrors spec
        §5.4's intent: the skill's first line of defence is its own
        refusal to open config.yaml for writing."""
        with TemporaryDirectory() as tmp:
            repo = _tmp_repo(TemporaryDirectory_wrapper(tmp))
            target = repo / "execution" / "validators" / "config.yaml"
            with self.assertRaises(pl.ProposalError):
                pl._atomic_write(target, b"status: approved")

    def test_module_never_opens_config_in_write_mode(self) -> None:
        """Static check: scan the module source for write-mode opens on
        config.yaml. Catches refactors that accidentally add a direct
        write path."""
        src = Path(pl.__file__).read_text(encoding="utf-8")
        # These patterns would indicate a write-path touching config.yaml.
        forbidden_patterns = [
            r'open\([^)]*config[^)]*,\s*[\'"]w[\'"]',
            r'write_text\([^)]*config\.yaml',
            r'write_bytes\([^)]*config\.yaml',
        ]
        import re as _re

        for pat in forbidden_patterns:
            self.assertIsNone(
                _re.search(pat, src),
                f"module unexpectedly contains write-mode pattern {pat!r}",
            )


# ---------- integration with cycle 5's --approve-limits ----------


class HandlerIntegrationTests(unittest.TestCase):
    """The critical contract: a file produced by build_and_write must be
    consumed by cycle 5's handle_approve_limits without raising.

    This is the cycle 6 P0 risk protection -- every Step-A validation
    the handler performs must see its expected shape in our output.
    """

    def setUp(self) -> None:
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.repo = _tmp_repo(TemporaryDirectory_wrapper(self.tmp.name))
        # --approve-limits uses a git repo for parent-sha capture; create one.
        subprocess.run(
            ["git", "init", "-q"], cwd=self.repo, check=True
        )
        subprocess.run(
            ["git", "config", "user.email", "test@example.com"],
            cwd=self.repo, check=True,
        )
        subprocess.run(
            ["git", "config", "user.name", "Test"],
            cwd=self.repo, check=True,
        )
        subprocess.run(
            ["git", "add", "-A"], cwd=self.repo, check=True
        )
        subprocess.run(
            ["git", "commit", "-q", "-m", "initial"],
            cwd=self.repo, check=True,
        )

    def _approve(self, path: Path) -> iss.LimitsCommitHints:
        """Run handle_approve_limits with a fixed parent_sha + now."""
        cfg = self.repo / "execution" / "validators" / "config.yaml"
        return iss.handle_approve_limits(
            path,
            config_path=cfg,
            parent_sha="deadbeef",
        )

    def test_widen_position_size_round_trip(self) -> None:
        result = pl.build_and_write(
            self.repo,
            "widen position size cap to 25%",
            rationale="diversify concentration",
            date_iso="2026-04-19",
        )
        path = Path(result["path"])
        hints = self._approve(path)
        self.assertEqual(hints.rule, "position_size")
        self.assertEqual(hints.change_type, "widen")
        # After apply, config.yaml has the new value.
        cfg_text = (
            self.repo / "execution" / "validators" / "config.yaml"
        ).read_text(encoding="utf-8")
        self.assertIn("max_ticker_concentration_pct: 0.25", cfg_text)

    def test_widen_per_trade_risk_round_trip(self) -> None:
        result = pl.build_and_write(
            self.repo,
            "widen per-trade risk to 2%",
            rationale="MVP tuning",
            date_iso="2026-04-19",
        )
        path = Path(result["path"])
        hints = self._approve(path)
        self.assertEqual(hints.rule, "position_size")
        cfg_text = (
            self.repo / "execution" / "validators" / "config.yaml"
        ).read_text(encoding="utf-8")
        self.assertIn("max_trade_risk_pct: 0.02", cfg_text)

    def test_widen_trade_risk_round_trip(self) -> None:
        result = pl.build_and_write(
            self.repo,
            "widen portfolio risk to 8%",
            rationale="room for more positions",
            date_iso="2026-04-19",
        )
        path = Path(result["path"])
        hints = self._approve(path)
        self.assertEqual(hints.rule, "trade_risk")
        cfg_text = (
            self.repo / "execution" / "validators" / "config.yaml"
        ).read_text(encoding="utf-8")
        self.assertIn("max_open_risk_pct: 0.08", cfg_text)

    def test_add_ticker_round_trip(self) -> None:
        result = pl.build_and_write(
            self.repo,
            "allow AAPL on the whitelist",
            rationale="broaden universe",
            date_iso="2026-04-19",
        )
        path = Path(result["path"])
        hints = self._approve(path)
        self.assertEqual(hints.rule, "instrument_whitelist")
        self.assertEqual(hints.change_type, "add")
        cfg_text = (
            self.repo / "execution" / "validators" / "config.yaml"
        ).read_text(encoding="utf-8")
        self.assertIn("- AAPL", cfg_text)

    def test_remove_market_hours_round_trip(self) -> None:
        result = pl.build_and_write(
            self.repo,
            "drop market_hours guard",
            rationale="support overnight",
            date_iso="2026-04-19",
        )
        path = Path(result["path"])
        hints = self._approve(path)
        self.assertEqual(hints.rule, "market_hours")
        cfg_text = (
            self.repo / "execution" / "validators" / "config.yaml"
        ).read_text(encoding="utf-8")
        self.assertIn("allow_pre_market: true", cfg_text)
        self.assertIn("allow_after_hours: true", cfg_text)

    def test_leverage_widen_round_trip(self) -> None:
        result = pl.build_and_write(
            self.repo,
            "widen leverage to 2x",
            rationale="margin use approved by risk desk",
            date_iso="2026-04-19",
        )
        path = Path(result["path"])
        hints = self._approve(path)
        self.assertEqual(hints.rule, "leverage")
        cfg_text = (
            self.repo / "execution" / "validators" / "config.yaml"
        ).read_text(encoding="utf-8")
        self.assertIn("cash_only: false", cfg_text)
        self.assertIn("max_leverage: 2", cfg_text)


class HandlerNegativeIntegrationTests(unittest.TestCase):
    """R1-minimax F3: negative-path tests -- a file the renderer
    silently drifts (e.g. missing `type:`, stale before-block) must be
    caught by the cycle-5 handler's Step-A checks. Without these
    assertions, a renderer regression could ship a file that bypasses
    validation by luck of the current config shape."""

    def setUp(self) -> None:
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.repo = _tmp_repo(TemporaryDirectory_wrapper(self.tmp.name))
        subprocess.run(["git", "init", "-q"], cwd=self.repo, check=True)
        subprocess.run(
            ["git", "config", "user.email", "test@example.com"],
            cwd=self.repo, check=True,
        )
        subprocess.run(
            ["git", "config", "user.name", "Test"],
            cwd=self.repo, check=True,
        )
        subprocess.run(["git", "add", "-A"], cwd=self.repo, check=True)
        subprocess.run(
            ["git", "commit", "-q", "-m", "init"], cwd=self.repo, check=True
        )
        self.cfg = self.repo / "execution" / "validators" / "config.yaml"

    def _write_valid(self) -> Path:
        result = pl.build_and_write(
            self.repo,
            "widen per-trade risk to 2%",
            rationale="test",
            date_iso="2026-04-19",
        )
        return Path(result["path"])

    def test_handler_rejects_missing_type_field(self) -> None:
        path = self._write_valid()
        text = path.read_text(encoding="utf-8")
        mangled = text.replace("type: limits-proposal\n", "")
        path.write_text(mangled, encoding="utf-8")
        with self.assertRaises(iss.ValidationError) as ctx:
            iss.handle_approve_limits(
                path, config_path=self.cfg, parent_sha="deadbeef"
            )
        self.assertIn("type", str(ctx.exception).lower())

    def test_handler_rejects_wrong_applies_to(self) -> None:
        path = self._write_valid()
        text = path.read_text(encoding="utf-8")
        mangled = text.replace(
            "applies-to: execution/validators/config.yaml",
            "applies-to: some/other/path.yaml",
        )
        path.write_text(mangled, encoding="utf-8")
        with self.assertRaises(iss.ValidationError):
            iss.handle_approve_limits(
                path, config_path=self.cfg, parent_sha="deadbeef"
            )

    def test_handler_rejects_before_not_in_config(self) -> None:
        """If Keith manually edits config between propose + approve, the
        before-block drifts. The handler's uniqueness check catches it."""
        path = self._write_valid()
        # Mutate config.yaml so the before-block no longer matches.
        cfg_text = self.cfg.read_text(encoding="utf-8")
        self.cfg.write_text(
            cfg_text.replace(
                "max_trade_risk_pct: 0.01",
                "max_trade_risk_pct: 0.015",
            ),
            encoding="utf-8",
        )
        with self.assertRaises(iss.ValidationError) as ctx:
            iss.handle_approve_limits(
                path, config_path=self.cfg, parent_sha="deadbeef"
            )
        self.assertIn("not found", str(ctx.exception).lower())

    def test_handler_rejects_reapplied_proposal(self) -> None:
        """Once status flipped to approved, re-approving must fail."""
        path = self._write_valid()
        iss.handle_approve_limits(
            path, config_path=self.cfg, parent_sha="deadbeef"
        )
        # Reset config.yaml so the before-block is present again.
        text = path.read_text(encoding="utf-8")
        # Re-approval should fail due to already-present approved_at.
        with self.assertRaises(iss.ValidationError):
            iss.handle_approve_limits(
                path, config_path=self.cfg, parent_sha="deadbee1"
            )


# ---------- CLI ----------


class CliTests(unittest.TestCase):
    def test_parse_cli_returns_clarification(self) -> None:
        with TemporaryDirectory() as tmp:
            repo = _tmp_repo(TemporaryDirectory_wrapper(tmp))
            cmd = [
                sys.executable,
                "-m",
                "scripts.lib.propose_limits",
                "--repo", str(repo),
                "parse",
                "--text", "tighten trade risk",
            ]
            out = subprocess.run(
                cmd, cwd=REPO_ROOT, capture_output=True, text=True
            )
            self.assertEqual(out.returncode, 0, out.stderr)
            payload = json.loads(out.stdout)
            self.assertEqual(payload["kind"], "clarification")

    def test_parse_cli_config_path_relative_to_repo(self) -> None:
        """Codex R3 P3: --config-path with a relative path is rebased
        onto --repo, matching build_and_write's behaviour."""
        with TemporaryDirectory() as tmp:
            repo = _tmp_repo(TemporaryDirectory_wrapper(tmp))
            cmd = [
                sys.executable,
                "-m",
                "scripts.lib.propose_limits",
                "--repo", str(repo),
                "parse",
                "--text", "widen per-trade risk to 2%",
                # relative to repo, not to the REPO_ROOT we cwd into
                "--config-path", "execution/validators/config.yaml",
            ]
            out = subprocess.run(
                cmd, cwd=REPO_ROOT, capture_output=True, text=True
            )
            self.assertEqual(out.returncode, 0, out.stderr)
            payload = json.loads(out.stdout)
            self.assertEqual(payload["kind"], "parsed")

    def test_parse_cli_returns_parsed(self) -> None:
        with TemporaryDirectory() as tmp:
            repo = _tmp_repo(TemporaryDirectory_wrapper(tmp))
            cmd = [
                sys.executable,
                "-m",
                "scripts.lib.propose_limits",
                "--repo", str(repo),
                "parse",
                "--text", "widen per-trade risk to 2%",
            ]
            out = subprocess.run(
                cmd, cwd=REPO_ROOT, capture_output=True, text=True
            )
            self.assertEqual(out.returncode, 0, out.stderr)
            payload = json.loads(out.stdout)
            self.assertEqual(payload["kind"], "parsed")
            self.assertEqual(payload["rule"], "position_size")
            self.assertEqual(payload["change_type"], "widen")

    def test_write_cli_produces_file(self) -> None:
        with TemporaryDirectory() as tmp:
            repo = _tmp_repo(TemporaryDirectory_wrapper(tmp))
            cmd = [
                sys.executable,
                "-m",
                "scripts.lib.propose_limits",
                "--repo", str(repo),
                "write",
                "--text", "widen per-trade risk to 2%",
                "--rationale", "MVP tuning",
                "--date", "2026-04-19",
            ]
            out = subprocess.run(
                cmd, cwd=REPO_ROOT, capture_output=True, text=True
            )
            self.assertEqual(out.returncode, 0, out.stderr)
            payload = json.loads(out.stdout)
            self.assertEqual(payload["kind"], "written")
            self.assertTrue(Path(payload["path"]).exists())


if __name__ == "__main__":
    unittest.main()
