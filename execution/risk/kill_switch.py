"""`.killed` lock file + per-strategy `.retired-<slug>` sentinels.

Hard-rule boundary per risk-controls.md:
    - Engine writes .killed atomically on total-drawdown breaker, manual
      Telegram kill, or deterministic audit veto.
    - Engine checks .killed on every order and every cron tick.
    - Engine NEVER deletes .killed. There is no delete function here on
      purpose. Removal is a human filesystem operation on the Mac Mini.

The per-strategy retirement sentinels (Bundle 3 m2.17, Q7) follow the
same contract, keyed by strategy slug:

    - `/invest-ship --retire-strategy` writes `.retired-<slug>` atomically
      via the cycle 4 post-commit hook (Q10) after the retire commit
      lands. Cycle 3 authors the API; cycle 4 wires the hook.
    - Engine checks `.retired-<slug>` synchronously in the submit path,
      right after the `.killed` check, closing the one-tick exposure
      window that Bundle 2's file-drift detection alone leaves open.
    - Engine NEVER deletes a sentinel. Un-retiring is a human filesystem
      operation; the architect-recommended flow is retire + new proposed
      draft (spec Q8), not sentinel deletion.

Default paths are vault-side so Syncthing replicates both classes of
state to Keith's MacBook (he can see "bot is killed" + "strategy X
retired" from Obsidian). Tests pass their own paths / base dir.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


LOG = logging.getLogger(__name__)


def _validate_slug(slug: str) -> None:
    """Raise ValueError for slugs that cannot be a filename component.

    Codex R1 + R3 + R4: intentionally permissive on content AND length.
    The loader accepts any non-empty `name:` string today and hashing
    in `_retired_path` makes the filename a fixed 25 ASCII bytes
    regardless of slug size, so a length cap here would only serve
    to reject legitimate previously-approved strategies (long names,
    case variants, non-ASCII) on every tick -- no filesystem-safety
    payoff. This check only rejects inputs that are clearly
    malformed frontmatter: non-str, empty, or containing NUL bytes
    (YAML parsers don't put NUL in string values; a NUL is a signal
    the upstream caller passed raw bytes or the field is uninitialised).
    """
    if not isinstance(slug, str):
        raise ValueError(
            f"invalid strategy slug {slug!r}: must be str, "
            f"got {type(slug).__name__}"
        )
    if not slug:
        raise ValueError("invalid strategy slug: must be non-empty")
    if "\0" in slug:
        raise ValueError(
            f"invalid strategy slug: NUL byte not allowed"
        )


DEFAULT_KILL_PATH = Path.home() / "Projects" / "K2Bi-Vault" / "System" / ".killed"

# Per-strategy retirement sentinels share the `.killed` parent directory:
# vault-side under System/ so Syncthing replicates them to the Mac Mini
# Trader tier. Sentinel files are named `.retired-<slug>`.
DEFAULT_RETIRED_DIR = Path.home() / "Projects" / "K2Bi-Vault" / "System"


def is_killed(path: Path | None = None) -> bool:
    target = path or DEFAULT_KILL_PATH
    return target.exists()


def read_kill_record(path: Path | None = None) -> dict[str, Any] | None:
    target = path or DEFAULT_KILL_PATH
    if not target.exists():
        return None
    with target.open("r", encoding="utf-8") as f:
        return json.load(f)


def write_killed(
    reason: str,
    source: str,
    detail: dict[str, Any] | None = None,
    path: Path | None = None,
) -> Path | None:
    """First-writer-wins atomic create of `.killed`.

    Once a kill record is written, its contents are immutable until a
    human deletes the file. That is the whole point of `.killed`: it is
    the record of the FIRST observed kill event. Subsequent triggers
    (the breaker re-evaluating, a manual Telegram kill landing
    concurrently with a deterministic drawdown trip) must NOT clobber
    the record -- misattributing the source/reason of the shutdown
    destroys audit evidence.

    Returns the target path on successful create, or None if `.killed`
    already existed. Callers in a race (breaker + Telegram firing at
    nearly the same time across processes) are all guaranteed exactly
    one winner.

    Implementation: write contents to a tmp file, then `os.link()` the
    tmp into the target. `link(2)` fails atomically with EEXIST if the
    target already exists -- no TOCTOU window between "is it killed"
    and "write killed".
    """
    target = path or DEFAULT_KILL_PATH
    target.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "ts": datetime.now(timezone.utc).isoformat(timespec="microseconds"),
        "reason": reason,
        "source": source,
        "detail": detail or {},
    }
    tmp_fd, tmp_name = tempfile.mkstemp(
        prefix=".killed.tmp.",
        dir=str(target.parent),
    )
    try:
        with os.fdopen(tmp_fd, "w", encoding="utf-8") as tmp:
            json.dump(record, tmp, indent=2)
            tmp.flush()
            os.fsync(tmp.fileno())
        try:
            os.link(tmp_name, str(target))
        except FileExistsError:
            # Another process won the race. Our tmp never became .killed;
            # clean it up and return None so callers do not fire another
            # kill notification.
            os.unlink(tmp_name)
            return None
        # We own .killed. Clean up the tmp's second link (contents are
        # preserved at target via the hard link we just created), then
        # fsync the parent dir so the new directory entry is durable.
        os.unlink(tmp_name)
        dir_fd = os.open(str(target.parent), os.O_DIRECTORY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    except Exception:
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass
        raise
    return target


class KillSwitchActiveError(RuntimeError):
    """Raised when the engine tries to accept an order while `.killed` is present."""

    def __init__(self, path: Path, record: dict[str, Any] | None) -> None:
        super().__init__(f"kill switch active at {path}")
        self.path = path
        self.record = record


def assert_not_killed(path: Path | None = None) -> None:
    target = path or DEFAULT_KILL_PATH
    if target.exists():
        raise KillSwitchActiveError(path=target, record=read_kill_record(target))


# ---------- per-strategy retirement sentinels (Bundle 3 m2.17, Q7) ----------


def resolve_retired_dir(
    retired_dir: Path | None,
    kill_path: Path | None,
) -> Path:
    """Canonical retired-sentinel base-dir resolution.

    Shared by the engine (reads sentinels) and the cycle-4 post-commit
    hook (writes sentinels). Both MUST call this to guarantee the hook
    writes to the dir the engine reads; a mismatch silently disables
    the retirement gate. Precedence:

    1. Explicit `retired_dir` override if set.
    2. `kill_path.parent` when kill_path is set but retired_dir is not
       -- test fixtures that scope `.killed` to a tmp dir automatically
       scope retirement sentinels to the same tmp dir.
    3. `DEFAULT_RETIRED_DIR` otherwise (vault-side default).
    """
    if retired_dir is not None:
        return retired_dir
    if kill_path is not None:
        return kill_path.parent
    return DEFAULT_RETIRED_DIR


def _retired_path(slug: str, base_dir: Path | None) -> Path:
    """Deterministic sentinel path for `slug`.

    Codex R3: filename is `.retired-<sha256-first-16-hex>` derived from
    the UTF-8 bytes of `slug`. This simultaneously closes three classes
    of bug that an encode-the-slug approach leaves open:

    1. APFS default is case-insensitive: `MeanRev` and `meanrev` would
       collide at the filesystem level and retiring one would block
       the other. The hex digest is lowercase-only, so two case-variant
       slugs hash to different digests -- no accidental collision.
    2. CJK / emoji / very long slugs blow past the 255-byte POSIX
       filename cap once URL-encoded (3x expansion), triggering
       ENAMETOOLONG inside `exists()` or `open()`. The hex digest is a
       fixed 16 bytes, so filename byte length is constant (25 bytes
       including the `.retired-` prefix) regardless of slug size.
    3. Path-separator and traversal bytes in the raw slug don't need
       to be encoded separately -- the digest has no `/` or `..`.

    Round-trip is preserved for same-slug lookups (same input hashes
    to same digest), and the human-readable slug is kept in the JSON
    record so journal replay + filesystem audit can cross-reference.
    """
    _validate_slug(slug)
    digest = hashlib.sha256(slug.encode("utf-8")).hexdigest()[:16]
    return (base_dir or DEFAULT_RETIRED_DIR) / f".retired-{digest}"


def is_strategy_retired(slug: str, base_dir: Path | None = None) -> bool:
    target = _retired_path(slug, base_dir)
    try:
        return target.exists()
    except OSError as exc:
        # R12-minimax: a `Path.exists()` that raises OSError (permission
        # denied, stale NFS handle, tmpfs unmounted mid-tick) must
        # fail-closed -- treating "cannot check" as "not retired" would
        # silently let a retired strategy trade during a vault-tier
        # outage. Mirror the same decision in assert_strategy_not_retired.
        LOG.warning(
            "retired sentinel path %s inaccessible (%s); "
            "treating as retired (fail-closed)",
            target,
            type(exc).__name__,
        )
        return True


def read_retired_record(
    slug: str, base_dir: Path | None = None
) -> dict[str, Any] | None:
    """Return the parsed sentinel record, or None if missing/unreadable.

    A malformed sentinel (manual edit, disk corruption, etc.) returns
    None rather than raising -- callers use the sentinel's EXISTENCE
    as the primary gate signal; the record's contents are for
    journaling + audit. Swallowing parse errors here means
    assert_strategy_not_retired still fail-closes on the `exists()`
    check and raises StrategyRetiredError with `record=None`, which
    the engine journals cleanly rather than crashing the tick on a
    JSONDecodeError propagating through _process_strategies.
    """
    target = _retired_path(slug, base_dir)
    if not target.exists():
        return None
    try:
        with target.open("r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError) as exc:
        # R5-minimax: surface corruption via module logger so disk
        # monitoring can see it, even though callers get None and the
        # gate still fail-closes via the exists() path above.
        LOG.warning(
            "retired sentinel %s exists but is unreadable (%s); "
            "treating as record=None -- gate stays closed via file-exists check",
            target,
            type(exc).__name__,
        )
        return None


def write_retired(
    slug: str,
    reason: str,
    commit_sha: str,
    base_dir: Path | None = None,
) -> Path | None:
    """First-writer-wins atomic create of `.retired-<slug>`.

    Mirrors `write_killed` semantics: tempfile + `os.link()` for
    atomic first-writer-wins, parent-dir fsync for durability, None
    return if another writer beat us. Cycle 3 exposes this for cycle
    4's post-commit hook + for tests to drive synthetic sentinels.
    `/invest-ship --retire-strategy` never calls this directly in
    cycle 3 -- it edits frontmatter and stages the commit; cycle 4's
    `.githooks/post-commit` grep's the landed commit's `Retired-Strategy:
    strategy_<slug>` trailer and invokes write_retired at that point
    (spec §4.3 + Q10).

    `source` is hardcoded to `"invest-ship --retire-strategy"` since
    retirement can only originate from that subcommand; the commit_sha
    ties the record to the exact commit that retired the strategy so
    post-mortem replay can git-show that commit's diff.

    Cycle 4 coupling (R1-minimax round 3): the post-commit hook MUST
    call `resolve_retired_dir()` to derive the base_dir, passing the
    same config values (`engine.retired_dir`, `engine.kill_path`) the
    engine reads from `execution/validators/config.yaml`. If the hook
    hardcodes a path instead, the sentinel lands where the engine is
    not looking and the retirement gate silently stays open. Cycle 4
    ships an integration test asserting hook-write-path ==
    engine-read-path across all three resolver branches.
    """
    target = _retired_path(slug, base_dir)
    target.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "ts": datetime.now(timezone.utc).isoformat(timespec="microseconds"),
        "reason": reason,
        "source": "invest-ship --retire-strategy",
        "slug": slug,
        "commit_sha": commit_sha,
    }
    # The tempfile prefix must not contain path separators -- use the
    # already-URL-encoded final filename (target.name) as the prefix
    # root so a slug like `foo/bar` cannot leak `/` into mkstemp's dir.
    tmp_fd, tmp_name = tempfile.mkstemp(
        prefix=f"{target.name}.tmp.",
        dir=str(target.parent),
    )
    try:
        with os.fdopen(tmp_fd, "w", encoding="utf-8") as tmp:
            json.dump(record, tmp, indent=2)
            tmp.flush()
            os.fsync(tmp.fileno())
        try:
            os.link(tmp_name, str(target))
        except FileExistsError:
            os.unlink(tmp_name)
            return None
        os.unlink(tmp_name)
        dir_fd = os.open(str(target.parent), os.O_DIRECTORY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    except Exception:
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass
        raise
    return target


class StrategyRetiredError(RuntimeError):
    """Raised when the engine tries to submit an order for a retired strategy."""

    def __init__(
        self,
        strategy_slug: str,
        path: Path,
        record: dict[str, Any] | None,
    ) -> None:
        super().__init__(f"strategy {strategy_slug!r} retired at {path}")
        self.strategy_slug = strategy_slug
        self.path = path
        self.record = record


def assert_strategy_not_retired(
    slug: str, base_dir: Path | None = None
) -> None:
    target = _retired_path(slug, base_dir)
    try:
        exists = target.exists()
    except OSError as exc:
        # R12-minimax: fail-closed on FS inaccessibility (see
        # `is_strategy_retired` for the rationale). Raise
        # StrategyRetiredError with a synthetic record that flags the
        # inaccessibility so journal replay can distinguish it from
        # a legitimate retirement.
        LOG.warning(
            "retired sentinel path %s inaccessible (%s); "
            "fail-closing the submit gate",
            target,
            type(exc).__name__,
        )
        raise StrategyRetiredError(
            strategy_slug=slug,
            path=target,
            record={
                "error": "base_dir_inaccessible",
                "error_class": type(exc).__name__,
                "error_message": str(exc),
            },
        )
    if exists:
        raise StrategyRetiredError(
            strategy_slug=slug,
            path=target,
            record=read_retired_record(slug, base_dir),
        )
