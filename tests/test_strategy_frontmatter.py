"""Tests for scripts.lib.strategy_frontmatter -- the shared helper used by
Bundle 3 cycle 4 hooks (pre-commit Checks A/B/D, commit-msg transition
enforcement, post-commit sentinel landing) and by `/invest-ship --approve-*`
step A in cycle 5.

The helper has two surfaces:

1. Python API -- consumed in-process by the post-commit hook
   (`#!/usr/bin/env python3`) and by this test suite.

2. CLI -- consumed by the bash pre-commit + commit-msg hooks via
   `python3 -m scripts.lib.strategy_frontmatter <subcommand>`.

Both surfaces hit the same parse + extract code paths so tests here
cover both by parameterising over the call shape.
"""

from __future__ import annotations

import io
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from scripts.lib import strategy_frontmatter as sf


def _strategy(
    *,
    status: str = "proposed",
    name: str = "my-strategy",
    how_this_works: str = "Explanation body.",
    extras: dict | None = None,
    include_how: bool = True,
    body_after: str = "",
) -> bytes:
    lines = ["---", f"name: {name}", f"status: {status}", "strategy_type: hand_crafted"]
    if extras:
        for k, v in extras.items():
            lines.append(f"{k}: {v}")
    lines.append("---")
    lines.append("")
    if include_how:
        lines.append("## How This Works")
        lines.append("")
        lines.append(how_this_works)
    if body_after:
        lines.append("")
        lines.append(body_after)
    return ("\n".join(lines) + "\n").encode("utf-8")


class ParseTests(unittest.TestCase):
    def test_parse_valid_frontmatter_returns_dict(self):
        content = _strategy(status="proposed")
        fm = sf.parse(content)
        self.assertEqual(fm["name"], "my-strategy")
        self.assertEqual(fm["status"], "proposed")
        self.assertEqual(fm["strategy_type"], "hand_crafted")

    def test_parse_empty_input_returns_empty_dict(self):
        self.assertEqual(sf.parse(b""), {})

    def test_parse_no_frontmatter_returns_empty_dict(self):
        self.assertEqual(sf.parse(b"# just a markdown heading\n\nbody\n"), {})

    def test_parse_unterminated_frontmatter_raises(self):
        with self.assertRaises(ValueError) as cm:
            sf.parse(b"---\nname: x\nstatus: proposed\n")
        self.assertIn("unterminated", str(cm.exception).lower())

    def test_parse_invalid_yaml_raises(self):
        # An intentionally malformed YAML mapping with a tab in an
        # indentation context that yaml.safe_load rejects.
        bad = b"---\nstatus: proposed\n\tinvalid: [unclosed\n---\n"
        with self.assertRaises(ValueError):
            sf.parse(bad)

    def test_parse_non_mapping_raises(self):
        # Top-level list is not a valid strategy frontmatter.
        with self.assertRaises(ValueError) as cm:
            sf.parse(b"---\n- a\n- b\n---\n")
        self.assertIn("mapping", str(cm.exception).lower())

    def test_parse_non_utf8_raises(self):
        with self.assertRaises(ValueError):
            sf.parse(b"\xff\xfe\x00\x01\x02")

    def test_parse_preserves_nested_frontmatter(self):
        content = (
            b"---\n"
            b"name: x\n"
            b"status: approved\n"
            b"order:\n"
            b"  ticker: SPY\n"
            b"  qty: 10\n"
            b"---\n"
            b"body\n"
        )
        fm = sf.parse(content)
        self.assertEqual(fm["order"], {"ticker": "SPY", "qty": 10})


class ExtractStatusTests(unittest.TestCase):
    def test_extract_status_present(self):
        self.assertEqual(sf.extract_status({"status": "approved"}), "approved")

    def test_extract_status_missing_returns_none(self):
        self.assertIsNone(sf.extract_status({"name": "x"}))

    def test_extract_status_none_value_returns_none(self):
        self.assertIsNone(sf.extract_status({"status": None}))

    def test_extract_status_whitespace_only_returns_none(self):
        self.assertIsNone(sf.extract_status({"status": "   "}))

    def test_extract_status_strips(self):
        self.assertEqual(sf.extract_status({"status": "  approved  "}), "approved")

    def test_extract_status_coerces_non_str(self):
        # YAML sometimes yields bool/int for values that *look* like
        # them. The helper's job is textual normalisation; coerce to str.
        self.assertEqual(sf.extract_status({"status": 1}), "1")


class HowThisWorksTests(unittest.TestCase):
    def test_section_present_returns_body(self):
        content = _strategy(how_this_works="Multi-line\nexplanation.")
        self.assertEqual(
            sf.extract_how_this_works_body(content),
            "Multi-line\nexplanation.",
        )

    def test_section_missing_returns_empty(self):
        content = _strategy(include_how=False)
        self.assertEqual(sf.extract_how_this_works_body(content), "")

    def test_section_whitespace_only_returns_empty(self):
        content = _strategy(how_this_works="   \n   ")
        self.assertEqual(sf.extract_how_this_works_body(content), "")

    def test_stops_at_next_heading(self):
        content = _strategy(
            how_this_works="Explanation.",
            body_after="## Next Section\n\nOther content.",
        )
        body = sf.extract_how_this_works_body(content)
        self.assertIn("Explanation", body)
        self.assertNotIn("Other content", body)
        self.assertNotIn("Next Section", body)

    def test_case_insensitive_heading(self):
        # Keith's Bundle-1 TODO language was "## How This Works (Plain English)" --
        # the helper must match the canonical title regardless of the suffix.
        content = (
            b"---\nname: x\nstatus: proposed\n---\n"
            b"## How This Works (Plain English)\n\nBody explanation.\n"
        )
        self.assertEqual(
            sf.extract_how_this_works_body(content),
            "Body explanation.",
        )

    def test_no_frontmatter_returns_empty(self):
        self.assertEqual(
            sf.extract_how_this_works_body(b"## How This Works\n\nbody"),
            "",
        )


