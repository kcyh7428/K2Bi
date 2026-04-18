---
name: invest-bear-case
description: Run a single adversarial Claude Code call against a thesis before any order ticket. Returns VETO (>70% conviction bear) or PROCEED (with top-3 counter-points). Adapted from AI Investing Lab's 2026 pattern; single call, not a standing agent, per agent-topology.md decision. Use when Keith says /bear <SYMBOL>, "bear-case this", "poke holes in this thesis", or automatically as gate before /invest-ship approves a strategy whose first trade is a specific ticker.
tier: Analyst
routines-ready: true
phase: 2
status: stub
---

# invest-bear-case (STUB -- Phase 2 build work)

Stub skill. Implementation is Phase 2 milestone 2.12. Specs below.

## MVP shape

**Input:** `<SYMBOL>` (required). Optional `--thesis <path>` to target a specific thesis version; default is the live `wiki/tickers/<SYMBOL>.md` body.

**Pipeline:**
1. Read thesis block from `wiki/tickers/<SYMBOL>.md` (or `--thesis <path>`). If no thesis present, refuse with "run /thesis first".
2. Construct a single adversarial prompt to Claude Code: "Here is a bull thesis on <SYMBOL>: ... Your job is to challenge it. Identify the strongest structural reasons this thesis is wrong. Identify the scenarios that invalidate it. Rate your conviction from 0-100 on how wrong the thesis is."
3. Parse Claude's structured response into:
   ```yaml
   bear-conviction: 0-100
   bear-top-counterpoints:
     - "..."
     - "..."
     - "..."
   bear-invalidation-scenarios:
     - "..."
   verdict: VETO | PROCEED
   ```
4. Write the bear block to `wiki/tickers/<SYMBOL>.md` (appends to the thesis, does not overwrite).
5. VETO if `bear-conviction > 70`; PROCEED otherwise. The engine gate (`/invest-ship` strategy approval) hard-refuses approval on VETO.

## Routines-Ready discipline (Analyst tier)

- **Stateless:** each run reads thesis + writes bear block; no process-local state
- **Vault-in/vault-out:** thesis in, bear block out, same file
- **Schedulable:** can run as pre-approval gate automation
- **JSON I/O:** verdict + conviction + bullet lists all YAML-serializable
- **Self-contained prompts:** the adversarial prompt template lives in this skill body; no cross-skill context required

## Non-goals (not in Phase 2)

- NBLM-grounded bear case (Phase 4 if NBLM experiment passes; BLOCKED-state contract means invest-bear-case returns `BLOCKED` if NBLM is down AND NBLM is a committed pillar)
- Multi-round adversarial debate (explicitly rejected per agent-topology.md -- single call only)
- Automatic re-run on 10-Q drops (Phase 4 if needed)

## Hard rule

Per risk-controls.md, the engine refuses to submit an order for a strategy whose primary ticker has `verdict: VETO` without a fresh overriding `PROCEED`. The override cannot be forced via a CLI flag; a fresh bear-case pass must return PROCEED.

## Pedagogical layer (Teach Mode)

At the start of every invocation, read the learning-stage dial:

```bash
LEARNING_STAGE=$(grep -E '^learning-stage:' ~/Projects/K2Bi-Vault/System/memory/active_rules.md 2>/dev/null | sed 's/learning-stage: *//' | tr -d '[:space:]')
LEARNING_STAGE=${LEARNING_STAGE:-novice}
```

If `LEARNING_STAGE` is `novice` or `intermediate`, append the following footer after the VETO/PROCEED block:

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

If `LEARNING_STAGE` is `advanced`, skip the footer.

Terms appearing for the first time in this output that exist in `K2Bi-Vault/wiki/reference/glossary.md` render as `[[glossary#term-name]]`. Terms not yet in the glossary get auto-stubbed per the Teach Mode convention in `CLAUDE.md`.
