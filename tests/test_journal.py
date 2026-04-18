"""Unit tests for m2.7 decision journal writer.

Verification per milestones.md#2.7:
    "Concurrent writes don't corrupt; restart preserves prior entries;
     JSONL format valid"

Plus architect addenda (2026-04-18 greenlight):
    - schema_version, trade_id, journal_entry_id, git_sha present
    - error + metadata optional fields accepted
    - recovery_truncated event emitted on startup when trailing partial
      line is truncated (silent-loss prevention)
"""

from __future__ import annotations

import concurrent.futures
import json
import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from execution.journal.schema import EVENT_TYPES, SCHEMA_VERSION, JournalSchemaError, validate
from execution.journal.ulid import new_ulid
from execution.journal.writer import JournalWriter


FIXED_GIT_SHA = "abc1234"


class SchemaTests(unittest.TestCase):
    def test_valid_minimal_record_passes(self):
        rec = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "schema_version": SCHEMA_VERSION,
            "event_type": "order_submitted",
            "trade_id": new_ulid(),
            "journal_entry_id": new_ulid(),
            "strategy": "spy-rotational",
            "git_sha": FIXED_GIT_SHA,
            "payload": {"ticker": "SPY", "qty": 10},
        }
        validate(rec)

    def test_missing_required_raises(self):
        rec = {"event_type": "order_submitted"}
        with self.assertRaises(JournalSchemaError):
            validate(rec)

    def test_unknown_event_type_raises(self):
        rec = {
            "ts": "2026-04-18T12:00:00+00:00",
            "schema_version": SCHEMA_VERSION,
            "event_type": "does_not_exist",
            "trade_id": None,
            "journal_entry_id": new_ulid(),
            "strategy": None,
            "git_sha": None,
            "payload": {},
        }
        with self.assertRaises(JournalSchemaError):
            validate(rec)

    def test_event_type_enum_includes_architect_additions(self):
        self.assertIn("recovery_truncated", EVENT_TYPES)
        self.assertIn("kill_switch_cleared", EVENT_TYPES)


class WriterBasicsTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.base = Path(self._tmp.name)
        self.writer = JournalWriter(base_dir=self.base, git_sha=FIXED_GIT_SHA)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_append_writes_one_valid_jsonl_line(self):
        self.writer.append(
            event_type="order_submitted",
            payload={"note": "hello"},
            strategy="spy-rotational",
            trade_id=new_ulid(),
            ticker="SPY",
            side="buy",
            qty=10,
        )
        path = self.writer.path_for_today()
        self.assertTrue(path.exists())
        with path.open("r", encoding="utf-8") as f:
            lines = f.read().splitlines()
        self.assertEqual(len(lines), 1)
        record = json.loads(lines[0])
        self.assertEqual(record["event_type"], "order_submitted")
        self.assertEqual(record["schema_version"], SCHEMA_VERSION)
        self.assertEqual(record["git_sha"], FIXED_GIT_SHA)
        self.assertEqual(record["ticker"], "SPY")
        self.assertIsNotNone(record["journal_entry_id"])

    def test_optional_error_and_metadata_round_trip(self):
        self.writer.append(
            event_type="validator_reject",
            payload={"rule": "position_size"},
            error={"code": "position_size_exceeded", "message": "too big", "traceback_excerpt": ""},
            metadata={"request_id": "r-123"},
        )
        records = self.writer.read_all()
        self.assertEqual(records[0]["error"]["code"], "position_size_exceeded")
        self.assertEqual(records[0]["metadata"]["request_id"], "r-123")

    def test_new_file_triggers_parent_directory_fsync(self):
        # Codex round 6 P1: creating a fresh daily file must fsync the
        # parent directory so the new entry is durable on crash. We can
        # observe this indirectly via a monkeypatched os.fsync that
        # records calls; the first append on a brand-new file must
        # fsync BOTH the fd and the parent dir, while a subsequent
        # append on the same file only fsyncs the fd.
        import os as _os

        target = _os
        observed: list[str] = []
        real_fsync = target.fsync

        def wrapped(fd: int) -> None:
            try:
                st = _os.fstat(fd)
                if st.st_mode & 0o040000:  # S_IFDIR
                    observed.append("dir")
                else:
                    observed.append("file")
            except OSError:
                observed.append("unknown")
            return real_fsync(fd)

        target.fsync = wrapped  # type: ignore[assignment]
        try:
            w = JournalWriter(base_dir=self.base, git_sha=FIXED_GIT_SHA)
            observed.clear()
            # First append: brand-new file today -> expect 'file' and 'dir'
            w.append(event_type="order_submitted", payload={"n": 1})
            self.assertIn("dir", observed, f"first append observed: {observed}")
            observed.clear()
            # Second append on same file: only file-fsync, no dir-fsync
            w.append(event_type="order_submitted", payload={"n": 2})
            self.assertNotIn("dir", observed, f"second append observed: {observed}")
        finally:
            target.fsync = real_fsync  # type: ignore[assignment]

    def test_daily_rotation(self):
        day1 = datetime(2026, 4, 17, 12, 0, tzinfo=timezone.utc)
        day2 = datetime(2026, 4, 18, 12, 0, tzinfo=timezone.utc)
        self.writer.append(
            event_type="order_submitted", payload={}, ts=day1
        )
        self.writer.append(
            event_type="order_submitted", payload={}, ts=day2
        )
        self.assertTrue((self.base / "2026-04-17.jsonl").exists())
        self.assertTrue((self.base / "2026-04-18.jsonl").exists())


class ConcurrencyTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.base = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_reads_block_on_in_flight_writes(self):
        # Codex round 10 P2: read_all must take the shared lock so an
        # in-flight append cannot expose partial bytes. With many
        # interleaved readers + writers, every parsed line must be
        # complete JSON; no intermittent parse failures allowed.
        import concurrent.futures

        N_WRITERS = 2
        N_READERS = 4
        PER_WRITER = 100

        def _writer(wid: int) -> None:
            w = JournalWriter(base_dir=self.base, git_sha=FIXED_GIT_SHA)
            for i in range(PER_WRITER):
                w.append(event_type="validator_pass", payload={"w": wid, "i": i})

        def _reader(rid: int) -> int:
            r = JournalWriter(base_dir=self.base, git_sha=FIXED_GIT_SHA)
            last_len = 0
            # Keep reading until writers are done (writers eventually win);
            # any exception means the lock didn't protect us.
            for _ in range(200):
                last_len = len(r.read_all())
            return last_len

        with concurrent.futures.ThreadPoolExecutor(max_workers=N_WRITERS + N_READERS) as ex:
            writers = [ex.submit(_writer, i) for i in range(N_WRITERS)]
            readers = [ex.submit(_reader, i) for i in range(N_READERS)]
            for f in writers + readers:
                f.result()

        final = JournalWriter(base_dir=self.base, git_sha=FIXED_GIT_SHA).read_all()
        self.assertEqual(len(final), N_WRITERS * PER_WRITER)

    def test_concurrent_writes_do_not_corrupt(self):
        """Hammer a single journal file from many threads across multiple
        writer instances; every line must parse as JSON + every record
        present."""

        N_WRITERS = 4
        PER_WRITER = 50
        writers = [JournalWriter(base_dir=self.base, git_sha=FIXED_GIT_SHA) for _ in range(N_WRITERS)]

        def _write(w: JournalWriter, idx: int) -> None:
            for i in range(PER_WRITER):
                w.append(
                    event_type="validator_pass",
                    payload={"writer": idx, "i": i},
                    strategy="spy-rotational",
                )

        with concurrent.futures.ThreadPoolExecutor(max_workers=N_WRITERS) as ex:
            futures = [ex.submit(_write, w, i) for i, w in enumerate(writers)]
            for f in futures:
                f.result()

        path = writers[0].path_for_today()
        with path.open("r", encoding="utf-8") as f:
            lines = f.read().splitlines()

        self.assertEqual(len(lines), N_WRITERS * PER_WRITER)
        ids = set()
        for line in lines:
            record = json.loads(line)  # must parse
            self.assertEqual(record["event_type"], "validator_pass")
            ids.add(record["journal_entry_id"])
        self.assertEqual(len(ids), N_WRITERS * PER_WRITER, "duplicate journal_entry_id")


class RestartRecoveryTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.base = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _write_today(self, writer: JournalWriter) -> Path:
        writer.append(event_type="order_submitted", payload={"n": 1})
        writer.append(event_type="order_filled", payload={"n": 2})
        return writer.path_for_today()

    def test_clean_restart_preserves_prior_entries(self):
        w1 = JournalWriter(base_dir=self.base, git_sha=FIXED_GIT_SHA)
        path = self._write_today(w1)
        original = path.read_bytes()

        w2 = JournalWriter(base_dir=self.base, git_sha=FIXED_GIT_SHA)
        after = path.read_bytes()
        self.assertEqual(original, after, "clean restart must not modify the journal")

        records = w2.read_all()
        self.assertEqual(len(records), 2)

    def test_trailing_partial_line_truncated_and_marked(self):
        w1 = JournalWriter(base_dir=self.base, git_sha=FIXED_GIT_SHA)
        path = self._write_today(w1)
        with path.open("ab") as f:
            # Simulate a crash mid-write: UNPARSEABLE partial JSON with
            # no terminating newline (a real crashed record, distinct
            # from the complete-but-missing-newline case).
            f.write(b'{"ts":"2026-04-18T12:34:56.000000+00:00","schema_version":1,"event_type":"order')
            f.flush()
            os.fsync(f.fileno())

        # Simulate restart.
        w2 = JournalWriter(base_dir=self.base, git_sha=FIXED_GIT_SHA)
        records = w2.read_all()

        # Expect the two original records + one recovery_truncated marker.
        self.assertEqual(len(records), 3)
        self.assertEqual(records[0]["event_type"], "order_submitted")
        self.assertEqual(records[1]["event_type"], "order_filled")
        self.assertEqual(records[2]["event_type"], "recovery_truncated")
        self.assertGreater(records[2]["metadata"]["truncated_bytes"], 0)
        self.assertIn("order", records[2]["metadata"]["truncated_excerpt"])

    def test_complete_record_missing_trailing_newline_is_preserved(self):
        # Codex round 9 P1: a crash that loses only the trailing \n on a
        # successfully-written record previously caused recovery to
        # truncate the VALID record. The fix parses the last fragment
        # first: if it's valid JSON, append \n and keep the data; only
        # if it fails to parse do we treat it as a crashed partial.
        w1 = JournalWriter(base_dir=self.base, git_sha=FIXED_GIT_SHA)
        self._write_today(w1)
        path = w1.path_for_today()

        # Strip the trailing newline off the last record to simulate a
        # newline-lost crash state.
        data = path.read_bytes()
        self.assertTrue(data.endswith(b"\n"))
        path.write_bytes(data[:-1])

        # Simulate restart.
        w2 = JournalWriter(base_dir=self.base, git_sha=FIXED_GIT_SHA)
        records = w2.read_all()

        # Two original records preserved, NO recovery_truncated appended.
        self.assertEqual(len(records), 2)
        for r in records:
            self.assertNotEqual(r["event_type"], "recovery_truncated")
        # Codex round 8 P1: if os.replace lands before the recovery
        # marker is appended, a crash in the gap = silent data loss
        # (clean file, no marker). The fix bakes the marker into the
        # same tmp file that's renamed, so at rename time either the
        # original-with-partial-tail OR the clean+marker state is on
        # disk -- never clean-without-marker. We can inspect the tmp
        # file's contents right before rename to prove the marker is
        # present.
        w1 = JournalWriter(base_dir=self.base, git_sha=FIXED_GIT_SHA)
        path = self._write_today(w1)
        with path.open("ab") as f:
            f.write(b'{"ts":"2026-04-18T12:34:56.000000+00:00","partial"')
            f.flush()
            os.fsync(f.fileno())

        # Monkeypatch os.replace to snapshot the tmp before rename.
        import os as _os
        real_replace = _os.replace
        snapshots: list[bytes] = []

        def spy_replace(src, dst) -> None:
            with open(src, "rb") as snap:
                snapshots.append(snap.read())
            return real_replace(src, dst)

        _os.replace = spy_replace  # type: ignore[assignment]
        try:
            JournalWriter(base_dir=self.base, git_sha=FIXED_GIT_SHA)
        finally:
            _os.replace = real_replace  # type: ignore[assignment]

        self.assertTrue(snapshots, "os.replace was not called during recovery")
        # The tmp that gets renamed over the journal must ALREADY contain
        # the recovery_truncated marker line. Otherwise a crash right
        # after rename would produce a clean file with no audit marker.
        self.assertIn(b'"event_type":"recovery_truncated"', snapshots[-1])

    def test_new_writer_after_clean_shutdown_emits_no_recovery_event(self):
        w1 = JournalWriter(base_dir=self.base, git_sha=FIXED_GIT_SHA)
        self._write_today(w1)

        before = len(w1.read_all())
        w2 = JournalWriter(base_dir=self.base, git_sha=FIXED_GIT_SHA)
        after = len(w2.read_all())
        self.assertEqual(before, after)

    def test_ts_always_has_microsecond_precision(self):
        # Codex round 5 P3: datetime.isoformat() omits the fractional part
        # when microsecond==0, so deterministic / whole-second timestamps
        # previously produced records that didn't match the documented
        # microsecond-precision schema.
        w = JournalWriter(base_dir=self.base, git_sha=FIXED_GIT_SHA)
        exact = datetime(2026, 4, 18, 12, 0, 0, tzinfo=timezone.utc)
        rec = w.append(event_type="order_submitted", payload={}, ts=exact)
        self.assertIn(".000000", rec["ts"])
        self.assertEqual(rec["ts"], "2026-04-18T12:00:00.000000+00:00")

    def test_jsonl_format_round_trip(self):
        w1 = JournalWriter(base_dir=self.base, git_sha=FIXED_GIT_SHA)
        w1.append(event_type="breaker_triggered", payload={"breaker": "daily_hard_stop"})
        w1.append(event_type="kill_switch_written", payload={}, metadata={"reason": "manual"})

        path = w1.path_for_today()
        with path.open("rb") as f:
            raw = f.read()
        # Every line must end with exactly one \n and parse as JSON.
        self.assertTrue(raw.endswith(b"\n"))
        for line in raw.splitlines():
            self.assertTrue(line)
            json.loads(line.decode("utf-8"))


