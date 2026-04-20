"""Shared helper + CLI for /invest-ship strategy approval subcommands (cycle 5).

Four subcommands ship here -- one for each strategy-transition flag on
`/invest-ship`, plus the limits-proposal variant:

    approve-strategy   proposed  -> approved      on wiki/strategies/strategy_*.md
    reject-strategy    proposed  -> rejected      (takes --reason)
    retire-strategy    approved  -> retired       (takes --reason)
    approve-limits     proposed  -> approved      on review/strategy-approvals/*_limits-proposal_*.md
                       AND applies the proposal's `## YAML Patch` to
                       execution/validators/config.yaml atomically (same-commit
                       pairing required by pre-commit Check C).

Each subcommand performs:

    Step A -- validate input file shape + current status, delegating the
              frontmatter parse / transition enum check to the cycle-4
              shared helper `scripts.lib.strategy_frontmatter`. Validation
              failures exit 1 with a specific stderr message; the skill
              body surfaces that to Keith verbatim.
    Step D -- capture the PARENT sha via `git rev-parse --short HEAD` as
              the FIRST action (spec §6 Q1 -- approved_commit_sha is the
              parent sha, never the approval commit's own sha, never via
              --amend). Rewrite the frontmatter atomically (tempfile +
              os.replace) with the status flip + new keys appended. The
              skill body then handles staging + commit.

Commit-message trailers are produced by a single shared function
`build_trailers` so the format matches cycle-4's `.githooks/commit-msg`
grammar byte-for-byte -- ONE seam across all four subcommands. Trailer
output is returned in the handler's result dict and emitted by the CLI
as JSON so the skill body can splice it into the commit message without
re-deriving anything.

Python API:

    handle_approve_strategy(path, *, parent_sha=None, now=None) -> dict
    handle_reject_strategy(path, reason, *, now=None)           -> dict
    handle_retire_strategy(path, reason, *, now=None)           -> dict
    handle_approve_limits(path, config_path=None, *, parent_sha=None,
                           now=None)                            -> dict
    build_trailers(kind, transition, slug, *, rule=None,
                    change_type=None) -> list[str]
    ValidationError                                              exception

CLI (consumed from the skill body via bash / Bash tool):

    python3 -m scripts.lib.invest_ship_strategy approve-strategy <path>
    python3 -m scripts.lib.invest_ship_strategy reject-strategy <path> --reason <text>
    python3 -m scripts.lib.invest_ship_strategy retire-strategy <path> --reason <text>
    python3 -m scripts.lib.invest_ship_strategy approve-limits <path> \
        [--config-path <override>]
    python3 -m scripts.lib.invest_ship_strategy build-trailers \
        --kind strategy --transition "proposed -> approved" --slug spy-rot

On success, CLI prints a JSON object to stdout and exits 0. On validation
failure, prints the error to stderr and exits 1. Unexpected failures
(unreadable file, git failure, etc.) exit 2.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from scripts.lib import invest_bear_case as ibc
from scripts.lib import strategy_frontmatter as sf


# ---------- backtest approval gate constants (Bundle 4 cycle 3 / m2.15) ----------


# Spec §3.5 LOCKED enum for the `backtest.look_ahead_check` field. Any
# other value on a backtest file refuses approval (forces the enum to
# stay locked; drift would need an explicit spec change + rev here).
_BACKTEST_LOOK_AHEAD_PASSED = "passed"
_BACKTEST_LOOK_AHEAD_SUSPICIOUS = "suspicious"


# Spec §3.5 override section heading. Keith writes this in the strategy
# body to accept a `suspicious` backtest. `has_section` accepts any suffix
# after the heading (Backtest Override (2026-04-19), etc.), so the text
# here is just the canonical heading title -- the helper handles variants.
_BACKTEST_OVERRIDE_HEADING = "Backtest Override"


# ---------- constants ----------

# Required frontmatter fields on a strategy file at approval time. The
# ordering mirrors spec §2.1; `sorted(...)` below keeps error output
# deterministic.
REQUIRED_STRATEGY_FIELDS = frozenset(
    {"name", "strategy_type", "risk_envelope_pct", "regime_filter", "order"}
)

# Codex R7 P1 #1: the cycle-4 commit-msg + post-commit hooks both glob
# staged/landed files with `^wiki/strategies/strategy_[^/]+\.md$`. A
# strategy file that sits off that canonical path (e.g. a sub-folder
# like `wiki/strategies/archive/strategy_x.md`, or a typo like
# `wiki/strategy/strategy_x.md`) receives no hook treatment -- the
# commit-msg trailer check never fires, and the post-commit retire
# sentinel never lands, so the engine retirement gate silently stays
# open. Step A must therefore match the SAME regex the hooks enforce,
# not just filename-stem consistency. The pattern is repo-relative and
# matches forward-slash-separated paths; `Path.as_posix()` gives us
# that normalisation on every platform we target.
CANONICAL_STRATEGY_PATH_RE = re.compile(
    r"^wiki/strategies/strategy_[^/]+\.md$"
)

# Required keys inside the `order:` mapping at approval time (spec §2.1).
REQUIRED_ORDER_FIELDS = frozenset(
    {"ticker", "side", "qty", "limit_price", "stop_loss", "time_in_force"}
)

# Limits-proposal required frontmatter fields (spec §2.3).
REQUIRED_LIMITS_FIELDS = frozenset(
    {"type", "status", "applies-to"}
)

# Limits-proposal `## Change` block required keys.
REQUIRED_CHANGE_KEYS = frozenset({"rule", "change_type", "before", "after"})

# Subset of validator rules we know how to route to a top-level config.yaml
# section when the proposal is applied. A proposal's `rule:` field must
# match one of these; unknown rules fail Step A rather than silently
# succeeding by shaping a no-op edit.
VALID_LIMITS_RULES = frozenset(
    {
        "position_size",
        "trade_risk",
        "leverage",
        "market_hours",
        "instrument_whitelist",
    }
)

# Change-type enum from spec §2.3.
VALID_CHANGE_TYPES = frozenset({"widen", "tighten", "add", "remove"})

# Default config.yaml path for --approve-limits. Tests override via
# --config-path; prod runs against the shipped one.
DEFAULT_CONFIG_YAML = Path("execution") / "validators" / "config.yaml"

# Q30 Session B -- Decision 3: the vault root is a deployment fact, not
# a repo-relative one. The code repo lives at ~/Projects/K2Bi and the
# Syncthing-managed vault lives at ~/Projects/K2Bi-Vault; the engine's
# runtime read path targets the vault, while /invest-ship writes to the
# code repo (git-tracked, where hooks fire + approved_commit_sha means
# something). `DEFAULT_VAULT_ROOT` names the vault as a hardcoded Path
# so consumers stop deriving it from `strategy_path.resolve().parents[2]`
# (the old auto-detect resolved to the REPO on code-repo strategy files
# -- the root of the Q30 split-brain). `K2BI_VAULT_ROOT` env overrides
# for test isolation + deploy flexibility (Mac Mini path remap, CI). A
# future vault rename needs to flip this constant AND
# `ResolveVaultRootPrecedence.test_default_points_at_expected_vault_path`
# so the mismatch surfaces at CI instead of in a silently-skipped mirror.
#
# Defensive HOME handling (MiniMax R1 #1): `Path.home()` raises
# `RuntimeError` when HOME is unset AND pwd lookup fails -- a rare
# container/CI edge case. Rather than letting an import-time failure
# here crash every consumer of this module (which would crash the
# post-commit hook, blocking both mirror + sentinel phases), fall back
# to a clearly-broken sentinel path. The `.exists()` guards in
# `resolve_vault_root` / `mirror_strategy_to_vault` / pre-flight then
# surface the misconfiguration with a readable message instead of a
# RuntimeError thousands of frames away.
try:
    DEFAULT_VAULT_ROOT = Path.home() / "Projects" / "K2Bi-Vault"
except RuntimeError:
    DEFAULT_VAULT_ROOT = Path("/nonexistent/HOME-unset/K2Bi-Vault")

# Q30 Session B -- trailer regex single source of truth. The
# `.githooks/post-commit` mirror phase imports this so the regex
# consumer and the `build_trailers` producer cannot drift out of
# lockstep. Byte-exact match against the format
# `build_trailers("strategy", "<transition>", ...)` emits:
#   `Strategy-Transition: proposed -> approved`
#   `Strategy-Transition: approved -> retired`
# Anchored BOL/EOL + re.MULTILINE so the trailer is matched line-wise
# (commit messages are multi-line; the subject + body + trailer block
# are all in the same buffer). Round-trip with build_trailers is
# enforced by `TrailerRegexRoundTrip` in test_invest_ship_mirror.py.
MIRROR_TRAILER_RE = re.compile(
    r"^Strategy-Transition: (?:proposed -> approved|approved -> retired)$",
    re.MULTILINE,
)

# Statuses eligible for vault mirroring. Defensive enum: the trailer
# regex already gates the transition, but the hook re-checks the
# landed file's frontmatter status before writing so a crafted commit
# message cannot make the hook mirror a status it shouldn't (e.g. a
# commit that smuggles the approve trailer onto a body with
# status=proposed).
MIRROR_STATUSES = frozenset({"approved", "retired"})

# Codex R7 #2 (HIGH): per-file (old, new) transitions that warrant a
# mirror. Set is the SAME as the trailer enum, expressed as pairs so
# the hook can derive eligibility from HEAD-vs-parent state rather
# than solely from the commit message trailer. Closes the mixed-
# commit escape: if one file legitimately transitions proposed ->
# approved AND another approved file is body-edited in the same
# commit (only possible via `--no-verify`), the trailer matches but
# per-file transition for the second file is ("approved", "approved")
# -- not in this set -- so the second file is NOT mirrored.
MIRROR_ELIGIBLE_TRANSITIONS = frozenset(
    {
        ("proposed", "approved"),
        ("approved", "retired"),
    }
)


# ---------- vault-root resolver (Q30 Session B -- Change 1) ----------


def resolve_vault_root(override: Path | None = None) -> Path:
    """Return the vault root Claude Code + engine agree on.

    Precedence (Q30 Decision 3, locked):

        1. ``override`` argument (explicit pass-through for tests +
           internal callers that already know the vault root).
        2. ``K2BI_VAULT_ROOT`` environment variable (test harnesses set
           this; prod deploys set it when the vault path diverges from
           the default, e.g. Mac Mini remap).
        3. ``DEFAULT_VAULT_ROOT`` module constant
           (``~/Projects/K2Bi-Vault``).

    Empty-string env values fall through to the constant -- a
    deployment typo should surface as ``[default vault path] not
    found`` rather than silently resolving to ``Path("")`` and
    producing cryptic cwd-relative path errors further downstream.

    Replaces the ``strategy_path.resolve().parents[2]`` auto-detect the
    approval-gate consumers previously used to locate bear-case + backtest
    evidence. That auto-detect assumed the strategy file lived under the
    vault, which was true only as a side effect of tests seeding both the
    strategy AND its gate evidence under the same tmp repo -- in
    production the strategy lives in the CODE REPO and the gate evidence
    lives in the VAULT, so ``parents[2]`` pointed at the wrong tree (root
    of the Q30 split-brain).

    Cross-tree alignment (Codex R6 #2, architect-scoped):
    Operators who remap K2BI_VAULT_ROOT MUST also update the engine's
    ``engine.strategies_dir`` in ``execution/validators/config.yaml``
    (or the engine will load from a different tree than the hook
    mirrors to, recreating the Q30 split-brain across a different
    axis). Cross-tree unification -- making the engine derive its
    strategies_dir from the same resolver -- is Phase 4+ work scoped
    out of Session B (kickoff: ``DO NOT touch execution/engine/**``).
    Keith's single-machine deployment today uses DEFAULT_VAULT_ROOT
    at both sides + config.yaml's default strategies_dir, so the two
    align; the divergence risk only surfaces if an operator sets
    ONE without the other.
    """
    if override is not None:
        return override
    env_value = os.environ.get("K2BI_VAULT_ROOT", "").strip()
    if env_value:
        return Path(env_value)
    return DEFAULT_VAULT_ROOT


# ---------- mirror helper (Q30 Session B -- Change 2) ----------


def _probe_vault_destination(vault_root: Path) -> Path:
    """Validate the vault's strategy-mirror destination is writable.

    Codex R3 #2 (HIGH) fix: a plain ``os.access(vault_root, os.W_OK)``
    doesn't prove that ``vault_root/wiki/strategies/`` is reachable +
    writable -- a restrictive permission on ``wiki/`` or a non-
    directory at ``wiki/strategies/`` leaves approval succeeding while
    the post-commit mirror silently fails. This helper walks the
    ancestry (``wiki/`` then ``wiki/strategies/``), mkdir-on-demand,
    rejects non-directory obstacles + symlinks, then temp-probes the
    destination with a tempfile+replace write identical to what
    ``atomic_write_bytes`` performs. If the probe fails, the real
    mirror will fail too; raising HERE surfaces the defect at
    approval time instead of letting the commit land and the hook
    silently log `mirror_failed`.

    Returns the destination directory on success so callers can reuse
    the probed-to-exist path without re-resolving.
    """
    # Codex R4 #2 (HIGH): reject symlinks on EVERY path component in
    # the destination subtree, not just `vault_root`. `Path.is_dir()`
    # follows symlinks, so a crafted `vault_root/wiki -> /elsewhere`
    # or `vault_root/wiki/strategies -> /elsewhere` would pass the
    # is_dir() guard and then silently redirect writes outside the
    # vault. Explicit `is_symlink()` checks at each level close that
    # containment hole; atomic_write_bytes still refuses symlinks at
    # the final file path (three layers of defence total).
    #
    # Codex R9 (HIGH): also reject a symlinked IMMEDIATE parent of
    # vault_root (e.g. K2BI_VAULT_ROOT=/safe/link/vault where
    # /safe/link is a symlink). Full ancestor-chain walk up to / is
    # not applied because OS-level symlinks (macOS /var -> /private/
    # var, typical on tmpfile paths used by tests) are legitimate
    # and checking them would break the test harness without
    # meaningfully closing the threat. The immediate parent is the
    # attack surface most exposed to operator-level misconfiguration;
    # deeper ancestor symlinks are architect-scoped out as de minimis
    # for K2Bi's single-user threat model.
    if vault_root.parent.is_symlink():
        raise ValueError(
            f"vault_root {vault_root!s} has a symlinked immediate "
            f"parent ({vault_root.parent!s}); refuse to mirror through "
            f"a redirected path. Set K2BI_VAULT_ROOT to a canonical "
            f"path with no symlinked direct parent."
        )
    if vault_root.is_symlink():
        raise ValueError(
            f"vault_root {vault_root!s} is a symlink; refuse to mirror "
            f"through a vault symlink (TOCTOU hazard + deployment "
            f"drift vector; resolve or replace the symlink)"
        )
    wiki_dir = vault_root / "wiki"
    if wiki_dir.is_symlink():
        raise ValueError(
            f"{wiki_dir!s} is a symlink; refuse to mirror through a "
            f"symlinked wiki/ (could redirect writes outside the vault; "
            f"resolve or replace)"
        )
    if wiki_dir.exists() and not wiki_dir.is_dir():
        raise ValueError(
            f"{wiki_dir!s} exists but is not a directory; remove the "
            f"blocker before approving strategies"
        )
    dest_dir = wiki_dir / "strategies"
    if dest_dir.is_symlink():
        raise ValueError(
            f"{dest_dir!s} is a symlink; refuse to mirror through a "
            f"symlinked strategies/ (could redirect writes outside the "
            f"vault; resolve or replace)"
        )
    if dest_dir.exists() and not dest_dir.is_dir():
        raise ValueError(
            f"{dest_dir!s} exists but is not a directory; remove the "
            f"blocker before approving strategies"
        )
    try:
        dest_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise ValueError(
            f"cannot create vault destination {dest_dir!s}: {exc}; "
            f"check filesystem permissions + parent directories"
        ) from exc
    # Probe a tempfile in the actual destination dir with the same
    # atomic_write_bytes mechanics the real mirror uses. Any error
    # here (read-only mount, denied group write, disk full) surfaces
    # BEFORE approval proceeds. The probe file is immediately
    # unlinked; concurrent mirrors to the SAME strategy file name are
    # governed by atomic_write_bytes's tempfile+os.replace semantics
    # + first-writer-wins -- the probe is about the dir, not any
    # specific filename.
    probe_name = f".k2bi-mirror-probe.{os.getpid()}"
    probe_path = dest_dir / probe_name
    try:
        sf.atomic_write_bytes(probe_path, b"")
    except (OSError, ValueError) as exc:
        raise ValueError(
            f"vault destination {dest_dir!s} is not writable: {exc}; "
            f"check permissions, Syncthing mount state, + mount flags"
        ) from exc
    finally:
        try:
            probe_path.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            # Left-behind probe is not a correctness issue; the
            # janitor + next-probe-overwrite keeps it bounded. Don't
            # fail the whole approval over cleanup noise.
            pass
    return dest_dir


def mirror_strategy_to_vault(
    repo_path: Path,
    *,
    vault_root: Path | None = None,
    content: bytes | None = None,
) -> Path:
    """Atomic-mirror a strategy file from the code repo into the vault.

    The engine's runtime read path is ``{VAULT_ROOT}/wiki/strategies/``;
    ``/invest-ship --approve-strategy`` + ``--retire-strategy`` edit
    files under ``{REPO_ROOT}/wiki/strategies/`` (so git hooks fire +
    ``approved_commit_sha`` resolves). Without a mirror step the engine
    never sees approvals. The ``.githooks/post-commit`` mirror phase
    calls this helper on approve + retire transitions so vault state
    tracks commit state atomically with the commit.

    Q30 Decisions wired in here (LOCKED, see Session B kickoff):

      * Decision 2 -- dest is ``vault_root/wiki/strategies/<filename>``
        (byte-parity mirror, no slug derivation, no filename rewrite).
      * Decision 2 -- overwrites stale vault dest atomically; the
        commit is the source of truth. Symlinks at the dest are
        refused (inherited from ``sf.atomic_write_bytes``).
      * Decision 5 -- idempotent: calling twice with identical source
        bytes produces identical destination bytes. Safe on
        ``git commit --amend`` re-fire.
      * Fail-closed on invalid vault_root (not a directory, or the
        path does not exist). A deployment typo in the env var should
        surface HERE with a clear message, not downstream as a cryptic
        mkdir error.

    Parameters
    ----------
    repo_path : Path
        The strategy file inside the code repo. Used for the
        destination filename (``repo_path.name``). When ``content`` is
        supplied (Codex R3 #1 fix: hook reads HEAD bytes), the disk
        file at ``repo_path`` is NOT read -- this closes the working-
        tree-vs-HEAD drift window. When ``content`` is ``None``,
        falls back to ``repo_path.read_bytes()`` for direct callers.
    vault_root : Path | None
        Override the resolved vault root (tests). ``None`` defers to
        :func:`resolve_vault_root`.
    content : bytes | None
        Explicit bytes to write. The post-commit hook passes
        ``_file_at_head(path)`` output so the mirror always reflects
        the committed blob, not the working tree (which might have
        drifted if the user edited between commit and post-commit
        hook execution). Falls back to ``repo_path.read_bytes()``
        when ``None`` -- preserves the existing test + direct-call
        ergonomics.

    Returns
    -------
    Path
        The vault-side destination the strategy now lives at. Callers
        that want to log the mirror landing record this path.

    Raises
    ------
    ValueError
        vault_root does not exist, is not a directory, is a symlink,
        is not writable, or the destination subtree has a blocker.
    ValueError
        dest is a symlink (inherited symlink-refusal from
        ``sf.atomic_write_bytes``).
    FileNotFoundError
        ``repo_path`` does not exist (and ``content`` is None).
    """
    resolved_vault = resolve_vault_root(vault_root)
    if not resolved_vault.exists():
        raise ValueError(
            f"vault_root {resolved_vault!s} does not exist; create the "
            f"Syncthing-managed vault directory before approving "
            f"strategies (Q30 mirror requires a live vault)"
        )
    if not resolved_vault.is_dir():
        raise ValueError(
            f"vault_root {resolved_vault!s} is not a directory; check "
            f"K2BI_VAULT_ROOT env var for a deployment typo"
        )
    # Codex R3 #2 (HIGH): validate the ACTUAL destination subtree, not
    # just vault_root. `_probe_vault_destination` walks ancestry,
    # catches non-dir blockers / symlinks, mkdirs on demand, and runs
    # a tempfile+replace probe using the same primitive the real write
    # uses. If the probe passes here, the subsequent atomic_write_bytes
    # to the same directory will also succeed for the same reason.
    dest_dir = _probe_vault_destination(resolved_vault)
    payload = content if content is not None else repo_path.read_bytes()
    # Codex R5 #2 (MEDIUM): narrow the TOCTOU window between the
    # ancestor-symlink check inside `_probe_vault_destination` and the
    # actual write. Another process could swap `wiki/` or
    # `wiki/strategies/` to a symlink between the probe and the real
    # write. Re-checking `is_symlink()` here reduces the window from
    # "probe duration" to "microseconds between this check and
    # os.replace"; `atomic_write_bytes` refuses symlinks at the FINAL
    # file path (third layer). Fully closing this requires openat/dirfd
    # semantics which are not cross-platform; the threat model (K2Bi
    # is single-user on Keith's machine) makes the residual window
    # acceptable for MVP -- a privileged attacker who can swap the
    # dir in microseconds could do much worse.
    wiki_dir = resolved_vault / "wiki"
    if wiki_dir.is_symlink():
        raise ValueError(
            f"{wiki_dir!s} was replaced with a symlink between probe "
            f"and write; refusing to mirror (TOCTOU guard)"
        )
    if dest_dir.is_symlink():
        raise ValueError(
            f"{dest_dir!s} was replaced with a symlink between probe "
            f"and write; refusing to mirror (TOCTOU guard)"
        )
    dest = dest_dir / repo_path.name
    sf.atomic_write_bytes(dest, payload)
    return dest


# ---------- exceptions ----------


class ValidationError(ValueError):
    """Step-A validation failure. Message is surfaced to Keith as-is."""


# ---------- bear-case approval gate (Bundle 4 cycle 2 / m2.12) ----------


@dataclass(frozen=True)
class BearCaseScanResult:
    """Outcome of `scan_bear_case_for_ticker` -- mirrors the ScanResult
    shape cycle 5 will use for `scan_backtests_for_slug` (spec §3.5).
    `verdict` is the single enum the gate consumes; `reason` is empty on
    PROCEED and populated with a Keith-facing message on REFUSE.
    """

    verdict: str  # "PROCEED" | "REFUSE"
    reason: str


# Ticker format regex -- same contract as invest_bear_case.validate_symbol
# + invest_thesis. Keeps gate-side and writer-side ticker validation in
# lockstep so a value accepted by the writer is accepted by the scanner
# (and vice versa). Codex R1 HIGH -- closes the path-traversal surface
# where an order.ticker like `../../reference/foo` would otherwise make
# the scan read an arbitrary markdown file under the vault.
_SCAN_SYMBOL_RE = re.compile(r"^[A-Z0-9]+(?:\.[A-Z0-9]+)?$")


def _validate_ticker_for_scan(ticker: str) -> str | None:
    """Return None when `ticker` matches the K2Bi symbol format; a short
    error string otherwise. Used by scan_bear_case_for_ticker to refuse
    scanning for syntactically-invalid tickers BEFORE touching the
    filesystem.
    """
    if not ticker:
        return "ticker is empty"
    if not _SCAN_SYMBOL_RE.match(ticker):
        return (
            f"ticker {ticker!r} is not a valid K2Bi symbol "
            f"(expected [A-Z0-9]+(\\.[A-Z0-9]+)?)"
        )
    if not any(ch.isalpha() for ch in ticker):
        return (
            f"ticker {ticker!r} must contain at least one letter "
            f"(digits-only strings are not valid tickers)"
        )
    return None


def scan_bear_case_for_ticker(
    ticker: str,
    *,
    vault_root: Path,
    now: _dt.date | None = None,
) -> BearCaseScanResult:
    """Scan `wiki/tickers/<TICKER>.md` under `vault_root` for a fresh
    PROCEED bear-case verdict. Returns PROCEED on success or REFUSE with
    a specific reason on any gate condition (spec §3.2 + Codex cycle-2
    R1/R2/R3 hardening):

      1. Missing ticker file OR missing `bear_verdict` field.
      2. `bear-last-verified` more than `FRESH_DAYS` ago (stale) OR in
         the future (likely clock-skew / hand-edit).
      3. `bear_verdict: VETO`.
      4. Frontmatter parse error OR ticker path that escapes the vault.
      5. Partial / malformed bear-case schema (any of the 5 bear_* fields
         missing, conviction out of 0..100, wrong type).

    `bear-last-verified` in the inclusive window `[now - FRESH_DAYS, now]`
    is fresh. Values outside that window (stale OR future-dated) refuse.
    `bear_verdict: PROCEED` is accepted; `VETO` refused. Any other value
    refuses with the "run /invest bear-case" hint so drift cannot
    silently approve.

    The helper is `vault_root`-explicit so callers that compose strategy
    paths with an arbitrary vault layout (tests in particular) can steer
    the scan. Production callers (`handle_approve_strategy`) derive
    `vault_root` from the strategy file path: a strategy at
    `<root>/wiki/strategies/strategy_*.md` implies tickers at
    `<root>/wiki/tickers/<SYMBOL>.md`. Symbol + containment checks
    prevent a crafted `order.ticker` from redirecting the scan to an
    arbitrary file outside the tickers directory.
    """
    if now is None:
        now = _dt.date.today()

    # Codex R1: validate ticker format BEFORE any filesystem touch.
    format_err = _validate_ticker_for_scan(ticker)
    if format_err is not None:
        return BearCaseScanResult(
            verdict="REFUSE",
            reason=(
                f"cannot scan bear-case for invalid ticker: "
                f"{format_err}. Fix the strategy's `order.ticker` "
                f"field to a real K2Bi symbol and retry."
            ),
        )

    try:
        resolved_vault_root = vault_root.resolve(strict=False)
        tickers_dir = (vault_root / "wiki" / "tickers").resolve(strict=False)
    except OSError as exc:
        return BearCaseScanResult(
            verdict="REFUSE",
            reason=(
                f"cannot resolve vault/tickers path ({exc}); refusing "
                f"to scan bear-case for {ticker}."
            ),
        )
    # Codex round-2 R1 HIGH: the tickers_dir itself must stay under the
    # resolved vault_root. If `wiki/tickers` is a symlink pointing
    # outside the vault, a well-formed ticker filename could still clear
    # approval from a file outside the repository. Containment of the
    # ticker PATH alone is not enough -- we also need the ANCESTOR dir
    # to be inside the vault.
    try:
        tickers_dir.relative_to(resolved_vault_root)
    except ValueError:
        return BearCaseScanResult(
            verdict="REFUSE",
            reason=(
                f"wiki/tickers resolves to {tickers_dir!s}, outside "
                f"vault_root {resolved_vault_root!s}; refusing to scan."
            ),
        )

    ticker_path = vault_root / "wiki" / "tickers" / f"{ticker}.md"
    # Codex round-1 R1: defence-in-depth containment check. The symbol
    # regex above already rejects `/` and `..`; this second gate catches
    # a future regex loosening or a symlink under wiki/tickers that
    # redirects outside the intended directory.
    try:
        resolved = ticker_path.resolve(strict=False)
        resolved.relative_to(tickers_dir)
    except (ValueError, OSError):
        return BearCaseScanResult(
            verdict="REFUSE",
            reason=(
                f"ticker path {ticker_path!s} resolves outside "
                f"wiki/tickers/; refusing to scan."
            ),
        )

    if not ticker_path.exists():
        return BearCaseScanResult(
            verdict="REFUSE",
            reason=(
                f"run /invest bear-case {ticker} first; approval "
                f"requires bear-case pass (no thesis at {ticker_path})"
            ),
        )
    # Codex round-2 R3 MEDIUM: ticker_path.exists() returns True for
    # directories, sockets, and other non-regular entries. read_bytes()
    # on a directory raises IsADirectoryError (OSError). Catch that at
    # the shape gate BEFORE the read so the failure surfaces as a
    # deterministic REFUSE with guidance instead of an unhandled
    # exception bubbling out of handle_approve_strategy.
    if not ticker_path.is_file():
        return BearCaseScanResult(
            verdict="REFUSE",
            reason=(
                f"wiki/tickers/{ticker}.md is not a regular file "
                f"(directory, socket, or special entry); cannot read "
                f"bear-case. Replace with a thesis file or run "
                f"/invest thesis {ticker} first."
            ),
        )

    try:
        raw_bytes = ticker_path.read_bytes()
    except OSError as exc:
        # Permission denied, device error, etc. -- same refuse-style
        # response as the malformed-frontmatter path so Keith sees
        # guidance, not a traceback.
        return BearCaseScanResult(
            verdict="REFUSE",
            reason=(
                f"could not read wiki/tickers/{ticker}.md ({exc}); "
                f"check permissions then retry"
            ),
        )

    try:
        fm = sf.parse(raw_bytes)
    except ValueError as exc:
        return BearCaseScanResult(
            verdict="REFUSE",
            reason=(
                f"cannot parse wiki/tickers/{ticker}.md frontmatter "
                f"({exc}); fix manually then retry"
            ),
        )

    # Codex round-2 R2 HIGH: require the file to be a real thesis, not
    # just a bear-case blob. `run_bear_case` already requires
    # `thesis_score` at write time; mirror that at scan time so a
    # hand-crafted file with only bear_* fields cannot satisfy the gate.
    # Also require `symbol` to match the requested ticker so a
    # mislabelled file (thesis for AAPL stored at wiki/tickers/NVDA.md)
    # refuses rather than silently approving.
    if fm.get("thesis_score") is None:
        return BearCaseScanResult(
            verdict="REFUSE",
            reason=(
                f"wiki/tickers/{ticker}.md has no thesis_score; the "
                f"file does not look like a thesis. Run /invest "
                f"thesis {ticker} first."
            ),
        )
    # Codex round-3 HIGH: `symbol:` is REQUIRED (must be present,
    # must be a non-empty string, must byte-equal ticker). Previously
    # the check only fired on mismatched strings -- missing, null, or
    # non-string values (e.g. `symbol: 123` parsed as int) silently
    # passed. Reasonable defensive construction here: reject any shape
    # that is not "string equal to ticker".
    symbol_field = fm.get("symbol")
    if not isinstance(symbol_field, str) or symbol_field.strip() != ticker:
        return BearCaseScanResult(
            verdict="REFUSE",
            reason=(
                f"wiki/tickers/{ticker}.md must have "
                f"`symbol: {ticker}` in frontmatter to prove file "
                f"identity; got {symbol_field!r}. Rename, regenerate, "
                f"or run /invest thesis {ticker} first."
            ),
        )

    bear_verdict_raw = fm.get("bear_verdict")
    if bear_verdict_raw is None:
        return BearCaseScanResult(
            verdict="REFUSE",
            reason=(
                f"run /invest bear-case {ticker} first; approval "
                f"requires bear-case pass"
            ),
        )

    # Codex round-1 R3: enforce the full persisted schema at scan time.
    # Spec §2.2 mandates all 5 bear_* fields co-present with typed
    # values; any drift lets corrupted state satisfy the gate. Reject
    # BEFORE the verdict/freshness checks so the "schema broken" error
    # is the message Keith sees first when hand-editing goes wrong.
    schema_err = _validate_scan_bear_schema(fm, ticker)
    if schema_err is not None:
        return BearCaseScanResult(
            verdict="REFUSE",
            reason=schema_err,
        )

    # Freshness -- stale bear-cases could reflect a very different market
    # structure than today's. Future-dated values are also refused
    # (clock-skew or hand-edit). Inclusive window [now - FRESH_DAYS, now].
    last_raw = fm.get("bear-last-verified")
    last_date: _dt.date | None = None
    # Codex round-4 MEDIUM: YAML parses `2026-04-19T00:00:00Z` as
    # datetime.datetime, and `datetime.datetime` IS-A `date` in Python
    # -- so a naive `isinstance(x, date)` branch would accept it and
    # then `date - datetime` would raise TypeError, crashing the scan
    # instead of refusing cleanly. Check datetime FIRST and normalise
    # to .date() so subtraction is always date-vs-date.
    if isinstance(last_raw, _dt.datetime):
        last_date = last_raw.date()
    elif isinstance(last_raw, _dt.date):
        last_date = last_raw
    elif isinstance(last_raw, str):
        try:
            last_date = _dt.date.fromisoformat(last_raw.strip())
        except ValueError:
            last_date = None
    if last_date is None:
        return BearCaseScanResult(
            verdict="REFUSE",
            reason=(
                f"bear-last-verified missing or malformed on "
                f"wiki/tickers/{ticker}.md; run /invest bear-case "
                f"{ticker} --refresh"
            ),
        )
    days_old = (now - last_date).days
    # Codex R2: mirror invest_bear_case._is_fresh -- reject future dates
    # AND stale dates so a hand-edit stamping next year does not
    # permanently satisfy the freshness gate.
    if days_old < 0:
        return BearCaseScanResult(
            verdict="REFUSE",
            reason=(
                f"bear-last-verified on wiki/tickers/{ticker}.md is "
                f"in the future ({last_date.isoformat()}); likely a "
                f"clock-skew or hand-edit. Run /invest bear-case "
                f"{ticker} --refresh to rewrite cleanly."
            ),
        )
    if days_old > ibc.FRESH_DAYS:
        return BearCaseScanResult(
            verdict="REFUSE",
            reason=(
                f"bear-case stale ({last_date.isoformat()}); run "
                f"/invest bear-case {ticker} --refresh"
            ),
        )

    if bear_verdict_raw == "VETO":
        conviction = fm.get("bear_conviction", "unknown")
        return BearCaseScanResult(
            verdict="REFUSE",
            reason=(
                f"bear-case VETO'd this thesis (conviction "
                f"{conviction}); address top counterpoints + "
                f"re-run bear-case"
            ),
        )
    if bear_verdict_raw == "PROCEED":
        return BearCaseScanResult(verdict="PROCEED", reason="")

    return BearCaseScanResult(
        verdict="REFUSE",
        reason=(
            f"unknown bear_verdict value {bear_verdict_raw!r} on "
            f"wiki/tickers/{ticker}.md; run /invest bear-case "
            f"{ticker} --refresh"
        ),
    )


# ---------- backtest approval gate (Bundle 4 cycle 3 / m2.15) ----------


_CAPTURE_FILENAME_DATE_RE = re.compile(
    r"^(\d{4}-\d{2}-\d{2})(?:_(\d{6}))?_"
)


def _is_writer_produced_filename(name: str, slug: str) -> bool:
    """Strict check that `name` matches the writer's capture contract:

        <YYYY-MM-DD>_<slug>_backtest.md                    (bare)
        <YYYY-MM-DD>_<HHMMSS>_<slug>_backtest.md           (collision)

    HHMMSS is validated as a real wall-clock time (00..23, 00..59,
    00..59). The slug segment must match `slug` byte-exact (prevents a
    file labeled for a different slug from glob-matching via suffix
    collision).

    Codex R8 #1 HIGH: the glob `*_<slug>_backtest.md` admits many non-
    writer-produced paths (junk_<slug>_backtest.md, dates with invalid
    HHMMSS like _999999_, misspelled segments). Without strict
    filename validation, a hand-crafted file can enter the sort and
    clear approval with forged passed content. This function is the
    single seam that decides whether a file is eligible at all.
    """
    suffix = f"_{slug}_backtest.md"
    if not name.endswith(suffix):
        return False
    prefix = name[: -len(suffix)]
    # Bare form: YYYY-MM-DD.
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", prefix):
        try:
            _dt.date.fromisoformat(prefix)
        except ValueError:
            return False
        return True
    # Collision form: YYYY-MM-DD_HHMMSS.
    m = re.fullmatch(
        r"(\d{4}-\d{2}-\d{2})_(\d{2})(\d{2})(\d{2})", prefix
    )
    if not m:
        return False
    try:
        _dt.date.fromisoformat(m.group(1))
    except ValueError:
        return False
    hh, mm, ss = int(m.group(2)), int(m.group(3)), int(m.group(4))
    return 0 <= hh <= 23 and 0 <= mm <= 59 and 0 <= ss <= 59


def _parse_filename_timestamp(name: str) -> _dt.datetime | None:
    """Extract an aware datetime (UTC) from a capture filename.

    Filename shapes accepted (spec §2.5 + plan-prompt decision #3):

        <YYYY-MM-DD>_<slug>_backtest.md             -> <date>T00:00:00Z
        <YYYY-MM-DD>_<HHMMSS>_<slug>_backtest.md    -> <date>T<HH:MM:SS>Z

    Used to compare candidate captures chronologically when their
    `backtest.last_run` cannot be parsed (malformed YAML, missing
    field, etc.) -- the filename itself encodes approximate
    chronology even when lex-sort disagrees with wall-clock.

    Returns None when the filename does not match either shape.
    """
    m = _CAPTURE_FILENAME_DATE_RE.match(name)
    if not m:
        return None
    date_str = m.group(1)
    time_str = m.group(2)
    try:
        date = _dt.date.fromisoformat(date_str)
    except ValueError:
        return None
    if time_str is None:
        return _dt.datetime(
            date.year, date.month, date.day, tzinfo=_dt.timezone.utc
        )
    try:
        hh = int(time_str[0:2])
        mm = int(time_str[2:4])
        ss = int(time_str[4:6])
        return _dt.datetime(
            date.year, date.month, date.day, hh, mm, ss,
            tzinfo=_dt.timezone.utc,
        )
    except ValueError:
        return None


def _parse_last_run_timestamp(raw: Any) -> _dt.datetime | None:
    """Coerce a `backtest.last_run` value to an aware `datetime` (UTC
    if the source was naive). Returns None on anything not coercible --
    callers skip files whose last_run is missing / malformed rather
    than raising so a single bad capture cannot jam the gate.

    Accepts:
      * datetime/datetime.date (YAML typed)
      * ISO-8601 string, with `Z` suffix normalised to `+00:00`
    """
    if raw is None:
        return None
    if isinstance(raw, _dt.datetime):
        ts = raw
    elif isinstance(raw, _dt.date):
        ts = _dt.datetime(raw.year, raw.month, raw.day)
    elif isinstance(raw, str):
        text = raw.strip()
        if not text:
            return None
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            ts = _dt.datetime.fromisoformat(text)
        except ValueError:
            return None
    else:
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=_dt.timezone.utc)
    return ts


def _select_most_recent_backtest(
    files: list[Path],
) -> tuple[Path | None, dict | None, tuple[Path, str] | None]:
    """Return `(path, frontmatter, malformed_newer)` for the capture
    with the maximum `backtest.last_run` among the non-empty candidates.

    `malformed_newer` is `(path, reason)` when any candidate with a
    filename lex-greater than the selected one failed to parse or
    lacks a usable last_run. Codex R2 #3 fix: surfacing this to the
    caller forces a REFUSE rather than silently falling back to an
    older valid capture when the newest one is corrupted. That is the
    safe default -- a malformed latest run should block approval, not
    hide behind stale evidence.

    Codex R1 HIGH fix: the plan-prompt's "filename-descending =
    chronological" assumption is false on same-day collisions (bare
    `<date>_<slug>` sorts after `<date>_<HHMMSS>_<slug>` because
    digits < lowercase in ASCII). Parsing `last_run` from every
    candidate closes that surface at small cost (a few YAML parses
    per approval; typical captures-per-slug is O(1..10)).

    Returns `(None, None, None)` when NO candidate has a usable
    last_run -- the caller falls back to filename-descending[0] so
    the specific malformed-capture schema error surfaces to Keith.
    """
    parsed: list[tuple[_dt.datetime, Path, dict]] = []
    unparseable: list[tuple[Path, str]] = []
    for p in files:
        try:
            fm = sf.parse(p.read_bytes())
        except OSError as exc:
            unparseable.append((p, f"could not read ({exc})"))
            continue
        except ValueError as exc:
            unparseable.append((p, f"unparseable ({exc})"))
            continue
        bt = fm.get("backtest") if isinstance(fm, dict) else None
        if not isinstance(bt, dict):
            unparseable.append((p, "missing `backtest:` mapping"))
            continue
        ts = _parse_last_run_timestamp(bt.get("last_run"))
        if ts is None:
            unparseable.append(
                (p, "missing or malformed `last_run` timestamp")
            )
            continue
        # Codex R4 #1 HIGH: last_run is mutable YAML -- a tampered
        # capture could set a future last_run and outrank a newer
        # valid rerun. Consistency check: the parsed last_run MUST
        # stay within a tolerance of the filename-encoded timestamp.
        # Different tolerances apply to bare vs HHMMSS filenames:
        #
        #   - HHMMSS filename encodes time-of-day, so the tolerance
        #     is tight (1h -- covers clock-skew + second-boundary
        #     writes but nothing more).
        #   - Bare filename encodes date only, so the tolerance is
        #     48h (±24h for timezone offsets -- Codex R5 #1: the
        #     writer stamps UTC but a third-party-produced capture
        #     could use local time, landing in adjacent UTC dates
        #     near day boundaries).
        #
        # Outside these windows, treat as unparseable so the gate
        # refuses rather than trusting a forged value.
        _fname_match = _CAPTURE_FILENAME_DATE_RE.match(p.name)
        if _fname_match is not None:
            # Codex R6 #1 HIGH: bare filenames encode DATE only. The
            # safe check is "last_run's local calendar date (in its
            # own timezone) must equal the filename date". That
            # naturally accommodates any timezone offset without
            # allowing nearly-48h of future drift -- an older bare
            # capture with `last_run: 2026-04-20T23:00Z` has local
            # date 2026-04-20, which won't equal a filename-encoded
            # 2026-04-19. HHMMSS filenames still require tight match
            # (1h) against the encoded wall-clock.
            is_bare = _fname_match.group(2) is None
            filename_date_str = _fname_match.group(1)
            if is_bare:
                # Compare last_run's LOCAL calendar date (i.e. the
                # date seen by the wall-clock of `ts.tzinfo`) to the
                # filename date.
                ts_local_date = ts.date().isoformat()
                if ts_local_date != filename_date_str:
                    unparseable.append(
                        (
                            p,
                            f"last_run ({ts.isoformat()}) local date "
                            f"{ts_local_date} does not match filename "
                            f"date {filename_date_str}; capture appears "
                            f"tampered or filename/content mismatch"
                        )
                    )
                    continue
            else:
                # HHMMSS filename: tight 1h window against UTC value.
                filename_ts = _parse_filename_timestamp(p.name)
                if filename_ts is not None:
                    delta = abs((ts - filename_ts).total_seconds())
                    if delta > 3600:
                        unparseable.append(
                            (
                                p,
                                f"last_run ({ts.isoformat()}) diverges "
                                f"from filename timestamp "
                                f"({filename_ts.isoformat()}) by more "
                                f"than 1h; capture appears tampered or "
                                f"filename/content mismatch"
                            )
                        )
                        continue
        parsed.append((ts, p, fm))
    if not parsed:
        return None, None, None
    # Pick the last_run-max among parsed candidates.
    parsed.sort(reverse=True, key=lambda x: x[0])
    selected_ts, selected_path, selected_fm = parsed[0]
    # Check whether any unparseable file is CHRONOLOGICALLY newer
    # than the selected one. Codex R3 HIGH: filename lex-order
    # disagrees with chronology on same-day collisions (bare form
    # sorts after HHMMSS form because digits < lowercase), so
    # `path.name > selected_path.name` wrongly flagged an earlier
    # malformed first-of-day run as "newer" than the later valid
    # HHMMSS-form rerun. Fix: parse the filename's embedded date +
    # optional HHMMSS into a datetime and compare that to the
    # selected capture's last_run. Only block when the malformed
    # file was actually written AFTER the selected one.
    # Codex R7 #1 HIGH fix: compare malformed-vs-selected ordering in
    # the FILENAME DOMAIN (date + optional HHMMSS), not in UTC-instant
    # domain. Selected's `ts` is its last_run (which may live in a
    # negative-offset tz, pushing UTC instants across day boundaries
    # and creating the asymmetry R7 #1 identified). Filename keys are
    # immutable + ISO-sortable + match the writer's stamping logic.
    selected_fname_key = _filename_sort_key(selected_path.name)
    newer_malformed: tuple[Path, str] | None = None
    for path, reason in unparseable:
        path_fname_key = _filename_sort_key(path.name)
        if path_fname_key is None:
            # Filename doesn't match our scheme; be defensive and
            # treat unparseable filename prefixes as potentially
            # newer. Surface the file so Keith can inspect.
            newer_malformed = (path, reason)
            break
        if (
            selected_fname_key is not None
            and path_fname_key > selected_fname_key
        ):
            newer_malformed = (path, reason)
            break
    return selected_path, selected_fm, newer_malformed


def _filename_sort_key(name: str) -> tuple[str, str] | None:
    """Return a (date_str, hhmmss_str) tuple suitable for chronological
    comparison. HHMMSS defaults to `000000` for bare filenames, so a
    same-day HHMMSS form sorts AFTER the bare form (correct chronology
    at the filename level) and a next-day bare form sorts after a
    prior-day HHMMSS form. Returns None when the filename does not
    match the capture scheme.
    """
    m = _CAPTURE_FILENAME_DATE_RE.match(name)
    if not m:
        return None
    return (m.group(1), m.group(2) or "000000")


# Override section body expected shape (spec §3.5 LOCK):
#
#   ## Backtest Override
#
#   Backtest run: <ISO date> at `raw/backtests/YYYY-MM-DD_<slug>_backtest.md`
#   Suspicious flag reason: <copy from backtest's look_ahead_check_reason>
#   Why this is acceptable: <Keith's text, must be non-empty>
#
# The gate validates (Codex R1 HIGH #2 + R2 HIGH #1 fixes):
#   1. Section is present (heading check via sf.has_section).
#   2. Section body has a `Why this is acceptable:` line with
#      non-empty after-label text (R1).
#   3. Section body has a `Backtest run:` line naming the SELECTED
#      capture's basename (R2 -- stale overrides pointing at a
#      prior capture no longer clear a fresh suspicious run).
#   4. Section body has a `Suspicious flag reason:` line containing
#      the CURRENT look_ahead_check_reason (R2 -- reason substring
#      match, not exact match, so Keith can add context prose).
#
# This binds the override to the specific capture it was written for;
# boilerplate / stale overrides left in place for a prior run are
# rejected. Reason is substring-match (not byte-exact) so Keith can
# prefix / suffix commentary without tripping the gate.
_OVERRIDE_JUSTIFICATION_LABEL = "why this is acceptable:"
_OVERRIDE_BACKTEST_RUN_LABEL = "backtest run:"
_OVERRIDE_SUSPICIOUS_REASON_LABEL = "suspicious flag reason:"


@dataclass(frozen=True)
class _OverrideSection:
    """Parsed `## Backtest Override` section fields. `None` on any field
    means "label not found"; empty string means "label present but no
    text after it"."""

    justification: str | None
    backtest_run_line: str | None
    suspicious_reason_line: str | None


def _extract_override_section(strategy_body: str) -> _OverrideSection | None:
    """Parse the `## Backtest Override` section body into its three
    labelled lines. Returns None when the heading is absent.

    Labels are matched case-insensitively. Each label's value is the
    text on the same line AFTER the label (not subsequent lines) --
    overrides that wrap a label's value across lines are treated as
    a single-line value whose continuation is discarded. The locked
    format in spec §3.5 puts each label on its own line, so this is
    not a real limitation.
    """
    if not sf.has_section(strategy_body, _BACKTEST_OVERRIDE_HEADING):
        return None
    lines = strategy_body.splitlines()
    in_section = False
    justification: str | None = None
    backtest_run_line: str | None = None
    suspicious_reason_line: str | None = None
    justification_collector: list[str] = []
    justification_label_seen = False
    for line in lines:
        stripped = line.strip()
        stripped_low = stripped.lower()
        if not in_section:
            if stripped_low.startswith("## backtest override"):
                in_section = True
            continue
        # End section on next H2.
        if line.startswith("## ") and not stripped_low.startswith(
            "## backtest override"
        ):
            break
        # Label matching: search at the start of the line (after strip)
        # so markdown emphasis prefixes (`- `, `* `) still match.
        if (
            backtest_run_line is None
            and stripped_low.startswith(_OVERRIDE_BACKTEST_RUN_LABEL)
        ):
            backtest_run_line = stripped[
                len(_OVERRIDE_BACKTEST_RUN_LABEL):
            ].strip()
            continue
        if (
            suspicious_reason_line is None
            and stripped_low.startswith(_OVERRIDE_SUSPICIOUS_REASON_LABEL)
        ):
            suspicious_reason_line = stripped[
                len(_OVERRIDE_SUSPICIOUS_REASON_LABEL):
            ].strip()
            continue
        if (
            not justification_label_seen
            and stripped_low.startswith(_OVERRIDE_JUSTIFICATION_LABEL)
        ):
            justification_label_seen = True
            after = stripped[
                len(_OVERRIDE_JUSTIFICATION_LABEL):
            ].lstrip()
            if after:
                justification_collector.append(after)
            continue
        # Continuation lines for the justification: collect everything
        # after the label until the section ends or another H3 starts.
        if justification_label_seen:
            if line.startswith("### "):
                break
            justification_collector.append(line)
    if justification_label_seen:
        justification = "\n".join(justification_collector).strip()
    return _OverrideSection(
        justification=justification,
        backtest_run_line=backtest_run_line,
        suspicious_reason_line=suspicious_reason_line,
    )


@dataclass(frozen=True)
class BacktestScanResult:
    """Outcome of `scan_backtests_for_slug` -- mirrors the
    BearCaseScanResult shape from cycle 2 so `handle_approve_strategy`
    can consume both gates with identical plumbing. `verdict` is the
    single enum the gate consumes; `reason` is empty on PROCEED and
    populated with a Keith-facing message on REFUSE.

    Spec §3.5 LOCKED algorithm produces this outcome.
    """

    verdict: str  # "PROCEED" | "REFUSE"
    reason: str


def scan_backtests_for_slug(
    slug: str,
    *,
    vault_root: Path,
) -> BacktestScanResult:
    """Scan `<vault_root>/raw/backtests/*_<slug>_backtest.md` for a
    most-recent backtest capture + check its `look_ahead_check` field
    against the override-section rule in the strategy body.

    Spec §3.5 LOCKED algorithm (verbatim):

      1. Glob `*_<slug>_backtest.md`. Empty => HARD REFUSE.
      2. Filter out zero-byte files (defensive against interrupted
         atomic writes from the concurrency window per §2.5). All empty
         => HARD REFUSE.
      3. Filename-descending sort; pick files[0] as "most recent".
      4. Parse frontmatter. YAML error => HARD REFUSE.
      5. `look_ahead_check == "passed"` => PROCEED.
      6. `look_ahead_check == "suspicious"`: read strategy body; if
         `## Backtest Override` section present (any suffix), PROCEED;
         else REFUSE with the threshold-reason surfaced.
      7. Any other `look_ahead_check` value => HARD REFUSE (forces the
         enum to stay locked to {passed, suspicious}).

    `vault_root` is explicit for test determinism; production callers
    (`handle_approve_strategy`) resolve the vault via `resolve_vault_root`
    (override > K2BI_VAULT_ROOT env > DEFAULT_VAULT_ROOT). Q30 Session B
    replaced the prior `path.resolve().parents[2]` auto-detect; the
    gate still accepts an explicit vault_root so test fixtures can pin
    scan isolation. Symlink containment is enforced on the
    backtests directory so a crafted `wiki/backtests -> elsewhere`
    symlink cannot clear approval from a file outside the vault.

    Known limitation: on a same-day re-run collision, the locked
    filename scheme produces paths where the bare `<date>_<slug>_
    backtest.md` form sorts lexicographically AFTER the
    `<date>_HHMMSS_<slug>_backtest.md` form (digits < lowercase slug
    letters in ASCII). Filename-descending thus picks the EARLIER
    same-day run as "most recent". Fixed via `_select_most_recent_
    backtest` which uses parsed `backtest.last_run` for selection +
    filename-sort-key for malformed-newer detection.

    **Phase 4 deferral (Codex R2 #2 / R4 #2 / R9 #1):** this gate
    does NOT cross-verify the capture's `strategy_commit_sha` against
    the strategy file's current revision. That means a strategy
    edited AFTER a passed backtest can still be approved on the
    stale capture's evidence without re-running /backtest. The
    defence is operational: `/invest-ship --approve-strategy` runs
    after Keith has hand-edited + reviewed; the expectation is that
    Keith re-runs /backtest when material strategy edits happen.
    Phase 4 will add the git-log-based cross-check alongside the
    walk-forward harness (triggered by first paper trade signal or
    operational discipline gap surfacing during burn-in).
    """
    # Step 1-2 preamble: resolve + containment-check the backtests dir.
    try:
        resolved_vault_root = vault_root.resolve(strict=False)
        backtests_dir = (vault_root / "raw" / "backtests").resolve(
            strict=False
        )
    except OSError as exc:
        return BacktestScanResult(
            verdict="REFUSE",
            reason=(
                f"cannot resolve vault/backtests path ({exc}); refusing "
                f"to scan backtest for {slug}."
            ),
        )
    try:
        backtests_dir.relative_to(resolved_vault_root)
    except ValueError:
        return BacktestScanResult(
            verdict="REFUSE",
            reason=(
                f"raw/backtests resolves to {backtests_dir!s}, outside "
                f"vault_root {resolved_vault_root!s}; refusing to scan."
            ),
        )

    # MiniMax R1 #3 MEDIUM: disambiguate "vault missing" from "no
    # backtest yet". `resolve(strict=False)` silently succeeds on a
    # typoed vault_root, which would otherwise produce the same "no
    # backtest found; run /backtest" message as a legitimate first-
    # run state. That sends Keith into a loop where /backtest also
    # fails (can't write to the missing vault). Explicit vault guard.
    if not vault_root.exists():
        return BacktestScanResult(
            verdict="REFUSE",
            reason=(
                f"vault_root {vault_root!s} does not exist; check "
                f"vault path before re-running approval"
            ),
        )

    # Step 1: glob + filename-descending sort. We use pathlib.Path.glob
    # here rather than stdlib glob so test vault_roots with spaces /
    # dots behave identically to production. Sorted on the full repo-
    # relative path string so ISO date prefix drives the ordering.
    if not backtests_dir.is_dir():
        return BacktestScanResult(
            verdict="REFUSE",
            reason=(
                f"no backtest found for strategy; run "
                f"/backtest {slug} first"
            ),
        )
    # Codex R8 #1 HIGH: glob admits off-scheme filenames (e.g.
    # `junk_<slug>_backtest.md`, invalid HHMMSS like `_999999_`).
    # Filter to writer-produced filenames ONLY before any sorting or
    # parsing. Off-scheme files are ignored rather than refused: if
    # the only files for this slug are off-scheme, the gate surfaces
    # the normal "no backtest found" error since no valid capture
    # exists. Keith cleans up off-scheme files manually.
    raw_candidates = backtests_dir.glob(f"*_{slug}_backtest.md")
    candidates = sorted(
        (
            p
            for p in raw_candidates
            if _is_writer_produced_filename(p.name, slug)
        ),
        key=lambda p: p.name,
        reverse=True,
    )
    if not candidates:
        return BacktestScanResult(
            verdict="REFUSE",
            reason=(
                f"no backtest found for strategy; run "
                f"/backtest {slug} first"
            ),
        )

    # Step 2: skip zero-byte files. All-empty => refuse loudly.
    non_empty: list[Path] = []
    for p in candidates:
        try:
            if p.stat().st_size > 0:
                non_empty.append(p)
        except OSError:
            # Stat failure (race / permission) is treated as empty so
            # the next iteration picks up the next candidate. A fully-
            # unreadable directory fails below on the "none non-empty"
            # branch.
            continue
    if not non_empty:
        return BacktestScanResult(
            verdict="REFUSE",
            reason=(
                f"backtest files exist but all are empty (interrupted "
                f"writes?); re-run /backtest {slug}"
            ),
        )

    # Step 3: most recent. Codex R1 HIGH: the plan's "filename-suffix-
    # deterministic sort" assumption is empirically wrong -- on same-
    # day re-runs, the bare `<date>_<slug>` form sorts AFTER the
    # `<date>_<HHMMSS>_<slug>` form (digits < lowercase in ASCII), so
    # filename-descending picks the EARLIER run as "most recent". Fix:
    # parse `backtest.last_run` from every non-empty candidate + pick
    # the file with the maximum timestamp. Falls back to filename-
    # descending order if last_run is missing / unparseable on all
    # candidates (defensive: preserves behaviour for files written by
    # a future writer that omitted last_run).
    (
        most_recent,
        most_recent_fm,
        newer_malformed,
    ) = _select_most_recent_backtest(non_empty)
    # Codex R2 #3 HIGH: if a lex-newer capture is malformed, REFUSE
    # rather than silently selecting an older valid one. A failed or
    # tampered latest run must block approval, not fall through to
    # stale evidence.
    if newer_malformed is not None:
        p, reason = newer_malformed
        return BacktestScanResult(
            verdict="REFUSE",
            reason=(
                f"newer backtest capture {p.name} is malformed "
                f"({reason}); re-run /backtest {slug} -- a failed "
                f"or tampered latest run must not fall back to an "
                f"older valid capture"
            ),
        )
    if most_recent is None:
        # Fallback: no candidate has a valid last_run. Surface the
        # lexically-first non-empty file so the downstream schema
        # checks emit a specific malformed-capture error rather than
        # a generic "no readable" message. Keith's specific failure
        # (missing backtest: block, missing last_run, malformed YAML)
        # still surfaces via the targeted checks below.
        most_recent = non_empty[0]
        try:
            fm = sf.parse(most_recent.read_bytes())
        except (OSError, ValueError) as exc:
            return BacktestScanResult(
                verdict="REFUSE",
                reason=(
                    f"backtest {most_recent.name} unparseable "
                    f"({exc}); re-run /backtest {slug}"
                ),
            )
    else:
        fm = most_recent_fm

    backtest_block = fm.get("backtest")
    if not isinstance(backtest_block, dict):
        return BacktestScanResult(
            verdict="REFUSE",
            reason=(
                f"backtest {most_recent.name} has no `backtest:` "
                f"mapping in frontmatter; re-run /backtest {slug}"
            ),
        )
    # MiniMax R1 #1 HIGH: schema-enforce the required sub-fields so a
    # hand-crafted capture with only `look_ahead_check: passed` and no
    # metrics block cannot satisfy the gate. Mirrors the cycle-2 bear-
    # case scanner's schema discipline (Codex R3 HIGH on cycle 2). We
    # require `metrics:` to be a mapping + `last_run` to be present;
    # run_backtest always populates both, so any capture missing them
    # was not produced by the legitimate skill path.
    metrics_block = backtest_block.get("metrics")
    if not isinstance(metrics_block, dict):
        return BacktestScanResult(
            verdict="REFUSE",
            reason=(
                f"backtest {most_recent.name} has no `metrics:` "
                f"mapping in `backtest:` block; capture is malformed, "
                f"re-run /backtest {slug}"
            ),
        )
    # Codex R5 #2 HIGH: `last_run is None` catches the MISSING case
    # but NOT the unparseable-string case (e.g. `last_run: "not a
    # date"`). When the primary selection helper returns None (all
    # candidates had unparseable last_run) and the caller falls
    # back to non_empty[0], the fallback parse may succeed on the
    # frontmatter overall while last_run still holds garbage. Use
    # the same coercion helper as the selector so both paths agree
    # on what counts as a usable timestamp.
    last_run_raw = backtest_block.get("last_run")
    if (
        last_run_raw is None
        or _parse_last_run_timestamp(last_run_raw) is None
    ):
        return BacktestScanResult(
            verdict="REFUSE",
            reason=(
                f"backtest {most_recent.name} has missing or "
                f"unparseable `last_run` timestamp in `backtest:` "
                f"block (got {last_run_raw!r}); capture is malformed, "
                f"re-run /backtest {slug}"
            ),
        )
    # Codex R1 MEDIUM: cross-verify the capture's `strategy_slug`
    # frontmatter matches the requested slug. Without this, a hand-
    # crafted or copied capture renamed to match the target glob
    # could satisfy the gate while having been generated from a
    # different strategy. Writer always populates strategy_slug;
    # scan-time enforcement is defence-in-depth.
    capture_slug = fm.get("strategy_slug")
    if (
        not isinstance(capture_slug, str)
        or capture_slug.strip() != slug
    ):
        return BacktestScanResult(
            verdict="REFUSE",
            reason=(
                f"backtest {most_recent.name} has "
                f"strategy_slug={capture_slug!r}, expected {slug!r}; "
                f"capture appears to belong to a different strategy -- "
                f"re-run /backtest {slug}"
            ),
        )
    look_ahead = backtest_block.get("look_ahead_check")

    # Step 5: passed.
    if look_ahead == _BACKTEST_LOOK_AHEAD_PASSED:
        return BacktestScanResult(verdict="PROCEED", reason="")

    # Step 6: suspicious => check strategy body for the override.
    if look_ahead == _BACKTEST_LOOK_AHEAD_SUSPICIOUS:
        # Codex R6 #2 MEDIUM + R7 #2 MEDIUM: look_ahead_check_reason
        # MUST be a non-empty string when look_ahead_check is
        # suspicious. A non-string value would crash the substring-
        # match below; an empty value silently bypasses the reason-
        # binding check. Either shape defeats the gate's
        # accountability invariant (the reason should pin down WHICH
        # threshold tripped), so refuse the capture before any
        # override parsing.
        reason_val = backtest_block.get("look_ahead_check_reason")
        if not isinstance(reason_val, str):
            return BacktestScanResult(
                verdict="REFUSE",
                reason=(
                    f"backtest {most_recent.name} has look_ahead_check:"
                    f" suspicious but `look_ahead_check_reason` is "
                    f"{type(reason_val).__name__}, expected non-empty "
                    f"string; capture is malformed, re-run "
                    f"/backtest {slug}"
                ),
            )
        if not reason_val.strip():
            return BacktestScanResult(
                verdict="REFUSE",
                reason=(
                    f"backtest {most_recent.name} has look_ahead_check:"
                    f" suspicious but `look_ahead_check_reason` is "
                    f"empty; a suspicious verdict must name WHICH "
                    f"threshold tripped, so an empty reason invalidates "
                    f"the override-binding check. Re-run "
                    f"/backtest {slug}"
                ),
            )
        strategy_path = (
            vault_root / "wiki" / "strategies" / f"strategy_{slug}.md"
        )
        try:
            strategy_bytes = strategy_path.read_bytes()
        except OSError as exc:
            return BacktestScanResult(
                verdict="REFUSE",
                reason=(
                    f"backtest {most_recent.name} is suspicious but "
                    f"cannot read strategy body at {strategy_path.name} "
                    f"({exc}) to check for `## Backtest Override`"
                ),
            )
        strategy_body = sf._split_body(strategy_bytes)  # type: ignore[attr-defined]
        # Codex R1 HIGH #2 + R2 HIGH #1: require the override section
        # to (a) have a non-empty `Why this is acceptable:` line, (b)
        # reference the SELECTED capture's filename in `Backtest run:`,
        # and (c) contain the current `look_ahead_check_reason` as a
        # substring of `Suspicious flag reason:`. Stale overrides
        # written for a prior capture / prior reason are rejected.
        section = _extract_override_section(strategy_body)
        reason_text = backtest_block.get("look_ahead_check_reason") or ""
        if section is None:
            return BacktestScanResult(
                verdict="REFUSE",
                reason=(
                    f"backtest {most_recent.name} has look_ahead_check:"
                    f" suspicious ({reason_text}); add "
                    f"'## Backtest Override' section to strategy body "
                    f"with `Backtest run:`, `Suspicious flag reason:`, "
                    f"and non-empty `Why this is acceptable:` lines, "
                    f"then retry"
                ),
            )
        if section.justification is None:
            return BacktestScanResult(
                verdict="REFUSE",
                reason=(
                    f"backtest {most_recent.name} has look_ahead_check:"
                    f" suspicious ({reason_text}); strategy body's "
                    f"'## Backtest Override' section lacks a "
                    f"`Why this is acceptable:` line, then retry"
                ),
            )
        if not section.justification:
            return BacktestScanResult(
                verdict="REFUSE",
                reason=(
                    f"backtest {most_recent.name} has look_ahead_check:"
                    f" suspicious ({reason_text}); strategy body's "
                    f"'## Backtest Override' section has an empty "
                    f"`Why this is acceptable:` justification. Stale "
                    f"or unfilled overrides are rejected -- fill in a "
                    f"non-empty explanation, then retry"
                ),
            )
        # Binding check: `Backtest run:` must reference the selected
        # capture's basename so a stale override pointing at an older
        # capture cannot clear a new suspicious run. Substring match
        # (not byte-exact) so Keith can wrap the filename with backticks
        # or prefix a relative path.
        if (
            section.backtest_run_line is None
            or most_recent.name not in section.backtest_run_line
        ):
            return BacktestScanResult(
                verdict="REFUSE",
                reason=(
                    f"backtest {most_recent.name} has look_ahead_check:"
                    f" suspicious ({reason_text}); strategy body's "
                    f"'## Backtest Override' section's `Backtest run:` "
                    f"line does not reference {most_recent.name}. "
                    f"Stale overrides written for a prior capture are "
                    f"rejected -- update the override to reference the "
                    f"current capture, then retry"
                ),
            )
        # Reason-binding check: `Suspicious flag reason:` must contain
        # the current look_ahead_check_reason as a substring. Substring
        # (not exact) so Keith can add commentary like "the total_return
        # > 500% trip is because..." while still preserving the reason
        # itself as the anchor.
        if (
            reason_text
            and (
                section.suspicious_reason_line is None
                or reason_text not in section.suspicious_reason_line
            )
        ):
            return BacktestScanResult(
                verdict="REFUSE",
                reason=(
                    f"backtest {most_recent.name} has look_ahead_check:"
                    f" suspicious ({reason_text}); strategy body's "
                    f"'## Backtest Override' section's "
                    f"`Suspicious flag reason:` line does not contain "
                    f"the current reason string ({reason_text!r}). "
                    f"Stale overrides written for a different reason "
                    f"are rejected -- update the override to reference "
                    f"the current reason, then retry"
                ),
            )
        return BacktestScanResult(verdict="PROCEED", reason="")

    # Step 7: unknown enum value.
    return BacktestScanResult(
        verdict="REFUSE",
        reason=(
            f"backtest {most_recent.name} has unknown "
            f"look_ahead_check value: {look_ahead!r}"
        ),
    )


def _validate_scan_bear_schema(fm: dict[str, Any], ticker: str) -> str | None:
    """Return a REFUSE-reason string when the persisted bear-case
    frontmatter violates spec §2.2 schema. None when the on-disk shape
    is consistent enough to trust downstream checks.

    Enforces:
      - bear_conviction present, int (bool excluded), 0 <= v <= 100.
      - bear_top_counterpoints present, list of exactly 3 non-empty str.
      - bear_invalidation_scenarios present, list of 2..5 non-empty str.
      - bear-last-verified present (freshness check parses separately).

    Codex R3 HIGH -- before this check, a hand-edited file with
    `bear_verdict: PROCEED` + `bear_conviction: true` + no counterpoints
    could clear approval. Scan-time schema enforcement is the authority;
    writer-side enforcement is redundancy. Keep both.
    """
    conv = fm.get("bear_conviction")
    # `bool` is a subclass of `int` in Python -- exclude explicitly so
    # YAML `true` / `false` values do not masquerade as convictions.
    if conv is None:
        return (
            f"bear_verdict set on wiki/tickers/{ticker}.md but "
            f"bear_conviction is missing; the bear-case frontmatter "
            f"is internally inconsistent -- run /invest bear-case "
            f"{ticker} --refresh"
        )
    if not isinstance(conv, int) or isinstance(conv, bool):
        return (
            f"bear_conviction on wiki/tickers/{ticker}.md must be "
            f"an integer 0..100, got {conv!r}; run /invest bear-case "
            f"{ticker} --refresh"
        )
    if conv < 0 or conv > 100:
        return (
            f"bear_conviction on wiki/tickers/{ticker}.md must be "
            f"in [0, 100], got {conv}; run /invest bear-case "
            f"{ticker} --refresh"
        )

    cps = fm.get("bear_top_counterpoints")
    if not isinstance(cps, list) or len(cps) != 3:
        return (
            f"bear_top_counterpoints on wiki/tickers/{ticker}.md "
            f"must be a list of exactly 3 strings; run /invest "
            f"bear-case {ticker} --refresh"
        )
    for cp in cps:
        if not isinstance(cp, str) or not cp.strip():
            return (
                f"bear_top_counterpoints on wiki/tickers/{ticker}.md "
                f"contains a non-string or empty entry; run /invest "
                f"bear-case {ticker} --refresh"
            )

    scs = fm.get("bear_invalidation_scenarios")
    if not isinstance(scs, list) or not (2 <= len(scs) <= 5):
        return (
            f"bear_invalidation_scenarios on wiki/tickers/{ticker}.md "
            f"must be a list of 2..5 strings; run /invest bear-case "
            f"{ticker} --refresh"
        )
    for sc in scs:
        if not isinstance(sc, str) or not sc.strip():
            return (
                f"bear_invalidation_scenarios on "
                f"wiki/tickers/{ticker}.md contains a non-string or "
                f"empty entry; run /invest bear-case {ticker} --refresh"
            )

    if fm.get("bear-last-verified") is None:
        return (
            f"bear-last-verified missing on wiki/tickers/{ticker}.md; "
            f"run /invest bear-case {ticker} --refresh"
        )

    return None


# ---------- trailer builder (ONE seam per preemptive decision #6) ----------


def build_trailers(
    kind: str,
    transition: str,
    slug: str,
    *,
    rule: str | None = None,
    change_type: str | None = None,
) -> list[str]:
    """Build the commit-message trailer block for a transition.

    `kind`:
        "strategy" -- emits the three trailers cycle-4's commit-msg hook
                      enforces: Strategy-Transition, (Approved|Rejected|
                      Retired)-Strategy, Co-Shipped-By.
        "limits"   -- emits the four trailers from spec §5.3:
                      Limits-Transition, Approved-Limits, Config-Change,
                      Co-Shipped-By. The commit-msg hook does NOT enforce
                      the Limits-* trailers today (that lives in cycle 6);
                      the handler emits them anyway so the audit trail is
                      already present when the hook lands.

    `transition`:
        The `"<old> -> <new>"` literal expected on the first trailer line.
        cycle-4 commit-msg hook's `grep -qFx` matcher is byte-exact, so
        any deviation from this format would reject the commit.

    `slug`:
        The strategy slug (for strategy kind, `strategy_<slug>` is emitted
        on the action trailer; the hook derives slug via
        sf.derive_retire_slug at commit time and compares for parity)
        OR the limits-proposal basename-minus-prefix (for limits kind).

    `rule` / `change_type` are required when `kind == "limits"` and
    ignored otherwise.
    """
    _, _, new_status = transition.partition(" -> ")
    new_status = new_status.strip()
    if kind == "strategy":
        action_label = {
            "approved": "Approved-Strategy",
            "rejected": "Rejected-Strategy",
            "retired": "Retired-Strategy",
        }.get(new_status)
        if action_label is None:
            raise ValueError(
                f"build_trailers(kind='strategy'): unsupported target "
                f"status in transition {transition!r}"
            )
        return [
            f"Strategy-Transition: {transition}",
            f"{action_label}: strategy_{slug}",
            "Co-Shipped-By: invest-ship",
        ]
    if kind == "limits":
        if new_status != "approved":
            raise ValueError(
                f"build_trailers(kind='limits'): only proposed -> approved "
                f"is supported today, got {transition!r}"
            )
        if not rule or not change_type:
            raise ValueError(
                "build_trailers(kind='limits') requires rule and change_type"
            )
        return [
            f"Limits-Transition: {transition}",
            f"Approved-Limits: {slug}",
            f"Config-Change: {rule}:{change_type}",
            "Co-Shipped-By: invest-ship",
        ]
    raise ValueError(
        f"build_trailers: unknown kind {kind!r}; expected 'strategy' or 'limits'"
    )


# ---------- file IO helpers ----------


def capture_parent_sha(cwd: Path | None = None) -> str:
    """Run `git rev-parse --short HEAD`. Raises subprocess.CalledProcessError.

    Intentionally a thin wrapper so tests can monkey-patch it; the
    preemptive decision #5 invariant is that callers capture this BEFORE
    any staging or editing, so a shared single entry point makes the
    contract explicit.
    """
    result = subprocess.run(
        ["git", "rev-parse", "--short", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
        cwd=str(cwd) if cwd else None,
    )
    sha = result.stdout.strip()
    if not sha:
        raise RuntimeError(
            "git rev-parse --short HEAD returned empty output"
        )
    return sha


def _atomic_write_bytes(path: Path, content: bytes) -> None:
    """Write content to path atomically via tempfile + os.replace.

    Temp file lives in the same directory as target so os.replace is
    cross-filesystem-safe. Temp name starts with `.` so mailbox-style
    readers (none exist here, but the pattern mirrors pending_sync)
    would ignore it.
    """
    parent = path.parent
    parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{path.name}.tmp.",
        dir=str(parent),
    )
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, path)
    except Exception:
        try:
            tmp_path.unlink()
        except FileNotFoundError:
            pass
        raise


# ---------- frontmatter edit (preserves body byte-for-byte; flips status
#            line in-place; appends missing keys just before the closing
#            fence) ----------


def _find_fences(content: bytes) -> tuple[int, int]:
    """Return `(open_idx, close_idx)` as indexes into splitlines(keepends=True).

    Raises ValueError when the file has no fence or the fence is
    unterminated. Delegating to sf.parse first would duplicate the
    normalising step without giving us the line indexes we need for the
    status-line rewrite.
    """
    lines = content.decode("utf-8").splitlines(keepends=True)
    if not lines or lines[0].rstrip("\r\n").strip() != "---":
        raise ValueError(
            "file has no YAML frontmatter fence (first line must be `---`)"
        )
    for i in range(1, len(lines)):
        if lines[i].rstrip("\r\n").strip() == "---":
            return 0, i
    raise ValueError("unterminated YAML frontmatter (missing closing `---`)")


def _rewrite_status_line(lines: list[str], new_status: str) -> None:
    """Find the `status:` line in the frontmatter range and rewrite it.

    Preserves any indentation before `status:` (K2Bi convention has none
    at the top level, but test fixtures may vary). Raises ValueError if
    no `status:` line is present -- Step A should have caught that, so
    this is defence-in-depth.
    """
    status_re = re.compile(r"^(\s*)status:\s*\S.*$")
    for i in range(1, len(lines) - 1):
        # Stop at the closing fence.
        if lines[i].rstrip("\r\n").strip() == "---":
            break
        m = status_re.match(lines[i].rstrip("\r\n"))
        if m:
            indent = m.group(1)
            # Preserve the trailing newline (LF) that the original line
            # had; YAML frontmatter is always LF-terminated in K2Bi.
            eol = "\r\n" if lines[i].endswith("\r\n") else "\n"
            lines[i] = f"{indent}status: {new_status}{eol}"
            return
    raise ValueError("no `status:` line found in frontmatter")


def _serialize_yaml_field(key: str, value: Any) -> str:
    """Serialize a single `key: value` line, quoting as PyYAML's safe_dump
    sees fit. Trailing newline stripped; caller re-adds to match the
    existing file's line-ending style.

    safe_dump always emits proper escaping for strings containing `:`,
    `"`, `\\`, leading whitespace, YAML indicator chars, etc. Using it as
    the single source of truth for value quoting avoids hand-rolling an
    incomplete escaping routine.
    """
    dumped = yaml.safe_dump(
        {key: value}, default_flow_style=False, allow_unicode=True
    )
    return dumped.rstrip("\n")


def _append_fields_before_close(
    lines: list[str],
    close_idx: int,
    added_fields: list[tuple[str, Any]],
) -> int:
    """Insert `key: value` lines just before `lines[close_idx]`.

    Returns the new `close_idx` after the inserts (useful if caller
    stacks additional inserts). Each appended line ends with the same
    newline style the closing-fence line uses.
    """
    eol = "\r\n" if lines[close_idx].endswith("\r\n") else "\n"
    insert: list[str] = []
    for key, value in added_fields:
        insert.append(_serialize_yaml_field(key, value) + eol)
    new_lines = lines[:close_idx] + insert + lines[close_idx:]
    lines[:] = new_lines
    return close_idx + len(insert)


def _edit_frontmatter(
    content: bytes,
    *,
    new_status: str,
    added_fields: list[tuple[str, Any]],
) -> bytes:
    """Flip `status:` and append new keys before the closing fence.

    No other bytes change. Body (everything after the closing fence) is
    preserved verbatim, which is what cycle-4's pre-commit Check D
    requires for the approved -> retired transition (and is harmless for
    the proposed -> approved / proposed -> rejected transitions).
    """
    _, close_idx = _find_fences(content)
    lines = content.decode("utf-8").splitlines(keepends=True)
    _rewrite_status_line(lines, new_status)
    _append_fields_before_close(lines, close_idx, added_fields)
    return "".join(lines).encode("utf-8")


# ---------- shared validation primitives ----------


def _require_file_exists(path: Path, role: str) -> None:
    if not path.exists():
        raise ValidationError(f"{role} file does not exist: {path}")
    if not path.is_file():
        raise ValidationError(f"{role} path is not a regular file: {path}")


def _parse_strategy(path: Path) -> tuple[bytes, dict[str, Any]]:
    _require_file_exists(path, "strategy")
    try:
        content = path.read_bytes()
    except OSError as exc:
        raise ValidationError(
            f"could not read strategy file {path}: {exc}"
        ) from exc
    try:
        fm = sf.parse(content)
    except ValueError as exc:
        raise ValidationError(
            f"frontmatter parse error in {path}: {exc}"
        ) from exc
    if not fm:
        raise ValidationError(
            f"strategy file {path} has no YAML frontmatter"
        )
    return content, fm


def _parse_limits(path: Path) -> tuple[bytes, dict[str, Any], str]:
    """Return (content, frontmatter, body_text) for a limits-proposal."""
    _require_file_exists(path, "limits-proposal")
    try:
        content = path.read_bytes()
    except OSError as exc:
        raise ValidationError(
            f"could not read limits-proposal {path}: {exc}"
        ) from exc
    try:
        fm = sf.parse(content)
    except ValueError as exc:
        raise ValidationError(
            f"frontmatter parse error in {path}: {exc}"
        ) from exc
    if not fm:
        raise ValidationError(
            f"limits-proposal {path} has no YAML frontmatter"
        )
    body = sf._split_body(content)  # type: ignore[attr-defined]
    return content, fm, body


def _require_status(fm: dict[str, Any], expected: str, path: Path) -> None:
    current = sf.extract_status(fm)
    if current != expected:
        raise ValidationError(
            f"{path}: status is {current!r}, cannot run this subcommand. "
            f"Allowed starting status: {expected!r}."
        )


def _require_no_fields(
    fm: dict[str, Any], forbidden: list[str], path: Path
) -> None:
    """Every field in `forbidden` must be absent in `fm`. Used to block
    re-approval / re-rejection / re-retirement which would corrupt the
    audit trail by overwriting the first-decision timestamp + sha.
    """
    present = [f for f in forbidden if f in fm]
    if present:
        raise ValidationError(
            f"{path}: field(s) {sorted(present)} already present in "
            f"frontmatter -- this file appears to have been transitioned "
            f"before. Cannot re-apply without manual cleanup."
        )


def _relpath_for_canonical_check(path: Path) -> str:
    """Return the repo-relative POSIX path used for canonical-path matching.

    Codex R7 P1 #1: we match against the hook's canonical glob using a
    repo-relative form, so absolute paths are rebased. A `git rev-parse
    --show-toplevel` probe rebases the path when possible; on failure
    (no git context / path outside the working tree) we use the raw
    path string. The caller already refused non-existent files at
    `_require_file_exists`, so the git probe is cheap and
    almost-always successful.
    """
    posix = path.as_posix()
    if not path.is_absolute():
        return posix
    try:
        root = subprocess.check_output(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=str(path.parent),
            text=True,
            stderr=subprocess.DEVNULL,
            timeout=5,
        ).strip()
    except (subprocess.CalledProcessError, FileNotFoundError, subprocess.TimeoutExpired):
        return posix
    if not root:
        return posix
    try:
        rel = path.resolve().relative_to(Path(root).resolve())
    except ValueError:
        return posix
    return rel.as_posix()


def _validate_strategy_stem(path: Path, fm: dict[str, Any]) -> str:
    """Ensure filename stem == `strategy_<frontmatter.name>` AND the path
    matches the cycle-4 hook's canonical `wiki/strategies/strategy_*.md`
    glob. Returns the slug.

    Slug derivation matches sf.derive_retire_slug: filename stem with
    the `strategy_` prefix stripped. The engine keys sentinels +
    snapshots by this slug; hooks + helper MUST agree on the same
    derivation AND must agree on which paths get hook treatment, so a
    retire against an off-path strategy file cannot silently miss the
    sentinel write.
    """
    stem = path.stem
    name_raw = fm.get("name")
    if not isinstance(name_raw, str) or not name_raw.strip():
        raise ValidationError(
            f"{path}: frontmatter `name:` must be a non-empty string"
        )
    name = name_raw.strip()
    expected = f"strategy_{name}"
    if stem != expected:
        raise ValidationError(
            f"{path}: filename stem {stem!r} does not match frontmatter "
            f"name {name!r} (expected stem {expected!r})"
        )
    # Codex R7 P1 #1: canonical path enforcement. Step A must match
    # the hooks' glob exactly, else retire commits silently miss the
    # sentinel write (post-commit hook scans the same regex) and the
    # engine retirement gate stays open.
    rel = _relpath_for_canonical_check(path)
    if not CANONICAL_STRATEGY_PATH_RE.match(rel):
        raise ValidationError(
            f"{path}: strategy files must live at "
            f"wiki/strategies/strategy_*.md (repo-relative) so the "
            f"cycle-4 commit-msg + post-commit hooks fire on the "
            f"staged diff. Got {rel!r}. Move the file to the canonical "
            f"path before re-running this subcommand."
        )
    return sf.derive_retire_slug(str(path))


def _validate_strategy_shape(
    path: Path, fm: dict[str, Any], content: bytes
) -> None:
    """All required frontmatter fields present; `## How This Works` non-empty.

    This is cycle 5's skill-level Step A -- it catches the happy path
    before Codex review burn. Pre-commit Checks A/B/D run later as the
    adversarial gate (§6 Q5: defence in depth intentional).
    """
    missing = sorted(REQUIRED_STRATEGY_FIELDS - set(fm.keys()))
    if missing:
        raise ValidationError(
            f"{path}: missing required frontmatter fields: {missing}"
        )
    order = fm.get("order")
    if not isinstance(order, dict):
        raise ValidationError(
            f"{path}: `order:` must be a YAML mapping, got "
            f"{type(order).__name__}"
        )
    missing_order = sorted(REQUIRED_ORDER_FIELDS - set(order.keys()))
    if missing_order:
        raise ValidationError(
            f"{path}: `order:` missing required keys: {missing_order}"
        )
    how_body = sf.extract_how_this_works_body(content)
    if not how_body:
        raise ValidationError(
            f"{path}: missing or empty `## How This Works` section -- "
            f"required for strategy approval regardless of learning-stage"
        )


# ---------- limits-proposal body parsing ----------


_CHANGE_CODE_RE = re.compile(
    r"##\s*Change\s*\n+```(?:yaml)?\s*\n(.*?)\n```",
    re.DOTALL | re.IGNORECASE,
)
_PATCH_SECTION_RE = re.compile(
    r"##\s*YAML\s*Patch\s*\n+(.*?)(?=\n##\s|\Z)",
    re.DOTALL | re.IGNORECASE,
)
_CODE_FENCE_RE = re.compile(
    r"```(?:yaml)?\s*\n(.*?)\n```",
    re.DOTALL,
)


def _extract_change_block(body: str, path: Path) -> dict[str, Any]:
    """Parse the `## Change` YAML block of a limits-proposal."""
    m = _CHANGE_CODE_RE.search(body)
    if not m:
        raise ValidationError(
            f"{path}: missing `## Change` section with a fenced YAML "
            f"code block (```yaml ... ```)"
        )
    try:
        parsed = yaml.safe_load(m.group(1))
    except yaml.YAMLError as exc:
        raise ValidationError(
            f"{path}: `## Change` YAML parse error: {exc}"
        ) from exc
    if not isinstance(parsed, dict):
        raise ValidationError(
            f"{path}: `## Change` block must be a YAML mapping, "
            f"got {type(parsed).__name__}"
        )
    missing = sorted(REQUIRED_CHANGE_KEYS - set(parsed.keys()))
    if missing:
        raise ValidationError(
            f"{path}: `## Change` missing required keys: {missing}"
        )
    rule = str(parsed.get("rule", "")).strip()
    if rule not in VALID_LIMITS_RULES:
        raise ValidationError(
            f"{path}: `rule: {rule!r}` not in "
            f"{sorted(VALID_LIMITS_RULES)}"
        )
    change_type = str(parsed.get("change_type", "")).strip()
    if change_type not in VALID_CHANGE_TYPES:
        raise ValidationError(
            f"{path}: `change_type: {change_type!r}` not in "
            f"{sorted(VALID_CHANGE_TYPES)}"
        )
    return parsed


def _extract_yaml_patch(body: str, path: Path) -> tuple[str, str]:
    """Return (before_text, after_text) from the `## YAML Patch` section.

    cycle 5 --approve-limits convention: the limits-proposal MUST include
    a `## YAML Patch` section with two fenced YAML code blocks labelled
    `before:` and `after:` (on their own lines immediately before each
    fence). The handler does a simple string search-and-replace on
    `execution/validators/config.yaml`, asserting `before` appears
    exactly once. This is deterministic, comment-preserving, and avoids
    a ruamel.yaml dep for cycle 5.

    Cycle 6's invest-propose-limits MVP generates these patches;
    manual authorship is also supported.
    """
    section_match = _PATCH_SECTION_RE.search(body)
    if not section_match:
        raise ValidationError(
            f"{path}: missing `## YAML Patch` section -- required by "
            f"cycle-5 --approve-limits to perform the config.yaml edit "
            f"deterministically. Expected two fenced YAML code blocks "
            f"preceded by `before:` and `after:` lines."
        )
    section = section_match.group(1)
    # Expect exactly two fenced code blocks.
    fences = list(_CODE_FENCE_RE.finditer(section))
    if len(fences) != 2:
        raise ValidationError(
            f"{path}: `## YAML Patch` section must contain exactly two "
            f"fenced YAML code blocks (before + after); found "
            f"{len(fences)}"
        )
    # Find the `before:` / `after:` labels to identify which fence is which.
    before_idx: int | None = None
    after_idx: int | None = None
    for i, fence in enumerate(fences):
        # Walk backwards from the fence start looking for the nearest
        # non-blank line. That line must be `before:` or `after:`.
        prior = section[: fence.start()].rstrip("\r\n").splitlines()
        label = ""
        for line in reversed(prior):
            s = line.strip().lower()
            if s:
                label = s
                break
        if label.startswith("before:"):
            before_idx = i
        elif label.startswith("after:"):
            after_idx = i
    if before_idx is None or after_idx is None:
        raise ValidationError(
            f"{path}: `## YAML Patch` fences must be labelled "
            f"`before:` and `after:` (each on its own line immediately "
            f"before the fence)"
        )
    if before_idx == after_idx:
        raise ValidationError(
            f"{path}: `## YAML Patch` labels resolved to the same fence; "
            f"labels must identify distinct blocks"
        )
    return fences[before_idx].group(1), fences[after_idx].group(1)


def _apply_config_patch(
    config_path: Path, before_text: str, after_text: str
) -> tuple[str, str]:
    """Apply a single textual find-and-replace to config_path atomically.

    Returns (old_content, new_content) for audit / testing. Raises
    ValidationError if `before_text` does not appear exactly once in the
    config file.

    The substring match is EXACT including whitespace and newlines, so
    a patch that was authored against config.yaml at commit time will
    match verbatim. If the config has drifted since the patch was
    authored (e.g. Keith manually reformatted), the handler surfaces
    that cleanly via "not found" rather than performing a half-applied
    edit.
    """
    _require_file_exists(config_path, "config.yaml")
    try:
        old = config_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ValidationError(
            f"could not read {config_path}: {exc}"
        ) from exc
    occurrences = old.count(before_text)
    if occurrences == 0:
        raise ValidationError(
            f"{config_path}: `## YAML Patch` before-block not found in "
            f"config. Either the proposal is stale (config has moved "
            f"since the patch was authored) or the block differs by "
            f"whitespace/newline; author the patch against the CURRENT "
            f"config.yaml contents."
        )
    if occurrences > 1:
        raise ValidationError(
            f"{config_path}: `## YAML Patch` before-block matches "
            f"{occurrences} locations -- must be uniquely identifiable. "
            f"Author the patch with enough surrounding context to match "
            f"exactly one location."
        )
    new = old.replace(before_text, after_text, 1)
    _atomic_write_bytes(config_path, new.encode("utf-8"))
    return old, new


def _derive_limits_slug(path: Path) -> str:
    """Slug for Approved-Limits / Config-Change trailers.

    Derived from the limits-proposal filename. The filename follows
    `YYYY-MM-DD_limits-proposal_<slug>.md` (spec §2.3). We strip the
    date prefix and the `limits-proposal_` token. Falls back to the
    stem if the filename shape is unexpected so the trailer is always
    emittable (the pre-commit / commit-msg hook does not enforce
    Limits-* trailers in cycle 5 anyway; format is for audit).
    """
    stem = path.stem
    m = re.match(r"^\d{4}-\d{2}-\d{2}_limits-proposal_(.+)$", stem)
    if m:
        return m.group(1)
    return stem


# ---------- subcommand handlers ----------


@dataclass
class StrategyCommitHints:
    """Skill-body consumer contract for the three strategy subcommands.

    Keeps every field the commit-message builder needs in one place; CLI
    serializes this to JSON so bash / Bash-tool callers can parse without
    reconstructing any of it.
    """

    file: str
    slug: str
    transition: str
    commit_subject: str
    trailers: list[str]
    timestamp_field: str
    timestamp_value: str
    parent_commit_sha: str | None = None
    reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "kind": "strategy",
            "file": self.file,
            "slug": self.slug,
            "transition": self.transition,
            "commit_subject": self.commit_subject,
            "trailers": list(self.trailers),
            "timestamp_field": self.timestamp_field,
            "timestamp_value": self.timestamp_value,
        }
        if self.parent_commit_sha is not None:
            out["parent_commit_sha"] = self.parent_commit_sha
        if self.reason is not None:
            out["reason"] = self.reason
        return out


@dataclass
class LimitsCommitHints:
    file: str
    config_path: str
    slug: str
    rule: str
    change_type: str
    transition: str
    approved_at: str
    parent_commit_sha: str
    commit_subject: str
    trailers: list[str]
    config_changed: bool = True
    config_before_excerpt: str = ""
    config_after_excerpt: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": "limits",
            "file": self.file,
            "config_path": self.config_path,
            "slug": self.slug,
            "rule": self.rule,
            "change_type": self.change_type,
            "transition": self.transition,
            "approved_at": self.approved_at,
            "parent_commit_sha": self.parent_commit_sha,
            "commit_subject": self.commit_subject,
            "trailers": list(self.trailers),
            "config_changed": self.config_changed,
            "config_before_excerpt": self.config_before_excerpt,
            "config_after_excerpt": self.config_after_excerpt,
        }


def _now_iso(now: datetime | None) -> str:
    ts = now if now is not None else datetime.now(timezone.utc)
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts.astimezone(timezone.utc).isoformat(timespec="microseconds")


def _validate_reason(reason: str) -> str:
    """Reason must be non-empty, not just whitespace. Stripped result is
    what lands in the frontmatter value."""
    if not isinstance(reason, str):
        raise ValidationError(
            f"--reason must be a string, got {type(reason).__name__}"
        )
    stripped = reason.strip()
    if not stripped:
        raise ValidationError(
            "--reason must be a non-empty string (provide a short "
            "explanation of why this strategy is being transitioned)"
        )
    return stripped


def handle_approve_strategy(
    path: Path,
    *,
    parent_sha: str | None = None,
    now: datetime | None = None,
    vault_root: Path | None = None,
    today: _dt.date | None = None,
) -> StrategyCommitHints:
    """Execute Step A + Step D for `/invest-ship --approve-strategy`.

    Side effect: atomically rewrites `path` with status=approved plus the
    approved_at + approved_commit_sha fields. Returns commit hints for
    the skill body to splice into its commit message.

    Bundle 4 cycle 2 addition: the bear-case gate refuses approval unless
    the strategy's primary ticker has a fresh PROCEED verdict in
    `wiki/tickers/<TICKER>.md` (spec §3.2 + §9.3). `vault_root` overrides
    the path-derived vault for tests; `today` pins freshness comparisons
    for test determinism.
    """
    content, fm = _parse_strategy(path)
    _require_status(fm, "proposed", path)
    slug = _validate_strategy_stem(path, fm)
    _validate_strategy_shape(path, fm, content)
    _require_no_fields(
        fm, ["approved_at", "approved_commit_sha"], path
    )

    # Bundle 4 cycle 2: bear-case freshness gate. Runs AFTER syntactic
    # validation (we need order.ticker to scan) and BEFORE capture_parent_
    # sha + the atomic rewrite (so a REFUSE leaves the working tree
    # unchanged). Cycle 5 will slot scan_backtests_for_slug(slug) in
    # right after this block -- same shape, different surface.
    order = fm.get("order") or {}
    primary_ticker = order.get("ticker") if isinstance(order, dict) else None
    if not isinstance(primary_ticker, str) or not primary_ticker.strip():
        raise ValidationError(
            f"{path}: frontmatter `order.ticker` must be a non-empty "
            f"string for the bear-case gate"
        )
    primary_ticker = primary_ticker.strip()
    # Q30 Session B: vault root is a deployment fact, not a repo-relative
    # one. `resolve_vault_root` honours explicit override > K2BI_VAULT_ROOT
    # env > DEFAULT_VAULT_ROOT constant. The prior `path.resolve().parents[2]`
    # auto-detect resolved to the REPO on code-repo strategy files, so the
    # bear-case + backtest gates scanned the wrong tree (root of the Q30
    # split-brain). The approval-gate consumers below
    # (scan_bear_case_for_ticker, scan_backtests_for_slug) already took an
    # explicit `vault_root` kwarg; this is the single handler-level seam
    # that flipped from parents[2] to the resolver.
    resolved_vault = resolve_vault_root(vault_root)
    # Approval-time vault pre-flight (MiniMax R1 #2 + #4). Without this
    # guard, a misconfigured K2BI_VAULT_ROOT surfaces downstream as
    # "no thesis at /bad/path/wiki/tickers/SPY.md" from the bear-case
    # scanner -- misleading because the real issue is the vault path.
    # More importantly, if the approve commit lands and the
    # post-commit mirror phase then fails (Decision 4: logged not
    # fatal), the engine reads the CODE REPO state but the VAULT stays
    # stale until operator intervention. Pre-flight catches the
    # misconfig at the approve step so the commit never lands until
    # the vault is reachable.
    if not resolved_vault.exists():
        raise ValidationError(
            f"vault_root {resolved_vault!s} does not exist; set "
            f"K2BI_VAULT_ROOT or create the Syncthing-managed vault "
            f"directory before approving strategies (Q30 mirror "
            f"requires a live vault)"
        )
    if not resolved_vault.is_dir():
        raise ValidationError(
            f"vault_root {resolved_vault!s} is not a directory; check "
            f"K2BI_VAULT_ROOT env var for a deployment typo"
        )
    # Codex R3 #2 (HIGH): surface vault-destination permission +
    # subtree problems at approval time by running the same probe the
    # mirror will later attempt (wiki/strategies/ mkdir + tempfile
    # probe-write). A plain os.access(vault_root, W_OK) misses tighter
    # permissions on wiki/ or wiki/strategies/, letting approval
    # succeed while the post-commit mirror fails silently. This shares
    # the helper with mirror_strategy_to_vault so the two code paths
    # fail on the same conditions.
    try:
        _probe_vault_destination(resolved_vault)
    except ValueError as exc:
        raise ValidationError(
            f"vault destination pre-flight failed: {exc}"
        ) from exc
    bear_scan = scan_bear_case_for_ticker(
        primary_ticker, vault_root=resolved_vault, now=today,
    )
    if bear_scan.verdict == "REFUSE":
        raise ValidationError(bear_scan.reason)

    # Bundle 4 cycle 3: backtest sanity-gate. Per spec §3.5 LOCKED
    # algorithm. Runs AFTER the bear-case scan (so a VETO or stale-
    # bear case refuses first -- the ordering matches how Keith
    # iterates: update the thesis, re-run bear-case, re-run backtest,
    # retry approval) and BEFORE parent-sha capture so a REFUSE here
    # leaves the working tree unchanged.
    backtest_scan = scan_backtests_for_slug(
        slug, vault_root=resolved_vault,
    )
    if backtest_scan.verdict == "REFUSE":
        raise ValidationError(backtest_scan.reason)

    # Preemptive decision #5: capture parent sha FIRST, before touching
    # any file or staging. If caller already resolved it (test fixtures),
    # honour the passed value -- makes testing deterministic without
    # subprocess-patching git.
    if parent_sha is None:
        parent_sha = capture_parent_sha(cwd=path.parent)

    approved_at = _now_iso(now)
    new_content = _edit_frontmatter(
        content,
        new_status="approved",
        added_fields=[
            ("approved_at", approved_at),
            ("approved_commit_sha", parent_sha),
        ],
    )
    _atomic_write_bytes(path, new_content)

    transition = "proposed -> approved"
    trailers = build_trailers("strategy", transition, slug)
    return StrategyCommitHints(
        file=str(path),
        slug=slug,
        transition=transition,
        commit_subject=f"feat(strategy): approve {slug}",
        trailers=trailers,
        timestamp_field="approved_at",
        timestamp_value=approved_at,
        parent_commit_sha=parent_sha,
    )


def handle_reject_strategy(
    path: Path,
    reason: str,
    *,
    now: datetime | None = None,
) -> StrategyCommitHints:
    """Execute Step A + Step D for `/invest-ship --reject-strategy`.

    Rejection is a proposed -> rejected terminal transition. No Codex
    plan review is required (rejection is a decision, not a spec change;
    spec §3.2 variant note). The skill body still runs Checkpoint-2
    Codex on the diff itself later.
    """
    reason_clean = _validate_reason(reason)
    content, fm = _parse_strategy(path)
    _require_status(fm, "proposed", path)
    slug = _validate_strategy_stem(path, fm)
    # Reject doesn't require the approval-shape checks (a broken draft
    # getting rejected is a legitimate path -- catching the break at
    # propose-time was Check B's job). We only require the `status:` flip
    # to be clean and that we're not re-rejecting.
    _require_no_fields(fm, ["rejected_at", "rejected_reason"], path)

    rejected_at = _now_iso(now)
    new_content = _edit_frontmatter(
        content,
        new_status="rejected",
        added_fields=[
            ("rejected_at", rejected_at),
            ("rejected_reason", reason_clean),
        ],
    )
    _atomic_write_bytes(path, new_content)

    transition = "proposed -> rejected"
    trailers = build_trailers("strategy", transition, slug)
    return StrategyCommitHints(
        file=str(path),
        slug=slug,
        transition=transition,
        commit_subject=f"feat(strategy): reject {slug}",
        trailers=trailers,
        timestamp_field="rejected_at",
        timestamp_value=rejected_at,
        reason=reason_clean,
    )


def handle_retire_strategy(
    path: Path,
    reason: str,
    *,
    now: datetime | None = None,
) -> StrategyCommitHints:
    """Execute Step A + Step D for `/invest-ship --retire-strategy`.

    The retire transition is the ONLY staged diff cycle-4 Check D allows
    on an approved file: status flip + same-commit addition of
    `retired_at` + `retired_reason`, body + all other frontmatter keys
    byte-identical. `_edit_frontmatter` honours this by never touching
    anything outside the status line + the new field inserts.

    Cycle-4 post-commit hook auto-lands the retire sentinel when the
    commit with the Retired-Strategy trailer actually lands, so the
    sentinel is atomic with the commit (Q10 race closed).
    """
    reason_clean = _validate_reason(reason)
    content, fm = _parse_strategy(path)
    _require_status(fm, "approved", path)
    slug = _validate_strategy_stem(path, fm)
    _require_no_fields(fm, ["retired_at", "retired_reason"], path)

    retired_at = _now_iso(now)
    new_content = _edit_frontmatter(
        content,
        new_status="retired",
        added_fields=[
            ("retired_at", retired_at),
            ("retired_reason", reason_clean),
        ],
    )
    _atomic_write_bytes(path, new_content)

    transition = "approved -> retired"
    trailers = build_trailers("strategy", transition, slug)
    return StrategyCommitHints(
        file=str(path),
        slug=slug,
        transition=transition,
        commit_subject=f"feat(strategy): retire {slug}",
        trailers=trailers,
        timestamp_field="retired_at",
        timestamp_value=retired_at,
        reason=reason_clean,
    )


def handle_approve_limits(
    path: Path,
    config_path: Path | None = None,
    *,
    parent_sha: str | None = None,
    now: datetime | None = None,
) -> LimitsCommitHints:
    """Execute Step A + Step D for `/invest-ship --approve-limits`.

    Side effects (both atomic via tempfile + os.replace):
      1. Rewrite the limits-proposal at `path` with status=approved +
         approved_at + approved_commit_sha fields.
      2. Apply the proposal's `## YAML Patch` to `config_path`
         (defaults to execution/validators/config.yaml).

    Cycle-4 pre-commit Check C requires both files to appear in the same
    staged commit diff with the proposal transitioning proposed ->
    approved. The skill body stages both files after this handler runs;
    the hook enforces the atomicity invariant at commit time.

    **Single-operator invariant (Bundle 3 MVP):** this function assumes
    a single /invest-ship invocation at a time. Concurrent invocations
    on the same config.yaml can silently lose one process's patch in
    the rollback path because the rollback overwrites from in-memory
    `old_config`, not from a locked baseline. R6-minimax F2 flagged
    this; a file-lock guard is deferred to Bundle 6 when pm2-driven
    automation becomes a realistic source of concurrency. Today Keith
    runs /invest-ship from a single terminal session; violating that
    assumption is a spec gap, not an implementation bug.
    """
    resolved_config = config_path or DEFAULT_CONFIG_YAML
    if not resolved_config.is_absolute():
        resolved_config = Path.cwd() / resolved_config

    content, fm, body = _parse_limits(path)
    _require_status(fm, "proposed", path)
    # Limits-proposal shape checks (spec §2.3):
    missing_fm = sorted(REQUIRED_LIMITS_FIELDS - set(fm.keys()))
    if missing_fm:
        raise ValidationError(
            f"{path}: limits-proposal missing required frontmatter: "
            f"{missing_fm}"
        )
    if str(fm.get("type", "")).strip() != "limits-proposal":
        raise ValidationError(
            f"{path}: `type:` must be `limits-proposal`, got "
            f"{fm.get('type')!r}"
        )
    applies_to = str(fm.get("applies-to", "")).strip()
    if applies_to != "execution/validators/config.yaml":
        raise ValidationError(
            f"{path}: `applies-to:` must be "
            f"`execution/validators/config.yaml`, got {applies_to!r}"
        )
    _require_no_fields(
        fm, ["approved_at", "approved_commit_sha"], path
    )

    change = _extract_change_block(body, path)
    rule = str(change["rule"]).strip()
    change_type = str(change["change_type"]).strip()
    before_block, after_block = _extract_yaml_patch(body, path)
    if before_block == after_block:
        raise ValidationError(
            f"{path}: `## YAML Patch` before-block is identical to "
            f"after-block -- no-op edits are not allowed"
        )

    if parent_sha is None:
        parent_sha = capture_parent_sha(cwd=path.parent)
    approved_at = _now_iso(now)

    # Compute the proposal rewrite in memory BEFORE any disk mutation
    # so a frontmatter error (e.g. unreachable closing fence) is caught
    # before we touch config.yaml. Step A has already validated shape,
    # so reaching an exception here indicates a genuine defect; fail
    # before any mutation so partial state cannot result.
    new_proposal_content = _edit_frontmatter(
        content,
        new_status="approved",
        added_fields=[
            ("approved_at", approved_at),
            ("approved_commit_sha", parent_sha),
        ],
    )

    # Apply config.yaml patch. On search-and-replace failure this
    # raises ValidationError before the proposal is touched.
    old_config, new_config = _apply_config_patch(
        resolved_config, before_block, after_block
    )

    # R6-minimax F3: post-patch YAML validity check. A malformed
    # after-block (unclosed quote, bad indentation) would render
    # config.yaml unparseable and only surface when the engine next
    # loads validators -- far from the commit that introduced it.
    # Validate HERE so a bad patch bails before the proposal is
    # touched; the handler then rolls config.yaml back via the
    # try/except below.
    try:
        yaml.safe_load(new_config)
    except yaml.YAMLError as yaml_exc:
        # Roll config back before raising so the working tree stays
        # at HEAD. Same rollback pattern the proposal-write failure
        # path uses; duplicated for explicitness rather than
        # refactored into a shared helper so each failure leg is
        # auditable on its own.
        try:
            _atomic_write_bytes(
                resolved_config, old_config.encode("utf-8")
            )
        except Exception as rollback_exc:  # noqa: BLE001
            raise ValidationError(
                f"patched config.yaml would not parse as YAML "
                f"AND rollback failed; manual recovery required. "
                f"YAML error: {yaml_exc!r}. Rollback error: "
                f"{rollback_exc!r}. Restore config.yaml from git HEAD."
            ) from yaml_exc
        raise ValidationError(
            f"patched config.yaml would not parse as YAML; the "
            f"`## YAML Patch` after-block is malformed. Rollback "
            f"applied; re-author the patch against valid YAML. "
            f"Parser error: {yaml_exc!r}"
        ) from yaml_exc

    # R4-minimax F1: the previous design let a proposal write failure
    # leave config.yaml applied without a matching approved proposal
    # (partial-commit state). Guard the proposal write + roll the
    # config edit back on any exception so `handle_approve_limits`
    # either writes both files or writes neither. Git staging + the
    # commit-msg / pre-commit hooks provide a second gate at commit
    # time; this makes the on-disk state atomic from the caller's
    # perspective and avoids the "config applied, proposal still
    # proposed" ambiguity Keith would otherwise have to diagnose.
    try:
        _atomic_write_bytes(path, new_proposal_content)
    except Exception as exc:
        # R6-minimax F1: before rolling back, verify the file on disk
        # is still what we wrote. A concurrent process that modified
        # config.yaml between our patch and this rollback would have
        # its work silently overwritten; raise a clear "concurrent
        # modification" error instead of blindly restoring. Under the
        # single-operator invariant (docstring) this branch should
        # never fire, but failing loudly beats silent data loss when
        # the invariant is violated.
        try:
            current_config = resolved_config.read_text(encoding="utf-8")
        except OSError as read_exc:
            raise ValidationError(
                f"proposal write failed AND current config.yaml is "
                f"unreadable; manual recovery required. Original "
                f"error: {exc!r}. Read error: {read_exc!r}."
            ) from exc
        if current_config != new_config:
            raise ValidationError(
                f"proposal write failed, but config.yaml on disk has "
                f"diverged from the patched bytes this call wrote -- "
                f"refusing to roll back over a concurrent modification. "
                f"Manual recovery required: inspect config.yaml, decide "
                f"whether to keep the other writer's change or restore "
                f"from git HEAD, then re-run --approve-limits. Original "
                f"proposal-write error: {exc!r}"
            ) from exc
        # Rollback: restore the pre-edit config.yaml bytes. Using
        # _atomic_write_bytes again keeps the rollback itself atomic.
        try:
            _atomic_write_bytes(
                resolved_config, old_config.encode("utf-8")
            )
        except Exception as rollback_exc:  # noqa: BLE001 -- chain + surface
            raise ValidationError(
                f"proposal write failed AND config rollback failed; "
                f"manual recovery required. Original error: "
                f"{exc!r}. Rollback error: {rollback_exc!r}. "
                f"Restore config.yaml from git HEAD and re-author the "
                f"limits-proposal at status=proposed."
            ) from exc
        raise ValidationError(
            f"proposal write failed, config.yaml rolled back to pre-edit "
            f"state. Original error: {exc!r}. Re-run --approve-limits "
            f"after fixing the underlying cause."
        ) from exc

    slug = _derive_limits_slug(path)
    transition = "proposed -> approved"
    trailers = build_trailers(
        "limits", transition, slug, rule=rule, change_type=change_type
    )
    return LimitsCommitHints(
        file=str(path),
        config_path=str(resolved_config),
        slug=slug,
        rule=rule,
        change_type=change_type,
        transition=transition,
        approved_at=approved_at,
        parent_commit_sha=parent_sha,
        commit_subject=f"feat(limits): approve {slug}",
        trailers=trailers,
        config_before_excerpt=before_block,
        config_after_excerpt=after_block,
    )


# ---------- CLI ----------


def _emit_json(payload: dict[str, Any]) -> None:
    json.dump(payload, sys.stdout, indent=2, sort_keys=True)
    sys.stdout.write("\n")


def _cli_approve_strategy(args: argparse.Namespace) -> int:
    hints = handle_approve_strategy(Path(args.path))
    _emit_json(hints.to_dict())
    return 0


def _cli_reject_strategy(args: argparse.Namespace) -> int:
    hints = handle_reject_strategy(Path(args.path), args.reason)
    _emit_json(hints.to_dict())
    return 0


def _cli_retire_strategy(args: argparse.Namespace) -> int:
    hints = handle_retire_strategy(Path(args.path), args.reason)
    _emit_json(hints.to_dict())
    return 0


def _cli_approve_limits(args: argparse.Namespace) -> int:
    cfg = Path(args.config_path) if args.config_path else None
    hints = handle_approve_limits(Path(args.path), config_path=cfg)
    _emit_json(hints.to_dict())
    return 0


def _cli_build_trailers(args: argparse.Namespace) -> int:
    trailers = build_trailers(
        args.kind,
        args.transition,
        args.slug,
        rule=args.rule,
        change_type=args.change_type,
    )
    for line in trailers:
        print(line)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="invest_ship_strategy",
        description=(
            "Strategy + limits approval subcommand helpers consumed by "
            "`/invest-ship --approve-strategy|--reject-strategy|"
            "--retire-strategy|--approve-limits`."
        ),
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_approve = sub.add_parser(
        "approve-strategy",
        help="proposed -> approved on wiki/strategies/strategy_*.md",
    )
    p_approve.add_argument("path", help="strategy file path")

    p_reject = sub.add_parser(
        "reject-strategy",
        help="proposed -> rejected on wiki/strategies/strategy_*.md",
    )
    p_reject.add_argument("path", help="strategy file path")
    p_reject.add_argument(
        "--reason",
        required=True,
        help="Keith's rejection reason (stored in frontmatter + commit body)",
    )

    p_retire = sub.add_parser(
        "retire-strategy",
        help="approved -> retired on wiki/strategies/strategy_*.md",
    )
    p_retire.add_argument("path", help="strategy file path")
    p_retire.add_argument(
        "--reason",
        required=True,
        help="Keith's retirement reason (stored in frontmatter + commit body)",
    )

    p_limits = sub.add_parser(
        "approve-limits",
        help=(
            "proposed -> approved on review/strategy-approvals/"
            "*_limits-proposal_*.md AND apply the proposal's `## YAML Patch` "
            "to execution/validators/config.yaml"
        ),
    )
    p_limits.add_argument("path", help="limits-proposal file path")
    p_limits.add_argument(
        "--config-path",
        default=None,
        help=(
            "Override config.yaml path (default: "
            "execution/validators/config.yaml relative to cwd)"
        ),
    )

    p_tr = sub.add_parser(
        "build-trailers",
        help="Print the commit-message trailer block for a transition",
    )
    p_tr.add_argument(
        "--kind", required=True, choices=("strategy", "limits")
    )
    p_tr.add_argument(
        "--transition", required=True, help='e.g. "proposed -> approved"'
    )
    p_tr.add_argument(
        "--slug", required=True, help="strategy slug or limits slug"
    )
    p_tr.add_argument("--rule", default=None)
    p_tr.add_argument("--change-type", default=None)

    args = parser.parse_args(argv)

    try:
        if args.cmd == "approve-strategy":
            return _cli_approve_strategy(args)
        if args.cmd == "reject-strategy":
            return _cli_reject_strategy(args)
        if args.cmd == "retire-strategy":
            return _cli_retire_strategy(args)
        if args.cmd == "approve-limits":
            return _cli_approve_limits(args)
        if args.cmd == "build-trailers":
            return _cli_build_trailers(args)
    except ValidationError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except (subprocess.CalledProcessError, RuntimeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    # Unknown subcommand -- argparse should have caught this, but defence
    # in depth.
    parser.print_usage(sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
