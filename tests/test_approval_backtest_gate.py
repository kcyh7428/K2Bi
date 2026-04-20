"""Tests for scan_backtests_for_slug and its integration into
scripts.lib.invest_ship_strategy.handle_approve_strategy (Bundle 4
cycle 3 / m2.15 Part B).

Mirrors tests/test_approval_bear_case_gate.py shape: the gate helper
gets direct unit coverage (in tests/test_invest_backtest.py::ScanTests)
and this file wires the full approve-strategy pipeline end-to-end so
we see the REFUSE paths surface as ValidationError in the handler.

Two-gate ordering (spec §3.5 + cycle 3 plan):

    bear-case gate  -> refuses if ticker lacks fresh PROCEED
    backtest gate   -> refuses if slug lacks passed backtest OR
                       lacks `## Backtest Override` for suspicious

Tests here seed a valid bear-case (helper `_seed_bear_case_proceed`) so
the backtest gate is the one exercised. Handler integration with
bear-case alone is covered by the cycle-2 test module.
"""

from __future__ import annotations

import datetime as _dt
import shutil
import subprocess
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from scripts.lib import invest_ship_strategy as iss
from scripts.lib import strategy_frontmatter as sf


def _seed_bear_case_proceed(
    repo: Path, ticker: str, today: _dt.date
) -> Path:
    """Write a minimal schema-complete fresh-PROCEED bear-case thesis at
    wiki/tickers/<TICKER>.md under `repo`. Mirrors the helper used in
    tests/test_approval_bear_case_gate.py fixtures so the backtest gate
    tests do not re-litigate bear-case acceptance.
    """
    tickers_dir = repo / "wiki" / "tickers"
    tickers_dir.mkdir(parents=True, exist_ok=True)
    path = tickers_dir / f"{ticker}.md"
    lines = [
        "---",
        f"tags: [ticker, {ticker}, thesis]",
        f"date: {today.isoformat()}",
        "type: ticker",
        "origin: k2bi-extract",
        'up: "[[tickers/index]]"',
        f"symbol: {ticker}",
        "thesis_score: 73",
        "bear_verdict: PROCEED",
        "bear_conviction: 40",
        f"bear-last-verified: {today.isoformat()}",
        "bear_top_counterpoints:",
        "  - c1",
        "  - c2",
        "  - c3",
        "bear_invalidation_scenarios:",
        "  - s1",
        "  - s2",
        "---",
        "",
        "## Phase 1: Business Model",
        "dummy",
        "",
    ]
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def _seed_proposed_strategy(
    repo: Path,
    slug: str = "spy-rotational",
    *,
    ticker: str = "SPY",
    include_backtest_override: bool = False,
) -> Path:
    """Write a proposed-status strategy spec suitable for handle_approve_
    strategy. Same required fields as cycle 5's _write_strategy."""
    strat_dir = repo / "wiki" / "strategies"
    strat_dir.mkdir(parents=True, exist_ok=True)
    path = strat_dir / f"strategy_{slug}.md"
    lines = [
        "---",
        f"name: {slug}",
        "status: proposed",
        "strategy_type: hand_crafted",
        "risk_envelope_pct: 0.01",
        "regime_filter:",
        "  - risk_on",
        "order:",
        f"  ticker: {ticker}",
        "  side: buy",
        "  qty: 1",
        "  limit_price: 500.00",
        "  stop_loss: 490.00",
        "  time_in_force: DAY",
        "tags: [strategy, SPY]",
        "date: 2026-04-19",
        "type: strategy",
        "origin: keith",
        'up: "[[index]]"',
        "---",
        "",
        "## How This Works",
        "",
        "Buy SPY at 500 limit with 490 stop when risk_on regime holds.",
    ]
    if include_backtest_override:
        lines += [
            "",
            "## Backtest Override",
            "",
            "Backtest run: 2026-04-19 at raw/backtests/2026-04-19_"
            f"{slug}_backtest.md",
            "Suspicious flag reason: total_return=620.0% > 500%",
            "Why this is acceptable: initial sanity baseline captured "
            "the post-2024 equity rally; strategy logic is "
            "conservative and not look-ahead dependent.",
        ]
    content = "\n".join(lines) + "\n"
    path.write_text(content, encoding="utf-8")
    return path


