from __future__ import annotations

import base64

import pytest

from airelay.store import AppStore
from airelay.transforms import (
    TranslationError,
    chat_completions_to_responses,
    chat_route_request_to_responses,
    completions_to_responses,
    normalize_models_payload,
    prepare_response_request,
    responses_to_chat_completion,
    strip_unsupported_response_parameters,
)


@pytest.fixture
def store(tmp_path):
    return AppStore(tmp_path / "data")


def test_reasoning_effort_is_forwarded_on_both_openai_text_routes(store: AppStore) -> None:
    """The single line that carries `reasoning_effort` upstream must be
    pinned: silently dropping it is the exact regression class that
    mislabeled experiment arms (see CHANGELOG 0.7.0/0.10.0)."""
    chat_payload, _, _ = chat_completions_to_responses(
        {
            "model": "gpt-5.5",
            "messages": [{"role": "user", "content": "hi"}],
            "reasoning_effort": "high",
        },
        store,
        allow_tools=True,
    )
    assert chat_payload["reasoning"] == {"effort": "high"}

    completions_payload, _, _ = completions_to_responses(
        {"model": "gpt-5.5", "prompt": "hi", "reasoning_effort": "high"}
    )
    assert completions_payload["reasoning"] == {"effort": "high"}


def test_reasoning_effort_null_is_treated_as_absent(store: AppStore) -> None:
    """An explicit JSON null means "not set" (OpenAI semantics); forwarding
    `reasoning: {"effort": null}` to the unverified upstream is a risk with
    zero upside."""
    chat_payload, _, _ = chat_completions_to_responses(
        {
            "model": "gpt-5.5",
            "messages": [{"role": "user", "content": "hi"}],
            "reasoning_effort": None,
        },
        store,
        allow_tools=True,
    )
    assert "reasoning" not in chat_payload

    completions_payload, _, _ = completions_to_responses(
        {"model": "gpt-5.5", "prompt": "hi", "reasoning_effort": None}
    )
    assert "reasoning" not in completions_payload


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


def test_prepare_response_request_strips_unsupported_identity_fields(store: AppStore) -> None:
    payload, _, _ = prepare_response_request(
        {
            "model": "gpt-5.4-mini",
            "input": "hello",
            "user": "cursor-user",
            "safety_identifier": "hash-123",
        },
        store,
        allow_tools=True,
    )

    assert "user" not in payload
    assert "safety_identifier" not in payload


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


def test_prepare_response_request_rewrites_local_pdf_file_id_as_input_file(
    store: AppStore,
) -> None:
    record = store.create_file(
        filename="sample.pdf",
        purpose="user_data",
        content_type="application/pdf",
        data=b"%PDF-1.4\nsample\n",
        sha256="abc123",
    )

    payload, _, _ = prepare_response_request(
        {
            "model": "gpt-5.4-mini",
            "input": [
                {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_file", "file_id": record["id"]}],
                }
            ],
        },
        store,
        allow_tools=True,
    )

    assert payload["input"][0]["content"] == [
        {
            "type": "input_file",
            "filename": "sample.pdf",
            "file_data": "data:application/pdf;base64,JVBERi0xLjQKc2FtcGxlCg==",
        }
    ]


def test_prepare_response_request_normalizes_raw_base64_input_file_to_data_url(
    store: AppStore,
) -> None:
    encoded = base64.b64encode(b"%PDF-1.4\nsample\n").decode("ascii")

    payload, _, _ = prepare_response_request(
        {
            "model": "gpt-5.4-mini",
            "input": [
                {
                    "type": "message",
                    "role": "user",
                    "content": [
                        {
                            "type": "input_file",
                            "filename": "sample.pdf",
                            "file_data": encoded,
                        }
                    ],
                }
            ],
        },
        store,
        allow_tools=True,
    )

    assert payload["input"][0]["content"] == [
        {
            "type": "input_file",
            "filename": "sample.pdf",
            "file_data": f"data:application/pdf;base64,{encoded}",
        }
    ]


def test_prepare_response_request_strips_max_output_tokens(store: AppStore) -> None:
    payload, wants_stream, conversation_id = prepare_response_request(
        {
            "model": "gpt-5.4-mini",
            "input": "hello",
            "max_output_tokens": 20,
        },
        store,
        allow_tools=True,
    )

    assert wants_stream is False
    assert conversation_id is None
    assert "max_output_tokens" not in payload


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


