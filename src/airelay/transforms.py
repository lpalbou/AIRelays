from __future__ import annotations

import base64
import copy
import json
import mimetypes
from datetime import datetime, timezone
from typing import Any

from airelay.store import AppStore


TEXT_MIME_PREFIXES = (
    "text/",
    "application/json",
    "application/xml",
    "application/yaml",
    "application/x-yaml",
    "application/csv",
)
INLINE_TEXT_FILE_MAX_BYTES = 1_000_000
DEFAULT_MINIMAL_INSTRUCTIONS = "."
UNSUPPORTED_UPSTREAM_SAMPLING_PARAMETERS = (
    "temperature",
    "top_p",
    "presence_penalty",
    "frequency_penalty",
)


class TranslationError(ValueError):
    pass


def strip_unsupported_response_parameters(payload: dict[str, Any]) -> list[str]:
    ignored: list[str] = []
    for name in UNSUPPORTED_UPSTREAM_SAMPLING_PARAMETERS:
        if name in payload:
            payload.pop(name, None)
            ignored.append(name)
    return ignored


def _schema_allows_null(schema: Any) -> bool:
    if not isinstance(schema, dict):
        return False
    schema_type = schema.get("type")
    if schema_type == "null":
        return True
    if isinstance(schema_type, list) and "null" in schema_type:
        return True
    if schema.get("const", object()) is None:
        return True
    enum_values = schema.get("enum")
    if isinstance(enum_values, list) and None in enum_values:
        return True
    for key in ("anyOf", "oneOf"):
        variants = schema.get(key)
        if isinstance(variants, list) and any(_schema_allows_null(item) for item in variants):
            return True
    return False


def _make_schema_nullable(schema: Any) -> Any:
    if not isinstance(schema, dict) or _schema_allows_null(schema):
        return schema
    normalized = dict(schema)
    schema_type = normalized.get("type")
    if isinstance(schema_type, str) and schema_type != "null":
        normalized["type"] = [schema_type, "null"]
        return normalized
    if isinstance(schema_type, list):
        normalized["type"] = [*schema_type, "null"]
        return normalized
    for key in ("anyOf", "oneOf"):
        variants = normalized.get(key)
        if isinstance(variants, list):
            normalized[key] = [*variants, {"type": "null"}]
            return normalized
    return {"anyOf": [normalized, {"type": "null"}]}


def _normalize_json_schema_object_constraints(
    schema: Any,
    path: tuple[str, ...] = (),
) -> Any:
    if isinstance(schema, list):
        return [
            _normalize_json_schema_object_constraints(item, path + (str(index),))
            for index, item in enumerate(schema)
        ]
    if not isinstance(schema, dict):
        return schema

    normalized = dict(schema)
    for key in ("properties", "patternProperties", "$defs", "definitions"):
        value = normalized.get(key)
        if isinstance(value, dict):
            normalized[key] = {
                name: _normalize_json_schema_object_constraints(
                    child, path + (key, str(name))
                )
                for name, child in value.items()
            }

    for key in ("items", "contains", "additionalItems", "if", "then", "else", "not"):
        if key in normalized:
            normalized[key] = _normalize_json_schema_object_constraints(
                normalized[key], path + (key,)
            )

    for key in ("anyOf", "allOf", "oneOf", "prefixItems"):
        value = normalized.get(key)
        if isinstance(value, list):
            normalized[key] = [
                _normalize_json_schema_object_constraints(item, path + (key, str(index)))
                for index, item in enumerate(value)
            ]

    is_object_schema = normalized.get("type") == "object" or "properties" in normalized
    if is_object_schema:
        properties = normalized.get("properties")
        property_names: list[str] = []
        if properties is not None and not isinstance(properties, dict):
            location = "root" if not path else ".".join(path)
            raise TranslationError(
                f"`properties` must be an object in response schema at `{location}`."
            )
        if isinstance(properties, dict):
            property_names = list(properties.keys())
            existing_required = normalized.get("required", [])
            if "required" in normalized and not isinstance(existing_required, list):
                location = "root" if not path else ".".join(path)
                raise TranslationError(
                    f"`required` must be an array in response schema at `{location}`."
                )
            if not all(isinstance(name, str) for name in existing_required):
                location = "root" if not path else ".".join(path)
                raise TranslationError(
                    f"`required` must contain only strings in response schema at `{location}`."
                )
            existing_required_set = set(existing_required)
            for name in property_names:
                if name not in existing_required_set:
                    properties[name] = _make_schema_nullable(properties[name])
            normalized["required"] = property_names + [
                name for name in existing_required if name not in property_names
            ]
        additional_properties = normalized.get("additionalProperties")
        if "additionalProperties" not in normalized:
            normalized["additionalProperties"] = False
        elif additional_properties is not False:
            location = "root" if not path else ".".join(path)
            raise TranslationError(
                "The subscription backend requires every object schema to set "
                f"`additionalProperties` to `false`; incompatible value at `{location}`."
            )

    return normalized


