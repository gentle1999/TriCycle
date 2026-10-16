---
name: reaction-db-filter
description: "Apply the database-authoritative reaction rxn-SMARTS predicate to a project."
---

# Filter Database Reactions

Requests require internal `project_id`, `cleaned_only = true`, and this fixed `reaction_smarts`:

```text
[#6,#7]=,:[#6,#7].[#6,#7]=,:[#6,#7]-,:[#6,#7]=,:[#6,#7]>>[#6,#7]1[#6,#7][#6,#7][#6,#7]=,:[#6,#7][#6,#7]1
```

TriCycle must not reconstruct relationships or reimplement rxn-SMARTS locally, nor pass `reaction_class`. The adapter forwards `reaction_smarts` unchanged to database actual-reaction search and returns counts with database provenance.

This read-only operation still requires an injected adapter. Dry-run only validates the project ID and filter constraints.

Implementation: `packages/tricycle-reaction-db/src/tricycle_reaction_db/plugin.py`.

Counts describe database results for this project/SMARTS, not ene/diene input-pair coverage. For changed RXN SMILES mapping/traversal, check structural equivalence under the [dataset adaptation contract](../../../docs/reaction-dataset-adaptation.md). String differences or fewer locally matched pairs do not establish missing database data. Use [reaction-db-export-rxnresid](../reaction-db-export-rxnresid/SKILL.md) for subsequent export/adaptation.
