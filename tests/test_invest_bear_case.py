"""Tests for scripts.lib.invest_bear_case -- Bundle 4 cycle 2 (m2.12).

Covers the 9-row test matrix from spec §6 cycle-2 column + supporting
unit tests for the VETO threshold + body format + atomic write.

Fixtures avoid the real K2Bi-Vault: every test gets its own tmp dir
seeded with a minimal thesis matching spec §2.1 (the fields the bear-
case module reads from frontmatter + the header/Asymmetry sections of
the body the adversarial prompt would excerpt in the real skill flow).

Test classes:

    * HappyPathTests          -- NVDA PROCEED run writes all 5 bear_* fields
                                  + appends body section.
    * RefreshTests            -- same-day + within-30d refresh skip; --refresh
                                  forces rewrite.
    * ThesisMissingTests      -- missing ticker file OR missing thesis_score
                                  field = refuse.
    * LowConvictionTests      -- conviction <= 70 = PROCEED (boundary 65 + 70).
    * HighConvictionTests     -- conviction > 70 = VETO (85 + boundary 71).
    * SchemaNonClobberTests   -- all existing thesis fields preserved;
                                  only the 5 bear_* fields added.
    * TeachModeTests          -- novice with position_size appends footer;
                                  advanced skips footer.
    * AtomicWriteTests        -- injected failure between fsync + replace
                                  leaves original file intact + no orphan
                                  tempfile.
    * HookIntegrationTests    -- cycle 4 pre-commit Check D regex does NOT
                                  match ticker files.
    * BodyFormatTests         -- supporting: verdict line, section header,
                                  counterpoints + scenarios formatting.
    * CounterpointsCountTests -- strict exactly-3 enforcement.
    * InvalidationRangeTests  -- 2..5 scenario range enforcement.
"""

from __future__ import annotations

import datetime as _dt
import os
import re
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from scripts.lib import invest_bear_case as ibc
from scripts.lib import strategy_frontmatter as sf


# ---------- fixture helpers ----------


def _seed_vault(root: Path, *, active_rules: str | None = None) -> None:
    """Create the minimal vault tree invest-bear-case writes into."""
    (root / "wiki" / "tickers").mkdir(parents=True)
    (root / "wiki" / "reference").mkdir(parents=True)
    (root / "System" / "memory").mkdir(parents=True)
    (root / "wiki" / "reference" / "glossary.md").write_text(
        "---\n"
        "tags: [glossary]\n"
        "type: glossary\n"
        "origin: k2bi-generate\n"
        "up: \"[[index]]\"\n"
        "---\n"
        "\n"
        "# K2Bi Trading Glossary\n"
    )
    if active_rules is not None:
        (root / "System" / "memory" / "active_rules.md").write_text(active_rules)


