from __future__ import annotations

import importlib.util
import json
import sys
import unittest
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any
from unittest.mock import patch

from tricycle_core import ExecutionContext, InvalidRequestError, TaskStatus
from tricycle_reactionopt import (
    ConformerSamplingConfig,
    LocalRitsPipelineSampler,
    Reaction,
    ReactionOptPlugin,
    ReactionProfileRequest,
    RitsInferenceConfig,
    RitsTransitionStateInput,
    RitsTransitionStateResult,
    molecule_to_xyz,
)

HAS_CHEMISTRY = all(importlib.util.find_spec(name) is not None for name in ("rdkit", "numpy"))
EXPLICIT_METHANE = "[C:1]([H:2])([H:3])([H:4])[H:5]"
EXPLICIT_METHANE_REACTION = f"{EXPLICIT_METHANE}>>{EXPLICIT_METHANE}"
EXPLICIT_CYCLOADDITION_REACTION = (
    "[C:1]([H:5])([H:6])=[C:2]([H:7])[H:8]."
    "[C:3]([H:9])([H:10])=[C:4]([H:11])[H:12]>>"
    "[C:1]1([H:5])([H:6])[C:2]([H:7])([H:8])"
    "[C:3]([H:9])([H:10])[C:4]1([H:11])[H:12]"
)


