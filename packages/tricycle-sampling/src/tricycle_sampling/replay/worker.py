#!/usr/bin/env python3
"""One historical-replay SAR round; no sampling and no remote submission.

The full model follows the frozen baseline recipe. An auxiliary model is trained
from scratch on 80% of revealed substrate pairs, never from the full checkpoint.
Only this auxiliary model supplies acquisition validation errors.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sys
import time
from pathlib import Path
from typing import Any

from tricycle_core.io import file_sha256 as _sha256_file
from tricycle_core.io import write_json as _write_json
from tricycle_core.metrics import regression_metrics
from tricycle_sar import get_backend
from tricycle_sar.predictions import validate_rows
from tricycle_sar.sampling_runtime import verify as verify_sar_runtime

from tricycle_sampling import trajectory_worker as baseline
from tricycle_sampling.replay.results import publish_training_results


def pair(row: dict[str, Any]) -> str:
    return f"{str(row['ene_id']).zfill(12)}+{str(row['diene_id']).zfill(12)}"


def fit(data: Path, out: Path, recipe: dict[str, Any], previous: Path | None) -> Path:
    from tricycle_sar.rxnresid import fit_all_revealed

    vocabulary = baseline.ROOT / "route-id-vocabulary.json"
    return fit_all_revealed(data, out, recipe, source=baseline.ROOT / "rxnresid",
                           previous=previous,
                           route_vocabulary=vocabulary if vocabulary.is_file() else None)


def predict(
    checkpoint: Path, data: Path, out: Path, recipe: dict[str, Any]
) -> list[dict[str, str]]:
    vocabulary = baseline.ROOT / "route-id-vocabulary.json"
    command = list(get_backend("rxnresid").inference_command(
        python_executable=sys.executable, source_directory=baseline.ROOT / "rxnresid",
        route_vocabulary=vocabulary if vocabulary.is_file() else None,
        checkpoint=str(checkpoint), data=str(data), output=str(out),
        batch_size=recipe["batch_size_paths"], component_id_columns=("ene_id", "diene_id"),
        cpu=recipe["device"] == "cpu",
    ))
    baseline._run_logged(command, cwd=baseline.ROOT / "rxnresid", log_path=out.with_suffix(".log"))
    rows = baseline._read_csv(out)
    expected = baseline._read_csv(data)
    validate_rows(rows, expected=expected, required=("prediction", "target", *baseline.SCORE_COLUMNS))
    return rows


def metrics(rows: list[dict[str, str]]) -> dict[str, float | int]:
    values = regression_metrics([float(r["target"]) for r in rows], [float(r["prediction"]) for r in rows])
    return {key: values[key] for key in ("n", "mae", "rmse", "bias")}


def run(request_path: Path) -> None:
    work = request_path.resolve().parent / "work/round"
    work.mkdir(parents=True, exist_ok=True)
    try:
        _run(request_path, work)
    except BaseException:
        publish_training_results(work, request_path.resolve().parent / "outputs/round")
        raise


def _run(request_path: Path, work: Path) -> None:
    root = request_path.resolve().parent
    request = json.loads(request_path.read_text())
    for name, digest in request["input_hashes"].items():
        if _sha256_file(root / name) != digest:
            raise ValueError(f"input changed: {name}")
    os.chdir(root)
    baseline.ROOT = root
    verify_sar_runtime(root, request["sar_backend"])
    if request["source_revision"] != request["sar_backend"]["source_revision"]:
        raise ValueError("worker source revision differs from SAR backend")
    sys.path.insert(0, str(root / "rxnresid/src"))
    output = root / "outputs/round"
    output.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    labels = baseline._native_rows(root / "training.csv")
    if not labels:
        raise ValueError("cannot bootstrap model without revealed labels")
    recipe = request["training"]
    previous = root / "previous.pt"
    pre_update_seconds = 0.0
    if request.get('mae_protocol'):
        from tricycle_sampling.replay.mae import pre_update

        pre_started = time.monotonic()
        new_labels = baseline._read_csv(root / 'newly-revealed.csv')
        if previous.exists() and new_labels:
            before = predict(previous, root / 'newly-revealed.csv', output / 'pre-update-predictions.csv', recipe)
            pre_metrics = pre_update(before, _sha256_file(previous))
        else:
            pre_metrics = {'scope': 'previous main checkpoint before update',
                           'status': 'initial-round' if not previous.exists() else 'no-new-labels',
                           'n_pairs': 0, 'by_pair': {}}
        _write_json(output / 'pre-update-metrics.json', pre_metrics)
        pre_update_seconds = time.monotonic() - pre_started
    if request["round_index"] == 0 and previous.exists():
        raise ValueError("new trajectories must train round zero from scratch")
    if request["reuse_checkpoint"] and request["round_index"] > 0:
        checkpoint = previous
    else:
        checkpoint = fit(
            root / "training.csv",
            work / "full-fit",
            recipe,
            previous if previous.exists() else None,
        )
    output_references = {}
    if request["reuse_checkpoint"] and request["round_index"] > 0:
        output_references["checkpoint.pt"] = {"input": "previous.pt", "sha256": _sha256_file(previous)}
    else:
        shutil.copyfile(checkpoint, output / "checkpoint.pt")
        checkpoint = output / "checkpoint.pt"
    if request["need_decision"]:
        predict(
            checkpoint,
            root / "candidates.csv",
            output / "candidate-predictions.csv",
            recipe,
        )
    evaluation = predict(
        checkpoint,
        root / "evaluation.csv",
        output / "evaluation-predictions.csv",
        recipe,
    )
    _write_json(
        output / "evaluation.json",
        {
            **metrics(evaluation),
            "unit": "kcal/mol",
            "role": "evaluation-only",
            "ground_truth_sha256": request["evaluation_sha256"],
        },
    )
    full_elapsed = time.monotonic() - started - pre_update_seconds
    if request["need_decision"]:
        # Split by substrate pair, never by individual reaction path.
        pairs = sorted(
            {pair(r) for r in labels},
            key=lambda p: hashlib.sha256(("validation42:" + p).encode()).hexdigest(),
        )
        if len(pairs) < 5:
            raise ValueError("need at least five labeled pairs for independent validation")
        fixed_panel = request.get('validation_pair_ids')
        heldout = set(fixed_panel) if fixed_panel is not None else set(pairs[: max(1, len(pairs) // 5)])
        if not heldout or not heldout < set(pairs):
            raise ValueError('validation panel must be nonempty, revealed, and disjoint from auxiliary training')
        training = [r for r in labels if pair(r) not in heldout]
        validation = [r for r in labels if pair(r) in heldout]
        baseline._write_csv(
            work / "validation-train.csv", list(baseline.NATIVE_COLUMNS), training
        )
        baseline._write_csv(
            work / "validation-labels.csv", list(baseline.NATIVE_COLUMNS), validation
        )
        auxiliary = fit(work / "validation-train.csv", work / "validation-fit", recipe, None)
        predictions = predict(
            auxiliary,
            work / "validation-labels.csv",
            output / "validation-predictions.csv",
            recipe,
        )
        from collections import defaultdict

        grouped: dict[str, list[dict[str, str]]] = defaultdict(list)
        for row in predictions:
            grouped[pair(row)].append(row)
        _write_json(
            output / "model-metrics.json",
            {
                **metrics(predictions),
                "unit": "kcal/mol",
                "role": "sampled-pair-holdout",
                "scope": "auxiliary model trained from scratch; not full-model validation",
                **({'validation_policy': 'fixed-initial-pair-panel', 'panel_pair_count': len(heldout)} if fixed_panel is not None else {}),
                "validation_pairs": sorted(heldout),
                "training_pairs": sorted(set(pairs) - heldout),
                "checkpoint_sha256": _sha256_file(auxiliary),
                "by_pair": {p: metrics(rows) for p, rows in grouped.items()},
            },
        )
    publish_training_results(work, output)
    _write_json(
        output / "worker-report.json",
        {
            "schema": "tricycle.replay-sar-worker/v1",
            "request_sha256": _sha256_file(request_path),
            "source_revision": request["source_revision"],
            "sar_backend": request["sar_backend"],
            "full_fit_infer_seconds": full_elapsed,
            "validation_seconds": time.monotonic() - started - full_elapsed - pre_update_seconds,
            **({'pre_update_seconds': pre_update_seconds} if request.get('mae_protocol') else {}),
            "output_references": output_references,
            "outputs": {
                **{str(p.relative_to(output)): _sha256_file(p)
                   for p in sorted(output.rglob("*")) if p.is_file()},
                **{name: ref["sha256"] for name, ref in output_references.items()},
            },
        },
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--request", type=Path, required=True)
    run(parser.parse_args().request)
