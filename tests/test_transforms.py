from __future__ import annotations

import pytest

from airelay.store import AppStore
from airelay.transforms import (
    TranslationError,
    chat_completions_to_responses,
    completions_to_responses,
    normalize_models_payload,
    prepare_response_request,
    strip_unsupported_response_parameters,
)


@pytest.fixture
def store(tmp_path):
    return AppStore(tmp_path / "data")


def test_prepare_response_request_defaults_to_minimal_instructions(store: AppStore) -> None:
    payload, wants_stream, conversation_id = prepare_response_request(
        {"model": "gpt-5.4-mini", "input": "hello"},
        store,
        allow_tools=True,
    )

    assert wants_stream is False
    assert conversation_id is None
    assert payload["instructions"] == "."
    assert payload["store"] is False
    assert payload["stream"] is True
    assert payload["tools"] == []
    assert payload["input"][0]["content"][0]["text"] == "hello"


def test_prepare_response_request_rejects_store_true(store: AppStore) -> None:
    with pytest.raises(TranslationError, match="store=false"):
        prepare_response_request(
            {"model": "gpt-5.4-mini", "input": "hello", "store": True},
            store,
            allow_tools=True,
        )


def test_prepare_response_request_rejects_unknown_local_file(store: AppStore) -> None:
    with pytest.raises(TranslationError, match="Unknown local file id `file_missing`"):
        prepare_response_request(
            {
                "model": "gpt-5.4-mini",
                "input": [
                    {
                        "type": "message",
                        "role": "user",
                        "content": [{"type": "input_image", "file_id": "file_missing"}],
                    }
                ],
            },
            store,
            allow_tools=True,
        )


def test_prepare_response_request_accepts_conversation_object_id(store: AppStore) -> None:
    payload, wants_stream, conversation_id = prepare_response_request(
        {
            "model": "gpt-5.4-mini",
            "input": "hello",
            "conversation": {"id": "conv_123"},
        },
        store,
        allow_tools=True,
    )

    assert wants_stream is False
    assert conversation_id == "conv_123"
    assert "conversation" not in payload


def test_prepare_response_request_normalizes_json_schema_text_format(store: AppStore) -> None:
    payload, _, _ = prepare_response_request(
        {
            "model": "gpt-5.4-mini",
            "input": "hello",
            "text": {
                "format": {
                    "type": "json_schema",
                    "name": "demo_schema",
                    "schema": {
                        "type": "object",
                        "properties": {"answer": {"type": "string"}},
                        "required": ["answer"],
                    },
                    "strict": True,
                }
            },
        },
        store,
        allow_tools=True,
    )

    assert payload["text"]["format"] == {
        "type": "json_schema",
        "name": "demo_schema",
        "schema": {
            "type": "object",
            "properties": {"answer": {"type": "string"}},
            "required": ["answer"],
            "additionalProperties": False,
        },
        "strict": True,
    }


def test_prepare_response_request_normalizes_nested_required_keys_in_text_format(
    store: AppStore,
) -> None:
    payload, _, _ = prepare_response_request(
        {
            "model": "gpt-5.4-mini",
            "input": "hello",
            "text": {
                "format": {
                    "type": "json_schema",
                    "name": "nested_schema",
                    "schema": {
                        "type": "object",
                        "properties": {
                            "sources": {
                                "type": "array",
                                "items": {
                                    "type": "object",
                                    "properties": {
                                        "title": {"type": "string"},
                                        "publisher": {"type": "string"},
                                    },
                                    "required": ["title"],
                                },
                            }
                        },
                        "required": ["sources"],
                    },
                }
            },
        },
        store,
        allow_tools=True,
    )

    assert payload["text"]["format"]["schema"] == {
        "type": "object",
        "properties": {
            "sources": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "title": {"type": "string"},
                        "publisher": {"type": ["string", "null"]},
                    },
                    "required": ["title", "publisher"],
                    "additionalProperties": False,
                },
            }
        },
        "required": ["sources"],
        "additionalProperties": False,
    }


