"""Tests for shared watchlist-index writer with file locking (m2.22 F4)."""

from __future__ import annotations

import multiprocessing as mp
import tempfile
import unittest
from pathlib import Path

from scripts.lib.watchlist_index import (
    _index_lock,
    remove_watchlist_index_row,
    symbol_lock,
    update_watchlist_index,
)


class UpdateWatchlistIndexBasicTests(unittest.TestCase):
    def test_creates_new_index(self):
        with tempfile.TemporaryDirectory() as td:
            td_path = Path(td)
            update_watchlist_index(td_path, "NVDA", "2026-04-26", "promoted")
            index_path = td_path / "wiki" / "watchlist" / "index.md"
            self.assertTrue(index_path.exists())
            content = index_path.read_text()
            self.assertIn("| Symbol | Date | Status |", content)
            self.assertIn("| [[NVDA]] | 2026-04-26 | promoted |", content)

    def test_appends_to_existing_index(self):
        with tempfile.TemporaryDirectory() as td:
            td_path = Path(td)
            update_watchlist_index(td_path, "NVDA", "2026-04-26", "promoted")
            update_watchlist_index(td_path, "LRCX", "2026-04-26", "screened")
            content = (td_path / "wiki" / "watchlist" / "index.md").read_text()
            self.assertIn("| [[NVDA]]", content)
            self.assertIn("| [[LRCX]]", content)

    def test_idempotent_on_existing_symbol(self):
        with tempfile.TemporaryDirectory() as td:
            td_path = Path(td)
            update_watchlist_index(td_path, "NVDA", "2026-04-26", "promoted")
            content_before = (td_path / "wiki" / "watchlist" / "index.md").read_text()
            update_watchlist_index(td_path, "NVDA", "2026-04-27", "screened")
            content_after = (td_path / "wiki" / "watchlist" / "index.md").read_text()
            self.assertEqual(content_before, content_after)

    def test_lock_sentinel_created(self):
        with tempfile.TemporaryDirectory() as td:
            td_path = Path(td)
            update_watchlist_index(td_path, "NVDA", "2026-04-26", "promoted")
            lock_path = td_path / "wiki" / "watchlist" / ".index.lock"
            self.assertTrue(lock_path.exists())


class IndexLockContextManagerTests(unittest.TestCase):
    def test_lock_creates_parent_dirs(self):
        with tempfile.TemporaryDirectory() as td:
            index_path = Path(td) / "deeply" / "nested" / "wiki" / "watchlist" / "index.md"
            with _index_lock(index_path):
                self.assertTrue(index_path.parent.exists())
            self.assertTrue((index_path.parent / ".index.lock").exists())

    def test_lock_released_on_exception(self):
        with tempfile.TemporaryDirectory() as td:
            index_path = Path(td) / "wiki" / "watchlist" / "index.md"
            try:
                with _index_lock(index_path):
                    raise RuntimeError("simulated failure")
            except RuntimeError:
                pass
            # Re-acquire must succeed (lock was released by context manager exit).
            with _index_lock(index_path):
                pass


# ---------------------------------------------------------------------------
# Concurrency proof (m2.22 F4): two processes racing on the same index must
# both have their rows survive after both complete.
# ---------------------------------------------------------------------------


def _writer_process(vault_path_str: str, symbol: str, date: str, status: str):
    """Worker target -- imports must happen inside (mp.spawn fork-context)."""
    from scripts.lib.watchlist_index import update_watchlist_index as fn

    fn(Path(vault_path_str), symbol, date, status)


class ConcurrentWritersDoNotDropRowsTests(unittest.TestCase):
    def test_two_processes_both_rows_survive(self):
        """Without locking, two concurrent writers can each read the same
        old index and the second atomic replace drops the first update.
        With locking, both rows must survive."""
        with tempfile.TemporaryDirectory() as td:
            td_path = Path(td)
            # Pre-create an empty index dir so the first writer's mkdir is a no-op
            # in the racy critical section.
            (td_path / "wiki" / "watchlist").mkdir(parents=True, exist_ok=True)

            ctx = mp.get_context("spawn")
            p1 = ctx.Process(
                target=_writer_process,
                args=(str(td_path), "NVDA", "2026-04-26", "promoted"),
            )
            p2 = ctx.Process(
                target=_writer_process,
                args=(str(td_path), "LRCX", "2026-04-26", "screened"),
            )
            p1.start()
            p2.start()
            p1.join(timeout=30)
            p2.join(timeout=30)
            self.assertEqual(p1.exitcode, 0, "Writer 1 failed")
            self.assertEqual(p2.exitcode, 0, "Writer 2 failed")

            content = (td_path / "wiki" / "watchlist" / "index.md").read_text()
            self.assertIn(
                "| [[NVDA]]", content, "NVDA row was dropped by the race"
            )
            self.assertIn(
                "| [[LRCX]]", content, "LRCX row was dropped by the race"
            )


