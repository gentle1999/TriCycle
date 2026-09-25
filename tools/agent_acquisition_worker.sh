#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"
request_path="${1:-agent-acquisition-worker.toml}"
exec python3.12 agent_acquisition_worker.py --request "$request_path"
