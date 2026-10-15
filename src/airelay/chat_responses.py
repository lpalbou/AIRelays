"""OpenAI's Responses API on top of a chat-completions runtime (Claude).

The Claude runtime speaks chat completions. `/v1/responses` requests are
translated into that shape, and the answer back into a Responses object or
its stream of events, so both runtimes serve the same three routes.
Conversation history (Responses items) uses the same item <-> message
translation.
"""

from __future__ import annotations

import json
import time
import uuid
from typing import Any, Iterator

from airelay.transforms import UNSUPPORTED_UPSTREAM_PARAMETERS, TranslationError

# Responses fields with no meaning on Claude that would change the answer.
_REFUSED = {
    "previous_response_id": "Claude models do not keep responses; pass `conversation` to continue a conversation.",
    "background": "Background responses are not supported on Claude models.",
    "prompt": "Stored prompts are not supported on Claude models.",
}
# Mapped explicitly below.
_MAPPED = {"model", "input", "instructions", "tools", "tool_choice", "parallel_tool_calls", "text", "reasoning",
           "stream", "stream_options", "store", "conversation", "metadata", "include", "max_output_tokens"}


def _message(role: str, content: Any) -> dict[str, Any]:
    if isinstance(content, str):
        return {"role": role, "content": content}
    parts: list[dict[str, Any]] = []
    for part in content or []:
        if not isinstance(part, dict):
            raise TranslationError("Each content part must be an object.")
        kind = part.get("type")
        if kind in ("input_text", "output_text", "text"):
            parts.append({"type": "text", "text": part.get("text", "")})
        elif kind == "refusal":
            parts.append({"type": "text", "text": part.get("refusal", "")})
        elif kind == "input_image":
            image = {"url": part["image_url"]} if part.get("image_url") else {"file_id": part.get("file_id")}
            parts.append({"type": "image_url", "image_url": image})
        elif kind == "input_file":
            file = {key: part[key] for key in ("file_id", "file_data", "filename") if part.get(key)}
            parts.append({"type": "file", "file": file})
        else:
            raise TranslationError(f"Unsupported content part `{kind}` for Claude models.")
    return {"role": role, "content": parts}


def items_to_messages(items: list[Any]) -> list[dict[str, Any]]:
    """Responses input items as chat messages (consecutive function calls
    become one assistant message with several `tool_calls`)."""
    messages: list[dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict):
            raise TranslationError("Each input item must be an object.")
        kind = item.get("type", "message")
        if kind == "message":
            role = item.get("role")
            if role not in ("user", "assistant", "system", "developer"):
                raise TranslationError(f"Unsupported message role `{role}`.")
            messages.append(_message(role, item.get("content")))
        elif kind == "function_call":
            call = {"id": item.get("call_id"), "type": "function",
                    "function": {"name": item.get("name"), "arguments": item.get("arguments") or "{}"}}
            last = messages[-1] if messages else None
            if last and last["role"] == "assistant" and last.get("tool_calls") is not None and not last.get("content"):
                last["tool_calls"].append(call)
            else:
                messages.append({"role": "assistant", "content": None, "tool_calls": [call]})
        elif kind == "function_call_output":
            output = item.get("output")
            messages.append({"role": "tool", "tool_call_id": item.get("call_id"),
                             "content": output if isinstance(output, str) else json.dumps(output)})
        elif kind == "reasoning":
            continue  # Claude cannot replay another model's reasoning
        else:
            raise TranslationError(f"Input items of type `{kind}` are not supported on Claude models.")
    return messages


