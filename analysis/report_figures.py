"""Render a consistent figure set from a published five-fold and replay report.

This offline presentation layer reads published statistics without refitting
models, changing budgets, or filling incomplete sampling trajectories.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from matplotlib.axes import (  # pyright: ignore[reportMissingImports, reportMissingModuleSource]
        Axes,
    )
    from matplotlib.figure import (  # pyright: ignore[reportMissingImports, reportMissingModuleSource]
        Figure,
    )
    from numpy.typing import NDArray

SPACES = {
    "dpa-fined-rits-da": "DPA fine-tuned RitS",
    "rits-zero-shot-da": "RitS zero-shot",
    "autode": "AutoDE",
}
METHODS = {
    "predictive-uncertainty": ("Predictive uncertainty", "#0072B2"),
    "latin-hypercube": ("Latin hypercube", "#8C7853"),
    "ucb": ("Bayes-UCB", "#CC79A7"),
    "distance-uncertainty-product": ("Distance × uncertainty", "#D55E00"),
    "uncertainty-kriging-believer": ("Uncertainty + KB", "#56B4E9"),
    "intelligent": ("Intelligent", "#009E73"),
}
MODELS = {
    "rxnresid": "RxnResid",
    "rxngraphormer": "RXNGraphormer",
    "reactiont5": "ReactionT5",
    "chemprop_cgr": "Chemprop-CGR",
    "chemprop_multicomponent": "Chemprop-multicomponent",
    "uam": "UAM",
    "molclr": "MolCLR",
    "fingerprint_extra_trees": "Morgan Extra Trees",
    "fingerprint_rf": "Morgan RF",
    "fingerprint_ridge": "Morgan Ridge",
    "t5chem": "T5Chem",
}
STYLE = {
    "font.family": "DejaVu Sans",
    "font.size": 8,
    "axes.labelsize": 8,
    "axes.titlesize": 8,
    "axes.titleweight": "normal",
    "axes.linewidth": 0.6,
    "axes.spines.top": False,
    "axes.spines.right": False,
    "xtick.labelsize": 7,
    "ytick.labelsize": 7,
    "xtick.major.width": 0.6,
    "ytick.major.width": 0.6,
    "xtick.major.size": 3,
    "ytick.major.size": 3,
    "legend.fontsize": 7,
    "legend.frameon": False,
    "text.color": "#222222",
    "axes.labelcolor": "#222222",
    "axes.edgecolor": "#444444",
    "pdf.fonttype": 42,
    "svg.fonttype": "none",
    "svg.hashsalt": "tricycle-report-figures",
    "savefig.facecolor": "white",
}
CAPTIONS = {
    "fivefold-mae-comparison": {
        "caption": "Grouped five-fold test MAE on 3,252 GT paths per model. Small circles show individual folds; diamonds and whiskers show the unweighted fold mean and sample SD (n=5), not confidence intervals. All 11 models are retained. Color emphasizes RxnResid and RXNGraphormer without implying a significant ranking. The axis is linear.",
        "alt": "Eleven models ordered by mean fold MAE. RxnResid and RXNGraphormer are nearly tied around 1.03 kcal/mol, while T5Chem is near 5.62. Five fold observations and their dispersion remain visible for every model.",
    },
    "three-space-sampling": {
        "caption": "Historical six-strategy sampling comparison. Means and sample SD are computed on the original MAE scale and shown on a logarithmic axis. Solid segments contain five seeds; dotted segments contain fewer, and bands require at least two. Published curves use the latest checkpoint within each observed seed domain without interpolation or extrapolation. Horizontal dashed lines are full-pool LHS endpoint references, not theoretical lower bounds. Costs include initial TS sampling and exclude SAR and agent overhead. Methods are adaptations to the present pool.",
        "alt": "Three sampling spaces share the same logarithmic error axis. DPA fine-tuned RitS reaches the lowest endpoint error; historical intelligent sampling reaches MAE 2 at lower mean first-attainment cost than its baselines. This advantage does not hold in zero-shot, and AutoDE does not reach MAE 2.",
    },
    "language-comparison": {
        "caption": "English versus historical Chinese intelligent policies at matched seeds and costs. Top: paired mean MAE on shared log axes. Middle: mean English-minus-Chinese MAE with sample SD of paired differences on shared linear axes; negative favors English. Bottom: paired seed coverage. Solid segments use five seeds, dotted segments fewer. No interpolation or extrapolation. Historical prompts already included English instructions; narrative limits, authorized decision repairs and service variability prevent language-only causal attribution. Costs exclude SAR and agent overhead.",
        "alt": "Matched policy effects change with space and budget. English helps at some early DPA budgets but not consistently at later budgets or in other spaces. Coverage decreases to four paired seeds for zero-shot and three for AutoDE; the bottom panels expose these changes.",
    },
}


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(newline="") as stream:
        return list(csv.DictReader(stream))


def render(root: Path, output: Path) -> dict:
    import matplotlib  # pyright: ignore[reportMissingImports, reportMissingModuleSource]

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt  # pyright: ignore[reportMissingImports, reportMissingModuleSource]
    import numpy as np
    from matplotlib.lines import (  # pyright: ignore[reportMissingImports, reportMissingModuleSource]
        Line2D,
    )
    from matplotlib.ticker import (  # pyright: ignore[reportMissingImports, reportMissingModuleSource]
        FixedLocator,
        NullLocator,
        ScalarFormatter,
    )

    root, output = root.resolve(), output.resolve()
    if output.is_relative_to(root):
        raise ValueError("Figures must be outside the published result package")
    if output.exists() and any(output.iterdir()):
        raise FileExistsError(f"Refusing to replace an existing figure set: {output}")
    sources = {
        "models": "fivefold/results/model-summary.csv",
        "folds": "fivefold/results/fold-metrics.csv",
        "curves": "sampling-analysis/baseline-curves/curve-data.csv",
        "floors": "sampling-analysis/baseline-curves/empirical-floors.csv",
        "language": "sampling-analysis/paired-curve-data.csv",
    }
    inventory = dict(
        (name.removeprefix("*"), sha)
        for sha, name in (line.split(maxsplit=1) for line in (root / "SHA256SUMS").read_text().splitlines())
    )
    for relative in sources.values():
        if digest(root / relative) != inventory[relative]:
            raise ValueError(f"Published table hash mismatch: {relative}")
    tables = {key: read_csv(root / relative) for key, relative in sources.items()}
    output.mkdir(parents=True, exist_ok=True)
    audits = []

    def save(fig: Figure, name: str) -> None:
        fig.canvas.draw()
        renderer = fig.canvas.get_renderer()
        outside = []
        hidden_ticks: set[int] = set()
        for ax in fig.axes:
            for axis, limits in ((ax.xaxis, ax.get_xlim()), (ax.yaxis, ax.get_ylim())):
                low, high = sorted(limits)
                for tick in axis.get_major_ticks() + axis.get_minor_ticks():
                    if not low <= tick.get_loc() <= high:
                        hidden_ticks.update((id(tick.label1), id(tick.label2)))
        for text in fig.findobj(matplotlib.text.Text):
            if id(text) in hidden_ticks or not text.get_visible() or not text.get_text():
                continue
            box = text.get_window_extent(renderer)
            if box.width and box.height and (
                box.x0 < -1 or box.y0 < -1
                or box.x1 > fig.bbox.width + 1 or box.y1 > fig.bbox.height + 1
            ):
                outside.append(text.get_text())
        if outside:
            raise ValueError(f"Text outside figure {name}: {outside}")
        for extension in ("png", "pdf", "svg"):
            fig.savefig(output / f"{name}.{extension}", dpi=400)
        audits.append({"figure": name, "size_inches": list(fig.get_size_inches()),
                       "text_inside_canvas": True, "axes": len(fig.axes)})
        plt.close(fig)

    def axes_style(ax: Axes, panel: str | None = None, title: str | None = None) -> None:
        ax.grid(axis="y", color="#E5E5E5", lw=0.4, zorder=0)
        ax.set_axisbelow(True)
        if title:
            ax.set_title(title, pad=8)
        if panel:
            ax.text(-0.14, 1.07, panel, transform=ax.transAxes, weight="bold", fontsize=10)

    def log_mae(ax: Axes) -> None:
        ax.set_yscale("log")
        ax.set_ylim(0.38, 9)
        ax.yaxis.set_major_locator(FixedLocator([0.5, 1, 2, 4, 8]))
        ax.yaxis.set_major_formatter(ScalarFormatter())
        ax.yaxis.set_minor_locator(NullLocator())

    def coverage_line(ax: Axes, x: NDArray[Any], y: NDArray[Any],
                      counts: NDArray[Any], color: str, width: float = 1.2) -> None:
        ax.step(x, y, where="post", color=color, lw=width, ls=":")
        full = np.where(counts == 5, y, np.nan)
        ax.step(x, full, where="post", color=color, lw=width)

    with plt.rc_context(STYLE):
        summary = sorted(tables["models"], key=lambda r: float(r["fold_mae_mean"]))
        fig, ax = plt.subplots(figsize=(7.2, 3.8))
        fig.subplots_adjust(left=0.29, right=0.76, top=0.89, bottom=0.17)
        for i, row in enumerate(summary):
            model = row["model"]
            values = [float(r["mae"]) for r in sorted(tables["folds"], key=lambda r: int(r["fold"])) if r["model"] == model]
            color = {"rxnresid": "#009E73", "rxngraphormer": "#0072B2"}.get(model, "#777777")
            ax.scatter(values, i + np.linspace(-0.17, 0.17, len(values)), s=12,
                       facecolor="white", edgecolor=color, lw=0.6, zorder=3)
            ax.errorbar(float(row["fold_mae_mean"]), i, xerr=float(row["fold_mae_sd"]),
                        fmt="D", color=color, markersize=3.5, capsize=2, elinewidth=0.8, zorder=4)
            ax.text(1.05, i, f"{float(row['fold_mae_mean']):.3f} ± {float(row['fold_mae_sd']):.3f}",
                    transform=ax.get_yaxis_transform(), va="center", fontsize=7)
        ax.set_yticks(range(len(summary)), [MODELS[r["model"]] for r in summary])
        ax.set_ylim(len(summary) - 0.5, -0.5)
        ax.set_xlim(0, 6)
        ax.set_xlabel(r"Test MAE (kcal mol$^{-1}$)")
        ax.tick_params(axis="y", length=0, pad=6)
        ax.grid(axis="x", color="#E5E5E5", lw=0.4)
        ax.set_axisbelow(True)
        ax.text(1.05, 1.04, "Mean ± SD", transform=ax.transAxes, fontsize=7)
        fig.text(0.04, 0.96, "Grouped five-fold cross-validation", fontsize=9)
        fig.text(0.04, 0.05, "3,252 OOF paths per model · circles: folds · diamonds: mean · whiskers: sample SD (n = 5)", fontsize=7)
        save(fig, "fivefold-mae-comparison")

        fig, axes = plt.subplots(1, 3, figsize=(7.2, 3.55), sharey=True)
        fig.subplots_adjust(left=0.09, right=0.98, top=0.88, bottom=0.32, wspace=0.22)
        for i, (space, title) in enumerate(SPACES.items()):
            ax = axes[i]
            axes_style(ax, "abc"[i], title)
            log_mae(ax)
            for method, (_, color) in METHODS.items():
                rows = [r for r in tables["curves"] if r["space"] == space and r["variant"] == method]
                rows.sort(key=lambda r: float(r["cost"]))
                x = np.array([float(r["cost"]) / 1000 for r in rows])
                y = np.array([float(r["mean"]) for r in rows])
                sd = np.array([float(r["sd"]) if r["sd"] else np.nan for r in rows])
                counts = np.array([int(r["n_seeds"]) for r in rows])
                ax.fill_between(x, y - sd, y + sd, where=counts >= 2,
                                step="post", color=color, alpha=0.08, linewidth=0)
                coverage_line(ax, x, y, counts, color, 1.5 if method == "intelligent" else 0.9)
            floor = next(float(r["mean"]) for r in tables["floors"] if r["space"] == space)
            ax.axhline(floor, color="#555555", lw=0.8, ls=(0, (5, 3)))
            ax.set_xlim(left=0)
            if i == 0:
                ax.axhline(2, color="#888888", lw=0.7, ls=":")
        axes[0].set_ylabel(r"GT MAE (kcal mol$^{-1}$)")
        fig.supxlabel(r"Cumulative TS sampling cost (10$^3$ core-hours)", y=0.20, fontsize=8)
        handles = [Line2D([], [], color=color, label=label, lw=1.5) for label, color in METHODS.values()]
        fig.legend(handles=handles, loc="lower center", bbox_to_anchor=(0.5, 0.055), ncol=3,
                   columnspacing=1.5, handlelength=2.1)
        fig.text(0.09, 0.018, "Mean ± SD; solid: n = 5, dotted: n < 5. Dashed: full-pool LHS reference.", fontsize=6.5)
        save(fig, "three-space-sampling")

        fig, axes = plt.subplots(3, 3, figsize=(7.2, 5.1), sharex="col", sharey="row",
                                 gridspec_kw={"height_ratios": [2.2, 1.4, 0.5]})
        fig.subplots_adjust(left=0.10, right=0.98, top=0.9, bottom=0.17, wspace=0.22, hspace=0.16)
        for i, (space, title) in enumerate(SPACES.items()):
            rows = sorted((r for r in tables["language"] if r["space"] == space),
                          key=lambda r: float(r["budget_core_hours"]))
            x = np.array([float(r["budget_core_hours"]) / 1000 for r in rows])
            counts = np.array([int(r["paired_seeds"]) for r in rows])
            ax, delta, coverage = axes[:, i]
            axes_style(ax, "abc"[i], title)
            axes_style(delta)
            axes_style(coverage)
            log_mae(ax)
            for cohort, color in [("english", "#009E73"), ("chinese", "#CC79A7")]:
                y = np.array([float(r[f"{cohort}_mean_mae"]) for r in rows])
                coverage_line(ax, x, y, counts, color)
            y = np.array([float(r["mean_difference"]) for r in rows])
            sd = np.array([float(r["sd_difference"]) if r["sd_difference"] else np.nan for r in rows])
            delta.fill_between(x, y - sd, y + sd, where=counts >= 2,
                               step="post", color="#777777", alpha=0.15, linewidth=0)
            coverage_line(delta, x, y, counts, "#444444", 1)
            delta.axhline(0, color="#999999", lw=0.6)
            coverage.step(x, counts, where="post", color="#555555", lw=1)
            coverage.set_ylim(0, 5.8)
            coverage.set_yticks([0, 3, 5])
            coverage.set_xlim(left=0)
        axes[0, 0].set_ylabel(r"GT MAE (kcal mol$^{-1}$)")
        axes[1, 0].set_ylabel("English − Chinese\n" + r"(kcal mol$^{-1}$)")
        axes[2, 0].set_ylabel("Paired n")
        fig.legend(handles=[Line2D([], [], color="#009E73", label="English policy", lw=1.5),
                            Line2D([], [], color="#CC79A7", label="Historical Chinese policy", lw=1.5)],
                   loc="upper center", bbox_to_anchor=(0.54, 1), ncol=2)
        fig.supxlabel(r"Cumulative TS sampling cost (10$^3$ core-hours)", y=0.085, fontsize=8)
        fig.text(0.10, 0.038, "Solid: 5 paired seeds; dotted: partial coverage. Band: sample SD of paired differences.", fontsize=6.5)
        fig.text(0.10, 0.012, "Matched policy comparison; authorized repairs prevent language-only causal attribution.", fontsize=6.5)
        save(fig, "language-comparison")

    result = {
        "DATA_STATUS": "PROVIDED", "STYLE_STATUS": "ESTIMATED",
        "REPRODUCIBILITY_STATUS": "CODED", "EXPORT_STATUS": "READY",
        "INTEGRITY_STATUS": "PASS_WITH_WARNINGS",
        "style_note": "General academic style; no journal-specific compliance claimed.",
        "source_root": str(root),
        "source_hashes": {v: digest(root / v) for v in sources.values()},
        "script": str(Path(__file__).resolve()), "script_sha256": digest(Path(__file__)),
        "analysis_lock_sha256": digest(Path(__file__).parent / "uv.lock"),
        "software": {"matplotlib": matplotlib.__version__, "numpy": np.__version__},
        "style": STYLE, "captions": CAPTIONS, "layout_checks": audits,
        "limitations": ["Fold/seed SD is not a confidence interval.",
                        "Partial coverage retained without extrapolation.",
                        "Scientific metrics use the published tables unchanged.",
                        "Language cohorts include documented execution interventions."],
        "outputs": {p.name: digest(p) for p in sorted(output.iterdir())},
    }
    (output / "figure-manifest.json").write_text(json.dumps(result, indent=2) + "\n")
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report-dir", type=Path, required=True)
    parser.add_argument("--out-dir", type=Path, required=True)
    args = parser.parse_args()
    result = render(args.report_dir, args.out_dir)
    print(json.dumps({"figures": 3, "out_dir": str(args.out_dir), "status": result["EXPORT_STATUS"]}))


if __name__ == "__main__":
    main()
