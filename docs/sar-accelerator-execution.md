# SAR worker accelerator execution

Execution scripts expose environment installation, model loading, and training as observable, bounded stages. GPU allocation or `torch.cuda.is_available() == True` does not prove GPU model execution. Model smoke/training results belong to individual runs and cannot be replaced by base-image checks.

## Entry points

Current models share package-local `backends/workers/unified.sh`; `execution.py` selects the model runner. Environment recipes, runtime files, and actual machine types are bound through the [current backend catalog](../packages/tricycle-sar/src/tricycle_sar/backends/catalog.json). There are no model-named shell forwarding entry points or shell forwarding scripts under `tools/`.

| Model | Package runner | Resources |
| --- | --- | --- |
| Fingerprint RF / Extra Trees / Ridge | `classical_runner.py` | Bohr CPU, CUDA explicitly hidden |
| UAM / MolCLR | `molecular_runner.py` | Bohr GPU, CUDA required in the actual model process |
| Chemprop CGR / multicomponent, T5Chem | `backends/native_runner.py` | Bohr GPU, training CLI in its dedicated Python with CUDA checks |
| ReactionT5, RxnResid | `backends/complete_runner.py` | Bohr GPU; device evidence archived with the run |
| RXNGraphormer | `backends/complete_runner.py` | Fixed internal SSH/Docker executor |

## Execution flow

1. Local preparation validates the TOML actually written, input hashes, and machine type. Uploads include execution support code and SHA-256 hashes; old runs are not rewritten.
2. Workers validate uploaded runtime hashes and clear proxies and external Python import paths. CPU workers force `CUDA_VISIBLE_DEVICES` to empty.
3. `sar_stage.py` supervises installation/training subprocesses, streams logs, and records stage and GPU/process memory every 30 seconds. GPU stages first check `nvidia-smi`.
4. The model-specific interpreter runs `sar_accelerator_exec.py`. It observes device and training events while preserving the original model, split, loss, and optimizer.
5. Actual parameterized modules have their parameter and input-tensor devices checked. Backpropagation loss tensors must be on the corresponding device. The first optimizer update requires finite, nonzero gradients on that same device.
6. On completion, GPU training processes require real forward, backward, and optimizer-update records. Smoke runs do not require optimizer updates; inference does not require backward passes. Missing records or device mismatches fail immediately.

Reports include PID, PyTorch version, device name, allocated CUDA memory, runtime hashes, first training events, and optimizer-step count. GPU utilization samples are diagnostic only. Success does not depend on an instantaneous utilization threshold, and small models are not expected to maintain full utilization.

## Timeouts and recovery

- Shared-executor setup stages have a 2400-second total limit. Setup commands inside the environment engine share an 1800-second cumulative budget. Each pip command is limited to 450 seconds; other subcommands to 900 seconds.
- Transient network failures permit at most three command attempts, with 5-second and 15-second backoffs. Deterministic errors are not retried. See [SAR setup and outputs](../packages/tricycle-sar/README.md#compact-job-outputs).
- A real backward event must appear within 600 seconds of training start. A later 600-second interval without training progress also fails.
- Each CPU fold is limited to 7200 seconds; each GPU fold to 86400 seconds. The Bohr request's overall timeout remains an additional limit.
- Timeouts/signals propagate to subprocess groups. On exit, preserve generated model files and reports where possible. Do not overwrite old verification files or resubmit automatically.

Installation failures, nonzero exits, missing training events, and device mismatches never produce success. CPU models do not load GPU checkers or enable GPUs merely because a deep-learning image includes CUDA libraries.

## Validation records

Execution-layer unit tests cover CPU optimizer updates, unavailable CUDA, model-device mismatches, missing training events, stale evidence reuse, nonzero exits, subprocess-group timeout cleanup, TOML, and task-bundle generation. Model smoke and formal training status are experiment records: verify the relevant report and provenance. This document defines execution constraints only.

Environment configuration selects package sources. The default mirror may fall back to official PyPI after recoverable network errors or an empty version list; explicitly selected sources remain unchanged. Installation attempts and CUDA preflight evidence go to `worker.log`. CUDA failures set the next action to `inspect_error`; error text alone must not trigger node changes or Torch/CUDA changes.

Python, PyTorch, CUDA, and dependency versions come from the fixed model recipe and each task's provenance. Do not treat a shared worker image's version as every model's environment.