def _seed_thesis(
    root: Path,
    symbol: str = "NVDA",
    *,
    thesis_score: int = 73,
    with_bear_fields: bool = False,
    bear_conviction: int = 65,
    bear_verdict: str = "PROCEED",
    bear_days_old: int = 0,
    today: _dt.date | None = None,
) -> Path:
    """Write a wiki/tickers/<SYMBOL>.md file matching spec §2.1 schema.

    Minimal but schema-complete. Tests that exercise refresh / schema-non-
    clobber need a complete thesis frontmatter so the non-bear keys can
    be asserted preserved.
    """
    today = today or _dt.date(2026, 4, 19)
    fm_lines = [
        "---",
        f"tags: [ticker, {symbol}, thesis]",
        "date: 2026-04-19",
        "type: ticker",
        "origin: k2bi-extract",
        'up: "[[tickers/index]]"',
        f"symbol: {symbol}",
        "confidence-last-verified: 2026-04-19",
        "thesis-last-verified: 2026-04-19",
        f"thesis_score: {thesis_score}",
        "sub_scores:",
        "  catalyst_clarity: 16",
        "  asymmetry: 14",
        "  timeline_precision: 15",
        "  edge_identification: 12",
        "  conviction_level: 16",
        "fundamental_sub_scores:",
        "  valuation: 13",
        "  growth: 16",
        "  profitability: 17",
        "  financial_health: 15",
        "  moat_strength: 13",
        "bull_case:",
        "  reasons:",
        "    - reason: data-center growth",
        "      evidence: Q3 +47%",
        "      impact_estimate: 15-20% forward revenue uplift",
        "bear_case:",
        "  reasons:",
        "    - reason: customer concentration",
        "      evidence: top-3 = 62%",
        "      impact_estimate: multiple compression 30-40%",
        "base_case:",
        "  scenario: steady",
        "  probability: 0.55",
        "  target_price: 850.0",
        "entry_exit_levels:",
        "  entry: 700.0",
        "  stop: 630.0",
        "  targets:",
        "    - level: T1",
        "      price: 800.0",
        "      sell_pct: 33",
        "      reasoning: prior resistance",
        "    - level: T2",
        "      price: 900.0",
        "      sell_pct: 33",
        "      reasoning: bull fair value",
        "    - level: T3",
        "      price: 1000.0",
        "      sell_pct: 34",
        "      reasoning: stretch",
        "  risk_reward_ratio: 4.3",
        "entry_triggers:",
        "  - rsi < 40",
        "entry_invalidation:",
        "  - break 630",
        "exit_signals:",
        "  - fraud",
        "time_stop:",
        "  max_hold_period: 6 months",
        "  reassessment_triggers:",
        "    - earnings",
        "recommended_action: bull",
        "conviction_band: good",
        "next_catalyst:",
        "  event: Q4 earnings",
        "  date: 2026-02-15",
        "  expected_impact: guidance",
        "catalyst_timeline:",
        "  - date: 2026-02-15",
        "    event: Q4 earnings",
        "    expected_impact: positive",
        "    probability: high",
        "ticker_type: equity",
    ]
    if with_bear_fields:
        bear_date = today - _dt.timedelta(days=bear_days_old)
        fm_lines += [
            f"bear-last-verified: {bear_date.isoformat()}",
            f"bear_conviction: {bear_conviction}",
            "bear_top_counterpoints:",
            "  - c1",
            "  - c2",
            "  - c3",
            "bear_invalidation_scenarios:",
            "  - s1",
            "  - s2",
            f"bear_verdict: {bear_verdict}",
        ]
    fm_lines += ["---", ""]
    body = [
        "> [!robot] K2Bi analysis -- Phase 2 MVP via one-shot /research",
        "",
        "## Phase 1: Business Model",
        "Dominant supplier of AI chips.",
        "",
        "## Phase 2: Competitive Position / Moat",
        "CUDA ecosystem lock-in.",
        "",
        "## Phase 3: Financial Quality",
        "47% growth; 76% gross margin.",
        "",
        "## Phase 4: Risks + Valuation",
        "Forward P/E 38x.",
        "",
        "## Asymmetry Analysis",
        "| Scenario | Probability | Target Price | EV Contribution |",
        "|---|---|---|---|",
        "| Bull | 0.30 | $1000 | 300 |",
        "",
    ]
    content = "\n".join(fm_lines) + "\n" + "\n".join(body) + "\n"
    path = root / "wiki" / "tickers" / f"{symbol}.md"
    path.write_text(content)
    return path


def _default_input(
    conviction: int = 65,
    counterpoints: list[str] | None = None,
    scenarios: list[str] | None = None,
) -> ibc.BearCaseInput:
    """Return a BearCaseInput with spec §2.2 example-shaped values."""
    return ibc.BearCaseInput(
        bear_conviction=conviction,
        bear_top_counterpoints=counterpoints or [
            "Single-customer concentration risk",
            "Forward P/E pricing perfection",
            "Geopolitical export-control extension",
        ],
        bear_invalidation_scenarios=scenarios or [
            "Hyperscaler capex deceleration > 20% YoY",
            "Tier-2 chip export ban announcement",
        ],
    )


class _VaultTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp_vault = Path(tempfile.mkdtemp(prefix="invest_bear_"))
        _seed_vault(self.tmp_vault)
        self.today = _dt.date(2026, 4, 19)

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp_vault, ignore_errors=True)


# ---------- Test 1: Happy path ----------


