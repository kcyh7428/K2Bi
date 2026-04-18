---
name: invest-journal
description: Start or end the trading day -- compiles today's captures from vault and session wrap-ups into a structured trading journal entry through multi-turn conversation. Use when Keith says /journal, "today", "start the day", "end of day", "EOD", "what's on today", or anything about daily trading planning/review.
---

# Invest Journal

## Core Model

`/journal` is a **multi-turn conversation**, not a one-shot generator. K2B-Investment harvests what it can find, presents a draft, asks about gaps, and Keith refines until it's right.

## Vault Path

`~/Projects/K2B-Investment-Vault`

## Workflow

### Step 1: Harvest Today's Captures

Gather from all available sources in parallel:

**a) Vault notes created or modified today:**
```bash
find ~/Projects/K2B-Investment-Vault/{review,raw,wiki,Daily} -name "*.md" -newer ~/Projects/K2B-Investment-Vault/Daily/$(date -v-1d +%Y-%m-%d).md 2>/dev/null
```
Or glob for today's date prefix in filenames.

**b) Session wrap-ups from today:**
Check for today's research / session-wrapup notes in:
- `raw/research/` for any session wrap-ups, research extracts, or insights saved earlier today

**c) Yesterday's journal entry:**
Read `~/Projects/K2B-Investment-Vault/Daily/$(date -v-1d +%Y-%m-%d).md` for open loops to carry forward.

### Step 2: Classify and Draft

Classify each captured item into the appropriate section:

- **Market Recap** -- what moved, what mattered, regime read
- **Positions / Trades** -- entries, exits, sizing changes, stops moved (anything touching a live position)
- **Watchlist Activity** -- setups firing, levels hit, tickers added or removed
- **Theses & Research** -- new or updated bull/bear theses, earnings prep, filings read
- **Insights** -- observations, patterns, things that surprised Keith
- **Open Loops** -- unfinished items to carry forward to tomorrow (orders to place, filings to read, theses to update)

Rules:
- **Omit empty sections entirely.** If nothing fits a section, don't show it.
- A quiet trading day = a short note. That's fine.
- Use bullet points, not paragraphs.
- Don't hallucinate details. If a capture is vague, include what's there and ask.
- Don't generate insights Keith didn't express. Use `> [!robot] K2B analysis` callout if surfacing a K2B-Investment-originated connection.

### Step 3: Present Draft and Ask Questions

Show Keith the compiled draft, then ask targeted questions about gaps:

- "I see you mentioned [TICKER] but no fill price -- what was the entry?"
- "Quiet on positions today -- did anything move in the book or was it actually flat?"
- "This note about [macro event] -- thesis update or just observation?"
- "Any open loops to carry forward?"

Do NOT try to ask everything at once. Ask 2-3 questions max per round. Keith will fill in what matters.

### Step 4: Refine

Based on Keith's responses:
- Add, correct, or remove items
- Reclassify items if Keith says they belong elsewhere
- Show the updated draft
- Repeat until Keith confirms

### Step 5: Save

- Save to `~/Projects/K2B-Investment-Vault/Daily/YYYY-MM-DD.md` (auto-promote -- Daily/ notes never go through review)
- If the file already exists (morning + evening use), **merge** new content into existing note rather than overwriting
- Use the k2b-vault-writer skill for the actual write
- After saving, append via helper:
  `scripts/wiki-log-append.sh /journal <journal-note> "captured: <entities>"`

**Channel-aware preview:**
- On Claude Code terminal: show the full note before saving
- On compact channels: show compact summary (section headers + bullet counts), ask "Save? Or tell me what to change"

## Morning Mode

When Keith says `/journal` in the morning (or when no captures exist for today yet):

1. Pull yesterday's open loops
2. If any captures already exist for today, include them
3. Otherwise: show open loops and say "Capture trades and observations as they happen today. /journal again at the close to compile."

Morning mode is brief. Don't prompt for a full trading plan.

## File Convention

Journal entries: `~/Projects/K2B-Investment-Vault/Daily/YYYY-MM-DD.md`

## Template

Use the daily-note template from `~/Projects/K2B-Investment-Vault/Templates/daily-note.md` for frontmatter structure (if it exists). Sections are dynamic based on what has content.

## Frontmatter

```yaml
---
tags: [journal]
date: YYYY-MM-DD
type: journal
origin: keith
up: "[[Home]]"
---
```

## Cross-Linking

When creating or updating the journal entry, add `[[wiki links]]`:

1. **Tickers**: Link as `[[<SYMBOL>]]` (e.g. `[[NVDA]]`, `[[AAPL]]`)
2. **Sectors / macro themes**: Link as `[[sector_name]]` or `[[macro-theme_name]]`
3. **Strategies**: Link as `[[strategy_name]]`
4. **Positions**: Link as `[[<SYMBOL>_YYYY-MM-DD]]` per File Conventions (e.g. `[[NVDA_2026-04-20]]`) for live position notes
5. **Playbooks / regimes**: Link as `[[playbook_name]]`, `[[regime_name]]` when invoked
6. **Yesterday's note**: When carrying forward open loops, link as `[[YYYY-MM-DD]]`
7. **Linked Notes section**: At the bottom, collect all wiki links for graph visibility

Before linking, glob the vault to confirm the target exists. If a ticker, strategy, or position doesn't have a note, create a stub.

## Usage Logging

After completing the main task:
```bash
echo -e "$(date +%Y-%m-%d)\tinvest-journal\t$(echo $RANDOM | md5sum | head -c 8)\tcompiled journal entry for YYYY-MM-DD" >> ~/Projects/K2B-Investment-Vault/wiki/context/skill-usage-log.tsv
```

## Vault Awareness

- All captures go to raw/ subfolders first, then invest-compile digests them into wiki/ pages.
- Research / session wrap-ups go to `raw/research/`, news to `raw/news/`, filings to `raw/filings/`, earnings to `raw/earnings/`.
- After the journal entry is saved, any extracted insights or thesis seeds can be promoted to wiki/ and invest-compile triggered to digest them.
- When the journal references items created today by other skills, link to wiki/ pages (the compiled output: tickers, sectors, strategies, positions, playbooks, regimes).

## Phase 4+ Stub

Once Phase 4 ships, daily journal entries also include **P&L review**, **slippage vs expectation**, and **fee erosion** sections sourced from the broker feed and execution log. For Phase 1-3 those fields are blank/optional -- the structure exists in intent but the data inputs don't yet flow. Don't fabricate numbers; if Keith volunteers fills/PnL inline, capture them under Positions / Trades and they'll migrate to the dedicated subsections when Phase 4 lands.

## Rules

- No em dashes. No AI cliches. No sycophancy.
- `origin: keith` always -- journal entries are Keith's own capture, K2B-Investment just organizes them.
- Keep it concise. Bullet points over paragraphs.
- A short journal entry is better than a padded one.
- The conversation IS the skill. Don't rush to save -- iterate until Keith's satisfied.
- Use k2b-vault-writer conventions for all note creation and cross-linking.
