---
name: invest-observer
description: Harvest implicit preference signals from K2B-Investment vault behavior and synthesize a preference profile that other skills reference. This skill should be used when Keith says /observe, "what have you noticed", "check preferences", "review feedback", or on session-start/scheduled runs. It reads observer-loop analysis, review queue outcomes (trade-ideas, strategy-approvals, alerts, contradictions), revision patterns, and adoption rates to learn what Keith actually wants in his trading workflow without him having to say it explicitly.
---

# Invest Observer

Harvest implicit preference signals from Keith's K2B-Investment vault behavior. Synthesize patterns into a preference profile that other invest-* skills reference before producing trade ideas, alerts, strategy approvals, and thesis updates.

## Vault & Skill Paths

- Vault: `~/Projects/K2B-Investment-Vault`
- Preference signals log: `~/Projects/K2B-Investment-Vault/wiki/context/preference-signals.jsonl`
- Preference profile: `~/Projects/K2B-Investment-Vault/wiki/context/preference-profile.md`
- Skills: `~/Projects/K2B-Investment/.claude/skills/`
- Learnings: `~/.claude/projects/*/memory/self_improve_learnings.md`

## Vault Query Tools

- **Dataview DQL** (structured frontmatter queries): `~/Projects/K2B-Investment/scripts/vault-query.sh dql '<TABLE query>'`
- **Full-text search**: `mcp__obsidian__search` MCP tool or `vault-query.sh search "<term>"`
- **Read file**: `mcp__obsidian__get_file_contents` or Read tool
- **List files**: `mcp__obsidian__list_files_in_dir`

Prefer DQL queries over Glob+Read+Filter when scanning multiple files for frontmatter fields.

## Commands

- `/observe` -- Run the full observation cycle (harvest + synthesize)
- `/observe harvest` -- Harvest new signals only (no synthesis)
- `/observe profile` -- Show the current preference profile
- `/observe signals` -- Show raw signal stats (counts by skill, action, etc.)
- `/observe reset` -- Archive current signals and start fresh (confirm with Keith first)

## Phase 1: Harvest Signals

### 1a. Preference Signal Sources

Read `preference-signals.jsonl`. This file has two signal sources:

1. **Observer-loop (primary, active at Phase 4)**: The background observer on Mac Mini analyzes vault behavior via MiniMax M2.7 and appends signals with schema: `{date, source, type, description, confidence, skill}`. This is the main source of signals once the Phase 4 background loop is provisioned.
2. **Review queue outcomes (secondary, active now)**: When Keith approves/rejects items in `review/trade-ideas/`, `review/strategy-approvals/`, `review/alerts/`, or `review/contradictions/`, append signals with schema: `{date, file, source_skill, type, action, days_in_inbox, has_feedback, feedback, queue}`. The `queue` field records which subfolder the decision came from so signals can be sliced by domain.

If the file doesn't exist or is empty, tell Keith: "No preference signals yet. Process some review/{trade-ideas,strategy-approvals,alerts,contradictions}/ items, or wait for the Phase 4 observer-loop to start generating signals automatically."

Then check if this is the first run (no preference-profile.md exists). If so, run the Bootstrapping procedure below.

### 1a-filter. Filter out processed signals (APPEND-cutoff reader)

Read `~/Projects/K2B-Investment-Vault/wiki/context/preference-signals.jsonl` in **two passes** (signal-processed lines appear after the original signal, so a single top-to-bottom pass would surface signals before seeing their processed marker):

**Pass 1 -- collect filter state:** Walk the entire file. Track:
1. `cutoff_line` -- the line number of the `type: "grandfather-cutoff"` entry (0 if absent). All lines before it are grandfathered.
2. `processed_ids` set -- for every `type: "signal-processed"` line whose `action` is `confirmed` or `rejected`, add its `signal_id`. `action: watching` is intentionally EXCLUDED -- deferring a signal should resurface it next session, not silence it forever.

**Pass 2 -- collect candidates:** Walk the file again. A signal is filtered out when any of these is true:
- It appears before `cutoff_line` (grandfathered).
- Its `signal_id` is in `processed_ids`.
- It has no `signal_id` field at all (pre-cutoff historical; grandfathered).

Remaining signals flow into Phase 2 pattern detection and Phase 3 synthesis.

### 1b. Revision Detection

For items with action = "approve" or "promote" in the signals log:

1. Parse `review-notes` feedback text for trade-domain patterns:
   - "too tight" / "stops too close" / "widen the stop" -- risk-management preference
   - "skip this setup" / "not my style" / "wrong regime" -- setup-filter preference
   - "good thesis" / "clean trigger" / "solid risk/reward" -- positive reinforcement
   - "wrong" / "missed the catalyst" / "ignored macro" / "not what I asked" -- quality issue

2. Categorize each feedback instance and store alongside the raw signal data for pattern detection.

### 1c. Adoption Rate by Skill

From the signals, calculate per-skill stats:
- Total items produced
- Approve rate (approved / total)
- Reject rate (rejected / total)
- Defer rate (deferred / total)
- Revise rate (revised / total)
- Average days in review before action
- Feedback rate (has_feedback = yes / total)

### 1d. Trade-Decision Pipeline Signals

For trade-decision tracking:
- How many trade ideas (origin: k2bi-generate) got approved vs rejected vs deferred?
- How many strategy proposals were adopted as-is vs revised vs scrapped?
- Which sectors, regimes, or playbooks produce ideas Keith actually takes?
- For approved trades, how often did Keith hand-edit position size, stop, or target before greenlighting? (size-discipline signal)

### 1e. Contradiction-Queue Harvesting

Read items in `review/contradictions/`. Each entry surfaces a conflict between two wiki pages (bull thesis vs bear thesis on the same ticker, conflicting playbook signals, etc.). For each:

- **Resolution chosen**: which side Keith kept, which side he revised, or whether he rejected both
- **Time to resolve**: nudge_date vs decision_date (long defers = unresolved cognitive load)
- **Recurring contradicted entity**: same ticker showing up repeatedly = thesis instability worth flagging

Aggregate into per-entity stats: how often a given ticker/strategy/playbook gets contradicted and how Keith tends to resolve it.

## Phase 2: Detect Patterns

A pattern requires a minimum of **3 occurrences** of the same behavior to be considered real. Below 3, it's noise.

### Pattern Types to Detect

**Skill-Level Patterns:**
- "invest-compile: 70% of auto-generated thesis updates within 24h of earnings get rejected" (timing preference)
- "invest-session-wrapup: Keith always provides review-notes mentioning 'too verbose'" (format preference)
- "invest-research: average 5 days in review before action" (low urgency/relevance)
- "trade-ideas queue: 20% approve rate for mean-reversion setups vs 65% for breakout setups" (setup-style preference)

**Cross-Skill Patterns:**
- "Notes with type: thesis have 40% approve rate vs 80% for type: playbook entries" (relative trust by entity type)
- "Keith acts on items within 1 day when source is the alerts queue" (high-urgency signal)
- "Items with has_feedback = yes are 3x more likely to be approved" (engagement signal)

**Revision Patterns (from review-notes parsing):**
- "Keith mentions 'too tight stop' in 4 of 7 trade-idea rejections" (consistent risk-management preference)
- "Keith removes the macro-context section from strategy-approvals before approving" (section preference)

### Pattern Confidence

Assign confidence based on:
- **High (3+ occurrences, consistent direction)**: Ready to include in preference profile
- **Medium (3+ occurrences, mixed signal)**: Note in profile with caveat
- **Low (<3 occurrences)**: Do not include in profile yet, track for future

## Phase 3: Synthesize Preference Profile

Write to `~/Projects/K2B-Investment-Vault/wiki/context/preference-profile.md`:

```yaml
---
tags: [k2bi-system, preferences]
date: YYYY-MM-DD
type: reference
origin: k2bi-generate
up: "[[index]]"
---
```

Body structure:

```markdown
# K2B-Investment Preference Profile

Last updated: YYYY-MM-DD
Based on: N feedback signals over N days

## Skill-Specific Preferences

### invest-session-wrapup
- **Approve rate**: X% (N items)
- **Observed preferences**:
  - Keith prefers shorter session wrap-ups (N instances of "too verbose" feedback)
  - The macro-context section is frequently removed (N instances)
- **Recommendation**: Keep wrap-ups to 3-5 bullets. Consider making the macro-context block opt-in.

### invest-compile
- **Approve rate**: X% overall
  - By entity type: tickers X%, strategies X%, playbooks X%, ...
- **Observed preferences**:
  - Thesis updates pushed within 24h of earnings get rejected at high rate -- Keith wants a 24h cool-off after earnings calls
- **Recommendation**: Suppress auto-thesis updates for tickers with an earnings event in the last 24h.

### trade-ideas queue
- **Approve rate**: X% of generated ideas approved
- **Observed preferences**:
  - Keith rejects long-duration mean-reversion setups (N instances)
  - Keith approves 1% risk per trade not 2% (N revisions of position size before greenlight)
  - Keith vetoes any position above 20% concentration (N rejections citing "too concentrated")
- **Recommendation**: Default new trade-idea drafts to 1% risk and exclude long-duration mean-reversion templates.

### strategy-approvals queue
- **Approve rate**: X%
- **Observed preferences**:
  - [patterns detected]
- **Recommendation**: [actionable suggestion]

### alerts queue
- **Action speed**: median X hours from alert to decision
- **Observed preferences**:
  - [patterns detected]
- **Recommendation**: [actionable suggestion]

### contradictions queue
- **Resolution rate**: X% resolved within 7 days
- **Recurring entities**: [tickers/strategies showing up >2x]
- **Recommendation**: [actionable suggestion]

## General Preferences

- **Response length**: [observed pattern across skills]
- **Review processing speed**: Keith acts fastest on [queue], slowest on [queue]
- **Risk discipline**: [observed sizing/stop patterns]
- **Regime sensitivity**: [observed setup preferences by regime]

## Candidate Learnings

These patterns are strong enough to consider promoting to a /learn entry:

1. "[specific pattern]" -- Confidence: high (N occurrences)
   - Suggested /learn entry: "[what invest-* should do differently]"
2. ...

## Signal Quality Notes

- Total signals: N
- Date range: YYYY-MM-DD to YYYY-MM-DD
- Signals per skill: [breakdown]
- Signals per queue: [trade-ideas / strategy-approvals / alerts / contradictions]
- Note: Patterns with <3 occurrences are excluded. More data improves accuracy.
```

## Phase 4: Candidate Learnings Promotion

When patterns reach high confidence, present them to Keith as candidate /learn entries:

"I've noticed you consistently [pattern]. Should I /learn this so it sticks?"

Examples of high-quality, specific candidate prompts:
- "Keith rejects high-IV options entries within 5 days of earnings -- /learn this as a hard filter on trade-idea drafts?"
- "Keith approves 1% risk per trade and revises down whenever a draft proposes 2% -- /learn this as the default sizing in invest-compile?"
- "Keith pauses thesis updates for 24h after earnings calls -- /learn this as a cool-off rule for invest-compile?"
- "Keith vetoes any position above 20% portfolio concentration -- /learn this as a hard cap?"

If Keith agrees, capture the rule via the standard /learn workflow with proper dedup and reinforcement.

**Never auto-promote without Keith's confirmation.** The observer suggests, Keith decides.

## How Other Skills Use the Preference Profile (Planned)

**Status: Not yet implemented.** The design intent is documented here for when preference-based adaptation is added to downstream skills. Currently, no skill reads preference-profile.md before producing output.

**Planned integration** -- skills that produce output Keith reviews would read the preference profile before generating:

1. **invest-session-wrapup**: Read preference-profile.md section. Apply any length, section, or format preferences.
2. **invest-compile**: Read preference-profile.md for entity-type and timing preferences (e.g. earnings cool-off). Adjust analysis depth and suppress updates that violate observed rules.
3. **invest-journal**: Read preference-profile.md for risk-discipline and sizing patterns. Pre-fill defaults that match Keith's observed behavior.

**When implemented**: Each skill would add one line to its workflow: "Read `wiki/context/preference-profile.md` for skill-specific preferences. Apply any relevant preferences to output formatting and defaults."

The preference profile is a reference document, not an enforcement mechanism. Keith decides when to update skill instructions based on strong preferences.

## Bootstrapping (First Run)

On first `/observe`, if preference-signals.jsonl is empty or doesn't exist:

1. Query archived notes via DQL:
   ```bash
   ~/Projects/K2B-Investment/scripts/vault-query.sh dql 'TABLE type, origin, date FROM "Archive"'
   ```
   Archived items are implicit "not valuable enough to keep" signals.
2. Query approved trade ideas and adopted strategies:
   ```bash
   ~/Projects/K2B-Investment/scripts/vault-query.sh dql 'TABLE type, origin, status FROM "wiki/positions" OR FROM "wiki/strategies"'
   ```
   These are implicit "this was valuable" signals.
