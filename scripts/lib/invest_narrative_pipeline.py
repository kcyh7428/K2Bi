"""Invest-narrative Ship 2 pipeline -- two-call decomposition + validators + --promote.

Library API:
    run_pipeline(narrative, *, vault_root=None, call1_fn=None, call2_fn=None) -> Path
    promote_to_watchlist(symbol, theme_file_path, *, vault_root=None) -> Path

CLI:
    python3 -m scripts.lib.invest_narrative_pipeline --promote SYMBOL --theme-file PATH
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from scripts.lib.canonical_ticker_registry import load_registry
from scripts.lib.invest_narrative_validators import (
    ValidatorSkipped,
    validate_citation_url,
    validate_liquidity,
    validate_market_cap,
    validate_priced_in,
    validate_ticker_exists,
)
from scripts.lib.invest_ship_strategy import resolve_vault_root
from scripts.lib.strategy_frontmatter import atomic_write_bytes, parse as parse_frontmatter
from scripts.lib.watchlist_index import (
    remove_watchlist_index_row,
    symbol_lock,
    update_watchlist_index,
)

# ---------------------------------------------------------------------------
# Slug derivation (matches Ship 1 SKILL.md exactly)
# ---------------------------------------------------------------------------


def _derive_slug(narrative: str, vault: Path) -> str:
    """Derive a unique kebab-case slug from the first 6 words of the narrative."""
    words = narrative.split()
    first_words: list[str] = []
    for w in words:
        w = w.strip(".,;:!?")
        if not w:
            continue
        first_words.append(w)
        if len(first_words) >= 6:
            break
    base = "-".join(w.lower() for w in first_words)
    base = re.sub(r"[^a-z0-9-]+", "", base)
    base = base.strip("-")
    if not base:
        base = "theme"
    slug = base
    suffix = 1
    while (vault / "wiki" / "macro-themes" / f"theme_{slug}.md").exists():
        suffix += 1
        slug = f"{base}_{suffix}"
    return slug


# ---------------------------------------------------------------------------
# JSON extraction from LLM responses
# ---------------------------------------------------------------------------


def _extract_json(text: str) -> dict:
    """Strip optional markdown fences and parse JSON."""
    text = text.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    return json.loads(text)


# ---------------------------------------------------------------------------
# Default LLM call implementations (routed through minimax_common)
# ---------------------------------------------------------------------------

_SYSTEM_PROMPT = (
    "You are an investment research analyst doing top-of-funnel ticker "
    "discovery for K2Bi (Keith's personal investment system). Your task is "
    "to take a macro narrative and produce a candidate ticker list that "
    "Keith will manually review.\n\n"
    "Critical rules you MUST follow:\n"
    "1. For every ticker you propose, provide a 2-4 step reasoning chain "
    "that shows HOW the narrative leads to that ticker. Rank each ticker "
    "by order of beneficiary (1st, 2nd, 3rd) based on economic logic. "
    "Include 1st-order primary beneficiaries when their fundamentals support "
    "the narrative; do NOT default to 2nd/3rd-order tail plays as a generic "
    "preference. The right ranking is the one the economic structure dictates.\n"
    "2. For every ticker, cite ONE specific real news article URL or SEC "
    "filing URL from the last 6 months that supports the connection. "
    "If you cannot cite a real source, do NOT include the ticker.\n"
    "3. Skip companies with market cap below $2B.\n"
    "4. Skip companies that have risen more than 90% in the last 90 days "
    "unless the narrative is genuinely new (in which case flag them as "
    '"may already be priced in").\n\n'
    "Return ONLY a JSON object with no markdown formatting."
)


def _default_call1(
    narrative: str,
    *,
    order_preference: str = "any",
    lived_signal: str | None = None,
    llm_provider: str = "kimi-coding",
) -> list[dict]:
    """Call 1: return sub-themes as list of dicts with 'name' and 'reasoning'."""
    from scripts.lib.minimax_common import extract_assistant_text

    user_parts = [
        f'Narrative: "{narrative}"\n',
    ]
    if lived_signal:
        user_parts.append(
            "\nOperator lived-signal context (treat as primary evidence; "
            "weight equal to any other anchor when reasoning about this narrative):\n\n"
            f"{lived_signal}\n"
        )
    user_parts.append(
        "\nProvide 4-6 sub-themes / value chain segments from this narrative. "
        "For each sub-theme, give a one-line reasoning.\n"
    )
    if order_preference == "1st-emphasis":
        user_parts.append(
            "Strongly weight 1st-order primary beneficiaries; only include "
            "2nd/3rd-order if their fundamentals are exceptional.\n"
        )
    elif order_preference == "tail-emphasis":
        user_parts.append(
            "Strongly prefer 2nd-order and 3rd-order beneficiaries; only include "
            "1st-order if their fundamentals decisively dominate.\n"
        )
    user_parts.append(
        'Return ONLY a JSON object like:\n'
        '{"sub_themes": [{"name": "...", "reasoning": "..."}]}'
    )
    user = "".join(user_parts)

    if llm_provider == "openai-search":
        from scripts.lib.minimax_common import (
            _extract_json as _extract_json_common,
            openai_search_chat_completion,
        )
        resp = openai_search_chat_completion(
            messages=[
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": user},
            ],
            max_tokens=2048,
            temperature=0.3,
        )
        text = extract_assistant_text(resp)
        data = _extract_json_common(text)
    else:
        from scripts.lib.minimax_common import chat_completion
        resp = chat_completion(
            model="kimi-for-coding",
            messages=[
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": user},
            ],
            max_tokens=2048,
            temperature=0.3,
        )
        text = extract_assistant_text(resp)
        data = _extract_json(text)
    return data.get("sub_themes", [])


def _default_call2(
    narrative: str,
    sub_theme: dict,
    *,
    order_preference: str = "any",
    lived_signal: str | None = None,
    llm_provider: str = "kimi-coding",
) -> list[dict]:
    """Call 2: return candidates for a single sub-theme."""
    from scripts.lib.minimax_common import extract_assistant_text

    user_parts = [
        f'Narrative: "{narrative}"\n'
        f'Sub-theme: "{sub_theme["name"]}" -- {sub_theme["reasoning"]}\n',
    ]
    if lived_signal:
        user_parts.append(
            "\nOperator lived-signal context (treat as primary evidence; "
            "weight equal to any other anchor when reasoning about this narrative):\n\n"
            f"{lived_signal}\n"
        )
    user_parts.append(
        "\nFor this sub-theme, provide 2-3 candidate tickers with:\n"
        "- Symbol\n"
        "- Reasoning chain (2-4 steps)\n"
        "- Citation URL (real, last 6 months)\n"
        '- Order of beneficiary (1st, 2nd, 3rd)\n'
        "- ARK 6-metric initial scores (1-10 each): people_culture, "
        "rd_execution, moat, product_leadership, thesis_risk, valuation\n\n"
    )
    if order_preference == "1st-emphasis":
        user_parts.append(
            "Strongly weight 1st-order primary beneficiaries; only include "
            "2nd/3rd-order if their fundamentals are exceptional.\n"
        )
    elif order_preference == "tail-emphasis":
        user_parts.append(
            "Strongly prefer 2nd-order and 3rd-order beneficiaries; only include "
            "1st-order if their fundamentals decisively dominate.\n"
        )
    user_parts.append(
        'Return ONLY a JSON object like:\n'
        '{"candidates": [{"symbol": "...", "reasoning_chain": "...", '
        '"citation_url": "...", "order": "1st", '
        '"ark_scores": {"people_culture": 8, "rd_execution": 9, '
        '"moat": 9, "product_leadership": 9, "thesis_risk": 7, '
        '"valuation": 6}}]}'
    )
    user = "".join(user_parts)

    if llm_provider == "openai-search":
        from scripts.lib.minimax_common import (
            _extract_json as _extract_json_common,
            openai_search_chat_completion,
        )
        resp = openai_search_chat_completion(
            messages=[
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": user},
            ],
            max_tokens=4096,
            temperature=0.3,
        )
        text = extract_assistant_text(resp)
        data = _extract_json_common(text)
    else:
        from scripts.lib.minimax_common import chat_completion
        resp = chat_completion(
            model="kimi-for-coding",
            messages=[
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": user},
            ],
            max_tokens=4096,
            temperature=0.3,
        )
        text = extract_assistant_text(resp)
        data = _extract_json(text)
    candidates = data.get("candidates", [])
    for c in candidates:
        c["sub_theme"] = sub_theme["name"]
    return candidates


# ---------------------------------------------------------------------------
# Theme file builder
# ---------------------------------------------------------------------------


def _narrative_to_title(narrative: str) -> str:
    """Convert narrative to a human-readable title."""
    return narrative.strip().rstrip(".").title()


def _build_theme_file(
    narrative: str,
    slug: str,
    sub_themes: list[dict],
    candidates: list[dict],
    priced_in_warnings: list[str],
    stats: dict[str, int],
    skipped_checks: list[dict],
) -> bytes:
    """Assemble the theme Markdown file as bytes."""
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    candidate_ark_scores: dict[str, dict] = {}
    for c in candidates:
        ark = c.get("ark_scores")
        if ark:
            candidate_ark_scores[c["symbol"]] = ark

    frontmatter: dict[str, Any] = {
        "tags": ["macro-theme", "narrative", "candidates", "k2bi"],
        "date": today,
        "type": "macro-theme",
        "origin": "k2bi-extract",
        "narrative": narrative,
        "sub-themes": [st["name"] for st in sub_themes],
        "candidate-count": len(candidates),
        "attention-score": "<stub for Ship 3>",
        "priced-in-warnings": priced_in_warnings,
        "status": "candidates-pending-review",
        "up": "[[index]]",
    }
    if candidate_ark_scores:
        frontmatter["candidate_ark_scores"] = candidate_ark_scores

    fm_lines = ["---"]
    fm_lines.extend(yaml.safe_dump(frontmatter, sort_keys=False, allow_unicode=True).splitlines())
    fm_lines.append("---")

    body_lines: list[str] = [
        f"# Macro Theme: {_narrative_to_title(narrative)}",
        "",
        "## Narrative",
        "",
        narrative,
        "",
        "## Sub-themes (Call 1)",
        "",
    ]
    for i, st in enumerate(sub_themes, 1):
        body_lines.append(f"{i}. **{st['name']}** -- {st['reasoning']}")
    body_lines.append("")
    body_lines.append("## Candidate tickers (Call 2)")
    body_lines.append("")

    for st in sub_themes:
        st_cands = [c for c in candidates if c.get("sub_theme") == st["name"]]
        if not st_cands:
            continue
        body_lines.append(f"### {st['name']}")
        body_lines.append("")
        body_lines.append("| Symbol | Order | Reasoning chain | Citation | ARK score (sum/60) |")
        body_lines.append("|---|---|---|---|---|")
        for c in st_cands:
            ark = c.get("ark_scores", {})
            total = sum(ark.values()) if isinstance(ark, dict) else 0
            citation_md = f"[source]({c['citation_url']})"
            # Escape pipe chars and newlines so markdown table stays valid
            reasoning = str(c["reasoning_chain"]).replace("|", "\\|").replace("\n", " ")
            body_lines.append(
                f"| {c['symbol']} | {c['order']} | {reasoning} | "
                f"{citation_md} | {total}/60 |"
            )
        body_lines.append("")

    body_lines.append("## Validator results")
    body_lines.append("")
    body_lines.append(f"- Total candidates from LLM: {stats['total']}")
    body_lines.append(f"- Rejected (hallucinated symbol): {stats['rejected_symbol']}")
    body_lines.append(f"- Rejected (below market-cap floor $2B): {stats['rejected_cap']}")
    body_lines.append(f"- Rejected (below liquidity floor $10M ADV): {stats['rejected_liq']}")
    body_lines.append(f"- Rejected (no working citation): {stats['rejected_citation']}")
    body_lines.append(f"- Rejected (malformed LLM output): {stats.get('rejected_malformed', 0)}")
    body_lines.append(
        f"- Flagged (>90% gain in last 90 days, may already be priced in): {priced_in_warnings}"
    )
    body_lines.append(f"- Final candidates shown above: {len(candidates)}")
    if skipped_checks:
        body_lines.append("- Validator skipped (yfinance unavailable):")
        for sk in skipped_checks:
            body_lines.append(f"  - {sk['symbol']} / {sk['check']}: {sk['reason']}")
    body_lines.append("")
    body_lines.append("## Promotion log")
    body_lines.append("")
    body_lines.append("(Keith fills this in as he promotes candidates to invest-screen)")
    body_lines.append("")
    body_lines.append("## Linked notes")
    body_lines.append("")
    body_lines.append("- [[skills-design]] -- invest-narrative skill spec")
    body_lines.append("- [[roadmap]] -- where this theme sits in K2Bi's narrative agenda")
    body_lines.append("")

    full = "\n".join(fm_lines) + "\n" + "\n".join(body_lines) + "\n"
    return full.encode("utf-8")


# ---------------------------------------------------------------------------
# Index updaters
# ---------------------------------------------------------------------------


def _append_promotion_to_theme(theme_path: Path, symbol: str, date: str) -> None:
    """Append a promotion line to the theme file's ## Promotion log section."""
    content = theme_path.read_text()
    # Idempotent: skip if any promotion entry for this symbol already exists
    if f"promoted {symbol} to watchlist" in content:
        return
    promo_line = f"- {date}: promoted {symbol} to watchlist"
    lines = content.splitlines()
    insert_pos = len(lines)
    for i, line in enumerate(lines):
        if line.strip() == "## Promotion log":
            # Find the first blank line after the heading to insert
            for j in range(i + 1, len(lines)):
                if lines[j].strip() == "":
                    insert_pos = j + 1
                    break
            break
    lines.insert(insert_pos, promo_line)
    new_content = "\n".join(lines) + "\n"
    atomic_write_bytes(theme_path, new_content.encode("utf-8"))


