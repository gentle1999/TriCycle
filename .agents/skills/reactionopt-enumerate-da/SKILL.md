---
name: reactionopt-enumerate-da
description: "Expand ene/diene substrate pairs into atom-mapped Diels–Alder reaction candidates and save a local CSV."
---

# Enumerate Diels–Alder Reactions from Ene/Diene Pairs

Use substrate pairs to generate mapped reaction SMILES candidates for later conformer generation or computation preparation.

## Inputs and outputs

- TOML contains one or more `[[pairs]]` with `ene_smiles`, `diene_smiles`, and optional stable `ene_id`/`diene_id`.
- Output is `mapped-reactions.csv` in the run directory, one fully atom-mapped candidate per row.
- By default, preserve specified substrate E/Z relationships and enumerate unspecified non-invertible product stereocenters.
- Invertible atoms (`[#8,#16,#34;X3;v3;+1]`, `[#7,#15,#33;X3;v3;+0]`, `[#9,#17,#35,#53;X3;v3;+2]`) receive no product stereotags and do not define separately enumerated stereoisomers.
- `stereochemistry = "all"` disables filtering by substrate E/Z relationships at reaction sites.
- No Bohr connection or calculation creation/submission occurs.

```bash
uv run --package tricycle-agent tricycle-agent run \
  reactionopt.enumerate-da configs/example-reactionopt-da-enumerate-request.toml \
  --run-id da-candidates
```

`metadata.candidate_count` records the count; `metadata.enumeration_complete` reports whether a count limit truncated enumeration. Optional `max_products_per_pair` limits RDKit reaction-match products; `max_isomers_per_product` limits isomers. Truncated CSV rows have `enumeration_complete = false`. Enumeration grows quickly with stereocenter count.

```toml
stereochemistry = "reactant-constrained"

[[pairs]]
ene_id = "ene-001"
ene_smiles = "C=C"
diene_id = "diene-001"
diene_smiles = "C=CC=C"
```

Optional `output_csv` specifies the CSV path and `output_dir` the directory; otherwise output goes to the run directory. Relative paths resolve against `--workdir`.

## Scope

The built-in template supports neutral C/N enes and neutral C/N conjugated dienes. Substructure matching enumerates all mapped connectivities and unspecified non-invertible product stereochemistry. It does not filter by endo/exo preference, catalyst, solvent, or reaction conditions. Limited enumeration is explicitly marked incomplete.
