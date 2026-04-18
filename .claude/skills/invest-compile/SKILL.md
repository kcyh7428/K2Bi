---
name: invest-compile
description: Compile raw sources into wiki knowledge pages -- reads raw captures, identifies affected wiki pages, shows Keith a summary, updates wiki on approval. The knowledge compilation engine that turns filing into digestion for K2B-Investment.
triggers:
  - /compile
  - compile this
  - digest this
  - process raw
scope: project
---

# invest-compile -- Knowledge Compilation Engine

Reads raw source captures and compiles them into wiki knowledge pages. Based on Karpathy's LLM Wiki architecture: the LLM owns the wiki layer, Keith curates sources and approves updates.

## Trigger

- `/compile` -- compile unprocessed raw sources
- `/compile <path>` -- compile a specific raw source
- `/compile batch` -- batch-compile multiple sources with one approval
- `/compile deep` -- deep analysis seeking cross-vault connections
- Automatically triggered by capture skills after writing to raw/

## Core Concept

**Filing vs compiling:** Filing creates one note and stops. Compiling reads a source, extracts entities/themes/insights, and ripples updates across 5-15 wiki pages. A single earnings-call review might update 2 ticker pages, 1 sector page, create 1 strategy page, and add entries to 2 indexes.

## Paths

- Raw sources: `~/Projects/K2B-Investment-Vault/raw/` (news/, filings/, analysis/, earnings/, macro/, youtube/, research/)
- Wiki output: `~/Projects/K2B-Investment-Vault/wiki/` (tickers/, sectors/, macro-themes/, strategies/, positions/, watchlist/, playbooks/, regimes/, reference/, insights/, context/)
- Master index: `~/Projects/K2B-Investment-Vault/wiki/index.md`
- Activity log: `~/Projects/K2B-Investment-Vault/wiki/log.md`
- Review queue: `~/Projects/K2B-Investment-Vault/review/` (trade-ideas/, strategy-approvals/, alerts/, contradictions/)

## Commander/Worker Architecture

**MiniMax M2.7** does the heavy cognitive work (reading, analyzing, generating structured output).
**Opus** orchestrates (calls the script, presents summary, applies file changes, updates indexes).

This is the same pattern used by the observer loop. ~30-50x cheaper than running everything on Opus.

## Policy Ledger Check (MANDATORY -- runs before every compile)

Before creating or updating ANY wiki page, check the policy ledger:

1. **Read** `wiki/context/policy-ledger.jsonl`
2. **Filter** entries where `scope` is `invest-compile` or `*` (global)
3. **For each matching guard**: verify the action complies. Key guards:
   - `create_wiki_page`: Check raw source `related:` frontmatter and grep wiki/ before creating. Enrich existing pages, don't duplicate.
   - `update_index`: Must call scripts/compile-index-update.py -- the helper covers all 4 indexes atomically.
   - `classify_note`: Triage context vs insight correctly.
4. **For autonomy entries**: if `auto_eligible` is true, proceed without asking. Otherwise ask Keith.
5. **After Keith approves/rejects**: update the ledger entry's approved/rejected count.

## Compile Flow

### 1. Call MiniMax Compile Worker

> **TODO Phase 2: invest-compile MiniMax worker not yet ported, this section describes the target behavior.** The bash script `~/Projects/K2B-Investment/scripts/minimax-compile.sh` does NOT yet exist for K2B-Investment. Until Phase 2 ports it, run the extraction step inline within Opus by reading the raw source plus relevant indexes and generating the same structured JSON shape directly.

```bash
~/Projects/K2B-Investment/scripts/minimax-compile.sh "<raw-source-path>"
```

The script (target behavior):
1. Reads the raw source file
2. Reads wiki/index.md + relevant subfolder indexes (tickers, sectors, macro-themes, strategies, positions, watchlist, playbooks, regimes, reference, insights)
3. Sends everything to MiniMax M2.7 with a structured extraction prompt
4. Returns JSON with: pages_to_update, pages_to_create, summary

### 2. Parse, Validate, and Present Summary