def _update_macro_themes_index(vault: Path, slug: str, title: str, date: str, count: int) -> None:
    index_path = vault / "wiki" / "macro-themes" / "index.md"
    entry_line = f"| [[theme_{slug}\\|{title}]] | {date} | {count} | candidates-pending-review |"

    if index_path.exists():
        content = index_path.read_text()
        # Match the full wiki link target to avoid prefix collisions
        if f"[[theme_{slug}\\|" in content:
            return
        lines = content.splitlines()
        insert_pos = len(lines)
        in_table = False
        for i, line in enumerate(lines):
            if line.startswith("| [[theme_"):
                in_table = True
            elif in_table and not line.startswith("|"):
                insert_pos = i
                break
        lines.insert(insert_pos, entry_line)
        new_content = "\n".join(lines) + "\n"
    else:
        frontmatter = {
            "tags": ["macro-themes", "index", "k2bi"],
            "date": date,
            "type": "index",
            "origin": "k2bi-generate",
            "up": "[[index]]",
        }
        fm_lines = ["---"]
        fm_lines.extend(yaml.safe_dump(frontmatter, sort_keys=False, allow_unicode=True).splitlines())
        fm_lines.append("---")
        new_content = "\n".join(fm_lines) + "\n\n# Macro Themes Index\n\n"
        new_content += "| Theme | Date | Candidates | Status |\n|---|---|---|---|\n"
        new_content += entry_line + "\n"

    atomic_write_bytes(index_path, new_content.encode("utf-8"))


