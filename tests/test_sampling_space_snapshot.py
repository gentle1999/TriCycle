from __future__ import annotations

import csv
import hashlib
import json
from pathlib import Path
from typing import Any

import pytest
from tricycle_reaction_db.pools import export_database_collections
from tricycle_sampling.active_learning import load_experiment_spec, prepare_experiment
from tricycle_sampling.space_snapshot import freeze_database_space, verify_frozen_space


def _source(tmp_path: Path, *, complete: bool = True, unlabeled: bool = False) -> Path:
    registry = tmp_path / "registry.csv"
    with registry.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=[
            "ene_id", "ene_smiles", "diene_id", "diene_smiles", "old_gt_energy",
        ])
        writer.writeheader()
        for ident, smiles in (("1", "Cl/C=C/Cl"), ("3", "Cl/C=C\\Cl")):
            writer.writerow({"ene_id": ident, "ene_smiles": smiles, "diene_id": "2",
                             "diene_smiles": "C=CC=C", "old_gt_energy": "SECRET_EVAL_VALUE"})
    config = tmp_path / "request.toml"
    config.write_text(
        f"substrate_registry = {json.dumps(str(registry))}\n"
        '[export_filters]\nhas_activation_gibbs_free_energy=true\n'
        '[[projects]]\nname="ground-truth"\nrole="evaluation"\n'
        'project_id="00000000-0000-7000-8000-000000000201"\n'
        + "".join(f'[[projects]]\nname="{name}"\nrole="candidate"\nproject_id="{name}"\n'
                  for name in ("autode", "dpa-fined-rits-da", "rits-zero-shot-da"))
    )

    class Adapter:
        def export_thermodynamics_csv(
            self, request: Any, destination: Path, *, has_activation_gibbs_free_energy: bool | None = None,
            has_reaction_gibbs_free_energy: bool | None = None,
        ) -> dict[str, Any]:
            destination.parent.mkdir(parents=True, exist_ok=True)
            rows = [{"mapped_reaction_id": "shared-ts",
                     "mapped_reaction_smiles": f"{smi}.C=CC=C>>C1CC=CCC1",
                     "activation_gibbs_free_energy_kcal_mol": str(10 + i)}
                    for i, smi in enumerate(("Cl/C=C/Cl", "Cl/C=C\\Cl"))]
            if not complete:
                rows = rows[:1]
            if unlabeled:
                rows.append({"mapped_reaction_id": "unlabeled",
                             "mapped_reaction_smiles": "N.C=CC=C>>C1CC=CCC1",
                             "activation_gibbs_free_energy_kcal_mol": ""})
            with destination.open("w", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
                writer.writeheader()
                writer.writerows(rows)
            result = {"project_id": request.project_id, "row_count": len(rows),
                      "arguments": {"has_activation_gibbs_free_energy": has_activation_gibbs_free_energy},
                      "sha256": hashlib.sha256(destination.read_bytes()).hexdigest()}
            destination.with_suffix(".receipt.json").write_text(json.dumps(result))
            return result

    root = tmp_path / "source"
    export_database_collections(config, live=True, adapter=Adapter(), output_root=root)
    return root


def test_freeze_preserves_shared_ts_rows_and_isolates_registry_energy(tmp_path: Path) -> None:
    source = _source(tmp_path)
    pin = tmp_path / "pin.toml"
    result = freeze_database_space(source, pin, snapshot_id="space-v1", output_root=tmp_path / "frozen")
    root = Path(result["root"])
    assert not (root / "adapter-source").exists()
    assert not list(root.rglob("*.py"))
    assert result["expected_pair_count"] == 2
    assert result["pools"]["ground-truth"]["labeled_pairs"] == 2
    relative = "evaluation/ground-truth/reaction-collection.csv"
    assert (root / relative).read_bytes() == (source / relative).read_bytes()
    assert "SECRET_EVAL_VALUE" not in (root / "substrate-registry.csv").read_text()
    assert "SECRET_EVAL_VALUE" in (root / "evaluation/provenance/substrate-registry-source.csv").read_text()
    with (root / "substrate-registry.csv").open() as handle:
        assert csv.DictReader(handle).fieldnames == ["ene_id", "ene_smiles", "diene_id", "diene_smiles"]
    with pytest.raises(ValueError, match="pin already exists"):
        freeze_database_space(source, pin, snapshot_id="space-v1", output_root=tmp_path / "another")


def test_frozen_verification_rejects_data_manifest_and_inventory_changes(tmp_path: Path) -> None:
    pin = tmp_path / "pin.toml"
    result = freeze_database_space(_source(tmp_path), pin, snapshot_id="space-v1", output_root=tmp_path / "frozen")
    root = Path(result["root"])
    candidate = root / "candidates/autode/reaction-collection.csv"
    original = candidate.read_bytes()
    candidate.write_bytes(original + b"tampered")
    with pytest.raises(ValueError, match="file changed"):
        verify_frozen_space(pin)
    candidate.write_bytes(original)
    unexpected = root / "another-version.csv"
    unexpected.write_text("untracked data")
    with pytest.raises(ValueError, match="inventory changed"):
        verify_frozen_space(pin)
    unexpected.unlink()
    manifest = root / "frozen-space.json"
    manifest.write_bytes(manifest.read_bytes() + b" ")
    with pytest.raises(ValueError, match="manifest hash changed"):
        verify_frozen_space(pin)


def test_freeze_rejects_incomplete_ground_truth(tmp_path: Path) -> None:
    source = _source(tmp_path, complete=False)
    with pytest.raises(ValueError, match="complete registry Cartesian product"):
        freeze_database_space(source, tmp_path / "pin.toml", snapshot_id="space-v1", output_root=tmp_path / "frozen")
    assert not (tmp_path / "pin.toml").exists()


def test_freeze_rejects_gt_as_candidate(tmp_path: Path) -> None:
    source = _source(tmp_path)
    manifest_path = source / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["projects"][0]["role"] = "candidate"
    manifest_path.write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match="isolate ground truth"):
        freeze_database_space(source, tmp_path / "pin.toml", snapshot_id="space-v1", output_root=tmp_path / "frozen")


