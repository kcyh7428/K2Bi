"""Commit-msg hook tests -- spec §8.1 matrix.

The commit-msg hook enforces:
  - Status transitions in wiki/strategies/strategy_*.md are in the
    allowed matrix (§2.2): (new file)->proposed, proposed->approved,
    proposed->rejected, approved->retired. Anything else fails.
  - Every status transition commit includes the three required
    trailers: `Strategy-Transition:`, `<Action>-Strategy:`, and
    `Co-Shipped-By: invest-ship`.
  - Body-only edits (status unchanged) require no trailers.
  - Pre-existing feature_*.md status-edit guard still fires on commits
    that touch wiki/concepts/feature_*.md.
  - Override env `K2BI_ALLOW_STRATEGY_STATUS_EDIT=1` bypasses the
    strategy-specific checks with a warning.
"""

from __future__ import annotations

import unittest
from pathlib import Path

from tests._hook_harness import (
    hook_repo,
    run_git,
    seed_initial_commit,
    write_file,
    write_strategy,
)


def _with_overrides(env: dict, **overrides) -> dict:
    out = dict(env)
    for k, v in overrides.items():
        out[k] = v
    return out


def _approve_message(slug: str = "foo") -> str:
    return (
        f"feat(strategy): approve {slug}\n"
        "\n"
        "Strategy-Transition: proposed -> approved\n"
        f"Approved-Strategy: strategy_{slug}\n"
        "Co-Shipped-By: invest-ship\n"
    )


def _reject_message(slug: str = "foo") -> str:
    return (
        f"feat(strategy): reject {slug}\n"
        "\n"
        "Strategy-Transition: proposed -> rejected\n"
        f"Rejected-Strategy: strategy_{slug}\n"
        "Co-Shipped-By: invest-ship\n"
    )


def _retire_message(slug: str = "foo") -> str:
    return (
        f"feat(strategy): retire {slug}\n"
        "\n"
        "Strategy-Transition: approved -> retired\n"
        f"Retired-Strategy: strategy_{slug}\n"
        "Co-Shipped-By: invest-ship\n"
    )


def _seed_proposed(repo: Path, env: dict, slug: str = "foo") -> str:
    write_strategy(repo, slug, status="proposed")
    run_git(repo, "add", "-A", env=env, check=True)
    run_git(repo, "commit", "-m", f"draft: {slug}", env=env, check=True)
    return f"wiki/strategies/strategy_{slug}.md"


def _seed_approved(repo: Path, env: dict, slug: str = "foo") -> str:
    path = _seed_proposed(repo, env, slug)
    write_strategy(
        repo,
        slug,
        status="approved",
        approved_at="2026-04-19T10:00:00Z",
        approved_commit_sha="abc1234",
    )
    run_git(repo, "add", "-A", env=env, check=True)
    run_git(repo, "commit", "-m", _approve_message(slug), env=env, check=True)
    return path


def _seed_rejected(repo: Path, env: dict, slug: str = "foo") -> str:
    path = _seed_proposed(repo, env, slug)
    write_strategy(
        repo,
        slug,
        status="rejected",
        rejected_at="2026-04-19T10:00:00Z",
        rejected_reason="not safe",
    )
    run_git(repo, "add", "-A", env=env, check=True)
    run_git(repo, "commit", "-m", _reject_message(slug), env=env, check=True)
    return path


def _seed_retired(repo: Path, env: dict, slug: str = "foo") -> str:
    path = _seed_approved(repo, env, slug)
    write_strategy(
        repo,
        slug,
        status="retired",
        approved_at="2026-04-19T10:00:00Z",
        approved_commit_sha="abc1234",
        retired_at="2026-04-19T12:00:00Z",
        retired_reason="obsolete",
    )
    run_git(repo, "add", "-A", env=env, check=True)
    run_git(repo, "commit", "-m", _retire_message(slug), env=env, check=True)
    return path


# ---------- Happy-path transitions (rows 1-3, 4, 13) ----------


