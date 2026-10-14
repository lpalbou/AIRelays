from __future__ import annotations

import base64
import copy
import json
import mimetypes
import time
from datetime import datetime, timezone
from typing import Any, Iterator

from airelay.store import AppStore

# One label vocabulary for window durations across the normalized status
# payload and the token tally (usage_windows owns the implementation).
from airelay.usage_windows import window_label as _window_label


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
# Parameters the verified upstreams cannot honor: the subscription backend
# rejects the sampling controls outright, does not honor client-set
# output-token caps, and rejects caller-supplied end-user identifiers. The
# compatibility layer strips them from the inbound request and discloses the
# omission (x-airelays-ignored-parameters, compatibility_adaptation traffic
# record) instead of failing the request.
UNSUPPORTED_UPSTREAM_PARAMETERS = (
    "temperature",
    "top_p",
    "presence_penalty",
    "frequency_penalty",
    "max_output_tokens",
    "max_completion_tokens",
    "max_tokens",
    "user",
    "safety_identifier",
    # Best-effort determinism in OpenAI's own API; the upstreams have none.
    "seed",
)

# Chat/Completions controls neither upstream can honor. Dropping them would
# silently change the output (ADR 0001), so they are rejected instead.
UNHONORED_CHAT_PARAMETERS = ("stop", "logprobs", "top_logprobs", "logit_bias")


# Every parameter the translated chat and completions routes understand:
# translated, stripped and disclosed, or rejected. Anything else is dropped
# and disclosed with the stripped parameters (strip_unknown_parameters), so
# a parameter AIRelays does not know never disappears silently. The last
# three chat entries and `conversation` are AIRelays extensions.
CHAT_PARAMETERS = frozenset({
    "model", "messages", "stream", "stream_options", "n", "store", "metadata", "service_tier",
    "tools", "tool_choice", "parallel_tool_calls", "functions", "function_call",
    "response_format", "reasoning_effort", "verbosity", "prompt_cache_key",
    "audio", "modalities", "prediction", "web_search_options", "moderation",
    *UNSUPPORTED_UPSTREAM_PARAMETERS, *UNHONORED_CHAT_PARAMETERS,
    "conversation", "instructions", "include",
})
COMPLETIONS_PARAMETERS = frozenset({
    "model", "prompt", "stream", "stream_options", "n", "store", "metadata",
    "best_of", "echo", "suffix", "reasoning_effort",
    *UNSUPPORTED_UPSTREAM_PARAMETERS, *UNHONORED_CHAT_PARAMETERS,
    "conversation", "instructions",
})


# Feature switches whose empty object is still a request: OpenAI documents
# `web_search_options: {}` as "search with defaults".
_ANY_VALUE_REQUESTS = frozenset({"web_search_options", "audio", "prediction", "moderation"})


class TranslationError(ValueError):
    pass


def strip_unknown_parameters(payload: dict[str, Any], known: frozenset[str]) -> list[str]:
    """Drop parameters outside `known`; return the non-null ones for
    disclosure (null means "not set")."""
    unknown = sorted(name for name in payload if name not in known)
    return [name for name in unknown if payload.pop(name) is not None]


def strip_unsupported_response_parameters(payload: dict[str, Any]) -> list[str]:
    ignored: list[str] = []
    for name in UNSUPPORTED_UPSTREAM_PARAMETERS:
        if name in payload and payload.pop(name) is not None:
            ignored.append(name)
    # OpenAI: "Only set this when you set `stream: true`". Without streaming
    # it has no effect, so it is dropped and disclosed rather than ignored.
    if "stream_options" in payload and not payload.get("stream"):
        if payload.pop("stream_options") is not None:
            ignored.append("stream_options")
    return ignored


def is_requested(name: str, value: Any) -> bool:
    """Whether a parameter asks for something, as opposed to restating the
    default many clients send (null, false, 0, empty, `best_of: 1`,
    `modalities: ["text"]`). Legacy completions `logprobs` is an integer
    where 0 still asks for the sampled token's logprob."""
    if name == "logprobs" and isinstance(value, int) and not isinstance(value, bool):
        return True
    if name in _ANY_VALUE_REQUESTS:
        return value is not None
    if value in (None, False, 0, "", [], {}):
        return False
    return not ((name == "best_of" and value == 1) or (name == "modalities" and value == ["text"]))