def test_prepare_response_request_rejects_true_additional_properties_in_text_format(
    store: AppStore,
) -> None:
    with pytest.raises(TranslationError, match="additionalProperties"):
        prepare_response_request(
            {
                "model": "gpt-5.4-mini",
                "input": "hello",
                "text": {
                    "format": {
                        "type": "json_schema",
                        "name": "bad_schema",
                        "schema": {
                            "type": "object",
                            "properties": {"answer": {"type": "string"}},
                            "required": ["answer"],
                            "additionalProperties": True,
                        },
                    }
                },
            },
            store,
            allow_tools=True,
        )


def test_prepare_response_request_rejects_json_object_text_format(store: AppStore) -> None:
    with pytest.raises(TranslationError, match="json_object"):
        prepare_response_request(
            {
                "model": "gpt-5.4-mini",
                "input": "hello",
                "text": {"format": {"type": "json_object"}},
            },
            store,
            allow_tools=True,
        )


def test_prepare_response_request_rejects_tools_on_no_tools_route(store: AppStore) -> None:
    with pytest.raises(TranslationError, match="disables tools"):
        prepare_response_request(
            {
                "model": "gpt-5.4-mini",
                "input": "hello",
                "tools": [{"type": "function", "name": "lookup", "parameters": {"type": "object"}}],
            },
            store,
            allow_tools=False,
        )


def test_chat_completions_to_responses_uses_system_messages_as_instructions(
    store: AppStore,
) -> None:
    payload, wants_stream, conversation_id = chat_completions_to_responses(
        {
            "model": "gpt-5.4-mini",
            "n": 1,
            "stream": True,
            "messages": [
                {"role": "system", "content": "Be terse."},
                {"role": "developer", "content": "Return lowercase text only."},
                {"role": "user", "content": "Say Hello"},
            ],
        },
        store,
        allow_tools=True,
    )

    assert wants_stream is True
    assert conversation_id is None
    assert payload["instructions"] == "Be terse.\n\nReturn lowercase text only."
    assert payload["store"] is False
    assert payload["tool_choice"] == "none"
    assert payload["input"] == [
        {
            "type": "message",
            "role": "user",
            "content": [{"type": "input_text", "text": "Say Hello"}],
        }
    ]


def test_chat_completions_to_responses_maps_assistant_history_to_output_text(
    store: AppStore,
) -> None:
    payload, _, _ = chat_completions_to_responses(
        {
            "model": "gpt-5.4-mini",
            "messages": [
                {"role": "user", "content": "Question"},
                {"role": "assistant", "content": "Answer"},
                {"role": "user", "content": "Follow-up"},
            ],
        },
        store,
        allow_tools=True,
    )

    assert payload["input"] == [
        {
            "type": "message",
            "role": "user",
            "content": [{"type": "input_text", "text": "Question"}],
        },
        {
            "type": "message",
            "role": "assistant",
            "content": [{"type": "output_text", "text": "Answer"}],
        },
        {
            "type": "message",
            "role": "user",
            "content": [{"type": "input_text", "text": "Follow-up"}],
        },
    ]


def test_chat_completions_to_responses_omits_empty_assistant_message_with_tool_calls(
    store: AppStore,
) -> None:
    payload, _, _ = chat_completions_to_responses(
        {
            "model": "gpt-5.4-mini",
            "messages": [
                {"role": "user", "content": "Research this"},
                {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {
                            "id": "call_123",
                            "type": "function",
                            "function": {"name": "web_search", "arguments": "{\"query\":\"x\"}"},
                        }
                    ],
                },
                {
                    "role": "tool",
                    "tool_call_id": "call_123",
                    "content": "{\"results\":[]}",
                },
            ],
            "tools": [
                {
                    "type": "function",
                    "function": {
                        "name": "web_search",
                        "parameters": {"type": "object", "properties": {"query": {"type": "string"}}},
                    },
                }
            ],
        },
        store,
        allow_tools=True,
    )

    assert payload["input"] == [
        {
            "type": "message",
            "role": "user",
            "content": [{"type": "input_text", "text": "Research this"}],
        },
        {
            "type": "function_call",
            "call_id": "call_123",
            "name": "web_search",
            "arguments": "{\"query\":\"x\"}",
        },
        {
            "type": "function_call_output",
            "call_id": "call_123",
            "output": "{\"results\":[]}",
        },
    ]


