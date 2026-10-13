"""Real GPU forward/backward smoke for remaining pinned models; no optimizer update."""

from __future__ import annotations

import argparse
import csv
import hashlib
import importlib.util
import json
import sys
from pathlib import Path
from typing import Any


def rxnresid(rows: Any) -> None:
    import torch  # pyright: ignore[reportMissingImports, reportMissingModuleSource]  # isort: skip
    from rxnresid.config import load_project_config  # pyright: ignore[reportMissingImports, reportMissingModuleSource]  # isort: skip
    from rxnresid.data.collate import collate_reaction_groups  # pyright: ignore[reportMissingImports, reportMissingModuleSource]  # isort: skip
    from rxnresid.data.dataset import ReactionGroupDataset  # pyright: ignore[reportMissingImports, reportMissingModuleSource]  # isort: skip
    from rxnresid.models.build import build_model  # pyright: ignore[reportMissingImports, reportMissingModuleSource]  # isort: skip

    source = Path(__import__("os").environ["SAR_MODEL_SOURCE"])
    config = load_project_config(source / "configs/rxnresid.yaml")
    groups = []
    for row in rows:
        pair = (row["ene_id"], row["diene_id"])
        if pair not in groups:
            groups.append(pair)
        if len(groups) == 2:
            break
    selected = [r for r in rows if (r["ene_id"], r["diene_id"]) in groups]
    dataset = ReactionGroupDataset(
        rows,
        route_id_column="prod_id",
        path_id_column="path_id",
        include_stereo=True,
        cache_dir=None,
    )
    if {row.path_id for row in dataset.rows} != {row["path_id"] for row in rows}:
        raise ValueError("full frozen preprocessing changed path identities")
    print(
        f"Full frozen preprocessing passed: {len(dataset.rows)} paths, {len(dataset)} groups",
        flush=True,
    )
    dataset = ReactionGroupDataset(
        selected,
        route_id_column="prod_id",
        path_id_column="path_id",
        include_stereo=True,
        cache_dir=None,
        route_id_vocabulary=dataset.route_id_vocabulary,
    )
    batch = collate_reaction_groups([dataset[i] for i in range(len(dataset))]).to("cuda:0")
    model, _ = build_model(config.model)
    model.cuda().train()
    output = model(batch)
    loss = (output.prediction - batch.energies).square().mean()
    loss = loss + output.predictive_variance.mean() * 1e-4
    if not torch.isfinite(loss):
        raise ValueError("Nonfinite RxnResid smoke loss")
    loss.backward()
    verify_gradients(model)
    print("RxnResid full production architecture backward passed", flush=True)


def verify_gradients(model: Any) -> None:
    import torch  # pyright: ignore[reportMissingImports, reportMissingModuleSource]  # isort: skip

    gradients = [p.grad for p in model.parameters() if p.grad is not None]
    if not gradients or not all(g.is_cuda and bool(torch.isfinite(g).all()) for g in gradients):
        raise ValueError("Missing, nonfinite or non-CUDA gradients")
    if not any(bool((g != 0).any()) for g in gradients):
        raise ValueError("Zero gradients")


