"""Read-only Bohr SAR environment inventory; never installs or trains models."""
from __future__ import annotations

import argparse
import glob
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

MODELS = {
    "chemprop_cgr": ["chemprop", "lightning", "rdkit"],
    "chemprop_multicomponent": ["chemprop", "lightning", "rdkit"],
    "t5chem": ["t5chem", "transformers", "accelerate", "rdkit"],
    "rxngraphormer": ["rxngraphormer", "torch_geometric", "safetensors", "rdkit"],
    "reactiont5": ["transformers", "tokenizers", "sentencepiece", "rdkit"],
}
PROBE = r'''
import importlib,json,sys
name=sys.argv[1]
try:
 m=importlib.import_module(name)
 result={"status":"passed","version":str(getattr(m,"__version__","unknown")),"path":str(getattr(m,"__file__",""))}
 if name=="torch":
  result.update(cuda_available=m.cuda.is_available(),cuda_version=str(m.version.cuda))
  if m.cuda.is_available():
   x=m.arange(16,device="cuda",dtype=m.float32).reshape(4,4)
   result["cuda_matmul_passed"]=bool(m.isfinite(x@x).all().item())
   result["gpu"]=m.cuda.get_device_name(0)
 elif name=="rdkit":
  from rdkit import Chem
  assert Chem.MolFromSmiles("C=C.C=CC=C") is not None
 elif name=="transformers":
  from transformers import T5ForConditionalGeneration,T5Config,AutoTokenizer
  result["t5_api_import"]="passed"
 print("TRICYCLE_RESULT="+json.dumps(result))
except Exception as e:
 print("TRICYCLE_RESULT="+json.dumps({"status":"failed","error":type(e).__name__+": "+str(e)}))
'''


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out-dir', type=Path, default=Path('results'))
    args = parser.parse_args()
    output = args.out_dir
    output.mkdir(parents=True, exist_ok=True)
    with (output / 'nvidia-smi.txt').open('w') as handle:
        try:
            subprocess.run(['nvidia-smi'], stdout=handle, stderr=subprocess.STDOUT, timeout=20, check=False)
        except (OSError, subprocess.TimeoutExpired) as error:
            handle.write(str(error) + '\n')
    candidates = [sys.executable, shutil.which('python'), shutil.which('python3')]
    for pattern in ('/opt/*/bin/python', '/opt/conda/envs/*/bin/python',
                    '/opt/*/.venv/bin/python', '/root/*/bin/python'):
        candidates.extend(glob.glob(pattern))
    # Do not resolve venv symlinks: the symlink location selects site-packages.
    interpreters = list(dict.fromkeys(str(p) for p in candidates if p and Path(p).is_file()))
    results = []
    modules = sorted({'torch', 'numpy', 'pandas', 'sklearn'} | {m for v in MODELS.values() for m in v})
    for python in interpreters:
        for module in modules:
            record = {'python': python, 'module': module}
            try:
                response = subprocess.run([python, '-c', PROBE, module], capture_output=True,
                                          text=True, timeout=45, env=os.environ.copy())
                lines = [line[len('TRICYCLE_RESULT='):] for line in response.stdout.splitlines()
                         if line.startswith('TRICYCLE_RESULT=')]
                record.update(json.loads(lines[-1]) if lines else {
                    'status': 'failed', 'error': (response.stderr or response.stdout)[-2000:]})
            except (subprocess.TimeoutExpired, OSError) as error:
                record.update(status='failed', error=str(error))
            results.append(record)
            print(python, module, record['status'], flush=True)
    lines = ['phase = "environment_inventory"', 'training_started = false',
             'model_forward_tested = false', 'benchmark_ready = false']
    for record in results:
        lines.append('\n[[checks]]')
        for key, value in record.items():
            lines.append(f'{key} = ' + (str(value).lower() if isinstance(value, bool) else json.dumps(str(value))))
    for model, required in MODELS.items():
        matching = [python for python in interpreters if all(any(
            r['python'] == python and r['module'] == module and r['status'] == 'passed'
            for r in results) for module in required)]
        lines.extend(['\n[[models]]', f'name = "{model}"',
                      'import_candidates = ' + json.dumps(matching),
                      'status = "requires_model_forward_and_asset_check"' if matching else 'status = "missing_dependencies"'])
    assets = ['/opt/rxngraphormer', '/opt/t5chem', '/opt/chemprop']
    for path in assets:
        lines.extend(['\n[[source_paths]]', 'path = ' + json.dumps(path),
                      'exists = ' + str(Path(path).exists()).lower()])
    (output / 'environment.toml').write_text('\n'.join(lines) + '\n')
    # Inventory completion is not a model readiness pass; inspect per-model states.
    print('Inventory complete; no model readiness claim. See results/environment.toml.')


if __name__ == '__main__':
    main()
