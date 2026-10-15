"""Fold-level boxplots from frozen benchmark data and complete worker predictions."""
from __future__ import annotations

import json
from pathlib import Path

from tricycle_core.asset_store import resolve_input

import matplotlib  # pyright: ignore[reportMissingImports, reportMissingModuleSource]  # isort: skip

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # pyright: ignore[reportMissingImports, reportMissingModuleSource]  # isort: skip
from matplotlib.ticker import LogLocator, NullFormatter, ScalarFormatter  # pyright: ignore[reportMissingImports, reportMissingModuleSource]  # isort: skip
import numpy as np
from tricycle_core.io import file_sha256
from tricycle_core.toml import load_toml
from tricycle_sar.benchmark import write_rows
from tricycle_sar.modelling_benchmark import collect

LABELS = {
    "rxnresid": "RxnResid",
    "rxngraphormer": "RXNGraphormer",
    "reactiont5": "ReactionT5",
    "chemprop_cgr": "Chemprop · CGR",
    "chemprop_multicomponent": "Chemprop · multicomponent",
    "uam": "UAM",
    "molclr": "MolCLR",
    "fingerprint_extra_trees": "Fingerprint · Extra Trees",
    "fingerprint_rf": "Fingerprint · Random Forest",
    "fingerprint_ridge": "Fingerprint · Ridge",
    "t5chem": "T5Chem",
}


