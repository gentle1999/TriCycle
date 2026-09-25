from __future__ import annotations

import csv
import importlib.util
import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from tricycle_core import ExecutionContext, TaskStatus
from tricycle_reactionopt import (
    ConformerSamplingConfig,
    DielsAlderEnumerationRequest,
    DielsAlderSubstratePair,
    Reaction,
    ReactionOptPlugin,
    ReactionProfileRequest,
    RitsTransitionStateResult,
    enumerate_diels_alder_reactions,
)

HAS_CHEMISTRY = all(
    importlib.util.find_spec(name) is not None for name in ("rdkit", "numpy")
)


class DielsAlderFlippableAtomTests(unittest.TestCase):
    @unittest.skipUnless(HAS_CHEMISTRY, "RDKit is optional in the base smoke environment")
    def test_flippable_centers_are_unmarked_and_mapped(self) -> None:
        from rdkit import Chem

        cases = (
            ("O+", "C([O+](C)CC)=CC=C", "[#8,#16,#34;X3;v3;+1]"),
            ("S+", "C([S+](C)CC)=CC=C", "[#8,#16,#34;X3;v3;+1]"),
            ("Se+", "C([Se+](C)CC)=CC=C", "[#8,#16,#34;X3;v3;+1]"),
            ("N0", "C(N(C)CC)=CC=C", "[#7,#15,#33;X3;v3;+0]"),
            ("P0", "C(P(C)CC)=CC=C", "[#7,#15,#33;X3;v3;+0]"),
            ("P0-specified", "C([P@](C)CC)=CC=C", "[#7,#15,#33;X3;v3;+0]"),
            ("As0", "C([As](C)CC)=CC=C", "[#7,#15,#33;X3;v3;+0]"),
            ("F+2", "C([F+2](C)CC)=CC=C", "[#9,#17,#35,#53;X3;v3;+2]"),
            ("Cl+2", "C([Cl+2](C)CC)=CC=C", "[#9,#17,#35,#53;X3;v3;+2]"),
            ("Br+2", "C([Br+2](C)CC)=CC=C", "[#9,#17,#35,#53;X3;v3;+2]"),
            ("I+2", "C([I+2](C)CC)=CC=C", "[#9,#17,#35,#53;X3;v3;+2]"),
        )

        for name, diene_smiles, flippable_smarts in cases:
            with self.subTest(center=name):
                pair = DielsAlderSubstratePair(
                    ene_smiles="C=C",
                    diene_smiles=diene_smiles,
                )
                result = enumerate_diels_alder_reactions(
                    DielsAlderEnumerationRequest(pairs=(pair,), stereochemistry="all")
                )
                self.assertEqual(len(result.candidates), 2)
                self.assertEqual(
                    len({row["product_smiles"] for row in result.candidates}), 2
                )

                query = Chem.MolFromSmarts(flippable_smarts)
                self.assertIsNotNone(query)
                for row in result.candidates:
                    reaction = Reaction.from_rxn_smiles(row["mapped_reaction_smiles"])
                    self.assertIn(">>", reaction.reaction_smiles)
                    reactant_text, product_text = row["mapped_reaction_smiles"].split(">>")
                    reactants = Chem.MolFromSmiles(reactant_text)
                    products = Chem.MolFromSmiles(product_text)
                    reactant_maps = [atom.GetAtomMapNum() for atom in reactants.GetAtoms()]
                    product_maps = [atom.GetAtomMapNum() for atom in products.GetAtoms()]
                    self.assertEqual(len(reactant_maps), len(set(reactant_maps)))
                    self.assertEqual(sorted(reactant_maps), sorted(product_maps))

                    matches = products.GetSubstructMatches(query, uniquify=True)
                    self.assertTrue(matches)
                    for match in matches:
                        self.assertEqual(
                            products.GetAtomWithIdx(match[0]).GetChiralTag(),
                            Chem.ChiralType.CHI_UNSPECIFIED,
                        )

    @unittest.skipUnless(
        HAS_CHEMISTRY,
        "RDKit and NumPy are optional in the base smoke environment",
    )
    def test_complete_single_pair_space_builds_every_reaction_profile(self) -> None:
        from rdkit.Chem import AllChem

        pair = DielsAlderSubstratePair(
            ene_id="e-pentene-E",
            ene_smiles="C/C=C/CC",
            diene_id="1-methylbutadiene",
            diene_smiles="C(C)=CC=C",
        )
        sampled_reactions: list[str] = []

        class LocalTestSampler:
            def sample(self, request: object) -> RitsTransitionStateResult:
                mapped_reaction = request.reaction_smiles
                sampled_reactions.append(mapped_reaction)
                reaction = Reaction.from_rxn_smiles(mapped_reaction)
                structure = reaction.merged_reactants
                structure.RemoveAllConformers()
                status = AllChem.EmbedMolecule(structure, randomSeed=3407)
                if status < 0:
                    raise RuntimeError("could not embed the local TS test structure")
                return RitsTransitionStateResult(
                    (structure,), {"adapter": "local-test-double"}
                )

        sampling = ConformerSamplingConfig(
            target_conformers=1,
            max_conformers=1,
            max_iterations=60,
            rms_threshold=None,
            num_threads=1,
        )
        plugin = ReactionOptPlugin()
        with TemporaryDirectory(prefix="tricycle-da-profile-") as temporary_dir:
            root = Path(temporary_dir)
            enumeration_request = DielsAlderEnumerationRequest(
                pairs=(pair,),
                stereochemistry="reactant-constrained",
                output_csv=str(root / "mapped-reactions.csv"),
            )
            enumeration_result = plugin.execute_diels_alder_enumeration(
                enumeration_request,
                ExecutionContext(
                    run_id="da-profile-e2e",
                    workdir=root,
                    dry_run=False,
                ),
            )
            self.assertEqual(enumeration_result.status, TaskStatus.SUCCEEDED)
            self.assertTrue(enumeration_result.metadata["enumeration_complete"])
            self.assertEqual(enumeration_result.metadata["candidate_count"], 8)
            with (root / "mapped-reactions.csv").open(encoding="utf-8", newline="") as handle:
                candidates = list(csv.DictReader(handle))
            self.assertEqual(len(candidates), 8)
            reaction_smiles = {row["mapped_reaction_smiles"] for row in candidates}
            self.assertEqual(len(reaction_smiles), len(candidates))

            for candidate in candidates:
                profile_dir = root / candidate["reaction_id"]
                request = ReactionProfileRequest(
                    reaction_id=candidate["reaction_id"],
                    reaction_smiles=candidate["mapped_reaction_smiles"],
                    sampling=sampling,
                    output_dir=str(profile_dir),
                )
                result = plugin.execute_profile(
                    request,
                    LocalTestSampler(),
                    ExecutionContext(
                        run_id="da-profile-e2e",
                        workdir=root,
                        dry_run=False,
                    ),
                )
                self.assertEqual(result.status, TaskStatus.SUCCEEDED)
                self.assertGreaterEqual(result.metadata["reactant_conformer_count"], 1)
                self.assertGreaterEqual(result.metadata["product_conformer_count"], 1)
                self.assertEqual(result.metadata["transition_state_conformer_count"], 1)
                self.assertEqual(
                    set(path.name for path in profile_dir.iterdir()),
                    {
                        "reactants.sdf",
                        "reactants.xyz",
                        "products.sdf",
                        "products.xyz",
                        "transition_states.sdf",
                        "transition_states.xyz",
                        "profile.json",
                    },
                )
                profile = json.loads((profile_dir / "profile.json").read_text())
                self.assertEqual(profile["atom_order"], "ascending-atom-map")
                self.assertEqual(profile["transition_state_conformer_count"], 1)

        self.assertEqual(set(sampled_reactions), reaction_smiles)
