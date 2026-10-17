"""OpenAI Images API requests on the ChatGPT subscription backend.

The backend serves `images/generations` and `images/edits` (JSON in, the
OpenAI Images response out) for the Codex client's built-in image tool.
Verified live on 2026-10-18, it validates almost nothing: any `model` is
accepted, `n` always yields one image, and `stream`, `output_format`,
`output_compression`, `partial_images`, `response_format`, `style`, `user`
and `safety_identifier` change nothing (`background` is the one parameter
it checks and applies). `quality` and `size` are accepted but the service
chooses its own: four flat-shape requests asking for portrait, square and
landscape sizes at low, medium and high quality all came back 1254x1254 at
`low`, while photographic prompts came back 1536x1024 at `medium`. So this
module validates every parameter against OpenAI's published surface before
the paid call, forwards `quality` and `size` and reports them as ignored
when the answer differs from the request (`unapplied_image_parameters`),
honours `n` by fan-out, honours `stream` by delivering the finished image
as OpenAI's stream events, and names every parameter that could not be
honoured so the caller sees them in `x-airelays-ignored-parameters` instead
of silently losing them. A `mask` is refused: dropping it would change the
output.
"""

from __future__ import annotations

import base64
import re
from dataclasses import dataclass, field
from typing import Any, Mapping

from airelay.errors import InvalidRequestError
from airelay.media import MAX_IMAGE_BYTES, sniff_image

DEFAULT_IMAGE_MODEL = "gpt-image-2"
# Advertised in /v1/models. Other `gpt-image-*` ids are forwarded as named;
# the upstream reports no model back, so no claim is made about them.
IMAGE_MODEL_IDS = (DEFAULT_IMAGE_MODEL,)
IMAGE_QUALITIES = ("auto", "low", "medium", "high")
IMAGE_BACKGROUNDS = ("transparent", "opaque", "auto")
IMAGE_MODERATIONS = ("low", "auto")
IMAGE_OUTPUT_FORMATS = ("png", "jpeg", "webp")
IMAGE_RESPONSE_FORMATS = ("url", "b64_json")
IMAGE_STYLES = ("vivid", "natural")
IMAGE_INPUT_FIDELITIES = ("high", "low")
MAX_PROMPT_CHARS = 32000
MAX_IMAGES_PER_REQUEST = 10
MAX_EDIT_IMAGES = 16
MAX_EDIT_IMAGE_BYTES = MAX_IMAGE_BYTES
# Size rules for the GPT image models (OpenAI's reference): any WIDTHxHEIGHT
# with both sides divisible by 16, aspect ratio within 1:3..3:1, longest
# edge at most 3840, within the model's pixel budget.
_SIZE_MIN_PIXELS = 655_360
_SIZE_MAX_PIXELS = 8_294_400
_SIZE_MAX_EDGE = 3840
_SIZE = re.compile(r"^(\d{2,4})x(\d{2,4})$")
_DATA_URL = re.compile(r"data:(?P<type>[\w.+-]+/[\w.+-]+)?(?:;[\w-]+=[^;,]*)*;base64,(?P<data>.*)", re.S | re.I)

_COMMON_PARAMETERS = frozenset({
    "prompt", "model", "n", "quality", "size", "background", "moderation", "stream", "partial_images",
    "output_format", "output_compression", "response_format", "user", "safety_identifier",
})
GENERATION_PARAMETERS = frozenset({*_COMMON_PARAMETERS, "style"})
EDIT_PARAMETERS = frozenset({*_COMMON_PARAMETERS, "image", "mask", "input_fidelity"})
# Multipart form values arrive as strings; these are typed on the JSON API.
_INT_FIELDS = ("n", "partial_images", "output_compression")
_BOOL_FIELDS = ("stream",)

IMAGE_ADAPTATION_REASON = (
    "The ChatGPT subscription backend's image service does not apply these OpenAI "
    "parameters (verified live), so the compatibility layer omitted them."
)


class UnknownImageModelError(InvalidRequestError):
    status_code = 404


@dataclass(frozen=True, slots=True)
class ImageRequest:
    """One validated image request: the upstream JSON for a single image,
    how many images to produce (one upstream call each), whether to answer
    as a stream, and the parameters that were dropped and must be disclosed."""

    operation: str  # "generation" or "edit"
    payload: dict[str, Any]
    model: str
    count: int = 1
    stream: bool = False
    ignored: list[str] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class EditImage:
    """One input image for an edit, already decoded and checked."""

    media_type: str
    data: str  # base64

    @property
    def data_url(self) -> str:
        return f"data:{self.media_type};base64,{self.data}"


