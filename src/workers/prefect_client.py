"""Prefect API helpers for triggering outbox projection runs."""

from __future__ import annotations

import os
import uuid

from prefect import get_client


def _deployment_name() -> str:
    return os.environ.get(
        "PREFECT_OUTBOX_DEPLOYMENT",
        "project-outbox-row/project-outbox-row",
    )


async def _create_run_async(outbox_id: int) -> bool:
    deployment_name = _deployment_name()
    async with get_client() as client:
        deployment = await client.read_deployment_by_name(deployment_name)
        await client.create_flow_run_from_deployment(
            deployment_id=deployment.id,
            parameters={"outbox_id": outbox_id},
            idempotency_key=f"outbox-{outbox_id}-{uuid.uuid4()}",
        )
    return True


def trigger_project_outbox_run(outbox_id: int) -> bool:
    import asyncio

    try:
        asyncio.run(_create_run_async(outbox_id))
        return True
    except Exception:
        return False