class AllowedStatusesTests(unittest.TestCase):
    def test_enum_has_four_values(self):
        # Spec §2.2 authoritative set.
        self.assertEqual(
            sf.ALLOWED_STATUSES,
            frozenset({"proposed", "approved", "rejected", "retired"}),
        )

    def test_enum_matches_loader_types(self):
        # The hook helper + the runtime loader must agree on the enum
        # (Bundle 2 gap closed in this cycle's types.py edit).
        from execution.strategies.types import ALLOWED_STATUSES as loader_set

        self.assertEqual(sf.ALLOWED_STATUSES, loader_set)


class AllowedTransitionsTests(unittest.TestCase):
    def test_happy_paths(self):
        for pair in [
            (sf.NEW_FILE, "proposed"),
            ("proposed", "approved"),
            ("proposed", "rejected"),
            ("approved", "retired"),
        ]:
            self.assertIn(pair, sf.ALLOWED_TRANSITIONS)

    def test_forbidden_paths_not_in_set(self):
        forbidden = [
            (sf.NEW_FILE, "approved"),
            (sf.NEW_FILE, "rejected"),
            (sf.NEW_FILE, "retired"),
            ("approved", "proposed"),
            ("rejected", "approved"),
            ("rejected", "proposed"),
            ("retired", "approved"),
            ("retired", "proposed"),
            ("approved", "rejected"),
        ]
        for pair in forbidden:
            self.assertNotIn(pair, sf.ALLOWED_TRANSITIONS)