def reject_unhonored_parameters(payload: dict[str, Any]) -> None:
    named = [name for name in UNHONORED_CHAT_PARAMETERS if is_requested(name, payload.get(name))]
    if named:
        listed = ", ".join(f"`{name}`" for name in named)
        raise TranslationError(
            f"{listed} cannot be honored by AIRelays' upstreams and would change the output; "
            "remove it from the request."
        )


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
    # Chat's default is non-strict; the Responses upstream defaults to strict.
    translated["strict"] = _strict_flag(function.get("strict"), "function.strict")
    return translated


def _strict_flag(value: Any, name: str) -> bool:
    if value is None:
        return False
    if not isinstance(value, bool):
        raise TranslationError(f"`{name}` must be a boolean.")
    return value


def _translate_custom_tool_format(format_value: Any) -> dict[str, Any]:
    if not isinstance(format_value, dict):
        raise TranslationError("Custom tools require `format` to be an object when provided.")
    translated = copy.deepcopy(format_value)
    if translated.get("type") != "grammar":
        return translated
    grammar = translated.get("grammar")
    if grammar is None:
        return translated
    if not isinstance(grammar, dict):
        raise TranslationError("Custom grammar tools require `format.grammar` to be an object.")
    normalized = {"type": "grammar"}
    if "syntax" in grammar:
        normalized["syntax"] = grammar.get("syntax")
    if "definition" in grammar:
        normalized["definition"] = grammar.get("definition")
    return normalized


def _translate_custom_tool(custom: Any) -> dict[str, Any]:
    if not isinstance(custom, dict):
        raise TranslationError("Custom tools must include a `custom` object.")
    name = custom.get("name")
    if not isinstance(name, str) or not name:
        raise TranslationError("Custom tools require a non-empty `name`.")
    translated = {
        "type": "custom",
        "name": name,
    }
    if "description" in custom:
        translated["description"] = custom.get("description")
    if "format" in custom and custom.get("format") is not None:
        translated["format"] = _translate_custom_tool_format(custom.get("format"))
    return translated


def _translate_chat_tool(tool: Any) -> dict[str, Any]:
    if not isinstance(tool, dict):
        raise TranslationError("Each tool must be an object.")
    tool_type = tool.get("type")
    if tool_type == "function":
        if "function" in tool:
            return _translate_function_tool(tool.get("function"))
        return _translate_function_tool(tool)
    if tool_type == "custom":
        if "custom" in tool:
            return _translate_custom_tool(tool.get("custom"))
        # Cursor currently sends Responses-shaped custom tools on chat routes.
        return _translate_custom_tool(tool)
    raise TranslationError("Only function and custom tools are currently supported on chat routes.")


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

    tool_type = value.get("type")
    if tool_type == "allowed_tools":
        allowed = value.get("allowed_tools")
        if not isinstance(allowed, dict) or allowed.get("mode") not in {"auto", "required"}:
            raise TranslationError(
                "`tool_choice.allowed_tools` requires `mode` (`auto` or `required`) and `tools`."
            )
        tools = allowed.get("tools")
        if not isinstance(tools, list) or not tools:
            raise TranslationError("`tool_choice.allowed_tools.tools` must be a non-empty array.")
        if not all(isinstance(tool, dict) and tool.get("type") in {"function", "custom"} for tool in tools):
            raise TranslationError("`tool_choice.allowed_tools.tools` entries must be function or custom tools.")
        return {"type": "allowed_tools", "mode": allowed["mode"],
                "tools": [_translate_chat_tool_choice(tool) for tool in tools]}
    if tool_type == "custom":
        name: Any = value.get("name")
        if not isinstance(name, str):
            custom = value.get("custom")
            if isinstance(custom, dict):
                name = custom.get("name")
        if not isinstance(name, str) or not name:
            raise TranslationError("Custom tool choices require a non-empty `name`.")
        return {"type": "custom", "name": name}

    name: Any = value.get("name")
    if not isinstance(name, str):
        function = value.get("function")
        if isinstance(function, dict):
            name = function.get("name")
    if not isinstance(name, str) or not name:
        raise TranslationError("Function tool choices require a non-empty `name`.")
    return {"type": "function", "name": name}


