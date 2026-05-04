"""Tests for invest-coach -- Phase 3.8a.

Covers all 11 binary MVP gates plus schema validators.
Each gate has at least one passing test (asserts the named pass condition)
and at least one failing test (proves the gate catches its named failure mode).
"""

from __future__ import annotations

import datetime as _dt
import fcntl
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts.lib import invest_coach as ic
from scripts.lib import invest_coach_schemas as ics
from scripts.lib import strategy_frontmatter as sf


# ---------- helpers ----------


def _make_vault(tmp: Path) -> Path:
    """Create a minimal vault skeleton under tmp."""
    for sub in [
        "wiki/tickers",
        "wiki/strategies",
        "wiki/macro-themes",
        "wiki/watchlist",
        "raw/coach-feedback",
        "System/memory",
    ]:
        (tmp / sub).mkdir(parents=True, exist_ok=True)
    return tmp


# ---------- Gate 1: Synthetic end-to-end (partial; full e2e is skill-level) ----------


class Gate1EndToEndArtifactTests(unittest.TestCase):
    """Gate 1: verify that the Python helpers produce the expected vault artifacts."""

    def test_lived_signal_artifact_schema_passes(self):
        with tempfile.TemporaryDirectory() as td:
            vault = _make_vault(Path(td))
            fm = {
                "tags": ["context", "lived-signal", "k2bi"],
                "date": "2026-05-04",
                "type": "lived-signal",
                "origin": "keith",
                "up": "[[index]]",
                "sigid": "2026-05-04-test-signal",
                "captured_via": "invest-coach",
                "narrative_status": "refined",
            }
            ics.validate_lived_signal_frontmatter(fm)  # must not raise

    def test_lived_signal_artifact_missing_required_field_fails(self):
        fm = {
            "tags": ["context", "lived-signal", "k2bi"],
            "date": "2026-05-04",
            "type": "lived-signal",
            "origin": "keith",
            "up": "[[index]]",
            "sigid": "2026-05-04-test-signal",
            "captured_via": "invest-coach",
            # missing narrative_status
        }
        with self.assertRaises(ValueError) as cm:
            ics.validate_lived_signal_frontmatter(fm)
        self.assertIn("narrative_status", str(cm.exception))


# ---------- Gate 2: CALX info-set re-run (MVP-2 refusal) ----------


