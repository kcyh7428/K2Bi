---
name: invest-session-wrapup
description: Capture a conversation summary -- extracts decisions, action items, and insights into a K2B-Investment vault note. Use when Keith says /session-wrapup, "summarize this", "capture this", "save this conversation", or wants to save the key points from a research/trading session.
---

# Invest Session Wrap-Up Capture

## Vault Path

`~/Projects/K2B-Investment-Vault`

## Workflow

1. Review the current conversation
2. Extract:
   - **Summary**: 3-5 bullet points of what was discussed/accomplished
   - **Decisions**: Any choices made (e.g., entry/exit, sizing, thesis change, watchlist add/remove)
   - **Action Items**: Next steps with context
   - **Insights**: Technical/operational things learned during the session (e.g., "IBKR FX conversion settles T+2", "earnings call transcript lag is ~6h on Seeking Alpha"). These inform K2B-Investment's behavior and future sessions.
3. Save the wrap-up to `raw/research/YYYY-MM-DD_session-wrapup-topic.md`, then trigger invest-compile:
   - invest-compile reads the raw wrap-up + wiki/index.md
   - Shows Keith a summary of wiki pages to update (insights to wiki/insights/, action items to ticker/strategy/position pages)
   - On approval: updates wiki pages, indexes, wiki/log.md
   - Marks raw source as compiled
   - The raw wrap-up stays in raw/research/ (raw/ is the archive)
4. Save with proper frontmatter and linking
5. **Update related ticker/strategy/position notes**: If the conversation involved progress on a ticker, strategy, or position, use the vault-writer update workflow to:
   - Append a dated entry to the note's `## Updates` section
   - Update `## Current Status` if the status meaningfully changed
   - Check off any completed milestones in `## Key Milestones`
   - Add new `[[wikilinks]]` to `## Related Notes` if new notes were created
6. Confirm what was saved and where

## Frontmatter Format

Save to `raw/research/`, then trigger invest-compile to digest into wiki pages.

```yaml
---
tags: [tldr, {context-tags}]
date: YYYY-MM-DD
type: tldr
origin: k2bi-extract
source: claude-code-session
up: "[[Home]]"
---
```

After saving to raw/research/, trigger invest-compile to digest insights and action items into wiki pages. The raw wrap-up stays in raw/research/ (raw/ is the archive).

## Cross-Linking

When saving a wrap-up note, always add `[[wiki links]]`:

1. **Tickers**: Link any tickers mentioned as `[[<SYMBOL>]]` (e.g., `[[NVDA]]`, `[[TSM]]`).
2. **Strategies**: Link any strategies discussed as `[[strategy_name]]`.
3. **Positions**: Link any open positions referenced as `[[<SYMBOL>_YYYY-MM-DD]]` per File Conventions (e.g. `[[NVDA_2026-04-20]]`).
4. **Sectors / macro themes / playbooks / regimes**: Link as `[[sector_name]]`, `[[macro-theme_name]]`, `[[playbook_name]]`, `[[regime_name]]` when relevant.
5. **Source notes**: If referencing existing vault notes, link them directly.
6. Glob the vault before linking to confirm targets exist.

## Usage Logging

After completing the main task, log this skill invocation:
```bash
echo -e "$(date +%Y-%m-%d)\tinvest-session-wrapup\t$(echo $RANDOM | md5sum | head -c 8)\tcaptured wrap-up for conversation" >> ~/Projects/K2B-Investment-Vault/wiki/context/skill-usage-log.tsv
```

## Section Guidance

### Insights
Insights = what K2B-Investment learned (technical, operational, process). They inform future behavior. Examples:
- "IBKR API requires --data-urlencode for symbols with special chars"
- "Polygon free tier rate-limits at 5 req/min, switch to paid tier above that"
- "Earnings call sentiment scoring drifts when transcript has speaker mis-attribution"

Not every session has insights. Don't force them.

## Notes
- Be ruthlessly concise. Wrap-up means wrap-up.
- Action items should be copy-pasteable as tasks.
- Always link to related existing notes when possible.
- No em dashes, no AI cliches, no sycophancy
