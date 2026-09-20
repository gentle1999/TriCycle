#!/usr/bin/env python3
"""Correct, merge, and plot downloaded RxnResid active-learning curves.

The first active-learning batches wrote ``ts_compute_seconds_sum`` under a
core-hour label.  This tool never edits the downloaded archives.  It joins
each selected substrate pair back to the authoritative project resource
export, uses ``ts_compute_hours_sum`` as the cost, and writes auditable
corrected trajectories plus a combined MAE-vs-core-hour plot.

Both downloaded Bohr ``out.zip`` files and already extracted output
directories are supported through the archive-root interface.  Missing or
invalid resource values are assigned zero, as required by the experiment
protocol, and are counted in the audit summary.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import re
import zipfile
from collections import defaultdict
from collections.abc import Iterable, Mapping
from pathlib import Path

PROJECTS = (
    "autode",
    "dpa-fined-rits-da",
    "rits-zero-shot-da",
)
PROJECT_MARKERS = {
    "autode": "o",
    "dpa-fined-rits-da": "s",
    "rits-zero-shot-da": "^",
}
STRATEGY_ORDER = (
    "random",
    "predictive-uncertainty",
    "epistemic-uncertainty",
    "aleatoric-uncertainty",
    "pair-core-set",
    "density-weighted-uncertainty",
    "uncertainty-diversity",
)


def _read_csv_text(text: str) -> list[dict[str, str]]:
    reader = csv.DictReader(text.splitlines())
    if reader.fieldnames is None:
        raise ValueError("CSV has no header")
    return [dict(row) for row in reader]


def _read_csv_file(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames is None:
            raise ValueError(f"CSV has no header: {path}")
        return [dict(row) for row in reader]


def _pair_id(row: Mapping[str, str]) -> str:
    value = row.get("pair_id", "").strip()
    if value:
        return value
    ene_id = row.get("ene_id", "").strip()
    diene_id = row.get("diene_id", "").strip()
    if not ene_id or not diene_id:
        raise ValueError("row has neither pair_id nor ene_id/diene_id")
    return f"{ene_id}+{diene_id}"


def _number(value: object, *, default: float = 0.0) -> float:
    if value is None or not str(value).strip():
        return default
    try:
        parsed = float(str(value).strip())
    except (TypeError, ValueError):
        return default
    return parsed if math.isfinite(parsed) else default


def _format(value: float) -> str:
    return format(value, ".12g")


def _safe_name(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.+-]+", "_", value)


def _resource_costs(path: Path) -> tuple[dict[str, float], dict[str, bool], dict[str, float]]:
    rows = _read_csv_file(path)
    costs: dict[str, float] = {}
    missing: dict[str, bool] = {}
    seconds: dict[str, float] = {}
    for row in rows:
        pair = _pair_id(row)
        if pair in costs:
            raise ValueError(f"duplicate pair in resource export {path}: {pair}")
        raw_hours = row.get("ts_compute_hours_sum", "")
        hours = _number(raw_hours, default=0.0)
        was_missing = not str(raw_hours).strip() or not math.isfinite(_number(raw_hours, default=math.nan))
        if hours < 0:
            raise ValueError(f"negative ts_compute_hours_sum in {path}: {pair}")
        costs[pair] = hours
        missing[pair] = was_missing
        seconds[pair] = _number(row.get("ts_compute_seconds_sum"), default=0.0)
    return costs, missing, seconds


def _iter_zip_trajectories(archive_root: Path) -> Iterable[tuple[str, str, str, list[dict[str, str]], list[dict[str, str]]]]:
    """Yield group id, job id, trajectory id, curve rows, decision rows."""

    for archive in sorted(archive_root.rglob("out.zip")):
        job_id = archive.parent.name
        group_id = archive_root.name
        try:
            with zipfile.ZipFile(archive) as handle:
                curve_members = [
                    name
                    for name in handle.namelist()
                    if name.endswith("/curve.csv") or name == "curve.csv"
                ]
                for curve_member in curve_members:
                    parts = Path(curve_member).parts
                    trajectory = parts[-2] if len(parts) >= 2 else job_id
                    prefix = curve_member[: -len("curve.csv")]
                    decision_member = f"{prefix}selection-decisions.csv"
                    if decision_member not in handle.namelist():
                        candidates = [
                            name
                            for name in handle.namelist()
                            if name.endswith("/selection-decisions.csv")
                            and Path(name).parts[-2] == trajectory
                        ]
                        if not candidates:
                            raise ValueError(
                                f"{archive}: no selection-decisions.csv for {trajectory}"
                            )
                        decision_member = candidates[0]
                    curve_rows = _read_csv_text(handle.read(curve_member).decode("utf-8"))
                    decision_rows = _read_csv_text(
                        handle.read(decision_member).decode("utf-8")
                    )
                    if not curve_rows:
                        continue
                    trajectory_from_curve = curve_rows[0].get("trajectory_id", "").strip()
                    if trajectory_from_curve:
                        trajectory = trajectory_from_curve
                    yield group_id, job_id, trajectory, curve_rows, decision_rows
        except zipfile.BadZipFile as exc:
            raise ValueError(f"incomplete or invalid archive: {archive}") from exc


def _iter_directory_trajectories(
    archive_root: Path,
) -> Iterable[tuple[str, str, str, list[dict[str, str]], list[dict[str, str]]]]:
    for curve_path in sorted(archive_root.rglob("curve.csv")):
        trajectory_dir = curve_path.parent
        decisions_path = trajectory_dir / "selection-decisions.csv"
        if not decisions_path.is_file():
            raise ValueError(f"{trajectory_dir}: missing selection-decisions.csv")
        curve_rows = _read_csv_file(curve_path)
        decision_rows = _read_csv_file(decisions_path)
        if not curve_rows:
            continue
        trajectory = curve_rows[0].get("trajectory_id", "").strip() or trajectory_dir.name
        yield archive_root.name, trajectory_dir.name, trajectory, curve_rows, decision_rows


def _iter_trajectories(
    archive_roots: Iterable[Path],
) -> Iterable[tuple[str, str, str, list[dict[str, str]], list[dict[str, str]]]]:
    for root in archive_roots:
        if not root.is_dir():
            raise ValueError(f"download root not found: {root}")
        if any(root.rglob("out.zip")):
            yield from _iter_zip_trajectories(root)
        else:
            yield from _iter_directory_trajectories(root)


def _correct_trajectory(
    *,
    curve_rows: list[dict[str, str]],
    decision_rows: list[dict[str, str]],
    costs: Mapping[str, float],
    missing_by_pair: Mapping[str, bool],
    seconds_by_pair: Mapping[str, float],
    resource_path: Path,
) -> tuple[list[dict[str, str]], list[dict[str, str]], dict[str, object]]:
    decisions_by_round: defaultdict[int, list[dict[str, str]]] = defaultdict(list)
    corrected_decisions: list[dict[str, str]] = []
    unknown_pairs = 0
    missing_values = 0
    seconds_from_resource = 0.0
    hours_from_resource = 0.0
    for original in decision_rows:
        row = dict(original)
        pair = _pair_id(row)
        known = pair in costs
        corrected_cost = costs.get(pair, 0.0)
        was_missing = not known or missing_by_pair.get(pair, False)
        if not known:
            unknown_pairs += 1
        if was_missing:
            missing_values += 1
        old_cost = row.get("materialized_sampling_cost_core_hours", "")
        old_column = row.get("cost_source_column", "")
        old_unit = row.get("cost_unit", "")
        row["raw_materialized_sampling_cost_core_hours"] = old_cost
        row["raw_cost_source_column"] = old_column
        row["raw_cost_unit"] = old_unit
        row["resource_cost_seconds"] = _format(seconds_by_pair.get(pair, corrected_cost * 3600.0))
        row["materialized_sampling_cost_core_hours"] = _format(corrected_cost)
        row["cost_was_missing"] = str(was_missing).lower()
        row["cost_source_column"] = "ts_compute_hours_sum"
        row["cost_unit"] = "core_hours"
        row["corrected_cost_source"] = str(resource_path)
        try:
            round_index = int(row.get("round_index", ""))
        except ValueError as exc:
            raise ValueError(f"invalid round_index in selection decisions: {row}") from exc
        decisions_by_round[round_index].append(row)
        corrected_decisions.append(row)
        hours_from_resource += corrected_cost
        seconds_from_resource += corrected_cost * 3600.0

    corrected_curve: list[dict[str, str]] = []
    cumulative = 0.0
    for original in sorted(curve_rows, key=lambda row: int(row.get("round_index", "0"))):
        row = dict(original)
        round_index = int(row.get("round_index", "0"))
        selected = decisions_by_round.get(round_index, [])
        round_cost = sum(_number(item.get("materialized_sampling_cost_core_hours")) for item in selected)
        raw_round_cost = _number(row.get("round_sampling_cost_core_hours"))
        raw_cumulative = _number(row.get("cumulative_sampling_cost_core_hours"))
        cumulative += round_cost
        row["raw_round_sampling_cost_core_hours"] = _format(raw_round_cost)
        row["raw_cumulative_sampling_cost_core_hours"] = _format(raw_cumulative)
        row["round_sampling_cost_core_hours"] = _format(round_cost)
        row["cumulative_sampling_cost_core_hours"] = _format(cumulative)
        row["cost_column"] = "ts_compute_hours_sum"
        row["cost_unit"] = "core_hours"
        row["corrected_cost_source"] = str(resource_path)
        row["selected_decision_count"] = str(len(selected))
        if selected:
            expected_count = int(row.get("new_pair_count", len(selected)))
            if expected_count != len(selected):
                row["cost_audit_warning"] = (
                    f"curve new_pair_count={expected_count}, decisions={len(selected)}"
                )
        else:
            row["cost_audit_warning"] = "no selection decisions for curve round"
        corrected_curve.append(row)

    summary = {
        "resource_path": str(resource_path),
        "decision_rows": len(corrected_decisions),
        "unknown_resource_pairs": unknown_pairs,
        "missing_or_invalid_hour_values": missing_values,
        "corrected_total_core_hours": hours_from_resource,
        "corrected_total_seconds_equivalent": seconds_from_resource,
        "raw_final_curve_cost": _number(curve_rows[-1].get("cumulative_sampling_cost_core_hours"))
        if curve_rows
        else 0.0,
    }
    return corrected_curve, corrected_decisions, summary


def _write_csv(path: Path, rows: list[Mapping[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    fields: list[str] = []
    for row in rows:
        for key in row:
            if key not in fields:
                fields.append(key)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def _parse_resources(values: list[str]) -> dict[str, Path]:
    result: dict[str, Path] = {}
    for value in values:
        if "=" not in value:
            raise ValueError(f"resource must be PROJECT=PATH: {value}")
        project, path = value.split("=", 1)
        if project not in PROJECTS:
            raise ValueError(f"unknown project in resource mapping: {project}")
        result[project] = Path(path).expanduser().resolve()
    missing = [project for project in PROJECTS if project not in result]
    if missing:
        raise ValueError(f"missing resource mappings: {', '.join(missing)}")
    return result


def _correct_and_merge(
    archive_roots: list[Path],
    resource_paths: Mapping[str, Path],
    output_dir: Path,
    min_pairs: int,
) -> tuple[list[dict[str, str]], list[dict[str, str]], list[dict[str, str]]]:
    resource_data = {project: _resource_costs(path) for project, path in resource_paths.items()}
    all_curves: list[dict[str, str]] = []
    eligible_curves: list[dict[str, str]] = []
    summaries: list[dict[str, str]] = []
    seen: set[tuple[str, str, int]] = set()
    trajectory_count = 0
    for group_id, job_id, trajectory, curve_rows, decision_rows in _iter_trajectories(archive_roots):
        trajectory_count += 1
        project = curve_rows[0].get("project", "").strip()
        if project not in resource_data:
            raise ValueError(f"{trajectory}: unknown project {project!r}")
        strategy = curve_rows[0].get("strategy", "").strip()
        seed = int(curve_rows[0].get("seed", "0"))
        identity = (project, strategy, seed)
        if identity in seen:
            raise ValueError(f"duplicate trajectory identity: {identity}")
        seen.add(identity)
        costs, missing_by_pair, seconds_by_pair = resource_data[project]
        corrected_curve, corrected_decisions, audit = _correct_trajectory(
            curve_rows=curve_rows,
            decision_rows=decision_rows,
            costs=costs,
            missing_by_pair=missing_by_pair,
            seconds_by_pair=seconds_by_pair,
            resource_path=resource_paths[project],
        )
        trajectory_output = output_dir / "trajectories" / _safe_name(trajectory)
        _write_csv(trajectory_output / "curve-corrected.csv", corrected_curve)
        _write_csv(trajectory_output / "selection-decisions-corrected.csv", corrected_decisions)
        max_pairs = max(
            (int(row.get("total_selected_pairs", "0")) for row in corrected_curve),
            default=0,
        )
        eligible = max_pairs >= min_pairs
        for row in corrected_curve:
            combined = {
                **row,
                "group_id": group_id,
                "job_id": job_id,
                "source_trajectory": trajectory,
                "eligible_min_pairs": str(eligible).lower(),
                "max_trajectory_pairs": str(max_pairs),
            }
            all_curves.append(combined)
            if eligible:
                eligible_curves.append(combined)
        summaries.append(
            {
                "group_id": group_id,
                "job_id": job_id,
                "trajectory_id": trajectory,
                "project": project,
                "strategy": strategy,
                "seed": str(seed),
                "curve_rounds": str(len(corrected_curve)),
                "max_selected_pairs": str(max_pairs),
                "eligible_min_pairs": str(eligible).lower(),
                "final_corrected_core_hours": _format(
                    _number(corrected_curve[-1].get("cumulative_sampling_cost_core_hours"))
                    if corrected_curve
                    else 0.0
                ),
                "decision_rows": str(audit["decision_rows"]),
                "unknown_resource_pairs": str(audit["unknown_resource_pairs"]),
                "missing_or_invalid_hour_values": str(audit["missing_or_invalid_hour_values"]),
                "raw_final_curve_cost": _format(float(audit["raw_final_curve_cost"])),
            }
        )

    if not trajectory_count:
        raise ValueError("no curve.csv files found in download roots")
    _write_csv(output_dir / "combined-curves-all.csv", all_curves)
    _write_csv(output_dir / f"combined-curves-min-{min_pairs}.csv", eligible_curves)
    _write_csv(output_dir / "trajectory-summary.csv", summaries)
    manifest = {
        "archive_roots": [str(path) for path in archive_roots],
        "resource_paths": {project: str(path) for project, path in resource_paths.items()},
        "cost_source_column": "ts_compute_hours_sum",
        "cost_unit": "core_hours",
        "seconds_to_hours_factor": 3600,
        "missing_cost_rule": "missing or invalid ts_compute_hours_sum is zero",
        "minimum_selected_pairs_for_plot": min_pairs,
        "trajectory_count": trajectory_count,
        "eligible_trajectory_count": len({
            (row["project"], row["strategy"], row["seed"])
            for row in summaries
            if row["eligible_min_pairs"] == "true"
        }),
    }
    (output_dir / "correction-manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return all_curves, eligible_curves, summaries


def _plot(
    rows: list[dict[str, str]],
    output_path: Path,
    *,
    title: str,
) -> None:
    try:
        import matplotlib.pyplot as plt
        import numpy as np
        import seaborn as sns
        from matplotlib.lines import Line2D
    except ImportError as exc:
        raise RuntimeError(
            "plotting requires matplotlib, numpy, and seaborn; run with "
            "uv run --with matplotlib --with numpy --with seaborn"
        ) from exc

    sns.set_theme(style="whitegrid", context="notebook")
    palette = sns.color_palette("tab10", n_colors=len(STRATEGY_ORDER))
    colors = dict(zip(STRATEGY_ORDER, palette, strict=True))
    grouped: defaultdict[tuple[str, str], list[dict[str, str]]] = defaultdict(list)
    for row in rows:
        grouped[(row["project"], row["strategy"])].append(row)

    fig, ax = plt.subplots(figsize=(13, 8), constrained_layout=True)
    for (project, strategy), group_rows in sorted(
        grouped.items(), key=lambda item: (PROJECTS.index(item[0][0]), STRATEGY_ORDER.index(item[0][1]))
    ):
        by_seed: defaultdict[int, list[dict[str, str]]] = defaultdict(list)
        for row in group_rows:
            by_seed[int(row["seed"])].append(row)
        curves: list[tuple[np.ndarray, np.ndarray]] = []
        for seed_rows in by_seed.values():
            ordered = sorted(seed_rows, key=lambda row: int(row["round_index"]))
            points = [
                (_number(row.get("cumulative_sampling_cost_core_hours"), default=math.nan),
                 _number(row.get("full_space_mae_kcal_mol"), default=math.nan))
                for row in ordered
            ]
            points = [(x, y) for x, y in points if math.isfinite(x) and math.isfinite(y)]
            if not points:
                continue
            points.sort()
            dedup: dict[float, float] = {}
            for x, y in points:
                dedup[x] = y
            x_values = np.array(sorted(dedup), dtype=float)
            y_values = np.array([dedup[x] for x in x_values], dtype=float)
            curves.append((x_values, y_values))
        if not curves:
            continue
        color = colors[strategy]
        marker = PROJECT_MARKERS.get(project, "o")
        min_x = min(float(x[0]) for x, _ in curves)
        max_x = max(float(x[-1]) for x, _ in curves)
        grid = np.linspace(min_x, max_x, 180) if max_x > min_x else np.array([min_x])
        interpolated = np.full((len(curves), len(grid)), np.nan, dtype=float)
        for index, (x_values, y_values) in enumerate(curves):
            inside = (grid >= x_values[0]) & (grid <= x_values[-1])
            if inside.any():
                interpolated[index, inside] = np.interp(grid[inside], x_values, y_values)
        count = np.sum(np.isfinite(interpolated), axis=0)
        mean = np.nanmean(interpolated, axis=0)
        lower = np.nanmin(interpolated, axis=0)
        upper = np.nanmax(interpolated, axis=0)
        valid = count > 0
        ax.plot(
            grid[valid],
            mean[valid],
            color=color,
            marker=marker,
            markevery=max(1, int(valid.sum() / 10)),
            linewidth=2.0,
            label=f"{project} / {strategy}",
        )
        band_valid = count >= 2
        if band_valid.any():
            ax.fill_between(
                grid[band_valid],
                lower[band_valid],
                upper[band_valid],
                color=color,
                alpha=0.12,
                linewidth=0,
            )

    strategy_handles = [
        Line2D([0], [0], color=colors[strategy], lw=2, label=strategy)
        for strategy in STRATEGY_ORDER
        if any(row["strategy"] == strategy for row in rows)
    ]
    project_handles = [
        Line2D(
            [0],
            [0],
            color="black",
            marker=PROJECT_MARKERS[project],
            linestyle="None",
            label=project,
        )
        for project in PROJECTS
        if any(row["project"] == project for row in rows)
    ]
    first_legend = ax.legend(handles=strategy_handles, title="Sampling strategy", loc="best")
    ax.add_artist(first_legend)
    ax.legend(handles=project_handles, title="Training collection", loc="lower left")
    ax.set_xlabel("Cumulative sampling cost (core-hours; ts_compute_hours_sum)")
    ax.set_ylabel("Full-space MAE (kcal/mol)")
    ax.set_title(title)
    ax.grid(True, which="major", alpha=0.25)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(output_path, dpi=220)
    plt.close(fig)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--archive-root",
        action="append",
        type=Path,
        required=True,
        help="Bohr group download directory or extracted result directory; repeatable",
    )
    parser.add_argument(
        "--resource",
        action="append",
        required=True,
        metavar="PROJECT=PATH",
        help="authoritative project ts-time-aggregation.csv; repeatable for all projects",
    )
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--min-pairs", type=int, default=1000)
    parser.add_argument("--no-plot", action="store_true")
    parser.add_argument(
        "--plot-all",
        action="store_true",
        help="also plot trajectories that did not reach --min-pairs",
    )
    args = parser.parse_args(argv)
    if args.min_pairs < 1:
        parser.error("--min-pairs must be positive")
    archive_roots = [path.expanduser().resolve() for path in args.archive_root]
    resource_paths = _parse_resources(args.resource)
    output_dir = args.output_dir.expanduser().resolve()
    all_curves, eligible, summaries = _correct_and_merge(
        archive_roots,
        resource_paths,
        output_dir,
        args.min_pairs,
    )
    if not args.no_plot:
        _plot(
            eligible,
            output_dir / "mae-vs-cost.png",
            title=f"RxnResid active learning: MAE vs cost (trajectories reaching {args.min_pairs} pairs)",
        )
        if args.plot_all:
            _plot(
                all_curves,
                output_dir / "mae-vs-cost-all-available.png",
                title="RxnResid active learning: MAE vs cost (all downloaded trajectory prefixes)",
            )
    print(
        json.dumps(
            {
                "output_dir": str(output_dir),
                "eligible_curve_rows": len(eligible),
                "eligible_trajectories": len({
                    (row["project"], row["strategy"], row["seed"])
                    for row in eligible
                }),
                "summary": str(output_dir / "trajectory-summary.csv"),
                "plot": None if args.no_plot else str(output_dir / "mae-vs-cost.png"),
                "plot_all": (
                    None
                    if args.no_plot or not args.plot_all
                    else str(output_dir / "mae-vs-cost-all-available.png")
                ),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
