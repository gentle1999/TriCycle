from __future__ import annotations

import ast
import csv
import hashlib
import math
import operator
from collections.abc import Mapping, Sequence
from functools import lru_cache
from pathlib import Path
from typing import Any

from tricycle_core import canonical_json
from tricycle_core.errors import InvalidRequestError

from .models import Candidate


AGENT_ACQUISITION_STRATEGY = "agent-authored-acquisition"
AGENT_ACQUISITION_SCHEMA = "tricycle.agent-acquisition/v1"
AGENT_ACQUISITION_VERSION = "1.0.0"
_MAX_EXPRESSION_LENGTH = 4096
_MAX_EXPRESSION_NODES = 256
_REQUIRED_COST_FEATURES = {"product_count", "mean_product_atom_count"}


def _number(value: Any, *, field_name: str) -> float:
    if isinstance(value, bool):
        raise InvalidRequestError(f"{field_name} must be a finite number")
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise InvalidRequestError(f"{field_name} must be a finite number") from exc
    if not math.isfinite(result):
        raise InvalidRequestError(f"{field_name} must be a finite number")
    return result


def _read_mapping_key(value: Any, key: Any, *, expression: str) -> Any:
    if not isinstance(value, Mapping):
        raise InvalidRequestError(
            f"agent acquisition expression cannot read {key!r} from a non-mapping"
        )
    if key not in value:
        raise InvalidRequestError(
            f"agent acquisition expression references missing field {key!r}: {expression}"
        )
    return value[key]


@lru_cache(maxsize=512)
def _parse_expression(expression: str) -> ast.Expression:
    if not expression.strip():
        raise InvalidRequestError("agent acquisition expression must not be empty")
    if len(expression) > _MAX_EXPRESSION_LENGTH:
        raise InvalidRequestError("agent acquisition expression is too long")
    try:
        tree = ast.parse(expression, mode="eval")
    except SyntaxError as exc:
        raise InvalidRequestError(f"invalid agent acquisition expression: {exc.msg}") from exc
    nodes = tuple(ast.walk(tree))
    if len(nodes) > _MAX_EXPRESSION_NODES:
        raise InvalidRequestError("agent acquisition expression is too complex")
    _validate_expression_node(tree.body, expression=expression)
    return tree


def _validate_expression_node(node: ast.AST, *, expression: str) -> None:
    allowed = (
        ast.Constant,
        ast.Name,
        ast.Attribute,
        ast.Subscript,
        ast.BinOp,
        ast.UnaryOp,
        ast.BoolOp,
        ast.Compare,
        ast.IfExp,
        ast.Call,
        ast.Add,
        ast.Sub,
        ast.Mult,
        ast.Div,
        ast.FloorDiv,
        ast.Mod,
        ast.Pow,
        ast.UAdd,
        ast.USub,
        ast.Not,
        ast.And,
        ast.Or,
        ast.Eq,
        ast.NotEq,
        ast.Lt,
        ast.LtE,
        ast.Gt,
        ast.GtE,
        ast.Load,
    )
    if not isinstance(node, allowed):
        raise InvalidRequestError(
            f"unsupported syntax in agent acquisition expression: {type(node).__name__}"
        )
    if isinstance(node, ast.Constant) and (
        isinstance(node.value, bool) or not isinstance(node.value, (int, float, str))
    ):
        raise InvalidRequestError("agent acquisition constants must be numbers or field names")
    if isinstance(node, ast.Name) and node.id.startswith("_"):
        raise InvalidRequestError("private names are not allowed in agent acquisition expressions")
    if isinstance(node, ast.Attribute) and node.attr.startswith("_"):
        raise InvalidRequestError("private attributes are not allowed in agent acquisition expressions")
    if isinstance(node, ast.Subscript) and not (
        isinstance(node.slice, ast.Constant)
        and isinstance(node.slice.value, (str, int))
        and not isinstance(node.slice.value, bool)
    ):
        raise InvalidRequestError("agent acquisition subscripts must use a literal key")
    if isinstance(node, ast.Call):
        if not isinstance(node.func, ast.Name) or node.func.id not in {
            "abs",
            "exp",
            "log",
            "log1p",
            "max",
            "min",
            "sqrt",
        }:
            raise InvalidRequestError(
                "agent acquisition calls may use only abs, exp, log, log1p, max, min, and sqrt"
            )
        if node.keywords:
            raise InvalidRequestError("agent acquisition calls do not support keyword arguments")
    for child in ast.iter_child_nodes(node):
        _validate_expression_node(child, expression=expression)


