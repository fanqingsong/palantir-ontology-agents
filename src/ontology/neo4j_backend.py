"""Neo4j ontology backend for graph traversal and projection."""

from __future__ import annotations

import json
import os
import hashlib
import time
import re
from typing import Any, Optional

from src.ontology.codec import entity_to_record, record_to_entity, record_to_relationship
from src.ontology.graph_ops import (
    calculate_exposure_score,
    get_dependency_chains,
    store_to_dict,
    traverse,
)
from src.ontology.schema import (
    RelationshipType,
    Entity,
    EntityType,
    Relationship,
    entity_from_dict,
)
from src.ontology.cypher_readonly import CypherPolicy, validate_readonly_cypher

_ALLOWED_REL_TYPES = {t.value for t in RelationshipType}
_TYPE_LABELS = {
    EntityType.ORGANIZATION: "Organization",
    EntityType.PERSON: "Person",
    EntityType.LOCATION: "Location",
    EntityType.EVENT: "Event",
    EntityType.ASSET: "Asset",
    EntityType.THREAT: "Threat",
}


def _label(entity_type: EntityType) -> str:
    return _TYPE_LABELS.get(entity_type, "Entity")


_EXISTING_SCHEMA_CODES = {
    "Neo.ClientError.Schema.EquivalentSchemaRuleAlreadyExists",
    "Neo.ClientError.Schema.ConstraintAlreadyExists",
    "Neo.ClientError.Schema.IndexAlreadyExists",
}


def _run_schema(session: Any, cypher: str) -> None:
    """Create a constraint/index, ignoring races with an equivalent rule."""
    try:
        session.run(cypher)
    except Exception as exc:
        if getattr(exc, "code", None) in _EXISTING_SCHEMA_CODES:
            return
        raise


