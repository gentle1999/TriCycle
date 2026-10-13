"""Opt-in MAE decisions: scientific freedom, selected-label feedback and review.

This module only reads the acquisition directory. Evaluation-only data and
unselected historical labels/costs are never inputs.
"""
from __future__ import annotations

import ast
import math
from collections import defaultdict
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from tricycle_core.io import file_sha256 as digest
from tricycle_core.io import write_json as save
from tricycle_core.validation import validate_fields

from tricycle_sampling.protocols import LITERATURE_BASELINE_STRATEGIES
from tricycle_sampling.replay import core_hours as replay_core_hours
from tricycle_sampling.replay import cost_guidance as replay_cost_guidance
from tricycle_sampling.replay.worker import metrics, pair

NAME = "autonomous-mae-v1"
SCHEMA = "tricycle.agent-acquisition/v2"
OBJECTIVE = "minimize-generalization-mae"
FIELDS = ("expression", "cost_expression", "rationale", "author_notes",
          "decision_action", "hypothesis", "expected_effect")


def settings(config: dict[str, Any]) -> dict[str, Any] | None:
    if "decision_protocol" not in config:
        return None
    value = config["decision_protocol"]
    if not isinstance(value, Mapping):
        raise ValueError("decision_protocol must be a table")
    if value.get("name") not in (NAME, replay_core_hours.NAME) or config.get("acquisition_policy"):
        raise ValueError("MAE protocol requires an autonomous protocol and no fixed cost policy")
    allowed = {"name", "feedback", "review", "objective"}
    if value["name"] == replay_core_hours.NAME:
        allowed.update({"evaluation_budgets_core_hours", "guidance"})
    validate_fields(value, allowed, context="decision_protocol")
    objective = replay_core_hours.objective(dict(value))
    if "objective" in value and value["objective"] != objective:
        raise ValueError("decision_protocol.objective differs from the selected protocol")
    strategy = config.get("sampling_strategy", "intelligent")
    if strategy != "intelligent" and (
        strategy not in LITERATURE_BASELINE_STRATEGIES
        or value.get("feedback") or value.get("review") or value.get("guidance")
    ):
        raise ValueError("fixed literature strategies require shared evaluation without agent feedback/review")
    for key in ("feedback", "review"):
        if not isinstance(value.get(key, False), bool):
            raise ValueError(f"decision_protocol.{key} must be a boolean")
    if value.get("review") and not value.get("feedback"):
        raise ValueError("batch review requires feedback")
    result: dict[str, Any] = {"name": value["name"], "feedback": value.get("feedback", False),
              "review": value.get("review", False), "objective": objective}
    if value["name"] == replay_core_hours.NAME:
        result["evaluation_budgets_core_hours"] = replay_core_hours.budgets(value.get("evaluation_budgets_core_hours"))
    if value.get("guidance") is not None:
        if (value["guidance"] != replay_cost_guidance.NAME or value["name"] != replay_core_hours.NAME
                or not result["feedback"] or not result["review"]):
            raise ValueError("coverage/cost guidance requires core-hour MAE, feedback and batch review")
        result["guidance"] = replay_cost_guidance.NAME
    return result


def compile_response(value: dict[str, Any], task: dict[str, Any],
                     previous: dict[str, Any] | None) -> dict[str, Any]:
    for key in FIELDS:
        if not isinstance(value.get(key), str) or not value[key].strip():
            raise ValueError(f"MAE response requires non-empty {key}")
    if value["decision_action"] not in {"retain", "adjust", "replace"}:
        raise ValueError("decision_action must be retain, adjust or replace")
    if value["decision_action"] == "retain":
        if previous is None:
            raise ValueError("cannot retain a function before the first decision")
        for key in ("expression", "cost_expression"):
            if value[key] != previous[key]:
                raise ValueError(f"retain must preserve the previous {key} exactly")
    objective = replay_core_hours.objective(task["decision_protocol"])
    if objective == replay_core_hours.OBJECTIVE and task.get("objective") != objective:
        raise ValueError("core-hour task objective is missing or inconsistent")
    if objective == replay_core_hours.OBJECTIVE:
        try:
            tree = ast.parse(value['cost_expression'], mode='eval')
        except SyntaxError as exc:
            raise ValueError('invalid core-hour cost_expression') from exc
        names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
        names.update(n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute))
        if not names & {'estimated_core_hours', 'core_hours_per_proxy_unit'}:
            raise ValueError("core-hour cost_expression must use calibrated core-hour units")
    return {**value, "objective": objective, "acquisition_schema": SCHEMA,
            "decision_protocol": task["decision_protocol"]}


