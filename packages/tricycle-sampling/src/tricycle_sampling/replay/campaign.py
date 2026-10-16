#!/usr/bin/env python3
"""Persistent, independent intelligent active-learning trajectories on replay labels.

Run as ``python -m tricycle_sampling.replay.campaign``. Preparation is entirely local.
External work requires --live; SAR submissions additionally require --allow-submit.
An injected BohrGateway and command agent are the only external boundaries.
"""

from __future__ import annotations

import argparse
import csv
import fcntl
import hashlib
import json
import math
import os
import plistlib
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import tomllib
import zipfile
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Protocol

from tricycle_bohrsubmit.archives import extract_archive
from tricycle_bohrsubmit.lifecycle import classify_status
from tricycle_core import BohrJobSpec, timestamped_run_directory
from tricycle_core import record_io as rio
from tricycle_core.asset_store import externalize_tree, put_file, put_tree, resolve_tree
from tricycle_core.io import copy_snapshot_tree as copy_tree
from tricycle_core.protocols import BohrGateway
from tricycle_core.record_io import dump_toml
from tricycle_core.record_io import file_sha256 as digest
from tricycle_core.record_io import write_json as save
from tricycle_sar.predictions import aggregate_pairs
from tricycle_sar.sampling_runtime import stage as stage_sar_runtime
from tricycle_sar.sampling_runtime import worker_launcher

from tricycle_sampling import structure_analysis as structure
from tricycle_sampling import trajectory_worker as baseline
from tricycle_sampling.experiment import EXPERIMENT_SAMPLING_SCHEMA
from tricycle_sampling.protocols import LITERATURE_BASELINE_STRATEGIES
from tricycle_sampling.replay import baselines as replay_baselines
from tricycle_sampling.replay import core_hours as replay_core_hours
from tricycle_sampling.replay import cost_guidance as replay_cost_guidance
from tricycle_sampling.replay import mae as replay_mae
from tricycle_sampling.replay.brief import build as build_decision_brief
from tricycle_sampling.replay.decision import (
    DecisionOutputError,
    DecisionTimeoutError,
    archive_timeout,
)
from tricycle_sampling.replay.decision import attempt_limit as decision_attempt_limit
from tricycle_sampling.replay.decision import reject as reject_decision
from tricycle_sampling.replay.decision import validate as validate_decision
from tricycle_sampling.replay.worker import pair

from .inputs import input_file, verify_inputs
from .selection import select_round

ROOT = rio.resolve(Path.cwd())
PACKAGE_ROOT = rio.resolve(Path(__file__)).parents[1]


def read(path: Path) -> dict[str, Any]:
    return dict(json.loads(rio.read_text(path, encoding="utf-8")))


def table(path: Path) -> list[dict[str, str]]:
    return baseline._read_csv(path)


def write_table(path: Path, rows: list[dict[str, Any]], fields: list[str] | None = None) -> None:
    baseline._write_csv(path, fields or list(rows[0]), rows)



def hashes(directory: Path) -> dict[str, str]:
    return {
        str(p.relative_to(directory)): digest(p)
        for p in sorted(rio.rglob(directory, "*"))
        if rio.is_file(p) and "__pycache__" not in p.parts
    }


def check_hashes(directory: Path, expected: dict[str, str]) -> None:
    for name, value in expected.items():
        path = rio.resolve(directory / name)
        path.relative_to(rio.resolve(directory))
        if not rio.is_file(path) or digest(path) != value:
            raise ValueError(f"frozen artifact changed or missing: {path}")


def check_controller(root: Path) -> None:
    """Validate original bindings and explicitly authorized, source-bound amendments."""
    config_path = root / "experiment.json"
    config = read(config_path)
    expected = dict(config.get("controller_hashes", {}))
    record = root / "storage-compatibility.json"
    if rio.exists(record):
        binding = read(record)
        if (binding.get("schema") != "tricycle.replay-storage-compatibility/v1"
                or binding.get("experiment_sha256") != digest(config_path)
                or not binding.get("authorization") or not binding.get("reason")):
            raise ValueError("Invalid audited storage compatibility binding")
        evidence = Path(binding["evidence_path"])
        if digest(evidence) != binding["evidence_sha256"]:
            raise ValueError("Storage compatibility evidence changed")
        for name, change in binding["changes"].items():
            if name not in expected or change["before"] != expected[name]:
                raise ValueError("Storage compatibility original controller differs")
            expected[name] = change["after"]
    record = root / 'decision-controller-amendment.json'
    if rio.exists(record):
        binding = read(record)
        if (binding.get('schema') != 'tricycle.decision-controller-amendment/v1'
                or binding.get('experiment_sha256') != digest(config_path)
                or not binding.get('authorization') or not binding.get('reason')
                or not binding.get('changes')):
            raise ValueError('Invalid decision controller amendment')
        if digest(Path(binding['evidence_path'])) != binding['evidence_sha256']:
            raise ValueError('Decision amendment evidence changed')
        if binding.get('bohr_gateway_sha256'):
            from tricycle_bohrsubmit import gateway as bohr_gateway
            if digest(Path(bohr_gateway.__file__)) != binding['bohr_gateway_sha256']:
                raise ValueError('Amended Bohr gateway changed')
        allowed = {'replay/campaign.py', 'replay/mae.py', 'replay/decision.py'}
        for name, change in binding['changes'].items():
            if name not in allowed or name not in expected or change['before'] != expected[name]:
                raise ValueError('Decision amendment original controller differs or scope is invalid')
            expected[name] = change['after']
    record = root / "live-records-amendment.json"
    persistence = config.get("record_backend_hashes")
    if rio.exists(record):
        binding = read(record)
        if (binding.get("schema") != "tricycle.live-records-amendment/v1"
                or binding.get("experiment_sha256") != digest(config_path)
                or not binding.get("authorization") or not binding.get("reason")
                or digest(Path(binding["evidence_path"])) != binding["evidence_sha256"]):
            raise ValueError("Invalid audited live record amendment")
        for name, change in binding["changes"].items():
            if name not in expected or change["before"] != expected[name]:
                raise ValueError("Live record amendment original controller differs")
            expected[name] = change["after"]
        persistence = binding["record_backend_hashes"]
    if persistence is not None:
        from .runtime_records import source_hashes
        if persistence != source_hashes():
            raise ValueError("Live record persistence implementation changed")
    check_hashes(PACKAGE_ROOT, expected)




def prepare(config_path: Path, directory: Path | None) -> Path:
    with tempfile.TemporaryDirectory(prefix="tricycle-replay-prepare-") as temporary:
        return _prepare(config_path, directory, Path(temporary))


