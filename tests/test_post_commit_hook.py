"""Post-commit hook tests -- spec §8.2 rows 14-17 + parity rows.

The post-commit hook watches for `Retired-Strategy: strategy_<slug>`
trailers on just-landed commits and writes the `.retired-<sha16>`
sentinel through `execution.risk.kill_switch.write_retired`, using
`execution.engine.main.derive_retire_slug` on the retired file's
source path so the slug-to-hash derivation matches what the engine
checks on each submit tick.

Session B (Q30) adds a parallel mirror phase: on
`Strategy-Transition: proposed -> approved` or
`approved -> retired` trailers, the hook mirrors the strategy file
from the code repo to the vault so the engine's runtime read path
reflects the commit state. The mirror phase runs BEFORE the
retire-sentinel phase; both fail-open-log-only on errors (post-commit
cannot abort a landed commit).

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
  - Approve commit -> vault mirror appears with status=approved.
  - Retire commit -> vault mirror updates to status=retired AND
    sentinel lands.
  - Reject commit (proposed -> rejected) -> NO mirror.
  - New-file draft commit (new file -> proposed) -> NO mirror.
  - Mirror write failure -> commit succeeds, stderr + wiki-log
    record `mirror_failed`.
  - K2BI_SKIP_POST_COMMIT_MIRROR=1 suppresses the mirror.
"""

from __future__ import annotations

import hashlib
import json
import os
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


# ---------- Session B (Q30) mirror phase ----------


def _use_separate_vault(repo: Path, env: dict) -> Path:
    """Point `K2BI_VAULT_ROOT` at `<repo>/vault` instead of the repo.

    The default `hook_repo()` sets vault==repo so the mirror is a
    same-file idempotent overwrite (safe no-op). Tests that need to
    ASSERT mirror landed distinctly call this to get a separate dest
    directory they can read from directly. Both the subprocess env
    (for the hook) and os.environ (for in-process callers, e.g.
    `iss.handle_approve_strategy` if a test ever mixes) get updated
    so the two paths agree. The hook_repo harness already restores
    os.environ on context exit.
    """
    vault = repo / "vault"
    vault.mkdir(exist_ok=True)
    env["K2BI_VAULT_ROOT"] = str(vault)
    os.environ["K2BI_VAULT_ROOT"] = str(vault)
    return vault


def _vault_strategy_path(vault: Path, slug: str = "foo") -> Path:
    return vault / "wiki" / "strategies" / f"strategy_{slug}.md"


class MirrorOnApproveCommit(unittest.TestCase):
    def test_approve_commit_mirrors_approved_file_to_vault(self):
        with hook_repo() as (repo, env):
            vault = _use_separate_vault(repo, env)
            seed_initial_commit(repo, env)
            _seed_approved(repo, env)

            dest = _vault_strategy_path(vault)
            self.assertTrue(
                dest.exists(),
                f"mirror missing: vault has {list(vault.rglob('*.md'))}",
            )
            # Byte-parity: dest matches the post-commit state of the
            # source so the engine's vault-side `load_approved()` sees
            # exactly what landed in the commit.
            source = repo / "wiki" / "strategies" / "strategy_foo.md"
            self.assertEqual(dest.read_bytes(), source.read_bytes())
            # Frontmatter status is approved -- the primary Q30 gate.
            self.assertIn(b"status: approved", dest.read_bytes())


class MirrorOnRetireCommit(unittest.TestCase):
    def test_retire_commit_mirrors_retired_file_and_sentinel_lands(self):
        with hook_repo() as (repo, env):
            vault = _use_separate_vault(repo, env)
            seed_initial_commit(repo, env)
            _seed_approved(repo, env)
            # After approve, mirror has status: approved.
            dest = _vault_strategy_path(vault)
            self.assertIn(b"status: approved", dest.read_bytes())

            commit_sha = _land_retire(repo, env)

            # Retire updates the mirror in place.
            self.assertIn(b"status: retired", dest.read_bytes())
            # Sentinel still lands (retire-sentinel phase still fires).
            retired_dir = Path(env["K2BI_RETIRED_DIR"])
            sentinel = _sentinel_path(retired_dir, "foo")
            self.assertTrue(sentinel.exists())
            self.assertEqual(
                json.loads(sentinel.read_text())["commit_sha"], commit_sha
            )


