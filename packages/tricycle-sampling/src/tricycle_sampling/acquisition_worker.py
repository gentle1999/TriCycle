#!/usr/bin/env python3
"""Run SAR inference for one agent-authored acquisition round inside a worker.

The worker predicts the complete remaining candidate table and a separate
labelled validation table. It never selects candidates or submits jobs. Its
outputs are intended to be downloaded and inspected locally before the next
acquisition function is authored.

This worker reuses the tricycle_sar command backend; stage that package and its
dependencies alongside the pinned RxnResid runtime in the worker environment.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import os
import subprocess
import sys
import tomllib
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from tricycle_core.io import file_sha256 as _sha256_file
from tricycle_core.io import tree_sha256 as _sha256_tree
from tricycle_core.io import write_delimited
from tricycle_core.io import write_json as _write_json
from tricycle_core.metrics import regression_metrics
from tricycle_sar.predictions import identity, validate_rows

SCHEMA = "tricycle.agent-acquisition-worker/v1"
REQUIRED_SIGNAL_FIELDS = (
    "prediction",
    "epistemic_variance",
    "aleatoric_variance",
    "predictive_variance",
)
OPTIONAL_SIGNAL_FIELDS = (
    "epistemic_std",
    "aleatoric_std",
    "predictive_std",
    "lower_95",
    "upper_95",
)


class WorkerInputError(ValueError):
    """Raised when the worker input or model output violates its contract."""




def _read_csv(path: Path) -> tuple[list[str], list[dict[str, str]]]:
    if not path.is_file():
        raise WorkerInputError(f"CSV input does not exist: {path}")
    try:
        with path.open(encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            if reader.fieldnames is None or not reader.fieldnames:
                raise WorkerInputError(f"CSV input has no header: {path}")
            fields = [str(field) for field in reader.fieldnames]
            if len(fields) != len(set(fields)):
                raise WorkerInputError(f"CSV input has duplicate header fields: {path}")
            rows: list[dict[str, str]] = []
            for line_number, row in enumerate(reader, start=2):
                if None in row:
                    raise WorkerInputError(f"CSV row has too many values at {path}:{line_number}")
                rows.append({key: value or "" for key, value in row.items()})
    except (OSError, UnicodeError, csv.Error) as exc:
        raise WorkerInputError(f"cannot read CSV input {path}: {exc}") from exc
    if not rows:
        raise WorkerInputError(f"CSV input is empty: {path}")
    return fields, rows


def _write_csv(path: Path, fields: Sequence[str], rows: Sequence[Mapping[str, object]]) -> None:
    write_delimited(path, fields, rows)




def _required_text(request: Mapping[str, Any], key: str) -> str:
    value = request.get(key)
    if not isinstance(value, str) or not value.strip():
        raise WorkerInputError(f"{key} must be a non-empty string")
    return value.strip()


def _resolve_file(base: Path, request: Mapping[str, Any], key: str) -> Path:
    value = _required_text(request, key)
    path = Path(value)
    if not path.is_absolute():
        path = base / path
    path = path.resolve()
    if not path.is_file():
        raise WorkerInputError(f"{key} is not a file: {path}")
    return path


def _resolve_directory(base: Path, request: Mapping[str, Any], key: str) -> Path:
    value = _required_text(request, key)
    path = Path(value)
    if not path.is_absolute():
        path = base / path
    path = path.resolve()
    if not path.is_dir():
        raise WorkerInputError(f"{key} is not a directory: {path}")
    return path


def _read_request(path: Path) -> dict[str, Any]:
    try:
        with path.open("rb") as handle:
            request = tomllib.load(handle)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise WorkerInputError(f"cannot read worker request {path}: {exc}") from exc
    if request.get("schema") != SCHEMA:
        raise WorkerInputError(f"schema must be {SCHEMA!r}")
    return request


def _column_list(
    request: Mapping[str, Any],
    key: str,
    *,
    default: Sequence[str],
) -> tuple[str, ...]:
    value = request.get(key, list(default))
    if not isinstance(value, (list, tuple)) or not value:
        raise WorkerInputError(f"{key} must be a non-empty list of CSV column names")
    result = tuple(str(item).strip() for item in value)
    if any(not item for item in result) or len(result) != len(set(result)):
        raise WorkerInputError(f"{key} must contain distinct non-empty column names")
    return result


def _identity_key(row: Mapping[str, str], columns: Sequence[str], *, path: Path) -> tuple[str, ...]:
    values = tuple(str(row.get(column, "")).strip() for column in columns)
    if any(not value for value in values):
        raise WorkerInputError(
            f"missing identity column(s) {', '.join(columns)} in {path}"
        )
    return values


def _identity_digest(rows: Sequence[Mapping[str, str]], column: str, *, path: Path) -> str:
    identities = sorted(str(row.get(column, "")).strip() for row in rows)
    if any(not value for value in identities):
        raise WorkerInputError(f"blank {column!r} values in {path}")
    if len(identities) != len(set(identities)):
        raise WorkerInputError(f"duplicate {column!r} values in {path}")
    material = json.dumps(identities, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def _prediction_command(
    *,
    python: str,
    script: Path,
    checkpoint: Path,
    data: Path,
    output: Path,
    batch_size: int | None,
    cpu: bool,
    mixed_precision: str | None,
    component_id_columns: Sequence[str],
) -> list[str]:
    from tricycle_sar import get_backend

    return list(get_backend("rxnresid").inference_command(
        python_executable=python, script=str(script), checkpoint=str(checkpoint),
        data=str(data), output=str(output), batch_size=batch_size, cpu=cpu,
        mixed_precision=mixed_precision, component_id_columns=component_id_columns,
    ))


def _run_prediction(
    *,
    name: str,
    command: Sequence[str],
    source_directory: Path,
    source_python_paths: Sequence[Path],
    output_csv: Path,
    log_path: Path,
) -> None:
    output_csv.parent.mkdir(parents=True, exist_ok=True)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    environment = os.environ.copy()
    search_paths = [str(path) for path in source_python_paths]
    if environment.get("PYTHONPATH"):
        search_paths.append(environment["PYTHONPATH"])
    environment["PYTHONPATH"] = os.pathsep.join(search_paths)
    with log_path.open("w", encoding="utf-8") as log:
        log.write("$ " + " ".join(str(part) for part in command) + "\n\n")
        log.flush()
        try:
            completed = subprocess.run(
                list(command),
                cwd=source_directory,
                env=environment,
                stdout=log,
                stderr=subprocess.STDOUT,
                check=False,
            )
        except OSError as exc:
            raise WorkerInputError(f"cannot start RxnResid {name} inference: {exc}") from exc
    if completed.returncode != 0:
        raise WorkerInputError(
            f"RxnResid {name} inference failed with exit code {completed.returncode}; "
            f"see {log_path}"
        )
    if not output_csv.is_file():
        raise WorkerInputError(f"RxnResid {name} inference produced no CSV: {output_csv}")


def _validate_prediction_rows(
    *,
    path: Path,
    expected_rows: Sequence[Mapping[str, str]],
    identity_columns: Sequence[str],
    required_fields: Sequence[str],
) -> tuple[list[str], list[dict[str, str]]]:
    fields, rows = _read_csv(path)
    missing_fields = [
        field
        for field in (*identity_columns, *required_fields)
        if field not in fields
    ]
    if missing_fields:
        raise WorkerInputError(
            f"{path} is missing required prediction columns: {', '.join(missing_fields)}"
        )
    try:
        validate_rows(rows, expected=expected_rows, columns=identity_columns,
                      required=required_fields, unique=True)
    except ValueError as exc:
        raise WorkerInputError(f"invalid prediction output {path}: {exc}") from exc
    return fields, sorted(rows, key=lambda row: identity(row, identity_columns))


def _candidate_signal_rows(
    *,
    candidate_fields: Sequence[str],
    candidate_rows: Sequence[Mapping[str, str]],
    prediction_fields: Sequence[str],
    prediction_rows: Sequence[Mapping[str, str]],
    candidate_id_column: str,
    identity_columns: Sequence[str],
    model_snapshot_id: str,
) -> tuple[list[str], list[dict[str, str]]]:
    excluded = {"target", *identity_columns}
    signals = [
        field
        for field in (*REQUIRED_SIGNAL_FIELDS, *OPTIONAL_SIGNAL_FIELDS)
        if field in prediction_fields and field not in excluded
    ]
    missing = [field for field in REQUIRED_SIGNAL_FIELDS if field not in signals]
    if missing:
        raise WorkerInputError(
            "candidate predictions do not provide required signal fields: " + ", ".join(missing)
        )
    candidate_by_key: dict[tuple[str, ...], Mapping[str, str]] = {}
    for row in candidate_rows:
        key = _identity_key(row, identity_columns, path=Path("candidate_space"))
        if key in candidate_by_key:
            raise WorkerInputError(f"candidate space has duplicate identity {key!r}")
        candidate_by_key[key] = row
    prediction_by_key = {
        _identity_key(row, identity_columns, path=Path("predictions")): row
        for row in prediction_rows
    }
    stale_signal_fields = {
        *REQUIRED_SIGNAL_FIELDS,
        *OPTIONAL_SIGNAL_FIELDS,
        "model_snapshot_id",
        "target",
    }
    feature_fields = [field for field in candidate_fields if field not in stale_signal_fields]
    output_fields = [*feature_fields, *signals, "model_snapshot_id"]
    output_rows: list[dict[str, str]] = []
    for key in sorted(candidate_by_key):
        candidate = candidate_by_key[key]
        prediction = prediction_by_key[key]
        merged = {field: candidate.get(field, "") for field in feature_fields}
        merged.update({field: prediction[field] for field in signals})
        merged["model_snapshot_id"] = model_snapshot_id
        output_rows.append(merged)
    if candidate_id_column not in output_fields:
        raise WorkerInputError(f"candidate output is missing ID column {candidate_id_column!r}")
    return output_fields, output_rows


def _validation_metrics(
    *,
    validation_rows: Sequence[Mapping[str, str]],
    validation_predictions: Sequence[Mapping[str, str]],
    validation_id_column: str,
    target_column: str,
    validation_role: str,
    target_unit: str,
) -> dict[str, Any]:
    targets: dict[str, float] = {}
    predictions: dict[str, float] = {}
    for row in validation_rows:
        identity = str(row.get(validation_id_column, "")).strip()
        if not identity or identity in targets:
            raise WorkerInputError(
                f"validation data has a blank or duplicate {validation_id_column!r}"
            )
        try:
            target = float(row[target_column])
        except (KeyError, TypeError, ValueError) as exc:
            raise WorkerInputError(f"invalid validation target {target_column!r}") from exc
        if not math.isfinite(target):
            raise WorkerInputError(f"non-finite validation target {target_column!r}")
        targets[identity] = target
    for row in validation_predictions:
        identity = str(row.get(validation_id_column, "")).strip()
        if not identity or identity in predictions:
            raise WorkerInputError(
                f"validation predictions have a blank or duplicate {validation_id_column!r}"
            )
        try:
            prediction = float(row["prediction"])
        except (KeyError, TypeError, ValueError) as exc:
            raise WorkerInputError(f"invalid validation prediction for {identity!r}") from exc
        if not math.isfinite(prediction):
            raise WorkerInputError(f"non-finite validation prediction for {identity!r}")
        predictions[identity] = prediction
    if set(targets) != set(predictions):
        missing = sorted(set(targets) - set(predictions))[:5]
        extra = sorted(set(predictions) - set(targets))[:5]
        raise WorkerInputError(
            "validation prediction IDs do not match validation targets; "
            f"missing={missing}, extra={extra}"
        )
    keys = sorted(targets)
    metrics = regression_metrics([targets[key] for key in keys], [predictions[key] for key in keys])
    return {
        "validation_role": validation_role,
        "target_column": target_column,
        "target_unit": target_unit,
        "metric_direction": "lower-is-better",
        "sample_count": metrics["n"],
        "validation_mae": metrics["mae"],
        "validation_rmse": metrics["rmse"],
    }


def run_worker(request_path: Path) -> dict[str, Any]:
    request_path = request_path.resolve()
    request = _read_request(request_path)
    base = request_path.parent

    model_snapshot_id = _required_text(request, "model_snapshot_id")
    source_revision = _required_text(request, "source_revision")
    source_directory = _resolve_directory(base, request, "source_directory")
    from tricycle_sar.sampling_runtime import verify as verify_runtime

    try:
        backend = verify_runtime(base)
    except (OSError, ValueError) as exc:
        raise WorkerInputError(f"shared SAR runtime validation failed: {exc}") from exc
    if source_revision != backend["source_revision"] or source_directory != base / "rxnresid":
        raise WorkerInputError("worker source differs from the shared SAR backend")
    checkpoint = _resolve_file(base, request, "checkpoint")
    candidate_space = _resolve_file(base, request, "candidate_space")
    validation_data = _resolve_file(base, request, "validation_data")
    predict_script_name = str(request.get("predict_script", "predict.py")).strip()
    predict_script = Path(predict_script_name)
    if not predict_script.is_absolute():
        predict_script = source_directory / predict_script
    predict_script = predict_script.resolve()
    if not predict_script.is_file():
        raise WorkerInputError(f"RxnResid prediction script is missing: {predict_script}")
    if predict_script != source_directory / "predict.py":
        raise WorkerInputError("prediction entrypoint differs from the shared SAR backend")

    candidate_id_column = str(request.get("candidate_id_column", "candidate_id")).strip()
    validation_id_column = str(request.get("validation_id_column", "validation_id")).strip()
    target_column = str(request.get("validation_target_column", "target")).strip()
    target_unit = _required_text(request, "validation_target_unit")
    validation_role = str(request.get("validation_role", "")).strip()
    if not candidate_id_column or not validation_id_column or not target_column:
        raise WorkerInputError("candidate and validation ID/target columns must not be empty")
    if validation_role not in {"held-out", "cross-validation"}:
        raise WorkerInputError("validation_role must be 'held-out' or 'cross-validation'")

    candidate_fields, candidate_rows = _read_csv(candidate_space)
    if candidate_id_column not in candidate_fields:
        raise WorkerInputError(f"candidate space is missing {candidate_id_column!r}")
    _identity_digest(candidate_rows, candidate_id_column, path=candidate_space)
    candidate_component_columns = _column_list(
        request,
        "component_id_columns",
        default=(candidate_id_column,),
    )
    if candidate_id_column not in candidate_component_columns:
        raise WorkerInputError("component_id_columns must include candidate_id_column")
    for column in candidate_component_columns:
        if column not in candidate_fields:
            raise WorkerInputError(f"candidate space is missing component ID column {column!r}")
    for row in candidate_rows:
        _identity_key(row, candidate_component_columns, path=candidate_space)

    validation_fields, validation_rows = _read_csv(validation_data)
    if validation_id_column not in validation_fields or target_column not in validation_fields:
        raise WorkerInputError(
            f"validation data must contain {validation_id_column!r} and {target_column!r}"
        )
    _identity_digest(validation_rows, validation_id_column, path=validation_data)
    validation_component_columns = _column_list(
        request,
        "validation_component_id_columns",
        default=(validation_id_column,),
    )
    if validation_id_column not in validation_component_columns:
        raise WorkerInputError("validation_component_id_columns must include validation_id_column")
    for column in validation_component_columns:
        if column not in validation_fields:
            raise WorkerInputError(f"validation data is missing component ID column {column!r}")
    for row in validation_rows:
        _identity_key(row, validation_component_columns, path=validation_data)

    batch_size_value = request.get("batch_size")
    try:
        batch_size = int(batch_size_value) if batch_size_value not in (None, "") else None
    except (TypeError, ValueError) as exc:
        raise WorkerInputError("batch_size must be a positive integer") from exc
    if batch_size is not None and batch_size < 1:
        raise WorkerInputError("batch_size must be positive")
    cpu = bool(request.get("cpu", False))
    mixed_precision_value = request.get("mixed_precision")
    mixed_precision = str(mixed_precision_value).strip() if mixed_precision_value else None
    if mixed_precision not in {None, "no", "fp16", "bf16"}:
        raise WorkerInputError("mixed_precision must be no, fp16, bf16, or empty")
    python = str(request.get("python_executable", "python3.12")).strip()
    if not python:
        raise WorkerInputError("python_executable must not be empty")

    output_value = _required_text(request, "output_directory")
    output_directory = Path(output_value)
    if not output_directory.is_absolute():
        output_directory = base / output_directory
    output_directory = output_directory.resolve()
    output_directory.mkdir(parents=True, exist_ok=True)
    output_paths = (
        output_directory / "candidate-model-signals.csv",
        output_directory / "validation-predictions.csv",
        output_directory / "model-metrics.json",
        output_directory / "worker-report.json",
    )
    existing = [path for path in output_paths if path.exists()]
    if existing:
        raise WorkerInputError(
            "worker output already exists; use a fresh output directory: "
            + ", ".join(str(path) for path in existing)
        )

    temporary_candidate = output_directory / ".candidate-predictions.raw.csv"
    temporary_validation = output_directory / ".validation-predictions.raw.csv"
    candidate_command = _prediction_command(
        python=python,
        script=predict_script,
        checkpoint=checkpoint,
        data=candidate_space,
        output=temporary_candidate,
        batch_size=batch_size,
        cpu=cpu,
        mixed_precision=mixed_precision,
        component_id_columns=candidate_component_columns,
    )
    validation_command = _prediction_command(
        python=python,
        script=predict_script,
        checkpoint=checkpoint,
        data=validation_data,
        output=temporary_validation,
        batch_size=batch_size,
        cpu=cpu,
        mixed_precision=mixed_precision,
        component_id_columns=validation_component_columns,
    )
    source_python_paths = (source_directory / "src", source_directory)
    _run_prediction(
        name="remaining candidate",
        command=candidate_command,
        source_directory=source_directory,
        source_python_paths=source_python_paths,
        output_csv=temporary_candidate,
        log_path=output_directory / "candidate-inference.log",
    )
    candidate_prediction_fields, candidate_predictions = _validate_prediction_rows(
        path=temporary_candidate,
        expected_rows=candidate_rows,
        identity_columns=candidate_component_columns,
        required_fields=REQUIRED_SIGNAL_FIELDS,
    )
    signal_fields, signal_rows = _candidate_signal_rows(
        candidate_fields=candidate_fields,
        candidate_rows=candidate_rows,
        prediction_fields=candidate_prediction_fields,
        prediction_rows=candidate_predictions,
        candidate_id_column=candidate_id_column,
        identity_columns=candidate_component_columns,
        model_snapshot_id=model_snapshot_id,
    )
    candidate_output = output_paths[0]
    _write_csv(candidate_output, signal_fields, signal_rows)
    temporary_candidate.unlink(missing_ok=True)

    _run_prediction(
        name="validation",
        command=validation_command,
        source_directory=source_directory,
        source_python_paths=source_python_paths,
        output_csv=temporary_validation,
        log_path=output_directory / "validation-inference.log",
    )
    validation_prediction_fields, validation_predictions = _validate_prediction_rows(
        path=temporary_validation,
        expected_rows=validation_rows,
        identity_columns=validation_component_columns,
        required_fields=("prediction",),
    )
    validation_metrics = _validation_metrics(
        validation_rows=validation_rows,
        validation_predictions=validation_predictions,
        validation_id_column=validation_id_column,
        target_column=target_column,
        validation_role=validation_role,
        target_unit=target_unit,
    )
    targets_by_id = {
        row[validation_id_column]: row[target_column]
        for row in validation_rows
    }
    validation_output_rows = [
        {
            **{column: row[column] for column in validation_component_columns},
            "prediction": row["prediction"],
            "target": targets_by_id[row[validation_id_column]],
            **{
                column: row[column]
                for column in (*REQUIRED_SIGNAL_FIELDS, *OPTIONAL_SIGNAL_FIELDS)
                if column in validation_prediction_fields
            },
        }
        for row in validation_predictions
    ]
    validation_output_fields = list(
        dict.fromkeys(
            [*validation_component_columns, "prediction", "target"]
            + [
                column
                for column in (*REQUIRED_SIGNAL_FIELDS, *OPTIONAL_SIGNAL_FIELDS)
                if column in validation_prediction_fields
            ]
        )
    )
    validation_output = output_paths[1]
    _write_csv(validation_output, validation_output_fields, validation_output_rows)
    temporary_validation.unlink(missing_ok=True)

    metrics_path = output_paths[2]
    _write_json(
        metrics_path,
        {
            "schema": "tricycle.agent-acquisition-model-metrics/v1",
            "model_snapshot_id": model_snapshot_id,
            **validation_metrics,
        },
    )
    source_tree_hash = _sha256_tree(source_directory)
    candidate_ids_hash = _identity_digest(
        candidate_rows,
        candidate_id_column,
        path=candidate_space,
    )
    report_path = output_paths[3]
    report = {
        "schema": SCHEMA,
        "worker_script_sha256": _sha256_file(Path(__file__).resolve()),
        "worker_request_sha256": _sha256_file(request_path),
        "model_snapshot_id": model_snapshot_id,
        "source_revision": source_revision,
        "source_revision_verified": True,
        "sar_backend": backend,
        "source_tree_sha256": source_tree_hash,
        "predict_script": (
            predict_script.relative_to(source_directory).as_posix()
            if predict_script.is_relative_to(source_directory)
            else str(predict_script)
        ),
        "predict_script_sha256": _sha256_file(predict_script),
        "checkpoint_sha256": _sha256_file(checkpoint),
        "candidate_space_sha256": _sha256_file(candidate_space),
        "candidate_id_column": candidate_id_column,
        "candidate_ids_sha256": candidate_ids_hash,
        "candidate_count": len(candidate_rows),
        "validation_data_sha256": _sha256_file(validation_data),
        "inference_configuration": {
            "python_executable": python,
            "batch_size": batch_size,
            "cpu": cpu,
            "mixed_precision": mixed_precision or "",
            "candidate_component_id_columns": list(candidate_component_columns),
            "validation_component_id_columns": list(validation_component_columns),
        },
        "validation_role": validation_role,
        "validation_metrics": validation_metrics,
        "candidate_signal_fields": [
            field for field in (*REQUIRED_SIGNAL_FIELDS, *OPTIONAL_SIGNAL_FIELDS)
            if field in signal_fields
        ],
        "outputs": {
            candidate_output.name: _sha256_file(candidate_output),
            validation_output.name: _sha256_file(validation_output),
            metrics_path.name: _sha256_file(metrics_path),
        },
        "selection_performed": False,
        "jobs_submitted": False,
        "worker_report_file": report_path.name,
    }
    _write_json(report_path, report)
    return {**report, "report_path": str(report_path)}


def prepare_worker(root: Path) -> dict[str, Any]:
    """Deliver the current package runtime and its shared initialized launcher."""
    from tricycle_sar.sampling_runtime import stage, worker_launcher

    root = root.resolve()
    backend = stage(root)
    launcher = root / "run.sh"
    launcher.write_text(worker_launcher(module="tricycle_sampling.acquisition_worker"))
    launcher.chmod(0o755)
    return {"root": str(root), "launcher": str(launcher), "sar_backend": backend}


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--request", type=Path, help="worker TOML request")
    action.add_argument("--prepare-bundle", type=Path, help="stage a local worker without execution")
    args = parser.parse_args(argv)
    if args.prepare_bundle is not None:
        print(json.dumps(prepare_worker(args.prepare_bundle), ensure_ascii=False))
        return 0
    try:
        report = run_worker(args.request)
    except WorkerInputError as exc:
        print(f"[agent-acquisition-worker] {exc}", file=sys.stderr)
        return 2
    print(
        json.dumps(
            {
                "worker_report": report["report_path"],
                "candidate_count": report["candidate_count"],
                "model_snapshot_id": report["model_snapshot_id"],
                "validation_metrics": report["validation_metrics"],
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
