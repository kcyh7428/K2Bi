"""Tests for scripts.lib.invest_thesis -- Bundle 4 cycle 1 (m2.11).

Covers the 10-row test matrix from spec §6 + supporting unit tests for
the cross-cutting patterns from §4. Uses a tmp vault fixture (directory
tree + seeded glossary + optional active_rules.md) so every test runs
in isolation.

Fixtures avoid the real K2Bi-Vault and never shell out to
`wiki-log-append.sh`: the skill delegates log-append to the SKILL.md
bash wrapper, so Python stays pure (vault-in / vault-out only).

Test classes:

    * SymbolValidationTests  -- filename edge cases + regex rejects.
    * HappyPathTests         -- NVDA full-source run; all schema fields
                                 present; body has every H2 + Action
                                 Plan Summary literal POSITION line.
    * RefreshTests           -- 30-day freshness skip + --refresh override.
    * EdgeCaseTickerTypeTests -- ETF / pre_revenue / penny adaptations.
    * SchemaValidityTests    -- frontmatter YAML parses cleanly + all
                                 keys + value ranges + sell_pct sum +
                                 probabilities sum.
    * TeachModeTests         -- novice prepend / advanced skip.
    * AtomicWriteTests       -- interrupted write leaves prior content
                                 intact + no leftover tempfiles.
    * HookIntegrationTests   -- Check D regex does NOT match ticker
                                 files (positive + negative controls).
    * ConvictionBandTests    -- band derivation from composite score.
    * AsymmetryValidationTests -- probabilities must sum to 1.00.
    * ActionPlanSummaryTests -- POSITION line is the literal validator-
                                 owned string; NEVER computes size.
"""

from __future__ import annotations

import datetime as _dt
import re
import shutil
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from scripts.lib import invest_thesis as it
from scripts.lib import strategy_frontmatter as sf


# ---------- fixture helpers ----------


def _seed_vault(root: Path, *, active_rules: str | None = None) -> None:
    """Create the minimal vault tree invest-thesis writes into.

    Seeds a stub glossary + optional active_rules.md. invest-thesis
    does NOT require any of the other vault subfolders to exist -- it
    mkdirs `wiki/tickers/` on demand via the atomic-write helper.
    """
    (root / "wiki" / "tickers").mkdir(parents=True)
    (root / "wiki" / "reference").mkdir(parents=True)
    (root / "System" / "memory").mkdir(parents=True)
    (root / "wiki" / "reference" / "glossary.md").write_text(
        "---\n"
        "tags: [glossary, k2bi, reference]\n"
        "type: glossary\n"
        "origin: k2bi-generate\n"
        "up: \"[[index]]\"\n"
        "---\n"
        "\n"
        "# K2Bi Trading Glossary\n"
    )
    if active_rules is not None:
        (root / "System" / "memory" / "active_rules.md").write_text(active_rules)


def _make_default_input(symbol: str = "NVDA", ticker_type: str = "equity", **overrides):
    """Return a ThesisInput populated with spec §2.1 example values.

    Tests that need alternate values override specific fields. The
    defaults match the spec's NVDA-style example (sub_scores sum to 73,
    4 asymmetry scenarios summing to 1.00, three T1/T2/T3 targets
    summing to 100% sell_pct).
    """
    verification = overrides.pop("verification", None)
    if verification is None:
        verification = it.Verification(
            completed_at="2026-04-29T16:25:00+08:00",
            claims=[
                it.ClaimVerification(
                    claim_id="bull-1-evidence",
                    claim_text="Q3 2025 hyperscaler capex +47% YoY",
                    claim_load_bearing=True,
                    source_url="https://example.com/msft-capex",
                    operator_check="verified",
                ),
            ],
            status="pass",
        )
    defaults = dict(
        symbol=symbol,
        ticker_type=ticker_type,
        sub_scores=it.SubScores(
            catalyst_clarity=16,
            asymmetry=14,
            timeline_precision=15,
            edge_identification=12,
            conviction_level=16,
        ),
        fundamental_sub_scores=it.FundamentalSubScores(
            valuation=13,
            growth=16,
            profitability=17,
            financial_health=15,
            moat_strength=13,
        ),
        bull_reasons=[
            it.BullReason(
                reason="Data-center revenue concentration in hyperscaler capex cycle",
                evidence="Q3 2025 hyperscaler capex +47% YoY (MSFT, META calls)",
                impact_estimate="Adds 15-20% to forward revenue trajectory",
            ),
            it.BullReason(
                reason="Moat widening via CUDA ecosystem lock-in",
                evidence="Developer survey 2025: 92% use CUDA exclusively",
                impact_estimate="Multi-year switching-cost protection",
            ),
        ],
        bear_reasons=[
            it.BearReason(
                reason="Single-customer concentration risk",
                evidence="Top-3 customers = 62% of DC revenue (10-Q footnote)",
                impact_estimate="One delayed capex cycle compresses multiple 30-40%",
            ),
            it.BearReason(
                reason="Forward P/E pricing perfection",
                evidence="Forward P/E 38x vs sector 28x",
                impact_estimate="Any miss triggers multiple compression to sector avg",
            ),
        ],
        base_case=it.BaseCase(
            scenario="Steady hyperscaler capex through 2027",
            probability=0.55,
            target_price=850.00,
        ),
        entry_exit_levels=it.EntryExitLevels(
            entry=700.00,
            stop=630.00,
            targets=[
                it.Target(level="T1", price=800.00, sell_pct=33, reasoning="Prior resistance level"),
                it.Target(level="T2", price=900.00, sell_pct=33, reasoning="Bull case fair value"),
                it.Target(level="T3", price=1000.00, sell_pct=34, reasoning="Stretch target"),
            ],
            risk_reward_ratio=4.3,
        ),
        entry_triggers=[
            "RSI < 40 on daily timeframe",
            "Volume above 20-day average on green day",
            "No earnings within 14 days",
        ],
        entry_invalidation=[
            "Price breaks below $630 support on heavy volume",
            "Insider selling accelerates",
            "Sector rotation signals turn negative",
        ],
        exit_signals=[
            "Thesis-breaking news (loss of major customer, fraud)",
            "Fundamental deterioration: 2+ consecutive revenue misses",
            "Better opportunity identified (opportunity cost)",
        ],
        time_stop=it.TimeStop(
            max_hold_period="6 months",
            reassessment_triggers=[
                "Re-evaluate after each earnings report",
                "Reassess regardless of P/L if thesis hasn't played out in 6 months",
            ],
        ),
        recommended_action="bull",
        next_catalyst=it.NextCatalyst(
            event="Q4 2025 earnings",
            date="2026-02-15",
            expected_impact="Guidance for FY26 hyperscaler capex",
        ),
        catalyst_timeline=[
            it.CatalystTimelineEntry(
                date="2026-02-15",
                event="Q4 2025 earnings",
                expected_impact="Positive -- consensus 5% upside surprise potential",
                probability="high",
            ),
            it.CatalystTimelineEntry(
                date="2026-03-20",
                event="GTC keynote",
                expected_impact="Positive -- new product reveals",
                probability="medium",
            ),
        ],
        asymmetry_scenarios=[
            it.AsymmetryScenario(scenario="Bull", probability=0.30, target_price=1000.00),
            it.AsymmetryScenario(scenario="Base", probability=0.45, target_price=850.00),
            it.AsymmetryScenario(scenario="Neutral", probability=0.15, target_price=730.00),
            it.AsymmetryScenario(scenario="Bear", probability=0.10, target_price=580.00),
        ],
        asymmetry_score=8,
        asymmetry_score_rationale="4:1 R/R with quantified downside cap at 200MA",
        plain_english_summary=(
            "NVDA is the dominant supplier of AI training chips. "
            "The thesis is that hyperscaler spending continues through 2027, "
            "driving revenue and earnings higher. Risk: customer concentration."
        ),
        phase_1_business_model=(
            "NVDA designs GPUs + CUDA platform. Revenue split: "
            "data-center 78%, gaming 14%, other 8% (Q3 2025 10-Q)."
        ),
        phase_2_competitive_moat=(
            "Moat: CUDA ecosystem lock-in + 80% AI-training-chip share."
        ),
        phase_3_financial_quality=(
            "Revenue growth 47% YoY Q3 2025; gross margin 76%; "
            "FCF margin 52%; net cash position."
        ),
        phase_4_risks_valuation=(
            "Forward P/E 38x prices in continued hyperscaler capex. "
            "Bear case: customer concentration + geopolitical export risk."
        ),
        primary_entry_rationale="$700 -- 50-day MA support + volume shelf",
        secondary_entry_aggressive="$720 -- breakout above pattern resistance",
        secondary_entry_conservative="$660 -- pullback to 200-day MA",
        initial_stop_rationale="below the 200-day MA",
        trailing_stop_rationale="after T1 hits, move stop to breakeven",
        verification=verification,
    )
    defaults.update(overrides)
    return it.ThesisInput(**defaults)


class _VaultTestBase(unittest.TestCase):
    """Create + teardown a tmp vault for each test."""

    def setUp(self) -> None:
        self.tmp_vault = Path(tempfile.mkdtemp(prefix="invest_thesis_"))
        _seed_vault(self.tmp_vault)
        self.today = _dt.date(2026, 4, 19)

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp_vault, ignore_errors=True)


# ---------- symbol validation + filename edge cases ----------


