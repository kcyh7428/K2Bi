"""Tests for scripts/lib/deploy_config.py.

Covers the §7.3 test harness from the Bundle 3 architect spec:
- a new top-level dir not in `targets` and not in `excludes` fails preflight
- a top-level dir in `targets` passes
- a top-level dir in `excludes` passes
- list-categories / list-targets / classify emit expected output

These tests spin up a tmpdir "repo" with a minimal deploy-config.yml and
exercise the helper against it. No real git init required because the helper
falls back to $K2BI_DEPLOY_CONFIG when set.
"""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
HELPER = REPO_ROOT / "scripts" / "lib" / "deploy_config.py"


def _write_config(dir_: Path, body: str) -> Path:
    scripts_dir = dir_ / "scripts"
    scripts_dir.mkdir(parents=True, exist_ok=True)
    cfg = scripts_dir / "deploy-config.yml"
    cfg.write_text(textwrap.dedent(body))
    return cfg


def _run(cfg: Path, *args: str, cwd: Path | None = None) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    env["K2BI_DEPLOY_CONFIG"] = str(cfg)
    return subprocess.run(
        [sys.executable, str(HELPER), *args],
        capture_output=True,
        text=True,
        env=env,
        cwd=str(cwd) if cwd else None,
    )


def test_list_categories(tmp_path: Path) -> None:
    cfg = _write_config(
        tmp_path,
        """
        targets:
          - path: execution/
            category: execution
          - path: scripts/
            category: scripts
          - path: .claude/skills/
            category: skills
        excludes:
          - .git
        """,
    )
    r = _run(cfg, "list-categories")
    assert r.returncode == 0, r.stderr
    assert r.stdout.splitlines() == ["execution", "scripts", "skills"]


def test_list_targets_all_and_filtered(tmp_path: Path) -> None:
    cfg = _write_config(
        tmp_path,
        """
        targets:
          - path: execution/
            category: execution
          - path: requirements.txt
            category: execution
          - path: scripts/
            category: scripts
        excludes: []
        """,
    )
    r_all = _run(cfg, "list-targets")
    assert r_all.returncode == 0
    assert r_all.stdout.splitlines() == [
        "execution/",
        "requirements.txt",
        "scripts/",
    ]
    r_ex = _run(cfg, "list-targets", "execution")
    assert r_ex.stdout.splitlines() == ["execution/", "requirements.txt"]
    r_none = _run(cfg, "list-targets", "nonexistent")
    assert r_none.returncode == 0
    assert r_none.stdout == ""


def test_classify_prefix_match(tmp_path: Path) -> None:
    cfg = _write_config(
        tmp_path,
        """
        targets:
          - path: execution/
            category: execution
          - path: scripts/
            category: scripts
          - path: CLAUDE.md
            category: skills
        excludes: []
        """,
    )
    r = _run(
        cfg,
        "classify",
        "execution/engine/main.py",
        "scripts/foo.sh",
        "CLAUDE.md",
        "random/thing.txt",
    )
    assert r.returncode == 0, r.stderr
    lines = r.stdout.strip().splitlines()
    assert "execution\texecution/engine/main.py" in lines
    assert "scripts\tscripts/foo.sh" in lines
    assert "skills\tCLAUDE.md" in lines
    assert "uncovered\trandom/thing.txt" in lines


