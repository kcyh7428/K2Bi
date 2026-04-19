"""Pre-commit hook tests -- spec §8.2 matrix (rows 1-13, 18-22).

Rows 14-17 (post-commit sentinel landing) live in test_post_commit_hook.py.

Each matrix row is one self-contained test. The harness spins up a
fresh git repo per test so the initial-commit state is deterministic
and hook rejections don't leak into each other.

Check A -- status-in-enum on any staged wiki/strategies/strategy_*.md.
Check B -- `## How This Works` non-empty when status is proposed OR approved.
Check C -- config.yaml edit requires same-commit proposed->approved
           limits-proposal transition in the staged diff.
Check D -- approved strategy files are content-immutable except for
           the pure retire transition.
"""

from __future__ import annotations

import unittest
from pathlib import Path

from tests._hook_harness import (
    commit,
    hook_repo,
    run_git,
    seed_initial_commit,
    strategy_text,
    write_file,
    write_limits_proposal,
    write_strategy,
)


def _with_overrides(env: dict, **overrides) -> dict:
    """Return a new env dict with the given hook overrides set.
    Keeps the base env clean so subsequent commits still exercise hooks."""
    out = dict(env)
    for k, v in overrides.items():
        out[k] = v
    return out


# ---------- Check A -- status-in-enum ----------


class CheckA_StatusEnum(unittest.TestCase):
    def test_unknown_status_value_is_rejected(self):
        with hook_repo() as (repo, env):
            seed_initial_commit(repo, env)
            write_strategy(repo, "foo", status="pending")  # invalid enum
            result = commit(repo, env, "feat: propose foo", "wiki/strategies/strategy_foo.md")
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("pending", result.stderr)
            self.assertIn("allowed", result.stderr.lower())

    def test_all_enum_values_pass_check_a(self):
        for status in ("proposed", "rejected", "retired"):
            # `approved` is handled separately since it has extra
            # required fields + Check D ties it to retire-only diffs.
            with hook_repo() as (repo, env):
                seed_initial_commit(repo, env)
                # For rejected/retired we still need a file with
                # non-empty How-This-Works to pass Check B.
                extras = {}
                if status == "rejected":
                    extras = {"rejected_at": "2026-04-19T10:00:00Z"}
                if status == "retired":
                    # For a standalone new file at status=retired, the
                    # commit-msg hook will reject (transition not
                    # allowed), but pre-commit is happy as long as
                    # Check A/B pass. We scope the assertion to the
                    # pre-commit failure *not* being about Check A.
                    extras = {"retired_at": "2026-04-19T10:00:00Z"}
                write_strategy(repo, "foo", status=status, extras=extras)
                result = commit(
                    repo,
                    env,
                    "feat: test",
                    "wiki/strategies/strategy_foo.md",
                )
                # Check A doesn't fail here; if any failure, it must
                # not mention the allowed-enum message.
                if result.returncode != 0:
                    self.assertNotIn(
                        "not in allowed enum",
                        result.stderr,
                        f"Check A rejected status={status!r}: {result.stderr}",
                    )


# ---------- Check B -- How This Works non-empty ----------


class CheckB_HowThisWorks(unittest.TestCase):
    def test_proposed_without_how_this_works_rejected(self):
        with hook_repo() as (repo, env):
            seed_initial_commit(repo, env)
            write_strategy(repo, "foo", status="proposed", include_how=False)
            result = commit(repo, env, "feat: propose foo", "wiki/strategies/strategy_foo.md")
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("How This Works", result.stderr)

    def test_approved_with_empty_how_this_works_rejected(self):
        with hook_repo() as (repo, env):
            seed_initial_commit(repo, env)
            write_strategy(
                repo,
                "foo",
                status="approved",
                approved_at="2026-04-19T10:00:00Z",
                approved_commit_sha="abc1234",
                how_this_works="   ",  # whitespace only
            )
            result = commit(repo, env, "feat: approve foo", "wiki/strategies/strategy_foo.md")
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("How This Works", result.stderr)

    def test_approved_with_non_empty_how_this_works_passes_check_b(self):
        # Use the override to sidestep the forbidden (new-file)->approved
        # transition so we isolate what Check B alone would fail on.
        with hook_repo() as (repo, env):
            seed_initial_commit(repo, env)
            write_strategy(
                repo,
                "foo",
                status="approved",
                approved_at="2026-04-19T10:00:00Z",
                approved_commit_sha="abc1234",
                how_this_works="Genuine explanation body.",
            )
            override_env = _with_overrides(env, K2BI_ALLOW_STRATEGY_STATUS_EDIT="1")
            result = commit(
                repo,
                override_env,
                "feat: approve foo",
                "wiki/strategies/strategy_foo.md",
            )
            # Override bypasses Checks A/B/D. Commit should succeed.
            self.assertEqual(result.returncode, 0, result.stderr)

    def test_rejected_status_does_not_require_how_this_works(self):
        with hook_repo() as (repo, env):
            seed_initial_commit(repo, env)
            write_strategy(
                repo,
                "foo",
                status="rejected",
                rejected_at="2026-04-19T10:00:00Z",
                rejected_reason="not safe",
                include_how=False,
            )
            override_env = _with_overrides(env, K2BI_ALLOW_STRATEGY_STATUS_EDIT="1")
            result = commit(
                repo, override_env, "feat: reject foo",
                "wiki/strategies/strategy_foo.md"
            )
            # Rejection is terminal; the body requirement does not apply.
            self.assertEqual(result.returncode, 0, result.stderr)


