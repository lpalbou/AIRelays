"""Local conversation history, shared by both runtimes.

OpenAI's `conversation` parameter: the conversation's items are prepended
to every turn's input, and once a turn completes its own input and output
items are appended to the conversation. The upstream runs with
`store=false`, so the relay keeps the history itself, and replayed output
items carry no upstream ids (the upstream rejects ids it did not persist).
Kept output: messages, every call the client answers (items with a
`call_id`), and reasoning items with `encrypted_content`. Reasoning without
it, and server-side tool calls, cannot be replayed without their ids; their
outcome is already in the messages that follow.
"""

from __future__ import annotations

from typing import Any

from airelay.backend import SUCCESS_TERMINAL_EVENTS
from airelay.store import AppStore


def _replayable_content(part: Any) -> dict[str, Any] | None:
    if not isinstance(part, dict):
        return None
    if part.get("type") == "output_text" and isinstance(part.get("text"), str):
        return {"type": "output_text", "text": part["text"]}
    if part.get("type") == "refusal" and isinstance(part.get("refusal"), str):
        return {"type": "refusal", "refusal": part["refusal"]}
    return None


def replayable_items(output: Any) -> list[dict[str, Any]]:
    """The output items a later turn can send back as input."""
    items: list[dict[str, Any]] = []
    for item in output if isinstance(output, list) else []:
        if not isinstance(item, dict):
            continue
        kind = item.get("type")
        if kind == "message":
            content = [part for part in map(_replayable_content, item.get("content") or []) if part]
            if content:
                items.append({"type": "message", "role": item.get("role", "assistant"), "content": content})
        elif item.get("call_id") or (kind == "reasoning" and item.get("encrypted_content")):
            items.append({key: value for key, value in item.items() if key != "id"})
    return items


class ConversationTurn:
    """One request against a conversation: prepends the history on entry and
    appends the turn once the upstream reports success (completed or
    incomplete). A failed turn leaves the conversation unchanged."""

    def __init__(self, store: AppStore, conversation_id: str, turn_input: list[dict[str, Any]]) -> None:
        self._store = store
        self._conversation_id = conversation_id
        self._turn_input = turn_input
        self._output: dict[int, dict[str, Any]] = {}
        self._recorded = False

    @classmethod
    def open(cls, store: AppStore, conversation_id: str,
             turn_input: list[dict[str, Any]]) -> tuple[ConversationTurn, list[dict[str, Any]]]:
        """The turn and the conversation's stored items so far. Raises
        KeyError for an unknown conversation id."""
        history = store.conversation_items(conversation_id)
        return cls(store, conversation_id, list(turn_input)), history

    @classmethod
    def begin(cls, store: AppStore, conversation_id: str | None,
              payload: dict[str, Any]) -> ConversationTurn | None:
        """Prepends the history to a Responses payload's `input`. Raises
        KeyError for an unknown conversation id."""
        if not conversation_id:
            return None
        turn, history = cls.open(store, conversation_id, payload["input"])
        payload["input"] = history + payload["input"]
        return turn

    def observe(self, event: str, parsed: Any) -> None:
        """Feed every upstream stream event. The turn is recorded on the
        success terminal event itself, before the client sees the end of the
        stream, so a client hanging up right after it loses nothing."""
        if not isinstance(parsed, dict):
            return
        if event == "response.output_item.done":
            index, item = parsed.get("output_index"), parsed.get("item")
            if isinstance(index, int) and isinstance(item, dict):
                self._output[index] = item
        elif event in SUCCESS_TERMINAL_EVENTS and isinstance(parsed.get("response"), dict):
            self.complete(parsed["response"])

    def complete(self, response: dict[str, Any]) -> None:
        """Record the turn with its successful response."""
        if self._recorded:
            return
        self._recorded = True
        # Streamed terminal events may omit the output; the items seen in
        # `response.output_item.done` events are authoritative then.
        output = [self._output[index] for index in sorted(self._output)] or response.get("output")
        self._store.append_conversation_items(
            self._conversation_id, self._turn_input + replayable_items(output), response.get("id")
        )
