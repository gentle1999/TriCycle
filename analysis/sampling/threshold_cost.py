"""Descriptive first-passage cost from verified, frozen sampling checkpoints."""
from __future__ import annotations

import argparse
import json
import math
import statistics
from pathlib import Path

from .replay_sampling_panels import SPACES, freeze, sha, write_csv

LABELS = {
    "predictive-uncertainty": "Predictive uncertainty",
    "latin-hypercube": "Latin hypercube",
    "ucb": "UCB",
    "distance-uncertainty-product": "Distance × uncertainty",
    "uncertainty-kriging-believer": "Uncertainty + KB",
    "intelligent": "Intelligent sampling",
}


def compare(points: list[dict], thresholds: list[float]) -> tuple[list[dict], list[dict], list[dict]]:
    hits: list[dict] = []
    summaries: list[dict] = []
    comparisons: list[dict] = []
    for space in SPACES:
        for method in LABELS:
            for target in thresholds:
                costs = []
                for seed in range(5):
                    track = sorted((p for p in points if p['space'] == space
                                    and p['variant'] == method and p['seed'] == seed),
                                   key=lambda p: p['round_index'])
                    if not track or [p['round_index'] for p in track] != list(range(len(track))):
                        raise ValueError('Missing or noncontiguous trajectory')
                    if any(not math.isfinite(p[k]) or p[k] < 0 for p in track for k in ('mae', 'cost')):
                        raise ValueError('Invalid checkpoint')
                    hit = next((p for p in track if p['mae'] <= target), None)
                    if hit is not None:
                        costs.append(hit['cost'])
                    hits.append({'space': space, 'variant': method, 'seed': seed, 'target_mae': target,
                                 'reached': hit is not None, 'first_cost': hit['cost'] if hit else None,
                                 'first_round': hit['round_index'] if hit else None,
                                 'last_observed_cost': track[-1]['cost'],
                                 'evaluation_file': hit['evaluation_file'] if hit else None})
                summaries.append({'space': space, 'variant': method, 'target_mae': target,
                                  'n_reached': len(costs), 'n_seeds': 5,
                                  'mean_cost': statistics.mean(costs) if len(costs) == 5 else None,
                                  'sd_cost': statistics.stdev(costs) if len(costs) == 5 else None})
        for target in thresholds:
            group = [r for r in summaries if r['space'] == space and r['target_mae'] == target]
            intelligent = next(r for r in group if r['variant'] == 'intelligent')
            baselines = [r for r in group if r['variant'] != 'intelligent' and r['n_reached'] == 5]
            best = min(baselines, key=lambda r: r['mean_cost']) if baselines else None
            comparable = intelligent['n_reached'] == 5 and best is not None
            deltas = []
            cost_saving = None
            if intelligent['n_reached'] == 5 and best is not None:
                cost_saving = 100 * (1 - intelligent['mean_cost'] / best['mean_cost'])
                for seed in range(5):
                    own = next(h['first_cost'] for h in hits if h['space'] == space
                               and h['variant'] == 'intelligent' and h['seed'] == seed and h['target_mae'] == target)
                    ref = next(h['first_cost'] for h in hits if h['space'] == space
                               and h['variant'] == best['variant'] and h['seed'] == seed and h['target_mae'] == target)
                    deltas.append(own - ref)
            comparisons.append({'space': space, 'target_mae': target,
                                'intelligent_n_reached': intelligent['n_reached'],
                                'intelligent_mean_cost': intelligent['mean_cost'],
                                'best_complete_baseline': best['variant'] if best else None,
                                'baseline_mean_cost': best['mean_cost'] if best else None,
                                'complete_baselines': len(baselines),
                                'cost_saving_percent': cost_saving,
                                'paired_seed_wins': sum(d < 0 for d in deltas) if comparable else None,
                                'paired_cost_deltas': deltas})
    return hits, summaries, comparisons