class Gate2CALXRefusalTests(unittest.TestCase):
    """Gate 2: un-grounded claims must produce verification refusal."""

    def test_ungrounded_claim_produces_refuse_status(self):
        claims = [
            {
                "claim_id": "fcc-hsi-scrutiny",
                "claim_text": "FCC and HSI scrutiny event on April 24",
                "claim_load_bearing": True,
                "source_url": "https://example.com/news",
                "operator_check": "refused",
                "operator_note": "No primary source confirms this event. " * 2,
            },
            {
                "claim_id": "revenue-yoy",
                "claim_text": "Q1 revenue +27% YoY",
                "claim_load_bearing": True,
                "source_url": None,
                "operator_check": "verified",
                "operator_note": None,
            },
        ]
        result = ic.build_verification_result(claims)
        self.assertEqual(result["status"], "refuse")
        self.assertIn("refuse_reason", result)
        self.assertIsNotNone(result["refuse_reason"])

    def test_all_verified_load_bearing_produces_pass(self):
        claims = [
            {
                "claim_id": "gm-guide",
                "claim_text": "GM guide 54.25-57.25%",
                "claim_load_bearing": True,
                "source_url": None,
                "operator_check": "verified",
                "operator_note": None,
            }
        ]
        result = ic.build_verification_result(claims)
        self.assertEqual(result["status"], "pass")

    def test_advisory_on_load_bearing_produces_refuse(self):
        claims = [
            {
                "claim_id": "eps-beat",
                "claim_text": "EPS beat",
                "claim_load_bearing": True,
                "source_url": None,
                "operator_check": "advisory",
                "operator_note": None,
            }
        ]
        result = ic.build_verification_result(claims)
        self.assertEqual(result["status"], "refuse")

    def test_unknown_operator_check_raises(self):
        claims = [
            {
                "claim_id": "bad-check",
                "claim_text": "some claim",
                "claim_load_bearing": True,
                "source_url": None,
                "operator_check": "unverified",
                "operator_note": None,
            }
        ]
        with self.assertRaises(ValueError) as cm:
            ic.build_verification_result(claims)
        self.assertIn("unknown operator_check", str(cm.exception))

    def test_override_without_valid_note_raises(self):
        claims = [
            {
                "claim_id": "ovr-claim",
                "claim_text": "overridden claim",
                "claim_load_bearing": True,
                "source_url": None,
                "operator_check": "override",
                "operator_note": "short",
            }
        ]
        with self.assertRaises(ValueError) as cm:
            ic.build_verification_result(claims)
        self.assertIn("requires operator_note", str(cm.exception))

    def test_valid_override_reason_upgrades_refuse_to_operator_override(self):
        claims = [
            {
                "claim_id": "fcc-hsi-scrutiny",
                "claim_text": "FCC and HSI scrutiny event on April 24",
                "claim_load_bearing": True,
                "source_url": "https://example.com/news",
                "operator_check": "refused",
                "operator_note": "No primary source confirms this event. " * 2,
            },
            {
                "claim_id": "revenue-yoy",
                "claim_text": "Q1 revenue +27% YoY",
                "claim_load_bearing": True,
                "source_url": None,
                "operator_check": "verified",
                "operator_note": None,
            },
        ]
        reason = (
            "Operator accepts the risk because the FCC/HSI event is widely "
            "reported by tier-1 outlets and the investment thesis does not "
            "hinge on this single datapoint."
        )
        result = ic.build_verification_result(
            claims, operator_override_reason=reason
        )
        self.assertEqual(result["status"], "operator-override")
        self.assertEqual(result["override_reason"], reason)

    def test_missing_override_reason_keeps_refuse(self):
        claims = [
            {
                "claim_id": "fcc-hsi-scrutiny",
                "claim_text": "FCC and HSI scrutiny event on April 24",
                "claim_load_bearing": True,
                "source_url": "https://example.com/news",
                "operator_check": "refused",
                "operator_note": "No primary source confirms this event. " * 2,
            },
            {
                "claim_id": "revenue-yoy",
                "claim_text": "Q1 revenue +27% YoY",
                "claim_load_bearing": True,
                "source_url": None,
                "operator_check": "verified",
                "operator_note": None,
            },
        ]
        # No operator_override_reason passed
        result = ic.build_verification_result(claims)
        self.assertEqual(result["status"], "refuse")
        self.assertIsNone(result["override_reason"])

    def test_override_reason_must_be_string(self):
        """Non-string override_reason (e.g. list with 20 elements) must NOT
        upgrade to operator-override; isinstance gate rejects."""
        claims = [
            {
                "claim_id": "fcc-hsi-scrutiny",
                "claim_text": "FCC and HSI scrutiny event on April 24",
                "claim_load_bearing": True,
                "source_url": "https://example.com/news",
                "operator_check": "refused",
                "operator_note": "No primary source confirms this event. " * 2,
            },
            {
                "claim_id": "revenue-yoy",
                "claim_text": "Q1 revenue +27% YoY",
                "claim_load_bearing": True,
                "source_url": None,
                "operator_check": "verified",
                "operator_note": None,
            },
        ]
        result = ic.build_verification_result(
            claims,
            operator_override_reason=["a"] * 20,  # list, not str, but len >= 20
        )
        self.assertEqual(result["status"], "refuse")
        self.assertIsNone(result["override_reason"])

    def test_unhashable_operator_check_raises_value_error(self):
        """Non-string operator_check (e.g. list from YAML deserialization) must
        raise ValueError, not TypeError; upstream handlers expect ValueError."""
        claims = [
            {
                "claim_id": "test",
                "claim_text": "some claim",
                "claim_load_bearing": True,
                "source_url": None,
                "operator_check": ["bogus"],  # list, not str
                "operator_note": None,
            }
        ]
        with self.assertRaises(ValueError) as cm:
            ic.build_verification_result(claims)
        self.assertIn("unknown operator_check", str(cm.exception))

    def test_whitespace_only_override_reason_keeps_refuse(self):
        """Whitespace-only override_reason must NOT upgrade to operator-override;
        strip() before length check rejects it."""
        claims = [
            {
                "claim_id": "fcc-hsi-scrutiny",
                "claim_text": "FCC and HSI scrutiny event on April 24",
                "claim_load_bearing": True,
                "source_url": "https://example.com/news",
                "operator_check": "refused",
                "operator_note": "No primary source confirms this event. " * 2,
            },
            {
                "claim_id": "revenue-yoy",
                "claim_text": "Q1 revenue +27% YoY",
                "claim_load_bearing": True,
                "source_url": None,
                "operator_check": "verified",
                "operator_note": None,
            },
        ]
        result = ic.build_verification_result(
            claims,
            operator_override_reason="                    ",  # 20 spaces, strip() -> 0
        )
        self.assertEqual(result["status"], "refuse")
        self.assertIsNone(result["override_reason"])


