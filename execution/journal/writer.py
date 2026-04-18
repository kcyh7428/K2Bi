"""Append-only JSONL decision journal writer.

Architect-approved design (2026-04-18):
    - Path:         K2Bi-Vault/raw/journal/YYYY-MM-DD.jsonl (daily rotation)
    - Concurrency:  single-writer flock on a sidecar .lock file; O_APPEND + fsync
    - Crash safety: startup scan of most-recent file; trailing partial line is
                    truncated and a recovery_truncated event is appended as the
                    first record of the recovered file (silent data loss is
                    unacceptable for a source-of-truth journal)
    - Schema:       schema v1 (see schema.py)

Hard rule per risk-controls.md:
    - Append-only. The writer exposes no `update` or `delete` method.
    - `.jsonl` files are never rewritten in place; daily rotation handles size.
"""

from __future__ import annotations

import errno
import fcntl
import json
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .schema import SCHEMA_VERSION, JournalSchemaError, validate
from .ulid import new_ulid


DEFAULT_BASE_DIR = (
    Path.home() / "Projects" / "K2Bi-Vault" / "raw" / "journal"
)


def _git_sha_short() -> str | None:
    repo_root = Path(__file__).resolve().parents[2]
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=str(repo_root),
            check=True,
            capture_output=True,
            text=True,
            timeout=2,
        )
        return out.stdout.strip() or None
    except (subprocess.CalledProcessError, FileNotFoundError, subprocess.TimeoutExpired):
        return None


