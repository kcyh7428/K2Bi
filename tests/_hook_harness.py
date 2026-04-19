"""Shared integration-test harness for the Bundle 3 cycle 4 git hooks.

Each hook test spins up an ephemeral git repo that uses the REAL
`.githooks/*` files from this checkout (via symlink) so the tests
exercise exactly what ships. The shared helper + the `execution/`
package are loaded from the real repo via `PYTHONPATH`, so we do not
copy trees into the tmp repo.

Tests in this file must be fast enough to run in the normal suite --
each test hits only the hook machinery; no network, no real IBKR.

Conventions:
- `hook_repo()` yields `(repo_path, env)`; always pass `env` to git
  subprocess calls so `PYTHONPATH` + any hook overrides stick.
- `commit(repo, env, message, *paths)` is the canonical add+commit
  wrapper so tests express intent instead of plumbing.
- `write_strategy(repo, slug, **kw)` authors a strategy file under
  `wiki/strategies/strategy_<slug>.md` matching spec §2.1.
"""

from __future__ import annotations

import contextlib
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Iterable


REPO_ROOT = Path(__file__).resolve().parent.parent


def _env_with(extra: dict[str, str] | None = None) -> dict[str, str]:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(REPO_ROOT)
    # Never let the real user's vault path leak into tests; callers
    # that exercise the post-commit hook set a per-test retired dir.
    env.pop("K2BI_RETIRED_DIR", None)
    # Same story for the kill-path override; keep tests isolated from
    # any outer CI env that may have it set.
    env.pop("K2BI_KILL_PATH", None)
    if extra:
        env.update(extra)
    return env


def run_git(
    repo: Path, *args: str, env: dict[str, str] | None = None, check: bool = False
) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        env=env or _env_with(),
        check=check,
    )


@contextlib.contextmanager
def hook_repo(*, extra_env: dict[str, str] | None = None):
    """Yield `(repo_path, env)` pre-configured with K2Bi hooks installed.

    Keeps the retired-sentinel writes isolated to the tmp repo's own
    `System/` dir via `K2BI_RETIRED_DIR`, so a post-commit retire in a
    test does NOT touch Keith's real vault. Callers can pass
    `extra_env` to override anything else (e.g. K2BI_ALLOW_LOG_APPEND).
    """
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        env = _env_with(extra_env)
        # Isolate retired sentinels inside the tmp repo.
        retired_dir = tmp / "System"
        retired_dir.mkdir()
        env["K2BI_RETIRED_DIR"] = str(retired_dir)

        run_git(tmp, "init", "-q", "-b", "main", env=env, check=True)
        run_git(tmp, "config", "user.email", "test@test.test", env=env, check=True)
        run_git(tmp, "config", "user.name", "Test", env=env, check=True)
        run_git(tmp, "config", "commit.gpgSign", "false", env=env, check=True)

        # Use the real-repo hooks directly so the tests exercise the
        # shipped files. Symlink keeps the tmp write set small; if the
        # platform rejects symlinks, fall back to copy.
        hooks_link = tmp / ".githooks"
        real_hooks = REPO_ROOT / ".githooks"
        try:
            os.symlink(real_hooks, hooks_link)
        except OSError:
            shutil.copytree(real_hooks, hooks_link)
        run_git(tmp, "config", "core.hooksPath", ".githooks", env=env, check=True)

        # The hooks also call scripts/wiki-log-append.sh for best-effort
        # override logging. Symlink the helper into the tmp repo so the
        # `[ -x $REPO_ROOT/scripts/wiki-log-append.sh ]` check finds it;
        # tests that want to assert the helper actually wrote a line
        # set K2BI_WIKI_LOG to a scratch file inside the tmp tree.
        scripts_dir = tmp / "scripts"
        scripts_dir.mkdir(exist_ok=True)
        src_log_helper = REPO_ROOT / "scripts" / "wiki-log-append.sh"
        try:
            os.symlink(src_log_helper, scripts_dir / "wiki-log-append.sh")
        except OSError:
            shutil.copy2(src_log_helper, scripts_dir / "wiki-log-append.sh")

        yield tmp, env


def write_file(repo: Path, rel: str, content: str | bytes) -> Path:
    path = repo / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(content, str):
        path.write_text(content, encoding="utf-8")
    else:
        path.write_bytes(content)
    return path


