"""Standalone CPU fingerprint regressors using the benchmark's frozen folds."""

from __future__ import annotations

import argparse
import csv
import importlib.metadata
import time
import tomllib
from pathlib import Path

try:
    from sar_runtime.io import file_sha256  # pyright: ignore[reportMissingImports, reportMissingModuleSource]  # isort: skip
    from sar_runtime.toml import dump_toml  # pyright: ignore[reportMissingImports, reportMissingModuleSource]  # isort: skip
except ImportError:
    from tricycle_core.io import file_sha256
    from tricycle_core.toml import dump_toml


MODELS = ("fingerprint_rf", "fingerprint_extra_trees", "fingerprint_ridge")


def fingerprint(smiles: str, spec: dict):
    import numpy as np
    from rdkit import Chem
    from rdkit.Chem import rdFingerprintGenerator

    parts = smiles.split(">")
    if len(parts) != 3 or not parts[0] or not parts[2]:
        raise ValueError("expected nonempty reactants and products")
    generator = rdFingerprintGenerator.GetMorganGenerator(
        radius=spec["radius"], fpSize=spec["size"], includeChirality=spec["include_chirality"]
    )
    vectors = []
    for part in parts:
        if not part:
            vectors.append(np.zeros(spec["size"], dtype=np.float32))
            continue
        mol = Chem.MolFromSmiles(part)
        if mol is None:
            raise ValueError("invalid reaction SMILES")
        for atom in mol.GetAtoms():
            atom.SetAtomMapNum(0)
        vectors.append(generator.GetCountFingerprintAsNumPy(mol).astype(np.float32))
    reactant, agent, product = vectors
    # Only structure enters X: no barriers, reaction energies, IDs, or compute times.
    return np.concatenate([reactant, product, product - reactant, agent])


def features(smiles: list[str], spec: dict):
    import numpy as np
    from scipy.sparse import csr_matrix

    return csr_matrix(np.stack([fingerprint(s, spec) for s in smiles]))


def estimator(model: str, config: dict, *, smoke: bool = False):
    from sklearn.ensemble import ExtraTreesRegressor, RandomForestRegressor  # pyright: ignore[reportMissingImports, reportMissingModuleSource]  # isort: skip
    from sklearn.linear_model import Ridge  # pyright: ignore[reportMissingImports, reportMissingModuleSource]  # isort: skip
    from sklearn.pipeline import make_pipeline  # pyright: ignore[reportMissingImports, reportMissingModuleSource]  # isort: skip
    from sklearn.preprocessing import StandardScaler  # pyright: ignore[reportMissingImports, reportMissingModuleSource]  # isort: skip

    params = dict(config["models"][model])
    if model == "fingerprint_ridge":
        return make_pipeline(StandardScaler(with_mean=False), Ridge(solver="lsqr", **params))
    if model not in MODELS:
        raise ValueError("unknown CPU baseline")
    if smoke:
        params["n_estimators"] = 8
    cls = RandomForestRegressor if model == "fingerprint_rf" else ExtraTreesRegressor
    return cls(random_state=config["seed"], n_jobs=config["n_jobs"], **params)


def read_csv(path: Path) -> list[dict]:
    with path.open(newline="") as f:
        return list(csv.DictReader(f))



def write_metadata(path: Path, data: dict) -> None:
    dump_toml(path, data)


