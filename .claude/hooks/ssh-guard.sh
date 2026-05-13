#!/usr/bin/env bash
# PreToolUse guard: force K2Bi VPS SSH calls through scripts/ssh-vps.sh.
#
# Exit codes: 0 allow; 2 block.

set -euo pipefail

command -v python3 >/dev/null 2>&1 || {
    echo "[ssh-guard] FAIL-CLOSED: python3 not found in PATH" >&2
    exit 2
}

input="$(cat)"
if [[ -z "$input" ]]; then
    echo "[ssh-guard] FAIL-CLOSED: empty hook stdin (payload missing)" >&2
    exit 2
fi

cmd="$(python3 -c 'import json,sys
try:
    d = json.load(sys.stdin)
    print(d.get("tool_input", {}).get("command", ""))
except Exception:
    sys.exit(3)' <<<"$input" 2>/dev/null)" || {
    echo "[ssh-guard] FAIL-CLOSED: unparseable hook input JSON" >&2
    exit 2
}

analysis="$(CMD="$cmd" python3 - <<'PY'
import os
import re
import shlex
import sys

cmd = os.environ.get("CMD", "")

if os.environ.get("K2BI_SSH_OVERRIDE"):
    print("override")
    sys.exit(0)

allowlist = (
    "scripts/ssh-vps-transport.sh",
    "scripts/ssh-vps.sh",
    "scripts/deploy-to-vps.sh",
    "scripts/gateway-query.sh",
)

try:
    lexer = shlex.shlex(cmd, posix=True, punctuation_chars=True)
    lexer.whitespace_split = True
    tokens = list(lexer)
except Exception:
    print("parse-error")
    sys.exit(0)

blocked_targets = {
    "hostinger",
    "k2bi@hostinger",
    "root@hostinger",
    "72.62.253.226",
    "k2bi@72.62.253.226",
    "root@72.62.253.226",
}
options_with_values = {
    "-b", "-c", "-D", "-E", "-F", "-I", "-i", "-J", "-L", "-l", "-m",
    "-O", "-o", "-p", "-Q", "-R", "-S", "-W", "-w",
}
separators = {";", "&&", "||", "|", "(", ")"}

def is_env_assignment(token):
    return re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", token) is not None

def is_allowlisted_command(token):
    return any(
        token == item
        or token == f"./{item}"
        or token.endswith(f"/{item}")
        for item in allowlist
    )

def command_token(segment):
    idx = 0
    while idx < len(segment) and is_env_assignment(segment[idx]):
        idx += 1
    if idx < len(segment) and segment[idx] in {"env", "command"}:
        idx += 1
        while idx < len(segment) and is_env_assignment(segment[idx]):
            idx += 1
    if idx < len(segment) and segment[idx] == "sudo":
        idx += 1
    return segment[idx] if idx < len(segment) else ""

def is_audit_command(segment):
    return command_token(segment) in {"rg", "grep", "sed", "awk"}

def segment_sets_override(segment):
    idx = 0
    while idx < len(segment) and is_env_assignment(segment[idx]):
        if segment[idx].startswith("K2BI_SSH_OVERRIDE="):
            return True
        idx += 1
    if idx < len(segment) and segment[idx] == "env":
        idx += 1
        while idx < len(segment) and is_env_assignment(segment[idx]):
            if segment[idx].startswith("K2BI_SSH_OVERRIDE="):
                return True
            idx += 1
    return False

def is_ssh_token(token):
    return token == "ssh" or token.endswith("/ssh")

def raw_ssh_target(segment):
    for idx, token in enumerate(segment):
        if not is_ssh_token(token):
            continue
        j = idx + 1
        seen_destination = False
        past_dashdash = False
        while j < len(segment):
            current = segment[j]
            if current in separators:
                break
            if current == "--":
                past_dashdash = True
                j += 1
                continue
            if not past_dashdash and current in options_with_values:
                j += 2
                continue
            if not past_dashdash and current.startswith("-"):
                j += 1
                continue
            if current in blocked_targets:
                return current
            if not seen_destination:
                seen_destination = True
            j += 1
    return ""

segment = []
segments = []
for token in tokens + [";"]:
    if token not in separators:
        segment.append(token)
        continue
    if not segment:
        continue
    segments.append(segment)
    if segment_sets_override(segment):
        print("override")
        sys.exit(0)
    target = raw_ssh_target(segment)
    if target and not is_allowlisted_command(command_token(segment)):
        print(f"raw:{target}")
        sys.exit(0)
    segment = []

if "K2BI_SSH_OVERRIDE" in cmd and not all(is_audit_command(item) for item in segments):
    print("override")
    sys.exit(0)

print("allow")
PY
)"

case "$analysis" in
    allow)
        exit 0
        ;;
    override)
        echo "[ssh-guard] BLOCKED: automation must not set K2BI_SSH_OVERRIDE." >&2
        echo "[ssh-guard] Human emergency debug sets it in an operator shell before invoking scripts/ssh-vps.sh." >&2
        exit 2
        ;;
    raw:*)
        target="${analysis#raw:}"
        echo "[ssh-guard] BLOCKED: raw SSH to ${target} is not allowed from Bash automation." >&2
        echo "[ssh-guard] Use scripts/ssh-vps.sh, or scripts/ssh-vps-transport.sh for rsync -e." >&2
        echo "[ssh-guard] Exit 78 means the circuit is open; stop and surface it to the operator." >&2
        exit 2
        ;;
    *)
        echo "[ssh-guard] FAIL-CLOSED: could not inspect Bash command safely" >&2
        exit 2
        ;;
esac
