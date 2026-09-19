"""Create the process-shared ontology store from environment configuration."""

from __future__ import annotations

import os
from contextvars import ContextVar
from typing import Optional

from src.ontology.store import OntologyStore

_store_var: ContextVar[Optional[OntologyStore]] = ContextVar("ontology_store", default=None)


def backend_mode() -> str:
    return os.environ.get("ONTOLOGY_BACKEND", "memory").strip().lower()


def create_store() -> OntologyStore:
    mode = backend_mode()
    if mode == "memory":
        return OntologyStore()
    if mode == "postgres":
        from src.ontology.postgres_backend import PostgresBackend

        return OntologyStore(PostgresBackend(_require_env("DATABASE_URL"), record_outbox=False))
    if mode == "neo4j":
        from src.ontology.neo4j_backend import Neo4jBackend

        return OntologyStore(_build_neo4j())
    if mode == "dual":
        from src.ontology.dual import DualBackend
        from src.ontology.postgres_backend import PostgresBackend

        postgres = PostgresBackend(_require_env("DATABASE_URL"), record_outbox=True)
        return OntologyStore(DualBackend(postgres, _build_neo4j()))
    raise ValueError(f"Unsupported ONTOLOGY_BACKEND: {mode}")


def bootstrap_if_empty(store: OntologyStore) -> OntologyStore:
    if store.entity_count == 0:
        from src.ontology.loader import load_sample_data

        load_sample_data(store)
    return store


def get_shared_store() -> OntologyStore:
    store = _store_var.get()
    if store is None:
        store = bootstrap_if_empty(create_store())
        _store_var.set(store)
    return store


def reset_shared_store() -> None:
    store = _store_var.get()
    backend = getattr(store, "_backend", None) if store else None
    closer = getattr(backend, "close", None)
    if callable(closer):
        try:
            closer()
        except Exception:
            pass
    _store_var.set(None)


def _build_neo4j():
    from src.ontology.neo4j_backend import Neo4jBackend

    return Neo4jBackend(
        uri=_require_env("NEO4J_URI"),
        user=os.environ.get("NEO4J_USER", "neo4j"),
        password=_require_env("NEO4J_PASSWORD"),
    )


def _require_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"{name} is required when ONTOLOGY_BACKEND={backend_mode()}")
    return value
