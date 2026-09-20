"""Language-agnostic surface-form normalization."""

from __future__ import annotations

import re
import unicodedata

_SPACE = re.compile(r"\s+")
_PUNCT = re.compile(r"[^\w\u3400-\u9fff]+", re.UNICODE)


def normalize_surface(text: str) -> str:
    value = unicodedata.normalize("NFKC", text or "").casefold().strip()
    value = _PUNCT.sub(" ", value)
    return _SPACE.sub(" ", value).strip()


def compact_surface(text: str) -> str:
    return normalize_surface(text).replace(" ", "")