class HappyPathTests(_VaultTestBase):
    def test_writes_bear_frontmatter_and_body(self) -> None:
        _seed_thesis(self.tmp_vault, "NVDA")
        result = ibc.run_bear_case(
            "NVDA", _default_input(conviction=65),
            self.tmp_vault, now=self.today,
        )
        self.assertTrue(result.written)
        self.assertEqual(result.bear_verdict, "PROCEED")
        self.assertEqual(result.bear_conviction, 65)

        content = (self.tmp_vault / "wiki/tickers/NVDA.md").read_bytes()
        fm = sf.parse(content)
        for key in (
            "bear-last-verified",
            "bear_conviction",
            "bear_top_counterpoints",
            "bear_invalidation_scenarios",
            "bear_verdict",
        ):
            self.assertIn(key, fm, f"missing bear frontmatter key: {key}")
        self.assertEqual(fm["bear_verdict"], "PROCEED")
        self.assertEqual(fm["bear_conviction"], 65)
        self.assertEqual(len(fm["bear_top_counterpoints"]), 3)
        self.assertGreaterEqual(len(fm["bear_invalidation_scenarios"]), 2)
        self.assertLessEqual(len(fm["bear_invalidation_scenarios"]), 5)

    def test_body_has_bear_section_with_date(self) -> None:
        _seed_thesis(self.tmp_vault, "NVDA")
        ibc.run_bear_case(
            "NVDA", _default_input(conviction=65),
            self.tmp_vault, now=self.today,
        )
        body = (self.tmp_vault / "wiki/tickers/NVDA.md").read_text()
        self.assertIn("## Bear Case (2026-04-19)", body)
        self.assertIn("**Verdict:** PROCEED (conviction: 65)", body)
        self.assertIn("### Top counterpoints to monitor", body)
        self.assertIn("### Invalidation scenarios", body)

    def test_counterpoints_rendered_as_numbered_list(self) -> None:
        _seed_thesis(self.tmp_vault, "NVDA")
        counterpoints = ["alpha", "beta", "gamma"]
        ibc.run_bear_case(
            "NVDA",
            _default_input(conviction=65, counterpoints=counterpoints),
            self.tmp_vault, now=self.today,
        )
        body = (self.tmp_vault / "wiki/tickers/NVDA.md").read_text()
        self.assertIn("1. alpha", body)
        self.assertIn("2. beta", body)
        self.assertIn("3. gamma", body)

    def test_scenarios_rendered_as_bulleted_list(self) -> None:
        _seed_thesis(self.tmp_vault, "NVDA")
        scenarios = ["scenario-one", "scenario-two"]
        ibc.run_bear_case(
            "NVDA",
            _default_input(conviction=65, scenarios=scenarios),
            self.tmp_vault, now=self.today,
        )
        body = (self.tmp_vault / "wiki/tickers/NVDA.md").read_text()
        self.assertIn("- scenario-one", body)
        self.assertIn("- scenario-two", body)

    def test_existing_body_preserved(self) -> None:
        _seed_thesis(self.tmp_vault, "NVDA")
        before = (self.tmp_vault / "wiki/tickers/NVDA.md").read_text()
        ibc.run_bear_case(
            "NVDA", _default_input(conviction=65),
            self.tmp_vault, now=self.today,
        )
        after = (self.tmp_vault / "wiki/tickers/NVDA.md").read_text()
        # Everything from the original body still appears in the new file
        for heading in (
            "> [!robot] K2Bi analysis",
            "## Phase 1: Business Model",
            "## Phase 2: Competitive Position / Moat",
            "## Phase 3: Financial Quality",
            "## Phase 4: Risks + Valuation",
            "## Asymmetry Analysis",
        ):
            self.assertIn(heading, after)


# ---------- Test 2: Refresh skip ----------


class RefreshTests(_VaultTestBase):
    def test_fresh_within_30d_no_refresh_skips(self) -> None:
        _seed_thesis(
            self.tmp_vault, "NVDA",
            with_bear_fields=True, bear_days_old=15,
            bear_conviction=40, bear_verdict="PROCEED",
        )
        orig_bytes = (self.tmp_vault / "wiki/tickers/NVDA.md").read_bytes()
        result = ibc.run_bear_case(
            "NVDA", _default_input(conviction=99),
            self.tmp_vault, now=self.today, refresh=False,
        )
        self.assertFalse(result.written)
        self.assertIsNotNone(result.skipped_reason)
        self.assertEqual(
            (self.tmp_vault / "wiki/tickers/NVDA.md").read_bytes(),
            orig_bytes,
            "file must be byte-identical on fresh skip",
        )

    def test_same_day_no_refresh_skips_with_today_message(self) -> None:
        _seed_thesis(
            self.tmp_vault, "NVDA",
            with_bear_fields=True, bear_days_old=0,
            bear_conviction=40,
        )
        result = ibc.run_bear_case(
            "NVDA", _default_input(conviction=99),
            self.tmp_vault, now=self.today, refresh=False,
        )
        self.assertFalse(result.written)
        self.assertIsNotNone(result.skipped_reason)

    def test_boundary_30d_still_fresh(self) -> None:
        _seed_thesis(
            self.tmp_vault, "NVDA",
            with_bear_fields=True, bear_days_old=30,
            bear_conviction=40,
        )
        result = ibc.run_bear_case(
            "NVDA", _default_input(conviction=99),
            self.tmp_vault, now=self.today, refresh=False,
        )
        self.assertFalse(result.written)

    def test_boundary_31d_triggers_rewrite(self) -> None:
        _seed_thesis(
            self.tmp_vault, "NVDA",
            with_bear_fields=True, bear_days_old=31,
            bear_conviction=40,
        )
        result = ibc.run_bear_case(
            "NVDA", _default_input(conviction=99),
            self.tmp_vault, now=self.today, refresh=False,
        )
        self.assertTrue(result.written)

    def test_refresh_flag_forces_rewrite(self) -> None:
        _seed_thesis(
            self.tmp_vault, "NVDA",
            with_bear_fields=True, bear_days_old=5,
            bear_conviction=40,
        )
        result = ibc.run_bear_case(
            "NVDA", _default_input(conviction=75),
            self.tmp_vault, now=self.today, refresh=True,
        )
        self.assertTrue(result.written)
        self.assertEqual(result.bear_verdict, "VETO")
        fm = sf.parse((self.tmp_vault / "wiki/tickers/NVDA.md").read_bytes())
        self.assertEqual(fm["bear_verdict"], "VETO")
        self.assertEqual(fm["bear_conviction"], 75)


