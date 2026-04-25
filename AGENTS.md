# K2Bi -- Agent Onboarding Guide

This file is the single source of truth for AI coding agents working on K2Bi.
K2Bi is "Keith 2nd Brain for Investment" -- a Python-based trading research,
backtesting, and semi-automated execution system. It runs locally on macOS
(development) and on a Hostinger KL VPS (Trader tier; Mac Mini deprecated for
K2Bi compute as of Phase 3.9). Read this file first before making any code
changes.

---

## 1. Project Overview

K2Bi is a standalone Python project with three primary responsibilities:

1. **Research & ground theses** -- per-ticker research, screening, watchlist curation.
2. **Generate, backtest, and gate strategies** -- strategy specs with deterministic
   pre-trade validators, adversarial bear-case review, and approval rituals.
3. **Execute under hard limits** -- a standalone Python engine reads approved
   strategies from the vault and enforces position sizing, circuit breakers,
   kill-switch, and cash-only trading before submitting orders to IBKR paper.

The execution engine is **architecturally isolated** from Claude Code. Claude
generates, backtests, approves, and monitors strategies. The engine enforces
hard limits and places orders. Claude CANNOT bypass validators, force a trade,
or delete the `.killed` lock file.

- **Code repo**: `~/Projects/K2Bi/` (this directory), git-tracked, GitHub remote.
- **Vault**: `~/Projects/K2Bi-Vault/` (Syncthing-managed, NOT a git repo).
- **Broker**: IBKR HK demo paper account (IB Gateway on localhost:4002).
- **VPS server**: `ssh hostinger` (Hostinger KL, Phase 3.9+; Mac Mini deprecated for K2Bi compute).

---

## 2. Technology Stack

- **Language**: Python 3.12
- **Package manager**: `pip` + `requirements.txt` (no `pyproject.toml`, `setup.py`, or `poetry`)
- **Test runner**: `pytest` (primary) or `python -m unittest` (fallback). Tests use stdlib `unittest` API.
- **Key runtime dependencies**:
  - `PyYAML>=6.0` -- validator config loader, strategy frontmatter
  - `exchange_calendars>=4.5` -- NYSE market-hours calendar
  - `yfinance>=0.2.40,<2.0` -- backtest price data
  - `pandas>=2.2`, `numpy>=1.26` -- backtest simulation
- **Deferred dependencies** (noted in `requirements.txt`, installed only when their milestone arrives):
  - `ib_async` -- IBKR connector (m2.5, already in use but not pinned in requirements.txt)
  - `pandas_market_calendars` -- real calendar upgrade (Phase 4)
- **Process management**: systemd (VPS, Phase 3.9+ via `k2bi-engine.service`); pm2 (Bundle 5 m2.19, deferred to post-3.9)
- **Deployment**: `rsync` via `scripts/deploy-to-vps.sh` (renamed from `deploy-to-mini.sh` in Phase 3.9 Stage 2)

---

## 3. Project Structure

```
K2Bi/
├── execution/           # Python trading engine (standalone process)
│   ├── connectors/      # Broker-neutral types + IBKR connector + mock
│   ├── engine/          # main.py (state machine) + recovery.py (reconciliation)
│   ├── journal/         # append-only JSONL writer, schema, ULID generator
│   ├── risk/            # circuit breakers, kill switch, cash-only, market calendar
│   ├── strategies/      # strategy loader, runner, types
│   └── validators/      # 5 pre-trade validators + config.yaml
├── scripts/             # Automation, deploy, review wrappers, skill implementations
│   ├── lib/             # Python modules shared by hooks and skills
│   ├── deploy-config.yml
│   ├── deploy-to-vps.sh
│   ├── review.sh
│   ├── minimax-review.sh
│   └── ...
├── tests/               # Flat test directory (34+ files, unittest + pytest hybrid)
├── .claude/skills/      # 23 invest-* skills (YAML frontmatter + Markdown body)
├── .githooks/           # pre-commit, commit-msg, post-commit (bash + python)
├── pm2/                 # ecosystem.config.js (stub daemon manifest)
├── wiki/                # Git-tracked authorial source for strategies + theses
├── proposals/           # Design docs (not deployed)
├── requirements.txt     # Runtime deps
├── CLAUDE.md            # Project identity, rules, vault taxonomy
├── DEVLOG.md            # Append-only shipped changelog (one entry per commit)
└── README.md            # Minimal 2-line project header
```