def _prepare(config_path: Path, directory: Path | None, input_root: Path) -> Path:
    config = tomllib.loads(rio.read_text(config_path))
    if "acquisition_policy" in config:
        raise ValueError("acquisition_policy is no longer supported; prepare a new run with decision_protocol")
    from .runtime_records import source_hashes
    config["record_backend_hashes"] = source_hashes()
    mae_protocol = replay_mae.settings(config)
    if mae_protocol is not None:
        config['decision_protocol'] = mae_protocol
        config['controller_hashes'] = {
            str(path.relative_to(PACKAGE_ROOT)): digest(path)
            for path in sorted(rio.rglob(PACKAGE_ROOT, '*.py'))
        }
    strategy = config.get("sampling_strategy", "intelligent")
    schema = config.get("sampling_experiment")
    if schema is not None and schema != EXPERIMENT_SAMPLING_SCHEMA:
        raise ValueError("Unsupported sampling_experiment schema; prepare a new run")
    if strategy in LITERATURE_BASELINE_STRATEGIES:
        config["sampling_experiment"] = EXPERIMENT_SAMPLING_SCHEMA
        config["fixed_seed"] = int(config.get("fixed_seed", 42))
    if strategy != "intelligent":
        if strategy not in replay_baselines.STRATEGIES:
            raise ValueError("baseline requires supported strategy and no agent policy")
        config["baseline_policy_sha256"] = digest(Path(__file__).with_name("baselines.py"))
    readiness_path = rio.resolve(Path(config["readiness"]))
    readiness = tomllib.loads(rio.read_text(readiness_path))
    if readiness["experiment_mode"] != "historical-label-replay":
        raise ValueError("historical-label-replay confirmation required")
    directory = rio.resolve(directory or timestamped_run_directory(ROOT / "runs", config["experiment_id"]))
    rio.mkdir(directory, parents=True, exist_ok=False)
    private = input_root / "private"
    rio.mkdir(private)
    project = str(readiness["project"])
    if project not in {"autode", "dpa-fined-rits-da", "rits-zero-shot-da"}:
        raise ValueError(f"unsupported replay project: {project}")
    config["project"] = project
    source = Path(readiness["baseline_run"]) / "bohr-input" / project
    common = source / "common"
    pool = table(common / "pair-pool.csv")
    if len(pool) != 1600 or len({r["pair_id"] for r in pool}) != 1600:
        raise ValueError("expected the baseline 1600-pair space")
    for name in ("pair-pool.csv", "project-labels.csv"):
        rio.copyfile(common / name, private / name)
    evaluation = Path(readiness["evaluation"]["ground_truth_path"])
    if digest(evaluation) != readiness["evaluation"]["ground_truth_sha256"]:
        raise ValueError("ground truth snapshot changed")
    rio.copyfile(evaluation, private / "evaluation.csv")
    assets = input_root / "assets"
    rio.mkdir(assets)
    sar_backend = stage_sar_runtime(assets, config.get("rxnresid"))
    model_revision = sar_backend["source_revision"]
    for key in ("image", "machine_type"):
        if config["bohr"][key] != sar_backend["environment"][key]:
            raise ValueError(f"sampling executor differs from shared SAR backend: {key}")
    launcher = worker_launcher(module="tricycle_sampling.replay.worker")
    rio.write_text(assets / "run.sh", launcher)
    shared_prior = config.get("prior_structures")
    if shared_prior:
        prior = rio.resolve(Path(shared_prior["path"]))
        if digest(prior) != shared_prior["sha256"]:
            raise ValueError("shared structural enumeration changed")
        shared_routes = table(prior)
        if {pair(row) for row in shared_routes} != {row["pair_id"] for row in pool}:
            raise ValueError("shared structural enumeration does not cover pool")
        if any(float(row["G_T_activate"]) != 0 for row in shared_routes):
            raise ValueError("shared prior must not contain labels")
        rio.copyfile(prior, assets / "prior-structures.csv")
    else:
        # Enumerate structures without reading oracle targets or success counts.
        from tricycle_reactionopt.diels_alder import enumerate_diels_alder_reactions
        from tricycle_reactionopt.models import (
            DielsAlderEnumerationRequest,
            DielsAlderSubstratePair,
        )

        routes: list[dict[str, Any]] = []
        for index, row in enumerate(pool):
            result = enumerate_diels_alder_reactions(
                DielsAlderEnumerationRequest(
                    pairs=(
                        DielsAlderSubstratePair(
                            row["ene_smiles"], row["diene_smiles"], row["ene_id"], row["diene_id"]
                        ),
                    )
                )
            )
            if not result.complete or not result.candidates:
                raise ValueError(f"incomplete structural enumeration: {row['pair_id']}")
            for route_index, candidate in enumerate(result.candidates):
                routes.append(
                    {
                        "path_id": candidate["reaction_id"],
                        "source_mapped_reaction_id": candidate["reaction_id"],
                        "source_row_index": len(routes),
                        "rxn_smiles": candidate["mapped_reaction_smiles"],
                        "G_T_activate": 0,
                        "ene_id": row["ene_id"],
                        "diene_id": row["diene_id"],
                        "prod_id": route_index,
                    }
                )
            if index % 100 == 0:
                print(f"enumerated {index + 1}/1600 pairs", flush=True)
        write_table(assets / "prior-structures.csv", routes)
    from tricycle_sar.rxnresid import freeze_route_vocabulary
    freeze_route_vocabulary(assets / "prior-structures.csv", assets / "route-id-vocabulary.json")
    epochs: set[int] = set()
    for initial in readiness["initial_sets"].values():
        seed = int(initial["seed"])
        initial_csv = Path(initial["initial_csv"])
        if digest(initial_csv) != initial["initial_csv_sha256"]:
            raise ValueError("initial set changed")
        ids = [row["pair_id"] for row in table(initial_csv)]
        if ids != initial["pair_ids"] or len(ids) != 40 or len(set(ids)) != 40:
            raise ValueError("initial IDs differ from verified baseline set")
        if mae_protocol is not None:
            valid = {pair(r) for r in table(private / 'project-labels.csv') if pair(r) in set(ids)}
            if len(valid) < 10:
                raise ValueError('initial set has too few valid labeled pairs for auxiliary monitoring')
            panel = sorted(valid, key=lambda p: hashlib.sha256(('validation42:' + p).encode()).hexdigest())[:8]
            config.setdefault('validation_panels', {})[str(seed)] = panel
        # Only read recipe metadata. No baseline model, training output or later
        # selection enters the new trajectory.
        with zipfile.ZipFile(initial["source_archive"]) as archive:
            member = initial["source_member"].split("/round-0000/")[0] + "/trajectory.json"
            recipe = json.loads(archive.read(member))
        epochs.add(int(recipe["training_epochs"]))
        seed_dir = directory / f"seed-{seed:02d}"
        rio.mkdir(seed_dir)
        from .runtime_records import initialize
        initialize(seed_dir)
        save(
            seed_dir / "state.json",
            {
                "seed": seed,
                "round": 0,
                "phase": "prepare",
                "selected_ids": ids,
                "new_ids": ids,
                "failures": 0,
                "revision": 0,
            },
        )
    if len(epochs) != 1:
        raise ValueError("baseline recipes disagree")
    baseline_epochs = epochs.pop()
    training_epochs = int(config.get("training", {}).get("epochs", baseline_epochs))
    if training_epochs < 1:
        raise ValueError("training epochs must be positive")
    config.update(
        {
            "schema": "tricycle.intelligent-replay/v1",
            "round_count": 40,
            "batch_size": 40,
            "baseline_training_epochs": baseline_epochs,
            "training": {"epochs": training_epochs, "batch_size_paths": 512, "device": "auto"},
            "evaluation_sha256": digest(private / "evaluation.csv"),
            "source_revision": model_revision,
            "sar_backend": sar_backend,
            "initialization": "from-scratch; previous checkpoint only from same new trajectory",
            "assets_hashes": hashes(assets),
            "private_hashes": hashes(private),
        }
    )
    store = rio.resolve(Path(config.get("asset_store", ROOT / "artifacts/task-assets")))
    config["asset_store"] = str(store)
    config["asset_references"] = {
        "assets": put_tree(assets, store),
        "private": put_tree(private, store),
    }
    save(directory / "asset-references.json", config["asset_references"])
    rio.copyfile(readiness_path, directory / "readiness.toml")
    save(directory / "experiment.json", config)
    save(directory / "experiment-hash.json", {"sha256": digest(directory / "experiment.json")})
    return directory


def stage_round(root: Path, seed_dir: Path, state: dict[str, Any], config: dict[str, Any]) -> Path:
    with tempfile.TemporaryDirectory(prefix="tricycle-round-prepare-") as temporary:
        return _stage_round(root, seed_dir, state, config, Path(temporary) / "input")


