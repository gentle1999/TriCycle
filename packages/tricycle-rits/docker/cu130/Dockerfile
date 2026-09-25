# Use the CUDA 12.8/Python 3.12 base already mirrored on the internal host,
# then replace its Torch wheel with the official CUDA 13.0 build. The cu130
# wheel supplies its matching CUDA Python runtime libraries.
FROM tricycle/base:torch-cu128-py312@sha256:b00824ecdd021686326f23dc261a54f3c5d8e485cedafcbad44f89dff1ed9c48

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONUNBUFFERED=1 \
    RITS_DEPS_IN_IMAGE=1

WORKDIR /workspace/job
ENTRYPOINT []

COPY requirements.txt /tmp/rits-requirements.txt

RUN python3.12 -m ensurepip --upgrade --default-pip \
    && python3.12 -m pip install --no-cache-dir --upgrade pip \
    && python3.12 -m pip install --no-cache-dir --upgrade \
        --index-url https://download.pytorch.org/whl/cu130 \
        --extra-index-url https://pypi.org/simple \
        'torch==2.12.0+cu130' \
    && python3.12 -m pip install --no-cache-dir -r /tmp/rits-requirements.txt \
    && python3.12 -m pip check \
    && python3.12 - <<'PY'
import torch
import torch_geometric
import torch_scatter
import torch_sparse
import pyg_lib
import lightning
import hydra
import rdkit
import tensorboard

assert torch.__version__.split("+")[0].startswith("2.12."), torch.__version__
assert torch.version.cuda == "13.0", torch.version.cuda
print(
    "RitS image dependencies ready:",
    f"torch={torch.__version__}",
    f"cuda={torch.version.cuda}",
    f"torch_geometric={torch_geometric.__version__}",
    f"torch_scatter={torch_scatter.__version__}",
    f"torch_sparse={torch_sparse.__version__}",
    f"pyg_lib={pyg_lib.__version__}",
    f"lightning={lightning.__version__}",
)
PY
