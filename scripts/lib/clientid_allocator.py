"""Flock-backed clientId lease allocator for operator IB Gateway queries."""

from __future__ import annotations

import argparse
from contextlib import contextmanager
from dataclasses import dataclass
import fcntl
import json
import os
from pathlib import Path
import secrets
import sys
import time
from typing import Iterator


DEFAULT_CLIENT_IDS = tuple(range(90, 100))


class ClientIdUnavailable(RuntimeError):
    """Raised when no requested operator clientId lease can be acquired."""


@dataclass(frozen=True)
class ClientIdLease:
    """A held clientId lease."""

    client_id: int
    lease_dir: Path
    path: Path
    token: str


@contextmanager
def _locked_lease_dir(lease_dir: Path) -> Iterator[None]:
    lease_dir.mkdir(parents=True, exist_ok=True)
    lock_path = lease_dir / ".clientid.lock"
    with lock_path.open("a+") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def _lease_path(lease_dir: Path, client_id: int) -> Path:
    return lease_dir / f"clientId-{client_id}.json"


def _candidate_ids(
    preferred: int | None,
    client_ids: tuple[int, ...],
) -> tuple[int, ...]:
    if preferred is None:
        return client_ids
    if preferred not in client_ids:
        raise ValueError(
            f"clientId {preferred} is outside operator range "
            f"{client_ids[0]}-{client_ids[-1]}"
        )
    return (preferred,)


def allocate_client_id(
    *,
    lease_dir: Path,
    preferred: int | None = None,
    owner: str = "",
    client_ids: tuple[int, ...] = DEFAULT_CLIENT_IDS,
) -> ClientIdLease:
    """Acquire one operator clientId lease under an exclusive filesystem lock."""
    token = secrets.token_hex(16)
    lease_dir = Path(lease_dir)
    candidates = _candidate_ids(preferred, client_ids)

    with _locked_lease_dir(lease_dir):
        for client_id in candidates:
            path = _lease_path(lease_dir, client_id)
            if path.exists():
                continue
            payload = {
                "client_id": client_id,
                "created_at": time.time(),
                "owner": owner,
                "pid": os.getpid(),
                "token": token,
            }
            path.write_text(json.dumps(payload, sort_keys=True) + "\n")
            return ClientIdLease(
                client_id=client_id,
                lease_dir=lease_dir,
                path=path,
                token=token,
            )

    if preferred is not None:
        raise ClientIdUnavailable(f"clientId {preferred} is already leased")
    raise ClientIdUnavailable(
        f"no available clientId in {client_ids[0]}-{client_ids[-1]}"
    )


def release_client_id(
    lease: ClientIdLease | Path | str,
    *,
    token: str | None = None,
) -> None:
    """Release a clientId lease if the token matches or no token is supplied."""
    path = lease.path if isinstance(lease, ClientIdLease) else Path(lease)
    lease_dir = path.parent
    expected_token = token
    if isinstance(lease, ClientIdLease):
        expected_token = lease.token

    with _locked_lease_dir(lease_dir):
        if not path.exists():
            return
        if expected_token is not None:
            try:
                payload = json.loads(path.read_text())
            except json.JSONDecodeError as exc:
                raise ClientIdUnavailable(f"malformed lease file: {path}") from exc
            if payload.get("token") != expected_token:
                raise ClientIdUnavailable(f"lease token mismatch for {path.name}")
        path.unlink()


def _print_shell(lease: ClientIdLease) -> None:
    print(f"client_id={lease.client_id}")
    print(f"lease_path={lease.path}")
    print(f"token={lease.token}")


def _print_json(lease: ClientIdLease) -> None:
    print(
        json.dumps(
            {
                "client_id": lease.client_id,
                "lease_path": str(lease.path),
                "token": lease.token,
            },
            sort_keys=True,
        )
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)

    acquire = subparsers.add_parser("acquire")
    acquire.add_argument("--lease-dir", required=True)
    acquire.add_argument("--preferred", type=int)
    acquire.add_argument("--owner", default="")
    acquire.add_argument("--format", choices=("json", "shell"), default="json")

    release = subparsers.add_parser("release")
    release.add_argument("--lease-path", required=True)
    release.add_argument("--token", required=True)

    args = parser.parse_args(argv)
    try:
        if args.command == "acquire":
            lease = allocate_client_id(
                lease_dir=Path(args.lease_dir),
                preferred=args.preferred,
                owner=args.owner,
            )
            if args.format == "shell":
                _print_shell(lease)
            else:
                _print_json(lease)
            return 0
        if args.command == "release":
            release_client_id(Path(args.lease_path), token=args.token)
            return 0
    except (ClientIdUnavailable, ValueError) as exc:
        print(f"clientid_allocator: {exc}", file=sys.stderr)
        return 1

    parser.error("unknown command")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