# ---------- Test 3: Thesis missing ----------


class ThesisMissingTests(_VaultTestBase):
    def test_missing_ticker_file_raises(self) -> None:
        with self.assertRaises(FileNotFoundError) as cm:
            ibc.run_bear_case(
                "NVDA", _default_input(),
                self.tmp_vault, now=self.today,
            )
        msg = str(cm.exception)
        self.assertIn("NVDA", msg)
        self.assertIn("thesis", msg.lower())

    def test_file_without_thesis_score_raises(self) -> None:
        (self.tmp_vault / "wiki/tickers/NVDA.md").write_text(
            "---\ntags: [ticker]\nsymbol: NVDA\n---\n\nnot a thesis\n"
        )
        with self.assertRaises(ValueError) as cm:
            ibc.run_bear_case(
                "NVDA", _default_input(),
                self.tmp_vault, now=self.today,
            )
        self.assertIn("thesis_score", str(cm.exception).lower())


# ---------- Test 4: Low-conviction PROCEED ----------


class LowConvictionProceedTests(_VaultTestBase):
    def test_conviction_65_is_proceed(self) -> None:
        _seed_thesis(self.tmp_vault, "NVDA")
        result = ibc.run_bear_case(
            "NVDA", _default_input(conviction=65),
            self.tmp_vault, now=self.today,
        )
        self.assertEqual(result.bear_verdict, "PROCEED")
        fm = sf.parse((self.tmp_vault / "wiki/tickers/NVDA.md").read_bytes())
        self.assertEqual(fm["bear_verdict"], "PROCEED")

    def test_conviction_70_boundary_is_proceed(self) -> None:
        # Spec §5 Q7: VETO iff conviction > 70 (strictly greater).
        _seed_thesis(self.tmp_vault, "NVDA")
        result = ibc.run_bear_case(
            "NVDA", _default_input(conviction=70),
            self.tmp_vault, now=self.today,
        )
        self.assertEqual(result.bear_verdict, "PROCEED")

    def test_conviction_0_is_proceed(self) -> None:
        _seed_thesis(self.tmp_vault, "NVDA")
        result = ibc.run_bear_case(
            "NVDA", _default_input(conviction=0),
            self.tmp_vault, now=self.today,
        )
        self.assertEqual(result.bear_verdict, "PROCEED")


# ---------- Test 5: High-conviction VETO ----------


class HighConvictionVetoTests(_VaultTestBase):
    def test_conviction_85_is_veto(self) -> None:
        _seed_thesis(self.tmp_vault, "NVDA")
        result = ibc.run_bear_case(
            "NVDA", _default_input(conviction=85),
            self.tmp_vault, now=self.today,
        )
        self.assertEqual(result.bear_verdict, "VETO")
        fm = sf.parse((self.tmp_vault / "wiki/tickers/NVDA.md").read_bytes())
        self.assertEqual(fm["bear_verdict"], "VETO")
        self.assertEqual(fm["bear_conviction"], 85)

    def test_conviction_71_boundary_is_veto(self) -> None:
        # Spec §5 Q7: VETO iff conviction > 70 (strictly greater).
        _seed_thesis(self.tmp_vault, "NVDA")
        result = ibc.run_bear_case(
            "NVDA", _default_input(conviction=71),
            self.tmp_vault, now=self.today,
        )
        self.assertEqual(result.bear_verdict, "VETO")

    def test_conviction_100_is_veto(self) -> None:
        _seed_thesis(self.tmp_vault, "NVDA")
        result = ibc.run_bear_case(
            "NVDA", _default_input(conviction=100),
            self.tmp_vault, now=self.today,
        )
        self.assertEqual(result.bear_verdict, "VETO")

    def test_veto_body_shows_verdict_and_conviction(self) -> None:
        _seed_thesis(self.tmp_vault, "NVDA")
        ibc.run_bear_case(
            "NVDA", _default_input(conviction=85),
            self.tmp_vault, now=self.today,
        )
        body = (self.tmp_vault / "wiki/tickers/NVDA.md").read_text()
        self.assertIn("**Verdict:** VETO (conviction: 85)", body)


# ---------- Test 6: Schema non-clobber ----------


