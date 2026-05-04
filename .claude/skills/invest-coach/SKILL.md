---
name: invest-coach
description: Multi-turn coaching skill that walks Keith through the existing K2Bi pipeline (narrative -> screen -> thesis -> bear-case -> backtest -> strategy spec -> approval) so he can produce hedge-fund-analyst-grade inputs unaided. The coach NEVER bypasses any gate. Use when Keith says /invest-coach, "walk me through this trade", "help me build a thesis", or brings a fresh lived signal he wants to take end-to-end.
tier: portfolio-manager
routines-ready: false
---

# invest-coach

Multi-turn coaching skill. Makes the existing K2Bi pipeline reachable for a novice operator by drafting analytical content, presenting it section-by-section, and pausing for operator confirmation at every gate.

## When to Trigger

- Keith says `/invest-coach`, `/coach`, "walk me through this trade", "help me build a thesis"
- Keith brings a fresh lived signal and wants to run it end-to-end through the pipeline
- Keith wants to resume a partially-completed coach session
- Any time Keith needs translation between his domain expertise and the structured inputs the pipeline expects

## When NOT to Use

- Keith already has an approved strategy and wants to submit an order -> route to `/execute`
- Keith wants a quick backtest on an existing strategy -> route to `/backtest <strategy>`
- Keith wants adversarial review on an existing thesis -> route to `/bear <SYMBOL>`
- Keith wants to edit validator config -> route to `/propose-limits`

## Plug-in point

Front-end before invest-narrative; orchestrates the full pipeline through to `/invest-ship --approve-strategy`.

The coach OWNS the multi-turn conversation. It INVOKES existing skills for their specialized work. It does NOT modify them.

## Multi-turn conversation pattern

