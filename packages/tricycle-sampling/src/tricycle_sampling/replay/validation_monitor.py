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


def read(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


from tricycle_core.io import atomic_write_text as atomic_write  # noqa: E402
from tricycle_core.io import file_sha256 as digest  # noqa: E402


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
        for seed_root in sorted(experiment_root.glob("seed-*")):
            try:
                state = read(seed_root / "state.json")
                seed = int(seed_root.name.removeprefix("seed-"))
                base = {"project": item["project"], "variant": item["variant"], "seed": seed}
                completed = []
                for summary in sorted(seed_root.glob("round-*/evaluation-summary.json")):
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
    lines = ["## 最新验证集合 MAE", "", f"更新时间：{report['updated_at']}；单位 kcal/mol。",
             "每条轨迹取最近已完成并通过产物 hash、固定验证面板和模型版本校验的轮次；轮次 0 为初始训练。",
             "辅助验证 MAE 来自固定初始留出底物对的独立辅助模型，不代表主模型验证误差。",
             "GT MAE 来自主模型在完整固化 GT 上的只读评估，不反馈到采样决策。",
             "各 seed 当前轮次和累计核时不同，下表均值仅用于监控；公平策略比较仍使用预设核时预算。", "",
             "| 空间 | 策略 | 有结果 seed | 最近完成轮次（seed 顺序） | 辅助验证 MAE 均值 | 主模型 GT MAE 均值 |",
             "| --- | --- | --- | --- | ---: | ---: |"]
    for group in report["groups"]:
        rounds = ", ".join(f"{r['seed']}:{r['completed_round'] if r['completed_round'] is not None else '—'}" for r in group["latest"])
        lines.append(f"| {group['project']} | {group['variant']} | {group['available_seed_count']}/{group['seed_count']} | {rounds} | {number(group['validation_mae_mean'])} | {number(group['gt_mae_mean'])} |")
    lines.extend(["", "### 每条 seed 的最新记录", "", "Δ 为相较上一条已完成记录的变化，负值表示 MAE 下降；缺失值显示为 —。", "",
                  "| 空间 | 策略 | seed | 当前轮次/阶段 | 完成轮次 | 辅助验证 MAE | Δ | 验证路径数 | GT MAE | Δ | 累计核时 |",
                  "| --- | --- | ---: | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |"])
    for row in report["latest"]:
        lines.append(f"| {row['project']} | {row['variant']} | {row['seed']} | {row['current_round']}/{row['phase']} | {row['completed_round'] if row['completed_round'] is not None else '—'} | {number(row['validation_mae'])} | {number(row['validation_mae_delta'], True)} | {row.get('validation_n', '—')} | {number(row['gt_mae'])} | {number(row['gt_mae_delta'], True)} | {number(row.get('cumulative_sampling_cost_core_hours'))} |")
    lines.extend(["", "[完整逐轮历史](VALIDATION_MAE_HISTORY.csv) · [每条轨迹最新值](VALIDATION_MAE_LATEST.csv) · [校验及来源记录](VALIDATION_MAE_STATUS.json)", ""])
    if report["errors"]:
        lines.extend([f"有 {len(report['errors'])} 条产物读取或校验错误；未纳入对应结果，详见来源记录。", ""])
    return "\n".join(lines)


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    output = io.StringIO()
    fields = list(dict.fromkeys(key for row in rows for key in row))
    writer = csv.DictWriter(output, fieldnames=fields)
    writer.writeheader()
    writer.writerows(rows)
    atomic_write(path, output.getvalue())


def update(root: Path) -> dict[str, Any]:
    with (root / ".validation-monitor.lock").open("a") as lock:
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
    print(json.dumps(update(args.run_dir.resolve()), ensure_ascii=False))


if __name__ == "__main__":
    main()