_BINARY_OPERATORS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
}
_COMPARE_OPERATORS = {
    ast.Eq: operator.eq,
    ast.NotEq: operator.ne,
    ast.Lt: operator.lt,
    ast.LtE: operator.le,
    ast.Gt: operator.gt,
    ast.GtE: operator.ge,
}
_SAFE_FUNCTIONS = {
    "abs": abs,
    "exp": math.exp,
    "log": math.log,
    "log1p": math.log1p,
    "max": max,
    "min": min,
    "sqrt": math.sqrt,
}


def _evaluate_node(node: ast.AST, names: Mapping[str, Any], *, expression: str) -> Any:
    if isinstance(node, ast.Constant):
        return node.value
    if isinstance(node, ast.Name):
        if node.id in _SAFE_FUNCTIONS:
            return _SAFE_FUNCTIONS[node.id]
        return _read_mapping_key(names, node.id, expression=expression)
    if isinstance(node, ast.Attribute):
        return _read_mapping_key(
            _evaluate_node(node.value, names, expression=expression),
            node.attr,
            expression=expression,
        )
    if isinstance(node, ast.Subscript):
        return _read_mapping_key(
            _evaluate_node(node.value, names, expression=expression),
            node.slice.value,  # type: ignore[attr-defined]
            expression=expression,
        )
    if isinstance(node, ast.BinOp):
        left = _number(_evaluate_node(node.left, names, expression=expression), field_name="expression operand")
        right = _number(_evaluate_node(node.right, names, expression=expression), field_name="expression operand")
        if isinstance(node.op, ast.Pow):
            if abs(right) > 16:
                raise InvalidRequestError("agent acquisition exponent magnitude cannot exceed 16")
            value = left**right
        else:
            operation = _BINARY_OPERATORS.get(type(node.op))
            if operation is None:
                raise InvalidRequestError("unsupported binary operator in agent acquisition expression")
            value = operation(left, right)
        return _number(value, field_name="agent acquisition expression result")
    if isinstance(node, ast.UnaryOp):
        value = _evaluate_node(node.operand, names, expression=expression)
        if isinstance(node.op, ast.Not):
            return not bool(value)
        numeric = _number(value, field_name="expression operand")
        return numeric if isinstance(node.op, ast.UAdd) else -numeric
    if isinstance(node, ast.BoolOp):
        if isinstance(node.op, ast.And):
            result: Any = True
            for value_node in node.values:
                result = _evaluate_node(value_node, names, expression=expression)
                if not bool(result):
                    return result
            return result
        result = False
        for value_node in node.values:
            result = _evaluate_node(value_node, names, expression=expression)
            if bool(result):
                return result
        return result
    if isinstance(node, ast.Compare):
        left = _evaluate_node(node.left, names, expression=expression)
        for operation_node, comparator in zip(node.ops, node.comparators, strict=True):
            right = _evaluate_node(comparator, names, expression=expression)
            operation = _COMPARE_OPERATORS.get(type(operation_node))
            if operation is None or not operation(left, right):
                return False
            left = right
        return True
    if isinstance(node, ast.IfExp):
        branch = node.body if bool(_evaluate_node(node.test, names, expression=expression)) else node.orelse
        return _evaluate_node(branch, names, expression=expression)
    if isinstance(node, ast.Call):
        function = _SAFE_FUNCTIONS[node.func.id]  # type: ignore[union-attr]
        args = [
            _number(_evaluate_node(arg, names, expression=expression), field_name="function argument")
            for arg in node.args
        ]
        if function in (min, max):
            if not args:
                raise InvalidRequestError("min and max need at least one expression argument")
            value = function(args)
        else:
            if len(args) != 1:
                raise InvalidRequestError(f"{node.func.id} needs exactly one expression argument")  # type: ignore[union-attr]
            value = function(args[0])
        return _number(value, field_name="agent acquisition expression result")
    raise InvalidRequestError(f"unsupported expression node: {type(node).__name__}")


