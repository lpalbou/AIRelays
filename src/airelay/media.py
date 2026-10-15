"""Images and documents in OpenAI requests, checked once for every runtime.

A bad image must fail the same way on every model: as a clear 422 before any
upstream call. The Claude CLI silently replaces images it cannot read with a
text notice and reports success, so nothing unreadable may reach it.
"""

from __future__ import annotations

import base64
import binascii
import re
from dataclasses import dataclass

MAX_IMAGE_BYTES = 20 * 1024 * 1024
MAX_DOCUMENT_BYTES = 32 * 1024 * 1024
MAX_MEDIA_PARTS = 100

# Formats both upstreams read, recognised by their first bytes; a wrong
# declared type is corrected, not rejected.
_IMAGE_SIGNATURES = (
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"GIF87a", "image/gif"),
    (b"GIF89a", "image/gif"),
)
_DATA_URL = re.compile(r"data:(?P<type>[\w.+-]+/[\w.+-]+)?(?:;[\w-]+=[^;,]*)*;base64,(?P<data>.*)", re.S | re.I)
TEXT_TYPES = ("text/", "application/json", "application/xml", "application/yaml", "application/x-yaml")


class MediaError(ValueError):
    """An image or document that cannot be sent; the message says why."""


@dataclass(frozen=True, slots=True)
class Media:
    kind: str  # "image" or "document"
    media_type: str | None = None
    data: str | None = None  # base64, or text for a text document
    url: str | None = None
    filename: str | None = None


def sniff_image(raw: bytes) -> str | None:
    for signature, media_type in _IMAGE_SIGNATURES:
        if raw.startswith(signature):
            return media_type
    if raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":
        return "image/webp"
    return None


def _decode(data: str, what: str) -> bytes:
    try:
        raw = base64.b64decode("".join(data.split()), validate=True)
    except (binascii.Error, ValueError) as exc:
        raise MediaError(f"The {what} is not valid base64 data.") from exc
    if not raw:
        raise MediaError(f"The {what} is empty.")
    return raw


def image_from_url(url: object) -> Media:
    """An `image_url` value: a base64 data URL (PNG, JPEG, GIF or WebP) or
    an http(s) URL, which the upstream fetches itself."""
    if not isinstance(url, str) or not url.strip():
        raise MediaError("`image_url.url` must be a non-empty string.")
    url = url.strip()
    if url.lower().startswith(("https://", "http://")):
        return Media("image", url=url)
    match = _DATA_URL.fullmatch(url)
    if not match:
        raise MediaError("`image_url.url` must be an http(s) URL or a base64 `data:` URL.")
    raw = _decode(match.group("data"), "image")
    if len(raw) > MAX_IMAGE_BYTES:
        raise MediaError(f"The image is larger than {MAX_IMAGE_BYTES // (1024 * 1024)} MiB.")
    media_type = sniff_image(raw)
    if media_type is None:
        raise MediaError("The image is not a PNG, JPEG, GIF or WebP file.")
    return Media("image", media_type=media_type, data=base64.b64encode(raw).decode("ascii"))


def document_from_data(file_data: object, filename: object = None) -> Media:
    """A `file` part's inline `file_data`: a data URL or raw base64. PDFs are
    sent as documents and text files as text; images as images."""
    if not isinstance(file_data, str) or not file_data.strip():
        raise MediaError("`file.file_data` must be a non-empty string.")
    name = filename if isinstance(filename, str) and filename else None
    match = _DATA_URL.fullmatch(file_data.strip())
    declared = (match.group("type") or "").lower() if match else ""
    raw = _decode(match.group("data") if match else file_data, "file")
    return document_from_bytes(raw, declared, name)


def document_from_bytes(raw: bytes, declared_type: str, filename: str | None) -> Media:
    if raw.startswith(b"%PDF-"):
        if len(raw) > MAX_DOCUMENT_BYTES:
            raise MediaError(f"The PDF is larger than {MAX_DOCUMENT_BYTES // (1024 * 1024)} MiB.")
        return Media("document", "application/pdf", base64.b64encode(raw).decode("ascii"), filename=filename)
    image_type = sniff_image(raw)
    if image_type is not None:
        if len(raw) > MAX_IMAGE_BYTES:
            raise MediaError(f"The image is larger than {MAX_IMAGE_BYTES // (1024 * 1024)} MiB.")
        return Media("image", image_type, base64.b64encode(raw).decode("ascii"), filename=filename)
    if declared_type.startswith(TEXT_TYPES) or (filename or "").lower().endswith((".txt", ".md", ".csv", ".json")):
        if len(raw) > MAX_DOCUMENT_BYTES:
            raise MediaError(f"The text file is larger than {MAX_DOCUMENT_BYTES // (1024 * 1024)} MiB.")
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise MediaError("The text file is not valid UTF-8.") from exc
        return Media("document", "text/plain", text, filename=filename)
    raise MediaError("Unsupported file type: send a PDF, a PNG/JPEG/GIF/WebP image, or a UTF-8 text file.")