class HappyTransitions(unittest.TestCase):
    def test_proposed_to_approved_passes(self):
        with hook_repo() as (repo, env):
            seed_initial_commit(repo, env)
            _seed_proposed(repo, env)
            write_strategy(
                repo,
                "foo",
                status="approved",
                approved_at="2026-04-19T10:00:00Z",
                approved_commit_sha="abc1234",
            )
            run_git(repo, "add", "-A", env=env, check=True)
            result = run_git(
                repo, "commit", "-m", _approve_message(), env=env
            )
            self.assertEqual(result.returncode, 0, result.stderr)

    def test_proposed_to_rejected_passes(self):
        with hook_repo() as (repo, env):
            seed_initial_commit(repo, env)
            _seed_proposed(repo, env)
            write_strategy(
                repo,
                "foo",
                status="rejected",
                rejected_at="2026-04-19T10:00:00Z",
                rejected_reason="not safe",
            )
            run_git(repo, "add", "-A", env=env, check=True)
            result = run_git(
                repo, "commit", "-m", _reject_message(), env=env
            )
            self.assertEqual(result.returncode, 0, result.stderr)

    def test_approved_to_retired_passes(self):
        with hook_repo() as (repo, env):
            seed_initial_commit(repo, env)
            _seed_approved(repo, env)
            write_strategy(
                repo,
                "foo",
                status="retired",
                approved_at="2026-04-19T10:00:00Z",
                approved_commit_sha="abc1234",
                retired_at="2026-04-19T12:00:00Z",
                retired_reason="obsolete",
            )
            run_git(repo, "add", "-A", env=env, check=True)
            result = run_git(
                repo, "commit", "-m", _retire_message(), env=env
            )
            self.assertEqual(result.returncode, 0, result.stderr)

    def test_body_only_edit_on_proposed_needs_no_trailers(self):
        with hook_repo() as (repo, env):
            seed_initial_commit(repo, env)
            _seed_proposed(repo, env)
            # Edit body only; status still proposed.
            write_strategy(
                repo,
                "foo",
                status="proposed",
                how_this_works="Drafts evolve without ceremony.",
            )
            run_git(repo, "add", "-A", env=env, check=True)
            result = run_git(
                repo, "commit", "-m", "draft: refine foo body", env=env
            )
            self.assertEqual(result.returncode, 0, result.stderr)

    def test_new_file_at_proposed_needs_no_trailers(self):
        # (new file) -> proposed is an allowed transition per §2.2.
        # No trailer required since this is the draft-creation step.
        with hook_repo() as (repo, env):
            seed_initial_commit(repo, env)
            write_strategy(repo, "foo", status="proposed")
            run_git(repo, "add", "-A", env=env, check=True)
            result = run_git(
                repo, "commit", "-m", "draft: propose foo", env=env
            )
            self.assertEqual(result.returncode, 0, result.stderr)


# ---------- Missing trailer cases (rows 5-7) ----------


class MissingTrailers(unittest.TestCase):
    def _stage_approve(self, repo: Path, env: dict):
        write_strategy(
            repo,
            "foo",
            status="approved",
            approved_at="2026-04-19T10:00:00Z",
            approved_commit_sha="abc1234",
        )
        run_git(repo, "add", "-A", env=env, check=True)

    def test_missing_strategy_transition_trailer_fails(self):
        with hook_repo() as (repo, env):
            seed_initial_commit(repo, env)
            _seed_proposed(repo, env)
            self._stage_approve(repo, env)
            msg = (
                "feat(strategy): approve foo\n"
                "\n"
                "Approved-Strategy: strategy_foo\n"
                "Co-Shipped-By: invest-ship\n"
            )
            result = run_git(repo, "commit", "-m", msg, env=env)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("Strategy-Transition", result.stderr)

    def test_missing_co_shipped_by_trailer_fails(self):
        with hook_repo() as (repo, env):
            seed_initial_commit(repo, env)
            _seed_proposed(repo, env)
            self._stage_approve(repo, env)
            msg = (
                "feat(strategy): approve foo\n"
                "\n"
                "Strategy-Transition: proposed -> approved\n"
                "Approved-Strategy: strategy_foo\n"
            )
            result = run_git(repo, "commit", "-m", msg, env=env)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("Co-Shipped-By", result.stderr)

    def test_missing_approved_strategy_trailer_fails(self):
        with hook_repo() as (repo, env):
            seed_initial_commit(repo, env)
            _seed_proposed(repo, env)
            self._stage_approve(repo, env)
            msg = (
                "feat(strategy): approve foo\n"
                "\n"
                "Strategy-Transition: proposed -> approved\n"
                "Co-Shipped-By: invest-ship\n"
            )
            result = run_git(repo, "commit", "-m", msg, env=env)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("Approved-Strategy", result.stderr)

    def test_missing_retired_strategy_trailer_fails(self):
        with hook_repo() as (repo, env):
            seed_initial_commit(repo, env)
            _seed_approved(repo, env)
            write_strategy(
                repo,
                "foo",
                status="retired",
                approved_at="2026-04-19T10:00:00Z",
                approved_commit_sha="abc1234",
                retired_at="2026-04-19T12:00:00Z",
                retired_reason="obsolete",
            )
            run_git(repo, "add", "-A", env=env, check=True)
            msg = (
                "feat(strategy): retire foo\n"
                "\n"
                "Strategy-Transition: approved -> retired\n"
                "Co-Shipped-By: invest-ship\n"
            )
            result = run_git(repo, "commit", "-m", msg, env=env)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("Retired-Strategy", result.stderr)

    def test_missing_rejected_strategy_trailer_fails(self):
        with hook_repo() as (repo, env):
            seed_initial_commit(repo, env)
            _seed_proposed(repo, env)
            write_strategy(
                repo,
                "foo",
                status="rejected",
                rejected_at="2026-04-19T10:00:00Z",
                rejected_reason="not safe",
            )
            run_git(repo, "add", "-A", env=env, check=True)
            msg = (
                "feat(strategy): reject foo\n"
                "\n"
                "Strategy-Transition: proposed -> rejected\n"
                "Co-Shipped-By: invest-ship\n"
            )
            result = run_git(repo, "commit", "-m", msg, env=env)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("Rejected-Strategy", result.stderr)

    def test_mismatched_strategy_transition_values_rejected(self):
        # Trailer claims proposed->rejected but file actually goes
        # proposed->approved. Hook should catch the lie.
        with hook_repo() as (repo, env):
            seed_initial_commit(repo, env)
            _seed_proposed(repo, env)
            self._stage_approve(repo, env)
            msg = (
                "feat(strategy): approve foo\n"
                "\n"
                "Strategy-Transition: proposed -> rejected\n"  # wrong!
                "Approved-Strategy: strategy_foo\n"
                "Co-Shipped-By: invest-ship\n"
            )
            result = run_git(repo, "commit", "-m", msg, env=env)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("Strategy-Transition", result.stderr)


