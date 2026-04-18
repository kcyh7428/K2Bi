"""`.killed` lock file.

Hard-rule boundary per risk-controls.md:
    - Engine writes .killed atomically on total-drawdown breaker, manual
      Telegram kill, or deterministic audit veto.
    - Engine checks .killed on every order and every cron tick.
    - Engine NEVER deletes .killed. There is no delete function here on
      purpose. Removal is a human filesystem operation on the Mac Mini.

Default path is vault-side so Syncthing replicates kill state to Keith's
MacBook (he can see "bot is killed" from Obsidian). Tests pass their own
path.
"""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


DEFAULT_KILL_PATH = Path.home() / "Projects" / "K2Bi-Vault" / "System" / ".killed"


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