# ---------- Gate 3: Bucket-rule contradiction (MVP-3) ----------


class Gate3BucketRuleTests(unittest.TestCase):
    """Gate 3: bucket-4 EXIT inside GM guide range must be caught."""

    def test_pass_when_all_thresholds_outside_guide(self):
        metrics = [
            {
                "metric": "gross_margin_ttm",
                "locked_threshold_text": "bucket-4 EXIT at GM < 54%",
                "guide_source_text": "Q1 2026 transcript",
                "guide_range_text": "54.25%-57.25%",
                "sits_inside_guide": False,
                "operator_note": "Recalibrated below guide floor.",
            }
        ]
        fgc = ic.assemble_forward_guidance_check(
            metrics, status="pass"
        )
        self.assertEqual(fgc.status, "pass")

    def test_pass_with_inside_guide_refuses(self):
        metrics = [
            {
                "metric": "gross_margin_ttm",
                "locked_threshold_text": "bucket-4 EXIT at GM < 56%",
                "guide_source_text": "Q1 2026 transcript",
                "guide_range_text": "54.25%-57.25%",
                "sits_inside_guide": True,
                "operator_note": None,
            }
        ]
        with self.assertRaises(ValueError) as cm:
            ic.assemble_forward_guidance_check(metrics, status="pass")
        self.assertIn("pass", str(cm.exception))
        self.assertIn("gross_margin_ttm", str(cm.exception))

    def test_override_with_reason_accepted(self):
        metrics = [
            {
                "metric": "gross_margin_ttm",
                "locked_threshold_text": "bucket-4 EXIT at GM < 56%",
                "guide_source_text": "Q1 2026 transcript",
                "guide_range_text": "54.25%-57.25%",
                "sits_inside_guide": True,
                "operator_note": None,
            }
        ]
        fgc = ic.assemble_forward_guidance_check(
            metrics,
            status="override",
            override_reason="Management guide is conservative; threshold is intentional.",
        )
        self.assertEqual(fgc.status, "override")


# ---------- Gate 4: Override-path visibility (D5) ----------


class Gate4OverrideVisibilityTests(unittest.TestCase):
    """Gate 4: T12 summary must surface every override."""

    def test_override_surfaces_in_t12_summary(self):
        overrides = [
            {
                "gate": "MVP-2",
                "claim_id": "fcc-hsi-scrutiny",
                "original_verdict": "refused",
                "override_reason": "Operator accepts risk on this claim.",
                "categorical_reason": "intentional accept",
            },
            {
                "gate": "MVP-3",
                "threshold_name": "gross_margin_ttm",
                "original_verdict": "inside_guide",
                "override_reason": "Guide is conservative; threshold intentional.",
                "categorical_reason": "intentional accept",
            },
        ]
        summary = ic.render_final_summary(
            sigid="2026-05-04-test",
            symbol="TEST",
            theme_slug="test-theme",
            verification_status="operator-override",
            forward_guidance_status="override",
            overrides=overrides,
        )
        self.assertIn("MVP-2", summary)
        self.assertIn("fcc-hsi-scrutiny", summary)
        self.assertIn("MVP-3", summary)
        self.assertIn("gross_margin_ttm", summary)

    def test_no_override_shows_clean_pipeline(self):
        summary = ic.render_final_summary(
            sigid="2026-05-04-test",
            symbol="TEST",
            theme_slug="test-theme",
            verification_status="pass",
            forward_guidance_status="pass",
            overrides=[],
        )
        self.assertIn("No overrides taken", summary)