class RemoveWatchlistIndexRowTests(unittest.TestCase):
    """m2.22 N2: compensating-removal rollback semantics."""

    def test_removes_only_target_symbol(self):
        with tempfile.TemporaryDirectory() as td:
            td_path = Path(td)
            update_watchlist_index(td_path, "NVDA", "2026-04-26", "promoted")
            update_watchlist_index(td_path, "LRCX", "2026-04-26", "screened")
            remove_watchlist_index_row(td_path, "LRCX")
            content = (td_path / "wiki" / "watchlist" / "index.md").read_text()
            self.assertIn("| [[NVDA]]", content)
            self.assertNotIn("| [[LRCX]]", content)

    def test_noop_on_missing_index(self):
        with tempfile.TemporaryDirectory() as td:
            # Should not raise even though no index file exists.
            remove_watchlist_index_row(Path(td), "NVDA")

    def test_noop_on_missing_symbol(self):
        with tempfile.TemporaryDirectory() as td:
            td_path = Path(td)
            update_watchlist_index(td_path, "NVDA", "2026-04-26", "promoted")
            content_before = (td_path / "wiki" / "watchlist" / "index.md").read_text()
            remove_watchlist_index_row(td_path, "TSLA")
            content_after = (td_path / "wiki" / "watchlist" / "index.md").read_text()
            self.assertEqual(content_before, content_after)


class SymbolLockTests(unittest.TestCase):
    """m2.22 N1: per-symbol promotion lock."""

    def test_creates_per_symbol_sentinel(self):
        with tempfile.TemporaryDirectory() as td:
            td_path = Path(td)
            with symbol_lock(td_path, "LRCX"):
                pass
            lock_path = td_path / "wiki" / "watchlist" / ".LRCX.lock"
            self.assertTrue(lock_path.exists())

    def test_different_symbols_use_different_sentinels(self):
        with tempfile.TemporaryDirectory() as td:
            td_path = Path(td)
            with symbol_lock(td_path, "LRCX"):
                pass
            with symbol_lock(td_path, "NVDA"):
                pass
            self.assertTrue((td_path / "wiki" / "watchlist" / ".LRCX.lock").exists())
            self.assertTrue((td_path / "wiki" / "watchlist" / ".NVDA.lock").exists())

    def test_lock_released_on_exception(self):
        with tempfile.TemporaryDirectory() as td:
            td_path = Path(td)
            try:
                with symbol_lock(td_path, "LRCX"):
                    raise RuntimeError("simulated failure")
            except RuntimeError:
                pass
            # Re-acquire must succeed (lock was released).
            with symbol_lock(td_path, "LRCX"):
                pass


# ---------------------------------------------------------------------------
# Concurrency proof for compensating-removal rollback (m2.22 N2): a
# rollback that re-reads under the index lock and removes only the
# target row must NOT clobber another writer's row that landed
# in between.
# ---------------------------------------------------------------------------


def _writer_then_remover(vault_path_str: str, write_symbol: str, remove_symbol: str):
    """Worker target: write one symbol then remove a different one
    by compensating action -- mimics a rollback racing with another
    writer."""
    from scripts.lib.watchlist_index import (
        remove_watchlist_index_row as rm,
        update_watchlist_index as wr,
    )

    wr(Path(vault_path_str), write_symbol, "2026-04-26", "promoted")
    rm(Path(vault_path_str), remove_symbol)


class CompensatingRemovalSurvivesConcurrentWriterTests(unittest.TestCase):
    def test_rollback_does_not_clobber_concurrent_row(self):
        """A snapshot-and-restore rollback would lose NVDA. The
        compensating-removal rollback must preserve it."""
        with tempfile.TemporaryDirectory() as td:
            td_path = Path(td)
            # Pre-stage one row to simulate "in-flight" promote that
            # already landed an index update.
            update_watchlist_index(td_path, "LRCX", "2026-04-26", "promoted")

            ctx = mp.get_context("spawn")
            # Process 1 writes NVDA (the "concurrent writer").
            p1 = ctx.Process(
                target=_writer_process,
                args=(str(td_path), "NVDA", "2026-04-26", "promoted"),
            )
            # Process 2 simulates the rolled-back promote: it removes
            # LRCX (its own previously-written row) by compensating
            # action.
            p2 = ctx.Process(
                target=_writer_then_remover,
                args=(str(td_path), "TSLA", "LRCX"),
            )
            p1.start()
            p2.start()
            p1.join(timeout=30)
            p2.join(timeout=30)
            self.assertEqual(p1.exitcode, 0)
            self.assertEqual(p2.exitcode, 0)

            content = (td_path / "wiki" / "watchlist" / "index.md").read_text()
            self.assertIn("| [[NVDA]]", content, "Concurrent writer's row must survive")
            self.assertIn("| [[TSLA]]", content, "Process-2 write must survive")
            self.assertNotIn("| [[LRCX]]", content, "Compensating removal must succeed")


if __name__ == "__main__":
    unittest.main()
