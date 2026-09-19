"""Project Postgres outbox rows onto Neo4j (shared by DualBackend and Prefect workers)."""

from __future__ import annotations

import os
from datetime import timedelta
from typing import Any, Optional

from src.ontology.neo4j_backend import Neo4jBackend
from src.ontology.postgres_backend import PostgresBackend


def _lock_minutes() -> int:
    return int(os.environ.get("OUTBOX_LOCK_MINUTES", "5"))


def apply_outbox_op(neo4j: Neo4jBackend, op: str, payload: dict[str, Any]) -> None:
    if op == "upsert_entity":
        neo4j.project_entity(payload)
    elif op == "upsert_relationship":
        neo4j.project_relationship(payload)
    elif op == "delete_entity":
        neo4j.project_delete_entity(payload["id"])
    elif op == "delete_relationship":
        neo4j.project_delete_relationship(payload["id"])
    else:
        raise ValueError(f"Unknown outbox op: {op}")


class OutboxProjector:
    def __init__(self, postgres: PostgresBackend, neo4j: Neo4jBackend) -> None:
        self.postgres = postgres
        self.neo4j = neo4j

    def process_row(self, row: dict[str, Any]) -> None:
        apply_outbox_op(self.neo4j, row["op"], row["payload"])

    def project_one(self, outbox_id: int, worker_id: str) -> bool:
        """Claim and project a single outbox row. Returns False if already handled."""
        row = self.postgres.claim_outbox(outbox_id, worker_id, _lock_minutes())
        if row is None:
            return False
        try:
            self.process_row(row)
            self.postgres.mark_outbox(outbox_id, "done")
        except Exception as exc:
            self.postgres.mark_outbox(outbox_id, "pending", str(exc), increment_attempts=True)
            raise
        return True

    def drain_pending(self, limit: int = 500, worker_id: str = "drain") -> int:
        self.postgres.release_stale_processing(_lock_minutes())
        processed = 0
        for outbox_id in self.postgres.list_pending_outbox_ids(limit=limit):
            if self.project_one(outbox_id, worker_id):
                processed += 1
        return processed


def build_projector_from_env() -> OutboxProjector:
    from src.ontology.factory import _build_neo4j, _require_env

    postgres = PostgresBackend(_require_env("DATABASE_URL"), record_outbox=False)
    return OutboxProjector(postgres, _build_neo4j())