def test_chat_completions_to_responses_flattens_function_tools_and_tool_choice(
    store: AppStore,
) -> None:
    payload, _, _ = chat_completions_to_responses(
        {
            "model": "gpt-5.4-mini",
            "messages": [{"role": "user", "content": "hello"}],
            "tools": [
                {
                    "type": "function",
                    "function": {
                        "name": "web_search",
                        "description": "Search the web.",
                        "parameters": {"type": "object", "properties": {"query": {"type": "string"}}},
                    },
                }
            ],
            "tool_choice": {"type": "function", "function": {"name": "web_search"}},
        },
        store,
        allow_tools=True,
    )

    assert payload["tools"] == [
        {
            "type": "function",
            "name": "web_search",
            "description": "Search the web.",
            "parameters": {"type": "object", "properties": {"query": {"type": "string"}}},
        }
    ]
    assert payload["tool_choice"] == {"type": "function", "name": "web_search"}


def test_chat_completions_to_responses_flattens_legacy_functions_and_function_call(
    store: AppStore,
) -> None:
    payload, _, _ = chat_completions_to_responses(
        {
            "model": "gpt-5.4-mini",
            "messages": [{"role": "user", "content": "hello"}],
            "functions": [
                {
                    "name": "lookup",
                    "description": "Look up data.",
                    "parameters": {"type": "object", "properties": {"id": {"type": "string"}}},
                }
            ],
            "function_call": {"name": "lookup"},
        },
        store,
        allow_tools=True,
    )

    assert payload["tools"] == [
        {
            "type": "function",
            "name": "lookup",
            "description": "Look up data.",
            "parameters": {"type": "object", "properties": {"id": {"type": "string"}}},
        }
    ]
    assert payload["tool_choice"] == {"type": "function", "name": "lookup"}


def test_chat_completions_to_responses_rejects_legacy_functions_on_no_tools_route(
    store: AppStore,
) -> None:
    with pytest.raises(TranslationError, match="disables tools"):
        chat_completions_to_responses(
            {
                "model": "gpt-5.4-mini",
                "messages": [{"role": "user", "content": "hello"}],
                "functions": [{"name": "lookup", "parameters": {"type": "object"}}],
            },
            store,
            allow_tools=False,
        )


def test_chat_completions_to_responses_rejects_unverified_json_object_response_format(
    store: AppStore,
) -> None:
    with pytest.raises(TranslationError, match="json_object"):
        chat_completions_to_responses(
            {
                "model": "gpt-5.4-mini",
                "messages": [{"role": "user", "content": "hello"}],
                "response_format": {"type": "json_object"},
            },
            store,
            allow_tools=True,
        )


def test_chat_completions_to_responses_maps_json_schema_response_format(store: AppStore) -> None:
    payload, _, _ = chat_completions_to_responses(
        {
            "model": "gpt-5.4-mini",
            "messages": [{"role": "user", "content": "hello"}],
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": "demo_schema",
                    "schema": {
                        "type": "object",
                        "properties": {"answer": {"type": "string"}},
                        "required": ["answer"],
                    },
                    "strict": True,
                },
            },
        },
        store,
        allow_tools=True,
    )

    assert payload["text"]["format"] == {
        "type": "json_schema",
        "name": "demo_schema",
        "schema": {
            "type": "object",
            "properties": {"answer": {"type": "string"}},
            "required": ["answer"],
            "additionalProperties": False,
        },
        "strict": True,
    }


def test_chat_completions_to_responses_normalizes_nested_object_schemas(store: AppStore) -> None:
    payload, _, _ = chat_completions_to_responses(
        {
            "model": "gpt-5.4-mini",
            "messages": [{"role": "user", "content": "hello"}],
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": "nested_schema",
                    "schema": {
                        "type": "object",
                        "properties": {
                            "sources": {
                                "type": "array",
                                "items": {
                                    "type": "object",
                                    "properties": {"title": {"type": "string"}},
                                    "required": ["title"],
                                },
                            }
                        },
                        "required": ["sources"],
                    },
                },
            },
        },
        store,
        allow_tools=True,
    )

    assert payload["text"]["format"]["schema"] == {
        "type": "object",
        "properties": {
            "sources": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {"title": {"type": "string"}},
                    "required": ["title"],
                    "additionalProperties": False,
                },
            }
        },
        "required": ["sources"],
        "additionalProperties": False,
    }


