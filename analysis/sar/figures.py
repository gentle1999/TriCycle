"""Shared GT five-fold figures from verified scientific result tables."""

from __future__ import annotations

import json
import math
import os
from pathlib import Path
from typing import Any

import matplotlib  # pyright: ignore[reportMissingImports, reportMissingModuleSource]  # isort: skip

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # pyright: ignore[reportMissingImports, reportMissingModuleSource]  # isort: skip
import numpy as np

from matplotlib.colors import LogNorm  # pyright: ignore[reportMissingImports, reportMissingModuleSource]  # isort: skip

from tricycle_sar.results import read, verify


def render(out: Path, oof: list[dict], folds: list[dict], summary: list[dict]) -> None:
    plt.rcParams.update(
        {
            "font.family": "DejaVu Sans",
            "font.size": 11,
            "axes.titlesize": 13,
            "axes.labelsize": 12,
            "axes.spines.top": False,
            "axes.spines.right": False,
            "axes.edgecolor": "#CBD5E1",
            "text.color": "#142338",
            "axes.labelcolor": "#24344A",
            "xtick.color": "#42536A",
            "ytick.color": "#42536A",
            "savefig.facecolor": "white",
            "pdf.fonttype": 42,
            "svg.fonttype": "none",
        }
    )
    palette = [
        "#2563EB",
        "#0D9488",
        "#F97316",
        "#8B5CF6",
        "#E11D48",
        "#65A30D",
        "#0891B2",
        "#B45309",
        "#DB2777",
        "#64748B",
        "#1F2937",
    ]
    colors = {row["model"]: palette[i] for i, row in enumerate(summary)}

    def save(fig: Any, name: Any) -> None:
        for ext in ("png", "pdf", "svg"):
            fig.savefig(out / f"{name}.{ext}", dpi=220, bbox_inches="tight")
        plt.close(fig)

    fig, ax = plt.subplots(figsize=(11.5, 7.4))
    fig.subplots_adjust(left=0.28, right=0.77, top=0.84, bottom=0.15)
    fig.suptitle(
        "GT five-fold baseline comparison", x=0.06, ha="left", y=0.98, fontsize=20, weight="bold"
    )
    fig.text(
        0.06,
        0.92,
        f"{len(summary)} models · grouped test folds · G_T_activate · lower MAE is better",
        fontsize=12,
        color="#58677C",
    )
    for i, row in enumerate(summary):
        model = row["model"]
        vals = sorted((r for r in folds if r["model"] == model), key=lambda r: r["fold"])
        ax.scatter(
            [r["mae"] for r in vals],
            i + np.linspace(-0.14, 0.14, len(vals)),
            s=35,
            c=colors[model],
            alpha=0.4,
            zorder=3,
        )
        ax.errorbar(
            row["fold_mae_mean"],
            i,
            xerr=row["fold_mae_sd"],
            fmt="D",
            color=colors[model],
            markersize=7,
            capsize=5,
            elinewidth=2.3,
            zorder=4,
        )
        ax.text(
            1.03,
            i,
            f"{row['fold_mae_mean']:.3f} ± {row['fold_mae_sd']:.3f}",
            transform=ax.get_yaxis_transform(),
            va="center",
            fontsize=11,
        )
    ax.set_yticks(range(len(summary)), [row["label"] for row in summary])
    ax.invert_yaxis()
    ax.set_ylim(len(summary) - 0.5, -0.5)
    ax.set_xlim(left=0)
    ax.set_xlabel("Test MAE (kcal/mol)", labelpad=13)
    ax.grid(axis="x", color="#E8EDF3", linewidth=0.8)
    ax.set_axisbelow(True)
    ax.tick_params(axis="y", length=0, pad=12)
    ax.text(1.03, 1.03, "Mean ± SD", transform=ax.transAxes, fontsize=11, weight="bold")
    fig.text(
        0.06,
        0.045,
        f"{summary[0]['n_oof']:,} paths/model · dots: individual folds · diamonds: unweighted fold mean · whiskers: ±1 sample SD (n={summary[0]['folds']})",
        fontsize=10,
        color="#58677C",
    )
    save(fig, "fivefold-mae-comparison")

    all_values = np.array([r[key] for r in oof for key in ("y_true", "y_pred")])
    lo, hi = float(all_values.min()), float(all_values.max())
    pad = (hi - lo) * 0.035
    limits = (lo - pad, hi + pad)
    panel_rows = math.ceil((len(summary) + 1) / 4)
    fig, axes = plt.subplots(
        panel_rows, 4, figsize=(16, 4 * panel_rows), sharex=True, sharey=True, squeeze=False
    )
    fig.subplots_adjust(left=0.075, right=0.96, top=0.87, bottom=0.10, wspace=0.16, hspace=0.28)
    fig.suptitle(
        "Out-of-fold predictions against GT", x=0.04, ha="left", y=0.98, fontsize=21, weight="bold"
    )
    fig.text(
        0.04,
        0.934,
        "Identical test paths and axes across models · one held-out prediction per path · density on a logarithmic color scale",
        fontsize=12,
        color="#58677C",
    )
    xy = {
        row["model"]: (
            np.array([r["y_true"] for r in oof if r["model"] == row["model"]]),
            np.array([r["y_pred"] for r in oof if r["model"] == row["model"]]),
        )
        for row in summary
    }
    counts = []
    for ax, row in zip(axes.flat, summary, strict=False):
        y, pred = xy[row["model"]]
        hexes = ax.hexbin(
            y,
            pred,
            gridsize=40,
            extent=(*limits, *limits),
            mincnt=1,
            cmap="viridis",
            norm=LogNorm(vmin=1),
            linewidths=0,
        )
        counts.append(hexes)
        ax.plot(limits, limits, "--", color="#687588", linewidth=1.2)
        ax.set_title(row["label"], loc="left", weight="bold", fontsize=12)
        ax.text(
            0.04,
            0.96,
            f"MAE {row['oof_mae']:.3f}\nRMSE {row['oof_rmse']:.3f}\nR² {row['oof_r2']:.3f}",
            transform=ax.transAxes,
            va="top",
            fontsize=9.5,
            bbox={"facecolor": "white", "edgecolor": "none", "alpha": 0.88, "pad": 3},
        )
        ax.set_xlim(limits)
        ax.set_ylim(limits)
        ax.set_aspect("equal", adjustable="box")
        ax.tick_params(labelsize=9)
    maximum = max(float(h.get_array().max()) for h in counts)
    for h in counts:
        h.set_norm(LogNorm(vmin=1, vmax=maximum))
    for unused in axes.flat[len(summary) :]:
        unused.axis("off")
    last = axes.flat[-1]
    last.text(
        0.05,
        0.72,
        f"{summary[0]['n_oof']:,} OOF paths/model\n{summary[0]['folds']} grouped test folds\nShared x/y limits\nDashed line: prediction = GT",
        transform=last.transAxes,
        fontsize=11,
        linespacing=1.8,
    )
    cax = last.inset_axes([0.05, 0.23, 0.8, 0.06])
    fig.colorbar(counts[0], cax=cax, orientation="horizontal", label="Paths per hexagon")
    fig.supxlabel("GT G_T_activate (kcal/mol)", y=0.035)
    fig.supylabel("Predicted G_T_activate (kcal/mol)", x=0.015)
    save(fig, "fivefold-oof-parity")

    fig, ax = plt.subplots(figsize=(12, 7.2))
    fig.subplots_adjust(left=0.09, right=0.72, top=0.84, bottom=0.14)
    fig.suptitle(
        "Out-of-fold absolute error distribution",
        x=0.05,
        ha="left",
        y=0.98,
        fontsize=20,
        weight="bold",
    )
    fig.text(
        0.05,
        0.92,
        "A curve farther toward the upper left has more predictions within a smaller error",
        fontsize=12,
        color="#58677C",
    )
    for row in summary:
        y, pred = xy[row["model"]]
        errors = np.sort(np.abs(pred - y))
        cumulative = np.arange(1, len(errors) + 1) / len(errors)
        ax.step(
            np.r_[0, errors],
            np.r_[0, cumulative],
            where="post",
            color=colors[row["model"]],
            linewidth=1.9,
            label=row["label"],
        )
    ax.set_xlabel("Absolute error (kcal/mol)", labelpad=12)
    ax.set_ylabel("Cumulative fraction of OOF paths", labelpad=12)
    ax.set_xlim(left=0)
    ax.set_ylim(0, 1.015)
    ax.grid(color="#E8EDF3", linewidth=0.8)
    ax.legend(loc="center left", bbox_to_anchor=(1.03, 0.5), frameon=False, fontsize=10)
    ax.axhline(0.95, color="#94A3B8", linestyle=":", linewidth=1)
    fig.text(
        0.05,
        0.035,
        "All observed errors retained; no tail truncation. Curves use the same canonical GT values as the MAE and parity figures.",
        fontsize=10,
        color="#58677C",
    )
    save(fig, "fivefold-absolute-error-cdf")