Opus parses the JSON but treats it as a **suggestion, not a directive**. Before presenting to Keith:
- For each `pages_to_create`: check raw source `related:` frontmatter links and grep `wiki/` for the entity. If an existing page covers this entity, convert the "create" to an "update" in the plan.
- For each `pages_to_update`: verify the target file exists. If not, convert to a "create".

Then present Keith with a concise summary he can approve in ~2 seconds:

```
## Compile: [Source Title]

**Will update:**
- wiki/tickers/NVDA.md -- add Q4 earnings results from 2026-04-08
- wiki/strategies/strategy_semis-cycle-long.md -- append data-center capex update

**Will create:**
- wiki/macro-themes/macro-theme_ai-capex-cycle.md -- new macro theme page
- wiki/positions/position_NVDA-core-long.md -- new position thesis stub

**Total: 2 updates, 2 creates across 4 wiki pages**

Proceed? [approve/skip/edit]
```

If Keith says approve (or yes/ok/go/y): proceed with all updates.
If Keith says skip: mark source as `compiled: skipped` and move on.
If Keith gives specific feedback: adjust plan and re-present.

### 4. Execute Updates

For each planned change:

Opus applies changes from the MiniMax JSON output:

**For each entry in `pages_to_create`:**
1. **BEFORE creating:** Check if the raw source frontmatter has `related:` links pointing to existing wiki pages that cover this entity. If yes, ENRICH the existing page instead of creating a new one. MiniMax's create suggestion is a hint, not a directive -- Opus must verify against existing wiki state.
2. **BEFORE creating:** Grep `wiki/` for the entity name (ticker symbol, sector slug, strategy slug). If a page already exists, update it instead.
3. Only if steps 1-2 confirm no existing page: write the file using frontmatter + content from JSON
4. Verify wikilinks point to existing pages (glob check)
5. Create stubs for missing link targets

**For each entry in `pages_to_update`:**
1. Read the current wiki page
2. Find the section specified in the JSON
3. Append the content under that section using Edit tool
4. If section doesn't exist, create it before the last section

**Rules for updates:**
- NEVER overwrite existing content
- ALWAYS append under dated headers
- Preserve existing wikilinks and sections
- If new info contradicts existing info: flag in the update with `> [!warning] Potential conflict` and add to review/contradictions/ queue
- Minimum 2 wikilinks per new page (soft target, not hard enforcement)

### 5. Update indexes (single helper call)

Call the atomic 4-index helper. This is the ONLY permitted way to update any index during a compile run. Do NOT hand-edit `wiki/<sub>/index.md`, `raw/<sub>/index.md`, `wiki/index.md`, or append to `wiki/log.md` directly.

```bash
~/Projects/K2B-Investment/scripts/compile-index-update.py \
  "<raw-source-path>" \
  "<comma-separated-updated-pages>" \
  "<comma-separated-created-pages>"
```

The helper:
- Resolves each wiki page to its deepest containing subfolder (nested-aware, so `wiki/tickers/semis/NVDA.md` maps to `tickers/semis`, not `tickers`).
- Groups mixed-subfolder updates and touches every affected subfolder index exactly once.
- Parses the existing `Last updated: ... | Entries: N` header and the master 3-column table in place; never rewrites shape.
- Validates every target index; exits 1 if any shape is unrecognized (nothing mutated).
- Stages all updates into tempfiles under a mkdir lock at `/tmp/k2bi-compile-index.lock.d`, then atomic-renames each into place.
- Calls `scripts/wiki-log-append.sh` to append the log line. If the log append fails, exits 2 -- the indices are already written, so loud failure is preferred over silent.

Exit codes: 0 ok, 1 validation failure, 2 partial write (indices written, log append failed or mid-rename failure), 3 lock timeout. On non-zero exit, stop the compile run and surface stderr to Keith. Do not retry blindly.

Before calling the helper, still mark the raw source frontmatter with `compiled: true` and `compiled-date: YYYY-MM-DD`. That is a content edit on the raw source file, not an index update, so it stays outside the helper's scope.

