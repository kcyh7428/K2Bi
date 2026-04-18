"""Minimal ULID generator (Crockford base-32, 26 chars).

Pure stdlib so the engine doesn't depend on a ulid pip package. Not
strictly monotonic under concurrent generation within the same
millisecond -- acceptable for a decision journal where cross-writer
ordering is already supplied by the flock-protected append order.
"""

from __future__ import annotations

import os
import time

ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


def _encode(value: int, length: int) -> str:
    out = [""] * length
    for i in range(length - 1, -1, -1):
        out[i] = ALPHABET[value & 0x1F]
        value >>= 5
    return "".join(out)


def new_ulid() -> str:
    ts_ms = int(time.time() * 1000) & ((1 << 48) - 1)
    rand = int.from_bytes(os.urandom(10), "big")  # 80 bits
    return _encode(ts_ms, 10) + _encode(rand, 16)