def _translate_function_tool(function: Any) -> dict[str, Any]:
    if not isinstance(function, dict):
        raise TranslationError("Function tools must include a `function` object.")
    name = function.get("name")
    if not isinstance(name, str) or not name:
        raise TranslationError("Function tools require a non-empty `name`.")
    translated = {
        "type": "function",
        "name": name,
        "parameters": function.get("parameters", {"type": "object", "properties": {}}),
    }
    if "description" in function:
        translated["description"] = function.get("description")
    if "strict" in function:
        translated["strict"] = function.get("strict")
    return translated


def _translate_chat_tool(tool: Any) -> dict[str, Any]:
    if not isinstance(tool, dict):
        raise TranslationError("Each tool must be an object.")
    tool_type = tool.get("type")
    if tool_type != "function":
        raise TranslationError("Only function tools are currently supported on chat routes.")
    if "function" in tool:
        return _translate_function_tool(tool.get("function"))
    return _translate_function_tool(tool)


def _translate_chat_tools(tools: Any) -> list[dict[str, Any]]:
    if not isinstance(tools, list):
        raise TranslationError("`tools` must be an array when provided.")
    return [_translate_chat_tool(tool) for tool in tools]


def _translate_chat_tool_choice(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, str):
        if value in {"auto", "none", "required"}:
            return value
        raise TranslationError("Unsupported `tool_choice` value.")
    if not isinstance(value, dict):
        raise TranslationError("`tool_choice` must be a string or object.")

    name: Any = value.get("name")
    if not isinstance(name, str):
        function = value.get("function")
        if isinstance(function, dict):
            name = function.get("name")
    if not isinstance(name, str) or not name:
        raise TranslationError("Function tool choices require a non-empty `name`.")
    return {"type": "function", "name": name}


def _translate_legacy_function_call(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, str):
        if value in {"auto", "none"}:
            return value
        raise TranslationError("Unsupported `function_call` value.")
    if not isinstance(value, dict):
        raise TranslationError("`function_call` must be a string or object.")
    name = value.get("name")
    if not isinstance(name, str) or not name:
        raise TranslationError("`function_call.name` must be a non-empty string.")
    return {"type": "function", "name": name}


def _translate_chat_response_format(response_format: dict[str, Any]) -> dict[str, Any] | None:
    response_format_type = response_format.get("type")
    if response_format_type == "text":
        return None
    if response_format_type == "json_object":
        raise TranslationError(
            "`response_format.type=json_object` is not yet verified against the subscription backend."
        )
    if response_format_type != "json_schema":
        raise TranslationError("Unsupported `response_format` value.")
    schema_payload = response_format.get("json_schema")
    if not isinstance(schema_payload, dict):
        raise TranslationError(
            "`response_format.type=json_schema` requires a `json_schema` object."
        )
    translated = {"type": "json_schema", **schema_payload}
    if not isinstance(translated.get("name"), str) or not translated["name"]:
        raise TranslationError(
            "`response_format.json_schema.name` must be a non-empty string."
        )
    if not isinstance(translated.get("schema"), dict):
        raise TranslationError(
            "`response_format.json_schema.schema` must be an object."
        )
    translated["schema"] = _normalize_json_schema_object_constraints(translated["schema"])
    return translated