## Compile Modes

### summary (default)
Shows plan, waits for Keith's approval. Best for interactive sessions.

### batch
Groups multiple uncompiled sources:
1. Read all raw files where `compiled:` is missing or false
2. Show combined summary: "5 sources, 12 wiki updates, 4 new pages"
3. One approval for all
4. Process sequentially

### deep
Manual trigger for deeper analysis:
1. Read the source AND all related wiki pages
2. Look for non-obvious connections across domains
3. Suggest new macro-theme or strategy pages that bridge tickers/sectors
4. Takes longer but finds richer cross-links

## Entity Handling

### Tickers
- **Match by symbol:** Search wiki/tickers/index.md for the exact symbol (e.g. NVDA, TSM, BABA)
- **Disambiguation:** Distinguish primary listing vs ADR vs HK-listed share class (e.g. BABA vs 9988.HK) by exchange/market context
- **Stub creation:** New ticker -> create stub with symbol, name, sector, exchange, and `> Stub -- to be populated`

### Sectors
- **Match by slug:** Search wiki/sectors/index.md (e.g. semis, energy, china-tech, financials)
- **Sector tagging:** Tag each ticker with its primary sector and any cross-sector exposures
- **Stub creation:** New sector -> create stub with name, key tickers, current regime tag

### Strategies
- **Match by slug:** Search wiki/strategies/index.md (e.g. strategy_semis-cycle-long, strategy_china-tech-mean-reversion)
- **Threshold:** Only create strategy pages for thesis frameworks Keith has explicitly endorsed or that recur across 2+ sources
- **Merge not duplicate:** If a strategy page exists, enrich it -- don't create a second one

### Positions
- **Match by slug:** Search wiki/positions/index.md (e.g. position_NVDA-core-long)
- **One per active book entry:** A position page tracks the live thesis, sizing, stops, and review cadence for an open or recently-closed book entry
- **Stub creation:** New position -> create stub with ticker, side, size, entry date, thesis link

## Idempotency

Running compile on the same source twice must not create duplicates:
1. Check raw source frontmatter for `compiled: true`
2. If already compiled: report "Already compiled on YYYY-MM-DD" and skip
3. If Keith wants to re-compile: use `/compile deep <path>` which re-reads and enriches

## Error Handling

- If wiki/index.md is missing or corrupted: rebuild from folder contents before proceeding
- If a wiki page to update doesn't exist: create it (treat as new page)
- If raw source has no meaningful content: mark `compiled: empty` and skip
- If Keith rejects the compile plan: mark `compiled: skipped` in frontmatter

## Integration with Capture Skills

Capture skills trigger compile after writing to raw/:
- invest-feed (not yet built) -> writes to raw/news/ -> triggers compile
- invest-thesis (not yet built) -> writes to raw/analysis/ -> triggers compile
- invest-earnings (not yet built) -> writes to raw/earnings/ -> triggers compile
- invest-session-wrapup -> writes to raw/research/ -> triggers compile
- invest-journal -> writes to Daily/YYYY-MM-DD.md -> triggers compile

The trigger pattern: after the capture skill logs its raw source, it calls compile in summary mode. Keith approves the compilation plan inline.

## Frontmatter for Raw Sources (Post-Compile)

```yaml
compiled: true | false | skipped | empty
compiled-date: YYYY-MM-DD
compiled-pages: ["wiki/tickers/NVDA.md", "wiki/strategies/strategy_semis-cycle-long.md"]
```

## Frontmatter for Wiki Pages (Compiled)

```yaml
compiled-from: ["[[raw-source-1]]", "[[raw-source-2]]"]
```

This tracks provenance -- which raw sources contributed to this wiki page.

## Usage Logging

After completing compilation:
```bash
echo -e "$(date +%Y-%m-%d)\tinvest-compile\t$(echo $RANDOM | md5sum | head -c 8)\tcompiled: SOURCE_FILE -> N wiki pages" >> ~/Projects/K2B-Investment-Vault/wiki/context/skill-usage-log.tsv
```
