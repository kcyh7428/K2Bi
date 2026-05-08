"""Migration script tests -- Boundary D.

Spec: feature_invest-coach-cycle5-helper-schema-reconciliation.md, Boundary D.

Coverage:
  - Strategy: order.quantity -> order.qty rename
  - Strategy: order.stop_loss_usd -> order.stop_loss rename
  - Strategy: forward_guidance_check.t11_completed_at -> completed_at rename
  - Ticker: nested bear_case fields surfaced top-level
  - Ticker: t6_close_summary.thesis_5dim_sub_scores.pct -> thesis_score
  - Idempotence: running twice produces no further changes
  - PRE-T8 detection: ticker with no bear data anywhere reports OK
"""

from __future__ import annotations

import unittest

from scripts.migrate_existing_theses_to_canonical_schema import (
    _check_ticker,
    _is_pre_t8_ticker,
    migrate_strategy_frontmatter,
    migrate_ticker_frontmatter,
)


class StrategyMigrationTests(unittest.TestCase):
    def test_quantity_renamed_to_qty(self):
        fm = {
            "name": "x",
            "order": {"quantity": 10, "ticker": "X"},
        }
        out, changes = migrate_strategy_frontmatter(fm)
        self.assertNotIn("quantity", out["order"])
        self.assertEqual(out["order"]["qty"], 10)
        self.assertIn("rename order.quantity -> order.qty", changes)

    def test_stop_loss_usd_renamed_to_stop_loss(self):
        fm = {
            "name": "x",
            "order": {"stop_loss_usd": 30.0, "ticker": "X"},
        }
        out, changes = migrate_strategy_frontmatter(fm)
        self.assertNotIn("stop_loss_usd", out["order"])
        self.assertEqual(out["order"]["stop_loss"], 30.0)

    def test_forward_guidance_t11_completed_at_renamed(self):
        fm = {
            "name": "x",
            "forward_guidance_check": {
                "t11_completed_at": "2026-05-08T00:00:00",
                "status": "pass",
            },
        }
        out, changes = migrate_strategy_frontmatter(fm)
        self.assertNotIn("t11_completed_at", out["forward_guidance_check"])
        self.assertEqual(
            out["forward_guidance_check"]["completed_at"],
            "2026-05-08T00:00:00",
        )

    def test_slug_aliased_to_name_when_name_missing(self):
        fm = {
            "slug": "x-strategy",
            "order": {"ticker": "X"},
        }
        out, changes = migrate_strategy_frontmatter(fm)
        self.assertEqual(out["name"], "x-strategy")
        self.assertEqual(out["slug"], "x-strategy")

    def test_strategy_idempotent(self):
        fm = {
            "name": "x",
            "order": {"quantity": 10, "stop_loss_usd": 30.0, "ticker": "X"},
            "forward_guidance_check": {"t11_completed_at": "ts", "status": "pass"},
        }
        once, _ = migrate_strategy_frontmatter(fm)
        twice, twice_changes = migrate_strategy_frontmatter(once)
        self.assertEqual(once, twice)
        self.assertEqual(twice_changes, [])

    def test_mixed_state_drops_stale_quantity(self):
        fm = {
            "name": "x",
            "order": {"qty": 10, "quantity": 10, "ticker": "X"},
        }
        out, changes = migrate_strategy_frontmatter(fm)
        self.assertNotIn("quantity", out["order"])
        self.assertEqual(out["order"]["qty"], 10)
        self.assertIn("drop stale order.quantity duplicate of qty", changes)

    def test_mixed_state_drops_stale_stop_loss_usd(self):
        fm = {
            "name": "x",
            "order": {
                "stop_loss": 30.0,
                "stop_loss_usd": 30.0,
                "ticker": "X",
            },
        }
        out, changes = migrate_strategy_frontmatter(fm)
        self.assertNotIn("stop_loss_usd", out["order"])

    def test_mixed_state_with_disagreeing_values_raises(self):
        fm = {
            "name": "x",
            "order": {"qty": 10, "quantity": 99, "ticker": "X"},
        }
        with self.assertRaises(ValueError) as cm:
            migrate_strategy_frontmatter(fm)
        self.assertIn("qty", str(cm.exception))

    def test_already_canonical_strategy_no_changes(self):
        fm = {
            "name": "x",
            "order": {"qty": 10, "stop_loss": 30.0, "ticker": "X"},
        }
        out, changes = migrate_strategy_frontmatter(fm)
        self.assertEqual(changes, [])
        self.assertEqual(out, fm)


class TickerMigrationTests(unittest.TestCase):
    def test_bear_case_fields_surfaced_top_level(self):
        fm = {
            "ticker": "X",
            "bear_case": {
                "bear_verdict": "PROCEED",
                "bear_last_verified": "2026-05-08",
                "bear_conviction": 45,
                "bear_top_counterpoints": ["a", "b", "c"],
            },
        }
        out, changes = migrate_ticker_frontmatter(fm)
        self.assertEqual(out["bear_verdict"], "PROCEED")
        self.assertEqual(out["bear-last-verified"], "2026-05-08")
        self.assertEqual(out["bear_conviction"], 45)
        self.assertEqual(out["bear_top_counterpoints"], ["a", "b", "c"])
        self.assertEqual(out["symbol"], "X")

    def test_thesis_score_surfaced_from_t6_close_summary(self):
        fm = {
            "ticker": "X",
            "t6_close_summary": {
                "thesis_5dim_sub_scores": {"pct": 70},
            },
        }
        out, changes = migrate_ticker_frontmatter(fm)
        self.assertEqual(out["thesis_score"], 70)

    def test_ticker_idempotent(self):
        fm = {
            "ticker": "X",
            "bear_case": {
                "bear_verdict": "PROCEED",
                "bear_last_verified": "2026-05-08",
                "bear_conviction": 45,
                "bear_top_counterpoints": ["a"],
            },
            "t6_close_summary": {"thesis_5dim_sub_scores": {"pct": 70}},
        }
        once, _ = migrate_ticker_frontmatter(fm)
        twice, twice_changes = migrate_ticker_frontmatter(once)
        self.assertEqual(once, twice)
        self.assertEqual(twice_changes, [])

    def test_pre_t8_ticker_detected(self):
        fm = {"symbol": "CALX", "thesis_score": 62}
        self.assertTrue(_is_pre_t8_ticker(fm))
        missing = _check_ticker(fm)
        self.assertEqual(missing, [])

    def test_partial_bear_case_is_drift_not_pre_t8(self):
        fm = {
            "symbol": "X",
            "thesis_score": 70,
            "bear_verdict": "PROCEED",
        }
        self.assertFalse(_is_pre_t8_ticker(fm))
        missing = _check_ticker(fm)
        self.assertIn("bear-last-verified", missing)


if __name__ == "__main__":
    unittest.main()