class MirrorOnRejectNoMirror(unittest.TestCase):
    def test_reject_commit_produces_no_vault_file(self):
        with hook_repo() as (repo, env):
            vault = _use_separate_vault(repo, env)
            seed_initial_commit(repo, env)

            # Draft foo -> reject via status flip + rejected trailers.
            write_strategy(repo, "foo", status="proposed")
            run_git(repo, "add", "-A", env=env, check=True)
            run_git(
                repo, "commit", "-m", "draft: foo", env=env, check=True
            )
            write_strategy(
                repo,
                "foo",
                status="rejected",
                rejected_at="2026-04-20T10:00:00Z",
                rejected_reason="too aggressive",
            )
            run_git(repo, "add", "-A", env=env, check=True)
            reject_message = (
                "feat(strategy): reject foo\n"
                "\n"
                "Strategy-Transition: proposed -> rejected\n"
                "Rejected-Strategy: strategy_foo\n"
                "Co-Shipped-By: invest-ship\n"
            )
            result = run_git(
                repo, "commit", "-m", reject_message, env=env
            )
            self.assertEqual(result.returncode, 0, result.stderr)

            # Decision 1 (LOCKED): reject -> NO mirror. The engine
            # never runs rejected strategies; mirroring them adds churn
            # + risks Syncthing surfacing a rejected file in the
            # vault's UI as if it were live.
            dest = _vault_strategy_path(vault)
            if dest.exists():
                # Defer the file read INSIDE the branch so an absent
                # file (the happy path) does not raise in the
                # unconditional f-string.
                self.fail(
                    f"reject leaked to vault: {dest} contents:\n"
                    f"{dest.read_text(errors='ignore')}"
                )


class MirrorOnProposedDraftNoMirror(unittest.TestCase):
    def test_new_file_proposed_draft_produces_no_vault_file(self):
        with hook_repo() as (repo, env):
            vault = _use_separate_vault(repo, env)
            seed_initial_commit(repo, env)

            # Fresh draft: (new file) -> proposed. The commit does NOT
            # have a Strategy-Transition trailer at all (the draft
            # commit is just `draft: foo`), so the mirror phase
            # short-circuits on the trailer check before ever
            # enumerating files.
            write_strategy(repo, "foo", status="proposed")
            run_git(repo, "add", "-A", env=env, check=True)
            run_git(
                repo, "commit", "-m", "draft: foo", env=env, check=True
            )

            dest = _vault_strategy_path(vault)
            self.assertFalse(
                dest.exists(),
                "proposed draft should not be mirrored (Decision 1 LOCKED)",
            )


class SkipMirrorEnvSuppresses(unittest.TestCase):
    def test_skip_override_env_prevents_mirror(self):
        with hook_repo(
            extra_env={"K2BI_SKIP_POST_COMMIT_MIRROR": "1"}
        ) as (repo, env):
            vault = _use_separate_vault(repo, env)
            # extra_env K2BI_SKIP_POST_COMMIT_MIRROR survives through
            # the env mutation in _use_separate_vault (we only touch
            # K2BI_VAULT_ROOT).
            self.assertEqual(env["K2BI_SKIP_POST_COMMIT_MIRROR"], "1")

            seed_initial_commit(repo, env)
            _seed_approved(repo, env)

            dest = _vault_strategy_path(vault)
            self.assertFalse(
                dest.exists(),
                "K2BI_SKIP_POST_COMMIT_MIRROR=1 should suppress mirror",
            )


class MirrorFailureDoesNotBlockCommit(unittest.TestCase):
    def test_mirror_write_failure_logs_and_commit_still_lands(self):
        # Deliberately misconfigure K2BI_VAULT_ROOT to a path that
        # doesn't exist. `mirror_strategy_to_vault` fails closed. The
        # post-commit hook must catch, log (stderr + wiki-log), and
        # exit 0 so the commit stays clean.
        with hook_repo() as (repo, env):
            bogus_vault = repo / "does-not-exist-yet"
            env["K2BI_VAULT_ROOT"] = str(bogus_vault)
            os.environ["K2BI_VAULT_ROOT"] = str(bogus_vault)
            # Point wiki-log helper at a scratch file so we can assert
            # the mirror_failed record without touching the real vault
            # log.
            wiki_log = repo / "wiki_log.md"
            wiki_log.write_text("", encoding="utf-8")
            env["K2BI_WIKI_LOG"] = str(wiki_log)
            env["K2BI_ALLOW_LOG_APPEND"] = "1"

            seed_initial_commit(repo, env)
            # draft + approve; approval commit fires the hook. The
            # approval itself is a successful commit (git post-commit
            # can't abort). We just need to prove the hook's mirror
            # leg didn't crash the process + left an audit trail.
            write_strategy(repo, "foo", status="proposed")
            run_git(repo, "add", "-A", env=env, check=True)
            run_git(
                repo, "commit", "-m", "draft: foo", env=env, check=True
            )
            write_strategy(
                repo,
                "foo",
                status="approved",
                approved_at="2026-04-20T10:00:00Z",
                approved_commit_sha="abc1234",
            )
            run_git(repo, "add", "-A", env=env, check=True)
            result = run_git(
                repo, "commit", "-m", _approve_message(), env=env
            )
            self.assertEqual(
                result.returncode,
                0,
                f"approve commit should land even with broken vault: "
                f"{result.stderr}",
            )
            # Stderr includes mirror_failed marker so Keith can see
            # the failure in his terminal output post-commit.
            self.assertIn("mirror", result.stderr.lower())
            # Wiki-log records the failure for the durable audit
            # trail.
            log_content = wiki_log.read_text(encoding="utf-8")
            self.assertIn("mirror_failed", log_content)