def messages_to_items(messages: list[Any]) -> list[dict[str, Any]]:
    """Chat messages (except system ones) as Responses items, the form
    conversations store; the inverse of `items_to_messages`."""
    items: list[dict[str, Any]] = []
    for message in messages:
        if not isinstance(message, dict):
            raise TranslationError("Each message must be an object.")
        role = message.get("role")
        if role in ("system", "developer"):
            continue
        if role == "tool":
            output = message.get("content")
            text = output if isinstance(output, str) else "".join(
                p.get("text", "") for p in output or [] if isinstance(p, dict))
            items.append({"type": "function_call_output", "call_id": message.get("tool_call_id"), "output": text})
            continue
        text_type = "output_text" if role == "assistant" else "input_text"
        content = message.get("content")
        parts: list[dict[str, Any]] = []
        for part in [{"type": "text", "text": content}] if isinstance(content, str) else content or []:
            kind = part.get("type") if isinstance(part, dict) else None
            if kind == "text" and part.get("text"):
                parts.append({"type": text_type, "text": part["text"]})
            elif kind == "refusal":
                parts.append({"type": "refusal", "refusal": part.get("refusal", "")})
            elif kind == "image_url":
                value = part.get("image_url")
                image = value if isinstance(value, dict) else {"url": value}
                parts.append({"type": "input_image", "file_id": image["file_id"]} if image.get("file_id")
                             else {"type": "input_image", "image_url": image.get("url")})
            elif kind == "file":
                parts.append({"type": "input_file", **(part.get("file") or {})})
        if parts:
            items.append({"type": "message", "role": role, "content": parts})
        for call in message.get("tool_calls") or []:
            function = call.get("function") or {}
            items.append({"type": "function_call", "call_id": call.get("id"), "name": function.get("name"),
                          "arguments": function.get("arguments") or "{}"})
    return items


def _input_items(value: Any) -> list[Any]:
    if isinstance(value, str):
        return [{"type": "message", "role": "user", "content": value}]
    if isinstance(value, list):
        return value
    raise TranslationError("`input` must be a string or an array of items.")


def request_to_chat(body: dict[str, Any]) -> tuple[dict[str, Any], list[Any], list[str]]:
    """A `/v1/responses` request as (chat body, the turn's input items,
    ignored parameter names)."""
    for name, reason in _REFUSED.items():
        if body.get(name) is not None:
            raise TranslationError(reason)
    if body.get("store"):
        raise TranslationError("Claude models do not store responses; send `store: false` or omit it.")
    items = _input_items(body.get("input"))
    chat: dict[str, Any] = {"model": body.get("model"), "messages": items_to_messages(items),
                            "stream": bool(body.get("stream")), "stream_options": {"include_usage": True}}
    if isinstance(body.get("instructions"), str) and body["instructions"].strip():
        chat["messages"].insert(0, {"role": "system", "content": body["instructions"]})
    if body.get("tools"):
        chat["tools"] = [{"type": "function", "function": {key: tool[key] for key in
                          ("name", "description", "parameters", "strict") if key in tool}}
                         if isinstance(tool, dict) and tool.get("type") == "function" else tool
                         for tool in body["tools"]]
    choice = body.get("tool_choice")
    if isinstance(choice, dict) and choice.get("type") == "function":
        chat["tool_choice"] = {"type": "function", "function": {"name": choice.get("name")}}
    elif isinstance(choice, dict) and choice.get("type") == "allowed_tools":
        chat["tool_choice"] = {"type": "allowed_tools", "allowed_tools": {"mode": choice.get("mode"), "tools": [
            {"type": "function", "function": {"name": tool.get("name")}} for tool in choice.get("tools") or []]}}
    elif choice is not None:
        chat["tool_choice"] = choice
    if "parallel_tool_calls" in body:
        chat["parallel_tool_calls"] = body["parallel_tool_calls"]
    text_format = (body.get("text") or {}).get("format") if isinstance(body.get("text"), dict) else None
    if isinstance(text_format, dict) and text_format.get("type") == "json_schema":
        chat["response_format"] = {"type": "json_schema", "json_schema": {
            key: text_format[key] for key in ("name", "schema", "strict", "description") if key in text_format}}
    elif isinstance(text_format, dict) and text_format.get("type") == "json_object":
        chat["response_format"] = {"type": "json_object"}
    if body.get("max_output_tokens") is not None:
        chat["max_completion_tokens"] = body["max_output_tokens"]
    effort = (body.get("reasoning") or {}).get("effort") if isinstance(body.get("reasoning"), dict) else None
    if effort is not None:
        chat["reasoning_effort"] = effort
    ignored = sorted(name for name, value in body.items() if value is not None and (
        name in UNSUPPORTED_UPSTREAM_PARAMETERS or name not in _MAPPED))
    if isinstance(body.get("reasoning"), dict) and body["reasoning"].get("summary") is not None:
        ignored.append("reasoning.summary")
    return chat, items, ignored