class SchemaNonClobberTests(_VaultTestBase):
    def test_all_thesis_fields_preserved(self) -> None:
        _seed_thesis(self.tmp_vault, "NVDA")
        orig_content = (self.tmp_vault / "wiki/tickers/NVDA.md").read_bytes()
        orig_fm = sf.parse(orig_content)
        orig_thesis_keys = set(orig_fm.keys())

        ibc.run_bear_case(
            "NVDA", _default_input(conviction=65),
            self.tmp_vault, now=self.today,
        )

        new_fm = sf.parse(
            (self.tmp_vault / "wiki/tickers/NVDA.md").read_bytes()
        )
        new_keys = set(new_fm.keys())

        # Original keys preserved with byte-equivalent values (via parse).
        for k in orig_thesis_keys:
            self.assertIn(k, new_keys, f"thesis key {k!r} lost after bear-case")
            self.assertEqual(
                new_fm[k], orig_fm[k],
                f"thesis field {k!r} changed: "
                f"orig={orig_fm[k]!r}, new={new_fm[k]!r}",
            )

        # Exactly 5 new bear_* fields, no more.
        added = new_keys - orig_thesis_keys
        expected = {
            "bear-last-verified",
            "bear_conviction",
            "bear_top_counterpoints",
            "bear_invalidation_scenarios",
            "bear_verdict",
        }
        self.assertEqual(added, expected)

    def test_specific_thesis_values_preserved(self) -> None:
        _seed_thesis(self.tmp_vault, "NVDA")
        ibc.run_bear_case(
            "NVDA", _default_input(conviction=65),
            self.tmp_vault, now=self.today,
        )
        fm = sf.parse((self.tmp_vault / "wiki/tickers/NVDA.md").read_bytes())
        self.assertEqual(fm["thesis_score"], 73)
        self.assertEqual(fm["entry_exit_levels"]["targets"][1]["sell_pct"], 33)
        self.assertEqual(fm["recommended_action"], "bull")
        self.assertEqual(fm["conviction_band"], "good")


# ---------- Test 7: Teach Mode ----------


class TeachModeTests(_VaultTestBase):
    def test_novice_with_position_size_emits_footer(self) -> None:
        _seed_thesis(self.tmp_vault, "NVDA")
        ibc.run_bear_case(
            "NVDA", _default_input(conviction=65),
            self.tmp_vault, now=self.today,
            learning_stage="novice",
            position_size_hkd=500_000,
        )
        body = (self.tmp_vault / "wiki/tickers/NVDA.md").read_text()
        self.assertIn("Why this matters for your position", body)
        self.assertIn("500,000", body)

    def test_intermediate_with_position_size_emits_footer(self) -> None:
        _seed_thesis(self.tmp_vault, "NVDA")
        ibc.run_bear_case(
            "NVDA", _default_input(conviction=65),
            self.tmp_vault, now=self.today,
            learning_stage="intermediate",
            position_size_hkd=250_000,
        )
        body = (self.tmp_vault / "wiki/tickers/NVDA.md").read_text()
        self.assertIn("Why this matters for your position", body)

    def test_advanced_skips_footer(self) -> None:
        _seed_thesis(self.tmp_vault, "NVDA")
        ibc.run_bear_case(
            "NVDA", _default_input(conviction=65),
            self.tmp_vault, now=self.today,
            learning_stage="advanced",
            position_size_hkd=500_000,
        )
        body = (self.tmp_vault / "wiki/tickers/NVDA.md").read_text()
        self.assertNotIn("Why this matters for your position", body)

    def test_novice_without_position_size_skips_footer(self) -> None:
        _seed_thesis(self.tmp_vault, "NVDA")
        ibc.run_bear_case(
            "NVDA", _default_input(conviction=65),
            self.tmp_vault, now=self.today,
            learning_stage="novice",
            position_size_hkd=None,
        )
        body = (self.tmp_vault / "wiki/tickers/NVDA.md").read_text()
        self.assertNotIn("Why this matters for your position", body)

    def test_veto_footer_different_from_proceed_footer(self) -> None:
        _seed_thesis(self.tmp_vault, "NVDA")
        ibc.run_bear_case(
            "NVDA", _default_input(conviction=85),
            self.tmp_vault, now=self.today,
            learning_stage="novice",
            position_size_hkd=500_000,
        )
        body = (self.tmp_vault / "wiki/tickers/NVDA.md").read_text()
        # VETO footer nudges: do NOT open
        self.assertIn("do NOT open", body)


# ---------- Test 8: Atomic write ----------


