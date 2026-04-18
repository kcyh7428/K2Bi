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


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
