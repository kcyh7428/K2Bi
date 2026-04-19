"""Unit tests for scripts/lib/minimax_review.py Phase B scope gatherers.

Ports the K2B test suite (tests/minimax-review-scope.test.sh) to Python
unittest -- K2Bi uses unittest, K2B uses bash. Same scenarios, same
assertions, same fixture-mini-repo strategy.

Each test builds a deterministic git repo in tempfile.TemporaryDirectory()
and points the gatherer at it via repo_root=Path(tempdir). No mocks --
real git commands run against real fixture repos.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts" / "lib"))

from minimax_review import (  # noqa: E402
    gather_diff_scoped_context,
    gather_file_list_context,
    gather_plan_context,
    gather_working_tree_context,
)


def build_fixture_repo(out: Path) -> None:
    """Initialize a fresh git repo with two committed files + one untracked."""
    out.mkdir(parents=True, exist_ok=True)

    def git(*args: str) -> None:
        subprocess.check_call(
            ["git", *args], cwd=out, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )

    git("init", "-q", "-b", "main")
    git("config", "user.email", "test@example.com")
    git("config", "user.name", "test")
    (out / "file_a.py").write_text("def a():\n    return 1\n")
    (out / "file_b.py").write_text("def b():\n    return 2\n")
    git("add", "file_a.py", "file_b.py")
    git("commit", "-q", "-m", "init")
    (out / "extra.py").write_text("def extra():\n    return 3\n")  # untracked


class WorkingTreeRegression(unittest.TestCase):
    """Test 1: Phase A working-tree gatherer behavior is preserved."""

    def test_working_tree_regression(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            build_fixture_repo(tmp_path)
            (tmp_path / "file_a.py").write_text("def a():\n    return 99\n")
            (tmp_path / "file_b.py").unlink()  # tracked deletion

            # 1a: determinism
            out1, files1 = gather_working_tree_context(repo_root=tmp_path)
            out2, files2 = gather_working_tree_context(repo_root=tmp_path)
            self.assertEqual(out1, out2, "gatherer must be deterministic")
            self.assertEqual(files1, files2)

            # 1b: section header ordering
            expected_headers = [
                "## git status --short",
                "## diffstat (HEAD)",
                "## diff vs HEAD",
                "## Full file contents (changed and untracked)",
            ]
            last_pos = -1
            for header in expected_headers:
                pos = out1.find(header)
                self.assertNotEqual(pos, -1, f"missing header: {header}")
                self.assertGreater(pos, last_pos, f"header out of order: {header}")
                last_pos = pos

            # 1c: deleted-file marker
            self.assertIn("_(deleted)_", out1, "deleted-file marker missing")

            # 1d: untracked file included
            self.assertIn("### extra.py", out1, "untracked extra.py missing")

            # 1e: line numbering
            self.assertIn("    1  def a():", out1, "line numbers missing")
            self.assertIn("    2      return 99", out1, "line 2 missing")

            # 1f: returned file list is sorted
            self.assertEqual(files1, sorted(files1), "returned list not sorted")

    def test_clean_tree_returns_empty(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            build_fixture_repo(tmp_path)
            (tmp_path / "extra.py").unlink()  # eliminate untracked too
            ctx, files = gather_working_tree_context(repo_root=tmp_path)
            self.assertEqual(ctx, "", "clean tree should return empty context")
            self.assertEqual(files, [])

    def test_untracked_only_omits_diff_section(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            build_fixture_repo(tmp_path)
            # extra.py is untracked, file_a/file_b unmodified
            ctx, _ = gather_working_tree_context(repo_root=tmp_path)
            self.assertNotIn(
                "## diff vs HEAD",
                ctx,
                "empty diff should omit '## diff vs HEAD' section",
            )
            self.assertIn(
                "## Full file contents",
                ctx,
                "untracked-only must still produce 'Full file contents'",
            )


class DiffScoped(unittest.TestCase):
    def test_clean_tree(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            build_fixture_repo(tmp_path)
            ctx, files = gather_diff_scoped_context(["file_a.py"], repo_root=tmp_path)
            self.assertIn("file_a.py", ctx)
            self.assertIn("    1  def a", ctx, "line numbering missing")
            self.assertNotIn("file_b.py", ctx, "file_b.py leaked into output")

    def test_excludes_unrelated_dirty_files(self) -> None:
        """The 2026-04-19 incident fix: unrelated dirty files must not leak."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            build_fixture_repo(tmp_path)
            (tmp_path / "file_a.py").write_text("def a():\n    return 99\n")
            (tmp_path / "file_b.py").write_text("def b():\n    return 99\n")
            ctx, _ = gather_diff_scoped_context(["file_a.py"], repo_root=tmp_path)
            self.assertIn("file_a.py", ctx)
            self.assertNotIn("file_b.py", ctx, "unrelated dirty file_b.py leaked")
            self.assertNotIn("extra.py", ctx, "unrelated untracked extra.py leaked")
            self.assertIn("return 99", ctx)
            self.assertIn("```diff", ctx)

    def test_returns_sorted_file_list(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            build_fixture_repo(tmp_path)
            _, files = gather_diff_scoped_context(
                ["file_b.py", "file_a.py"], repo_root=tmp_path
            )
            self.assertEqual(files, sorted(files))


class FileList(unittest.TestCase):
    def test_happy_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            build_fixture_repo(tmp_path)
            ctx, _ = gather_file_list_context(
                ["file_a.py", "file_b.py"], repo_root=tmp_path
            )
            self.assertIn("file_a.py", ctx)
            self.assertIn("file_b.py", ctx)
            self.assertIn("    1  def a", ctx)
            self.assertIn("    1  def b", ctx)
            self.assertNotIn("## git status", ctx, "file-list leaked git status")
            self.assertNotIn("```diff", ctx, "file-list leaked git diff")

    def test_warns_and_skips_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            build_fixture_repo(tmp_path)
            with self._capture_stderr() as err:
                ctx, _ = gather_file_list_context(
                    ["file_a.py", "missing.py"], repo_root=tmp_path
                )
            self.assertIn("file_a.py", ctx)
            self.assertNotIn("missing.py", ctx)
            self.assertIn("skipping missing file: missing.py", err.getvalue())

    def test_warns_and_skips_directory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            build_fixture_repo(tmp_path)
            (tmp_path / "subdir").mkdir()
            (tmp_path / "subdir" / "inner.py").write_text("inside\n")
            with self._capture_stderr() as err:
                ctx, _ = gather_file_list_context(
                    ["file_a.py", "subdir"], repo_root=tmp_path
                )
            self.assertIn("file_a.py", ctx)
            self.assertNotIn("### subdir", ctx)
            self.assertNotIn("inner.py", ctx, "gatherer should not recurse")
            self.assertIn("skipping directory: subdir", err.getvalue())

    @staticmethod
    def _capture_stderr():
        import contextlib
        import io

        buf = io.StringIO()
        return contextlib.redirect_stderr(buf) if False else _StderrCapture(buf)


class _StderrCapture:
    """Wrap an io.StringIO so it can be used as a context manager AND
    expose .getvalue() the way tests expect."""

    def __init__(self, buf):
        self._buf = buf
        self._old = None

    def __enter__(self):
        import sys
        self._old = sys.stderr
        sys.stderr = self._buf
        return self

    def __exit__(self, *exc):
        import sys
        sys.stderr = self._old

    def getvalue(self):
        return self._buf.getvalue()


class PlanScoped(unittest.TestCase):
    def test_resolves_wikilinks_and_paths(self) -> None:
        """Wikilinks via wiki/raw/ search; abs/rel paths via direct resolution."""
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as abs_tmp:
            tmp_path = Path(tmp)
            build_fixture_repo(tmp_path)
            (tmp_path / "wiki" / "concepts").mkdir(parents=True)
            (tmp_path / "scripts").mkdir()
            (tmp_path / "tests").mkdir()
            (tmp_path / "docs").mkdir()
            (tmp_path / "scripts" / "foo.py").write_text("def foo():\n    pass\n")
            (tmp_path / "tests" / "bar.test.sh").write_text("echo bar\n")
            (tmp_path / "wiki" / "concepts" / "concept_x.md").write_text("# concept x\n")
            (tmp_path / "README.md").write_text("# top-level readme\n")
            (tmp_path / "docs" / "notes.md").write_text("# nested doc\n")

            abs_fixture = Path(abs_tmp) / "abs_target.py"
            abs_fixture.write_text('def abs_func():\n    return "abs"\n')

            (tmp_path / "plan.md").write_text(
                f"""# Plan: example

References:
- [[concept_x]]
- scripts/foo.py
- tests/bar.test.sh
- README.md
- docs/notes.md
- {abs_fixture}
"""
            )

            ctx, _ = gather_plan_context("plan.md", repo_root=tmp_path)

            self.assertIn("plan.md", ctx)
            self.assertIn("wiki/concepts/concept_x.md", ctx)
            self.assertIn("scripts/foo.py", ctx)
            self.assertIn("tests/bar.test.sh", ctx)
            self.assertIn("README.md", ctx)
            self.assertIn("docs/notes.md", ctx)
            self.assertIn(str(abs_fixture), ctx)
            self.assertIn("    1  def foo", ctx)
            self.assertIn("    1  def abs_func", ctx)

    def test_warns_on_unresolvable_wikilink(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            build_fixture_repo(tmp_path)
            (tmp_path / "plan.md").write_text(
                "# Plan: example\nReferences:\n- [[does-not-exist]]\n"
            )
            with FileList._capture_stderr() as err:
                ctx, _ = gather_plan_context("plan.md", repo_root=tmp_path)
            self.assertIn("plan.md", ctx)
            self.assertIn(
                "unresolvable wikilink: [[does-not-exist]]", err.getvalue()
            )
            self.assertNotIn("#### does-not-exist", ctx)
            self.assertNotIn("### Referenced files", ctx)

    def test_marks_missing_path_refs(self) -> None:
        """Spec 'mark, don't drop' rule: path-refs that resolve to missing
        files must appear in output with _(file missing)_ marker, never
        silently dropped."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            build_fixture_repo(tmp_path)
            (tmp_path / "scripts").mkdir()
            (tmp_path / "scripts" / "real.py").write_text("def real():\n    pass\n")
            (tmp_path / "plan.md").write_text(
                """# Plan: example
References:
- scripts/real.py
- scripts/missing.py
- /absolute/that/does/not/exist.py
"""
            )
            ctx, _ = gather_plan_context("plan.md", repo_root=tmp_path)
            self.assertIn("scripts/real.py", ctx)
            self.assertIn("    1  def real", ctx)
            self.assertIn(
                "scripts/missing.py", ctx, "missing path-ref must be marked"
            )
            self.assertIn("_(file missing)_", ctx)
            self.assertIn("/absolute/that/does/not/exist.py", ctx)

    def test_ignores_prose_with_slashes_no_extension(self) -> None:
        """MiniMax Checkpoint 2 HIGH-1 fix: PATH_REF_RE used to match
        slash-bearing prose like 'gather/run_git'. Now requires extension
        on relative paths."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            build_fixture_repo(tmp_path)
            (tmp_path / "scripts").mkdir()
            (tmp_path / "scripts" / "real.py").write_text("def real():\n    pass\n")
            (tmp_path / "plan.md").write_text(
                """# Plan: example
The gatherer in `gather/run_git` does the heavy lifting.
We support abs/rel paths via Path resolution.
The 'unreadable/deleted' state is marked, not dropped.
Real reference: scripts/real.py
"""
            )
            ctx, _ = gather_plan_context("plan.md", repo_root=tmp_path)
            self.assertIn("scripts/real.py", ctx)
            self.assertNotIn("#### gather/run_git", ctx)
            self.assertNotIn("#### abs/rel", ctx)
            self.assertNotIn("#### unreadable/deleted", ctx)


class CLIDispatch(unittest.TestCase):
    """Tests 10-15: CLI argparse + dispatcher behavior. Each test runs
    the script via subprocess and asserts on exit code + stderr message,
    stopping short of the actual MiniMax network call."""

    SCRIPT = REPO_ROOT / "scripts" / "lib" / "minimax_review.py"

    def _run(self, args: list[str], cwd: Path | None = None) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, str(self.SCRIPT), *args, "--no-archive"],
            capture_output=True,
            text=True,
            cwd=cwd,
        )

    def test_empty_files_list_exits_1(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            build_fixture_repo(tmp_path)
            res = self._run(
                ["--scope", "files", "--files", ",, ,"], cwd=tmp_path
            )
            self.assertEqual(res.returncode, 1, f"stderr: {res.stderr}")
            self.assertIn("parsed to empty list", res.stderr)
            res2 = self._run(
                ["--scope", "diff", "--files", ",, ,"], cwd=tmp_path
            )
            self.assertEqual(res2.returncode, 1)

    def test_scope_plan_requires_plan(self) -> None:
        res = self._run(["--scope", "plan"])
        self.assertEqual(res.returncode, 1)
        self.assertIn("requires --plan", res.stderr)

    def test_scope_diff_requires_files(self) -> None:
        res = self._run(["--scope", "diff"])
        self.assertEqual(res.returncode, 1)
        self.assertIn("requires --files", res.stderr)

    def test_scope_files_requires_files(self) -> None:
        res = self._run(["--scope", "files"])
        self.assertEqual(res.returncode, 1)
        self.assertIn("requires --files", res.stderr)

    def test_argparse_rejects_invalid_scope(self) -> None:
        res = self._run(["--scope", "bogus"])
        self.assertNotEqual(res.returncode, 0)

    def test_default_scope_is_working_tree(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            build_fixture_repo(tmp_path)
            (tmp_path / "extra.py").unlink()  # eliminate untracked
            res = self._run([], cwd=tmp_path)
            self.assertEqual(res.returncode, 0)
            self.assertIn("no working-tree changes", res.stderr)
            self.assertIn("gathering working-tree context", res.stderr)


if __name__ == "__main__":
    unittest.main()
