"""Unit tests for m2.4 circuit breakers + .killed lock file.

Verification per milestones.md#2.4:
    "Simulated drawdown triggers each breaker correctly; `.killed`
     written at -10%; engine refuses orders while file exists"
"""

from __future__ import annotations

import json
import tempfile
import unittest
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

from execution.risk import circuit_breakers as cb
from execution.risk import kill_switch as ks
from execution.risk.types import AccountState


def _state(
    current: str,
    day_open: str = "1000000",
    peak: str = "1000000",
    week_history=None,
) -> AccountState:
    return AccountState(
        current_value=Decimal(current),
        day_open_value=Decimal(day_open),
        peak_value=Decimal(peak),
        week_history=week_history or [],
    )


class BreakerTriggerTests(unittest.TestCase):
    def test_daily_soft_triggers_at_minus_2_pct(self):
        state = _state(current="980000")  # -2% intraday, 0% total
        results = cb.evaluate(state)
        soft = next(r for r in results if r.breaker == "daily_soft_stop")
        hard = next(r for r in results if r.breaker == "daily_hard_stop")
        self.assertTrue(soft.tripped)
        self.assertFalse(hard.tripped)
        self.assertEqual(soft.action, "halve_positions")

    def test_daily_hard_triggers_at_minus_3_pct(self):
        state = _state(current="970000")
        results = cb.evaluate(state)
        hard = next(r for r in results if r.breaker == "daily_hard_stop")
        self.assertTrue(hard.tripped)
        self.assertEqual(hard.action, "flatten_all")

    def test_weekly_triggers_at_minus_5_pct_rolling(self):
        week_start = Decimal("1000000")
        hist = [(date(2026, 4, 14) + timedelta(days=i), week_start) for i in range(5)]
        state = _state(current="950000", day_open="950000", peak="1000000", week_history=hist)
        results = cb.evaluate(state)
        weekly = next(r for r in results if r.breaker == "weekly_cap")
        self.assertTrue(weekly.tripped)
        self.assertEqual(weekly.action, "reduce_budget")

    def test_weekly_does_not_trip_before_full_5_session_window(self):
        # Codex round 6 P2: only 2 sessions of history -- the breaker
        # must NOT fire even if current is far below the first point.
        # Previously this would have read as -6% and tripped.
        hist = [
            (date(2026, 4, 14), Decimal("1000000")),
            (date(2026, 4, 15), Decimal("1000000")),
        ]
        state = _state(current="940000", day_open="940000", peak="1000000", week_history=hist)
        results = cb.evaluate(state)
        weekly = next(r for r in results if r.breaker == "weekly_cap")
        self.assertFalse(weekly.tripped)

    def test_weekly_engages_exactly_at_5_sessions(self):
        hist = [
            (date(2026, 4, 14) + timedelta(days=i), Decimal("1000000"))
            for i in range(5)
        ]
        state = _state(current="940000", day_open="940000", peak="1000000", week_history=hist)
        results = cb.evaluate(state)
        weekly = next(r for r in results if r.breaker == "weekly_cap")
        self.assertTrue(weekly.tripped)

    def test_total_drawdown_triggers_at_minus_10_pct(self):
        # -10% from peak (day_open shifted so intraday isn't also -10%)
        state = _state(current="900000", day_open="905000", peak="1000000")
        results = cb.evaluate(state)
        kill = next(r for r in results if r.breaker == "total_drawdown_kill")
        self.assertTrue(kill.tripped)
        self.assertEqual(kill.action, "write_killed")

    def test_no_breaker_trips_on_clean_state(self):
        state = _state(current="1005000", day_open="1000000", peak="1005000")
        results = cb.evaluate(state)
        self.assertFalse(any(r.tripped for r in results))


class KillSwitchTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / ".killed"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_write_and_detect_killed(self):
        self.assertFalse(ks.is_killed(self.path))
        ks.write_killed(
            reason="total_drawdown_kill",
            source="circuit_breaker",
            detail={"total_drawdown_pct": "-0.10"},
            path=self.path,
        )
        self.assertTrue(ks.is_killed(self.path))

        record = ks.read_kill_record(self.path)
        self.assertIsNotNone(record)
        self.assertEqual(record["reason"], "total_drawdown_kill")
        self.assertEqual(record["source"], "circuit_breaker")

    def test_assert_not_killed_raises_when_file_present(self):
        ks.write_killed(reason="manual", source="telegram", path=self.path)
        with self.assertRaises(ks.KillSwitchActiveError) as cm:
            ks.assert_not_killed(self.path)
        self.assertEqual(cm.exception.record["reason"], "manual")

    def test_no_delete_api_exposed(self):
        # Hard-rule surface: the module must not export a kill-clearing
        # function. Only a human filesystem operation can unlock.
        forbidden = {"delete_killed", "clear_kill", "unlock"}
        exported = set(dir(ks))
        self.assertTrue(forbidden.isdisjoint(exported), f"forbidden export found: {forbidden & exported}")

    def test_first_writer_wins_is_immutable(self):
        # Codex round 5 P2: once .killed is written, its contents are
        # frozen until a human deletes the file. A second write (e.g.
        # Telegram kill landing right after the breaker kill) must NOT
        # overwrite the record.
        first = ks.write_killed(reason="first", source="circuit_breaker", path=self.path)
        second = ks.write_killed(reason="second", source="telegram", path=self.path)
        self.assertIsNotNone(first)
        self.assertIsNone(second, "second write should have returned None (first-writer-wins)")
        record = ks.read_kill_record(self.path)
        self.assertEqual(record["reason"], "first")
        self.assertEqual(record["source"], "circuit_breaker")

    def test_write_killed_fsyncs_parent_directory(self):
        # Codex round 3 P2: the rename's durability depends on fsyncing
        # the parent directory -- without it a host crash can lose the
        # renamed entry and the bot would resume after reboot. We can't
        # easily observe fsync, but we can confirm the parent-directory
        # open is non-fatal and leaves no leaked temp file.
        import os as _os
        ks.write_killed(reason="total_drawdown_kill", source="circuit_breaker", path=self.path)
        self.assertTrue(self.path.exists())
        # No leftover temp files in the parent dir.
        leftovers = [
            p for p in self.path.parent.iterdir()
            if p.name.startswith(".killed.tmp.")
        ]
        self.assertEqual(leftovers, [])


class BreakerKillIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / ".killed"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_total_drawdown_writes_killed_and_blocks_orders(self):
        state = _state(current="900000", day_open="905000", peak="1000000")
        results = cb.evaluate(state)
        tripped = cb.apply_kill_on_trip(results, kill_path=self.path)
        self.assertIsNotNone(tripped)
        self.assertEqual(tripped.breaker, "total_drawdown_kill")
        self.assertTrue(self.path.exists())

        # Engine boundary: assert_not_killed must raise
        with self.assertRaises(ks.KillSwitchActiveError):
            ks.assert_not_killed(self.path)

    def test_soft_trip_does_not_write_killed(self):
        state = _state(current="980000")
        results = cb.evaluate(state)
        tripped = cb.apply_kill_on_trip(results, kill_path=self.path)
        self.assertIsNone(tripped)
        self.assertFalse(self.path.exists())

    def test_apply_kill_is_idempotent_across_ticks(self):
        # Codex round 4 P2: engine evaluates breakers every tick. If the
        # kill breaker stays tripped, we must not rewrite .killed (and
        # retrigger journal/Telegram logic) every tick -- the original
        # kill record must be preserved.
        state = _state(current="900000", day_open="905000", peak="1000000")
        first = cb.apply_kill_on_trip(cb.evaluate(state), kill_path=self.path)
        self.assertIsNotNone(first)
        original = self.path.read_bytes()

        # Simulate N more ticks with the same tripped state. Each should
        # return None and NOT overwrite the file.
        for _ in range(3):
            again = cb.apply_kill_on_trip(cb.evaluate(state), kill_path=self.path)
            self.assertIsNone(again)
        self.assertEqual(self.path.read_bytes(), original)

    def test_apply_kill_skips_even_on_unrelated_breaker_when_already_killed(self):
        # Manual kill already wrote .killed; a later breaker evaluation
        # with total-drawdown tripped must not overwrite it.
        ks.write_killed(reason="manual", source="telegram", path=self.path)
        original = self.path.read_bytes()
        state = _state(current="900000", day_open="905000", peak="1000000")
        result = cb.apply_kill_on_trip(cb.evaluate(state), kill_path=self.path)
        self.assertIsNone(result)
        self.assertEqual(self.path.read_bytes(), original)

    def test_atomic_first_writer_wins_under_race(self):
        # Codex round 5 P2: simulate the breaker + Telegram kill firing
        # in two processes concurrently. Both observe "not killed" at
        # t0; one links, the other EEXISTs. The file's content must
        # match exactly one writer.
        import threading
        results = []
        barrier = threading.Barrier(5)

        def _kill(source: str) -> None:
            barrier.wait()
            out = ks.write_killed(reason=source, source=source, path=self.path)
            results.append((source, out is not None))

        threads = [threading.Thread(target=_kill, args=(f"src-{i}",)) for i in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        winners = [s for s, ok in results if ok]
        self.assertEqual(len(winners), 1, f"exactly one winner; got {results}")
        rec = ks.read_kill_record(self.path)
        self.assertEqual(rec["source"], winners[0])


class StrategyRetiredSentinelTests(unittest.TestCase):
    """Bundle 3 cycle 3 (spec §3.2 + Q7): per-strategy retirement
    sentinels mirror the `.killed` contract but are keyed by strategy
    slug so retirement of strategy X cannot block strategy Y's submits.
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.base = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_write_and_detect_strategy_retired(self):
        self.assertFalse(
            ks.is_strategy_retired("spy-rotational", base_dir=self.base)
        )
        out = ks.write_retired(
            "spy-rotational",
            reason="superseded by atr-trail",
            commit_sha="abcd123",
            base_dir=self.base,
        )
        self.assertIsNotNone(out)
        # Codex R3: filename is `.retired-<sha256-first-16-hex>`, not
        # the raw slug -- this is what makes APFS case-insensitivity
        # and UTF-8 expansion non-issues. Readability lives in the
        # JSON record instead.
        self.assertTrue(
            out.name.startswith(".retired-"),
            f"unexpected filename shape: {out.name!r}",
        )
        self.assertEqual(
            len(out.name), len(".retired-") + 16,
            f"expected 25-char filename, got {out.name!r}",
        )
        self.assertTrue(
            ks.is_strategy_retired("spy-rotational", base_dir=self.base)
        )

        record = ks.read_retired_record("spy-rotational", base_dir=self.base)
        self.assertIsNotNone(record)
        self.assertEqual(record["slug"], "spy-rotational")
        self.assertEqual(record["reason"], "superseded by atr-trail")
        self.assertEqual(record["commit_sha"], "abcd123")
        self.assertEqual(record["source"], "invest-ship --retire-strategy")
        # Timestamp is ISO-8601 UTC microsecond precision (same contract
        # as kill_switch.write_killed).
        self.assertIn("T", record["ts"])
        self.assertTrue(record["ts"].endswith("+00:00"))

    def test_assert_strategy_not_retired_raises_when_sentinel_present(self):
        ks.write_retired(
            "spy-rotational",
            reason="end-of-life",
            commit_sha="abc123",
            base_dir=self.base,
        )
        with self.assertRaises(ks.StrategyRetiredError) as cm:
            ks.assert_strategy_not_retired(
                "spy-rotational", base_dir=self.base
            )
        self.assertEqual(cm.exception.strategy_slug, "spy-rotational")
        self.assertEqual(cm.exception.record["reason"], "end-of-life")

    def test_assert_strategy_not_retired_passes_when_absent(self):
        # No raise when sentinel does not exist.
        ks.assert_strategy_not_retired("nonexistent", base_dir=self.base)

    def test_is_strategy_retired_false_when_missing(self):
        self.assertFalse(
            ks.is_strategy_retired("nonexistent", base_dir=self.base)
        )

    def test_read_retired_record_returns_none_when_missing(self):
        self.assertIsNone(
            ks.read_retired_record("nonexistent", base_dir=self.base)
        )

    def test_first_writer_wins_is_immutable(self):
        # Mirrors the `.killed` first-writer-wins property: two sequential
        # write_retired calls for the same slug must produce exactly one
        # landed record, and the record of record is the first call's.
        first = ks.write_retired(
            "spy-rotational",
            reason="first",
            commit_sha="sha1",
            base_dir=self.base,
        )
        second = ks.write_retired(
            "spy-rotational",
            reason="second",
            commit_sha="sha2",
            base_dir=self.base,
        )
        self.assertIsNotNone(first)
        self.assertIsNone(
            second,
            "second write_retired should have returned None (first-writer-wins)",
        )
        record = ks.read_retired_record(
            "spy-rotational", base_dir=self.base
        )
        self.assertEqual(record["reason"], "first")
        self.assertEqual(record["commit_sha"], "sha1")

    def test_cross_strategy_isolation(self):
        # Sentinel for strategy X must not affect strategy Y.
        ks.write_retired(
            "spy-rotational",
            reason="superseded",
            commit_sha="abc",
            base_dir=self.base,
        )
        self.assertTrue(
            ks.is_strategy_retired("spy-rotational", base_dir=self.base)
        )
        self.assertFalse(
            ks.is_strategy_retired("atr-trail", base_dir=self.base)
        )
        ks.assert_strategy_not_retired("atr-trail", base_dir=self.base)
        with self.assertRaises(ks.StrategyRetiredError):
            ks.assert_strategy_not_retired(
                "spy-rotational", base_dir=self.base
            )

    def test_no_delete_api_exposed(self):
        # Hard-rule surface: the module must not export a function that
        # un-retires a strategy. Only human filesystem operation unlocks.
        # Architect-recommended flow for un-retire is retire + new
        # proposed draft, not sentinel deletion.
        forbidden = {
            "delete_retired",
            "clear_retired",
            "unretire",
            "unretire_strategy",
            "remove_retired",
        }
        exported = set(dir(ks))
        self.assertTrue(
            forbidden.isdisjoint(exported),
            f"forbidden export found: {forbidden & exported}",
        )

    def test_write_leaves_no_tempfile_residue(self):
        # Parent-dir fsync path is tested in write_killed; here we
        # assert the atomic-rename cleanup leaves no `.retired-*.tmp.*`
        # leftovers on the happy path. Codex R4: match any tempfile
        # with `.tmp.` in the name since the target filename is now
        # the sha256 hash digest, not the raw slug.
        ks.write_retired(
            "spy-rotational",
            reason="ok",
            commit_sha="x",
            base_dir=self.base,
        )
        leftovers = [
            p for p in self.base.iterdir()
            if p.name.startswith(".retired-") and ".tmp." in p.name
        ]
        self.assertEqual(leftovers, [])

    def test_slug_arbitrary_chars_are_path_safe_via_hash(self):
        # R4-minimax P0 + Codex R1 + R3: slugs with path-separator
        # bytes, `..`, whitespace, non-ASCII, or weird casing must
        # produce safe sentinel filenames. The defense is the sha256
        # hash in _retired_path -- any slug becomes a fixed 16-hex
        # digest inside base_dir. The validator stays permissive to
        # match the loader's existing `name:` contract.
        traversal_slugs = [
            "../etc/passwd",
            "..",
            "foo/../../etc",
            "foo/bar",
            "foo\\bar",
            "has spaces",
            "UPPER/case",
            "spy$rotational",
            "naïve-mean",
            "mean.reversion",
            "SPY rotational",
            ".hidden",
            "-dash-leading",
        ]
        base_resolved = self.base.resolve()
        for slug in traversal_slugs:
            with self.subTest(slug=slug):
                # Each entry point accepts the slug without raising.
                self.assertFalse(
                    ks.is_strategy_retired(slug, base_dir=self.base)
                )
                self.assertIsNone(
                    ks.read_retired_record(slug, base_dir=self.base)
                )
                ks.assert_strategy_not_retired(slug, base_dir=self.base)

                # Write + round-trip verification: the sentinel must
                # land INSIDE base_dir (path-safe) AND subsequent reads
                # for the same slug must find it.
                out = ks.write_retired(
                    slug, reason="x", commit_sha="y", base_dir=self.base
                )
                self.assertIsNotNone(out)
                self.assertEqual(
                    out.resolve().parent, base_resolved,
                    f"slug {slug!r} escaped base_dir: landed at {out}",
                )
                self.assertTrue(
                    ks.is_strategy_retired(slug, base_dir=self.base),
                    f"slug {slug!r} round-trip failed: wrote but cannot read",
                )
                out.unlink()

    def test_case_variants_hash_to_distinct_filenames(self):
        # Codex R3 P1: on case-insensitive APFS (the default on Keith's
        # Mac Mini + MacBook) a URL-encoded filename like
        # `.retired-MeanRev` collides with `.retired-meanrev`. With the
        # sha256 hash, differently-cased slugs hash to distinct digests,
        # so retiring one never blocks the other.
        p_upper = ks.write_retired(
            "MeanRev", reason="mixed case", commit_sha="A",
            base_dir=self.base,
        )
        p_lower = ks.write_retired(
            "meanrev", reason="lower case", commit_sha="B",
            base_dir=self.base,
        )
        self.assertIsNotNone(p_upper)
        self.assertIsNotNone(p_lower)
        self.assertNotEqual(
            p_upper, p_lower,
            "case-variant slugs must produce distinct sentinel paths",
        )
        # Both sentinels present simultaneously.
        self.assertTrue(ks.is_strategy_retired("MeanRev", base_dir=self.base))
        self.assertTrue(ks.is_strategy_retired("meanrev", base_dir=self.base))

    def test_invalid_slug_types_rejected(self):
        # The narrow ValueError surface: empty, non-str, or NUL-byte
        # slugs are rejected because they represent malformed
        # frontmatter rather than legitimate strategy names. Codex R4:
        # NO length cap -- hash-based filenames make long names a
        # non-issue for filesystem safety and the loader accepts any
        # non-empty name today.
        for slug in ("", None, 123, b"bytes"):
            with self.subTest(slug=slug):
                with self.assertRaises(ValueError):
                    ks.is_strategy_retired(slug, base_dir=self.base)
        for slug in ("\0", "foo\0bar", "\0foo"):
            with self.subTest(slug=slug):
                with self.assertRaises(ValueError):
                    ks.is_strategy_retired(slug, base_dir=self.base)
                with self.assertRaises(ValueError):
                    ks.assert_strategy_not_retired(
                        slug, base_dir=self.base
                    )
                with self.assertRaises(ValueError):
                    ks.write_retired(
                        slug, reason="x", commit_sha="y",
                        base_dir=self.base,
                    )

    def test_long_slugs_are_accepted_and_path_safe(self):
        # Codex R4: length cap removed. A 500-char name is legitimate
        # frontmatter (just unusual); it must hash cleanly and land
        # inside base_dir with no byte-cap ValueError.
        long_slug = "a" * 500
        out = ks.write_retired(
            long_slug, reason="len-test", commit_sha="deadbeef",
            base_dir=self.base,
        )
        self.assertIsNotNone(out)
        self.assertEqual(out.resolve().parent, self.base.resolve())
        self.assertTrue(
            ks.is_strategy_retired(long_slug, base_dir=self.base)
        )

    def test_slug_valid_examples_accepted(self):
        # Positive coverage: K2Bi file-convention examples plus loose
        # names the loader already accepts must all pass the validator.
        # Codex R1: this set MUST include `mean.reversion` /
        # `SPY rotational` style names so we do not silently regress
        # the loader's existing `name:` contract.
        good_slugs = [
            "spy-rotational",
            "atr_trail",
            "momo2026",
            "a",
            "a1",
            "A-B_c-9",
            "a" * 64,
            "mean.reversion",
            "SPY rotational",
            "naïve-mean",
        ]
        for slug in good_slugs:
            with self.subTest(slug=slug):
                ks.is_strategy_retired(slug, base_dir=self.base)
                ks.read_retired_record(slug, base_dir=self.base)
                ks.assert_strategy_not_retired(slug, base_dir=self.base)

    def test_malformed_sentinel_does_not_crash_assert(self):
        # R4-minimax P1: if the sentinel file exists but contains
        # garbage bytes (human manual edit, disk corruption), the
        # assert path MUST fail-closed and raise StrategyRetiredError
        # with record=None, not propagate a JSONDecodeError up through
        # the engine's hot loop.
        # Write a valid sentinel first so we land on the correct
        # hash-based filename (Codex R3), then overwrite its contents
        # with garbage to simulate corruption.
        out = ks.write_retired(
            "spy-rotational",
            reason="initial",
            commit_sha="abc",
            base_dir=self.base,
        )
        self.assertIsNotNone(out)
        out.write_bytes(b"not valid json {{{")

        self.assertTrue(
            ks.is_strategy_retired("spy-rotational", base_dir=self.base)
        )
        self.assertIsNone(
            ks.read_retired_record(
                "spy-rotational", base_dir=self.base
            )
        )
        with self.assertRaises(ks.StrategyRetiredError) as cm:
            ks.assert_strategy_not_retired(
                "spy-rotational", base_dir=self.base
            )
        self.assertIsNone(cm.exception.record)
        self.assertEqual(cm.exception.strategy_slug, "spy-rotational")

    def test_resolve_retired_dir_explicit_wins(self):
        # Branch 1: explicit retired_dir takes precedence over kill_path.
        explicit = self.base / "explicit-retired"
        kill_path = self.base / "kill" / ".killed"
        self.assertEqual(
            ks.resolve_retired_dir(explicit, kill_path),
            explicit,
        )

    def test_resolve_retired_dir_derives_from_kill_path(self):
        # Branch 2: no explicit retired_dir, but kill_path is set ->
        # derive from kill_path.parent so test fixtures automatically
        # isolate retirement sentinels to the same tmp dir as .killed.
        kill_path = self.base / "tmp" / ".killed"
        self.assertEqual(
            ks.resolve_retired_dir(None, kill_path),
            kill_path.parent,
        )

    def test_resolve_retired_dir_falls_through_to_default(self):
        # Branch 3: neither set -> vault-side default.
        self.assertEqual(
            ks.resolve_retired_dir(None, None),
            ks.DEFAULT_RETIRED_DIR,
        )

    def test_slug_derivation_chain_round_trip(self):
        # R11-minimax P1 + R12 finding 3: the end-to-end contract the
        # cycle-4 hook must honor. Given a canonical K2Bi strategy
        # file `strategy_<slug>.md`, both sides MUST arrive at the
        # same sentinel file by:
        #   1. derive_retire_slug(filename) -> slug with `strategy_`
        #      prefix stripped (the engine's _retire_slug is a thin
        #      wrapper; the hook imports this same function).
        #   2. sha256(slug)[:16] -> filename (done inside _retired_path).
        # The test calls the REAL derive_retire_slug so a future
        # change to the derivation automatically moves this test's
        # expectations.
        from execution.engine.main import derive_retire_slug

        engine_slug = derive_retire_slug(
            "/some/path/wiki/strategies/strategy_spy-rotational.md"
        )
        self.assertEqual(engine_slug, "spy-rotational")

        out = ks.write_retired(
            engine_slug, reason="chain test",
            commit_sha="deadbeef", base_dir=self.base,
        )
        self.assertIsNotNone(out)
        self.assertTrue(
            ks.is_strategy_retired(engine_slug, base_dir=self.base),
            "engine+hook must resolve to the same sentinel file",
        )

        # Guardrail: a differing key (the full stem including
        # `strategy_` prefix, a common mistake in a naive hook) MUST
        # resolve to a different file and NOT find the sentinel.
        self.assertFalse(
            ks.is_strategy_retired(
                "strategy_spy-rotational", base_dir=self.base
            ),
            "keys differing by `strategy_` prefix must not collide",
        )

        # Flat-layout coverage: file without the `strategy_` prefix
        # returns the stem directly.
        self.assertEqual(
            derive_retire_slug("/foo/meanrev-v2.md"),
            "meanrev-v2",
        )

    def test_inaccessible_base_dir_fails_closed(self):
        # R12-minimax P1: if `.exists()` on the sentinel raises OSError
        # (base_dir unmounted, permission denied, stale NFS), the
        # submit gate must fail-CLOSED -- treat the strategy as
        # retired and synthesize a `base_dir_inaccessible` record.
        # Silent fail-open would let retired strategies trade during
        # a vault-tier outage.
        from unittest.mock import patch

        def _raise_oserror(*args, **kwargs):
            raise OSError("simulated: base_dir unreachable")

        with patch("pathlib.Path.exists", _raise_oserror):
            self.assertTrue(
                ks.is_strategy_retired(
                    "spy-rotational", base_dir=self.base
                ),
                "OSError on exists() must fail-closed",
            )
            with self.assertRaises(ks.StrategyRetiredError) as cm:
                ks.assert_strategy_not_retired(
                    "spy-rotational", base_dir=self.base
                )
            self.assertIsNotNone(cm.exception.record)
            self.assertEqual(
                cm.exception.record["error"],
                "base_dir_inaccessible",
            )
            self.assertIn("error_class", cm.exception.record)

    def test_hook_engine_resolver_parity_round_trip(self):
        # R6-minimax: cycle 4's post-commit hook MUST use
        # resolve_retired_dir to derive base_dir so the path it writes
        # to matches the path the engine reads from. This test locks
        # the contract: given identical inputs, a "hook"-style write
        # lands at the same location an "engine"-style read checks.
        # The hook itself is out-of-tree in cycle 3; this test is the
        # cycle-3 anchor pending cycle 4's live integration test.
        kill_path = self.base / ".killed"
        hook_dir = ks.resolve_retired_dir(None, kill_path)
        engine_dir = ks.resolve_retired_dir(None, kill_path)
        self.assertEqual(hook_dir, engine_dir)

        ks.write_retired(
            "spy-rotational",
            reason="hook-engine parity",
            commit_sha="deadbeef",
            base_dir=hook_dir,
        )
        self.assertTrue(
            ks.is_strategy_retired(
                "spy-rotational", base_dir=engine_dir
            )
        )

    def test_atomic_first_writer_wins_under_race(self):
        # Mirror the kill-switch race test: N concurrent writers for the
        # same slug must produce exactly one successful write and the
        # record must reflect that winner's inputs.
        import threading

        outcomes: list[tuple[str, bool]] = []
        barrier = threading.Barrier(5)

        def _retire(reason: str) -> None:
            barrier.wait()
            out = ks.write_retired(
                "spy-rotational",
                reason=reason,
                commit_sha=reason,
                base_dir=self.base,
            )
            outcomes.append((reason, out is not None))

        threads = [
            threading.Thread(target=_retire, args=(f"src-{i}",))
            for i in range(5)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        winners = [s for s, ok in outcomes if ok]
        self.assertEqual(
            len(winners), 1, f"exactly one winner; got {outcomes}"
        )
        record = ks.read_retired_record(
            "spy-rotational", base_dir=self.base
        )
        self.assertEqual(record["reason"], winners[0])
        self.assertEqual(record["commit_sha"], winners[0])


if __name__ == "__main__":
    unittest.main()