def _invalid(message: str, param: str, *, code: str = "invalid_value") -> InvalidRequestError:
    return InvalidRequestError(message, code=code, param=param)


def _listed(values: tuple[str, ...]) -> str:
    return ", ".join(f"`{value}`" for value in values)


def _enum(body: Mapping[str, Any], name: str, allowed: tuple[str, ...]) -> str | None:
    value = body.get(name)
    if value is None:
        return None
    if not isinstance(value, str) or value not in allowed:
        raise _invalid(f"`{name}` must be one of {_listed(allowed)}.", name)
    return value


def _int_in_range(body: Mapping[str, Any], name: str, low: int, high: int) -> int | None:
    value = body.get(name)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise _invalid(f"`{name}` must be an integer between {low} and {high}.", name)
    return value


def image_model_id(value: Any) -> str:
    """The model id to send upstream: the default when absent, any GPT
    image model id as named (the upstream accepts every id without saying
    which model answered, so only the family it serves is admitted), and a
    clear refusal for anything else."""
    if value is None:
        return DEFAULT_IMAGE_MODEL
    if not isinstance(value, str) or not value.strip():
        raise _invalid("`model` must be a non-empty string.", "model")
    model = value.strip()
    lowered = model.lower()
    if lowered.startswith(("claude:", "claude-")):
        raise InvalidRequestError(
            "Image generation is available on GPT models only; use `gpt-image-2`.",
            code="unsupported_for_provider", param="model",
        )
    if lowered.startswith("gpt-image-") or lowered == "chatgpt-image-latest":
        return model
    raise UnknownImageModelError(
        f"Unknown image model `{model}`. The subscription backend serves the GPT image models; "
        f"use `{DEFAULT_IMAGE_MODEL}` (listed in `/v1/models`).",
        code="model_not_found", param="model",
    )


