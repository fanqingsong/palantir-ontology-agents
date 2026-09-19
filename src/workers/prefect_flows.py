"""Prefect flows: project one outbox row (CDC-triggered) and scheduled reconcile."""

from __future__ import annotations

import os
import uuid

from prefect import flow, get_run_logger, task
from prefect.context import get_run_context

from src.ontology.outbox_projector import build_projector_from_env


@task(retries=2, retry_delay_seconds=10)
def project_outbox_task(outbox_id: int) -> bool:
    logger = get_run_logger()
    ctx = get_run_context()
    worker_id = f"prefect-{ctx.flow_run.id if ctx.flow_run else uuid.uuid4()}"
    projector = build_projector_from_env()
    try:
        handled = projector.project_one(outbox_id, worker_id)
        if handled:
            logger.info("Projected outbox id=%s", outbox_id)
        else:
            logger.info("Skipped outbox id=%s (not pending or already done)", outbox_id)
        return handled
    finally:
        projector.postgres.close()
        projector.neo4j.close()


@flow(name="project-outbox-row")
def project_outbox_row(outbox_id: int) -> bool:
    """Project a single outbox row to Neo4j (triggered by Kafka bridge or reconcile)."""
    return project_outbox_task(outbox_id)


@flow(name="reconcile-pending-outbox")
def reconcile_pending_outbox(batch_size: int | None = None) -> int:
    """Fallback: enqueue or process pending outbox rows (scheduled)."""
    logger = get_run_logger()
    size = batch_size or int(os.environ.get("OUTBOX_RECONCILE_BATCH", "200"))
    projector = build_projector_from_env()
    try:
        pending_ids = projector.postgres.list_pending_outbox_ids(limit=size)
        logger.info("Reconcile found %s pending outbox rows", len(pending_ids))
        triggered = 0
        use_subflows = os.environ.get("OUTBOX_RECONCILE_INLINE", "false").lower() in (
            "1",
            "true",
            "yes",
        )
        if use_subflows:
            for oid in pending_ids:
                project_outbox_row(oid)
                triggered += 1
        else:
            from src.workers.prefect_client import trigger_project_outbox_run

            for oid in pending_ids:
                if trigger_project_outbox_run(oid):
                    triggered += 1
        return triggered
    finally:
        projector.postgres.close()
        projector.neo4j.close()


if __name__ == "__main__":
    project_outbox_row(int(os.environ["OUTBOX_ID"]))
