from __future__ import annotations

import os
import sys
from typing import TextIO


def supports_color(stream: TextIO | None = None) -> bool:
    target = stream or sys.stdout
    if os.environ.get("NO_COLOR"):
        return False
    if os.environ.get("CLICOLOR") == "0":
        return False
    if os.environ.get("TERM") == "dumb":
        return False
    return bool(getattr(target, "isatty", lambda: False)())


def style(text: str, *codes: str, stream: TextIO | None = None) -> str:
    if not supports_color(stream):
        return text
    joined = ";".join(codes)
    return f"\033[{joined}m{text}\033[0m"


def accent(text: str, stream: TextIO | None = None) -> str:
    return style(text, "1", "36", stream=stream)


def bold(text: str, stream: TextIO | None = None) -> str:
    return style(text, "1", stream=stream)


def good(text: str, stream: TextIO | None = None) -> str:
    return style(text, "32", stream=stream)


def warn(text: str, stream: TextIO | None = None) -> str:
    return style(text, "33", stream=stream)


def bad(text: str, stream: TextIO | None = None) -> str:
    return style(text, "31", stream=stream)


def muted(text: str, stream: TextIO | None = None) -> str:
    return style(text, "2", stream=stream)
