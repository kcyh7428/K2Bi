"""Shared HTTP client + key loading for K2Bi MiniMax wrappers.

Always uses the global endpoint (api.minimaxi.com), never the China-only
.chat host. See ~/.claude/projects/.../memory/minimax_endpoint.md.
"""

import http.client
import json
import os
import random
import re
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

import openai

MINIMAX_API_HOST = os.environ.get("MINIMAX_API_HOST", "https://api.minimaxi.com")
CHAT_PATH = "/v1/text/chatcompletion_v2"
# 300s aligns with scripts/review.sh's 360s per-reviewer deadline (with 60s
# headroom for network + archive write). The earlier 180s ceiling caused
# every Cycle 7 cumulative review with prompt > ~80K chars to TimeoutError
# at ~181s before the server finished inference -- shadowing the wrapper's
# watchdog and turning recoverable slow calls into both_failed exits.
DEFAULT_TIMEOUT_S = 300

# Kimi K2.6 provider (Anthropic-compatible /coding endpoint). Ported from
# K2B's 2026-04-25 provider swap (commit ec2884a). MiniMax `2061` plan-tier
# errors during a lapsed subscription leave K2Bi's code-review gate with no
# fallback; the Kimi path keeps `/ship` working through any MiniMax outage.
# Default `kimi`; flip back with K2B_LLM_PROVIDER=minimax when MiniMax is
# the preferred reviewer.
K2B_LLM_PROVIDER = os.environ.get("K2B_LLM_PROVIDER", "kimi").strip() or "kimi"
KIMI_API_HOST = os.environ.get("KIMI_API_HOST", "https://api.kimi.com/coding")
KIMI_MESSAGES_PATH = "/v1/messages"
KIMI_DEFAULT_MODEL = os.environ.get("KIMI_DEFAULT_MODEL", "kimi-for-coding")
OPENAI_SEARCH_MODEL = os.environ.get("OPENAI_SEARCH_MODEL", "gpt-5-search-api")

# Retry constants (used only by the Kimi path -- K2Bi's existing MiniMax
# branch is intentionally retry-free per its operational baseline; do not
# backport retry to the MiniMax branch in this swap, that's a separate
# scope).
RETRY_HTTP_STATUSES = {502, 503, 504, 529}
MAX_RETRIES = 3
RETRY_BACKOFF_S = (10, 20, 40)
RETRY_JITTER_MAX_S = 5.0


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


def load_kimi_api_key() -> str:
    key = os.environ.get("KIMI_API_KEY", "").strip()
    if key:
        return key
    zshrc = Path.home() / ".zshrc"
    if zshrc.exists():
        match = re.search(
            r'^\s*export\s+KIMI_API_KEY\s*=\s*"([^"]+)"',
            zshrc.read_text(),
            re.MULTILINE,
        )
        if match:
            return match.group(1)
    raise MinimaxError(
        "KIMI_API_KEY not set and not found in ~/.zshrc. "
        "Export it or set K2B_LLM_PROVIDER=minimax to fall back."
    )


def load_openai_api_key() -> str:
    key = os.environ.get("OPENAI_API_KEY", "").strip()
    if key:
        return key
    zshrc = Path.home() / ".zshrc"
    if zshrc.exists():
        match = re.search(
            r'^\s*export\s+OPENAI_API_KEY\s*=\s*"([^"]+)"',
            zshrc.read_text(),
            re.MULTILINE,
        )
        if match:
            return match.group(1)
    raise MinimaxError(
        "OPENAI_API_KEY not set and not found in ~/.zshrc. "
        'Export it or add: export OPENAI_API_KEY="..."'
    )


def _extract_json(text: str) -> dict:
    """Strip optional markdown fences and locate the outermost JSON object."""
    text = text.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    start = text.find("{")
    if start == -1:
        raise ValueError("No JSON object found in response")
    depth = 0
    for i, ch in enumerate(text[start:], start):
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return json.loads(text[start : i + 1])
    raise ValueError("No complete JSON object found in response")


def openai_search_chat_completion(
    messages: list,
    *,
    max_tokens: int,
    temperature: float = 0.3,
    timeout: int = DEFAULT_TIMEOUT_S,
) -> dict:
    """Call gpt-5-search-api via the OpenAI SDK and return a MiniMax-style envelope."""
    api_key = load_openai_api_key()
    client = openai.OpenAI(api_key=api_key, timeout=timeout)
    kwargs: dict = {
        "model": OPENAI_SEARCH_MODEL,
        "messages": messages,
        "max_completion_tokens": max_tokens,
    }
    if temperature is not None:
        kwargs["temperature"] = temperature
    try:
        response = client.chat.completions.create(**kwargs)
    except Exception as exc:
        if "unsupported_value" in str(exc).lower() or "temperature" in str(exc).lower():
            # Retry without temperature
            kwargs.pop("temperature", None)
            try:
                response = client.chat.completions.create(**kwargs)
            except Exception as exc2:
                raise MinimaxError(f"OpenAI API error: {exc2}") from exc2
        else:
            raise MinimaxError(f"OpenAI API error: {exc}") from exc

    text = response.choices[0].message.content or ""
    return {
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": text},
                "finish_reason": response.choices[0].finish_reason or "stop",
            }
        ],
        "usage": {
            "prompt_tokens": response.usage.prompt_tokens if response.usage else None,
            "completion_tokens": response.usage.completion_tokens if response.usage else None,
            "total_tokens": response.usage.total_tokens if response.usage else None,
        },
        "base_resp": {"status_code": 0, "status_msg": "success"},
    }


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
    """POST a chat-completion request and return the parsed JSON response.

    Routes to Kimi K2.6 when K2B_LLM_PROVIDER=kimi (default since the K2B
    2026-04-25 swap, ported here for K2Bi's review pipeline). Falls back
    to MiniMax chatcompletion_v2 when K2B_LLM_PROVIDER=minimax.

    Kimi responses are translated into the MiniMax chatcompletion_v2
    envelope (choices[0].message.content / usage.{prompt,completion,total}_tokens
    / base_resp.status_code=0) so downstream extract_assistant_text /
    extract_token_usage callers keep working unchanged.

    Raises MinimaxError on transport, HTTP, or API-level errors.
    """
    if K2B_LLM_PROVIDER == "kimi":
        return _kimi_chat_completion(
            messages=messages,
            max_tokens=max_tokens,
            temperature=temperature,
            timeout=timeout,
        )
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


