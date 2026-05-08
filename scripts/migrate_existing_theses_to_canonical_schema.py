"""Migrate existing ticker + strategy files to the canonical schema.

Spec ref: K2Bi-Vault/wiki/planning/feature_invest-coach-cycle5-helper-schema-reconciliation.md
Boundary D.

Two modes:

    --check        Report per-file status; no writes. Exit 0 if all files
                   already pass cycle-5 helper Step A; exit 1 otherwise.

    --apply        Apply idempotent safe transforms in-place via atomic
                   tmp + os.replace.

Safe transforms applied:

    Strategy files (wiki/strategies/strategy_*.md)
      - rename order.quantity -> order.qty
      - rename order.stop_loss_usd -> order.stop_loss
      - surface t11_completed_at -> completed_at inside forward_guidance_check
        (other forward_guidance_check shape changes require operator review)

    Ticker files (wiki/tickers/<SYMBOL>.md)
      - surface bear_case.bear_verdict + bear_case.bear_last_verified +
        bear_case.bear_conviction + bear_case.bear_top_counterpoints +
        bear_case.bear_invalidation_scenarios to top-level if missing
      - surface t6_close_summary.thesis_5dim_sub_scores.pct as thesis_score
        if missing top-level

Anything more invasive (rewriting forward_guidance_check from
thresholds_evaluated mapping into thresholded_metrics list, restructuring
nested bear_case entries) is OUT OF SCOPE for this script. Those require
operator review and the canonical builder for new authoring.
"""

from __future__ import annotations

import argparse
import os
import sys
import tempfile
from pathlib import Path
from typing import Any

import yaml


REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_VAULT_ROOT = Path.home() / "Projects" / "K2Bi-Vault"

REQUIRED_TICKER_TOP_LEVEL = (
    "symbol",
    "thesis_score",
    "bear_verdict",
    "bear-last-verified",
    "bear_conviction",
    "bear_top_counterpoints",
)

REQUIRED_STRATEGY_TOP_LEVEL = (
    "name",
    "strategy_type",
    "risk_envelope_pct",
    "regime_filter",
    "order",
)

REQUIRED_ORDER_KEYS = (
    "ticker",
    "side",
    "qty",
    "limit_price",
    "stop_loss",
    "time_in_force",
)


def _split_frontmatter(text: str) -> tuple[str, str]:
    if not text.startswith("---\n"):
        return ("", text)
    rest = text[4:]
    end = rest.find("\n---")
    if end == -1:
        return ("", text)
    fm = rest[:end]
    body = rest[end + 4:]
    if body.startswith("\n"):
        body = body[1:]
    return (fm, body)