def _stage_round(root: Path, seed_dir: Path, state: dict[str, Any], config: dict[str, Any],
                 bundle: Path) -> Path:
    current = seed_dir / f"round-{state['round']:04d}"
    rio.mkdir(current, exist_ok=True)
    if rio.exists(current / "input-hashes.json"):
        verify_inputs(current)
        return current
    if rio.exists(bundle):
        # Only an unsubmitted, partially staged directory is disposable.
        rio.rmtree(bundle)
    rio.mkdir(bundle)
    references = config["asset_references"]
    assets = resolve_tree(references["assets"])
    private = resolve_tree(references["private"])
    store = Path(config["asset_store"])
    fixed: dict[str, Any] = {"schema": "tricycle.asset-input/v1", "trees": [references["assets"]],
             "files": {"evaluation.csv": put_file(private / "evaluation.csv", store)}}
    selected = set(state["selected_ids"])
    pool = table(private / "pair-pool.csv")
    if not selected.issubset({r["pair_id"] for r in pool}) or len(selected) != 40 * (
        state["round"] + 1
    ):
        raise ValueError("selected set violates round budget or candidate space")
    labels = [r for r in table(private / "project-labels.csv") if pair(r) in selected]
    write_table(bundle / "training.csv", labels, list(baseline.NATIVE_COLUMNS))
    candidates = [r for r in table(assets / "prior-structures.csv") if pair(r) not in selected]
    write_table(bundle / "candidates.csv", candidates, list(baseline.NATIVE_COLUMNS))
    # Keep the true evaluation data in the worker/evaluation boundary only.
    # Evaluation bytes are referenced, never repeated in each local round.
    if state["round"]:
        previous = seed_dir / f"round-{state['round'] - 1:04d}" / "result/checkpoint.pt"
        receipt = read(previous.parent / "worker-report.json")
        if config.get("sar_backend") and (receipt.get("sar_backend") != config["sar_backend"]
                or receipt.get("source_revision") != config["source_revision"]):
            raise ValueError("previous checkpoint belongs to a different SAR backend")
        if digest(previous) != receipt["outputs"]["checkpoint.pt"]:
            raise ValueError("previous trajectory checkpoint changed")
        fixed["files"]["previous.pt"] = put_file(rio.resolve(previous), store)
    new_labels = [r for r in labels if pair(r) in state["new_ids"]]
    if config.get('decision_protocol'):
        write_table(bundle / 'newly-revealed.csv', new_labels, list(baseline.NATIVE_COLUMNS))
    request = {
        "round_index": state["round"],
        "training": config["training"],
        "source_revision": config["source_revision"],
        **({"sar_backend": config["sar_backend"]} if "sar_backend" in config else {}),
        "evaluation_sha256": config["evaluation_sha256"],
        "reuse_checkpoint": bool(state["round"] and not new_labels),
        "need_decision": state["round"] + 1 < config["round_count"],
        "input_hashes": {**config["assets_hashes"],
                         **{name: ref["sha256"] for name, ref in fixed["files"].items()},
                         **hashes(bundle)},
    }
    if config.get('decision_protocol'):
        request['validation_pair_ids'] = config['validation_panels'][str(state['seed'])]
        request['mae_protocol'] = config['decision_protocol']
    save(bundle / "request.json", request)
    fixed["trees"].append(put_tree(bundle, store))
    with rio.transaction(current):
        save(current / "input-assets.json", fixed)
        save(current / "input-hashes.json", hashes(bundle))
        save(current / "input-assets-hash.json", {"sha256": digest(current / "input-assets.json")})
        save(
            current / "reveal.json",
            {
                "selected_ids": state["selected_ids"],
                "new_ids": state["new_ids"],
                "training_rows": len(labels),
                "new_label_rows": len(new_labels),
                "cumulative_sampling_cost_core_hours": sum(
                    float(r["materialized_sampling_cost_core_hours"])
                    for r in pool
                    if r["pair_id"] in selected
                ),
            },
        )
    return current


class ReplayGateway(BohrGateway, Protocol):
    def create_job_group(self, name: str, project_id: str) -> str: ...