| Turn | Coach posture | Operator action | Output |
|---|---|---|---|
| **T0** | Ask: new lived signal or resume existing? If resume, read context artifact + scan partial vault writes, infer state, present summary, ask where to pick up. | Says "new" or "resume <sigid>". Confirms or corrects inferred resume point. | State oriented; subsequent turns proceed from correct entry point |
| **T1** | Ask for the lived signal in operator's own words. | Free-text narrative. | Raw narrative captured atomically to `context_<sigid>-lived-signal.md` |
| **T2** | Restate signal in structured form: macro narrative + "why this matters now" + value-chain hint. Ask to confirm or correct. | Confirms or corrects. May iterate 2-3 times. | Refined narrative section populated |
| **T3** | Call `/invest-narrative "<refined narrative>"`. Present sub-themes + candidates as digestible list. Flag priced-in warnings or thin-set verdicts. Surface L-2026-04-27-001 conflict if discovery output violates constraints. | Reads, asks questions, picks 1-3 to promote. | Theme file written; --promote invocation(s) |
| **T4** | After --promote, read Quick Score output. Write plain-English summary per ticker: "scored B because [...]; sub-factor [N] pulled it down; that means [...]". | Reads, asks follow-ups, picks 1 ticker for thesis. | Watchlist enriched; one ticker chosen |
| **T5** | Ask for 4-source set (10-K, 10-Q, last 4 earnings transcripts, optional deck). Ask at T5 close: "handoff to deep-research vendor at T5.5, or draft section-by-section in T6?" | Provides URLs or accepts coach's offer. Picks T5.5 elect or skip. | /research output ready; T5.5 election recorded |
| **T5.5 (OPTIONAL)** | **OPERATOR-ELECTED ONLY.** Draft structured research prompt (Ahern + sub-score + asymmetry questions referencing T5 source set). Present prompt. Operator runs externally on vendor of choice and pastes response back. Ingest as DRAFT MATERIAL, never load-bearing. Write `vendor_provenance:` block to thesis frontmatter atomically. | Picks vendor, runs prompt externally, pastes response. | `vendor_provenance:` frontmatter block + ingested draft queued for T6 |
| **T6** | Draft Ahern 4-phase section by section. Phase 1 first, ask "does that match what you observed?". Repeat for Phases 2-4. Then draft 5-dim thesis sub-scores with band justifications. Same for fundamental sub-scores. Same for EV-weighted asymmetry. If T5.5 elected: present vendor draft section-by-section for review. | Reviews each section, asks questions, accepts or rejects framings. | Structured thesis input ready; each confirmed sub-section writes atomically |
| **T7 (MVP-2)** | List every load-bearing claim. Pre-fetch source URL where possible; present excerpt side-by-side with curated info set framing. Ask operator to mark `verified | refused | override | advisory`. NEVER auto-mark. If T5.5 elected: surface explicit vendor warning at entry. Spot-check is operator-elected ONLY ("spot-check this claim"). Vendor-must-differ enforced. | Clicks through sources, marks each claim, writes notes for refusals. | `verification:` block ready; `generate_thesis` writes or raises ValueError |
| **T8** | Call `invest-bear-case` (single adversarial call). Read VETO/PROCEED + counter-points. Translate into plain English with calibration support. | Reads, calibrates against own knowledge, decides recalibrate or proceed. | bear_verdict + bear_score + counter-points captured |
| **T9** | Call `invest-backtest`. Read Sharpe/DD/win rate. Translate: "Sharpe [N] is [moderate/strong/weak] vs SPY. Max drawdown [N]% means worst point down [N]%." | Reads, asks follow-ups, decides yes / recalibrate / abort. | Backtest verdict captured |
| **T10** | Draft strategy spec with bucket rules from thesis. Walk through each bucket: "bucket-4 EXIT fires at [metric] [op] [threshold]; does this match your intent?" | Reviews, asks questions, confirms or recalibrates. | Strategy spec draft ready |
| **T11 (MVP-3)** | Ask operator to paste most recent management forward guidance for each thresholded metric. Assemble `forward_guidance_check:` block. If any threshold sits inside guide: surface contradiction, suggest recalibration, offer override LAST with L-2026-04-30-001 framing visible. | Pastes guidance. Recalibrates if MVP-3 flags. | `forward_guidance_check:` block populated; status='pass' or 'override' |
| **T12** | Summarize everything: lived signal, theme, candidate, thesis with verification, bear-case, backtest, strategy spec with forward-guidance check. If T5.5 elected: name vendor explicitly, list verified claim count + overrides. If overrides taken: list every override with structured text. Suggest `/invest-ship --approve-strategy <slug>`. Coach falls silent. | Reviews summary. Runs `/invest-ship --approve-strategy <slug>` directly. | Approval gate evaluates; pass or refuse is binary |
| **T13 (conditional)** | If approval refuses, operator re-engages coach. Coach reads refuse message, diagnoses failed turn, walks back to relevant stage, re-runs. | Re-engages, accepts diagnosis, walks back. | New attempt with corrected input |

### Pause points

T0, T1, T2, T3, T4, T5, T5.5, T6, T7, T8, T9, T10, T11, T12.

The biggest pause point is T7: primary-source clicking is operator manual work. T5.5 prior context tightens the entry warning but does not reduce the manual work.

### Generation points

T0 (resume summary), T2 (refined narrative), T3 (theme digest), T4 (enrichment translation), T5.5 (research prompt + vendor_provenance assembly), T6 (thesis sections + sub-scores), T7 (per-claim source pre-fetch + framing comparison + vendor warning), T8 (bear-case translation), T9 (backtest translation), T10 (strategy spec draft), T11 (forward-guidance assembly), T12 (final summary).

## Write-as-you-go discipline (state persistence)

Every confirmed turn writes its output to the vault atomically (tmp + os.replace). No `coach-state-<sigid>.md` scratch file. The state spine is:

- `context_<sigid>-lived-signal.md` (T0 reads, T1 + T2 write, every later turn appends a Lineage row)
- `wiki/macro-themes/theme_<slug>.md` (T3 writes via invest-narrative)
- Watchlist entry (T3 --promote writes; T4 invest-screen enriches)
- `wiki/tickers/<SYMBOL>.md` draft (T5.5 writes vendor_provenance if elected; T6 builds Ahern phases + sub-scores atomically; T7 verification gate writes final file or refuses)
- `wiki/strategies/strategy_<slug>.md` draft (T10 writes; T11 adds forward_guidance_check block)