def _normalize_responses_text_format(text_format: Any) -> dict[str, Any]:
    if not isinstance(text_format, dict):
        raise TranslationError("`text.format` must be an object when provided.")
    format_type = text_format.get("type")
    if format_type == "text":
        return copy.deepcopy(text_format)
    if format_type == "json_object":
        raise TranslationError(
            "`text.format.type=json_object` is not yet verified against the subscription backend."
        )
    if format_type != "json_schema":
        raise TranslationError("Unsupported `text.format.type` value.")

    translated = copy.deepcopy(text_format)
    if not isinstance(translated.get("name"), str) or not translated["name"]:
        raise TranslationError("`text.format.name` must be a non-empty string.")
    if not isinstance(translated.get("schema"), dict):
        raise TranslationError("`text.format.schema` must be an object.")
    translated["schema"] = _normalize_json_schema_object_constraints(translated["schema"])
    return translated


def _is_text_file(content_type: str) -> bool:
    lowered = content_type.lower()
    return lowered.startswith(TEXT_MIME_PREFIXES)


def _data_url(content_type: str, body: bytes) -> str:
    encoded = base64.b64encode(body).decode("ascii")
    return f"data:{content_type};base64,{encoded}"


def _guess_file_content_type(filename: str | None, raw: bytes | None = None) -> str | None:
    if isinstance(filename, str) and filename:
        guessed, _ = mimetypes.guess_type(filename)
        if guessed:
            return guessed
    if raw:
        if raw.startswith(b"%PDF-"):
            return "application/pdf"
        if raw.startswith(b"\x89PNG\r\n\x1a\n"):
            return "image/png"
        if raw.startswith(b"\xff\xd8\xff"):
            return "image/jpeg"
        if raw.startswith(b"GIF87a") or raw.startswith(b"GIF89a"):
            return "image/gif"
        if raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":
            return "image/webp"
    return None


def _normalize_input_file_item(item: dict[str, Any]) -> dict[str, Any]:
    normalized = copy.deepcopy(item)
    if normalized.get("type") != "input_file":
        return normalized
    file_data = normalized.get("file_data")
    if not isinstance(file_data, str) or not file_data:
        return normalized
    if file_data.startswith("data:"):
        return normalized
    filename = normalized.get("filename")
    if filename is not None and not isinstance(filename, str):
        raise TranslationError("`input_file.filename` must be a string when provided.")
    content_type = _guess_file_content_type(filename)
    if content_type is None:
        raise TranslationError(
            "Raw `input_file.file_data` requires a recognizable filename extension so AIRelays "
            "can translate it for the subscription backend."
        )
    normalized["file_data"] = f"data:{content_type};base64,{file_data}"
    return normalized


def _content_to_text(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, dict):
                if item.get("type") in {"text", "input_text", "output_text"}:
                    text = item.get("text")
                    if isinstance(text, str):
                        parts.append(text)
                elif item.get("type") == "image_url":
                    parts.append("[image]")
        return "\n".join(parts)
    raise TranslationError("Unsupported message content shape.")


def _coerce_responses_input(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, str):
        return [
            {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": value}],
            }
        ]
    if isinstance(value, dict):
        return [value]
    if isinstance(value, list):
        return value
    raise TranslationError(
        "Responses requests must include `input` as a string, object, or array."
    )


def _expand_local_file(content_item: dict[str, Any], store: AppStore) -> list[dict[str, Any]]:
    file_id = content_item.get("file_id")
    if not isinstance(file_id, str) or not file_id:
        return [content_item]
    try:
        file_record, raw = store.get_file_bytes(file_id)
    except KeyError as exc:
        raise TranslationError(
            f"Unknown local file id `{file_id}`. Upload it first with `POST /v1/files`."
        ) from exc
    content_type = file_record["content_type"]
    filename = file_record["filename"]
    kind = content_item.get("type")
    if kind in {"input_image", "image_url"}:
        if not content_type.startswith("image/"):
            raise TranslationError(f"File `{filename}` is not an image.")
        return [
            {
                "type": "input_image",
                "image_url": _data_url(content_type, raw),
                "detail": content_item.get("detail", "auto"),
            }
        ]
    if kind in {"input_file", "file"}:
        return [
            {
                "type": "input_file",
                "filename": filename,
                "file_data": _data_url(content_type, raw),
            }
        ]
    if _is_text_file(content_type):
        if len(raw) > INLINE_TEXT_FILE_MAX_BYTES:
            raise TranslationError(
                f"File `{filename}` is too large to inline as text without truncation."
            )
        text = raw.decode("utf-8", errors="replace")
        return [
            {
                "type": "input_text",
                "text": f"Uploaded file `{filename}` ({content_type}):\n\n{text}",
            }
        ]
    raise TranslationError(
        f"File `{filename}` with content type `{content_type}` is not supported by the subscription backend."
    )


