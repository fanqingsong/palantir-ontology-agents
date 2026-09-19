#!/bin/sh
set -e

PREFECT_API_URL="${PREFECT_API_URL:-http://prefect-server:4200/api}"
export PREFECT_API_URL

echo "Waiting for Prefect API at ${PREFECT_API_URL}..."
until python -c "
import urllib.request
urllib.request.urlopen('${PREFECT_API_URL}/health', timeout=2)
" 2>/dev/null; do
  sleep 2
done

prefect work-pool create default-process-pool --type process 2>/dev/null || true
cd /app
prefect deploy --all --prefect-file prefect.yaml

echo "Starting Prefect worker (pool=default-process-pool)..."
exec prefect worker start --pool default-process-pool