def ensure_job_group(root: Path, config: dict[str, Any], gateway: ReplayGateway) -> str:
    """One durable group per experiment, shared by all seeds and later steps."""
    if config["bohr"].get("shared_group_dir"):
        shared = rio.resolve(Path(config["bohr"]["shared_group_dir"]))
        rio.mkdir(shared, parents=True, exist_ok=True)
        group_config = {**config, "experiment_id": config["bohr"]["job_group_name"],
                        "bohr": {k:v for k,v in config["bohr"].items() if k != "shared_group_dir"}}
        group_id = ensure_job_group(shared, group_config, gateway)
        expected = {"experiment_id": config["experiment_id"], "project_id": str(config["bohr"]["project_id"]),
                    "job_group_id": group_id, "shared_group_source": str(shared / "job-group.json")}
        receipt = root / "job-group.json"
        if rio.exists(receipt) and read(receipt) != expected:
            raise ValueError("experiment shared-group receipt changed")
        save(receipt, expected)
        return group_id
    with rio.open(root / ".job-group.lock", "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        receipt = root / "job-group.json"
        if rio.exists(receipt):
            record = read(receipt)
            if record["experiment_id"] != config["experiment_id"] or record["project_id"] != str(
                config["bohr"]["project_id"]
            ):
                raise ValueError("job group receipt belongs to another experiment/project")
            group_id = str(record["job_group_id"]).strip()
            if not group_id:
                raise ValueError("job group receipt has no ID")
            return group_id
        marker = root / "job-group-inflight.json"
        if rio.exists(marker):
            raise ValueError("job group creation uncertain: reconcile platform group before retry")
        group_name = config["bohr"].get("job_group_name", config["experiment_id"])
        save(
            marker,
            {
                "experiment_id": config["experiment_id"],
                "name": group_name,
                "project_id": str(config["bohr"]["project_id"]),
                "started_at": time.time(),
            },
        )
        group_id = gateway.create_job_group(group_name, str(config["bohr"]["project_id"]))
        if not str(group_id).strip():
            raise ValueError("Bohr returned an empty job group ID")
        save(
            receipt,
            {
                "experiment_id": config["experiment_id"],
                "name": group_name,
                "project_id": str(config["bohr"]["project_id"]),
                "job_group_id": str(group_id),
                "created_at": time.time(),
            },
        )
        return str(group_id)


def job_spec(
    current: Path, state: dict[str, Any], config: dict[str, Any], *, group_id: str
) -> BohrJobSpec:
    if not group_id.strip():
        raise ValueError("every replay submission requires the experiment job group")
    retries = config["bohr"].get("max_reschedule_times", 2)
    if isinstance(retries, bool) or not isinstance(retries, int) or retries < 0:
        raise ValueError("bohr.max_reschedule_times must be a non-negative integer")
    return BohrJobSpec(
        name=f"{config['experiment_id']}-s{state['seed']}-r{state['round']:04d}",
        operation="rxnresid.intelligent-replay-round",
        image=config["bohr"]["image"],
        command=("bash", "run.sh", "request.json"),
        project_id=str(config["bohr"]["project_id"]),
        machine_type=config["bohr"]["machine_type"],
        parameters={
            "input_assets_manifest": str(current / "input-assets.json"),
            **({"input_directory": str(current / "input")} if rio.is_dir(current / "input") else {}),
            "job_group_id": group_id,
            "backward_files": ["outputs/round/"],
            "extra_submit_args": ["--yes"],
            "max_run_time": config["bohr"].get("max_run_time", 1440),
            "max_reschedule_times": retries,
        },
        tags=("tricycle", "new-intelligent-trajectory"),
    )


def extract_result(current: Path, download_directory: Path | None = None) -> Path:
    download = download_directory or current / "download"
    reports = list(rio.rglob(download, "worker-report.json"))
    if not reports:
        for archive_path in sorted(rio.rglob(download, "*.zip")):
            extract_archive(archive_path, download / "extracted")
        reports = list(rio.rglob(download, "worker-report.json"))
    if len(reports) != 1:
        raise ValueError("need exactly one complete SAR worker report")
    result = reports[0].parent
    report = read(reports[0])
    if report["request_sha256"] != digest(input_file(current, "request.json")):
        raise ValueError("worker result belongs to a different request")
    required = {"checkpoint.pt", "evaluation.json", "evaluation-predictions.csv"}
    if read(input_file(current, "request.json"))["need_decision"]:
        required |= {
            "model-metrics.json",
            "candidate-predictions.csv",
            "validation-predictions.csv",
        }
    if read(input_file(current, "request.json")).get('mae_protocol'):
        required.add('pre-update-metrics.json')
    if not required.issubset(report["outputs"]):
        raise ValueError("incomplete worker report")
    references = report.get("output_references", {})
    request = read(input_file(current, "request.json"))
    for name, ref in references.items():
        if (name != "checkpoint.pt" or ref.get("input") != "previous.pt"
                or not request["reuse_checkpoint"] or request["round_index"] == 0
                or ref.get("sha256") != request["input_hashes"].get("previous.pt")
                or report["outputs"].get(name) != ref["sha256"]):
            raise ValueError("Invalid inherited worker result reference")
        if digest(input_file(current, ref["input"])) != ref["sha256"]:
            raise ValueError("Inherited checkpoint changed")
    check_hashes(result, {name: checksum for name, checksum in report["outputs"].items()
                          if name not in references})
    destination = current / "result"
    unified = rio.location(current) is not None
    if unified and not rio.exists(destination):
        from .runtime_records import work_directory
        working_result = work_directory(current, "download") / "result"
        if working_result.exists():
            shutil.rmtree(working_result)
    else:
        working_result = rio.resolve(destination) if unified else destination
    if not rio.exists(destination):
        copy_tree(result, working_result)
        for name, ref in references.items():
            rio.copyfile(input_file(current, ref["input"]), working_result / name)
    check_hashes(working_result, report["outputs"])
    files = [str(p.relative_to(download)) for p in rio.rglob(download, "*") if rio.is_file(p)]
    if not rio.is_symlink(download):
        rio.write_text(download / "download-manifest.txt", "\n".join(files) + "\n")
    write_tsv(
        current / "download-results.tsv",
        [{"worker": "sar", "status": "DOWNLOADED", "download_dir": str(current / "download")}],
    )
    config = read(current.parent.parent / "experiment.json")
    store = Path(config["asset_store"])
    for name, directory in (("result", working_result), ("download", download)):
        reference_path = current / f"{name}-asset-reference.json"
        if unified:
            if rio.is_symlink(current / name):
                if resolve_tree(read(reference_path)) != rio.resolve(current / name):
                    raise ValueError("Result asset reference differs")
            else:
                reference = put_tree(directory, store)
                with rio.transaction(current):
                    save(reference_path, reference)
                    rio.symlink_to(current / name, resolve_tree(reference))
        elif not directory.is_symlink():
            reference = externalize_tree(directory, store)
            save(reference_path, reference)
        elif resolve_tree(read(reference_path)) != directory.resolve():
            raise ValueError("Result asset reference differs")
    if unified:
        from .runtime_records import work_directory
        workspace = work_directory(current, "download")
        if workspace.exists():
            shutil.rmtree(workspace)
    return destination


def write_tsv(path: Path, rows: list[dict[str, Any]]) -> None:
    with rio.open(path, "w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]), delimiter="\t")
        writer.writeheader()
        writer.writerows(rows)


def analyze(current: Path, state: dict[str, Any]) -> Path:
    with rio.transaction(current):
        return _analyze(current, state)


def _analyze(current: Path, state: dict[str, Any]) -> Path:
    decision = current / "decision"
    task = decision / "task.json"
    if rio.exists(task):
        check_hashes(decision, read(decision / "evidence-hashes.json"))
        return task
    if rio.exists(decision):
        rio.rmtree(decision)
    rio.mkdir(decision)
    candidates = table(input_file(current, "candidates.csv"))
    labeled = table(input_file(current, "training.csv"))
    # Only acquisition-approved files enter this directory. Oracle labels,
    # evaluation metrics and private paths are deliberately absent.
    write_table(
        decision / "candidates.csv",
        [{k: v for k, v in r.items() if k != "G_T_activate"} for r in candidates],
    )
    write_table(decision / "labeled.csv", labeled)
    structure.analyze(
        candidates_path=decision / "candidates.csv",
        labeled_path=decision / "labeled.csv",
        output_dir=decision / "structure",
        candidate_id_column="path_id",
        reaction_smiles_column="rxn_smiles",
        labeled_id_column="path_id",
        labeled_reaction_smiles_column="rxn_smiles",
        fingerprint_radius=2,
        fingerprint_bits=2048,
        density_neighbors=10,
    )
    features = table(decision / "structure/structure-features.csv")
    by_pair: dict[str, list[dict[str, str]]] = defaultdict(list)
    for row in features:
        by_pair[pair(row)].append(row)
    predictions = table(current / "result/candidate-predictions.csv")
    scores: dict[str, list[dict[str, str]]] = defaultdict(list)
    for row in predictions:
        scores[pair(row)].append(row)
    if set(scores) != set(by_pair) or any(
        len(scores[p]) != len(rows) for p, rows in by_pair.items()
    ):
        raise ValueError("complete candidate prediction coverage required")
    pair_signals = aggregate_pairs(predictions, pair_key=lambda row: pair(dict(row)))
    payloads: list[dict[str, Any]] = []
    for candidate_id, rows in sorted(by_pair.items()):
        payload: dict[str, Any] = {
            key: sum(float(r[key]) for r in rows) / len(rows)
            for key in (
                "sampled_novelty",
                "pool_local_density",
                "product_ring_count",
                "product_heavy_atom_count",
            )
        }
        payload.update(
            {
                "product_count": len(rows),
                "mean_product_atom_count": sum(float(r["product_atom_count"]) for r in rows)
                / len(rows),
                "mapped_reaction_smiles": rows[0]["rxn_smiles"],
            }
        )
        payload.update(pair_signals[candidate_id])
        payloads.append({"candidate_id": candidate_id, "payload": payload})
    campaign_config = read(current.parent.parent / "experiment.json")
    if campaign_config.get("sampling_experiment") == EXPERIMENT_SAMPLING_SCHEMA:
        from tricycle_sampling.experiment import reactant_structure_features as reactant_features
        for item in payloads:
            item["payload"]["design_features"] = list(reactant_features(item["payload"]["mapped_reaction_smiles"]))
        references = {pair(row): list(reactant_features(row["rxn_smiles"])) for row in labeled}
        save(decision / "reference-features.json", references)
    metrics = read(current / "result/model-metrics.json")
    save(decision / "model-metrics.json", metrics)
    # Descriptors of held-out labels permit validation residual grouping without
    # consulting final evaluation labels or assigning full-model validation.
    validation_ids = set(metrics["validation_pairs"])
    validation = [r for r in labeled if pair(r) in validation_ids]
    train = [r for r in labeled if pair(r) not in validation_ids]
    write_table(decision / "validation.csv", validation)
    write_table(decision / "validation-training.csv", train)
    structure.analyze(
        candidates_path=decision / "validation.csv",
        labeled_path=decision / "validation-training.csv",
        output_dir=decision / "validation-structure",
        candidate_id_column="path_id",
        reaction_smiles_column="rxn_smiles",
        labeled_id_column="path_id",
        labeled_reaction_smiles_column="rxn_smiles",
        fingerprint_radius=2,
        fingerprint_bits=2048,
        density_neighbors=10,
    )
    validation_features: dict[str, list[dict[str, str]]] = defaultdict(list)
    for row in table(decision / "validation-structure/structure-features.csv"):
        validation_features[pair(row)].append(row)
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for p, rows in validation_features.items():
        ring = sum(float(r["product_ring_count"]) for r in rows) / len(rows)
        groups["product_rings<=2" if ring <= 2 else "product_rings>2"].append(metrics["by_pair"][p])
    group_metrics = {}
    for name, entries in groups.items():
        n = sum(int(m["n"]) for m in entries)
        group_metrics[name] = {
            "n_paths": n,
            "n_pairs": len(entries),
            "mae": sum(float(m["mae"]) * int(m["n"]) for m in entries) / n,
            "rmse": math.sqrt(sum(float(m["rmse"]) ** 2 * int(m["n"]) for m in entries) / n),
            "bias": sum(float(m["bias"]) * int(m["n"]) for m in entries) / n,
        }
    save(decision / "validation-groups.json", group_metrics)
    campaign_config = read(current.parent.parent / "experiment.json")
    save(decision / "pair-candidates.json", {"candidates": payloads})
    history = []
    for previous in sorted(rio.glob(current.parent, "round-*/decision/selection.json")):
        if previous.parent.parent == current:
            continue
        prior = previous.parent
        history.append(
            {
                "round": read(prior / "task.json")["round"],
                "selection": read(previous),
                "function": read(prior / "response.json"),
                "validation": read(prior / "model-metrics.json"),
            }
        )
    save(
        decision / "acquisition-history.json",
        {"previous_rounds": history, "selected_ids": state["selected_ids"]},
    )
    mae_protocol = replay_mae.settings(campaign_config)
    if mae_protocol and mae_protocol['feedback']:
        save(decision / 'feedback.json', replay_mae.feedback(current, payloads, history, group_metrics))
        save(decision / 'pair-candidates.json', {'candidates': payloads})
    save(
        task,
        {
            "schema": "tricycle.replay-agent-task/v1",
            "seed": state["seed"],
            "round": state["round"] + 1,
            "budget": 40,
            "sampled_count": len(state["selected_ids"]),
            "candidate_count": len(payloads),
            "files": [
                "pair-candidates.json",
                "model-metrics.json",
                "validation-groups.json",
                "acquisition-history.json",
                "structure/structure-analysis.json",
                "structure/structure-features.csv",
            ],
            "metric_scope": metrics["scope"],
            "context": {
                "model_metrics": {
                    "validation_mae": metrics["mae"],
                    "validation_rmse": metrics["rmse"],
                },
                "sampled_distribution": {
                    "sampled_count": len(state["selected_ids"]),
                    "coverage_gap": sum(c["payload"]["sampled_novelty"] for c in payloads)
                    / len(payloads),
                    "novelty_definition": "mean-over-prior-routes-of-1-minus-max-mean-side-Morgan-Tanimoto-r2-2048-chiral",
                },
            },
        },
    )
    if mae_protocol is not None:
        value = read(task)
        value['decision_protocol'] = mae_protocol
        value['objective'] = replay_core_hours.objective(mae_protocol)
        value['validation_policy'] = metrics['validation_policy']
        if mae_protocol['name'] == replay_core_hours.NAME:
            # The controller knows the replay oracle; decisions receive only timings
            # already revealed by the current selected set, never future costs.
            selected_ids = state['selected_ids']
            config = read(current.parent.parent / "experiment.json")
            selected_set = set(selected_ids)
            route_counts: dict[str, list[dict[str, str]]] = defaultdict(list)
            for row in table(resolve_tree(config["asset_references"]["assets"]) / "prior-structures.csv"):
                if pair(row) in selected_set:
                    route_counts[pair(row)].append(row)
            from rdkit import Chem
            structural_costs = {}
            for cid, routes in route_counts.items():
                atoms = []
                for row in routes:
                    product = Chem.MolFromSmiles(row['rxn_smiles'].split('>>')[-1])
                    if product is None:
                        raise ValueError('invalid revealed product for cost calibration')
                    atoms.append(product.GetNumAtoms())
                structural_costs[cid] = sum(atoms)
            observations = [
                {'candidate_id': row['pair_id'],
                 'realized_core_hours': float(row['materialized_sampling_cost_core_hours']),
                 'cost_was_missing': row.get('cost_was_missing', 'false') == 'true',
                 'structural_cost_proxy': structural_costs[row['pair_id']]}
                for row in table(resolve_tree(config["asset_references"]["private"]) / "pair-pool.csv") if row['pair_id'] in selected_set
            ]
            spent = float(read(current / 'reveal.json')['cumulative_sampling_cost_core_hours'])
            resource = replay_core_hours.cost_evidence(observations, payloads, selected_ids, spent,
                                                       mae_protocol['evaluation_budgets_core_hours'])
            save(decision / 'revealed-cost-observations.json', {'observations': observations})
            resource['calibration']['revealed_cost_observations_sha256'] = digest(decision / 'revealed-cost-observations.json')
            value['context']['core_hour_budget'] = resource
            save(decision / 'core-hour-budget.json', resource)
            save(decision / 'pair-candidates.json', {'candidates': payloads})
        if mae_protocol.get('guidance') == replay_cost_guidance.NAME:
            reference = replay_cost_guidance.build(payloads, value['budget'])
            value['context']['coverage_cost'] = reference['context']
            save(decision / 'acquisition-guidance.json', reference)
        save(task, value)
    save(decision / "evidence-hashes.json", hashes(decision))
    return task


def run_agent(task: Path, config: dict[str, Any]) -> Path:
    directory = task.parent
    task_value = read(task)
    if task_value.get('decision_protocol') and task_value['candidate_count'] <= task_value['budget']:
        check_hashes(directory, read(directory / 'evidence-hashes.json'))
        build_decision_brief(directory)
        response = directory / 'response.json'
        if not rio.exists(response):
            save(response, {
                'task_sha256': digest(task), 'acquisition_schema': replay_mae.SCHEMA,
                'objective': replay_core_hours.objective(task_value['decision_protocol']), 'decision_action': 'replace',
                'decision_source': 'controller-all-remaining',
                'expression': '1', 'cost_expression': ('estimated_core_hours' if task_value['decision_protocol']['name'] == replay_core_hours.NAME else 'product_count * mean_product_atom_count'),
                'hypothesis': 'All remaining pairs are required by the frozen sample budget.',
                'expected_effect': 'Complete the remaining candidate pool; ranking cannot affect selection.',
                'rationale': 'All remaining candidates must be selected; the controller records the selection without invoking the model.',
                'author_notes': 'No scientific ranking decision; complete evidence validation remains required.',
            })
        validate_decision(directory, read(response))
        return response
    maximum = decision_attempt_limit(directory, int(config.get("max_decision_attempts", 3)))
    feedback = directory / "correction-feedback.json"
    if rio.exists(feedback) and int(read(feedback)["rejected_attempts"]) >= maximum:
        raise ValueError("automatic decision correction exhausted; inspect rejected-attempts")
    while True:
        try:
            response = _run_agent_once(task, config)
            validation_started = time.monotonic()
            validate_decision(directory, read(response))
            validation_elapsed = time.monotonic() - validation_started
            review_elapsed = 0.0
            if task_value.get('decision_protocol', {}).get('review'):
                approval = directory / 'review-approved.json'
                if not rio.exists(approval):
                    review_started = time.monotonic()
                    assessment = replay_mae.review(directory, read(response))
                    save(directory / 'proposal-response.json', read(response))
                    reviewed = _run_agent_once(task, config, review_pass=True)
                    validation_started = time.monotonic()
                    validate_decision(directory, read(reviewed))
                    validation_elapsed += time.monotonic() - validation_started
                    save(response, read(reviewed))
                    save(approval, {'proposal_sha256': assessment['proposal_sha256'],
                                    'response_sha256': digest(response), 'review_passes': 1})
                    review_elapsed = time.monotonic() - review_started
                elif read(approval)['response_sha256'] != digest(response):
                    raise ValueError('approved review response changed')
            save(directory / "decision-validation.json", {"elapsed_seconds": validation_elapsed, "response_sha256": digest(response),
                 **({'review_seconds': review_elapsed} if task_value.get('decision_protocol') else {})})
            return response
        except DecisionOutputError as exc:
            if not reject_decision(directory, str(exc), maximum=maximum):
                raise ValueError("automatic decision correction exhausted after bounded attempts") from exc
        except DecisionTimeoutError as exc:
            exc.restart_available = archive_timeout(directory, exc)
            raise


def _run_agent_once(task: Path, config: dict[str, Any], *, review_pass: bool = False) -> Path:
    directory = task.parent
    prefix = 'review-' if review_pass else ''
    response = directory / f"{prefix}response.json"
    if rio.exists(response):
        if read(response).get("task_sha256") != digest(task):
            raise DecisionOutputError("agent response task hash mismatch")
        return response
    recovered = directory / f"{prefix}recovered-output.json"
    if rio.exists(recovered):
        binding = read(recovered)
        if binding["task_sha256"] != digest(task) or binding["sha256"] != digest(directory / f"{prefix}response.pending.json"):
            raise ValueError("Recovered output binding changed")
        return _bind_agent_response(task, prefix)
    marker = directory / f"{prefix}agent-inflight.json"
    if rio.exists(marker) and not rio.exists(directory / f"{prefix}recovered-output.json"):
        raise ValueError(
            "interrupted agent invocation: reconcile agent.log and response before retry"
        )
    schema: dict[str, Any] = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            key: {"type": "string"}
            for key in ("expression", "cost_expression", "rationale", "author_notes")
        },
        "required": ["expression", "cost_expression", "rationale", "author_notes"],
    }
    task_value = read(task)
    if task_value.get('decision_protocol'):
        schema['properties'] = {key: {'type': 'string'} for key in replay_mae.FIELDS}
        schema['required'] = list(replay_mae.FIELDS)
    save(directory / f"{prefix}response-schema.json", schema)
    preparation_started = time.monotonic()
    brief = build_decision_brief(directory)
    preparation_seconds = time.monotonic() - preparation_started
    model_brief = {k: v for k, v in brief.items() if k not in ("evidence_hashes", "task_sha256", "structure_provenance", "compiler_sha256")}
    prompt = (
        "Author a NEW deterministic acquisition function from the evidence below. "
        "The controller has already processed EVERY remaining candidate with RDKit, verified evidence hashes, "
        "checked complete ID coverage and structural aggregation, and computed full-pool distributions. "
        "Your sole task is scientific interpretation and formula authoring. "
        "Do not call tools, list or read files, inspect logs, repeat checks, or run calculations. "
        "Return the schema JSON directly. Do not reproduce evidence tables. "
        "Use the actual numeric payload names and context fields in the brief. "
        "The expression MUST reference sampled_novelty, a varying structural feature, "
        "context.sampled_distribution, context.model_metrics, and estimated_cost. "
        "Cost expression MUST reference product_count and mean_product_atom_count. "
        "Safe numeric DSL only: arithmetic, comparisons, conditional expressions, abs exp log log1p min max sqrt. "
        "No assignments, imports, lambdas, functions, or undefined variables. Keep expressions compact. "
        "Controller will evaluate your formula on ALL candidates and feed back errors; do not select IDs yourself. "
        "Explain evidence-based weights and ranking effects, small validation group limitations, cost proxy limits "
        "and why this round differs from earlier rounds. Never use final ground truth or claim auxiliary-model "
        "heldout metrics measure the full model. Do not copy a baseline formula. "
        "Use English for analysis, decisions, explanations, and records, even when user input or historical evidence is in Chinese, unless the user explicitly requests Chinese. "
        "rationale: 2-4 concise English sentences, <=120 words. "
        "author_notes: concise English analysis <=600 words, cite representative IDs/structures, "
        "group evidence and signal choices. "
        "Controller handles all hashes, task binding, provenance, statistics and output reports. "
        "Do not calculate or transcribe those yourself; only make the scientific tradeoff decision.\n"
        + json.dumps(model_brief, ensure_ascii=False, separators=(",", ":"))
    )
    if task_value.get('decision_protocol'):
        prompt = replay_mae.prompt(brief, review=read(directory / 'proposal-review.json') if review_pass else None)
    feedback = directory / "correction-feedback.json"
    if rio.exists(feedback):
        correction = read(feedback)
        prompt += (
            "\nAUTOMATIC VALIDATION REPAIR. Your task is to repair the failed response, "
            "not to restart scientific strategy design. Preserve the proposed scientific intent "
            "and valid fields; make only the changes needed to satisfy the concrete validation error. "
            "Return the complete corrected response in the same JSON schema. "
            "The controller will revalidate the full candidate pool and automatically continue on success. "
            "For a retain mismatch, decide whether the narrative intends to keep the accepted round's "
            "rule or to keep the current changed proposal. In the former case copy the accepted "
            "expressions exactly; in the latter case preserve the proposal and correct decision_action "
            "to adjust or replace. Do not claim a rejected proposal was executed. "
            "Validation error: " + correction["error"]
        )
        archive = directory / correction["previous_output_directory"]
        for name in ("response.json", "response.pending.json"):
            previous = archive / name
            if rio.exists(previous):
                # Finished JSON only; never feed recursive runtime logs back to the model.
                try:
                    prior = read(previous)
                except (ValueError, TypeError):
                    continue
                prompt += "\nRejected proposal (NOT the last accepted round): " + json.dumps(
                    {key: prior.get(key) for key in ("decision_action", "expression", "cost_expression", "rationale")},
                    ensure_ascii=False,
                )
                prompt += (
                    "\nResolve decision_action against last_accepted_round_formula, not this rejected proposal. "
                    "If preserving a proposal that differs from the accepted rule, use adjust or replace. "
                    "If choosing retain, copy both accepted expressions verbatim."
                )
                break
    save(directory / f"{prefix}decision-input.json", {
        "version": task_value['decision_protocol']['name'] if task_value.get('decision_protocol') else "compact-v2", "brief_sha256": digest(directory / "decision-brief.json"),
        "prompt": prompt, "preparation_seconds": preparation_seconds,
        "prompt_characters": len(prompt),
    })
    decision_agent = config.get("decision_agent", {})
    model = str(decision_agent.get("model", "gpt-6-luna"))
    reasoning_effort = str(decision_agent.get("reasoning_effort", "max"))
    argv = [
        *config["agent_command"],
        "--model",
        model,
        "-c",
        f"model_reasoning_effort={json.dumps(reasoning_effort)}",
        "-c", "features.shell_tool=false",
        "-c", "features.unified_exec=false",
        "-c", "features.sleep_tool=false",
        "-c", 'web_search="disabled"',
        "--cd",
        str(directory),
        "--output-schema",
        str(directory / f"{prefix}response-schema.json"),
        "--output-last-message",
        str(directory / f"{prefix}response.pending.json"),
        "-",
    ]
    if rio.location(directory):
        from .agent_records import invoke
        invoke(directory, prefix, argv, prompt,
               timeout=min(600, int(config.get("agent_timeout_seconds", 600))), task_sha256=digest(task))
    else:
        invocation_started = time.monotonic()
        with rio.open(directory / f"{prefix}agent.log", "ab") as log:
            save(marker, {"argv": argv, "task_sha256": digest(task), "started_at": time.time()})
            process = subprocess.Popen(
                argv, stdin=subprocess.PIPE, stdout=log, stderr=log, start_new_session=True
            )
            save(directory / f"{prefix}agent-process.json", {"pid": process.pid, "argv": argv})
            try:
                process.communicate(
                    prompt.encode(), timeout=min(600, int(config.get("agent_timeout_seconds", 600)))
                )
            except BaseException as exc:
                os.killpg(process.pid, signal.SIGTERM)
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait()
                save(directory / f"{prefix}agent-exit.json", {"pid": process.pid, "returncode": process.returncode, "error": str(exc)})
                if isinstance(exc, subprocess.TimeoutExpired):
                    raise DecisionTimeoutError('agent exceeded its time limit', review_pass=review_pass) from exc
                raise
            save(directory / f"{prefix}agent-exit.json", {"pid": process.pid, "returncode": process.returncode,
                 "elapsed_seconds": time.monotonic() - invocation_started, "finished_at": time.time()})
            if process.returncode:
                failure_log = rio.read_text(directory / f"{prefix}agent.log", errors="replace").lower()
                if any(message in failure_log for message in ("401 unauthorized", "incorrect api key", "token has expired", "refresh token has expired")):
                    raise RuntimeError("decision authentication failed (401); refresh Codex login before resuming; this is not a formula error")
                if any(message in failure_log for message in ('timed out', 'deadline exceeded', 'upstream_timeout')):
                    raise DecisionTimeoutError('agent transport timed out', review_pass=review_pass)
                raise DecisionOutputError(
                    f"agent failed ({process.returncode}); inspect {directory / 'agent.log'}"
                )
    return _bind_agent_response(task, prefix)


