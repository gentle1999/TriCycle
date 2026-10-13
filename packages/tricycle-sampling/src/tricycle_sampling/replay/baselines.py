"""Frozen deterministic replay baselines; no model or oracle calls."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from tricycle_core.io import file_sha256 as digest
from tricycle_core.io import write_json as save

from tricycle_sampling import Candidate, validate_calculation_evidence
from tricycle_sampling.experiment import EXPERIMENT_SAMPLING_SCHEMA, select_experiment_candidates
from tricycle_sampling.protocols import LITERATURE_BASELINE_STRATEGIES

STRATEGIES = frozenset(LITERATURE_BASELINE_STRATEGIES)


def read(path: Path) -> dict[str, Any]:
    return dict(json.loads(path.read_text()))


def make(directory: Path, config: dict[str, Any]) -> dict[str, Any]:
    if config.get("sampling_experiment") != EXPERIMENT_SAMPLING_SCHEMA:
        raise ValueError("Replay baselines require the current sampling_experiment schema; prepare a new run")
    if config["sampling_strategy"] not in STRATEGIES:
        raise ValueError("Replay baselines require a registered literature strategy")
    if digest(Path(__file__)) != config["baseline_policy_sha256"]:
        raise ValueError("frozen baseline implementation changed")
    for name, expected in read(directory / "evidence-hashes.json").items():
        if digest(directory / name) != expected:
            raise ValueError("baseline evidence changed")
    evidence = {key: {"manifest_path": str(directory.parent / name),
                      "sha256": digest(directory.parent / name)}
                for key, name in (("calculation_status", "status.tsv"),
                                  ("downloaded_results", "download-results.tsv"))}
    validate_calculation_evidence(evidence)
    task = read(directory / "task.json")
    candidates = tuple(Candidate(**c) for c in read(directory / "pair-candidates.json")["candidates"])
    result = select_experiment_candidates(
        config["sampling_strategy"], candidates,
        reference_features=read(directory / "reference-features.json"), budget=task["budget"],
        seed=int(config.get("fixed_seed", 42)),
        design_size=config["round_count"] * config["batch_size"] - config["batch_size"],
        offset=task["sampled_count"] - config["batch_size"],
    )
    definition = {
        "strategy": config["sampling_strategy"], "authored_by": "deterministic-baseline",
        "task_sha256": digest(directory / "task.json"), "implementation_sha256": digest(Path(__file__)),
        "sampling_protocol": result.metadata["sampling_protocol"],
        "sampling_experiment": result.metadata["sampling_experiment"],
        "expression": result.metadata["sampling_protocol"]["rule"],
        "rationale": result.metadata["sampling_protocol"]["adaptation"],
        "cost_expression": "report only; shared realized core-hours axis",
    }
    response = directory / "response.json"
    if response.exists() and read(response) != definition:
        raise ValueError("existing baseline decision differs")
    ranking = {"definition": definition, "selected_ids": list(result.selected_ids), "evidence": evidence,
               "sampling_details": result.metadata["sampling_details"]}
    ranking = json.loads(json.dumps(ranking, allow_nan=False))
    target = directory / "baseline-ranking.json"
    if target.exists() and read(target) != ranking:
        raise ValueError("existing baseline ranking differs")
    save(target, ranking)
    save(response, definition)
    return {"chosen": list(result.selected_ids), "definition": definition, "evidence": evidence}


def select(directory: Path, config: dict[str, Any], selected_before: list[str]) -> list[str]:
    decision=make(directory,config)
    chosen=decision["chosen"]
    if len(chosen)!=read(directory/"task.json")["budget"] or len(set(chosen))!=len(chosen) or set(chosen)&set(selected_before):
        raise ValueError("baseline selection violates round budget")
    request={"strategy":config["sampling_strategy"],"task_sha256":digest(directory/"task.json"),
             "response_sha256":digest(directory/"response.json"),"ranking_sha256":digest(directory/"baseline-ranking.json"),
             "selected_ids":chosen,"evidence":decision["evidence"]}
    save(directory/"baseline-selection-request.json",request)
    selection={"candidate_ids":chosen,"request_sha256":digest(directory/"baseline-selection-request.json"),
               "acquisition_function_hash":digest(directory/"response.json")}
    target=directory/"selection.json"
    if target.exists() and read(target)!=selection:
        raise ValueError("refusing to change completed baseline selection")
    save(directory/"accepted-response.json",{"sha256":digest(directory/"response.json")})
    save(target,selection)
    return list(chosen)