class ReactionOptProfileContractTests(unittest.TestCase):
    def test_rits_provenance_defaults_follow_manifest_for_both_input_paths(self) -> None:
        from tricycle_rits.assets import load_model_asset

        asset = replace(
            load_model_asset(), repository="https://example.invalid/rits", revision="1" * 40,
        )
        with patch("tricycle_reactionopt.rits.load_model_asset", return_value=asset):
            for config in (RitsInferenceConfig(), RitsInferenceConfig.from_mapping({})):
                self.assertEqual(config.source_repository, asset.repository)
                self.assertEqual(config.source_revision, asset.revision)
            explicit = RitsInferenceConfig.from_mapping(
                {"source_repository": "https://example.invalid/fork", "source_revision": "2" * 40}
            )
            self.assertEqual(explicit.source_repository, "https://example.invalid/fork")
            self.assertEqual(explicit.source_revision, "2" * 40)

    @unittest.skipUnless(
        HAS_CHEMISTRY, "RDKit and NumPy are optional in the base smoke environment"
    )
    def test_request_defaults_are_stable_and_rits_plan_is_explicit(self) -> None:
        request = ReactionProfileRequest.from_mapping(
            {
                "reaction_smiles": EXPLICIT_METHANE_REACTION,
                "sampling": {"target_conformers": 2, "force_field": "uff"},
                "rits": {"project_id": "project-1", "machine_type": "gpu"},
            }
        )
        assert request.reaction_id is not None
        self.assertTrue(request.reaction_id.startswith("reaction-"))
        self.assertEqual(request.sampling.target_conformers, 2)
        config = RitsInferenceConfig.from_mapping(request.rits)
        job = config.to_bohr_job(
            job_name="profile-ts-0000",
            output="outputs/ts.xyz",
            reaction_smiles=EXPLICIT_METHANE_REACTION,
        )
        self.assertEqual(job.operation, "rits.infer")
        self.assertEqual(job.project_id, "project-1")
        self.assertIn("scripts/run_ts_pipeline.py", job.command)
        self.assertIn("--reactions", job.command)
        self.assertIn("--generate", job.command)
        self.assertNotIn("--reactant_smi", job.command)
        self.assertNotIn("--product_smi", job.command)
        self.assertEqual(job.output_artifacts[0].kind, "directory")
        self.assertEqual(job.parameters["reaction_input_mode"], "mapped-smiles")
        self.assertEqual(job.parameters["reactionopt_ts_builder"], "rits")
        self.assertEqual(job.parameters["upstream_entrypoint"], "scripts/run_ts_pipeline.py")
        self.assertEqual(job.parameters["upstream_protocol"], "run_ts_pipeline")
        self.assertEqual(
            job.parameters["pipeline_reaction_smiles"],
            EXPLICIT_METHANE_REACTION,
        )

        reaction = Reaction.from_rxn_smiles(EXPLICIT_METHANE_REACTION)
        self.assertEqual(len(reaction.plan_rits_jobs(rits=config)), 1)

    @unittest.skipUnless(
        HAS_CHEMISTRY, "RDKit and NumPy are optional in the base smoke environment"
    )
    def test_profile_samples_both_sides_and_uses_rits_for_ts(self) -> None:
        import tricycle_reactionopt.conformers as conformers
        from rdkit import Chem
        from rdkit.Chem import rdDistGeom

        reaction = Reaction.from_rxn_smiles(EXPLICIT_CYCLOADDITION_REACTION)
        self.assertEqual(reaction.atom_map_numbers, tuple(range(1, 13)))
        self.assertEqual(reaction.forming_bonds, ((0, 3), (1, 2)))
        self.assertEqual(reaction.breaking_bonds, ((0, 1), (2, 3)))

        sampled_sides: list[str] = []

        def fake_sample(mol: Chem.Mol, config: ConformerSamplingConfig, *, side: str) -> Chem.Mol:
            del config
            sampled_sides.append(side)
            result = Chem.Mol(mol)
            result.RemoveAllConformers()
            status = rdDistGeom.EmbedMolecule(result, randomSeed=3407, useRandomCoords=True)
            self.assertGreaterEqual(status, 0)
            return result

        test_case = self

        sampler_requests: list[RitsTransitionStateInput] = []

        class FakeRitsSampler:
            def sample(self, request: RitsTransitionStateInput) -> RitsTransitionStateResult:
                test_case.assertEqual(request.__class__.__name__, "RitsTransitionStateInput")
                sampler_requests.append(request)
                ts = Chem.Mol(reaction.merged_reactants)
                status = rdDistGeom.EmbedMolecule(ts, randomSeed=3407, useRandomCoords=True)
                test_case.assertGreaterEqual(status, 0)
                return RitsTransitionStateResult((ts,), {"adapter": "fake"})

        with patch.object(conformers, "sample_conformer_ensemble", fake_sample):
            profile = reaction.build_profile(
                FakeRitsSampler(),
                sampling=ConformerSamplingConfig(target_conformers=1),
            )

        self.assertEqual(sampled_sides, ["reactants", "products"])
        self.assertEqual(len(sampler_requests), 1)
        self.assertEqual(sampler_requests[0].reactant_smiles, reaction.reactant_smiles)
        self.assertEqual(sampler_requests[0].product_smiles, reaction.product_smiles)
        self.assertFalse(hasattr(sampler_requests[0], "reactant"))
        self.assertEqual(len(profile.transition_states), 1)
        expected_atom_maps = tuple(range(1, 13))
        self.assertEqual(profile.atom_map_numbers, expected_atom_maps)
        for molecule in (
            profile.reactants,
            profile.products,
            *profile.transition_states,
        ):
            self.assertEqual(
                tuple(atom.GetAtomMapNum() for atom in molecule.GetAtoms()),
                expected_atom_maps,
            )
        self.assertEqual(profile.metadata["ts_sampling"], "rits.run_ts_pipeline")
        self.assertEqual(
            profile.transition_state_conformer_set.conformers[0].metadata["adapter"], "fake"
        )

        serialized = molecule_to_xyz(profile.transition_states[0], comment="rits")
        restored = RitsTransitionStateResult.from_xyz(
            serialized,
            reaction.merged_reactants,
        )
        self.assertEqual(len(restored.structures), 1)

        with TemporaryDirectory() as temp:
            written = profile.save(Path(temp), file_format="both")
            self.assertTrue(written)
            metadata = json.loads((Path(temp) / "profile.json").read_text(encoding="utf-8"))
            self.assertEqual(metadata["atom_map_numbers"], list(expected_atom_maps))
            self.assertEqual(metadata["atom_order"], "ascending-atom-map")
            self.assertEqual(metadata["transition_state_conformer_count"], 1)
            self.assertTrue((Path(temp) / "transition_states.sdf").is_file())
            self.assertTrue((Path(temp) / "transition_states.xyz").is_file())

    @unittest.skipUnless(
        HAS_CHEMISTRY, "RDKit and NumPy are optional in the base smoke environment"
    )
    def test_rits_branch_does_not_require_endpoint_sampling(self) -> None:
        from rdkit import Chem
        from rdkit.Chem import rdDistGeom

        reaction = Reaction.from_rxn_smiles(EXPLICIT_CYCLOADDITION_REACTION)
        test_case = self

        class FakeRitsSampler:
            def sample(self, request: RitsTransitionStateInput) -> RitsTransitionStateResult:
                test_case.assertEqual(request.reaction_smiles, reaction.reaction_smiles)
                ts = Chem.Mol(reaction.merged_reactants)
                status = rdDistGeom.EmbedMolecule(ts, randomSeed=3407, useRandomCoords=True)
                test_case.assertGreaterEqual(status, 0)
                return RitsTransitionStateResult((ts,))

        with (
            patch.object(
                reaction,
                "sample_reactant_conformers",
                side_effect=AssertionError("RitS must not depend on reactant conformers"),
            ),
            patch.object(
                reaction,
                "sample_product_conformers",
                side_effect=AssertionError("RitS must not depend on product conformers"),
            ),
        ):
            result = reaction.sample_transition_states(FakeRitsSampler())

        self.assertEqual(len(result.conformers), 1)

    @unittest.skipUnless(
        HAS_CHEMISTRY, "RDKit and NumPy are optional in the base smoke environment"
    )
    def test_sampler_requires_sample_method_and_typed_result(self) -> None:
        reaction = Reaction.from_rxn_smiles(EXPLICIT_METHANE_REACTION)

        def bare_callable(request: RitsTransitionStateInput) -> RitsTransitionStateResult:
            raise AssertionError("a bare callable must not be invoked")

        class InvalidResultSampler:
            def __init__(self, result: Any) -> None:
                self.result = result

            def sample(self, request: RitsTransitionStateInput) -> Any:
                return self.result

        invalid_sampler: Any = bare_callable
        with self.assertRaisesRegex(InvalidRequestError, "must implement sample"):
            reaction.sample_transition_states(invalid_sampler)
        for result in ([object()], (object(),), None):
            with self.subTest(result_type=type(result).__name__), self.assertRaisesRegex(
                InvalidRequestError, "must return RitsTransitionStateResult"
            ):
                reaction.sample_transition_states(InvalidResultSampler(result))

    @unittest.skipUnless(
        HAS_CHEMISTRY, "RDKit and NumPy are optional in the base smoke environment"
    )
    def test_local_pipeline_adapter_restores_original_atom_maps(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            script = root / "fake_pipeline.py"
            script.write_text(
                """import argparse
from pathlib import Path

parser = argparse.ArgumentParser()
parser.add_argument('--reactions')
parser.add_argument('--output')
args, _ = parser.parse_known_args()
output = Path(args.output) / 'selected_xyz'
output.mkdir(parents=True)
reaction = Path(args.reactions).read_text().strip()
reactants = reaction.split('>>', 1)[0]
from rdkit import Chem
params = Chem.SmilesParserParams()
params.removeHs = False
mol = Chem.MolFromSmiles(reactants, params)
lines = [str(mol.GetNumAtoms()), 'rxn_smiles=' + reaction]
for atom in mol.GetAtoms():
    lines.append(f'{atom.GetSymbol()} 0.0 0.0 0.0')
(output / 'rxn_0000__cluster_001__seed_000.xyz').write_text('\\n'.join(lines) + '\\n')
(Path(args.output) / 'summary.json').write_text('{"energy_method": "g-xTB"}')
""",
                encoding="utf-8",
            )
            config = RitsInferenceConfig.from_mapping(
                {
                    "input_directory": str(root),
                    "python_executable": sys.executable,
                    "checkpoint_path": "unused.ckpt",
                    "pipeline": {
                        "script": str(script),
                        "generate": False,
                        "max_atoms_per_batch": None,
                    },
                }
            )
            request = RitsTransitionStateInput(
                reaction_smiles="[C:10]([H:20])([H:30])([H:40])[H:50]>>"
                "[C:10]([H:20])([H:30])([H:40])[H:50]",
                config=config,
            )
            result = LocalRitsPipelineSampler(project_dir=root).sample(request)

        self.assertEqual(len(result.structures), 1)
        self.assertEqual(
            tuple(atom.GetAtomMapNum() for atom in result.structures[0].GetAtoms()),
            (10, 20, 30, 40, 50),
        )
        self.assertEqual(result.metadata["adapter"], "local-rits-pipeline")
        self.assertEqual(result.metadata["summary"]["energy_method"], "g-xTB")

    @unittest.skipUnless(
        HAS_CHEMISTRY, "RDKit and NumPy are optional in the base smoke environment"
    )
    def test_profile_rejects_incomplete_hydrogen_or_atom_mapping(self) -> None:
        with self.assertRaisesRegex(InvalidRequestError, "implicit hydrogens"):
            Reaction.from_rxn_smiles("CC>>CC")
        with self.assertRaisesRegex(InvalidRequestError, "stores hydrogen"):
            Reaction.from_rxn_smiles("[CH3:1]>>[CH3:1]")
        with self.assertRaisesRegex(InvalidRequestError, "atom-map numbers must match"):
            Reaction.from_rxn_smiles(
                "[C:1]([H:2])([H:3])([H:4])[H:5]>>[C:1]([H:2])([H:3])([H:4])[H:6]"
            )
        with self.assertRaisesRegex(InvalidRequestError, "unknown reactionopt.profile fields"):
            ReactionProfileRequest.from_mapping(
                {
                    "reaction_smiles": EXPLICIT_METHANE_REACTION,
                    "endpoint_pairing": "zip",
                }
            )

    def test_profile_plugin_dry_run_keeps_sampler_out_of_execution(self) -> None:
        if not HAS_CHEMISTRY:
            self.skipTest("RDKit and NumPy are optional in the base smoke environment")
        request = ReactionProfileRequest(
            reaction_smiles=EXPLICIT_METHANE_REACTION,
            sampling=ConformerSamplingConfig(target_conformers=1),
        )
        result = ReactionOptPlugin().execute_profile(
            request,
            object(),
            ExecutionContext(run_id="profile-dry-run", dry_run=True),
        )
        self.assertEqual(result.status, TaskStatus.PLANNED)


if __name__ == "__main__":
    unittest.main()