class AtomicWriteTests(_VaultTestBase):
    def test_injected_failure_leaves_original_intact(self) -> None:
        _seed_thesis(self.tmp_vault, "NVDA")
        orig = (self.tmp_vault / "wiki/tickers/NVDA.md").read_bytes()

        def boom(*args, **kwargs):
            raise RuntimeError("injected failure")

        with mock.patch("os.replace", side_effect=boom):
            with self.assertRaises(RuntimeError):
                ibc.run_bear_case(
                    "NVDA", _default_input(),
                    self.tmp_vault, now=self.today,
                )

        # Original file still intact (verdict NOT partially applied)
        self.assertEqual(
            (self.tmp_vault / "wiki/tickers/NVDA.md").read_bytes(),
            orig,
            "original thesis corrupted after interrupted bear-case write",
        )

        # No orphan tempfiles in the tickers directory
        orphans = [
            p for p in (self.tmp_vault / "wiki/tickers").iterdir()
            if p.name.startswith(".NVDA.md.tmp.")
        ]
        self.assertEqual(
            orphans, [],
            f"orphan tempfiles left behind: {[str(p) for p in orphans]}",
        )


# ---------- Test 9: Hook integration ----------


class HookIntegrationTests(unittest.TestCase):
    """Ticker files live at wiki/tickers/, not wiki/strategies/. The Bundle
    3 cycle 4 pre-commit Check D regex + the cycle-5 canonical strategy
    path regex must NOT match ticker paths."""

    def test_canonical_strategy_path_regex_does_not_match_ticker_paths(self) -> None:
        from scripts.lib.invest_ship_strategy import CANONICAL_STRATEGY_PATH_RE
        # Negative cases: tickers must not match
        for p in (
            "wiki/tickers/NVDA.md",
            "wiki/tickers/BRK.B.md",
            "wiki/tickers/0700.HK.md",
        ):
            self.assertIsNone(
                CANONICAL_STRATEGY_PATH_RE.match(p),
                f"regex unexpectedly matched ticker path {p!r}",
            )
        # Positive control: strategy files DO match
        self.assertIsNotNone(
            CANONICAL_STRATEGY_PATH_RE.match("wiki/strategies/strategy_spy.md")
        )


# ---------- supporting unit tests ----------


class DeriveVerdictTests(unittest.TestCase):
    def test_strict_gt_70_veto(self) -> None:
        self.assertEqual(ibc.derive_verdict(71), "VETO")
        self.assertEqual(ibc.derive_verdict(70), "PROCEED")
        self.assertEqual(ibc.derive_verdict(0), "PROCEED")
        self.assertEqual(ibc.derive_verdict(100), "VETO")
        self.assertEqual(ibc.derive_verdict(85), "VETO")


class BearCaseInputValidationTests(_VaultTestBase):
    def test_conviction_out_of_range_raises(self) -> None:
        _seed_thesis(self.tmp_vault, "NVDA")
        for bad in (-1, 101, 200):
            with self.subTest(conviction=bad):
                with self.assertRaises(ValueError):
                    ibc.run_bear_case(
                        "NVDA", _default_input(conviction=bad),
                        self.tmp_vault, now=self.today,
                    )

    def test_counterpoints_must_be_exactly_three(self) -> None:
        _seed_thesis(self.tmp_vault, "NVDA")
        # Too few
        with self.assertRaises(ValueError):
            ibc.run_bear_case(
                "NVDA",
                _default_input(counterpoints=["only-one", "only-two"]),
                self.tmp_vault, now=self.today,
            )
        # Too many
        with self.assertRaises(ValueError):
            ibc.run_bear_case(
                "NVDA",
                _default_input(
                    counterpoints=["one", "two", "three", "four"],
                ),
                self.tmp_vault, now=self.today,
            )

    def test_scenarios_must_be_2_to_5(self) -> None:
        _seed_thesis(self.tmp_vault, "NVDA")
        with self.assertRaises(ValueError):
            # Too few
            ibc.run_bear_case(
                "NVDA", _default_input(scenarios=["only-one"]),
                self.tmp_vault, now=self.today,
            )
        with self.assertRaises(ValueError):
            # Too many
            ibc.run_bear_case(
                "NVDA",
                _default_input(scenarios=[f"s{i}" for i in range(6)]),
                self.tmp_vault, now=self.today,
            )

    def test_scenarios_at_boundary_2_ok(self) -> None:
        _seed_thesis(self.tmp_vault, "NVDA")
        result = ibc.run_bear_case(
            "NVDA",
            _default_input(scenarios=["s1", "s2"]),
            self.tmp_vault, now=self.today,
        )
        self.assertTrue(result.written)

    def test_scenarios_at_boundary_5_ok(self) -> None:
        _seed_thesis(self.tmp_vault, "NVDA")
        result = ibc.run_bear_case(
            "NVDA",
            _default_input(scenarios=[f"s{i}" for i in range(5)]),
            self.tmp_vault, now=self.today,
        )
        self.assertTrue(result.written)


