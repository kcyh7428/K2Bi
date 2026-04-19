"""Shared HTTP client + key loading for K2Bi MiniMax wrappers.

Always uses the global endpoint (api.minimaxi.com), never the China-only
.chat host. See ~/.claude/projects/.../memory/minimax_endpoint.md.
"""

import json
import os
import re
import urllib.error
import urllib.request
from pathlib import Path

MINIMAX_API_HOST = os.environ.get("MINIMAX_API_HOST", "https://api.minimaxi.com")
CHAT_PATH = "/v1/text/chatcompletion_v2"
# 300s aligns with scripts/review.sh's 360s per-reviewer deadline (with 60s
# headroom for network + archive write). The earlier 180s ceiling caused
# every Cycle 7 cumulative review with prompt > ~80K chars to TimeoutError
# at ~181s before the server finished inference -- shadowing the wrapper's
# watchdog and turning recoverable slow calls into both_failed exits.
DEFAULT_TIMEOUT_S = 300


class MinimaxError(RuntimeError):
    pass


def load_api_key() -> str:
    key = os.environ.get("MINIMAX_API_KEY", "").strip()
    if key:
        return key
    zshrc = Path.home() / ".zshrc"
    if zshrc.exists():
        match = re.search(
            r'^\s*export\s+MINIMAX_API_KEY\s*=\s*"([^"]+)"',
            zshrc.read_text(),
            re.MULTILINE,
        )
        if match:
            return match.group(1)
    raise MinimaxError(
        "MINIMAX_API_KEY not set and not found in ~/.zshrc. "
        "Export it or add: export MINIMAX_API_KEY=\"...\""
    )


def chat_completion(
    model: str,
    messages: list,
    *,
    max_tokens: int = 4096,
    temperature: float = 0.2,
    tools: list | None = None,
    tool_choice: str | None = None,
    response_format: dict | None = None,
    timeout: int = DEFAULT_TIMEOUT_S,
) -> dict:
    """POST to chatcompletion_v2 and return the parsed JSON response.

    Raises MinimaxError on transport, HTTP, or API-level errors.
    """
    api_key = load_api_key()
    payload: dict = {
        "model": model,
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": temperature,
    }
    if tools:
        payload["tools"] = tools
    if tool_choice:
        payload["tool_choice"] = tool_choice
    if response_format:
        payload["response_format"] = response_format

    req = urllib.request.Request(
        f"{MINIMAX_API_HOST}{CHAT_PATH}",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = e.read().decode("utf-8", errors="replace")
        except Exception:
            pass
        raise MinimaxError(f"HTTP {e.code} from MiniMax: {detail[:500]}") from e
    except urllib.error.URLError as e:
        raise MinimaxError(f"Network error contacting MiniMax: {e}") from e

    try:
        parsed = json.loads(body)
    except json.JSONDecodeError as e:
        raise MinimaxError(f"Non-JSON response from MiniMax: {body[:500]}") from e

    base_resp = parsed.get("base_resp") or {}
    status_code = base_resp.get("status_code")
    if status_code not in (None, 0):
        raise MinimaxError(
            f"MiniMax API error {status_code}: {base_resp.get('status_msg', 'unknown')}"
        )

    return parsed


def extract_assistant_text(response: dict) -> str:
    """Pull the assistant message content out of a chatcompletion_v2 response."""
    choices = response.get("choices") or []
    if not choices:
        return ""
    message = choices[0].get("message") or {}
    return message.get("content") or ""


def extract_token_usage(response: dict) -> dict:
    usage = response.get("usage") or {}
    return {
        "prompt_tokens": usage.get("prompt_tokens"),
        "completion_tokens": usage.get("completion_tokens"),
        "total_tokens": usage.get("total_tokens"),
    }
