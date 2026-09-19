"""Consume Debezium outbox CDC events and trigger Prefect projection runs."""

from __future__ import annotations

import json
import logging
import os
import signal
import sys
import time

from confluent_kafka import Consumer, KafkaError

from src.workers.kafka_cdc import should_trigger_debezium_record
from src.workers.prefect_client import trigger_project_outbox_run

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    stream=sys.stdout,
    force=True,
)
logger = logging.getLogger("outbox-kafka-bridge")

_shutdown = False


def _handle_signal(*_args: object) -> None:
    global _shutdown
    _shutdown = True


def _env(name: str, default: str) -> str:
    return os.environ.get(name, default).strip()


def parse_message(raw: bytes | None) -> dict | None:
    if not raw:
        return None
    try:
        return json.loads(raw.decode("utf-8"))
    except json.JSONDecodeError:
        return None


def run_bridge() -> None:
    topic = _env("KAFKA_OUTBOX_TOPIC", "ontology.public.outbox")
    bootstrap = _env("KAFKA_BOOTSTRAP_SERVERS", "kafka:9092")
    group = _env("KAFKA_CONSUMER_GROUP", "outbox-prefect-bridge")

    consumer = Consumer(
        {
            "bootstrap.servers": bootstrap,
            "group.id": group,
            "auto.offset.reset": "earliest",
            "enable.auto.commit": True,
        }
    )
    consumer.subscribe([topic])
    logger.info("Bridge subscribed to %s (brokers=%s)", topic, bootstrap)

    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    while not _shutdown:
        msg = consumer.poll(1.0)
        if msg is None:
            continue
        if msg.error():
            if msg.error().code() == KafkaError._PARTITION_EOF:
                continue
            logger.warning("Kafka error: %s", msg.error())
            continue
        payload = parse_message(msg.value())
        if not payload:
            continue
        outbox_id = should_trigger_debezium_record(payload)
        if outbox_id is None:
            continue
        logger.info("CDC outbox id=%s op=%s -> Prefect", outbox_id, payload.get("op"))
        ok = trigger_project_outbox_run(outbox_id)
        if not ok:
            logger.error("Failed to trigger Prefect for outbox id=%s", outbox_id)
            time.sleep(2)
        else:
            logger.info("Triggered Prefect for outbox id=%s", outbox_id)

    consumer.close()
    logger.info("Bridge stopped")


def main() -> None:
    retries = int(_env("KAFKA_BRIDGE_STARTUP_RETRIES", "60"))
    for attempt in range(retries):
        try:
            run_bridge()
            return
        except Exception as exc:
            logger.warning("Bridge attempt %s failed: %s", attempt + 1, exc)
            time.sleep(3)
    logger.error("Bridge failed after %s attempts", retries)
    sys.exit(1)


if __name__ == "__main__":
    main()
