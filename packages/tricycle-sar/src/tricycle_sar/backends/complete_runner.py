"""Execute one full grouped-CV fold for RxnResid, RXNGraphormer, or ReactionT5."""

from __future__ import annotations

import argparse
import csv
import importlib.util
import json
import math
import os
import runpy
import sys

try:
    from sar_runtime.io import file_sha256, write_delimited  # pyright: ignore[reportMissingImports, reportMissingModuleSource]  # isort: skip
except ImportError:
    from tricycle_core.io import file_sha256, write_delimited


try:
    import tomllib
except ModuleNotFoundError:  # RXNGraphormer pins Python 3.10.
    import tomli as tomllib  # pyright: ignore[reportMissingImports, reportMissingModuleSource]  # isort: skip

from pathlib import Path


def read_rows(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def read_frozen_snapshot(root: Path) -> list[dict[str, str]]:
    """Validate this snapshot's integrity without imposing a dataset size.

    The manifest count checks consistency with the supplied snapshot; it is
    never a required training size. Fold coverage is derived from path IDs.
    """
    snapshot = root / "data/snapshots/ground-truth"
    manifest = json.loads((snapshot / "manifest.json").read_text(encoding="utf-8"))
    expected_rows = manifest["row_count"]
    if type(expected_rows) is not int or expected_rows < 1:
        raise ValueError("frozen snapshot must declare a positive integer row count")
    path = snapshot / "dataset.csv"
    if file_sha256(path) != manifest["dataset"]["sha256"]:
        raise ValueError("frozen snapshot dataset hash mismatch")
    rows = read_rows(path)
    if len(rows) != expected_rows:
        raise ValueError("frozen snapshot dataset row count mismatch")
    path_ids = [row.get("path_id", "") for row in rows]
    if any(not isinstance(ident, str) or not ident.strip() for ident in path_ids) or len(
        set(path_ids)
    ) != len(rows):
        raise ValueError("frozen snapshot has missing or duplicate path ids")
    return rows


def write_csv(path: Path, rows: list[dict[str, object]]) -> None:
    if not rows:
        raise ValueError(f"refusing to write empty predictions: {path}")
    write_delimited(path, list(rows[0]), rows, project_fields=False)


def verify_inputs(task_path: Path, model: str, fold: int) -> None:
    root = Path.cwd()
    task = tomllib.loads(task_path.read_text(encoding="utf-8"))
    if task.get("model") != model or task.get("fold") != fold or task.get("smoke") is not False:
        raise ValueError("task model/fold/scope does not match the requested full-training fold")
    manifest = json.loads((root / "training-bundle.json").read_text(encoding="utf-8"))
    if (
        task["benchmark_dataset_sha256"] != manifest["dataset_sha256"]
        or task["benchmark_splits_sha256"] != manifest["splits_sha256"]
    ):
        raise ValueError("frozen benchmark dataset or split hash mismatch")
    for entry in manifest["files"]:
        relative = Path(entry["path"])
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError(f"unsafe frozen input path: {relative}")
        path = root / relative
        actual = file_sha256(path)
        if actual != entry["sha256"]:
            raise ValueError(f"frozen training input hash mismatch: {relative}")


def run_rxnresid(fold: int) -> None:
    import torch  # pyright: ignore[reportMissingImports, reportMissingModuleSource]  # isort: skip
    from rxnresid.data.dataset import ReactionGroupDataset  # pyright: ignore[reportMissingImports, reportMissingModuleSource]  # isort: skip
    from rxnresid.data.split import FoldAssignment, SplitManifest, dataset_group_fingerprint, dataset_record_fingerprint, save_split_manifest  # pyright: ignore[reportMissingImports, reportMissingModuleSource]  # isort: skip

    root = Path.cwd()
    source = Path(os.environ["SAR_MODEL_SOURCE"])
    assignments_path = root / "data/splits/ground-truth-5fold/assignments.csv"
    rows = read_frozen_snapshot(root)
    dataset_path = root / "data/snapshots/ground-truth/dataset.csv"
    assignment_rows = read_rows(assignments_path)
    path_role: dict[int, dict[str, str]] = {}
    for item in assignment_rows:
        path_role.setdefault(int(item["fold"]), {})[item["row_id"]] = item["split"]
    dataset = ReactionGroupDataset(
        dataset_path,
        target_column="G_T_activate",
        reaction_column="rxn_smiles",
        include_stereo=True,
        path_id_column="path_id",
        route_id_column="prod_id",
        baseline_reduction="mean",
        cache_dir=None,
    )
    processed_ids = [row.path_id for row in dataset.rows]
    if len(set(processed_ids)) != len(processed_ids) or set(processed_ids) != {
        row["path_id"] for row in rows
    }:
        raise ValueError("RxnResid preprocessing changed source path identities")
    group_roles: dict[int, dict[str, list[str]]] = {}
    for fold_index in range(5):
        roles = {name: [] for name in ("train", "valid", "test")}
        row_roles = path_role.get(fold_index, {})
        if set(row_roles) != {row["path_id"] for row in rows}:
            raise ValueError(f"RxnResid fold {fold_index} path coverage mismatch")
        for group_id in dataset.group_ids:
            members = dataset.group_to_rows[group_id]
            member_roles = {row_roles[dataset.rows[index].path_id] for index in members}
            if len(member_roles) != 1:
                raise ValueError("RxnResid structural group crosses the frozen fold boundary")
            role = member_roles.pop()
            roles["valid" if role == "val" else role].append(group_id)
        if any(not values for values in roles.values()):
            raise ValueError(f"RxnResid fold {fold_index} has an empty split")
        group_roles[fold_index] = roles

    manifest_path = root / "data/splits/ground-truth-5fold/rxnresid-manifest.json"
    manifest = SplitManifest(
        schema_version=1,
        strategy="tricycle-ground-truth-substrate-group-5fold",
        seed=42,
        num_folds=5,
        dataset_group_count=len(dataset),
        dataset_fingerprint=dataset_group_fingerprint(dataset),
        dataset_row_count=len(dataset.rows),
        dataset_record_fingerprint=dataset_record_fingerprint(dataset),
        folds=tuple(
            FoldAssignment(
                fold_index=index,
                train_group_ids=tuple(group_roles[index]["train"]),
                valid_group_ids=tuple(group_roles[index]["valid"]),
                test_group_ids=tuple(group_roles[index]["test"]),
            )
            for index in range(5)
        ),
    )
    if manifest_path.exists():
        existing = json.loads(manifest_path.read_text(encoding="utf-8"))
        if json.dumps(existing, sort_keys=True) != json.dumps(manifest.to_dict(), sort_keys=True):
            raise ValueError("existing RxnResid split manifest differs from frozen assignments")
    else:
        save_split_manifest(manifest, manifest_path)

    try:
        from sar_runtime.rxnresid import load_base_config, write_round_config  # pyright: ignore[reportMissingImports, reportMissingModuleSource]  # isort: skip
        from sar_runtime.sar_commands import build_training_command  # pyright: ignore[reportMissingImports, reportMissingModuleSource]  # isort: skip
    except ImportError:
        from tricycle_sar.commands import build_training_command
        from tricycle_sar.rxnresid import load_base_config, write_round_config
    config = load_base_config(source)
    config_path = root / "configs/rxnresid-ground-truth.yaml"
    write_round_config(
        config_path,
        config,
        training_csv=dataset_path.resolve(),
        split_manifest=manifest_path.resolve(),
        output_dir=root / "model-runs/rxnresid",
        epochs=config["training"]["epochs"],
        batch_size_paths=config["training"]["batch_size_paths"],
        device="auto",
        mixed_precision="no",
        seed=42,
    )
    output = root / f"model-runs/rxnresid/fold_{fold}"
    if output.exists():
        raise FileExistsError(output)
    command = build_training_command(
        python_executable=sys.executable,
        script=str(source / "train.py"),
        config=str(config_path),
        output=str(output),
        fold_index=fold,
        split_manifest=str(manifest_path),
        seed=42,
    )
    sys.argv = list(command[1:])
    try:
        runpy.run_path(str(source / "train.py"), run_name="__main__")
    except SystemExit as error:
        if error.code not in (0, None):
            raise
    native = read_rows(output / "test_predictions.csv")
    target_by_id = {row["path_id"]: (index, row) for index, row in enumerate(rows)}
    prediction_rows = []
    for item in native:
        row_index, source_row = target_by_id[item["path_id"]]
        prediction_rows.append(
            {
                "row_index": row_index,
                "reaction_id": item["path_id"],
                "target_name": "G_T_activate",
                "y_true": float(item["target"]),
                "y_pred": float(item["prediction"]),
            }
        )
    write_csv(output / "predictions.csv", prediction_rows)
    (output / "accelerator-runtime.json").write_text(
        json.dumps(
            {
                "model": "rxnresid",
                "fold": fold,
                "device": torch.cuda.get_device_name(0),
                "torch": str(torch.__version__),
                "training_protocol": "upstream selection/refit grouped fold",
                "pretrained_checkpoint_loaded": False,
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )


def _config_bool(value: object) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _rxng_config(assets: Path, *, regression: bool = False) -> dict[str, object]:
    filename = "thiol_addition_parameters.json" if regression else "parameters.json"
    spec = json.loads((assets / filename).read_text(encoding="utf-8"))["model"]
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
    config = {rename.get(key, key): spec[key] for key in keys}
    if regression:
        config.update(
            {
                "output_norm": _config_bool(spec["output_norm"]),
                "rct_batch_norm": _config_bool(spec["rct_batch_norm"]),
                "pdt_batch_norm": _config_bool(spec["pdt_batch_norm"]),
                # The unified benchmark has no mechanistic intermediate graph.
                "use_mid_inf": False,
                "mid_iteract_method": spec["mid_iteract_method"],
                "mid_batch_norm": _config_bool(spec["mid_batch_norm"]),
                "mid_layer_num": int(spec["mid_layer_num"]),
            }
        )
    return config


def run_rxngraphormer(fold: int) -> None:
    import numpy as np
    import torch  # pyright: ignore[reportMissingImports, reportMissingModuleSource]  # isort: skip
    from rxngraphormer.data import RXNDataset  # pyright: ignore[reportMissingImports, reportMissingModuleSource]  # isort: skip
    from rxngraphormer.model import RXNGClassifier, RXNGRegressor  # pyright: ignore[reportMissingImports, reportMissingModuleSource]  # isort: skip
    from torch_geometric.data import Batch  # pyright: ignore[reportMissingImports, reportMissingModuleSource]  # isort: skip

    root = Path.cwd()
    assets = root / "assets"
    regression_parameters = json.loads(
        (assets / "thiol_addition_parameters.json").read_text(encoding="utf-8")
    )
    model_parameters = regression_parameters["model"]
    optimizer_parameters = regression_parameters["optimizer"]
    scheduler_parameters = regression_parameters["scheduler"]
    data_parameters = regression_parameters["data"]
    training_parameters = regression_parameters["training"]
    if str(regression_parameters.get("task", "")).lower() != "regression":
        raise ValueError("the pinned author parameter file is not a regression configuration")
    if str(optimizer_parameters["optimizer"]).lower() != "adamw":
        raise ValueError("unsupported author optimizer configuration")
    if str(scheduler_parameters["type"]).lower() != "noamlr":
        raise ValueError("unsupported author scheduler configuration")
    if int(training_parameters["accum"]) != 1:
        raise ValueError("author accumulation setting requires an explicit implementation")
    if int(data_parameters["radius"]) != 2:
        raise ValueError("the training bundle expects the author's radius-2 graph preprocessing")
    regressor_config = _rxng_config(assets, regression=True)
    data = root / "rxngraphormer-data"
    data.mkdir(exist_ok=True)
    rows = read_frozen_snapshot(root)
    assignments = read_rows(root / "data/splits/ground-truth-5fold/assignments.csv")
    roles: dict[str, list[int]] = {"train": [], "val": [], "test": []}
    ids = {row["path_id"]: index for index, row in enumerate(rows)}
    for assignment in assignments:
        if int(assignment["fold"]) == fold:
            roles[assignment["split"]].append(ids[assignment["row_id"]])
    if sorted(index for values in roles.values() for index in values) != list(range(len(rows))):
        raise ValueError("RXNGraphormer fold coverage is invalid")
    groups = {
        role: {(rows[i]["ene_id"], rows[i]["diene_id"]) for i in indices}
        for role, indices in roles.items()
    }
    if any(
        groups[a] & groups[b] for a, b in (("train", "val"), ("train", "test"), ("val", "test"))
    ):
        raise ValueError("RXNGraphormer substrate leakage")

    reactant_file, product_file = data / "reactants.csv", data / "products.csv"
    if not reactant_file.exists() or not product_file.exists():
        with (
            reactant_file.open("w", encoding="utf-8") as rct,
            product_file.open("w", encoding="utf-8") as pdt,
        ):
            for row in rows:
                parts = row["rxn_smiles"].split(">")
                if len(parts) != 3 or not parts[0] or not parts[2]:
                    raise ValueError(f"invalid reaction SMILES for {row['path_id']}")
                rct.write(f"{parts[0]},0\n")
                pdt.write(f"{parts[2]},0\n")
    rct_dataset = RXNDataset(
        root=str(data), name=reactant_file.name, task="regression", multi_process=False
    )
    pdt_dataset = RXNDataset(
        root=str(data), name=product_file.name, task="regression", multi_process=False
    )
    if len(rct_dataset) != len(rows) or len(pdt_dataset) != len(rows):
        raise ValueError("RXNGraphormer source graph preprocessing dropped rows")

    seed = int(data_parameters["seed"])
    torch.manual_seed(seed + fold)
    torch.cuda.manual_seed_all(seed + fold)
    classifier = RXNGClassifier(**_rxng_config(assets), output_size=2, split_process=True)
    checkpoint = torch.load(
        assets / "model/valid_checkpoint.pt", map_location="cpu", weights_only=False
    )
    state = checkpoint["model_state_dict"]
    state = {key.removeprefix("module."): value for key, value in state.items()}
    classifier.load_state_dict(state, strict=True)
    regressor = RXNGRegressor(
        **regressor_config,
        output_size=1,
        split_process=True,
        pretrained_rct_encoder=classifier.rct_encoder,
        pretrained_pdt_encoder=classifier.pdt_encoder,
    ).cuda()
    del classifier
    device = torch.device("cuda:0")
    y = torch.tensor([float(row["G_T_activate"]) for row in rows], dtype=torch.float32)
    learning_rate = float(optimizer_parameters["learning_rate"])
    encoder_lr_scale = float(model_parameters["pretrained_lr_scaled_coef"])
    optimizer = torch.optim.AdamW(
        [
            {"params": regressor.rct_encoder.parameters(), "lr": learning_rate * encoder_lr_scale},
            {"params": regressor.pdt_encoder.parameters(), "lr": learning_rate * encoder_lr_scale},
            {"params": regressor.decoder.parameters(), "lr": learning_rate},
        ],
        lr=learning_rate,
        weight_decay=float(optimizer_parameters["weight_decay"]),
    )
    from rxngraphormer.scheduler import NoamLR  # pyright: ignore[reportMissingImports, reportMissingModuleSource]  # isort: skip

    scheduler = NoamLR(
        optimizer,
        model_size=int(model_parameters["emb_dim"]),
        warmup_steps=int(scheduler_parameters["warmup_step"]),
    )
    batch_size = int(data_parameters["batch_size"])
    max_epochs = int(training_parameters["epoch"])
    gradient_clip = float(training_parameters["clip_norm"])
    loss_name = str(training_parameters["loss"]).lower()
    if loss_name == "l2":
        loss_function = torch.nn.functional.mse_loss
    elif loss_name == "l1":
        loss_function = torch.nn.functional.l1_loss
    else:
        raise ValueError(f"unsupported RXNGraphormer regression loss: {loss_name}")
    output = root / f"model-runs/rxngraphormer/fold_{fold}"
    output.mkdir(parents=True, exist_ok=False)

    def batch(indices: list[int]):
        rct = Batch.from_data_list([rct_dataset[index] for index in indices]).to(device)
        pdt = Batch.from_data_list([pdt_dataset[index] for index in indices]).to(device)
        return [rct, pdt]

    def chunks(indices: list[int]) -> list[list[int]]:
        pieces = [indices[i : i + batch_size] for i in range(0, len(indices), batch_size)]
        if len(pieces) > 1 and len(pieces[-1]) == 1:
            pieces[-2].extend(pieces.pop())
        return pieces

    def predict(indices: list[int]) -> tuple[list[int], list[float]]:
        regressor.eval()
        values: list[float] = []
        ordered: list[int] = []
        with torch.no_grad():
            for part in chunks(indices):
                prediction = regressor(batch(part)).reshape(-1).cpu()
                values.extend(float(value) for value in prediction)
                ordered.extend(part)
        if not all(math.isfinite(value) for value in values):
            raise ValueError("RXNGraphormer produced nonfinite predictions")
        return ordered, values

    best, best_state = math.inf, None
    history = []
    for epoch in range(max_epochs):
        regressor.train()
        permutation = (
            np.random.default_rng(seed + fold * 1000 + epoch).permutation(roles["train"]).tolist()
        )
        for part in chunks(permutation):
            optimizer.zero_grad(set_to_none=True)
            prediction = regressor(batch(part)).reshape(-1)
            loss = loss_function(prediction, y[part].to(device))
            if not torch.isfinite(loss):
                raise ValueError("RXNGraphormer training loss is nonfinite")
            loss.backward()
            torch.nn.utils.clip_grad_norm_(regressor.parameters(), gradient_clip)
            optimizer.step()
            scheduler.step()
        valid_ids, valid_predictions = predict(roles["val"])
        mae = sum(
            abs(value - float(y[index]))
            for index, value in zip(valid_ids, valid_predictions, strict=True)
        ) / len(valid_ids)
        history.append({"epoch": epoch + 1, "validation_mae": mae})
        if mae < best:
            best = mae
            best_state = {
                key: value.detach().cpu().clone() for key, value in regressor.state_dict().items()
            }
        print(f"[rxngraphormer] fold={fold} epoch={epoch + 1} val_mae={mae:.6f}", flush=True)
    if best_state is None:
        raise RuntimeError("RXNGraphormer did not select a checkpoint")
    regressor.load_state_dict(best_state, strict=True)
    test_ids, test_predictions = predict(roles["test"])
    write_csv(
        output / "predictions.csv",
        [
            {
                "row_index": index,
                "reaction_id": rows[index]["path_id"],
                "target_name": "G_T_activate",
                "y_true": float(y[index]),
                "y_pred": prediction,
            }
            for index, prediction in zip(test_ids, test_predictions, strict=True)
        ],
    )
    torch.save(best_state, output / "best.pt")
    (output / "training.json").write_text(
        json.dumps(
            {
                "fold": fold,
                "official_source_revision": "ab6d13d1900a2a3125df24dc798ffa8a6cc6761d",
                "pretrained_encoders": "strictly_loaded_from_official_classification_checkpoint",
                "regression_parameter_source": "config/thiol_addition_parameters.json",
                "regression_parameter_sha256": file_sha256(
                    assets / "thiol_addition_parameters.json"
                ),
                "training_parameters": {
                    "model": regressor_config,
                    "batch_size": batch_size,
                    "epochs": max_epochs,
                    "optimizer": str(optimizer_parameters["optimizer"]),
                    "base_learning_rate": learning_rate,
                    "pretrained_encoder_lr_scale": encoder_lr_scale,
                    "weight_decay": float(optimizer_parameters["weight_decay"]),
                    "scheduler": str(scheduler_parameters["type"]),
                    "warmup_steps": int(scheduler_parameters["warmup_step"]),
                    "loss": loss_name,
                    "gradient_clip_norm": gradient_clip,
                    "target_scaling": "none",
                    "mechanistic_intermediate_graph": "disabled; absent from benchmark rows",
                    "split": "frozen grouped five-fold assignments; author single split not used",
                },
                "best_validation_mae": best,
                "epochs_completed": len(history),
                "validation_history": history,
                "torch_version": str(torch.__version__),
                "device": torch.cuda.get_device_name(0),
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )


def run_reactiont5(fold: int) -> None:
    root = Path.cwd()
    spec = json.loads((root / "reactiont5.json").read_text(encoding="utf-8"))
    spec["checkpoint_directory"] = os.environ["SAR_MODEL_ASSETS"]
    path = root / "scripts/tricycle_reactiont5.py"
    module_spec = importlib.util.spec_from_file_location("reactiont5_cv", path)
    if module_spec is None or module_spec.loader is None:
        raise RuntimeError("could not load ReactionT5 fold adapter")
    module = importlib.util.module_from_spec(module_spec)
    sys.modules[module_spec.name] = module
    module_spec.loader.exec_module(module)
    module.train(root, spec, fold, smoke=False)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--model", choices=("rxnresid", "rxngraphormer", "reactiont5"), required=True
    )
    parser.add_argument("--fold", type=int, choices=range(5), required=True)
    parser.add_argument("--task", type=Path, required=True)
    args = parser.parse_args()
    verify_inputs(args.task, args.model, args.fold)
    if args.model == "rxnresid":
        run_rxnresid(args.fold)
    elif args.model == "rxngraphormer":
        run_rxngraphormer(args.fold)
    else:
        run_reactiont5(args.fold)


if __name__ == "__main__":
    main()