def run_fold(config: dict, model: str, fold: int, *, smoke: bool = False) -> Path:
    import joblib
    import numpy as np
    from sklearn.metrics import mean_absolute_error  # pyright: ignore[reportMissingImports, reportMissingModuleSource]  # isort: skip

    if model not in MODELS or not 0 <= fold < 5:
        raise ValueError("invalid model/fold")
    for package, version in config["versions"].items():
        if importlib.metadata.version(package) != version:
            raise ValueError("unexpected runtime version: " + package)
    dataset, splits = Path(config["dataset"]), Path(config["splits"])
    if file_sha256(dataset) != config["dataset_sha256"] or file_sha256(splits) != config["splits_sha256"]:
        raise ValueError("frozen input checksum mismatch")
    rows, assignments = read_csv(dataset), read_csv(splits)
    row_id, target = config["row_id"], config["target"]
    ids = {r[row_id]: i for i, r in enumerate(rows)}
    if len(ids) != len(rows):
        raise ValueError("duplicate row identity")
    selected = [r for r in assignments if int(r["fold"]) == fold]
    if len(selected) != len(rows) or {r["row_id"] for r in selected} != set(ids):
        raise ValueError("invalid fold coverage")
    indices = {
        role: sorted(ids[r["row_id"]] for r in selected if r["split"] == role)
        for role in ("train", "val", "test")
    }
    groups = {
        role: {tuple(rows[i][k] for k in config["group_columns"]) for i in idx}
        for role, idx in indices.items()
    }
    if any(not g for g in groups.values()) or any(
        groups[a] & groups[b] for a, b in [("train", "val"), ("train", "test"), ("val", "test")]
    ):
        raise ValueError("empty split or substrate-group leakage")
    if smoke:
        indices = {role: idx[:16] for role, idx in indices.items()}
    root = config["smoke_output"] if smoke else config["output"]
    out = Path(root) / model / model / ("fold_" + str(fold))
    out.mkdir(parents=True, exist_ok=False)
    started = time.monotonic()
    matrices = {
        role: features([rows[i][config["smiles_column"]] for i in idx], config["fingerprint"])
        for role, idx in indices.items()
    }
    y = {role: np.asarray([float(rows[i][target]) for i in idx]) for role, idx in indices.items()}
    if not all(np.isfinite(v).all() for v in y.values()):
        raise ValueError("nonfinite target")
    fitted = estimator(model, config, smoke=smoke)
    fitted.fit(matrices["train"], y["train"])
    # Hyperparameters are predeclared; neither validation nor test labels enter fit.
    val_mae = float(mean_absolute_error(y["val"], fitted.predict(matrices["val"])))
    prediction = fitted.predict(matrices["test"])
    if prediction.shape != y["test"].shape or not np.isfinite(prediction).all():
        raise ValueError("invalid predictions")
    with (out / "predictions.csv").open("w", newline="") as f:
        writer = csv.DictWriter(
            f, fieldnames=["reaction_id", "row_index", "target_name", "y_true", "y_pred"]
        )
        writer.writeheader()
        for i, truth, pred in zip(indices["test"], y["test"], prediction, strict=True):
            writer.writerow(
                dict(
                    reaction_id=rows[i][row_id],
                    row_index=i,
                    target_name=target,
                    y_true=float(truth),
                    y_pred=float(pred),
                )
            )
    joblib.dump(fitted, out / "model.joblib")
    report = dict(
        model=model,
        fold=fold,
        device="cpu",
        smoke_only=smoke,
        benchmark_result=not smoke,
        train_rows=len(indices["train"]),
        val_rows=len(indices["val"]),
        test_rows=len(indices["test"]),
        validation_mae=val_mae,
        elapsed_seconds=time.monotonic() - started,
        fingerprint_dimension=config["fingerprint"]["size"] * 4,
        feature_layout=["reactants", "products", "products-minus-reactants", "agents"],
        dataset_sha256=config["dataset_sha256"],
        splits_sha256=config["splits_sha256"],
        fit_splits=["train"],
        hyperparameters_selected_using_test=False,
    )
    report.update(
        {
            "version_" + k.replace("-", "_"): importlib.metadata.version(k)
            for k in ["numpy", "scipy", "scikit-learn", "rdkit"]
        }
    )
    write_metadata(out / "runtime.toml", report)
    return out


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--model", choices=MODELS, required=True)
    parser.add_argument("--fold", type=int, required=True)
    parser.add_argument("--smoke", action="store_true")
    args = parser.parse_args()
    config = tomllib.loads(args.config.read_text())
    print(run_fold(config, args.model, args.fold, smoke=args.smoke))


if __name__ == "__main__":
    main()