def evaluate_numeric_expression(
    expression: str,
    *,
    candidate: Mapping[str, Any],
    context: Mapping[str, Any],
    estimated_cost: float | None = None,
) -> float:
    """Evaluate the deliberately small, deterministic acquisition expression language."""

    names: dict[str, Any] = dict(candidate)
    names["candidate"] = candidate
    names["context"] = context
    if estimated_cost is not None:
        names["estimated_cost"] = estimated_cost
    try:
        value = _evaluate_node(_parse_expression(expression).body, names, expression=expression)
    except (ArithmeticError, OverflowError, TypeError, ValueError) as exc:
        raise InvalidRequestError(f"cannot evaluate agent acquisition expression: {exc}") from exc
    return _number(value, field_name="agent acquisition score")


def _referenced_candidate_names(expression: str) -> set[str]:
    tree = _parse_expression(expression)
    return {
        node.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Name) and node.id not in _SAFE_FUNCTIONS and node.id not in {"context", "candidate", "estimated_cost"}
    }


def _attribute_path(node: ast.AST) -> tuple[str, ...]:
    if isinstance(node, ast.Name):
        return (node.id,)
    if isinstance(node, ast.Attribute):
        return (*_attribute_path(node.value), node.attr)
    if (
        isinstance(node, ast.Subscript)
        and isinstance(node.slice, ast.Constant)
        and isinstance(node.slice.value, str)
    ):
        return (*_attribute_path(node.value), node.slice.value)
    return ()


def _require_adaptive_inputs(expression: str) -> None:
    tree = _parse_expression(expression)
    paths = {_attribute_path(node) for node in ast.walk(tree)}
    names = {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}
    if "estimated_cost" not in names:
        raise InvalidRequestError("acquisition expression must use estimated_cost")
    if "sampled_novelty" not in names:
        raise InvalidRequestError("acquisition expression must use candidate sampled_novelty")
    for context_key in ("sampled_distribution", "model_metrics"):
        if not any(path[:2] == ("context", context_key) and len(path) > 2 for path in paths):
            raise InvalidRequestError(
                f"acquisition expression must use a context.{context_key} metric"
            )


def _required_number(candidate: Candidate, key: str, *, positive: bool = False) -> float:
    if key not in candidate.payload:
        raise InvalidRequestError(
            f"candidate {candidate.candidate_id!r} is missing payload.{key} required for cost estimation"
        )
    value = _number(candidate.payload[key], field_name=f"candidate {candidate.candidate_id!r} {key}")
    if key == "product_count" and not value.is_integer():
        raise InvalidRequestError(f"candidate {candidate.candidate_id!r} product_count must be an integer")
    if value < 0 or (positive and value == 0):
        qualifier = "positive" if positive else "non-negative"
        raise InvalidRequestError(f"candidate {candidate.candidate_id!r} {key} must be {qualifier}")
    return value


def _cost_summary(costs: Sequence[float]) -> dict[str, float | int]:
    if not costs:
        return {
            "candidate_count": 0,
            "total": 0.0,
            "mean": 0.0,
            "median": 0.0,
            "p90": 0.0,
            "maximum": 0.0,
        }
    ordered = sorted(costs)
    middle = len(ordered) // 2
    median = (
        ordered[middle]
        if len(ordered) % 2
        else ordered[middle - 1] / 2.0 + ordered[middle] / 2.0
    )
    p90 = ordered[max(0, math.ceil(0.9 * len(ordered)) - 1)]
    try:
        total = math.fsum(ordered)
    except OverflowError as exc:
        raise InvalidRequestError("estimated candidate costs overflow the supported numeric range") from exc
    mean = total / len(ordered)
    if not all(math.isfinite(value) for value in (total, mean, median, p90)):
        raise InvalidRequestError("estimated candidate cost summary is not finite")
    return {
        "candidate_count": len(ordered),
        "total": total,
        "mean": mean,
        "median": median,
        "p90": p90,
        "maximum": ordered[-1],
    }