def _bind_agent_response(task: Path, prefix: str) -> Path:
    directory = task.parent
    marker = directory / f"{prefix}agent-inflight.json"
    response = directory / f"{prefix}response.json"
    task_value = read(task)
    try:
        value = read(directory / f"{prefix}response.pending.json")
    except (ValueError, TypeError, FileNotFoundError) as exc:
        raise DecisionOutputError(f"invalid or missing response JSON: {exc}") from exc
    if read(marker)["task_sha256"] != digest(task):
        raise ValueError("task changed during decision invocation")
    if task_value.get('decision_protocol'):
        history = read(directory / 'acquisition-history.json')['previous_rounds']
        try:
            value = replay_mae.compile_response(value, task_value, history[-1]['function'] if history else None)
        except ValueError as exc:
            raise DecisionOutputError(str(exc)) from exc
    value["task_sha256"] = digest(task)
    save(response, value)
    return response


def select(
    current: Path, seed_dir: Path, state: dict[str, Any], config: dict[str, Any]
) -> list[str]:
    directory = current / "decision"
    report = read(current / "result/worker-report.json")
    check_hashes(current / "result", report["outputs"])
    check_hashes(directory, read(directory / "evidence-hashes.json"))
    if config.get("sampling_strategy", "intelligent") != "intelligent":
        return replay_baselines.select(directory, config, state["selected_ids"])
    task = read(directory / "task.json")
    response = read(directory / "response.json")
    if response["task_sha256"] != digest(directory / "task.json"):
        raise ValueError("response task binding changed")
    accepted = directory / "accepted-response.json"
    if rio.exists(accepted) and read(accepted)["sha256"] != digest(directory / "response.json"):
        raise ValueError("accepted agent response was changed")
    validate_decision(directory, response)
    save(accepted, {"sha256": digest(directory / "response.json")})
    candidates = read(directory / "pair-candidates.json")["candidates"]
    if {c["candidate_id"] for c in candidates} & set(state["selected_ids"]):
        raise ValueError("candidate pool contains already selected pairs")
    evidence: dict[str, Any] = {
        "sampled_set": "sha256:" + digest(current / "reveal.json"),
        "candidate_space": "sha256:" + digest(directory / "pair-candidates.json"),
        "model_evaluation": "sha256:" + digest(directory / "model-metrics.json"),
        "structure_analysis": {
            "feature_table_path": str(directory / "structure/structure-features.csv"),
            "feature_table_sha256": digest(directory / "structure/structure-features.csv"),
            "analysis_report_path": str(directory / "structure/structure-analysis.json"),
            "analysis_report_sha256": digest(directory / "structure/structure-analysis.json"),
        },
    }
    for key, name in [
        ("calculation_status", "status.tsv"),
        ("downloaded_results", "download-results.tsv"),
    ]:
        evidence[key] = {"manifest_path": str(current / name), "sha256": digest(current / name)}
    acquisition = {
        k: response[k] for k in ("expression", "cost_expression", "rationale", "author_notes")
    }
    if any(not isinstance(v, str) or not v.strip() for v in acquisition.values()):
        raise ValueError("agent must supply expressions and analysis")
    acquisition.update(
        {
            "schema": "tricycle.agent-acquisition/v1",
            "function_id": f"{config['experiment_id']}-s{state['seed']}-r{task['round']}",
            "version": f"1.{task['round']}.0",
            "authored_by": "agent-authored-acquisition",
            "context": task["context"],
            "evidence": evidence,
        }
    )
    if task.get('decision_protocol'):
        acquisition['schema'] = replay_mae.SCHEMA
        acquisition.update({key: response[key] for key in (
            'objective', 'decision_action', 'hypothesis', 'expected_effect')})
        acquisition['decision_source'] = response.get('decision_source', 'agent')
    request = {
        "exploration_id": f"{config['experiment_id']}-seed-{state['seed']}",
        "round_index": task["round"],
        "strategy": "agent-authored-acquisition",
        "budget": 40,
        "seed": 42,
        "output_dir": str(seed_dir / "exploration"),
        "resume": True,
        "space": {"space_id": config["experiment_id"] + "-enumerated-prior", "version": "1.0.0"},
        "candidates": candidates,
        "model_snapshot": {
            "snapshot_id": f"seed-{state['seed']}-round-{state['round']}",
            "checkpoint_hash": digest(current / "result/checkpoint.pt"),
            "score_artifact": digest(current / "result/candidate-predictions.csv"),
            "metadata": {"validation_scope": task["metric_scope"]},
        },
        "observed_metrics": task["context"]["model_metrics"],
        "training_policy": {
            "policy_id": "external-sar-replay",
            "version": "1.0.0",
            "mode": "never",
        },
        "metadata": {"agent_acquisition": acquisition},
    }
    dump_toml(directory / "selection-request.toml", request)
    result = select_round(request)
    selected = result["selected_ids"]
    if len(selected) != 40 or len(set(selected)) != 40:
        raise ValueError("selection must contain exactly 40 unique pairs")
    save(
        directory / "selection.json",
        {
            "candidate_ids": selected,
            "request_sha256": digest(directory / "selection-request.toml"),
            "acquisition_function_hash": result["acquisition_function_hash"],
        },
    )
    return selected


