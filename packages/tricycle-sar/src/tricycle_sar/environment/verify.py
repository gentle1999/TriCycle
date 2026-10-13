"""Import/device smoke for the current model environments."""

import importlib
import json
import sys

spec = json.loads(sys.argv[1])
checks = []
for name in spec["modules"]:
    try:
        module = importlib.import_module(name)
        checks.append(
            {
                "module": name,
                "status": "passed",
                "version": str(getattr(module, "__version__", "unknown")),
                "path": str(getattr(module, "__file__", "")),
            }
        )
    except Exception as error:
        checks.append({"module": name, "status": "failed", "error": str(error)})
try:
    from rdkit import Chem

    assert Chem.MolFromSmiles("C=C.C=CC=C") is not None
    if spec["expected_torch"]:
        torch = importlib.import_module("torch")
        assert str(torch.__version__) == spec["expected_torch"], "unexpected PyTorch version"
        if spec["require_cuda"]:
            assert torch.cuda.is_available(), "CUDA unavailable"
            tensor = torch.arange(16, dtype=torch.float32, device="cuda").reshape(4, 4)
            assert bool(torch.isfinite(tensor @ tensor).all().item())
        checks.append(
            {
                "module": "device",
                "status": "passed",
                "torch": str(torch.__version__),
                "cuda": str(torch.version.cuda),
                "device": "cuda" if spec["require_cuda"] else "cpu",
            }
        )
    else:
        checks.append({"module": "device", "status": "passed", "device": "cpu"})
except Exception as error:
    checks.append({"module": "device", "status": "failed", "error": str(error)})
forward_passed = False
backward_passed = False
if spec.get("model_probe") == "rxngraphormer":
    try:
        torch = importlib.import_module("torch")
        import torch  # pyright: ignore[reportMissingImports, reportMissingModuleSource]  # isort: skip
        from torch_scatter import scatter_add  # pyright: ignore[reportMissingImports, reportMissingModuleSource]  # isort: skip

        src = torch.tensor(
            [1.0, 2.0, 3.0, 4.0], dtype=torch.float32, device="cuda", requires_grad=True
        )
        index = torch.tensor([0, 1, 0, 1], dtype=torch.long, device="cuda")
        result = scatter_add(src, index, dim=0, dim_size=2)
        expected = torch.tensor([4.0, 6.0], dtype=torch.float32, device="cuda")
        assert torch.allclose(result, expected), "torch_scatter CUDA forward result mismatch"
        result.sum().backward()
        assert src.grad is not None and torch.equal(src.grad, torch.ones_like(src)), (
            "torch_scatter CUDA backward mismatch"
        )
        torch.cuda.synchronize()
        checks.append(
            {
                "module": "rxngraphormer_torch_scatter_cuda",
                "status": "passed",
                "device": torch.cuda.get_device_name(0),
                "forward": True,
                "backward": True,
            }
        )
        forward_passed = True
        backward_passed = True
    except Exception as error:
        checks.append(
            {"module": "rxngraphormer_torch_scatter_cuda", "status": "failed", "error": str(error)}
        )
print(
    "TRICYCLE_RESULT="
    + json.dumps(
        {
            "checks": checks,
            "passed": all(c["status"] == "passed" for c in checks),
            "model_forward_passed": forward_passed,
            "model_backward_passed": backward_passed,
        }
    )
)
