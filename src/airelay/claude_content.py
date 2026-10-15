"""OpenAI chat messages as the one user message the Claude CLI receives.

The CLI answers each stream-json user line as a separate, billed model call,
so a whole conversation travels as one message: a "Human:/Assistant:"
transcript written as content blocks, with every image or document at the
place it appeared. Earlier tool calls and their results are part of that
transcript (verified live: the model answers from the results and makes new
native calls on later turns).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from airelay.errors import InvalidRequestError
from airelay.media import MAX_MEDIA_PARTS, Media, document_from_data, image_from_url


class ClaudeContentError(InvalidRequestError):
    """A message the Claude runtime cannot send; the message says why."""


@dataclass
class ClaudeConversation:
    system_parts: list[str] = field(default_factory=list)
    blocks: list[dict[str, Any]] = field(default_factory=list)
    media_count: int = 0


def media_block(media: Media) -> dict[str, Any]:
    if media.kind == "image":
        source = ({"type": "url", "url": media.url} if media.url else
                  {"type": "base64", "media_type": media.media_type, "data": media.data})
        return {"type": "image", "source": source}
    if media.media_type == "text/plain":
        block: dict[str, Any] = {"type": "document", "source": {"type": "text", "media_type": "text/plain",
                                                                 "data": media.data}}
    else:
        block = {"type": "document", "source": {"type": "base64", "media_type": media.media_type,
                                                "data": media.data}}
    if media.filename:
        block["title"] = media.filename
    return block


def _parts(content: Any, role: str) -> list[dict[str, Any] | str]:
    """A message's content as text strings and media blocks, in order."""
    if content is None:
        return []
    if isinstance(content, str):
        return [content]
    if not isinstance(content, list):
        raise ClaudeContentError("Message `content` must be a string or an array of content parts.")
    parts: list[dict[str, Any] | str] = []
    for part in content:
        if not isinstance(part, dict):
            raise ClaudeContentError("Each content part must be an object.")
        kind = part.get("type")
        if kind in {"text", "input_text", "output_text"}:
            if isinstance(part.get("text"), str):
                parts.append(part["text"])
        elif kind == "refusal" and role == "assistant":
            if isinstance(part.get("refusal"), str):
                parts.append(part["refusal"])
        elif kind in {"image_url", "input_image"} and role == "user":
            value = part.get("image_url")
            url = value.get("url") if isinstance(value, dict) else value
            parts.append(media_block(image_from_url(url)))
        elif kind in {"file", "input_file"} and role == "user":
            file = part.get("file") if kind == "file" else part
            if not isinstance(file, dict) or not file.get("file_data"):
                raise ClaudeContentError("`file` parts need `file_data` (or a `file_id` uploaded with `POST /v1/files`).")
            parts.append(media_block(document_from_data(file["file_data"], file.get("filename"))))
        elif kind in {"image_url", "input_image", "file", "input_file"}:
            raise ClaudeContentError(f"`{kind}` content is only allowed in user messages.")
        else:
            raise ClaudeContentError(f"Unsupported content part `{kind}` for Claude models.")
    return parts


def _tool_call_text(call: Any) -> str:
    if not isinstance(call, dict) or call.get("type", "function") != "function":
        raise ClaudeContentError("Claude models support only function tool calls.")
    function = call.get("function") or {}
    return f"[tool call {call.get('id')}: {function.get('name')} {function.get('arguments') or '{}'}]"


def conversation_from_chat(messages: list[dict[str, Any]]) -> ClaudeConversation:
    """Chat messages -> system text plus the transcript's content blocks."""
    conversation = ClaudeConversation()
    turns: list[tuple[str, list[dict[str, Any] | str]]] = []
    for message in messages:
        if not isinstance(message, dict):
            raise ClaudeContentError("Each message must be an object.")
        role = message.get("role")
        if role in {"system", "developer"}:
            text = "".join(p for p in _parts(message.get("content"), role) if isinstance(p, str))
            if text.strip():
                conversation.system_parts.append(text)
            continue
        if role == "tool":
            output = _parts(message.get("content"), "tool")
            if not all(isinstance(p, str) for p in output):
                raise ClaudeContentError("Tool results on Claude models must be text.")
            text = "".join(output)
            turns.append(("user", [f"[tool result {message.get('tool_call_id')}] {text}"]))
            continue
        if role not in {"user", "assistant"}:
            raise ClaudeContentError(f"Unsupported message role `{role}` for Claude models.")
        parts = _parts(message.get("content"), role)
        if role == "assistant":
            for call in message.get("tool_calls") or []:
                parts += ["\n" if parts else "", _tool_call_text(call)]
        if role == "user" and not parts:
            raise ClaudeContentError("User messages require `content`.")
        turns.append((role, parts))
    if not turns:
        raise ClaudeContentError("The Claude runtime requires at least one user or assistant turn.")
    single = len(turns) == 1 and turns[0][0] == "user"
    pieces: list[dict[str, Any] | str] = []
    for index, (role, parts) in enumerate(turns):
        if not single:
            pieces.append(("\n\n" if index else "") + ("Assistant: " if role == "assistant" else "Human: "))
        pieces.extend(parts)
    if not single:
        pieces.append("\n\nAssistant:")
    conversation.blocks = _merge(pieces)
    conversation.media_count = sum(1 for block in conversation.blocks if block["type"] != "text")
    if conversation.media_count > MAX_MEDIA_PARTS:
        raise ClaudeContentError(f"At most {MAX_MEDIA_PARTS} images and documents per request.")
    return conversation


def conversation_from_prompt(prompt: str) -> ClaudeConversation:
    return ClaudeConversation(blocks=[{"type": "text", "text": prompt}])


def _merge(pieces: list[dict[str, Any] | str]) -> list[dict[str, Any]]:
    blocks: list[dict[str, Any]] = []
    for piece in pieces:
        if isinstance(piece, str):
            if blocks and blocks[-1]["type"] == "text":
                blocks[-1]["text"] += piece
            elif piece:
                blocks.append({"type": "text", "text": piece})
        else:
            blocks.append(piece)
    return [block for block in blocks if block["type"] != "text" or block["text"].strip()]


def transcript_text(blocks: list[dict[str, Any]]) -> str:
    """The blocks' text, for logs (media is summarised, never logged raw)."""
    return "".join(block["text"] if block["type"] == "text" else f"[{block['type']}]" for block in blocks)


def user_line(blocks: list[dict[str, Any]]) -> bytes:
    return (json.dumps({"type": "user", "message": {"role": "user", "content": blocks}}, ensure_ascii=False)
            + "\n").encode("utf-8")
