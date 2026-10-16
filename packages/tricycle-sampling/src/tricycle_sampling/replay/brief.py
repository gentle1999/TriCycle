"""Deterministic, bounded acquisition evidence compiled from the entire pool."""
from __future__ import annotations

import csv
import json
import math
from collections import defaultdict
from pathlib import Path
from typing import Any

from tricycle_core import record_io as rio
from tricycle_core.record_io import file_sha256 as digest
from tricycle_core.record_io import write_json as save


def read(path: Path) -> dict[str, Any]:
    return dict(json.loads(rio.read_text(path)))



def group_summary(payloads: list[dict[str, Any]]) -> dict[str, Any]:
    keys = ("sampled_novelty", "pool_local_density", "epistemic_variance",
            "product_count", "mean_product_atom_count")
    return {"n_pairs": len(payloads), "mean": {
        k: sum(float(p[k]) for p in payloads) / len(payloads) for k in keys
    }} if payloads else {"n_pairs": 0, "mean": {}}


def correlations(payloads: list[dict[str, Any]]) -> dict[str, float | None]:
    result: dict[str, float | None] = {}
    for xkey, ykey in (("sampled_novelty", "epistemic_variance"),
                       ("pool_local_density", "epistemic_variance"),
                       ("product_count", "epistemic_variance"),
                       ("sampled_novelty", "pool_local_density")):
        xs, ys = [float(p[xkey]) for p in payloads], [float(p[ykey]) for p in payloads]
        xm, ym = sum(xs) / len(xs), sum(ys) / len(ys)
        numerator = sum((x-xm)*(y-ym) for x,y in zip(xs,ys,strict=True))
        denominator = math.sqrt(sum((x-xm)**2 for x in xs)*sum((y-ym)**2 for y in ys))
        result[f"{xkey} vs {ykey}"] = numerator / denominator if denominator else None
    return result