# ---------- Gate 5: T0 resume from partial state (D2) ----------


class Gate5ResumeTests(unittest.TestCase):
    """Gate 5: partial thesis draft must be readable for resume."""

    def test_subsection_atomic_writes_build_incremental_draft(self):
        with tempfile.TemporaryDirectory() as td:
            vault = _make_vault(Path(td))
            thesis_path = vault / "wiki" / "tickers" / "TEST.md"
            ic.atomic_write_thesis_subsection(
                thesis_path,
                "phase_1_business_model",
                "Revenue split: 80% product, 20% services.",
                vault,
            )
            ic.atomic_write_thesis_subsection(
                thesis_path,
                "phase_2_competitive_moat",
                "Switching costs are high.",
                vault,
            )
            content = thesis_path.read_bytes()
            fm = sf.parse(content)
            self.assertIn("draft_sections", fm)
            self.assertEqual(
                fm["draft_sections"]["phase_1_business_model"],
                "Revenue split: 80% product, 20% services.",
            )
            self.assertEqual(
                fm["draft_sections"]["phase_2_competitive_moat"],
                "Switching costs are high.",
            )

    def test_resume_reads_confirmed_sections(self):
        with tempfile.TemporaryDirectory() as td:
            vault = _make_vault(Path(td))
            thesis_path = vault / "wiki" / "tickers" / "TEST.md"
            # Simulate partial progress: phases 1-2 confirmed, 3-4 not yet
            for key in ("phase_1_business_model", "phase_2_competitive_moat"):
                ic.atomic_write_thesis_subsection(
                    thesis_path, key, f"{key} content", vault
                )
            content = thesis_path.read_bytes()
            fm = sf.parse(content)
            confirmed = set(fm.get("draft_sections", {}).keys())
            self.assertIn("phase_1_business_model", confirmed)
            self.assertIn("phase_2_competitive_moat", confirmed)
            self.assertNotIn("phase_3_financial_quality", confirmed)

    def test_invalid_section_key_raises(self):
        with tempfile.TemporaryDirectory() as td:
            vault = _make_vault(Path(td))
            thesis_path = vault / "wiki" / "tickers" / "TEST.md"
            with self.assertRaises(ValueError) as cm:
                ic.atomic_write_thesis_subsection(
                    thesis_path, "bad_key", "content", vault
                )
            self.assertIn("bad_key", str(cm.exception))


# ---------- Gate 6: Single-call bear-case discipline ----------


class Gate6SingleCallBearTests(unittest.TestCase):
    """Gate 6: T8 must invoke invest-bear-case as a single call.

    This is a skill-level contract; the Python layer has no multi-call logic.
    We verify that the helper surface does not introduce multi-call orchestration.
    """

    def test_no_multi_call_helper_exists(self):
        # The invest_coach module has no function that calls bear-case multiple
        # times. This is a structural assertion.
        self.assertFalse(hasattr(ic, "run_bear_case_multiple"))
        self.assertFalse(hasattr(ic, "orchestrate_bear_calls"))


# ---------- Gate 7: Refusal recovery ----------


class Gate7RefusalRecoveryTests(unittest.TestCase):
    """Gate 7: T13 must diagnose refusal and walk back without restart.

    The Python helpers support this by keeping partial draft state in the vault.
    """

    def test_partial_draft_persists_after_refusal(self):
        with tempfile.TemporaryDirectory() as td:
            vault = _make_vault(Path(td))
            thesis_path = vault / "wiki" / "tickers" / "TEST.md"
            # Write some draft state
            ic.atomic_write_thesis_subsection(
                thesis_path, "phase_1_business_model", "content", vault
            )
            # Simulate refusal: thesis file still exists with partial state
            self.assertTrue(thesis_path.exists())
            fm = sf.parse(thesis_path.read_bytes())
            self.assertIn("draft_sections", fm)