# ---------- Forbidden transitions (rows 8-10) ----------


class ForbiddenTransitions(unittest.TestCase):
    def test_approved_to_proposed_rejected(self):
        # Defense in depth: pre-commit Check D catches this on the
        # content-immutability invariant before commit-msg runs. Either
        # rejection message is acceptable; what matters is the commit
        # never lands.
        with hook_repo() as (repo, env):
            seed_initial_commit(repo, env)
            _seed_approved(repo, env)
            write_strategy(repo, "foo", status="proposed")
            run_git(repo, "add", "-A", env=env, check=True)
            msg = (
                "feat(strategy): unapprove foo\n"
                "\n"
                "Strategy-Transition: approved -> proposed\n"
                "Approved-Strategy: strategy_foo\n"
                "Co-Shipped-By: invest-ship\n"
            )
            result = run_git(repo, "commit", "-m", msg, env=env)
            self.assertNotEqual(result.returncode, 0)
            stderr_lower = result.stderr.lower()
            # Either Check D (pre-commit) or strategy-transition (commit-msg)
            # must surface the rejection.
            self.assertTrue(
                "content-immutable" in stderr_lower
                or "forbidden" in stderr_lower,
                f"expected rejection message, got: {result.stderr}",
            )

    def test_rejected_to_approved_rejected(self):
        with hook_repo() as (repo, env):
            seed_initial_commit(repo, env)
            _seed_rejected(repo, env)
            write_strategy(
                repo,
                "foo",
                status="approved",
                approved_at="2026-04-19T10:00:00Z",
                approved_commit_sha="abc1234",
            )
            run_git(repo, "add", "-A", env=env, check=True)
            msg = (
                "feat(strategy): resurrect foo\n"
                "\n"
                "Strategy-Transition: rejected -> approved\n"
                "Approved-Strategy: strategy_foo\n"
                "Co-Shipped-By: invest-ship\n"
            )
            result = run_git(repo, "commit", "-m", msg, env=env)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("forbidden", result.stderr.lower())

    def test_retired_to_approved_rejected(self):
        with hook_repo() as (repo, env):
            seed_initial_commit(repo, env)
            _seed_retired(repo, env)
            write_strategy(
                repo,
                "foo",
                status="approved",
                approved_at="2026-04-19T10:00:00Z",
                approved_commit_sha="abc1234",
                retired_at="2026-04-19T12:00:00Z",
                retired_reason="obsolete",
            )
            run_git(repo, "add", "-A", env=env, check=True)
            msg = (
                "feat(strategy): un-retire foo\n"
                "\n"
                "Strategy-Transition: retired -> approved\n"
                "Approved-Strategy: strategy_foo\n"
                "Co-Shipped-By: invest-ship\n"
            )
            result = run_git(repo, "commit", "-m", msg, env=env)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("forbidden", result.stderr.lower())

    def test_new_file_at_approved_rejected(self):
        # Spec §8.6 negative test: creating a strategy already at
        # status: approved without a proposed predecessor.
        with hook_repo() as (repo, env):
            seed_initial_commit(repo, env)
            write_strategy(
                repo,
                "foo",
                status="approved",
                approved_at="2026-04-19T10:00:00Z",
                approved_commit_sha="abc1234",
            )
            run_git(repo, "add", "-A", env=env, check=True)
            msg = (
                "feat(strategy): ship foo\n"
                "\n"
                "Strategy-Transition: (new file) -> approved\n"
                "Approved-Strategy: strategy_foo\n"
                "Co-Shipped-By: invest-ship\n"
            )
            result = run_git(repo, "commit", "-m", msg, env=env)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("forbidden", result.stderr.lower())


