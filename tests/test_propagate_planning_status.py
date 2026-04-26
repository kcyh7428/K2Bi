"""Tests for the planning-status propagation engine + handlers."""

from __future__ import annotations

import io
import logging
import os
import shutil
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest.mock import patch

from scripts.lib import propagate_handlers, propagate_planning_status
from scripts.lib.propagate_handlers import (
    HANDLERS,
    render_bundle5_status,
    render_next_concrete_action,
    render_phase3_status,
)
from scripts.lib.propagate_planning_status import (
    FENCE_RE,
    main,
    propagate,
)


FIXTURES = Path(__file__).parent / "fixtures" / "propagate"


def _make_synthetic_vault(tmp_path: Path, milestones_src: Path) -> Path:
    """Build a minimal vault skeleton at tmp_path/K2Bi-Vault/wiki/planning/.

    Copies the given milestones fixture into milestones.md and seeds an
    empty index.md so the planning glob has at least one extra file.
    Returns the vault root.
    """
    vault = tmp_path / "K2Bi-Vault"
    planning = vault / "wiki" / "planning"
    planning.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(milestones_src, planning / "milestones.md")
    return vault


class HandlerUnitTests(unittest.TestCase):
    """Pure-function handler tests against the synthetic milestones fixture."""

    def setUp(self) -> None:
        self.milestones = FIXTURES / "milestones_synthetic.md"

    def test_phase3_status_pre_m213_ship(self) -> None:
        out = render_phase3_status(self.milestones)
        # 3.7 in the synthetic fixture is 🟡 NEXT, so 8 main shipped
        # (3.1-3.6 + 3.6.5 + 3.9; Q42 is special). 3.7 m2.13 NEXT.
        self.assertIn("8 of 12 main milestones shipped", out)
        self.assertIn("3.1-3.6", out)
        self.assertIn("3.6.5", out)
        self.assertIn("3.9", out)
        self.assertIn("Q42", out)
        self.assertIn("3.7 m2.13 \U0001f7e1 NEXT", out)
        self.assertIn("3.7.5 m2.22 ⏳ gates on m2.13", out)
        self.assertIn("3.8 + 3.10 + 3.11 pending", out)

    def test_bundle5_status_three_shipped(self) -> None:
        out = render_bundle5_status(self.milestones)
        self.assertIn("Bundle 5 ✅ 3 of 4 SHIPPED", out)
        # m2.9 should show only post-(z.4)+(bb) sha (`ccccccc`), not
        # the original Bundle 5a (`aaaaaaa`) or cron-env (`bbbbbbb`).
        self.assertIn("m2.9 (z.4)+(bb) `ccccccc`", out)
        self.assertNotIn("`aaaaaaa`", out)
        self.assertNotIn("`bbbbbbb`", out)
        self.assertIn("m2.19 `ddddddd`", out)
        self.assertIn("m2.20 `eeeeeee`", out)
        self.assertIn("m2.22 ⏳ LAST, gates on m2.13", out)

    def test_next_concrete_action_picks_phase3_next(self) -> None:
        out = render_next_concrete_action(self.milestones)
        # 3.7 is 🟡 NEXT in the fixture, so the action targets it.
        self.assertIn("Phase 3.7", out)
        self.assertIn("m2.13", out)
        self.assertIn("invest-screen", out)
        self.assertIn("kimi-handoff", out)


