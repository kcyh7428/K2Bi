---
proposal-id: 2026-04-18_teach-mode-pedagogical-layer
date: 2026-04-18
author: K2B architect session (Keith via K2B working dir)
status: pending-review
target-vault: K2Bi-Vault/
target-repo: K2Bi/
affects: K2Bi/CLAUDE.md, K2Bi-Vault/wiki/reference/glossary.md (new), K2Bi-Vault/Templates/strategy.md (new), K2Bi/.claude/skills/invest-bear-case/SKILL.md, K2Bi/.claude/skills/invest-execute/SKILL.md, K2Bi-Vault/System/memory/active_rules.md
---

# Proposal: Teach Mode -- Pedagogical Layer for K2Bi

Add four reinforcing layers so K2Bi explains trading concepts in plain English as Keith builds, plus a one-field "learning-stage" dial so verbosity scales down as Keith graduates from novice to advanced.

## Why

Keith stated 2026-04-18 evening (K2B session): "I want it to be able to talk to me using more plain english or insert some explanation on the terminology on those trading -- I need to learn as I build and especially on those strategy I need to grasp a better understanding."

The four layers below are reinforcing, not redundant. Each catches a different moment where comprehension matters:

- **A. CLAUDE.md Teach Mode rule** -- always-loaded behavioral rule that every invest-* skill output prepends a "Plain English" preamble when introducing a new concept
- **B. Living glossary** -- single deep-reference file Keith can search; auto-grows from skill outputs
- **D. Strategy spec extension** -- mandatory "How This Works (Plain English)" section above the YAML rules block; approval is gated on this section being non-empty
- **E. Decision-point footers** -- invest-bear-case VETO/PROCEED + invest-execute fills end with 2-3 sentences translating jargon to dollar/risk impact for Keith's actual position

**The dial:** `learning-stage:` field in `K2Bi-Vault/System/memory/active_rules.md` lets Keith toggle verbosity (`novice` → maximum, `intermediate` → glossary links + decision footers only, `advanced` → glossary links only). Skills check the field at start of each invocation.

Skipped C (`/explain` slash command) per K2B architect's recommendation -- the auto-pedagogy in A/D/E covers 80% of moments. Build C only if Keith finds himself wishing for it.

## The 4 Components + Dial

### A. K2Bi/CLAUDE.md "Teach Mode" Section

Add the following section to `K2Bi/CLAUDE.md`, placed immediately after the existing "Rules" section (or wherever soft behavioral rules currently live). This is an always-loaded behavioral rule per the Memory Layer Ownership matrix -- soft pedagogical discipline is a CLAUDE.md concern, not a skill or code concern.

