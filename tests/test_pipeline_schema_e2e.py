"""invest-coach <-> cycle-5 helper schema reconciliation -- integration test.

Spec: K2Bi-Vault/wiki/planning/feature_invest-coach-cycle5-helper-schema-reconciliation.md
Section: "Implementation breakdown" -> Boundary E.

The bug under test:
    "Next ticker thesis requires manual schema reshaping during cycle-5
    helper approval."

The test simulates the data invest-coach has at the close of T6/T8/T10/T11
and asserts that the canonical builders the spec introduces produce
frontmatter dicts that:

    1. Pass the helper-side REQUIRED_STRATEGY_FIELDS check
    2. Pass REQUIRED_ORDER_FIELDS check inside order:
    3. Pass validate_forward_guidance_check (MVP-3 shape)
    4. Parse via the engine loader's load_document (post-fcf5b0f)
    5. Satisfy T8 invest-bear-case precondition (top-level thesis_score)
    6. Satisfy T9 invest-backtest precondition (extractable order.ticker)

Today (red) the helpers that build these dicts do not exist on
scripts.lib.invest_coach. After Boundary A + A.2 ships they will exist
and this test goes green. After ship, this test is the drift detector
that fails on first CI run if the coach output ever falls out of step
with the helper input again.
"""

from __future__ import annotations

import datetime as _dt
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

import yaml

from execution.strategies import loader as engine_loader
from scripts.lib import invest_coach as ic
from scripts.lib import invest_ship_strategy as iss
from scripts.lib import strategy_frontmatter as sf


SYNTHETIC_SYMBOL = "ZZZ"
SYNTHETIC_SLUG = "zzz-2026-05_e2e-test"
SYNTHETIC_SIGID = "2026-05-08-e2e-test-signal"


def _synthetic_ticker_inputs() -> dict:
    """Inputs invest-coach has at the close of T6 + T8 for a fresh ticker.

    Mirrors the data shape that lives in coach pipeline state at the
    point where the ticker file gets written. Builders consume this and
    surface the cycle-5-required fields to top level.
    """
    return {
        "symbol": SYNTHETIC_SYMBOL,
        "sigid": SYNTHETIC_SIGID,
        "thesis_5dim_pct": 70,
        "bear_case": {
            "bear_verdict": "PROCEED",
            "bear_last_verified": "2026-05-08",
            "bear_conviction": 45,
            "bear_top_counterpoints": [
                "First counterpoint with enough text to read like a real bear-case point.",
                "Second counterpoint covering the second risk vector under examination.",
                "Third counterpoint covering the third risk vector under examination.",
            ],
            "bear_invalidation_scenarios": [
                "Scenario A: load-bearing assumption inverts.",
                "Scenario B: regime breaks the entry premise.",
            ],
        },
    }


def _synthetic_strategy_inputs() -> dict:
    """Inputs invest-coach has at the close of T10 + T11 for a fresh strategy."""
    return {
        "name": SYNTHETIC_SLUG,
        "symbol": SYNTHETIC_SYMBOL,
        "sigid": SYNTHETIC_SIGID,
        "risk_envelope_pct": Decimal("0.0025"),
        "order": {
            "ticker": SYNTHETIC_SYMBOL,
            "side": "buy",
            "qty": 71,
            "order_type": "MKT",
            "limit_price": None,
            "stop_loss": Decimal("30.00"),
            "time_in_force": "DAY",
        },
        "forward_guidance_metrics": [
            {
                "metric": "gross_margin",
                "locked_threshold_text": "GM expansion >= 50bps in FY2026",
                "guide_source_text": "FY2026 guide call, 2026-04-20",
                "guide_range_text": "+25 to +75 bps GM expansion",
                "sits_inside_guide": False,
                "operator_note": None,
            },
            {
                "metric": "adj_op_margin",
                "locked_threshold_text": "Adj op margin >= +25bps YoY",
                "guide_source_text": "FY2026 guide call, 2026-04-20",
                "guide_range_text": "+10 to +40 bps adj op margin expansion",
                "sits_inside_guide": False,
                "operator_note": None,
            },
        ],
        "forward_guidance_status": "pass",
    }