def _seed_backtest_capture(
    repo: Path,
    slug: str,
    *,
    filename: str,
    look_ahead_check: str = "passed",
    look_ahead_check_reason: str = "",
) -> Path:
    """Write a valid-schema backtest capture at raw/backtests/<filename>."""
    import yaml

    backtests_dir = repo / "raw" / "backtests"
    backtests_dir.mkdir(parents=True, exist_ok=True)
    fm = {
        "tags": ["backtest", slug, "raw"],
        "date": "2026-04-19",
        "type": "backtest",
        "origin": "k2bi-generate",
        "up": "[[backtests/index]]",
        "strategy_slug": slug,
        "strategy_commit_sha": "abc123def456",
        "backtest": {
            "window": {"start": "2024-04-19", "end": "2026-04-19"},
            "source": "yfinance",
            "source_version": "1.3.0",
            "symbol": "SPY",
            "reference_symbol": "SPY",
            "metrics": {
                "sharpe": 1.0,
                "sortino": 1.5,
                "max_dd_pct": -5.0,
                "win_rate_pct": 55.0,
                "avg_winner_pct": 2.0,
                "avg_loser_pct": -1.5,
                "total_return_pct": 20.0,
                "n_trades": 30,
                "avg_trade_holding_days": 5.0,
            },
            "look_ahead_check": look_ahead_check,
            "look_ahead_check_reason": look_ahead_check_reason,
            "last_run": "2026-04-19T10:00:00+00:00",
        },
    }
    path = backtests_dir / filename
    path.write_text(
        "---\n"
        + yaml.safe_dump(fm, sort_keys=False, default_flow_style=False)
        + "---\n\nbody\n",
        encoding="utf-8",
    )
    return path


def _make_tmp_repo() -> tuple[Path, str]:
    """Seed git repo + return (path, sha). Same helper as cycle-5 tests."""
    tmp = Path(tempfile.mkdtemp(prefix="ibgate_"))
    subprocess.run(
        ["git", "init", "-q", "-b", "main"], cwd=str(tmp), check=True
    )
    subprocess.run(
        ["git", "config", "user.email", "test@test"],
        cwd=str(tmp), check=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test"], cwd=str(tmp), check=True
    )
    subprocess.run(
        ["git", "config", "commit.gpgSign", "false"],
        cwd=str(tmp), check=True,
    )
    (tmp / "README.md").write_text("seed\n")
    subprocess.run(["git", "add", "README.md"], cwd=str(tmp), check=True)
    subprocess.run(
        ["git", "commit", "-m", "seed", "-q"], cwd=str(tmp), check=True
    )
    sha = subprocess.check_output(
        ["git", "rev-parse", "--short", "HEAD"], cwd=str(tmp), text=True
    ).strip()
    return tmp, sha


class BacktestGateBase(unittest.TestCase):
    """Shared setUp seeding a fresh repo with a valid bear-case so the
    backtest gate is the one under test."""

    def setUp(self) -> None:
        self.repo, self.sha = _make_tmp_repo()
        self.today = _dt.date(2026, 4, 19)
        self.now = datetime(2026, 4, 19, 10, 0, 0, tzinfo=timezone.utc)
        self.slug = "spy-rotational"
        _seed_bear_case_proceed(self.repo, "SPY", self.today)

    def tearDown(self) -> None:
        shutil.rmtree(self.repo, ignore_errors=True)


# ---------- handle_approve_strategy REFUSE paths via backtest gate ----------


class MissingBacktestTests(BacktestGateBase):
    def test_approval_refuses_when_no_backtest_exists(self) -> None:
        path = _seed_proposed_strategy(self.repo, self.slug)
        with self.assertRaises(iss.ValidationError) as cm:
            iss.handle_approve_strategy(
                path,
                parent_sha=self.sha,
                now=self.now,
                today=self.today,
                vault_root=self.repo,
            )
        self.assertIn("no backtest found", str(cm.exception))
        self.assertIn(self.slug, str(cm.exception))
        # Strategy file must be unchanged (REFUSE before mutation).
        fm = sf.parse(path.read_bytes())
        self.assertEqual(sf.extract_status(fm), "proposed")
        self.assertNotIn("approved_at", fm)


class SuspiciousNoOverrideTests(BacktestGateBase):
    def test_approval_refuses_when_suspicious_and_no_override(self) -> None:
        path = _seed_proposed_strategy(self.repo, self.slug)
        _seed_backtest_capture(
            self.repo,
            self.slug,
            filename=f"2026-04-19_{self.slug}_backtest.md",
            look_ahead_check="suspicious",
            look_ahead_check_reason="total_return=620.0% > 500%",
        )
        with self.assertRaises(iss.ValidationError) as cm:
            iss.handle_approve_strategy(
                path,
                parent_sha=self.sha,
                now=self.now,
                today=self.today,
                vault_root=self.repo,
            )
        msg = str(cm.exception)
        self.assertIn("suspicious", msg)
        self.assertIn("Backtest Override", msg)
        self.assertIn("total_return", msg)
        fm = sf.parse(path.read_bytes())
        self.assertEqual(sf.extract_status(fm), "proposed")


class MalformedCaptureTests(BacktestGateBase):
    def test_approval_refuses_on_unparseable_backtest(self) -> None:
        path = _seed_proposed_strategy(self.repo, self.slug)
        backtests = self.repo / "raw" / "backtests"
        backtests.mkdir(parents=True)
        (backtests / f"2026-04-19_{self.slug}_backtest.md").write_text(
            "---\nbroken: : : yaml\n---\nbody\n", encoding="utf-8"
        )
        with self.assertRaises(iss.ValidationError) as cm:
            iss.handle_approve_strategy(
                path,
                parent_sha=self.sha,
                now=self.now,
                today=self.today,
                vault_root=self.repo,
            )
        self.assertIn("unparseable", str(cm.exception))