def render_results(root: Path, output: Path) -> dict:
    """Render locally without modifying the published package or its figures."""
    root, output = root.resolve(), output.resolve()
    verify(root)
    if output.is_relative_to(root):
        raise ValueError("Render outside the published result package")
    if output.exists() and any(output.iterdir()):
        raise FileExistsError(f"Refusing to replace figures: {output}")
    tables = [
        read(root / "results" / name)
        for name in ("oof-predictions.csv", "fold-metrics.csv", "model-summary.csv")
    ]
    for table in tables:
        for source_row in table:
            row: dict = source_row
            for key in row.keys() - {"model", "label", "path_id"}:
                row[key] = (
                    int(row[key])
                    if key in {"fold", "folds", "row_index", "n_oof", "n_test"}
                    else float(row[key])
                )
    oof, folds, summary = tables
    summary.sort(key=lambda row: row["fold_mae_mean"])
    output.mkdir(parents=True, exist_ok=True)
    previous_epoch = os.environ.get("SOURCE_DATE_EPOCH")
    os.environ["SOURCE_DATE_EPOCH"] = "1791676800"
    try:
        with plt.rc_context({"svg.hashsalt": "tricycle-cycloaddition-v4-fivefold"}):
            render(output, oof, folds, summary)
    finally:
        if previous_epoch is None:
            os.environ.pop("SOURCE_DATE_EPOCH", None)
        else:
            os.environ["SOURCE_DATE_EPOCH"] = previous_epoch
    return {"out_dir": str(output), "figures": 3, "formats": ["png", "pdf", "svg"]}