def test_chat_completions_to_responses_flattens_custom_tools_and_tool_choice(
    store: AppStore,
) -> None:
    payload, _, _ = chat_completions_to_responses(
        {
            "model": "gpt-5.4-mini",
            "messages": [{"role": "user", "content": "hello"}],
            "tools": [
                {
                    "type": "custom",
                    "custom": {
                        "name": "ApplyPatch",
                        "description": "Apply a patch.",
                        "format": {
                            "type": "grammar",
                            "grammar": {
                                "syntax": "lark",
                                "definition": "start: patch",
                            },
                        },
                    },
                }
            ],
            "tool_choice": {"type": "custom", "custom": {"name": "ApplyPatch"}},
        },
        store,
        allow_tools=True,
    )

    assert payload["tools"] == [
        {
            "type": "custom",
            "name": "ApplyPatch",
            "description": "Apply a patch.",
            "format": {
                "type": "grammar",
                "syntax": "lark",
                "definition": "start: patch",
            },
        }
    ]
    assert payload["tool_choice"] == {"type": "custom", "name": "ApplyPatch"}


def test_chat_completions_to_responses_accepts_cursor_flat_custom_tool_shape(
    store: AppStore,
) -> None:
    payload, _, _ = chat_completions_to_responses(
        {
            "model": "gpt-5.4-mini",
            "messages": [{"role": "user", "content": "hello"}],
            "tools": [
                {
                    "type": "custom",
                    "name": "ApplyPatch",
                    "description": "Apply a patch.",
                    "format": {
                        "type": "grammar",
                        "syntax": "lark",
                        "definition": "start: patch",
                    },
                }
            ],
            "tool_choice": {"type": "custom", "name": "ApplyPatch"},
        },
        store,
        allow_tools=True,
    )

    assert payload["tools"] == [
        {
            "type": "custom",
            "name": "ApplyPatch",
            "description": "Apply a patch.",
            "format": {
                "type": "grammar",
                "syntax": "lark",
                "definition": "start: patch",
            },
        }
    ]
    assert payload["tool_choice"] == {"type": "custom", "name": "ApplyPatch"}


def test_chat_completions_to_responses_maps_custom_tool_history_and_output(
    store: AppStore,
) -> None:
    payload, _, _ = chat_completions_to_responses(
        {
            "model": "gpt-5.4-mini",
            "messages": [
                {"role": "user", "content": "Apply this patch"},
                {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {
                            "id": "call_patch",
                            "type": "custom",
                            "custom": {
                                "name": "ApplyPatch",
                                "input": "*** Begin Patch\n*** End Patch\n",
                            },
                        }
                    ],
                },
                {
                    "role": "tool",
                    "tool_call_id": "call_patch",
                    "content": "applied",
                },
            ],
        },
        store,
        allow_tools=True,
    )

    assert payload["input"] == [
        {
            "type": "message",
            "role": "user",
            "content": [{"type": "input_text", "text": "Apply this patch"}],
        },
        {
            "type": "custom_tool_call",
            "call_id": "call_patch",
            "name": "ApplyPatch",
            "input": "*** Begin Patch\n*** End Patch\n",
        },
        {
            "type": "custom_tool_call_output",
            "call_id": "call_patch",
            "output": "applied",
        },
    ]


def test_chat_completions_to_responses_accepts_flat_custom_tool_call_history(
    store: AppStore,
) -> None:
    payload, _, _ = chat_completions_to_responses(
        {
            "model": "gpt-5.4-mini",
            "messages": [
                {"role": "user", "content": "Apply this patch"},
                {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {
                            "id": "call_patch",
                            "type": "custom",
                            "name": "ApplyPatch",
                            "input": "*** Begin Patch\n*** End Patch\n",
                        }
                    ],
                },
                {
                    "role": "tool",
                    "tool_call_id": "call_patch",
                    "content": "applied",
                },
            ],
        },
        store,
        allow_tools=True,
    )

    assert payload["input"][1:] == [
        {
            "type": "custom_tool_call",
            "call_id": "call_patch",
            "name": "ApplyPatch",
            "input": "*** Begin Patch\n*** End Patch\n",
        },
        {
            "type": "custom_tool_call_output",
            "call_id": "call_patch",
            "output": "applied",
        },
    ]


def test_chat_completions_to_responses_rejects_orphaned_tool_output(store: AppStore) -> None:
    with pytest.raises(
        TranslationError,
        match="preceding assistant tool call",
    ):
        chat_completions_to_responses(
            {
                "model": "gpt-5.4-mini",
                "messages": [
                    {"role": "user", "content": "Apply this patch"},
                    {
                        "role": "tool",
                        "tool_call_id": "call_patch",
                        "content": "applied",
                    },
                ],
            },
            store,
            allow_tools=True,
        )


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


