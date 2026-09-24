"""Hybrid exact, fuzzy, full-text, and vector candidate retrieval.

本文件只做召回，不排序、不消歧、不写库。
``candidate.score`` / LINKED / NEW 在 ``reranker.py`` 与 ``service.py``。

数据流::

    Mention(text, expected_types, context)
            │
            ├── embed_query(text ⏎ context ⏎ types)  → query_embedding
            │        失败则写入 degraded[]，向量保持空，后面两路照跑
            │
            ├─────────────────┬─────────────────┐
            ▼                 ▼                 ▼
       Direct            Store             Graph
       get_entity(text)  name / alias      Neo4j 全文 + 向量
       把 text 当 ID     Postgres / 内存    缺 Neo4j → degraded
            │                 │                 │
            └────────┬────────┴────────┬────────┘
                     ▼
              _merge(by_id, item)
              主键永远是 entity.id，同一实体不裂成两条
              scores[k] = max(已有, 本次)
                     │
              收尾：SequenceMatcher 补 fuzzy，类型开关写 type
                     │
              return list(by_id.values()), degraded

例子 — mention「台积电」/ organization / id=tsmc 的种子实体::

    Direct   get_entity("台积电")          miss   ← ID 是 tsmc，不是台积电
    Store    alias「台积电」→ tsmc        exact=1  fuzzy=1
    Graph    全文/向量 → tsmc, asml       vector=0.91 / 0.62
    merge    by_id["tsmc"] 各分数取 max，asml 另占一条
    收尾     「台积电」vs「TSMC」ratio≈0，但 max 不会把 alias 的 1.0 打下去
"""

from __future__ import annotations

from difflib import SequenceMatcher
from typing import Any

from src.entity_linking.embeddings import EmbeddingProvider
from src.entity_linking.models import EntityCandidate, Mention
from src.entity_linking.normalizer import normalize_surface
from src.ontology.store import OntologyStore


class HybridCandidateRetriever:
    def __init__(
        self,
        store: OntologyStore,
        embedding_provider: EmbeddingProvider | None = None,
        limit: int = 20,
    ) -> None:
        self.store = store
        self.embeddings = embedding_provider or EmbeddingProvider()
        self.limit = limit

    def retrieve(self, mention: Mention) -> tuple[list[EntityCandidate], list[str]]:
        # by_id: 全程唯一索引。键 = 本体实体 ID（tsmc），不是表面形式（台积电）。
        # degraded: 某条路挂了也继续，把原因带给 service._decide 写进 reasons。
        by_id: dict[str, EntityCandidate] = {}
        degraded: list[str] = []
        query_embedding: list[float] = []

        # ── 1. 可选向量 ──────────────────────────────────────────
        # 这不是召回。只是给 Neo4j vector.queryNodes 准备查询向量。
        # 拼法：表面形式 + 上下文 + 期望类型，缺 OPENAI_API_KEY 整段跳过。
        if self.embeddings.available:
            try:
                query_embedding = self.embeddings.embed_query(
                    f"{mention.text}\n{mention.context}\n{' '.join(mention.expected_types)}"
                )
            except Exception as exc:
                degraded.append(f"embedding unavailable: {exc}")
        else:
            degraded.append("embedding unavailable: OPENAI_API_KEY is not configured")

        # ── 2. 三路召回（语义并行，代码顺序执行；一路失败不影响另外两路）──
        #
        #   Direct ──► get_entity(mention.text)     把 text 当主键，不是按名字找
        #   Store  ──► name / canonical / alias     exact + fuzzy（pg_trgm / SequenceMatcher）
        #   Graph  ──► Neo4j fulltext + vector      fulltext + vector
        #
        # Direct ≠ 按名字查找：种子实体 id=tsmc 时，「台积电」「TSMC」都走不到这里。
        direct = self.store.get_entity(mention.text)
        if direct and (
            not mention.expected_types or direct.entity_type.value in mention.expected_types
        ):
            self._merge(by_id, {
                "entity": direct,
                "exact_score": 1.0,
                "fuzzy_score": 1.0,
            })
        for item in self.store.search_entity_candidates(
            mention.text, mention.expected_types or None, self.limit
        ):
            self._merge(by_id, item)

        try:
            graph = self.store.graph_search_backend()
            for item in graph.search_entity_candidates(
                mention.text,
                mention.expected_types or None,
                self.limit,
                query_embedding,
            ):
                self._merge(by_id, item)
        except Exception as exc:
            degraded.append(f"neo4j candidate retrieval unavailable: {exc}")
            if query_embedding:
                try:
                    for item in self.store.search_by_embedding(
                        query_embedding, mention.expected_types or None, self.limit
                    ):
                        self._merge(by_id, item)
                except Exception as embedding_exc:
                    degraded.append(
                        f"stored embedding retrieval unavailable: {embedding_exc}"
                    )

        # ── 3. 收尾：给已有候选补 fuzzy / type（不再召回新实体）──
        #
        #   Graph 命中往往没有 fuzzy。用名字+别名跟归一化 mention 做 SequenceMatcher，
        #   再和已有 fuzzy 取 max —— 已有的 alias exact=1.0 不会被汉字-拉丁 ratio≈0 打掉。
        #
        #   type 是 1/0 开关，给 rerank 用；召回阶段已经按 expected_types 过滤过。
        normalized = normalize_surface(mention.text)
        for candidate in by_id.values():
            forms = [candidate.name, *candidate.aliases]
            fuzzy = max(
                (
                    SequenceMatcher(None, normalized, normalize_surface(form)).ratio()
                    for form in forms if form
                ),
                default=0.0,
            )
            candidate.scores["fuzzy"] = max(candidate.scores.get("fuzzy", 0.0), fuzzy)
            candidate.scores["type"] = (
                1.0 if not mention.expected_types or
                candidate.entity_type in mention.expected_types else 0.0
            )
        # 不在这里算总分、不排序。下一棒：rerank() → apply_graph_coherence()。
        return list(by_id.values()), degraded

    def _merge(self, by_id: dict[str, EntityCandidate], item: dict[str, Any]) -> None:
        """把一条召回命中折进 ``by_id[entity.id]``。

        同一实体从多路进来时取 max，不会裂成两条::

            Store  tsmc {exact:1.0, fuzzy:1.0, alias:台积电}
            Graph  tsmc {fulltext:0.81, vector:0.91}
                   asml {vector:0.62}
                    │
                    ▼
            by_id["tsmc"] = {exact:1.0, fuzzy:1.0, fulltext:0.81, vector:0.91, aliases:[…, 台积电]}
            by_id["asml"] = {vector:0.62}

        分数键映射：exact_score→exact, fuzzy_score→fuzzy,
        fulltext_score→fulltext, vector_score→vector。缺的键保持 0。
        """
        entity = item["entity"]
        candidate = by_id.setdefault(
            entity.id,
            EntityCandidate(
                entity_id=entity.id,
                name=entity.name,
                entity_type=entity.entity_type.value,
                description=entity.description,
                aliases=list(getattr(entity, "aliases", []) or self.store.aliases_for(entity.id)),
            ),
        )
        aliases = list(candidate.aliases)
        if item.get("matched_alias") and item["matched_alias"] not in aliases:
            aliases.append(str(item["matched_alias"]))
        candidate.aliases = aliases
        for source, target in (
            ("exact_score", "exact"),
            ("fuzzy_score", "fuzzy"),
            ("fulltext_score", "fulltext"),
            ("vector_score", "vector"),
        ):
            candidate.scores[target] = max(
                candidate.scores.get(target, 0.0), float(item.get(source) or 0.0)
            )