class EndToEndPropagationTests(unittest.TestCase):
    """End-to-end exercises of the propagation engine on synthetic vaults."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self._tmp.name)
        self.vault = _make_synthetic_vault(
            self.tmp_path, FIXTURES / "milestones_synthetic.md"
        )

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _write_mirror(self, name: str, content: str) -> Path:
        path = self.vault / "wiki" / "planning" / name
        path.write_text(content, encoding="utf-8")
        return path

    def test_phase3_status_fence_regenerated(self) -> None:
        mirror = self._write_mirror(
            "mirror_phase3.md",
            "Status: <!-- AUTO: phase3-status -->stale<!-- END AUTO --> end.\n",
        )
        result = propagate(vault_root=self.vault)
        self.assertEqual(result["status"], "ok")
        self.assertGreaterEqual(result["fences_replaced"], 1)
        text = mirror.read_text(encoding="utf-8")
        self.assertIn("8 of 12 main milestones shipped", text)
        self.assertNotIn(">stale<", text)

    def test_bundle5_status_fence_regenerated(self) -> None:
        mirror = self._write_mirror(
            "mirror_bundle5.md",
            "Status: <!-- AUTO: bundle5-status -->stale<!-- END AUTO --> end.\n",
        )
        propagate(vault_root=self.vault)
        text = mirror.read_text(encoding="utf-8")
        self.assertIn("Bundle 5 ✅ 3 of 4 SHIPPED", text)

    def test_next_concrete_action_fence_regenerated(self) -> None:
        mirror = self._write_mirror(
            "mirror_next.md",
            "Action: <!-- AUTO: next-concrete-action -->stale<!-- END AUTO --> end.\n",
        )
        propagate(vault_root=self.vault)
        text = mirror.read_text(encoding="utf-8")
        self.assertIn("Phase 3.7 m2.13 invest-screen", text)

    def test_idempotency_zero_diff_on_second_run(self) -> None:
        mirror = self._write_mirror(
            "mirror_idem.md",
            "x <!-- AUTO: phase3-status -->stale<!-- END AUTO --> y\n"
            "z <!-- AUTO: bundle5-status -->stale<!-- END AUTO --> w\n",
        )
        propagate(vault_root=self.vault)
        first = mirror.read_text(encoding="utf-8")
        result2 = propagate(vault_root=self.vault)
        second = mirror.read_text(encoding="utf-8")
        self.assertEqual(first, second)
        # Second run rewrites zero files because content is already canonical.
        self.assertEqual(result2["files_rewritten"], 0)

    def test_unknown_tag_logs_warning_and_skips(self) -> None:
        original = (
            "Body <!-- AUTO: nonexistent-tag -->keep me<!-- END AUTO --> tail.\n"
        )
        mirror = self._write_mirror("mirror_unknown.md", original)
        with self.assertLogs("propagate_planning_status", level="WARNING") as ctx:
            result = propagate(vault_root=self.vault)
        self.assertEqual(result["unknown_tags"], 1)
        # Mirror content unchanged because the unknown tag is preserved.
        self.assertEqual(mirror.read_text(encoding="utf-8"), original)
        # Warning mentions the unknown tag name.
        joined = "\n".join(ctx.output)
        self.assertIn("nonexistent-tag", joined)

    def test_handler_exception_bubbles_to_main_nonzero_exit(self) -> None:
        self._write_mirror(
            "mirror_throws.md",
            "x <!-- AUTO: phase3-status -->stale<!-- END AUTO --> y\n",
        )
        # Patch the registered handler to raise. Engine catches and
        # surfaces the exception via the top-level main() exit code.
        bad = lambda _path: (_ for _ in ()).throw(RuntimeError("boom"))  # noqa: E731
        with patch.dict(HANDLERS, {"phase3-status": bad}):
            with patch.object(
                propagate_planning_status,
                "_resolve_vault_root",
                return_value=self.vault,
            ):
                stderr = io.StringIO()
                with redirect_stderr(stderr):
                    rc = main()
        self.assertEqual(rc, 1)

    def test_multiple_fences_in_one_file(self) -> None:
        mirror = self._write_mirror(
            "mirror_multi.md",
            "Header: <!-- AUTO: phase3-status -->stale<!-- END AUTO -->\n\n"
            "Bundle: <!-- AUTO: bundle5-status -->stale<!-- END AUTO -->\n\n"
            "Next: <!-- AUTO: next-concrete-action -->stale<!-- END AUTO -->\n",
        )
        result = propagate(vault_root=self.vault)
        text = mirror.read_text(encoding="utf-8")
        self.assertEqual(result["fences_replaced"], 3)
        self.assertIn("8 of 12 main milestones shipped", text)
        self.assertIn("Bundle 5 ✅ 3 of 4 SHIPPED", text)
        self.assertIn("Phase 3.7 m2.13 invest-screen", text)

    def test_scan_picks_up_newly_added_file(self) -> None:
        # First run: only milestones.md and one mirror exist.
        first_mirror = self._write_mirror(
            "mirror_first.md",
            "Initial <!-- AUTO: phase3-status -->stale<!-- END AUTO -->\n",
        )
        result1 = propagate(vault_root=self.vault)
        self.assertGreaterEqual(result1["files_rewritten"], 1)

        # Add a second mirror file mid-stream: the engine must scan and
        # find it on the next run without any code changes (proves
        # scan-not-enumerate behavior).
        second_mirror = self._write_mirror(
            "mirror_second.md",
            "Later <!-- AUTO: phase3-status -->stale<!-- END AUTO -->\n",
        )
        result2 = propagate(vault_root=self.vault)
        self.assertGreaterEqual(result2["files_rewritten"], 1)
        self.assertIn(
            "8 of 12 main milestones shipped",
            second_mirror.read_text(encoding="utf-8"),
        )
        # First mirror remains canonical (zero-diff).
        self.assertIn(
            "8 of 12 main milestones shipped",
            first_mirror.read_text(encoding="utf-8"),
        )

    def test_atomic_write_failure_leaves_original_unchanged(self) -> None:
        original = "x <!-- AUTO: phase3-status -->stale<!-- END AUTO --> y\n"
        mirror = self._write_mirror("mirror_atomic.md", original)

        # Simulate a write failure mid-flight: patch atomic_write_bytes
        # to raise after the engine has computed the new content but
        # before the file lands. The original content must remain.
        def boom(*args, **kwargs):
            raise OSError("simulated write failure")

        with patch(
            "scripts.lib.propagate_planning_status.atomic_write_bytes",
            side_effect=boom,
        ):
            with self.assertRaises(OSError):
                propagate(vault_root=self.vault)
        self.assertEqual(mirror.read_text(encoding="utf-8"), original)

    def test_modify_milestones_then_propagate_updates_mirror(self) -> None:
        """Manual sanity-check shape: edit milestones.md, mirror reflects it."""
        mirror = self._write_mirror(
            "mirror_modify.md",
            "Snapshot: <!-- AUTO: phase3-status -->stale<!-- END AUTO -->\n"
            "Action: <!-- AUTO: next-concrete-action -->stale<!-- END AUTO -->\n",
        )
        propagate(vault_root=self.vault)
        before = mirror.read_text(encoding="utf-8")
        self.assertIn("8 of 12 main milestones shipped", before)
        self.assertIn("Phase 3.7 m2.13 invest-screen", before)

        # Swap the milestones source for the post-m2.13-ship variant.
        post_ship = (FIXTURES / "milestones_post_m213_ship.md").read_text(
            encoding="utf-8"
        )
        (self.vault / "wiki" / "planning" / "milestones.md").write_text(
            post_ship, encoding="utf-8"
        )
        propagate(vault_root=self.vault)
        after = mirror.read_text(encoding="utf-8")
        # Now 9 main shipped, 3.7 included in the shipped list, no NEXT row.
        self.assertIn("9 of 12 main milestones shipped", after)
        # 3.6.5 is non-simple so it breaks the 3.1-3.6 run; 3.7 starts a
        # new run after it. Expected shipped segment:
        # "3.1-3.6, 3.6.5, 3.7, 3.9, Q42".
        self.assertIn("3.1-3.6, 3.6.5, 3.7, 3.9, Q42", after)
        self.assertNotIn("3.7 m2.13 \U0001f7e1 NEXT", after)
        # next-concrete-action falls back to m2.22 review since nothing
        # is 🟡 NEXT and m2.22 is still pending in the fixture.
        self.assertIn("Phase 3.7.5 m2.22 Codex full-stack review", after)


class FenceRegexTests(unittest.TestCase):
    def test_fence_regex_captures_tag_and_body(self) -> None:
        text = "before <!-- AUTO: phase3-status -->\nbody\n<!-- END AUTO --> after"
        match = FENCE_RE.search(text)
        self.assertIsNotNone(match)
        self.assertEqual(match.group(1), "phase3-status")
        self.assertEqual(match.group(2), "\nbody\n")

    def test_fence_regex_handles_inline_form(self) -> None:
        text = "x <!-- AUTO: bundle5-status -->inline body<!-- END AUTO --> y"
        match = FENCE_RE.search(text)
        self.assertIsNotNone(match)
        self.assertEqual(match.group(1), "bundle5-status")
        self.assertEqual(match.group(2), "inline body")

    def test_fence_regex_accepts_mixed_case_tag(self) -> None:
        """Auto-formatters / inadvertent capitalization must not silently drop fences."""
        text = "x <!-- AUTO: Phase3-Status -->stale<!-- END AUTO --> y"
        match = FENCE_RE.search(text)
        self.assertIsNotNone(match)
        # Tag captured verbatim; engine canonicalizes to lowercase before
        # HANDLERS lookup, so registered names stay lowercase.
        self.assertEqual(match.group(1), "Phase3-Status")


class MixedCaseTagDispatchTests(unittest.TestCase):
    """Mixed-case tag names must dispatch to the lowercase-keyed HANDLERS."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self._tmp.name)
        self.vault = _make_synthetic_vault(
            self.tmp_path, FIXTURES / "milestones_synthetic.md"
        )

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_mixed_case_tag_dispatches_to_handler(self) -> None:
        path = self.vault / "wiki" / "planning" / "mirror_mixed.md"
        path.write_text(
            "Status: <!-- AUTO: Phase3-Status -->stale<!-- END AUTO --> end.\n",
            encoding="utf-8",
        )
        result = propagate(vault_root=self.vault)
        # Despite mixed-case tag, the handler runs and rewrites the body.
        self.assertEqual(result["unknown_tags"], 0)
        text = path.read_text(encoding="utf-8")
        self.assertIn("8 of 12 main milestones shipped", text)


