"""Read accepted sampling outputs and publish validation/GT MAE monitoring.

This reporter never changes a campaign or supplies GT metrics to acquisition.
"""

from __future__ import annotations

import argparse
import csv
import fcntl
import io
import json
import math
import statistics
from datetime import datetime
from pathlib import Path
from typing import Any

from tricycle_core import record_io as rio


def read(path: Path) -> dict[str, Any]:
    return json.loads(rio.read_text(path, encoding="utf-8"))


from tricycle_core.record_io import atomic_write_text as atomic_write  # noqa: E402
from tricycle_core.record_io import file_sha256 as digest  # noqa: E402


def accepted_round(directory: Path, experiment: dict[str, Any], seed: int) -> dict[str, Any]:
    """Require a completed local evaluation and hash-verified worker metrics."""
    summary = read(directory / "evaluation-summary.json")
    report = read(directory / "result/worker-report.json")
    round_index = int(directory.name.removeprefix("round-"))
    if summary["seed"] != seed or summary["round_index"] != round_index:
        raise ValueError("evaluation seed/round mismatch")
    if report["source_revision"] != experiment["source_revision"]:
        raise ValueError("worker source revision mismatch")
    metrics: dict[str, dict[str, Any]] = {}
    for name in ("model-metrics.json", "evaluation.json"):
        path = directory / "result" / name
        if report["outputs"][name] != digest(path):
            raise ValueError(f"worker output hash mismatch: {name}")
        metrics[name] = read(path)
    validation = metrics["model-metrics.json"]
    evaluation = metrics["evaluation.json"]
    panel = experiment["validation_panels"][str(seed)]
    if (len(validation["validation_pairs"]) != len(panel)
            or set(validation["validation_pairs"]) != set(panel)
            or validation["panel_pair_count"] != len(panel)):
        raise ValueError("fixed validation panel mismatch")
    if set(panel) & set(validation["training_pairs"]):
        raise ValueError("validation panel leaked into auxiliary training")
    if (validation["role"] != "sampled-pair-holdout"
            or validation["validation_policy"] != "fixed-initial-pair-panel"
            or validation["scope"] != "auxiliary model trained from scratch; not full-model validation"):
        raise ValueError("unexpected validation role/policy/scope")
    if validation["checkpoint_sha256"] != report["outputs"]["validation-fit/model/checkpoint.pt"]:
        raise ValueError("auxiliary checkpoint provenance mismatch")
    if evaluation["role"] != "evaluation-only" or summary["role"] != "evaluation-only":
        raise ValueError("unexpected GT evaluation role")
    if (evaluation["ground_truth_sha256"] != experiment["evaluation_sha256"]
            or summary["ground_truth_sha256"] != experiment["evaluation_sha256"]):
        raise ValueError("GT snapshot mismatch")
    if summary["mae"] != evaluation["mae"] or summary["n"] != evaluation["n"]:
        raise ValueError("GT summary does not match worker output")
    for metric in (validation, evaluation):
        if metric["unit"] != "kcal/mol" or int(metric["n"]) <= 0:
            raise ValueError("invalid metric unit/sample count")
        if not math.isfinite(float(metric["mae"])) or float(metric["mae"]) < 0:
            raise ValueError("invalid MAE")
    return {
        "seed": seed, "completed_round": round_index,
        "validation_mae": float(validation["mae"]), "validation_n": validation["n"],
        "validation_panel_pairs": len(panel), "validation_scope": validation["scope"],
        "gt_mae": float(evaluation["mae"]), "gt_n": evaluation["n"],
        "cumulative_sampling_cost_core_hours": summary["cumulative_sampling_cost_core_hours"],
        "unit": "kcal/mol", "source_revision": report["source_revision"],
        "evaluation_sha256": evaluation["ground_truth_sha256"],
        "validation_metrics_path": str(directory / "result/model-metrics.json"),
        "validation_metrics_sha256": report["outputs"]["model-metrics.json"],
        "evaluation_path": str(directory / "result/evaluation.json"),
        "evaluation_metrics_sha256": report["outputs"]["evaluation.json"],
    }


