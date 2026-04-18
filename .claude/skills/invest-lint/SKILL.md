---
name: invest-lint
description: K2B-Investment vault health maintenance -- find and fix structural issues, keep indexes current, detect orphans and stale content across the trading research vault.
triggers:
  - /lint
  - vault health check
  - check vault
  - run lint
scope: project
---

# invest-lint -- Vault Health Maintenance

Run weekly (scheduled) or on-demand via `/lint`.

## Trigger

When Keith says `/lint`, "check vault health", "run lint", or when scheduled weekly.

## Lint Checks

Run all checks in order. Report findings grouped by severity.

### 1. Index Drift

For each `wiki/*/index.md` and `raw/*/index.md`:
- Glob the folder for all .md files (excluding index.md itself)
- Compare against index.md entries
- **Missing from index**: page exists but no index entry --> auto-fix (add entry)
- **Ghost in index**: index entry but page doesn't exist --> auto-fix (remove entry)
- **Stale summary**: page title changed but index summary is outdated --> flag for review

Expected wiki/ subfolders: `tickers/`, `sectors/`, `macro-themes/`, `strategies/`, `positions/`, `watchlist/`, `playbooks/`, `regimes/`, `reference/`, `insights/`, `context/`.

Expected raw/ subfolders: `news/`, `filings/`, `analysis/`, `earnings/`, `macro/`, `youtube/`, `research/`.

### 2. Orphan Pages