class TransactionalPropagateTests(unittest.TestCase):
    """Two-pass design: a handler exception must not leave the vault half-written."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self._tmp.name)
        self.vault = _make_synthetic_vault(
            self.tmp_path, FIXTURES / "milestones_synthetic.md"
        )

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_handler_exception_aborts_before_any_write(self) -> None:
        # Two mirror docs, both stale. Make the SECOND file's handler
        # raise; the first must NOT be written because pass-1 reads
        # both before pass-2 writes any.
        first = self.vault / "wiki" / "planning" / "mirror_a.md"
        second = self.vault / "wiki" / "planning" / "mirror_b.md"
        first_text = "x <!-- AUTO: phase3-status -->stale<!-- END AUTO --> y\n"
        second_text = "z <!-- AUTO: bundle5-status -->stale<!-- END AUTO --> w\n"
        first.write_text(first_text, encoding="utf-8")
        second.write_text(second_text, encoding="utf-8")

        original_handler = HANDLERS["bundle5-status"]
        call_state = {"saw_first": False}

        def flaky_bundle5(milestones_md_path):
            # Pass 1 walks files in sorted order: mirror_a.md is first
            # (phase3-status), then mirror_b.md (bundle5-status). When
            # we hit bundle5-status, blow up.
            call_state["saw_first"] = True
            raise RuntimeError("simulated handler boom")

        with patch.dict(HANDLERS, {"bundle5-status": flaky_bundle5}):
            with self.assertRaises(RuntimeError):
                propagate(vault_root=self.vault)

        # Neither file was mutated -- pass-1 aborted before any write.
        self.assertEqual(first.read_text(encoding="utf-8"), first_text)
        self.assertEqual(second.read_text(encoding="utf-8"), second_text)


class DynamicCountTests(unittest.TestCase):
    """Counts derive from milestones.md, not from hand-maintained constants."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self._tmp.name)
        self.vault = _make_synthetic_vault(
            self.tmp_path, FIXTURES / "milestones_synthetic.md"
        )

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_adding_phase3_row_grows_denominator(self) -> None:
        """Adding `3.12` to milestones.md must bump the denominator from 12 to 13."""
        m = self.vault / "wiki" / "planning" / "milestones.md"
        text = m.read_text(encoding="utf-8")
        # Append a `3.12` row right after the 3.11 retro row.
        text = text.replace(
            "| 3.11 | Burn-in retro committed | Insights doc. |",
            "| 3.11 | Burn-in retro committed | Insights doc. |\n"
            "| 3.12 | Future synthetic milestone | tests synthetic. |",
        )
        m.write_text(text, encoding="utf-8")

        out = render_phase3_status(m)
        self.assertIn("of 13 main milestones shipped", out)

    def test_adding_bundle5_row_grows_total(self) -> None:
        """Adding a new m2.X row to Bundle 5 must bump the SHIPPED denominator."""
        m = self.vault / "wiki" / "planning" / "milestones.md"
        text = m.read_text(encoding="utf-8")
        # Append a new pending m2.99 row to the Bundle 5 table.
        text = text.replace(
            "| m2.22 | LAST Bundle 5 item; gates on m2.13. | Codex full-stack review | runs after m2.13 |",
            "| m2.22 | LAST Bundle 5 item; gates on m2.13. | Codex full-stack review | runs after m2.13 |\n"
            "| m2.99 | NEW pending synthetic milestone. | synthetic | tests synthetic. |",
        )
        m.write_text(text, encoding="utf-8")

        out = render_bundle5_status(m)
        # Total goes from 4 to 5; m2.99 appears in pending list (numeric sort places it last).
        self.assertIn("of 5 SHIPPED", out)


class VaultRootResolutionTests(unittest.TestCase):
    def test_fallback_path_logs_warning(self) -> None:
        # Force the env override + git lookup to fail by patching out
        # both. The hardcoded fallback should fire AND emit a WARNING.
        with patch.dict("os.environ", {}, clear=False):
            os.environ.pop("K2BI_VAULT_ROOT", None)
            with patch.object(
                propagate_planning_status.subprocess,
                "check_output",
                side_effect=propagate_planning_status.subprocess.CalledProcessError(
                    1, ["git"]
                ),
            ):
                with self.assertLogs(
                    "propagate_planning_status", level="WARNING"
                ) as ctx:
                    result = propagate_planning_status._resolve_vault_root()
        self.assertEqual(
            result, Path.home() / "Projects" / "K2Bi-Vault"
        )
        joined = "\n".join(ctx.output)
        self.assertIn("hardcoded fallback", joined)
        self.assertIn("K2BI_VAULT_ROOT", joined)


if __name__ == "__main__":
    unittest.main()