def strategy_text(
    *,
    name: str,
    status: str = "proposed",
    strategy_type: str = "hand_crafted",
    risk_envelope_pct: str = "0.01",
    regime_filter: list[str] | None = None,
    order: dict | None = None,
    approved_at: str | None = None,
    approved_commit_sha: str | None = None,
    retired_at: str | None = None,
    retired_reason: str | None = None,
    rejected_at: str | None = None,
    rejected_reason: str | None = None,
    how_this_works: str | None = "Plain-English explanation body.",
    extras: dict | None = None,
    include_how: bool = True,
    body_after_how: str = "",
) -> str:
    """Render a strategy markdown file matching spec §2.1 frontmatter.

    Default shape is a happy-path proposed draft; tests override
    individual knobs to construct the row they need. `extras` injects
    raw `k: v` lines verbatim after the spec fields (for testing
    foreign frontmatter on approved files).
    """
    lines = ["---", f"name: {name}", f"status: {status}", f"strategy_type: {strategy_type}"]
    lines.append(f"risk_envelope_pct: {risk_envelope_pct}")
    if regime_filter is not None:
        lines.append("regime_filter:")
        for r in regime_filter:
            lines.append(f"  - {r}")
    if order is not None:
        lines.append("order:")
        for k, v in order.items():
            lines.append(f"  {k}: {v}")
    if approved_at is not None:
        lines.append(f"approved_at: {approved_at}")
    if approved_commit_sha is not None:
        lines.append(f"approved_commit_sha: {approved_commit_sha}")
    if retired_at is not None:
        lines.append(f"retired_at: {retired_at}")
    if retired_reason is not None:
        lines.append(f'retired_reason: "{retired_reason}"')
    if rejected_at is not None:
        lines.append(f"rejected_at: {rejected_at}")
    if rejected_reason is not None:
        lines.append(f'rejected_reason: "{rejected_reason}"')
    if extras:
        for k, v in extras.items():
            lines.append(f"{k}: {v}")
    lines.append("---")
    lines.append("")
    if include_how:
        lines.append("## How This Works")
        lines.append("")
        if how_this_works:
            lines.append(how_this_works)
    if body_after_how:
        lines.append("")
        lines.append(body_after_how)
    return "\n".join(lines) + "\n"


def default_order() -> dict:
    return {
        "ticker": "SPY",
        "side": "buy",
        "qty": 1,
        "limit_price": "500.00",
        "stop_loss": "490.00",
        "time_in_force": "DAY",
    }


def write_strategy(
    repo: Path,
    slug: str,
    *,
    status: str = "proposed",
    filename: str | None = None,
    **kwargs,
) -> Path:
    """Author `wiki/strategies/strategy_<slug>.md` with default order."""
    kwargs.setdefault("order", default_order())
    content = strategy_text(name=slug, status=status, **kwargs)
    rel = filename if filename else f"wiki/strategies/strategy_{slug}.md"
    return write_file(repo, rel, content)


def write_limits_proposal(
    repo: Path,
    slug: str,
    *,
    date: str = "2026-04-19",
    status: str = "proposed",
    approved_at: str | None = None,
    approved_commit_sha: str | None = None,
    rule: str = "position_size",
) -> Path:
    frontmatter = [
        "---",
        "tags: [review, strategy-approvals, limits-proposal]",
        f"date: {date}",
        "type: limits-proposal",
        "origin: keith",
        f"status: {status}",
        "applies-to: execution/validators/config.yaml",
    ]
    if approved_at:
        frontmatter.append(f"approved_at: {approved_at}")
    if approved_commit_sha:
        frontmatter.append(f"approved_commit_sha: {approved_commit_sha}")
    frontmatter += [
        'up: "[[index]]"',
        "---",
        "",
        f"# Limits Proposal: {slug}",
        "",
        "## Change",
        "",
        "```yaml",
        f"rule: {rule}",
        "change_type: widen",
        "before: 0.10",
        "after: 0.15",
        "```",
        "",
        "## Rationale (Keith's)",
        "",
        "Increase sizing cap for blue-chip equities.",
        "",
        "## Safety Impact (skill's assessment)",
        "",
        "Neutral if regime_filter is honored.",
        "",
        "## Approval",
        "",
        "Pending Keith.",
    ]
    rel = f"review/strategy-approvals/{date}_limits-proposal_{slug}.md"
    return write_file(repo, rel, "\n".join(frontmatter) + "\n")


def stage(repo: Path, env: dict[str, str], *paths: str | Path) -> None:
    args = [str(p) if isinstance(p, Path) else p for p in paths]
    result = run_git(repo, "add", *args, env=env)
    if result.returncode != 0:
        raise AssertionError(f"git add failed: {result.stderr}")


def stage_all(repo: Path, env: dict[str, str]) -> None:
    result = run_git(repo, "add", "-A", env=env)
    if result.returncode != 0:
        raise AssertionError(f"git add failed: {result.stderr}")


def commit(
    repo: Path,
    env: dict[str, str],
    message: str,
    *paths: str | Path,
    extra_env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess:
    """Stage files + attempt commit; return CompletedProcess for assertions."""
    if paths:
        stage(repo, env, *paths)
    effective_env = env.copy()
    if extra_env:
        effective_env.update(extra_env)
    return run_git(repo, "commit", "-m", message, env=effective_env)


def seed_initial_commit(repo: Path, env: dict[str, str]) -> str:
    """Make an initial commit so HEAD exists; tests need `git show HEAD:`
    in many rows. Returns the commit sha."""
    write_file(repo, "README.md", "k2bi test repo\n")
    run_git(repo, "add", "README.md", env=env, check=True)
    run_git(
        repo, "commit", "-m", "initial commit", env=env, check=True
    )
    res = run_git(repo, "rev-parse", "HEAD", env=env, check=True)
    return res.stdout.strip()


def paths_exist(base: Path, *rels: Iterable[str]) -> list[str]:
    return [r for r in rels if (base / r).exists()]