def prompt(brief: dict[str, Any], *, review: dict[str, Any] | None = None) -> str:
    import json

    instruction = (
        "Choose a deterministic acquisition rule to MINIMIZE GENERALIZATION MAE at the supplied "
        "sample budget. You have full scientific freedom: select or omit any available feature; "
        "no novelty, uncertainty, structural, global-metric or cost term is mandatory. "
        "Cost is recorded for audit, NOT a secondary optimization target or a mandatory penalty. "
        "You may retain a justified previous formula EXACTLY, adjust it, or replace it. "
        "Do not change a working rule just to produce a new formula. Simple rules are acceptable "
        "when supported by evidence. Avoid inventing chemical priors or mistaking feature presence "
        "for an actual ranking effect. The controller has checked the entire pool and will apply "
        "your rule to ALL remaining candidates with stable ID tie breaking. "
        "Safe numeric DSL: arithmetic, comparisons, conditional expressions, abs exp log log1p "
        "min max sqrt; numeric payload fields, candidate, context and estimated_cost only. "
        "No undefined names, assignments, imports or executable code. Scores must be finite and "
        "discriminating when a choice exists. cost_expression must be positive; a structural proxy "
        "product_count * mean_product_atom_count is available for reporting and need not enter score. "
        "Auxiliary validation is not main-model validation; small fixed panels cannot establish "
        "global accuracy or causality. Pre-update surprise measures selected-label difficulty, "
        "not evidence of a generalization gain. Never use final evaluation or unselected true cost. "
        "State a falsifiable hypothesis and expected regional effect, justify adopted and omitted "
        "signals with sample support, and describe uncertainty. decision_action is retain/adjust/replace; "
        "retain copies the immediately preceding expression and cost_expression exactly. "
        "rationale: concise Chinese, <=180 characters; author_notes: <=1200 Chinese characters. "
        "Return schema JSON directly. No tools, file access, logs, hashes or table reproduction.\n"
    )
    if brief.get("decision_protocol", {}).get("name") == replay_core_hours.NAME:
        if brief.get("objective") != replay_core_hours.OBJECTIVE or not brief.get("core_hour_budget"):
            raise ValueError("core-hour decision brief lacks its objective or resource evidence")
        instruction = instruction.replace(
            "Choose a deterministic acquisition rule to MINIMIZE GENERALIZATION MAE at the supplied "
            "sample budget.",
            "Choose a deterministic acquisition rule to MINIMIZE GROUND-TRUTH MAE at the supplied "
            "CUMULATIVE PROXY CORE-HOUR BUDGETS. Ground-truth labels and metrics are unavailable to you. "
            "The batch pair count is an execution limit, not the optimization budget.",
        ).replace(
            "Cost is recorded for audit, NOT a secondary optimization target or a mandatory penalty.",
            "Core-hours are the resource constraint for the MAE objective. Use the supplied realized "
            "expenditure, remaining milestone budget and revealed-only cost calibration to reason "
            "about achievable accuracy. You choose how estimated cost affects ranking; the controller "
            "does not divide scores by cost or prescribe weights.",
        ).replace(
            "cost_expression must be positive; a structural proxy "
            "product_count * mean_product_atom_count is available for reporting and need not enter score.",
            "cost_expression must estimate positive CORE-HOURS. candidate.estimated_core_hours is "
            "calibrated from revealed timings and structural workload; document any alternative estimate. "
            "Raw product_count * mean_product_atom_count alone has proxy units, not core-hours.",
        )
        instruction += (
            "\nInspect novelty, coverage, uncertainty and cost evidence, and justify omitted signals "
            "in terms of accuracy per core-hour; no feature is mandatory in the formula. "
            "Coarse scores can leave the actual batch determined by ID tie breaking. Inspect supplied "
            "tie and batch-cost diagnostics. These do not impose diversity or a particular score. "
            "Retain may be appropriate, but absence of proof that a rule failed is not evidence that it "
            "is resource-efficient. A tiny proxy panel does not establish ground-truth accuracy. "
            "Milestones are evaluation budgets; unknown true candidate costs cannot guarantee a hard "
            "spending cap. Over-budget checkpoints do not count at an earlier milestone.\n"
        )
    if brief.get("decision_protocol", {}).get("guidance") == replay_cost_guidance.NAME:
        if not brief.get("acquisition_guidance"):
            raise ValueError("guided decision brief lacks its coverage/cost reference")
        instruction += (
            "\nUse COVERAGE AND COST as your default scientific starting point: prioritize gaps in the "
            "sampled distribution and sparse pool regions while penalizing above-median structural "
            "workload. The supplied reference uses novelty/sparsity weights 0.5/0.5 and cost exponent 1. "
            "At the first decision, prefer the supplied reference expression unless the current evidence "
            "supports a specific adjustment. At later decisions, retain a supported rule or adjust its "
            "weights, cost sensitivity, or regional/uncertainty terms using revealed-label feedback. "
            "The aim of the adaptive strategy is lower MAE than ALL five static baselines at "
            "matched core-hour budgets; their evaluation outcomes are unavailable during acquisition. "
            "Initialization is not proof of optimality. Avoid defaulting to "
            "uncertainty-only ranking or abandoning coverage/cost merely to invent a new formula. "
            "Explain departures from the reference and previous rule, with sample support and an "
            "expected accuracy-per-core-hour effect. The reference is advisory; the controller does "
            "not overwrite your ranking or impose the reference weights. Inspect the reference batch "
            "and proposal comparison: if your batch costs more or covers less, justify the tradeoff. "
            "No ground-truth outcomes, unselected labels or measured future costs are supplied.\n"
        )
    material = {k: v for k, v in brief.items() if k not in (
        "evidence_hashes", "task_sha256", "structure_provenance", "compiler_sha256")}
    if review is not None:
        instruction += (
            "This is ONE bounded scientific review before any selection. Inspect the proposed "
            "batch diagnostics below. You may approve the proposal unchanged or revise it. "
            "No diversity weight or change is compulsory. High similarity may be justified for "
            "a specific error region. decision_action is relative to the previous ROUND, not the "
            "proposal. Do not claim the unexecuted batch improved accuracy.\n"
        )
        material["proposal_review"] = review
    return instruction + json.dumps(material, ensure_ascii=False, separators=(",", ":"))