class Neo4jBackend:
    def __init__(self, uri: str, user: str, password: str) -> None:
        from neo4j import GraphDatabase

        self._driver = GraphDatabase.driver(uri, auth=(user, password))
        self._ensure_constraints()
        try:
            self.ensure_vector_index(int(os.environ.get("EMBEDDING_DIMENSION", "1536")))
        except Exception:
            # Full-text graph search remains available when vector indexes are unsupported.
            pass

    def close(self) -> None:
        self._driver.close()

    def _ensure_constraints(self) -> None:
        with self._driver.session() as session:
            _run_schema(
                session,
                "CREATE CONSTRAINT entity_id IF NOT EXISTS FOR (n:Entity) REQUIRE n.id IS UNIQUE",
            )
            _run_schema(
                session,
                """
                CREATE FULLTEXT INDEX entity_text IF NOT EXISTS
                FOR (n:Entity) ON EACH [n.name, n.canonical_name, n.aliases, n.description]
                """,
            )

    def verify_connectivity(self) -> None:
        self._driver.verify_connectivity()

    def _entity_props(self, entity: Entity) -> dict[str, Any]:
        rec = entity_to_record(entity)
        stored_attributes = dict(rec["attributes"])
        for key in ("canonical_name", "aliases", "language", "external_ids", "embedding"):
            stored_attributes.pop(key, None)
        props = {
            "id": rec["id"],
            "name": rec["name"],
            "entity_type": rec["entity_type"],
            "description": rec["description"],
            "tags": rec["tags"],
            "source": rec["source"],
            "confidence": rec["confidence"],
            "created_at": rec["created_at"] or "",
            "attributes_json": json.dumps(stored_attributes),
            "canonical_name": entity.canonical_name or entity.name,
            "aliases": list(entity.aliases),
            "language": entity.language,
            "external_ids_json": json.dumps(entity.external_ids),
        }
        if entity.embedding:
            props["embedding"] = list(entity.embedding)
        for key, value in stored_attributes.items():
            if value is None or isinstance(value, (str, int, float, bool)):
                props[key] = value
            elif isinstance(value, list) and all(isinstance(x, (str, int, float)) for x in value):
                props[key] = value
        return props

    def _node_to_entity(self, node: Any) -> Entity:
        data = dict(node)
        attributes = {}
        raw_attrs = data.pop("attributes_json", "") or "{}"
        try:
            attributes = json.loads(raw_attrs)
        except json.JSONDecodeError:
            attributes = {}
        for key in ("canonical_name", "aliases", "language", "embedding"):
            if key in data:
                attributes[key] = data[key]
        raw_external_ids = data.pop("external_ids_json", "") or "{}"
        try:
            attributes["external_ids"] = json.loads(raw_external_ids)
        except json.JSONDecodeError:
            attributes["external_ids"] = {}
        data["attributes"] = attributes
        data.update({k: v for k, v in attributes.items() if k not in data})
        return record_to_entity(data)

    def _rel_to_relationship(self, rel: Any, rel_type: Optional[str] = None) -> Relationship:
        data = dict(rel)
        data.setdefault("relationship_type", rel_type or rel.type)
        data.setdefault("source_id", rel.start_node.get("id") if hasattr(rel, "start_node") else data.get("source_id"))
        data.setdefault("target_id", rel.end_node.get("id") if hasattr(rel, "end_node") else data.get("target_id"))
        return record_to_relationship(data)

    def add_entity(self, entity: Entity) -> str:
        label = _label(entity.entity_type)
        props = self._entity_props(entity)
        query = f"""
        MERGE (n:Entity {{id: $id}})
        SET n:{label}
        SET n += $props
        """
        with self._driver.session() as session:
            session.run(query, id=entity.id, props=props)
        return entity.id

    def get_entity(self, entity_id: str) -> Optional[Entity]:
        with self._driver.session() as session:
            result = session.run("MATCH (n:Entity {id: $id}) RETURN n", id=entity_id)
            record = result.single()
        return self._node_to_entity(record["n"]) if record else None

    def get_entity_by_name(self, name: str) -> Optional[Entity]:
        with self._driver.session() as session:
            result = session.run(
                "MATCH (n:Entity) WHERE toLower(n.name) = toLower($name) RETURN n LIMIT 1",
                name=name,
            )
            record = result.single()
        return self._node_to_entity(record["n"]) if record else None

    def remove_entity(self, entity_id: str) -> bool:
        with self._driver.session() as session:
            result = session.run(
                "MATCH (n:Entity {id: $id}) DETACH DELETE n RETURN $id AS id",
                id=entity_id,
            )
            return result.single() is not None

    def add_relationship(self, relationship: Relationship) -> str:
        rel_type = relationship.relationship_type.value
        if rel_type not in _ALLOWED_REL_TYPES:
            raise ValueError(f"Unsupported relationship type: {rel_type}")
        props = {
            "id": relationship.id,
            "weight": relationship.weight,
            "bidirectional": relationship.bidirectional,
            "description": relationship.description,
            "confidence": relationship.confidence,
            "source_id": relationship.source_id,
            "target_id": relationship.target_id,
            "created_at": relationship.created_at or "",
        }
        query = f"""
        MATCH (a:Entity {{id: $source_id}}), (b:Entity {{id: $target_id}})
        MERGE (a)-[r:{rel_type} {{id: $rid}}]->(b)
        SET r += $props
        """
        with self._driver.session() as session:
            session.run(
                query,
                source_id=relationship.source_id,
                target_id=relationship.target_id,
                rid=relationship.id,
                props=props,
            )
        return relationship.id

    def get_relationship(self, rel_id: str) -> Optional[Relationship]:
        with self._driver.session() as session:
            result = session.run(
                "MATCH ()-[r]->() WHERE r.id = $id RETURN r, type(r) AS rel_type",
                id=rel_id,
            )
            record = result.single()
        if not record:
            return None
        return self._rel_to_relationship(record["r"], record["rel_type"])

    def query_by_type(self, entity_type: EntityType) -> list[Entity]:
        with self._driver.session() as session:
            result = session.run(
                "MATCH (n:Entity {entity_type: $t}) RETURN n",
                t=entity_type.value,
            )
            return [self._node_to_entity(rec["n"]) for rec in result]

    def query_by_attribute(self, key: str, value: Any) -> list[Entity]:
        matches = []
        for entity in self.all_entities():
            if entity.attributes.get(key) == value:
                matches.append(entity)
            elif hasattr(entity, key) and getattr(entity, key) == value:
                matches.append(entity)
        return matches

    def search(self, query: str) -> list[Entity]:
        q = query.lower()
        with self._driver.session() as session:
            result = session.run(
                """
                MATCH (n:Entity)
                WHERE toLower(n.name) CONTAINS $q OR toLower(n.description) CONTAINS $q
                RETURN n
                """,
                q=q,
            )
            return [self._node_to_entity(rec["n"]) for rec in result]

    def search_entity_candidates(
        self,
        query: str,
        entity_types: Optional[list[str]] = None,
        limit: int = 20,
        embedding: Optional[list[float]] = None,
    ) -> list[dict[str, Any]]:
        candidates: dict[str, dict[str, Any]] = {}
        with self._driver.session() as session:
            result = session.run(
                """
                CALL db.index.fulltext.queryNodes('entity_text', $search_query, {limit: $limit})
                YIELD node, score
                WHERE size($types) = 0 OR node.entity_type IN $types
                RETURN node, score
                """,
                search_query=_escape_fulltext_query(query),
                types=entity_types or [],
                limit=limit,
            )
            for rec in result:
                entity = self._node_to_entity(rec["node"])
                candidates[entity.id] = {
                    "entity": entity,
                    "fulltext_score": float(rec["score"] or 0),
                    "vector_score": 0.0,
                }
            if embedding:
                index_name = os.environ.get("NEO4J_VECTOR_INDEX", "entity_embedding")
                try:
                    vector_rows = session.run(
                        """
                        CALL db.index.vector.queryNodes($index, $limit, $embedding)
                        YIELD node, score
                        WHERE size($types) = 0 OR node.entity_type IN $types
                        RETURN node, score
                        """,
                        index=index_name,
                        limit=limit,
                        embedding=embedding,
                        types=entity_types or [],
                    )
                    for rec in vector_rows:
                        entity = self._node_to_entity(rec["node"])
                        item = candidates.setdefault(
                            entity.id,
                            {"entity": entity, "fulltext_score": 0.0, "vector_score": 0.0},
                        )
                        item["vector_score"] = float(rec["score"] or 0)
                except Exception:
                    pass
        return sorted(
            candidates.values(),
            key=lambda item: max(item["fulltext_score"], item["vector_score"]),
            reverse=True,
        )[:limit]

    def ensure_vector_index(self, dimensions: int) -> None:
        name = os.environ.get("NEO4J_VECTOR_INDEX", "entity_embedding")
        if not name.replace("_", "").isalnum():
            raise ValueError("Invalid Neo4j vector index name")
        dimensions = int(dimensions)
        with self._driver.session() as session:
            _run_schema(
                session,
                f"""
                CREATE VECTOR INDEX {name} IF NOT EXISTS
                FOR (n:Entity) ON n.embedding
                OPTIONS {{indexConfig: {{
                  `vector.dimensions`: {dimensions},
                  `vector.similarity_function`: 'cosine'
                }}}}
                """,
            )

    def execute_readonly(
        self,
        cypher: str,
        parameters: Optional[dict[str, Any]] = None,
        *,
        max_rows: int = 200,
        timeout_seconds: int = 10,
    ) -> dict[str, Any]:
        started = time.monotonic()
        params = validate_readonly_cypher(
            cypher,
            parameters,
            CypherPolicy(max_rows=max_rows),
        )
        from neo4j import Query

        with self._driver.session(default_access_mode="READ") as session:
            result = session.run(
                Query(cypher, timeout=timeout_seconds),
                params,
            )
            keys = list(result.keys())
            rows = []
            truncated = False
            for index, record in enumerate(result):
                if index >= max_rows:
                    truncated = True
                    break
                rows.append({
                    key: _serialize_neo4j_value(record[key])
                    for key in keys
                })
        return {
            "columns": keys,
            "rows": rows,
            "row_count": len(rows),
            "truncated": truncated,
            "audit": {
                "cypher_hash": hashlib.sha256(cypher.encode("utf-8")).hexdigest(),
                "parameter_keys": sorted(params),
                "duration_ms": round((time.monotonic() - started) * 1000, 2),
            },
        }

    def get_neighbors(
        self,
        entity_id: str,
        relationship_type: Optional[RelationshipType] = None,
        direction: str = "both",
    ) -> list[tuple[Entity, Relationship]]:
        if direction == "outgoing":
            pattern = "(n:Entity {id: $id})-[r]->(m:Entity)"
        elif direction == "incoming":
            pattern = "(n:Entity {id: $id})<-[r]-(m:Entity)"
        else:
            pattern = "(n:Entity {id: $id})-[r]-(m:Entity)"
        rel_filter = ""
        params: dict[str, Any] = {"id": entity_id}
        if relationship_type:
            rel_type = relationship_type.value
            if rel_type not in _ALLOWED_REL_TYPES:
                raise ValueError(f"Unsupported relationship type: {rel_type}")
            pattern = pattern.replace("[r]", f"[r:{rel_type}]")
        query = f"MATCH {pattern} {rel_filter} RETURN m, r, type(r) AS rel_type"
        neighbors: list[tuple[Entity, Relationship]] = []
        with self._driver.session() as session:
            for rec in session.run(query, **params):
                rel = self._rel_to_relationship(rec["r"], rec["rel_type"])
                neighbors.append((self._node_to_entity(rec["m"]), rel))
        return neighbors

    def traverse(
        self,
        entity_id: str,
        hops: int = 2,
        relationship_type: Optional[RelationshipType] = None,
    ) -> dict[str, Any]:
        return traverse(self, entity_id, hops=hops, relationship_type=relationship_type)

    def find_path(self, source_id: str, target_id: str, max_hops: int = 5) -> Optional[list[str]]:
        if source_id == target_id:
            return [source_id]
        query = """
        MATCH (a:Entity {id: $src}), (b:Entity {id: $tgt})
        MATCH p = shortestPath((a)-[*..%d]-(b))
        RETURN [n IN nodes(p) | n.id] AS path
        """ % max_hops
        with self._driver.session() as session:
            record = session.run(query, src=source_id, tgt=target_id).single()
        if not record or not record["path"]:
            return None
        return list(record["path"])

    def get_dependency_chains(
        self,
        entity_id: str,
        rel_types: Optional[list[RelationshipType]] = None,
        max_depth: int = 5,
    ) -> list[list[str]]:
        return get_dependency_chains(self, entity_id, rel_types=rel_types, max_depth=max_depth)

    def calculate_exposure_score(
        self, entity_id: str, threat_ids: Optional[list[str]] = None
    ) -> float:
        return calculate_exposure_score(self, entity_id, threat_ids=threat_ids)

    def to_dict(self) -> dict[str, Any]:
        return store_to_dict(self)

    def load_dict(self, data: dict[str, Any]) -> None:
        for payload in data.get("entities", {}).values():
            self.add_entity(entity_from_dict(payload))
        for payload in data.get("relationships", {}).values():
            self.add_relationship(Relationship.from_dict(payload))

    def project_entity(self, payload: dict[str, Any]) -> None:
        self.add_entity(entity_from_dict(payload))

    def project_relationship(self, payload: dict[str, Any]) -> None:
        self.add_relationship(Relationship.from_dict(payload))

    def project_delete_entity(self, entity_id: str) -> None:
        self.remove_entity(entity_id)

    def project_delete_relationship(self, rel_id: str) -> None:
        with self._driver.session() as session:
            session.run("MATCH ()-[r]->() WHERE r.id = $id DELETE r", id=rel_id)

    @property
    def entity_count(self) -> int:
        with self._driver.session() as session:
            rec = session.run("MATCH (n:Entity) RETURN count(n) AS n").single()
        return int(rec["n"]) if rec else 0

    @property
    def relationship_count(self) -> int:
        with self._driver.session() as session:
            rec = session.run("MATCH ()-[r]->() RETURN count(r) AS n").single()
        return int(rec["n"]) if rec else 0

    def all_entities(self) -> list[Entity]:
        with self._driver.session() as session:
            result = session.run("MATCH (n:Entity) RETURN n")
            return [self._node_to_entity(rec["n"]) for rec in result]

    def all_relationships(self) -> list[Relationship]:
        with self._driver.session() as session:
            result = session.run("MATCH ()-[r]->() RETURN r, type(r) AS rel_type")
            return [self._rel_to_relationship(rec["r"], rec["rel_type"]) for rec in result]