def _normalize_responses_input(payload: dict[str, Any], store: AppStore) -> dict[str, Any]:
    normalized = copy.deepcopy(payload)
    input_items = _coerce_responses_input(normalized.get("input"))
    normalized["input"] = input_items
    for message in input_items:
        if not isinstance(message, dict):
            raise TranslationError("Each input item must be an object.")
        content = message.get("content")
        if not isinstance(content, list):
            continue
        expanded: list[dict[str, Any]] = []
        for item in content:
            if not isinstance(item, dict):
                raise TranslationError("Each content item must be an object.")
            normalized_item = _normalize_input_file_item(item)
            expanded.extend(_expand_local_file(normalized_item, store))
        message["content"] = expanded
    return normalized


def prepare_response_request(
    body: dict[str, Any],
    store: AppStore,
    allow_tools: bool,
) -> tuple[dict[str, Any], bool, str | None]:
    payload = _normalize_responses_input(body, store)
    wants_stream = bool(payload.get("stream"))
    conversation_id = payload.pop("conversation", None)
    if isinstance(conversation_id, dict):
        conversation_id = conversation_id.get("id")
    if conversation_id is not None and not isinstance(conversation_id, str):
        raise TranslationError("`conversation` must be a string or an object with string `id`.")
    if payload.get("store") not in {None, False}:
        raise TranslationError("The subscription backend requires `store=false`.")
    if "max_output_tokens" in payload:
        raise TranslationError(
            "The verified subscription backend does not currently support `max_output_tokens` on `/v1/responses`."
        )
    if not allow_tools and payload.get("tools"):
        raise TranslationError("This route disables tools.")
    if not allow_tools and payload.get("tool_choice") not in {None, "none"}:
        raise TranslationError("This route disables tools.")
    instructions = payload.get("instructions")
    if not isinstance(instructions, str) or not instructions.strip():
        payload["instructions"] = DEFAULT_MINIMAL_INSTRUCTIONS
    text = payload.get("text")
    if text is not None:
        if not isinstance(text, dict):
            raise TranslationError("`text` must be an object when provided.")
        text_format = text.get("format")
        if text_format is not None:
            payload["text"] = copy.deepcopy(text)
            payload["text"]["format"] = _normalize_responses_text_format(text_format)
    payload["store"] = False
    if "tools" not in payload:
        payload["tools"] = []
    payload["stream"] = True
    return payload, wants_stream, conversation_id


def _responses_message(
    role: str,
    content: Any,
    refusal: str | None = None,
) -> dict[str, Any] | None:
    text_part_type = "output_text" if role == "assistant" else "input_text"
    parts: list[dict[str, Any]]
    if isinstance(content, str):
        parts = [{"type": text_part_type, "text": content}] if content else []
    elif isinstance(content, list):
        parts = []
        for item in content:
            if not isinstance(item, dict):
                raise TranslationError("Message content items must be objects.")
            kind = item.get("type")
            if kind in {"text", "input_text", "output_text"}:
                text = item.get("text", "")
                if text:
                    parts.append({"type": text_part_type, "text": text})
                continue
            if kind == "refusal":
                if role != "assistant":
                    raise TranslationError("Only assistant messages can include `refusal` content.")
                refusal_text = item.get("refusal")
                if not isinstance(refusal_text, str):
                    refusal_text = item.get("text", "")
                if refusal_text:
                    parts.append({"type": "refusal", "refusal": refusal_text})
                continue
            if kind == "image_url":
                image_url = item.get("image_url")
                if isinstance(image_url, dict):
                    file_id = image_url.get("file_id")
                    if isinstance(file_id, str):
                        parts.extend(
                            _expand_local_file(
                                {"type": "image_url", "file_id": file_id, "detail": image_url.get("detail", "auto")},
                                _STORE_PLACEHOLDER,
                            )
                        )
                    else:
                        parts.append(
                            {
                                "type": "input_image",
                                "image_url": image_url.get("url", ""),
                                "detail": image_url.get("detail", "auto"),
                            }
                        )
                else:
                    raise TranslationError("`image_url` must be an object.")
                continue
            file_id = item.get("file_id")
            if isinstance(file_id, str):
                parts.extend(_expand_local_file({"type": kind, "file_id": file_id}, _STORE_PLACEHOLDER))
                continue
            raise TranslationError(f"Unsupported chat content part `{kind}`.")
    else:
        raise TranslationError("Unsupported chat message content.")
    if isinstance(refusal, str) and refusal:
        if role != "assistant":
            raise TranslationError("Only assistant messages can include `refusal`.")
        parts.append({"type": "refusal", "refusal": refusal})
    if not parts:
        return None
    return {"type": "message", "role": role, "content": parts}


