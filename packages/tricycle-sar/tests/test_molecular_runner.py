from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from tricycle_core.io import file_sha256
from tricycle_core.training_protocol import verify_asset_files

from tricycle_sar.modelling_benchmark import validate_models
from tricycle_sar.molecular_runner import batches, canonical_parts


class MolecularRunnerTests(unittest.TestCase):
    def test_extensions_are_separate_from_existing_models(self) -> None:
        self.assertEqual(
            validate_models(["chemprop_cgr", "chemprop_multicomponent", "uam", "molclr"]),
            ["chemprop_cgr", "chemprop_multicomponent", "uam", "molclr"],
        )

    def test_assets_are_required_and_tampering_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            file = root / "weights.pt"
            entry = {"path": "weights.pt", "sha256": "0" * 64}
            self.assertEqual(verify_asset_files(root, [entry]), [str(file)])
            file.write_bytes(b"fixture")
            entry["sha256"] = file_sha256(file)
            self.assertEqual(verify_asset_files(root, [entry]), [])
            file.write_bytes(b"changed")
            with self.assertRaisesRegex(ValueError, "SHA256"):
                verify_asset_files(root, [entry])
            with self.assertRaisesRegex(ValueError, "unsafe"):
                verify_asset_files(root, [{"path": "../escape", "sha256": "0" * 64}])

    def test_batches_preserve_rows_and_avoid_batchnorm_singletons(self) -> None:
        for n in [2, 8, 9, 17]:
            result = batches(list(range(n)), 8)
            self.assertEqual([i for batch in result for i in batch], list(range(n)))
            self.assertTrue(all(len(batch) >= 2 for batch in result))
        with self.assertRaises(ValueError):
            batches([0], 8)

    def test_structure_normalization_does_not_use_atom_mapping_or_component_order(self) -> None:
        self.assertEqual(
            canonical_parts("[CH2:1]=[CH2:2].O>>CCO"), canonical_parts("O.[CH2:15]=[CH2:27]>>CCO")
        )
        self.assertNotEqual(
            canonical_parts("CC(O)F>>C[C@H](O)F"), canonical_parts("CC(O)F>>C[C@@H](O)F")
        )
        with self.assertRaises(ValueError):
            canonical_parts("CCO")
