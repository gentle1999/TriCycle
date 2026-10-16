"""Automatically publish concise acquisition reports from accepted local artifacts.

No model calls, network calls, training or changes to scientific evidence.
Can run alongside an already running campaign without interrupting decisions.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import time
from pathlib import Path
from typing import Any

from tricycle_core import record_io as rio
from tricycle_core.record_io import file_sha256 as _sha256_file
from tricycle_core.record_io import write_json as _write_json

PHASES = {
    "prepare": "Prepare training inputs",
    "submit": "Submit training",
    "monitor": "Monitor remote training",
    "download": "Download results",
    "analyze": "Local structural analysis",
    "agent": "Agent decision / queued",
    "select": "Validate formula and select candidates",
    "blocked": "Error requires attention",
    "complete": "Complete",
}


def read(path: Path) -> dict[str, Any]:
    return dict(json.loads(rio.read_text(path, encoding="utf-8")))


def write_text(path: Path, text: str) -> None:
    if rio.is_file(path) and rio.read_text(path, encoding="utf-8") == text:
        return
    rio.atomic_write_text(path, text)


def cell(value: object) -> str:
    return str(value).replace("|", "\\|").replace("\n", " ")


def decision_summary(directory: Path) -> dict[str, Any]:
    response = read(directory / "response.json")
    task = read(directory / "task.json")
    if response["task_sha256"] != _sha256_file(directory / "task.json"):
        raise ValueError("response/task hash mismatch")
    response_hash = _sha256_file(directory / "response.json")
    accepted = directory / "accepted-response.json"
    if rio.exists(accepted) and read(accepted)["sha256"] != response_hash:
        raise ValueError("accepted response changed")
    selection_path = directory / "selection.json"
    selection = read(selection_path) if rio.exists(selection_path) else {}
    rationale = str(response["rationale"])
    brief = directory / "rationale-brief.json"
    if rio.exists(brief):
        override = read(brief)
        if override.get("response_sha256") == response_hash:
            rationale = str(override["rationale"])
    model = "Model not explicitly recorded by the earlier invocation"
    process_path = directory / "agent-process.json"
    if rio.exists(process_path):
        argv = read(process_path)["argv"]
        if "--model" in argv:
            model = str(argv[argv.index("--model") + 1])
            effort = next(
                (
                    str(a).split("=", 1)[1].strip('"')
                    for a in argv
                    if str(a).startswith("model_reasoning_effort=")
                ),
                "",
            )
            model += f" / {effort}" if effort else ""
    metrics = task["context"]["model_metrics"]
    status = "Deterministic selection complete" if selection else "Formula generated; validation and selection pending"
    summary = {
        "seed": task["seed"],
        "selection_round": task["round"],
        "status": status,
        "sampled_before": task["sampled_count"],
        "candidate_count": task["candidate_count"],
        "selected_count": len(selection.get("candidate_ids", [])),
        "rationale": rationale,
        "expression": response["expression"],
        "cost_expression": response["cost_expression"],
        "model": model,
        "validation_metrics": metrics,
        "response_sha256": response_hash,
        "function_hash": selection.get("acquisition_function_hash", ""),
    }
    if task.get('decision_protocol'):
        summary.update({key: response[key] for key in ('objective', 'decision_action', 'hypothesis', 'expected_effect')})
        summary['decision_source'] = response.get('decision_source', 'agent')
    _write_json(directory / "acquisition-summary.json", summary)
    lines = [
        f"# Seed {task['seed']:02d} · Intelligent selection {task['round']}",
        "",
        f"**Status: {status}** · Decision model: {model}",
        "",
        "## Brief rationale",
        "",
        rationale,
        "",
        "## Acquisition function",
        "",
        "```text",
        str(response["expression"]),
        "```",
        "",
        "Cost proxy:",
        "",
        "```text",
        str(response["cost_expression"]),
        "```",
        "",
        "## Round results",
        "",
        f"- Previously sampled: {task['sampled_count']} pairs; candidates: {task['candidate_count']} pairs; selected this round: {summary['selected_count']} pairs.",
        f"- Auxiliary-model holdout MAE: {float(metrics['validation_mae']):.4f} kcal/mol; RMSE: {float(metrics['validation_rmse']):.4f} kcal/mol.",
        "- These errors come from an auxiliary validation model using revealed labels; ground truth does not enter acquisition decisions.",
        "",
        "[Full analysis and original response](response.json) · [Candidate table](pair-candidates.json) · "
        "[Validation errors by group](validation-groups.json)",
        "",
    ]
    if selection:
        lines.extend(
            [
                "[Selected candidate IDs](selection.json) · [Replayable request](selection-request.toml)",
                "",
                f"Function hash: `{summary['function_hash']}`",
                "",
            ]
        )
    if task.get('decision_protocol'):
        lines.extend(['## Scientific decision and follow-up validation', '',
                      f"- Objective: reduce generalization MAE; action: {response['decision_action']}.",
                      f"- Hypothesis: {response['hypothesis']}",
                      f"- Expected effect: {response['expected_effect']}",
                      '- The fixed auxiliary panel contains eight initial substrate pairs with limited support; it does not measure main-model generalization error.', '',
                      '[Feedback evidence](feedback.json)' if rio.exists(directory / 'feedback.json') else '',
                      '[Batch review](proposal-review.json)' if rio.exists(directory / 'proposal-review.json') else '', ''])
    write_text(directory / "ACQUISITION.md", "\n".join(lines))
    return summary


def update(root: Path) -> dict[str, Any]:
    with rio.open(root / ".reports.lock", "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        lines = [
            "# Intelligent sampling decision summary",
            "",
            "Generated automatically from round responses and selection artifacts. Round 0 is initial training; intelligent selection starts at round 1.",
            "",
            "| Trajectory | Current training round | Phase | Cumulative sampled pairs | Decision index |",
            "| --- | ---: | --- | ---: | --- |",
        ]
        summaries = []
        errors = []
        complete = True
        for seed_dir in sorted(rio.glob(root, "seed-*")):
            state = read(seed_dir / "state.json")
            complete = complete and state["phase"] == "complete"
            lines.append(
                f"| {seed_dir.name} | {state['round']} | {PHASES.get(state['phase'], state['phase'])} | "
                f"{len(state['selected_ids'])} | [View]({seed_dir.name}/ACQUISITION.md) |"
            )
            seed_lines = [
                f"# {seed_dir.name} · Acquisition functions and rationales",
                "",
                "[Back to experiment overview](../ACQUISITION.md)",
                "",
                "| Selection round | Status | Added pairs | Brief rationale | Formula and evidence |",
                "| ---: | --- | ---: | --- | --- |",
            ]
            for response in sorted(rio.glob(seed_dir, "round-*/decision/response.json")):
                try:
                    summary = decision_summary(response.parent)
                except (ValueError, OSError, KeyError) as exc:
                    errors.append({"path": str(response), "error": str(exc)})
                    continue
                summary["summary_path"] = str(
                    (response.parent / "ACQUISITION.md").relative_to(root)
                )
                summaries.append(summary)
                link = (response.parent / "ACQUISITION.md").relative_to(seed_dir)
                seed_lines.append(
                    f"| {summary['selection_round']} | {summary['status']} | {summary['selected_count']} | "
                    f"{cell(summary['rationale'])} | [Details]({link}) |"
                )
            if len(seed_lines) == 6:
                seed_lines.extend(["", "No acquisition function is available yet; see the experiment overview for the current phase."])
            if state.get("last_error"):
                seed_lines.extend(["", "Current error:", "", str(state["last_error"])])
            write_text(seed_dir / "ACQUISITION.md", "\n".join(seed_lines) + "\n")
        lines.extend(["", "## Generated decisions", ""])
        for summary in summaries:
            lines.extend(
                [
                    f"### Seed {summary['seed']:02d} · Selection {summary['selection_round']}",
                    "",
                    summary["rationale"],
                    "",
                    f"[View function and selection results]({summary['summary_path']})",
                    "",
                ]
            )
        if not summaries:
            lines.append("No acquisition function is available yet.")
        if errors:
            lines.extend(["", "Some summaries are temporarily unavailable; see report-status.json."])
        write_text(root / "ACQUISITION.md", "\n".join(lines) + "\n")
        _write_json(root / "acquisition-summaries.json", {"decisions": summaries})
        status = {
            "updated_at": time.time(),
            "decision_count": len(summaries),
            "complete": complete,
            "errors": errors,
        }
        _write_json(root / "report-status.json", status)
        return status


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--watch", action="store_true")
    args = parser.parse_args()
    while True:
        status = update(rio.resolve(args.run_dir))
        if not args.watch or status["complete"]:
            print(json.dumps(status))
            return
        time.sleep(10)


if __name__ == "__main__":
    main()