def _touch(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("content")


def test_preflight_fails_on_uncovered_top_level_dir(tmp_path: Path) -> None:
    """§7.3 test harness (a): a new path not in targets + not in excludes
    must make preflight exit 1 with the drift root in stderr."""
    _touch(tmp_path / "execution" / "engine.py")
    _touch(tmp_path / "scripts" / "deploy.sh")
    _touch(tmp_path / "an_uncovered_dir" / "file.txt")
    cfg = _write_config(
        tmp_path,
        """
        targets:
          - path: execution/
            category: execution
          - path: scripts/
            category: scripts
        excludes:
          - .git
        """,
    )
    r = _run(cfg, "preflight")
    assert r.returncode == 1
    assert "an_uncovered_dir" in r.stderr


def test_preflight_passes_when_target_covers_dir(tmp_path: Path) -> None:
    """§7.3 test harness (b): a top-level dir present in targets passes."""
    _touch(tmp_path / "execution" / "engine.py")
    _touch(tmp_path / "scripts" / "deploy.sh")
    cfg = _write_config(
        tmp_path,
        """
        targets:
          - path: execution/
            category: execution
          - path: scripts/
            category: scripts
        excludes: []
        """,
    )
    r = _run(cfg, "preflight")
    assert r.returncode == 0, r.stderr


def test_preflight_passes_when_excluded(tmp_path: Path) -> None:
    _touch(tmp_path / "execution" / "engine.py")
    _touch(tmp_path / "proposals" / "notes.md")
    _touch(tmp_path / "scripts" / "deploy.sh")  # scripts/ holds the config
    cfg = _write_config(
        tmp_path,
        """
        targets:
          - path: execution/
            category: execution
          - path: scripts/
            category: scripts
        excludes:
          - proposals
        """,
    )
    r = _run(cfg, "preflight")
    assert r.returncode == 0, r.stderr


def test_preflight_covers_file_targets(tmp_path: Path) -> None:
    """A top-level FILE target (e.g. CLAUDE.md) is matched by exact path."""
    _touch(tmp_path / "execution" / "engine.py")
    _touch(tmp_path / "scripts" / "deploy.sh")
    _touch(tmp_path / "CLAUDE.md")
    cfg = _write_config(
        tmp_path,
        """
        targets:
          - path: execution/
            category: execution
          - path: scripts/
            category: scripts
          - path: CLAUDE.md
            category: skills
        excludes: []
        """,
    )
    r = _run(cfg, "preflight")
    assert r.returncode == 0, r.stderr


def test_preflight_catches_partial_tree_drift(tmp_path: Path) -> None:
    """Codex R1 P2 #2: coverage must be full-path prefix, not top-segment.

    If .claude/skills/ is a target and .claude/plugins is an exclude, adding
    a new .claude/commands/foo.md must still flag preflight -- the parent
    .claude/ is only PARTIALLY covered, so a novel sibling is drift."""
    _touch(tmp_path / "execution" / "engine.py")
    _touch(tmp_path / "scripts" / "deploy.sh")
    _touch(tmp_path / ".claude" / "skills" / "one" / "SKILL.md")
    _touch(tmp_path / ".claude" / "plugins" / "plug.json")
    _touch(tmp_path / ".claude" / "commands" / "foo.md")
    cfg = _write_config(
        tmp_path,
        """
        targets:
          - path: execution/
            category: execution
          - path: scripts/
            category: scripts
          - path: .claude/skills/
            category: skills
        excludes:
          - .claude/plugins
        """,
    )
    r = _run(cfg, "preflight")
    assert r.returncode == 1, r.stdout + r.stderr
    assert ".claude/commands" in r.stderr
    # Must NOT accidentally re-flag the covered sibling subtrees.
    assert ".claude/skills" not in r.stderr.replace(".claude/skills/", "")
    assert "execution" not in r.stderr.replace("execution/", "")


def test_preflight_ignores_gitignored_scratch(tmp_path: Path) -> None:
    """Codex R1 P2 #1: gitignored developer-local scratch (notes/, .DS_Store,
    runtime caches) must NOT trip preflight. Requires git init so .gitignore
    enumeration works."""
    _touch(tmp_path / "execution" / "engine.py")
    _touch(tmp_path / "scripts" / "deploy.sh")
    _touch(tmp_path / "notes" / "scratch.md")
    _touch(tmp_path / ".DS_Store")
    (tmp_path / ".gitignore").write_text("notes/\n.DS_Store\n")
    cfg = _write_config(
        tmp_path,
        """
        targets:
          - path: execution/
            category: execution
          - path: scripts/
            category: scripts
        excludes:
          - .gitignore
        """,
    )
    subprocess.run(
        ["git", "init", "-q"], cwd=str(tmp_path), check=True, capture_output=True
    )
    subprocess.run(
        ["git", "add", "-A"], cwd=str(tmp_path), check=True, capture_output=True
    )
    r = _run(cfg, "preflight")
    assert r.returncode == 0, (
        f"gitignored scratch tripped preflight: {r.stderr}"
    )


def test_missing_config_fails_loud(tmp_path: Path) -> None:
    env = os.environ.copy()
    env["K2BI_DEPLOY_CONFIG"] = str(tmp_path / "nope.yml")
    r = subprocess.run(
        [sys.executable, str(HELPER), "list-categories"],
        capture_output=True,
        text=True,
        env=env,
    )
    assert r.returncode == 2
    assert "missing" in r.stderr.lower()


def test_malformed_yaml_fails_loud(tmp_path: Path) -> None:
    cfg = _write_config(
        tmp_path,
        """
        targets:
          - path: execution/
            category: execution
          bogus bogus bogus
        """,
    )
    r = _run(cfg, "list-categories")
    assert r.returncode == 2
    assert "parse" in r.stderr.lower() or "yaml" in r.stderr.lower()


def test_fallback_parser_handles_deploy_config_schema() -> None:
    """Codex R2 P2: the helper must work on a fresh clone whose host python
    does not have PyYAML installed yet (operational /ship + /sync path).
    Verify the stdlib fallback parses the real schema correctly."""
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "deploy_config_under_test",
        REPO_ROOT / "scripts" / "lib" / "deploy_config.py",
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    sample = textwrap.dedent("""
        # Comment line
        targets:
          - path: execution/
            category: execution
          - path: CLAUDE.md       # inline comment
            category: skills
          - path: scripts/
            category: scripts
        excludes:
          - .git
          - .venv
          - proposals
    """).strip()
    parsed = mod._fallback_parse(sample)
    assert [t["path"] for t in parsed["targets"]] == [
        "execution/",
        "CLAUDE.md",
        "scripts/",
    ]
    assert [t["category"] for t in parsed["targets"]] == [
        "execution",
        "skills",
        "scripts",
    ]
    assert parsed["excludes"] == [".git", ".venv", "proposals"]


def test_non_mapping_top_level_fails_loud(tmp_path: Path) -> None:
    """Codex R3 P2: a syntactically valid YAML that is not a mapping at the
    top level (e.g. a stray list) must fail loudly rather than silently
    coerce to an empty config. Otherwise /ship + /sync get confusing
    behavior (every path uncovered, no categories) instead of a clean error."""
    cfg = _write_config(
        tmp_path,
        """
        - path: execution/
          category: execution
        - path: scripts/
          category: scripts
        """,
    )
    r = _run(cfg, "list-categories")
    assert r.returncode == 2, (r.stdout, r.stderr)
    assert "mapping" in r.stderr.lower() or "must be" in r.stderr.lower()


def test_empty_file_fails_loud(tmp_path: Path) -> None:
    """Codex R4 P2: `or {}` silently coerced empty/None YAML to empty config.
    An empty deploy-config.yml should fail loud the same as any other
    malformed config so Keith sees an actionable error."""
    cfg = _write_config(tmp_path, "")
    r = _run(cfg, "list-categories")
    assert r.returncode == 2, (r.stdout, r.stderr)
    assert "empty" in r.stderr.lower()


def test_null_yaml_fails_loud(tmp_path: Path) -> None:
    cfg = _write_config(tmp_path, "null\n")
    r = _run(cfg, "list-categories")
    assert r.returncode == 2, (r.stdout, r.stderr)
    # Either 'empty' (None path) or 'mapping' (scalar path) is acceptable
    assert "empty" in r.stderr.lower() or "mapping" in r.stderr.lower()


def test_fallback_parser_rejects_unexpected_top_level() -> None:
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "deploy_config_under_test_bad",
        REPO_ROOT / "scripts" / "lib" / "deploy_config.py",
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    with pytest.raises(ValueError):
        mod._fallback_parse("garbage: value\n  nested: bad")


def test_fallback_path_runs_when_pyyaml_absent(tmp_path: Path) -> None:
    """Force the fallback path by running the helper in a subprocess whose
    sys.path excludes PyYAML, and verify it still loads a config and prints
    categories. Uses python -I (isolated mode) which disables user site but
    not global site-packages -- so we also mask yaml via a sitecustomize
    shim in the tmp dir."""
    _touch(tmp_path / "execution" / "engine.py")
    _touch(tmp_path / "scripts" / "deploy.sh")
    cfg = _write_config(
        tmp_path,
        """
        targets:
          - path: execution/
            category: execution
          - path: scripts/
            category: scripts
        excludes:
          - .git
        """,
    )
    shim = tmp_path / "_no_yaml_shim"
    shim.mkdir()
    (shim / "yaml.py").write_text(
        "raise ImportError('yaml disabled by test shim')"
    )
    env = os.environ.copy()
    env["K2BI_DEPLOY_CONFIG"] = str(cfg)
    env["PYTHONPATH"] = f"{shim}:{env.get('PYTHONPATH', '')}"
    r = subprocess.run(
        [sys.executable, str(HELPER), "list-categories"],
        capture_output=True,
        text=True,
        env=env,
    )
    assert r.returncode == 0, r.stderr
    assert r.stdout.splitlines() == ["execution", "scripts"]


def test_repo_level_config_preflight_currently_passes() -> None:
    """Sanity check: the actual scripts/deploy-config.yml in this repo must
    have no uncovered top-level drift. If a new top-level dir is added and
    this test fails, either add it to the config's targets or its excludes."""
    r = subprocess.run(
        [sys.executable, str(HELPER), "preflight"],
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
    )
    assert r.returncode == 0, (
        f"Repo deploy-config preflight failed.\nstderr:\n{r.stderr}"
    )


# =====================================================================
# detect-categories + record-sync (Bundle 3 cycle 7 carry-over fix)
# =====================================================================


def _init_git_repo(path: Path) -> None:
    """Initialize a minimal git repo + identity at `path` for detection tests.

    Pinned to `-b main` so tests do not depend on the user's init.defaultBranch
    config. `commit.gpgsign=false` so the host's signing config (which the
    test has no key for) cannot make every commit call fail.
    """
    subprocess.run(
        ["git", "init", "-q", "-b", "main", str(path)], check=True
    )
    for cfg_pair in (
        ("user.email", "test@k2bi.local"),
        ("user.name", "k2bi-test"),
        ("commit.gpgsign", "false"),
    ):
        subprocess.run(
            ["git", "-C", str(path), "config", *cfg_pair], check=True
        )


def _git_commit_all(path: Path, message: str) -> str:
    """Stage everything and commit. Return the new HEAD SHA."""
    subprocess.run(["git", "-C", str(path), "add", "-A"], check=True)
    subprocess.run(
        ["git", "-C", str(path), "commit", "-q", "-m", message],
        check=True,
        env={**os.environ, "GIT_AUTHOR_DATE": "2026-04-19T00:00:00Z",
             "GIT_COMMITTER_DATE": "2026-04-19T00:00:00Z"},
    )
    out = subprocess.run(
        ["git", "-C", str(path), "rev-parse", "HEAD"],
        check=True, capture_output=True, text=True,
    )
    return out.stdout.strip()


def _write_sentinel(repo: Path, sha: str) -> Path:
    sentinel_dir = repo / ".sync-state"
    sentinel_dir.mkdir(exist_ok=True)
    sentinel = sentinel_dir / "last-synced-commit"
    sentinel.write_text(sha + "\n")
    return sentinel


def _standard_cfg(tmp_path: Path) -> Path:
    """Write a minimal multi-category config used by the cycle-5-scenario
    tests. Excludes cover .git + .sync-state so preflight on this tree is
    clean too, keeping the tests self-contained."""
    return _write_config(
        tmp_path,
        """
        targets:
          - path: .claude/skills/
            category: skills
          - path: CLAUDE.md
            category: skills
          - path: DEVLOG.md
            category: skills
          - path: scripts/
            category: scripts
          - path: execution/
            category: execution
        excludes:
          - .git
          - .gitignore
          - .sync-state
          - .pending-sync
          - .minimax-reviews
          - .code-reviews
          - tests
          - proposals
          - plans
        """,
    )


def test_detect_categories_cycle_5_devlog_after_code_commit(tmp_path: Path) -> None:
    """Cycle-5 carry-over bug: a devlog-only commit after a scripts/ commit
    must not mask the scripts/ category in auto-detect.

    Pre-fix, the detect_changes() bash function fell back to
    `git diff --name-only HEAD~1 HEAD` when the working tree was clean. If
    commit HEAD is a devlog-only commit sitting on top of a scripts/ code
    commit, the fallback picks up only DEVLOG.md and classifies as `skills`,
    silently dropping the `scripts` category -- the Mini would run stale
    helper code until someone noticed.

    Post-fix: detect-categories diffs `<sentinel>..HEAD`, where <sentinel>
    was the SHA immediately before the scripts/ commit. Both categories
    must surface.
    """
    _init_git_repo(tmp_path)
    (tmp_path / "CLAUDE.md").write_text("initial\n")
    baseline = _git_commit_all(tmp_path, "baseline")
    _write_sentinel(tmp_path, baseline)

    # Commit A: scripts/ code change (the thing that must not get missed)
    _touch(tmp_path / "scripts" / "foo.sh")
    _git_commit_all(tmp_path, "feat: scripts change")

    # Commit B: devlog-only follow-up (the thing that was masking A)
    (tmp_path / "DEVLOG.md").write_text("devlog entry\n")
    _git_commit_all(tmp_path, "docs: devlog")

    # Working tree is clean after both commits landed.
    cfg = _standard_cfg(tmp_path)
    r = _run(cfg, "detect-categories", cwd=tmp_path)
    assert r.returncode == 0, r.stderr
    cats = sorted(r.stdout.strip().splitlines())
    assert "scripts" in cats, (
        f"cycle-5 bug regressed: 'scripts' missing from {cats}"
    )
    assert "skills" in cats, f"'skills' missing from {cats}"


def test_detect_categories_no_sentinel_returns_all(tmp_path: Path) -> None:
    """First-time-sync semantics: without a sentinel, detect-categories must
    conservatively return every known category so nothing is silently
    skipped on a fresh clone."""
    _init_git_repo(tmp_path)
    (tmp_path / "CLAUDE.md").write_text("x\n")
    _git_commit_all(tmp_path, "initial")

    cfg = _standard_cfg(tmp_path)
    r = _run(cfg, "detect-categories", cwd=tmp_path)
    assert r.returncode == 0, r.stderr
    cats = sorted(r.stdout.strip().splitlines())
    assert cats == ["execution", "scripts", "skills"]


def test_detect_categories_stale_sentinel_returns_all(tmp_path: Path) -> None:
    """Sentinel SHA that is not reachable in the current object graph
    (rebased / amended / gc'd away) must degrade gracefully to
    "return all categories", NOT crash and NOT silently return empty."""
    _init_git_repo(tmp_path)
    (tmp_path / "CLAUDE.md").write_text("x\n")
    _git_commit_all(tmp_path, "initial")
    _write_sentinel(tmp_path, "deadbeef" * 5)  # unreachable 40-char SHA

    cfg = _standard_cfg(tmp_path)
    r = _run(cfg, "detect-categories", cwd=tmp_path)
    assert r.returncode == 0, r.stderr
    cats = sorted(r.stdout.strip().splitlines())
    assert cats == ["execution", "scripts", "skills"]


def test_detect_categories_clean_tree_matching_sentinel_returns_empty(
    tmp_path: Path,
) -> None:
    """Sentinel at HEAD AND a clean working tree AND no untracked files
    must return an empty set -- there is genuinely nothing to deploy."""
    _init_git_repo(tmp_path)
    (tmp_path / "CLAUDE.md").write_text("x\n")
    # Write the config BEFORE the baseline commit so it is tracked and does
    # not show up as an untracked file in detect-categories' ls-files scan.
    cfg = _standard_cfg(tmp_path)
    sha = _git_commit_all(tmp_path, "initial")
    _write_sentinel(tmp_path, sha)

    r = _run(cfg, "detect-categories", cwd=tmp_path)
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == "", (
        f"expected empty output, got: {r.stdout!r}"
    )


def test_detect_categories_uncommitted_modifications(tmp_path: Path) -> None:
    """A tracked file modified in the working tree but not yet committed
    must surface via the `git diff --name-only HEAD` branch, independent
    of sentinel state."""
    _init_git_repo(tmp_path)
    _touch(tmp_path / "scripts" / "foo.sh")
    sha = _git_commit_all(tmp_path, "initial")
    _write_sentinel(tmp_path, sha)

    # Modify the tracked file in-place; do not commit.
    (tmp_path / "scripts" / "foo.sh").write_text("modified\n")

    cfg = _standard_cfg(tmp_path)
    r = _run(cfg, "detect-categories", cwd=tmp_path)
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip().splitlines() == ["scripts"]


def test_detect_categories_untracked_new_file(tmp_path: Path) -> None:
    """An untracked new file that deploy-config.yml's targets cover must
    surface via `git ls-files --others --exclude-standard`, even when no
    commits have landed since the sentinel."""
    _init_git_repo(tmp_path)
    (tmp_path / "CLAUDE.md").write_text("x\n")
    sha = _git_commit_all(tmp_path, "baseline")
    _write_sentinel(tmp_path, sha)

    # Add a new untracked file in an execution/ subtree.
    _touch(tmp_path / "execution" / "new_module.py")

    cfg = _standard_cfg(tmp_path)
    r = _run(cfg, "detect-categories", cwd=tmp_path)
    assert r.returncode == 0, r.stderr
    cats = r.stdout.strip().splitlines()
    assert "execution" in cats


def test_detect_categories_not_a_git_repo_returns_all(tmp_path: Path) -> None:
    """If the override path is not a git repo (no .git dir), detect-categories
    must fall back to list-categories rather than crash on `git rev-parse`."""
    # No _init_git_repo -- tmp_path stays a plain directory.
    (tmp_path / "CLAUDE.md").write_text("x\n")

    cfg = _standard_cfg(tmp_path)
    r = _run(cfg, "detect-categories", cwd=tmp_path)
    assert r.returncode == 0, r.stderr
    cats = sorted(r.stdout.strip().splitlines())
    assert cats == ["execution", "scripts", "skills"]


def test_detect_categories_ignores_uncovered_changes(tmp_path: Path) -> None:
    """Changes in paths that are neither targets nor excludes (preflight
    would catch these) must not emit spurious categories -- detect-categories
    returns only classified-covered categories."""
    _init_git_repo(tmp_path)
    _touch(tmp_path / "CLAUDE.md")
    cfg = _standard_cfg(tmp_path)
    sha = _git_commit_all(tmp_path, "baseline")
    _write_sentinel(tmp_path, sha)

    # An uncovered top-level file change after baseline.
    _touch(tmp_path / "random_note.md")

    # Note: this test deliberately skips calling preflight. In prod, preflight
    # blocks /ship on this drift, so detect-categories would never see it.
    # The test verifies the helper is robust even when it does.
    r = _run(cfg, "detect-categories", cwd=tmp_path)
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == "", (
        f"uncovered change leaked into categories: {r.stdout!r}"
    )


def test_record_sync_writes_head_sha(tmp_path: Path) -> None:
    """record-sync must atomically persist HEAD's SHA to
    .sync-state/last-synced-commit."""
    _init_git_repo(tmp_path)
    (tmp_path / "CLAUDE.md").write_text("x\n")
    sha = _git_commit_all(tmp_path, "initial")

    cfg = _standard_cfg(tmp_path)
    r = _run(cfg, "record-sync", cwd=tmp_path)
    assert r.returncode == 0, r.stderr

    sentinel = tmp_path / ".sync-state" / "last-synced-commit"
    assert sentinel.is_file()
    assert sentinel.read_text().strip() == sha


def test_record_sync_overwrites_existing_sentinel(tmp_path: Path) -> None:
    """Calling record-sync twice (second commit) must refresh the sentinel
    to the new HEAD, not append or refuse."""
    _init_git_repo(tmp_path)
    (tmp_path / "CLAUDE.md").write_text("x\n")
    first_sha = _git_commit_all(tmp_path, "first")

    cfg = _standard_cfg(tmp_path)
    _run(cfg, "record-sync", cwd=tmp_path)

    # Second commit, then record again.
    (tmp_path / "CLAUDE.md").write_text("y\n")
    second_sha = _git_commit_all(tmp_path, "second")
    r = _run(cfg, "record-sync", cwd=tmp_path)
    assert r.returncode == 0, r.stderr

    sentinel = tmp_path / ".sync-state" / "last-synced-commit"
    assert sentinel.read_text().strip() == second_sha
    assert sentinel.read_text().strip() != first_sha


def test_record_sync_atomic_tempfile_cleaned_on_success(tmp_path: Path) -> None:
    """After record-sync succeeds, no .tmp file should linger next to the
    sentinel. This exercises the os.replace() atomicity contract."""
    _init_git_repo(tmp_path)
    (tmp_path / "CLAUDE.md").write_text("x\n")
    _git_commit_all(tmp_path, "initial")

    cfg = _standard_cfg(tmp_path)
    r = _run(cfg, "record-sync", cwd=tmp_path)
    assert r.returncode == 0, r.stderr

    state_dir = tmp_path / ".sync-state"
    # Match both the pre-fix `.tmp` suffix and the post-F3-fix `.tmp_*`
    # prefix + `.sync-state` suffix, so a regression in EITHER direction
    # fails this test.
    leftover = list(state_dir.glob("*.tmp")) + list(state_dir.glob(".tmp_*"))
    assert leftover == [], f"temp files not cleaned: {leftover}"


def test_record_sync_fails_when_not_a_git_repo(tmp_path: Path) -> None:
    """record-sync outside a git repo must exit non-zero with a clear error
    rather than silently writing a bogus SHA."""
    cfg = _standard_cfg(tmp_path)
    r = _run(cfg, "record-sync", cwd=tmp_path)
    assert r.returncode != 0
    assert "record-sync" in r.stderr


# =====================================================================
# Codex R7 final-gate F1/F2/F3 fixes (sentinel snapshot + ancestor-check
# + unique-tempfile)
# =====================================================================


def test_detect_categories_sentinel_not_ancestor_of_head_returns_all(
    tmp_path: Path,
) -> None:
    """Codex R7 final-gate F2: a sentinel SHA that is still reachable in
    the object database but is NOT an ancestor of HEAD (orphaned branch,
    post-force-push tip) must trigger the all-categories fallback rather
    than diffing against an unrelated history."""
    _init_git_repo(tmp_path)
    _touch(tmp_path / "CLAUDE.md")
    cfg = _standard_cfg(tmp_path)
    _git_commit_all(tmp_path, "main baseline")

    # Create an orphan branch with its own content, keep the SHA, then go
    # back to main and commit something different. The orphan SHA is
    # reachable via the orphan branch ref but is NOT an ancestor of main.
    subprocess.run(
        ["git", "-C", str(tmp_path), "checkout", "-q", "--orphan", "orphan"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(tmp_path), "rm", "-rf", "-q", "--cached", "."],
        check=True,
    )
    (tmp_path / "CLAUDE.md").unlink()
    (tmp_path / "orphan_file.md").write_text("from orphan\n")
    orphan_sha = _git_commit_all(tmp_path, "orphan commit")

    # Switch back to main. CLAUDE.md gets restored from main's tree.
    subprocess.run(
        ["git", "-C", str(tmp_path), "checkout", "-q", "main"], check=True
    )

    # Write the orphan SHA into the sentinel: reachable via the orphan ref
    # but NOT an ancestor of main. detect-categories must treat it as
    # stale and fall back to all categories.
    _write_sentinel(tmp_path, orphan_sha)

    r = _run(cfg, "detect-categories", cwd=tmp_path)
    assert r.returncode == 0, r.stderr
    cats = sorted(r.stdout.strip().splitlines())
    assert cats == ["execution", "scripts", "skills"], (
        f"orphan-branch sentinel should trigger all-categories fallback; "
        f"got {cats}"
    )


def test_detect_categories_honors_head_override(tmp_path: Path) -> None:
    """Codex R7 final-gate F1: `detect-categories --head <sha>` must use
    the explicit SHA as the diff target rather than the current `HEAD`.
    deploy-to-vps.sh relies on this so detection + record-sync see the
    same snapshot even if a new commit lands mid-sync."""
    _init_git_repo(tmp_path)
    _touch(tmp_path / "CLAUDE.md")
    cfg = _standard_cfg(tmp_path)
    baseline = _git_commit_all(tmp_path, "baseline")
    _write_sentinel(tmp_path, baseline)

    # First new commit: touches scripts. This is the "pin" target.
    _touch(tmp_path / "scripts" / "foo.sh")
    pinned_head = _git_commit_all(tmp_path, "B: scripts only")

    # A SECOND commit lands after detection pinned its target -- simulates
    # a commit racing in while rsync is copying files. Touches execution.
    _touch(tmp_path / "execution" / "racing.py")
    _git_commit_all(tmp_path, "C: execution (raced in mid-sync)")

    # detect-categories --head <pinned_head> must NOT include `execution`
    # (that change landed AFTER the pin). Without --head, detection would
    # see HEAD=C and include execution, and record-sync would advance the
    # sentinel past content that was never part of the rsync plan.
    r = _run(cfg, "detect-categories", "--head", pinned_head, cwd=tmp_path)
    assert r.returncode == 0, r.stderr
    cats = sorted(r.stdout.strip().splitlines())
    assert "scripts" in cats, (
        f"--head pin missing scripts category; got {cats}"
    )
    assert "execution" not in cats, (
        f"--head pin should exclude post-pin changes; got {cats}."
        f" `execution` commit landed after the pin and must not appear."
    )


def test_record_sync_honors_sha_override(tmp_path: Path) -> None:
    """Codex R7 final-gate F1: `record-sync --sha <explicit>` writes the
    given SHA rather than `git rev-parse HEAD`. Prevents the sentinel
    advancing past content the sync plan did not include when a new
    commit landed during rsync."""
    _init_git_repo(tmp_path)
    _touch(tmp_path / "CLAUDE.md")
    cfg = _standard_cfg(tmp_path)
    pinned = _git_commit_all(tmp_path, "pinned baseline")

    # Simulate a race: another commit landed after the sync started.
    _touch(tmp_path / "scripts" / "raced.sh")
    _git_commit_all(tmp_path, "C: raced in")

    r = _run(cfg, "record-sync", "--sha", pinned, cwd=tmp_path)
    assert r.returncode == 0, r.stderr

    sentinel = tmp_path / ".sync-state" / "last-synced-commit"
    assert sentinel.read_text().strip() == pinned, (
        "sentinel should be the pinned SHA, not the post-race HEAD"
    )


def test_record_sync_rejects_nonhex_sha(tmp_path: Path) -> None:
    """`--sha` input must be a hex string. cmd_record_sync validates
    format before touching the filesystem."""
    _init_git_repo(tmp_path)
    _touch(tmp_path / "CLAUDE.md")
    _git_commit_all(tmp_path, "initial")

    cfg = _standard_cfg(tmp_path)
    r = _run(cfg, "record-sync", "--sha", "not a sha", cwd=tmp_path)
    assert r.returncode != 0
    assert "malformed" in r.stderr.lower()


def test_record_sync_rejects_unreachable_sha(tmp_path: Path) -> None:
    """`--sha` must name a real commit in this repo. A typo-SHA that
    looks well-formed but has no corresponding object must be rejected
    BEFORE the sentinel is written, so a bad manual invocation cannot
    poison the sync baseline."""
    _init_git_repo(tmp_path)
    _touch(tmp_path / "CLAUDE.md")
    _git_commit_all(tmp_path, "initial")

    cfg = _standard_cfg(tmp_path)
    r = _run(cfg, "record-sync", "--sha", "deadbeef" * 5, cwd=tmp_path)
    assert r.returncode != 0
    assert "does not resolve" in r.stderr


def test_record_sync_concurrent_writes_no_tempfile_collision(
    tmp_path: Path,
) -> None:
    """Codex R7 final-gate F3: concurrent record-sync invocations must
    not collide on a deterministic temp filename. Each writer owns its
    own mkstemp temp file; whichever os.replace lands last wins cleanly,
    with no ENOENT failures for any racer.

    Prior to the fix, a shared `.tmp` suffix meant one invocation could
    unlink or replace the shared tmp file while another was still
    writing, producing ENOENT on some of the parallel callers.

    MiniMax R7 R2 F2 strengthening: each racer writes a DISTINCT SHA so
    a last-write-wins bug that happens to pick a shared SHA from a
    colliding tempfile (pre-fix pathology) is distinguishable from the
    mkstemp-per-writer fixed behaviour. All racers still succeed; the
    final sentinel must contain exactly ONE of the five SHAs written,
    never an empty / truncated / hybrid value.
    """
    _init_git_repo(tmp_path)
    _touch(tmp_path / "CLAUDE.md")
    base = _git_commit_all(tmp_path, "baseline")

    # Seed 5 distinct commits so each racer can write a distinct SHA.
    # Using a stack of commits keeps every SHA reachable + ancestor of
    # HEAD so rev-parse --verify passes for all five.
    distinct_shas: list[str] = []
    for i in range(5):
        (tmp_path / f"marker_{i}.txt").write_text(f"{i}\n")
        distinct_shas.append(_git_commit_all(tmp_path, f"marker {i}"))
    assert len(set(distinct_shas)) == 5, "fixture drift: SHAs collided"
    assert base not in distinct_shas

    cfg = _standard_cfg(tmp_path)
    env = os.environ.copy()
    env["K2BI_DEPLOY_CONFIG"] = str(cfg)

    import concurrent.futures as cf

    def _invoke(idx: int) -> int:
        proc = subprocess.run(
            [
                sys.executable,
                str(HELPER),
                "record-sync",
                "--sha",
                distinct_shas[idx],
            ],
            capture_output=True,
            text=True,
            env=env,
            cwd=str(tmp_path),
        )
        return proc.returncode

    with cf.ThreadPoolExecutor(max_workers=5) as pool:
        rcs = list(pool.map(_invoke, range(5)))

    assert all(rc == 0 for rc in rcs), (
        f"concurrent record-sync had failures: {rcs}"
    )
    sentinel = tmp_path / ".sync-state" / "last-synced-commit"
    assert sentinel.is_file()
    final = sentinel.read_text().strip()
    # The final sentinel must be one of the five SHAs (any ordering of
    # os.replace is acceptable) -- NEVER empty, truncated, or a hybrid
    # of multiple writers' content.
    assert final in distinct_shas, (
        f"concurrent writers left sentinel in corrupt state: {final!r};"
        f" expected one of {distinct_shas}"
    )
    # Also guard against a pathological no-newline or mixed-content
    # result: last-write-wins still produces exactly 40 hex chars.
    assert len(final) == 40 and all(c in "0123456789abcdef" for c in final)


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