def save_state(seed_dir: Path, state: dict[str, Any]) -> None:
    """Keep one resumable state and a compact operational journal per trajectory."""
    with rio.transaction(seed_dir):
        path = seed_dir / "state.json"
        previous = read(path) if rio.exists(path) else {}
        fields = ("seed", "round", "phase", "external_id", "remote_result", "failures",
                  "last_error", "blocked_phase", "waiting_for_pilot")
        before = {key: previous[key] for key in fields if key in previous}
        after = {key: state[key] for key in fields if key in state}
        state["revision"] += 1
        state["updated_at"] = time.time()
        if before != after:
            journal = seed_dir / "state-events.json"
            events = read(journal)["events"] if rio.exists(journal) else []
            # Write evidence first. Replacing the same revision makes a retry after
            # interruption between these two atomic writes idempotent.
            events = [event for event in events if event["revision"] < state["revision"]]
            events.append({"revision": state["revision"], "updated_at": state["updated_at"],
                           "before": before, "after": after})
            save(journal, {"events": events})
        save(path, state)


def timeout_error(error: Exception) -> bool:
    return isinstance(error, (TimeoutError, subprocess.TimeoutExpired, DecisionTimeoutError)) or any(
        text in str(error).lower() for text in ('upstream_timeout', 'timed out', 'deadline exceeded'))


