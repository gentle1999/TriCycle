"""Assemble pinned Bohr requests; retain RXNGraphormer's internal executor binding."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from tricycle_bohrsubmit.task_table import expand_task_specs
from tricycle_core.asset_store import resolve_input
from tricycle_core.toml import dump_toml, load_toml

from tricycle_sar.classical_runner import MODELS as CLASSICAL_MODELS
from tricycle_sar.modelling_benchmark import check, run_models
from tricycle_sar.preparation.additional import prepare as prepare_additional
from tricycle_sar.preparation.additional import require_reactiont5_assets
from tricycle_sar.preparation.classical import prepare as prepare_classical
from tricycle_sar.preparation.molecular import prepare as prepare_molecular
from tricycle_sar.preparation.native import prepare as prepare_native
from tricycle_sar.training import protocol, protocol_file, require_benchmark_protocol


def prepare(
    benchmark: Path,
    output: Path,
    *,
    group_name: str,
    group_config: Path,
) -> Path:
    check(benchmark)
    require_benchmark_protocol(benchmark)
    models = run_models(benchmark)
    assets = None
    if "reactiont5" in models:
        settings = json.loads(
            (resolve_input(benchmark, "workspace/configs/tricycle-reactiont5.json")).read_text()
        )
        assets = resolve_input(benchmark, "workspace") / settings["checkpoint_directory"]
        require_reactiont5_assets(
            assets,
            json.loads(
                protocol_file("reactiont5", "common/training_bundle/reactiont5.json").read_text()
            ),
        )
    output.mkdir(parents=True, exist_ok=False)
    components = output / "model-bundles"
    classical = [m for m in models if m in CLASSICAL_MODELS]
    molecular = [m for m in models if m in ("uam", "molclr")]
    native = [m for m in models if m in ("chemprop_cgr", "chemprop_multicomponent", "t5chem")]
    additional = [m for m in models if m in ("rxnresid", "reactiont5")]
    if classical:
        prepare_classical(benchmark, components / "classical", group_config=group_config)
    if molecular:
        prepare_molecular(benchmark, components / "molecular", group_config=group_config)
    if native:
        prepare_native(benchmark, components / "native", group_config=group_config)
    for model in additional:
        prepare_additional(
            model,
            benchmark,
            components / model,
            assets=assets if model == "reactiont5" else None,
            group_name=group_name,
            group_config=group_config,
        )
    locations = (
        [("classical", components / "classical", 5 * len(classical), min(3, 5 * len(classical)))]
        if classical
        else []
    )
    locations += [(m, components / "molecular" / m, 5, 5) for m in molecular]
    locations += [(m, components / "native" / m, 5, 5) for m in native]
    locations += [(m, components / m, 5, 5) for m in additional]
    requests = []
    for name, directory, tasks, workers in locations:
        path = directory / "bohr-train.toml"
        request = load_toml(path)
        if len(expand_task_specs(request["task_defaults"], request["task_specs"])) != tasks:
            raise ValueError(f"Incomplete task table: {name}")
        request.update(
            job_group_id="", job_group_name=group_name, auto_job_group=True, workers=workers
        )
        dump_toml(path, request)
        requests.append(
            {
                "name": name,
                "executor": "bohr",
                "request": str(path.resolve()),
                "fold_tasks": tasks,
                "workers": workers,
                "machine_type": request["machine_type"],
            }
        )
    if "rxngraphormer" in models:
        internal, sha = protocol("rxngraphormer")
        requests.append(
            {
                "name": "rxngraphormer",
                "executor": internal["executor"],
                "method_id": internal["method_id"],
                "registry": internal["manifest"],
                "registry_sha256": sha,
                "fold_tasks": 5,
                "status": "requires-internal-preparation",
            }
        )
    dump_toml(
        output / "submit-order.toml",
        {
            "job_group_name": group_name,
            "create_group_with": locations[0][0] if locations else "",
            "bohr_tasks": sum(item[2] for item in locations),
            "internal_tasks": 5 if "rxngraphormer" in models else 0,
            "models": requests,
        },
    )
    return output


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--benchmark-run", type=Path, required=True)
    parser.add_argument("--out-dir", type=Path, required=True)
    parser.add_argument("--job-group-name", required=True)
    parser.add_argument("--group-config", required=True, type=Path)
    args = parser.parse_args()
    prepare(
        args.benchmark_run,
        args.out_dir,
        group_name=args.job_group_name,
        group_config=args.group_config,
    )


if __name__ == "__main__":
    main()