def region(row: dict[str, Any]) -> str:
    return "product_rings<=2" if float(row["product_ring_count"]) <= 2 else "product_rings>2"


def aggregate(entries: list[dict[str, Any]]) -> dict[str, Any]:
    n = sum(int(m["n"]) for m in entries)
    if not n:
        return {"n_pairs": 0, "n_paths": 0}
    return {"n_pairs": len(entries), "n_paths": n,
            "mae": sum(float(m["mae"])*int(m["n"]) for m in entries)/n,
            "rmse": math.sqrt(sum(float(m["rmse"])**2*int(m["n"]) for m in entries)/n),
            "bias": sum(float(m["bias"])*int(m["n"]) for m in entries)/n}


def feedback(current: Path, candidates: list[dict[str, Any]],
             history: list[dict[str, Any]], groups: dict[str, Any]) -> dict[str, Any]:
    """Only revealed-label residuals and the fixed auxiliary panel enter feedback."""
    from tricycle_sampling.replay.campaign import read

    model = read(current / "result/model-metrics.json")
    prior = history[-1] if history else None
    comparison: dict[str, Any] = {"comparable": False}
    if prior:
        old = prior["validation"]
        if set(old["validation_pairs"]) == set(model["validation_pairs"]):
            comparison = {
                "comparable": True, "n_pairs": len(model["validation_pairs"]),
                "mae_delta": model["mae"]-old["mae"],
                "rmse_delta": model["rmse"]-old["rmse"],
                "scope": "fixed-panel auxiliary models; descriptive, not causal",
            }
    prequential = read(current / "result/pre-update-metrics.json")
    selected_feedback: dict[str, Any] = {}
    if prior:
        previous_dir = current.parent / f"round-{prior['round']-1:04d}" / 'decision'
        pool = read(previous_dir / 'pair-candidates.json')['candidates']
        selected_ids = set(prior['selection']['candidate_ids'])
        selected = [c for c in pool if c['candidate_id'] in selected_ids]
        if len(selected) != len(selected_ids):
            raise ValueError('previous selected batch lacks complete feature coverage')
        grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for c in selected:
            if c['candidate_id'] in prequential['by_pair']:
                grouped[region(c['payload'])].append(prequential['by_pair'][c['candidate_id']])
        selected_feedback = {
            'selected_count': len(selected),
            'new_valid_label_pairs': prequential['n_pairs'],
            'valid_label_pair_fraction': prequential['n_pairs']/len(selected),
            'selected_feature_means': {k: sum(c['payload'][k] for c in selected)/len(selected)
                                       for k in ('sampled_novelty', 'pool_local_density', 'product_ring_count', 'epistemic_variance')},
            'pre_update_region_errors': {key: aggregate(entries) for key, entries in grouped.items()},
            'limits': 'Absent valid labels do not identify a particular calculation failure cause.',
        }
    for candidate in candidates:
        p = candidate["payload"]
        group = groups.get(region(p), {})
        p["validation_region_mae"] = float(group.get("mae", model["mae"]))
        p["validation_region_bias"] = float(group.get("bias", model["bias"]))
        p["validation_region_pairs"] = int(group.get("n_pairs", 0))
        for kind in ("epistemic", "aleatoric", "predictive"):
            p[kind+"_variance_per_product"] = p[kind+"_variance"]/p["product_count"]
    return {
        "schema": "tricycle.mae-feedback/v1",
        "fixed_panel_comparison": comparison,
        "previous_hypothesis": prior["function"].get("hypothesis", "") if prior else "",
        "previous_expected_effect": prior["function"].get("expected_effect", "") if prior else "",
        "pre_update_selected_label_errors": prequential,
        'previous_batch_outcome': selected_feedback,
        "region_feature_definition": "pair-average product ring count <=2 vs >2; empty group falls back to global auxiliary error, support stays zero",
        "limits": ["Eight initial pairs form a small auxiliary panel, not a representative test set.",
                   "Auxiliary model never trains on panel pairs; main model trains on all revealed labels.",
                   "Selected-label surprise does not estimate population MAE or causal sampling benefit."],
    }