def test_chat_completions_to_responses_normalizes_optional_properties_to_nullable_required(
    store: AppStore,
) -> None:
    payload, _, _ = chat_completions_to_responses(
        {
            "model": "gpt-5.4-mini",
            "messages": [{"role": "user", "content": "hello"}],
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": "nullable_schema",
                    "schema": {
                        "type": "object",
                        "properties": {
                            "answer": {"type": "string"},
                            "publisher": {"type": "string"},
                        },
                        "required": ["answer"],
                    },
                },
            },
        },
        store,
        allow_tools=True,
    )

    assert payload["text"]["format"]["schema"] == {
        "type": "object",
        "properties": {
            "answer": {"type": "string"},
            "publisher": {"type": ["string", "null"]},
        },
        "required": ["answer", "publisher"],
        "additionalProperties": False,
    }


def test_chat_completions_to_responses_normalizes_nested_required_keys(
    store: AppStore,
) -> None:
    payload, _, _ = chat_completions_to_responses(
        {
            "model": "gpt-5.4-mini",
            "messages": [{"role": "user", "content": "hello"}],
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": "nested_required_schema",
                    "schema": {
                        "type": "object",
                        "properties": {
                            "sources": {
                                "type": "array",
                                "items": {
                                    "type": "object",
                                    "properties": {
                                        "title": {"type": "string"},
                                        "url": {"type": "string"},
                                        "publisher": {
                                            "anyOf": [{"type": "string"}, {"type": "null"}]
                                        },
                                    },
                                    "required": ["title", "url"],
                                },
                            }
                        },
                        "required": ["sources"],
                    },
                },
            },
        },
        store,
        allow_tools=True,
    )

    assert payload["text"]["format"]["schema"] == {
        "type": "object",
        "properties": {
            "sources": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "title": {"type": "string"},
                        "url": {"type": "string"},
                        "publisher": {"anyOf": [{"type": "string"}, {"type": "null"}]},
                    },
                    "required": ["title", "url", "publisher"],
                    "additionalProperties": False,
                },
            }
        },
        "required": ["sources"],
        "additionalProperties": False,
    }


def test_chat_completions_to_responses_rejects_true_additional_properties(store: AppStore) -> None:
    with pytest.raises(TranslationError, match="additionalProperties"):
        chat_completions_to_responses(
            {
                "model": "gpt-5.4-mini",
                "messages": [{"role": "user", "content": "hello"}],
                "response_format": {
                    "type": "json_schema",
                    "json_schema": {
                        "name": "bad_schema",
                        "schema": {
                            "type": "object",
                            "properties": {"answer": {"type": "string"}},
                            "required": ["answer"],
                            "additionalProperties": True,
                        },
                    },
                },
            },
            store,
            allow_tools=True,
        )


def test_completions_to_responses_supports_legacy_prompt_shape() -> None:
    payload, wants_stream, conversation_id = completions_to_responses(
        {
            "model": "gpt-5.4-mini",
            "prompt": "Say hello.",
            "stream": False,
            "max_tokens": 32,
        }
    )

    assert wants_stream is False
    assert conversation_id is None
    assert payload["instructions"] == "."
    assert payload["input"][0]["content"][0]["text"] == "Say hello."
    assert payload["max_output_tokens"] == 32


def test_strip_unsupported_response_parameters_removes_sampling_parameters() -> None:
    payload = {
        "model": "gpt-5.4",
        "temperature": 0.7,
        "top_p": 0.9,
        "presence_penalty": 0,
        "frequency_penalty": 0,
        "max_output_tokens": 32,
    }

    ignored = strip_unsupported_response_parameters(payload)

    assert ignored == [
        "temperature",
        "top_p",
        "presence_penalty",
        "frequency_penalty",
    ]
    assert payload == {
        "model": "gpt-5.4",
        "max_output_tokens": 32,
    }


def test_normalize_models_payload_returns_openai_list_shape() -> None:
    payload = normalize_models_payload(
        {
            "models": [
                {"slug": "gpt-5.4-mini", "supported_in_api": True},
                {"slug": "gpt-5.5", "supported_in_api": True},
            ]
        }
    )

    assert payload == {
        "object": "list",
        "data": [
            {
                "id": "gpt-5.4-mini",
                "object": "model",
                "created": 0,
                "owned_by": "airelays-upstream",
            },
            {
                "id": "gpt-5.5",
                "object": "model",
                "created": 0,
                "owned_by": "airelays-upstream",
            },
        ],
    }