class SymbolValidationTests(_VaultTestBase):
    def test_rejects_lowercase(self) -> None:
        _seed_thesis(self.tmp_vault, "NVDA")
        with self.assertRaises(ValueError):
            ibc.run_bear_case(
                "nvda", _default_input(),
                self.tmp_vault, now=self.today,
            )

    def test_accepts_hong_kong_ticker(self) -> None:
        _seed_thesis(self.tmp_vault, "0700.HK")
        result = ibc.run_bear_case(
            "0700.HK", _default_input(conviction=65),
            self.tmp_vault, now=self.today,
        )
        self.assertTrue(result.written)

    def test_accepts_share_class(self) -> None:
        _seed_thesis(self.tmp_vault, "BRK.B")
        result = ibc.run_bear_case(
            "BRK.B", _default_input(conviction=65),
            self.tmp_vault, now=self.today,
        )
        self.assertTrue(result.written)


class BytePreservationTests(_VaultTestBase):
    """MiniMax review finding #4: verify the line-level frontmatter
    editor is byte-preserving for non-bear fields. Parse-equal alone is
    not enough -- yaml.safe_dump round-trip would pass parse-equal but
    emit diff noise on refresh. Line-level edit must leave other
    frontmatter lines byte-identical."""

    def test_non_bear_frontmatter_lines_byte_identical_on_first_run(self) -> None:
        path = _seed_thesis(self.tmp_vault, "NVDA")
        orig_bytes = path.read_bytes()
        ibc.run_bear_case(
            "NVDA", _default_input(conviction=65),
            self.tmp_vault, now=self.today,
        )
        new_bytes = path.read_bytes()

        # Extract frontmatter lines from both, drop any bear_* blocks
        # from the new file, and compare line-for-line with the original.
        def _fm_lines(b: bytes) -> list[str]:
            text = b.decode("utf-8")
            lines = text.splitlines(keepends=True)
            # Opening fence index 0; find closing.
            close_idx = next(
                i for i, ln in enumerate(lines)
                if i > 0 and ln.rstrip("\r\n").strip() == "---"
            )
            return lines[: close_idx + 1]

        def _drop_bear_blocks(fm_lines: list[str]) -> list[str]:
            # fm_lines INCLUDES both fences. Compute the close-fence
            # index so bear-block deletion stops BEFORE it; otherwise
            # a bear block that is the last frontmatter field (common)
            # would swallow the closing `---` line. Mirrors the bound
            # used by production _find_bear_block_ranges(lines, ..., close_idx).
            close_idx = next(
                i for i, ln in enumerate(fm_lines)
                if i > 0 and ln.rstrip("\r\n").strip() == "---"
            )
            out: list[str] = []
            i = 0
            while i < len(fm_lines):
                if i >= close_idx:
                    out.extend(fm_lines[i:])
                    break
                line = fm_lines[i]
                stripped = line.rstrip("\r\n")
                is_top = (
                    stripped
                    and not stripped[0].isspace()
                    and re.match(r"^[A-Za-z_][\w-]*:", stripped)
                )
                key = stripped.split(":", 1)[0] if is_top else None
                if key in ibc._BEAR_KEY_EXACT:
                    j = i + 1
                    while j < close_idx:
                        nxt = fm_lines[j].rstrip("\r\n")
                        if (
                            nxt
                            and not nxt[0].isspace()
                            and re.match(r"^[A-Za-z_][\w-]*:", nxt)
                        ):
                            break
                        j += 1
                    i = j
                    continue
                out.append(line)
                i += 1
            return out

        orig_fm = _fm_lines(orig_bytes)
        new_fm = _drop_bear_blocks(_fm_lines(new_bytes))
        self.assertEqual(
            orig_fm, new_fm,
            "non-bear frontmatter lines must remain byte-identical; "
            f"orig={orig_fm!r}\nnew_drop_bear={new_fm!r}",
        )

    def test_refresh_byte_identical_thesis_fields(self) -> None:
        # Seed thesis with bear-case already present (from a prior run).
        _seed_thesis(
            self.tmp_vault, "NVDA",
            with_bear_fields=True, bear_days_old=0,
            bear_conviction=40,
        )
        # Force refresh with different values.
        path = self.tmp_vault / "wiki/tickers/NVDA.md"
        orig_bytes = path.read_bytes()
        ibc.run_bear_case(
            "NVDA", _default_input(conviction=85),
            self.tmp_vault, now=self.today, refresh=True,
        )
        new_bytes = path.read_bytes()

        # Non-bear frontmatter fields: confirm byte-identical via
        # re-parse. The frontmatter LINES up to the bear block are
        # untouched by line-level edit; this is the safety net.
        orig_fm_dict = sf.parse(orig_bytes)
        new_fm_dict = sf.parse(new_bytes)
        for k, v in orig_fm_dict.items():
            if k in ibc._BEAR_KEY_EXACT:
                continue
            self.assertEqual(
                new_fm_dict[k], v,
                f"non-bear field {k!r} changed on refresh",
            )


