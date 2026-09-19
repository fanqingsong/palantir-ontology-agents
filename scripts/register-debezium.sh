#!/bin/sh
set -e

CONNECT_URL="${KAFKA_CONNECT_URL:-http://kafka-connect:8083}"
CONFIG_PATH="${DEBEZIUM_CONNECTOR_CONFIG:-/config/postgres-outbox.json}"

echo "Waiting for Kafka Connect at ${CONNECT_URL}..."
until curl -sf "${CONNECT_URL}/connectors" >/dev/null; do
  sleep 3
done

echo "Registering Debezium connector..."
status=$(curl -s -o /tmp/debezium-resp.txt -w "%{http_code}" \
  -X POST "${CONNECT_URL}/connectors" \
  -H "Content-Type: application/json" \
  -d @"${CONFIG_PATH}")

if [ "$status" = "201" ] || [ "$status" = "409" ]; then
  echo "Connector ontology-outbox ready (HTTP ${status})"
  exit 0
fi

echo "Failed to register connector (HTTP ${status}):"
cat /tmp/debezium-resp.txt
exit 1
