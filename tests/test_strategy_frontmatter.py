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


if __name__ == "__main__":
    unittest.main()