class _StorePlaceholder:
    def __init__(self) -> None:
        self._store: AppStore | None = None

    def bind(self, store: AppStore) -> None:
        self._store = store

    def get_file_bytes(self, file_id: str) -> tuple[dict[str, Any], bytes]:
        if self._store is None:
            raise RuntimeError("Store placeholder is not bound.")
        return self._store.get_file_bytes(file_id)


_STORE_PLACEHOLDER = _StorePlaceholder()


def chat_completions_to_responses(
    body: dict[str, Any],
    store: AppStore,
    allow_tools: bool,
) -> tuple[dict[str, Any], bool, str | None]:
    _STORE_PLACEHOLDER.bind(store)
    unsupported = [name for name in ("audio", "modalities", "prediction") if name in body]
    if unsupported:
        raise TranslationError(
            f"Unsupported chat.completions parameters for the subscription backend: {', '.join(unsupported)}"
        )
    if body.get("n") not in {None, 1}:
        raise TranslationError("Only `n=1` is supported.")
    if not allow_tools and (body.get("tools") or body.get("functions")):
        raise TranslationError("This route disables tools.")
    if not allow_tools and body.get("function_call") not in {None, "none"}:
        raise TranslationError("This route disables tools.")
    if body.get("store") not in {None, False}:
        raise TranslationError("The subscription backend requires `store=false`.")
    messages = body.get("messages")
    if not isinstance(messages, list) or not messages:
        raise TranslationError("Chat completions requests must include a non-empty `messages` array.")
    model = body.get("model")
    if not isinstance(model, str) or not model:
        raise TranslationError("Chat completions requests must include a `model` string.")

    input_items: list[dict[str, Any]] = []
    instruction_parts: list[str] = []
    top_level_instructions = body.get("instructions")
    if isinstance(top_level_instructions, str) and top_level_instructions.strip():
        instruction_parts.append(top_level_instructions)
    for message in messages:
        if not isinstance(message, dict):
            raise TranslationError("Each message must be an object.")
        role = message.get("role")
        if role in {"system", "developer"}:
            content_text = _content_to_text(message.get("content"))
            if content_text:
                instruction_parts.append(content_text)
            continue
        if role in {"user", "assistant"}:
            text_message = _responses_message(
                role,
                message.get("content"),
                refusal=message.get("refusal") if isinstance(message.get("refusal"), str) else None,
            )
            if text_message is not None:
                input_items.append(text_message)
        elif role == "tool":
            tool_call_id = message.get("tool_call_id")
            if not isinstance(tool_call_id, str) or not tool_call_id:
                raise TranslationError("Tool messages must include `tool_call_id`.")
            input_items.append(
                {
                    "type": "function_call_output",
                    "call_id": tool_call_id,
                    "output": _content_to_text(message.get("content")),
                }
            )
            continue
        else:
            raise TranslationError(f"Unsupported message role `{role}`.")

        tool_calls = message.get("tool_calls")
        if tool_calls:
            if role != "assistant":
                raise TranslationError("Only assistant messages can include `tool_calls`.")
            for tool_call in tool_calls:
                if not isinstance(tool_call, dict) or tool_call.get("type") != "function":
                    raise TranslationError("Only function tool calls are supported.")
                function = tool_call.get("function") or {}
                input_items.append(
                    {
                        "type": "function_call",
                        "call_id": tool_call.get("id") or f"call_{len(input_items)}",
                        "name": function.get("name", ""),
                        "arguments": function.get("arguments", "{}"),
                    }
                )

    tools = body.get("tools")
    if tools is not None:
        tools = _translate_chat_tools(tools)
    elif "functions" in body:
        functions = body.get("functions")
        if not isinstance(functions, list):
            raise TranslationError("`functions` must be an array when provided.")
        tools = [_translate_function_tool(item) for item in functions]

    tool_choice = body.get("tool_choice")
    if tool_choice is not None:
        tool_choice = _translate_chat_tool_choice(tool_choice)
    elif "function_call" in body:
        tool_choice = _translate_legacy_function_call(body.get("function_call"))
    else:
        tool_choice = "auto" if tools else "none"

    instructions = "\n\n".join(part for part in instruction_parts if part.strip())
    payload: dict[str, Any] = {
        "model": model,
        "instructions": instructions or DEFAULT_MINIMAL_INSTRUCTIONS,
        "input": input_items,
        "tools": tools or [],
        "tool_choice": tool_choice,
        "parallel_tool_calls": body.get("parallel_tool_calls", True),
        "store": False,
        "stream": True,
        "include": body.get("include", []),
    }
    if "temperature" in body:
        payload["temperature"] = body["temperature"]
    if "top_p" in body:
        payload["top_p"] = body["top_p"]
    if "presence_penalty" in body:
        payload["presence_penalty"] = body["presence_penalty"]
    if "frequency_penalty" in body:
        payload["frequency_penalty"] = body["frequency_penalty"]
    if "max_completion_tokens" in body:
        raise TranslationError(
            "The verified subscription backend does not currently support "
            "`max_completion_tokens` on `/v1/chat/completions`."
        )
    if "metadata" in body:
        payload["metadata"] = body["metadata"]
    if "service_tier" in body:
        payload["service_tier"] = body["service_tier"]
    if "user" in body:
        payload["user"] = body["user"]
    if "reasoning_effort" in body:
        payload["reasoning"] = {"effort": body["reasoning_effort"]}

    response_format = body.get("response_format")
    if isinstance(response_format, dict):
        translated_format = _translate_chat_response_format(response_format)
        if translated_format is not None:
            payload.setdefault("text", {})
            payload["text"]["format"] = translated_format

    wants_stream = bool(body.get("stream"))
    conversation_id = body.get("conversation")
    if conversation_id is not None and not isinstance(conversation_id, str):
        raise TranslationError("`conversation` must be a string when provided.")
    return payload, wants_stream, conversation_id