def rxngraphormer(rows: Any, assets: Any) -> None:
    import torch  # pyright: ignore[reportMissingImports, reportMissingModuleSource]  # isort: skip
    from rxngraphormer.data import RXNDataset  # pyright: ignore[reportMissingImports, reportMissingModuleSource]  # isort: skip
    from rxngraphormer.model import RXNGClassifier, RXNGRegressor  # pyright: ignore[reportMissingImports, reportMissingModuleSource]  # isort: skip
    from torch_geometric.data import Batch  # pyright: ignore[reportMissingImports, reportMissingModuleSource]  # isort: skip

    spec = json.loads((assets / "parameters.json").read_text())["model"]
    rename = {
        "gnn_num_layer": "gnum_layer",
        "trans_num_layer": "tnum_layer",
        "output_num_layer": "onum_layer",
        "gnn_jk": "JK",
    }
    keys = [
        "emb_dim",
        "gnn_type",
        "gnn_aggr",
        "node_readout",
        "num_heads",
        "graph_pooling",
        "trans_readout",
        "drop_ratio",
        "split_merge_method",
        "output_act_func",
        *rename,
    ]
    config = {rename.get(k, k): spec[k] for k in keys}
    model = RXNGClassifier(**config, output_size=2, split_process=True)
    state = torch.load(
        assets / "model/valid_checkpoint.pt", map_location="cpu", weights_only=False
    )["model_state_dict"]
    state = {key.removeprefix("module."): value for key, value in state.items()}
    model.load_state_dict(state, strict=True)
    print("Official RXNGraphormer pretrained classifier loaded strictly", flush=True)
    model.cuda().train()
    data = Path("probe-data")
    data.mkdir(exist_ok=False)
    batches = []
    for name, index in [("rct", 0), ("pdt", 2)]:
        (data / f"{name}.csv").write_text(
            "".join(r["rxn_smiles"].split(">")[index] + ",0\n" for r in rows[:2])
        )
        dataset = RXNDataset(root=str(data), name=f"{name}.csv", multi_process=False)
        batches.append(Batch.from_data_list([dataset[i] for i in range(len(dataset))]).to("cuda:0"))
    output = model(batches)
    loss = torch.nn.functional.cross_entropy(output, torch.tensor([0, 1], device="cuda"))
    if not torch.isfinite(loss):
        raise ValueError("Nonfinite RXNGraphormer smoke loss")
    loss.backward()
    verify_gradients(model)
    print("Official pretrained classifier GPU backward passed", flush=True)
    # Follow the official trainer: transfer the strictly loaded reactant/product
    # encoders into a scalar regressor. This is still a probe, not a five-fold run.
    regression = (
        RXNGRegressor(
            **config,
            output_size=1,
            split_process=True,
            pretrained_rct_encoder=model.rct_encoder,
            pretrained_pdt_encoder=model.pdt_encoder,
        )
        .cuda()
        .train()
    )
    regression.zero_grad(set_to_none=True)
    target = torch.tensor([float(r["G_T_activate"]) for r in rows[:2]], device="cuda")
    prediction = regression(batches).reshape(-1)
    regression_loss = (prediction - target).square().mean()
    if not torch.isfinite(regression_loss):
        raise ValueError("Nonfinite RXNGraphormer regression smoke loss")
    regression_loss.backward()
    verify_gradients(regression)
    print("Official pretrained-encoder energy regression GPU backward passed", flush=True)


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--model", choices=["rxnresid", "rxngraphormer", "reactiont5"], required=True)
    a = p.parse_args()
    manifest = json.loads(Path("probe.json").read_text())
    for entry in manifest["files"]:
        path = Path(entry["path"])
        data = path.read_bytes()
        actual = (
            hashlib.sha256(data).hexdigest()
            if "sha256" in entry
            else hashlib.sha1(b"blob " + str(len(data)).encode() + b"\0" + data).hexdigest()
        )
        if actual != entry.get("sha256", entry.get("git_blob_sha1")):
            raise ValueError(f"Asset checksum mismatch: {path}")
    with Path("data/snapshots/ground-truth/dataset.csv").open() as f:
        rows = list(csv.DictReader(f))
    if a.model == "rxnresid":
        rxnresid(rows)
    elif a.model == "rxngraphormer":
        rxngraphormer(rows, Path("assets"))
    else:
        module_spec = importlib.util.spec_from_file_location(
            "reactiont5_probe", "scripts/tricycle_reactiont5.py"
        )
        if module_spec is None or module_spec.loader is None:
            raise ValueError("ReactionT5 smoke module is unavailable")
        module = importlib.util.module_from_spec(module_spec)
        sys.modules[module_spec.name] = module
        module_spec.loader.exec_module(module)
        spec = json.loads(Path("reactiont5.json").read_text())
        module.train(Path.cwd(), spec, 0, smoke=True)


if __name__ == "__main__":
    main()