def review(directory: Path, response: dict[str, Any]) -> dict[str, Any]:
    """Inspect an unselected proposal, using features and formulas only."""
    from tricycle_sampling import AgentAuthoredAcquisitionSamplingStrategy, Candidate
    from tricycle_sampling.replay.campaign import read
    from tricycle_sampling.replay.decision import decision_spec

    task = read(directory / "task.json")
    candidates = [Candidate(c["candidate_id"], c["payload"])
                  for c in read(directory / "pair-candidates.json")["candidates"]]
    spec = decision_spec(directory, response)
    plan = AgentAuthoredAcquisitionSamplingStrategy().plan_with_context(
        candidates, task["budget"], {"agent_acquisition": spec})
    ranked = sorted(plan["candidate_scores"], key=lambda r: (-r["score"], r["candidate_id"]))
    ids = [r["candidate_id"] for r in ranked[:task["budget"]]]
    selected = [c for c in candidates if c.candidate_id in set(ids)]
    numeric = [k for k, v in candidates[0].payload.items() if isinstance(v, (int, float))]
    distributions = {key: {"pool_mean": sum(c.payload[key] for c in candidates)/len(candidates),
                           "selected_mean": sum(c.payload[key] for c in selected)/len(selected)}
                     for key in numeric}
    counts: dict[str, int] = defaultdict(int)
    for c in selected:
        counts[region(dict(c.payload))] += 1
    prior_functions = read(directory / "acquisition-history.json")["previous_rounds"]
    overlap: dict[str, Any] = {}
    if prior_functions:
        old = prior_functions[-1]["function"]
        old_spec = {**spec, 'expression': old['expression'], 'cost_expression': old['cost_expression']}
        # A reference rule may become constant after the pool shrinks. In the
        # new guided profile it is a counterfactual diagnostic, not a proposal.
        old_budget = len(candidates) if task.get('decision_protocol', {}).get('guidance') == replay_cost_guidance.NAME else task['budget']
        old_plan = AgentAuthoredAcquisitionSamplingStrategy().plan_with_context(
            candidates, old_budget, {'agent_acquisition': old_spec})
        old_ids = {r['candidate_id'] for r in sorted(old_plan['candidate_scores'], key=lambda r: (-r['score'], r['candidate_id']))[:task['budget']]}
        overlap = {"previous_rule_on_current_pool_overlap": len(old_ids & set(ids)),
                   "batch_size": task["budget"]}
        if old_budget == len(candidates):
            overlap['previous_rule_unique_scores'] = len({row['score'] for row in old_plan['candidate_scores']})
            overlap['previous_rule_tie_break'] = 'candidate_id ascending; constant reference is diagnostic only'
    # Representative structures are used for explanation only, never as a filter.
    result = {"proposal": {k: response[k] for k in FIELDS}, "selected_count": len(ids),
              "selected_region_counts": dict(counts), "feature_comparison": distributions,
              "score_min": ranked[-1]["score"], "score_max": ranked[0]["score"],
              "unique_scores": len({r["score"] for r in ranked}), **overlap,
              "batch_similarity": batch_similarity([str(c.payload['mapped_reaction_smiles']) for c in selected]),
              "selected_examples": [{"candidate_id": c.candidate_id,
                                      "reaction_smiles": c.payload["mapped_reaction_smiles"]}
                                     for c in selected[:6]],
              "limits": "No labels or generalization outcome for the proposed batch are available."}
    result["proposal_sha256"] = digest(directory / "response.json")
    if task.get('decision_protocol', {}).get('name') == replay_core_hours.NAME:
        result['resource_diagnostics'] = replay_core_hours.ranking_diagnostics(
            ranked, task['budget'], read(directory / 'pair-candidates.json')['candidates'])
    if task.get('decision_protocol', {}).get('guidance') == replay_cost_guidance.NAME:
        result['guidance_comparison'] = replay_cost_guidance.compare(
            read(directory / 'acquisition-guidance.json'), ranked,
            read(directory / 'pair-candidates.json')['candidates'], task['budget'])
    save(directory / "proposal-review.json", result)
    return result


