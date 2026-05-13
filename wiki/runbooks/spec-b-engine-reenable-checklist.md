---
tags: [runbook, spec-b, engine, reenable]
date: 2026-05-11
type: runbook
origin: k2bi-generate
status: ready-for-operator
up: "[[index]]"
---

# Spec B Engine Re-Enable Checklist

Operator-only runbook for re-enabling `k2bi-engine.service` after Spec B.
Codex must not run the final enable command. The engine stays off until the
operator completes every checkbox below and manually runs the final command.

## Preconditions

- [ ] Spec B sections 1 through 8 are committed.
- [ ] `pytest tests/ -q` passes on the MacBook after the final Spec B commit.
- [ ] `pytest tests/test_engine_journal_durability.py -v` passes (§8.1).
- [ ] `pytest tests/test_engine_singular_pending_rebuild.py -v` passes (§8.2).
- [ ] `pytest tests/test_runner_observability.py -v` passes (§8.3).
- [ ] `git status --short --untracked-files=all` shows only accepted local-only paths.
- [ ] No agent has created or removed `~/Projects/K2Bi-Vault/System/.killed`.

## VPS State

Run from the MacBook:

```bash
scripts/ssh-vps.sh 'systemctl is-active k2bi-engine.service; systemctl is-enabled k2bi-engine.service'
```

Expected:

```text
inactive
disabled
```

- [ ] `k2bi-engine.service inactive AND disabled` is verified.

Run:

```bash
scripts/ssh-vps.sh 'test ! -e ~/Projects/K2Bi-Vault/System/.killed && echo ".killed absent"'
```

- [ ] `.killed absent` is verified.

## Gateway Visibility Config

Run:

```bash
scripts/ssh-vps.sh 'grep MasterClientID /home/ibgateway/ibc/config.ini'
```

Expected config line:

```text
OverrideTwsMasterClientID=99
```

Terminology note: `OverrideTwsMasterClientID=99` is the IBC config key on the
VPS. MasterClientID=99 is the operator-facing IB Gateway setting it controls.
The setting is visibility-only: it allows cross-client open-order visibility
through `reqAllOpenOrders()`, but it does not grant cross-client `cancelOrder()`
authority.

- [ ] `OverrideTwsMasterClientID=99` is present.
- [ ] MasterClientID=99 visibility-only limitation is understood.

Run:

```bash
scripts/ssh-vps.sh 'systemctl is-active ib-gateway.service; systemctl show -p ActiveEnterTimestamp ib-gateway.service'
```

- [ ] `ib-gateway.service` is active and has uptime after the MasterClientID=99 config edit.

## Fresh Section 0 Broker Recheck

Create a temporary Python snippet that connects with `clientId=99`, queries G
position state, calls `reqAllOpenOrders()`, and prints visible G stop orders.
Run it only through the operator helper:

```bash
scripts/gateway-query.sh -f /path/to/spec-b-section0-recheck.py
```

The snippet must use `clientId=99`; the helper leases the operator clientId and
refuses `clientId=1`.

Expected broker state:

- [ ] G position qty is 71.
- [ ] G avgCost is within 0.5% of the baseline 31.3340875 ± $0.16/share.
      See Spec B §0 Baseline re-anchor history for the 2026-05-13 ruling.
- [ ] Exactly one G open STP SELL order exists by durable identity:
      permId 499958748, qty 71, stop 30, status PreSubmitted or Submitted.
      `parentId` is informational after daily reauth; see Spec B §0 Known
      limitations.
- [ ] SPY position qty is 2, avgCost is 707.72, and exactly one SPY open STP
      SELL order exists by durable identity: permId 1888063981, qty 2, stop
      697.13, status PreSubmitted or Submitted.
- [ ] No Spec B test orders remain open.

Write the audit line:

```bash
scripts/wiki-log-append.sh /spec-b "§0-recheck-completed" "pre-reenable: ts_utc=<UTC>; G qty=71 avgCost=<value>; exactly one G open STP SELL qty=71 @30; no spec-b test orders open; k2bi-engine inactive+disabled; .killed absent"
```

- [ ] Fresh `wiki/log.md` line exists after the broker recheck.

## Final Local Checks

- [ ] DEVLOG.md has the Spec B entry.
- [ ] `wiki/concepts/feature_k2bi-discipline-cleanup.md` has Known §5 limitations and Section 6 closure dispositions.
- [ ] `JournalDurabilityError` exists in `execution/engine/main.py`.
- [ ] `read_back_last_event()` exists in `execution/journal/writer.py`.
- [ ] Terminal-signal recovery helper exists in `execution/journal/reader.py`.
- [ ] `cycle_evaluated_skip_position_held` is registered in `execution/journal/schema.py`.
- [ ] The post-Spec-B regression test plan remains queued for a fresh K2Bi session.

## Final Manual Action

After every checkbox above is complete, the operator runs:

```bash
sudo systemctl enable --now k2bi-engine
```