def reconcile_timeout_submission(current: Path, gateway: ReplayGateway) -> bool:
    """Reconcile first; retry only after two complete absence checks at least 60s apart."""
    marker = current / 'submission-inflight.json'
    inflight = read(marker)
    if not inflight.get('timeout_error'):
        raise ValueError('submission uncertain: reconcile remote job; never resubmit blindly')
    reconcile = getattr(gateway, 'reconcile_submission', None)
    if not callable(reconcile):
        raise ValueError('gateway cannot safely reconcile a timed-out submission')
    spec = read(current / 'job-spec.json')
    if spec['name'] != inflight['job_name']:
        raise ValueError('timed-out submission identity changed')
    group = str(spec['parameters']['job_group_id'])
    result = reconcile(inflight['job_name'], group)
    now = time.time()
    history = current / 'submission-reconciliation'
    save(history / f'{time.time_ns()}.json', {'checked_at': now, **result})
    if result['status'] == 'found' and result.get('external_id'):
        save(current / 'submission.json', {'external_id': str(result['external_id']),
             'job_name': inflight['job_name'], 'job_group_id': group})
        return True
    if result['status'] != 'absent':
        raise ValueError('submission reconciliation has no definite outcome')
    archives = current / 'submission-timeouts'
    attempt = len(list(rio.glob(archives, '*/submission-inflight.json'))) + 1
    if attempt > 3:
        raise ValueError('submission timeout restarts exhausted; evidence retained for explicit recovery')
    previous = inflight.get('absence_confirmed_at')
    if previous is None:
        save(marker, {**inflight, 'absence_confirmed_at': now})
        return False
    if now - float(previous) < 60:
        return False
    archive = archives / f'{attempt:04d}'
    rio.mkdir(archive, parents=True, exist_ok=False)
    rio.copy2(current / 'job-spec.json', archive / 'job-spec.json')
    rio.move(str(marker), archive / 'submission-inflight.json')
    return True