def _update_watchlist_index(vault: Path, symbol: str, date: str, status: str) -> None:
    """Backwards-compatible thin shim. New code should call
    ``scripts.lib.watchlist_index.update_watchlist_index`` directly.
    """
    update_watchlist_index(vault, symbol, date, status)


# ---------------------------------------------------------------------------
# Core pipeline
# ---------------------------------------------------------------------------


def run_pipeline(
    narrative: str,
    *,
    vault_root: Path | None = None,
    call1_fn: Callable[[str], list[dict]] | None = None,
    call2_fn: Callable[[str, dict], list[dict]] | None = None,
    order_preference: str = "any",
    lived_signal: str | None = None,
    llm_provider: str = "kimi-coding",
) -> Path:
    """Run the two-call invest-narrative pipeline and return the theme file path.

    ``call1_fn`` receives the narrative string and returns a list of sub-theme
    dicts (keys: ``name``, ``reasoning``). ``call2_fn`` receives the narrative
    string and a sub-theme dict and returns a list of candidate dicts (keys:
    ``symbol``, ``reasoning_chain``, ``citation_url``, ``order``, ``ark_scores``).

    When either callable is omitted, the default implementation routes through
    :func:`scripts.lib.minimax_common.chat_completion`.
    """
    vault = resolve_vault_root(vault_root)
    run_started_at = datetime.now(timezone.utc).isoformat()
    slug = _derive_slug(narrative, vault)

    if call1_fn is None:
        call1_fn = lambda n: _default_call1(
            n,
            order_preference=order_preference,
            lived_signal=lived_signal,
            llm_provider=llm_provider,
        )
    if call2_fn is None:
        call2_fn = lambda n, st: _default_call2(
            n,
            st,
            order_preference=order_preference,
            lived_signal=lived_signal,
            llm_provider=llm_provider,
        )

    # Fail fast on missing registry before spending LLM calls
    registry = load_registry(vault)
    if not registry:
        raise ValueError(
            "Canonical ticker registry is empty or missing. "
            "Run: python3 -m scripts.build_canonical_registry"
        )

    sub_themes = call1_fn(narrative)
    if not sub_themes:
        raise ValueError("Call 1 returned no sub-themes")
    # (minimum checked after filtering malformed items above)

    good_sub_themes: list[dict] = []
    for st in sub_themes:
        if isinstance(st, dict) and all(k in st for k in ("name", "reasoning")):
            good_sub_themes.append(st)
    if len(good_sub_themes) < 4:
        raise ValueError(
            f"Call 1 returned {len(good_sub_themes)} well-formed sub-themes; minimum is 4"
        )

    all_candidates: list[dict] = []
    for st in good_sub_themes:
        cands = call2_fn(narrative, st)
        if isinstance(cands, list):
            all_candidates.extend(cands)

    if len(all_candidates) < 5:
        raise ValueError(
            f"Call 2 returned {len(all_candidates)} candidates total; minimum is 5"
        )
    validated: list[dict] = []
    rejected_symbol = 0
    rejected_cap = 0
    rejected_liq = 0
    rejected_citation = 0
    rejected_malformed = 0
    priced_in_warnings: list[str] = []
    skipped_checks: list[dict] = []
    rejected: list[dict] = []

    for cand in all_candidates:
        raw_cand = dict(cand) if isinstance(cand, dict) else cand
        if not isinstance(cand, dict):
            rejected.append({
                "symbol": None,
                "reason": "malformed_output",
                "sub_theme": None,
                "raw_candidate": raw_cand,
                "details": "candidate was not a dict",
            })
            rejected_malformed += 1
            continue
        raw_sym = cand.get("symbol")
        if not isinstance(raw_sym, str) or not raw_sym.strip():
            rejected.append({
                "symbol": None,
                "reason": "malformed_output",
                "sub_theme": cand.get("sub_theme"),
                "raw_candidate": raw_cand,
                "details": "missing or empty symbol",
            })
            rejected_malformed += 1
            continue
        symbol = raw_sym.strip().upper()
        # Validate required fields from Call 2 before processing
        required_fields = ("order", "reasoning_chain", "citation_url", "ark_scores")
        if not all(k in cand for k in required_fields):
            missing = [k for k in required_fields if k not in cand]
            rejected.append({
                "symbol": symbol,
                "reason": "malformed_output",
                "sub_theme": cand.get("sub_theme"),
                "raw_candidate": raw_cand,
                "details": f"missing required fields: {', '.join(missing)}",
            })
            rejected_malformed += 1
            continue
        if not cand.get("order") or not cand.get("reasoning_chain") or not cand.get("citation_url"):
            empty = [k for k in ("order", "reasoning_chain", "citation_url") if not cand.get(k)]
            rejected.append({
                "symbol": symbol,
                "reason": "malformed_output",
                "sub_theme": cand.get("sub_theme"),
                "raw_candidate": raw_cand,
                "details": f"empty required fields: {', '.join(empty)}",
            })
            rejected_malformed += 1
            continue
        order_normalized = str(cand.get("order", "")).strip().lower()
        if order_normalized not in {"1st", "2nd", "3rd"}:
            rejected.append({
                "symbol": symbol,
                "reason": "malformed_output",
                "sub_theme": cand.get("sub_theme"),
                "raw_candidate": raw_cand,
                "details": f"invalid order value: {cand.get('order')!r}",
            })
            rejected_malformed += 1
            continue
        if not isinstance(cand.get("ark_scores"), dict):
            rejected.append({
                "symbol": symbol,
                "reason": "malformed_output",
                "sub_theme": cand.get("sub_theme"),
                "raw_candidate": raw_cand,
                "details": "ark_scores is not a dict",
            })
            rejected_malformed += 1
            continue
        cand["symbol"] = symbol
        cand["order"] = order_normalized

        # 1. Ticker exists
        if not validate_ticker_exists(symbol, registry):
            rejected.append({
                "symbol": symbol,
                "reason": "hallucinated_symbol",
                "sub_theme": cand.get("sub_theme"),
                "raw_candidate": raw_cand,
                "details": symbol,
            })
            rejected_symbol += 1
            continue

        # 2. Market cap
        cap_pass = True
        try:
            cap_pass = validate_market_cap(symbol)
        except ValidatorSkipped as exc:
            skipped_checks.append({"symbol": symbol, "check": "market_cap", "reason": exc.reason})
        if not cap_pass:
            rejected.append({
                "symbol": symbol,
                "reason": "below_market_cap",
                "sub_theme": cand.get("sub_theme"),
                "raw_candidate": raw_cand,
                "details": symbol,
            })
            rejected_cap += 1
            continue

        # 3. Liquidity
        liq_pass = True
        try:
            liq_pass = validate_liquidity(symbol)
        except ValidatorSkipped as exc:
            skipped_checks.append({"symbol": symbol, "check": "liquidity", "reason": exc.reason})
        if not liq_pass:
            rejected.append({
                "symbol": symbol,
                "reason": "below_liquidity",
                "sub_theme": cand.get("sub_theme"),
                "raw_candidate": raw_cand,
                "details": symbol,
            })
            rejected_liq += 1
            continue

        # 4. Citation URL
        url = cand.get("citation_url", "")
        if not url or not validate_citation_url(url):
            rejected.append({
                "symbol": symbol,
                "reason": "no_working_citation",
                "sub_theme": cand.get("sub_theme"),
                "raw_candidate": raw_cand,
                "details": url or "missing",
            })
            rejected_citation += 1
            continue

        # 5. Priced-in flag (never blocks)
        priced_in = validate_priced_in(symbol)
        if priced_in.get("flagged"):
            priced_in_warnings.append(symbol)
        if priced_in.get("skipped"):
            skipped_checks.append(
                {"symbol": symbol, "check": "priced_in", "reason": priced_in.get("reason", "")}
            )

        validated.append(cand)

    # Deduplicate validated candidates by symbol, keeping first valid occurrence
    deduped: list[dict] = []
    seen_symbols: set[str] = set()
    for c in validated:
        sym = c.get("symbol", "").upper()
        if sym and sym not in seen_symbols:
            seen_symbols.add(sym)
            deduped.append(c)
    validated = deduped

    # Enforce at least one 2nd- or 3rd-order beneficiary
    if not any(c.get("order") in {"2nd", "3rd"} for c in validated):
        raise ValueError(
            "No 2nd- or 3rd-order beneficiaries survived validation. "
            "At least one candidate must not be an obvious pure-play."
        )

    stats = {
        "total": len(all_candidates),
        "rejected_symbol": rejected_symbol,
        "rejected_cap": rejected_cap,
        "rejected_liq": rejected_liq,
        "rejected_citation": rejected_citation,
        "rejected_malformed": rejected_malformed,
    }

    theme_path = vault / "wiki" / "macro-themes" / f"theme_{slug}.md"
    content = _build_theme_file(
        narrative, slug, good_sub_themes, validated, priced_in_warnings, stats, skipped_checks
    )
    atomic_write_bytes(theme_path, content)

    rejected_path = theme_path.with_suffix(".rejected.json")
    rejected_payload = {
        "theme_file": theme_path.name,
        "narrative": narrative,
        "run_started_at": run_started_at,
        "llm_provider": llm_provider,
        "rejected": rejected,
    }
    atomic_write_bytes(
        rejected_path, json.dumps(rejected_payload, indent=2).encode("utf-8")
    )

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    _update_macro_themes_index(vault, slug, _narrative_to_title(narrative), today, len(validated))

    return theme_path