def _serialize_neo4j_value(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, dict):
        return {str(key): _serialize_neo4j_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_serialize_neo4j_value(item) for item in value]
    if hasattr(value, "nodes") and hasattr(value, "relationships"):
        return {
            "nodes": [_serialize_neo4j_value(node) for node in value.nodes],
            "relationships": [
                {
                    "type": rel.type,
                    **{str(k): _serialize_neo4j_value(v) for k, v in dict(rel).items()},
                }
                for rel in value.relationships
            ],
        }
    if hasattr(value, "items"):
        payload = {str(key): _serialize_neo4j_value(item) for key, item in dict(value).items()}
        labels = getattr(value, "labels", None)
        if labels:
            payload["_labels"] = sorted(labels)
        rel_type = getattr(value, "type", None)
        if rel_type:
            payload["_type"] = rel_type
        return payload
    if hasattr(value, "iso_format"):
        return value.iso_format()
    return str(value)


def _escape_fulltext_query(value: str) -> str:
    tokens = [
        token for token in re.split(r"\s+", value.strip())
        if token
    ]
    escaped = [
        re.sub(r'([+\-!(){}\[\]^"~*?:\\/]|&&|\|\|)', r"\\\1", token)
        for token in tokens
    ]
    return " ".join(escaped) or '""'