class AllEmptyCapturesTests(BacktestGateBase):
    def test_approval_refuses_when_all_captures_empty(self) -> None:
        path = _seed_proposed_strategy(self.repo, self.slug)
        backtests = self.repo / "raw" / "backtests"
        backtests.mkdir(parents=True)
        (backtests / f"2026-04-19_{self.slug}_backtest.md").write_bytes(b"")
        (backtests / f"2026-04-18_{self.slug}_backtest.md").write_bytes(b"")
        with self.assertRaises(iss.ValidationError) as cm:
            iss.handle_approve_strategy(
                path,
                parent_sha=self.sha,
                now=self.now,
                today=self.today,
                vault_root=self.repo,
            )
        self.assertIn("all are empty", str(cm.exception))


# ---------- handle_approve_strategy PROCEED paths ----------


class BothGatesPassTests(BacktestGateBase):
    def test_approval_proceeds_when_bear_case_and_backtest_both_pass(
        self,
    ) -> None:
        path = _seed_proposed_strategy(self.repo, self.slug)
        _seed_backtest_capture(
            self.repo,
            self.slug,
            filename=f"2026-04-19_{self.slug}_backtest.md",
            look_ahead_check="passed",
        )
        hints = iss.handle_approve_strategy(
            path,
            parent_sha=self.sha,
            now=self.now,
            today=self.today,
            vault_root=self.repo,
        )
        # Status flipped to approved.
        fm = sf.parse(path.read_bytes())
        self.assertEqual(sf.extract_status(fm), "approved")
        self.assertEqual(fm["approved_commit_sha"], self.sha)
        # Returned hints carry the expected transition + slug.
        self.assertEqual(hints.transition, "proposed -> approved")
        self.assertEqual(hints.slug, self.slug)


class SuspiciousWithOverrideProceedsTests(BacktestGateBase):
    def test_approval_proceeds_when_suspicious_and_override_present(
        self,
    ) -> None:
        path = _seed_proposed_strategy(
            self.repo, self.slug, include_backtest_override=True
        )
        _seed_backtest_capture(
            self.repo,
            self.slug,
            filename=f"2026-04-19_{self.slug}_backtest.md",
            look_ahead_check="suspicious",
            look_ahead_check_reason="total_return=620.0% > 500%",
        )
        hints = iss.handle_approve_strategy(
            path,
            parent_sha=self.sha,
            now=self.now,
            today=self.today,
            vault_root=self.repo,
        )
        fm = sf.parse(path.read_bytes())
        self.assertEqual(sf.extract_status(fm), "approved")
        # Strategy body still has the override section post-approval
        # (approve handler only mutates frontmatter; body is preserved).
        body = path.read_text(encoding="utf-8")
        self.assertIn("## Backtest Override", body)


# ---------- gate-ordering ----------


class GateOrderingTests(BacktestGateBase):
    def test_bear_case_refuses_before_backtest_scan_runs(self) -> None:
        """If bear-case is STALE, approval refuses with a bear-case
        message -- the backtest gate must not fire its own REFUSE
        message on top. This protects the user-facing error clarity."""
        # Delete the fresh bear-case; replace with a stale one.
        (self.repo / "wiki" / "tickers" / "SPY.md").unlink()
        stale_date = (self.today - _dt.timedelta(days=45)).isoformat()
        tickers_dir = self.repo / "wiki" / "tickers"
        (tickers_dir / "SPY.md").write_text(
            "---\n"
            "tags: [ticker, SPY, thesis]\n"
            "date: 2026-04-19\n"
            "type: ticker\n"
            "origin: k2bi-extract\n"
            'up: "[[tickers/index]]"\n'
            "symbol: SPY\n"
            "thesis_score: 73\n"
            "bear_verdict: PROCEED\n"
            "bear_conviction: 40\n"
            f"bear-last-verified: {stale_date}\n"
            "bear_top_counterpoints:\n  - c1\n  - c2\n  - c3\n"
            "bear_invalidation_scenarios:\n  - s1\n  - s2\n"
            "---\n\n## Phase 1\nx\n",
            encoding="utf-8",
        )
        path = _seed_proposed_strategy(self.repo, self.slug)
        # NO backtest file at all -- if bear-case check doesn't fire
        # first, the backtest-missing REFUSE would surface instead.
        with self.assertRaises(iss.ValidationError) as cm:
            iss.handle_approve_strategy(
                path,
                parent_sha=self.sha,
                now=self.now,
                today=self.today,
                vault_root=self.repo,
            )
        msg = str(cm.exception)
        # Bear-case STALE message should surface -- NOT the backtest
        # "no backtest found" message.
        self.assertIn("stale", msg.lower())
        self.assertNotIn("no backtest found", msg)


if __name__ == "__main__":
    unittest.main()