def acquisition_spec(request_metadata: Mapping[str, Any]) -> Mapping[str, Any]:
    raw = request_metadata.get("agent_acquisition")
    if not isinstance(raw, Mapping):
        raise InvalidRequestError(
            "agent-authored-acquisition requires metadata.agent_acquisition"
        )
    if raw.get("schema") != AGENT_ACQUISITION_SCHEMA:
        raise InvalidRequestError(
            f"metadata.agent_acquisition.schema must be {AGENT_ACQUISITION_SCHEMA!r}"
        )
    for key in ("function_id", "version", "expression", "cost_expression", "rationale", "authored_by"):
        if not isinstance(raw.get(key), str) or not str(raw[key]).strip():
            raise InvalidRequestError(f"metadata.agent_acquisition.{key} must not be empty")
    if str(raw["authored_by"]) != "agent-authored-acquisition":
        raise InvalidRequestError(
            "metadata.agent_acquisition.authored_by must be 'agent-authored-acquisition'"
        )
    context = raw.get("context")
    if not isinstance(context, Mapping):
        raise InvalidRequestError("metadata.agent_acquisition.context must be a mapping")
    for key in ("sampled_distribution", "model_metrics"):
        if not isinstance(context.get(key), Mapping) or not context[key]:
            raise InvalidRequestError(
                f"metadata.agent_acquisition.context.{key} must be a non-empty mapping"
            )
    if not isinstance(context["sampled_distribution"].get("novelty_definition"), str) or not str(
        context["sampled_distribution"]["novelty_definition"]
    ).strip():
        raise InvalidRequestError(
            "sampled_distribution.novelty_definition must identify how candidate novelty was computed"
        )
    evidence = raw.get("evidence")
    if not isinstance(evidence, Mapping) or not evidence:
        raise InvalidRequestError("metadata.agent_acquisition.evidence must be a non-empty mapping")
    for key in ("sampled_set", "candidate_space", "model_evaluation"):
        if not isinstance(evidence.get(key), str) or not str(evidence[key]).strip():
            raise InvalidRequestError(f"metadata.agent_acquisition.evidence.{key} must not be empty")
    status_path, status_workers = _validate_status_evidence(evidence.get("calculation_status"))
    download_path, download_workers = _validate_download_evidence(
        evidence.get("downloaded_results")
    )
    if status_path.parent.resolve() != download_path.parent.resolve():
        raise InvalidRequestError(
            "calculation_status.tsv and download-results.tsv must belong to the same local run"
        )
    if status_workers != download_workers:
        raise InvalidRequestError(
            "calculation_status.tsv and download-results.tsv must report the same workers"
        )
    _parse_expression(str(raw["expression"]))
    _require_adaptive_inputs(str(raw["expression"]))
    _parse_expression(str(raw["cost_expression"]))
    cost_features = _referenced_candidate_names(str(raw["cost_expression"]))
    if not _REQUIRED_COST_FEATURES.issubset(cost_features):
        missing = sorted(_REQUIRED_COST_FEATURES - cost_features)
        raise InvalidRequestError(
            "cost_expression must use payload.product_count and "
            f"payload.mean_product_atom_count (missing: {', '.join(missing)})"
        )
    return raw


def _read_local_tsv_evidence(value: Any, *, name: str) -> tuple[Path, list[dict[str, str]]]:
    if not isinstance(value, Mapping):
        raise InvalidRequestError(
            f"metadata.agent_acquisition.evidence.{name} must contain a local report"
        )
    path_value = value.get("manifest_path")
    expected_hash = value.get("sha256")
    if not isinstance(path_value, str) or not path_value.strip():
        raise InvalidRequestError(f"{name}.manifest_path must be an absolute local path")
    if not isinstance(expected_hash, str) or not expected_hash.strip():
        raise InvalidRequestError(f"{name}.sha256 must not be empty")
    report_path = Path(path_value)
    if not report_path.is_absolute() or not report_path.is_file():
        raise InvalidRequestError(
            f"{name} must already exist at an absolute local path: {report_path}"
        )
    actual_hash = hashlib.sha256(report_path.read_bytes()).hexdigest()
    normalized_hash = expected_hash.removeprefix("sha256:").lower()
    if normalized_hash != actual_hash:
        raise InvalidRequestError(f"{name} SHA-256 does not match: {report_path}")
    expected_name = "status.tsv" if name == "calculation_status" else "download-results.tsv"
    if report_path.name != expected_name:
        raise InvalidRequestError(
            f"{name}.manifest_path must point to Bohr batch {expected_name}"
        )
    try:
        with report_path.open(encoding="utf-8", newline="") as handle:
            rows = list(csv.DictReader(handle, delimiter="\t"))
    except (OSError, UnicodeError, csv.Error) as exc:
        raise InvalidRequestError(f"cannot read {name} report: {report_path}") from exc
    if not rows:
        raise InvalidRequestError(f"{expected_name} must contain at least one worker")
    normalized_rows = [
        {key: value or "" for key, value in row.items()}
        for row in rows
    ]
    return report_path, normalized_rows


