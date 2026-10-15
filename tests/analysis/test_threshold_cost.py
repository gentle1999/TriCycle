from analysis.sampling.threshold_cost import LABELS, SPACES, compare


def test_first_crossing_and_incomplete_seed_costs() -> None:
    points: list[dict] = []
    for space in SPACES:
        for method in LABELS:
            for seed in range(5):
                # A later rebound must not erase the observed first crossing;
                # a subsequent lower MAE must not replace its cost.
                values = [6., 2.9, 4., 2.]
                if method == 'intelligent' and seed == 4:
                    values = [6., 4., 4., 4.]
                points.extend({'space': space, 'variant': method, 'seed': seed,
                               'round_index': i, 'cost': 10.*(i+1), 'mae': value,
                               'evaluation_file': f'{space}/{method}/{seed}/{i}'}
                              for i, value in enumerate(values))
    hits, summaries, comparisons = compare(points, [3., 2.])
    row = next(h for h in hits if h['target_mae'] == 3. and h['variant'] != 'intelligent')
    assert row['first_cost'] == 20.
    assert row['first_round'] == 1
    incomplete = next(s for s in summaries if s['variant'] == 'intelligent')
    assert incomplete['n_reached'] == 4
    assert incomplete['mean_cost'] is None
    assert incomplete['sd_cost'] is None
    assert all(c['cost_saving_percent'] is None for c in comparisons)
    exact = next(h for h in hits if h['target_mae'] == 2. and h['variant'] != 'intelligent')
    assert exact['first_cost'] == 40.
