"""Spec B section 7 engine re-enable runbook contract tests."""

from __future__ import annotations

from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
RUNBOOK = REPO_ROOT / "wiki" / "runbooks" / "spec-b-engine-reenable-checklist.md"


def test_reenable_runbook_exists_and_is_operator_only() -> None:
    text = RUNBOOK.read_text()

    assert "Operator-only" in text
    assert "Codex must not run" in text
    assert "sudo systemctl enable --now k2bi-engine" in text


def test_reenable_runbook_pins_broker_visibility_terms() -> None:
    text = RUNBOOK.read_text()

    assert "MasterClientID=99" in text
    assert "OverrideTwsMasterClientID=99" in text
    assert "grep MasterClientID /home/ibgateway/ibc/config.ini" in text


def test_reenable_runbook_requires_fresh_safety_gates() -> None:
    text = RUNBOOK.read_text()

    assert "pytest tests/ -q" in text
    assert "scripts/gateway-query.sh -f" in text
    assert "clientId=99" in text
    assert "k2bi-engine.service inactive AND disabled" in text
    assert ".killed absent" in text
    assert "wiki/log.md" in text