# ---------------------------------------------------------------------------
# Markdown table parser for --promote
# ---------------------------------------------------------------------------


def _find_candidate_in_theme(content: str, symbol: str) -> dict | None:
    """Parse markdown tables in ``content`` and return the row for ``symbol``."""
    sym_upper = symbol.upper()
    lines = content.splitlines()
    in_table = False
    for line in lines:
        if line.startswith("| Symbol "):
            in_table = True
            continue
        if in_table and line.startswith("|---"):
            continue
        if in_table and line.startswith("|"):
            # Temporarily mask escaped pipes so split is safe
            masked = line.replace("\\|", "\x00ESCAPED_PIPE\x00")
            cells = [c.strip().replace("\x00ESCAPED_PIPE\x00", "|") for c in masked.split("|")][1:-1]
            if len(cells) >= 5 and cells[0].upper() == sym_upper:
                citation_md = cells[3]
                # Handle [source](URL) including URLs with parentheses
                if citation_md.startswith("[source](") and citation_md.endswith(")"):
                    citation_url = citation_md[9:-1]
                else:
                    m = re.search(r"\[([^\]]*)\]\(([^)]+)\)", citation_md)
                    citation_url = m.group(2) if m else citation_md
                return {
                    "symbol": cells[0],
                    "order": cells[1],
                    "reasoning_chain": cells[2],
                    "citation_url": citation_url,
                }
        if in_table and not line.startswith("|"):
            in_table = False
    return None