def render_distributions(root: Path, output: Path, *, style: str = "analysis") -> dict:
    """Plot frozen GT and deviations from substrate-group means, without models."""
    from .distributions import activation_distribution_data

    if style not in {"analysis", "publication"}:
        raise ValueError(f"Unknown distribution style: {style}")
    root, output = root.resolve(), output.resolve()
    verify(root)
    if output.is_relative_to(root):
        raise ValueError("Render outside the published result package")
    if output.exists() and any(output.iterdir()):
        raise FileExistsError(f"Refusing to replace figures: {output}")
    manifest = json.loads((root / "manifest.json").read_text())
    data = activation_distribution_data(read(root / "data/dataset.csv"), manifest["target"])
    summary = data["summary"]
    summary.update(
        {
            "target": manifest["target"],
            "unit": manifest["unit"],
            "dataset_sha256": manifest["dataset_sha256"],
            "definitions": {
                "overall": "One activation Gibbs free energy per path; paths equally weighted",
                "within_group": "Path energy minus its (ene_id, diene_id) group's mean",
                "group_mean": "Group mean minus the unweighted mean of all group means; groups equally weighted",
                "multi_path": "Within-group residuals excluding singleton groups; paths equally weighted",
            },
        }
    )
    panels = [
        (
            "overall",
            "A. Activation free energies",
            "values",
            "#2563EB",
            r"$\Delta G^{\ddagger}$ (kcal/mol)",
        ),
        (
            "within-group",
            "B. Within-group residuals",
            "within_group_residuals",
            "#0D9488",
            r"$G_{gi}-\overline{G}_g$ (kcal/mol)",
        ),
        (
            "group-mean",
            "C. Group-mean residuals",
            "group_mean_residuals",
            "#7C3AED",
            r"$\overline{G}_g-\overline{G}_{\mathrm{groups}}$ (kcal/mol)",
        ),
    ]
    arrays = {key: np.asarray(data[key], dtype=float) for _, _, key, _, _ in panels}
    stats: dict[str, dict[str, Any] | None] = {}
    for key, values in {
        **arrays,
        "multi_path_residuals": np.asarray(data["multi_path_residuals"]),
    }.items():
        if not len(values):
            stats[key] = None
            continue
        stats[key] = {
            "n": len(values),
            "mean": float(values.mean()),
            "median": float(np.median(values)),
            "sd": float(values.std(ddof=0)),
            "variance": float(values.var(ddof=0)),
            "min": float(values.min()),
            "max": float(values.max()),
            "p2_5": float(np.quantile(values, 0.025)),
            "p97_5": float(np.quantile(values, 0.975)),
        }
    summary["distributions"] = stats
    summary["within_fraction_abs_le_1"] = float(
        np.mean(np.abs(arrays["within_group_residuals"]) <= 1)
    )
    summary["within_fraction_abs_le_2"] = float(
        np.mean(np.abs(arrays["within_group_residuals"]) <= 2)
    )
    output.mkdir(parents=True, exist_ok=True)
    if style == "publication":
        from .distribution_figures import render_publication

        return render_publication(root, output, data, summary)

    def cdf(ax: Any, values: Any, color: Any, label: Any = None, linestyle: Any = "-") -> None:
        ordered = np.sort(values)
        ax.step(
            ordered,
            np.arange(1, len(values) + 1) / len(values),
            where="post",
            color=color,
            linewidth=2,
            label=label,
            linestyle=linestyle,
        )

    def draw(axes: Any, panel: Any) -> None:
        name, title, key, color, xlabel = panel
        values, info = arrays[key], stats[key]
        if info is None:
            raise ValueError("Cannot plot an empty distribution panel")
        histogram, cumulative = axes
        edges = np.histogram_bin_edges(values, bins="fd")
        if key == "within_group_residuals":
            limit = max(0.5, float(np.max(np.abs(values))))
            edges = np.linspace(
                -limit * 1.025, limit * 1.025, min(121, max(21, int(2 * limit / 0.3)))
            )
        histogram.hist(
            values,
            bins=edges,
            density=True,
            color=color,
            alpha=0.75,
            edgecolor="white",
            linewidth=0.5,
            label="All paths" if name == "within-group" else None,
        )
        cdf(cumulative, values, color, "All paths" if name == "within-group" else None)
        reference = info["mean"] if name == "overall" else 0
        for ax in axes:
            ax.axvline(reference, color="#334155", linestyle="--", linewidth=1.1)
            ax.set_xlabel(xlabel, labelpad=8)
            ax.grid(axis="y", color="#E8EDF3", linewidth=0.7)
            ax.set_axisbelow(True)
            ax.tick_params(labelsize=10)
        histogram.set_title(title, loc="left", weight="bold", fontsize=14, pad=13)
        histogram.set_ylabel("Probability density (mol/kcal)")
        cumulative.set_title("Cumulative distribution", loc="left", fontsize=13, pad=13)
        cumulative.set_ylabel("Cumulative fraction")
        cumulative.set_ylim(0, 1.03)
        cumulative.axhspan(0.025, 0.975, color="#E8EDF3", alpha=0.4, zorder=0)
        note = (
            f"n = {info['n']:,}    Mean = {info['mean']:.2f}    SD = {info['sd']:.2f}\n"
            f"Range = [{info['min']:.2f}, {info['max']:.2f}] kcal/mol"
        )
        histogram.text(
            0.98,
            0.96,
            note,
            transform=histogram.transAxes,
            ha="right",
            va="top",
            fontsize=10,
            bbox={"facecolor": "white", "edgecolor": "none", "alpha": 0.9, "pad": 4},
        )
        if name == "within-group":
            multiple = np.asarray(data["multi_path_residuals"])
            if len(multiple):
                histogram.hist(
                    multiple,
                    bins=edges,
                    density=True,
                    histtype="step",
                    linewidth=1.8,
                    color="#F97316",
                    label="Multi-path groups only",
                )
                cdf(cumulative, multiple, "#F97316", "Multi-path groups only", "--")
                cumulative.legend(loc="lower right", frameon=False, fontsize=9)
            histogram.text(
                0.98,
                0.74,
                f"{summary['singleton_groups']:,} singleton groups contribute zero residuals",
                transform=histogram.transAxes,
                ha="right",
                va="top",
                fontsize=9,
                color="#58677C",
            )

    def save(fig: Any, name: Any) -> None:
        for ext in ("png", "pdf", "svg"):
            fig.savefig(output / f"{name}.{ext}", dpi=220, facecolor="white", bbox_inches="tight")
        plt.close(fig)

    with plt.rc_context(
        {
            "font.family": "DejaVu Sans",
            "font.size": 11,
            "text.color": "#142338",
            "axes.labelcolor": "#24344A",
            "axes.edgecolor": "#CBD5E1",
            "axes.spines.top": False,
            "axes.spines.right": False,
            "pdf.fonttype": 42,
            "svg.fonttype": "none",
            "svg.hashsalt": "tricycle-gt-distributions",
        }
    ):
        fig, axes = plt.subplots(3, 2, figsize=(15, 13))
        fig.subplots_adjust(left=0.08, right=0.98, bottom=0.09, top=0.90, hspace=0.55, wspace=0.25)
        fig.suptitle(
            "GT activation free energies and substrate-group residuals",
            x=0.055,
            y=0.97,
            ha="left",
            fontsize=22,
            weight="bold",
        )
        fig.text(
            0.055,
            0.935,
            f"{summary['paths']:,} paths · {summary['groups']:,} substrate pairs · all tails retained · SD uses ddof = 0",
            fontsize=12,
            color="#58677C",
        )
        for ax_pair, panel in zip(axes, panels, strict=True):
            draw(ax_pair, panel)
        fig.text(
            0.055,
            0.025,
            f"A/B: each path has equal weight. C: each substrate pair has equal weight.\n"
            f"Path mean: {summary['path_weighted_mean']:.3f}; equal-group mean: {summary['equal_group_mean']:.3f} kcal/mol. "
            "Residuals describe GT variation, not model prediction errors.",
            fontsize=10,
            color="#58677C",
        )
        save(fig, "gt-distributions-overview")
        for panel in panels:
            fig, axes = plt.subplots(1, 2, figsize=(13, 4.6))
            fig.subplots_adjust(left=0.075, right=0.98, bottom=0.20, top=0.85, wspace=0.27)
            draw(axes, panel)
            save(fig, f"gt-{panel[0]}-distribution")
    (output / "distribution-summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    return {
        "out_dir": str(output),
        "figures": 4,
        "formats": ["png", "pdf", "svg"],
        "summary": summary,
    }
