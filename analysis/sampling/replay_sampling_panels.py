"""Freeze current sampling evidence and plot cost/GT MAE and paired random residuals."""

from __future__ import annotations

import argparse
import bisect
import csv
import hashlib
import json
import math
import statistics
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from tricycle_core.asset_store import resolve_tree

SPACES = {
    "dpa-fined-rits-da": "DPA fine-tuned RitS",
    "rits-zero-shot-da": "RitS zero-shot",
    "autode": "AutoDE",
}
METHODS = {
    "random": ("Random", "#717782", False),
    "epistemic-uncertainty": ("Epistemic", "#4393C3", False),
    "aleatoric-uncertainty": ("Aleatoric", "#E3AA39", False),
    "predictive-uncertainty": ("Predictive", "#9473B4", False),
    "latin-hypercube": ("LHS · Science 2024", "#617A35", False),
    "ucb": ("Bayes-UCB · Nature 2024", "#9A5D99", False),
    "distance-uncertainty-product": ("A Score · Nat Commun 2024", "#AA793E", False),
    "uncertainty-kriging-believer": ("Uncertainty + KB · JACS 2025", "#357E90", False),
    "uncertainty-cost": ("Uncertainty / cost", "#D87588", False),
    "intelligent": ("Intelligent sampling", "#008775", True),
    "autonomous": ("Autonomous", "#174E8C", True),
    "feedback": ("+ Feedback", "#C74F21", True),
    "reviewed": ("+ Feedback + review", "#008775", True),
}


def sha(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def read(path: Path) -> Any:
    return json.loads(path.read_text())


def write_csv(path: Path, rows: list[dict]) -> None:
    with path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]) if rows else [])
        writer.writeheader()
        writer.writerows(rows)


def verified_gt(directory: Path, experiment: dict, seed: int) -> dict:
    """Final rounds may omit auxiliary validation; verify the main GT output directly."""
    summary = read(directory / "evaluation-summary.json")
    report = read(directory / "result/worker-report.json")
    evaluation_path = directory / "result/evaluation.json"
    evaluation = read(evaluation_path)
    if report["outputs"]["evaluation.json"] != sha(evaluation_path):
        raise ValueError("GT worker output hash mismatch")
    if report["source_revision"] != experiment["source_revision"]:
        raise ValueError("worker source mismatch")
    if summary["seed"] != seed or summary["round_index"] != int(
        directory.name.removeprefix("round-")
    ):
        raise ValueError("GT seed/round mismatch")
    for field in ("mae", "n", "role", "unit", "ground_truth_sha256"):
        if summary[field] != evaluation[field]:
            raise ValueError("GT summary differs from worker evaluation")
    if (
        evaluation["role"] != "evaluation-only"
        or evaluation["unit"] != "kcal/mol"
        or evaluation["ground_truth_sha256"] != experiment["evaluation_sha256"]
        or evaluation["n"] <= 0
    ):
        raise ValueError("GT evaluation scope mismatch")
    return {
        "evaluation_sha256": evaluation["ground_truth_sha256"],
        "gt_n": evaluation["n"],
        "unit": evaluation["unit"],
        "source_revision": report["source_revision"],
        "completed_round": summary["round_index"],
        "gt_mae": evaluation["mae"],
    }


def summarize(points: list[dict]) -> tuple[list[dict], list[dict]]:
    """Available-seed means; never extend a seed beyond its observed cost domain."""
    summaries, residuals = [], []
    reference = (
        "random" if any(p["variant"] == "random" for p in points) else "predictive-uncertainty"
    )
    for space in SPACES:
        local = [p for p in points if p["space"] == space]
        variants = [v for v in METHODS if any(p["variant"] == v for p in local)]
        tracks = {}
        for variant in variants:
            for seed in sorted({p["seed"] for p in local if p["variant"] == variant}):
                track = sorted(
                    [p for p in local if p["variant"] == variant and p["seed"] == seed],
                    key=lambda p: p["round_index"],
                )
                if [p["round_index"] for p in track] != list(range(len(track))):
                    raise ValueError("noncontiguous completed trajectory")
                if any(b["cost"] < a["cost"] for a, b in zip(track, track[1:], strict=False)):
                    raise ValueError("decreasing sampling cost")
                if any(
                    not math.isfinite(p["cost"])
                    or p["cost"] < 0
                    or not math.isfinite(p["mae"])
                    or p["mae"] < 0
                    for p in track
                ):
                    raise ValueError("invalid metric or cost")
                tracks[(variant, seed)] = ([p["cost"] for p in track], track)
        grid = sorted({p["cost"] for p in local})

        def available(variant: Any, budget: Any, tracks: Any = tracks) -> Any:
            values = {}
            for (method, seed), (costs, track) in tracks.items():
                if method == variant and costs[0] <= budget <= costs[-1]:
                    index = bisect.bisect_right(costs, budget) - 1
                    values[seed] = track[index]["mae"]
            return values

        for budget in grid:
            random = available(reference, budget)
            for variant in variants:
                values = available(variant, budget)
                if not values:
                    continue
                summaries.append(
                    {
                        "space": space,
                        "variant": variant,
                        "cost": budget,
                        "n_seeds": len(values),
                        "seeds": list(values),
                        "mean": statistics.mean(values.values()),
                        "sd": statistics.stdev(values.values()) if len(values) > 1 else None,
                    }
                )
                if variant == reference:
                    continue
                seeds = sorted(set(values) & set(random))
                if seeds:
                    deltas = [values[s] - random[s] for s in seeds]
                    residuals.append(
                        {
                            "space": space,
                            "variant": variant,
                            "cost": budget,
                            "n_seeds": len(seeds),
                            "seeds": seeds,
                            "mean": statistics.mean(deltas),
                            "sd": statistics.stdev(deltas) if len(seeds) > 1 else None,
                            "seed_deltas": deltas,
                        }
                    )
    return summaries, residuals