**Critical split**: `wiki/` is git-tracked (authorial truth for strategy specs,
required for `approved_commit_sha` and pre-commit hooks). The engine's runtime
read path is `K2Bi-Vault/wiki/` via Syncthing. The `.githooks/post-commit`
mirror phase atomically copies approved/retired strategy files from the repo to
the vault at commit time.

---

## 4. Build, Test, and Run Commands

### Install dependencies
```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### Run tests
```bash
# Full suite (local MacBook only; VPS runs engine, not tests)
pytest

# Or via unittest
python -m unittest discover -s tests

# Specific test file
pytest tests/test_journal.py
python -m unittest tests.test_validators
```

### Run the engine
```bash
# Diagnose mode (read-only, no orders)
python -m execution.engine.main --diagnose-approved

# Single tick (--once mode, used for smoke tests)
python -m execution.engine.main --once --account-id DUQ220152

# Continuous run (production mode)
python -m execution.engine.main --account-id DUQ220152
```

### Deploy to Hostinger VPS
```bash
# Auto-detect changed categories since last sync
bash scripts/deploy-to-vps.sh auto

# Dry run
bash scripts/deploy-to-vps.sh --dry-run

# Single category
bash scripts/deploy-to-vps.sh execution
```

---

## 5. Code Style and Conventions

- **No em dashes. Ever.**
- **No AI cliches.** No "Certainly!", "Great question!", "I'd be happy to".
- **Speak plainly.** Short sentences. Gloss tech jargon and trading terms on first
  use (e.g. "drawdown (peak-to-trough loss)", "Sharpe (risk-adjusted return)").
- **Don't narrate.** Just do the work.
- **Type hints**: Use Python 3.12 type hints. `Decimal` for money/prices.
- **Error handling**: Fail-closed for safety paths. Defense-in-depth with
  `try/except` around external API calls (IBKR, yfinance).
- **String formatting**: Use f-strings. Use `Decimal(str(value))` for float-to-Decimal
  conversion, wrapped in `try/except (InvalidOperation, TypeError, ValueError)`.
- **File naming**: snake_case for Python modules. Test files prefixed with `test_`.
- **Docstrings**: Multi-line docstrings for modules and public functions.
- **Constants**: UPPER_SNAKE_CASE for module-level constants.

### Hard rules ship as code, not prose
Anything that cannot be violated without human override belongs in a pre-commit
hook, a wrapper script, or a Python validator. Never in a markdown bullet alone.

---

## 6. Testing Strategy

The test suite is a **hybrid pytest + unittest** setup. All test classes extend
`unittest.TestCase` or `unittest.IsolatedAsyncioTestCase`. pytest is the runner.

### Test organization (flat under `tests/`)

| Domain | Key Files |
|--------|-----------|
| Engine core | `test_engine_main.py` (1914 lines, async), `test_engine_recovery.py` (2924 lines), `test_engine_once_barrier.py`, `test_engine_diagnose.py` |
| Validators | `test_validators.py` |
| Journal | `test_journal.py`, `test_journal_v2.py` |
| Risk | `test_cash_only.py`, `test_circuit_breakers.py` |
| Git hooks | `test_pre_commit_hook.py`, `test_commit_msg_hook.py`, `test_post_commit_hook.py` |
| Skills / scripts | `test_invest_ship_strategy.py`, `test_invest_ship_mirror.py`, `test_invest_backtest.py`, `test_invest_bear_case.py`, `test_invest_thesis.py`, `test_propose_limits.py`, `test_deploy_coverage.py` |
| IBKR connector | `test_ibkr_marketprice_q43.py`, `test_ibkr_qualify.py`, `test_ibkr_timeout.py` |
| Approval gates | `test_approval_backtest_gate.py`, `test_approval_bear_case_gate.py` |
| E2E | `test_bundle_3_e2e.py` (gated behind env vars) |

### E2E / live broker tests
`tests/test_bundle_3_e2e.py` requires both:
```bash
export K2BI_RUN_IBKR_TESTS=1
export K2BI_IB_ACCOUNT_ID=DUQxxxxxx
```
These tests are marked `pytest.mark.xfail(strict=False)` because infrastructure
flakes (Gateway disconnect, rate limit) are expected.

### Shared harness
`tests/_hook_harness.py` provides ephemeral git repo fixtures for hook tests.

---

## 7. Security and Safety Architecture

### Execution layer isolation (critical)
- The engine is a **separate Python process** with no import of Claude Code.
- Validators have **no bypass flag**. The engine never skips them.
- Claude can **read** validator config and **propose** changes via `/invest propose-limits`.
- Claude **CANNOT** directly edit validator config, bypass validators, or delete `.killed`.

### Kill switch
- `~/Projects/K2Bi-Vault/System/.killed` is a 0-byte sentinel file.
- Engine checks it every tick. If present, engine shuts down.
- Only the human operator creates/deletes it. There is **no `delete_killed()` function in code**.

### Cash-only invariant
- `execution.risk.cash_only.check_sell_covered()` rejects any sell where the
  seller does not own enough shares (accounting for pending sells).
- Called by both the leverage validator AND the engine pre-submit hook.
- No margin support in Phase 2.

### Circuit breakers
- Daily soft (-2%) -> halve positions.
- Daily hard (-3%) -> flatten all.
- Weekly cap (-5% over 5 sessions) -> reduce budget.
- Total drawdown (-10%) -> writes `.killed` atomically.

### Recovery mismatch override
`K2BI_ALLOW_RECOVERY_MISMATCH=1` lets the engine start despite broker/journal
mismatches. Use only for verified operational scenarios; the mismatch is still
logged.

---

## 8. Deployment Process

Deployment is **manual rsync to the VPS** (Hostinger KL, Phase 3.9+), not CI/CD.

1. `scripts/deploy-config.yml` is the single source of truth for deploy categories
   (`skills`, `execution`, `scripts`, `pm2`).
2. `scripts/deploy-to-vps.sh` reads the config and runs `rsync -av --delete`.
3. `.sync-state/last-synced-commit` tracks the last deployed SHA.
4. The preflight (`scripts/lib/deploy_config.py preflight`) blocks `/ship` if any
   top-level repo path is uncovered by `targets:` or `excludes:`.

**Excluded from deploy** (see `deploy-config.yml`):
- `.git/`, `.githooks/`, `tests/`, `wiki/`, `proposals/`, `.pending-sync/`,
  `.minimax-reviews/`, `.code-reviews/`, `.sync-state/`, `.venv/`, `__pycache__/`

`wiki/` is excluded because the vault mirror (post-commit hook) + Syncthing are
the authoritative propagation paths.

---

## 9. Git Workflow and Hooks

Git hooks are stored in `.githooks/` and must be manually installed (they are
NOT managed by a framework like pre-commit).

### pre-commit (bash, 218 lines)
- **Check A**: Strategy status must be in allowed enum.
- **Check B**: `## How This Works` section must be non-empty for proposed/approved strategies.
- **Check C**: Editing `execution/validators/config.yaml` requires a same-commit
  limits-proposal transition (`proposed -> approved`).