def test_experiment_resolves_pin_and_rechecks_before_preparing(tmp_path: Path) -> None:
    pin = tmp_path / "pin.toml"
    frozen = tmp_path / "frozen"
    freeze_database_space(_source(tmp_path), pin, snapshot_id="space-v1", output_root=frozen)
    config = tmp_path / "experiment.toml"
    config.write_text(
        'pair_batch_size=1\nround_count=2\nexpected_pair_count=2\n'
        f'[dataset]\nfrozen_space_config={json.dumps(str(pin))}\n'
        + "".join(f'[projects.{name}]\nresources="costs.csv"\n'
                  for name in ("autode", "dpa-fined-rits-da", "rits-zero-shot-da"))
    )
    spec = load_experiment_spec(config)
    assert spec.ground_truth_collection == frozen / "evaluation/ground-truth/reaction-collection.csv"
    assert spec.projects[0].collection == frozen / "candidates/autode/reaction-collection.csv"
    raw = frozen / "audit/source-export/candidates/autode/database-export.csv"
    raw.write_text(raw.read_text() + "changed after loading spec")
    with pytest.raises(ValueError, match="file changed"):
        prepare_experiment(spec, output_root=tmp_path / "experiment")
    assert not (tmp_path / "experiment").exists()


def test_freeze_rejects_broken_native_label_filter_without_local_fallback(tmp_path: Path) -> None:
    source = _source(tmp_path, unlabeled=True)
    with pytest.raises(ValueError, match="contains an unlabeled row"):
        freeze_database_space(source, tmp_path / "pin.toml", snapshot_id="labeled-v3", output_root=tmp_path / "frozen")
    assert not (tmp_path / "frozen").exists()


def test_freeze_requires_native_filter_provenance(tmp_path: Path) -> None:
    source = _source(tmp_path)
    path = source / "evaluation/ground-truth/database-export.receipt.json"
    receipt = json.loads(path.read_text())
    receipt.pop("arguments")
    path.write_text(json.dumps(receipt))
    with pytest.raises(ValueError, match="requires the native database activation-label filter"):
        freeze_database_space(source, tmp_path / "pin.toml", snapshot_id="labeled-v3", output_root=tmp_path / "frozen")