def freeze(root: Path, output: Path) -> dict:
    plan_path = root / "sampling-plan.json"
    if not plan_path.exists():
        plan_path = root / "suite.json"
    plan = read(plan_path)
    if (
        plan_path.name == "suite.json"
        and sha(plan_path) != read(root / "suite-hash.json")["sha256"]
    ):
        raise ValueError("frozen suite hash mismatch")
    points, statuses, sources = [], [], {str(plan_path): sha(plan_path)}
    scope = None
    # Freeze the inventory first, so later arrivals cannot silently enter this figure.
    inventory = []
    for item in plan["experiments"]:
        experiment_root = Path(item["root"])
        inventory.append(
            (
                item,
                experiment_root,
                sorted(experiment_root.glob("seed-*/round-*/evaluation-summary.json")),
            )
        )
    for item, experiment_root, paths in inventory:
        config_path = experiment_root / "experiment.json"
        experiment = read(config_path)
        if sha(config_path) != read(experiment_root / "experiment-hash.json")["sha256"]:
            raise ValueError("frozen experiment hash mismatch")
        expected_config = item.get("config_sha256")
        if expected_config is None:
            suite_root = Path(item["suite"])
            suite_path = suite_root / "suite.json"
            if sha(suite_path) != read(suite_root / "suite-hash.json")["sha256"]:
                raise ValueError("frozen suite hash mismatch")
            bound = next(e for e in read(suite_path)["experiments"]
                         if Path(e["root"]) == experiment_root and e["variant"] == item["variant"])
            expected_config = bound["config_sha256"]
            sources[str(suite_path)] = sha(suite_path)
        if sha(config_path) != expected_config:
            raise ValueError("sampling plan/experiment hash mismatch")
        sources[str(config_path)] = sha(config_path)
        if item["project"] != experiment["project"] or item["variant"] not in METHODS:
            raise ValueError("unexpected experiment identity")
        private = (resolve_tree(experiment["asset_references"]["private"])
                   if "asset_references" in experiment else experiment_root / "private")
        pool_path = private / "pair-pool.csv"
        if sha(pool_path) != experiment["private_hashes"]["pair-pool.csv"]:
            raise ValueError("cost pool hash mismatch")
        sources[str(pool_path)] = sha(pool_path)
        with pool_path.open() as stream:
            pool_rows = list(csv.DictReader(stream))
        pool = {p["pair_id"]: p for p in pool_rows}
        if len(pool) != len(pool_rows) or any(
            p["cost_unit"] != "core_hours" or p["cost_source_column"] != "ts_cpu_hours"
            for p in pool.values()
        ):
            raise ValueError("cost pool identity/unit/source mismatch")
        previous: dict[int, Any] = {}
        for path in paths:
            value = read(path)
            seed = int(path.parent.parent.name.removeprefix("seed-"))
            verified = verified_gt(path.parent, experiment, seed)
            identity = (
                verified["evaluation_sha256"],
                verified["gt_n"],
                verified["unit"],
                verified["source_revision"],
                experiment["training"]["epochs"],
            )
            if scope is None:
                scope = identity
            if identity != scope:
                raise ValueError("GT/source/training scopes differ")
            selected = value["selected_ids"]
            last = previous.get(seed, set())
            if len(selected) != len(set(selected)) or not last <= set(selected):
                raise ValueError("sampling history is not cumulative and unique")
            previous[seed] = set(selected)
            cost = math.fsum(
                float(pool[p]["materialized_sampling_cost_core_hours"]) for p in selected
            )
            if not math.isclose(
                cost, value["cumulative_sampling_cost_core_hours"], rel_tol=0, abs_tol=1e-6
            ):
                raise ValueError("cost does not match selected pool accounting")
            points.append(
                {
                    "space": item["project"],
                    "variant": item["variant"],
                    "seed": seed,
                    "round_index": verified["completed_round"],
                    "cost": cost,
                    "mae": verified["gt_mae"],
                    "n_gt": verified["gt_n"],
                    "sampled_pairs": len(selected),
                    "evaluation_file": str(path),
                }
            )
            for evidence in (
                path,
                path.parent / "result/worker-report.json",
                path.parent / "result/evaluation.json",
            ):
                sources[str(evidence)] = sha(evidence)
        for state_path in sorted(experiment_root.glob("seed-*/state.json")):
            state = read(state_path)
            local = [
                p
                for p in points
                if p["space"] == item["project"]
                and p["variant"] == item["variant"]
                and p["seed"] == state["seed"]
            ]
            statuses.append(
                {
                    "space": item["project"],
                    "variant": item["variant"],
                    "seed": state["seed"],
                    "phase": state["phase"],
                    "completed_checkpoints": len(local),
                    "latest_cost": local[-1]["cost"] if local else None,
                }
            )
    summaries, residuals = summarize(points)
    value = {
        "schema": "tricycle.three-space-sampling-panels/v1",
        "snapshot_at": datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(),
        "run_dir": str(root),
        "evaluation_scope": scope,
        "source_hashes": sources,
        "points": points,
        "statuses": statuses,
        "summaries": summaries,
        "residuals": residuals,
        "accounting": "Unique selected pair TS CPU seconds / 3600; includes initialization; excludes SAR/agent overhead.",
        "alignment": "Latest affordable completed checkpoint within each seed's observed cost domain; no extrapolation or interpolation.",
        "band": "Mean +/-1 sample SD (ddof=1); no band for one seed; available seed counts may vary.",
        "residual": "Per-seed method GT MAE minus reference GT MAE at the same cost; summarize paired differences only.",
        "reference": "random"
        if any(p["variant"] == "random" for p in points)
        else "predictive-uncertainty",
    }
    output.mkdir(parents=True, exist_ok=False)
    (output / "snapshot.json").write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")
    for name, rows in (
        ("checkpoints", points),
        ("coverage", statuses),
        ("mae-by-cost", summaries),
        ("paired-residuals", residuals),
    ):
        write_csv(output / f"{name}.csv", rows)
    return value