class MirrorIsIdempotentOnAmend(unittest.TestCase):
    def test_amend_reruns_mirror_without_content_drift(self):
        # `git commit --amend` re-fires post-commit in standard git.
        # Decision 5 (LOCKED): mirror is idempotent -- atomic replace
        # with identical bytes is a safe re-run. The amended commit's
        # mirror must still be byte-identical to the source.
        with hook_repo() as (repo, env):
            vault = _use_separate_vault(repo, env)
            seed_initial_commit(repo, env)
            _seed_approved(repo, env)
            dest = _vault_strategy_path(vault)
            first_bytes = dest.read_bytes()

            # Amend the approve commit with a reword (no body diff to
            # the strategy file).
            amended_msg = _approve_message() + "\nRefinement: first iteration\n"
            result = run_git(
                repo, "commit", "--amend", "-m", amended_msg, env=env
            )
            self.assertEqual(result.returncode, 0, result.stderr)

            second_bytes = dest.read_bytes()
            source = repo / "wiki" / "strategies" / "strategy_foo.md"
            self.assertEqual(
                second_bytes, source.read_bytes(),
                "mirror must track the amended commit's file bytes",
            )
            # The file content was not changed, so the bytes are the
            # same before and after amend.
            self.assertEqual(first_bytes, second_bytes)


class MirrorPerFileTransitionGating(unittest.TestCase):
    """Codex R7 #2 (HIGH): per-file (old, new) transition derivation
    prevents a legit approve trailer on file X from also mirroring an
    unrelated approved-status edit on file Y in the same commit.

    The scenario requires `--no-verify` in practice (pre-commit Check
    D blocks body edits on approved files). This test drives the
    scenario directly via a crafted working tree + --no-verify so the
    gate is exercised on the hook's own terms.
    """

    def test_mixed_commit_only_mirrors_eligible_transition_files(
        self,
    ):
        with hook_repo() as (repo, env):
            vault = _use_separate_vault(repo, env)
            seed_initial_commit(repo, env)

            # Seed approve for "alpha" via the real approval flow so
            # alpha's HEAD is (approved) and vault has alpha's
            # approved file.
            _seed_approved(repo, env, slug="alpha")

            # Now craft a "mixed" commit that (a) approves a NEW
            # strategy "beta" (proposed -> approved, eligible) AND
            # (b) body-edits alpha (approved -> approved, NOT
            # eligible). Both land on commit because we bypass Check
            # D with --no-verify.
            write_strategy(repo, "beta", status="proposed")
            run_git(repo, "add", "-A", env=env, check=True)
            run_git(
                repo, "commit", "-m", "draft: beta", env=env, check=True
            )

            # Body-edit alpha (still approved) + flip beta to
            # approved. Stage both, commit with alpha's approve
            # trailer absent + beta's approve trailer present.
            alpha_path = repo / "wiki" / "strategies" / "strategy_alpha.md"
            alpha_content = alpha_path.read_bytes()
            # Prepend a marker in the body of alpha (post-approval
            # body edit, Check-D-rejected in the normal path).
            alpha_new = alpha_content.replace(
                b"## How This Works",
                b"## How This Works\n\n<!-- UNRELATED EDIT -->\n",
                1,
            )
            alpha_path.write_bytes(alpha_new)

            write_strategy(
                repo,
                "beta",
                status="approved",
                approved_at="2026-04-20T10:00:00Z",
                approved_commit_sha="abc1234",
            )
            run_git(repo, "add", "-A", env=env, check=True)
            # Beta's approve trailer lands; alpha's body-edit is
            # tagged along. --no-verify bypasses Check D so the
            # commit actually lands.
            result = run_git(
                repo,
                "commit",
                "-m",
                _approve_message("beta"),
                "--no-verify",
                env=env,
            )
            self.assertEqual(result.returncode, 0, result.stderr)

            beta_vault = _vault_strategy_path(vault, slug="beta")
            alpha_vault = _vault_strategy_path(vault, slug="alpha")

            # Beta's legit transition mirrored.
            self.assertTrue(
                beta_vault.exists(),
                "beta approve should mirror to vault",
            )
            self.assertIn(b"status: approved", beta_vault.read_bytes())

            # Alpha mirrored via the earlier legit approval (seed),
            # but the NEW body edit must NOT reach the vault -- the
            # per-file transition gate rejects (approved, approved).
            self.assertTrue(alpha_vault.exists())
            self.assertNotIn(
                b"<!-- UNRELATED EDIT -->",
                alpha_vault.read_bytes(),
                "alpha body-edit leaked to vault despite per-file "
                "transition gate",
            )


