"""Tests for invest-narrative Ship 2 pipeline and --promote writer."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from scripts.lib.invest_narrative_pipeline import (
    _SYSTEM_PROMPT,
    _default_call1,
    _default_call2,
    _derive_slug,
    _extract_json,
    _find_candidate_in_theme,
    _update_macro_themes_index,
    _update_watchlist_index,
    main,
    promote_to_watchlist,
    run_pipeline,
)


class DeriveSlugTests(unittest.TestCase):
    def test_derive_slug_basic(self):
        with tempfile.TemporaryDirectory() as td:
            slug = _derive_slug("AI compute demand drives semiconductor capex", Path(td))
            self.assertEqual(slug, "ai-compute-demand-drives-semiconductor-capex")

    def test_derive_slug_strips_punctuation(self):
        with tempfile.TemporaryDirectory() as td:
            slug = _derive_slug("War drives oil to electric! transition?", Path(td))
            self.assertEqual(slug, "war-drives-oil-to-electric-transition")

    def test_derive_slug_collision(self):
        with tempfile.TemporaryDirectory() as td:
            td_path = Path(td)
            (td_path / "wiki" / "macro-themes").mkdir(parents=True)
            (td_path / "wiki" / "macro-themes" / "theme_ai-compute-demand.md").write_text("x")
            slug = _derive_slug("AI compute demand", td_path)
            self.assertEqual(slug, "ai-compute-demand_2")

    def test_derive_slug_empty_fallback(self):
        with tempfile.TemporaryDirectory() as td:
            slug = _derive_slug("!!!", Path(td))
            self.assertEqual(slug, "theme")


class ExtractJsonTests(unittest.TestCase):
    def test_extract_json_plain(self):
        data = _extract_json('{"key": "value"}')
        self.assertEqual(data, {"key": "value"})

    def test_extract_json_with_fences(self):
        data = _extract_json('```json\n{"key": "value"}\n```')
        self.assertEqual(data, {"key": "value"})


class FindCandidateInThemeTests(unittest.TestCase):
    def test_find_candidate_found(self):
        content = """
