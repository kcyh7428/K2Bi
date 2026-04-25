---
name: invest-narrative
description: Manual MVP single-call top-of-funnel ticker discovery. Takes a macro narrative from Keith and produces a structured theme file with 4-6 sub-themes, 5+ candidate tickers (prioritizing 2nd/3rd-order beneficiaries), reasoning chains, citations, and ARK 6-metric scores. Output lands in wiki/macro-themes/theme_<slug>.md. Use when Keith says /invest-narrative "<text>", "find tickers for this theme", "narrative to candidates", or brings a macro story he wants mapped to symbols.
tier: Analyst
routines-ready: yes
ship: 1-of-3
---

# invest-narrative

Top-of-funnel ticker discovery skill. Closes the gap between "Keith has a narrative" and "Keith has tickers to screen." Runs BEFORE invest-screen in the pipeline.

## When to Trigger

- Keith says `/invest-narrative "<text>"` or `/invest-narrative <text>`
- Keith brings a macro story, sector thesis, or thematic observation and asks for tickers
- Any phrase like "find tickers for this theme", "narrative to candidates", "what plays this story"

## When NOT to Use

- Keith already has a specific ticker symbol -- route to `/screen <SYMBOL>` instead
- Keith wants deep due diligence on one company -- route to `/thesis <SYMBOL>` instead
- Keith wants backtesting -- route to `/backtest <strategy>` instead
- Execution or order placement -- route to `/execute` instead

## Input

A free-text narrative from Keith. Examples:
- "AI compute demand drives semiconductor capex cycle"
- "Fed rate cuts benefit emerging-market debt"
- "China reopening boosts Macau luxury hospitality"

## Output

A single Markdown file at `K2Bi-Vault/wiki/macro-themes/theme_<slug>.md`.

### Slug derivation

1. Take the first 6 words of the narrative (stop at punctuation)
2. Convert to kebab-case (lowercase, spaces -> hyphens, drop non-alphanumeric except hyphens)
3. If `theme_<slug>.md` already exists, suffix `_2`, `_3`, etc. until unique

### Atomic write

Write via tempfile + `os.replace`. Never partial writes.

### Index update

After writing the theme file, append a row to `K2Bi-Vault/wiki/macro-themes/index.md` under `## Entries`:

```markdown
| [[theme_<slug>\|<human-readable title>]] | <date> | <candidate-count> | candidates-pending-review |
```

If `wiki/macro-themes/index.md` does not exist, create it with full frontmatter (see spec).

## Workflow

1. **Receive narrative** from Keith
2. **Run the prompt** below (verbatim SYSTEM + USER prompt) against the narrative
3. **Validate output structure** -- must have 4-6 sub-themes, >=5 candidates total, each with reasoning chain + citation + order + ARK scores
4. **Check for 2nd/3rd-order requirement** -- at least one candidate must NOT be the obvious pure-play
5. **Write theme file** to vault at `wiki/macro-themes/theme_<slug>.md`
6. **Update index** with new row
7. **Surface summary** to Keith (sub-themes + candidate symbols + any priced-in warnings)

## Prompt (LOCKED VERBATIM -- do not edit)

```
SYSTEM PROMPT:
You are an investment research analyst doing top-of-funnel ticker
discovery for K2Bi (Keith's personal investment system). Your task is
to take a macro narrative and produce a candidate ticker list that
Keith will manually review.

Critical rules you MUST follow:
1. Do NOT pick obvious "pure-play" tickers if they are likely already
   priced in. Prefer 2nd-order and 3rd-order beneficiaries.
2. For every ticker you propose, provide a 2-4 step reasoning chain
   that shows HOW the narrative leads to that ticker (the ARK Nvidia
   pattern: AI -> needs central nervous system -> that's GPUs -> NVDA
   is the GPU leader).
3. For every ticker, cite ONE specific real news article URL or SEC
   filing URL from the last 6 months that supports the connection.
   If you cannot cite a real source, do NOT include the ticker.
4. Skip companies with market cap below $2B.
5. Skip companies that have risen more than 90% in the last 90 days
   unless the narrative is genuinely new (in which case flag them as
   "may already be priced in").

Output format: structured Markdown matching the template at the end
of this prompt. No prose preamble. No conclusion. Just the structured
output.

USER PROMPT:
Narrative: "{KEITH_NARRATIVE_TEXT}"

Provide:
1. 4-6 sub-themes / value chain segments from this narrative
2. For each sub-theme, 2-3 candidate tickers with:
   - Symbol
   - Reasoning chain (2-4 steps)
   - Citation URL (real, last 6 months)
   - Order of beneficiary (1st, 2nd, 3rd)
3. ARK 6-metric initial scores per ticker (1-10 each):
   - People/culture
   - R&D execution
   - Moat
   - Product leadership
   - Thesis risk (10 = lowest risk)
   - Valuation (10 = clearly under-valued for the thesis)

Use the markdown template below. Do not deviate.

[TEMPLATE]
```