def _translate_legacy_function_call(value: Any) -> Any:
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


def uses_legacy_functions(body: dict[str, Any]) -> bool:
    """Whether a chat request uses the deprecated `functions` interface,
    whose replies carry `message.function_call` instead of `tool_calls`."""
    return is_requested("functions", body.get("functions"))


def _translate_chat_response_format(response_format: dict[str, Any]) -> dict[str, Any] | None:
    response_format_type = response_format.get("type")
    if response_format_type == "text":
        return None
    if response_format_type == "json_object":
        # Native JSON mode (verified live); the upstream applies OpenAI's own
        # rule that the input must mention JSON.
        return {"type": "json_object"}
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
    _apply_strictness(translated)
    return translated


def _apply_strictness(schema_format: dict[str, Any]) -> None:
    """OpenAI's `strict` defaults to false, but the upstream treats a missing
    `strict` as strict mode, so it is always sent explicitly. Strict schemas
    are rewritten to OpenAI's strict-mode form; others are left as given."""
    schema_format["strict"] = _strict_flag(schema_format.get("strict"), "strict")
    if schema_format["strict"]:
        schema_format["schema"] = _normalize_json_schema_object_constraints(schema_format["schema"])


def _normalize_responses_text_format(text_format: Any) -> dict[str, Any]:
    if not isinstance(text_format, dict):
        raise TranslationError("`text.format` must be an object when provided.")
    format_type = text_format.get("type")
    if format_type == "text":
        return copy.deepcopy(text_format)
    if format_type == "json_object":
        return {"type": "json_object"}
    if format_type != "json_schema":
        raise TranslationError("Unsupported `text.format.type` value.")

    translated = copy.deepcopy(text_format)
    if not isinstance(translated.get("name"), str) or not translated["name"]:
        raise TranslationError("`text.format.name` must be a non-empty string.")
    if not isinstance(translated.get("schema"), dict):
        raise TranslationError("`text.format.schema` must be an object.")
    _apply_strictness(translated)
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
        raise UnknownLocalFileError(file_id) from exc
    _check_file_fits(content_item, file_record)
    content_type = file_record["content_type"]
    filename = file_record["filename"]
    kind = content_item.get("type")
    if kind in {"input_image", "image_url"}:
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
    text = raw.decode("utf-8", errors="replace")
    return [
        {
            "type": "input_text",
            "text": f"Uploaded file `{filename}` ({content_type}):\n\n{text}",
        }
    ]


def _check_file_fits(part: dict[str, Any], record: dict[str, Any]) -> None:
    """Whether a stored file can fill this content part; checked when a
    request or conversation references it and again when it is inlined."""
    content_type, filename = record["content_type"], record["filename"]
    kind = part.get("type")
    if kind in {"input_image", "image_url"}:
        if not content_type.startswith("image/"):
            raise TranslationError(f"File `{filename}` is not an image.")
    elif kind not in {"input_file", "file"}:
        if not _is_text_file(content_type):
            raise TranslationError(
                f"File `{filename}` with content type `{content_type}` is not supported by the subscription backend."
            )
        if record["bytes"] > INLINE_TEXT_FILE_MAX_BYTES:
            raise TranslationError(f"File `{filename}` is too large to inline as text without truncation.")


class UnknownLocalFileError(TranslationError):
    def __init__(self, file_id: str) -> None:
        super().__init__(f"Unknown local file id `{file_id}`. Upload it first with `POST /v1/files`.")
        self.file_id = file_id


def _local_file_parts(items: list[Any]) -> Iterator[dict[str, Any]]:
    for item in items:
        if isinstance(item, dict) and isinstance(item.get("content"), list):
            for part in item["content"]:
                if isinstance(part, dict) and isinstance(part.get("file_id"), str) and part["file_id"]:
                    yield part


def _check_local_files(items: list[Any], store: AppStore) -> None:
    for part in _local_file_parts(items):
        try:
            record = store.get_file(part["file_id"])
        except KeyError as exc:
            raise UnknownLocalFileError(part["file_id"]) from exc
        _check_file_fits(part, record)


