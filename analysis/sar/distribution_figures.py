"""Publication layout for frozen GT distributions, using the shared plot environment."""

from __future__ import annotations

import json
import os
import shlex
from pathlib import Path
from typing import Any

import matplotlib  # pyright: ignore[reportMissingImports, reportMissingModuleSource]  # isort: skip

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # pyright: ignore[reportMissingImports, reportMissingModuleSource]  # isort: skip
import numpy as np

from matplotlib.ticker import MaxNLocator  # pyright: ignore[reportMissingImports, reportMissingModuleSource]  # isort: skip
from tricycle_core.io import file_sha256
from tricycle_sar import results as sar_results

BLUE = "#7884B4"
INK = "#292D3E"
ACCENT = "#A76646"
PANELS = (
    ("a", "Activation free energy", "values", r"$\Delta G^{\ddagger}$ (kcal mol$^{-1}$)"),
    (
        "b",
        "Within-group residual",
        "within_group_residuals",
        r"$G_{gi}-\overline{G}_g$ (kcal mol$^{-1}$)",
    ),
    (
        "c",
        "Group-mean residual",
        "group_mean_residuals",
        r"$\overline{G}_g-\overline{G}_{\mathrm{groups}}$ (kcal mol$^{-1}$)",
    ),
)


def render_publication(root: Path, output: Path, data: dict, summary: dict) -> dict:
    """Export a 183-mm triptych and complementary ECDF, with editable vector text."""
    arrays = {key: np.asarray(data[key]) for _, _, key, _ in PANELS}
    multi = np.asarray(data["multi_path_residuals"])
    edges = {key: np.histogram_bin_edges(values, bins="fd") for key, values in arrays.items()}
    limit = max(0.5, float(np.max(np.abs(arrays["within_group_residuals"]))))
    edges["within_group_residuals"] = np.linspace(
        -limit * 1.025, limit * 1.025, min(121, max(21, int(2 * limit / 0.3)))
    )
    layouts = {}
    outputs = []

    def ecdf(ax: Any, values: Any, color: Any, label: Any = None, linestyle: Any = "-") -> None:
        ordered = np.sort(values)
        ax.step(
            np.r_[ordered[0], ordered],
            np.r_[0, np.arange(1, len(values) + 1) / len(values)],
            where="post",
            color=color,
            label=label,
            linestyle=linestyle,
            linewidth=1.25,
        )

    def capture(fig: Any, axes: Any, name: Any) -> None:
        fig.canvas.draw()
        renderer = fig.canvas.get_renderer()
        fw, fh = fig.bbox.width, fig.bbox.height
        elements = []
        panels = []
        for ax, (letter, _, _, _) in zip(axes, PANELS, strict=True):
            panels.append(
                {
                    "id": letter,
                    "row": 0,
                    "bbox": list(ax.get_position().bounds),
                    "plot_type": "histogram" if "overview" in name else "ecdf",
                    "semantic_color_map": {"all_observations": BLUE, "multi_path_groups": ACCENT},
                }
            )
            items = [
                (ax.title, "panel_title"),
                (ax.xaxis.label, "x_label"),
                (ax.yaxis.label, "y_label"),
            ]
            for positions, labels, bounds in (
                (ax.get_xticks(), ax.get_xticklabels(), ax.get_xlim()),
                (ax.get_yticks(), ax.get_yticklabels(), ax.get_ylim()),
            ):
                # Matplotlib allocates labels for out-of-range locator ticks but does not draw them.
                items.extend(
                    (label, "tick_label")
                    for position, label in zip(positions, labels, strict=True)
                    if bounds[0] <= position <= bounds[1]
                )
            items.extend(
                (label, "panel_label" if label.get_text() == letter else "annotation")
                for label in ax.texts
            )
            if ax.get_legend():
                items.extend((label, "legend_label") for label in ax.get_legend().get_texts())
            for text, role in items:
                if not text.get_visible() or not text.get_text():
                    continue
                box = text.get_window_extent(renderer)
                if not box.width or not box.height:
                    continue
                elements.append(
                    {
                        "panel_id": letter,
                        "role": role,
                        "text": text.get_text(),
                        "font_size_pt": text.get_fontsize(),
                        "bbox": [box.x0 / fw, box.y0 / fh, box.width / fw, box.height / fh],
                    }
                )
        for legend in fig.legends:
            for text in legend.get_texts():
                box = text.get_window_extent(renderer)
                elements.append(
                    {
                        "panel_id": "figure",
                        "role": "legend_label",
                        "text": text.get_text(),
                        "font_size_pt": text.get_fontsize(),
                        "bbox": [box.x0 / fw, box.y0 / fh, box.width / fw, box.height / fh],
                    }
                )
        layout = {
            "figure_id": name,
            "layout_engine": "manual_axes",
            "canvas": {"width_mm": 183, "height_mm": fig.get_figheight() * 25.4},
            "panels": panels,
            "text_elements": elements,
            "semantic_color_map": {"all_observations": BLUE, "multi_path_groups": ACCENT},
        }
        layouts[name] = layout
        (output / f"{name}-layout.json").write_text(json.dumps(layout, indent=2) + "\n")
        # Fixed canvas preserves specified physical dimensions and audited normalized bounds.
        for extension in ("svg", "pdf", "png", "tiff"):
            path = output / f"{name}.{extension}"
            options = {"pil_kwargs": {"compression": "tiff_lzw"}} if extension == "tiff" else {}
            fig.savefig(path, dpi=600 if extension == "tiff" else 300, facecolor="white", **options)
            outputs.append(path)
        plt.close(fig)

    rc = {
        "font.family": "sans-serif",
        "font.sans-serif": ["Arial", "Helvetica", "DejaVu Sans"],
        "font.size": 7.5,
        "axes.titlesize": 8,
        "axes.labelsize": 7.5,
        "xtick.labelsize": 7,
        "ytick.labelsize": 7,
        "legend.fontsize": 6.3,
        "axes.linewidth": 0.65,
        "axes.edgecolor": INK,
        "text.color": INK,
        "axes.labelcolor": INK,
        "xtick.color": INK,
        "ytick.color": INK,
        "axes.spines.top": False,
        "axes.spines.right": False,
        "legend.frameon": False,
        "pdf.fonttype": 42,
        "svg.fonttype": "none",
        "svg.hashsalt": "tricycle-gt-publication-v1",
    }
    with plt.rc_context(rc):
        for kind, height in (("overview", 85), ("ecdf", 72)):
            fig = plt.figure(figsize=(183 / 25.4, height / 25.4))
            axes = [fig.add_axes([left, 0.25, 0.245, 0.55]) for left in (0.078, 0.408, 0.738)]
            for ax, (letter, title, key, xlabel) in zip(axes, PANELS, strict=True):
                values, info = arrays[key], summary["distributions"][key]
                ax.set_title(title, loc="left", pad=23)
                ax.text(
                    -0.22,
                    1.18,
                    letter,
                    transform=ax.transAxes,
                    fontsize=10,
                    weight="bold",
                    va="bottom",
                )
                ax.text(
                    0,
                    1.035,
                    f"n = {len(values):,}   SD = {info['sd']:.2f}",
                    transform=ax.transAxes,
                    color="#55596A",
                    fontsize=6.7,
                    va="bottom",
                )
                ax.set_xlabel(xlabel, labelpad=5)
                ax.tick_params(length=2.5, width=0.65, pad=3)
                reference = info["mean"] if key == "values" else 0
                ax.axvline(
                    reference, color="#606475", linestyle=(0, (3, 2)), linewidth=0.8, zorder=3
                )
                if kind == "overview":
                    ax.hist(
                        values,
                        bins=edges[key],
                        density=True,
                        color=BLUE,
                        edgecolor="white",
                        linewidth=0.3,
                        label="All paths" if letter == "b" else None,
                    )
                    if letter == "b" and len(multi):
                        ax.hist(
                            multi,
                            bins=edges[key],
                            density=True,
                            histtype="step",
                            color=ACCENT,
                            linestyle="--",
                            linewidth=1.0,
                            label="Multi-path groups",
                        )
                    ax.set_ylabel(r"Density (mol kcal$^{-1}$)", labelpad=3)
                    ax.yaxis.set_major_locator(MaxNLocator(4))
                else:
                    ecdf(ax, values, INK)
                    if letter == "b" and len(multi):
                        # All-observation identity is unchanged between histogram and ECDF.
                        ax.lines[-1].set_color(BLUE)
                        ax.lines[-1].set_label("All paths")
                        ecdf(ax, multi, ACCENT, "Multi-path groups", "--")
                    else:
                        ax.lines[-1].set_color(BLUE)
                    ax.set_ylabel("Cumulative fraction", labelpad=3)
                    ax.set_ylim(0, 1.02)
                    ax.set_yticks([0, 0.5, 1])
                ax.set_xlim(float(edges[key][0]), float(edges[key][-1]))
                ax.xaxis.set_major_locator(MaxNLocator(4))
            handles, labels = axes[1].get_legend_handles_labels()
            if handles:
                fig.legend(
                    handles,
                    labels,
                    loc="lower center",
                    bbox_to_anchor=(0.53, 0.015),
                    ncol=2,
                    handlelength=2.0,
                    columnspacing=1.7,
                )
            capture(fig, axes, f"gt-distributions-{kind}")

    counts = summary
    caption = (
        "GT activation free energies and substrate-group deviations. "
        f"(a) Distribution of activation Gibbs free energies for {counts['paths']:,} paths "
        f"(mean {counts['path_weighted_mean']:.2f} kcal/mol). "
        "(b) Within-group residuals, defined as each path's value minus the mean of its "
        "(ene_id, diene_id) substrate group. Filled bars include all paths; the dashed outline "
        f"shows {len(multi):,} paths in {counts['groups'] - counts['singleton_groups']:,} multi-path groups. "
        f"The {counts['singleton_groups']:,} singleton groups contribute "
        "zero residuals to the all-path distribution. Each distribution is normalized separately. "
        f"(c) Residuals of the {counts['groups']:,} group means relative to their unweighted mean "
        f"({counts['equal_group_mean']:.4f} kcal/mol); each group has equal weight. SD is the population standard "
        "deviation (ddof=0), in kcal/mol. Dashed vertical lines mark the overall mean in a "
        "and zero in b/c. Histograms and empirical cumulative distributions retain every "
        "observed value; x and y scales differ between panels. Bins use the Freedman-Diaconis "
        "rule in a/c and symmetric equal-width edges in b. These are descriptive GT distributions, "
        "not model errors or sampling confidence intervals; no hypothesis tests are performed.\n"
    )
    (output / "figure-caption.md").write_text(caption)
    (output / "figure-alt-text.txt").write_text(
        "Three panels show a roughly unimodal activation free energy distribution with a high-energy "
        "right tail, narrow within-group deviations with a sharp peak at zero, and broader group-mean "
        "deviations with a right tail. A dashed outline in the middle panel excludes singleton groups. "
        "All observed tails are retained. A separate triptych provides empirical cumulative distributions.\n"
    )
    spec = {
        "figure_id": "gt-activation-distributions-publication",
        "backend": "Python / Matplotlib",
        "core_conclusion": "Substrate-group means vary more broadly than paths within the same substrate group.",
        "archetype": "quantitative grid",
        "main_dimensions_mm": [183, 85],
        "ecdf_dimensions_mm": [183, 72],
        "panel_map": [
            {"id": letter, "title": title, "variable": key, "histogram_edges": edges[key].tolist()}
            for letter, title, key, _ in PANELS
        ],
        "statistics": "Descriptive census of frozen GT; ddof=0; no inferential intervals or tests",
        "review_risks": [
            "Path and group weightings differ",
            "Singleton residuals are identically zero",
            "Independent axis ranges are explicitly stated",
            "Style is estimated without a named journal",
        ],
        "status": {
            "DATA_STATUS": "PROVIDED",
            "STYLE_STATUS": "ESTIMATED",
            "REPRODUCIBILITY_STATUS": "CODED",
            "EXPORT_STATUS": "READY",
            "INTEGRITY_STATUS": "PASS_WITH_WARNINGS",
        },
    }
    (output / "figure-spec.json").write_text(json.dumps(spec, indent=2) + "\n")
    (output / "visual-claim-ledger.json").write_text(
        json.dumps(
            [
                {
                    "panel": letter,
                    "claim": title,
                    "source": "frozen GT dataset",
                    "computation": key,
                    "n": summary["distributions"][key]["n"],
                    "sd_ddof0": summary["distributions"][key]["sd"],
                }
                for letter, title, key, _ in PANELS
            ],
            indent=2,
        )
        + "\n"
    )
    (output / "distribution-summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    project = Path(__file__).resolve().parents[2]
    sources = [
        root / "data/dataset.csv",
        Path(__file__),
        Path(__file__).with_name("figures.py"),
        Path(__file__).with_name("distributions.py"),
        Path(__file__).with_name("__main__.py"),
        Path(sar_results.__file__),
        project / "analysis/__init__.py",
        project / "analysis/pyproject.toml",
        project / "analysis/uv.lock",
    ]
    manifest = {
        "figure_id": spec["figure_id"],
        "status": spec["status"],
        "reproduction_command": "uv run --project analysis --locked python -m analysis.sar distributions "
        f"--result-dir {shlex.quote(os.path.relpath(root, project))} --out-dir /path/to/empty-output --style publication",
        "sources": [
            {"path": os.path.relpath(path, project), "sha256": file_sha256(path)}
            for path in sources
        ],
        "outputs": [{"path": path.name, "sha256": file_sha256(path)} for path in outputs],
        "caption": "figure-caption.md",
        "alt_text": "figure-alt-text.txt",
        "spec": "figure-spec.json",
        "visual_claim_ledger": "visual-claim-ledger.json",
        "layout_artifacts": [f"{name}-layout.json" for name in layouts],
    }
    (output / "submission_manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    return {
        "out_dir": str(output),
        "figures": 2,
        "formats": ["svg", "pdf", "png", "tiff"],
        "summary": summary,
    }