def _validate_status_evidence(value: Any) -> tuple[Path, set[str]]:
    report_path, rows = _read_local_tsv_evidence(value, name="calculation_status")
    workers: set[str] = set()
    for row in rows:
        worker = str(row.get("worker", "")).strip()
        if not worker:
            raise InvalidRequestError("status.tsv contains a row without a worker ID")
        if str(row.get("terminal", "")).lower() != "yes" or str(row.get("result", "")).upper() not in {
            "SUCCEEDED",
            "FAILED",
        }:
            raise InvalidRequestError(
                "adaptive sampling requires every calculation worker to be terminal; "
                f"worker={worker!r}, result={row.get('result', '')!r}"
            )
        workers.add(worker)
    if len(workers) != len(rows):
        raise InvalidRequestError(f"status.tsv contains duplicate worker rows: {report_path}")
    return report_path, workers


def _validate_download_evidence(value: Any) -> tuple[Path, set[str]]:
    report_path, rows = _read_local_tsv_evidence(value, name="downloaded_results")
    workers: set[str] = set()
    for row in rows:
        worker = str(row.get("worker", "")).strip()
        if not worker:
            raise InvalidRequestError("download-results.tsv contains a row without a worker ID")
        if str(row.get("status", "")).upper() != "DOWNLOADED":
            raise InvalidRequestError(
                "adaptive sampling requires every worker result to be downloaded; "
                f"worker={worker!r}, status={row.get('status', '')!r}"
            )
        workers.add(worker)
        download_dir_value = str(row.get("download_dir", "")).strip()
        if not download_dir_value:
            raise InvalidRequestError("download-results.tsv contains a row without download_dir")
        download_dir = Path(download_dir_value)
        if not download_dir.is_absolute():
            possible_dirs = {
                download_dir.resolve(),
                (report_path.parent / download_dir).resolve(),
            }
            matching_dirs = [
                path for path in possible_dirs if (path / "download-manifest.txt").is_file()
            ]
            if len(matching_dirs) != 1:
                raise InvalidRequestError(
                    "download-results.tsv has an ambiguous or missing relative download_dir: "
                    f"{download_dir_value}"
                )
            download_dir = matching_dirs[0]
        manifest_path = download_dir / "download-manifest.txt"
        if not manifest_path.is_file():
            raise InvalidRequestError(
                f"downloaded worker has no local download-manifest.txt: {download_dir}"
            )
        try:
            entries = [line.strip() for line in manifest_path.read_text(encoding="utf-8").splitlines()]
        except (OSError, UnicodeError) as exc:
            raise InvalidRequestError(f"cannot read worker download manifest: {manifest_path}") from exc
        if not entries:
            raise InvalidRequestError(f"worker download manifest is empty: {manifest_path}")
        root = download_dir.resolve()
        for entry in entries:
            payload_path = (download_dir / entry).resolve()
            try:
                payload_path.relative_to(root)
            except ValueError as exc:
                raise InvalidRequestError(
                    f"worker download manifest points outside its download directory: {entry}"
                ) from exc
            if not payload_path.is_file():
                raise InvalidRequestError(f"downloaded worker artifact is missing: {payload_path}")
    if len(workers) != len(rows):
        raise InvalidRequestError(f"download-results.tsv contains duplicate worker rows: {report_path}")
    return report_path, workers