def completions_to_responses(body: dict[str, Any]) -> tuple[dict[str, Any], bool, str | None]:
    unsupported = [name for name in ("best_of", "echo", "logprobs", "suffix") if name in body]
    if unsupported:
        raise TranslationError(
            f"Unsupported completions parameters for the subscription backend: {', '.join(unsupported)}"
        )
    if body.get("n") not in {None, 1}:
        raise TranslationError("Only `n=1` is supported.")
    if body.get("store") not in {None, False}:
        raise TranslationError("The subscription backend requires `store=false`.")
    model = body.get("model")
    if not isinstance(model, str) or not model:
        raise TranslationError("Completions requests must include a `model` string.")

    prompt = body.get("prompt", "")
    if isinstance(prompt, list):
        if len(prompt) != 1 or not isinstance(prompt[0], str):
            raise TranslationError("Only a single string prompt is supported.")
        prompt = prompt[0]
    if not isinstance(prompt, str):
        raise TranslationError("`prompt` must be a string or a single-item string array.")

    instructions = body.get("instructions")
    payload: dict[str, Any] = {
        "model": model,
        "instructions": instructions
        if isinstance(instructions, str) and instructions.strip()
        else DEFAULT_MINIMAL_INSTRUCTIONS,
        "input": [
            {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": prompt}],
            }
        ],
        "tools": [],
        "tool_choice": "none",
        "parallel_tool_calls": False,
        "store": False,
        "stream": True,
        "include": [],
    }
    if "temperature" in body:
        payload["temperature"] = body["temperature"]
    if "top_p" in body:
        payload["top_p"] = body["top_p"]
    if "presence_penalty" in body:
        payload["presence_penalty"] = body["presence_penalty"]
    if "frequency_penalty" in body:
        payload["frequency_penalty"] = body["frequency_penalty"]
    if "max_tokens" in body:
        raise TranslationError(
            "The verified subscription backend does not currently support "
            "`max_tokens` on `/v1/completions`."
        )
    if "stop" in body:
        payload["stop"] = body["stop"]
    if "metadata" in body:
        payload["metadata"] = body["metadata"]
    if "user" in body:
        payload["user"] = body["user"]

    wants_stream = bool(body.get("stream"))
    conversation_id = body.get("conversation")
    if conversation_id is not None and not isinstance(conversation_id, str):
        raise TranslationError("`conversation` must be a string when provided.")
    return payload, wants_stream, conversation_id