def expand_local_files(payload: dict[str, Any], store: AppStore) -> None:
    """Inline local file references (`file_id` from `POST /v1/files`), which
    the upstream cannot resolve, into the final upstream `input`. Translation
    keeps the references so conversations store them, not the file bytes;
    the input items themselves are not modified."""
    expanded: list[Any] = []
    for item in payload["input"]:
        if isinstance(item, dict) and any(_local_file_parts([item])):
            parts: list[dict[str, Any]] = []
            for part in item["content"]:
                parts.extend(_expand_local_file(part, store) if isinstance(part, dict) else [part])
            item = {**item, "content": parts}
        expanded.append(item)
    payload["input"] = expanded


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
        for index, item in enumerate(content):
            if not isinstance(item, dict):
                raise TranslationError("Each content item must be an object.")
            content[index] = _normalize_input_file_item(item)
    _check_local_files(input_items, store)
    return normalized


def prepare_response_request(
    body: dict[str, Any],
    store: AppStore,
    allow_tools: bool,
) -> tuple[dict[str, Any], bool, str | None]:
    payload = _normalize_responses_input(body, store)
    strip_unsupported_response_parameters(payload)
    wants_stream = bool(payload.get("stream"))
    conversation_id = payload.pop("conversation", None)
    if isinstance(conversation_id, dict):
        conversation_id = conversation_id.get("id")
    if conversation_id is not None and not isinstance(conversation_id, str):
        raise TranslationError("`conversation` must be a string or an object with string `id`.")
    if payload.get("store") not in {None, False}:
        raise TranslationError("The subscription backend requires `store=false`.")
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
    if content is None:
        # Assistant messages carrying only `tool_calls` or a `refusal` have
        # null content (OpenAI sends it, and so does AIRelays).
        if role != "assistant":
            raise TranslationError(f"`{role}` messages require `content`.")
        parts = []
    elif isinstance(content, str):
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
                        parts.append({"type": "input_image", "file_id": file_id,
                                      "detail": image_url.get("detail", "auto")})
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
            if kind == "file" and isinstance(item.get("file"), dict):
                parts.extend(_chat_file_part(item["file"]))
                continue
            file_id = item.get("file_id")
            if isinstance(file_id, str):
                parts.append({"type": kind, "file_id": file_id})
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


def _chat_file_part(file: dict[str, Any]) -> list[dict[str, Any]]:
    """OpenAI's chat file part, `{"type": "file", "file": {...}}`: a local
    `file_id`, or inline `file_data` with its `filename`."""
    file_id = file.get("file_id")
    if isinstance(file_id, str) and file_id:
        return [{"type": "input_file", "file_id": file_id}]
    if isinstance(file.get("file_data"), str) and file["file_data"]:
        item = {"type": "input_file", "file_data": file["file_data"]}
        if file.get("filename") is not None:
            item["filename"] = file["filename"]
        return [_normalize_input_file_item(item)]
    raise TranslationError("`file` content parts require `file.file_id` or `file.file_data`.")


def _translate_chat_tool_call(tool_call: Any, fallback_index: int) -> tuple[dict[str, Any], str]:
    if not isinstance(tool_call, dict):
        raise TranslationError("Each tool call must be an object.")
    tool_type = tool_call.get("type")
    call_id = tool_call.get("id") or f"call_{fallback_index}"
    if tool_type == "function":
        function = tool_call.get("function") if isinstance(tool_call.get("function"), dict) else tool_call
        return (
            {
                "type": "function_call",
                "call_id": call_id,
                "name": function.get("name", ""),
                "arguments": function.get("arguments", "{}"),
            },
            "function",
        )
    if tool_type == "custom":
        custom = tool_call.get("custom") if isinstance(tool_call.get("custom"), dict) else tool_call
        return (
            {
                "type": "custom_tool_call",
                "call_id": call_id,
                "name": custom.get("name", ""),
                "input": custom.get("input", ""),
            },
            "custom",
        )
    raise TranslationError("Only function and custom tool calls are supported.")


def response_item_to_chat_tool_call(item: Any) -> dict[str, Any] | None:
    if not isinstance(item, dict):
        return None
    if item.get("type") == "function_call":
        return {
            "id": item.get("call_id"),
            "type": "function",
            "function": {
                "name": item.get("name"),
                "arguments": item.get("arguments", "{}"),
            },
        }
    if item.get("type") == "custom_tool_call":
        return {
            "id": item.get("call_id"),
            "type": "custom",
            "custom": {
                "name": item.get("name"),
                "input": item.get("input", ""),
            },
        }
    return None