class TimestampNormalizationTests(unittest.TestCase):
    """Codex P2: non-UTC aware datetimes must be normalized before journaling
    or daily-file selection. Naive datetimes must be rejected outright."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.base = Path(self._tmp.name)
        self.writer = JournalWriter(base_dir=self.base, git_sha=FIXED_GIT_SHA)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_rejects_naive_datetime(self):
        naive = datetime(2026, 4, 18, 23, 30)
        with self.assertRaises(ValueError):
            self.writer.append(event_type="order_submitted", payload={}, ts=naive)

    def test_aware_non_utc_gets_routed_by_utc_date(self):
        from zoneinfo import ZoneInfo

        et = ZoneInfo("US/Eastern")
        # 2026-04-18 21:00 ET == 2026-04-19 01:00 UTC -> should land in UTC-dated file
        when = datetime(2026, 4, 18, 21, 0, tzinfo=et)
        self.writer.append(event_type="order_submitted", payload={}, ts=when)

        self.assertFalse((self.base / "2026-04-18.jsonl").exists())
        self.assertTrue((self.base / "2026-04-19.jsonl").exists())
        records = self.writer.read_all(when=datetime(2026, 4, 19, 12, 0, tzinfo=timezone.utc))
        self.assertEqual(len(records), 1)
        self.assertTrue(records[0]["ts"].endswith("+00:00"))

    def test_read_all_normalizes_non_utc_when(self):
        # Codex round 2 P3: read_all must select the same daily file as
        # the corresponding write; a non-UTC query near midnight
        # previously read the wrong file.
        from zoneinfo import ZoneInfo

        et = ZoneInfo("US/Eastern")
        when = datetime(2026, 4, 18, 21, 0, tzinfo=et)  # 2026-04-19 01:00 UTC
        self.writer.append(event_type="order_submitted", payload={"n": 1}, ts=when)

        records = self.writer.read_all(when=when)  # same ET timestamp
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["payload"]["n"], 1)

    def test_read_all_rejects_naive_when(self):
        with self.assertRaises(ValueError):
            self.writer.read_all(when=datetime(2026, 4, 18, 12, 0))


class RecoveryLockTests(unittest.TestCase):
    """Codex P1: recover_trailing_partial must hold the same sidecar lock
    as _atomic_append so a concurrent in-flight append is not misread as a
    crashed partial line and truncated."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.base = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_recovery_blocks_while_append_lock_is_held(self):
        import fcntl as _fcntl
        import os as _os
        import threading

        # Prime a file so there's something to potentially recover.
        w1 = JournalWriter(base_dir=self.base, git_sha=FIXED_GIT_SHA)
        w1.append(event_type="order_submitted", payload={"n": 1})
        data_path = w1.path_for_today()
        lock_path = JournalWriter._lock_path_for(data_path)

        # Simulate another process holding the append lock.
        holder_fd = _os.open(str(lock_path), _os.O_CREAT | _os.O_RDWR, 0o644)
        _fcntl.flock(holder_fd, _fcntl.LOCK_EX)

        started = threading.Event()
        finished = threading.Event()

        def _run_recovery() -> None:
            started.set()
            JournalWriter(base_dir=self.base, git_sha=FIXED_GIT_SHA)
            finished.set()

        t = threading.Thread(target=_run_recovery)
        t.start()
        self.assertTrue(started.wait(2.0))
        # Recovery constructor should be blocked on the append lock.
        self.assertFalse(finished.wait(0.3))

        # Release lock; recovery should now proceed.
        _fcntl.flock(holder_fd, _fcntl.LOCK_UN)
        _os.close(holder_fd)
        self.assertTrue(finished.wait(3.0))
        t.join()


if __name__ == "__main__":
    unittest.main()