class SymbolValidationTests(_VaultTestBase):
    def test_happy_symbol_nvda(self) -> None:
        ti = _make_default_input("NVDA")
        result = it.generate_thesis(ti, self.tmp_vault, now=self.today)
        self.assertTrue(result.written)
        self.assertEqual(result.path, self.tmp_vault / "wiki/tickers/NVDA.md")

    def test_preserves_hong_kong_ticker(self) -> None:
        ti = _make_default_input("0700.HK")
        it.generate_thesis(ti, self.tmp_vault, now=self.today)
        self.assertTrue((self.tmp_vault / "wiki/tickers/0700.HK.md").exists())

    def test_preserves_berkshire_b_ticker(self) -> None:
        ti = _make_default_input("BRK.B")
        it.generate_thesis(ti, self.tmp_vault, now=self.today)
        self.assertTrue((self.tmp_vault / "wiki/tickers/BRK.B.md").exists())

    def test_rejects_lowercase(self) -> None:
        ti = _make_default_input("nvda")
        with self.assertRaises(ValueError) as cm:
            it.generate_thesis(ti, self.tmp_vault, now=self.today)
        self.assertIn("symbol", str(cm.exception).lower())

    def test_rejects_dash(self) -> None:
        ti = _make_default_input("NVDA-B")
        with self.assertRaises(ValueError):
            it.generate_thesis(ti, self.tmp_vault, now=self.today)

    def test_rejects_digits_only(self) -> None:
        # The regex allows leading digits (for 0700.HK) but pure digits
        # without a letter segment is not a valid ticker.
        ti = _make_default_input("123")
        with self.assertRaises(ValueError):
            it.generate_thesis(ti, self.tmp_vault, now=self.today)

    def test_rejects_empty_symbol(self) -> None:
        ti = _make_default_input("")
        with self.assertRaises(ValueError):
            it.generate_thesis(ti, self.tmp_vault, now=self.today)


# ---------- happy path: all H2 sections + all schema fields ----------


class HappyPathTests(_VaultTestBase):
    def test_writes_all_h2_sections(self) -> None:
        ti = _make_default_input("NVDA")
        it.generate_thesis(ti, self.tmp_vault, now=self.today)
        body = (self.tmp_vault / "wiki/tickers/NVDA.md").read_text()
        for heading in (
            "## Phase 1: Business Model",
            "## Phase 2: Competitive Position / Moat",
            "## Phase 3: Financial Quality",
            "## Phase 4: Risks + Valuation",
            "## Catalyst Timeline",
            "## Entry Strategy",
            "## Exit Strategy",
            "## Asymmetry Analysis",
            "## Thesis Scorecard",
            "## Fundamental Sub-Scoring",
            "## Action Plan Summary",
        ):
            self.assertIn(heading, body, f"missing heading: {heading}")

    def test_writes_robot_callout(self) -> None:
        ti = _make_default_input("NVDA")
        it.generate_thesis(ti, self.tmp_vault, now=self.today)
        body = (self.tmp_vault / "wiki/tickers/NVDA.md").read_text()
        self.assertIn("> [!robot] K2Bi analysis", body)

    def test_writes_entry_strategy_subsections(self) -> None:
        ti = _make_default_input("NVDA")
        it.generate_thesis(ti, self.tmp_vault, now=self.today)
        body = (self.tmp_vault / "wiki/tickers/NVDA.md").read_text()
        self.assertIn("### Entry Triggers", body)
        self.assertIn("### Entry Invalidation", body)
        # All 3 triggers rendered
        self.assertIn("RSI < 40 on daily timeframe", body)
        # All 3 invalidation conditions rendered
        self.assertIn("Insider selling accelerates", body)

    def test_writes_exit_strategy_subsections(self) -> None:
        ti = _make_default_input("NVDA")
        it.generate_thesis(ti, self.tmp_vault, now=self.today)
        body = (self.tmp_vault / "wiki/tickers/NVDA.md").read_text()
        self.assertIn("### Profit Targets", body)
        self.assertIn("### Stop Loss", body)
        self.assertIn("### Time Stop", body)
        self.assertIn("### Exit Signals", body)

    def test_writes_catalyst_timeline_table(self) -> None:
        ti = _make_default_input("NVDA")
        it.generate_thesis(ti, self.tmp_vault, now=self.today)
        body = (self.tmp_vault / "wiki/tickers/NVDA.md").read_text()
        # Markdown table header
        self.assertIn("| Date | Catalyst | Expected Impact | Probability |", body)
        # Both rows
        self.assertIn("Q4 2025 earnings", body)
        self.assertIn("GTC keynote", body)

    def test_writes_profit_targets_table_with_sell_pct(self) -> None:
        ti = _make_default_input("NVDA")
        it.generate_thesis(ti, self.tmp_vault, now=self.today)
        body = (self.tmp_vault / "wiki/tickers/NVDA.md").read_text()
        # Three target rows, all sell_pct values present
        self.assertIn("T1", body)
        self.assertIn("T2", body)
        self.assertIn("T3", body)
        self.assertIn("33%", body)
        self.assertIn("34%", body)


# ---------- schema validity: parse + key presence + ranges ----------


class SchemaValidityTests(_VaultTestBase):
    def test_frontmatter_parses_cleanly(self) -> None:
        ti = _make_default_input("NVDA")
        it.generate_thesis(ti, self.tmp_vault, now=self.today)
        content = (self.tmp_vault / "wiki/tickers/NVDA.md").read_bytes()
        fm = sf.parse(content)  # raises if invalid
        self.assertIsInstance(fm, dict)

    def test_all_top_level_keys_present(self) -> None:
        ti = _make_default_input("NVDA")
        it.generate_thesis(ti, self.tmp_vault, now=self.today)
        fm = sf.parse((self.tmp_vault / "wiki/tickers/NVDA.md").read_bytes())
        expected = {
            "tags", "date", "type", "origin", "up", "symbol",
            "confidence-last-verified", "thesis-last-verified",
            "thesis_score", "sub_scores", "fundamental_sub_scores",
            "bull_case", "bear_case", "base_case",
            "entry_exit_levels", "entry_triggers", "entry_invalidation",
            "exit_signals", "time_stop",
            "recommended_action", "conviction_band",
            "next_catalyst", "catalyst_timeline",
            "ticker_type",
        }
        missing = expected - set(fm.keys())
        self.assertFalse(missing, f"missing frontmatter keys: {missing}")

    def test_thesis_score_is_int_in_range(self) -> None:
        ti = _make_default_input("NVDA")
        it.generate_thesis(ti, self.tmp_vault, now=self.today)
        fm = sf.parse((self.tmp_vault / "wiki/tickers/NVDA.md").read_bytes())
        self.assertIsInstance(fm["thesis_score"], int)
        self.assertGreaterEqual(fm["thesis_score"], 0)
        self.assertLessEqual(fm["thesis_score"], 100)
        # 16+14+15+12+16 = 73
        self.assertEqual(fm["thesis_score"], 73)

    def test_sub_scores_all_in_range(self) -> None:
        ti = _make_default_input("NVDA")
        it.generate_thesis(ti, self.tmp_vault, now=self.today)
        fm = sf.parse((self.tmp_vault / "wiki/tickers/NVDA.md").read_bytes())
        for key in (
            "catalyst_clarity", "asymmetry", "timeline_precision",
            "edge_identification", "conviction_level",
        ):
            val = fm["sub_scores"][key]
            self.assertIsInstance(val, int, f"{key} not int: {val!r}")
            self.assertGreaterEqual(val, 0)
            self.assertLessEqual(val, 20)

    def test_fundamental_sub_scores_all_in_range(self) -> None:
        ti = _make_default_input("NVDA")
        it.generate_thesis(ti, self.tmp_vault, now=self.today)
        fm = sf.parse((self.tmp_vault / "wiki/tickers/NVDA.md").read_bytes())
        for key in (
            "valuation", "growth", "profitability",
            "financial_health", "moat_strength",
        ):
            val = fm["fundamental_sub_scores"][key]
            self.assertIsInstance(val, int)
            self.assertGreaterEqual(val, 0)
            self.assertLessEqual(val, 20)

    def test_targets_sell_pct_sums_to_100(self) -> None:
        ti = _make_default_input("NVDA")
        it.generate_thesis(ti, self.tmp_vault, now=self.today)
        fm = sf.parse((self.tmp_vault / "wiki/tickers/NVDA.md").read_bytes())
        total = sum(t["sell_pct"] for t in fm["entry_exit_levels"]["targets"])
        self.assertEqual(total, 100)

    def test_conviction_band_is_in_enum(self) -> None:
        ti = _make_default_input("NVDA")
        it.generate_thesis(ti, self.tmp_vault, now=self.today)
        fm = sf.parse((self.tmp_vault / "wiki/tickers/NVDA.md").read_bytes())
        self.assertIn(fm["conviction_band"], {"high", "good", "watchlist", "pass", "avoid"})

    def test_recommended_action_is_in_enum(self) -> None:
        ti = _make_default_input("NVDA")
        it.generate_thesis(ti, self.tmp_vault, now=self.today)
        fm = sf.parse((self.tmp_vault / "wiki/tickers/NVDA.md").read_bytes())
        self.assertIn(fm["recommended_action"], {"bull", "neutral", "bear"})

    def test_tags_contain_symbol_and_thesis(self) -> None:
        ti = _make_default_input("NVDA")
        it.generate_thesis(ti, self.tmp_vault, now=self.today)
        fm = sf.parse((self.tmp_vault / "wiki/tickers/NVDA.md").read_bytes())
        tags = fm["tags"]
        self.assertIn("ticker", tags)
        self.assertIn("NVDA", tags)
        self.assertIn("thesis", tags)

    def test_no_extra_unspecified_top_level_keys(self) -> None:
        """Silent schema drift was Bundle 3 retro learning #1. The
        invest-thesis frontmatter locks to §2.1. New fields require a
        spec bump, not a drive-by add."""
        ti = _make_default_input("NVDA")
        it.generate_thesis(ti, self.tmp_vault, now=self.today)
        fm = sf.parse((self.tmp_vault / "wiki/tickers/NVDA.md").read_bytes())
        allowed = {
            "tags", "date", "type", "origin", "up", "symbol",
            "confidence-last-verified", "thesis-last-verified",
            "thesis_score", "sub_scores", "fundamental_sub_scores",
            "bull_case", "bear_case", "base_case",
            "entry_exit_levels", "entry_triggers", "entry_invalidation",
            "exit_signals", "time_stop",
            "recommended_action", "conviction_band",
            "next_catalyst", "catalyst_timeline",
            "ticker_type", "verification",
        }
        extra = set(fm.keys()) - allowed
        self.assertFalse(extra, f"unexpected frontmatter keys: {extra}")


# ---------- refresh: 30-day freshness skip + --refresh override ----------