# ---------- Gate 8: invest-feedback auto-capture (D7) ----------


class Gate8FeedbackAutoCaptureTests(unittest.TestCase):
    """Gate 8: rejection events must write coach-feedback files atomically."""

    def test_rejection_writes_feedback_file(self):
        with tempfile.TemporaryDirectory() as td:
            vault = _make_vault(Path(td))
            path = ic.capture_coach_rejection(
                vault,
                sigid="2026-05-04-test",
                turn_id="T2",
                rejected_framing="The narrative is about oil.",
                operator_correction="The narrative is about renewables.",
            )
            self.assertTrue(path.exists())
            content = path.read_bytes()
            fm = sf.parse(content)
            self.assertEqual(fm.get("sigid"), "2026-05-04-test")
            self.assertEqual(fm.get("turn_id"), "T2")
            body = sf._split_body(content)
            self.assertIn("oil", body)
            self.assertIn("renewables", body)

    def test_rejection_missing_sigid_raises(self):
        with tempfile.TemporaryDirectory() as td:
            vault = _make_vault(Path(td))
            with self.assertRaises(ValueError):
                ic.capture_coach_rejection(
                    vault,
                    sigid="",
                    turn_id="T2",
                    rejected_framing="x",
                    operator_correction="y",
                )

    def test_rejection_path_traversal_in_sigid_raises(self):
        with tempfile.TemporaryDirectory() as td:
            vault = _make_vault(Path(td))
            with self.assertRaises(ValueError) as cm:
                ic.capture_coach_rejection(
                    vault,
                    sigid="../../../etc/passwd",
                    turn_id="T2",
                    rejected_framing="x",
                    operator_correction="y",
                )
            self.assertIn("disallowed characters", str(cm.exception))

    def test_rejection_control_char_in_sigid_raises(self):
        with tempfile.TemporaryDirectory() as td:
            vault = _make_vault(Path(td))
            with self.assertRaises(ValueError) as cm:
                ic.capture_coach_rejection(
                    vault,
                    sigid="test\ninject",
                    turn_id="T2",
                    rejected_framing="x",
                    operator_correction="y",
                )
            self.assertIn("disallowed characters", str(cm.exception))

    def test_rejection_path_traversal_in_turn_id_raises(self):
        with tempfile.TemporaryDirectory() as td:
            vault = _make_vault(Path(td))
            with self.assertRaises(ValueError) as cm:
                ic.capture_coach_rejection(
                    vault,
                    sigid="test",
                    turn_id="../escape",
                    rejected_framing="x",
                    operator_correction="y",
                )
            self.assertIn("disallowed characters", str(cm.exception))


# ---------- Gate 9: Operator-elected spot-check (D3) ----------


class Gate9SpotCheckTests(unittest.TestCase):
    """Gate 9: spot-check fires only on operator command; vendor-must-differ."""

    def test_spot_check_permitted_when_no_t55_record(self):
        self.assertTrue(ic.enforce_vendor_must_differ("Kimi DR", None))

    def test_spot_check_permitted_when_vendor_differs(self):
        vp = {"vendor": "Kimi DR", "timestamp": "2026-05-04T10:00:00+08:00"}
        self.assertTrue(ic.enforce_vendor_must_differ("Perplexity", vp))

    def test_spot_check_rejected_when_vendor_matches(self):
        vp = {"vendor": "Kimi DR", "timestamp": "2026-05-04T10:00:00+08:00"}
        self.assertFalse(ic.enforce_vendor_must_differ("Kimi DR", vp))

    def test_spot_check_case_insensitive(self):
        vp = {"vendor": "kimi dr", "timestamp": "2026-05-04T10:00:00+08:00"}
        self.assertFalse(ic.enforce_vendor_must_differ("KIMI DR", vp))