3. Query stale review items across all four queues:
   ```bash
   ~/Projects/K2B-Investment/scripts/vault-query.sh dql 'TABLE date, review-action AS "action", file.folder AS "queue" FROM "review" WHERE date <= date(today) - dur(7 days)'
   ```
   Items older than 7 days with no review-action = low urgency signal. The queue field shows which subfolder (trade-ideas / strategy-approvals / alerts / contradictions) is accumulating staleness.
4. Generate an initial preference-signals.jsonl from this retrospective data
5. Run the full synthesis to produce the first preference-profile.md

Tell Keith: "Bootstrapped preference profile from N archived items, N approved positions/strategies, and N pending review items across the four queues. This will get more accurate as the Phase 4 observer-loop and ongoing /review activity generate more signals."

## /observe reset

When Keith says `/observe reset`:
1. Confirm with Keith first: "This will archive current signals and start fresh. Continue?"
2. Move `preference-signals.jsonl` to `Archive/preference-signals-YYYY-MM-DD.jsonl`
3. Create a new empty `preference-signals.jsonl`
4. Keep `preference-profile.md` in place (it's still valid until a new synthesis runs)

## File Formats

### preference-signals.jsonl

One JSON object per line, append-only. Two signal sources produce different schemas:

**Observer-loop signals** (primary source at Phase 4, written by background MiniMax M2.7 analysis):
```json
{"date":"2026-04-08","source":"observer-loop","type":"vault-behavior","description":"Keith revised three consecutive trade-idea drafts down from 2% to 1% risk before approving","confidence":"high","skill":"invest-compile"}
{"date":"2026-04-09","source":"observer-loop","type":"trade-preference","description":"Mean-reversion setups with >5 day expected duration archived without review","confidence":"medium","skill":"invest-compile"}
```

**Review queue outcome signals** (secondary source, written when processing review/{trade-ideas,strategy-approvals,alerts,contradictions}/ items):
```json
{"date":"2026-03-28","file":"trade-idea_NVDA_breakout.md","source_skill":"invest-compile","type":"trade-idea","action":"reject","queue":"trade-ideas","days_in_inbox":3,"has_feedback":"yes","feedback":"high IV within 5 days of earnings, skip"}
{"date":"2026-03-28","file":"strategy_pairs-trade-energy.md","source_skill":"invest-compile","type":"strategy","action":"approve","queue":"strategy-approvals","days_in_inbox":1,"has_feedback":"yes","feedback":"good but cap concentration at 15%, not 20%"}
```

When reading signals, check for `source` field (observer-loop) vs `source_skill` field (review queue) to distinguish the schemas. The `queue` field on review-outcome signals indicates which of the four review subfolders the decision came from.

### preference-profile.md

See Phase 3 above for full format. This is a vault note with frontmatter, readable by both Keith in Obsidian and invest-* skills.

## Background Observer Loop

> **Phase 4 deferral.** K2B-Investment Mac Mini provisioning happens at Phase 4. Until then, observer runs manually via `/observe`. The pm2 background loop documented below is the Phase 4 target. For Phase 1-3, ignore the pm2 details and treat all signal harvesting as on-demand from the manual `/observe` command.

K2B-Investment will run a background observer on Mac Mini (`scripts/observer-loop.sh`, managed by pm2 as `invest-observer`). Once Phase 4 ships, this loop:

1. Captures observations via a Stop hook (`scripts/hooks/stop-observe.sh`) after every Claude response
2. When 20+ observations accumulate, calls MiniMax-M2.7 API to analyze patterns _(TODO Phase 2: minimax helpers not yet ported -- the calling script `~/Projects/K2B-Investment/scripts/minimax-observer-analyze.sh` is the planned target)_
3. Writes findings to `observer-candidates.md` (surfaced by session-start hook)
4. Appends detected patterns to `preference-signals.jsonl`
5. Archives processed observations

**How `/observe` relates to the background loop:**
- The background loop (Phase 4+) will run automatically and cheaply via MiniMax (~$0.007/analysis)
- `/observe` is Keith's manual command for on-demand analysis with full Claude reasoning
- `/observe` reads the same files (preference-signals.jsonl, observations.jsonl) and produces the same output (preference-profile.md)
- Once the background loop is live they complement each other: background catches patterns continuously, `/observe` does deep synthesis on demand
- `/observe` should read `observer-candidates.md` (when present) and incorporate any background findings

## Session-Start Inline Confirmation

When observer findings appear in the session-start hook output, act on them immediately -- do not wait for Keith to remember `/observe`. This collapses the old 3-step manual flow (`/observe` -> /learn -> wait for reinforcement) into one natural-language response from Keith. `/observe` remains available for deep synthesis but is no longer required for the loop to close.

### HIGH confidence findings

Present each HIGH finding with three options:

- **confirm** -- run /learn inline with the finding text. This auto-creates a policy ledger entry (the correction becomes an executable guardrail).
- **keep watching** -- do nothing. Let the finding accumulate more evidence before acting on it.
- **reject** -- note the rejection in `wiki/context/preference-signals.jsonl` so the observer learns what Keith does NOT endorse. Use the exact format below (one JSON object per line, trailing newline, atomic write):

```json
{"date":"YYYY-MM-DD","source":"session-start-reject","type":"rejection","description":"<finding text>","confidence":"high","skill":"invest-observer"}
```

### MEDIUM confidence findings

Show MEDIUM findings as context. Do not prompt for action unless Keith asks. They exist to nudge Keith's awareness, not to force a decision.

### Post-action mark

After Keith answers y/n/skip, mark the signal as processed via the helper so both the session-start inline flow and `/observe` deep synthesis filter it out on the next read:

```bash
scripts/observer-mark-processed.sh <signal_id> <confirmed|rejected|watching> [L-ID]
```

_(TODO Phase 2: minimax helpers not yet ported -- the helper script `~/Projects/K2B-Investment/scripts/observer-mark-processed.sh` mirrors the K2B implementation and will be ported alongside the MiniMax worker scripts.)_

Pass `confirmed` when Keith answered yes and a learning was created, `rejected` when he said no (do not surface again), `watching` when he deferred. Include the new L-ID as the third argument when the action produced a learning. `watching` is recorded but does NOT suppress the signal on subsequent reads -- deferred findings resurface next session.

### Idempotency

Once a finding is confirmed/kept/rejected inline, it is considered processed for this session. A subsequent `/observe` run streams `preference-signals.jsonl` using the Phase 1a-filter APPEND-cutoff reader: signals written before the `grandfather-cutoff` line are skipped, signals after the cutoff are filtered by `signal_id` against any `signal-processed` lines with `action: confirmed` or `action: rejected`. Keith is never asked the same question twice.

## Integration Map

```
Stop hook captures observations (Phase 4)
    |
    +--> appends to observations.jsonl
    |
Background observer loop (MiniMax-M2.7, pm2 -- Phase 4 target)
    |
    +--> reads observations.jsonl periodically
    +--> calls MiniMax API for pattern detection (TODO Phase 2: helper not yet ported)
    +--> writes observer-candidates.md (for session-start hook)
    +--> appends to preference-signals.jsonl
    |
Manual /review processing of review/{trade-ideas,strategy-approvals,alerts,contradictions}/ items
    |
    +--> appends to preference-signals.jsonl (review queue outcome schema, with `queue` field)
    |
invest-observer (/observe command) reads preference-signals.jsonl + observer-candidates.md
    |
    +--> detects patterns (deep synthesis)
    +--> writes preference-profile.md
    +--> suggests candidate /learn entries
    |
Session-start hook reads observer-candidates.md
    |
    +--> surfaces findings to Keith
    |
Other invest-* skills can read preference-profile.md before producing output (planned, not yet implemented)
```

## Usage Logging

After completing the main task:
```bash
echo -e "$(date +%Y-%m-%d)\tinvest-observer\t$(echo $RANDOM | md5sum | head -c 8)\tobserved: SUMMARY" >> ~/Projects/K2B-Investment-Vault/wiki/context/skill-usage-log.tsv
```

## Notes

- No em dashes, no AI cliches, no sycophancy
- Never auto-promote learnings without Keith's confirmation
- The preference profile is a reference document, not an enforcement mechanism
- Patterns require 3+ occurrences to be considered real
- The observer suggests, Keith decides
- Keep the profile scannable -- bullet points, not essays
- preference-signals.jsonl is append-only. Never delete or modify existing entries.
- On /observe reset, move the current jsonl to Archive/ with a date suffix, don't delete
- Preference examples must be specific and trade-relevant ("Keith rejects high-IV options entries within 5 days of earnings", "Keith caps concentration at 20%"), never shallow ("Keith likes good trades")