class FreshnessDatetimeCompatTests(_VaultTestBase):
    """Codex R7 R4 #1: existing files with a datetime-valued
    `thesis-last-verified` (hand edit, older writer) must be handled
    gracefully, not crash freshness check."""

    def test_datetime_valued_thesis_last_verified_is_parsed_as_date(
        self,
    ) -> None:
        ti = _make_default_input("NVDA")
        # First run creates NVDA.md with a date value
        it.generate_thesis(ti, self.tmp_vault, now=self.today)
        # Rewrite the file with a datetime-valued field
        path = self.tmp_vault / "wiki/tickers/NVDA.md"
        content = path.read_text()
        new_content = content.replace(
            f"thesis-last-verified: '{self.today.isoformat()}'",
            f"thesis-last-verified: '{self.today.isoformat()}T10:30:00Z'",
        )
        # If the replacement didn't happen, try unquoted form
        if new_content == content:
            new_content = content.replace(
                f"thesis-last-verified: {self.today.isoformat()}",
                f"thesis-last-verified: {self.today.isoformat()}T10:30:00Z",
            )
        path.write_text(new_content)
        # Second run with the datetime-valued field -- should NOT crash
        # and should honor freshness (refresh skipped).
        result = it.generate_thesis(ti, self.tmp_vault, now=self.today)
        # Fresh within 30 days -> skipped
        self.assertFalse(result.written)
        self.assertIn("fresh", result.skipped_reason.lower())


class RefreshTests(_VaultTestBase):
    def test_fresh_within_30_days_skips_without_refresh(self) -> None:
        ti = _make_default_input("NVDA")
        first = it.generate_thesis(ti, self.tmp_vault, now=self.today)
        self.assertTrue(first.written)
        orig_bytes = (self.tmp_vault / "wiki/tickers/NVDA.md").read_bytes()

        # 15 days later, no --refresh
        fifteen_days_later = self.today + _dt.timedelta(days=15)
        second = it.generate_thesis(ti, self.tmp_vault, now=fifteen_days_later)
        self.assertFalse(second.written)
        self.assertIsNotNone(second.skipped_reason)
        self.assertIn("fresh", second.skipped_reason.lower())
        # File unchanged
        self.assertEqual(
            (self.tmp_vault / "wiki/tickers/NVDA.md").read_bytes(),
            orig_bytes,
        )

    def test_boundary_30_days_still_fresh(self) -> None:
        ti = _make_default_input("NVDA")
        it.generate_thesis(ti, self.tmp_vault, now=self.today)
        thirty_days_later = self.today + _dt.timedelta(days=30)
        second = it.generate_thesis(ti, self.tmp_vault, now=thirty_days_later)
        self.assertFalse(second.written)

    def test_boundary_31_days_triggers_rewrite(self) -> None:
        ti = _make_default_input("NVDA")
        it.generate_thesis(ti, self.tmp_vault, now=self.today)
        thirty_one_days_later = self.today + _dt.timedelta(days=31)
        second = it.generate_thesis(ti, self.tmp_vault, now=thirty_one_days_later)
        self.assertTrue(second.written)

    def test_refresh_flag_forces_rewrite(self) -> None:
        ti = _make_default_input("NVDA")
        it.generate_thesis(ti, self.tmp_vault, now=self.today)
        # Same day, new sub_scores -- without --refresh this would skip.
        ti2 = _make_default_input(
            "NVDA",
            sub_scores=it.SubScores(
                catalyst_clarity=10, asymmetry=10, timeline_precision=10,
                edge_identification=10, conviction_level=10,
            ),
        )
        second = it.generate_thesis(ti2, self.tmp_vault, now=self.today, refresh=True)
        self.assertTrue(second.written)
        fm = sf.parse((self.tmp_vault / "wiki/tickers/NVDA.md").read_bytes())
        self.assertEqual(fm["thesis_score"], 50)


# ---------- edge cases: ETF / pre_revenue / penny ----------


class EdgeCaseTickerTypeTests(_VaultTestBase):
    def test_etf_ticker_type_in_frontmatter(self) -> None:
        ti = _make_default_input("SPY", ticker_type="etf")
        it.generate_thesis(ti, self.tmp_vault, now=self.today)
        fm = sf.parse((self.tmp_vault / "wiki/tickers/SPY.md").read_bytes())
        self.assertEqual(fm["ticker_type"], "etf")

    def test_etf_body_has_adaptation_note(self) -> None:
        ti = _make_default_input("SPY", ticker_type="etf")
        it.generate_thesis(ti, self.tmp_vault, now=self.today)
        body = (self.tmp_vault / "wiki/tickers/SPY.md").read_text()
        # ETF gets an adaptation note covering sector/thematic thesis
        # rather than single-company fundamentals.
        self.assertIn("ETF", body)
        self.assertTrue(
            "tracking" in body.lower() or "sector" in body.lower(),
            "ETF body should mention tracking or sector adaptation",
        )

    def test_pre_revenue_ticker_type_in_frontmatter(self) -> None:
        ti = _make_default_input("BIOX", ticker_type="pre_revenue")
        it.generate_thesis(ti, self.tmp_vault, now=self.today)
        fm = sf.parse((self.tmp_vault / "wiki/tickers/BIOX.md").read_bytes())
        self.assertEqual(fm["ticker_type"], "pre_revenue")

    def test_pre_revenue_body_has_runway_language(self) -> None:
        ti = _make_default_input("BIOX", ticker_type="pre_revenue")
        it.generate_thesis(ti, self.tmp_vault, now=self.today)
        body = (self.tmp_vault / "wiki/tickers/BIOX.md").read_text().lower()
        # Pre-revenue substitutes runway + TAM + pipeline.
        self.assertTrue(
            "runway" in body or "tam" in body or "pipeline" in body,
            "pre_revenue body should mention runway / TAM / pipeline",
        )

    def test_penny_ticker_type_in_frontmatter(self) -> None:
        ti = _make_default_input("PENNY", ticker_type="penny")
        it.generate_thesis(ti, self.tmp_vault, now=self.today)
        fm = sf.parse((self.tmp_vault / "wiki/tickers/PENNY.md").read_bytes())
        self.assertEqual(fm["ticker_type"], "penny")

    def test_penny_body_has_liquidity_warning(self) -> None:
        ti = _make_default_input("PENNY", ticker_type="penny")
        it.generate_thesis(ti, self.tmp_vault, now=self.today)
        body = (self.tmp_vault / "wiki/tickers/PENNY.md").read_text().lower()
        self.assertIn("liquidity", body)

    def test_rejects_unknown_ticker_type(self) -> None:
        ti = _make_default_input("NVDA", ticker_type="mystery")
        with self.assertRaises(ValueError):
            it.generate_thesis(ti, self.tmp_vault, now=self.today)


# ---------- Teach Mode ----------


class TeachModeTests(_VaultTestBase):
    def test_novice_prepend_above_phase_1(self) -> None:
        ti = _make_default_input("NVDA")
        it.generate_thesis(ti, self.tmp_vault, now=self.today, learning_stage="novice")
        body = (self.tmp_vault / "wiki/tickers/NVDA.md").read_text()
        self.assertIn("Plain-English summary", body)
        preamble_idx = body.index("Plain-English summary")
        phase1_idx = body.index("## Phase 1")
        self.assertLess(preamble_idx, phase1_idx)

    def test_advanced_omits_preamble(self) -> None:
        ti = _make_default_input("NVDA")
        it.generate_thesis(ti, self.tmp_vault, now=self.today, learning_stage="advanced")
        body = (self.tmp_vault / "wiki/tickers/NVDA.md").read_text()
        self.assertNotIn("Plain-English summary", body)

    def test_intermediate_omits_preamble_by_default(self) -> None:
        """Per CLAUDE.md Teach Mode table, intermediate drops preamble on
        routine outputs. invest-thesis is a routine output (not first-
        time concept surfacing)."""
        ti = _make_default_input("NVDA")
        it.generate_thesis(
            ti, self.tmp_vault, now=self.today, learning_stage="intermediate"
        )
        body = (self.tmp_vault / "wiki/tickers/NVDA.md").read_text()
        self.assertNotIn("Plain-English summary", body)

    def test_invalid_learning_stage_defaults_to_advanced(self) -> None:
        # Unknown stage should not crash -- fall back to advanced (no
        # preamble), per "skills should never fail because the dial is
        # unset" guidance in CLAUDE.md Teach Mode section.
        ti = _make_default_input("NVDA")
        it.generate_thesis(ti, self.tmp_vault, now=self.today, learning_stage="bogus")
        body = (self.tmp_vault / "wiki/tickers/NVDA.md").read_text()
        self.assertNotIn("Plain-English summary", body)


# ---------- atomic write: no partial on interrupt ----------


class AtomicWriteTests(_VaultTestBase):
    def test_exception_during_replace_preserves_prior_content(self) -> None:
        # Seed an existing NVDA thesis
        ti = _make_default_input("NVDA")
        it.generate_thesis(ti, self.tmp_vault, now=self.today)
        original_bytes = (self.tmp_vault / "wiki/tickers/NVDA.md").read_bytes()

        # Force os.replace in the shared helper to raise on the NEXT
        # write. The helper should unlink its tempfile + re-raise.
        with mock.patch(
            "scripts.lib.strategy_frontmatter.os.replace",
            side_effect=RuntimeError("simulated interrupt"),
        ):
            # Refresh so the write actually runs (instead of skipping on freshness)
            with self.assertRaises(RuntimeError):
                it.generate_thesis(ti, self.tmp_vault, now=self.today, refresh=True)

        # Final file is the prior content (not partial, not missing)
        self.assertEqual(
            (self.tmp_vault / "wiki/tickers/NVDA.md").read_bytes(),
            original_bytes,
        )
        # No leftover tempfiles in the directory (helper unlinked on error)
        leftovers = [
            p for p in (self.tmp_vault / "wiki/tickers").iterdir()
            if p.name != "NVDA.md"
        ]
        self.assertEqual(leftovers, [], f"temp leftover: {leftovers}")


# ---------- hook integration: Check D does not match ticker files ----------