class AgentAuthoredAcquisitionSamplingStrategy:
    """Evaluate one agent-authored, round-specific score with a safe numeric DSL."""

    name = AGENT_ACQUISITION_STRATEGY
    version = AGENT_ACQUISITION_VERSION

    def validate_with_context(
        self,
        candidates: Sequence[Candidate],
        budget: int,
        metadata: Mapping[str, Any] | None,
    ) -> None:
        del budget
        spec = acquisition_spec(metadata or {})
        for candidate in candidates:
            product_count = _required_number(candidate, "product_count")
            atom_count = _required_number(candidate, "mean_product_atom_count", positive=True)
            novelty = _required_number(candidate, "sampled_novelty")
            if novelty > 1.0:
                raise InvalidRequestError(
                    f"candidate {candidate.candidate_id!r} sampled_novelty must be in [0, 1]"
                )
            if product_count == 0:
                raise InvalidRequestError(
                    f"candidate {candidate.candidate_id!r} must have at least one possible product"
                )
            evaluate_numeric_expression(
                str(spec["cost_expression"]),
                candidate=candidate.payload,
                context=spec["context"],  # type: ignore[arg-type]
            )
            if atom_count <= 0:
                raise InvalidRequestError("mean_product_atom_count must be positive")

    def _evaluate(
        self,
        candidates: Sequence[Candidate],
        metadata: Mapping[str, Any] | None,
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        spec = acquisition_spec(metadata or {})
        base_context = dict(spec["context"])
        runtime = (metadata or {}).get("round_context", {})
        if runtime:
            if not isinstance(runtime, Mapping):
                raise InvalidRequestError("sampling metadata.round_context must be a mapping")
            base_context["round"] = dict(runtime)
            if "observed_metrics" in runtime:
                base_context.setdefault("observed_metrics", runtime["observed_metrics"])

        costs: list[float] = []
        for candidate in candidates:
            product_count = _required_number(candidate, "product_count")
            if product_count == 0:
                raise InvalidRequestError(
                    f"candidate {candidate.candidate_id!r} must have at least one possible product"
                )
            _required_number(candidate, "mean_product_atom_count", positive=True)
            novelty = _required_number(candidate, "sampled_novelty")
            if novelty > 1.0:
                raise InvalidRequestError(
                    f"candidate {candidate.candidate_id!r} sampled_novelty must be in [0, 1]"
                )
            cost = evaluate_numeric_expression(
                str(spec["cost_expression"]),
                candidate=candidate.payload,
                context=base_context,
            )
            if cost <= 0:
                raise InvalidRequestError(
                    f"candidate {candidate.candidate_id!r} estimated cost must be positive"
                )
            costs.append(cost)

        summary = _cost_summary(costs)
        effective_context = {**base_context, "cost_summary": summary}
        entries: list[dict[str, Any]] = []
        for candidate, cost in zip(candidates, costs, strict=True):
            score = evaluate_numeric_expression(
                str(spec["expression"]),
                candidate=candidate.payload,
                context=effective_context,
                estimated_cost=cost,
            )
            entries.append(
                {
                    "candidate_id": candidate.candidate_id,
                    "score": score,
                    "estimated_cost": cost,
                }
            )

        definition = {
            "schema": AGENT_ACQUISITION_SCHEMA,
            "strategy": self.name,
            "strategy_version": self.version,
            "function_id": str(spec["function_id"]),
            "version": str(spec["version"]),
            "authored_by": str(spec["authored_by"]),
            "expression_language": "tricycle.safe-numeric-expression/v1",
            "selection": "highest-score; candidate_id ascending tie-break",
            "expression": str(spec["expression"]),
            "cost_expression": str(spec["cost_expression"]),
            "rationale": str(spec["rationale"]),
            "context": base_context,
            "effective_context": effective_context,
            "evidence": dict(spec["evidence"]),
            "author_notes": spec.get("author_notes", ""),
            "cost_summary": summary,
        }
        definition["function_hash"] = hashlib.sha256(
            canonical_json(definition).encode("utf-8")
        ).hexdigest()
        return definition, entries

    def plan_with_context(
        self,
        candidates: Sequence[Candidate],
        budget: int,
        metadata: Mapping[str, Any] | None,
    ) -> dict[str, Any]:
        if budget < 0 or budget > len(candidates):
            raise InvalidRequestError("sampling budget cannot exceed candidate count")
        definition, entries = self._evaluate(candidates, metadata)
        return {
            "score_field": "agent_acquisition_score",
            "score_fields": ["agent_acquisition.expression"] * len(entries),
            "candidate_scores": entries,
            "acquisition_function": definition,
        }

    def select_with_context(
        self,
        candidates: Sequence[Candidate],
        budget: int,
        seed: int | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> tuple[Candidate, ...]:
        del seed
        definition, entries = self._evaluate(candidates, metadata)
        del definition
        if budget == 0:
            return ()
        score_by_id = {str(item["candidate_id"]): float(item["score"]) for item in entries}
        ranked = sorted(candidates, key=lambda item: (-score_by_id[item.candidate_id], item.candidate_id))
        return tuple(ranked[:budget])

    def select(
        self,
        candidates: Sequence[Candidate],
        budget: int,
        seed: int | None = None,
    ) -> tuple[Candidate, ...]:
        del candidates, budget, seed
        raise InvalidRequestError(
            "agent-authored-acquisition requires request metadata; use SamplingPlugin.select"
        )
