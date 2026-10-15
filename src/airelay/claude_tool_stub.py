"""Tool descriptions for the Claude CLI, with nothing behind them.

AIRelays returns Claude's tool calls to the client, which runs its own tools
(as with OpenAI). The CLI learns the client's tools from this minimal MCP
server over stdio: it lists the definitions it is given and answers every
call with "not executed". It imports nothing beyond the standard library and
touches no file other than the definitions it reads.

Run by the CLI by file path, so it works with any install layout:
python -I <path>/claude_tool_stub.py <tools.json>
"""

from __future__ import annotations

import json
import sys

NOT_EXECUTED = "Not executed: AIRelays returns tool calls to the application, which runs them."


def _reply(message_id: object, result: dict) -> None:
    sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": message_id, "result": result}) + "\n")
    sys.stdout.flush()


def main() -> None:
    with open(sys.argv[1], encoding="utf-8") as handle:
        tools = json.load(handle)
    for line in sys.stdin:
        try:
            message = json.loads(line)
        except ValueError:
            continue
        method, message_id = message.get("method"), message.get("id")
        if message_id is None:
            continue  # notifications need no answer
        if method == "initialize":
            version = (message.get("params") or {}).get("protocolVersion", "2025-06-18")
            _reply(message_id, {"protocolVersion": version, "capabilities": {"tools": {}},
                                "serverInfo": {"name": "airelays", "version": "1"}})
        elif method == "tools/list":
            _reply(message_id, {"tools": tools})
        elif method == "tools/call":
            _reply(message_id, {"content": [{"type": "text", "text": NOT_EXECUTED}], "isError": True})
        else:
            _reply(message_id, {})


if __name__ == "__main__":
    main()