def test_chat_route_request_to_responses_accepts_responses_shape_on_chat_route(
    store: AppStore,
) -> None:
    payload, wants_stream, conversation_id = chat_route_request_to_responses(
        {
            "model": "gpt-5.4-mini",
            "input": "hello",
            "stream": False,
            "tools": [
                {
                    "type": "custom",
                    "name": "ApplyPatch",
                    "description": "Apply a patch.",
                    "format": {
                        "type": "grammar",
                        "syntax": "lark",
                        "definition": "start: patch",
                    },
                }
            ],
        },
        store,
        allow_tools=True,
    )

    assert wants_stream is False
    assert conversation_id is None
    assert payload["input"] == [
        {
            "type": "message",
            "role": "user",
            "content": [{"type": "input_text", "text": "hello"}],
        }
    ]
    assert payload["tools"][0]["type"] == "custom"
    assert payload["tools"][0]["name"] == "ApplyPatch"


def test_chat_route_request_to_responses_rejects_mixed_chat_and_responses_shapes(
    store: AppStore,
) -> None:
    with pytest.raises(TranslationError, match="either chat.completions `messages` or Responses `input`"):
        chat_route_request_to_responses(
            {
                "model": "gpt-5.4-mini",
                "messages": [{"role": "user", "content": "hello"}],
                "input": "hello",
            },
            store,
            allow_tools=True,
        )


def test_chat_completions_to_responses_rejects_tool_choice_on_no_tools_route(
    store: AppStore,
) -> None:
    with pytest.raises(TranslationError, match="disables tools"):
        chat_completions_to_responses(
            {
                "model": "gpt-5.4-mini",
                "messages": [{"role": "user", "content": "hello"}],
                "tool_choice": {"type": "custom", "name": "ApplyPatch"},
            },
            store,
            allow_tools=False,
        )


def test_responses_to_chat_completion_maps_custom_tool_calls() -> None:
    payload = responses_to_chat_completion(
        {
            "id": "resp_123",
            "created_at": 1,
            "model": "gpt-5.4-mini",
            "output": [
                {
                    "type": "custom_tool_call",
                    "call_id": "call_patch",
                    "name": "ApplyPatch",
                    "input": "*** Begin Patch\n*** End Patch\n",
                }
            ],
        }
    )

    assert payload["choices"][0]["message"]["tool_calls"] == [
        {
            "id": "call_patch",
            "type": "custom",
            "custom": {
                "name": "ApplyPatch",
                "input": "*** Begin Patch\n*** End Patch\n",
            },
        }
    ]
    assert payload["choices"][0]["finish_reason"] == "tool_calls"


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
        }
    )

    assert wants_stream is False
    assert conversation_id is None
    assert payload["instructions"] == "."
    assert payload["input"][0]["content"][0]["text"] == "Say hello."


def test_chat_completions_to_responses_ignores_max_completion_tokens(store: AppStore) -> None:
    payload, wants_stream, conversation_id = chat_completions_to_responses(
        {
            "model": "gpt-5.4-mini",
            "messages": [{"role": "user", "content": "hello"}],
            "max_completion_tokens": 32,
        },
        store,
        allow_tools=True,
    )

    assert wants_stream is False
    assert conversation_id is None
    assert "max_completion_tokens" not in payload


def test_completions_to_responses_ignores_max_tokens() -> None:
    payload, wants_stream, conversation_id = completions_to_responses(
        {
            "model": "gpt-5.4-mini",
            "prompt": "Say hello.",
            "max_tokens": 32,
        }
    )

    assert wants_stream is False
    assert conversation_id is None
    assert "max_tokens" not in payload


def test_strip_unsupported_response_parameters_removes_sampling_token_and_identity_fields() -> None:
    payload = {
        "model": "gpt-5.4",
        "temperature": 0.7,
        "top_p": 0.9,
        "presence_penalty": 0,
        "frequency_penalty": 0,
        "max_output_tokens": 32,
        "user": "cursor-user",
        "safety_identifier": "hash-123",
    }

    ignored = strip_unsupported_response_parameters(payload)

    assert ignored == [
        "temperature",
        "top_p",
        "presence_penalty",
        "frequency_penalty",
        "max_output_tokens",
        "user",
        "safety_identifier",
    ]
    assert payload == {
        "model": "gpt-5.4",
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