| Symbol | Order | Reasoning chain | Citation | ARK score (sum/60) |
|---|---|---|---|---|
| NVDA | 1st | AI -> GPUs -> NVDA | [source](https://example.com) | 48/60 |
| AMD | 1st | AI -> challengers -> AMD | [link](https://amd.com) | 42/60 |
"""
        row = _find_candidate_in_theme(content, "AMD")
        self.assertIsNotNone(row)
        self.assertEqual(row["symbol"], "AMD")
        self.assertEqual(row["order"], "1st")
        self.assertEqual(row["citation_url"], "https://amd.com")

    def test_find_candidate_not_found(self):
        content = "| Symbol | Order | Reasoning | Citation |\n|---|---|---|---|\n| AAPL | 1st | ... | ... |"
        row = _find_candidate_in_theme(content, "TSLA")
        self.assertIsNone(row)

    def test_find_candidate_case_insensitive(self):
        content = "| Symbol | Order | Reasoning | Citation | ARK score (sum/60) |\n|---|---|---|---|---|\n| NVDA | 1st | ... | ... | 48/60 |"
        row = _find_candidate_in_theme(content, "nvda")
        self.assertIsNotNone(row)


class RunPipelineTests(unittest.TestCase):
    def _mock_call1(self, narrative: str) -> list[dict]:
        return [
            {"name": "Sub-theme A", "reasoning": "Reason A"},
            {"name": "Sub-theme B", "reasoning": "Reason B"},
            {"name": "Sub-theme C", "reasoning": "Reason C"},
            {"name": "Sub-theme D", "reasoning": "Reason D"},
        ]

    def _mock_call2(self, narrative: str, sub_theme: dict) -> list[dict]:
        if sub_theme["name"] == "Sub-theme A":
            return [
                {"symbol": "VALID", "reasoning_chain": "step1 -> step2", "citation_url": "https://example.com/valid", "order": "2nd", "ark_scores": {}, "sub_theme": sub_theme["name"]},
                {"symbol": "EXTRA1", "reasoning_chain": "ok", "citation_url": "https://example.com/ex1", "order": "3rd", "ark_scores": {}, "sub_theme": sub_theme["name"]},
            ]
        return [
            {"symbol": "FAKE", "reasoning_chain": "step1 -> step2", "citation_url": "https://example.com/fake", "order": "1st", "ark_scores": {}, "sub_theme": sub_theme["name"]},
        ]

    @patch("scripts.lib.invest_narrative_pipeline.load_registry")
    def test_run_pipeline_fails_fast_on_empty_registry(self, mock_registry):
        mock_registry.return_value = {}
        with tempfile.TemporaryDirectory() as td:
            td_path = Path(td)
            (td_path / "wiki" / "macro-themes").mkdir(parents=True)
            with self.assertRaises(ValueError) as ctx:
                run_pipeline(
                    "Test narrative",
                    vault_root=td_path,
                    call1_fn=self._mock_call1,
                    call2_fn=self._mock_call2,
                )
            self.assertIn("Canonical ticker registry is empty", str(ctx.exception))

    @patch("scripts.lib.invest_narrative_pipeline.load_registry")
    @patch("scripts.lib.invest_narrative_pipeline.validate_ticker_exists")
    @patch("scripts.lib.invest_narrative_pipeline.validate_market_cap")
    @patch("scripts.lib.invest_narrative_pipeline.validate_liquidity")
    @patch("scripts.lib.invest_narrative_pipeline.validate_citation_url")
    @patch("scripts.lib.invest_narrative_pipeline.validate_priced_in")
    def test_run_pipeline_writes_theme_file(
        self, mock_priced_in, mock_citation, mock_liq, mock_cap, mock_exists, mock_registry
    ):
        mock_registry.return_value = {"VALID": {}, "FAKE": {}}
        mock_exists.return_value = True
        mock_cap.return_value = True
        mock_liq.return_value = True
        mock_citation.return_value = True
        mock_priced_in.return_value = {"flagged": False, "skipped": False}

        with tempfile.TemporaryDirectory() as td:
            td_path = Path(td)
            (td_path / "wiki" / "macro-themes").mkdir(parents=True)
            path = run_pipeline(
                "Test narrative for pipeline",
                vault_root=td_path,
                call1_fn=self._mock_call1,
                call2_fn=self._mock_call2,
            )
            self.assertTrue(path.exists())
            content = path.read_text()
            self.assertIn("## Sub-themes (Call 1)", content)
            self.assertIn("## Candidate tickers (Call 2)", content)
            self.assertIn("Sub-theme A", content)
            self.assertIn("VALID", content)

    @patch("scripts.lib.invest_narrative_pipeline.load_registry")
    @patch("scripts.lib.invest_narrative_pipeline.validate_ticker_exists")
    @patch("scripts.lib.invest_narrative_pipeline.validate_market_cap")
    @patch("scripts.lib.invest_narrative_pipeline.validate_liquidity")
    @patch("scripts.lib.invest_narrative_pipeline.validate_citation_url")
    @patch("scripts.lib.invest_narrative_pipeline.validate_priced_in")
    def test_run_pipeline_drops_hallucinated_symbol(
        self, mock_priced_in, mock_citation, mock_liq, mock_cap, mock_exists, mock_registry
    ):
        mock_registry.return_value = {"VALID": {}}
        mock_exists.side_effect = lambda s, r: s == "VALID"
        mock_cap.return_value = True
        mock_liq.return_value = True
        mock_citation.return_value = True
        mock_priced_in.return_value = {"flagged": False, "skipped": False}

        with tempfile.TemporaryDirectory() as td:
            td_path = Path(td)
            (td_path / "wiki" / "macro-themes").mkdir(parents=True)
            path = run_pipeline(
                "Test narrative",
                vault_root=td_path,
                call1_fn=self._mock_call1,
                call2_fn=self._mock_call2,
            )
            content = path.read_text()
            self.assertIn("Rejected (hallucinated symbol): 4", content)
            self.assertIn("Final candidates shown above: 1", content)

    @patch("scripts.lib.invest_narrative_pipeline.load_registry")
    @patch("scripts.lib.invest_narrative_pipeline.validate_ticker_exists")
    @patch("scripts.lib.invest_narrative_pipeline.validate_market_cap")
    @patch("scripts.lib.invest_narrative_pipeline.validate_liquidity")
    @patch("scripts.lib.invest_narrative_pipeline.validate_citation_url")
    @patch("scripts.lib.invest_narrative_pipeline.validate_priced_in")
    def test_run_pipeline_flags_priced_in(
        self, mock_priced_in, mock_citation, mock_liq, mock_cap, mock_exists, mock_registry
    ):
        mock_registry.return_value = {"VALID": {}}
        mock_exists.return_value = True
        mock_cap.return_value = True
        mock_liq.return_value = True
        mock_citation.return_value = True
        mock_priced_in.return_value = {"flagged": True, "skipped": False}

        with tempfile.TemporaryDirectory() as td:
            td_path = Path(td)
            (td_path / "wiki" / "macro-themes").mkdir(parents=True)
            path = run_pipeline(
                "Test narrative",
                vault_root=td_path,
                call1_fn=self._mock_call1,
                call2_fn=self._mock_call2,
            )
            content = path.read_text()
            self.assertIn("Flagged (>90% gain", content)
            self.assertIn("VALID", content)

    @patch("scripts.lib.invest_narrative_pipeline.load_registry")
    @patch("scripts.lib.invest_narrative_pipeline.validate_ticker_exists")
    @patch("scripts.lib.invest_narrative_pipeline.validate_market_cap")
    @patch("scripts.lib.invest_narrative_pipeline.validate_liquidity")
    @patch("scripts.lib.invest_narrative_pipeline.validate_citation_url")
    @patch("scripts.lib.invest_narrative_pipeline.validate_priced_in")
    def test_run_pipeline_drops_dead_citation(
        self, mock_priced_in, mock_citation, mock_liq, mock_cap, mock_exists, mock_registry
    ):
        mock_registry.return_value = {"VALID": {}, "FAKE": {}}
        mock_exists.return_value = True
        mock_cap.return_value = True
        mock_liq.return_value = True
        mock_citation.side_effect = lambda url: "valid" in url
        mock_priced_in.return_value = {"flagged": False, "skipped": False}

        with tempfile.TemporaryDirectory() as td:
            td_path = Path(td)
            (td_path / "wiki" / "macro-themes").mkdir(parents=True)
            path = run_pipeline(
                "Test narrative",
                vault_root=td_path,
                call1_fn=self._mock_call1,
                call2_fn=self._mock_call2,
            )
            content = path.read_text()
            self.assertIn("Rejected (no working citation): 4", content)

    @patch("scripts.lib.invest_narrative_pipeline.load_registry")
    @patch("scripts.lib.invest_narrative_pipeline.validate_ticker_exists")
    @patch("scripts.lib.invest_narrative_pipeline.validate_market_cap")
    @patch("scripts.lib.invest_narrative_pipeline.validate_liquidity")
    @patch("scripts.lib.invest_narrative_pipeline.validate_citation_url")
    @patch("scripts.lib.invest_narrative_pipeline.validate_priced_in")
    def test_run_pipeline_records_yfinance_skip(
        self, mock_priced_in, mock_citation, mock_liq, mock_cap, mock_exists, mock_registry
    ):
        from scripts.lib.invest_narrative_validators import ValidatorSkipped

        mock_registry.return_value = {"VALID": {}}
        mock_exists.return_value = True
        mock_cap.side_effect = ValidatorSkipped("yfinance down")
        mock_liq.return_value = True
        mock_citation.return_value = True
        mock_priced_in.return_value = {"flagged": False, "skipped": False}

        with tempfile.TemporaryDirectory() as td:
            td_path = Path(td)
            (td_path / "wiki" / "macro-themes").mkdir(parents=True)
            path = run_pipeline(
                "Test narrative",
                vault_root=td_path,
                call1_fn=self._mock_call1,
                call2_fn=self._mock_call2,
            )
            content = path.read_text()
            self.assertIn("Validator skipped (yfinance unavailable):", content)
            self.assertIn("market_cap", content)

    def _mock_call1_single(self, narrative: str) -> list[dict]:
        return [
            {"name": "Sub-theme A", "reasoning": "Reason A"},
            {"name": "Sub-theme B", "reasoning": "Reason B"},
            {"name": "Sub-theme C", "reasoning": "Reason C"},
            {"name": "Sub-theme D", "reasoning": "Reason D"},
        ]

    def _mock_call2_malformed(self, narrative: str, sub_theme: dict) -> list[dict]:
        return [
            {"symbol": "VALID", "reasoning_chain": "ok", "citation_url": "https://example.com/valid", "order": "2nd", "ark_scores": {}, "sub_theme": sub_theme["name"]},
            {"symbol": "NO_ORDER", "reasoning_chain": "ok", "citation_url": "https://example.com/noorder", "ark_scores": {}, "sub_theme": sub_theme["name"]},
            {"symbol": "NO_CHAIN", "citation_url": "https://example.com/nochain", "order": "1st", "ark_scores": {}, "sub_theme": sub_theme["name"]},
            {"symbol": "NO_CITE", "reasoning_chain": "ok", "order": "1st", "ark_scores": {}, "sub_theme": sub_theme["name"]},
        ]

    @patch("scripts.lib.invest_narrative_pipeline.load_registry")
    @patch("scripts.lib.invest_narrative_pipeline.validate_ticker_exists")
    @patch("scripts.lib.invest_narrative_pipeline.validate_market_cap")
    @patch("scripts.lib.invest_narrative_pipeline.validate_liquidity")
    @patch("scripts.lib.invest_narrative_pipeline.validate_citation_url")
    @patch("scripts.lib.invest_narrative_pipeline.validate_priced_in")
    def test_run_pipeline_skips_malformed_candidates(
        self, mock_priced_in, mock_citation, mock_liq, mock_cap, mock_exists, mock_registry
    ):
        mock_registry.return_value = {"VALID": {}, "NO_ORDER": {}, "NO_CHAIN": {}, "NO_CITE": {}}
        mock_exists.return_value = True
        mock_cap.return_value = True
        mock_liq.return_value = True
        mock_citation.return_value = True
        mock_priced_in.return_value = {"flagged": False, "skipped": False}

        with tempfile.TemporaryDirectory() as td:
            td_path = Path(td)
            (td_path / "wiki" / "macro-themes").mkdir(parents=True)
            path = run_pipeline(
                "Test narrative",
                vault_root=td_path,
                call1_fn=self._mock_call1_single,
                call2_fn=self._mock_call2_malformed,
            )
            content = path.read_text()
            self.assertIn("Rejected (malformed LLM output): 12", content)
            self.assertIn("VALID", content)
            self.assertNotIn("NO_ORDER", content)
            self.assertNotIn("NO_CHAIN", content)
            self.assertNotIn("NO_CITE", content)

    def _mock_call2_non_dict_and_null(self, narrative: str, sub_theme: dict) -> list:
        return [
            {"symbol": "VALID", "reasoning_chain": "ok", "citation_url": "https://example.com/valid", "order": "2nd", "ark_scores": {}, "sub_theme": sub_theme["name"]},
            None,
            {"symbol": 123, "reasoning_chain": "ok", "citation_url": "https://example.com/num", "order": "1st", "ark_scores": {}, "sub_theme": sub_theme["name"]},
            {"symbol": "", "reasoning_chain": "ok", "citation_url": "https://example.com/empty", "order": "1st", "ark_scores": {}, "sub_theme": sub_theme["name"]},
        ]

    @patch("scripts.lib.invest_narrative_pipeline.load_registry")
    @patch("scripts.lib.invest_narrative_pipeline.validate_ticker_exists")
    @patch("scripts.lib.invest_narrative_pipeline.validate_market_cap")
    @patch("scripts.lib.invest_narrative_pipeline.validate_liquidity")
    @patch("scripts.lib.invest_narrative_pipeline.validate_citation_url")
    @patch("scripts.lib.invest_narrative_pipeline.validate_priced_in")
    def test_run_pipeline_skips_non_dict_and_null_symbols(
        self, mock_priced_in, mock_citation, mock_liq, mock_cap, mock_exists, mock_registry
    ):
        mock_registry.return_value = {"VALID": {}}
        mock_exists.return_value = True
        mock_cap.return_value = True
        mock_liq.return_value = True
        mock_citation.return_value = True
        mock_priced_in.return_value = {"flagged": False, "skipped": False}

        with tempfile.TemporaryDirectory() as td:
            td_path = Path(td)
            (td_path / "wiki" / "macro-themes").mkdir(parents=True)
            path = run_pipeline(
                "Test narrative",
                vault_root=td_path,
                call1_fn=self._mock_call1_single,
                call2_fn=self._mock_call2_non_dict_and_null,
            )
            content = path.read_text()
            self.assertIn("Rejected (malformed LLM output): 12", content)
            self.assertIn("VALID", content)

    def _mock_call1_with_bad_subtheme(self, narrative: str) -> list:
        return [
            {"name": "Good", "reasoning": "ok"},
            None,
            {"name": "Also Good", "reasoning": "ok"},
            {"reasoning": "missing name"},
        ]

    def _mock_call2_good(self, narrative: str, sub_theme: dict) -> list[dict]:
        return [
            {"symbol": "VALID", "reasoning_chain": "ok", "citation_url": "https://example.com/valid", "order": "1st", "sub_theme": sub_theme["name"]},
        ]

    @patch("scripts.lib.invest_narrative_pipeline.load_registry")
    @patch("scripts.lib.invest_narrative_pipeline.validate_ticker_exists")
    @patch("scripts.lib.invest_narrative_pipeline.validate_market_cap")
    @patch("scripts.lib.invest_narrative_pipeline.validate_liquidity")
    @patch("scripts.lib.invest_narrative_pipeline.validate_citation_url")
    @patch("scripts.lib.invest_narrative_pipeline.validate_priced_in")
    def test_run_pipeline_skips_malformed_sub_themes(
        self, mock_priced_in, mock_citation, mock_liq, mock_cap, mock_exists, mock_registry
    ):
        mock_registry.return_value = {"VALID": {}}
        mock_exists.return_value = True
        mock_cap.return_value = True
        mock_liq.return_value = True
        mock_citation.return_value = True
        mock_priced_in.return_value = {"flagged": False, "skipped": False}

        with tempfile.TemporaryDirectory() as td:
            td_path = Path(td)
            (td_path / "wiki" / "macro-themes").mkdir(parents=True)
            with self.assertRaises(ValueError) as ctx:
                run_pipeline(
                    "Test narrative",
                    vault_root=td_path,
                    call1_fn=self._mock_call1_with_bad_subtheme,
                    call2_fn=self._mock_call2_good,
                )
            self.assertIn("minimum is 4", str(ctx.exception))

    @patch("scripts.lib.invest_narrative_pipeline.load_registry")
    @patch("scripts.lib.invest_narrative_pipeline.validate_ticker_exists")
    @patch("scripts.lib.invest_narrative_pipeline.validate_market_cap")
    @patch("scripts.lib.invest_narrative_pipeline.validate_liquidity")
    @patch("scripts.lib.invest_narrative_pipeline.validate_citation_url")
    @patch("scripts.lib.invest_narrative_pipeline.validate_priced_in")
    def test_run_pipeline_updates_macro_themes_index(
        self, mock_priced_in, mock_citation, mock_liq, mock_cap, mock_exists, mock_registry
    ):
        mock_registry.return_value = {"VALID": {}}
        mock_exists.return_value = True
        mock_cap.return_value = True
        mock_liq.return_value = True
        mock_citation.return_value = True
        mock_priced_in.return_value = {"flagged": False, "skipped": False}

        with tempfile.TemporaryDirectory() as td:
            td_path = Path(td)
            (td_path / "wiki" / "macro-themes").mkdir(parents=True)
            run_pipeline(
                "Test narrative",
                vault_root=td_path,
                call1_fn=self._mock_call1,
                call2_fn=self._mock_call2,
            )
            index_path = td_path / "wiki" / "macro-themes" / "index.md"
            self.assertTrue(index_path.exists())
            self.assertIn("Test Narrative", index_path.read_text())


class PromoteToWatchlistTests(unittest.TestCase):
    def _make_theme_file(self, path: Path, with_ark_scores: bool = True) -> None:
        ark_block = ""
        if with_ark_scores:
            ark_block = """
candidate_ark_scores:
  LRCX:
    people_culture: 8
    rd_execution: 9
    moat: 9
    product_leadership: 8
    thesis_risk: 7
    valuation: 6"""
        content = f"""---
tags: [macro-theme, narrative, candidates, k2bi]
date: 2026-04-26
type: macro-theme
origin: k2bi-extract
narrative: "AI compute demand drives semiconductor capex cycle"
sub-themes: [Wafer Fab Equipment]
candidate-count: 1
attention-score: <stub for Ship 3>
priced-in-warnings: []
status: candidates-pending-review
up: "[[index]]"{ark_block}
---

# Macro Theme: AI Compute Demand Drives Semiconductor Capex Cycle

## Candidate tickers (Call 2)

### Wafer Fab Equipment

| Symbol | Order | Reasoning chain | Citation | ARK score (sum/60) |
|---|---|---|---|---|
| LRCX | 2nd | Leading-edge chips -> Lam Research -> LRCX | [Motley Fool](https://www.fool.com/earnings/call-transcripts/2026/04/22/lam-research-lrcx-q3-2026-earnings-transcript/) | 45/60 |

## Validator results

- Total candidates from LLM: 1
- Final candidates shown above: 1
"""
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)

    def test_promote_writes_watchlist_entry(self):
        with tempfile.TemporaryDirectory() as td:
            td_path = Path(td)
            theme_path = td_path / "wiki" / "macro-themes" / "theme_ai-compute-demand.md"
            self._make_theme_file(theme_path)
            result = promote_to_watchlist("LRCX", theme_path, vault_root=td_path)
            self.assertTrue(result.exists())
            content = result.read_text()
            self.assertIn("symbol: LRCX", content)
            self.assertIn("status: promoted", content)
            self.assertIn("order_of_beneficiary: 2", content)
            self.assertIn("schema_version: 1", content)
            self.assertIn("narrative_provenance:", content)
            self.assertIn("ark_6_metric_initial_scores:", content)

    def test_promote_idempotent(self):
        with tempfile.TemporaryDirectory() as td:
            td_path = Path(td)
            theme_path = td_path / "wiki" / "macro-themes" / "theme_ai-compute-demand.md"
            self._make_theme_file(theme_path)
            path1 = promote_to_watchlist("LRCX", theme_path, vault_root=td_path)
            path2 = promote_to_watchlist("LRCX", theme_path, vault_root=td_path)
            self.assertEqual(path1, path2)

    def test_promote_symbol_not_found_raises(self):
        with tempfile.TemporaryDirectory() as td:
            td_path = Path(td)
            theme_path = td_path / "wiki" / "macro-themes" / "theme_test.md"
            self._make_theme_file(theme_path)
            with self.assertRaises(ValueError):
                promote_to_watchlist("TSLA", theme_path, vault_root=td_path)

    def test_promote_updates_watchlist_index(self):
        with tempfile.TemporaryDirectory() as td:
            td_path = Path(td)
            theme_path = td_path / "wiki" / "macro-themes" / "theme_ai-compute-demand.md"
            self._make_theme_file(theme_path)
            promote_to_watchlist("LRCX", theme_path, vault_root=td_path)
            index_path = td_path / "wiki" / "watchlist" / "index.md"
            self.assertTrue(index_path.exists())
            self.assertIn("LRCX", index_path.read_text())

    def test_promote_without_ark_scores_writes_empty_block(self):
        with tempfile.TemporaryDirectory() as td:
            td_path = Path(td)
            theme_path = td_path / "wiki" / "macro-themes" / "theme_ai-compute-demand.md"
            self._make_theme_file(theme_path, with_ark_scores=False)
            result = promote_to_watchlist("LRCX", theme_path, vault_root=td_path)
            content = result.read_text()
            self.assertIn("ark_6_metric_initial_scores:", content)

    def test_promote_refuses_overwrite_existing_non_promoted(self):
        with tempfile.TemporaryDirectory() as td:
            td_path = Path(td)
            theme_path = td_path / "wiki" / "macro-themes" / "theme_ai-compute-demand.md"
            self._make_theme_file(theme_path)
            watchlist_path = td_path / "wiki" / "watchlist" / "LRCX.md"
            watchlist_path.parent.mkdir(parents=True, exist_ok=True)
            watchlist_path.write_text("---\ntype: watchlist\nstatus: screening\n---\n# LRCX\n")
            with self.assertRaises(ValueError) as ctx:
                promote_to_watchlist("LRCX", theme_path, vault_root=td_path)
            self.assertIn("Refusing to overwrite", str(ctx.exception))

    def test_promote_conflict_raises_on_different_reasoning(self):
        """m2.22 F2: an existing promoted file with different Ship-2 fields
        is a conflict, NOT idempotent state."""
        with tempfile.TemporaryDirectory() as td:
            td_path = Path(td)
            theme_path = td_path / "wiki" / "macro-themes" / "theme_ai-compute-demand.md"
            self._make_theme_file(theme_path)
            # Pre-stage a watchlist file as if a different theme had promoted LRCX
            # with completely different reasoning.
            watchlist_path = td_path / "wiki" / "watchlist" / "LRCX.md"
            watchlist_path.parent.mkdir(parents=True, exist_ok=True)
            watchlist_path.write_text(
                "---\n"
                "tags: [watchlist, k2bi]\n"
                "date: 2026-04-25\n"
                "type: watchlist\n"
                "origin: k2bi-extract\n"
                "up: \"[[index]]\"\n"
                "symbol: LRCX\n"
                "status: promoted\n"
                "schema_version: 1\n"
                "narrative_provenance: \"[[macro-themes/theme_other]]\"\n"
                "reasoning_chain: \"completely different reasoning from a different theme\"\n"
                "citation_url: \"https://other.example.com\"\n"
                "order_of_beneficiary: 1\n"
                "ark_6_metric_initial_scores: {}\n"
                "---\n"
                "# Watchlist: LRCX\n"
            )
            with self.assertRaises(ValueError) as ctx:
                promote_to_watchlist("LRCX", theme_path, vault_root=td_path)
            msg = str(ctx.exception)
            self.assertIn("Conflict", msg)
            self.assertIn("narrative_provenance", msg)
            self.assertIn("reasoning_chain", msg)

    def test_promote_idempotent_only_when_ship2_fields_match(self):
        """m2.22 F2: an existing promoted file with byte-for-byte matching
        Ship-2 fields IS idempotent."""
        with tempfile.TemporaryDirectory() as td:
            td_path = Path(td)
            theme_path = td_path / "wiki" / "macro-themes" / "theme_ai-compute-demand.md"
            self._make_theme_file(theme_path)
            # First promote creates the canonical file.
            promote_to_watchlist("LRCX", theme_path, vault_root=td_path)
            # Second promote against the SAME theme must succeed (idempotent).
            result = promote_to_watchlist("LRCX", theme_path, vault_root=td_path)
            self.assertTrue(result.exists())

    def test_promote_rolls_back_watchlist_on_index_failure(self):
        """m2.22 F3: if the watchlist-index update fails after the
        watchlist file is created, the file must be unlinked so the
        operator does not see a half-committed state."""
        with tempfile.TemporaryDirectory() as td:
            td_path = Path(td)
            theme_path = td_path / "wiki" / "macro-themes" / "theme_ai-compute-demand.md"
            self._make_theme_file(theme_path)
            watchlist_path = td_path / "wiki" / "watchlist" / "LRCX.md"

            with patch(
                "scripts.lib.invest_narrative_pipeline._update_watchlist_index",
                side_effect=RuntimeError("simulated index failure"),
            ):
                with self.assertRaises(RuntimeError):
                    promote_to_watchlist("LRCX", theme_path, vault_root=td_path)

            self.assertFalse(
                watchlist_path.exists(),
                "Watchlist file should have been rolled back on index failure",
            )

    def test_promote_rolls_back_index_and_watchlist_on_theme_failure(self):
        """m2.22 F3 + N2: if the theme-log append fails, the watchlist
        file is unlinked and the LRCX row is removed from the index by
        compensating removal under index lock (not by snapshot
        restore -- snapshot restore would clobber concurrent writers
        per the m2.22 re-review N2 finding)."""
        with tempfile.TemporaryDirectory() as td:
            td_path = Path(td)
            theme_path = td_path / "wiki" / "macro-themes" / "theme_ai-compute-demand.md"
            self._make_theme_file(theme_path)
            watchlist_path = td_path / "wiki" / "watchlist" / "LRCX.md"
            index_path = td_path / "wiki" / "watchlist" / "index.md"

            with patch(
                "scripts.lib.invest_narrative_pipeline._append_promotion_to_theme",
                side_effect=RuntimeError("simulated theme-log failure"),
            ):
                with self.assertRaises(RuntimeError):
                    promote_to_watchlist("LRCX", theme_path, vault_root=td_path)

            self.assertFalse(
                watchlist_path.exists(),
                "Watchlist file should have been rolled back",
            )
            # Compensating-removal rollback leaves the index file in
            # place but with no LRCX row.
            self.assertTrue(index_path.exists())
            content = index_path.read_text()
            self.assertNotIn("| [[LRCX]]", content)

    def test_promote_rollback_partial_failure_self_heals_on_retry(self):
        """m2.22 N3: when both the promote AND its index-row-removal
        rollback fail, the next promote attempt converges to clean
        state via update_watchlist_index's idempotent guard rather
        than stranding split-brain."""
        with tempfile.TemporaryDirectory() as td:
            td_path = Path(td)
            theme_path = td_path / "wiki" / "macro-themes" / "theme_ai-compute-demand.md"
            self._make_theme_file(theme_path)
            watchlist_path = td_path / "wiki" / "watchlist" / "LRCX.md"
            index_path = td_path / "wiki" / "watchlist" / "index.md"

            # Force theme-append to fail AND force the rollback's
            # remove_watchlist_index_row to ALSO fail. The new
            # ordering unlinks the file FIRST, so after the failed
            # promote we expect: no watchlist file, phantom index row.
            with patch(
                "scripts.lib.invest_narrative_pipeline._append_promotion_to_theme",
                side_effect=RuntimeError("simulated theme failure"),
            ), patch(
                "scripts.lib.invest_narrative_pipeline.remove_watchlist_index_row",
                side_effect=RuntimeError("simulated rollback failure"),
            ):
                with self.assertRaises(RuntimeError) as ctx:
                    promote_to_watchlist("LRCX", theme_path, vault_root=td_path)
                self.assertIn("self-heal", str(ctx.exception))

            self.assertFalse(watchlist_path.exists(), "Unlink succeeded; file should be gone")
            self.assertTrue(index_path.exists())
            self.assertIn(
                "| [[LRCX]]",
                index_path.read_text(),
                "Phantom row remains after rollback removal failed",
            )

            # Retry: the idempotent guard inside update_watchlist_index
            # sees the existing row and no-ops on the index step,
            # while the new watchlist file write succeeds. Theme append
            # is no longer mocked-to-fail.
            result = promote_to_watchlist("LRCX", theme_path, vault_root=td_path)
            self.assertTrue(result.exists(), "Retry must self-heal the watchlist file")
            content = index_path.read_text()
            self.assertEqual(
                content.count("| [[LRCX]]"),
                1,
                "Idempotent guard should not duplicate the row on retry",
            )

    def test_promote_preserves_existing_index_on_theme_failure(self):
        """m2.22 F3 + N2: when an index already has rows, rollback must
        leave the unrelated rows intact while removing only this
        symbol's row."""
        with tempfile.TemporaryDirectory() as td:
            td_path = Path(td)
            theme_path = td_path / "wiki" / "macro-themes" / "theme_ai-compute-demand.md"
            self._make_theme_file(theme_path)
            # Pre-stage an index with an unrelated symbol.
            index_path = td_path / "wiki" / "watchlist" / "index.md"
            index_path.parent.mkdir(parents=True, exist_ok=True)
            prior_index = (
                "---\n"
                "tags: [watchlist, index, k2bi]\n"
                "date: 2026-04-25\n"
                "type: index\n"
                "origin: k2bi-generate\n"
                "up: \"[[index]]\"\n"
                "---\n\n"
                "# Watchlist Index\n\n"
                "| Symbol | Date | Status |\n|---|---|---|\n"
                "| [[NVDA]] | 2026-04-25 | screened |\n"
            )
            index_path.write_text(prior_index)

            with patch(
                "scripts.lib.invest_narrative_pipeline._append_promotion_to_theme",
                side_effect=RuntimeError("simulated theme-log failure"),
            ):
                with self.assertRaises(RuntimeError):
                    promote_to_watchlist("LRCX", theme_path, vault_root=td_path)

            self.assertTrue(index_path.exists(), "Pre-existing index must survive rollback")
            content = index_path.read_text()
            self.assertIn("| [[NVDA]]", content, "Unrelated NVDA row must survive rollback")
            self.assertNotIn("| [[LRCX]]", content, "LRCX row must be removed by rollback")


class CliTests(unittest.TestCase):
    @patch("scripts.lib.invest_narrative_pipeline.promote_to_watchlist")
    def test_cli_promote(self, mock_promote):
        mock_promote.return_value = Path("/vault/wiki/watchlist/LRCX.md")
        code = main(["--promote", "LRCX", "--theme-file", "/some/theme.md"])
        self.assertEqual(code, 0)
        mock_promote.assert_called_once_with("LRCX", Path("/some/theme.md"))

    @patch("scripts.lib.invest_narrative_pipeline.run_pipeline")
    def test_cli_run(self, mock_run):
        mock_run.return_value = Path("/vault/wiki/macro-themes/theme_test.md")
        code = main(["--narrative", "Test narrative"])
        self.assertEqual(code, 0)
        mock_run.assert_called_once_with(
            "Test narrative",
            order_preference="any",
            lived_signal=None,
            llm_provider="kimi-coding",
        )

    def test_cli_no_args(self):
        code = main([])
        self.assertEqual(code, 1)

    def test_cli_promote_missing_theme_file(self):
        with self.assertRaises(SystemExit):
            main(["--promote", "LRCX"])


class SystemPromptTests(unittest.TestCase):
    def test_no_hardcoded_prefer_2nd_3rd(self):
        self.assertNotIn("Prefer 2nd-order and 3rd-order", _SYSTEM_PROMPT)


class OrderPreferenceTests(unittest.TestCase):
    @patch("scripts.lib.minimax_common.chat_completion")
    def test_any_does_not_append(self, mock_chat):
        mock_chat.return_value = {
            "choices": [{"message": {"content": '{"sub_themes": []}'}}]
        }
        _default_call1("Test narrative", order_preference="any")
        user = mock_chat.call_args[1]["messages"][1]["content"]
        self.assertNotIn("Strongly weight 1st-order", user)
        self.assertNotIn("Strongly prefer 2nd-order", user)

    @patch("scripts.lib.minimax_common.chat_completion")
    def test_1st_emphasis_appended_to_call1(self, mock_chat):
        mock_chat.return_value = {
            "choices": [{"message": {"content": '{"sub_themes": []}'}}]
        }
        _default_call1("Test narrative", order_preference="1st-emphasis")
        user = mock_chat.call_args[1]["messages"][1]["content"]
        self.assertIn(
            "Strongly weight 1st-order primary beneficiaries; only include "
            "2nd/3rd-order if their fundamentals are exceptional.",
            user,
        )

    @patch("scripts.lib.minimax_common.chat_completion")
    def test_tail_emphasis_appended_to_call1(self, mock_chat):
        mock_chat.return_value = {
            "choices": [{"message": {"content": '{"sub_themes": []}'}}]
        }
        _default_call1("Test narrative", order_preference="tail-emphasis")
        user = mock_chat.call_args[1]["messages"][1]["content"]
        self.assertIn(
            "Strongly prefer 2nd-order and 3rd-order beneficiaries; only include "
            "1st-order if their fundamentals decisively dominate.",
            user,
        )

    @patch("scripts.lib.minimax_common.chat_completion")
    def test_1st_emphasis_appended_to_call2(self, mock_chat):
        mock_chat.return_value = {
            "choices": [{"message": {"content": '{"candidates": []}'}}]
        }
        _default_call2(
            "Test narrative",
            {"name": "ST", "reasoning": "R"},
            order_preference="1st-emphasis",
        )
        user = mock_chat.call_args[1]["messages"][1]["content"]
        self.assertIn(
            "Strongly weight 1st-order primary beneficiaries; only include "
            "2nd/3rd-order if their fundamentals are exceptional.",
            user,
        )

    @patch("scripts.lib.minimax_common.chat_completion")
    def test_tail_emphasis_appended_to_call2(self, mock_chat):
        mock_chat.return_value = {
            "choices": [{"message": {"content": '{"candidates": []}'}}]
        }
        _default_call2(
            "Test narrative",
            {"name": "ST", "reasoning": "R"},
            order_preference="tail-emphasis",
        )
        user = mock_chat.call_args[1]["messages"][1]["content"]
        self.assertIn(
            "Strongly prefer 2nd-order and 3rd-order beneficiaries; only include "
            "1st-order if their fundamentals decisively dominate.",
            user,
        )


class LivedSignalTests(unittest.TestCase):
    @patch("scripts.lib.minimax_common.chat_completion")
    def test_lived_signal_injected_into_call1(self, mock_chat):
        mock_chat.return_value = {
            "choices": [{"message": {"content": '{"sub_themes": []}'}}]
        }
        _default_call1("Test narrative", lived_signal="Signal text here.")
        user = mock_chat.call_args[1]["messages"][1]["content"]
        self.assertIn(
            "Operator lived-signal context (treat as primary evidence; "
            "weight equal to any other anchor when reasoning about this narrative):",
            user,
        )
        self.assertIn("Signal text here.", user)

    @patch("scripts.lib.minimax_common.chat_completion")
    def test_lived_signal_injected_into_call2(self, mock_chat):
        mock_chat.return_value = {
            "choices": [{"message": {"content": '{"candidates": []}'}}]
        }
        _default_call2(
            "Test narrative",
            {"name": "ST", "reasoning": "R"},
            lived_signal="Signal text here.",
        )
        user = mock_chat.call_args[1]["messages"][1]["content"]
        self.assertIn(
            "Operator lived-signal context (treat as primary evidence; "
            "weight equal to any other anchor when reasoning about this narrative):",
            user,
        )
        self.assertIn("Signal text here.", user)

    @patch("scripts.lib.invest_narrative_pipeline.run_pipeline")
    def test_cli_lived_signal_from_fixture(self, mock_run):
        mock_run.return_value = Path("/vault/wiki/macro-themes/theme_test.md")
        fixture = Path(__file__).with_name("fixtures") / "lived_signal_smoke.md"
        code = main(["--narrative", "Test", "--lived-signal", str(fixture)])
        self.assertEqual(code, 0)
        args = mock_run.call_args
        self.assertIn("lived_signal", args.kwargs)
        self.assertIn("Operator runs a small office", args.kwargs["lived_signal"])

    def test_cli_missing_lived_signal_file(self):
        with self.assertRaises(SystemExit) as ctx:
            main(["--narrative", "Test", "--lived-signal", "/nonexistent/path.md"])
        self.assertEqual(ctx.exception.code, 2)


class ProviderDispatchTests(unittest.TestCase):
    @patch("scripts.lib.minimax_common.openai_search_chat_completion")
    def test_openai_search_routed_for_call1(self, mock_openai):
        mock_openai.return_value = {
            "choices": [{"message": {"content": '{"sub_themes": []}'}}]
        }
        _default_call1("Test narrative", llm_provider="openai-search")
        mock_openai.assert_called_once()
        args = mock_openai.call_args[1]
        self.assertEqual(args["max_tokens"], 2048)

    @patch("scripts.lib.minimax_common.chat_completion")
    def test_kimi_coding_routed_for_call1(self, mock_kimi):
        mock_kimi.return_value = {
            "choices": [{"message": {"content": '{"sub_themes": []}'}}]
        }
        _default_call1("Test narrative", llm_provider="kimi-coding")
        mock_kimi.assert_called_once()
        args = mock_kimi.call_args[1]
        self.assertEqual(args["model"], "kimi-for-coding")

    @patch("scripts.lib.minimax_common.openai_search_chat_completion")
    def test_openai_search_routed_for_call2(self, mock_openai):
        mock_openai.return_value = {
            "choices": [{"message": {"content": '{"candidates": []}'}}]
        }
        _default_call2(
            "Test narrative",
            {"name": "ST", "reasoning": "R"},
            llm_provider="openai-search",
        )
        mock_openai.assert_called_once()
        args = mock_openai.call_args[1]
        self.assertEqual(args["max_tokens"], 4096)

    @patch("scripts.lib.minimax_common.chat_completion")
    def test_kimi_coding_routed_for_call2(self, mock_kimi):
        mock_kimi.return_value = {
            "choices": [{"message": {"content": '{"candidates": []}'}}]
        }
        _default_call2(
            "Test narrative",
            {"name": "ST", "reasoning": "R"},
            llm_provider="kimi-coding",
        )
        mock_kimi.assert_called_once()
        args = mock_kimi.call_args[1]
        self.assertEqual(args["model"], "kimi-for-coding")


class RejectionLogTests(unittest.TestCase):
    def _mock_call1(self, narrative: str) -> list[dict]:
        return [
            {"name": "Sub-theme A", "reasoning": "Reason A"},
            {"name": "Sub-theme B", "reasoning": "Reason B"},
            {"name": "Sub-theme C", "reasoning": "Reason C"},
            {"name": "Sub-theme D", "reasoning": "Reason D"},
        ]

    def _mock_call2_mixed(self, narrative: str, sub_theme: dict) -> list[dict]:
        if sub_theme["name"] == "Sub-theme A":
            return [
                {
                    "symbol": "VALID",
                    "reasoning_chain": "ok",
                    "citation_url": "https://example.com/valid",
                    "order": "2nd",
                    "ark_scores": {},
                    "sub_theme": sub_theme["name"],
                },
                {
                    "symbol": "BADURL",
                    "reasoning_chain": "ok",
                    "citation_url": "https://example.com/bad",
                    "order": "3rd",
                    "ark_scores": {},
                    "sub_theme": sub_theme["name"],
                },
                {
                    "symbol": "FAKESYM",
                    "reasoning_chain": "ok",
                    "citation_url": "https://example.com/fake",
                    "order": "1st",
                    "ark_scores": {},
                    "sub_theme": sub_theme["name"],
                },
            ]
        return [
            {
                "symbol": "VALID2",
                "reasoning_chain": "ok",
                "citation_url": "https://example.com/valid2",
                "order": "3rd",
                "ark_scores": {},
                "sub_theme": sub_theme["name"],
            },
        ]

    @patch("scripts.lib.invest_narrative_pipeline.load_registry")
    @patch("scripts.lib.invest_narrative_pipeline.validate_ticker_exists")
    @patch("scripts.lib.invest_narrative_pipeline.validate_market_cap")
    @patch("scripts.lib.invest_narrative_pipeline.validate_liquidity")
    @patch("scripts.lib.invest_narrative_pipeline.validate_citation_url")
    @patch("scripts.lib.invest_narrative_pipeline.validate_priced_in")
    def test_rejected_json_written_with_schema(
        self,
        mock_priced_in,
        mock_citation,
        mock_liq,
        mock_cap,
        mock_exists,
        mock_registry,
    ):
        mock_registry.return_value = {"VALID": {}, "VALID2": {}, "BADURL": {}}
        mock_exists.side_effect = lambda s, r: s in {"VALID", "VALID2", "BADURL"}
        mock_cap.return_value = True
        mock_liq.return_value = True
        mock_citation.side_effect = lambda url: "valid" in url
        mock_priced_in.return_value = {"flagged": False, "skipped": False}

        with tempfile.TemporaryDirectory() as td:
            td_path = Path(td)
            (td_path / "wiki" / "macro-themes").mkdir(parents=True)
            path = run_pipeline(
                "Test narrative",
                vault_root=td_path,
                call1_fn=self._mock_call1,
                call2_fn=self._mock_call2_mixed,
            )
            rejected_path = path.with_suffix(".rejected.json")
            self.assertTrue(rejected_path.exists())
            payload = json.loads(rejected_path.read_text())
            self.assertEqual(payload["theme_file"], path.name)
            self.assertEqual(payload["narrative"], "Test narrative")
            self.assertIn("run_started_at", payload)
            self.assertEqual(payload["llm_provider"], "kimi-coding")
            rejected = payload["rejected"]
            # Should contain BADURL (no_working_citation) and FAKESYM (hallucinated_symbol)
            reasons = {r["reason"] for r in rejected}
            self.assertIn("no_working_citation", reasons)
            self.assertIn("hallucinated_symbol", reasons)
            # VALID should NOT be in rejected list
            for r in rejected:
                self.assertNotEqual(r["symbol"], "VALID")
            # Schema checks
            for r in rejected:
                self.assertIn("symbol", r)
                self.assertIn("reason", r)
                self.assertIn("sub_theme", r)
                self.assertIn("raw_candidate", r)
                self.assertIn("details", r)


class IndexUpdaterTests(unittest.TestCase):
    def test_update_macro_themes_index_creates_new(self):
        with tempfile.TemporaryDirectory() as td:
            td_path = Path(td)
            _update_macro_themes_index(td_path, "slug", "Title", "2026-04-26", 5)
            path = td_path / "wiki" / "macro-themes" / "index.md"
            self.assertTrue(path.exists())
            self.assertIn("theme_slug", path.read_text())

    def test_update_macro_themes_index_appends(self):
        with tempfile.TemporaryDirectory() as td:
            td_path = Path(td)
            _update_macro_themes_index(td_path, "slug1", "Title1", "2026-04-26", 5)
            _update_macro_themes_index(td_path, "slug2", "Title2", "2026-04-26", 3)
            path = td_path / "wiki" / "macro-themes" / "index.md"
            content = path.read_text()
            self.assertIn("theme_slug1", content)
            self.assertIn("theme_slug2", content)

    def test_update_watchlist_index_creates_new(self):
        with tempfile.TemporaryDirectory() as td:
            td_path = Path(td)
            _update_watchlist_index(td_path, "NVDA", "2026-04-26", "promoted")
            path = td_path / "wiki" / "watchlist" / "index.md"
            self.assertTrue(path.exists())
            self.assertIn("NVDA", path.read_text())


if __name__ == "__main__":
    unittest.main()