class HookIntegrationTests(unittest.TestCase):
    """Pre-commit Check D targets `wiki/strategies/strategy_*.md` only.
    The hook's enumerator regex is the canonical source of truth for
    "which files Check D considers"; this test pins that regex against
    the three filename shapes invest-thesis emits.
    """

    CHECK_D_REGEX = re.compile(r"^wiki/strategies/strategy_[^/]+\.md$")

    def test_check_d_does_not_match_nvda_ticker_file(self) -> None:
        self.assertIsNone(self.CHECK_D_REGEX.match("wiki/tickers/NVDA.md"))

    def test_check_d_does_not_match_hong_kong_ticker_file(self) -> None:
        self.assertIsNone(self.CHECK_D_REGEX.match("wiki/tickers/0700.HK.md"))

    def test_check_d_does_not_match_berkshire_ticker_file(self) -> None:
        self.assertIsNone(self.CHECK_D_REGEX.match("wiki/tickers/BRK.B.md"))

    def test_check_d_does_match_strategy_file_control(self) -> None:
        """Positive control: Check D SHOULD match actual strategy files."""
        self.assertIsNotNone(self.CHECK_D_REGEX.match("wiki/strategies/strategy_spy.md"))


# ---------- conviction band derivation ----------


class ConvictionBandTests(_VaultTestBase):
    def _run_with_score(self, symbol: str, composite: int):
        # Distribute composite across the 5 sub-dims (4 at 20, last
        # carries the remainder, clamped 0..20). Good enough for band-
        # boundary testing; real runs use authentic sub-scores.
        per = min(20, composite // 5)
        remainder = composite - per * 4
        remainder = max(0, min(20, remainder))
        ti = _make_default_input(
            symbol,
            sub_scores=it.SubScores(
                catalyst_clarity=per,
                asymmetry=per,
                timeline_precision=per,
                edge_identification=per,
                conviction_level=remainder,
            ),
        )
        it.generate_thesis(ti, self.tmp_vault, now=self.today)
        return sf.parse((self.tmp_vault / f"wiki/tickers/{symbol}.md").read_bytes())

    def test_band_high_at_80(self) -> None:
        self.assertEqual(self._run_with_score("HIGHBND", 80)["conviction_band"], "high")

    def test_band_good_at_79(self) -> None:
        self.assertEqual(self._run_with_score("GOODBND", 79)["conviction_band"], "good")

    def test_band_good_at_65(self) -> None:
        self.assertEqual(self._run_with_score("GOODLOW", 65)["conviction_band"], "good")

    def test_band_watchlist_at_64(self) -> None:
        self.assertEqual(self._run_with_score("WATCHB", 64)["conviction_band"], "watchlist")

    def test_band_pass_at_49(self) -> None:
        self.assertEqual(self._run_with_score("PASSBND", 49)["conviction_band"], "pass")

    def test_band_avoid_at_34(self) -> None:
        self.assertEqual(self._run_with_score("AVOIDBN", 34)["conviction_band"], "avoid")


# ---------- asymmetry probabilities sum to 1.00 ----------


class AsymmetryValidationTests(_VaultTestBase):
    def test_rejects_probabilities_not_summing_to_one(self) -> None:
        bad_scenarios = [
            it.AsymmetryScenario(scenario="Bull", probability=0.5, target_price=1000),
            it.AsymmetryScenario(scenario="Base", probability=0.5, target_price=850),
            it.AsymmetryScenario(scenario="Neutral", probability=0.5, target_price=730),
            it.AsymmetryScenario(scenario="Bear", probability=0.5, target_price=580),
        ]
        ti = _make_default_input("NVDA", asymmetry_scenarios=bad_scenarios)
        with self.assertRaises(ValueError) as cm:
            it.generate_thesis(ti, self.tmp_vault, now=self.today)
        self.assertIn("probabilit", str(cm.exception).lower())

    def test_accepts_within_tolerance(self) -> None:
        # Float sum 0.30 + 0.45 + 0.15 + 0.10 -- common FP drift is OK.
        near_one = [
            it.AsymmetryScenario(scenario="Bull", probability=0.30, target_price=1000),
            it.AsymmetryScenario(scenario="Base", probability=0.45, target_price=850),
            it.AsymmetryScenario(scenario="Neutral", probability=0.15, target_price=730),
            it.AsymmetryScenario(scenario="Bear", probability=0.10, target_price=580),
        ]
        ti = _make_default_input("NVDA", asymmetry_scenarios=near_one)
        result = it.generate_thesis(ti, self.tmp_vault, now=self.today)
        self.assertTrue(result.written)

    def test_rejects_negative_individual_probability(self) -> None:
        """Codex R7 P2 #1: individual probabilities must be in [0,1]
        even when the sum happens to equal 1.0."""
        bad_scenarios = [
            it.AsymmetryScenario(scenario="Bull", probability=1.5, target_price=1000),
            it.AsymmetryScenario(scenario="Base", probability=0.45, target_price=850),
            it.AsymmetryScenario(scenario="Neutral", probability=0.15, target_price=730),
            it.AsymmetryScenario(scenario="Bear", probability=-0.10, target_price=580),
        ]
        ti = _make_default_input("NVDA", asymmetry_scenarios=bad_scenarios)
        with self.assertRaises(ValueError) as cm:
            it.generate_thesis(ti, self.tmp_vault, now=self.today)
        self.assertIn("[0, 1]", str(cm.exception))

    def test_rejects_missing_scenario_label(self) -> None:
        """Codex R7 P2 #1: Bull/Base/Neutral/Bear contract -- omitting
        one (or duplicating another) must fail validation."""
        bad_scenarios = [
            it.AsymmetryScenario(scenario="Bull", probability=0.30, target_price=1000),
            it.AsymmetryScenario(scenario="Base", probability=0.45, target_price=850),
            it.AsymmetryScenario(scenario="Base", probability=0.15, target_price=730),
            it.AsymmetryScenario(scenario="Bear", probability=0.10, target_price=580),
        ]
        ti = _make_default_input("NVDA", asymmetry_scenarios=bad_scenarios)
        with self.assertRaises(ValueError) as cm:
            it.generate_thesis(ti, self.tmp_vault, now=self.today)
        self.assertIn("bull", str(cm.exception).lower())
        self.assertIn("neutral", str(cm.exception).lower())


class BaseCaseProbabilityTests(_VaultTestBase):
    """Codex R7 P2 #2: base_case.probability must be in [0, 1]."""

    def test_rejects_probability_55_integer(self) -> None:
        """Common bug pattern: passing 55 (percent) instead of 0.55."""
        ti = _make_default_input(
            "NVDA",
            base_case=it.BaseCase(scenario="s", probability=55, target_price=850),
        )
        with self.assertRaises(ValueError) as cm:
            it.generate_thesis(ti, self.tmp_vault, now=self.today)
        self.assertIn("probability", str(cm.exception).lower())

    def test_rejects_negative_probability(self) -> None:
        ti = _make_default_input(
            "NVDA",
            base_case=it.BaseCase(scenario="s", probability=-0.1, target_price=850),
        )
        with self.assertRaises(ValueError) as cm:
            it.generate_thesis(ti, self.tmp_vault, now=self.today)
        self.assertIn("probability", str(cm.exception).lower())


class BearThesisSignRenderingTests(_VaultTestBase):
    """Codex R7 R2 #1: `+-17%` is a rendering bug on bear theses.
    `_fmt_pct_signed` returns a correctly-signed glyph for negative
    percentages."""

    def test_bear_thesis_renders_correctly_signed_targets(self) -> None:
        # Bear thesis: entry high, targets lower.
        bear_levels = it.EntryExitLevels(
            entry=1000.00,
            stop=1100.00,  # stop above entry for short
            targets=[
                it.Target(level="T1", price=850.00, sell_pct=33, reasoning="r"),
                it.Target(level="T2", price=700.00, sell_pct=33, reasoning="r"),
                it.Target(level="T3", price=580.00, sell_pct=34, reasoning="r"),
            ],
            risk_reward_ratio=4.0,
        )
        bear_scenarios = [
            it.AsymmetryScenario(scenario="Bull", probability=0.10, target_price=1200),
            it.AsymmetryScenario(scenario="Base", probability=0.45, target_price=800),
            it.AsymmetryScenario(scenario="Neutral", probability=0.15, target_price=1000),
            it.AsymmetryScenario(scenario="Bear", probability=0.30, target_price=500),
        ]
        ti = _make_default_input(
            "NVDA",
            entry_exit_levels=bear_levels,
            asymmetry_scenarios=bear_scenarios,
            base_case=it.BaseCase(scenario="s", probability=0.45, target_price=800),
            recommended_action="bear",
        )
        it.generate_thesis(ti, self.tmp_vault, now=self.today)
        body = (self.tmp_vault / "wiki/tickers/NVDA.md").read_text()
        # No +- or -+ hybrids anywhere
        self.assertNotIn("+-", body, "bear thesis leaked `+-` prefix")
        self.assertNotIn("-+", body, "bear thesis leaked `-+` prefix")
        # Specifically, TARGET 1 (-15%) should appear as "-15%" not "+-15%"
        self.assertIn("-15%", body)


class VaultContainmentTests(_VaultTestBase):
    """Codex R7 R2 #2: a symlinked ancestor directory (e.g. wiki/ ->
    elsewhere) must be refused."""

    def test_symlinked_wiki_dir_refused(self) -> None:
        # Set up a secondary location OUTSIDE the vault
        outside = Path(tempfile.mkdtemp(prefix="outside_vault_"))
        try:
            # Remove the seeded wiki/ and replace with a symlink to outside
            import shutil as _sh
            _sh.rmtree(self.tmp_vault / "wiki")
            (self.tmp_vault / "wiki").symlink_to(outside)
            ti = _make_default_input("NVDA")
            with self.assertRaises(ValueError) as cm:
                it.generate_thesis(ti, self.tmp_vault, now=self.today)
            self.assertIn("outside vault root", str(cm.exception).lower())
        finally:
            # Clean up
            import shutil as _sh
            _sh.rmtree(outside, ignore_errors=True)


class NextCatalystConsistencyTests(_VaultTestBase):
    """Codex R7 P2 #3 + R7 R3 #1: next_catalyst.date must be the
    soonest date in catalyst_timeline per spec §3.1 step 11, AND
    next_catalyst content must match a soonest-date row."""

    def test_rejects_next_catalyst_out_of_sync(self) -> None:
        ti = _make_default_input(
            "NVDA",
            next_catalyst=it.NextCatalyst(
                event="GTC",  # later date from the timeline
                date="2026-03-20",
                expected_impact="p",
            ),
            # catalyst_timeline has 2026-02-15 as soonest, so this
            # creates a drift.
        )
        with self.assertRaises(ValueError) as cm:
            it.generate_thesis(ti, self.tmp_vault, now=self.today)
        self.assertIn("soonest", str(cm.exception).lower())

    def test_rejects_same_date_but_wrong_event(self) -> None:
        """Codex R7 R3 #1: when multiple events share the soonest
        date, next_catalyst.event must match one of them. expected_impact
        is allowed to differ (spec §2.1 example uses divergent impact
        wording between next_catalyst and timeline)."""
        timeline = [
            it.CatalystTimelineEntry(
                date="2026-02-15",
                event="Q4 earnings",
                expected_impact="Positive",
                probability="high",
            ),
            it.CatalystTimelineEntry(
                date="2026-02-15",
                event="Dividend announcement",
                expected_impact="Neutral",
                probability="low",
            ),
        ]
        ti = _make_default_input(
            "NVDA",
            next_catalyst=it.NextCatalyst(
                event="Capital-markets day",  # not in timeline at all
                date="2026-02-15",
                expected_impact="Positive",
            ),
            catalyst_timeline=timeline,
        )
        with self.assertRaises(ValueError) as cm:
            it.generate_thesis(ti, self.tmp_vault, now=self.today)
        self.assertIn("next_catalyst.event", str(cm.exception).lower())

    def test_accepts_divergent_expected_impact_wording(self) -> None:
        """Spec §2.1 example uses divergent wording intentionally --
        the check must only enforce event + date, not expected_impact."""
        timeline = [
            it.CatalystTimelineEntry(
                date="2026-02-15",
                event="Q4 earnings",
                expected_impact="Positive -- consensus 5% upside surprise",
                probability="high",
            ),
        ]
        ti = _make_default_input(
            "NVDA",
            next_catalyst=it.NextCatalyst(
                event="Q4 earnings",  # same event
                date="2026-02-15",
                expected_impact="guidance for FY26 capex",  # different impact wording
            ),
            catalyst_timeline=timeline,
        )
        result = it.generate_thesis(ti, self.tmp_vault, now=self.today)
        self.assertTrue(result.written)


class TargetLabelOrderTests(_VaultTestBase):
    """Codex R7 R3 #2: targets must be in positional T1/T2/T3 order."""

    def test_rejects_out_of_order_labels(self) -> None:
        bad_levels = it.EntryExitLevels(
            entry=700.00,
            stop=630.00,
            targets=[
                # T2 first, T1 second -- scrambles Action Plan semantics
                it.Target(level="T2", price=900.00, sell_pct=33, reasoning="r"),
                it.Target(level="T1", price=800.00, sell_pct=33, reasoning="r"),
                it.Target(level="T3", price=1000.00, sell_pct=34, reasoning="r"),
            ],
            risk_reward_ratio=4.3,
        )
        ti = _make_default_input("NVDA", entry_exit_levels=bad_levels)
        with self.assertRaises(ValueError) as cm:
            it.generate_thesis(ti, self.tmp_vault, now=self.today)
        self.assertIn("t1", str(cm.exception).lower())

    def test_rejects_custom_labels(self) -> None:
        bad_levels = it.EntryExitLevels(
            entry=700.00,
            stop=630.00,
            targets=[
                it.Target(level="low", price=800.00, sell_pct=33, reasoning="r"),
                it.Target(level="mid", price=900.00, sell_pct=33, reasoning="r"),
                it.Target(level="high", price=1000.00, sell_pct=34, reasoning="r"),
            ],
            risk_reward_ratio=4.3,
        )
        ti = _make_default_input("NVDA", entry_exit_levels=bad_levels)
        with self.assertRaises(ValueError) as cm:
            it.generate_thesis(ti, self.tmp_vault, now=self.today)
        self.assertIn("t1", str(cm.exception).lower())


# ---------- targets sell_pct sum must be 100 ----------


class TargetsSellPctTests(_VaultTestBase):
    def test_rejects_sell_pct_not_summing_to_100(self) -> None:
        bad_levels = it.EntryExitLevels(
            entry=700.00,
            stop=630.00,
            targets=[
                it.Target(level="T1", price=800.00, sell_pct=50, reasoning="r"),
                it.Target(level="T2", price=900.00, sell_pct=50, reasoning="r"),
                it.Target(level="T3", price=1000.00, sell_pct=50, reasoning="r"),
            ],
            risk_reward_ratio=4.3,
        )
        ti = _make_default_input("NVDA", entry_exit_levels=bad_levels)
        with self.assertRaises(ValueError) as cm:
            it.generate_thesis(ti, self.tmp_vault, now=self.today)
        self.assertIn("sell_pct", str(cm.exception).lower())

    def test_rejects_four_targets(self) -> None:
        """Body formatters slice to T1/T2/T3 -- a 4th target would drop
        silently with phantom sell_pct. Closes MiniMax R2 HIGH #2."""
        bad_levels = it.EntryExitLevels(
            entry=700.00,
            stop=630.00,
            targets=[
                it.Target(level="T1", price=800.00, sell_pct=25, reasoning="r"),
                it.Target(level="T2", price=900.00, sell_pct=25, reasoning="r"),
                it.Target(level="T3", price=1000.00, sell_pct=25, reasoning="r"),
                it.Target(level="T4", price=1100.00, sell_pct=25, reasoning="r"),
            ],
            risk_reward_ratio=4.3,
        )
        ti = _make_default_input("NVDA", entry_exit_levels=bad_levels)
        with self.assertRaises(ValueError) as cm:
            it.generate_thesis(ti, self.tmp_vault, now=self.today)
        self.assertIn("exactly 3", str(cm.exception).lower())

    def test_rejects_two_targets(self) -> None:
        bad_levels = it.EntryExitLevels(
            entry=700.00,
            stop=630.00,
            targets=[
                it.Target(level="T1", price=800.00, sell_pct=50, reasoning="r"),
                it.Target(level="T2", price=900.00, sell_pct=50, reasoning="r"),
            ],
            risk_reward_ratio=4.3,
        )
        ti = _make_default_input("NVDA", entry_exit_levels=bad_levels)
        with self.assertRaises(ValueError) as cm:
            it.generate_thesis(ti, self.tmp_vault, now=self.today)
        self.assertIn("exactly 3", str(cm.exception).lower())


class PriceValidationTests(_VaultTestBase):
    """Non-positive prices are nonsensical. Closes MiniMax R2 HIGH #1."""

    def _with_levels(self, *, entry=700.0, stop=630.0, t1_price=800.0):
        return it.EntryExitLevels(
            entry=entry,
            stop=stop,
            targets=[
                it.Target(level="T1", price=t1_price, sell_pct=33, reasoning="r"),
                it.Target(level="T2", price=900.0, sell_pct=33, reasoning="r"),
                it.Target(level="T3", price=1000.0, sell_pct=34, reasoning="r"),
            ],
            risk_reward_ratio=4.3,
        )

    def test_rejects_zero_entry(self) -> None:
        ti = _make_default_input(
            "NVDA", entry_exit_levels=self._with_levels(entry=0)
        )
        with self.assertRaises(ValueError) as cm:
            it.generate_thesis(ti, self.tmp_vault, now=self.today)
        self.assertIn("entry", str(cm.exception).lower())

    def test_rejects_negative_stop(self) -> None:
        ti = _make_default_input(
            "NVDA", entry_exit_levels=self._with_levels(stop=-10)
        )
        with self.assertRaises(ValueError) as cm:
            it.generate_thesis(ti, self.tmp_vault, now=self.today)
        self.assertIn("stop", str(cm.exception).lower())

    def test_rejects_zero_target_price(self) -> None:
        ti = _make_default_input(
            "NVDA", entry_exit_levels=self._with_levels(t1_price=0)
        )
        with self.assertRaises(ValueError) as cm:
            it.generate_thesis(ti, self.tmp_vault, now=self.today)
        self.assertIn("price", str(cm.exception).lower())

    def test_rejects_zero_base_case_target_price(self) -> None:
        ti = _make_default_input(
            "NVDA",
            base_case=it.BaseCase(scenario="s", probability=0.55, target_price=0),
        )
        with self.assertRaises(ValueError) as cm:
            it.generate_thesis(ti, self.tmp_vault, now=self.today)
        self.assertIn("target_price", str(cm.exception).lower())

    def test_rejects_zero_asymmetry_scenario_price(self) -> None:
        scenarios = [
            it.AsymmetryScenario(scenario="Bull", probability=0.30, target_price=1000),
            it.AsymmetryScenario(scenario="Base", probability=0.45, target_price=850),
            it.AsymmetryScenario(scenario="Neutral", probability=0.15, target_price=0),
            it.AsymmetryScenario(scenario="Bear", probability=0.10, target_price=580),
        ]
        ti = _make_default_input("NVDA", asymmetry_scenarios=scenarios)
        with self.assertRaises(ValueError) as cm:
            it.generate_thesis(ti, self.tmp_vault, now=self.today)
        self.assertIn("target_price", str(cm.exception).lower())

    def test_rejects_negative_risk_reward_ratio(self) -> None:
        """R5 HIGH #1: risk_reward_ratio must be positive."""
        bad_levels = self._with_levels()
        bad_levels = it.EntryExitLevels(
            entry=bad_levels.entry,
            stop=bad_levels.stop,
            targets=bad_levels.targets,
            risk_reward_ratio=-2.0,
        )
        ti = _make_default_input("NVDA", entry_exit_levels=bad_levels)
        with self.assertRaises(ValueError) as cm:
            it.generate_thesis(ti, self.tmp_vault, now=self.today)
        self.assertIn("risk_reward_ratio", str(cm.exception).lower())

    def test_rejects_zero_risk_reward_ratio(self) -> None:
        bad_levels = it.EntryExitLevels(
            entry=700.0,
            stop=630.0,
            targets=[
                it.Target(level="T1", price=800.0, sell_pct=33, reasoning="r"),
                it.Target(level="T2", price=900.0, sell_pct=33, reasoning="r"),
                it.Target(level="T3", price=1000.0, sell_pct=34, reasoning="r"),
            ],
            risk_reward_ratio=0,
        )
        ti = _make_default_input("NVDA", entry_exit_levels=bad_levels)
        with self.assertRaises(ValueError) as cm:
            it.generate_thesis(ti, self.tmp_vault, now=self.today)
        self.assertIn("risk_reward_ratio", str(cm.exception).lower())


class AsymmetryScoreRangeTests(_VaultTestBase):
    """R6 MEDIUM #2: asymmetry_score must be int in 1..10."""

    def test_rejects_zero(self) -> None:
        ti = _make_default_input("NVDA", asymmetry_score=0)
        with self.assertRaises(ValueError) as cm:
            it.generate_thesis(ti, self.tmp_vault, now=self.today)
        self.assertIn("asymmetry_score", str(cm.exception).lower())

    def test_rejects_eleven(self) -> None:
        ti = _make_default_input("NVDA", asymmetry_score=11)
        with self.assertRaises(ValueError) as cm:
            it.generate_thesis(ti, self.tmp_vault, now=self.today)
        self.assertIn("asymmetry_score", str(cm.exception).lower())

    def test_accepts_one(self) -> None:
        ti = _make_default_input("NVDA", asymmetry_score=1)
        result = it.generate_thesis(ti, self.tmp_vault, now=self.today)
        self.assertTrue(result.written)

    def test_accepts_ten(self) -> None:
        ti = _make_default_input("NVDA", asymmetry_score=10)
        result = it.generate_thesis(ti, self.tmp_vault, now=self.today)
        self.assertTrue(result.written)


class DateValidationTests(_VaultTestBase):
    """R6 LOW #3: catalyst dates must be ISO-8601."""

    def test_rejects_non_iso_catalyst_date(self) -> None:
        bad_timeline = [
            it.CatalystTimelineEntry(
                date="Q4 2025",  # not ISO
                event="earnings",
                expected_impact="Positive",
                probability="high",
            ),
        ]
        ti = _make_default_input("NVDA", catalyst_timeline=bad_timeline)
        with self.assertRaises(ValueError) as cm:
            it.generate_thesis(ti, self.tmp_vault, now=self.today)
        self.assertIn("iso", str(cm.exception).lower())

    def test_rejects_non_iso_next_catalyst_date(self) -> None:
        ti = _make_default_input(
            "NVDA",
            next_catalyst=it.NextCatalyst(
                event="earnings", date="yesterday", expected_impact="p"
            ),
        )
        with self.assertRaises(ValueError) as cm:
            it.generate_thesis(ti, self.tmp_vault, now=self.today)
        self.assertIn("iso", str(cm.exception).lower())


class CatalystProbabilityEnumTests(_VaultTestBase):
    """Closed-enum probability on catalyst_timeline entries. Closes
    MiniMax R2 MEDIUM #3."""

    def test_rejects_invalid_probability(self) -> None:
        bad_timeline = [
            it.CatalystTimelineEntry(
                date="2026-02-15",
                event="Q4 earnings",
                expected_impact="Positive",
                probability="certain",  # not in enum
            ),
        ]
        ti = _make_default_input("NVDA", catalyst_timeline=bad_timeline)
        with self.assertRaises(ValueError) as cm:
            it.generate_thesis(ti, self.tmp_vault, now=self.today)
        self.assertIn("probability", str(cm.exception).lower())

    def test_accepts_low_probability(self) -> None:
        tl = [
            it.CatalystTimelineEntry(
                date="2026-02-15",
                event="Q4 earnings",
                expected_impact="Positive",
                probability="low",
            ),
            it.CatalystTimelineEntry(
                date="2026-03-20",
                event="GTC",
                expected_impact="Positive",
                probability="medium",
            ),
        ]
        ti = _make_default_input(
            "NVDA",
            catalyst_timeline=tl,
            # `next_catalyst` must agree with the soonest timeline row;
            # override to match the overridden timeline.
            next_catalyst=it.NextCatalyst(
                event="Q4 earnings",
                date="2026-02-15",
                expected_impact="Positive",
            ),
        )
        result = it.generate_thesis(ti, self.tmp_vault, now=self.today)
        self.assertTrue(result.written)


# ---------- Action Plan Summary: POSITION line is literal ----------


class ActionPlanSummaryTests(_VaultTestBase):
    LITERAL_POSITION = (
        "POSITION:      validator-owned (see config.yaml position_size cap)"
    )

    def test_action_plan_position_is_literal(self) -> None:
        ti = _make_default_input("NVDA")
        it.generate_thesis(ti, self.tmp_vault, now=self.today)
        body = (self.tmp_vault / "wiki/tickers/NVDA.md").read_text()
        self.assertIn(self.LITERAL_POSITION, body)

    def test_action_plan_contains_all_required_lines(self) -> None:
        ti = _make_default_input("NVDA")
        it.generate_thesis(ti, self.tmp_vault, now=self.today)
        body = (self.tmp_vault / "wiki/tickers/NVDA.md").read_text()
        # Verify the 11 Action-Plan lines (trade-thesis SKILL.md §10).
        for prefix in (
            "TICKER:",
            "DIRECTION:",
            "ENTRY:",
            "STOP LOSS:",
            "TARGET 1:",
            "TARGET 2:",
            "TARGET 3:",
            "RISK/REWARD:",
            "POSITION:",
            "TIMEFRAME:",
            "NEXT CATALYST:",
        ):
            self.assertIn(prefix, body, f"Action Plan missing line: {prefix}")


# ---------- glossary stubbing ----------


class GlossaryStubTests(_VaultTestBase):
    def test_creates_glossary_stub_if_missing(self) -> None:
        # Remove the seeded glossary
        (self.tmp_vault / "wiki/reference/glossary.md").unlink()
        ti = _make_default_input("NVDA")
        it.generate_thesis(ti, self.tmp_vault, now=self.today)
        self.assertTrue((self.tmp_vault / "wiki/reference/glossary.md").exists())

    def test_appends_pending_stub_for_unknown_term(self) -> None:
        """If a TERM_LIST term appears in the body and is not yet in
        the glossary, the skill appends a pending-stub section."""
        # Ensure seeded glossary has no "moat" heading
        glossary_path = self.tmp_vault / "wiki/reference/glossary.md"
        self.assertNotIn("## moat", glossary_path.read_text())
        ti = _make_default_input("NVDA")  # default body mentions "moat"
        it.generate_thesis(ti, self.tmp_vault, now=self.today)
        glossary = glossary_path.read_text()
        self.assertIn("## moat", glossary.lower())
        self.assertIn("pending", glossary.lower())

    def test_no_duplicate_stub_if_term_already_present(self) -> None:
        """Under-lock re-check prevents double-appending a term. If
        another process landed the stub between our pre-lock read and
        our lock acquisition (simulated here by seeding the glossary
        with `## moat`), we MUST NOT append a second one. Closes
        MiniMax R2 TOCTOU finding.
        """
        glossary_path = self.tmp_vault / "wiki/reference/glossary.md"
        # Pre-populate "## moat" as if another process appended it
        with glossary_path.open("a") as f:
            f.write("\n## moat\n\n_pre-existing definition_\n")

        ti = _make_default_input("NVDA")  # body mentions "moat"
        it.generate_thesis(ti, self.tmp_vault, now=self.today)

        glossary = glossary_path.read_text()
        moat_occurrences = sum(
            1 for line in glossary.splitlines()
            if line.strip().lower() == "## moat"
        )
        self.assertEqual(
            moat_occurrences, 1,
            f"Expected exactly one `## moat` heading; got {moat_occurrences}",
        )

    def test_lock_file_is_hidden_dotfile(self) -> None:
        """The glossary lock file must be dot-prefixed (hidden from
        casual `ls`) so it does not clutter Obsidian's file tree."""
        ti = _make_default_input("NVDA")
        it.generate_thesis(ti, self.tmp_vault, now=self.today)
        ref_dir = self.tmp_vault / "wiki/reference"
        visible_extras = [
            p.name for p in ref_dir.iterdir()
            if not p.name.startswith(".") and p.name not in {"glossary.md", "index.md"}
        ]
        self.assertEqual(
            visible_extras, [],
            f"Unexpected visible files next to glossary.md: {visible_extras}",
        )

    def test_under_lock_recheck_simulation(self) -> None:
        """White-box: patch `_read_glossary_headings` so the pre-lock
        cheap read returns a stale (empty) view, while the
        `_parse_glossary_headings` call under the lock sees the current
        state with `## moat` already present. Verifies the under-lock
        re-check is the source of truth, not the pre-lock hint.
        """
        glossary_path = self.tmp_vault / "wiki/reference/glossary.md"
        with glossary_path.open("a") as f:
            f.write("\n## moat\n\n_written by another process_\n")

        # Force the pre-lock check to return an empty set so "moat"
        # appears in candidate_missing -- triggering the under-lock path.
        with mock.patch.object(
            it, "_read_glossary_headings", return_value=set(),
        ):
            ti = _make_default_input("NVDA")
            it.generate_thesis(ti, self.tmp_vault, now=self.today)

        glossary = glossary_path.read_text()
        moat_headings = sum(
            1 for line in glossary.splitlines()
            if line.strip().lower() == "## moat"
        )
        self.assertEqual(
            moat_headings, 1,
            "Under-lock re-check should have seen the pre-existing "
            "`## moat` and declined to double-add",
        )


# ---------- idempotence / result shape ----------


class ResultShapeTests(_VaultTestBase):
    def test_result_written_true_on_first_run(self) -> None:
        ti = _make_default_input("NVDA")
        result = it.generate_thesis(ti, self.tmp_vault, now=self.today)
        self.assertTrue(result.written)
        self.assertIsNone(result.skipped_reason)
        self.assertEqual(result.path, self.tmp_vault / "wiki/tickers/NVDA.md")

    def test_result_written_false_on_freshness_skip(self) -> None:
        ti = _make_default_input("NVDA")
        it.generate_thesis(ti, self.tmp_vault, now=self.today)
        result = it.generate_thesis(ti, self.tmp_vault, now=self.today)
        self.assertFalse(result.written)
        self.assertIsNotNone(result.skipped_reason)


# ---------- R3 HIGH #3: vault_root directory validation ----------


class VaultRootValidationTests(unittest.TestCase):
    def test_rejects_nonexistent_vault_root(self) -> None:
        import datetime as _dt
        ti = _make_default_input("NVDA")
        bogus = Path("/nonexistent-dir-invest-thesis-test-never-created")
        with self.assertRaises(ValueError) as cm:
            it.generate_thesis(ti, bogus, now=_dt.date(2026, 4, 19))
        self.assertIn("vault_root", str(cm.exception).lower())

    def test_rejects_file_as_vault_root(self) -> None:
        import datetime as _dt
        with tempfile.NamedTemporaryFile(suffix=".tmp", delete=False) as f:
            file_path = Path(f.name)
        try:
            ti = _make_default_input("NVDA")
            with self.assertRaises(ValueError) as cm:
                it.generate_thesis(ti, file_path, now=_dt.date(2026, 4, 19))
            self.assertIn("not an existing directory", str(cm.exception).lower())
        finally:
            file_path.unlink(missing_ok=True)


# ---------- R3 HIGH #1: atomic_write refuses symlinked path ----------


class AtomicWriteSymlinkRefusalTests(_VaultTestBase):
    def test_refuses_to_write_through_symlink(self) -> None:
        """atomic_write_bytes must refuse when the target path is a
        symlink. POSIX rename(2) semantics make the attack surface
        small on Linux/macOS, but explicit refusal is defence-in-depth
        + matches the R3 HIGH #1 finding guidance."""
        target = self.tmp_vault / "wiki/tickers/NVDA.md"
        target.parent.mkdir(parents=True, exist_ok=True)
        # Create a symlink where the ticker file would go, pointing at
        # a decoy file we do NOT want overwritten.
        decoy = self.tmp_vault / "decoy.txt"
        decoy.write_text("DECOY ORIGINAL CONTENT\n")
        target.symlink_to(decoy)

        ti = _make_default_input("NVDA")
        with self.assertRaises(ValueError) as cm:
            it.generate_thesis(ti, self.tmp_vault, now=self.today)
        self.assertIn("symlink", str(cm.exception).lower())
        # Decoy content intact (symlink target untouched)
        self.assertEqual(decoy.read_text(), "DECOY ORIGINAL CONTENT\n")


# ---------- R3 HIGH #2: glossary OSError surfaces to stderr ----------


class GlossaryErrorSurfacingTests(_VaultTestBase):
    def test_glossary_permission_error_prints_warning_but_preserves_thesis(
        self,
    ) -> None:
        """OSError in glossary update must not be silently swallowed.
        The primary thesis file must still exist, and a warning must
        surface to stderr so disk-full / permission-denied are
        diagnosable. Closes R3 HIGH #2.
        """
        import io
        ti = _make_default_input("NVDA")
        # Force _update_glossary to raise PermissionError
        captured = io.StringIO()
        with mock.patch.object(
            it, "_update_glossary",
            side_effect=PermissionError("simulated EACCES on glossary"),
        ), mock.patch.object(sys, "stderr", captured):
            result = it.generate_thesis(ti, self.tmp_vault, now=self.today)

        # Thesis itself still written
        self.assertTrue(result.written)
        self.assertTrue((self.tmp_vault / "wiki/tickers/NVDA.md").exists())
        # Warning surfaced
        stderr_text = captured.getvalue()
        self.assertIn("glossary update failed", stderr_text)
        self.assertIn("PermissionError", stderr_text)

    def test_glossary_symlink_rejection_surfaces_but_preserves_thesis(
        self,
    ) -> None:
        """Bundle 4 R4 HIGH #1: a symlink at the glossary path causes
        `atomic_write_bytes` to raise ValueError. That ValueError must
        be caught (alongside OSError) so the thesis itself is not
        aborted -- the glossary layout is orthogonal to the primary
        output."""
        import io
        ti = _make_default_input("NVDA")
        # Replace the glossary with a symlink pointing to a decoy file
        glossary = self.tmp_vault / "wiki/reference/glossary.md"
        glossary.unlink()
        decoy = self.tmp_vault / "decoy.txt"
        decoy.write_text("DECOY\n")
        glossary.symlink_to(decoy)

        captured = io.StringIO()
        with mock.patch.object(sys, "stderr", captured):
            result = it.generate_thesis(ti, self.tmp_vault, now=self.today)

        # Thesis itself still written despite glossary symlink refusal
        self.assertTrue(result.written)
        self.assertTrue((self.tmp_vault / "wiki/tickers/NVDA.md").exists())
        # Decoy untouched
        self.assertEqual(decoy.read_text(), "DECOY\n")
        # Warning surfaced
        stderr_text = captured.getvalue()
        self.assertIn("glossary update failed", stderr_text)
        self.assertIn("ValueError", stderr_text)


class OrphanTempfileCleanupTests(_VaultTestBase):
    """Bundle 4 R4 MEDIUM #2: scoped startup cleanup for dot-prefixed
    tempfiles left behind by a SIGKILL between fsync and os.replace in
    a prior run. Scope must stay scoped to the current symbol to keep
    the scan O(1)."""

    def test_cleans_up_leftover_nvda_tempfile(self) -> None:
        import os as _os
        tickers = self.tmp_vault / "wiki/tickers"
        tickers.mkdir(parents=True, exist_ok=True)
        leftover = tickers / ".NVDA.md.tmp.abc123"
        leftover.write_text("stale tempfile content\n")
        # Backdate mtime so the cleanup considers it aged-enough
        old = time.time() - (it.ORPHAN_TEMPFILE_MIN_AGE_SECONDS + 10)
        _os.utime(leftover, (old, old))
        ti = _make_default_input("NVDA")
        it.generate_thesis(ti, self.tmp_vault, now=self.today)
        self.assertFalse(
            leftover.exists(),
            f"orphan tempfile was not cleaned: {leftover}",
        )

    def test_does_not_touch_other_symbols_tempfiles(self) -> None:
        """Cleanup scope is the current symbol; a SPY tempfile must
        survive a NVDA run."""
        tickers = self.tmp_vault / "wiki/tickers"
        tickers.mkdir(parents=True, exist_ok=True)
        other = tickers / ".SPY.md.tmp.zzz"
        other.write_text("foreign tempfile\n")
        ti = _make_default_input("NVDA")
        it.generate_thesis(ti, self.tmp_vault, now=self.today)
        self.assertTrue(
            other.exists(),
            "SPY tempfile was cleaned during NVDA run",
        )

    def test_recent_tempfile_preserved_peer_writer_race_guard(self) -> None:
        """Codex R7 R5 #2: a dot-prefixed tempfile younger than
        ORPHAN_TEMPFILE_MIN_AGE_SECONDS must be left alone -- it may
        be an active peer writer's tempfile that has not yet reached
        os.replace."""
        tickers = self.tmp_vault / "wiki/tickers"
        tickers.mkdir(parents=True, exist_ok=True)
        fresh = tickers / ".NVDA.md.tmp.peer_writer"
        fresh.write_text("recent content\n")
        # Default mtime is `now` for a newly-written file; don't touch.
        ti = _make_default_input("NVDA")
        it.generate_thesis(ti, self.tmp_vault, now=self.today)
        self.assertTrue(
            fresh.exists(),
            "a fresh peer-writer tempfile was cleaned; race guard failed",
        )


class EmptyCatalystTimelineTests(_VaultTestBase):
    """Codex R7 R5 #1: timeline must have at least one entry because
    next_catalyst is derived from the soonest row."""

    def test_rejects_empty_timeline(self) -> None:
        ti = _make_default_input("NVDA", catalyst_timeline=[])
        with self.assertRaises(ValueError) as cm:
            it.generate_thesis(ti, self.tmp_vault, now=self.today)
        self.assertIn("at least one", str(cm.exception).lower())


# ---------- Phase 3.8.6 MVP-2: verification gate ----------


class VerificationGateTests(_VaultTestBase):
    """Five new test cases for the curated info set verification gate.
    Covers PASS, OVERRIDE, REFUSE, ADVISORY-ONLY, and INVALID states.
    """

    def _make_verification(
        self,
        status: str,
        claims: list[it.ClaimVerification],
        override_reason: str | None = None,
        refuse_reason: str | None = None,
    ) -> it.Verification:
        return it.Verification(
            completed_at="2026-04-29T16:25:00+08:00",
            claims=claims,
            status=status,
            override_reason=override_reason,
            refuse_reason=refuse_reason,
        )

    def test_pass_all_load_bearing_verified(self) -> None:
        """PASS case: all load-bearing claims verified → write succeeds;
        frontmatter contains the verification block."""
        v = self._make_verification(
            status="pass",
            claims=[
                it.ClaimVerification(
                    claim_id="bull-1-evidence",
                    claim_text="Q3 2025 hyperscaler capex +47% YoY",
                    claim_load_bearing=True,
                    source_url="https://example.com/msft-capex",
                    operator_check="verified",
                ),
                it.ClaimVerification(
                    claim_id="phase-3-numeric-revenue",
                    claim_text="Revenue growth 47% YoY Q3 2025",
                    claim_load_bearing=True,
                    source_url="https://example.com/nvda-10q",
                    operator_check="verified",
                ),
            ],
        )
        ti = _make_default_input("NVDA", verification=v)
        result = it.generate_thesis(ti, self.tmp_vault, now=self.today)
        self.assertTrue(result.written)
        fm = sf.parse((self.tmp_vault / "wiki/tickers/NVDA.md").read_bytes())
        self.assertIn("verification", fm)
        self.assertEqual(fm["verification"]["status"], "pass")
        self.assertEqual(len(fm["verification"]["claims"]), 2)

    def test_pass_with_mixed_verified_and_advisory(self) -> None:
        """PASS with load-bearing verified + non-load-bearing advisory
        → write succeeds; both claim types appear in frontmatter."""
        v = self._make_verification(
            status="pass",
            claims=[
                it.ClaimVerification(
                    claim_id="bull-1-evidence",
                    claim_text="Q3 2025 hyperscaler capex +47% YoY",
                    claim_load_bearing=True,
                    source_url="https://example.com/msft-capex",
                    operator_check="verified",
                ),
                it.ClaimVerification(
                    claim_id="color-commentary-1",
                    claim_text="CEO tone was bullish on earnings call",
                    claim_load_bearing=False,
                    source_url=None,
                    operator_check="advisory",
                ),
            ],
        )
        ti = _make_default_input("NVDA", verification=v)
        result = it.generate_thesis(ti, self.tmp_vault, now=self.today)
        self.assertTrue(result.written)
        fm = sf.parse((self.tmp_vault / "wiki/tickers/NVDA.md").read_bytes())
        self.assertEqual(fm["verification"]["status"], "pass")
        self.assertEqual(len(fm["verification"]["claims"]), 2)

    def test_override_one_load_bearing_refused(self) -> None:
        """OVERRIDE case: one load-bearing claim refused BUT
        status == operator-override with reason >= 20 chars → write
        succeeds; frontmatter captures override."""
        v = self._make_verification(
            status="operator-override",
            override_reason="Operator accepts risk of unverified claim",
            claims=[
                it.ClaimVerification(
                    claim_id="bull-1-evidence",
                    claim_text="Q3 2025 hyperscaler capex +47% YoY",
                    claim_load_bearing=True,
                    source_url="https://example.com/msft-capex",
                    operator_check="verified",
                ),
                it.ClaimVerification(
                    claim_id="bear-1-evidence",
                    claim_text="Top-3 customers = 62% of DC revenue",
                    claim_load_bearing=True,
                    source_url="https://example.com/nvda-10q",
                    operator_check="refused",
                    operator_note="10-Q footnote missing from source",
                ),
            ],
        )
        ti = _make_default_input("NVDA", verification=v)
        result = it.generate_thesis(ti, self.tmp_vault, now=self.today)
        self.assertTrue(result.written)
        fm = sf.parse((self.tmp_vault / "wiki/tickers/NVDA.md").read_bytes())
        self.assertEqual(fm["verification"]["status"], "operator-override")
        self.assertEqual(
            fm["verification"]["override_reason"],
            "Operator accepts risk of unverified claim",
        )

    def test_refuse_status_raises_before_write(self) -> None:
        """REFUSE case: status == refuse with reason >= 20 chars →
        validate_verification raises ValueError; no file written; no
        glossary stubs appended."""
        v = self._make_verification(
            status="refuse",
            refuse_reason="Unverified load-bearing claims cannot proceed",
            claims=[
                it.ClaimVerification(
                    claim_id="bull-1-evidence",
                    claim_text="Q3 2025 hyperscaler capex +47% YoY",
                    claim_load_bearing=True,
                    source_url="https://example.com/msft-capex",
                    operator_check="refused",
                    operator_note="Source does not contain this claim",
                ),
            ],
        )
        ti = _make_default_input("NVDA", verification=v)
        with self.assertRaises(ValueError) as cm:
            it.generate_thesis(ti, self.tmp_vault, now=self.today)
        exc_text = str(cm.exception)
        self.assertIn("verification gate refused", exc_text)
        self.assertIn(
            "Unverified load-bearing claims cannot proceed", exc_text
        )
        self.assertFalse(
            (self.tmp_vault / "wiki/tickers/NVDA.md").exists(),
            "file must not be written on refusal",
        )

    def test_advisory_only_non_load_bearing(self) -> None:
        """ADVISORY-ONLY case: all load-bearing claims verified;
        some non-load-bearing claims marked advisory; status == pass
        → write succeeds."""
        v = self._make_verification(
            status="pass",
            claims=[
                it.ClaimVerification(
                    claim_id="bull-1-evidence",
                    claim_text="Q3 2025 hyperscaler capex +47% YoY",
                    claim_load_bearing=True,
                    source_url="https://example.com/msft-capex",
                    operator_check="verified",
                ),
                it.ClaimVerification(
                    claim_id="color-commentary-1",
                    claim_text="CEO tone was bullish on earnings call",
                    claim_load_bearing=False,
                    source_url=None,
                    operator_check="advisory",
                ),
            ],
        )
        ti = _make_default_input("NVDA", verification=v)
        result = it.generate_thesis(ti, self.tmp_vault, now=self.today)
        self.assertTrue(result.written)
        fm = sf.parse((self.tmp_vault / "wiki/tickers/NVDA.md").read_bytes())
        self.assertEqual(fm["verification"]["status"], "pass")

    def test_invalid_load_bearing_refused_with_pass_status(self) -> None:
        """INVALID case: load-bearing claim operator_check == refused
        AND status == pass → raises ValueError (validator must catch
        the contradiction)."""
        v = self._make_verification(
            status="pass",
            claims=[
                it.ClaimVerification(
                    claim_id="bull-1-evidence",
                    claim_text="Q3 2025 hyperscaler capex +47% YoY",
                    claim_load_bearing=True,
                    source_url="https://example.com/msft-capex",
                    operator_check="refused",
                    operator_note="Source does not contain this claim",
                ),
            ],
        )
        ti = _make_default_input("NVDA", verification=v)
        with self.assertRaises(ValueError) as cm:
            it.generate_thesis(ti, self.tmp_vault, now=self.today)
        self.assertIn("pass", str(cm.exception).lower())
        self.assertIn("load-bearing", str(cm.exception).lower())

    def test_rejects_empty_claims_list(self) -> None:
        """Empty claims list bypasses the gate → raise ValueError."""
        v = self._make_verification(status="pass", claims=[])
        ti = _make_default_input("NVDA", verification=v)
        with self.assertRaises(ValueError) as cm:
            it.generate_thesis(ti, self.tmp_vault, now=self.today)
        self.assertIn("empty", str(cm.exception).lower())

    def test_rejects_override_with_all_verified_claims(self) -> None:
        """operator-override with no refused load-bearing claims is
        meaningless audit noise → raise ValueError."""
        v = self._make_verification(
            status="operator-override",
            override_reason="Operator accepts risk of unverified claim",
            claims=[
                it.ClaimVerification(
                    claim_id="bull-1-evidence",
                    claim_text="Q3 2025 hyperscaler capex +47% YoY",
                    claim_load_bearing=True,
                    source_url="https://example.com/msft-capex",
                    operator_check="verified",
                ),
            ],
        )
        ti = _make_default_input("NVDA", verification=v)
        with self.assertRaises(ValueError) as cm:
            it.generate_thesis(ti, self.tmp_vault, now=self.today)
        self.assertIn("operator-override", str(cm.exception).lower())
        self.assertIn("load-bearing", str(cm.exception).lower())

    def test_override_reason_19_chars_rejected(self) -> None:
        v = self._make_verification(
            status="operator-override",
            override_reason="x" * 19,
            claims=[
                it.ClaimVerification(
                    claim_id="bull-1-evidence",
                    claim_text="Q3 2025 hyperscaler capex +47% YoY",
                    claim_load_bearing=True,
                    source_url="https://example.com/msft-capex",
                    operator_check="refused",
                    operator_note="Source does not contain this claim",
                ),
            ],
        )
        ti = _make_default_input("NVDA", verification=v)
        with self.assertRaises(ValueError) as cm:
            it.generate_thesis(ti, self.tmp_vault, now=self.today)
        self.assertIn("override_reason", str(cm.exception).lower())

    def test_override_reason_20_chars_accepted(self) -> None:
        v = self._make_verification(
            status="operator-override",
            override_reason="x" * 20,
            claims=[
                it.ClaimVerification(
                    claim_id="bull-1-evidence",
                    claim_text="Q3 2025 hyperscaler capex +47% YoY",
                    claim_load_bearing=True,
                    source_url="https://example.com/msft-capex",
                    operator_check="refused",
                    operator_note="Source does not contain this claim",
                ),
            ],
        )
        ti = _make_default_input("NVDA", verification=v)
        result = it.generate_thesis(ti, self.tmp_vault, now=self.today)
        self.assertTrue(result.written)

    def test_refuse_reason_19_chars_rejected(self) -> None:
        v = self._make_verification(
            status="refuse",
            refuse_reason="x" * 19,
            claims=[
                it.ClaimVerification(
                    claim_id="bull-1-evidence",
                    claim_text="Q3 2025 hyperscaler capex +47% YoY",
                    claim_load_bearing=True,
                    source_url="https://example.com/msft-capex",
                    operator_check="refused",
                    operator_note="Source does not contain this claim",
                ),
            ],
        )
        ti = _make_default_input("NVDA", verification=v)
        with self.assertRaises(ValueError) as cm:
            it.generate_thesis(ti, self.tmp_vault, now=self.today)
        self.assertIn("refuse_reason", str(cm.exception).lower())

    def test_refuse_reason_20_chars_accepted(self) -> None:
        v = self._make_verification(
            status="refuse",
            refuse_reason="x" * 20,
            claims=[
                it.ClaimVerification(
                    claim_id="bull-1-evidence",
                    claim_text="Q3 2025 hyperscaler capex +47% YoY",
                    claim_load_bearing=True,
                    source_url="https://example.com/msft-capex",
                    operator_check="refused",
                    operator_note="Source does not contain this claim",
                ),
            ],
        )
        ti = _make_default_input("NVDA", verification=v)
        with self.assertRaises(ValueError) as cm:
            it.generate_thesis(ti, self.tmp_vault, now=self.today)
        self.assertIn("verification gate refused", str(cm.exception))


if __name__ == "__main__":
    unittest.main()