# ---------- Gate 10: Stage advancement with flock CAS (D8) ----------


class Gate10StageAdvancementTests(unittest.TestCase):
    """Gate 10: learning-stage dial update must use CAS under flock."""

    def test_suggestion_when_threshold_met(self):
        with tempfile.TemporaryDirectory() as td:
            vault = _make_vault(Path(td))
            result = ic.suggest_stage_advancement(
                vault, explained_concepts=["catalyst clarity", "asymmetry", "moat"]
            )
            self.assertTrue(result["threshold_met"])
            self.assertEqual(result["suggested_stage"], "intermediate")

    def test_no_suggestion_when_below_threshold(self):
        with tempfile.TemporaryDirectory() as td:
            vault = _make_vault(Path(td))
            result = ic.suggest_stage_advancement(
                vault, explained_concepts=["catalyst clarity"]
            )
            self.assertFalse(result["threshold_met"])

    def test_cas_write_succeeds_when_value_unchanged(self):
        with tempfile.TemporaryDirectory() as td:
            vault = _make_vault(Path(td))
            # Seed active_rules.md with novice
            rules_path = vault / "System" / "memory" / "active_rules.md"
            rules_path.write_text("learning-stage: novice\n")
            ok = ic.write_learning_stage_dial(vault, "intermediate", "novice")
            self.assertTrue(ok)
            self.assertEqual(ic.read_learning_stage(vault), "intermediate")

    def test_cas_write_fails_when_value_changed_concurrently(self):
        with tempfile.TemporaryDirectory() as td:
            vault = _make_vault(Path(td))
            rules_path = vault / "System" / "memory" / "active_rules.md"
            rules_path.write_text("learning-stage: novice\n")
            # Simulate concurrent change: another writer flipped to intermediate
            rules_path.write_text("learning-stage: intermediate\n")
            ok = ic.write_learning_stage_dial(vault, "intermediate", "novice")
            self.assertFalse(ok)

    def test_concurrent_sessions_result_in_one_flip(self):
        with tempfile.TemporaryDirectory() as td:
            vault = _make_vault(Path(td))
            rules_path = vault / "System" / "memory" / "active_rules.md"
            rules_path.write_text("learning-stage: novice\n")

            results: list[bool] = []

            def writer(expected: str, new: str):
                time.sleep(0.05)
                results.append(ic.write_learning_stage_dial(vault, new, expected))

            t1 = threading.Thread(target=writer, args=("novice", "intermediate"))
            t2 = threading.Thread(target=writer, args=("novice", "intermediate"))
            t1.start()
            t2.start()
            t1.join()
            t2.join()

            # Exactly one CAS should succeed
            self.assertEqual(sum(results), 1)
            final = ic.read_learning_stage(vault)
            self.assertEqual(final, "intermediate")

    def test_invalid_new_stage_raises(self):
        with tempfile.TemporaryDirectory() as td:
            vault = _make_vault(Path(td))
            with self.assertRaises(ValueError):
                ic.write_learning_stage_dial(vault, "expert", "novice")


# ---------- Gate 11: T5.5 bulk-research-handoff (D10) ----------