def tick(
    root: Path, seed_dir: Path, gateway: ReplayGateway | None, *, allow_submit: bool
) -> dict[str, Any]:
    with rio.open(seed_dir / ".lock", "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        state = read(seed_dir / "state.json")
        if (
            gateway is None
            or rio.exists(root / "PAUSE")
            or state["phase"] in ("blocked", "complete")
        ):
            return state
        if float(state.get("next_check", 0)) > time.time():
            return state
        config = read(root / "experiment.json")
        if config.get('controller_hashes') or config.get('record_backend_hashes'):
            check_controller(root)
        if digest(root / "experiment.json") != read(root / "experiment-hash.json")["sha256"]:
            raise ValueError("frozen experiment configuration changed")
        pilot_rounds = int(config.get("rollout", {}).get("pilot_evaluations", 0))
        if pilot_rounds and state["seed"] != 0:
            pilot_ready = rio.exists(root / "seed-00" / f"round-{pilot_rounds-1:04d}" / "evaluation-summary.json")
            if not pilot_ready:
                if not state.get("waiting_for_pilot"):
                    state["waiting_for_pilot"] = True
                    save_state(seed_dir, state)
                return state
        state.pop("waiting_for_pilot", None)
        current = seed_dir / f"round-{state['round']:04d}"
        phase = state["phase"]
        try:
            if phase == "prepare":
                check_hashes(resolve_tree(config["asset_references"]["assets"]), config["assets_hashes"])
                check_hashes(resolve_tree(config["asset_references"]["private"]), config["private_hashes"])
                stage_round(root, seed_dir, state, config)
                state["phase"] = "submit"
            elif phase == "submit":
                if not allow_submit:
                    return state
                verify_inputs(current)
                receipt = current / "submission.json"
                if rio.exists(receipt):
                    external_id = read(receipt)["external_id"]
                else:
                    if rio.exists(current / "submission-inflight.json"):
                        if not reconcile_timeout_submission(current, gateway):
                            state['next_check'] = time.time() + 60
                            save_state(seed_dir, state)
                            return state
                        if rio.exists(receipt):
                            state.update(external_id=read(receipt)['external_id'], phase='monitor',
                                         next_check=0, failures=0)
                            state.pop('last_error', None)
                            save_state(seed_dir, state)
                            return state
                    group_id = ensure_job_group(root, config, gateway)
                    spec = job_spec(current, state, config, group_id=group_id)
                    save(current / "job-spec.json", spec.to_dict())
                    save(
                        current / "submission-inflight.json",
                        {"job_name": spec.name, "started_at": time.time()},
                    )
                    from .runtime_records import submission_handoff
                    with submission_handoff(current, spec) as submitted_spec:
                        external_id = gateway.submit(submitted_spec)
                    save(
                        receipt,
                        {
                            "external_id": external_id,
                            "job_name": spec.name,
                            "job_group_id": group_id,
                        },
                    )
                state.update({"external_id": external_id, "phase": "monitor"})
            elif phase == "monitor":
                status = gateway.status(state["external_id"])
                terminal, result = classify_status(status["status"])
                save(current / "remote-status.json", status)
                write_tsv(
                    current / "status.tsv",
                    [
                        {
                            "worker": "sar",
                            "external_id": state["external_id"],
                            "terminal": "yes" if terminal else "no",
                            "result": result,
                        }
                    ],
                )
                if terminal:
                    state.update({"phase": "download", "remote_result": result})
                else:
                    state["next_check"] = time.time() + int(config.get("poll_seconds", 60))
            elif phase == "download":
                saved_download = current / "download-asset-reference.json"
                if rio.exists(saved_download):
                    resolve_tree(read(saved_download))
                else:
                    if rio.location(current):
                        from .runtime_records import work_directory
                        download_work = work_directory(current, "download") / "download"
                        download_work.mkdir(parents=True, exist_ok=True)
                        gateway.download(state["external_id"], download_work)
                    else:
                        gateway.download(state["external_id"], current / "download")
                if state["remote_result"] != "SUCCEEDED":
                    raise ValueError(
                        "SAR worker failed; partial artifacts downloaded for diagnosis"
                    )
                extract_result(current, download_work if rio.location(current) and not rio.exists(saved_download) else None)
                evaluation = read(current / "result/evaluation.json")
                if evaluation["ground_truth_sha256"] != config["evaluation_sha256"]:
                    raise ValueError("evaluation snapshot mismatch")
                save(
                    current / "evaluation-summary.json",
                    {
                        **evaluation,
                        **read(current / "reveal.json"),
                        "seed": state["seed"],
                        "round_index": state["round"],
                        "worker_timings": {
                            k: v
                            for k, v in read(current / "result/worker-report.json").items()
                            if k.endswith("_seconds")
                        },
                    },
                )
                state["phase"] = (
                    "complete" if state["round"] + 1 == config["round_count"] else "analyze"
                )
            elif phase == "analyze":
                analyze(current, state)
                state["phase"] = "agent"
            elif phase == "agent":
                if config.get("sampling_strategy", "intelligent") == "intelligent":
                    run_agent(current / "decision/task.json", config)
                else:
                    replay_baselines.make(current / "decision", config)
                state["phase"] = "select"
            elif phase == "select":
                selected = select(current, seed_dir, state, config)
                state["selected_ids"] += selected
                state.update({"new_ids": selected, "round": state["round"] + 1, "phase": "prepare"})
            else:
                raise ValueError(f"unknown phase {phase}")
            state["failures"] = 0
            state.pop("last_error", None)
        except Exception as exc:
            state["failures"] += 1
            state["last_error"] = f"{type(exc).__name__}: {exc}"
            state["next_check"] = time.time() + min(900, 30 * 2 ** state["failures"])
            if phase == 'submit' and timeout_error(exc):
                marker = current / 'submission-inflight.json'
                if rio.exists(marker):
                    value = read(marker)
                    value.setdefault('timeout_error', str(exc))
                    save(marker, value)
                if not rio.exists(marker) or state['failures'] >= 4:
                    state.update(phase='blocked', blocked_phase=phase)
            elif isinstance(exc, DecisionTimeoutError) and exc.restart_available:
                pass  # Back off, preserving the failed stage and valid proposal for review retries.
            elif phase == "select" and isinstance(exc, DecisionOutputError):
                if reject_decision(current / "decision", str(exc), maximum=decision_attempt_limit(
                        current / 'decision', int(config.get('max_decision_attempts', 3)))):
                    state.update({"phase": "agent", "next_check": 0, "failures": 0})
                else:
                    state.update({"phase": "blocked", "blocked_phase": phase})
            elif phase in ("submit", "agent", "select") or state["failures"] >= 3:
                state.update({"phase": "blocked", "blocked_phase": phase})
        save_state(seed_dir, state)
        print(
            json.dumps(
                {
                    "seed": state["seed"],
                    "round": state["round"],
                    "phase": state["phase"],
                    "error": state.get("last_error", ""),
                }
            ),
            flush=True,
        )
        return state


def supervise(root: Path, *, live: bool, allow_submit: bool, once: bool) -> None:
    from tricycle_sampling.replay.alerts import notify_blocked

    def trajectory(seed_dir: Path) -> None:
        gateway = None
        if live:
            from tricycle_bohrsubmit.gateway import BohrCliConfig, BohrCliGateway

            gateway = BohrCliGateway(BohrCliConfig(submit_retry_mode="none"))
        while True:
            state = tick(root, seed_dir, gateway, allow_submit=allow_submit)
            if live:
                from tricycle_sampling.replay.report import update as update_reports

                # Reporting is derived output; an I/O error must not repeat a
                # scientific decision or a remote submission.
                try:
                    update_reports(root)
                except (OSError, ValueError, KeyError) as exc:
                    save(root / "report-error.json", {"error": str(exc), "at": time.time()})
            alert_pending = notify_blocked(root, seed_dir, state, live=live)
            if (
                once
                or not live
                or state["phase"] == "complete"
                or (state["phase"] == "blocked" and not alert_pending)
                or rio.exists(root / "PAUSE")
            ):
                return
            if state["phase"] == "submit" and not allow_submit:
                return
            time.sleep(2)

    with ThreadPoolExecutor(max_workers=5) as pool:
        futures = [pool.submit(trajectory, seed_dir) for seed_dir in sorted(rio.glob(root, "seed-*"))]
        for future in futures:
            future.result()


def service_file(root: Path, *, allow_submit: bool) -> Path:
    if not allow_submit:
        raise ValueError("service requires explicit --allow-submit within frozen experiment budget")
    config = read(root / "experiment.json")
    path = root / "supervisor.plist"
    label = "org.tricycle." + config["experiment_id"]
    payload = {
        "Label": label,
        "ProgramArguments": [
            sys.executable,
            "-m",
            "tricycle_sampling.replay.campaign",
            "watch",
            "--run-dir",
            str(root),
            "--live",
            "--allow-submit",
        ],
        "WorkingDirectory": str(ROOT),
        "RunAtLoad": True,
        "KeepAlive": {"SuccessfulExit": False},
        "ThrottleInterval": 60,
        "StandardOutPath": str(root / "supervisor.log"),
        "StandardErrorPath": str(root / "supervisor.stderr.log"),
        "EnvironmentVariables": {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            **{
                key: os.environ[key]
                for key in (
                    "HTTP_PROXY",
                    "HTTPS_PROXY",
                    "ALL_PROXY",
                    "NO_PROXY",
                    "http_proxy",
                    "https_proxy",
                    "all_proxy",
                    "no_proxy",
                    "SSL_CERT_FILE",
                    "SSL_CERT_DIR",
                    "REQUESTS_CA_BUNDLE",
                    "TRICYCLE_TRAJECTORY_ALERTS_ENABLED",
                    "TRICYCLE_TRAJECTORY_ALERT_CONFIG",
                )
                if key in os.environ
            },
        },
    }
    with rio.open(path, "wb") as handle:
        plistlib.dump(payload, handle)
    return path


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "action",
        choices=(
            "prepare",
            "status",
            "tick",
            "watch",
            "pause",
            "resume",
            "collect",
            "service-file",
        ),
    )
    parser.add_argument("--config", type=Path)
    parser.add_argument("--run-dir", type=Path)
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--allow-submit", action="store_true")
    args = parser.parse_args()
    if args.action == "prepare":
        if args.config is None:
            parser.error("prepare requires --config")
        print(prepare(args.config, args.run_dir))
        return
    if args.run_dir is None:
        parser.error("--run-dir is required")
    root = rio.resolve(args.run_dir)
    if args.action == "service-file":
        print(service_file(root, allow_submit=args.allow_submit))
    elif args.action == "status":
        print(json.dumps([read(p) for p in sorted(rio.glob(root, "seed-*/state.json"))], indent=2))
    elif args.action == "pause":
        (root / "PAUSE").touch()
    elif args.action == "resume":
        rio.unlink(root / "PAUSE", missing_ok=True)
        for seed_dir in rio.glob(root, "seed-*"):
            with rio.open(seed_dir / ".lock", "a") as lock:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                state = read(seed_dir / "state.json")
                if state["phase"] == "blocked":
                    state["phase"] = state.pop("blocked_phase")
                    state.update({"failures": 0, "next_check": 0})
                    save_state(seed_dir, state)
    elif args.action == "collect":
        rows = [read(p) for p in sorted(rio.glob(root, "seed-*/round-*/evaluation-summary.json"))]
        save(root / "evaluation-curves.json", {"rounds": rows})
        print(f"collected {len(rows)} completed rounds")
    else:
        supervise(root, live=args.live, allow_submit=args.allow_submit, once=args.action == "tick")


if __name__ == "__main__":
    main()