def chat_completions_to_responses(
    body: dict[str, Any],
    store: AppStore,
    allow_tools: bool,
) -> tuple[dict[str, Any], bool, str | None]:
    unsupported = [name for name in ("audio", "modalities", "prediction", "web_search_options", "moderation")
                   if is_requested(name, body.get(name))]
    if unsupported:
        raise TranslationError(
            f"Unsupported chat.completions parameters for the subscription backend: {', '.join(unsupported)}"
        )
    legacy_functions = uses_legacy_functions(body)
    if legacy_functions and body.get("tools"):
        raise TranslationError("Use either `tools` or the deprecated `functions`, not both.")
    if legacy_functions and body.get("conversation") is not None:
        raise TranslationError("The deprecated `functions` cannot be combined with `conversation`; use `tools`.")
    if body.get("n") not in {None, 1}:
        raise TranslationError("Only `n=1` is supported.")
    if not allow_tools and (body.get("tools") or body.get("functions")):
        raise TranslationError("This route disables tools.")
    tool_choice_value = body.get("tool_choice")
    if not allow_tools and tool_choice_value is not None and tool_choice_value != "none":
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

    conversation_id = body.get("conversation")
    if conversation_id is not None and not isinstance(conversation_id, str):
        raise TranslationError("`conversation` must be a string when provided.")

    input_items: list[dict[str, Any]] = []
    instruction_parts: list[str] = []
    # Tool results may answer calls made earlier in the conversation.
    tool_call_types = _conversation_tool_calls(store, conversation_id)
    # Deprecated interface: `function` messages answer calls by name.
    legacy_calls: dict[str, str] = {}
    response_format = body.get("response_format")
    json_mode = isinstance(response_format, dict) and response_format.get("type") == "json_object"
    top_level_instructions = body.get("instructions")
    if isinstance(top_level_instructions, str) and top_level_instructions.strip():
        instruction_parts.append(top_level_instructions)
    for message in messages:
        if not isinstance(message, dict):
            raise TranslationError("Each message must be an object.")
        role = message.get("role")
        if role in {"system", "developer"}:
            content_text = _content_to_text(message.get("content"))
            if content_text and json_mode:
                # OpenAI's JSON mode needs "json" in the messages, and the
                # upstream does not count `instructions`; as developer input
                # the system prompt counts, as it does on OpenAI's chat API.
                input_items.append({"type": "message", "role": "developer",
                                    "content": [{"type": "input_text", "text": content_text}]})
            elif content_text:
                instruction_parts.append(content_text)
            continue
        if role == "function":
            name = message.get("name")
            call_id = legacy_calls.pop(name, None) if isinstance(name, str) else None
            if call_id is None:
                raise TranslationError(
                    "`function` messages must answer a preceding assistant `function_call` with the same `name`."
                )
            input_items.append(
                {"type": "function_call_output", "call_id": call_id, "output": _content_to_text(message.get("content"))}
            )
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
            if tool_call_id not in tool_call_types:
                raise TranslationError(
                    "Tool messages must reference a preceding assistant tool call in the same request."
                )
            output_type = "custom_tool_call_output" if tool_call_types.get(tool_call_id) == "custom" else "function_call_output"
            input_items.append(
                {
                    "type": output_type,
                    "call_id": tool_call_id,
                    "output": _content_to_text(message.get("content")),
                }
            )
            continue
        else:
            raise TranslationError(f"Unsupported message role `{role}`.")

        function_call = message.get("function_call")
        if function_call is not None:
            if role != "assistant" or not isinstance(function_call, dict) or not function_call.get("name"):
                raise TranslationError("`function_call` must be an assistant message's `{name, arguments}`.")
            call_id = f"call_fn_{len(input_items)}"
            input_items.append({"type": "function_call", "call_id": call_id, "name": function_call["name"],
                                "arguments": function_call.get("arguments", "{}")})
            legacy_calls[function_call["name"]] = call_id

        tool_calls = message.get("tool_calls")
        if tool_calls:
            if role != "assistant":
                raise TranslationError("Only assistant messages can include `tool_calls`.")
            for tool_call in tool_calls:
                translated_call, call_type = _translate_chat_tool_call(tool_call, len(input_items))
                input_items.append(translated_call)
                call_id = translated_call.get("call_id")
                if isinstance(call_id, str) and call_id:
                    tool_call_types[call_id] = call_type

    tools = body.get("tools")
    if tools is not None:
        tools = _translate_chat_tools(tools)
    elif legacy_functions:
        functions = body.get("functions")
        if not isinstance(functions, list):
            raise TranslationError("`functions` must be an array when provided.")
        tools = [_translate_function_tool(item) for item in functions]

    tool_choice = body.get("tool_choice")
    if tool_choice is not None:
        tool_choice = _translate_chat_tool_choice(tool_choice)
    elif body.get("function_call") is not None:
        tool_choice = _translate_legacy_function_call(body["function_call"])
    else:
        tool_choice = "auto" if tools else "none"

    _check_local_files(input_items, store)
    instructions = "\n\n".join(part for part in instruction_parts if part.strip())
    payload: dict[str, Any] = {
        "model": model,
        "instructions": instructions or DEFAULT_MINIMAL_INSTRUCTIONS,
        "input": input_items,
        "tools": tools or [],
        "tool_choice": tool_choice,
        # The deprecated interface returns a single `function_call`.
        "parallel_tool_calls": False if legacy_functions else body.get("parallel_tool_calls", True),
        "store": False,
        "stream": True,
        "include": body.get("include", []),
    }
    if "metadata" in body:
        payload["metadata"] = body["metadata"]
    if "service_tier" in body:
        payload["service_tier"] = body["service_tier"]
    # An explicit JSON null means "not set" (OpenAI semantics), so key
    # presence alone must not forward `reasoning: {"effort": null}` upstream.
    if body.get("reasoning_effort") is not None:
        payload["reasoning"] = {"effort": body["reasoning_effort"]}
    if body.get("prompt_cache_key") is not None:
        payload["prompt_cache_key"] = body["prompt_cache_key"]
    if body.get("verbosity") is not None:
        payload["text"] = {"verbosity": body["verbosity"]}

    if isinstance(response_format, dict):
        translated_format = _translate_chat_response_format(response_format)
        if translated_format is not None:
            payload.setdefault("text", {})
            payload["text"]["format"] = translated_format

    wants_stream = bool(body.get("stream"))
    return payload, wants_stream, conversation_id