class CanonicalTickerFrontmatterTests(unittest.TestCase):
    """Boundary A.2: ticker file top-level field surfacing.

    The canonical builder must emit thesis_score, symbol, bear_verdict,
    bear-last-verified, bear_conviction, bear_top_counterpoints at the
    top level so that T8 invest-bear-case + cycle-5 helper Step A read
    them without walking nested blocks.
    """

    def test_canonical_ticker_builder_exists(self):
        """Boundary A.2 introduces ic.build_canonical_ticker_frontmatter."""
        self.assertTrue(
            hasattr(ic, "build_canonical_ticker_frontmatter"),
            "scripts.lib.invest_coach must expose "
            "build_canonical_ticker_frontmatter() per Boundary A.2",
        )

    def test_canonical_ticker_surfaces_thesis_score_top_level(self):
        """T8 invest-bear-case precondition: top-level thesis_score field."""
        builder = getattr(ic, "build_canonical_ticker_frontmatter", None)
        if builder is None:
            self.skipTest("Boundary A.2 not yet shipped")
        fm = builder(**_synthetic_ticker_inputs())
        self.assertIn("thesis_score", fm)
        self.assertIsInstance(fm["thesis_score"], int)

    def test_canonical_ticker_surfaces_bear_case_fields_top_level(self):
        """Cycle-5 helper Step A: top-level bear_verdict + bear-last-verified
        + bear_conviction + bear_top_counterpoints + symbol."""
        builder = getattr(ic, "build_canonical_ticker_frontmatter", None)
        if builder is None:
            self.skipTest("Boundary A.2 not yet shipped")
        fm = builder(**_synthetic_ticker_inputs())
        for required in (
            "symbol",
            "bear_verdict",
            "bear-last-verified",
            "bear_conviction",
            "bear_top_counterpoints",
        ):
            self.assertIn(
                required,
                fm,
                f"canonical ticker frontmatter must surface {required} "
                f"to top level (cycle-5 helper Step A precondition)",
            )
        self.assertEqual(fm["bear_verdict"], "PROCEED")
        self.assertEqual(fm["symbol"], SYNTHETIC_SYMBOL)