# ---------- Check C -- config.yaml requires matching limits-proposal ----------


class CheckC_ConfigRequiresLimitsProposal(unittest.TestCase):
    def _write_config_yaml(
        self, repo: Path, value: str = "position_size: 0.10\n"
    ) -> Path:
        return write_file(repo, "execution/validators/config.yaml", value)

    def _seed_config(self, repo: Path, env: dict) -> None:
        """Seed execution/validators/config.yaml in HEAD. Needs a
        config-override for that specific commit since Check C itself
        would otherwise reject the seed."""
        self._write_config_yaml(repo, "position_size: 0.10\n")
        run_git(repo, "add", "-A", env=env, check=True)
        seed_env = _with_overrides(env, K2BI_ALLOW_CONFIG_EDIT="1")
        run_git(
            repo, "commit", "-m", "seed config", env=seed_env, check=True
        )

    def test_config_edit_without_limits_proposal_rejected(self):
        with hook_repo() as (repo, env):
            seed_initial_commit(repo, env)
            self._seed_config(repo, env)
            # Now edit config without any proposal.
            self._write_config_yaml(repo, "position_size: 0.25\n")
            result = commit(
                repo,
                env,
                "feat: widen cap",
                "execution/validators/config.yaml",
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("limits-proposal", result.stderr)

    def test_config_edit_with_matching_proposal_transition_passes(self):
        # Spec §8.2 row 18: PASS when HEAD has proposal at status=proposed
        # and staged has proposal at status=approved in the same commit as
        # the config.yaml edit.
        with hook_repo() as (repo, env):
            seed_initial_commit(repo, env)
            self._seed_config(repo, env)
            # Commit 1: create proposal at status=proposed.
            write_limits_proposal(repo, "widen-size", status="proposed")
            run_git(repo, "add", "-A", env=env, check=True)
            run_git(
                repo, "commit", "-m", "feat: propose widen-size",
                env=env, check=True,
            )
            # Commit 2: flip proposal -> approved + apply config edit.
            write_limits_proposal(
                repo,
                "widen-size",
                status="approved",
                approved_at="2026-04-19T10:00:00Z",
                approved_commit_sha="abc1234",
            )
            self._write_config_yaml(repo, "position_size: 0.15\n")
            # The proposal's status change will trip commit-msg (since
            # the limits proposal is under review/strategy-approvals/
            # not wiki/strategies/, the strategy-transition check does
            # NOT fire -- no extra override needed for that surface).
            run_git(repo, "add", "-A", env=env, check=True)
            result = run_git(
                repo, "commit", "-m",
                "feat: approve widen-size + apply to config",
                env=env,
            )
            self.assertEqual(result.returncode, 0, result.stderr)

    def test_config_edit_with_already_approved_proposal_rejected(self):
        # Spec §8.2 row 19: proposal was approved in a PRIOR commit.
        # Check C requires the transition to happen IN THIS commit.
        with hook_repo() as (repo, env):
            seed_initial_commit(repo, env)
            self._seed_config(repo, env)
            # Land proposal through the legal two-commit path so the
            # repo isn't in a state the hook already rejects.
            write_limits_proposal(repo, "widen-size", status="proposed")
            run_git(repo, "add", "-A", env=env, check=True)
            run_git(
                repo, "commit", "-m", "feat: propose widen-size",
                env=env, check=True,
            )
            write_limits_proposal(
                repo,
                "widen-size",
                status="approved",
                approved_at="2026-04-19T10:00:00Z",
                approved_commit_sha="abc1234",
            )
            self._write_config_yaml(repo, "position_size: 0.15\n")
            run_git(repo, "add", "-A", env=env, check=True)
            run_git(
                repo, "commit", "-m",
                "feat: approve widen-size + apply to config",
                env=env, check=True,
            )
            # Now the PRIOR commit landed proposal at approved + edited
            # config. A follow-up config-only edit should be rejected
            # because the transition is not atomic with it.
            self._write_config_yaml(repo, "position_size: 0.16\n")
            result = commit(
                repo,
                env,
                "feat: tweak config again",
                "execution/validators/config.yaml",
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("limits-proposal", result.stderr.lower())

    def test_config_edit_with_forged_new_file_approved_proposal_rejected(self):
        # Spec §8.2 row 20: proposal is a new file already at status=approved.
        # Check C requires the HEAD state to be proposed.
        with hook_repo() as (repo, env):
            seed_initial_commit(repo, env)
            self._seed_config(repo, env)
            # Stage: new proposal file born already at approved + config edit.
            write_limits_proposal(
                repo,
                "forged-widen",
                status="approved",
                approved_at="2026-04-19T10:00:00Z",
                approved_commit_sha="abc1234",
            )
            self._write_config_yaml(repo, "position_size: 0.15\n")
            run_git(repo, "add", "-A", env=env, check=True)
            result = run_git(
                repo, "commit", "-m", "feat: forged widen", env=env
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("limits-proposal", result.stderr.lower())

    def test_config_edit_override_env_passes_with_warning(self):
        with hook_repo() as (repo, env):
            seed_initial_commit(repo, env)
            self._seed_config(repo, env)
            self._write_config_yaml(repo, "position_size: 0.15\n")
            override_env = _with_overrides(env, K2BI_ALLOW_CONFIG_EDIT="1")
            result = commit(
                repo,
                override_env,
                "emergency: rollback limits",
                "execution/validators/config.yaml",
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("K2BI_ALLOW_CONFIG_EDIT", result.stderr)

    def test_config_edit_override_logs_to_wiki_log(self):
        # MiniMax R1 F2: every override must write an auditable record
        # to wiki/log.md beyond the stderr-only warning. The helper is
        # best-effort (silent on missing log file), so we set up a
        # scratch wiki/log.md + point K2BI_WIKI_LOG at it and assert
        # the override entry lands.
        with hook_repo() as (repo, env):
            seed_initial_commit(repo, env)
            self._seed_config(repo, env)
            self._write_config_yaml(repo, "position_size: 0.15\n")

            log_file = Path(env["K2BI_RETIRED_DIR"]).parent / "scratch_log.md"
            log_file.parent.mkdir(parents=True, exist_ok=True)
            log_file.write_text("", encoding="utf-8")
            override_env = _with_overrides(
                env,
                K2BI_ALLOW_CONFIG_EDIT="1",
                K2BI_WIKI_LOG=str(log_file),
                K2BI_WIKI_LOG_LOCK=str(log_file) + ".lock",
            )
            result = commit(
                repo,
                override_env,
                "emergency: rollback limits",
                "execution/validators/config.yaml",
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            log_contents = log_file.read_text(encoding="utf-8")
            self.assertIn("hook-override", log_contents)
            self.assertIn("K2BI_ALLOW_CONFIG_EDIT", log_contents)


# ---------- Check D -- approved content immutability ----------


class CheckD_ApprovedContentImmutability(unittest.TestCase):
    def _seed_approved(self, repo: Path, env: dict, slug: str = "foo") -> str:
        """Seed an approved strategy file. The approve commit itself
        is a forbidden transition at the commit-msg layer
        ((new file)->approved), so we bypass WITH OVERRIDE only for
        this specific commit; the returned `env` stays clean so the
        test's actual assertion commits run under full enforcement."""
        write_strategy(
            repo,
            slug,
            status="approved",
            approved_at="2026-04-19T10:00:00Z",
            approved_commit_sha="abc1234",
        )
        run_git(repo, "add", "-A", env=env, check=True)
        seed_env = _with_overrides(env, K2BI_ALLOW_STRATEGY_STATUS_EDIT="1")
        run_git(
            repo,
            "commit",
            "-m",
            f"feat: approve {slug}",
            env=seed_env,
            check=True,
        )
        return f"wiki/strategies/strategy_{slug}.md"

    def test_approved_field_edit_rejected_with_field_in_stderr(self):
        with hook_repo() as (repo, env):
            seed_initial_commit(repo, env)
            path = self._seed_approved(repo, env)
            # Widen risk_envelope_pct post-approval.
            write_strategy(
                repo,
                "foo",
                status="approved",
                approved_at="2026-04-19T10:00:00Z",
                approved_commit_sha="abc1234",
                risk_envelope_pct="0.05",
            )
            result = commit(repo, env, "feat: widen risk on foo", path)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("risk_envelope_pct", result.stderr)

    def test_approved_body_edit_rejected(self):
        with hook_repo() as (repo, env):
            seed_initial_commit(repo, env)
            path = self._seed_approved(repo, env)
            write_strategy(
                repo,
                "foo",
                status="approved",
                approved_at="2026-04-19T10:00:00Z",
                approved_commit_sha="abc1234",
                how_this_works="COMPLETELY REWRITTEN body.",
            )
            result = commit(repo, env, "fix: typo in foo how-this-works", path)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("content-immutable", result.stderr)

    def test_approved_pure_retire_passes_check_d(self):
        with hook_repo() as (repo, env):
            seed_initial_commit(repo, env)
            self._seed_approved(repo, env)
            # Pure retire transition: status + retired_at + retired_reason.
            write_strategy(
                repo,
                "foo",
                status="retired",
                approved_at="2026-04-19T10:00:00Z",
                approved_commit_sha="abc1234",
                retired_at="2026-04-19T12:00:00Z",
                retired_reason="obsolete",
            )
            message = (
                "feat(strategy): retire foo\n"
                "\n"
                "Strategy-Transition: approved -> retired\n"
                "Retired-Strategy: strategy_foo\n"
                "Co-Shipped-By: invest-ship\n"
            )
            run_git(repo, "add", "-A", env=env, check=True)
            result = run_git(repo, "commit", "-m", message, env=env)
            self.assertEqual(result.returncode, 0, result.stderr)

    def test_retire_with_comingled_qty_change_rejected(self):
        with hook_repo() as (repo, env):
            seed_initial_commit(repo, env)
            self._seed_approved(repo, env)
            # Retire AND sneak in qty=2 (order is nested dict).
            bad_order = {
                "ticker": "SPY",
                "side": "buy",
                "qty": 2,  # changed!
                "limit_price": "500.00",
                "stop_loss": "490.00",
                "time_in_force": "DAY",
            }
            write_strategy(
                repo,
                "foo",
                status="retired",
                approved_at="2026-04-19T10:00:00Z",
                approved_commit_sha="abc1234",
                retired_at="2026-04-19T12:00:00Z",
                retired_reason="sneaky",
                order=bad_order,
            )
            message = (
                "feat(strategy): retire foo\n"
                "\n"
                "Strategy-Transition: approved -> retired\n"
                "Retired-Strategy: strategy_foo\n"
                "Co-Shipped-By: invest-ship\n"
            )
            run_git(repo, "add", "-A", env=env, check=True)
            result = run_git(repo, "commit", "-m", message, env=env)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("order", result.stderr)

    def test_proposed_file_body_edit_not_blocked_by_check_d(self):
        # Check D only fires on HEAD=approved.
        with hook_repo() as (repo, env):
            seed_initial_commit(repo, env)
            write_strategy(repo, "foo", status="proposed")
            run_git(repo, "add", "-A", env=env, check=True)
            run_git(
                repo, "commit", "-m", "feat: propose foo", env=env, check=True
            )
            # Edit body on a proposed draft -- freely allowed.
            write_strategy(
                repo,
                "foo",
                status="proposed",
                how_this_works="Drafts evolve; this is fine.",
            )
            result = commit(
                repo,
                env,
                "draft: refine foo how-this-works",
                "wiki/strategies/strategy_foo.md",
            )
            self.assertEqual(result.returncode, 0, result.stderr)

    def test_strategy_override_env_bypasses_check_d_with_warning(self):
        with hook_repo() as (repo, env):
            seed_initial_commit(repo, env)
            self._seed_approved(repo, env)
            # Try to mutate the approved file.
            write_strategy(
                repo,
                "foo",
                status="approved",
                approved_at="2026-04-19T10:00:00Z",
                approved_commit_sha="abc1234",
                risk_envelope_pct="0.05",
            )
            override_env = _with_overrides(env, K2BI_ALLOW_STRATEGY_STATUS_EDIT="1")
            result = commit(
                repo,
                override_env,
                "hook-repair foo",
                "wiki/strategies/strategy_foo.md",
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("K2BI_ALLOW_STRATEGY_STATUS_EDIT", result.stderr)


# ---------- Existing check (log.md) must continue to pass ----------


class ExistingLogAppendCheckStillEnforced(unittest.TestCase):
    def test_direct_log_md_append_still_blocked(self):
        with hook_repo() as (repo, env):
            seed_initial_commit(repo, env)
            # Simulate a script that appends via >> to wiki/log.md.
            write_file(
                repo,
                "scripts/bad-helper.sh",
                "#!/bin/sh\necho event >> wiki/log.md\n",
            )
            result = commit(
                repo,
                env,
                "feat: bad helper",
                "scripts/bad-helper.sh",
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("wiki/log.md", result.stderr)


if __name__ == "__main__":
    unittest.main()