class CheckImmutableTests(unittest.TestCase):
    def _write(self, tmp: Path, name: str, content: bytes) -> Path:
        p = tmp / name
        p.write_bytes(content)
        return p

    def test_head_not_approved_means_check_does_not_apply(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            head = self._write(tmp, "head.md", _strategy(status="proposed"))
            staged = self._write(
                tmp,
                "staged.md",
                _strategy(status="proposed", how_this_works="different body"),
            )
            code, msg = sf.check_immutable(head, staged)
            self.assertEqual(code, 0, msg)

    def test_new_file_means_check_does_not_apply(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            head = tmp / "head.md"  # does not exist
            staged = self._write(tmp, "staged.md", _strategy(status="proposed"))
            code, msg = sf.check_immutable(head, staged)
            self.assertEqual(code, 0, msg)

    def test_approved_unchanged_is_allowed(self):
        # Identical approved -> approved (e.g. someone touched mtime but
        # content is identical). No transition; content immutability
        # trivially satisfied.
        content = _strategy(status="approved", how_this_works="Exact body.")
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            head = self._write(tmp, "head.md", content)
            staged = self._write(tmp, "staged.md", content)
            code, msg = sf.check_immutable(head, staged)
            self.assertEqual(code, 0, msg)

    def test_approved_to_retired_with_added_fields_allowed(self):
        head = _strategy(status="approved", how_this_works="Body.")
        staged = _strategy(
            status="retired",
            how_this_works="Body.",
            extras={
                "retired_at": "2026-04-19T10:00:00Z",
                "retired_reason": '"obsolete"',
            },
        )
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            hp = self._write(tmp, "h.md", head)
            sp = self._write(tmp, "s.md", staged)
            code, msg = sf.check_immutable(hp, sp)
            self.assertEqual(code, 0, msg)

    def test_approved_body_edit_rejected(self):
        head = _strategy(status="approved", how_this_works="Original body.")
        staged = _strategy(status="approved", how_this_works="Tweaked body.")
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            hp = self._write(tmp, "h.md", head)
            sp = self._write(tmp, "s.md", staged)
            code, msg = sf.check_immutable(hp, sp)
            self.assertEqual(code, 1)
            self.assertIn("content-immutable", msg)

    def test_approved_order_edit_rejected(self):
        head = _strategy(
            status="approved",
            extras={"risk_envelope_pct": "0.01"},
        )
        staged = _strategy(
            status="approved",
            extras={"risk_envelope_pct": "0.05"},  # widened post-approval!
        )
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            hp = self._write(tmp, "h.md", head)
            sp = self._write(tmp, "s.md", staged)
            code, msg = sf.check_immutable(hp, sp)
            self.assertEqual(code, 1)
            self.assertIn("risk_envelope_pct", msg)

    def test_retire_with_comingled_field_change_rejected(self):
        head = _strategy(
            status="approved",
            extras={"risk_envelope_pct": "0.01"},
        )
        staged = _strategy(
            status="retired",
            extras={
                "risk_envelope_pct": "0.05",  # co-mingled edit
                "retired_at": "2026-04-19T10:00:00Z",
                "retired_reason": "obsolete",
            },
        )
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            hp = self._write(tmp, "h.md", head)
            sp = self._write(tmp, "s.md", staged)
            code, msg = sf.check_immutable(hp, sp)
            self.assertEqual(code, 1)

    def test_retire_with_body_edit_rejected(self):
        head = _strategy(status="approved", how_this_works="Body.")
        staged = _strategy(
            status="retired",
            how_this_works="Body REVISED.",  # body cannot change during retire
            extras={
                "retired_at": "2026-04-19T10:00:00Z",
                "retired_reason": "obsolete",
            },
        )
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            hp = self._write(tmp, "h.md", head)
            sp = self._write(tmp, "s.md", staged)
            code, msg = sf.check_immutable(hp, sp)
            self.assertEqual(code, 1)
            self.assertIn("body", msg.lower())

    def test_retire_with_extra_frontmatter_field_rejected(self):
        head = _strategy(status="approved")
        staged = _strategy(
            status="retired",
            extras={
                "retired_at": "2026-04-19T10:00:00Z",
                "retired_reason": "obsolete",
                "new_field": "sneaky",  # not allowed
            },
        )
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            hp = self._write(tmp, "h.md", head)
            sp = self._write(tmp, "s.md", staged)
            code, msg = sf.check_immutable(hp, sp)
            self.assertEqual(code, 1)
            self.assertIn("new_field", msg)

    def test_approved_to_non_retired_status_rejected(self):
        # Check D must hold even when transition is forbidden by the
        # matrix (pre-commit owns content-locking; commit-msg owns
        # transition matrix; both must fire -- defense in depth).
        head = _strategy(status="approved")
        staged = _strategy(status="proposed")
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            hp = self._write(tmp, "h.md", head)
            sp = self._write(tmp, "s.md", staged)
            code, msg = sf.check_immutable(hp, sp)
            self.assertEqual(code, 1)
            self.assertIn("retirement", msg.lower())


class CheckTransitionTests(unittest.TestCase):
    def _write(self, tmp: Path, name: str, content: bytes) -> Path:
        p = tmp / name
        p.write_bytes(content)
        return p

    def test_new_file_to_proposed_allowed(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            head = tmp / "head.md"  # does not exist
            staged = self._write(tmp, "s.md", _strategy(status="proposed"))
            code, _, old, new = sf.check_transition(head, staged)
            self.assertEqual(code, 0)
            self.assertEqual(old, sf.NEW_FILE)
            self.assertEqual(new, "proposed")

    def test_proposed_to_approved_allowed(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            head = self._write(tmp, "h.md", _strategy(status="proposed"))
            staged = self._write(tmp, "s.md", _strategy(status="approved"))
            code, _, old, new = sf.check_transition(head, staged)
            self.assertEqual(code, 0)
            self.assertEqual((old, new), ("proposed", "approved"))

    def test_approved_to_retired_allowed(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            head = self._write(tmp, "h.md", _strategy(status="approved"))
            staged = self._write(tmp, "s.md", _strategy(status="retired"))
            code, _, old, new = sf.check_transition(head, staged)
            self.assertEqual(code, 0)
            self.assertEqual((old, new), ("approved", "retired"))

    def test_body_only_edit_returns_same_status(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            head = self._write(tmp, "h.md", _strategy(status="proposed"))
            staged = self._write(
                tmp,
                "s.md",
                _strategy(status="proposed", how_this_works="new body"),
            )
            code, _, old, new = sf.check_transition(head, staged)
            self.assertEqual(code, 0)
            self.assertEqual(old, new)

    def test_new_file_to_approved_forbidden(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            head = tmp / "head.md"
            staged = self._write(tmp, "s.md", _strategy(status="approved"))
            code, msg, _, _ = sf.check_transition(head, staged)
            self.assertEqual(code, 1)
            self.assertIn("forbidden", msg)

    def test_approved_to_proposed_forbidden(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            head = self._write(tmp, "h.md", _strategy(status="approved"))
            staged = self._write(tmp, "s.md", _strategy(status="proposed"))
            code, _, _, _ = sf.check_transition(head, staged)
            self.assertEqual(code, 1)

    def test_rejected_to_approved_forbidden(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            head = self._write(tmp, "h.md", _strategy(status="rejected"))
            staged = self._write(tmp, "s.md", _strategy(status="approved"))
            code, _, _, _ = sf.check_transition(head, staged)
            self.assertEqual(code, 1)

    def test_retired_to_any_forbidden(self):
        for new_status in ("proposed", "approved", "rejected"):
            with tempfile.TemporaryDirectory() as tmp:
                tmp = Path(tmp)
                head = self._write(tmp, "h.md", _strategy(status="retired"))
                staged = self._write(tmp, "s.md", _strategy(status=new_status))
                code, _, _, _ = sf.check_transition(head, staged)
                self.assertEqual(code, 1, f"retired->{new_status} should be forbidden")


# ---------- CLI subcommand tests ----------


class DeriveRetireSlugTests(unittest.TestCase):
    def test_strips_strategy_prefix_from_stem(self):
        self.assertEqual(
            sf.derive_retire_slug("wiki/strategies/strategy_spy-rotational.md"),
            "spy-rotational",
        )

    def test_flat_stem_unchanged(self):
        # Legacy / test fixtures: files without the `strategy_` prefix
        # use the stem directly.
        self.assertEqual(
            sf.derive_retire_slug("tmp/mean.reversion.md"),
            "mean.reversion",
        )

    def test_no_md_extension_still_works(self):
        # derive_retire_slug only strips the filename stem; callers
        # pass the real file path so .md is handled by Path().stem.
        self.assertEqual(
            sf.derive_retire_slug("wiki/strategies/strategy_foo"),
            "foo",
        )


class DeriveRetireSlugEngineParity(unittest.TestCase):
    """The helper's derive_retire_slug MUST produce identical output
    to execution.engine.main.derive_retire_slug for every input the
    engine + hooks will see. If they drift, the post-commit sentinel
    lands under a different key than the engine checks on submit ->
    retirement gate silently disabled."""

    def test_matches_engine_for_representative_inputs(self):
        from execution.engine.main import derive_retire_slug as engine_impl

        cases = [
            "wiki/strategies/strategy_spy-rotational.md",
            "wiki/strategies/strategy_foo.md",
            "wiki/strategies/strategy_meanrev-v2.md",
            "wiki/strategies/mean.reversion.md",  # flat layout
            "wiki/strategies/strategy_with.dots.in.name.md",
            "/abs/path/wiki/strategies/strategy_abs.md",
            "tmp/strategy_résumé.md",  # non-ASCII
            "strategy_A.md",
            "strategy_.md",  # empty slug after prefix -- edge case
        ]
        for case in cases:
            self.assertEqual(
                sf.derive_retire_slug(case),
                engine_impl(case),
                f"drift on input {case!r}",
            )


class CheckImmutableNFCNormalization(unittest.TestCase):
    def _write(self, tmp: Path, name: str, content: bytes) -> Path:
        p = tmp / name
        p.write_bytes(content)
        return p

    def test_nfc_vs_nfd_frontmatter_value_treated_as_equal(self):
        # Same character (é = U+00E9 NFC; U+0065 U+0301 NFD) in a
        # frontmatter value across HEAD and staged. Check D must not
        # flag this as a content change.
        import unicodedata

        nfc_reason = unicodedata.normalize("NFC", "obsolète")
        nfd_reason = unicodedata.normalize("NFD", "obsolète")
        self.assertNotEqual(nfc_reason, nfd_reason)  # sanity
        head = _strategy(status="approved", extras={"owner": nfc_reason})
        staged = _strategy(status="approved", extras={"owner": nfd_reason})
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            hp = self._write(tmp, "h.md", head)
            sp = self._write(tmp, "s.md", staged)
            code, msg = sf.check_immutable(hp, sp)
            self.assertEqual(code, 0, msg)

    def test_nfc_vs_nfd_body_treated_as_equal(self):
        import unicodedata

        nfc_body = unicodedata.normalize("NFC", "Résumé strategy.")
        nfd_body = unicodedata.normalize("NFD", "Résumé strategy.")
        self.assertNotEqual(nfc_body, nfd_body)
        head = _strategy(status="approved", how_this_works=nfc_body)
        staged = _strategy(status="approved", how_this_works=nfd_body)
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            hp = self._write(tmp, "h.md", head)
            sp = self._write(tmp, "s.md", staged)
            code, msg = sf.check_immutable(hp, sp)
            self.assertEqual(code, 0, msg)

    def test_semantically_different_unicode_still_rejected(self):
        # Different characters (é vs e) must still trip Check D.
        head = _strategy(status="approved", how_this_works="Résumé.")
        staged = _strategy(status="approved", how_this_works="Resume.")
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            hp = self._write(tmp, "h.md", head)
            sp = self._write(tmp, "s.md", staged)
            code, msg = sf.check_immutable(hp, sp)
            self.assertEqual(code, 1)

    def test_float_vs_quoted_string_scalar_treated_as_equal(self):
        # MiniMax R2 finding 1: approved frontmatter reformatted to
        # quote a previously-unquoted scalar should NOT trip Check D.
        # Here HEAD has `risk_envelope_pct: 0.01` (YAML float) and
        # staged has `risk_envelope_pct: "0.01"` (YAML string).
        head = _strategy(
            status="approved",
            extras={"risk_envelope_pct": "0.01"},
        )
        staged = _strategy(
            status="approved",
            extras={"risk_envelope_pct": '"0.01"'},
        )
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            hp = self._write(tmp, "h.md", head)
            sp = self._write(tmp, "s.md", staged)
            code, msg = sf.check_immutable(hp, sp)
            self.assertEqual(code, 0, msg)

    def test_int_vs_quoted_string_scalar_treated_as_equal(self):
        head = _strategy(status="approved", extras={"qty": "1"})
        staged = _strategy(status="approved", extras={"qty": '"1"'})
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            hp = self._write(tmp, "h.md", head)
            sp = self._write(tmp, "s.md", staged)
            code, msg = sf.check_immutable(hp, sp)
            self.assertEqual(code, 0, msg)

    def test_actual_numeric_change_still_rejected(self):
        # A real value change (0.01 -> 0.05) must still trip Check D.
        head = _strategy(status="approved", extras={"risk_envelope_pct": "0.01"})
        staged = _strategy(status="approved", extras={"risk_envelope_pct": "0.05"})
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            hp = self._write(tmp, "h.md", head)
            sp = self._write(tmp, "s.md", staged)
            code, msg = sf.check_immutable(hp, sp)
            self.assertEqual(code, 1)
            self.assertIn("risk_envelope_pct", msg)

    def test_datetime_vs_iso_string_treated_as_equal(self):
        # MiniMax R3 finding 1: approved_at unquoted (YAML parses as
        # datetime) vs quoted (YAML parses as string) represents the
        # same timestamp -- Check D must not flag this as a change.
        head = _strategy(
            status="approved",
            extras={"approved_at": "2026-04-19T10:00:00Z"},  # unquoted scalar
        )
        staged = _strategy(
            status="approved",
            extras={"approved_at": '"2026-04-19T10:00:00Z"'},  # quoted scalar
        )
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            hp = self._write(tmp, "h.md", head)
            sp = self._write(tmp, "s.md", staged)
            code, msg = sf.check_immutable(hp, sp)
            self.assertEqual(code, 0, msg)

    def test_datetime_value_change_still_rejected(self):
        # Different timestamps must still trip Check D.
        head = _strategy(
            status="approved",
            extras={"approved_at": '"2026-04-19T10:00:00Z"'},
        )
        staged = _strategy(
            status="approved",
            extras={"approved_at": '"2026-04-19T11:00:00Z"'},  # hour off
        )
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            hp = self._write(tmp, "h.md", head)
            sp = self._write(tmp, "s.md", staged)
            code, msg = sf.check_immutable(hp, sp)
            self.assertEqual(code, 1)
            self.assertIn("approved_at", msg)


class CLITests(unittest.TestCase):
    def _run(self, args: list[str], stdin: bytes = b"") -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, "-m", "scripts.lib.strategy_frontmatter", *args],
            input=stdin,
            capture_output=True,
            cwd=str(Path(__file__).resolve().parent.parent),
            check=False,
        )

    def test_status_prints_value(self):
        result = self._run(["status"], stdin=_strategy(status="approved"))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.decode().strip(), "approved")

    def test_status_empty_when_missing(self):
        result = self._run(["status"], stdin=b"---\nname: x\n---\nbody\n")
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout.decode().strip(), "")

    def test_status_exit_1_on_bad_yaml(self):
        result = self._run(["status"], stdin=b"---\nname: x\n")
        self.assertEqual(result.returncode, 1)
        self.assertIn(b"unterminated", result.stderr.lower())

    def test_validate_status_rejects_unknown_enum(self):
        result = self._run(
            ["validate-status"], stdin=_strategy(status="pending")
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn(b"pending", result.stderr)

    def test_validate_status_accepts_all_four(self):
        for s in ("proposed", "approved", "rejected", "retired"):
            result = self._run(["validate-status"], stdin=_strategy(status=s))
            self.assertEqual(result.returncode, 0, result.stderr)

    def test_how_this_works_returns_body(self):
        result = self._run(
            ["how-this-works"],
            stdin=_strategy(how_this_works="First line.\nSecond line."),
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(b"First line", result.stdout)

    def test_how_this_works_missing_returns_exit_1(self):
        result = self._run(["how-this-works"], stdin=_strategy(include_how=False))
        self.assertEqual(result.returncode, 1)

    def test_check_immutable_cli_exit_codes(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            head = tmp / "h.md"
            staged = tmp / "s.md"
            head.write_bytes(_strategy(status="approved"))
            staged.write_bytes(
                _strategy(
                    status="retired",
                    extras={
                        "retired_at": "2026-04-19T10:00:00Z",
                        "retired_reason": "done",
                    },
                )
            )
            ok = self._run(
                ["check-approved-immutable", "--head", str(head), "--staged", str(staged)]
            )
            self.assertEqual(ok.returncode, 0, ok.stderr)

            # Now break content immutability.
            staged.write_bytes(
                _strategy(status="approved", how_this_works="MUTATED")
            )
            head.write_bytes(_strategy(status="approved"))
            bad = self._run(
                ["check-approved-immutable", "--head", str(head), "--staged", str(staged)]
            )
            self.assertEqual(bad.returncode, 1)

    def test_validate_transition_cli_emits_old_new(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            head = tmp / "h.md"
            staged = tmp / "s.md"
            head.write_bytes(_strategy(status="proposed"))
            staged.write_bytes(_strategy(status="approved"))
            ok = self._run(
                ["validate-transition", "--head", str(head), "--staged", str(staged)]
            )
            self.assertEqual(ok.returncode, 0, ok.stderr)
            self.assertEqual(ok.stdout.decode().strip(), "proposed\tapproved")

    def test_retire_slug_cli_prints_stripped_slug(self):
        result = self._run(
            ["retire-slug", "wiki/strategies/strategy_spy-rotational.md"]
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.decode().strip(), "spy-rotational")

    def test_retire_slug_cli_flat_stem(self):
        result = self._run(["retire-slug", "tmp/meanrev.md"])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.decode().strip(), "meanrev")

    def test_validate_transition_cli_rejects_forbidden(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            head = tmp / "h.md"
            staged = tmp / "s.md"
            head.write_bytes(_strategy(status="approved"))
            staged.write_bytes(_strategy(status="proposed"))
            result = self._run(
                ["validate-transition", "--head", str(head), "--staged", str(staged)]
            )
            self.assertEqual(result.returncode, 1)
            self.assertEqual(result.stdout.decode().strip(), "approved\tproposed")


class AtomicWriteTests(unittest.TestCase):
    """Covers `atomic_write_bytes` -- shared Bundle 4+ helper for any
    analyst-tier skill writing to the vault."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="atomic_write_"))

    def tearDown(self) -> None:
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_round_trip_write_then_read(self) -> None:
        target = self.tmp / "sub" / "file.md"  # sub-dir auto-created
        sf.atomic_write_bytes(target, b"hello\n")
        self.assertEqual(target.read_bytes(), b"hello\n")

    def test_replaces_existing_file(self) -> None:
        target = self.tmp / "file.md"
        target.write_bytes(b"first\n")
        sf.atomic_write_bytes(target, b"second\n")
        self.assertEqual(target.read_bytes(), b"second\n")

    def test_refuses_symlink_target(self) -> None:
        """Bundle 4 R3 HIGH #1: explicit symlink refusal is defence-in-
        depth on top of POSIX rename(2)'s already-safe semantics."""
        decoy = self.tmp / "decoy.txt"
        decoy.write_text("DECOY\n")
        link = self.tmp / "link.md"
        link.symlink_to(decoy)
        with self.assertRaises(ValueError) as cm:
            sf.atomic_write_bytes(link, b"would clobber decoy\n")
        self.assertIn("symlink", str(cm.exception).lower())
        # Decoy untouched
        self.assertEqual(decoy.read_text(), "DECOY\n")

    def test_cleans_up_tempfile_on_error(self) -> None:
        import os as _os
        import unittest.mock as _mock
        target = self.tmp / "file.md"
        target.write_bytes(b"original\n")
        with _mock.patch(
            "scripts.lib.strategy_frontmatter.os.replace",
            side_effect=RuntimeError("boom"),
        ):
            with self.assertRaises(RuntimeError):
                sf.atomic_write_bytes(target, b"new\n")
        # Target retains original content
        self.assertEqual(target.read_bytes(), b"original\n")
        # No leftover tempfiles in the directory
        siblings = [p.name for p in self.tmp.iterdir()]
        self.assertEqual(
            [p for p in siblings if p.startswith(".file.md.tmp.")],
            [],
            f"tempfile leaked: {siblings}",
        )

    def test_fdopen_failure_closes_fd_and_cleans_tempfile(self) -> None:
        """R5 HIGH #2: when os.fdopen raises before ownership transfer,
        the raw fd must be closed (no FD leak) AND the tempfile must
        be unlinked.

        We simulate the fdopen failure by patching and capture the fd
        that mkstemp produced so we can assert it was closed (os.fstat
        on a closed fd raises OSError).
        """
        import os as _os
        import unittest.mock as _mock
        target = self.tmp / "file.md"
        captured_fd: list[int] = []

        real_mkstemp = tempfile.mkstemp

        def _recording_mkstemp(*args, **kwargs):
            fd, name = real_mkstemp(*args, **kwargs)
            captured_fd.append(fd)
            return fd, name

        with _mock.patch(
            "scripts.lib.strategy_frontmatter.tempfile.mkstemp",
            side_effect=_recording_mkstemp,
        ), _mock.patch(
            "scripts.lib.strategy_frontmatter.os.fdopen",
            side_effect=ValueError("simulated fdopen failure"),
        ):
            with self.assertRaises(ValueError):
                sf.atomic_write_bytes(target, b"content\n")

        # Exactly one fd was captured + it is now closed
        self.assertEqual(len(captured_fd), 1)
        with self.assertRaises(OSError):
            _os.fstat(captured_fd[0])
        # No leftover tempfiles in the directory
        siblings = [p.name for p in self.tmp.iterdir()]
        self.assertEqual(
            [p for p in siblings if p.startswith(".file.md.tmp.")],
            [],
            f"tempfile leaked: {siblings}",
        )


class HasSectionTests(unittest.TestCase):
    """Covers `has_section` -- Bundle 4 cycle 5 will consume this for
    the /invest-ship --approve-strategy backtest-override check.
    Invest-thesis cycle 1 authors the helper + proves the shape here."""

    def test_exact_match(self) -> None:
        body = "# Title\n\n## How This Works\n\nBody text\n"
        self.assertTrue(sf.has_section(body, "How This Works"))

    def test_missing_section_returns_false(self) -> None:
        body = "# Title\n\nno section here\n"
        self.assertFalse(sf.has_section(body, "How This Works"))

    def test_case_insensitive(self) -> None:
        body = "## how this works\n\nbody\n"
        self.assertTrue(sf.has_section(body, "How This Works"))

    def test_matches_parenthetical_suffix(self) -> None:
        body = "## Backtest Override (2026-04-19)\n\nreason\n"
        self.assertTrue(sf.has_section(body, "Backtest Override"))

    def test_matches_whitespace_suffix(self) -> None:
        body = "## Backtest Override last ran today\n\nreason\n"
        self.assertTrue(sf.has_section(body, "Backtest Override"))

    def test_does_not_prefix_match_unrelated_heading(self) -> None:
        """Codex R7 R2 #3: `## Backtest Overrides Pending` must NOT
        match `Backtest Override`. The suffix allow-list is limited to
        whitespace + open-paren so only the semantically-same heading
        matches."""
        body = "## Backtest Overrides Pending\n\nreason\n"
        self.assertFalse(sf.has_section(body, "Backtest Override"))

    def test_does_not_match_h3_subheadings(self) -> None:
        body = "### How This Works\n\nsub-section only\n"
        self.assertFalse(sf.has_section(body, "How This Works"))

    def test_does_not_match_indented_code_block(self) -> None:
        """Codex R7 R4 #2: a `## heading` inside an indented code block
        (or a fenced code block) is not a real section. The helper must
        require column-0 placement to avoid false positives from pasted
        examples."""
        body = (
            "# Title\n\n"
            "Example snippet:\n\n"
            "    ## How This Works\n"
            "    body text inside a 4-space code block\n"
        )
        self.assertFalse(sf.has_section(body, "How This Works"))


# ---------- forward-guidance check (Phase 3.8.6 MVP-3) ----------


class ForwardGuidanceExtractTests(unittest.TestCase):
    def _block(self, **kwargs) -> dict[str, Any]:
        """Build a minimal valid forward_guidance_check dict."""
        base = {
            "completed_at": "2026-04-27T15:30:00+08:00",
            "status": "pass",
            "thresholded_metrics": [
                {
                    "metric": "GM TTM",
                    "locked_threshold_text": "<56% triggers bucket-4 EXIT",
                    "guide_source_text": "Q1 2026 earnings transcript published 2026-04-21",
                    "guide_range_text": "54.25%-57.25%",
                    "sits_inside_guide": False,
                }
            ],
        }
        base.update(kwargs)
        return base

    def test_missing_block_returns_none(self):
        fm = {"name": "x", "status": "proposed"}
        self.assertIsNone(sf.extract_forward_guidance_check(fm))

    def test_valid_block_with_one_metric(self):
        fm = {"forward_guidance_check": self._block()}
        fgc = sf.extract_forward_guidance_check(fm)
        self.assertIsNotNone(fgc)
        self.assertEqual(fgc.status, "pass")
        self.assertEqual(len(fgc.thresholded_metrics), 1)
        self.assertEqual(fgc.thresholded_metrics[0].metric, "GM TTM")
        self.assertFalse(fgc.thresholded_metrics[0].sits_inside_guide)

    def test_valid_override_block(self):
        fm = {
            "forward_guidance_check": self._block(
                status="override",
                override_reason="Management guide is conservative; threshold is intentional.",
                thresholded_metrics=[
                    {
                        "metric": "GM TTM",
                        "locked_threshold_text": "<56% triggers bucket-4 EXIT",
                        "guide_source_text": "operator-pasted: 'we expect Q2 GM in the 54.25%-57.25% range'",
                        "guide_range_text": "54.25%-57.25%",
                        "sits_inside_guide": True,
                    }
                ],
            )
        }
        fgc = sf.extract_forward_guidance_check(fm)
        self.assertEqual(fgc.status, "override")
        self.assertTrue(fgc.thresholded_metrics[0].sits_inside_guide)

    def test_valid_waive_block(self):
        fm = {
            "forward_guidance_check": self._block(
                status="waive",
                waive_reason="No thresholded metrics in this MA-crossover strategy.",
                thresholded_metrics=[],
            )
        }
        fgc = sf.extract_forward_guidance_check(fm)
        self.assertEqual(fgc.status, "waive")
        self.assertEqual(fgc.thresholded_metrics, [])

    def test_malformed_block_not_dict_raises(self):
        fm = {"forward_guidance_check": "not-a-dict"}
        with self.assertRaises(ValueError) as cm:
            sf.extract_forward_guidance_check(fm)
        self.assertIn("mapping", str(cm.exception).lower())

    def test_missing_metric_field_raises(self):
        metrics = [
            {
                "locked_threshold_text": "<56% triggers bucket-4 EXIT",
                "guide_source_text": "s",
                "guide_range_text": "54.25%-57.25%",
                "sits_inside_guide": False,
            }
        ]
        fm = {"forward_guidance_check": self._block(thresholded_metrics=metrics)}
        with self.assertRaises(ValueError) as cm:
            sf.extract_forward_guidance_check(fm)
        self.assertIn("metric", str(cm.exception).lower())

    def test_invalid_sits_inside_guide_type_raises(self):
        metrics = [
            {
                "metric": "GM TTM",
                "locked_threshold_text": "<56% triggers bucket-4 EXIT",
                "guide_source_text": "s",
                "guide_range_text": "54.25%-57.25%",
                "sits_inside_guide": "false",
            }
        ]
        fm = {"forward_guidance_check": self._block(thresholded_metrics=metrics)}
        with self.assertRaises(ValueError) as cm:
            sf.extract_forward_guidance_check(fm)
        self.assertIn("bool", str(cm.exception).lower())

    def test_missing_thresholded_metrics_raises(self):
        block = self._block()
        del block["thresholded_metrics"]
        fm = {"forward_guidance_check": block}
        with self.assertRaises(ValueError) as cm:
            sf.extract_forward_guidance_check(fm)
        self.assertIn("thresholded_metrics", str(cm.exception).lower())

    def test_invalid_completed_at_raises(self):
        fm = {"forward_guidance_check": self._block(completed_at="not-a-date")}
        with self.assertRaises(ValueError) as cm:
            sf.extract_forward_guidance_check(fm)
        self.assertIn("completed_at", str(cm.exception).lower())


class ForwardGuidanceValidateTests(unittest.TestCase):
    def _metric(self, sits_inside_guide: bool = False) -> sf.ThresholdedMetric:
        return sf.ThresholdedMetric(
            metric="GM TTM",
            locked_threshold_text="<56% triggers bucket-4 EXIT",
            guide_source_text="Q1 2026 earnings transcript published 2026-04-21",
            guide_range_text="54.25%-57.25%",
            sits_inside_guide=sits_inside_guide,
        )

    def test_pass_with_all_outside_guide_ok(self):
        fgc = sf.ForwardGuidanceCheck(
            completed_at="2026-04-27T15:30:00+08:00",
            status="pass",
            thresholded_metrics=[self._metric(sits_inside_guide=False)],
        )
        sf.validate_forward_guidance_check(fgc)  # must not raise

    def test_pass_with_inside_guide_refuses(self):
        fgc = sf.ForwardGuidanceCheck(
            completed_at="2026-04-27T15:30:00+08:00",
            status="pass",
            thresholded_metrics=[self._metric(sits_inside_guide=True)],
        )
        with self.assertRaises(ValueError) as cm:
            sf.validate_forward_guidance_check(fgc)
        self.assertIn("pass", str(cm.exception))
        self.assertIn("GM TTM", str(cm.exception))

    def test_override_with_reason_ok(self):
        fgc = sf.ForwardGuidanceCheck(
            completed_at="2026-04-27T15:30:00+08:00",
            status="override",
            override_reason="Management guide is conservative; threshold intentional.",
            thresholded_metrics=[self._metric(sits_inside_guide=True)],
        )
        sf.validate_forward_guidance_check(fgc)  # must not raise

    def test_override_without_reason_refuses(self):
        fgc = sf.ForwardGuidanceCheck(
            completed_at="2026-04-27T15:30:00+08:00",
            status="override",
            override_reason="",
            thresholded_metrics=[self._metric(sits_inside_guide=True)],
        )
        with self.assertRaises(ValueError) as cm:
            sf.validate_forward_guidance_check(fgc)
        self.assertIn("override", str(cm.exception))
        self.assertIn("20", str(cm.exception))

    def test_waive_with_reason_ok(self):
        fgc = sf.ForwardGuidanceCheck(
            completed_at="2026-04-27T15:30:00+08:00",
            status="waive",
            waive_reason="No thresholded metrics in this MA-crossover strategy.",
            thresholded_metrics=[],
        )
        sf.validate_forward_guidance_check(fgc)  # must not raise

    def test_waive_without_reason_refuses(self):
        fgc = sf.ForwardGuidanceCheck(
            completed_at="2026-04-27T15:30:00+08:00",
            status="waive",
            waive_reason="short",
            thresholded_metrics=[],
        )
        with self.assertRaises(ValueError) as cm:
            sf.validate_forward_guidance_check(fgc)
        self.assertIn("waive", str(cm.exception))
        self.assertIn("20", str(cm.exception))

    def test_waive_with_quantitative_guide_metrics_refuses(self):
        """status='waive' must reject thresholded_metrics with quantitative guides.

        The waive contract is 'no published guide applies'; entries with concrete
        guide ranges contradict that aggregate state. P2 follow-up to MVP-3
        (commit 33b9ba5) Codex review finding.
        """
        fgc = sf.ForwardGuidanceCheck(
            completed_at="2026-04-30T10:00:00+08:00",
            status="waive",
            override_reason=None,
            waive_reason="testing waive with mixed metric states for validator hardening",
            thresholded_metrics=[
                sf.ThresholdedMetric(
                    metric="GM TTM",
                    locked_threshold_text="<56% triggers bucket-4 EXIT",
                    guide_source_text="Q1 2026 transcript published 2026-04-21",
                    guide_range_text="54.25%-57.25%",
                    sits_inside_guide=True,
                    operator_note=None,
                )
            ],
        )
        with self.assertRaises(ValueError) as cm:
            sf.validate_forward_guidance_check(fgc)
        self.assertIn("quantitative guide", str(cm.exception))

    def test_missing_block_refuses(self):
        with self.assertRaises(ValueError) as cm:
            sf.validate_forward_guidance_check(None)
        self.assertIn("missing", str(cm.exception).lower())

    def test_unknown_status_refuses(self):
        fgc = sf.ForwardGuidanceCheck(
            completed_at="2026-04-27T15:30:00+08:00",
            status="maybe",
            thresholded_metrics=[],
        )
        with self.assertRaises(ValueError) as cm:
            sf.validate_forward_guidance_check(fgc)
        self.assertIn("maybe", str(cm.exception))


if __name__ == "__main__":
    unittest.main()