On resume (T0): coach reads the lived-signal artifact + scans the above paths + reconstructs resume state. Mid-turn pauses recover by re-reading the partial draft state already in the vault.

## Teach Mode integration

invest-coach is the canonical novice-tier entry point.

| Stage | Coach behavior |
|---|---|
| `novice` (default) | Full multi-turn pattern. Plain-English preambles before every technical output. Glossary `[[term]]` links on first occurrence per output. "Why this matters" decision footer on every gate (T7, T8, T11). Strategy "How This Works (Plain English)" section drafted at T10 and shown before lock. |
| `intermediate` | Same multi-turn structure. Preambles dropped on routine outputs (T3, T4, T9). Glossary links + decision footers retained on T7, T8, T11. "How This Works" still mandatory at T10. |
| `advanced` | Multi-turn structure compressed: T6 thesis can be auto-drafted in one pass (operator reviews whole draft, not per-section). T7 verification still per-claim (gate permanent). T10 bucket-rule still operator-confirmed (gate permanent). T11 forward-guidance gate permanent. Glossary links retained. "How This Works" permanent. |

The strategy "How This Works (Plain English)" gate is NEVER optional regardless of stage. It is code-enforced by `/invest-ship` (commit-msg hook).

## Verification handoff (T7)

This is the load-bearing turn. The CALX cycle proves what happens when this step is bypassed.

1. **Coach lists every load-bearing claim** as a numbered table with claim_id, claim text, and source_url.
2. **Coach pre-fetches the source where possible.** If the source URL is open-web and HEAD-checkable, fetches and quotes the relevant excerpt side-by-side with the curated info set's framing.
3. **LLM spot-check backstop is OPERATOR-ELECTED, not auto-invoked.** Operator says "spot-check this claim". Only then does coach invoke a single call to a spot-check vendor. Default is per-claim manual click-through. **Vendor-must-differ constraint:** the spot-check vendor MUST differ from whoever produced the curated info set (or the T5.5 vendor if elected). Coach NEVER auto-decides verification.
4. **Operator marks each claim**: `verified | refused | override | advisory`. For `refused` or `override`, operator writes a note >= 20 chars.
5. **Aggregate decision**: `pass` if all load-bearing claims verified; `operator-override` if some refused but operator accepts (reason >= 20); `refuse` if operator declines to override. On `refuse`, `generate_thesis` raises `ValueError`; no `wiki/tickers/<SYMBOL>.md` is written.
6. **Coach surfaces L-2026-04-30-001 framing on the override path**: "operator-override is available, but it's the failure mode this gate is designed to prevent. The disciplined response is to refuse the thesis and correct the info set. Override only if the refused claim is genuinely advisory and your conviction holds without it."

## T5.5 bulk-research-handoff

**OPERATOR-ELECTED ONLY. NEVER auto-invoked.**

- Operator elects at T5 close: "handoff to deep-research vendor, or draft in T6?"
- If elected: coach drafts structured research prompt covering Ahern + sub-score + asymmetry questions referencing the T5 source set explicitly.
- Operator runs the prompt externally on vendor of choice. Coach does NOT auto-invoke.
- Operator pastes vendor response back. Coach ingests as DRAFT MATERIAL for T6, NEVER as load-bearing claims. Every vendor claim is tagged `un-verified` until T7 manual click-through.
- Coach writes `vendor_provenance:` block to thesis draft frontmatter capturing `{vendor, timestamp, prompt, source_set_ref}`. Atomic tmp + os.replace.
- T7 vendor-must-differ: a Kimi-sourced T5.5 claim cannot be spot-checked by another Kimi call. T7 spot-check vendor MUST differ from T5.5's vendor.
- T7 entry warning surfaces explicitly when T5.5 elected: "this thesis was drafted from `<vendor>` deep research. The verification gate that follows exists because vendor output without primary-source verification is the CALX failure mode (L-2026-04-30-001). Do not skip this turn."
- T12 final summary names the vendor explicitly when T5.5 elected. When T5.5 skipped, T12 omits the vendor section entirely (no empty stub).