def _usage(chat_usage: Any) -> dict[str, Any] | None:
    if not isinstance(chat_usage, dict):
        return None
    prompt_details = chat_usage.get("prompt_tokens_details") or {}
    completion_details = chat_usage.get("completion_tokens_details") or {}
    return {"input_tokens": chat_usage.get("prompt_tokens", 0),
            "input_tokens_details": {"cached_tokens": prompt_details.get("cached_tokens", 0)},
            "output_tokens": chat_usage.get("completion_tokens", 0),
            "output_tokens_details": {"reasoning_tokens": completion_details.get("reasoning_tokens", 0)},
            "total_tokens": chat_usage.get("total_tokens", 0)}


def _shell(request: dict[str, Any], response_id: str, created_at: int, model: str) -> dict[str, Any]:
    """The request-derived fields every Responses object carries."""
    text_format = (request.get("text") or {}).get("format") if isinstance(request.get("text"), dict) else None
    return {"id": response_id, "object": "response", "created_at": created_at, "model": model,
            "status": "in_progress", "output": [], "error": None, "incomplete_details": None,
            "instructions": request.get("instructions"), "metadata": request.get("metadata") or {},
            "parallel_tool_calls": request.get("parallel_tool_calls", True), "temperature": None, "top_p": None,
            "tool_choice": request.get("tool_choice") or "auto",
            # Claude runs every function tool non-strict (it has no strict mode).
            "tools": [{**tool, "strict": False} if isinstance(tool, dict) and tool.get("type") == "function" else tool
                      for tool in request.get("tools") or []],
            "text": {"format": text_format or {"type": "text"}}, "store": False, "usage": None}


_INCOMPLETE = {"length": "max_output_tokens", "content_filter": "content_filter"}


def _finish(response: dict[str, Any], finish_reason: str | None, usage: Any) -> dict[str, Any]:
    reason = _INCOMPLETE.get(finish_reason or "")
    response["status"] = "incomplete" if reason else "completed"
    response["incomplete_details"] = {"reason": reason} if reason else None
    response["usage"] = _usage(usage)
    return response


def _message_item(item_id: str, text: str, status: str = "completed") -> dict[str, Any]:
    content = [{"type": "output_text", "text": text, "annotations": []}] if status == "completed" or text else []
    return {"id": item_id, "type": "message", "role": "assistant", "status": status, "content": content}


def _call_item(call: dict[str, Any], status: str = "completed") -> dict[str, Any]:
    function = call.get("function") or {}
    return {"id": f"fc_{uuid.uuid4().hex}", "type": "function_call", "status": status, "call_id": call.get("id"),
            "name": function.get("name"), "arguments": function.get("arguments") or "{}"}


def chat_to_response(chat: dict[str, Any], request: dict[str, Any]) -> dict[str, Any]:
    """A chat completion as a Responses object."""
    response = _shell(request, f"resp_{uuid.uuid4().hex}", int(chat.get("created") or time.time()),
                      chat.get("model") or request.get("model"))
    choice = (chat.get("choices") or [{}])[0]
    message = choice.get("message") or {}
    if message.get("content"):
        response["output"].append(_message_item(f"msg_{uuid.uuid4().hex}", message["content"]))
    response["output"] += [_call_item(call) for call in message.get("tool_calls") or []]
    return _finish(response, choice.get("finish_reason"), chat.get("usage"))


