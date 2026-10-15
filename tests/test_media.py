"""The shared image and document check (airelay.media) and its use on both
runtimes: a bad image fails the same way on every model, before any
upstream call."""

from __future__ import annotations

import base64
import struct
import zlib

import pytest

from airelay.claude_content import ClaudeContentError, conversation_from_chat
from airelay.media import MediaError, document_from_data, image_from_url
from airelay.store import AppStore
from airelay.transforms import TranslationError, chat_completions_to_responses


def png(size: int = 8) -> bytes:
    raw = b"".join(b"\x00" + b"\xff\x00\x00" * size for _ in range(size))
    chunk = lambda t, d: struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d))  # noqa: E731
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


def data_url(raw: bytes, kind: str = "image/png") -> str:
    return f"data:{kind};base64,{base64.b64encode(raw).decode()}"


def test_images_are_sniffed_and_corrected_not_trusted() -> None:
    media = image_from_url(data_url(png(), "image/jpeg"))  # wrong declared type
    assert media.media_type == "image/png"
    assert image_from_url("https://example.com/a.png").url == "https://example.com/a.png"
    for bad, message in ((data_url(b"%PDF-1.4"), "not a PNG"), ("data:image/png;base64,!!!", "base64"),
                         ("ftp://x/a.png", "http"), ("", "non-empty"), (None, "non-empty")):
        with pytest.raises(MediaError, match=message):
            image_from_url(bad)


def test_documents_by_content() -> None:
    assert document_from_data(data_url(b"%PDF-1.4 x", "application/pdf")).media_type == "application/pdf"
    assert document_from_data(data_url(b"hello", "text/plain")).data == "hello"
    assert document_from_data(base64.b64encode(png()).decode(), "a.bin").kind == "image"
    with pytest.raises(MediaError, match="Unsupported file type"):
        document_from_data(data_url(b"\x00\x01binary", "application/octet-stream"))


def test_claude_transcript_keeps_media_in_place() -> None:
    conversation = conversation_from_chat([
        {"role": "system", "content": "S"},
        {"role": "user", "content": [{"type": "text", "text": "look"}, {"type": "image_url", "image_url": {"url": data_url(png())}}]},
        {"role": "assistant", "content": "a red square"},
        {"role": "user", "content": "and now?"}])
    kinds = [block["type"] for block in conversation.blocks]
    assert conversation.system_parts == ["S"] and kinds == ["text", "image", "text"] and conversation.media_count == 1
    assert conversation.blocks[0]["text"] == "Human: look" and conversation.blocks[2]["text"].endswith("\n\nAssistant:")
    with pytest.raises(ClaudeContentError, match="only allowed in user messages"):
        conversation_from_chat([{"role": "assistant", "content": [{"type": "image_url", "image_url": {"url": data_url(png())}}]}])


@pytest.mark.parametrize("image_url, expected", [
    (lambda: data_url(png(), "image/gif"), "data:image/png;base64,"),  # corrected type
    (lambda: "https://example.com/a.png", "https://example.com/a.png"),
])
def test_gpt_chat_images_use_the_same_check(tmp_path, image_url, expected) -> None:
    for value in ({"url": image_url()}, image_url()):  # object or the URL alone
        body = {"model": "gpt-5.4-mini", "messages": [{"role": "user", "content": [
            {"type": "image_url", "image_url": value}]}]}
        payload, _, _ = chat_completions_to_responses(body, AppStore(tmp_path), allow_tools=True)
        assert payload["input"][0]["content"][0]["image_url"].startswith(expected)


def test_gpt_chat_rejects_bad_images_locally(tmp_path) -> None:
    body = {"model": "gpt-5.4-mini", "messages": [{"role": "user", "content": [
        {"type": "image_url", "image_url": {"url": data_url(b"not an image")}}]}]}
    with pytest.raises(TranslationError, match="not a PNG"):
        chat_completions_to_responses(body, AppStore(tmp_path), allow_tools=True)
    body["messages"][0]["content"][0]["image_url"] = {}
    with pytest.raises(TranslationError, match="non-empty"):
        chat_completions_to_responses(body, AppStore(tmp_path), allow_tools=True)
