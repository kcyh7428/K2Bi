"""Mailbox scanner + janitor for `.pending-sync/` entries.

Owns the durable-recovery mailbox that `/invest-ship --defer` writes to
and `/invest-sync` consumes. Extracted from the inline Python heredoc
that previously lived in `.claude/skills/invest-sync/SKILL.md` so the
scan + delete logic is:

- Testable in isolation (no git repo setup required; tests pass a tmpdir).
- Free of hardcoded category allowlists -- the valid-category set is
  loaded at runtime from `scripts/lib/deploy_config.py list-categories`,
  which is the single source of truth defined in `scripts/deploy-config.yml`.
  Closes the Bundle 3 cycle 2 propagation gap: cycle 2 pointed
  `/invest-ship` at the config helper but `/invest-sync`'s SKILL.md kept
  K2B's hardcoded `{skills, code, dashboard, scripts}` set, which
  rejected every K2Bi-native mailbox entry that used `execution` or `pm2`.

Public API (called in-process by tests + the invest-sync CLI subcommand):

    load_valid_categories(repo_root: Path) -> frozenset[str]
        Shell out to the config helper; return the sorted set of
        category names. Raises `ValueError` on helper failure so
        callers can decide fail-closed vs fail-open.

    scan_mailbox(mailbox_dir: Path, repo_root: Path) -> ScanResult
        Classify every file in `mailbox_dir`. Returns a tagged result:
        - state == "EMPTY": no mailbox, or mailbox has zero actionable
          entries.
        - state == "VALID": one or more entries passed every schema
          + category check. `valid` carries `[(filename, payload)...]`.
        - state == "UNREADABLE": at least one entry is malformed or
          references an unknown category. `unreadable` carries
          `[(filename, reason)...]`. `valid` may ALSO be populated if
          some entries were clean -- the caller decides whether to
          surface the good ones or stop on the bad ones.

    delete_processed(mailbox_dir: Path, filenames: list[str]) -> list[str]
        Remove exactly the named files (not a glob, not a rewrite);
        return a list of warnings for entries that could not be removed.
        Missing files are silent (benign: caller may retry).

CLI subcommands (for invest-sync SKILL.md to shell out):

    python3 -m scripts.lib.pending_sync scan
        Walks `$(git rev-parse --show-toplevel)/.pending-sync/` and
        prints one of three line formats to stdout:
            EMPTY
            VALID|<json-array-of-[filename,payload]>
            UNREADABLE|<json-array-of-[filename,reason]>[\nVALID|...]
        Always exits 0 -- state is encoded in stdout so a caller that
        wraps the invocation in `set -e` does not lose the error signal.

    python3 -m scripts.lib.pending_sync delete --entries '<json-array>'
        Delete the listed filenames from the mailbox. Prints one
        `WARNING: <filename>: <reason>` line per non-benign failure.
        Exits 0 regardless.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


# If a producer crashes between fsync and os.replace() the only durable
# artifact is the `.tmp_` file. Anything older than this threshold is
# treated as UNREADABLE so Keith can recover it (rename if the JSON is
# complete, delete if not) -- silently skipping forever would lose the
# defer signal entirely.
STALE_TMP_THRESHOLD_SECONDS = 60

REQUIRED_FIELDS = ("set_at", "set_by_commit", "categories", "files", "entry_id")


@dataclass
class ScanResult:
    state: str  # "EMPTY" | "VALID" | "UNREADABLE"
    valid: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    unreadable: list[tuple[str, str]] = field(default_factory=list)


def _repo_root_from(start: Path) -> Path:
    """Resolve the git repo containing `start`. Raises on non-repo."""
    result = subprocess.run(
        ["git", "-C", str(start), "rev-parse", "--show-toplevel"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise ValueError(
            f"not inside a git repo (checked from {start}): {result.stderr.strip()}"
        )
    return Path(result.stdout.strip())


def load_valid_categories(repo_root: Path) -> frozenset[str]:
    """Shell out to `deploy_config.py list-categories` and return the set.

    Single source of truth: K2Bi's K2Bi-specific category set
    (execution, pm2, scripts, skills) vs K2B's (code, dashboard,
    scripts, skills) is carried in each repo's own
    `scripts/deploy-config.yml`. Neither set is hardcoded here so
    this module works unchanged across forks.

    Raises `ValueError` on helper error -- callers decide fail-closed
    (treat every entry as UNREADABLE) vs fail-open (surface the error
    and stop). The mailbox scanner in this module chooses fail-closed:
    an unavailable config helper means the safety contract is broken
    and we must not silently accept anything.
    """
    helper = repo_root / "scripts" / "lib" / "deploy_config.py"
    if not helper.exists():
        raise ValueError(f"deploy_config helper missing at {helper}")
    result = subprocess.run(
        [sys.executable, str(helper), "list-categories"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise ValueError(
            f"deploy_config list-categories failed ({result.returncode}): "
            f"{result.stderr.strip()}"
        )
    categories = frozenset(
        line.strip() for line in result.stdout.splitlines() if line.strip()
    )
    if not categories:
        # MiniMax R1 F1: exit-0 + empty stdout can happen when
        # deploy-config.yml exists but its `targets:` list is empty
        # or has every `category:` field stripped. Treat this as a
        # helper failure rather than a silent empty allowlist,
        # otherwise every mailbox entry would be flagged "category
        # unknown" with a confusing error that buries the real cause
        # (misconfigured deploy-config.yml).
        raise ValueError(
            "deploy_config list-categories returned empty output -- "
            "scripts/deploy-config.yml likely has no categories defined"
        )
    return categories


def _classify_entry(
    name: str,
    path: Path,
    valid_categories: frozenset[str],
) -> tuple[str, dict[str, Any] | str]:
    """Return `(state, payload_or_reason)` for one mailbox file.

    States: "VALID" | "UNREADABLE" | "SKIP" (pending:false stragglers).
    The SKIP state is silent per the original heredoc's contract --
    already-processed entries are not an error, just not actionable.
    """
    try:
        with path.open("r", encoding="utf-8") as f:
            data = json.load(f)
    except json.JSONDecodeError as exc:
        return "UNREADABLE", f"json:{exc.msg}"
    except OSError as exc:
        return "UNREADABLE", f"io:{exc}"

    if not isinstance(data, dict):
        return "UNREADABLE", "schema:top-level is not an object"
    if not data.get("pending", False):
        return "SKIP", ""

    missing = [k for k in REQUIRED_FIELDS if k not in data]
    if missing:
        return "UNREADABLE", f"schema:missing {','.join(missing)}"

    cats = data.get("categories", [])
    if not isinstance(cats, list) or not cats:
        return "UNREADABLE", "schema:categories must be non-empty list"
    bad = [c for c in cats if c not in valid_categories]
    if bad:
        return "UNREADABLE", (
            f"category:unknown {','.join(bad)} "
            f"(expected subset of {sorted(valid_categories)})"
        )
    return "VALID", data


def scan_mailbox(
    mailbox_dir: Path,
    repo_root: Path,
    *,
    now: float | None = None,
    stale_tmp_threshold: float = STALE_TMP_THRESHOLD_SECONDS,
) -> ScanResult:
    """Classify every file in `mailbox_dir`. See module docstring."""
    if not mailbox_dir.is_dir():
        return ScanResult(state="EMPTY")

    try:
        valid_categories = load_valid_categories(repo_root)
    except ValueError as exc:
        # Fail-closed: without a category allowlist every entry is
        # UNREADABLE, because silently accepting would risk routing
        # mailbox entries to undefined deploy targets.
        return ScanResult(
            state="UNREADABLE",
            unreadable=[
                ("<mailbox-config>", f"deploy-config unavailable: {exc}")
            ],
        )

    now = now if now is not None else time.time()
    valid: list[tuple[str, dict[str, Any]]] = []
    unreadable: list[tuple[str, str]] = []

    for name in sorted(os.listdir(mailbox_dir)):
        entry_path = mailbox_dir / name
        if name.startswith(".tmp_"):
            try:
                age = now - entry_path.stat().st_mtime
            except OSError:
                continue
            if age > stale_tmp_threshold:
                unreadable.append(
                    (
                        name,
                        f"stale-temp:{int(age)}s old, likely crashed producer",
                    )
                )
            continue
        if not name.endswith(".json"):
            continue

        state, payload = _classify_entry(name, entry_path, valid_categories)
        if state == "VALID":
            assert isinstance(payload, dict)
            valid.append((name, payload))
        elif state == "UNREADABLE":
            assert isinstance(payload, str)
            unreadable.append((name, payload))
        # SKIP state: already-processed, not an error; ignore silently.

    if not valid and not unreadable:
        return ScanResult(state="EMPTY")
    if unreadable:
        return ScanResult(state="UNREADABLE", valid=valid, unreadable=unreadable)
    return ScanResult(state="VALID", valid=valid)


def delete_processed(mailbox_dir: Path, filenames: list[str]) -> list[str]:
    """Remove exactly the named files from the mailbox.

    Returns warnings for non-benign failures. Missing files are silent
    (benign: concurrent `/sync` may have consumed, or Keith manually
    removed). Directory iteration is NOT re-scanned -- only the
    caller-provided filenames are touched so a concurrent
    `/invest-ship --defer` write under a new filename cannot be
    clobbered.
    """
    warnings: list[str] = []
    for name in filenames:
        path = mailbox_dir / name
        try:
            path.unlink()
        except FileNotFoundError:
            continue
        except OSError as exc:
            warnings.append(f"{name}: {exc}")
    return warnings


# ---------- CLI ----------


def _cli_scan(args: argparse.Namespace) -> int:
    # Repo resolution is always from CWD (= the repo whose deploy-config
    # owns the category allowlist). Mailbox can be redirected via
    # `--mailbox` for tests without changing which config is consulted;
    # without that separation a `--mailbox /tmp/...` override would
    # look for scripts/lib/deploy_config.py inside /tmp/ and fail.
    repo = _repo_root_from(Path.cwd())
    mailbox = (
        Path(args.mailbox).resolve()
        if args.mailbox
        else repo / ".pending-sync"
    )

    result = scan_mailbox(mailbox, repo)
    if result.state == "EMPTY":
        print("EMPTY")
    elif result.state == "UNREADABLE":
        print("UNREADABLE|" + json.dumps(result.unreadable))
        if result.valid:
            print("VALID|" + json.dumps(result.valid))
    else:
        print("VALID|" + json.dumps(result.valid))
    return 0


def _cli_delete(args: argparse.Namespace) -> int:
    mailbox = (
        Path(args.mailbox).resolve()
        if args.mailbox
        else _repo_root_from(Path.cwd()) / ".pending-sync"
    )
    try:
        filenames = json.loads(args.entries)
    except json.JSONDecodeError as exc:
        print(f"WARNING: --entries JSON parse error: {exc}", file=sys.stderr)
        return 2
    if not isinstance(filenames, list) or not all(
        isinstance(f, str) for f in filenames
    ):
        print(
            "WARNING: --entries must be a JSON array of filenames",
            file=sys.stderr,
        )
        return 2
    warnings = delete_processed(mailbox, filenames)
    for w in warnings:
        print(f"WARNING: {w}", file=sys.stderr)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="pending_sync",
        description=(
            "Scan + janitor the .pending-sync/ mailbox used by "
            "/invest-ship --defer + /invest-sync."
        ),
    )
    sub = parser.add_subparsers(dest="cmd", required=True)
    scan = sub.add_parser("scan", help="walk the mailbox; print state to stdout")
    scan.add_argument(
        "--mailbox",
        default=None,
        help="mailbox path (default: $(git rev-parse --show-toplevel)/.pending-sync)",
    )
    delete = sub.add_parser(
        "delete",
        help="remove the named entries (processed by a successful sync)",
    )
    delete.add_argument(
        "--mailbox",
        default=None,
        help="mailbox path (default: $(git rev-parse --show-toplevel)/.pending-sync)",
    )
    delete.add_argument(
        "--entries",
        required=True,
        help="JSON array of mailbox filenames to delete",
    )
    args = parser.parse_args(argv)
    if args.cmd == "scan":
        return _cli_scan(args)
    if args.cmd == "delete":
        return _cli_delete(args)
    return 2


if __name__ == "__main__":
    sys.exit(main())