def _conversation_tool_calls(store: AppStore, conversation_id: str | None) -> dict[str, str]:
    """call_id -> tool kind for the calls stored in a conversation."""
    if not conversation_id:
        return {}
    try:
        items = store.conversation_items(conversation_id)
    except KeyError:
        return {}  # the route answers 404 for the unknown conversation
    kinds = {"function_call": "function", "custom_tool_call": "custom"}
    return {item["call_id"]: kinds[item["type"]] for item in items
            if isinstance(item, dict) and item.get("type") in kinds and isinstance(item.get("call_id"), str)}


def normalize_conversation_items(items: Any, store: AppStore) -> list[dict[str, Any]]:
    """Initial conversation items, normalized like a Responses `input`."""
    if items is None:
        return []
    if not isinstance(items, list):
        raise TranslationError("`items` must be an array when provided.")
    return _normalize_responses_input({"input": items}, store)["input"]


def chat_route_request_to_responses(
    body: dict[str, Any],
    store: AppStore,
    allow_tools: bool,
) -> tuple[dict[str, Any], bool, str | None]:
    if is_responses_shape(body):
        return prepare_response_request(body, store, allow_tools)
    return chat_completions_to_responses(body, store, allow_tools)


def is_responses_shape(body: dict[str, Any]) -> bool:
    """Whether a chat-route body is Responses-shaped (Cursor sends these).
    A body mixing both shapes is ambiguous and rejected."""
    if "messages" in body and "input" in body:
        raise TranslationError(
            "Chat route requests must use either chat.completions `messages` or Responses `input`, not both."
        )
    return "input" in body