def _kimi_chat_completion(
    messages: list,
    *,
    max_tokens: int,
    temperature: float,
    timeout: int,
) -> dict:
    """Call Kimi K2.6 at /coding/v1/messages and return the response in
    MiniMax chatcompletion_v2 envelope shape.

    Translation:
      - System-role messages collapse into a single top-level `system`
        field (Anthropic Messages API convention).
      - `response_format` and `tools` / `tool_choice` are dropped (no
        Anthropic equivalents in the simple Kimi-for-coding mode).
      - Model id forced to KIMI_DEFAULT_MODEL regardless of caller's
        request -- callers may still carry MiniMax-* ids from older code.
    """
    api_key = load_kimi_api_key()

    system_parts = [m.get("content", "") for m in messages if m.get("role") == "system"]
    non_system = [m for m in messages if m.get("role") != "system"]
    payload: dict = {
        "model": KIMI_DEFAULT_MODEL,
        "max_tokens": max_tokens,
        "messages": non_system,
        "temperature": temperature,
    }
    if system_parts:
        payload["system"] = "\n\n".join(s for s in system_parts if s)

    headers = {
        "x-api-key": api_key,
        "anthropic-version": "2023-06-01",
        "Content-Type": "application/json",
    }
    req = urllib.request.Request(
        f"{KIMI_API_HOST}{KIMI_MESSAGES_PATH}",
        data=json.dumps(payload).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    last_err: Exception | None = None
    for attempt in range(MAX_RETRIES + 1):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                body = resp.read().decode("utf-8")
            break
        except urllib.error.HTTPError as e:
            detail = ""
            try:
                detail = e.read().decode("utf-8", errors="replace")
            except Exception:
                pass
            if e.code in RETRY_HTTP_STATUSES and attempt < MAX_RETRIES:
                wait_s = RETRY_BACKOFF_S[attempt] + random.uniform(0, RETRY_JITTER_MAX_S)
                print(
                    f"[kimi] HTTP {e.code} (transient) on attempt {attempt + 1}; "
                    f"retrying in {wait_s:.1f}s",
                    file=sys.stderr,
                    flush=True,
                )
                time.sleep(wait_s)
                last_err = e
                continue
            raise MinimaxError(f"HTTP {e.code} from Kimi: {detail[:500]}") from e
        except (urllib.error.URLError, http.client.HTTPException, ConnectionError, TimeoutError) as e:
            # RemoteDisconnected (HTTPException subclass), connection
            # resets, and socket timeouts all fall here. Kimi has occasional
            # mid-stream drops under long prompts -- retry generously.
            if attempt < MAX_RETRIES:
                wait_s = RETRY_BACKOFF_S[attempt] + random.uniform(0, RETRY_JITTER_MAX_S)
                print(
                    f"[kimi] network error on attempt {attempt + 1}: {type(e).__name__}: {e}; "
                    f"retrying in {wait_s:.1f}s",
                    file=sys.stderr,
                    flush=True,
                )
                time.sleep(wait_s)
                last_err = e
                continue
            raise MinimaxError(
                f"Network error contacting Kimi after {MAX_RETRIES + 1} attempts: {e}"
            ) from e
    else:
        raise MinimaxError(
            f"Kimi unreachable after {MAX_RETRIES + 1} attempts; last error: {last_err}"
        )

    try:
        parsed = json.loads(body)
    except json.JSONDecodeError as e:
        raise MinimaxError(f"Non-JSON response from Kimi: {body[:500]}") from e

    if isinstance(parsed.get("error"), dict):
        err = parsed["error"]
        raise MinimaxError(
            f"Kimi API error {err.get('type', '?')}: {err.get('message', 'unknown')}"
        )

    content_blocks = parsed.get("content") or []
    assistant_text = "".join(
        b.get("text", "") for b in content_blocks if b.get("type") == "text"
    )
    usage_raw = parsed.get("usage") or {}
    # Kimi already emits OpenAI-style prompt_tokens/completion_tokens/total_tokens
    # alongside its Anthropic-style input_tokens/output_tokens. Prefer the
    # OpenAI-compat fields; fall back to computing from Anthropic fields.
    usage = {
        "prompt_tokens": usage_raw.get("prompt_tokens", usage_raw.get("input_tokens")),
        "completion_tokens": usage_raw.get(
            "completion_tokens", usage_raw.get("output_tokens")
        ),
        "total_tokens": usage_raw.get(
            "total_tokens",
            (usage_raw.get("input_tokens") or 0) + (usage_raw.get("output_tokens") or 0),
        ),
    }

    return {
        "id": parsed.get("id"),
        "model": parsed.get("model", KIMI_DEFAULT_MODEL),
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": assistant_text},
                "finish_reason": parsed.get("stop_reason") or "stop",
            }
        ],
        "usage": usage,
        "base_resp": {"status_code": 0, "status_msg": "success"},
    }


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