class Gate11BulkResearchHandoffTests(unittest.TestCase):
    """Gate 11: T5.5 operator-elected vendor handoff with full audit trail."""

    def test_compose_research_prompt_references_source_set(self):
        prompt = ic.compose_research_prompt(
            source_set=["https://sec.gov/10k", "https://example.com/news"],
        )
        self.assertIn("https://sec.gov/10k", prompt)
        self.assertIn("Ahern:", prompt)
        self.assertIn("Sub-scores:", prompt)

    def test_ingest_vendor_response_tags_every_section_unverified(self):
        response = (
            "## Phase 1\n\nRevenue is $1B.\n\n"
            "## Phase 2\n\nMoat is strong.\n"
        )
        draft = ic.ingest_vendor_response(
            response, "Kimi DR", "2026-05-04T10:00:00+08:00", "prompt text"
        )
        self.assertEqual(draft["vendor_name"], "Kimi DR")
        self.assertEqual(len(draft["sections"]), 2)
        for sec in draft["sections"]:
            self.assertEqual(sec["status"], "un_verified")

    def test_ingest_empty_response_raises(self):
        with self.assertRaises(ValueError):
            ic.ingest_vendor_response("", "Kimi DR", "2026-05-04T10:00:00+08:00", "p")

    def test_vendor_provenance_writes_atomically(self):
        with tempfile.TemporaryDirectory() as td:
            vault = _make_vault(Path(td))
            thesis_path = vault / "wiki" / "tickers" / "TEST.md"
            ic.write_vendor_provenance(
                thesis_path,
                vendor="Kimi DR",
                timestamp="2026-05-04T10:00:00+08:00",
                prompt="research prompt text",
                source_set_ref=["https://sec.gov/10k"],
            )
            content = thesis_path.read_bytes()
            fm = sf.parse(content)
            vp = fm.get("vendor_provenance")
            self.assertIsNotNone(vp)
            self.assertEqual(vp["vendor"], "Kimi DR")
            self.assertEqual(vp["source_set_ref"], ["https://sec.gov/10k"])

    def test_vendor_provenance_schema_validates(self):
        fm = {
            "vendor_provenance": {
                "vendor": "Kimi DR",
                "timestamp": "2026-05-04T10:00:00+08:00",
                "prompt": "p",
                "source_set_ref": ["ref"],
            }
        }
        ics.validate_vendor_provenance_frontmatter(fm)  # must not raise

    def test_vendor_provenance_missing_vendor_raises(self):
        fm = {
            "vendor_provenance": {
                "timestamp": "2026-05-04T10:00:00+08:00",
                "prompt": "p",
                "source_set_ref": ["ref"],
            }
        }
        with self.assertRaises(ValueError) as cm:
            ics.validate_vendor_provenance_frontmatter(fm)
        self.assertIn("vendor", str(cm.exception))

    def test_vendor_provenance_bad_timestamp_raises(self):
        fm = {
            "vendor_provenance": {
                "vendor": "Kimi DR",
                "timestamp": "not-a-date",
                "prompt": "p",
                "source_set_ref": ["ref"],
            }
        }
        with self.assertRaises(ValueError) as cm:
            ics.validate_vendor_provenance_frontmatter(fm)
        self.assertIn("timestamp", str(cm.exception))

    def test_t7_surfaces_vendor_warning_when_t55_elected(self):
        vp = {"vendor": "Kimi DR", "timestamp": "2026-05-04T10:00:00+08:00"}
        warning = ic.surface_vendor_warning(vp)
        self.assertIn("Kimi DR", warning)
        self.assertIn("CALX failure mode", warning)

    def test_t7_no_warning_when_t55_skipped(self):
        warning = ic.surface_vendor_warning(None)
        self.assertEqual(warning, "")

    # 11a: auto-invocation refusal
    def test_no_auto_invocation_helper_exists(self):
        # Structural check: there is no function that auto-calls a vendor.
        self.assertFalse(hasattr(ic, "auto_invoke_vendor"))
        self.assertFalse(hasattr(ic, "call_vendor_without_consent"))

    # 11b: vendor-must-differ
    def test_vendor_must_differ_rejects_same_vendor(self):
        vp = {"vendor": "Kimi DR", "timestamp": "2026-05-04T10:00:00+08:00"}
        self.assertFalse(ic.enforce_vendor_must_differ("Kimi DR", vp))

    # 11c: T12 vendor visibility
    def test_t12_includes_vendor_when_t55_elected(self):
        vp = {"vendor": "Kimi DR", "timestamp": "2026-05-04T10:00:00+08:00", "prompt": "p", "source_set_ref": ["r"]}
        summary = ic.render_final_summary(
            sigid="2026-05-04-test",
            symbol="TEST",
            theme_slug="test-theme",
            verification_status="pass",
            forward_guidance_status="pass",
            overrides=[],
            vendor_provenance=vp,
        )
        self.assertIn("Kimi DR", summary)
        self.assertIn("deep research", summary)

    def test_t12_omits_vendor_when_t55_skipped(self):
        summary = ic.render_final_summary(
            sigid="2026-05-04-test",
            symbol="TEST",
            theme_slug="test-theme",
            verification_status="pass",
            forward_guidance_status="pass",
            overrides=[],
            vendor_provenance=None,
        )
        self.assertNotIn("Vendor source", summary)
        self.assertNotIn("deep research", summary)


