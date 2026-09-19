"""Pure helpers for Debezium outbox CDC payloads (no Kafka client dependency)."""

from __future__ import annotations


def should_trigger_debezium_record(value: dict) -> int | None:
    """Return outbox id if this CDC event should trigger projection."""
    op = value.get("op")
    after = value.get("after") or {}
    before = value.get("before") or {}
    if not after or after.get("status") != "pending":
        return None
    outbox_id = after.get("id")
    if outbox_id is None:
        return None
    if op == "c":
        return int(outbox_id)
    if op == "u" and before.get("status") != "pending":
        return int(outbox_id)
    return None