def validate_image_size(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise _invalid("`size` must be a string such as `1024x1024` or `auto`.", "size")
    size = value.strip().lower()
    if size == "auto":
        return size
    match = _SIZE.fullmatch(size)
    rules = (
        "`size` must be `auto` or `WIDTHxHEIGHT` with both sides multiples of 16, an aspect ratio "
        "between 1:3 and 3:1, a longest edge of at most 3840 and between 655,360 and 8,294,400 pixels "
        "(for example `1024x1024`, `1536x1024`, `1024x1536`, `2048x2048`, `3840x2160`)."
    )
    if not match:
        raise _invalid(rules, "size")
    width, height = int(match.group(1)), int(match.group(2))
    long_edge, short_edge = max(width, height), min(width, height)
    if (width % 16 or height % 16 or long_edge > _SIZE_MAX_EDGE or short_edge <= 0
            or long_edge > 3 * short_edge or not _SIZE_MIN_PIXELS <= width * height <= _SIZE_MAX_PIXELS):
        raise _invalid(rules, "size")
    return f"{width}x{height}"


def _prompt(body: Mapping[str, Any]) -> str:
    prompt = body.get("prompt")
    if prompt is None:
        raise _invalid("Missing required parameter: `prompt`.", "prompt", code="missing_required_parameter")
    if not isinstance(prompt, str) or not prompt.strip():
        raise _invalid("`prompt` must be a non-empty string.", "prompt")
    if len(prompt) > MAX_PROMPT_CHARS:
        raise _invalid(f"`prompt` must be at most {MAX_PROMPT_CHARS} characters.", "prompt",
                       code="string_above_max_length")
    return prompt


def _common(body: Mapping[str, Any], operation: str) -> tuple[dict[str, Any], str, int, bool, list[str]]:
    """Validates the parameters both routes share and builds the upstream
    JSON for one image. Returns (payload, model, count, stream, ignored)."""
    model = image_model_id(body.get("model"))
    payload: dict[str, Any] = {"prompt": _prompt(body), "model": model}
    for name, allowed in (("quality", IMAGE_QUALITIES), ("background", IMAGE_BACKGROUNDS),
                          ("moderation", IMAGE_MODERATIONS)):
        value = _enum(body, name, allowed)
        if value is not None:
            payload[name] = value
    size = validate_image_size(body.get("size"))
    if size is not None:
        payload["size"] = size
    count = _int_in_range(body, "n", 1, MAX_IMAGES_PER_REQUEST) or 1
    stream = body.get("stream")
    if stream is not None and not isinstance(stream, bool):
        raise _invalid("`stream` must be a boolean.", "stream")

    # Parameters the upstream does not apply (verified live). They are
    # checked like OpenAI would, then dropped and disclosed when they asked
    # for something the answer will not have.
    ignored: list[str] = []
    partial = _int_in_range(body, "partial_images", 0, 3)
    if partial:
        ignored.append("partial_images")
    output_format = _enum(body, "output_format", IMAGE_OUTPUT_FORMATS)
    if output_format is not None and output_format != "png":
        ignored.append("output_format")
    compression = _int_in_range(body, "output_compression", 0, 100)
    if compression is not None and compression != 100:
        ignored.append("output_compression")
    response_format = _enum(body, "response_format", IMAGE_RESPONSE_FORMATS)
    if response_format == "url":
        ignored.append("response_format")
    for name in ("user", "safety_identifier"):
        value = body.get(name)
        if value is not None and not isinstance(value, str):
            raise _invalid(f"`{name}` must be a string.", name)
        if value:
            ignored.append(name)
    if operation == "generation":
        if _enum(body, "style", IMAGE_STYLES) is not None:
            ignored.append("style")
    else:
        if _enum(body, "input_fidelity", IMAGE_INPUT_FIDELITIES) is not None:
            ignored.append("input_fidelity")
    known = GENERATION_PARAMETERS if operation == "generation" else EDIT_PARAMETERS
    ignored.extend(sorted(name for name, value in body.items() if name not in known and value is not None))
    return payload, model, count, bool(stream), ignored


def prepare_image_generation(body: Mapping[str, Any]) -> ImageRequest:
    """`POST /v1/images/generations` body → one validated request."""
    payload, model, count, stream, ignored = _common(body, "generation")
    return ImageRequest("generation", payload, model, count, stream, ignored)


def prepare_image_edit(body: Mapping[str, Any], images: list[EditImage], *, has_mask: bool) -> ImageRequest:
    """`POST /v1/images/edits` fields (already typed, see
    `coerce_image_form`) plus the decoded input images."""
    if has_mask:
        raise InvalidRequestError(
            "`mask` cannot be honored: the subscription backend edits whole images and dropping the mask "
            "would change the output. Describe the region to change in the prompt instead.",
            code="unsupported_parameter", param="mask",
        )
    if not images:
        raise _invalid("Missing required parameter: `image`.", "image", code="missing_required_parameter")
    if len(images) > MAX_EDIT_IMAGES:
        raise _invalid(f"`image` accepts at most {MAX_EDIT_IMAGES} images.", "image")
    payload, model, count, stream, ignored = _common(body, "edit")
    payload = {"images": [{"image_url": image.data_url} for image in images], **payload}
    return ImageRequest("edit", payload, model, count, stream, ignored)


def edit_image_from_bytes(raw: bytes, *, what: str = "image") -> EditImage:
    """An input image for an edit: PNG, JPEG or WebP (recognised by its
    bytes, never by its declared type), within the size limit."""
    if not raw:
        raise _invalid(f"The {what} is empty.", "image")
    if len(raw) > MAX_EDIT_IMAGE_BYTES:
        raise _invalid(f"The {what} is larger than {MAX_EDIT_IMAGE_BYTES // (1024 * 1024)} MiB.", "image",
                       code="request_too_large")
    media_type = sniff_image(raw)
    if media_type not in ("image/png", "image/jpeg", "image/webp"):
        raise _invalid(f"The {what} is not a PNG, JPEG or WebP file.", "image")
    return EditImage(media_type, base64.b64encode(raw).decode("ascii"))


def edit_image_from_data_url(value: str) -> EditImage:
    match = _DATA_URL.fullmatch(value.strip())
    if not match:
        raise _invalid("`image` entries must be base64 `data:` URLs or local file ids (`file_...`).", "image")
    try:
        raw = base64.b64decode("".join(match.group("data").split()), validate=True)
    except (ValueError, TypeError) as exc:
        raise _invalid("The image is not valid base64 data.", "image") from exc
    return edit_image_from_bytes(raw)


def coerce_image_form(fields: Mapping[str, str]) -> dict[str, Any]:
    """Multipart text fields → the JSON types the validators expect. A value
    that cannot be typed is refused by name, like OpenAI does."""
    typed: dict[str, Any] = {}
    for name, value in fields.items():
        if name in _INT_FIELDS:
            text = value.strip()
            if not re.fullmatch(r"-?\d+", text):
                raise _invalid(f"`{name}` must be an integer.", name)
            typed[name] = int(text)
        elif name in _BOOL_FIELDS:
            lowered = value.strip().lower()
            if lowered in ("true", "1", "yes"):
                typed[name] = True
            elif lowered in ("false", "0", "no", ""):
                typed[name] = False
            else:
                raise _invalid(f"`{name}` must be `true` or `false`.", name)
        else:
            typed[name] = value
    return typed


def unapplied_image_parameters(payload: Mapping[str, Any], response: Mapping[str, Any]) -> list[str]:
    """Parameters this call demonstrably did not apply: a requested
    `quality` or `size` that differs from what the response reports. `auto`
    asks for the service's choice and is never listed; a match is not
    listed either, even though it may be coincidence."""
    names: list[str] = []
    quality = payload.get("quality")
    reported_quality = response.get("quality")
    if (isinstance(quality, str) and quality != "auto" and isinstance(reported_quality, str)
            and reported_quality.lower() != quality.lower()):
        names.append("quality")
    size = payload.get("size")
    reported_size = response.get("size")
    if (isinstance(size, str) and size != "auto" and isinstance(reported_size, str)
            and reported_size.lower() != size.lower()):
        names.append("size")
    return names


def merge_image_responses(parts: list[dict[str, Any]]) -> dict[str, Any]:
    """The images of `n` upstream calls as one OpenAI Images response: the
    first answer's metadata, every image in order, usage summed."""
    if not parts:
        raise ValueError("merge_image_responses needs at least one response")
    first = parts[0]
    merged: dict[str, Any] = {key: value for key, value in first.items() if key != "data"}
    merged["data"] = [item for part in parts for item in (part.get("data") or []) if isinstance(item, dict)]
    if len(parts) > 1:
        usage = _sum_usage([part.get("usage") for part in parts])
        if usage is not None:
            merged["usage"] = usage
    return merged


def _sum_usage(usages: list[Any]) -> dict[str, Any] | None:
    readable = [usage for usage in usages if isinstance(usage, dict)]
    if len(readable) != len(usages):
        return None
    total: dict[str, Any] = {}
    for usage in readable:
        for key, value in usage.items():
            if isinstance(value, bool):
                continue
            if isinstance(value, int):
                total[key] = total.get(key, 0) + value
            elif isinstance(value, dict):
                bucket = total.setdefault(key, {})
                for inner_key, inner_value in value.items():
                    if isinstance(inner_value, int) and not isinstance(inner_value, bool):
                        bucket[inner_key] = bucket.get(inner_key, 0) + inner_value
    return total


def image_stream_events(response: dict[str, Any], operation: str) -> list[dict[str, Any]]:
    """OpenAI's image streaming events for a finished response: one
    `image_generation.completed` (or `image_edit.completed`) per image. No
    partial images exist on this upstream, so `partial_images` is disclosed
    as ignored and the stream carries finals only."""
    prefix = "image_generation" if operation == "generation" else "image_edit"
    events = []
    for item in response.get("data") or []:
        if not isinstance(item, dict) or not isinstance(item.get("b64_json"), str):
            continue
        events.append({
            "type": f"{prefix}.completed",
            "b64_json": item["b64_json"],
            "background": response.get("background") or "auto",
            "created_at": response.get("created"),
            "output_format": response.get("output_format") or "png",
            "quality": response.get("quality") or "auto",
            "size": response.get("size") or "auto",
            "usage": response.get("usage"),
            "safety_identifier": response.get("safety_identifier"),
        })
    return events


def summarize_image_payload(payload: Any) -> Any:
    """A traffic-log copy of an image request or response with the image
    bytes replaced by their size: a 4K PNG is megabytes of base64."""
    if isinstance(payload, dict):
        out: dict[str, Any] = {}
        for key, value in payload.items():
            if key == "b64_json" and isinstance(value, str):
                out[key] = f"<base64 image, {len(value)} chars>"
            else:
                out[key] = summarize_image_payload(value)
        return out
    if isinstance(payload, list):
        return [summarize_image_payload(item) for item in payload]
    if isinstance(payload, str) and payload.startswith("data:") and len(payload) > 256:
        return f"<data URL, {len(payload)} chars>"
    return payload
