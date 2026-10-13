"""Automatically publish concise acquisition reports from accepted local artifacts.

No model calls, network calls, training or changes to scientific evidence.
Can run alongside an already running campaign without interrupting decisions.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import tempfile
import time
from pathlib import Path
from typing import Any

from tricycle_core.io import file_sha256 as _sha256_file
from tricycle_core.io import write_json as _write_json

PHASES = {
    "prepare": "准备训练输入",
    "submit": "提交训练",
    "monitor": "监控远端训练",
    "download": "下载结果",
    "analyze": "本地结构分析",
    "agent": "智能体决策/排队",
    "select": "校验公式并选样",
    "blocked": "需处理错误",
    "complete": "完成",
}


def read(path: Path) -> dict[str, Any]:
    return dict(json.loads(path.read_text(encoding="utf-8")))


def write_text(path: Path, text: str) -> None:
    if path.is_file() and path.read_text(encoding="utf-8") == text:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=path.parent, prefix=f".{path.name}.", delete=False
    ) as handle:
        temporary = Path(handle.name)
        handle.write(text)
        handle.flush()
        os.fsync(handle.fileno())
    try:
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def cell(value: object) -> str:
    return str(value).replace("|", "\\|").replace("\n", " ")


def decision_summary(directory: Path) -> dict[str, Any]:
    response = read(directory / "response.json")
    task = read(directory / "task.json")
    if response["task_sha256"] != _sha256_file(directory / "task.json"):
        raise ValueError("response/task hash mismatch")
    response_hash = _sha256_file(directory / "response.json")
    accepted = directory / "accepted-response.json"
    if accepted.exists() and read(accepted)["sha256"] != response_hash:
        raise ValueError("accepted response changed")
    selection_path = directory / "selection.json"
    selection = read(selection_path) if selection_path.exists() else {}
    rationale = str(response["rationale"])
    brief = directory / "rationale-brief.json"
    if brief.exists():
        override = read(brief)
        if override.get("response_sha256") == response_hash:
            rationale = str(override["rationale"])
    model = "旧调用未显式指定模型"
    process_path = directory / "agent-process.json"
    if process_path.exists():
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
    status = "已完成确定性选样" if selection else "公式已生成，待校验选样"
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
        f"# Seed {task['seed']:02d} · 第 {task['round']} 次智能选样",
        "",
        f"**状态：{status}** · 决策模型：{model}",
        "",
        "## 简要理由",
        "",
        rationale,
        "",
        "## 采集函数",
        "",
        "```text",
        str(response["expression"]),
        "```",
        "",
        "成本代理：",
        "",
        "```text",
        str(response["cost_expression"]),
        "```",
        "",
        "## 本轮结果",
        "",
        f"- 已采样 {task['sampled_count']} 对；候选 {task['candidate_count']} 对；本轮已选 {summary['selected_count']} 对。",
        f"- 辅助模型留出 MAE：{float(metrics['validation_mae']):.4f} kcal/mol；RMSE：{float(metrics['validation_rmse']):.4f} kcal/mol。",
        "- 上述误差来自已揭示标签的辅助验证模型；ground truth 不参与采集决策。",
        "",
        "[完整分析与原始响应](response.json) · [候选表](pair-candidates.json) · "
        "[分组验证误差](validation-groups.json)",
        "",
    ]
    if selection:
        lines.extend(
            [
                "[选中的候选 ID](selection.json) · [可回放请求](selection-request.toml)",
                "",
                f"函数 hash：`{summary['function_hash']}`",
                "",
            ]
        )
    if task.get('decision_protocol'):
        lines.extend(['## 科学决定与后续检验', '',
                      f"- 目标：降低泛化 MAE；操作：{response['decision_action']}。",
                      f"- 假设：{response['hypothesis']}",
                      f"- 预期作用：{response['expected_effect']}",
                      '- 固定辅助验证样本为八个初始底物对，样本支持有限；不代表主模型泛化误差。', '',
                      '[反馈材料](feedback.json)' if (directory / 'feedback.json').exists() else '',
                      '[批次复核](proposal-review.json)' if (directory / 'proposal-review.json').exists() else '', ''])
    write_text(directory / "ACQUISITION.md", "\n".join(lines))
    return summary


def update(root: Path) -> dict[str, Any]:
    with (root / ".reports.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        lines = [
            "# 智能采样决策摘要",
            "",
            "自动从本轮响应与选样产物生成；无需翻阅运行日志。轮次 0 是初始训练，首次智能选样编号为 1。",
            "",
            "| 轨迹 | 当前训练轮次 | 阶段 | 累计采样对数 | 决策索引 |",
            "| --- | ---: | --- | ---: | --- |",
        ]
        summaries = []
        errors = []
        complete = True
        for seed_dir in sorted(root.glob("seed-*")):
            state = read(seed_dir / "state.json")
            complete = complete and state["phase"] == "complete"
            lines.append(
                f"| {seed_dir.name} | {state['round']} | {PHASES.get(state['phase'], state['phase'])} | "
                f"{len(state['selected_ids'])} | [查看]({seed_dir.name}/ACQUISITION.md) |"
            )
            seed_lines = [
                f"# {seed_dir.name} · 采集函数与理由",
                "",
                "[返回实验总览](../ACQUISITION.md)",
                "",
                "| 选样轮次 | 状态 | 新增对数 | 简要理由 | 公式与证据 |",
                "| ---: | --- | ---: | --- | --- |",
            ]
            for response in sorted(seed_dir.glob("round-*/decision/response.json")):
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
                    f"{cell(summary['rationale'])} | [详情]({link}) |"
                )
            if len(seed_lines) == 6:
                seed_lines.extend(["", "尚未生成采集函数，当前阶段见实验总览。"])
            if state.get("last_error"):
                seed_lines.extend(["", "当前错误：", "", str(state["last_error"])])
            write_text(seed_dir / "ACQUISITION.md", "\n".join(seed_lines) + "\n")
        lines.extend(["", "## 已生成的决策", ""])
        for summary in summaries:
            lines.extend(
                [
                    f"### Seed {summary['seed']:02d} · 选样 {summary['selection_round']}",
                    "",
                    summary["rationale"],
                    "",
                    f"[查看函数与选样结果]({summary['summary_path']})",
                    "",
                ]
            )
        if not summaries:
            lines.append("尚未生成采集函数。")
        if errors:
            lines.extend(["", "部分摘要暂不可用，详见 report-status.json。"])
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
        status = update(args.run_dir.resolve())
        if not args.watch or status["complete"]:
            print(json.dumps(status))
            return
        time.sleep(10)


if __name__ == "__main__":
    main()
