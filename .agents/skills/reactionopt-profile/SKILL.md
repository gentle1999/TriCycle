---
name: reactionopt-profile
description: "Build a mapped ReactionOpt profile from three independent reactant, product, and reaction-level RitS branches."
---

# Mapped ReactionOpt Profile

A fully atom-mapped reaction SMILES drives three independent branches: reactant conformers, product conformers, and a RitS pipeline that generates and selects TS conformers directly from mapped reactant/product connectivity. Merge only in the final profile. Do not invoke upstream ReactionOpt 4+2 product-stretch or product-backward strategies.

Use current fields: `rits.code_artifact`, `rits.checkpoint_artifact`, `rits.checkpoint_path`, and `rits.input_directory` bind source, weights, container weight path, and upload directory respectively. Old aliases are not supported.

## Inputs and outputs

- TOML requires `reaction_smiles`; optional fields are `reaction_id`, `[sampling]`, `[rits]`, and `output_dir`.
- Every hydrogen must be a separate `[H:<map>]` atom with a unique positive map. Heavy atoms cannot retain implicit hydrogens or hydrogen attributes such as `[CH3:...]`. Reactants and products must contain the same complete mapped atom set and elements.
- `sampling` applies independently to reactant/product branches. Multiple components may be merged within one branch for sampling; the two sides share neither embedding coordinates nor a required conformer count.
- `[rits.pipeline]` uses fork-added `scripts/run_ts_pipeline.py`, not an official RitS release entry point. It generates candidates, then applies stereo filtering, Kabsch RMSD clustering, and g-xTB energy selection. `code_artifact`/`input_directory` must identify a fork checkout containing that script.
- `source_repository`/`source_revision` default to official model/checkpoint provenance from the [RitS model configuration](../../../packages/tricycle-rits/src/tricycle_rits/assets/rits-model.toml), consistently for Python and TOML. Explicit values take precedence. Record the fork repository and commit separately; the official model revision is not the pipeline-script revision.
- `device` must be supported upstream, such as `cuda:0` or `cpu`, not `auto`.
- The RitS branch consumes mapped reaction connectivity, not reactant/product branch 3D structures. Invoke its pipeline once per reaction; `n_samples` controls raw candidates, while stereo/clustering/g-xTB stages determine final TS count.
- Outputs are `profile.json`, reactant/product conformer files, and `transition_states` SDF/XYZ. No `pre/ts/post` path is generated automatically. All three branches use the reaction's global atom-map order, also recorded in `profile.json`.

## Execution

Review the side-effect-free plan:

```bash
uv run --package tricycle-agent tricycle-agent plan \
  reactionopt.profile configs/example-reactionopt-profile-request.toml
```

Real construction requires an application-injected pipeline adapter implementing `RitsTransitionStateSampler`: `sample(request)` returns `RitsTransitionStateResult`. Plain callables and bare structure lists are rejected. `LocalRitsPipelineSampler` can call a local RitS checkout; cloud adapters can submit/download Bohr workers using the same `RitsInferenceConfig.build_pipeline_command()`. The component reads no credentials, accesses no network, and does not choose transport. Without an adapter dry-run planning works, but live execution fails explicitly.

## Recovery and boundaries

Dry-run does not optimize conformers, call RitS, or submit remotely. Each actual TS sample retains its RitS sample index and fixed source revision. On failure, inspect run state and profile outputs; repair the adapter/input and retry with a new run ID. Do not fall back to product-backward logic. RDKit embedding retries random-coordinate embedding; if no conformer remains, inspect the molecular graph/stereochemical constraints. Do not fabricate coordinates or remove stereochemistry to pass.

Implementation: `packages/tricycle-reactionopt/src/tricycle_reactionopt/reaction_profile.py`.