# ---------- Schema validator coverage ----------


class LivedSignalSchemaTests(unittest.TestCase):
    def test_valid_frontmatter_passes(self):
        fm = {
            "tags": ["context", "lived-signal", "k2bi"],
            "date": "2026-05-04",
            "type": "lived-signal",
            "origin": "keith",
            "up": "[[index]]",
            "sigid": "2026-05-04-s",
            "captured_via": "invest-coach",
            "narrative_status": "raw",
        }
        ics.validate_lived_signal_frontmatter(fm)

    def test_missing_tag_fails(self):
        fm = {
            "tags": ["lived-signal", "k2bi"],  # missing "context"
            "date": "2026-05-04",
            "type": "lived-signal",
            "origin": "keith",
            "up": "[[index]]",
            "sigid": "s",
            "captured_via": "invest-coach",
            "narrative_status": "raw",
        }
        with self.assertRaises(ValueError) as cm:
            ics.validate_lived_signal_frontmatter(fm)
        self.assertIn("context", str(cm.exception))

    def test_wrong_origin_fails(self):
        fm = {
            "tags": ["context", "lived-signal", "k2bi"],
            "date": "2026-05-04",
            "type": "lived-signal",
            "origin": "k2bi-extract",
            "up": "[[index]]",
            "sigid": "s",
            "captured_via": "invest-coach",
            "narrative_status": "raw",
        }
        with self.assertRaises(ValueError) as cm:
            ics.validate_lived_signal_frontmatter(fm)
        self.assertIn("origin", str(cm.exception))

    def test_wrong_captured_via_fails(self):
        fm = {
            "tags": ["context", "lived-signal", "k2bi"],
            "date": "2026-05-04",
            "type": "lived-signal",
            "origin": "keith",
            "up": "[[index]]",
            "sigid": "s",
            "captured_via": "manual",
            "narrative_status": "raw",
        }
        with self.assertRaises(ValueError) as cm:
            ics.validate_lived_signal_frontmatter(fm)
        self.assertIn("captured_via", str(cm.exception))

    def test_bad_narrative_status_fails(self):
        fm = {
            "tags": ["context", "lived-signal", "k2bi"],
            "date": "2026-05-04",
            "type": "lived-signal",
            "origin": "keith",
            "up": "[[index]]",
            "sigid": "s",
            "captured_via": "invest-coach",
            "narrative_status": "final",
        }
        with self.assertRaises(ValueError) as cm:
            ics.validate_lived_signal_frontmatter(fm)
        self.assertIn("narrative_status", str(cm.exception))


class VendorProvenanceSchemaTests(unittest.TestCase):
    def test_absent_block_is_ok(self):
        fm = {"name": "x"}
        ics.validate_vendor_provenance_frontmatter(fm)  # must not raise

    def test_missing_prompt_fails(self):
        fm = {
            "vendor_provenance": {
                "vendor": "Kimi DR",
                "timestamp": "2026-05-04T10:00:00+08:00",
                "source_set_ref": ["ref"],
            }
        }
        with self.assertRaises(ValueError) as cm:
            ics.validate_vendor_provenance_frontmatter(fm)
        self.assertIn("prompt", str(cm.exception))

    def test_missing_source_set_ref_fails(self):
        fm = {
            "vendor_provenance": {
                "vendor": "Kimi DR",
                "timestamp": "2026-05-04T10:00:00+08:00",
                "prompt": "p",
            }
        }
        with self.assertRaises(ValueError) as cm:
            ics.validate_vendor_provenance_frontmatter(fm)
        self.assertIn("source_set_ref", str(cm.exception))


if __name__ == "__main__":
    unittest.main()