## Output template

The theme file body MUST match this structure:

```markdown
---
tags: [macro-theme, narrative, candidates, k2bi]
date: YYYY-MM-DD
type: macro-theme
origin: k2bi-extract
narrative: "<verbatim Keith input>"
sub-themes: [list of 4-6 strings]
candidate-count: N
attention-score: <stub for Ship 3>
priced-in-warnings: [tickers flagged]
status: candidates-pending-review
up: "[[index]]"
---

# Macro Theme: <human-readable narrative title>

## Narrative

<verbatim Keith input>

## Sub-themes (decomposition)

1. **<sub-theme 1 name>** -- <one-line reasoning>
2. **<sub-theme 2 name>** -- <one-line reasoning>
...

## Candidate tickers

### Sub-theme 1: <name>

| Symbol | Order | Reasoning chain | Citation | ARK score (sum/60) |
|---|---|---|---|---|
| TICKER | 1st | step1 -> step2 -> step3 | [URL](url) | 42/60 |
| ... |

### Sub-theme 2: <name>
...

## Validator results

- Total candidates from LLM: N
- Rejected (hallucinated symbol): N
- Rejected (below market-cap floor $2B): N
- Rejected (below liquidity floor $10M ADV): N
- Rejected (no working citation): N
- Flagged (>90% gain in last 90 days, may already be priced in): [list]
- Final candidates shown above: N

## Promotion log

(Keith fills this in as he promotes candidates to invest-screen)

- 2026-MM-DD: promoted TICKER to watchlist; reasoning: ...

## Linked notes

- [[skills-design]] -- invest-narrative skill spec
- [[roadmap]] -- where this theme sits in K2Bi's narrative agenda
```

## Post-run

After producing the theme file, show Keith:
- A one-line summary of each sub-theme
- The candidate ticker list grouped by sub-theme
- Any `priced-in-warnings` flagged
- Remind him that promotion to watchlist is manual via `/screen <SYMBOL>`

## Test policy

Ship 1 is prompt-engineering scaffolding. The manual narrative runs ARE the test. No unit tests for this ship.

## Ship 1 Safety Disclaimer (MUST surface to Keith on every run)

Ship 1 is a **pure prompt-engineering scaffold with zero Python validators**. Every field in the theme file is unvalidated LLM output. Before acting on any candidate:

- **Citations**: Marked as UNVALIDATED. The LLM may hallucinate URLs. Verify every citation manually before trusting it.
- **Ticker symbols**: Not checked against any registry. Confirm the symbol exists and matches the company you think it does.
- **priced-in-warnings**: LLM self-assessment only. No market-data validation of 90-day returns.
- **attention-score**: Stub placeholder (`<stub for Ship 3>`). Do not consume as numeric.
- **Order of beneficiary**: Prompt-level request, not programmatically enforced. Review the reasoning chain yourself.
- **Atomic writes**: The SKILL.md specifies the pattern; actual atomicity depends on the runtime environment executing the write.

If any of the above is unacceptable for a given narrative, wait for Ship 2.

## Ship roadmap

- **Ship 1 (this skill)**: Single-call manual MVP. Prompt-engineering only. No Python validators.
- **Ship 2**: Two-call decomposition, Python validators (ticker-exists, market-cap, liquidity, priced-in), canonical ticker registry, citation HTTP-HEAD validation, `--promote <symbol>` writer.
- **Ship 3**: News-feed integration, scheduled refresh, attention-score auto-population.