- **Check D**: Approved strategies are content-immutable except pure retire transitions.

### commit-msg (bash, 188 lines)
- Validates strategy status transitions against allowed matrix.
- Enforces three required trailers:
  - `Strategy-Transition: old -> new`
  - `<Action>-Strategy: strategy_<slug>`
  - `Co-Shipped-By: invest-ship`

### post-commit (python, 588 lines)
- **Phase 1**: Mirror approved/retired strategy files to vault (`K2Bi-Vault/wiki/strategies/`).
- **Phase 2**: Write retire sentinel (`.retired-<sha16>`) so the engine blocks submits.
- Fail-open by design (git ignores post-commit exit codes).
- Skipped by `K2BI_SKIP_POST_COMMIT_MIRROR=1`.

### Adversarial review
Every commit requires a review pass by a second model (Codex primary, Kimi K2.6
fallback via `scripts/minimax-review.sh`; legacy MiniMax M2.7 reachable via
`K2B_LLM_PROVIDER=minimax`). Use `/ship` or `scripts/review.sh` to invoke.
Review runs at two checkpoints: plan review before implementation, pre-commit
review before committing.

---

## 10. Vault and File Conventions

### Vault structure (`~/Projects/K2Bi-Vault/`)
```
raw/        Immutable captures (news, filings, analysis, earnings, macro, youtube, research)
wiki/       Compiled knowledge (tickers, sectors, strategies, positions, watchlist, ...)
review/     Human judgment queue (trade-ideas, strategy-approvals, alerts, contradictions)
Daily/      Trading journal (human-written)
Archive/    Expired analyses, closed positions
Assets/     images, audio, video
System/     memory/ (symlinked from Claude Code memory dir)
Templates/  Note templates per type
Home.md     Vault landing page
```

