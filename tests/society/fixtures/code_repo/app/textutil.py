"""Small text utilities used by fixture task handlers."""

from __future__ import annotations

import re

_TRUE = {"true", "1"}
_FALSE = {"false", "0", "no", "off", "n", ""}


def slugify(text: str, max_len: int = 48) -> str:
    """Lower-case, dash-separated slug; never empty."""
    slug = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
    return (slug or "item")[:max_len]


def parse_bool(value) -> bool:
    """Parse a boolean-ish task input. Unknown values are False."""
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in _TRUE:
        return True
    if text in _FALSE:
        return False
    return False


def normalize_whitespace(text: str) -> str:
    return " ".join((text or "").split())
