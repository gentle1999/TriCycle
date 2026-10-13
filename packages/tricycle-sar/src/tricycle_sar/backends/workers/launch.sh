#!/usr/bin/env bash
# Environment and model execution remain in the smoke-validated worker.
set -uo pipefail
sar_launcher_python="$(command -v python3)"
sar_launcher_task="$(realpath "$1")"
sar_launcher_bundle="$("$sar_launcher_python" - "$sar_launcher_task" <<'PY'
import sys,tomllib
from pathlib import PurePosixPath
task=tomllib.load(open(sys.argv[1],"rb"))
name=task["bundle_name"]
if PurePosixPath(name).name != name or name in {".",".."}:
    raise ValueError("Unsafe SAR bundle")
print(name)
PY
)" || exit $?
export SAR_COMPACT_OUTPUT=1
bash "$PWD/common/sar_model_worker.sh" "$sar_launcher_task"
sar_launcher_code=$?
"$sar_launcher_python" "$PWD/common/sar_outputs.py" \
  --task "$sar_launcher_task" --bundle "$PWD/common/$sar_launcher_bundle" \
  --result "$PWD/results" --exit-code "$sar_launcher_code"
sar_export_code=$?
if [[ "$sar_launcher_code" != 0 ]]; then exit "$sar_launcher_code"; fi
exit "$sar_export_code"