def normalize_models_payload(payload: dict[str, Any]) -> dict[str, Any]:
    models = payload.get("models")
    if not isinstance(models, list):
        raise TranslationError("Upstream models payload is missing a `models` array.")
    data: list[dict[str, Any]] = []
    for item in models:
        if not isinstance(item, dict):
            continue
        slug = item.get("slug")
        if not isinstance(slug, str) or not slug:
            continue
        data.append(
            {
                "id": slug,
                "object": "model",
                "created": 0,
                "owned_by": "airelays-upstream",
            }
        )
    return {"object": "list", "data": data}


def normalize_subscription_status_payload(
    payload: dict[str, Any],
    *,
    include_raw: bool = False,
) -> dict[str, Any]:
    normalized = {
        "object": "subscription_status",
        "captured_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "source": {
            "kind": "chatgpt_subscription",
            "upstream_path": "/backend-api/wham/usage",
        },
        "account": {
            "user_id": _optional_str(payload.get("user_id")),
            "account_id": _optional_str(payload.get("account_id")),
            "email": _optional_str(payload.get("email")),
            "plan_type": _optional_str(payload.get("plan_type")),
        },
        "rate_limits": {
            "default": _normalize_rate_limit_details(payload.get("rate_limit")),
            "code_review": _normalize_rate_limit_details(payload.get("code_review_rate_limit")),
            "additional": _normalize_additional_rate_limits(payload.get("additional_rate_limits")),
        },
        "credits": _normalize_credits(payload.get("credits")),
        "spend_control": payload.get("spend_control")
        if isinstance(payload.get("spend_control"), dict)
        else None,
        "rate_limit_reached_type": _optional_str(payload.get("rate_limit_reached_type")),
        "rate_limit_reset_credits": payload.get("rate_limit_reset_credits")
        if isinstance(payload.get("rate_limit_reset_credits"), dict)
        else None,
    }
    if include_raw:
        normalized["raw"] = copy.deepcopy(payload)
    return normalized


