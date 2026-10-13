"""ReactionT5v2 barrier adaptation; standalone worker, no downloads.

Architecture follows sagawatatsuya/ReactionT5v2 models.ReactionT5Yield2
(MIT, revision 76eb08068e10fe255cae5d563a91e1c1e9abac54).
Preserve encoder/decoder and five linear layers; reset final yield head,
train on training-fold standardized barriers and remove yield scaling/clipping.
"""

from __future__ import annotations

import argparse
import csv
import importlib
import json
import math
import random
from pathlib import Path
from typing import Any

try:
    from sar_runtime.training_protocol import verify_asset_files  # pyright: ignore[reportMissingImports, reportMissingModuleSource]  # isort: skip
except ImportError:
    from tricycle_core.training_protocol import verify_asset_files



def reaction_text(smiles: str) -> str:
    from rdkit import Chem

    parts = smiles.split(">")
    if len(parts) != 3 or not parts[0] or not parts[2]:
        raise ValueError("expected reactants>reagents>products")
    normalized = []
    for part in parts:
        if not part:
            normalized.append(" ")
            continue
        mol = Chem.MolFromSmiles(part)
        if mol is None:
            raise ValueError("invalid reaction SMILES")
        for atom in mol.GetAtoms():
            atom.SetAtomMapNum(0)
        normalized.append(Chem.MolToSmiles(mol, canonical=True, isomericSmiles=True))
    return f"REACTANT:{normalized[0]}REAGENT:{normalized[1]}PRODUCT:{normalized[2]}"


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(newline="") as stream:
        return list(csv.DictReader(stream))


def load_fold(workspace: Path, fold: int) -> tuple[list[dict[str, str]], dict[str, list[int]]]:
    rows = read_csv(workspace / "data/snapshots/ground-truth/dataset.csv")
    roles = {
        role: [
            int(r["row_index"])
            for r in read_csv(
                workspace / f"data/splits/ground-truth-5fold/fold_{fold}/{role}.indices.csv"
            )
        ]
        for role in ("train", "val", "test")
    }
    flattened = [i for indices in roles.values() for i in indices]
    if sorted(flattened) != list(range(len(rows))) or any(not v for v in roles.values()):
        raise ValueError("invalid fold coverage")
    groups = {
        role: {rows[i]["substrate_group"] for i in indices} for role, indices in roles.items()
    }
    if any(
        groups[a] & groups[b] for a, b in [("train", "val"), ("train", "test"), ("val", "test")]
    ):
        raise ValueError("substrate group leakage")
    return rows, roles