# ---------- Override env (row 11) ----------


class OverrideEnv(unittest.TestCase):
    def test_override_bypasses_trailer_requirement(self):
        with hook_repo() as (repo, env):
            seed_initial_commit(repo, env)
            _seed_proposed(repo, env)
            write_strategy(
                repo,
                "foo",
                status="approved",
                approved_at="2026-04-19T10:00:00Z",
                approved_commit_sha="abc1234",
            )
            run_git(repo, "add", "-A", env=env, check=True)
            override_env = _with_overrides(env, K2BI_ALLOW_STRATEGY_STATUS_EDIT="1")
            # No trailers at all in the commit message.
            result = run_git(
                repo,
                "commit",
                "-m",
                "hook-repair: manual status edit",
                env=override_env,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            # Warning must surface the override use.
            self.assertIn("K2BI_ALLOW_STRATEGY_STATUS_EDIT", result.stderr)

    def test_override_writes_auditable_wiki_log_entry(self):
        # MiniMax R1 F2: overrides must leave an auditable record in
        # wiki/log.md via the single-writer helper. Best-effort logger,
        # so a pre-existing log file + K2BI_WIKI_LOG override route is
        # used to prove the helper was actually invoked.
        with hook_repo() as (repo, env):
            seed_initial_commit(repo, env)
            _seed_proposed(repo, env)
            write_strategy(
                repo,
                "foo",
                status="approved",
                approved_at="2026-04-19T10:00:00Z",
                approved_commit_sha="abc1234",
            )
            run_git(repo, "add", "-A", env=env, check=True)

            log_file = Path(env["K2BI_RETIRED_DIR"]).parent / "scratch_log.md"
            log_file.parent.mkdir(parents=True, exist_ok=True)
            log_file.write_text("", encoding="utf-8")
            override_env = _with_overrides(
                env,
                K2BI_ALLOW_STRATEGY_STATUS_EDIT="1",
                K2BI_WIKI_LOG=str(log_file),
                K2BI_WIKI_LOG_LOCK=str(log_file) + ".lock",
            )
            result = run_git(
                repo,
                "commit",
                "-m",
                "hook-repair: manual status edit",
                env=override_env,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            log_contents = log_file.read_text(encoding="utf-8")
            self.assertIn("hook-override", log_contents)
            self.assertIn("K2BI_ALLOW_STRATEGY_STATUS_EDIT", log_contents)


# ---------- Legacy feature_*.md hook still works ----------


class FeatureStatusHookStillFires(unittest.TestCase):
    def _seed_feature(self, repo: Path, env: dict) -> None:
        """Create feature_test.md under the existing status-edit guard.
        The file creation itself has a `+status: active` diff line, so
        we override for the seed; the test commit then exercises the
        guard under full enforcement."""
        write_file(
            repo,
            "wiki/concepts/feature_test.md",
            "---\nstatus: active\n---\nbody\n",
        )
        run_git(repo, "add", "-A", env=env, check=True)
        seed_env = _with_overrides(env, K2BI_ALLOW_STATUS_EDIT="1")
        run_git(
            repo,
            "commit",
            "-m",
            "feat: add feature_test",
            env=seed_env,
            check=True,
        )

    def test_feature_status_edit_needs_co_shipped_by(self):
        # The existing feature_*.md check (Bundle 1 port) must continue
        # to fire even though Bundle 3 added new checks around it.
        with hook_repo() as (repo, env):
            seed_initial_commit(repo, env)
            self._seed_feature(repo, env)
            # Change status; no Co-Shipped-By trailer.
            write_file(
                repo,
                "wiki/concepts/feature_test.md",
                "---\nstatus: shipped\n---\nbody\n",
            )
            run_git(repo, "add", "-A", env=env, check=True)
            result = run_git(
                repo, "commit", "-m", "feat: ship feature_test", env=env
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("status", result.stderr)

    def test_feature_status_edit_with_k2b_ship_trailer_accepted(self):
        with hook_repo() as (repo, env):
            seed_initial_commit(repo, env)
            self._seed_feature(repo, env)
            write_file(
                repo,
                "wiki/concepts/feature_test.md",
                "---\nstatus: shipped\n---\nbody\n",
            )
            run_git(repo, "add", "-A", env=env, check=True)
            msg = "feat: ship feature_test\n\nCo-Shipped-By: invest-ship\n"
            result = run_git(repo, "commit", "-m", msg, env=env)
            self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