class ChatStreamToResponses:
    """Chat-completion stream chunks -> Responses stream events, plus the
    final Responses object (for `response.completed` and conversations)."""

    def __init__(self, request: dict[str, Any], model: str) -> None:
        self.request = request
        self.response = _shell(request, f"resp_{uuid.uuid4().hex}", int(time.time()), model)
        self._sequence = 0
        self._text: list[str] | None = None
        self._item_id = ""
        self._finish_reason: str | None = None
        self._usage: Any = None

    def _event(self, kind: str, **fields: Any) -> dict[str, Any]:
        self._sequence += 1
        return {"type": kind, "sequence_number": self._sequence - 1, **fields}

    def start(self) -> list[dict[str, Any]]:
        snapshot = json.loads(json.dumps(self.response))
        return [self._event("response.created", response=snapshot),
                self._event("response.in_progress", response=snapshot)]

    def _close_message(self) -> Iterator[dict[str, Any]]:
        if self._text is None:
            return
        text, index = "".join(self._text), len(self.response["output"])
        part = {"type": "output_text", "text": text, "annotations": []}
        yield self._event("response.output_text.done", item_id=self._item_id, output_index=index,
                          content_index=0, text=text)
        yield self._event("response.content_part.done", item_id=self._item_id, output_index=index,
                          content_index=0, part=part)
        item = _message_item(self._item_id, text)
        self.response["output"].append(item)
        yield self._event("response.output_item.done", output_index=index, item=item)
        self._text = None

    def feed(self, chunk: dict[str, Any]) -> list[dict[str, Any]]:
        events: list[dict[str, Any]] = []
        if chunk.get("usage"):
            self._usage = chunk["usage"]
        for choice in chunk.get("choices") or []:
            delta = choice.get("delta") or {}
            text = delta.get("content") or delta.get("refusal")
            if text:
                index = len(self.response["output"])
                if self._text is None:
                    self._text, self._item_id = [], f"msg_{uuid.uuid4().hex}"
                    events.append(self._event("response.output_item.added", output_index=index,
                                              item=_message_item(self._item_id, "", "in_progress")))
                    events.append(self._event("response.content_part.added", item_id=self._item_id,
                                              output_index=index, content_index=0,
                                              part={"type": "output_text", "text": "", "annotations": []}))
                self._text.append(text)
                events.append(self._event("response.output_text.delta", item_id=self._item_id, output_index=index,
                                          content_index=0, delta=text))
            for call in delta.get("tool_calls") or []:
                events += list(self._close_message())
                index, item = len(self.response["output"]), _call_item(call, "in_progress")
                events.append(self._event("response.output_item.added", output_index=index,
                                          item={**item, "arguments": ""}))
                events.append(self._event("response.function_call_arguments.delta", item_id=item["id"],
                                          output_index=index, delta=item["arguments"]))
                events.append(self._event("response.function_call_arguments.done", item_id=item["id"],
                                          output_index=index, arguments=item["arguments"]))
                item["status"] = "completed"
                self.response["output"].append(item)
                events.append(self._event("response.output_item.done", output_index=index, item=item))
            if choice.get("finish_reason"):
                self._finish_reason = choice["finish_reason"]
        return events

    def finish(self) -> list[dict[str, Any]]:
        events = list(self._close_message())
        final = _finish(self.response, self._finish_reason, self._usage)
        kind = "response.incomplete" if final["status"] == "incomplete" else "response.completed"
        return events + [self._event(kind, response=json.loads(json.dumps(final)))]

    def fail(self, error: dict[str, Any]) -> list[dict[str, Any]]:
        self.response["status"] = "failed"
        self.response["error"] = {"code": error.get("code") or "server_error", "message": error.get("message") or ""}
        return [self._event("response.failed", response=json.loads(json.dumps(self.response)))]


def sse(event: dict[str, Any]) -> bytes:
    return f"event: {event['type']}\ndata: {json.dumps(event, ensure_ascii=True)}\n\n".encode("utf-8")


def chat_chunks(raw: bytes) -> Iterator[dict[str, Any] | str]:
    """Parse our own chat SSE bytes: chunk dicts, or "[DONE]"."""
    for line in raw.decode("utf-8").splitlines():
        if line.startswith("data: "):
            data = line[6:]
            yield "[DONE]" if data == "[DONE]" else json.loads(data)
