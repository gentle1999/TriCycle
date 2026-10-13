#!/usr/bin/env bash
set -euo pipefail
sar_task="$(realpath "$1")"
sar_bundle_name="$(python3 -c 'import sys,tomllib; print(tomllib.load(open(sys.argv[1],"rb"))["bundle_name"])' "$sar_task")"
bundle="$PWD/common/$sar_bundle_name"
accelerator=cuda
if [[ "$sar_bundle_name" == classical_bundle ]]; then accelerator=cpu; fi
source "$PWD/common/sar_worker_common.sh"
export PYTHONPATH="$PWD/common"
exec "$sar_base_python" "$PWD/common/sar_execution.py" --task "$task" --common "$PWD/common" --result "$result"
