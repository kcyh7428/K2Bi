"""Unit tests for scripts.lib.invest_ship_strategy -- Bundle 3 cycle 5.

The helper module is the Python seam for the four strategy/limits
subcommands on `/invest-ship`. Each handler performs Step A (validation)
and Step D (atomic frontmatter edit + parent-sha capture) in one shot;
the skill body stages + commits afterwards.

Coverage:

    * build_trailers happy paths + variant errors (strategy + limits;
      each target status; reject unknown kinds).
    * handle_approve_strategy:
        - happy path edits frontmatter, preserves body, emits trailers.
        - rejects non-proposed status.
        - rejects missing required frontmatter fields.
        - rejects missing order subkeys.
        - rejects filename/name stem mismatch.
        - rejects empty `## How This Works` body.
        - rejects pre-existing approved_at / approved_commit_sha fields.
        - atomic-write: frontmatter tempfile replace, not partial write.
        - frontmatter edit preserves the exact byte layout of all other
          keys (required for the engine's strategy_file_modified_post_approval
          hash gate behaviour on subsequent retirements).
    * handle_reject_strategy:
        - happy path, trailers, --reason stored.
        - rejects non-proposed status.
        - rejects empty reason string.
        - rejects already-rejected file.
    * handle_retire_strategy:
        - happy path flips approved -> retired, body byte-identical.
        - rejects non-approved status.
        - rejects empty reason.
        - preserves approved_at + approved_commit_sha on the retire edit
          (Check D requires all non-status keys byte-identical).
    * handle_approve_limits:
        - happy path applies config.yaml patch + rewrites proposal.
        - rejects non-proposed status.
        - rejects missing `## Change` block.
        - rejects `## Change` with missing keys.
        - rejects unknown `rule:` values.
        - rejects missing `## YAML Patch` section.
        - rejects YAML Patch without before/after labels.
        - rejects identical before/after.
        - rejects before-block not found in config.yaml.
        - rejects before-block matching multiple locations.
        - atomic ordering: config.yaml edit before proposal frontmatter
          flip so a partial failure leaves proposal at status=proposed.
    * CLI:
        - approve-strategy / reject-strategy / retire-strategy /
          approve-limits / build-trailers subcommands exercised via
          subprocess with PYTHONPATH.
        - Validation error -> exit 1 with stderr.
        - Mutual-exclusion + missing --reason failures (argparse).

Tests use tmp repos for handlers that call git (capture_parent_sha).
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import textwrap
import unittest
from datetime import datetime, timezone
from pathlib import Path

# Import under test.
from scripts.lib import invest_ship_strategy as iss


REPO_ROOT = Path(__file__).resolve().parent.parent


def _make_tmp_repo() -> tuple[Path, str]:
    """Create a fresh git repo with one seed commit; return (path, sha).

    We need a real HEAD so capture_parent_sha() returns deterministically.
    """
    tmp = Path(tempfile.mkdtemp(prefix="iss_"))
    subprocess.run(
        ["git", "init", "-q", "-b", "main"], cwd=str(tmp), check=True
    )
    subprocess.run(
        ["git", "config", "user.email", "test@test"], cwd=str(tmp), check=True
    )
    subprocess.run(
        ["git", "config", "user.name", "Test"], cwd=str(tmp), check=True
    )
    subprocess.run(
        ["git", "config", "commit.gpgSign", "false"], cwd=str(tmp), check=True
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


def _write_strategy(
    repo: Path,
    *,
    slug: str = "spy",
    status: str = "proposed",
    include_how: bool = True,
    how_body: str = "Buy SPY at 500 limit with 490 stop.",
    extras: list[str] | None = None,
    order: dict[str, str] | None = None,
    regime_filter: list[str] | None = None,
    missing_fields: list[str] | None = None,
    missing_order_fields: list[str] | None = None,
    name_override: str | None = None,
    filename_override: str | None = None,
) -> Path:
    """Author a strategy file matching spec §2.1 + K2Bi File Conventions.

    Lets individual tests knock out specific fields to exercise the
    Step-A negative paths without duplicating the scaffolding in each
    test method.
    """
    missing_fields = set(missing_fields or [])
    missing_order_fields = set(missing_order_fields or [])
    base_order = {
        "ticker": "SPY",
        "side": "buy",
        "qty": "1",
        "limit_price": "500.00",
        "stop_loss": "490.00",
        "time_in_force": "DAY",
    }
    if order:
        base_order.update(order)
    base_regime = regime_filter or ["risk_on"]
    name_value = name_override or slug
    lines: list[str] = ["---"]
    if "name" not in missing_fields:
        lines.append(f"name: {name_value}")
    lines.append(f"status: {status}")
    if "strategy_type" not in missing_fields:
        lines.append("strategy_type: hand_crafted")
    if "risk_envelope_pct" not in missing_fields:
        lines.append("risk_envelope_pct: 0.01")
    if "regime_filter" not in missing_fields:
        lines.append("regime_filter:")
        for r in base_regime:
            lines.append(f"  - {r}")
    if "order" not in missing_fields:
        lines.append("order:")
        for k, v in base_order.items():
            if k in missing_order_fields:
                continue
            lines.append(f"  {k}: {v}")
    # Append extras verbatim (for tests that need approved_at pre-existing,
    # retired_at pre-existing, etc.).
    for line in extras or []:
        lines.append(line)
    lines += [
        "tags: [strategy, SPY]",
        "date: 2026-04-19",
        "type: strategy",
        "origin: keith",
        'up: "[[index]]"',
        "---",
        "",
    ]
    if include_how:
        lines += ["## How This Works", "", how_body]
    content = "\n".join(lines) + "\n"
    strat_dir = repo / "wiki" / "strategies"
    strat_dir.mkdir(parents=True, exist_ok=True)
    filename = filename_override or f"strategy_{slug}.md"
    p = strat_dir / filename
    p.write_text(content, encoding="utf-8")
    return p


def _write_limits_proposal(
    repo: Path,
    *,
    slug: str = "widen-size",
    date: str = "2026-04-19",
    status: str = "proposed",
    rule: str = "position_size",
    change_type: str = "widen",
    before_block: str | None = None,
    after_block: str | None = None,
    include_change: bool = True,
    include_yaml_patch: bool = True,
    approved_at: str | None = None,
    approved_commit_sha: str | None = None,
    type_value: str = "limits-proposal",
    applies_to: str = "execution/validators/config.yaml",
    # Tests can mangle the block labels to exercise error paths.
    before_label: str = "before:",
    after_label: str = "after:",
) -> Path:
    if before_block is None:
        before_block = "  max_trade_risk_pct: 0.01  # 1% per trade"
    if after_block is None:
        after_block = (
            "  max_trade_risk_pct: 0.02  # 2% per trade (approved 2026-04-19)"
        )
    lines = [
        "---",
        "tags: [review, strategy-approvals, limits-proposal]",
        f"date: {date}",
        f"type: {type_value}",
        "origin: keith",
        f"status: {status}",
        f"applies-to: {applies_to}",
    ]
    if approved_at:
        lines.append(f"approved_at: {approved_at}")
    if approved_commit_sha:
        lines.append(f"approved_commit_sha: {approved_commit_sha}")
    lines += [
        'up: "[[index]]"',
        "---",
        "",
        f"# Limits Proposal: {slug}",
        "",
    ]
    if include_change:
        lines += [
            "## Change",
            "",
            "```yaml",
            f"rule: {rule}",
            f"change_type: {change_type}",
            "before: 0.01",
            "after: 0.02",
            "```",
            "",
        ]
    lines += [
        "## Rationale (Keith's)",
        "",
        "More cap efficiency.",
        "",
        "## Safety Impact (skill's assessment)",
        "",
        "Doubles loss per trade.",
        "",
    ]
    if include_yaml_patch:
        lines += [
            "## YAML Patch",
            "",
            before_label,
            "",
            "```yaml",
            before_block,
            "```",
            "",
            after_label,
            "",
            "```yaml",
            after_block,
            "```",
            "",
        ]
    lines += ["## Approval", "", "Pending Keith.", ""]
    approvals_dir = repo / "review" / "strategy-approvals"
    approvals_dir.mkdir(parents=True, exist_ok=True)
    p = approvals_dir / f"{date}_limits-proposal_{slug}.md"
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return p


def _seed_bear_case_proceed(
    repo: Path,
    ticker: str = "SPY",
    *,
    days_old: int = 0,
    today: str | None = None,
) -> Path:
    """Seed wiki/tickers/<TICKER>.md with a minimal thesis + fresh
    PROCEED bear-case so `handle_approve_strategy` passes the Bundle 4
    cycle 2 bear-case freshness gate. `days_old` offsets bear-last-
    verified backwards from `today` (default 0 = same-day fresh).

    `today` default is the real calendar date so CLI subprocess tests
    (which cannot pin `date.today()`) remain in the freshness window.
    Fixed-clock tests that call handle_approve_strategy directly pass
    both `today=` here AND `today=` to the handler so the scan and the
    seed agree on the reference date.
    """
    from datetime import date as _date
    from datetime import timedelta
    base_date = _date.fromisoformat(today) if today else _date.today()
    bear_date = (base_date - timedelta(days=days_old)).isoformat()
    today_str = base_date.isoformat()
    tickers_dir = repo / "wiki" / "tickers"
    tickers_dir.mkdir(parents=True, exist_ok=True)
    path = tickers_dir / f"{ticker}.md"
    path.write_text(
        "---\n"
        f"tags: [ticker, {ticker}, thesis]\n"
        f"date: {today_str}\n"
        "type: ticker\n"
        "origin: k2bi-extract\n"
        'up: "[[tickers/index]]"\n'
        f"symbol: {ticker}\n"
        f"thesis-last-verified: {today_str}\n"
        "thesis_score: 73\n"
        f"bear-last-verified: {bear_date}\n"
        "bear_conviction: 40\n"
        "bear_top_counterpoints:\n"
        "  - c1\n  - c2\n  - c3\n"
        "bear_invalidation_scenarios:\n"
        "  - s1\n  - s2\n"
        "bear_verdict: PROCEED\n"
        "---\n\n"
        f"## Phase 1: Business Model\n\ndummy\n",
        encoding="utf-8",
    )
    return path


def _seed_backtest_passed(
    repo: Path,
    slug: str = "spy",
    *,
    date: str | None = None,
    look_ahead_check: str = "passed",
    look_ahead_check_reason: str = "",
) -> Path:
    """Seed raw/backtests/<date>_<slug>_backtest.md with a valid-schema
    capture so `handle_approve_strategy`'s Bundle 4 cycle 3 backtest-
    gate scan finds a fresh PROCEED. Default is today (real calendar)
    with look_ahead_check=passed so CLI subprocess tests work without
    pinning the clock. Fixed-clock tests can pin the date explicitly.
    """
    from datetime import date as _date
    import yaml

    backtests_dir = repo / "raw" / "backtests"
    backtests_dir.mkdir(parents=True, exist_ok=True)
    d = date or _date.today().isoformat()
    fm = {
        "tags": ["backtest", slug, "raw"],
        "date": d,
        "type": "backtest",
        "origin": "k2bi-generate",
        "up": "[[backtests/index]]",
        "strategy_slug": slug,
        "strategy_commit_sha": "abc123def456",
        "backtest": {
            "window": {"start": "2024-04-19", "end": d},
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
            "last_run": f"{d}T10:00:00+00:00",
        },
    }
    path = backtests_dir / f"{d}_{slug}_backtest.md"
    path.write_text(
        "---\n"
        + yaml.safe_dump(fm, sort_keys=False, default_flow_style=False)
        + "---\n\nbody\n",
        encoding="utf-8",
    )
    return path


def _write_config_yaml(
    repo: Path, content: str | None = None
) -> Path:
    cfg_dir = repo / "execution" / "validators"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    p = cfg_dir / "config.yaml"
    if content is None:
        content = textwrap.dedent(
            """\
            position_size:
              max_trade_risk_pct: 0.01  # 1% per trade
              max_ticker_concentration_pct: 0.20
            leverage:
              cash_only: true
              max_leverage: 1.0
            """
        )
    p.write_text(content, encoding="utf-8")
    return p


# ---------- build_trailers ----------


class BuildTrailersTests(unittest.TestCase):
    def test_approve_trailers(self):
        trailers = iss.build_trailers(
            "strategy", "proposed -> approved", "spy-rotational"
        )
        self.assertEqual(
            trailers,
            [
                "Strategy-Transition: proposed -> approved",
                "Approved-Strategy: strategy_spy-rotational",
                "Co-Shipped-By: invest-ship",
            ],
        )

    def test_reject_trailers(self):
        trailers = iss.build_trailers(
            "strategy", "proposed -> rejected", "meanrev"
        )
        self.assertEqual(trailers[1], "Rejected-Strategy: strategy_meanrev")
        self.assertIn("Strategy-Transition: proposed -> rejected", trailers)
        self.assertIn("Co-Shipped-By: invest-ship", trailers)

    def test_retire_trailers(self):
        trailers = iss.build_trailers(
            "strategy", "approved -> retired", "foo"
        )
        self.assertEqual(trailers[1], "Retired-Strategy: strategy_foo")
        self.assertIn("Strategy-Transition: approved -> retired", trailers)

    def test_limits_trailers(self):
        trailers = iss.build_trailers(
            "limits",
            "proposed -> approved",
            "widen-size",
            rule="position_size",
            change_type="widen",
        )
        self.assertEqual(
            trailers,
            [
                "Limits-Transition: proposed -> approved",
                "Approved-Limits: widen-size",
                "Config-Change: position_size:widen",
                "Co-Shipped-By: invest-ship",
            ],
        )

    def test_limits_requires_rule_and_change_type(self):
        with self.assertRaises(ValueError):
            iss.build_trailers(
                "limits", "proposed -> approved", "foo"
            )

    def test_limits_rejects_non_approved_target(self):
        with self.assertRaises(ValueError):
            iss.build_trailers(
                "limits",
                "proposed -> rejected",
                "foo",
                rule="position_size",
                change_type="widen",
            )

    def test_strategy_rejects_unknown_target_status(self):
        with self.assertRaises(ValueError):
            iss.build_trailers(
                "strategy", "proposed -> mystery", "slug"
            )

    def test_unknown_kind(self):
        with self.assertRaises(ValueError):
            iss.build_trailers("notakind", "proposed -> approved", "slug")


# ---------- handle_approve_strategy ----------


class HandleApproveStrategyTests(unittest.TestCase):
    def setUp(self):
        self.repo, self.parent_sha = _make_tmp_repo()

    def tearDown(self):
        subprocess.run(["rm", "-rf", str(self.repo)], check=False)

    def test_happy_path_rewrites_frontmatter(self):
        _seed_bear_case_proceed(
            self.repo, ticker="SPY", today="2026-04-19",
        )
        _seed_backtest_passed(self.repo, slug="spy", date="2026-04-19")
        path = _write_strategy(self.repo, slug="spy")
        now = datetime(2026, 4, 19, 10, 0, 0, tzinfo=timezone.utc)
        hints = iss.handle_approve_strategy(
            path,
            now=now,
            today=_dt.date(2026, 4, 19),
            vault_root=self.repo,
        )

        self.assertEqual(hints.slug, "spy")
        self.assertEqual(hints.transition, "proposed -> approved")
        self.assertEqual(hints.parent_commit_sha, self.parent_sha)
        self.assertEqual(hints.commit_subject, "feat(strategy): approve spy")
        self.assertEqual(
            hints.timestamp_value, "2026-04-19T10:00:00.000000+00:00"
        )
        self.assertIn(
            "Strategy-Transition: proposed -> approved", hints.trailers
        )
        self.assertIn(
            "Approved-Strategy: strategy_spy", hints.trailers
        )
        self.assertIn("Co-Shipped-By: invest-ship", hints.trailers)

        rewritten = path.read_text(encoding="utf-8")
        self.assertIn("status: approved", rewritten)
        self.assertNotIn("status: proposed", rewritten)
        self.assertIn("approved_at:", rewritten)
        # PyYAML safe_dump may quote an all-digit short sha; re-parse
        # the frontmatter so the assertion tests the semantic value.
        import yaml as _yaml

        fm_after = _yaml.safe_load(rewritten.split("---", 2)[1])
        self.assertEqual(
            str(fm_after["approved_commit_sha"]), self.parent_sha
        )
        # Body preserved exactly.
        self.assertIn("## How This Works", rewritten)
        self.assertIn(
            "Buy SPY at 500 limit with 490 stop.", rewritten
        )

    def test_preserves_body_byte_for_byte(self):
        # Spec §4.1 Check D requires that the approve step leaves the body
        # identical so a subsequent retire can satisfy the content-
        # immutability invariant.
        # NB: _write_strategy prepends `## How This Works\n\n` itself, so
        # `how_body` is just the prose after that heading.
        _seed_bear_case_proceed(
            self.repo, ticker="SPY", today="2026-04-19",
        )
        _seed_backtest_passed(self.repo, slug="spy", date="2026-04-19")
        body = (
            "Line one.\n"
            "\n"
            "Line two with special chars: `\"'` and unicode ç é 中.\n"
        )
        path = _write_strategy(self.repo, how_body=body)
        original = path.read_bytes()
        iss.handle_approve_strategy(
            path, today=_dt.date(2026, 4, 19), vault_root=self.repo,
        )
        edited = path.read_bytes()
        # Body slice starts after the second `---\n` boundary.
        def body_bytes(b: bytes) -> bytes:
            parts = b.split(b"---\n", 2)
            return parts[2] if len(parts) > 2 else b""

        self.assertEqual(body_bytes(original), body_bytes(edited))

    def test_rejects_non_proposed_status(self):
        path = _write_strategy(self.repo, status="approved")
        with self.assertRaises(iss.ValidationError) as cm:
            iss.handle_approve_strategy(path, parent_sha=self.parent_sha)
        self.assertIn("status is 'approved'", str(cm.exception))

    def test_rejects_missing_required_fields(self):
        path = _write_strategy(
            self.repo, missing_fields=["strategy_type"]
        )
        with self.assertRaises(iss.ValidationError) as cm:
            iss.handle_approve_strategy(path, parent_sha=self.parent_sha)
        self.assertIn("strategy_type", str(cm.exception))

    def test_rejects_missing_order_field(self):
        path = _write_strategy(
            self.repo, missing_order_fields=["stop_loss"]
        )
        with self.assertRaises(iss.ValidationError) as cm:
            iss.handle_approve_strategy(path, parent_sha=self.parent_sha)
        self.assertIn("stop_loss", str(cm.exception))

    def test_rejects_filename_name_mismatch(self):
        path = _write_strategy(
            self.repo,
            slug="foo",
            filename_override="strategy_bar.md",
            name_override="bar",
        )
        # Frontmatter name=bar matches filename strategy_bar; this should
        # pass shape check. Now flip to mismatch:
        path.unlink()
        path = _write_strategy(
            self.repo,
            slug="foo",
            filename_override="strategy_bar.md",
            name_override="foo",
        )
        with self.assertRaises(iss.ValidationError) as cm:
            iss.handle_approve_strategy(path, parent_sha=self.parent_sha)
        self.assertIn("filename stem", str(cm.exception))

    def test_rejects_empty_how_this_works(self):
        path = _write_strategy(self.repo, how_body="")
        with self.assertRaises(iss.ValidationError) as cm:
            iss.handle_approve_strategy(path, parent_sha=self.parent_sha)
        self.assertIn("How This Works", str(cm.exception))

    def test_rejects_missing_how_this_works_section(self):
        path = _write_strategy(self.repo, include_how=False)
        with self.assertRaises(iss.ValidationError) as cm:
            iss.handle_approve_strategy(path, parent_sha=self.parent_sha)
        self.assertIn("How This Works", str(cm.exception))

    def test_rejects_preexisting_approved_at(self):
        path = _write_strategy(
            self.repo,
            extras=[
                "approved_at: 2026-04-18T10:00:00Z",
                "approved_commit_sha: old1234",
            ],
        )
        with self.assertRaises(iss.ValidationError) as cm:
            iss.handle_approve_strategy(path, parent_sha=self.parent_sha)
        self.assertIn("already present", str(cm.exception))

    def test_rejects_missing_file(self):
        path = self.repo / "wiki" / "strategies" / "strategy_nope.md"
        with self.assertRaises(iss.ValidationError):
            iss.handle_approve_strategy(path, parent_sha=self.parent_sha)

    def test_rejects_off_canonical_path(self):
        # Codex R7 P1 #1: a strategy file outside the hook's canonical
        # `^wiki/strategies/strategy_[^/]+\.md$` glob must be rejected
        # at Step A. Otherwise a retire run would skip the post-commit
        # sentinel write (hook doesn't scan the off-path file) and the
        # engine retirement gate would silently stay open.
        off_path_dir = self.repo / "wiki" / "strategies" / "archive"
        off_path_dir.mkdir(parents=True, exist_ok=True)
        content = (self.repo / "wiki" / "strategies" / "strategy_spy.md")
        # Seed a valid proposed file first, then move it to an off-path
        # location so only the path differs.
        valid = _write_strategy(self.repo, slug="spy")
        moved = off_path_dir / "strategy_spy.md"
        moved.write_bytes(valid.read_bytes())
        valid.unlink()
        with self.assertRaises(iss.ValidationError) as cm:
            iss.handle_approve_strategy(moved, parent_sha=self.parent_sha)
        self.assertIn("canonical path", str(cm.exception).lower() + " canonical path")
        # Substring check on the real message:
        self.assertIn("wiki/strategies/strategy_", str(cm.exception))

    def test_rejects_malformed_yaml(self):
        path = _write_strategy(self.repo, slug="badyaml")
        # Corrupt the frontmatter.
        path.write_text(
            "---\nstatus: proposed\ntabs_and_colons: [unclosed\n---\nbody",
            encoding="utf-8",
        )
        with self.assertRaises(iss.ValidationError):
            iss.handle_approve_strategy(path, parent_sha=self.parent_sha)


# ---------- Bundle 4 cycle 2: bear-case gate through handle_approve_strategy ----------


class BearCaseGateThroughApprovalTests(unittest.TestCase):
    """MiniMax review finding #1 (critical): end-to-end tests that the
    bear-case gate REFUSE path correctly propagates through
    handle_approve_strategy -- exercising order.ticker extraction +
    vault_root resolution + ValidationError surfacing.

    The scan helper is unit-tested in test_approval_bear_case_gate.py;
    these tests prove the WIRING (handler -> scan -> raise).
    """

    def setUp(self):
        self.repo, self.parent_sha = _make_tmp_repo()
        self.today = _dt.date(2026, 4, 19)

    def tearDown(self):
        subprocess.run(["rm", "-rf", str(self.repo)], check=False)

    def test_no_thesis_file_refuses_approval(self):
        # No wiki/tickers/SPY.md at all -- gate must refuse.
        path = _write_strategy(self.repo, slug="spy")
        with self.assertRaises(iss.ValidationError) as cm:
            iss.handle_approve_strategy(
                path,
                parent_sha=self.parent_sha,
                today=self.today,
                vault_root=self.repo,
            )
        self.assertIn("bear-case", str(cm.exception).lower())
        self.assertIn("SPY", str(cm.exception))

    def test_stale_bear_case_refuses_approval(self):
        # Seed bear-case 45 days old -- stale.
        _seed_bear_case_proceed(
            self.repo, ticker="SPY",
            days_old=45, today=self.today.isoformat(),
        )
        path = _write_strategy(self.repo, slug="spy")
        with self.assertRaises(iss.ValidationError) as cm:
            iss.handle_approve_strategy(
                path,
                parent_sha=self.parent_sha,
                today=self.today,
                vault_root=self.repo,
            )
        self.assertIn("stale", str(cm.exception).lower())

    def test_veto_bear_case_refuses_approval(self):
        # Seed bear_verdict: VETO with fresh date.
        tickers_dir = self.repo / "wiki" / "tickers"
        tickers_dir.mkdir(parents=True, exist_ok=True)
        fresh = self.today.isoformat()
        (tickers_dir / "SPY.md").write_text(
            "---\n"
            "tags: [ticker, SPY, thesis]\n"
            f"date: {fresh}\n"
            "type: ticker\n"
            "origin: k2bi-extract\n"
            'up: "[[tickers/index]]"\n'
            "symbol: SPY\n"
            f"thesis-last-verified: {fresh}\n"
            "thesis_score: 73\n"
            f"bear-last-verified: {fresh}\n"
            "bear_conviction: 85\n"
            "bear_verdict: VETO\n"
            "bear_top_counterpoints:\n"
            "  - c1\n  - c2\n  - c3\n"
            "bear_invalidation_scenarios:\n"
            "  - s1\n  - s2\n"
            "---\n\n"
            "## Phase 1: Business Model\n\ndummy\n",
            encoding="utf-8",
        )
        path = _write_strategy(self.repo, slug="spy")
        with self.assertRaises(iss.ValidationError) as cm:
            iss.handle_approve_strategy(
                path,
                parent_sha=self.parent_sha,
                today=self.today,
                vault_root=self.repo,
            )
        self.assertIn("VETO", str(cm.exception))
        self.assertIn("85", str(cm.exception))

    def test_malformed_ticker_frontmatter_refuses_approval(self):
        # Unterminated frontmatter fence -- scan refuses with parse error.
        tickers_dir = self.repo / "wiki" / "tickers"
        tickers_dir.mkdir(parents=True, exist_ok=True)
        (tickers_dir / "SPY.md").write_text(
            "---\nsymbol: SPY\nno closing fence\n",
            encoding="utf-8",
        )
        path = _write_strategy(self.repo, slug="spy")
        with self.assertRaises(iss.ValidationError) as cm:
            iss.handle_approve_strategy(
                path,
                parent_sha=self.parent_sha,
                today=self.today,
                vault_root=self.repo,
            )
        self.assertIn("parse", str(cm.exception).lower())
        self.assertIn("SPY", str(cm.exception))

    def test_missing_order_ticker_refuses_approval(self):
        # If the strategy's order.ticker is missing, the gate guard fires.
        _seed_bear_case_proceed(
            self.repo, ticker="SPY", today=self.today.isoformat(),
        )
        path = _write_strategy(
            self.repo, slug="spy", missing_order_fields=["ticker"],
        )
        with self.assertRaises(iss.ValidationError) as cm:
            iss.handle_approve_strategy(
                path,
                parent_sha=self.parent_sha,
                today=self.today,
                vault_root=self.repo,
            )
        # `_validate_strategy_shape` fires FIRST with the missing-key
        # message -- this proves the ordering: shape check precedes the
        # bear-case scan so a malformed strategy does not hit the gate.
        self.assertIn("ticker", str(cm.exception).lower())


# ---------- handle_reject_strategy ----------


class HandleRejectStrategyTests(unittest.TestCase):
    def setUp(self):
        self.repo, self.parent_sha = _make_tmp_repo()

    def tearDown(self):
        subprocess.run(["rm", "-rf", str(self.repo)], check=False)

    def test_happy_path(self):
        path = _write_strategy(self.repo, slug="spy")
        hints = iss.handle_reject_strategy(
            path, "stop too tight for realistic fills"
        )
        self.assertEqual(hints.transition, "proposed -> rejected")
        self.assertIn(
            "Rejected-Strategy: strategy_spy", hints.trailers
        )
        self.assertEqual(hints.reason, "stop too tight for realistic fills")
        rewritten = path.read_text(encoding="utf-8")
        self.assertIn("status: rejected", rewritten)
        self.assertIn("rejected_at:", rewritten)
        self.assertIn("rejected_reason: stop too tight", rewritten)
        # Reject does NOT set approved_commit_sha.
        self.assertIsNone(hints.parent_commit_sha)

    def test_rejects_non_proposed_status(self):
        path = _write_strategy(self.repo, status="approved")
        with self.assertRaises(iss.ValidationError):
            iss.handle_reject_strategy(path, "too late")

    def test_rejects_empty_reason(self):
        path = _write_strategy(self.repo, slug="spy")
        with self.assertRaises(iss.ValidationError) as cm:
            iss.handle_reject_strategy(path, "   ")
        self.assertIn("non-empty string", str(cm.exception))

    def test_rejects_already_rejected_file(self):
        # Author a file whose frontmatter already has rejected_at;
        # status=proposed still allows Step A to proceed to the
        # _require_no_fields check.
        path = _write_strategy(
            self.repo,
            extras=[
                "rejected_at: 2026-04-18T10:00:00Z",
                'rejected_reason: "stale"',
            ],
        )
        with self.assertRaises(iss.ValidationError) as cm:
            iss.handle_reject_strategy(path, "again")
        self.assertIn("already present", str(cm.exception))

    def test_reason_with_special_chars_escapes(self):
        # YAML safe_dump should quote the reason when it contains a
        # colon, a quote, or other YAML-significant chars.
        path = _write_strategy(self.repo, slug="spy")
        reason = 'too risky: "stop too wide" for regime'
        iss.handle_reject_strategy(path, reason)
        rewritten = path.read_text(encoding="utf-8")
        # Round-trip through strategy_frontmatter.parse to assert the
        # edited YAML is still valid + readable.
        import yaml

        fm = yaml.safe_load(
            rewritten.split("---")[1]
        )
        self.assertEqual(fm["rejected_reason"], reason)


# ---------- handle_retire_strategy ----------


class HandleRetireStrategyTests(unittest.TestCase):
    def setUp(self):
        self.repo, self.parent_sha = _make_tmp_repo()

    def tearDown(self):
        subprocess.run(["rm", "-rf", str(self.repo)], check=False)

    def test_happy_path_preserves_other_keys(self):
        # Approved file has approved_at + approved_commit_sha. Retire must
        # preserve those byte-for-byte (Check D requires all non-status
        # keys to be byte-identical).
        extras = [
            "approved_at: 2026-04-18T10:00:00Z",
            "approved_commit_sha: abc1234",
        ]
        path = _write_strategy(
            self.repo, slug="spy", status="approved", extras=extras
        )
        original_bytes = path.read_bytes()
        hints = iss.handle_retire_strategy(path, "obsolete after earnings")

        self.assertEqual(hints.transition, "approved -> retired")
        self.assertIn("Retired-Strategy: strategy_spy", hints.trailers)
        new_bytes = path.read_bytes()
        # Body (everything after second `---`) is byte-identical.
        def body(b: bytes) -> bytes:
            parts = b.split(b"---\n", 2)
            return parts[2] if len(parts) > 2 else b""

        self.assertEqual(body(original_bytes), body(new_bytes))
        # approved_at + approved_commit_sha lines still present verbatim.
        rewritten = path.read_text(encoding="utf-8")
        self.assertIn("approved_at: 2026-04-18T10:00:00Z", rewritten)
        self.assertIn("approved_commit_sha: abc1234", rewritten)
        self.assertIn("status: retired", rewritten)
        self.assertIn("retired_at:", rewritten)
        self.assertIn("retired_reason: obsolete after earnings", rewritten)

    def test_rejects_non_approved_status(self):
        path = _write_strategy(self.repo, slug="spy", status="proposed")
        with self.assertRaises(iss.ValidationError):
            iss.handle_retire_strategy(path, "too early")

    def test_rejects_empty_reason(self):
        path = _write_strategy(
            self.repo,
            status="approved",
            extras=[
                "approved_at: 2026-04-18T10:00:00Z",
                "approved_commit_sha: abc1234",
            ],
        )
        with self.assertRaises(iss.ValidationError):
            iss.handle_retire_strategy(path, "")

    def test_rejects_already_retired(self):
        path = _write_strategy(
            self.repo,
            status="approved",
            extras=[
                "approved_at: 2026-04-18T10:00:00Z",
                "approved_commit_sha: abc1234",
                "retired_at: 2026-04-18T20:00:00Z",
                'retired_reason: "prior retire"',
            ],
        )
        with self.assertRaises(iss.ValidationError):
            iss.handle_retire_strategy(path, "again")


# ---------- handle_approve_limits ----------


class HandleApproveLimitsTests(unittest.TestCase):
    def setUp(self):
        self.repo, self.parent_sha = _make_tmp_repo()
        self.cfg = _write_config_yaml(self.repo)

    def tearDown(self):
        subprocess.run(["rm", "-rf", str(self.repo)], check=False)

    def test_happy_path_applies_patch_and_rewrites_proposal(self):
        proposal = _write_limits_proposal(self.repo)
        hints = iss.handle_approve_limits(
            proposal, config_path=self.cfg, parent_sha=self.parent_sha
        )
        self.assertEqual(hints.transition, "proposed -> approved")
        self.assertEqual(hints.rule, "position_size")
        self.assertEqual(hints.change_type, "widen")
        self.assertEqual(hints.slug, "widen-size")
        self.assertIn(
            "Limits-Transition: proposed -> approved", hints.trailers
        )
        self.assertIn("Approved-Limits: widen-size", hints.trailers)
        self.assertIn(
            "Config-Change: position_size:widen", hints.trailers
        )

        cfg_text = self.cfg.read_text(encoding="utf-8")
        self.assertIn(
            "max_trade_risk_pct: 0.02  # 2% per trade", cfg_text
        )
        self.assertNotIn("max_trade_risk_pct: 0.01", cfg_text)

        proposal_text = proposal.read_text(encoding="utf-8")
        self.assertIn("status: approved", proposal_text)
        # PyYAML may quote all-digit short shas; re-parse to assert
        # the semantic value survived the round-trip.
        import yaml as _yaml

        proposal_fm = _yaml.safe_load(proposal_text.split("---", 2)[1])
        self.assertEqual(
            str(proposal_fm["approved_commit_sha"]), self.parent_sha
        )

    def test_rejects_non_proposed_status(self):
        proposal = _write_limits_proposal(self.repo, status="approved")
        with self.assertRaises(iss.ValidationError):
            iss.handle_approve_limits(
                proposal, config_path=self.cfg, parent_sha=self.parent_sha
            )

    def test_rejects_bad_type(self):
        proposal = _write_limits_proposal(
            self.repo, type_value="some-other-type"
        )
        with self.assertRaises(iss.ValidationError) as cm:
            iss.handle_approve_limits(
                proposal, config_path=self.cfg, parent_sha=self.parent_sha
            )
        self.assertIn("type:", str(cm.exception))

    def test_rejects_bad_applies_to(self):
        proposal = _write_limits_proposal(
            self.repo, applies_to="execution/validators/wrong.yaml"
        )
        with self.assertRaises(iss.ValidationError) as cm:
            iss.handle_approve_limits(
                proposal, config_path=self.cfg, parent_sha=self.parent_sha
            )
        self.assertIn("applies-to:", str(cm.exception))

    def test_rejects_missing_change_block(self):
        proposal = _write_limits_proposal(self.repo, include_change=False)
        with self.assertRaises(iss.ValidationError) as cm:
            iss.handle_approve_limits(
                proposal, config_path=self.cfg, parent_sha=self.parent_sha
            )
        self.assertIn("## Change", str(cm.exception))

    def test_rejects_unknown_rule(self):
        proposal = _write_limits_proposal(self.repo, rule="bogus_rule")
        with self.assertRaises(iss.ValidationError) as cm:
            iss.handle_approve_limits(
                proposal, config_path=self.cfg, parent_sha=self.parent_sha
            )
        self.assertIn("rule:", str(cm.exception))

    def test_rejects_unknown_change_type(self):
        proposal = _write_limits_proposal(
            self.repo, change_type="sideways"
        )
        with self.assertRaises(iss.ValidationError) as cm:
            iss.handle_approve_limits(
                proposal, config_path=self.cfg, parent_sha=self.parent_sha
            )
        self.assertIn("change_type:", str(cm.exception))

    def test_rejects_missing_yaml_patch_section(self):
        proposal = _write_limits_proposal(
            self.repo, include_yaml_patch=False
        )
        with self.assertRaises(iss.ValidationError) as cm:
            iss.handle_approve_limits(
                proposal, config_path=self.cfg, parent_sha=self.parent_sha
            )
        self.assertIn("YAML Patch", str(cm.exception))

    def test_rejects_identical_before_after(self):
        same = "  max_trade_risk_pct: 0.01  # 1% per trade"
        proposal = _write_limits_proposal(
            self.repo, before_block=same, after_block=same
        )
        with self.assertRaises(iss.ValidationError) as cm:
            iss.handle_approve_limits(
                proposal, config_path=self.cfg, parent_sha=self.parent_sha
            )
        self.assertIn("no-op", str(cm.exception))

    def test_rejects_before_not_found_in_config(self):
        proposal = _write_limits_proposal(
            self.repo,
            before_block="  does_not_exist: 42",
            after_block="  does_not_exist: 43",
        )
        with self.assertRaises(iss.ValidationError) as cm:
            iss.handle_approve_limits(
                proposal, config_path=self.cfg, parent_sha=self.parent_sha
            )
        self.assertIn("not found", str(cm.exception))

    def test_rejects_multiple_matches_in_config(self):
        # Replace config with one that has duplicate lines to make the
        # before-block ambiguous.
        self.cfg.write_text(
            "first:\n  shared: value\n"
            "second:\n  shared: value\n",
            encoding="utf-8",
        )
        proposal = _write_limits_proposal(
            self.repo,
            before_block="  shared: value",
            after_block="  shared: new",
        )
        with self.assertRaises(iss.ValidationError) as cm:
            iss.handle_approve_limits(
                proposal, config_path=self.cfg, parent_sha=self.parent_sha
            )
        self.assertIn("uniquely identifiable", str(cm.exception))

    def test_atomic_ordering_on_config_failure(self):
        # When config.yaml search-and-replace fails (before-block missing),
        # the proposal must NOT be flipped to approved. Keith re-runs
        # after fixing the patch without cleanup.
        proposal = _write_limits_proposal(
            self.repo,
            before_block="  does_not_exist: 42",
            after_block="  does_not_exist: 43",
        )
        with self.assertRaises(iss.ValidationError):
            iss.handle_approve_limits(
                proposal, config_path=self.cfg, parent_sha=self.parent_sha
            )
        proposal_text = proposal.read_text(encoding="utf-8")
        self.assertIn("status: proposed", proposal_text)
        self.assertNotIn("approved_at", proposal_text)

    def test_rollback_on_proposal_write_failure(self):
        # R4-minimax F1: if the proposal write fails AFTER config.yaml
        # has been patched, the handler MUST roll the config edit back
        # so the caller never sees a partial-commit state ("config
        # applied, proposal still proposed"). Simulate by monkey-
        # patching _atomic_write_bytes to raise on the second call
        # (proposal write) but succeed on the first (config write).
        proposal = _write_limits_proposal(self.repo)
        original_config = self.cfg.read_text(encoding="utf-8")

        real_write = iss._atomic_write_bytes
        call_count = {"n": 0}

        def failing_second(path, content):
            call_count["n"] += 1
            if call_count["n"] == 2:
                raise OSError(28, "No space left on device (simulated)")
            return real_write(path, content)

        iss._atomic_write_bytes = failing_second
        try:
            with self.assertRaises(iss.ValidationError) as cm:
                iss.handle_approve_limits(
                    proposal,
                    config_path=self.cfg,
                    parent_sha=self.parent_sha,
                )
            self.assertIn("rolled back", str(cm.exception))
        finally:
            iss._atomic_write_bytes = real_write

        # Config is restored to its pre-edit state.
        self.assertEqual(
            self.cfg.read_text(encoding="utf-8"),
            original_config,
            "config.yaml was not rolled back after proposal write failed",
        )
        # Proposal is still at status=proposed.
        self.assertIn(
            "status: proposed",
            proposal.read_text(encoding="utf-8"),
        )

    def test_malformed_yaml_after_block_rejected_with_rollback(self):
        # R6-minimax F3: a `## YAML Patch` after-block that is not
        # valid YAML must be caught before the proposal is flipped,
        # and config.yaml must be rolled back to its pre-edit state.
        original_config = self.cfg.read_text(encoding="utf-8")
        proposal = _write_limits_proposal(
            self.repo,
            before_block="  max_trade_risk_pct: 0.01  # 1% per trade",
            # Unclosed flow-style mapping -- yaml.safe_load raises.
            after_block='  max_trade_risk_pct: "0.02',
        )
        with self.assertRaises(iss.ValidationError) as cm:
            iss.handle_approve_limits(
                proposal, config_path=self.cfg, parent_sha=self.parent_sha
            )
        msg = str(cm.exception)
        self.assertIn("would not parse as YAML", msg)
        # Config is rolled back so no downstream consumer sees the
        # malformed bytes.
        self.assertEqual(
            self.cfg.read_text(encoding="utf-8"),
            original_config,
            "config.yaml was not rolled back after malformed patch",
        )
        self.assertIn(
            "status: proposed",
            proposal.read_text(encoding="utf-8"),
        )

    def test_concurrent_modification_refuses_rollback(self):
        # R6-minimax F1: if config.yaml on disk has diverged from the
        # patched state (a concurrent writer modified it between our
        # patch and the rollback), the handler must refuse to roll
        # back over that change and raise a clear error. Simulated by
        # making the proposal write fail (trigger rollback) AFTER
        # mutating config.yaml so it no longer matches new_config.
        proposal = _write_limits_proposal(self.repo)

        real_write = iss._atomic_write_bytes
        call_count = {"n": 0}
        cfg_path = self.cfg

        def failing_proposal_with_concurrent_peer(path, content):
            call_count["n"] += 1
            if call_count["n"] == 2:
                # Simulate a peer process mutating config between our
                # patch (call 1) and our rollback attempt (would be
                # call 3 if we got there). Write a distinctive marker
                # directly (bypass _atomic_write_bytes so we don't
                # increment the counter).
                cfg_path.write_text(
                    cfg_path.read_text(encoding="utf-8")
                    + "# concurrent peer wrote this line\n",
                    encoding="utf-8",
                )
                raise OSError(28, "simulated proposal write failure")
            return real_write(path, content)

        iss._atomic_write_bytes = failing_proposal_with_concurrent_peer
        try:
            with self.assertRaises(iss.ValidationError) as cm:
                iss.handle_approve_limits(
                    proposal,
                    config_path=self.cfg,
                    parent_sha=self.parent_sha,
                )
            msg = str(cm.exception)
            self.assertIn("concurrent modification", msg)
        finally:
            iss._atomic_write_bytes = real_write

        # The peer's modification is preserved -- we did NOT roll back
        # over it. This is the desired safety property.
        cfg_text = self.cfg.read_text(encoding="utf-8")
        self.assertIn("# concurrent peer wrote this line", cfg_text)

    def test_rollback_of_rollback_failure_surfaces_manual_recovery(self):
        # R5-minimax F1 + F3: when BOTH the proposal write AND the
        # config rollback fail, the handler must surface a clear
        # "manual recovery required" ValidationError rather than
        # silently leaving the system in an inconsistent state.
        # Exercising this path is the only way to prove the error
        # message is wired to the rollback-fails branch.
        proposal = _write_limits_proposal(self.repo)

        real_write = iss._atomic_write_bytes
        call_count = {"n": 0}

        def failing_after_first(path, content):
            call_count["n"] += 1
            if call_count["n"] == 1:
                return real_write(path, content)
            # Calls 2 (proposal write) and 3 (rollback) both fail.
            raise OSError(28, f"simulated write failure on call {call_count['n']}")

        iss._atomic_write_bytes = failing_after_first
        try:
            with self.assertRaises(iss.ValidationError) as cm:
                iss.handle_approve_limits(
                    proposal,
                    config_path=self.cfg,
                    parent_sha=self.parent_sha,
                )
            message = str(cm.exception)
            self.assertIn("manual recovery", message)
            self.assertIn("config rollback failed", message)
        finally:
            iss._atomic_write_bytes = real_write

        # With rollback itself failed, config is in the patched state
        # (not the original) and proposal is still proposed. This is
        # the documented "manual recovery required" state -- Keith
        # gets a loud error and intervenes via git restore.
        self.assertIn(
            "max_trade_risk_pct: 0.02",
            self.cfg.read_text(encoding="utf-8"),
            "config should remain in its patched state when rollback itself failed",
        )
        self.assertIn(
            "status: proposed",
            proposal.read_text(encoding="utf-8"),
        )


# ---------- CLI subprocess tests ----------


class CLISubprocessTests(unittest.TestCase):
    """Drive the full CLI via subprocess to catch argparse + exit-code wiring
    + JSON output format regressions that in-process calls don't exercise."""

    @classmethod
    def setUpClass(cls):
        cls._base_env = os.environ.copy()
        cls._base_env["PYTHONPATH"] = str(REPO_ROOT)
        # Q30 Session B: the approval handler now resolves vault root via
        # `resolve_vault_root` (constant + K2BI_VAULT_ROOT env) instead of
        # walking up `parents[2]` from the strategy path. CLI tests seed
        # bear-case + backtest evidence inside the tmp repo, so `self.repo`
        # IS the vault for these tests; pin K2BI_VAULT_ROOT accordingly
        # per-test (setUp) so concurrent tests do not cross-pollute.
        cls._base_env.pop("K2BI_VAULT_ROOT", None)

    def setUp(self):
        self.repo, self.parent_sha = _make_tmp_repo()
        self.env = dict(self._base_env)
        self.env["K2BI_VAULT_ROOT"] = str(self.repo)

    def tearDown(self):
        subprocess.run(["rm", "-rf", str(self.repo)], check=False)

    def _run(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [
                sys.executable,
                "-m",
                "scripts.lib.invest_ship_strategy",
                *args,
            ],
            capture_output=True,
            text=True,
            env=self.env,
            cwd=str(self.repo),
        )

    def test_approve_strategy_cli_refuses_without_bear_case(self):
        """MiniMax cycle-2 R1 finding: prove the bear-case REFUSE path
        reaches the CLI exit code + stderr surface, not just the
        in-process handler. No bear-case seed -> CLI must exit 1 with
        a stderr message citing `bear-case`."""
        _write_strategy(self.repo, slug="spy")
        result = self._run(
            "approve-strategy", "wiki/strategies/strategy_spy.md"
        )
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn("bear-case", result.stderr.lower())
        self.assertIn("SPY", result.stderr)

    def test_approve_strategy_happy_path_emits_json(self):
        _seed_bear_case_proceed(self.repo, ticker="SPY")
        _seed_backtest_passed(self.repo, slug="spy")
        _write_strategy(self.repo, slug="spy")
        result = self._run(
            "approve-strategy", "wiki/strategies/strategy_spy.md"
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["kind"], "strategy")
        self.assertEqual(payload["slug"], "spy")
        self.assertEqual(payload["transition"], "proposed -> approved")
        self.assertEqual(
            payload["commit_subject"], "feat(strategy): approve spy"
        )
        self.assertIn(
            "Approved-Strategy: strategy_spy", payload["trailers"]
        )
        self.assertEqual(
            payload["parent_commit_sha"], self.parent_sha
        )

    def test_reject_strategy_cli(self):
        _write_strategy(self.repo, slug="spy")
        result = self._run(
            "reject-strategy",
            "wiki/strategies/strategy_spy.md",
            "--reason",
            "too aggressive",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["slug"], "spy")
        self.assertEqual(payload["reason"], "too aggressive")

    def test_reject_strategy_missing_reason_fails(self):
        _write_strategy(self.repo, slug="spy")
        result = self._run(
            "reject-strategy", "wiki/strategies/strategy_spy.md"
        )
        self.assertNotEqual(result.returncode, 0)
        # argparse failure -> usage message on stderr.
        self.assertIn("--reason", result.stderr)

    def test_retire_strategy_cli(self):
        _write_strategy(
            self.repo,
            slug="spy",
            status="approved",
            extras=[
                "approved_at: 2026-04-18T10:00:00Z",
                "approved_commit_sha: abc1234",
            ],
        )
        result = self._run(
            "retire-strategy",
            "wiki/strategies/strategy_spy.md",
            "--reason",
            "obsolete",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertIn("Retired-Strategy: strategy_spy", payload["trailers"])

    def test_approve_limits_cli(self):
        _write_config_yaml(self.repo)
        _write_limits_proposal(self.repo)
        result = self._run(
            "approve-limits",
            "review/strategy-approvals/2026-04-19_limits-proposal_widen-size.md",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["kind"], "limits")
        self.assertEqual(payload["rule"], "position_size")
        self.assertEqual(payload["change_type"], "widen")
        self.assertIn(
            "Config-Change: position_size:widen", payload["trailers"]
        )

    def test_build_trailers_cli_prints_lines(self):
        result = self._run(
            "build-trailers",
            "--kind",
            "strategy",
            "--transition",
            "proposed -> approved",
            "--slug",
            "foo",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        lines = result.stdout.strip().splitlines()
        self.assertEqual(
            lines,
            [
                "Strategy-Transition: proposed -> approved",
                "Approved-Strategy: strategy_foo",
                "Co-Shipped-By: invest-ship",
            ],
        )

    def test_validation_error_exits_1_with_stderr(self):
        _write_strategy(self.repo, slug="spy", status="approved")
        result = self._run(
            "approve-strategy", "wiki/strategies/strategy_spy.md"
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn("error:", result.stderr)
        self.assertIn("status", result.stderr)

    def test_missing_file_errors(self):
        result = self._run(
            "approve-strategy", "wiki/strategies/strategy_nope.md"
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn("does not exist", result.stderr)


# ---------- capture_parent_sha ----------


class CaptureParentShaTests(unittest.TestCase):
    def test_returns_head_short_sha(self):
        repo, expected = _make_tmp_repo()
        try:
            sha = iss.capture_parent_sha(cwd=repo)
            self.assertEqual(sha, expected)
        finally:
            subprocess.run(["rm", "-rf", str(repo)], check=False)


# ---------- atomic write behaviour ----------


class AtomicWriteTests(unittest.TestCase):
    def test_atomic_write_leaves_no_tempfile_on_success(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            target = tmp / "file.md"
            target.write_text("old\n", encoding="utf-8")
            iss._atomic_write_bytes(target, b"new\n")
            self.assertEqual(target.read_bytes(), b"new\n")
            leftovers = [p.name for p in tmp.iterdir()]
            self.assertEqual(leftovers, ["file.md"])

    def test_atomic_write_removes_tempfile_on_replace_failure(self):
        # Force os.replace to fail by pointing target at an existing
        # directory (EISDIR). The tempfile must be cleaned up.
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            target_dir = tmp / "this_is_a_dir"
            target_dir.mkdir()
            with self.assertRaises(OSError):
                iss._atomic_write_bytes(target_dir, b"x")
            leftovers = [
                p.name for p in tmp.iterdir() if p.name.startswith(".")
            ]
            self.assertEqual(leftovers, [])


# ---------- yaml_patch parsing corner cases ----------


class YamlPatchExtractionTests(unittest.TestCase):
    def setUp(self):
        self.repo, self.parent_sha = _make_tmp_repo()
        self.cfg = _write_config_yaml(self.repo)

    def tearDown(self):
        subprocess.run(["rm", "-rf", str(self.repo)], check=False)

    def test_missing_before_label(self):
        proposal = _write_limits_proposal(
            self.repo, before_label="nope:"
        )
        with self.assertRaises(iss.ValidationError) as cm:
            iss.handle_approve_limits(
                proposal, config_path=self.cfg, parent_sha=self.parent_sha
            )
        self.assertIn("before:", str(cm.exception))

    def test_missing_after_label(self):
        proposal = _write_limits_proposal(
            self.repo, after_label="whatever:"
        )
        with self.assertRaises(iss.ValidationError) as cm:
            iss.handle_approve_limits(
                proposal, config_path=self.cfg, parent_sha=self.parent_sha
            )
        self.assertIn("after:", str(cm.exception))


if __name__ == "__main__":
    unittest.main()
