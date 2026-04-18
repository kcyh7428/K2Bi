# cash-only invariant: no sell-side paths in this module (config I/O
# only). Enforcement owned by execution.risk.cash_only.
"""Validator config loader.

Reads execution/validators/config.yaml and returns a plain dict. The
engine is the only caller; Claude's skills never import this to edit the
config (hard rule per risk-controls.md -- config changes go through
invest-propose-limits + /invest-ship).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml


DEFAULT_CONFIG_PATH = Path(__file__).with_name("config.yaml")


def load_config(path: Path | None = None) -> dict[str, Any]:
    target = path or DEFAULT_CONFIG_PATH
    with target.open("r", encoding="utf-8") as f:
        data = yaml.safe_load(f)
    if not isinstance(data, dict):
        raise ValueError(f"validator config at {target} is not a mapping")
    return data