def _normalize_additional_rate_limits(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    normalized: list[dict[str, Any]] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        normalized.append(
            {
                "limit_name": _optional_str(item.get("limit_name")),
                "metered_feature": _optional_str(item.get("metered_feature")),
                "rate_limit": _normalize_rate_limit_details(item.get("rate_limit")),
            }
        )
    return normalized


def _normalize_rate_limit_details(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    return {
        "allowed": value.get("allowed") if isinstance(value.get("allowed"), bool) else None,
        "limit_reached": value.get("limit_reached")
        if isinstance(value.get("limit_reached"), bool)
        else None,
        "primary_window": _normalize_rate_limit_window(value.get("primary_window")),
        "secondary_window": _normalize_rate_limit_window(value.get("secondary_window")),
    }


def _normalize_rate_limit_window(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    used_percent = _optional_number(value.get("used_percent"))
    window_seconds = _optional_int(value.get("limit_window_seconds"))
    reset_after_seconds = _optional_int(value.get("reset_after_seconds"))
    reset_at = _optional_int(value.get("reset_at"))
    remaining_percent = None
    if used_percent is not None:
        remaining_percent = round(max(0.0, min(100.0, 100.0 - used_percent)), 2)
    return {
        "used_percent": used_percent,
        "remaining_percent": remaining_percent,
        "window_seconds": window_seconds,
        "window_minutes": _window_minutes_from_seconds(window_seconds),
        "window_label": _window_label(window_seconds),
        "reset_after_seconds": reset_after_seconds,
        "reset_at": reset_at,
        "reset_at_iso": _timestamp_to_iso8601(reset_at),
    }


def _normalize_credits(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    return {
        "has_credits": value.get("has_credits")
        if isinstance(value.get("has_credits"), bool)
        else None,
        "unlimited": value.get("unlimited") if isinstance(value.get("unlimited"), bool) else None,
        "overage_limit_reached": value.get("overage_limit_reached")
        if isinstance(value.get("overage_limit_reached"), bool)
        else None,
        "balance": _optional_str(value.get("balance")),
        "approx_local_messages": value.get("approx_local_messages")
        if isinstance(value.get("approx_local_messages"), list)
        else None,
        "approx_cloud_messages": value.get("approx_cloud_messages")
        if isinstance(value.get("approx_cloud_messages"), list)
        else None,
    }


def _optional_str(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _optional_int(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _optional_number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _window_minutes_from_seconds(seconds: int | None) -> int | None:
    if seconds is None or seconds <= 0:
        return None
    return (seconds + 59) // 60


def _window_label(seconds: int | None) -> str | None:
    if seconds is None or seconds <= 0:
        return None
    if seconds == 604800:
        return "weekly"
    if seconds % 86400 == 0:
        return f"{seconds // 86400}d"
    if seconds % 3600 == 0:
        return f"{seconds // 3600}h"
    if seconds % 60 == 0:
        return f"{seconds // 60}m"
    return f"{seconds}s"


def _timestamp_to_iso8601(timestamp: int | None) -> str | None:
    if timestamp is None or timestamp <= 0:
        return None
    try:
        return datetime.fromtimestamp(timestamp, tz=timezone.utc).isoformat().replace(
            "+00:00", "Z"
        )
    except (OSError, OverflowError, ValueError):
        return None


def responses_to_chat_completion(response: dict[str, Any]) -> dict[str, Any]:
    text_parts: list[str] = []
    tool_calls: list[dict[str, Any]] = []
    for item in response.get("output", []):
        if item.get("type") == "message":
            for part in item.get("content", []):
                if part.get("type") == "output_text":
                    text = part.get("text")
                    if isinstance(text, str):
                        text_parts.append(text)
        if item.get("type") == "function_call":
            tool_calls.append(
                {
                    "id": item.get("call_id"),
                    "type": "function",
                    "function": {
                        "name": item.get("name"),
                        "arguments": item.get("arguments", "{}"),
                    },
                }
            )
    assistant_message: dict[str, Any] = {
        "role": "assistant",
        "content": "".join(text_parts) or None,
    }
    finish_reason = "stop"
    if tool_calls:
        assistant_message["tool_calls"] = tool_calls
        if not text_parts:
            assistant_message["content"] = None
        finish_reason = "tool_calls"
    return {
        "id": response.get("id"),
        "object": "chat.completion",
        "created": response.get("created_at"),
        "model": response.get("model"),
        "choices": [
            {
                "index": 0,
                "message": assistant_message,
                "finish_reason": finish_reason,
            }
        ],
        "usage": response.get("usage"),
    }


def responses_to_completion(response: dict[str, Any]) -> dict[str, Any]:
    text_parts: list[str] = []
    for item in response.get("output", []):
        if item.get("type") != "message":
            continue
        for part in item.get("content", []):
            if part.get("type") == "output_text":
                text = part.get("text")
                if isinstance(text, str):
                    text_parts.append(text)
    return {
        "id": response.get("id"),
        "object": "text_completion",
        "created": response.get("created_at"),
        "model": response.get("model"),
        "choices": [
            {
                "text": "".join(text_parts),
                "index": 0,
                "logprobs": None,
                "finish_reason": "stop",
            }
        ],
        "usage": response.get("usage"),
    }


def chat_completion_chunk(
    response_id: str,
    created_at: int,
    model: str,
    delta: dict[str, Any],
    finish_reason: str | None = None,
    usage: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "id": response_id,
        "object": "chat.completion.chunk",
        "created": created_at,
        "model": model,
        "choices": [
            {
                "index": 0,
                "delta": delta,
                "finish_reason": finish_reason,
            }
        ],
    }
    if usage is not None:
        payload["usage"] = usage
    return payload


def completion_chunk(
    response_id: str,
    created_at: int,
    model: str,
    text: str,
    finish_reason: str | None = None,
) -> dict[str, Any]:
    return {
        "id": response_id,
        "object": "text_completion",
        "created": created_at,
        "model": model,
        "choices": [
            {
                "text": text,
                "index": 0,
                "logprobs": None,
                "finish_reason": finish_reason,
            }
        ],
    }
