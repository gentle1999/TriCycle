import pytest

from analysis.sampling.publication_curves import SPACES, full_pool_reference


def endpoints() -> list[dict]:
    return [dict(space=space, variant='latin-hypercube', seed=seed,
                 sampled_pairs=1600, mae=float(seed+1), evaluation_file=f'{space}/{seed}')
            for space in SPACES for seed in range(5)]


def test_reference_uses_only_complete_pool_and_sample_sd() -> None:
    points = endpoints()
    points.append({**points[0], 'sampled_pairs': 1560, 'mae': 100.})
    result = full_pool_reference(points, 'latin-hypercube', 1600)
    assert result[0]['mean'] == 3.
    assert result[0]['sd'] == pytest.approx(2.5**.5)


def test_partial_pool_or_duplicate_seed_cannot_supply_full_pool_reference() -> None:
    points = endpoints()
    points[0]['sampled_pairs'] = 1560
    with pytest.raises(ValueError, match='five complete-pool'):
        full_pool_reference(points, 'latin-hypercube', 1600)
    points = endpoints()
    points[0]['seed'] = 1
    with pytest.raises(ValueError, match='five complete-pool'):
        full_pool_reference(points, 'latin-hypercube', 1600)