def render_run_boxplot(
    run: Path, prediction_dir: Path, output: Path, *, xscale: str = "log",
    aspect_ratio: float = 2.0, width_inches: float = 7.2,
) -> dict:
    if xscale not in {"linear", "log"}:
        raise ValueError("MAE axis scale must be linear or log")
    if not all(np.isfinite(v) and v > 0 for v in (aspect_ratio, width_inches)):
        raise ValueError("Figure dimensions must be positive and finite")
    run, prediction_dir, output = run.resolve(), prediction_dir.resolve(), output.resolve()
    if output.is_relative_to(run) or output.is_relative_to(prediction_dir):
        raise ValueError("Render outside the frozen inputs and prediction directory")
    if output.exists() and any(output.iterdir()):
        raise FileExistsError(f"Refusing to replace figures: {output}")
    summary = collect(run, prediction_dir=prediction_dir)
    state = load_toml(run / "run.toml")
    models = sorted(summary, key=lambda model: summary[model]["fold_mae_mean"])
    values = [[float(fold["mae"]) for fold in summary[model]["folds"]] for model in models]
    if any(len(folds) != 5 for folds in values):
        raise ValueError("Boxplot requires five complete test folds per model")
    if xscale == "log" and min(min(folds) for folds in values) <= 0:
        raise ValueError("Log MAE requires positive values; zero errors cannot be hidden or shifted")
    output.mkdir(parents=True, exist_ok=True)
    fold_rows = [
        {"model": model, "fold": fold, "n_test": metrics["n"], "mae": metrics["mae"],
         "rmse": metrics["rmse"], "r2": metrics.get("r2", "")}
        for model in models for fold, metrics in enumerate(summary[model]["folds"])
    ]
    model_rows = [
        {"model": model, "fold_mae_mean": summary[model]["fold_mae_mean"],
         "fold_mae_sd": summary[model]["fold_mae_std"],
         "oof_mae": summary[model]["pooled"]["mae"]}
        for model in models
    ]
    write_rows(output / "fold-metrics.csv", fold_rows)
    write_rows(output / "model-summary.csv", model_rows)
    with plt.rc_context({
        "font.family": "sans-serif", "font.sans-serif": ["Arial", "Helvetica", "DejaVu Sans"],
        "font.size": 7.5, "axes.titlesize": 8, "axes.labelsize": 7.5,
        "xtick.labelsize": 7, "ytick.labelsize": 7,
        "axes.spines.top": False, "axes.spines.right": False, "axes.spines.left": True,
        "axes.edgecolor": "#292D3E", "axes.linewidth": 0.65, "text.color": "#292D3E",
        "axes.labelcolor": "#292D3E", "xtick.color": "#292D3E", "ytick.color": "#292D3E",
        "pdf.fonttype": 42, "svg.fonttype": "none", "svg.hashsalt": "sar-fold-boxplot",
    }):
        fig, ax = plt.subplots(figsize=(width_inches, width_inches / aspect_ratio))
        fig.subplots_adjust(left=0.27, right=0.96, top=0.88, bottom=0.16)
        ax.set_title("Five-fold cross-validation", loc="left", pad=6)
        ax.text(0.98, 1.025, "Mean ± SD; n = 5 folds", transform=ax.transAxes,
                fontsize=7, color="#55596A", ha="right", va="bottom")
        positions = np.arange(len(models))
        ax.boxplot(values, orientation="horizontal", positions=positions, widths=0.54,
                   patch_artist=True, showfliers=False, whis=1.5,
                   boxprops={"facecolor": "#7884B4", "edgecolor": "#292D3E", "linewidth": 0.65},
                   medianprops={"color": "white", "linewidth": 0.9},
                   whiskerprops={"color": "#292D3E", "linewidth": 0.65},
                   capprops={"color": "#292D3E", "linewidth": 0.65})
        for index, fold_values in enumerate(values):
            ax.scatter(fold_values, index + np.linspace(-0.16, 0.16, 5),
                       s=10, color="#292D3E", edgecolors="white", linewidths=0.35, zorder=4)
            model = models[index]
            ax.annotate(
                f"{summary[model]['fold_mae_mean']:.3f} ± {summary[model]['fold_mae_std']:.3f}",
                xy=(max(fold_values), index), xytext=(7, 0), textcoords="offset points",
                ha="left", va="center", fontsize=7, color="#55596A",
            )
        ax.set_yticks(positions, [LABELS.get(model, model) for model in models])
        ax.set_ylim(len(models) - 0.55, -0.55)
        ax.set_xscale(xscale)
        ax.set_xlim(min(min(folds) for folds in values) / 1.10 if xscale == "log" else 0,
                    max(max(folds) for folds in values) * (1.65 if xscale == "log" else 1.25))
        if xscale == "log":
            ax.xaxis.set_major_locator(LogLocator(base=10, subs=(1, 1.5, 2, 3, 4, 6, 8)))
            ax.xaxis.set_major_formatter(ScalarFormatter())
            ax.xaxis.set_minor_formatter(NullFormatter())
        ax.set_xlabel(r"MAE (kcal mol$^{-1}$)" + (" · log scale" if xscale == "log" else ""), labelpad=4)
        ax.tick_params(axis="y", length=2.5, width=0.65, pad=3)
        ax.tick_params(axis="x", which="major", length=2.5, width=0.65, pad=3)
        ax.tick_params(axis="x", which="minor", length=0)
        ax.grid(False, which="both")
        ax.set_axisbelow(True)
        fig.canvas.draw()
        renderer = fig.canvas.get_renderer()
        bounds = fig.bbox
        for text in [*fig.texts, *ax.texts, *ax.get_yticklabels(), ax.xaxis.label, ax.title]:
            box = text.get_window_extent(renderer)
            if box.x0 < 0 or box.y0 < 0 or box.x1 > bounds.width or box.y1 > bounds.height:
                raise ValueError(f"Figure text lies outside canvas: {text.get_text()}")
        for extension in ("png", "pdf", "svg", "tiff"):
            options = {"pil_kwargs": {"compression": "tiff_lzw"}} if extension == "tiff" else {}
            fig.savefig(output / f"fivefold-mae-boxplot.{extension}", dpi=600, facecolor="white", **options)
        plt.close(fig)
    (output / "figure-caption.txt").write_text(
        f"GT grouped five-fold cross-validation for {len(models)} models; {state['rows']:,} paths per model. "
        "Each dot represents one held-out test fold (n = 5). Boxes show the 25th–75th percentiles; "
        "white lines show medians; whiskers reach the most extreme values within 1.5 IQR. "
        "All fold values are shown. Lower MAE is better. Models are ordered by arithmetic mean MAE. "
        "The axis scale changes display only; statistics are calculated on original MAE values. "
        "Labels to the right of each box show arithmetic mean ± sample standard deviation "
        "across the five folds (ddof = 1); exact values are in model-summary.csv. "
        "No inferential significance test was performed.\n"
    )
    inputs = [run / "run.toml", resolve_input(run, "dataset.csv"), resolve_input(run, "splits.csv")]
    inputs += sorted(prediction_dir.rglob("predictions.csv"))
    audit = {
        "models": len(models), "folds": 5, "paths_per_model": state["rows"],
        "metric": "test MAE against frozen GT, kcal/mol", "sd_ddof": 1,
        "figure_contract": {
            "claim": "Compare held-out MAE and fold-to-fold variation across all evaluated models",
            "archetype": "quantitative comparison", "backend": "Python/matplotlib",
            "xscale": xscale, "aspect_ratio": aspect_ratio,
            "width_inches": width_inches, "height_inches": width_inches / aspect_ratio,
            "canvas_ratio_scope": "Entire exported image, including title, axes labels and margins; no tight crop",
            "style": {"fill": "#7884B4", "ink": "#292D3E", "axes_linewidth": 0.65,
                      "font_family": ["Arial", "Helvetica", "DejaVu Sans"], "grid": False},
            "statistics": "Arithmetic summaries on raw MAE; log is an axis transform only; no significance test",
        },
        "backend_sha256": state["training_protocol_manifest_sha256"], "methods": state["methods"],
        "inputs": [{"path": str(p), "sha256": file_sha256(p)} for p in inputs],
        "plot_source_sha256": file_sha256(Path(__file__)),
        "analysis_lock_sha256": file_sha256(Path(__file__).parents[1] / "uv.lock"),
        "matplotlib": matplotlib.__version__, "numpy": np.__version__,
        "validation": "Frozen data/split hashes, group separation, exact OOF coverage, identity, targets and finite metrics verified",
    }
    (output / "figure-data.json").write_text(json.dumps(audit, indent=2) + "\n")
    return {"out_dir": str(output), "models": len(models), "folds": len(fold_rows),
            "formats": ["png", "pdf", "svg", "tiff"], "summary": model_rows}