def batch_similarity(reactions: list[str]) -> dict[str, Any]:
    from rdkit import Chem, DataStructs
    from rdkit.Chem import rdFingerprintGenerator

    generator = rdFingerprintGenerator.GetMorganGenerator(radius=2, fpSize=2048, includeChirality=True)
    fingerprints = []
    for reaction in reactions:
        sides = reaction.split('>')
        if len(sides) != 3:
            raise ValueError('review reaction must have reactant>>product syntax')
        entry = []
        for side in (sides[0], sides[2]):
            mol = Chem.MolFromSmiles(side)
            if mol is None:
                raise ValueError('review reaction cannot be parsed')
            for atom in mol.GetAtoms():
                atom.SetAtomMapNum(0)
            entry.append(generator.GetFingerprint(mol))
        fingerprints.append(entry)
    values = [(DataStructs.TanimotoSimilarity(left[0], right[0]) + DataStructs.TanimotoSimilarity(left[1], right[1]))/2
              for i, left in enumerate(fingerprints) for right in fingerprints[i+1:]]
    return {'pair_comparisons': len(values), 'mean': sum(values)/len(values) if values else None,
            'max': max(values) if values else None,
            'fraction_ge_0_9': sum(v >= .9 for v in values)/len(values) if values else None,
            'definition': 'mean-side chiral Morgan r2 2048 Tanimoto of ONE representative prior route per pair; not all-route or learned predictive redundancy'}


def pre_update(predictions: list[dict[str, str]], previous_hash: str) -> dict[str, Any]:
    by_pair: dict[str, list[dict[str, str]]] = defaultdict(list)
    for row in predictions:
        by_pair[pair(row)].append(row)
    return {"scope": "previous main checkpoint on newly revealed labels, before any update",
            "checkpoint_sha256": previous_hash, **metrics(predictions),
            "n_pairs": len(by_pair), "by_pair": {p: metrics(rows) for p, rows in by_pair.items()}}
