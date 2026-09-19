"""Neo4j ontology backend for graph traversal and projection."""

from __future__ import annotations

import json
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


class Neo4jBackend:
    def __init__(self, uri: str, user: str, password: str) -> None:
        from neo4j import GraphDatabase

        self._driver = GraphDatabase.driver(uri, auth=(user, password))
        self._ensure_constraints()

    def close(self) -> None:
        self._driver.close()

    def _ensure_constraints(self) -> None:
        with self._driver.session() as session:
            session.run(
                "CREATE CONSTRAINT entity_id IF NOT EXISTS FOR (n:Entity) REQUIRE n.id IS UNIQUE"
            )

    def _entity_props(self, entity: Entity) -> dict[str, Any]:
        rec = entity_to_record(entity)
        props = {
            "id": rec["id"],
            "name": rec["name"],
            "entity_type": rec["entity_type"],
            "description": rec["description"],
            "tags": rec["tags"],
            "source": rec["source"],
            "confidence": rec["confidence"],
            "created_at": rec["created_at"] or "",
            "attributes_json": json.dumps(rec["attributes"]),
        }
        for key, value in rec["attributes"].items():
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
