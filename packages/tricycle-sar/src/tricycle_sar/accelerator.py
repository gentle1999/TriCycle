"""Runtime accelerator contract; imports Torch only during explicit execution."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

try:
    from sar_runtime.io import file_sha256 as core_file_sha256  # pyright: ignore[reportMissingImports, reportMissingModuleSource]  # isort: skip
except ImportError:
    from tricycle_core.io import file_sha256 as core_file_sha256

try:
    from sar_runtime.toml import dump_toml  # pyright: ignore[reportMissingImports]  # isort: skip
except ImportError:
    from tricycle_core.toml import dump_toml


def write_report(path: Path, values: dict) -> None:
    dump_toml(path, values)


class AcceleratorGuard:
    """Observe actual model operations; never move tensors or alter optimization."""

    def __init__(self, expected: str, report: Path, *, training: bool, optimizer: bool = True):
        import torch  # pyright: ignore[reportMissingImports, reportMissingModuleSource]  # isort: skip

        if report.exists():
            raise FileExistsError(report)
        if expected not in {"cpu", "cuda"}:
            raise ValueError("expected accelerator must be cpu or cuda")
        if expected == "cuda":
            if not torch.cuda.is_available():
                raise RuntimeError("CUDA required: CPU fallback is forbidden")
            torch.cuda.set_device(0)
        self.torch = torch
        self.expected = expected
        self.report = report
        self.training = training
        self.optimizer = optimizer
        self.started = time.monotonic()
        self.state: dict[str, object] = dict(
            pid=os.getpid(),
            expected_accelerator=expected,
            torch=str(torch.__version__),
            guard_sha256=core_file_sha256(Path(__file__)),
            phase="device_checked",
            forward_checked=False,
            backward_checked=False,
            optimizer_checked=False,
            completed=False,
            device_name=torch.cuda.get_device_name(0) if expected == "cuda" else "CPU",
        )
        self.last_progress = 0.0
        self.steps = 0
        self.handles = []
        self.publish()

    def publish(self):
        self.state["elapsed_seconds"] = round(time.monotonic() - self.started, 3)
        if self.expected == "cuda":
            self.state["cuda_memory_allocated_bytes"] = self.torch.cuda.memory_allocated(0)
        write_report(self.report, self.state)
        print("[accelerator] " + json.dumps(self.state), flush=True)

    def tensor(self, tensor):
        if tensor.device.type != self.expected:
            raise RuntimeError(
                f"Accelerator mismatch: expected {self.expected}, got {tensor.device}"
            )
        if self.expected == "cuda" and tensor.device.index != 0:
            raise RuntimeError(f"Unexpected CUDA device: {tensor.device}")

    def tensors(self, value):
        if isinstance(value, self.torch.Tensor):
            yield value
        elif isinstance(value, dict):
            for item in value.values():
                yield from self.tensors(item)
        elif isinstance(value, (list, tuple)):
            for item in value:
                yield from self.tensors(item)
        elif hasattr(value, "to_dict"):  # PyG Data / Batch
            yield from self.tensors(value.to_dict())

    def inputs(self, value):
        for tensor in self.tensors(value):
            self.tensor(tensor)

    def __enter__(self):
        torch = self.torch
        guard = self

        def forward(module, inputs):
            # Parameter-bearing modules cannot silently run on the wrong device.
            params = list(module.parameters(recurse=False))
            for param in params:
                guard.tensor(param)
            if params:
                guard.inputs(inputs)
            if params and not guard.state["forward_checked"]:
                guard.state.update(forward_checked=True, phase="model_forward")
                guard.publish()

        self.handles.append(torch.nn.modules.module.register_module_forward_pre_hook(forward))
        self.original_backward = torch.autograd.backward
        self.original_init = torch.optim.Optimizer.__init__

        def backward(tensors, *args, **kwargs):
            guard.inputs(tensors)
            result = guard.original_backward(tensors, *args, **kwargs)
            if not guard.state["backward_checked"]:
                if guard.expected == "cuda":
                    torch.cuda.synchronize(0)
                guard.state.update(backward_checked=True, phase="model_backward")
                guard.publish()
            return result

        def step(optimizer, args, kwargs):
            guard.steps += 1
            if guard.state["optimizer_checked"]:
                if time.monotonic() - guard.last_progress >= 30:
                    guard.state["optimizer_steps"] = guard.steps
                    guard.publish()
                    guard.last_progress = time.monotonic()
                return
            gradients = [
                p.grad
                for group in optimizer.param_groups
                for p in group["params"]
                if p.grad is not None
            ]
            if not gradients:
                raise RuntimeError("Optimizer step had no gradients")
            for grad in gradients:
                guard.tensor(grad)
            if not all(bool(torch.isfinite(grad).all()) for grad in gradients):
                raise RuntimeError("Optimizer gradients are nonfinite")
            if not any(bool((grad != 0).any()) for grad in gradients):
                raise RuntimeError("Optimizer gradients are all zero")
            if guard.expected == "cuda":
                torch.cuda.synchronize(0)
            guard.last_progress = time.monotonic()
            guard.state.update(
                optimizer_checked=True, phase="optimizer_step", optimizer_steps=guard.steps
            )
            guard.publish()

        def initialize(optimizer, *args, **kwargs):
            guard.original_init(optimizer, *args, **kwargs)
            guard.handles.append(optimizer.register_step_post_hook(step))

        torch.autograd.backward = backward
        torch.optim.Optimizer.__init__ = initialize
        return self

    def __exit__(self, exc_type, exc, traceback):
        self.torch.autograd.backward = self.original_backward
        self.torch.optim.Optimizer.__init__ = self.original_init
        for handle in self.handles:
            handle.remove()
        if exc_type is not None:
            self.state.update(phase="failed", error=str(exc))
            self.publish()
            return False
        required = ["forward_checked"]
        if self.training:
            required.append("backward_checked")
            if self.optimizer:
                required.append("optimizer_checked")
        missing = [key for key in required if not self.state[key]]
        if missing:
            self.state.update(
                phase="failed", error="Missing actual model evidence: " + ", ".join(missing)
            )
            self.publish()
            raise RuntimeError(self.state["error"])
        self.state.update(phase="completed", completed=True)
        self.publish()