def render(summaries: list[dict], targets: list[float], output: Path) -> None:
    import matplotlib  # pyright: ignore[reportMissingImports, reportMissingModuleSource]

    matplotlib.use('Agg')
    import matplotlib.pyplot as plt  # pyright: ignore[reportMissingImports, reportMissingModuleSource]
    from matplotlib.patches import (  # pyright: ignore[reportMissingImports, reportMissingModuleSource]
        Rectangle,
    )

    with plt.rc_context({'font.family': 'sans-serif', 'font.sans-serif': ['Arial', 'Helvetica', 'DejaVu Sans'],
                         'font.size': 7.5, 'text.color': '#292D3E', 'svg.fonttype': 'none', 'pdf.fonttype': 42}):
        fig, axes = plt.subplots(1, 3, figsize=(12, 4.5))
        fig.subplots_adjust(left=.155, right=.985, bottom=.23, top=.79, wspace=.12)
        fig.text(.155, .93, 'Sampling cost to first reach target MAE', fontsize=11)
        fig.text(.155, .875, 'Mean ± SD across five matched seeds · costs in 1,000 core-hours', fontsize=8)
        for ax, (space, title) in zip(axes, SPACES.items(), strict=True):
            ax.set_title(title, loc='left', fontsize=9, pad=12)
            for x, target in enumerate(targets):
                group = [s for s in summaries if s['space'] == space and s['target_mae'] == target]
                full = [s['mean_cost'] for s in group if s['n_reached'] == 5]
                lowest = min(full) if full else None
                for y, method in enumerate(LABELS):
                    row = next(s for s in group if s['variant'] == method)
                    best = row['n_reached'] == 5 and row['mean_cost'] == lowest
                    color = '#D6EAE3' if best else '#EEF0F7' if row['n_reached'] == 5 else '#F5F5F5'
                    ax.add_patch(Rectangle((x-.5, y-.5), 1, 1, facecolor=color,
                                           edgecolor='white', linewidth=2))
                    label = (f"{row['mean_cost']/1000:.2f}\n± {row['sd_cost']/1000:.2f}"
                             if row['n_reached'] == 5 else f"{row['n_reached']}/5 reached")
                    ax.text(x, y, label, ha='center', va='center', fontsize=7,
                            fontweight='bold' if best else 'normal', color='#292D3E')
            ax.set_xlim(-.5, len(targets)-.5)
            ax.set_ylim(len(LABELS)-.5, -.5)
            ax.set_xticks(range(len(targets)), [f'≤ {t:g}' for t in targets], fontsize=8)
            ax.set_xlabel(r'Target GT MAE (kcal mol$^{-1}$)', fontsize=8, labelpad=8)
            ax.set_yticks(range(len(LABELS)), list(LABELS.values()) if ax is axes[0] else ['']*len(LABELS))
            ax.tick_params(length=0)
            for spine in ax.spines.values():
                spine.set_visible(False)
        fig.text(.155, .11, 'Green: lowest mean among methods reaching the target in all five seeds.', fontsize=7.5)
        fig.text(.155, .06, 'Incomplete cells show reach counts only. First crossing does not imply sustained error below target.', fontsize=7.5)
        for extension in ('png', 'svg', 'pdf'):
            fig.savefig(output / f'threshold-cost.{extension}', dpi=400, facecolor='white')
        plt.close(fig)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run-dir', type=Path, required=True)
    parser.add_argument('--out-dir', type=Path, required=True)
    args = parser.parse_args()
    output = args.out_dir.resolve()
    if output.exists():
        raise FileExistsError(output)
    snapshot = freeze(args.run_dir.resolve(), output / 'snapshot')
    targets = [5., 4., 3., 2., 1.]
    hits, summaries, comparisons = compare(snapshot['points'], targets)
    for name, rows in [('first-hits', hits), ('cost-summary', summaries), ('intelligent-comparison', comparisons)]:
        write_csv(output / f'{name}.csv', rows)
    render(summaries, targets, output)
    audit = {'snapshot_at': snapshot['snapshot_at'], 'thresholds': targets, 'comparisons': comparisons,
             'contract': 'First observed MAE <= threshold; initialization included; no interpolation. '
                         'Five-seed means only; no imputation for unreached seeds; sample SD ddof=1. '
                         'Costs are proxy TS core-hours, exclude SAR/agent overhead. Descriptive, no significance test.',
             'script_sha256': sha(Path(__file__)), 'canvas_inches': [12, 4.5], 'snapshot_sha256': sha(output/'snapshot/snapshot.json')}
    (output/'audit.json').write_text(json.dumps(audit, indent=2)+'\n')
    print(json.dumps(audit, indent=2))


if __name__ == '__main__':
    main()
