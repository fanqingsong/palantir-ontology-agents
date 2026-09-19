"""Minimal helpers for OpenAI-compatible LLM calls."""

from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path
from typing import Any

_PROMPTS_DIR = Path(__file__).resolve().parents[2] / "config" / "prompts"
_BULLET_PREFIX = re.compile(r"^(\d+[.)]\s+|[-*•]\s+)")


class MissingOpenAIKeyError(RuntimeError):
    """Raised when analysis is requested without an OpenAI-compatible API key."""


def load_prompt(name: str) -> str:
    """Load a system prompt from config/prompts/<name>.md."""
    return _load_prompt_cached(name)


@lru_cache(maxsize=16)
def _load_prompt_cached(name: str) -> str:
    path = _PROMPTS_DIR / f"{name}.md"
    return path.read_text(encoding="utf-8")


def invoke_llm(llm: Any, system: str, user: str) -> str:
    """Invoke a chat model and return plain text."""
    from langchain_core.messages import HumanMessage, SystemMessage

    response = llm.invoke([SystemMessage(content=system), HumanMessage(content=user)])
    return _message_text(response)


def bullet_lines(text: str, limit: int = 8) -> list[str]:
    """Split model output into finding-style lines."""
    lines: list[str] = []
    for raw in (text or "").splitlines():
        line = _BULLET_PREFIX.sub("", raw.strip()).strip()
        if len(line) > 20:
            lines.append(line)
        if len(lines) >= limit:
            break
    if not lines:
        compact = (text or "").strip()
        if len(compact) > 20:
            return [compact]
    return lines


def _message_text(message: Any) -> str:
    content = getattr(message, "content", message)
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict) and block.get("text"):
                parts.append(str(block["text"]))
        return "\n".join(parts).strip()
    return str(content).strip()