def _atomic_write(path: Path, content: str) -> None:
    fd, tmp = tempfile.mkstemp(
        dir=str(path.parent),
        prefix=f".{path.name}.",
        suffix=".tmp",
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


_BEAR_FIELDS = (
    "bear_verdict",
    "bear-last-verified",
    "bear_conviction",
    "bear_top_counterpoints",
)


def _is_pre_t8_ticker(fm: dict[str, Any]) -> bool:
    """A ticker is 'pre-T8' if it has no bear_case data anywhere.

    In that case the missing top-level bear fields are expected state
    rather than schema drift.
    """
    bear_nested = fm.get("bear_case")
    nested_has_bear = isinstance(bear_nested, dict) and any(
        k in bear_nested
        for k in (
            "bear_verdict",
            "bear_last_verified",
            "bear_conviction",
            "bear_top_counterpoints",
        )
    )
    top_has_bear = any(k in fm for k in _BEAR_FIELDS)
    return not (nested_has_bear or top_has_bear)


def _check_ticker(fm: dict[str, Any]) -> list[str]:
    missing = []
    pre_t8 = _is_pre_t8_ticker(fm)
    for field in REQUIRED_TICKER_TOP_LEVEL:
        if field in fm:
            continue
        if pre_t8 and field in _BEAR_FIELDS:
            continue
        missing.append(field)
    return missing


def _check_strategy(fm: dict[str, Any]) -> list[str]:
    missing = []
    for field in REQUIRED_STRATEGY_TOP_LEVEL:
        if field not in fm:
            missing.append(field)
    order = fm.get("order")
    if isinstance(order, dict):
        for key in REQUIRED_ORDER_KEYS:
            if key not in order:
                missing.append(f"order.{key}")
    return missing


def migrate_ticker_frontmatter(fm: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """Return (new_fm, applied_changes). Idempotent."""
    out = dict(fm)
    changes: list[str] = []

    bear = out.get("bear_case")
    if isinstance(bear, dict):
        for nested_key, top_key in (
            ("bear_verdict", "bear_verdict"),
            ("bear_last_verified", "bear-last-verified"),
            ("bear_conviction", "bear_conviction"),
            ("bear_top_counterpoints", "bear_top_counterpoints"),
            ("bear_invalidation_scenarios", "bear_invalidation_scenarios"),
        ):
            if top_key not in out and nested_key in bear:
                out[top_key] = bear[nested_key]
                changes.append(f"surface bear_case.{nested_key} -> {top_key}")

    if "thesis_score" not in out:
        t6 = out.get("t6_close_summary")
        if isinstance(t6, dict):
            sub = t6.get("thesis_5dim_sub_scores")
            if isinstance(sub, dict) and "pct" in sub:
                out["thesis_score"] = sub["pct"]
                changes.append(
                    "surface t6_close_summary.thesis_5dim_sub_scores.pct -> thesis_score"
                )

    if "symbol" not in out and "ticker" in out:
        out["symbol"] = out["ticker"]
        changes.append("surface ticker -> symbol")

    return out, changes


def migrate_strategy_frontmatter(fm: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """Return (new_fm, applied_changes). Idempotent."""
    out = dict(fm)
    changes: list[str] = []

    if "name" not in out and "slug" in out:
        out["name"] = out["slug"]
        changes.append("alias slug -> name")

    order = out.get("order")
    if isinstance(order, dict):
        new_order = dict(order)
        if "quantity" in new_order:
            if "qty" in new_order:
                if new_order["qty"] != new_order["quantity"]:
                    raise ValueError(
                        "order has both `qty` and `quantity` with different values; "
                        "operator must reconcile by hand"
                    )
                new_order.pop("quantity")
                changes.append("drop stale order.quantity duplicate of qty")
            else:
                new_order["qty"] = new_order.pop("quantity")
                changes.append("rename order.quantity -> order.qty")
        if "stop_loss_usd" in new_order:
            if "stop_loss" in new_order:
                if new_order["stop_loss"] != new_order["stop_loss_usd"]:
                    raise ValueError(
                        "order has both `stop_loss` and `stop_loss_usd` with different "
                        "values; operator must reconcile by hand"
                    )
                new_order.pop("stop_loss_usd")
                changes.append("drop stale order.stop_loss_usd duplicate")
            else:
                new_order["stop_loss"] = new_order.pop("stop_loss_usd")
                changes.append("rename order.stop_loss_usd -> order.stop_loss")
        out["order"] = new_order

    fgc = out.get("forward_guidance_check")
    if isinstance(fgc, dict):
        new_fgc = dict(fgc)
        if "completed_at" not in new_fgc and "t11_completed_at" in new_fgc:
            new_fgc["completed_at"] = new_fgc.pop("t11_completed_at")
            changes.append(
                "rename forward_guidance_check.t11_completed_at -> completed_at"
            )
        out["forward_guidance_check"] = new_fgc

    return out, changes


def process_file(
    path: Path,
    kind: str,
    apply: bool,
) -> dict[str, Any]:
    text = path.read_text(encoding="utf-8")
    fm_text, body = _split_frontmatter(text)
    if not fm_text:
        return {
            "path": str(path),
            "kind": kind,
            "skipped": True,
            "reason": "no YAML frontmatter",
            "missing": [],
            "changes": [],
        }
    try:
        fm = yaml.safe_load(fm_text) or {}
    except yaml.YAMLError as exc:
        return {
            "path": str(path),
            "kind": kind,
            "skipped": True,
            "reason": f"YAML parse error: {exc}",
            "missing": [],
            "changes": [],
        }
    if not isinstance(fm, dict):
        return {
            "path": str(path),
            "kind": kind,
            "skipped": True,
            "reason": "frontmatter is not a mapping",
            "missing": [],
            "changes": [],
        }

    if kind == "ticker":
        new_fm, changes = migrate_ticker_frontmatter(fm)
        missing = _check_ticker(new_fm)
        pre_t8 = _is_pre_t8_ticker(new_fm)
    elif kind == "strategy":
        new_fm, changes = migrate_strategy_frontmatter(fm)
        missing = _check_strategy(new_fm)
        pre_t8 = False
    else:
        raise ValueError(f"unknown kind: {kind}")

    result = {
        "path": str(path),
        "kind": kind,
        "skipped": False,
        "missing": missing,
        "changes": changes,
        "pre_t8": pre_t8,
    }

    if apply and changes:
        new_text = (
            "---\n"
            + yaml.safe_dump(new_fm, sort_keys=False, default_flow_style=False)
            + "---\n"
            + body
        )
        _atomic_write(path, new_text)
        result["written"] = True
    else:
        result["written"] = False

    return result


def gather_paths(vault_root: Path) -> list[tuple[Path, str]]:
    pairs: list[tuple[Path, str]] = []
    tickers_dir = vault_root / "wiki" / "tickers"
    strategies_dir = vault_root / "wiki" / "strategies"
    if tickers_dir.exists():
        for p in sorted(tickers_dir.glob("*.md")):
            if p.name == "index.md":
                continue
            pairs.append((p, "ticker"))
    if strategies_dir.exists():
        for p in sorted(strategies_dir.glob("strategy_*.md")):
            pairs.append((p, "strategy"))
    return pairs


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Migrate ticker + strategy files to canonical schema"
    )
    parser.add_argument(
        "--vault-root",
        type=Path,
        default=DEFAULT_VAULT_ROOT,
        help="K2Bi vault root (default: %(default)s)",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Apply transforms in-place; default is check-only",
    )
    args = parser.parse_args(argv)

    if not args.vault_root.exists():
        print(f"ERROR: vault root not found: {args.vault_root}", file=sys.stderr)
        return 2

    results = []
    for path, kind in gather_paths(args.vault_root):
        results.append(process_file(path, kind, apply=args.apply))

    any_missing = False
    for r in results:
        rel = Path(r["path"]).relative_to(args.vault_root)
        prefix = f"[{r['kind']}] {rel}"
        if r["skipped"]:
            print(f"{prefix} SKIP: {r['reason']}")
            continue
        if r["changes"]:
            verb = "WROTE" if r.get("written") else "WOULD-WRITE"
            print(f"{prefix} {verb}: {', '.join(r['changes'])}")
        if r["missing"]:
            any_missing = True
            print(f"{prefix} MISSING top-level fields: {r['missing']}")
        if r["kind"] == "ticker" and r.get("pre_t8"):
            print(f"{prefix} PRE-T8 (no bear-case yet; expected state)")
        if not r["changes"] and not r["missing"] and not r.get("pre_t8"):
            print(f"{prefix} OK")

    if any_missing:
        print(
            "\nFAIL: some files are missing canonical top-level fields. "
            "Run with --apply to surface migratable nested copies; "
            "anything still missing requires operator review.",
            file=sys.stderr,
        )
        return 1
    print("\nOK: all files satisfy canonical schema.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
