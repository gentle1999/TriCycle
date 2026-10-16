"""Publication curves from a verified snapshot, with full-pool reference and first-hit costs."""
from __future__ import annotations

import argparse
import json
import statistics
from pathlib import Path
from typing import Any

from tricycle_sampling.protocols import LITERATURE_BASELINES

from .replay_sampling_panels import SPACES, read, sha, summarize, write_csv
from .threshold_cost import LABELS, compare

COLORS = {
    'predictive-uncertainty': '#7884B4',
    'latin-hypercube': '#A99A7B',
    'ucb': '#A881A4',
    'distance-uncertainty-product': '#C58964',
    'uncertainty-kriging-believer': '#6193A3',
    'intelligent': '#008775',
}
JOURNALS = {'Nature Communications': 'Nat. Commun.', 'Science': 'Science',
            'Nature': 'Nature', 'Journal of the American Chemical Society': 'JACS'}
INK = '#292D3E'


def full_pool_reference(points: list[dict], method: str, pool_pairs: int) -> list[dict]:
    """Require all five seeds to have actually acquired the declared complete pool."""
    rows = []
    for space in SPACES:
        local = [p for p in points if p['space'] == space and p['variant'] == method
                 and p['sampled_pairs'] == pool_pairs]
        if len(local) != 5 or {p['seed'] for p in local} != set(range(5)):
            raise ValueError(f'{space}: reference requires exactly five complete-pool endpoints')
        values = [p['mae'] for p in local]
        rows.append({'space': space, 'reference_method': method, 'pool_pairs': pool_pairs,
                     'n_seeds': 5, 'mean': statistics.mean(values), 'sd': statistics.stdev(values),
                     'evaluation_files': [p['evaluation_file'] for p in local]})
    return rows


def legend_entries() -> list[Any]:
    from matplotlib.lines import (  # pyright: ignore[reportMissingImports, reportMissingModuleSource]
        Line2D,
    )

    protocols = {p.strategy: p for p in LITERATURE_BASELINES}
    handles = []
    for method, label in LABELS.items():
        if method == 'ucb':
            label = 'Bayes-UCB'
        paper = protocols.get(method)
        source = f'{paper.authors}, {JOURNALS[paper.journal]} {paper.year}' if paper else 'This work'
        line = Line2D([], [], color=COLORS[method], linewidth=1.9 if not paper else 1.25,
                      label=f'{label}\n{source}')
        if paper:
            line.set_url(f'https://doi.org/{paper.doi}')
        handles.append(line)
    return handles


def draw_panel(ax: Any, space: str, summaries: list[dict], reference: dict) -> None:
    import numpy as np
    from matplotlib.ticker import (  # pyright: ignore[reportMissingImports, reportMissingModuleSource]
        FixedLocator,
        NullLocator,
        ScalarFormatter,
    )

    for method in LABELS:
        rows = [r for r in summaries if r['space'] == space and r['variant'] == method]
        x = np.array([r['cost']/1000 for r in rows])
        y = np.array([r['mean'] for r in rows])
        sd = np.array([r['sd'] if r['sd'] is not None else np.nan for r in rows])
        n = np.array([r['n_seeds'] for r in rows])
        color = COLORS[method]
        width = 1.65 if method == 'intelligent' else 1.05
        ax.step(x, np.where(n < 5, y, np.nan), where='post', color=color,
                linewidth=width, linestyle=':', alpha=.75, zorder=3)
        ax.step(x, np.where(n == 5, y, np.nan), where='post', color=color,
                linewidth=width, zorder=5 if method == 'intelligent' else 3)
        ax.fill_between(x, y-sd, y+sd, where=n >= 2, step='post', color=color,
                        alpha=.12 if method == 'intelligent' else .075, linewidth=0, zorder=1)
    floor = reference['mean']
    ax.axhspan(floor-reference['sd'], floor+reference['sd'], color=INK, alpha=.055, zorder=0)
    ax.axhline(floor, color=INK, linestyle=(0, (5, 3)), linewidth=.95, zorder=4)
    dpa = space == 'dpa-fined-rits-da'
    ax.text(.035, floor*(1.06 if dpa else .86), f'Empirical error floor: {floor:.3f}',
            transform=ax.get_yaxis_transform(), fontsize=7, va='bottom' if dpa else 'top', color=INK)
    if space == 'dpa-fined-rits-da':
        ax.axhline(2, color='#69707B', linestyle=(0, (1.5, 2.5)), linewidth=.8, zorder=2)
        ax.text(.985, 2.06, 'MAE = 2', transform=ax.get_yaxis_transform(), ha='right',
                va='bottom', fontsize=7, color='#55596A')
    ax.set_yscale('log')
    ax.set_ylim(.35, 9)
    ax.set_xlim(0, max(r['cost'] for r in summaries if r['space'] == space)/1000*1.025)
    ax.yaxis.set_major_locator(FixedLocator([.5, 1, 2, 4, 8]))
    ax.yaxis.set_major_formatter(ScalarFormatter())
    ax.yaxis.set_minor_locator(NullLocator())
    ticks = {'dpa-fined-rits-da': [0, 100, 200, 300, 400],
             'rits-zero-shot-da': [0, 50, 100, 150, 200, 250], 'autode': [0, 4, 8, 12, 16]}
    ax.set_xticks(ticks[space])
    ax.set_xlabel('Cumulative sampling cost (1,000 core-hours)', labelpad=5)
    ax.tick_params(direction='out', width=.65, length=3, pad=3)
    ax.spines[['top', 'right']].set_visible(False)
    ax.set_title(SPACES[space], loc='left', fontsize=9, pad=9)