class CanonicalStrategyFrontmatterTests(unittest.TestCase):
    """Boundary A + A.1: strategy file schema + forward_guidance_check shape.

    The canonical builder must emit name, strategy_type, risk_envelope_pct,
    regime_filter, order (with qty + stop_loss), and forward_guidance_check
    in the MVP-3 list-of-mappings shape so that REQUIRED_STRATEGY_FIELDS
    pass and validate_forward_guidance_check passes.
    """

    def test_canonical_strategy_builder_exists(self):
        """Boundary A introduces ic.build_canonical_strategy_frontmatter."""
        self.assertTrue(
            hasattr(ic, "build_canonical_strategy_frontmatter"),
            "scripts.lib.invest_coach must expose "
            "build_canonical_strategy_frontmatter() per Boundary A",
        )

    def test_canonical_strategy_passes_required_fields(self):
        """REQUIRED_STRATEGY_FIELDS check from invest_ship_strategy.py."""
        builder = getattr(ic, "build_canonical_strategy_frontmatter", None)
        if builder is None:
            self.skipTest("Boundary A not yet shipped")
        fm = builder(**_synthetic_strategy_inputs())
        missing = iss.REQUIRED_STRATEGY_FIELDS - set(fm.keys())
        self.assertEqual(
            missing,
            set(),
            f"canonical strategy frontmatter is missing top-level fields "
            f"required by cycle-5 helper Step A: {sorted(missing)}",
        )

    def test_canonical_strategy_order_block_shape(self):
        """REQUIRED_ORDER_FIELDS check: ticker, side, qty, limit_price,
        stop_loss, time_in_force."""
        builder = getattr(ic, "build_canonical_strategy_frontmatter", None)
        if builder is None:
            self.skipTest("Boundary A not yet shipped")
        fm = builder(**_synthetic_strategy_inputs())
        order = fm.get("order")
        self.assertIsInstance(order, dict, "order: must be a mapping")
        missing = iss.REQUIRED_ORDER_FIELDS - set(order.keys())
        self.assertEqual(
            missing,
            set(),
            f"canonical order block is missing required keys: "
            f"{sorted(missing)} (must use qty + stop_loss, NOT quantity + "
            f"stop_loss_usd)",
        )
        self.assertNotIn(
            "quantity",
            order,
            "canonical order block uses `qty`, not `quantity`",
        )
        self.assertNotIn(
            "stop_loss_usd",
            order,
            "canonical order block uses `stop_loss`, not `stop_loss_usd`",
        )

    def test_canonical_strategy_forward_guidance_check_shape(self):
        """A.1: forward_guidance_check uses completed_at + thresholded_metrics
        list-of-mappings, not t11_completed_at + thresholds_evaluated mapping."""
        builder = getattr(ic, "build_canonical_strategy_frontmatter", None)
        if builder is None:
            self.skipTest("Boundary A not yet shipped")
        fm = builder(**_synthetic_strategy_inputs())
        fgc = sf.extract_forward_guidance_check(fm)
        sf.validate_forward_guidance_check(fgc)

    def test_canonical_strategy_loads_via_engine_loader(self):
        """Engine loader (post-fcf5b0f) must parse the canonical frontmatter
        without StrategyLoaderError. status=approved + commit-sha hooks
        cannot land at unit-test time; the test writes the frontmatter to
        disk with status=proposed and runs load_document (which does not
        require the approved-only fields)."""
        builder = getattr(ic, "build_canonical_strategy_frontmatter", None)
        if builder is None:
            self.skipTest("Boundary A not yet shipped")
        fm = builder(**_synthetic_strategy_inputs())
        fm["status"] = "proposed"
        fm["tags"] = ["strategy", SYNTHETIC_SYMBOL.lower(), "k2bi"]
        fm["date"] = "2026-05-08"
        fm["type"] = "strategy"
        fm["origin"] = "k2bi-generate"
        fm["up"] = "[[index]]"

        with tempfile.TemporaryDirectory() as td:
            strat_path = Path(td) / f"strategy_{SYNTHETIC_SLUG}.md"
            yaml_block = yaml.safe_dump(fm, sort_keys=False, default_flow_style=False)
            body = "\n## How This Works\n\nPlain English explanation.\n"
            strat_path.write_text(f"---\n{yaml_block}---\n{body}")

            doc = engine_loader.load_document(strat_path)
            self.assertEqual(doc.name, SYNTHETIC_SLUG)
            self.assertEqual(doc.status, "proposed")
            self.assertEqual(doc.strategy_type, "hand_crafted")
            self.assertIsNotNone(doc.order_spec)
            self.assertEqual(doc.order_spec.ticker, SYNTHETIC_SYMBOL)
            self.assertEqual(doc.order_spec.qty, 71)
            self.assertEqual(doc.order_spec.order_type, "MKT")
            self.assertIsNone(doc.order_spec.limit_price)


