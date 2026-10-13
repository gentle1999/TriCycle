from typing import Any
from unittest.mock import patch

import pytest
from tricycle_core.errors import InvalidRequestError
from tricycle_reactionopt.conformers import _embed
from tricycle_reactionopt.models import ConformerSamplingConfig


def test_empty_embedding_retries_random_coordinates() -> None:
    Chem = pytest.importorskip("rdkit.Chem")
    AllChem = pytest.importorskip("rdkit.Chem.AllChem")
    molecule = Chem.AddHs(Chem.MolFromSmiles("CCO"))
    original = AllChem.EmbedMultipleConfs
    calls = []

    def embed(mol: Any, *, numConfs: int, params: Any) -> Any:
        calls.append(params.useRandomCoords)
        if len(calls) == 1:
            return []
        return original(mol, numConfs=numConfs, params=params)

    with patch.object(AllChem, "EmbedMultipleConfs", embed):
        result = _embed(molecule, ConformerSamplingConfig(target_conformers=2))
    assert calls == [False, True]
    assert result.GetNumConformers() == 2


def test_failed_embedding_never_manufactures_linear_coordinates() -> None:
    Chem = pytest.importorskip("rdkit.Chem")
    AllChem = pytest.importorskip("rdkit.Chem.AllChem")
    molecule = Chem.AddHs(Chem.MolFromSmiles("C1CCCCC1"))
    with (
        patch.object(AllChem, "EmbedMultipleConfs", return_value=[]) as embed,
        pytest.raises(InvalidRequestError, match="could not embed"),
    ):
        _embed(molecule, ConformerSamplingConfig(target_conformers=2))
    assert embed.call_count == 2
