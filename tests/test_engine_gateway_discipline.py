"""Spec B section 6 discipline-cleanup regression tests."""

from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
VAULT_ROOT = Path.home() / "Projects" / "K2Bi-Vault"


def test_clientid_allocator_prevents_duplicate_preferred_lease(tmp_path: Path) -> None:
    """F1: ad-hoc gateway queries must coordinate clientId 90-99 leases."""
    from scripts.lib import clientid_allocator

    lease_dir = tmp_path / "leases"
    first = clientid_allocator.allocate_client_id(
        lease_dir=lease_dir,
        preferred=90,
        owner="red-test-a",
    )
    assert first.client_id == 90

    try:
        try:
            clientid_allocator.allocate_client_id(
                lease_dir=lease_dir,
                preferred=90,
                owner="red-test-b",
            )
        except clientid_allocator.ClientIdUnavailable as exc:
            assert "clientId 90" in str(exc)
        else:
            raise AssertionError("duplicate preferred clientId lease was allowed")
    finally:
        clientid_allocator.release_client_id(first)

    second = clientid_allocator.allocate_client_id(
        lease_dir=lease_dir,
        preferred=90,
        owner="red-test-c",
    )
    assert second.client_id == 90
    clientid_allocator.release_client_id(second)


def test_clientid_allocator_reclaims_stale_dead_owner_lease(tmp_path: Path) -> None:
    """F1 review hardening: dead owners must not exhaust clientId leases."""
    from scripts.lib import clientid_allocator

    lease_dir = tmp_path / "leases"
    first = clientid_allocator.allocate_client_id(
        lease_dir=lease_dir,
        preferred=91,
        owner="stale-owner",
    )
    payload = json.loads(first.path.read_text())
    payload["created_at"] = time.time() - 3600
    payload["owner_pid"] = 999999
    first.path.write_text(json.dumps(payload, sort_keys=True) + "\n")

    reclaimed = clientid_allocator.allocate_client_id(
        lease_dir=lease_dir,
        preferred=91,
        owner="reclaiming-owner",
    )

    assert reclaimed.client_id == 91
    assert reclaimed.token != first.token
    clientid_allocator.release_client_id(reclaimed)


def test_clientid_allocator_reclaims_fresh_dead_owner_lease(tmp_path: Path) -> None:
    """F1 review hardening: dead owners are stale even inside the TTL."""
    from scripts.lib import clientid_allocator

    lease_dir = tmp_path / "leases"
    first = clientid_allocator.allocate_client_id(
        lease_dir=lease_dir,
        preferred=92,
        owner="fresh-dead-owner",
    )
    payload = json.loads(first.path.read_text())
    payload["created_at"] = time.time()
    payload["owner_pid"] = 999999
    first.path.write_text(json.dumps(payload, sort_keys=True) + "\n")

    reclaimed = clientid_allocator.allocate_client_id(
        lease_dir=lease_dir,
        preferred=92,
        owner="fresh-reclaiming-owner",
    )

    assert reclaimed.client_id == 92
    assert reclaimed.token != first.token
    clientid_allocator.release_client_id(reclaimed)


def test_gateway_query_script_enforces_allocator_and_operator_context() -> None:
    """F1/F6: gateway-query.sh must allocate clientIds and block skill misuse."""
    script = (REPO_ROOT / "scripts" / "gateway-query.sh").read_text()

    assert "clientid_allocator.py" in script
    assert "assert_invoked_from_macbook" in script
    assert "K2BI_GATEWAY_QUERY_OPERATOR_OVERRIDE" in script
    assert "CLAUDE_CODE_SKILL_INVOCATION" in script
    assert "not an authentication boundary" in script
    assert "clientId=1" in script
    assert "trap" in script
    assert "release" in script
    assert "Convention (NOT enforced)" not in script


def test_review_directory_is_not_gitignored() -> None:
    """F4: limits proposals under review/ must be stageable for Check C."""
    result = subprocess.run(
        ["git", "check-ignore", "review/strategy-approvals/example.md"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 1, result.stdout + result.stderr


def test_invest_ship_skip_exception_excludes_architecture_rules() -> None:
    """F3: review skip exceptions must not cover new architecture rules."""
    skill = (REPO_ROOT / ".claude" / "skills" / "invest-ship" / "SKILL.md").read_text()

    assert "new architectural principles, conventions, or invariants" in skill


def test_liveness_learning_has_ibkr_and_syncthing_examples() -> None:
    """F7: L-2026-05-08-002 must be concrete for broker and sync migrations."""
    learning = (
        VAULT_ROOT / "System" / "memory" / "self_improve_learnings.md"
    ).read_text()

    assert "IBKR migration liveness" in learning
    assert "Syncthing liveness" in learning