class StrategyBuilderGuardTests(unittest.TestCase):
    """Boundary A + A.1 input validation guards (Codex review fixes)."""

    def _base_inputs(self) -> dict:
        return _synthetic_strategy_inputs()

    def test_mkt_with_non_null_limit_price_raises(self):
        builder = getattr(ic, "build_canonical_strategy_frontmatter", None)
        if builder is None:
            self.skipTest("Boundary A not yet shipped")
        inputs = self._base_inputs()
        inputs["order"]["order_type"] = "MKT"
        inputs["order"]["limit_price"] = Decimal("30.00")
        with self.assertRaises(ValueError) as cm:
            builder(**inputs)
        self.assertIn("MKT", str(cm.exception))

    def test_lmt_without_limit_price_raises(self):
        builder = getattr(ic, "build_canonical_strategy_frontmatter", None)
        if builder is None:
            self.skipTest("Boundary A not yet shipped")
        inputs = self._base_inputs()
        inputs["order"]["order_type"] = "LMT"
        inputs["order"]["limit_price"] = None
        with self.assertRaises(ValueError) as cm:
            builder(**inputs)
        self.assertIn("limit_price", str(cm.exception))

    def test_deprecated_quantity_key_rejected(self):
        builder = getattr(ic, "build_canonical_strategy_frontmatter", None)
        if builder is None:
            self.skipTest("Boundary A not yet shipped")
        inputs = self._base_inputs()
        inputs["order"]["quantity"] = inputs["order"]["qty"]
        with self.assertRaises(ValueError) as cm:
            builder(**inputs)
        self.assertIn("quantity", str(cm.exception))

    def test_deprecated_stop_loss_usd_key_rejected(self):
        builder = getattr(ic, "build_canonical_strategy_frontmatter", None)
        if builder is None:
            self.skipTest("Boundary A not yet shipped")
        inputs = self._base_inputs()
        inputs["order"]["stop_loss_usd"] = inputs["order"]["stop_loss"]
        with self.assertRaises(ValueError) as cm:
            builder(**inputs)
        self.assertIn("stop_loss_usd", str(cm.exception))

    def test_empty_operator_note_omitted_from_serialization(self):
        builder = getattr(ic, "build_canonical_strategy_frontmatter", None)
        if builder is None:
            self.skipTest("Boundary A not yet shipped")
        inputs = self._base_inputs()
        inputs["forward_guidance_metrics"][0]["operator_note"] = ""
        fm = builder(**inputs)
        first_metric = fm["forward_guidance_check"]["thresholded_metrics"][0]
        self.assertNotIn("operator_note", first_metric)


class T9PlaceholderTests(unittest.TestCase):
    """Boundary B.2: T9 invest-backtest precondition.

    invest-coach pipeline T9 entry must auto-author a placeholder strategy
    file with order.ticker extractable, so that invest-backtest's
    `_load_strategy` precondition passes before T10 fills in the bucket
    rules.
    """

    def test_placeholder_builder_exists(self):
        """Boundary B.2 introduces ic.build_t9_placeholder_strategy_frontmatter."""
        self.assertTrue(
            hasattr(ic, "build_t9_placeholder_strategy_frontmatter"),
            "scripts.lib.invest_coach must expose "
            "build_t9_placeholder_strategy_frontmatter() per Boundary B.2",
        )

    def test_placeholder_has_extractable_ticker(self):
        """invest-backtest reads order.ticker; placeholder must carry it."""
        builder = getattr(
            ic, "build_t9_placeholder_strategy_frontmatter", None
        )
        if builder is None:
            self.skipTest("Boundary B.2 not yet shipped")
        fm = builder(
            slug=SYNTHETIC_SLUG,
            symbol=SYNTHETIC_SYMBOL,
            sigid=SYNTHETIC_SIGID,
        )
        order = fm.get("order")
        self.assertIsInstance(order, dict)
        self.assertEqual(order.get("ticker"), SYNTHETIC_SYMBOL)

    def test_placeholder_status_marks_t9_origin(self):
        """T10 close detects placeholder via status=proposed-t9-placeholder
        and overwrites with full bucket rules."""
        builder = getattr(
            ic, "build_t9_placeholder_strategy_frontmatter", None
        )
        if builder is None:
            self.skipTest("Boundary B.2 not yet shipped")
        fm = builder(
            slug=SYNTHETIC_SLUG,
            symbol=SYNTHETIC_SYMBOL,
            sigid=SYNTHETIC_SIGID,
        )
        self.assertEqual(fm.get("status"), "proposed-t9-placeholder")


