---
name: reactionopt-profile
description: "Build a mapped ReactionOpt profile from three independent reactant, product, and reaction-level RitS branches."
---

# ReactionOpt mapped profile

Use this capability when a mapped reaction SMILES must produce three independent branches:
reactant conformer sampling, product conformer sampling, and reaction-level RitS
TS pipeline. The pipeline generates candidates, applies stereo filtering, Kabsch RMSD clustering,
and g-xTB ranking before returning the final TS set. RitS consumes mapped reactant/product connectivity, so the profile
must not make its TS branch depend on, create, or consume the other two conformer branches; the
three outputs are aggregated only at the final profile boundary. Use the injected pipeline adapter, not raw
`sample_transition_state.py` output, as the final TS source.
The input must contain complete explicit hydrogens as separate mapped `[H:...]` atoms and a
unique positive atom map on every atom; the reactant and product sides must contain the same
mapped atom set and elements. The legacy ReactionOpt product-stretch backward builder is
intentionally disabled.

The canonical request and safety boundary are documented in
[`../../.agents/skills/reactionopt-profile/SKILL.md`](../../.agents/skills/reactionopt-profile/SKILL.md).