class JournalWriter:
    """Single-writer append-only JSONL journal.

    Multiple JournalWriter instances on the same path are safe: each
    acquires an exclusive flock on the sidecar `.lock` file before the
    write and releases it after fsync. A process crash during the
    critical section leaves at most one partial trailing line, which is
    cleaned up by the next writer's startup scan.
    """

    def __init__(
        self,
        base_dir: Path | None = None,
        git_sha: str | None = None,
    ) -> None:
        self.base_dir = Path(base_dir) if base_dir else DEFAULT_BASE_DIR
        self.base_dir.mkdir(parents=True, exist_ok=True)
        self._git_sha = git_sha if git_sha is not None else _git_sha_short()
        self.recover_trailing_partial()

    # ---------- public API ----------

    def append(
        self,
        event_type: str,
        payload: dict[str, Any],
        *,
        strategy: str | None = None,
        trade_id: str | None = None,
        ticker: str | None = None,
        side: str | None = None,
        qty: int | None = None,
        error: dict[str, Any] | None = None,
        metadata: dict[str, Any] | None = None,
        ts: datetime | None = None,
    ) -> dict[str, Any]:
        when = ts or datetime.now(timezone.utc)
        # Normalize to UTC so daily rotation + schema contract stay consistent
        # regardless of what tz the caller hands us. A naive datetime is
        # rejected outright -- silently assuming UTC is how bugs land.
        if when.tzinfo is None:
            raise ValueError("journal ts must be timezone-aware")
        when = when.astimezone(timezone.utc)
        record: dict[str, Any] = {
            "ts": when.isoformat(timespec="microseconds"),
            "schema_version": SCHEMA_VERSION,
            "event_type": event_type,
            "trade_id": trade_id,
            "journal_entry_id": new_ulid(),
            "strategy": strategy,
            "git_sha": self._git_sha,
            "payload": payload,
        }
        if ticker is not None:
            record["ticker"] = ticker
        if side is not None:
            record["side"] = side
        if qty is not None:
            record["qty"] = qty
        if error is not None:
            record["error"] = error
        if metadata is not None:
            record["metadata"] = metadata

        validate(record)
        self._atomic_append(self._path_for(when), record)
        return record

    def path_for_today(self) -> Path:
        return self._path_for(datetime.now(timezone.utc))

    def read_all(self, when: datetime | None = None) -> list[dict[str, Any]]:
        ref = when or datetime.now(timezone.utc)
        # Symmetry with append(): reads must select the same daily file
        # the corresponding write landed in, so a non-UTC caller near
        # midnight doesn't read the wrong day.
        if ref.tzinfo is None:
            raise ValueError("journal read `when` must be timezone-aware")
        target = self._path_for(ref.astimezone(timezone.utc))
        if not target.exists():
            return []
        # Take a shared lock on the sidecar while reading so a concurrent
        # in-flight append (held under LOCK_EX) can't expose partial
        # bytes through os.write's byte-by-byte progression. LOCK_SH
        # blocks only while a writer holds LOCK_EX; multiple readers
        # coexist. The writer's existing flock path uses the same
        # sidecar, so this is the canonical synchronization point.
        lock_fd = self._acquire_shared_lock(target)
        try:
            out: list[dict[str, Any]] = []
            with target.open("r", encoding="utf-8") as f:
                for line in f:
                    line = line.rstrip("\n")
                    if not line:
                        continue
                    out.append(json.loads(line))
            return out
        finally:
            self._release_lock(lock_fd)

    def _acquire_shared_lock(self, path: Path) -> int:
        lock_path = self._lock_path_for(path)
        lock_fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o644)
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_SH)
        except Exception:
            os.close(lock_fd)
            raise
        return lock_fd

    def recover_trailing_partial(self) -> dict[str, Any] | None:
        """Scan most-recent file; truncate trailing partial line if any.

        Must hold the same sidecar flock that `_atomic_append()` uses, so
        an in-flight concurrent append cannot be misread as a crashed
        partial line and truncated. Without this, the multi-writer case
        the class docstring calls "safe" silently loses valid records.

        If a truncation occurs, a recovery_truncated event is appended
        (same lock, same file) so the loss is visible + auditable.
        Returns the recovery record or None if no recovery was needed.
        """
        latest = self._latest_file()
        if latest is None:
            return None

        lock_fd = self._acquire_lock(latest)
        try:
            try:
                with latest.open("rb") as f:
                    data = f.read()
            except FileNotFoundError:
                return None
            if not data:
                return None

            # Split on newline, keep line boundaries.
            parts = data.split(b"\n")
            # If the file ends with \n, last part is empty and all lines are complete.
            if parts[-1] == b"":
                return None

            # A missing trailing newline is ambiguous: the last record may
            # be a crashed partial, OR a COMPLETE JSON object whose
            # newline got lost on a short write. Try to parse it first.
            # If it parses, the record is valid -- append the newline
            # durably and skip recovery. Only if parse fails is this a
            # real partial that must be truncated + marked.
            last_fragment = parts[-1]
            try:
                json.loads(last_fragment.decode("utf-8"))
                # It parses: treat as a complete record just missing \n.
                # Write the trailing newline in place + fsync; no
                # recovery_truncated event (nothing was lost).
                with latest.open("ab") as f:
                    f.write(b"\n")
                    f.flush()
                    os.fsync(f.fileno())
                return None
            except (json.JSONDecodeError, UnicodeDecodeError):
                pass

            partial = last_fragment
            complete = parts[:-1]

            # Try parsing the last "complete" line to catch mid-line corruption too.
            last_complete_ok = True
            if complete:
                try:
                    json.loads(complete[-1].decode("utf-8"))
                except (json.JSONDecodeError, UnicodeDecodeError):
                    last_complete_ok = False
                    complete = complete[:-1]
                    partial = parts[-2] + b"\n" + partial

            truncated_bytes = len(partial)
            try:
                excerpt = partial.decode("utf-8", errors="replace")[:80]
            except Exception:  # pragma: no cover
                excerpt = "<decode failed>"

            detail = {
                "truncated_bytes": truncated_bytes,
                "truncated_excerpt": excerpt,
                "last_complete_line_ok": last_complete_ok,
                "recovered_file": latest.name,
            }
            now = datetime.now(timezone.utc)
            record = {
                "ts": now.isoformat(timespec="microseconds"),
                "schema_version": SCHEMA_VERSION,
                "event_type": "recovery_truncated",
                "trade_id": None,
                "journal_entry_id": new_ulid(),
                "strategy": None,
                "git_sha": self._git_sha,
                "payload": {},
                "metadata": detail,
            }
            validate(record)

            # Single atomic rename: new file = complete lines + recovery
            # marker as the final line. If the process crashes BETWEEN
            # the rename and a separate marker-append, the damaged
            # bytes are gone and the audit marker is never emitted
            # (silent loss). By baking the marker into the same tmp
            # file that we rename over the target, the post-crash
            # state is either "original with partial tail" OR "clean +
            # marker" -- never "clean without marker".
            marker_line = (
                json.dumps(record, separators=(",", ":"), ensure_ascii=False) + "\n"
            ).encode("utf-8")

            tmp_path = latest.with_suffix(latest.suffix + ".recover.tmp")
            with tmp_path.open("wb") as tmp:
                if complete:
                    tmp.write(b"\n".join(complete))
                    tmp.write(b"\n")
                tmp.write(marker_line)
                tmp.flush()
                os.fsync(tmp.fileno())
            os.replace(tmp_path, latest)
            # Parent-dir fsync so the rename is durable (same reason as
            # the kill switch's rename path).
            dir_fd = os.open(str(latest.parent), os.O_DIRECTORY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
            return record
        finally:
            self._release_lock(lock_fd)

    # ---------- internals ----------

    def _path_for(self, when: datetime) -> Path:
        return self.base_dir / f"{when.strftime('%Y-%m-%d')}.jsonl"

    def _latest_file(self) -> Path | None:
        if not self.base_dir.exists():
            return None
        candidates = sorted(self.base_dir.glob("*.jsonl"))
        return candidates[-1] if candidates else None

    @staticmethod
    def _lock_path_for(path: Path) -> Path:
        return path.with_suffix(path.suffix + ".lock")

    def _acquire_lock(self, path: Path) -> int:
        lock_path = self._lock_path_for(path)
        lock_fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o644)
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX)
        except Exception:
            os.close(lock_fd)
            raise
        return lock_fd

    @staticmethod
    def _release_lock(lock_fd: int) -> None:
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
        finally:
            os.close(lock_fd)

    def _write_record_holding_lock(self, path: Path, record: dict[str, Any]) -> None:
        """Append one record. Caller MUST already hold the sidecar lock for `path`."""
        line = json.dumps(record, separators=(",", ":"), ensure_ascii=False) + "\n"
        # Fresh-file creates also need a parent-directory fsync: on POSIX,
        # fsync of the file descriptor alone does not make the new
        # directory entry durable. Without this, a power loss after the
        # first append returns can lose the whole new daily file even
        # though we reported success.
        is_new_file = not path.exists()
        # O_APPEND gives POSIX append-atomicity even if flock were bypassed;
        # belt + suspenders.
        data_fd = os.open(
            str(path),
            os.O_CREAT | os.O_WRONLY | os.O_APPEND,
            0o644,
        )
        try:
            encoded = line.encode("utf-8")
            written = 0
            while written < len(encoded):
                n = os.write(data_fd, encoded[written:])
                if n <= 0:
                    raise IOError(
                        errno.EIO,
                        f"short write on {path} after {written}/{len(encoded)} bytes",
                    )
                written += n
            os.fsync(data_fd)
        finally:
            os.close(data_fd)
        if is_new_file:
            dir_fd = os.open(str(path.parent), os.O_DIRECTORY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)

    def _atomic_append(self, path: Path, record: dict[str, Any]) -> None:
        lock_fd = self._acquire_lock(path)
        try:
            self._write_record_holding_lock(path, record)
        finally:
            self._release_lock(lock_fd)