# ---------------------------------------------------------------------------
# --promote watchlist writer
# ---------------------------------------------------------------------------


def promote_to_watchlist(
    symbol: str,
    theme_file_path: Path,
    *,
    vault_root: Path | None = None,
) -> Path:
    """Promote a candidate from a theme file to the watchlist.

    Idempotent: if the watchlist entry already exists with the same symbol
    and status ``promoted``, returns the existing path without rewriting.
    """
    vault = resolve_vault_root(vault_root)
    symbol = symbol.upper()

    theme_bytes = theme_file_path.read_bytes()
    fm = parse_frontmatter(theme_bytes)
    content = theme_bytes.decode("utf-8")

    row = _find_candidate_in_theme(content, symbol)
    if row is None:
        raise ValueError(f"Symbol {symbol} not found in theme file {theme_file_path}")

    # ARK scores from frontmatter if available; otherwise empty dict
    ark_scores = (fm.get("candidate_ark_scores") or {}).get(symbol, {})

    order_map = {"1st": 1, "2nd": 2, "3rd": 3}
    order = order_map.get(row["order"], 2)

    rel_name = theme_file_path.stem
    provenance = f"[[macro-themes/{rel_name}]]"

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    frontmatter: dict[str, Any] = {
        "tags": ["watchlist", "k2bi"],
        "date": today,
        "type": "watchlist",
        "origin": "k2bi-extract",
        "up": "[[index]]",
        "symbol": symbol,
        "status": "promoted",
        "schema_version": 1,
        "narrative_provenance": provenance,
        "reasoning_chain": row["reasoning_chain"],
        "citation_url": row["citation_url"],
        "order_of_beneficiary": order,
        "ark_6_metric_initial_scores": ark_scores,
    }

    watchlist_dir = vault / "wiki" / "watchlist"
    watchlist_path = watchlist_dir / f"{symbol}.md"

    # Per-symbol lock makes the existence check + conflict detection +
    # write atomic so two concurrent promotions of the same symbol from
    # different theme files cannot both observe the file as absent and
    # race the write (m2.22 N1 fix).
    with symbol_lock(vault, symbol):
        # Idempotency: existing entry must match Ship-2-owned semantic
        # fields byte-for-byte. A different theme promoting the same
        # symbol with different reasoning is a real conflict, not
        # idempotent state.
        if watchlist_path.exists():
            existing_bytes = watchlist_path.read_bytes()
            existing_fm = parse_frontmatter(existing_bytes)
            existing_status = existing_fm.get("status")
            if existing_status != "promoted":
                raise ValueError(
                    f"Watchlist entry {symbol} already exists with status "
                    f"'{existing_status}'. Refusing to overwrite."
                )
            ship2_fields = (
                "symbol",
                "narrative_provenance",
                "reasoning_chain",
                "citation_url",
                "order_of_beneficiary",
                "ark_6_metric_initial_scores",
            )
            mismatches: list[str] = []
            for field in ship2_fields:
                existing_val = existing_fm.get(field)
                new_val = frontmatter.get(field)
                if existing_val != new_val:
                    mismatches.append(
                        f"{field}: existing={existing_val!r} new={new_val!r}"
                    )
            if mismatches:
                raise ValueError(
                    f"Conflict: watchlist entry {symbol} already promoted with "
                    f"different Ship-2 state. Mismatched fields: "
                    + "; ".join(mismatches)
                    + ". Resolve manually before re-promoting."
                )
            _update_watchlist_index(vault, symbol, today, "promoted")
            _append_promotion_to_theme(theme_file_path, symbol, today)
            print(f"{symbol} is already promoted to watchlist.")
            return watchlist_path

        body_lines = [
            f"# Watchlist: {symbol}",
            "",
            f"Promoted from {provenance} on {today}.",
            "",
            f"**Reasoning chain:** {row['reasoning_chain']}",
            "",
            f"**Citation:** [{row['citation_url']}]({row['citation_url']})",
            "",
            "## Linked notes",
            "",
            f"- {provenance}",
            "- [[index]]",
            "",
        ]

        fm_lines = ["---"]
        fm_lines.extend(
            yaml.safe_dump(frontmatter, sort_keys=False, allow_unicode=True).splitlines()
        )
        fm_lines.append("---")
        full_content = "\n".join(fm_lines) + "\n" + "\n".join(body_lines) + "\n"

        watchlist_written = False
        index_written = False
        try:
            atomic_write_bytes(watchlist_path, full_content.encode("utf-8"))
            watchlist_written = True

            _update_watchlist_index(vault, symbol, today, "promoted")
            index_written = True

            _append_promotion_to_theme(theme_file_path, symbol, today)
        except Exception as exc:
            # Rollback ordering matters when a compensator itself fails
            # (m2.22 N3 fix). Unlink the watchlist file FIRST: the file
            # is the ground-truth gate that other ops check via
            # ``watchlist_path.exists()``. Then remove the index row.
            # If unlink fails: file + index row + (maybe) theme-log
            # remain -- operator re-runs promote and either F2's
            # conflict detection raises a clear error, or the
            # idempotent path completes the recovery. If unlink
            # succeeds but index-row removal fails: file gone +
            # phantom index row -- operator re-runs promote and
            # update_watchlist_index's `if symbol in content: return`
            # guard self-heals. Either single-step rollback failure
            # therefore converges via the next promote attempt rather
            # than stranding silent split-brain.
            rollback_errors: list[str] = []
            file_unlinked = True
            if watchlist_written:
                try:
                    watchlist_path.unlink()
                except FileNotFoundError:
                    pass
                except Exception as rb_exc:
                    file_unlinked = False
                    rollback_errors.append(f"watchlist unlink: {rb_exc}")
            if index_written:
                try:
                    remove_watchlist_index_row(vault, symbol)
                except Exception as rb_exc:
                    rollback_errors.append(f"index row removal: {rb_exc}")
            if rollback_errors:
                if not file_unlinked:
                    recovery_hint = (
                        "Watchlist file remains; re-running promote will "
                        "hit F2 conflict detection or complete via the "
                        "idempotent path."
                    )
                else:
                    recovery_hint = (
                        "Watchlist file removed; index row remains. "
                        "Re-running promote will self-heal via "
                        "update_watchlist_index's idempotent guard."
                    )
                raise RuntimeError(
                    f"Promote failed AND rollback partially failed: "
                    + "; ".join(rollback_errors)
                    + f". Recovery: {recovery_hint}"
                ) from exc
            raise

    return watchlist_path


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Invest-narrative Ship 2 pipeline and --promote writer"
    )
    parser.add_argument(
        "--promote",
        metavar="SYMBOL",
        help="Promote a candidate symbol to the watchlist",
    )
    parser.add_argument(
        "--theme-file",
        help="Path to the source theme file (required with --promote)",
    )
    parser.add_argument(
        "--narrative",
        help="Run the pipeline for the given narrative text",
    )
    parser.add_argument(
        "--order-preference",
        choices=["any", "1st-emphasis", "tail-emphasis"],
        default="any",
        help="Control beneficiary-order bias in LLM prompts",
    )
    parser.add_argument(
        "--lived-signal",
        metavar="FILE",
        help="Path to a markdown file with operator-lived context to inject",
    )
    parser.add_argument(
        "--llm-provider",
        choices=["kimi-coding", "openai-search"],
        default="kimi-coding",
        help="LLM provider for the pipeline",
    )

    args = parser.parse_args(argv)

    if args.promote:
        if not args.theme_file:
            parser.error("--theme-file is required with --promote")
        path = promote_to_watchlist(args.promote.upper(), Path(args.theme_file))
        print(path)
        return 0

    if args.narrative:
        lived_signal = None
        if args.lived_signal:
            lived_path = Path(args.lived_signal)
            if not lived_path.exists():
                parser.error(f"--lived-signal file not found: {args.lived_signal}")
            try:
                lived_signal = lived_path.read_text(encoding="utf-8")
            except OSError as exc:
                parser.error(f"--lived-signal file not readable: {args.lived_signal}: {exc}")
        path = run_pipeline(
            args.narrative,
            order_preference=args.order_preference,
            lived_signal=lived_signal,
            llm_provider=args.llm_provider,
        )
        print(path)
        return 0

    parser.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