def train(workspace: Path, spec: dict, fold: int, *, smoke: bool = False) -> None:
    torch = importlib.import_module("torch")
    nn = torch.nn
    transformers = importlib.import_module("transformers")
    AutoTokenizer = transformers.AutoTokenizer
    T5Config = transformers.T5Config
    T5ForConditionalGeneration = transformers.T5ForConditionalGeneration

    seed = int(spec["seed"])
    random.seed(seed)
    torch.manual_seed(seed)
    torch.cuda.manual_seed_all(seed)
    torch.backends.cudnn.benchmark = False
    torch.backends.cudnn.deterministic = True
    assets = workspace / spec["checkpoint_directory"]
    missing = verify_asset_files(assets, spec["files"])
    if missing:
        raise FileNotFoundError(f"Missing pinned ReactionT5 assets: {missing}")
    config = T5Config.from_pretrained(assets, local_files_only=True)

    module_base: Any = nn.Module

    class BarrierModel(module_base):
        def __init__(self) -> None:
            super().__init__()
            self.model = T5ForConditionalGeneration(config)
            h = config.d_model
            for name, dims in [
                ("fc1", (h, h // 2)),
                ("fc2", (h, h // 2)),
                ("fc3", (h, h)),
                ("fc4", (h, h)),
                ("fc5", (h, 1)),
            ]:
                setattr(self, name, nn.Linear(*dims))

        def forward(self, inputs: dict[str, Any]) -> Any:
            hidden = self.model.encoder(**inputs).last_hidden_state
            start = torch.full(
                (len(hidden), 1),
                config.decoder_start_token_id,
                dtype=torch.long,
                device=hidden.device,
            )
            decoded = self.model.decoder(
                input_ids=start, encoder_hidden_states=hidden
            ).last_hidden_state
            joined = torch.cat((self.fc1(decoded[:, 0]), self.fc2(hidden[:, 0])), dim=1)
            return self.fc5(self.fc4(self.fc3(joined))).squeeze(-1)

    model = BarrierModel()
    checkpoint = torch.load(assets / "pytorch_model.bin", map_location="cpu", weights_only=True)
    loaded = model.load_state_dict(checkpoint, strict=False)
    # The pinned checkpoint omits T5's unused LM projection while its config
    # disables tied embeddings. The barrier adapter reads encoder/decoder states
    # directly; every checkpoint key and every other model key must still match.
    expected_missing = {"model.lm_head.weight"} if not config.tie_word_embeddings else set()
    if set(loaded.missing_keys) != expected_missing or loaded.unexpected_keys:
        raise RuntimeError(
            f"ReactionT5 checkpoint key mismatch: missing={loaded.missing_keys}, "
            f"unexpected={loaded.unexpected_keys}"
        )
    nn.init.normal_(model.fc5.weight, std=0.01)
    nn.init.zeros_(model.fc5.bias)
    device = torch.device(spec["device"])
    model.to(device)
    tokenizer = AutoTokenizer.from_pretrained(assets, local_files_only=True)
    rows, roles = load_fold(workspace, fold)
    if smoke:
        selected = roles["train"][:2]
        rows = [rows[i] for i in selected]
        roles = {"train": list(range(len(rows))), "val": [], "test": []}
    texts = [reaction_text(r["rxn_smiles"]) for r in rows]
    max_length = int(spec["max_length"])
    encoded = tokenizer(texts, truncation=False, padding=False)
    if max(map(len, encoded["input_ids"])) > max_length:
        raise ValueError("input exceeds max_length; refuse silent structural truncation")
    inputs = tokenizer(
        texts, padding="max_length", max_length=max_length, truncation=False, return_tensors="pt"
    )
    target = spec["target"]
    y = torch.tensor([float(r[target]) for r in rows], dtype=torch.float32)
    mean = y[roles["train"]].mean()
    scale = y[roles["train"]].std(unbiased=False).clamp(min=1e-8)
    normalized = (y - mean) / scale
    output = (
        workspace
        / ("smoke-results" if smoke else "model-runs")
        / f"reactiont5/reactiont5/fold_{fold}"
    )
    output.mkdir(parents=True, exist_ok=False)
    batch_size = int(spec["batch_size"])
    optimizer = torch.optim.AdamW(
        model.parameters(),
        lr=float(spec["learning_rate"]),
        weight_decay=float(spec["weight_decay"]),
    )

    def predict(indices: list[int]) -> list[float]:
        model.eval()
        values = []
        with torch.no_grad():
            for offset in range(0, len(indices), batch_size):
                ids = indices[offset : offset + batch_size]
                batch = {k: v[ids].to(device) for k, v in inputs.items()}
                values.extend((model(batch).cpu() * scale + mean).tolist())
        if not all(math.isfinite(v) for v in values):
            raise ValueError("nonfinite predictions")
        return values

    best, stale, history = math.inf, 0, []
    for epoch in range(int(spec["epochs"])):
        model.train()
        indices = list(roles["train"])
        random.shuffle(indices)
        for offset in range(0, len(indices), batch_size):
            ids = indices[offset : offset + batch_size]
            batch = {k: v[ids].to(device) for k, v in inputs.items()}
            optimizer.zero_grad(set_to_none=True)
            loss = nn.functional.mse_loss(model(batch), normalized[ids].to(device))
            if not torch.isfinite(loss):
                raise ValueError("nonfinite training loss")
            loss.backward()
            if smoke:
                gradients = [p.grad for p in model.parameters() if p.grad is not None]
                if not gradients or not all(
                    g.is_cuda and bool(torch.isfinite(g).all()) for g in gradients
                ):
                    raise ValueError("Invalid ReactionT5 smoke gradients")
                if not any(bool((g != 0).any()) for g in gradients):
                    raise ValueError("All ReactionT5 gradients are zero")
                print(
                    "ReactionT5 real pretrained model backward passed; no optimizer update",
                    flush=True,
                )
                return
            nn.utils.clip_grad_norm_(model.parameters(), 1.0)
            optimizer.step()
        prediction = predict(roles["val"])
        mae = sum(
            abs(p - float(y[i])) for i, p in zip(roles["val"], prediction, strict=True)
        ) / len(prediction)
        history.append({"epoch": epoch + 1, "validation_mae": mae})
        if mae < best:
            best, stale = mae, 0
            torch.save(model.state_dict(), output / "best.pt")
        else:
            stale += 1
        if stale >= int(spec["patience"]):
            break
    model.load_state_dict(torch.load(output / "best.pt", map_location=device, weights_only=True))
    prediction = predict(roles["test"])
    records = [
        {
            "row_index": i,
            "reaction_id": rows[i]["reaction_id"],
            "target_name": target,
            "y_true": rows[i][target],
            "y_pred": p,
        }
        for i, p in zip(roles["test"], prediction, strict=True)
    ]
    with (output / "predictions.csv").open("w", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(records[0]))
        writer.writeheader()
        writer.writerows(records)
    (output / "training.json").write_text(
        json.dumps(
            {
                "spec": spec,
                "fold": fold,
                "target_mean": float(mean),
                "target_scale": float(scale),
                "validation_history": history,
                "checkpoint_selection": "validation_mae",
                "torch_version": torch.__version__,
            },
            indent=2,
        )
        + "\n"
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--fold", type=int, choices=range(5), required=True)
    parser.add_argument("--check-only", action="store_true")
    args = parser.parse_args()
    spec = json.loads(args.config.read_text())
    workspace = Path.cwd()
    if args.check_only:
        rows, roles = load_fold(workspace, args.fold)
        for row in rows:
            reaction_text(row["rxn_smiles"])
        print(
            json.dumps(
                {
                    "fold": args.fold,
                    "counts": {k: len(v) for k, v in roles.items()},
                    "missing_assets": verify_asset_files(
                        workspace / spec["checkpoint_directory"], spec["files"]
                    ),
                    "training_started": False,
                }
            )
        )
    else:
        train(workspace, spec, args.fold)


if __name__ == "__main__":
    main()
