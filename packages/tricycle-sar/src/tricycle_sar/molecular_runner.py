"""Pinned UAM / pretrained MolCLR adapters for grouped barrier regression.

Author model code and assets are supplied locally and verified before import.
No downloads, upstream random split, or yield clipping are used.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import importlib.metadata
import importlib.util
import json
import random
import sys
import tomllib
from pathlib import Path

try:
    from sar_runtime.training_protocol import verify_asset_files  # pyright: ignore[reportMissingImports, reportMissingModuleSource]  # isort: skip
except ImportError:
    from tricycle_core.training_protocol import verify_asset_files


try:
    from sar_runtime.io import file_sha256  # pyright: ignore[reportMissingImports, reportMissingModuleSource]  # isort: skip
except ImportError:
    from tricycle_core.io import file_sha256


MODELS = ("uam", "molclr")




def canonical_parts(reaction: str) -> list[list[str]]:
    from rdkit import Chem

    parts = reaction.split(">")
    if len(parts) != 3 or not parts[0] or not parts[2]:
        raise ValueError("expected reactants>agents>products")
    result = []
    for part in parts:
        molecules = []
        for smiles in part.split(".") if part else []:
            mol = Chem.MolFromSmiles(smiles)
            if mol is None:
                raise ValueError("invalid SMILES")
            for atom in mol.GetAtoms():
                atom.SetAtomMapNum(0)
            molecules.append(Chem.MolToSmiles(mol, canonical=True, isomericSmiles=True))
        result.append(sorted(molecules))
    return result


def load_module(name: str, path: Path, *, package: bool = False):
    spec = importlib.util.spec_from_file_location(
        name, path, submodule_search_locations=[str(path.parent)] if package else None
    )
    if spec is None or spec.loader is None:
        raise ValueError(f"Model module cannot be loaded: {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def molclr_graph(smiles: str):
    import torch  # pyright: ignore[reportMissingImports, reportMissingModuleSource]  # isort: skip
    from rdkit import Chem
    from torch_geometric.data import Data  # pyright: ignore[reportMissingImports, reportMissingModuleSource]  # isort: skip

    mol = Chem.AddHs(Chem.MolFromSmiles(smiles))  # Author's fine-tuning featurizer.
    chirality = [
        Chem.ChiralType.CHI_UNSPECIFIED,
        Chem.ChiralType.CHI_TETRAHEDRAL_CW,
        Chem.ChiralType.CHI_TETRAHEDRAL_CCW,
    ]
    bonds = [
        Chem.BondType.SINGLE,
        Chem.BondType.DOUBLE,
        Chem.BondType.TRIPLE,
        Chem.BondType.AROMATIC,
    ]
    directions = [Chem.BondDir.NONE, Chem.BondDir.ENDUPRIGHT, Chem.BondDir.ENDDOWNRIGHT]
    atoms = []
    for atom in mol.GetAtoms():
        if not 1 <= atom.GetAtomicNum() <= 118 or atom.GetChiralTag() not in chirality:
            raise ValueError("atom unsupported by pretrained MolCLR vocabulary")
        atoms.append([atom.GetAtomicNum() - 1, chirality.index(atom.GetChiralTag())])
    edges, attrs = [], []
    for bond in mol.GetBonds():
        i, j = bond.GetBeginAtomIdx(), bond.GetEndAtomIdx()
        attr = [bonds.index(bond.GetBondType()), directions.index(bond.GetBondDir())]
        edges.extend([[i, j], [j, i]])
        attrs.extend([attr, attr])
    return Data(
        x=torch.tensor(atoms, dtype=torch.long),
        edge_index=torch.tensor(edges, dtype=torch.long).reshape(-1, 2).t().contiguous(),
        edge_attr=torch.tensor(attrs, dtype=torch.long).reshape(-1, 2),
    )


def build_model(name: str, config: dict, device):
    import torch  # pyright: ignore[reportMissingImports, reportMissingModuleSource]  # isort: skip
    from torch import nn  # pyright: ignore[reportMissingImports, reportMissingModuleSource]  # isort: skip

    root = Path(config["asset_directory"])
    missing = verify_asset_files(root, config["files"])
    if missing:
        raise ValueError("missing assets: " + ", ".join(missing))
    if name == "molclr":
        module = load_module("_tricycle_molclr", root / "models/ginet_finetune.py")

        class ReactionMolCLR(nn.Module):
            def __init__(self):
                super().__init__()
                self.encoder = module.GINet(
                    task="regression",
                    num_layer=5,
                    emb_dim=300,
                    feat_dim=512,
                    drop_ratio=0.3,
                    pool="mean",
                )
                self.encoder.pred_head = nn.Identity()
                weights = torch.load(
                    root / "ckpt/pretrained_gin/checkpoints/model.pth",
                    map_location="cpu",
                    weights_only=True,
                )
                weights = {
                    k: v for k, v in weights.items() if not k.startswith(("pred_head.", "out_lin."))
                }
                self.encoder.load_state_dict(weights, strict=True)
                self.head = nn.Sequential(
                    nn.Linear(2048, 256), nn.ReLU(), nn.Dropout(0.1), nn.Linear(256, 1)
                )

            def forward(self, graph, owners, count):
                embedding, _ = self.encoder(graph)
                pooled = embedding.new_zeros((count * 3, 512)).index_add(0, owners, embedding)
                pooled = pooled.reshape(count, 3, 512)
                r, a, p = pooled[:, 0], pooled[:, 1], pooled[:, 2]
                return self.head(torch.cat([r, p, p - r, a], dim=1)).flatten()

        return ReactionMolCLR().to(device), None
    if name != "uam":
        raise ValueError("unknown model")
    from transformers import AutoTokenizer  # pyright: ignore[reportMissingImports, reportMissingModuleSource]  # isort: skip

    tokenizer = AutoTokenizer.from_pretrained(root / "tokenizer", local_files_only=True)
    load_module("_tricycle_uam", root / "model/__init__.py", package=True)
    module = load_module("_tricycle_uam.mmmodel", root / "model/mmmodel.py")
    model = module.UAM(
        1024,
        9,
        3,
        mlp_hidden_size=512,
        dense_l=4,
        spar_l=2,
        num_exps=4,
        mlp_drop=0.2,
        mlp_out_size=512,
        g_hidden_size=64,
        num_step_mp=3,
        num_step_set2set=3,
        num_layer_set2set=1,
        g_output_dim=1024,
        vocab_size=tokenizer.vocab_size,
        s_embed_dim=512,
        num_heads=2,
        num_layers=4,
        context_length=512,
        s_output_dim=1024,
        predict_hidden_dim=512,
        prob_dropout=0.1,
        pre_trained="False",
    )
    return model.to(device), tokenizer


def collate(name: str, reactions: list[str], config: dict, device, tokenizer=None):
    import torch  # pyright: ignore[reportMissingImports, reportMissingModuleSource]  # isort: skip
    from rdkit import Chem
    from torch_geometric.data import Batch  # pyright: ignore[reportMissingImports, reportMissingModuleSource]  # isort: skip

    parts = [canonical_parts(r) for r in reactions]
    if name == "molclr":
        graphs, owners = [], []
        for i, roles in enumerate(parts):
            for role, smiles_list in enumerate(roles):
                for smiles in smiles_list:
                    graphs.append(molclr_graph(smiles))
                    owners.append(i * 3 + role)
        return (
            Batch.from_data_list(graphs).to(device),
            torch.tensor(owners, dtype=torch.long, device=device),
            len(parts),
        )
    helpers = sys.modules.get("_tricycle_uam_features")
    if helpers is None:
        helpers = load_module(
            "_tricycle_uam_features", Path(config["asset_directory"]) / "dataset/utils.py"
        )
    texts, graphs, fingerprints = [], [], []
    for reactants, agents, products in parts:
        left = sorted(reactants + agents)  # Author input has two roles (left >> product).
        texts.append(".".join(left) + ">>" + ".".join(products))
        graphs.append(
            tuple(
                Batch.from_data_list(
                    [helpers.mol_to_graph_data_obj_simple(Chem.MolFromSmiles(s)) for s in group]
                ).to(device)
                for group in [left, products]
            )
        )
        fingerprints.append(
            torch.stack(
                [helpers.convert_fp_to_tensor(helpers.get_ECFP(s)) for s in left + products]
            ).sum(0)
        )
    if tokenizer is None:
        raise ValueError("UAM tokenizer is required")
    tokens = tokenizer(texts, padding=True, truncation=False, return_tensors="pt")
    if tokens["input_ids"].shape[1] > 512:
        raise ValueError("UAM input exceeds 512 tokens; truncation is forbidden")
    return tokens.to(device), graphs, torch.stack(fingerprints).to(device)


def batches(indices: list[int], size: int):
    if size < 2 or len(indices) < 2:
        raise ValueError("batch normalization requires at least two training examples")
    chunks = [indices[i : i + size] for i in range(0, len(indices), size)]
    if len(chunks) > 1 and len(chunks[-1]) == 1:
        chunks[-2].extend(chunks.pop())
    return chunks


def loss_value(name, output, target):
    if name == "molclr":
        return ((output - target) ** 2).mean()
    mean, logvar, aux = output
    error = (mean - target) ** 2
    return 0.9 * error.mean() + 0.1 * (error * (-logvar).exp() + logvar).mean() + 0.01 * aux.mean()


def read_csv(path: Path):
    with path.open(newline="") as f:
        return list(csv.DictReader(f))


def run_fold(name: str, config: dict, fold: int, *, smoke: bool = False):
    import numpy as np
    import torch  # pyright: ignore[reportMissingImports, reportMissingModuleSource]  # isort: skip

    if name not in MODELS or not 0 <= fold < 5:
        raise ValueError("invalid model/fold")
    data_path, split_path = Path(config["dataset"]), Path(config["splits"])
    if (
        file_sha256(data_path) != config["dataset_sha256"]
        or file_sha256(split_path) != config["splits_sha256"]
    ):
        raise ValueError("input SHA256 mismatch")
    rows, assignments = read_csv(data_path), read_csv(split_path)
    lookup = {r[config["row_id"]]: i for i, r in enumerate(rows)}
    assigned = [a for a in assignments if int(a["fold"]) == fold]
    if (
        len(lookup) != len(rows)
        or len(assigned) != len(rows)
        or {a["row_id"] for a in assigned} != set(lookup)
    ):
        raise ValueError("fold coverage mismatch")
    indices = {
        role: sorted(lookup[a["row_id"]] for a in assigned if a["split"] == role)
        for role in ["train", "val", "test"]
    }
    groups = {
        role: {tuple(rows[i][k] for k in config["group_columns"]) for i in idx}
        for role, idx in indices.items()
    }
    if any(not g for g in groups.values()) or any(
        groups[a] & groups[b] for a, b in [("train", "val"), ("train", "test"), ("val", "test")]
    ):
        raise ValueError("empty split or group leakage")
    if smoke:
        indices = {role: idx[:2] for role, idx in indices.items()}
    target = np.asarray([float(r[config["target"]]) for r in rows])
    if not np.isfinite(target).all():
        raise ValueError("nonfinite target")
    mean, scale = float(target[indices["train"]].mean()), float(target[indices["train"]].std())
    scale = max(scale, 1e-8)
    random.seed(config["seed"])
    np.random.seed(config["seed"])
    torch.manual_seed(config["seed"])
    device = torch.device(config["device"])
    if device.type == "cuda" and not torch.cuda.is_available():
        raise RuntimeError("CUDA required; CPU fallback is forbidden")
    print(f"[molecular] model={name} fold={fold} stage=model_load device={device}", flush=True)
    model, tokenizer = build_model(name, config, device)
    if device.type == "cuda" and not all(p.is_cuda for p in model.parameters()):
        raise RuntimeError("model parameters not on GPU")
    print(f"[molecular] model_ready device={next(model.parameters()).device}", flush=True)
    out = Path("smoke-results" if smoke else "model-runs") / name / name / f"fold_{fold}"
    out.mkdir(parents=True, exist_ok=False)

    def forward(ids):
        inputs = collate(
            name, [rows[i][config["smiles_column"]] for i in ids], config, device, tokenizer
        )
        return model(*inputs)

    def evaluate(ids):
        model.eval()
        prediction = []
        with torch.no_grad():
            for offset in range(0, len(ids), config["batch_size"]):
                output = forward(ids[offset : offset + config["batch_size"]])
                prediction.extend((output[0] if name == "uam" else output).cpu().tolist())
        result = np.asarray(prediction) * scale + mean
        if not np.isfinite(result).all():
            raise ValueError("nonfinite predictions")
        return result

    if name == "molclr":
        parameters = [
            {"params": model.encoder.parameters(), "lr": config["encoder_learning_rate"]},
            {"params": model.head.parameters(), "lr": config["learning_rate"]},
        ]
    else:
        parameters = model.parameters()
    optimizer = torch.optim.AdamW(
        parameters, lr=config["learning_rate"], weight_decay=config["weight_decay"]
    )
    best, stale, best_epoch = float("inf"), 0, -1
    for epoch in range(1 if smoke else config["epochs"]):
        model.train()
        train = list(indices["train"])
        random.shuffle(train)
        for ids in batches(train, config["batch_size"]):
            output = forward(ids)
            y = torch.tensor((target[ids] - mean) / scale, dtype=torch.float32, device=device)
            loss = loss_value(name, output, y)
            if not bool(torch.isfinite(loss).item()):
                raise ValueError("nonfinite loss")
            optimizer.zero_grad()
            loss.backward()
            gradients = [p.grad for p in model.parameters() if p.grad is not None]
            if not gradients or not all(bool(torch.isfinite(g).all()) for g in gradients):
                raise ValueError("invalid gradients")
            if smoke:
                if not any(bool((g != 0).any()) for g in gradients):
                    raise ValueError("all gradients are zero")
                break  # no optimizer update for smoke
            torch.nn.utils.clip_grad_norm_(model.parameters(), 1.0)
            optimizer.step()
            if epoch == 0 and ids == train[: len(ids)]:
                print(
                    f"[molecular] first_optimizer_step device={y.device} loss={loss.item():.6f}",
                    flush=True,
                )
        score = float(np.abs(evaluate(indices["val"]) - target[indices["val"]]).mean())
        print(
            f"[molecular] model={name} fold={fold} epoch={epoch + 1} val_mae={score:.6f}",
            flush=True,
        )
        if score < best:
            best, stale, best_epoch = score, 0, epoch
            if not smoke:
                torch.save(model.state_dict(), out / "best.pt")
        else:
            stale += 1
        if stale >= config["patience"]:
            break
    if not smoke:
        model.load_state_dict(torch.load(out / "best.pt", map_location=device, weights_only=True))
    predictions = evaluate(indices["test"])
    with (out / "predictions.csv").open("w", newline="") as f:
        writer = csv.DictWriter(
            f, fieldnames=["reaction_id", "row_index", "target_name", "y_true", "y_pred"]
        )
        writer.writeheader()
        for i, prediction in zip(indices["test"], predictions, strict=True):
            writer.writerow(
                dict(
                    reaction_id=rows[i][config["row_id"]],
                    row_index=i,
                    target_name=config["target"],
                    y_true=target[i],
                    y_pred=prediction,
                )
            )
    report = dict(
        model=name,
        fold=fold,
        device=str(device),
        smoke_only=smoke,
        pretrained_loaded=name == "molclr",
        model_forward_passed=True,
        model_backward_passed=True,
        optimizer_updates=not smoke,
        training_target_mean=mean,
        training_target_scale=scale,
        validation_mae=best,
        best_epoch=best_epoch,
        source_revision=config["revision"],
        dataset_sha256=config["dataset_sha256"],
        splits_sha256=config["splits_sha256"],
        adapter_sha256=file_sha256(Path(__file__)),
        recipe_sha256=hashlib.sha256(json.dumps(config, sort_keys=True).encode()).hexdigest(),
        torch_version=str(torch.__version__),
        pyg_version=importlib.metadata.version("torch-geometric"),
        rdkit_version=importlib.metadata.version("rdkit"),
    )
    (out / "runtime.toml").write_text(
        "\n".join(k + " = " + json.dumps(v) for k, v in report.items()) + "\n"
    )
    print(out)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--model", choices=MODELS, required=True)
    parser.add_argument("--fold", type=int, required=True)
    parser.add_argument("--smoke", action="store_true")
    args = parser.parse_args()
    run_fold(args.model, tomllib.loads(args.config.read_text()), args.fold, smoke=args.smoke)
