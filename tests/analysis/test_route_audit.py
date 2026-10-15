from analysis.sampling.route_audit import ranked_ids


def test_counterfactual_ranking_uses_current_context_and_deterministic_ties() -> None:
    candidates = [
        {'candidate_id': 'b', 'payload': {'novelty': 2., 'hours': 4.}},
        {'candidate_id': 'a', 'payload': {'novelty': 1., 'hours': 1.}},
    ]
    expression = 'novelty / max(1, estimated_cost / context.scale)'
    assert ranked_ids(candidates, expression, 'hours', {'scale': 1}, 1) == {'a'}
    assert ranked_ids(candidates, expression, 'hours', {'scale': 4}, 1) == {'b'}
    assert ranked_ids(candidates, '1', 'hours', {}, 1) == {'a'}