def render(summaries: list[dict], floors: list[dict], cost_summaries: list[dict],
           comparison: dict, output: Path) -> None:
    import matplotlib  # pyright: ignore[reportMissingImports, reportMissingModuleSource]

    matplotlib.use('Agg')
    import matplotlib.pyplot as plt  # pyright: ignore[reportMissingImports, reportMissingModuleSource]

    intelligent = next(r for r in cost_summaries if r['space'] == 'dpa-fined-rits-da'
                       and r['variant'] == 'intelligent')
    baseline = next(r for r in cost_summaries if r['space'] == 'dpa-fined-rits-da'
                    and r['variant'] == comparison['best_complete_baseline'])
    callout = (f"{comparison['cost_saving_percent']:.1f}% lower cost at MAE ≤ 2\n"
               f"Intelligent: {intelligent['mean_cost']/1000:.1f} ± {intelligent['sd_cost']/1000:.1f}k\n"
               f"Predictive: {baseline['mean_cost']/1000:.1f} ± {baseline['sd_cost']/1000:.1f}k core-h\n"
               'First-passage mean ± SD; n = 5')
    style = {'font.family': 'sans-serif', 'font.sans-serif': ['Arial', 'Helvetica', 'DejaVu Sans'],
             'font.size': 7.5, 'axes.labelsize': 7.5, 'xtick.labelsize': 7, 'ytick.labelsize': 7,
             'text.color': INK, 'axes.labelcolor': INK, 'axes.edgecolor': INK,
             'xtick.color': INK, 'ytick.color': INK, 'axes.linewidth': .65,
             'svg.fonttype': 'none', 'pdf.fonttype': 42, 'legend.frameon': False}
    with plt.rc_context(style):
        fig, axes = plt.subplots(1, 3, figsize=(12, 4.5))
        fig.subplots_adjust(left=.065, right=.985, bottom=.18, top=.88, wspace=.18)
        for index, (ax, space, floor) in enumerate(zip(axes, SPACES, floors, strict=True)):
            draw_panel(ax, space, summaries, floor)
            ax.text(-.12, 1.065, chr(97+index), transform=ax.transAxes, fontsize=11, fontweight='bold')
        axes[0].set_ylabel(r'GT MAE (kcal mol$^{-1}$)', labelpad=7)
        axes[0].text(.985, .985, callout, transform=axes[0].transAxes, ha='right', va='top',
                     fontsize=6.8, linespacing=1.35,
                     bbox={'facecolor': 'white', 'edgecolor': 'none', 'alpha': .92, 'pad': 2})
        fig.text(.065, .956, 'Sampling efficiency across transition-state proxy spaces', fontsize=11)
        fig.text(.065, .073, 'Curves: mean ± SD; solid n = 5, dotted n < 5. '
                 'Dashed: full-pool LHS reference (n = 5).', fontsize=7)
        axes[2].legend(handles=legend_entries(), loc='upper left', bbox_to_anchor=(.035, .49),
                       fontsize=6.8, handlelength=2.5, labelspacing=.25,
                       borderaxespad=0, borderpad=0)
        fig.text(.065, .026, 'Literature methods adapted to the present reaction pool; '
                 'sampling costs exclude SAR training and agent overhead.', fontsize=6.5, color='#55596A')
        export(fig, output/'three-space-sampling')
        plt.close(fig)
        for space, floor in zip(SPACES, floors, strict=True):
            fig = plt.figure(figsize=(8, 3))
            ax = fig.add_axes((.085, .235, .515, .63))
            draw_panel(ax, space, summaries, floor)
            ax.set_ylabel(r'GT MAE (kcal mol$^{-1}$)', labelpad=5)
            fig.legend(handles=legend_entries(), loc='upper left', bbox_to_anchor=(.635, .91),
                       fontsize=6.6, handlelength=2.3, labelspacing=.65)
            if space == 'dpa-fined-rits-da':
                ax.text(.985, .985, callout, transform=ax.transAxes, ha='right', va='top',
                        fontsize=6.5, linespacing=1.25,
                        bbox={'facecolor': 'white', 'edgecolor': 'none', 'alpha': .92, 'pad': 2})
            fig.text(.085, .075, 'Mean ± SD; solid n = 5, dotted n < 5. Dashed: full-pool LHS reference (n = 5).', fontsize=6.6)
            fig.text(.085, .025, 'Literature methods adapted to this pool. Cost excludes SAR training and agent overhead.',
                     fontsize=6.2, color='#55596A')
            export(fig, output/space)
            plt.close(fig)