def build(directory: Path) -> dict[str, Any]:
    source_hashes = read(directory / "evidence-hashes.json")
    for name, expected in source_hashes.items():
        if digest(directory / name) != expected:
            raise ValueError(f"decision evidence changed: {name}")
    candidates = read(directory / "pair-candidates.json")["candidates"]
    task = read(directory / "task.json")
    history = read(directory / "acquisition-history.json")
    ids = [c["candidate_id"] for c in candidates]
    if len(ids) != len(set(ids)) or len(ids) != task["candidate_count"]:
        raise ValueError("candidate count or uniqueness mismatch")
    if set(ids) & set(history["selected_ids"]):
        raise ValueError("candidate pool overlaps selected history")
    by_pair: dict[str, list[dict[str, str]]] = defaultdict(list)
    with rio.open(directory / "structure/structure-features.csv") as handle:
        for row in csv.DictReader(handle):
            by_pair[f'{row["ene_id"].zfill(12)}+{row["diene_id"].zfill(12)}'].append(row)
    if set(by_pair) != set(ids):
        raise ValueError("structure features do not cover complete candidate pool")
    for c in candidates:
        p = c["payload"]
        rows = by_pair[c["candidate_id"]]
        if len(rows) != p["product_count"]:
            raise ValueError("structure route count mismatch")
        for key in ("sampled_novelty", "pool_local_density", "product_ring_count", "product_heavy_atom_count"):
            mean = sum(float(r[key]) for r in rows) / len(rows)
            if not math.isclose(mean, p[key], abs_tol=1e-9):
                raise ValueError(f"structure aggregation mismatch: {key}")
    numeric = sorted(k for k, v in candidates[0]["payload"].items() if isinstance(v, (int, float)))
    distributions = {}
    examples: dict[str, dict[str, Any]] = {}
    for key in numeric + ["linear_cost_proxy"]:
        values = []
        for c in candidates:
            p = c["payload"]
            v = float(p["product_count"] * p["mean_product_atom_count"] if key == "linear_cost_proxy" else p[key])
            if not math.isfinite(v):
                raise ValueError(f"non-finite candidate feature: {key}")
            values.append((v, c["candidate_id"]))
        values.sort()
        n = len(values)
        distributions[key] = {
            "min": values[0][0], "p10": values[int((n-1)*.1)][0],
            "median": values[(n-1)//2][0], "p90": values[int((n-1)*.9)][0],
            "max": values[-1][0], "mean": sum(v for v, _ in values)/n,
        }
        if key == "linear_cost_proxy":
            distributions[key]["total"] = sum(v for v, _ in values)
        if key in ("sampled_novelty", "pool_local_density", "epistemic_variance", "product_ring_count", "linear_cost_proxy"):
            for position in (0, (n-1)//2, n-1):
                candidate_id = values[position][1]
                examples.setdefault(candidate_id, {"candidate_id": candidate_id, "roles": []})["roles"].append(
                    f"{key}:{position}/{n-1}"
                )
    lookup = {c["candidate_id"]: c["payload"] for c in candidates}
    for candidate_id, example in examples.items():
        rows = by_pair[candidate_id]
        example["features"] = {k: lookup[candidate_id][k] for k in numeric}
        example["canonical_reaction_smiles"] = rows[0]["canonical_reaction_smiles"]
    metrics = read(directory / "model-metrics.json")
    structure = read(directory / "structure/structure-analysis.json")
    validation_rows: dict[str, list[dict[str, str]]] = defaultdict(list)
    with rio.open(directory / "validation-structure/structure-features.csv") as handle:
        for row in csv.DictReader(handle):
            validation_rows[f'{row["ene_id"].zfill(12)}+{row["diene_id"].zfill(12)}'].append(row)
    if set(validation_rows) != set(metrics["by_pair"]):
        raise ValueError("validation structure and metrics pair coverage mismatch")
    hardest = []
    for candidate_id, errors in sorted(metrics["by_pair"].items(), key=lambda item: (-item[1]["mae"], item[0]))[:3]:
        rows = validation_rows[candidate_id]
        hardest.append({"candidate_id": candidate_id, "errors": errors,
                        "canonical_reaction_smiles": rows[0]["canonical_reaction_smiles"],
                        "mean_features": {k: sum(float(r[k]) for r in rows)/len(rows)
                                          for k in ("product_ring_count", "sampled_novelty", "pool_local_density")}})
    payloads = [c["payload"] for c in candidates]
    pool_groups = {name: group_summary([p for p in payloads if (p["product_ring_count"] <= 2) == low])
                   for name, low in (("product_rings<=2", True), ("product_rings>2", False))}
    novelty90 = distributions["sampled_novelty"]["p90"]
    uncertainty90 = distributions["epistemic_variance"]["p90"]
    previous = history["previous_rounds"]
    trend = {"previous_validation_mae": previous[-1]["validation"]["mae"],
             "mae_delta": metrics["mae"]-previous[-1]["validation"]["mae"],
             "rmse_delta": metrics["rmse"]-previous[-1]["validation"]["rmse"]} if previous else {}
    brief = {
        "schema": "tricycle.replay-decision-brief/v2",
        "compiler_sha256": digest(Path(__file__)),
        "pool_groups": pool_groups, "feature_pearson_correlations": correlations(payloads),
        "validation_hardest_pairs": hardest, "validation_trend": trend,
        "novel_and_uncertain_count": sum(p["sampled_novelty"] >= novelty90 and p["epistemic_variance"] >= uncertainty90 for p in payloads),
        "task_sha256": digest(directory / "task.json"),
        "task": {k: task[k] for k in ("seed", "round", "budget", "sampled_count", "candidate_count", "metric_scope", "context")},
        "preflight": {"all_evidence_hashes_verified": True, "all_candidates_checked": len(ids),
                      "all_structure_paths_checked": sum(map(len, by_pair.values())),
                      "unique_ids_and_unselected": True, "structure_aggregation_verified": True},
        "feature_distributions": distributions, "representatives": list(examples.values()),
        "validation_groups": read(directory / "validation-groups.json"),
        "model": {k: metrics[k] for k in ("scope", "mae", "rmse", "bias", "n", "unit") if k in metrics},
        "structure_provenance": {k: structure[k] for k in ("tool", "tool_sha256", "rdkit_version", "parameters", "structure_validation") if k in structure},
        "evidence_hashes": source_hashes,
        "history": [{"round": h["round"], "validation_mae": h["validation"]["mae"],
                     "validation_rmse": h["validation"]["rmse"]} for h in history["previous_rounds"]],
        "recent_functions": [{"round": h["round"], "expression": h["function"]["expression"],
                              "cost_expression": h["function"]["cost_expression"],
                              "rationale": h["function"]["rationale"]} for h in history["previous_rounds"][-3:]],
        "limits": ["Auxiliary model heldout metrics are not full-model validation.",
                   "Linear cost proxy is product_count * mean_product_atom_count, not measured runtime.",
                   "Representatives explain distributions; the returned function is applied to ALL remaining candidates."],
    }
    if task.get("acquisition_policy"):
        brief["acquisition_policy"] = task["acquisition_policy"]
        brief["limits"].append("Cross-round auxiliary validation sets change; deltas are descriptive, not a plateau trigger.")
    if task.get('decision_protocol'):
        brief['decision_protocol'] = task['decision_protocol']
        brief['objective'] = task['objective']
        brief['validation_policy'] = task['validation_policy']
        brief['limits'].append('Fixed auxiliary panel contains eight initial labeled pairs; regional evidence is small and not main-model validation.')
        for item, previous_item in zip(brief['recent_functions'], previous[-3:], strict=True):
            for key in ('decision_action', 'hypothesis', 'expected_effect'):
                item[key] = previous_item['function'].get(key, '')
        if task['decision_protocol']['feedback']:
            brief['feedback'] = read(directory / 'feedback.json')
        if task['decision_protocol']['name'] == 'autonomous-core-hours-mae-v1':
            brief['core_hour_budget'] = task['context']['core_hour_budget']
            previous_diagnostics = []
            for item in previous[-3:]:
                path = directory.parent.parent / f"round-{item['round']-1:04d}" / 'decision/formula-assessment.json'
                if rio.exists(path):
                    assessment = read(path)
                    if assessment.get('resource_diagnostics'):
                        previous_diagnostics.append({'round': item['round'], **assessment['resource_diagnostics']})
            brief['previous_batch_resource_diagnostics'] = previous_diagnostics
        from tricycle_sampling.replay import cost_guidance as replay_cost_guidance
        if task['decision_protocol'].get('guidance') == replay_cost_guidance.NAME:
            reference = read(directory / 'acquisition-guidance.json')
            replay_cost_guidance.verify(reference, candidates, task['budget'])
            if task['context'].get('coverage_cost') != reference['context']:
                raise ValueError('coverage/cost context differs from its frozen reference')
            brief['acquisition_guidance'] = replay_cost_guidance.compact(reference)
    save(directory / "decision-brief.json", brief)
    return brief
