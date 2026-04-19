"""Post-commit hook tests -- spec §8.2 rows 14-17 + parity rows.

The post-commit hook watches for `Retired-Strategy: strategy_<slug>`
trailers on just-landed commits and writes the `.retired-<sha16>`
sentinel through `execution.risk.kill_switch.write_retired`, using
`execution.engine.main.derive_retire_slug` on the retired file's
source path so the slug-to-hash derivation matches what the engine
checks on each submit tick.

Key properties under test:
  - Retire commit lands -> sentinel file appears at
    `.retired-<sha16>` with `commit_sha` matching the just-landed SHA.
  - Commit aborted pre-commit (hook rejection) -> sentinel NEVER
    written (orphaned-sentinel race closed; Q10).
  - Non-retire commit (approve / reject / unrelated) -> hook is a
    no-op.
  - `git commit --amend` on a retire commit -> post-commit does not
    re-fire in standard git, AND write_retired is first-writer-wins,
    so the sentinel contents stay stable regardless.
  - Slug derivation matches engine.derive_retire_slug so hook writes
    where the engine reads (shared retired_dir + same hash input).
  - K2BI_SKIP_POST_COMMIT_RETIRE=1 suppresses the sentinel write.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from tests._hook_harness import (
    hook_repo,
    run_git,
    seed_initial_commit,
    write_strategy,
)


def _sentinel_path(retired_dir: Path, slug: str) -> Path:
    digest = hashlib.sha256(slug.encode("utf-8")).hexdigest()[:16]
    return retired_dir / f".retired-{digest}"


def _approve_message(slug: str = "foo") -> str:
    return (
        f"feat(strategy): approve {slug}\n"
        "\n"
        "Strategy-Transition: proposed -> approved\n"
        f"Approved-Strategy: strategy_{slug}\n"
        "Co-Shipped-By: invest-ship\n"
    )


def _retire_message(slug: str = "foo", reason: str = "obsolete") -> str:
    return (
        f"feat(strategy): retire {slug}\n"
        "\n"
        f"Retire reason: {reason}\n"
        "\n"
        "Strategy-Transition: approved -> retired\n"
        f"Retired-Strategy: strategy_{slug}\n"
        "Co-Shipped-By: invest-ship\n"
    )


def _seed_approved(repo: Path, env: dict, slug: str = "foo") -> None:
    write_strategy(repo, slug, status="proposed")
    run_git(repo, "add", "-A", env=env, check=True)
    run_git(repo, "commit", "-m", f"draft: {slug}", env=env, check=True)
    write_strategy(
        repo,
        slug,
        status="approved",
        approved_at="2026-04-19T10:00:00Z",
        approved_commit_sha="abc1234",
    )
    run_git(repo, "add", "-A", env=env, check=True)
    run_git(
        repo, "commit", "-m", _approve_message(slug), env=env, check=True
    )


def _land_retire(repo: Path, env: dict, slug: str = "foo") -> str:
    """Stage + commit a pure retire transition. Returns the commit sha."""
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
    result = run_git(
        repo, "commit", "-m", _retire_message(slug), env=env
    )
    if result.returncode != 0:
        raise AssertionError(
            f"retire commit was rejected: {result.stderr}"
        )
    res = run_git(repo, "rev-parse", "HEAD", env=env, check=True)
    return res.stdout.strip()


class RetireCommitWritesSentinel(unittest.TestCase):
    def test_retire_commit_lands_sentinel_written(self):
        with hook_repo() as (repo, env):
            seed_initial_commit(repo, env)
            _seed_approved(repo, env)
            commit_sha = _land_retire(repo, env)

            retired_dir = Path(env["K2BI_RETIRED_DIR"])
            # Engine strips the `strategy_` prefix from the stem.
            slug = "foo"
            sentinel = _sentinel_path(retired_dir, slug)
            self.assertTrue(
                sentinel.exists(),
                f"sentinel not found at {sentinel}; dir contents: "
                f"{list(retired_dir.iterdir())}",
            )

            record = json.loads(sentinel.read_text())
            self.assertEqual(record["slug"], slug)
            self.assertEqual(record["commit_sha"], commit_sha)
            self.assertEqual(record["source"], "invest-ship --retire-strategy")
            self.assertIn("obsolete", record["reason"])
            self.assertIn("ts", record)

    def test_non_retire_commit_no_sentinel(self):
        with hook_repo() as (repo, env):
            seed_initial_commit(repo, env)
            _seed_approved(repo, env)

            retired_dir = Path(env["K2BI_RETIRED_DIR"])
            # After approve, there should be no sentinel yet.
            sentinels = [p for p in retired_dir.iterdir() if p.name.startswith(".retired-")]
            self.assertEqual(sentinels, [])

    def test_pre_commit_rejection_leaves_no_sentinel(self):
        # Retire commit is deliberately malformed so pre-commit's
        # Check D rejects it. The post-commit hook must NOT fire
        # (git does not run post-commit for rejected commits).
        with hook_repo() as (repo, env):
            seed_initial_commit(repo, env)
            _seed_approved(repo, env)

            # Malformed retire: co-mingled order.qty change. Check D fails.
            bad_order = {
                "ticker": "SPY",
                "side": "buy",
                "qty": 2,
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
                retired_reason="bogus",
                order=bad_order,
            )
            run_git(repo, "add", "-A", env=env, check=True)
            # This commit is designed to fail pre-commit.
            result = run_git(
                repo, "commit", "-m", _retire_message(), env=env
            )
            self.assertNotEqual(
                result.returncode, 0, "retire commit should fail pre-commit"
            )

            retired_dir = Path(env["K2BI_RETIRED_DIR"])
            sentinels = [
                p for p in retired_dir.iterdir() if p.name.startswith(".retired-")
            ]
            self.assertEqual(
                sentinels,
                [],
                f"pre-commit rejection still wrote a sentinel: {sentinels}",
            )

    def test_amend_retire_commit_sentinel_unchanged(self):
        # git's standard commit sequence does not re-fire post-commit
        # on `commit --amend`. And write_retired is first-writer-wins
        # anyway. Both independently keep the sentinel stable.
        with hook_repo() as (repo, env):
            seed_initial_commit(repo, env)
            _seed_approved(repo, env)
            first_sha = _land_retire(repo, env)

            retired_dir = Path(env["K2BI_RETIRED_DIR"])
            sentinel = _sentinel_path(retired_dir, "foo")
            original_record = json.loads(sentinel.read_text())

            # Amend the retire commit (reword the message; no diff change).
            # Git's post-commit WILL fire on `--amend` (commit hook docs
            # list amend as a triggering event), but the sentinel is
            # already present and first-writer-wins guards the record.
            amended_message = _retire_message(reason="obsolete + amended")
            result = run_git(
                repo, "commit", "--amend", "-m", amended_message, env=env
            )
            self.assertEqual(result.returncode, 0, result.stderr)

            # Sentinel path unchanged (derived from filename_stem, not sha).
            self.assertTrue(sentinel.exists())
            amended_record = json.loads(sentinel.read_text())
            # First-writer-wins: ts, reason, commit_sha stay as original.
            self.assertEqual(amended_record["ts"], original_record["ts"])
            self.assertEqual(
                amended_record["commit_sha"], original_record["commit_sha"]
            )
            self.assertEqual(amended_record["reason"], original_record["reason"])
            self.assertEqual(amended_record["commit_sha"], first_sha)

    def test_skip_override_env_suppresses_sentinel_write(self):
        with hook_repo(
            extra_env={"K2BI_SKIP_POST_COMMIT_RETIRE": "1"}
        ) as (repo, env):
            seed_initial_commit(repo, env)
            _seed_approved(repo, env)
            _land_retire(repo, env)

            retired_dir = Path(env["K2BI_RETIRED_DIR"])
            sentinels = [
                p for p in retired_dir.iterdir() if p.name.startswith(".retired-")
            ]
            self.assertEqual(sentinels, [], f"override bypassed? {sentinels}")


class RetiredDirConfigResolution(unittest.TestCase):
    """Codex R1 P0: the post-commit hook's sentinel-dir resolution
    must honour `engine.retired_dir` / `engine.kill_path` from
    `execution/validators/config.yaml` -- same values the engine
    reads at submit time. Without this parity, a custom config
    writes sentinels to one dir + reads from another and the
    retirement gate silently stays open.
    """

    def test_config_retired_dir_picked_up_when_no_env_override(self):
        # Deliberately DO NOT set K2BI_RETIRED_DIR so the hook has
        # to load config.yaml. A custom retired_dir in the config
        # must be where the sentinel lands.
        with hook_repo() as (repo, env):
            # Scrub the env override so the harness default path is
            # not the one being tested.
            env = {k: v for k, v in env.items() if k != "K2BI_RETIRED_DIR"}

            seed_initial_commit(repo, env)

            # Write a config.yaml with engine.retired_dir pointing
            # to a tmp dir inside the repo so the hook reads it.
            custom_retired = repo / "custom_retired_dir"
            custom_retired.mkdir()
            config_yaml = repo / "execution" / "validators" / "config.yaml"
            config_yaml.parent.mkdir(parents=True, exist_ok=True)
            config_yaml.write_text(
                "engine:\n"
                f"  retired_dir: {custom_retired}\n",
                encoding="utf-8",
            )
            run_git(repo, "add", "-A", env=env, check=True)
            seed_env = dict(env)
            seed_env["K2BI_ALLOW_CONFIG_EDIT"] = "1"
            run_git(
                repo, "commit", "-m", "seed config", env=seed_env, check=True
            )

            _seed_approved(repo, env)
            _land_retire(repo, env)

            # Sentinel must land in the config-specified dir, not
            # the default vault path.
            sentinel = _sentinel_path(custom_retired, "foo")
            self.assertTrue(
                sentinel.exists(),
                f"sentinel not in config-specified dir "
                f"{custom_retired}; contents: "
                f"{list(custom_retired.iterdir())}",
            )

    def test_retired_dir_takes_precedence_over_kill_path(self):
        # MiniMax R5 F1: when BOTH engine.retired_dir and
        # engine.kill_path are set in config.yaml, retired_dir wins
        # (this is resolve_retired_dir's documented precedence;
        # diverging here would silently break the gate).
        with hook_repo() as (repo, env):
            env = {k: v for k, v in env.items() if k != "K2BI_RETIRED_DIR"}

            seed_initial_commit(repo, env)
            winner = repo / "winner_retired_dir"
            loser = repo / "loser_kill_parent"
            winner.mkdir()
            loser.mkdir()
            config_yaml = repo / "execution" / "validators" / "config.yaml"
            config_yaml.parent.mkdir(parents=True, exist_ok=True)
            config_yaml.write_text(
                "engine:\n"
                f"  retired_dir: {winner}\n"
                f"  kill_path: {loser}/.killed\n",
                encoding="utf-8",
            )
            run_git(repo, "add", "-A", env=env, check=True)
            seed_env = dict(env)
            seed_env["K2BI_ALLOW_CONFIG_EDIT"] = "1"
            run_git(
                repo, "commit", "-m", "seed config",
                env=seed_env, check=True,
            )

            _seed_approved(repo, env)
            _land_retire(repo, env)

            winner_sentinel = _sentinel_path(winner, "foo")
            loser_sentinel = _sentinel_path(loser, "foo")
            self.assertTrue(
                winner_sentinel.exists(),
                f"sentinel did NOT land in config-specified retired_dir "
                f"{winner}; contents: {list(winner.iterdir())}",
            )
            self.assertFalse(
                loser_sentinel.exists(),
                f"sentinel leaked into kill_path.parent {loser}; "
                f"retired_dir precedence broken. loser dir contents: "
                f"{list(loser.iterdir())}",
            )

    def test_hook_resolver_matches_engine_resolver_across_branches(self):
        # MiniMax R5 F4 + Codex R2: load the hook script as a Python
        # module and call its `_resolve_retired_dir()` directly, then
        # compare to the engine's `kill_switch.resolve_retired_dir(...)`
        # for the same inputs. This is a REAL parity proof: not just
        # "both call the same helper" but "the hook's compound logic
        # (env override + config.yaml read + resolver delegation)
        # produces the same Path the engine would read for each input
        # shape". The integration tests above already exercise the
        # hook end-to-end via git-commit-plus-sentinel-check; this
        # test adds the unit-level contract on each branch.
        import importlib.util
        import os
        from importlib.machinery import SourceFileLoader

        from execution.risk.kill_switch import (
            DEFAULT_RETIRED_DIR,
            resolve_retired_dir as engine_resolver,
        )

        hook_path = (
            Path(__file__).resolve().parent.parent / ".githooks" / "post-commit"
        )
        # The hook file has no `.py` extension, so spec_from_file_location
        # needs an explicit SourceFileLoader to recognise it as a
        # Python module.
        loader = SourceFileLoader(
            "k2bi_post_commit_hook_under_test", str(hook_path)
        )
        spec = importlib.util.spec_from_loader(loader.name, loader)
        hook_mod = importlib.util.module_from_spec(spec)
        loader.exec_module(hook_mod)

        def _call_hook(env: dict[str, str]) -> Path:
            saved = os.environ.copy()
            # Point REPO_ROOT at an isolated tmp dir so we control
            # whether a config.yaml is present for this test probe.
            try:
                for k, v in env.items():
                    os.environ[k] = v
                for k in list(os.environ):
                    if k.startswith("K2BI_") and k not in env:
                        del os.environ[k]
                return hook_mod._resolve_retired_dir()
            finally:
                os.environ.clear()
                os.environ.update(saved)

        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            # Branch 1: env override wins unconditionally.
            env_override = tmp / "env_override"
            got = _call_hook({"K2BI_RETIRED_DIR": str(env_override)})
            self.assertEqual(got, env_override)
            self.assertEqual(got, engine_resolver(env_override, None))

            # Branches 2-4 need the hook to read config.yaml. Point
            # REPO_ROOT at a tmp dir whose execution/validators/
            # contains the scenario-specific config.
            saved_root = hook_mod.REPO_ROOT
            try:
                hook_mod.REPO_ROOT = tmp
                cfg = tmp / "execution" / "validators" / "config.yaml"
                cfg.parent.mkdir(parents=True, exist_ok=True)

                # Branch 2: only retired_dir configured.
                rd_only = tmp / "only_retired_dir"
                cfg.write_text(
                    f"engine:\n  retired_dir: {rd_only}\n",
                    encoding="utf-8",
                )
                got = _call_hook({})
                self.assertEqual(got, rd_only)
                self.assertEqual(got, engine_resolver(rd_only, None))

                # Branch 3: only kill_path configured.
                kp_only = tmp / "only_kill" / ".killed"
                kp_only.parent.mkdir()
                cfg.write_text(
                    f"engine:\n  kill_path: {kp_only}\n",
                    encoding="utf-8",
                )
                got = _call_hook({})
                self.assertEqual(got, kp_only.parent)
                self.assertEqual(got, engine_resolver(None, kp_only))

                # Branch 4: both present -- retired_dir must win
                # (precedence parity with resolve_retired_dir).
                rd_win = tmp / "winner_retired"
                kp_lose = tmp / "loser_kill" / ".killed"
                kp_lose.parent.mkdir()
                cfg.write_text(
                    "engine:\n"
                    f"  retired_dir: {rd_win}\n"
                    f"  kill_path: {kp_lose}\n",
                    encoding="utf-8",
                )
                got = _call_hook({})
                self.assertEqual(got, rd_win)
                self.assertEqual(got, engine_resolver(rd_win, kp_lose))

                # Branch 5: no config present -- default vault path.
                cfg.unlink()
                got = _call_hook({})
                self.assertEqual(got, DEFAULT_RETIRED_DIR)
                self.assertEqual(got, engine_resolver(None, None))
            finally:
                hook_mod.REPO_ROOT = saved_root

    def test_config_kill_path_parent_used_when_retired_dir_absent(self):
        # If only kill_path is configured, resolve_retired_dir
        # defaults retired_dir to kill_path.parent. The hook must
        # follow the same logic.
        with hook_repo() as (repo, env):
            env = {k: v for k, v in env.items() if k != "K2BI_RETIRED_DIR"}

            seed_initial_commit(repo, env)
            kill_parent = repo / "kill_parent_dir"
            kill_parent.mkdir()
            kill_file = kill_parent / ".killed"
            config_yaml = repo / "execution" / "validators" / "config.yaml"
            config_yaml.parent.mkdir(parents=True, exist_ok=True)
            config_yaml.write_text(
                "engine:\n"
                f"  kill_path: {kill_file}\n",
                encoding="utf-8",
            )
            run_git(repo, "add", "-A", env=env, check=True)
            seed_env = dict(env)
            seed_env["K2BI_ALLOW_CONFIG_EDIT"] = "1"
            run_git(
                repo, "commit", "-m", "seed kill_path",
                env=seed_env, check=True,
            )

            _seed_approved(repo, env)
            _land_retire(repo, env)

            sentinel = _sentinel_path(kill_parent, "foo")
            self.assertTrue(
                sentinel.exists(),
                f"sentinel not in kill_path.parent "
                f"{kill_parent}; contents: {list(kill_parent.iterdir())}",
            )


class SlugDerivationParityWithEngine(unittest.TestCase):
    """The post-commit hook MUST derive the sentinel slug with the
    same function the engine calls at submit time. Drift here
    silently disables the retirement gate (engine reads path X, hook
    writes path Y, gate never trips)."""

    def test_hook_uses_derive_retire_slug_strategy_prefix(self):
        with hook_repo() as (repo, env):
            seed_initial_commit(repo, env)
            _seed_approved(repo, env, slug="spy-rotational")
            _land_retire(repo, env, slug="spy-rotational")

            # derive_retire_slug strips the `strategy_` prefix.
            # So for file strategy_spy-rotational.md the slug passed
            # to write_retired is "spy-rotational".
            from execution.engine.main import derive_retire_slug

            computed = derive_retire_slug(
                "wiki/strategies/strategy_spy-rotational.md"
            )
            self.assertEqual(computed, "spy-rotational")

            retired_dir = Path(env["K2BI_RETIRED_DIR"])
            sentinel = _sentinel_path(retired_dir, computed)
            self.assertTrue(sentinel.exists())


class MultipleStrategiesInOneCommit(unittest.TestCase):
    def test_single_retire_trailer_ignores_non_retired_strategy_files(self):
        # If a commit touches two strategy files (one retire + one
        # unrelated change) and the trailer names only one, only that
        # one gets a sentinel.
        with hook_repo() as (repo, env):
            seed_initial_commit(repo, env)
            _seed_approved(repo, env, slug="foo")

            # Add an unrelated proposed draft for bar in a separate commit.
            write_strategy(repo, "bar", status="proposed")
            run_git(repo, "add", "-A", env=env, check=True)
            run_git(
                repo, "commit", "-m", "draft: bar", env=env, check=True
            )

            # Retire foo (only). bar stays untouched.
            _land_retire(repo, env, slug="foo")

            retired_dir = Path(env["K2BI_RETIRED_DIR"])
            foo_sentinel = _sentinel_path(retired_dir, "foo")
            bar_sentinel = _sentinel_path(retired_dir, "bar")
            self.assertTrue(foo_sentinel.exists())
            self.assertFalse(bar_sentinel.exists())


class CommitWithoutRetireTrailerIsNoOp(unittest.TestCase):
    def test_proposed_draft_commit_leaves_no_sentinel(self):
        with hook_repo() as (repo, env):
            seed_initial_commit(repo, env)
            write_strategy(repo, "foo", status="proposed")
            run_git(repo, "add", "-A", env=env, check=True)
            run_git(
                repo, "commit", "-m", "draft: foo", env=env, check=True
            )
            retired_dir = Path(env["K2BI_RETIRED_DIR"])
            self.assertEqual(
                [p for p in retired_dir.iterdir() if p.name.startswith(".retired-")],
                [],
            )

    def test_arbitrary_commit_no_strategy_file(self):
        # A commit on a totally unrelated path must not trigger
        # strategy-file scanning.
        with hook_repo() as (repo, env):
            seed_initial_commit(repo, env)
            (repo / "notes.md").write_text("hello", encoding="utf-8")
            run_git(repo, "add", "-A", env=env, check=True)
            run_git(
                repo, "commit", "-m", "add notes", env=env, check=True
            )
            retired_dir = Path(env["K2BI_RETIRED_DIR"])
            self.assertEqual(
                [p for p in retired_dir.iterdir() if p.name.startswith(".retired-")],
                [],
            )


if __name__ == "__main__":
    unittest.main()