Grep all vault .md files for wikilinks. A page is orphan if:
- Not linked from any other note (zero inbound links)
- Not listed in any index.md
- Exceptions: index.md files, Home.md, MOC_*.md, Daily/*.md, templates

Report orphans. Suggest which index or note should link to them (the natural parent is usually a ticker, sector, strategy, position, watchlist, playbook, or regime page).

### 3. Broken Wikilinks

Grep all vault .md files for `[[...]]` patterns. For each wikilink:
- Check if target file exists (glob for `**/target-name.md`)
- If not found: report as broken
- If close match exists (fuzzy): suggest correction

Auto-fix: create stubs for missing ticker / strategy / position pages using templates.

### 4. Missing Stubs

Scan recent notes (last 30 days) for mentions of ticker symbols, sector names, strategy names, or playbook names that don't have dedicated pages.
- Tickers mentioned in news/filings/earnings without a `wiki/tickers/<SYMBOL>.md` page --> create stub
- Strategies referenced without a `wiki/strategies/strategy_*.md` page --> create stub
- Sectors / macro themes / playbooks / regimes referenced without their wiki page --> create stub
- After stub creation, update the relevant index.md

### 5. Stale Content

Flag pages not updated in 90+ days that have `status: on` or `status: active`:
- These may need status change to `simmering`, `archived`, or `closed` (for positions)
- Open positions in `wiki/positions/` get a tighter 30-day staleness check -- a live position should not go a month without a journal touch
- Report count and list

### 6. Unprocessed Review Queue

Count items in `review/` (across `trade-ideas/`, `strategy-approvals/`, `alerts/`, `contradictions/`) older than 7 days:
- Report count and age of oldest item, broken down by subfolder
- Flag anything in `review/` that is not under one of the four expected subfolders as misrouted
- Trade ideas and strategy approvals older than 7 days are a stronger signal than alerts/contradictions -- surface those first

### 7. log.md Health

Check `wiki/log.md`:
- Verify it exists and is parseable
- Report last 5 entries for Keith's awareness
- Flag if no entries in last 7 days (suggests captures aren't logging)

### 8. Orphan Sources (Cole's check #3)

Check raw/ folders for files where `compiled:` is missing or false, and the file is older than 24 hours:
- Glob `raw/**/*.md` (excluding index.md files)
- Read frontmatter of each file
- If `compiled:` is missing, false, or empty AND file date is >24h ago: flag as uncompiled
- Report: "N raw sources pending compilation"
- Suggest: run `/compile batch` to process them

### 9. Sparse Articles (Cole's check #6)

Check wiki/ pages for content under 200 words:
- Glob `wiki/**/*.md` (excluding index.md files)
- Count words in each file (exclude frontmatter)
- If <200 words: flag as sparse
- **Exemptions**: index.md files, files with `> Stub` callout, files in `wiki/context/` (operational notes are often short), `wiki/watchlist/` entries (often deliberately terse)
- Report: "N wiki pages are sparse (<200 words)"
- Suggest: enrich from related raw sources (filings, earnings, news, research) or mark as intentionally brief

### 10. Backlink Warnings (Cole's check #5, soft)

Check wiki/ pages for inbound link count:
- For each wiki page, count how many other wiki pages link to it via `[[filename]]`
- If a page has <2 inbound links: flag as weakly connected
- **Exemptions**: index.md files, newly created pages (<7 days old)
- Report: "N wiki pages have fewer than 2 inbound links"
- This is a SOFT warning, not enforcement. Don't auto-fix.

### 11. Active Rules Staleness

Catches the failure mode where `active_rules.md` drifts out of sync with the vault after refactors.

Steps:
1. Read `K2B-Investment-Vault/System/memory/active_rules.md`.
2. Parse the `Last promoted:` date from the header.
3. Extract all vault-relative path references from rule bodies:
   - Backtick-wrapped paths (`` `wiki/insights/` ``, `` `raw/earnings/` ``)
   - Bare folder references in prose
4. For each extracted path, check if it resolves in `K2B-Investment-Vault/`.
5. Flag:
   - **Dead path**: rule references a folder that does not exist (hard error)
   - **Stale promotion**: `Last promoted:` date is older than 30 days (soft warning)
6. **Promotion candidates**: Read `~/.claude/projects/*/memory/self_improve_learnings.md`. Surface any learnings with a date newer than `Last promoted:` AND `Reinforced >= 2`. These are candidates for promotion to active rules.
7. Report format:
   ```
   [rules] Rule N references dead path `wiki/foo/` -- does not exist
   [rules] Last promoted 45 days ago -- review learnings for promotion candidates
   [rules] 3 promotion candidates: L-2026-04-02-001, L-2026-04-04-001, L-2026-04-07-003
   ```
8. Never auto-fix. Active rules are Keith's voice; he decides what to rewrite or retire.

### 12. Contradiction Detection (Cole's check #7, semantic)

MiniMax M2.7-powered semantic check -- only runs when explicitly requested (`/lint deep`):

```bash
~/Projects/K2B-Investment/scripts/minimax-lint-deep.sh [domain]
```

- Runs on MiniMax M2.7 (not Opus) -- cheap (~$0.02-0.05 per run)
- Script reads wiki pages, sends to MiniMax, returns JSON with contradiction pairs
- Opus parses JSON and presents findings to Keith
- Add confirmed contradictions to `review/contradictions/` for Keith's judgment
- If domain is specified, only scans pages with matching `domain:` frontmatter
- If omitted, scans all wiki pages (excluding `context/`)
- Examples of contradictions to surface:
  - **Bull thesis vs bear thesis on the same ticker** (e.g. `wiki/tickers/NVDA.md` thesis section conflicts with a recent `wiki/insights/insight_*.md` warning)
  - Strategy guidelines that disagree across two `wiki/strategies/` pages (e.g. one says "never average down", another says "scale in 3 tranches on weakness")
  - Position sizing rule in a playbook that contradicts a regime page's risk posture
  - Macro theme pages with opposing directional calls on the same driver
- Note: only run on-demand, not weekly.

## Output Format

Every lint run produces two artifacts:
1. **Inline report** shown to Keith (for manual runs)
2. **Structured artifact** at `~/Projects/K2B-Investment-Vault/wiki/context/lint-report.md` -- overwritten each run, consumed by `/improve` and other skills

### Inline Report Format

```
# Vault Lint Report -- YYYY-MM-DD

## Summary
- Checks run: 12
- Auto-fixed: N issues
- Needs review: N items
- Clean: N checks passed

## Auto-Fixed
- [index] Added 3 missing entries to tickers/index.md
- [index] Removed 1 ghost entry from reference/index.md
- [stub] Created wiki/tickers/AVGO.md stub

## Needs Review
- [orphan] insight_old-topic.md has zero inbound links
- [stale] position_NVDA-long.md last updated 2026-03-22, status: open
- [broken] [[nonexistent-ticker]] referenced in strategy_momentum-breakouts.md
- [contradiction] Bull thesis on NVDA in wiki/tickers/NVDA.md conflicts with bear-flag warning in wiki/insights/insight_semis-rollover.md

## All Clear
- log.md: healthy, last entry 2 days ago
- review/: 1 trade idea, 0 alerts (normal)
```

### Artifact Format (`wiki/context/lint-report.md`)

Frontmatter carries the summary counts and per-check roll-up. Body groups findings by check so downstream skills can extract specific sections:

```yaml
---
type: lint-report
date: 2026-04-18
run-mode: manual  # or weekly, deep
checks-run: 12
auto-fixed: 3
needs-review: 5
clean: 4
hard-errors: 0
rules-dead-paths: 0
rules-last-promoted: 2026-04-18
rules-promotion-candidates: 0
vault-orphans: 2
vault-broken-links: 1
review-stale-items: 4
uncompiled-raw: 7
sparse-wiki-pages: 3
up: "[[index]]"
---

# Vault Lint Report -- 2026-04-18

## Needs Review

Aggregator across all checks, ordered by severity: hard errors first (dead paths, broken wikilinks targeting nonexistent files), then flagged items (orphans, stale review items, uncompiled raw, sparse wiki, weak backlinks, stale open positions), then soft warnings (stale promotion). Each line prefixed with the check tag (e.g. `[rules]`, `[orphan]`, `[broken]`, `[stale]`, `[uncompiled]`, `[contradiction]`).

This section is the canonical entry point for downstream consumers like `/improve` Section 3 -- they read this list rather than walking the per-check sections below.

## Active Rules (Check #11)
... findings ...

## Vault Structure (Checks #1-5)
... findings ...

## Capture Pipeline (Checks #6-9)
... findings ...

## Link Graph (Checks #3, #10)
... findings ...
```

This structured file is the source of truth for `/improve` Sections 1b and 3 -- they read this file rather than re-running the queries. Section 3 reads `## Needs Review`; Section 1b reads `## Active Rules`.

## Scheduled Execution

When run via weekly schedule:
1. Run all checks (1-11; check 12 is skipped in weekly runs)
2. Auto-fix what's safe
3. Write structured report to `wiki/context/lint-report.md` (overwrite)
4. Append lint summary via `scripts/wiki-log-append.sh /lint <lint-run-id> "<summary>"`
5. If any "needs review" items: leave report in vault for Keith

Checks 8-11 (orphan sources, sparse articles, backlink warnings, active rules staleness) run as part of the weekly schedule.
Check 12 (contradiction detection) only runs when Keith says `/lint deep` -- it is expensive and should not run automatically.

When run manually (`/lint`):
1. Run all checks
2. Show report inline
3. Ask Keith which auto-fixes to apply
4. Apply approved fixes
5. Write structured report to `wiki/context/lint-report.md` (overwrite)
6. Append via `scripts/wiki-log-append.sh /lint <lint-run-id> "<summary>"`

## Rules

- Never delete notes. Only flag for Keith's decision.
- Auto-fix is limited to: adding missing index entries, removing ghost index entries, creating stubs from templates.
- All other fixes require Keith's approval.
- Always update `wiki/log.md` via `scripts/wiki-log-append.sh` (never `>>`) after a lint pass.
- If lint finds 0 issues, still log it (proves the check ran).

## Usage Logging

After completing the main task:
```bash
echo -e "$(date +%Y-%m-%d)\tinvest-lint\t$(echo $RANDOM | md5sum | head -c 8)\tlint: MODE SUMMARY" >> ~/Projects/K2B-Investment-Vault/wiki/context/skill-usage-log.tsv
```