def completions_to_responses(body: dict[str, Any]) -> tuple[dict[str, Any], bool, str | None]:
    unsupported = [name for name in ("best_of", "echo", "logprobs", "suffix") if is_requested(name, body.get(name))]
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
    if "metadata" in body:
        payload["metadata"] = body["metadata"]
    # The completions route serves the same reasoning models as chat; the
    # control must not be silently dropped here while chat honors it (the
    # Claude runtime honors it on this route too).
    if body.get("reasoning_effort") is not None:
        payload["reasoning"] = {"effort": body["reasoning_effort"]}

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
    captured_at: str | None = None,
) -> dict[str, Any]:
    normalized = {
        "object": "subscription_status",
        "captured_at": captured_at or datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
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
        "model_usage": copy.deepcopy(payload.get("model_usage"))
        if isinstance(payload.get("model_usage"), dict) else None,
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


def _timestamp_to_iso8601(timestamp: int | None) -> str | None:
    if timestamp is None or timestamp <= 0:
        return None
    try:
        return datetime.fromtimestamp(timestamp, tz=timezone.utc).isoformat().replace(
            "+00:00", "Z"
        )
    except (OSError, OverflowError, ValueError):
        return None


def responses_to_chat_completion(response: dict[str, Any], *, legacy_functions: bool = False) -> dict[str, Any]:
    text_parts: list[str] = []
    refusal_parts: list[str] = []
    tool_calls: list[dict[str, Any]] = []
    for item in response.get("output", []):
        if item.get("type") == "message":
            for part in item.get("content", []):
                if part.get("type") == "output_text":
                    text = part.get("text")
                    if isinstance(text, str):
                        text_parts.append(text)
                elif part.get("type") == "refusal":
                    refusal = part.get("refusal")
                    if isinstance(refusal, str):
                        refusal_parts.append(refusal)
        tool_call = response_item_to_chat_tool_call(item)
        if tool_call is not None:
            tool_calls.append(tool_call)
    # `refusal` and `logprobs` are required (nullable) by the spec.
    assistant_message: dict[str, Any] = {
        "role": "assistant",
        "content": "".join(text_parts) or None,
        "refusal": "".join(refusal_parts) or None,
    }
    finish_reason = "stop"
    legacy_call = next((call for call in tool_calls if call["type"] == "function"), None)
    if legacy_functions and legacy_call is not None:
        # The deprecated interface carries one call as `message.function_call`.
        assistant_message["function_call"] = dict(legacy_call["function"])
        finish_reason = "function_call"
    elif tool_calls:
        assistant_message["tool_calls"] = tool_calls
        if not text_parts:
            assistant_message["content"] = None
        finish_reason = "tool_calls"
    if response.get("status") == "incomplete":
        finish_reason = incomplete_finish_reason(response)
    return _with_usage({
        "id": response.get("id"),
        "object": "chat.completion",
        "created": created_timestamp(response.get("created_at"), int(time.time())),
        "model": response.get("model"),
        "choices": [
            {
                "index": 0,
                "message": assistant_message,
                "logprobs": None,
                "finish_reason": finish_reason,
            }
        ],
    }, completion_usage_from_responses(response.get("usage")))


def incomplete_finish_reason(response: dict[str, Any]) -> str:
    """finish_reason for a Responses result that stopped early."""
    details = response.get("incomplete_details")
    reason = details.get("reason") if isinstance(details, dict) else None
    return "content_filter" if reason == "content_filter" else "length"


def created_timestamp(value: Any, default: int) -> int:
    """`created` is an integer in the chat/completions schemas; the
    Responses `created_at` is a number."""
    return int(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else default


def responses_to_completion(response: dict[str, Any]) -> dict[str, Any]:
    text_parts: list[str] = []
    refused = False
    for item in response.get("output", []):
        if item.get("type") != "message":
            continue
        for part in item.get("content", []):
            if part.get("type") == "output_text":
                text = part.get("text")
                if isinstance(text, str):
                    text_parts.append(text)
            elif part.get("type") == "refusal":
                refused = True
    if refused:
        finish_reason = "content_filter"
    elif response.get("status") == "incomplete":
        finish_reason = incomplete_finish_reason(response)
    else:
        finish_reason = "stop"
    return _with_usage({
        "id": response.get("id"),
        "object": "text_completion",
        "created": created_timestamp(response.get("created_at"), int(time.time())),
        "model": response.get("model"),
        "choices": [
            {
                "text": "".join(text_parts),
                "index": 0,
                "logprobs": None,
                "finish_reason": finish_reason,
            }
        ],
    }, completion_usage_from_responses(response.get("usage")))


def chat_completion_chunk(
    response_id: str,
    created_at: int,
    model: str,
    delta: dict[str, Any],
    finish_reason: str | None = None,
    *,
    include_usage: bool = False,
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
    if include_usage:
        payload["usage"] = None
    return payload


def completion_chunk(
    response_id: str,
    created_at: int,
    model: str,
    text: str,
    finish_reason: str | None = None,
    *,
    include_usage: bool = False,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
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
    if include_usage:
        payload["usage"] = None
    return payload


def stream_include_usage(body: dict[str, Any]) -> bool:
    """Whether a streaming request asked for the final usage chunk."""
    options = body.get("stream_options")
    return isinstance(options, dict) and options.get("include_usage") is True


def usage_chunk(object_: str, response_id: str, created_at: int, model: str,
                usage: dict[str, Any]) -> dict[str, Any]:
    """The extra last chunk `stream_options.include_usage` asks for: after the
    finish_reason chunk, before `[DONE]`, with an empty `choices` list."""
    return {"id": response_id, "object": object_, "created": created_at, "model": model,
            "choices": [], "usage": usage}


# ----- usage: the Chat/Completions `CompletionUsage` contract -----
# Upstreams report usage in their own dialect; clients of the chat and
# completions routes get OpenAI's CompletionUsage only. Unknown upstream keys
# (e.g. `attribution`) are dropped here and kept in the traffic logs. Usage
# that cannot be read is omitted, never reported as zeros (ADR 0001).


def _count(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def _details(**members: Any) -> dict[str, int] | None:
    kept = {name: count for name, value in members.items() if (count := _count(value)) is not None}
    return kept or None


def _completion_usage(prompt: int, completion: int, prompt_details: dict[str, int] | None = None,
                      completion_details: dict[str, int] | None = None) -> dict[str, Any]:
    usage: dict[str, Any] = {
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "total_tokens": prompt + completion,
    }
    if prompt_details:
        usage["prompt_tokens_details"] = prompt_details
    if completion_details:
        usage["completion_tokens_details"] = completion_details
    return usage


def completion_usage_from_responses(usage: Any) -> dict[str, Any] | None:
    """Responses API usage as CompletionUsage. Responses `input_tokens`
    already include cached and cache-write tokens."""
    if not isinstance(usage, dict):
        return None
    prompt, completion = _count(usage.get("input_tokens")), _count(usage.get("output_tokens"))
    if prompt is None or completion is None:
        return None
    inputs = usage.get("input_tokens_details")
    outputs = usage.get("output_tokens_details")
    inputs = inputs if isinstance(inputs, dict) else {}
    outputs = outputs if isinstance(outputs, dict) else {}
    return _completion_usage(
        prompt, completion,
        _details(cached_tokens=inputs.get("cached_tokens"), cache_write_tokens=inputs.get("cache_write_tokens")),
        _details(reasoning_tokens=outputs.get("reasoning_tokens")),
    )


def completion_usage_from_anthropic(usage: Any) -> dict[str, Any] | None:
    """Anthropic Messages usage (the Claude CLI's) as CompletionUsage.
    Anthropic `input_tokens` exclude cache reads and writes; CompletionUsage
    counts every prompt token in `prompt_tokens`."""
    if not isinstance(usage, dict):
        return None
    uncached, completion = _count(usage.get("input_tokens")), _count(usage.get("output_tokens"))
    if uncached is None or completion is None:
        return None
    # Anthropic types the cache counts as nullable (null = none); a present
    # but unreadable count would make prompt_tokens a guess.
    raw_read, raw_write = usage.get("cache_read_input_tokens"), usage.get("cache_creation_input_tokens")
    read, write = _count(raw_read), _count(raw_write)
    if (raw_read is not None and read is None) or (raw_write is not None and write is None):
        return None
    return _completion_usage(
        uncached + (read or 0) + (write or 0), completion,
        _details(cached_tokens=read, cache_write_tokens=write),
    )


def _with_usage(payload: dict[str, Any], usage: dict[str, Any] | None) -> dict[str, Any]:
    # `usage` is optional in the spec: omit it rather than send null.
    if usage is not None:
        payload["usage"] = usage
    return payload