def export(fig: Any, stem: Path) -> None:
    for extension in ('png', 'svg', 'pdf', 'tiff'):
        options = {'pil_kwargs': {'compression': 'tiff_lzw'}} if extension == 'tiff' else {}
        fig.savefig(stem.with_suffix('.'+extension), dpi=600, facecolor='white', **options)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--snapshot', type=Path, required=True)
    parser.add_argument('--out-dir', type=Path, required=True)
    parser.add_argument('--pool-pairs', type=int, required=True)
    args = parser.parse_args()
    output = args.out_dir.resolve()
    output.mkdir(parents=True, exist_ok=False)
    snapshot = read(args.snapshot)
    floors = full_pool_reference(snapshot['points'], 'latin-hypercube', args.pool_pairs)
    # Pool totals differ by floating-point addition order only; normalize sub-micro-core-hour noise.
    plot_points = [{**p, 'cost': round(p['cost'], 6)} for p in snapshot['points']]
    summaries, _ = summarize(plot_points)
    hits, costs, comparisons = compare(snapshot['points'], [2.])
    dpa_comparison = next(c for c in comparisons if c['space'] == 'dpa-fined-rits-da')
    if dpa_comparison['cost_saving_percent'] is None:
        raise ValueError('DPA target comparison requires five reached seeds')
    for name, rows in [('empirical-floors', floors), ('curve-data', summaries),
                       ('first-hits-mae2', hits), ('first-hit-costs-mae2', costs)]:
        write_csv(output/f'{name}.csv', rows)
    protocols = [p.to_dict() for p in LITERATURE_BASELINES]
    (output/'method-references.json').write_text(json.dumps(protocols, indent=2)+'\n')
    (output/'method-references.txt').write_text('\n\n'.join(
        f"{p['strategy']}: {p['authors']} ({p['year']}). {p['title']}. {p['journal']}.\n"
        f"{p['url']}\nAdaptation: {p['adaptation']}" for p in protocols)
        + '\n\nIntelligent sampling: this work; no external method citation.\n')
    render(summaries, floors, costs, dpa_comparison, output)
    caption = (
        'Sampling efficiency across three transition-state proxy spaces. Each checkpoint is evaluated '
        'on the same 3,252 GT reaction paths. Arithmetic means and sample SD (ddof=1) are calculated '
        'on the original MAE scale and displayed on a logarithmic axis. At each observed cumulative '
        'cost, use the most recent checkpoint within each seed’s observed cost domain; no '
        'extrapolation or interpolation. Solid lines use all five seeds; dotted segments have fewer '
        'than five (the accompanying curve-data.csv gives exact counts). Bands require at least two seeds. '
        'Costs include initial sampling and count proxy TS CPU core-hours, excluding SAR training and '
        'agent overhead. Costs are rounded to 1e-6 core-hours only for curve alignment to remove '
        'floating-point summation noise; first-passage statistics use unrounded costs. '
        'Dashed lines are empirical error floors: mean endpoint GT MAE from five '
        f'Latin hypercube trajectories that acquired all {args.pool_pairs} pairs. '
        'The faint gray range is their sample SD. These references are not mathematical lower bounds '
        'or isolated proxy–GT computational errors; model error and coverage differences remain. '
        'Other strategies may lie below this reference. Literature labels cite the studies used '
        'for the registered adapted baselines, not claims of exact reproduction or original invention. '
        'In DPA fine-tuned RitS, the dotted horizontal line marks MAE=2 kcal/mol. The cost annotation '
        'compares the mean first observed checkpoint at MAE≤2 across five seeds against the best '
        'of five literature baselines by that measure; it is not the crossing of the mean curve. '
        'First crossing does not imply sustained attainment. Comparisons are descriptive, without '
        'a significance test. Lower GT barrier error supports energetic proxy fidelity under this '
        'protocol; it does not alone establish transition-state geometric or frequency validity.\n'
    )
    (output/'figure-caption.txt').write_text(caption)
    audit = {'snapshot_at': snapshot['snapshot_at'], 'snapshot_sha256': sha(args.snapshot),
             'script_sha256': sha(Path(__file__)), 'floors': floors, 'dpa_mae2': dpa_comparison,
             'canvas_inches': {'combined': [12, 4.5], 'individual': [8, 3]},
             'checkpoints': len(snapshot['points']), 'states': snapshot['statuses'],
             'curve_cost_rounding_core_hours': 1e-6, 'caption': caption}
    (output/'audit.json').write_text(json.dumps(audit, indent=2)+'\n')
    print(json.dumps({'output': str(output), 'floors': floors, 'dpa_mae2': dpa_comparison}, indent=2))


if __name__ == '__main__':
    main()
