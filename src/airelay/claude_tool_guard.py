"""The Claude CLI's pre-execution check for client tools: always deny.

Defence in depth behind airelay.claude_tool_stub: the CLI asks this program
before running any tool, and the answer for every client tool is "deny", so
a call never reaches even the stub. `StructuredOutput`, the CLI's own
`--json-schema` enforcement, runs nothing and is allowed. (The CLI proceeds
if this check cannot start, which is why the stub, not this check, is the
guarantee.)

Run by the CLI as a `PreToolUse` hook, by file path with only the standard
library: python -I <path>/claude_tool_guard.py
"""

from __future__ import annotations

import json
import sys


def main() -> None:
    try:
        event = json.loads(sys.stdin.read() or "{}")
    except ValueError:
        event = {}
    if isinstance(event, dict) and event.get("tool_name") == "StructuredOutput":
        return  # no decision: the CLI's own schema enforcement proceeds
    print(json.dumps({"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": "deny",
        "permissionDecisionReason": "AIRelays returns tool calls to the application, which runs them.",
    }}))


if __name__ == "__main__":
    main()