def render(value: dict, output: Path) -> None:
    import matplotlib  # pyright: ignore[reportMissingImports, reportMissingModuleSource]  # isort: skip

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt  # pyright: ignore[reportMissingImports, reportMissingModuleSource]  # isort: skip
    import numpy as np
    from matplotlib.lines import Line2D  # pyright: ignore[reportMissingImports, reportMissingModuleSource]  # isort: skip
    from matplotlib.ticker import FuncFormatter, MaxNLocator  # pyright: ignore[reportMissingImports, reportMissingModuleSource]  # isort: skip

    plt.rcParams.update(
        {
            "font.family": "DejaVu Sans",
            "font.size": 11,
            "axes.spines.top": False,
            "axes.spines.right": False,
            "svg.fonttype": "none",
            "pdf.fonttype": 42,
        }
    )
    active_spaces = {
        key: label
        for key, label in SPACES.items()
        if any(p["space"] == key for p in value["points"])
    }
    if not active_spaces:
        raise ValueError("no completed checkpoints to render")
    fig, axes = plt.subplots(
        2,
        len(active_spaces),
        figsize=(6.2 * len(active_spaces), 9.6),
        squeeze=False,
        sharey="row",
        sharex=False,
    )

    def curve(ax: Any, rows: Any, color: Any, intelligent: Any) -> None:
        if not rows:
            return
        x = np.array([r["cost"] / 1000 for r in rows])
        mean = np.array([r["mean"] for r in rows])
        sd = np.array([r["sd"] if r["sd"] is not None else np.nan for r in rows])
        count = np.array([r["n_seeds"] for r in rows])
        style = "-" if intelligent else (0, (4, 2.7))
        ax.step(x, mean, where="post", color=color, lw=1.15, alpha=0.55, linestyle=style)
        # Thick segments represent full seed coverage; partial coverage stays visible.
        complete = np.where(count == 5, mean, np.nan)
        ax.step(x, complete, where="post", color=color, lw=2.25, linestyle=style)
        ax.fill_between(
            x, mean - sd, mean + sd, color=color, alpha=0.10, step="post", where=count >= 2
        )
        ax.plot(
            x[-1],
            mean[-1],
            "o",
            color=color,
            ms=3.5,
            markerfacecolor="white" if count[-1] < 5 else color,
        )

    for column, (space, label) in enumerate(active_spaces.items()):
        upper, lower = axes[:, column]
        for variant, (_, color, intelligent) in METHODS.items():
            for ax, field in ((upper, "summaries"), (lower, "residuals")):
                rows = [r for r in value[field] if r["space"] == space and r["variant"] == variant]
                curve(ax, rows, color, intelligent)
        upper.set_title(f"{label}\n{space}", fontsize=12, fontweight="bold", pad=12)
        lower.axhline(0, color="#555D66", lw=1, zorder=0)
        local = [p for p in value["points"] if p["space"] == space]
        maximum = max(p["cost"] for p in local) / 1000
        for ax in (upper, lower):
            ax.set_xlim(0, maximum * 1.025)
            ax.grid(axis="y", color="#CBD3DA", alpha=0.55, linewidth=0.65)
            ax.grid(axis="x", color="#E5E9EC", alpha=0.35, linewidth=0.5)
            ax.set_axisbelow(True)
            ax.xaxis.set_major_locator(MaxNLocator(6))
            ax.xaxis.set_major_formatter(FuncFormatter(lambda x, _: f"{x:g}"))
            ax.set_xlabel("Cumulative sampling cost (10³ CPU core-hours)")
        upper.text(
            0.03,
            0.97,
            chr(65 + column),
            transform=upper.transAxes,
            va="top",
            fontweight="bold",
            fontsize=13,
        )
        lower.text(
            0.03,
            0.97,
            chr(68 + column),
            transform=lower.transAxes,
            va="top",
            fontweight="bold",
            fontsize=13,
        )
    axes[0, 0].set_ylabel("GT MAE (kcal/mol)", labelpad=12)
    reference_label = value.get("reference", "random")
    axes[1, 0].set_ylabel(f"Δ GT MAE vs {reference_label} (kcal/mol)", labelpad=12)
    present = {p["variant"] for p in value["points"]}
    handles = [
        Line2D([0], [0], color=color, lw=2.3, linestyle="-" if intelligent else "--", label=label)
        for variant, (label, color, intelligent) in METHODS.items()
        if variant in present
    ]
    fig.legend(
        handles=handles,
        loc="upper center",
        bbox_to_anchor=(0.5, 0.94),
        ncol=5,
        frameon=False,
        columnspacing=2.1,
        handlelength=2.8,
        fontsize=10,
    )
    fig.suptitle(
        "Matched sampling · sampling cost and GT error", fontsize=16, y=0.992, fontweight="bold"
    )
    fig.text(
        0.5,
        0.949,
        f"Completed checkpoints, including unfinished trajectories · snapshot {value['snapshot_at'][:16].replace('T', ' ')} (UTC+8)",
        ha="center",
        fontsize=10,
        color="#56616B",
    )
    fig.text(
        0.5,
        0.045,
        "Dashed: static methods · Solid: intelligent methods · Band: mean ±1 sample SD · Thin segments: fewer than 5 seeds (no band for n=1)",
        ha="center",
        fontsize=9.5,
    )
    fig.text(
        0.5,
        0.022,
        f"Bottom row: paired method − {reference_label} at the same cost; negative is better. Latest affordable checkpoints; no extrapolation. GT is evaluation-only.",
        ha="center",
        fontsize=9.5,
    )
    fig.subplots_adjust(top=0.82, bottom=0.13, left=0.075, right=0.985, wspace=0.08, hspace=0.39)
    outputs = {}
    for extension in ("png", "pdf", "svg"):
        path = output / f"sampling-cost-gt-mae-residuals.{extension}"
        fig.savefig(path, dpi=220, facecolor="white")
        outputs[path.name] = sha(path)
    plt.close(fig)
    (output / "figure-manifest.json").write_text(
        json.dumps(
            {
                "snapshot_sha256": sha(output / "snapshot.json"),
                "renderer_sha256": sha(Path(__file__)),
                "matplotlib": matplotlib.__version__,
                "outputs": outputs,
            },
            indent=2,
        )
        + "\n"
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", type=Path)
    parser.add_argument("--out-dir", type=Path, required=True)
    parser.add_argument("--snapshot", type=Path)
    args = parser.parse_args()
    if args.snapshot:
        value = read(args.snapshot)
        expected = summarize(value["points"])
        if expected != (value["summaries"], value["residuals"]):
            raise ValueError("snapshot aggregates differ from checkpoints")
        render(value, args.out_dir.resolve())
    else:
        value = freeze(args.run_dir.resolve(), args.out_dir.resolve())
    phases = Counter(r["phase"] for r in value["statuses"])
    print(
        json.dumps(
            {
                "output": str(args.out_dir),
                "checkpoints": len(value["points"]),
                "trajectories": len(value["statuses"]),
                "phases": dict(phases),
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