## invest-feedback auto-capture (D7)

When operator rejects a coach-generated framing at any turn (T2 narrative restate, T6 sub-score band, T8 bear-case calibration, T10 bucket rule), the rejection event is auto-captured.

Call signature:
```python
from scripts.lib.invest_coach import capture_coach_rejection
path = capture_coach_rejection(
    vault_root=Path("~/Projects/K2Bi-Vault"),
    sigid="<sigid>",
    turn_id="T2",  # or T6, T8, T10
    rejected_framing="<the coach text that was rejected>",
    operator_correction="<the operator's corrected framing>",
)
```

This atomically writes `K2Bi-Vault/raw/coach-feedback/<sigid>_<turn>_rejected.md` with:
- Frontmatter: tags, date, type=coach-feedback, origin=keith, up, sigid, turn_id
- Body: rejected framing block + operator correction block

The existing invest-feedback skill (`/learn`, `/error`, `/request`) continues to operate independently. The coach auto-capture is a separate stream that seeds the learnings file with raw feedback for downstream pattern analysis.

## Stage advancement reflection (D8)

At end of each completed coach session (T12 reached), coach runs brief reflection:

1. Count distinct concepts the operator explained back without coach explanation.
2. If >=3 distinct concepts: suggest "want me to drop the novice preamble on similar outputs next session?"
3. Operator confirms yes/no.
4. On yes, coach writes new dial value to `active_rules.md` using compare-and-swap (CAS) guard: reads current value, confirms it matches expected, writes new value under flock. If concurrent session changed the value, CAS refuses and the current session declines to suggest.

## What invest-coach OUTPUTS

| Output artifact | Path | Consumed by |
|---|---|---|
| Lived signal capture | `wiki/context/context_<sigid>-lived-signal.md` | Coach itself; provenance spine |
| Narrative | inline to `/invest-narrative` | invest-narrative |
| Promotion decision | `--promote <SYMBOL>` | invest-narrative writer |
| Verification record | `verification:` block in thesis frontmatter | invest-thesis MVP-2 gate |
| Vendor provenance (T5.5 only) | `vendor_provenance:` block in thesis frontmatter | T7 vendor-must-differ + T12 visibility |
| Forward guidance paste | `forward_guidance_check:` block in strategy spec frontmatter | strategy spec MVP-3 gate |
| Strategy spec draft | `wiki/strategies/strategy_<slug>.md` (status: proposed) | `/invest-ship --approve-strategy` |

## Safety / negative space

1. Does NOT bypass any gate.
2. Does NOT auto-verify.
3. Does NOT substitute un-grounded LLM output for primary sources.
4. Does NOT author thesis directly bypassing invest-thesis.
5. Does NOT submit orders to the engine.
6. Does NOT promote a candidate without operator decision.
7. Does NOT bypass single-call discipline for invest-bear-case.
8. Does NOT replace invest-autoresearch.
9. Does NOT pollute existing skills with coach-specific knobs.
10. Does NOT run from Telegram in MVP.
11. Does NOT auto-invoke the LLM spot-check backstop.
12. Does NOT auto-flip the learning-stage dial.
13. Does NOT auto-invoke deep-research vendors at T5.5.

## Cross-links

- `K2Bi-Vault/proposals/2026-05-03_invest-coach-spec.md` -- full spec with D1-D10
- `K2Bi-Vault/proposals/2026-05-03_k2bi-ux-audit-operator-fit.md` -- motivating audit
- `K2Bi-Vault/wiki/insights/2026-04-30_calx-shadow-verification-rerun.md` -- failure mode the coach respects
- `K2Bi-Vault/System/memory/self_improve_learnings.md` L-2026-04-27-001, L-2026-04-27-004, L-2026-04-27-005, L-2026-04-30-001
- `K2Bi-Vault/wiki/context/policy-ledger.jsonl` -- executable guards
