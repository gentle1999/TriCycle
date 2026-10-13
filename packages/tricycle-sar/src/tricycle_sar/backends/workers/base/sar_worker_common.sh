#!/usr/bin/env bash
# Sourced by each model worker; contains no submission or credential access.
set -euo pipefail
unset HTTP_PROXY HTTPS_PROXY ALL_PROXY http_proxy https_proxy all_proxy PYTHONPATH PYTHONHOME PIP_PROXY pip_proxy
sar_base_python="$(command -v python3)"
export NO_PROXY='*' no_proxy='*' PIP_CONFIG_FILE=/dev/null PYTHONNOUSERSITE=1 PYTHONUNBUFFERED=1
export OMP_NUM_THREADS=2 OPENBLAS_NUM_THREADS=2 MKL_NUM_THREADS=2 TOKENIZERS_PARALLELISM=false
export PIP_DEFAULT_TIMEOUT=30 PIP_RETRIES=2
task="$(realpath "$1")"
result="$PWD/results"
stage_runner="$PWD/common/sar_stage.py"
mkdir -p "$result"
# Validate the exact uploaded execution support before creating environments.
python3 - "$bundle" <<'PYCODE'
import sys,tomllib
sys.path.insert(0,str(__import__("pathlib").Path(sys.argv[1]).parent))
from sar_runtime.io import file_sha256
from pathlib import Path
root=Path(sys.argv[1])
manifest=tomllib.loads((root/'accelerator-runtime.toml').read_text())
for item in manifest['files']:
    path=Path(item['path'])
    if path.is_absolute() or '..' in path.parts:
        raise ValueError('Invalid runtime file path')
    if file_sha256(root/path)!=item['sha256']:
        raise ValueError(f'Runtime hash mismatch: {path}')
print('[stage] uploaded runtime hashes verified',flush=True)
PYCODE
stage() {
  local name="$1" seconds="$2"
  shift 2
  "$sar_base_python" "$stage_runner" --name "$name" --output "$result/stages" --timeout "$seconds" \
    --accelerator "$accelerator" -- "$@"
}
preserve_outputs() {
  # The package launcher exports predictions/metrics; avoid copying checkpoints by default.
  if [[ "${SAR_COMPACT_OUTPUT:-0}" == 1 ]]; then return; fi
  local directory
  for directory in smoke-results model-runs; do
    if [[ -d "$bundle/$directory" ]]; then
      mkdir -p "$result/$directory"
      cp -R "$bundle/$directory/." "$result/$directory/"
    fi
  done
}
trap preserve_outputs EXIT
