"""Optional OpenAI-compatible multilingual embedding support."""

from __future__ import annotations

import hashlib
import os
from typing import Any


def entity_embedding_text(entity: Any) -> str:
    parts = [
        entity.name,
        getattr(entity, "canonical_name", ""),
        " ".join(getattr(entity, "aliases", []) or []),
        entity.entity_type.value,
        entity.description,
    ]
    return "\n".join(part for part in parts if part)


def content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class EmbeddingProvider:
    def __init__(self) -> None:
        self.model = os.environ.get("EMBEDDING_MODEL", "text-embedding-3-small")
        self.dimensions = int(os.environ.get("EMBEDDING_DIMENSION", "1536"))
        self._client: Any = None

    @property
    def available(self) -> bool:
        return bool(os.environ.get("OPENAI_API_KEY"))

    def _get_client(self):
        if self._client is None:
            from langchain_openai import OpenAIEmbeddings

            kwargs: dict[str, Any] = {
                "model": self.model,
                "api_key": os.environ["OPENAI_API_KEY"],
            }
            base_url = os.environ.get("OPENAI_BASE_URL") or os.environ.get("OPENAI_API_BASE")
            if base_url:
                kwargs["base_url"] = base_url
            if self.model.startswith("text-embedding-3"):
                kwargs["dimensions"] = self.dimensions
            self._client = OpenAIEmbeddings(**kwargs)
        return self._client

    def embed_query(self, text: str) -> list[float]:
        if not self.available:
            return []
        return list(self._get_client().embed_query(text))

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        if not texts or not self.available:
            return [[] for _ in texts]
        return [list(v) for v in self._get_client().embed_documents(texts)]