class FullPipelineSchemaTests(unittest.TestCase):
    """The five-condition binary MVP test from the spec.

    A fresh ticker thesis must produce ticker + strategy frontmatter that
    satisfies the cycle-5 helper Step A schema check + the engine loader
    contract + T8 + T9 preconditions on first attempt with zero manual
    schema edits. This is the single test that closes the bug.
    """

    def test_full_pipeline_emits_canonical_schema(self):
        ticker_builder = getattr(ic, "build_canonical_ticker_frontmatter", None)
        strategy_builder = getattr(
            ic, "build_canonical_strategy_frontmatter", None
        )
        if ticker_builder is None or strategy_builder is None:
            self.skipTest("Boundary A + A.2 not yet shipped")

        ticker_fm = ticker_builder(**_synthetic_ticker_inputs())
        strategy_fm = strategy_builder(**_synthetic_strategy_inputs())

        # Condition 1: cycle-5 helper Step A required ticker fields satisfied
        for required in (
            "symbol",
            "thesis_score",
            "bear_verdict",
            "bear-last-verified",
            "bear_conviction",
            "bear_top_counterpoints",
        ):
            self.assertIn(
                required, ticker_fm, f"ticker missing top-level {required}"
            )

        # Condition 2: cycle-5 helper Step A required strategy fields satisfied
        missing = iss.REQUIRED_STRATEGY_FIELDS - set(strategy_fm.keys())
        self.assertEqual(missing, set())

        # Condition 3: order block shape correct
        order = strategy_fm["order"]
        missing_order = iss.REQUIRED_ORDER_FIELDS - set(order.keys())
        self.assertEqual(missing_order, set())

        # Condition 4: forward_guidance_check shape correct
        fgc = sf.extract_forward_guidance_check(strategy_fm)
        sf.validate_forward_guidance_check(fgc)

        # Condition 5: T9 invest-backtest precondition (extractable order.ticker
        # from frontmatter without walking nested blocks)
        self.assertEqual(strategy_fm["order"]["ticker"], SYNTHETIC_SYMBOL)
        self.assertIsInstance(strategy_fm["order"]["ticker"], str)
        self.assertTrue(strategy_fm["order"]["ticker"].strip())

        # Condition 6: engine loader contract (post-fcf5b0f) parses canonical
        # frontmatter without error
        strategy_fm["status"] = "proposed"
        strategy_fm["tags"] = ["strategy", SYNTHETIC_SYMBOL.lower(), "k2bi"]
        strategy_fm["date"] = "2026-05-08"
        strategy_fm["type"] = "strategy"
        strategy_fm["origin"] = "k2bi-generate"
        strategy_fm["up"] = "[[index]]"
        with tempfile.TemporaryDirectory() as td:
            strat_path = Path(td) / f"strategy_{SYNTHETIC_SLUG}.md"
            yaml_block = yaml.safe_dump(
                strategy_fm, sort_keys=False, default_flow_style=False
            )
            strat_path.write_text(
                f"---\n{yaml_block}---\n\n## How This Works\n\nPlain English.\n"
            )
            doc = engine_loader.load_document(strat_path)
            self.assertEqual(doc.name, SYNTHETIC_SLUG)
            self.assertEqual(doc.order_spec.ticker, SYNTHETIC_SYMBOL)


class AcceptedGapsTemplateTests(unittest.TestCase):
    """Boundary C: accepted-gap template body section.

    The coach emits the four plan-review accepted-gap blocks verbatim
    into the strategy file body at T10 close so plan-review at /ship
    time does not re-surface them as novel findings.
    """

    def test_render_accepted_gaps_section_exists(self):
        self.assertTrue(
            hasattr(ic, "render_accepted_gaps_section"),
            "scripts.lib.invest_coach must expose "
            "render_accepted_gaps_section() per Boundary C",
        )

    def test_accepted_gaps_section_covers_all_four_gaps(self):
        renderer = getattr(ic, "render_accepted_gaps_section", None)
        if renderer is None:
            self.skipTest("Boundary C not yet shipped")
        body = renderer()
        self.assertIn(ic.ACCEPTED_GAPS_HEADING, body)
        for gap_marker in (
            "Gap 1",
            "Gap 2",
            "Gap 3",
            "Gap 4",
        ):
            self.assertIn(
                gap_marker,
                body,
                f"accepted-gaps section is missing {gap_marker}",
            )


if __name__ == "__main__":
    unittest.main()