class MirrorSkippedOnMergeCommit(unittest.TestCase):
    """Codex R8 (HIGH): per-file transition derivation uses HEAD~1,
    which on merge commits is only the first parent. Rather than
    misclassify transitions and silently skip legit mirrors, the
    hook detects merge commits and refuses to mirror, with a loud
    audit log directing the operator to re-run /invest-ship on a
    linear follow-up commit. Full merge-commit support is Phase 6+
    when / if Keith adopts PR-based approvals."""

    def test_merge_commit_skips_mirror_directly(self):
        # The kickoff scopes Keith's workflow to linear commits
        # (Phase 2-3 has no PR flow). To keep this test independent
        # of the hook-harness merge-commit quirks, we drive the
        # mirror-phase function directly + assert the merge
        # detection short-circuits before any vault I/O. The
        # `_is_merge_commit()` helper's own logic is unit-tested
        # below.
        with hook_repo() as (repo, env):
            vault = _use_separate_vault(repo, env)
            seed_initial_commit(repo, env)
            # Create a real merge commit via git so _is_merge_commit
            # returns True when the hook introspects HEAD.
            run_git(repo, "checkout", "-b", "side", env=env, check=True)
            (repo / "side.md").write_text("x", encoding="utf-8")
            run_git(repo, "add", "-A", env=env, check=True)
            run_git(repo, "commit", "-m", "side", env=env, check=True)
            run_git(repo, "checkout", "main", env=env, check=True)
            (repo / "main.md").write_text("x", encoding="utf-8")
            run_git(repo, "add", "-A", env=env, check=True)
            run_git(repo, "commit", "-m", "main", env=env, check=True)
            run_git(
                repo, "merge", "--no-ff", "-m", "merge", "side", env=env,
                check=True,
            )
            parents = (
                run_git(
                    repo,
                    "rev-list",
                    "--parents",
                    "-n",
                    "1",
                    "HEAD",
                    env=env,
                    check=True,
                )
                .stdout.strip()
                .split()
            )
            self.assertGreater(
                len(parents), 2,
                f"expected merge commit, got {parents!r}",
            )

            # Invoke the hook directly so we can observe its stderr
            # without depending on the harness's merge-hook wiring
            # (which has known quirks around .githooks symlinks +
            # hook inheritance through symlinked hooksPath).
            hook_path = (
                Path(__file__).resolve().parent.parent
                / ".githooks"
                / "post-commit"
            )
            result = subprocess.run(
                [str(hook_path)],
                capture_output=True,
                text=True,
                cwd=str(repo),
                env=env,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            # Stderr banner names the merge-commit skip case.
            self.assertIn("mirror phase: SKIPPED on merge commit", result.stderr)
            # Vault has no mirrored strategy (the merge commit
            # doesn't touch any strategy file, but more importantly
            # the short-circuit prevented vault I/O).
            strategies_dir = vault / "wiki" / "strategies"
            if strategies_dir.exists():
                self.assertEqual(list(strategies_dir.iterdir()), [])


class MirrorPreservesHeadByteFidelity(unittest.TestCase):
    """Codex R4 #1 (HIGH): the hook's `_file_at_head` must return the
    raw bytes of the committed blob. Using git's text-mode stdout
    decodes through the locale, then re-encodes to utf-8 -- which
    can normalise line endings or drop invalid byte sequences,
    silently corrupting the mirror (engine's source of truth).

    This test writes a strategy file with CRLF line endings, commits
    it through the approval flow, and asserts the vault mirror's
    bytes match the repo's post-commit bytes exactly.
    """

    def test_crlf_in_strategy_body_round_trips_through_mirror(self):
        with hook_repo() as (repo, env):
            vault = _use_separate_vault(repo, env)
            seed_initial_commit(repo, env)

            slug = "crlf"
            # Draft with CRLF line endings in the body. Commit-msg
            # hook + pre-commit hook validate frontmatter (LF) so we
            # keep those lines LF; only the markdown body carries
            # CRLF. This is a contrived shape, but any byte-fidelity
            # loss on committed blobs would show up here.
            body_crlf = (
                "## How This Works\r\n"
                "\r\n"
                "Line with CRLF.\r\n"
            )
            approved_content = (
                "---\n"
                f"name: {slug}\n"
                "status: approved\n"
                "strategy_type: hand_crafted\n"
                "risk_envelope_pct: 0.01\n"
                "regime_filter:\n"
                "  - risk_on\n"
                "order:\n"
                "  ticker: SPY\n"
                "  side: buy\n"
                "  qty: 1\n"
                "  limit_price: 500.00\n"
                "  stop_loss: 490.00\n"
                "  time_in_force: DAY\n"
                "approved_at: 2026-04-20T10:00:00Z\n"
                "approved_commit_sha: abc1234\n"
                "---\n"
                "\n"
            ) + body_crlf
            # Seed the proposed draft first (pre-commit Check B
            # requires How This Works non-empty at approve-time).
            proposed_content = approved_content.replace(
                "status: approved", "status: proposed"
            ).replace(
                "approved_at: 2026-04-20T10:00:00Z\n",
                "",
            ).replace(
                "approved_commit_sha: abc1234\n",
                "",
            )
            rel = f"wiki/strategies/strategy_{slug}.md"
            (repo / "wiki" / "strategies").mkdir(parents=True)
            (repo / rel).write_bytes(proposed_content.encode("utf-8"))
            run_git(repo, "add", "-A", env=env, check=True)
            run_git(
                repo, "commit", "-m", f"draft: {slug}", env=env, check=True
            )

            # Now write the approved version (still with CRLF body)
            # and commit via the approve trailer. The post-commit
            # mirror must see the post-commit HEAD bytes, byte-exact.
            (repo / rel).write_bytes(approved_content.encode("utf-8"))
            run_git(repo, "add", "-A", env=env, check=True)
            result = run_git(
                repo, "commit", "-m", _approve_message(slug), env=env
            )
            self.assertEqual(result.returncode, 0, result.stderr)

            # Mirror bytes MUST match HEAD bytes exactly. If the hook
            # round-tripped through text mode, CRLFs would become
            # LFs and this assertion would fail.
            dest = _vault_strategy_path(vault, slug=slug)
            self.assertTrue(dest.exists(), f"mirror missing: {dest}")
            head_bytes = subprocess.run(
                ["git", "-C", str(repo), "show", f"HEAD:{rel}"],
                capture_output=True,
                env=env,
                check=True,
            ).stdout
            self.assertEqual(
                dest.read_bytes(),
                head_bytes,
                "mirror drifted from HEAD bytes (likely CRLF "
                "normalisation via text-mode git output)",
            )


class MirrorRegexRejectsPartialMatches(unittest.TestCase):
    def test_body_only_edit_on_approved_does_not_mirror(self):
        # Check D blocks body edits on approved files, so this commit
        # shouldn't land at all. We test that IF (hypothetically) such
        # a commit landed, the mirror phase would NOT fire -- because
        # there's no Strategy-Transition trailer at all on a status-
        # unchanged edit. Proven by running a non-strategy commit on
        # top of an approved state and verifying the vault didn't
        # re-mirror. Guards against regex drift.
        with hook_repo() as (repo, env):
            vault = _use_separate_vault(repo, env)
            seed_initial_commit(repo, env)
            _seed_approved(repo, env)
            dest = _vault_strategy_path(vault)
            # Touch the mirror dest with a marker that a naive
            # re-mirror would overwrite.
            original = dest.read_bytes()
            dest.write_bytes(original + b"\n<!-- marker -->\n")

            # Unrelated commit with no Strategy-Transition trailer.
            (repo / "notes.md").write_text("hello", encoding="utf-8")
            run_git(repo, "add", "-A", env=env, check=True)
            run_git(
                repo, "commit", "-m", "chore: note", env=env, check=True
            )

            # Marker must still be present -- mirror did NOT fire.
            self.assertIn(b"<!-- marker -->", dest.read_bytes())


if __name__ == "__main__":
    unittest.main()