def collect(root: Path) -> dict[str, Any]:
    latest, history, errors, groups = [], [], [], []
    for item in read(root / "sampling-plan.json")["experiments"]:
        experiment_root = Path(item["root"])
        try:
            experiment = read(experiment_root / "experiment.json")
        except (OSError, ValueError) as exc:
            errors.append({"path": str(experiment_root), "error": str(exc)})
            continue
        group_latest = []
        for seed_root in sorted(rio.glob(experiment_root, "seed-*")):
            try:
                state = read(seed_root / "state.json")
                seed = int(seed_root.name.removeprefix("seed-"))
                base = {"project": item["project"], "variant": item["variant"], "seed": seed}
                completed = []
                for summary in sorted(rio.glob(seed_root, "round-*/evaluation-summary.json")):
                    try:
                        row = {**base, **accepted_round(summary.parent, experiment, seed)}
                        completed.append(row)
                    except (OSError, ValueError, KeyError, TypeError) as exc:
                        errors.append({"path": str(summary.parent), "error": str(exc)})
                completed.sort(key=lambda row: row["completed_round"])
                history.extend(completed)
                row = {**base, "current_round": state["round"], "phase": state["phase"],
                       "completed_round": None, "validation_mae": None, "gt_mae": None,
                       "previous_completed_round": None, "validation_mae_delta": None,
                       "gt_mae_delta": None}
                if completed:
                    row.update(completed[-1])
                if len(completed) > 1:
                    previous = completed[-2]
                    row.update(previous_completed_round=previous["completed_round"],
                               validation_mae_delta=row["validation_mae"] - previous["validation_mae"],
                               gt_mae_delta=row["gt_mae"] - previous["gt_mae"])
                latest.append(row)
                group_latest.append(row)
            except (OSError, ValueError, KeyError, TypeError) as exc:
                errors.append({"path": str(seed_root), "error": str(exc)})
        available = [row for row in group_latest if row["completed_round"] is not None]
        groups.append({"project": item["project"], "variant": item["variant"],
                       "seed_count": len(experiment["validation_panels"]),
                       "available_seed_count": len(available), "latest": group_latest,
                       "validation_mae_mean": statistics.mean(r["validation_mae"] for r in available) if available else None,
                       "gt_mae_mean": statistics.mean(r["gt_mae"] for r in available) if available else None})
    return {"updated_at": datetime.now().astimezone().isoformat(), "groups": groups,
            "latest": latest, "history": history, "errors": errors}


def number(value: Any, signed: bool = False) -> str:
    if value is None:
        return "—"
    return f"{value:+.4f}" if signed else f"{value:.4f}"


def markdown(report: dict[str, Any]) -> str:
    lines = ["## Latest validation-set MAE", "", f"Updated: {report['updated_at']}; unit: kcal/mol.",
             "For each trajectory, use the latest completed round that passes artifact hash, fixed validation panel, and model revision checks; round 0 is initial training.",
             "Auxiliary validation MAE comes from an independent auxiliary model on the fixed initial holdout pairs; it is not main-model validation error.",
             "GT MAE comes from read-only evaluation of the main model on the complete frozen GT set and is not fed back into sampling decisions.",
             "Seeds differ in current round and cumulative core-hours. The means below are for monitoring only; fair strategy comparisons use predefined core-hour budgets.", "",
             "| Space | Strategy | Seeds with results | Latest completed rounds (by seed) | Mean auxiliary validation MAE | Mean main-model GT MAE |",
             "| --- | --- | --- | --- | ---: | ---: |"]
    for group in report["groups"]:
        rounds = ", ".join(f"{r['seed']}:{r['completed_round'] if r['completed_round'] is not None else '—'}" for r in group["latest"])
        lines.append(f"| {group['project']} | {group['variant']} | {group['available_seed_count']}/{group['seed_count']} | {rounds} | {number(group['validation_mae_mean'])} | {number(group['gt_mae_mean'])} |")
    lines.extend(["", "### Latest record for each seed", "", "Δ is the change from the previous completed record; negative values mean lower MAE. Missing values appear as —.", "",
                  "| Space | Strategy | Seed | Current round/phase | Completed round | Auxiliary validation MAE | Δ | Validation paths | GT MAE | Δ | Cumulative core-hours |",
                  "| --- | --- | ---: | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |"])
    for row in report["latest"]:
        lines.append(f"| {row['project']} | {row['variant']} | {row['seed']} | {row['current_round']}/{row['phase']} | {row['completed_round'] if row['completed_round'] is not None else '—'} | {number(row['validation_mae'])} | {number(row['validation_mae_delta'], True)} | {row.get('validation_n', '—')} | {number(row['gt_mae'])} | {number(row['gt_mae_delta'], True)} | {number(row.get('cumulative_sampling_cost_core_hours'))} |")
    lines.extend(["", "[Full round history](VALIDATION_MAE_HISTORY.csv) · [Latest values per trajectory](VALIDATION_MAE_LATEST.csv) · [Validation and provenance records](VALIDATION_MAE_STATUS.json)", ""])
    if report["errors"]:
        lines.extend([f"There are {len(report['errors'])} artifact read or validation errors; affected results are excluded. See the provenance records.", ""])
    return "\n".join(lines)


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    output = io.StringIO()
    fields = list(dict.fromkeys(key for row in rows for key in row))
    writer = csv.DictWriter(output, fieldnames=fields)
    writer.writeheader()
    writer.writerows(rows)
    atomic_write(path, output.getvalue())


def update(root: Path) -> dict[str, Any]:
    with rio.open(root / ".validation-monitor.lock", "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        report = collect(root)
        atomic_write(root / "VALIDATION_MAE_STATUS.json", json.dumps(report, ensure_ascii=False, indent=2) + "\n")
        write_csv(root / "VALIDATION_MAE_LATEST.csv", report["latest"])
        write_csv(root / "VALIDATION_MAE_HISTORY.csv", report["history"])
        atomic_write(root / "VALIDATION_MAE_REPORT.md", markdown(report))
    return {"updated_at": report["updated_at"], "trajectories": len(report["latest"]),
            "available": sum(r["completed_round"] is not None for r in report["latest"]),
            "completed_records": len(report["history"]), "errors": report["errors"]}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(update(rio.resolve(args.run_dir)), ensure_ascii=False))


if __name__ == "__main__":
    main()