```markdown
## Teach Mode (Pedagogical Layer)

Keith is learning trading concepts as he builds K2Bi. Every invest-* skill that outputs trading-specific content must apply the pedagogical layer per the active `learning-stage:` setting.

### Behavior by stage

The dial lives at `K2Bi-Vault/System/memory/active_rules.md` as a single line: `learning-stage: novice|intermediate|advanced`. Default is `novice`. Skills read it at start of each invocation.

| Stage | Plain-English preamble | Glossary `[[term]]` links | "Why this matters" decision footer | Strategy "How This Works" section |
|-------|------------------------|--------------------------|------------------------------------|-----------------------------------|
| `novice` (default) | yes -- 2-3 sentences before any technical output | yes -- first occurrence per output | yes -- on every bear-case + execute output | yes -- mandatory, blocks approval if missing |
| `intermediate` | dropped on routine outputs; kept on first-time concepts | yes | yes -- on bear-case + execute | yes -- mandatory, blocks approval if missing |
| `advanced` | off | yes | off | yes -- mandatory, blocks approval if missing (this discipline is permanent) |

The "How This Works" section on strategy specs is **never optional regardless of stage** -- it is the primary input to strategy approval. If you cannot understand WHY a strategy works in plain English, you cannot approve it for real money.

### Glossary integration

The living glossary lives at `K2Bi-Vault/wiki/reference/glossary.md`. When a skill emits a trading term that has a glossary entry, the first occurrence in that output renders as `[[glossary#term-name]]` (Obsidian wiki-link to the section heading). When a skill uses a term not yet in the glossary, it MUST append a stub at the bottom of the glossary file in the same skill run:

```
## new-term-name

_definition pending -- added by invest-thesis 2026-04-19_
```

The next `/invest-compile` run fills out pending stubs. Keith can also fill them manually in 30 seconds in Obsidian. Stubs are visible signals that the glossary is one beat behind reality.

### Reading the dial

Bash one-liner skills can use:

```bash
LEARNING_STAGE=$(grep -E '^learning-stage:' ~/Projects/K2Bi-Vault/System/memory/active_rules.md 2>/dev/null | sed 's/learning-stage: *//' | tr -d '[:space:]')
LEARNING_STAGE=${LEARNING_STAGE:-novice}
```

If the field is missing, default to `novice`. Skills should never fail because the dial is unset.

### What this is NOT

- Not a tutorial system. The pedagogical layer is in-flow only -- explanations live next to the actions they describe.
- Not a replacement for the glossary. A/D/E surface explanations at the moment of use; the glossary is the deep reference Keith can search later.
- Not optional verbosity Keith can ignore. The strategy "How This Works" gate is enforced by `/invest-ship` (commit-msg hook), not just by convention.
```

### B. Living Glossary (New File)

Create `K2Bi-Vault/wiki/reference/glossary.md` with the content below. Seeded with 14 terms drawn from the planning docs Keith has already encountered; grows organically from here.

```markdown
---
tags: [glossary, k2bi, reference, pedagogy]
date: 2026-04-18
type: glossary
origin: k2b-generate
up: "[[index]]"
---

# K2Bi Trading Glossary

Living glossary of trading terms used by K2Bi skills. Grows organically as new concepts appear in skill outputs. Skills auto-stub new terms; definitions filled by Keith or by `/invest-compile`.

**Format per term:**
- `## term-name` heading (lowercase, hyphenated -- so `[[glossary#term-name]]` links work cleanly)
- 1-2 sentence definition in plain English
- **Why it matters:** line tied to retail trading reality, not textbook abstraction

If you see `_definition pending_` under a heading, that's an auto-stub from a recent skill run -- fill it or wait for the next `/invest-compile`.

## sharpe-ratio

Return per unit of risk taken. Calculated as (return - risk-free rate) / standard deviation of returns. Higher = better risk-adjusted return.

**Why it matters:** A strategy returning 8% with Sharpe 0.3 is worse than one returning 5% with Sharpe 1.5 -- the second earns more per dollar of risk taken. Most professional traders target Sharpe > 1.0 before deploying live capital.

## sortino-ratio

Like Sharpe but only penalizes downside volatility (losses below a target return), not upside swings. More forgiving of strategies that have sharp winning months.

**Why it matters:** Sharpe punishes a strategy that has a +30% month even though that's good news. Sortino measures what most traders actually care about: how much pain you go through to earn the gain.

## drawdown

Peak-to-trough loss before a new portfolio high is reached. Expressed as a percentage from peak.

**Why it matters:** A "20% max drawdown" means at the worst point you were down 20% from your highest balance, even if you ended the year up. Determines how much pain you can psychologically stomach. Most retail blow-ups happen during drawdowns when traders override their rules.

## walk-forward validation

Backtest method that mimics live trading: train strategy on a rolling historical window, test on the next unseen window, repeat across the full history. Prevents the cheat of optimizing on data the strategy wouldn't have had access to in real life.

**Why it matters:** A strategy that backtests at 200% return but walk-forwards at 8% is closer to honest. Most flashy backtest claims (the famous "11000% P&L" cases) come from look-ahead bias the walk-forward catches.

## look-ahead bias

A backtest cheat where the strategy uses information that wasn't available at the moment it would have made the trade.

**Why it matters:** Easy to introduce accidentally -- using today's closing price in a strategy that runs at 9am, for example. Walk-forward validation + point-in-time data stores prevent it. Without the prevention, you ship a strategy that "worked" on paper and bleeds in production.

## kill-switch

A code-enforced mechanism that immediately halts all trading and refuses new orders until a human intervenes. K2Bi: `.killed` lock file at the project root.

**Why it matters:** Knight Capital lost $440M in 45 minutes from a faulty algorithm with no kill switch. Untested kill switches fail exactly when needed -- K2Bi tests its own monthly during paper phase.

## strategy approval

A human (Keith) approves a strategy spec -- its rules, sizing, risk envelope. The bot then executes individual trades within those rules WITHOUT per-trade approval.

**Why it matters:** Trade-by-trade human override correlates with 68% capital loss on average (research finding from K2Bi's risk-controls planning doc). Strategy-level approval is the only safe semi-auto pattern.

## bear case

The strongest argument AGAINST a trade thesis. K2Bi runs a single Claude Code call (`invest-bear-case`) to generate this before any order ticket is created. Returns VETO (>70% conviction against) or PROCEED (with top-3 counter-points to monitor).

**Why it matters:** Retail investors skip this step. Forcing a structured "what could break this" pass catches confirmation bias before money moves. The single-call form (not a standing agent) is per K2Bi's agent-topology decision.

## position sizing

How much capital is committed to a single trade. K2Bi enforces: max 20% portfolio per ticker, max 1% portfolio risk per trade (risk = position size × stop-loss distance).

**Why it matters:** Bad position sizing kills more accounts than bad strategies. Even a 60% win rate strategy fails if losers are 4× winners. The 1% rule means a single bad trade never moves the portfolio more than 1%.

## slippage

The difference between the price you expected and the price you actually got. Always negative for marketable orders -- you bought higher or sold lower than the quote.

**Why it matters:** Free backtests assume zero slippage. Real trading has 0.05-0.5% per round trip on liquid US stocks, more on illiquid. Strategies with edge < slippage lose money in production even when they win in backtest.

## fee erosion

Cumulative trading fees as a percentage of gross gains. K2Bi targets fees < 30% of gains (research consensus); above that, the strategy is overtrading.

**Why it matters:** One slow-bleed case study went from $500 to $0 with $1152 in fees over 814 trades. The strategy "worked" -- the broker just won.

## decision journal

Append-only log of every trade decision with the "why" behind it. K2Bi writes one entry per trade attempt (success OR validator rejection) to `K2Bi-Vault/raw/journal/<date>.jsonl`.

**Why it matters:** Tradecraft pattern. Even a 0% win rate strategy is recoverable if the journal shows systematic reasoning. Random rejections without rationale are the real failure mode -- you can't debug what you didn't record.

## regime

The current market environment classification (crash / bear / neutral / bull / euphoria). Strategies are tested + approved against specific regimes.

**Why it matters:** A momentum strategy that crushed in the 2024 bull market crashes in 2025 sideways action. Regime-aware execution prevents running good strategies in bad weather. K2Bi's `invest-regime` skill writes the current classification to `wiki/regimes/current.md`.

## circuit breaker

Account-state trigger that halves or stops trading on drawdowns. K2Bi's three layers: -2% intraday halves position sizes for the day, -3% closes all positions, -10% writes `.killed` and requires manual restart.

**Why it matters:** Stops the spiral where losses trigger emotional doubling-down. Code enforcement, not willpower. Each breaker is tested via simulated drawdown before paper phase begins.

## paper trading

Simulated trading on a real broker's infrastructure with fake money. Order types, fills, slippage are modeled but no real cash is at risk.

**Why it matters:** The 90-day paper requirement for K2Bi (Phase 5) catches infrastructure bugs, prompt failures, and behavioral patterns BEFORE real money is exposed. Every shortened paper window in the research corpus is associated with a failure mode.

(More terms added as skills encounter them via the auto-stub mechanism described above.)

## Related

- [[teach-mode-overview]] -- the pedagogical layer this glossary is part of (if a top-level overview is created)
- `K2Bi/CLAUDE.md` Teach Mode section -- the behavioral rule that links skill outputs here
- [[strategy-template]] -- the template that requires a "How This Works (Plain English)" section
```

### D. Strategy Spec Template (New File)

Create `K2Bi-Vault/Templates/strategy.md` (creating the `Templates/` directory if it doesn't exist) with the content below. The template enforces the "How This Works (Plain English)" section as the first section after frontmatter.

The `K2Bi/.githooks/commit-msg` hook (currently a no-op for K2Bi per its Phase 1 stub) gets a Phase 2 extension: when committing changes to `wiki/strategies/feature_*.md` (or however K2Bi structures the strategy lane), the hook MUST verify the "How This Works (Plain English)" section exists and is non-empty before allowing `status: approved` to land. This is a Phase 2 implementation task, not part of this PR's merge -- this PR adds the template + behavioral rule; the hook enforcement ships as part of Phase 2 milestone 2.17 (strategy approval flow).

Template content:

```markdown
---
tags: [strategy, k2bi]
date: YYYY-MM-DD
type: strategy
origin: keith
status: proposed   # proposed | approved | active | paused | retired
up: "[[../strategies/index]]"

# Backtest metadata (filled by invest-backtest)
backtest:
  data-window: ""
  sharpe: null
  sortino: null
  max-drawdown: null
  win-rate: null
  total-trades: null
  point-in-time: false
  walk-forward: false   # set true once Phase 4 walk-forward harness exists

# Approval metadata (filled by /invest-ship on status: approved)
approval:
  approved-at: null
  approved-by: keith
  bear-case-verdict: null   # PROCEED | VETO
  codex-review: null

# Risk envelope (validators read this at runtime)
risk-envelope:
  max-position-pct: 5.0       # max % of portfolio for this strategy's positions combined
  max-trade-risk-pct: 0.5     # max % of portfolio risked per trade (position * stop-loss-distance)
  max-daily-loss-pct: 1.0
  regime-filter: ["bull", "neutral"]   # active regimes for this strategy
  instrument-whitelist: []    # tickers this strategy can trade
---

# [Strategy Name]

## How This Works (Plain English)

[MANDATORY. 2-5 sentences in plain English explaining:
1. What this strategy is trying to do (the thesis)
2. How it decides when to enter
3. How it decides when to exit
4. What the risk envelope means in dollar terms for a HK$1M portfolio
5. What conditions would make this strategy a bad fit (regime mismatch, etc.)]

[Example for SPY weekly rotation:
"This strategy holds the broad US stock market (SPY) from Monday open to Friday close every week, then sits in cash over the weekend. It's a baseline 'is the engine working' strategy, not a money-maker -- expected return is roughly buy-and-hold SPY minus 5 days of overnight risk. Position size is capped at 5% of HK$1M = HK$50,000 per week. The 1% trade risk cap means if SPY gaps down 5% before our Friday exit, we lose HK$2,500 max (5% of HK$50K). This strategy is a bad fit during high-volatility regimes (VIX > 30) because the weekend exit can miss continuation moves; the regime-filter blocks new entries during 'crash' or 'bear' classifications."]

This section is mandatory. K2B drafts it when proposing the strategy. Keith reviews it as the primary input to strategy approval. If you cannot understand WHY this strategy works in plain English, do not approve it.

## Entry Rules

[Specific conditions that must hold to open a position. Reference indicators, time windows, regime requirements.]

## Exit Rules

[Stop-loss, take-profit, time-in-trade max, regime-change exits.]

## Position Sizing

[Formula for how large a position is for a given signal. Must respect risk-envelope above.]

## Backtest Notes

[Filled by `invest-backtest`. Includes data window, Sharpe, max DD, win rate, point-in-time confirmation, walk-forward result if Phase 4 harness exists.]

## Bear Case

[Filled by `invest-bear-case` before approval. Top 3 counter-points to monitor, OR VETO with reasoning if conviction > 70%.]

## Approval Notes

[Filled by Keith at `/invest-ship` time. Why approved, what would trigger retirement, what to watch.]

## Performance Log

[Append-only entries from `invest-journal`. Weekly P&L, slippage vs expectation, fee erosion, observer signals.]

## Related

- [[../tickers/<TICKER>]] -- per-ticker thesis pages this strategy trades
- [[../regimes/current]] -- current market regime
- [[../../glossary]] -- for terminology used in this spec
```

The template is referenced by `invest-vault-writer` when it creates a new strategy spec, and by `/invest-ship` when it validates the spec before flipping `status: approved`.

### E. Decision-Point Footers (invest-bear-case + invest-execute SKILL.md)

Both skills already exist as stubs after Phase 2 scaffold (per the merged PR #1). This PR specifies the output convention to add to each SKILL.md. K2Bi session inserts the convention into both skill bodies on merge.

**Convention for `invest-bear-case` output:**

After the VETO/PROCEED block, append (only if `learning-stage` is `novice` or `intermediate`):

```markdown
---
**Why this matters for your position:**

[2-3 sentences translating the technical bear case to dollar/risk impact for Keith's actual portfolio. Reference: current cash, current open positions in the same ticker or correlated names, current daily risk envelope used vs available. Use HKD figures since K2Bi is HKD-denominated.]
```

Example:

```markdown
**bear-case verdict:** PROCEED

Top counter-points to monitor:
1. NVDA's data-center revenue concentration (87% of growth) is a single-customer risk -- one hyperscaler delaying capex would compress the multiple sharply.
2. The 28x forward P/E assumes 35% earnings growth holding through 2027; consensus is already pricing perfect execution.
3. Geopolitical: the 2026 export-control regime extension to "tier 2" chips would exclude NVDA's H100 successors from China entirely.

---
**Why this matters for your position:**

You currently hold no NVDA position and have HK$50K available in your daily risk envelope. If you take a 5% portfolio position (HK$50K at current price ~$700, that's ~70 shares) with a 10% stop-loss, your max loss on this trade is HK$5K -- well within the 1% trade-risk cap. The bear case is not strong enough to veto, but the data-center concentration risk means you should size at the lower end (3% rather than 5%) until next quarter's earnings confirms the growth pace.
```

**Convention for `invest-execute` output (per fill):**

After the standard fill receipt + decision journal entry confirmation, append (only if `learning-stage` is `novice` or `intermediate`):

```markdown
---
**Why this matters for your position:**

[2-3 sentences explaining what the fill changes: new total exposure, percentage of portfolio now in this name, daily risk budget remaining after this trade, any concentration or correlation flag, what watch-points are now active (stop-loss level in HKD, take-profit if any).]
```

Example:

```markdown
**Fill received:**

- order-id: O-2026-04-22-0017
- ticker: SPY
- action: buy
- quantity: 70 shares
- fill-price: $498.32
- slippage-vs-expected: -$0.08 (within 0.05% expectation)
- decision-journal: T-2026-04-22-0017

---
**Why this matters for your position:**

You now hold 70 shares of SPY at HK$50,820 (~5.1% of portfolio). Stop-loss is at $448.49 (10% below entry); if hit, you lose HK$5,082 -- exactly at the 1% trade-risk cap, no margin to spare. Daily risk envelope: HK$10,000 used of HK$10,000 budget today, so this is the last trade until tomorrow. Take-profit at Friday close per strategy rules. No correlated positions open, so no concentration flag.
```

Both skills check the dial via the bash one-liner from section A and skip the footer if `learning-stage: advanced`.

### Dial: learning-stage Field in active_rules.md

Add the following entry to `K2Bi-Vault/System/memory/active_rules.md`. Insert as a new rule after the existing rule list (or as rule #5 if there's room before the LRU cap).

```markdown
## 5. Pedagogical layer (learning-stage dial)

`learning-stage: novice`

This single-line field tunes how verbose K2Bi's pedagogical layer is. Values:
- `novice` (default) -- maximum: plain-English preambles, glossary `[[term]]` links, decision-point footers, mandatory strategy "How This Works" sections
- `intermediate` -- glossary links + decision footers; preambles dropped on routine outputs
- `advanced` -- glossary links only; pedagogical layer otherwise off

The strategy "How This Works (Plain English)" gate is **never optional regardless of stage** -- it's the primary input to strategy approval and code-enforced by `/invest-ship`.

**Why:** Keith is learning trading concepts as he builds K2Bi (per 2026-04-18 evening conversation). Verbosity should scale down as comprehension grows; the dial gives Keith control without requiring a code change.

**How to apply:** Skills read this field at start of each invocation via:
`LEARNING_STAGE=$(grep -E '^learning-stage:' ~/Projects/K2Bi-Vault/System/memory/active_rules.md 2>/dev/null | sed 's/learning-stage: *//' | tr -d '[:space:]'); LEARNING_STAGE=${LEARNING_STAGE:-novice}`

Default to `novice` if missing. Skills must never fail because the dial is unset.

To graduate: edit this file directly in Obsidian, change `novice` → `intermediate` → `advanced`. Or use `/learn intermediate` once the `k2b-feedback` skill is ported to K2Bi.
```

If active_rules.md is already at the LRU cap of 12, this rule replaces the least-reinforced-in-last-30-days rule per the auto-demote policy on line 1 of active_rules.md.

## Acceptance Instructions (K2Bi Session)

On merge of this PR:

1. **Append the "Teach Mode (Pedagogical Layer)" section** to `K2Bi/CLAUDE.md` immediately after the existing "Rules" section (or wherever soft behavioral rules currently live). Use the full text in section A above.
2. **Create `K2Bi-Vault/wiki/reference/glossary.md`** with the full content from section B above. Add a link to it from `K2Bi-Vault/wiki/reference/index.md`.
3. **Create `K2Bi-Vault/Templates/`** directory if it doesn't exist. Add `Templates/strategy.md` with the full template content from section D. Add a link to it from `K2Bi-Vault/Templates/index.md` (creating that index if needed).
4. **Update `K2Bi/.claude/skills/invest-bear-case/SKILL.md`** -- add the output convention from section E to the skill body (Output section). Wire the `learning-stage` dial check.
5. **Update `K2Bi/.claude/skills/invest-execute/SKILL.md`** -- same as #4, with the per-fill convention from section E.
6. **Append the new rule #5 "Pedagogical layer (learning-stage dial)"** to `K2Bi-Vault/System/memory/active_rules.md`. If at LRU cap, demote the least-reinforced rule per the cap policy on line 1.
7. **Update `K2Bi/.githooks/commit-msg`** -- add a Phase 2 task tag (or open an issue / TODO) noting that the strategy approval gate must verify the "How This Works (Plain English)" section is non-empty before allowing `status: approved` to land. Implementation is Phase 2 milestone 2.17 work, not part of this PR's merge.
8. **Run `/invest-ship`** to land the merge with Codex review on the application. Suggested commit message: `chore: apply Teach Mode pedagogical layer (proposal 2026-04-18)`.
9. **Update `wiki/log.md`** via single-writer helper with the pedagogical layer event.

## Testing After Apply

K2Bi session can self-test the new layer:

1. Read the glossary file in Obsidian -- click through `[[glossary#sharpe-ratio]]` from any wiki page that mentions Sharpe to verify the wiki-link works.
2. Create a test strategy spec from `Templates/strategy.md` -- verify the "How This Works (Plain English)" section is the first section after frontmatter and is unambiguously mandatory.
3. Read the new CLAUDE.md Teach Mode section end-to-end -- verify it makes sense to a fresh Claude Code session reading cold.
4. Toggle `learning-stage: novice → intermediate → advanced` in `active_rules.md`, run a stub invest-bear-case + invest-execute call (dry-run), verify the footer presence/absence matches the table.

## Future Extensions (Out of Scope for This PR)

- **Option C: `/explain` slash command** -- on-demand explainer skill (`/explain sharpe-ratio`) that returns a tailored definition grounded in Keith's open positions. Defer until Keith finds himself wishing for it during burn-in.
- **`/learn intermediate` shortcut** -- requires porting `k2b-feedback` skill to K2Bi (currently not a Phase 1 port per [[skills-design]]). Edit `active_rules.md` directly in Obsidian until then.
- **Glossary auto-fill via MiniMax** -- the M2.7 worker pattern from `k2b-compile` could fill stub definitions in batch during `/invest-compile`. Defer until stub backlog grows past 5 entries.
- **Plain-English skill output verification eval** -- a `k2b-autoresearch`-style evaluator that checks whether outputs include the required pedagogical sections per the active stage. Defer until evidence shows skills drift from the convention.

## Related

- `proposals/2026-04-18_phase2-mvp-scaffold-revision.md` -- the prior architectural revision (PR #1, merged) that this PR layers on top of
- `K2Bi/CLAUDE.md` -- the always-loaded behavioral rules this PR extends
- `K2Bi-Vault/System/memory/active_rules.md` -- where the dial lives
- `K2Bi-Vault/wiki/reference/glossary.md` -- the new file (B)
- `K2Bi-Vault/Templates/strategy.md` -- the new file (D)
- `K2Bi/.claude/skills/invest-bear-case/SKILL.md` + `K2Bi/.claude/skills/invest-execute/SKILL.md` -- the skills extended (E)