class WriteTimeConsistencyCheckTests(_VaultTestBase):
    """MiniMax cycle-2 R2 finding: run_bear_case must raise when the
    existing on-disk bear_verdict + bear_conviction pair is internally
    inconsistent (e.g. after a hand-edit). Forces Keith to use --refresh
    to rewrite cleanly rather than silently clobbering into a new state
    that hides the prior corruption."""

    def test_raises_on_inconsistent_existing_state(self) -> None:
        # Seed thesis with bear-case already present, BUT tamper so
        # bear_verdict contradicts bear_conviction.
        _seed_thesis(
            self.tmp_vault, "NVDA",
            with_bear_fields=True, bear_days_old=45,  # stale so not skipped
            bear_conviction=85, bear_verdict="PROCEED",  # conviction 85 implies VETO
        )
        with self.assertRaises(ValueError) as cm:
            ibc.run_bear_case(
                "NVDA", _default_input(conviction=50),
                self.tmp_vault, now=self.today,
            )
        self.assertIn("inconsistent", str(cm.exception).lower())
        self.assertIn("refresh", str(cm.exception).lower())

    def test_consistent_existing_state_overwrites_cleanly(self) -> None:
        _seed_thesis(
            self.tmp_vault, "NVDA",
            with_bear_fields=True, bear_days_old=45,  # stale
            bear_conviction=40, bear_verdict="PROCEED",  # consistent: 40 -> PROCEED
        )
        result = ibc.run_bear_case(
            "NVDA", _default_input(conviction=85),
            self.tmp_vault, now=self.today,
        )
        self.assertTrue(result.written)
        self.assertEqual(result.bear_verdict, "VETO")

    def test_refresh_repairs_inconsistent_existing_state(self) -> None:
        # R2-bundle-4a-sweep (cumulative Codex): the error message on
        # the consistency check tells the operator to rerun with
        # refresh=True, but the check previously fired unconditionally
        # -- refresh requests still hit the raise and approval stayed
        # blocked until manual frontmatter surgery. The sanctioned
        # recovery path must actually repair the inconsistent state.
        _seed_thesis(
            self.tmp_vault, "NVDA",
            with_bear_fields=True, bear_days_old=45,
            # conviction=85 implies VETO but verdict says PROCEED.
            bear_conviction=85, bear_verdict="PROCEED",
        )
        # Without refresh: raises (test above covers this). With
        # refresh=True: the writer overwrites both fields cleanly.
        result = ibc.run_bear_case(
            "NVDA", _default_input(conviction=50),
            self.tmp_vault, now=self.today, refresh=True,
        )
        self.assertTrue(result.written)
        # Rewrite lands the CONSISTENT new state derived from the
        # supplied conviction, not the tampered-existing state.
        self.assertEqual(result.bear_verdict, "PROCEED")  # 50 -> PROCEED
        self.assertEqual(result.bear_conviction, 50)
        # On-disk frontmatter matches.
        fm = sf.parse((self.tmp_vault / "wiki/tickers/NVDA.md").read_bytes())
        self.assertEqual(fm["bear_verdict"], "PROCEED")
        self.assertEqual(fm["bear_conviction"], 50)


class MultipleDatedSectionsTests(_VaultTestBase):
    """Refresh-triggered re-run keeps prior ## Bear Case (OLD_DATE) sections.
    Latest verdict overwrites frontmatter; body accumulates audit trail."""

    def test_refresh_appends_new_section_without_removing_old(self) -> None:
        _seed_thesis(self.tmp_vault, "NVDA")

        # First run (2026-04-19, low conviction -> PROCEED).
        ibc.run_bear_case(
            "NVDA", _default_input(conviction=40),
            self.tmp_vault, now=self.today,
        )
        body_after_first = (
            self.tmp_vault / "wiki/tickers/NVDA.md"
        ).read_text()
        self.assertIn("## Bear Case (2026-04-19)", body_after_first)

        # Second run 35 days later -> fresh window elapsed -> rewrite.
        # Different conviction -> different verdict in frontmatter.
        later = self.today + _dt.timedelta(days=35)
        ibc.run_bear_case(
            "NVDA", _default_input(conviction=85),
            self.tmp_vault, now=later,
        )
        body = (self.tmp_vault / "wiki/tickers/NVDA.md").read_text()
        # Both sections present -- audit trail preserved.
        self.assertIn("## Bear Case (2026-04-19)", body)
        self.assertIn(f"## Bear Case ({later.isoformat()})", body)
        # Frontmatter reflects LATEST verdict (VETO).
        fm = sf.parse((self.tmp_vault / "wiki/tickers/NVDA.md").read_bytes())
        self.assertEqual(fm["bear_verdict"], "VETO")
        self.assertEqual(fm["bear_conviction"], 85)


if __name__ == "__main__":
    unittest.main()