### Frontmatter (mandatory on all vault notes)
```yaml
---
tags: [<type>, <SYMBOL-if-applicable>, <theme>]
date: YYYY-MM-DD
type: ticker | sector | strategy | position | watchlist | playbook | regime | thesis | journal | tldr
origin: keith | k2bi-extract | k2bi-generate
up: "[[index]]"
confidence-last-verified: YYYY-MM-DD   # for wiki pages with claims that decay
---
```

### Raw captures (immutable, date-prefixed)
- News: `raw/news/YYYY-MM-DD_news_topic.md`
- Filings: `raw/filings/YYYY-MM-DD_<SYM>_<form>.md`
- Earnings: `raw/earnings/YYYY-MM-DD_<SYM>_Q<N>YYYY.md`
- NBLM research: `raw/research/YYYY-MM-DD_research_<SYM-or-theme>.md`

### Wiki pages
- Tickers: `wiki/tickers/<SYMBOL>.md`
- Strategies: `wiki/strategies/strategy_<name>.md` (performance frontmatter required)
- Positions: `wiki/positions/<SYMBOL>_YYYY-MM-DD.md`
- Watchlist: `wiki/watchlist/<SYMBOL>.md`

---

## 11. Key Environment Variables

| Variable | Purpose |
|----------|---------|
| `K2BI_VAULT_ROOT` | Override vault path (default: `~/Projects/K2Bi-Vault`) |
| `K2BI_SKIP_POST_COMMIT_MIRROR` | Suppress post-commit vault mirror (1 = skip) |
| `K2BI_ALLOW_RECOVERY_MISMATCH` | Allow engine startup despite recovery mismatches (1 = allow) |
| `K2BI_ALLOW_STRATEGY_STATUS_EDIT` | Override approved-content immutability check (1 = allow) |
| `K2BI_RUN_IBKR_TESTS` | Enable live IBKR E2E tests (1 = enable) |
| `K2BI_IB_ACCOUNT_ID` | IBKR paper account ID for E2E tests (e.g. `DUQ220152`) |

---

## 12. Key Architectural Boundaries

### Memory layer ownership
Every fact has exactly one home. When a rule lives in more than one place, the
second copy rots first.

| Fact type | Single home |
|-----------|-------------|
| Soft rules (tone, style) | `CLAUDE.md` |
| Hard rules (validator limits, kill-switch) | Code -- `execution/validators/`, pre-commit hook, deploy script |
| Domain conventions (naming, frontmatter) | `CLAUDE.md` File Conventions section |
| Skill how-tos | The skill's `SKILL.md` body |
| Auto-promoted learned preferences | `active_rules.md` (cap 12, LRU) |
| Raw learnings history | `self_improve_learnings.md` |
| Memory index | `MEMORY.md` |

### Single-writer hubs
`wiki/log.md` and wiki indexes have exactly one writer script each. No skill
appends directly.

### Commander / worker architecture
- **Opus (Claude Code)** = commander: daily dialogue, orchestration, file changes.
- **MiniMax M2.7** = worker: background analysis, extraction, contradiction detection.
- Pattern: Opus calls bash scripts that invoke MiniMax API, receives structured
  JSON, applies changes.

---

## 13. When to Modify This File

Update `AGENTS.md` when you change:
- Project structure (new top-level directories)
- Build / test / run commands
- Deploy categories or exclusions
- Environment variables
- Security boundaries or hard rules
- Testing strategy or test organization

Do NOT put procedural "how to do X" content here. That belongs in the skill that
does X. This file is identity, taxonomy, and agent-facing operational facts only.
