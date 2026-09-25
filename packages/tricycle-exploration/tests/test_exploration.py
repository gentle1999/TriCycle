import csv
import hashlib
import json
import tomllib
import unittest
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory

from tricycle_core import ExecutionContext, InvalidRequestError, TaskStatus, TraceStore
from tricycle_exploration import (
    ExplorationPlugin,
    ExplorationRoundRequest,
    ModelSnapshot,
    RangePolicyConfig,
    SpaceDefinition,
    TrainingPolicyConfig,
    decide_range,
    decide_training,
    resolve_snapshot_transition,
)
from tricycle_sampling import Candidate


class TraceStoreTests(unittest.TestCase):
    def test_events_are_hash_chained_and_tamper_evident(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            timestamps = iter(("2026-01-01T00:00:00+00:00", "2026-01-01T00:00:01+00:00"))
            identifiers = iter(("event-1", "event-2"))
            store = TraceStore(
                root,
                run_id="run-1",
                clock=lambda: next(timestamps),
                event_id_factory=lambda: next(identifiers),
            )

            first = store.emit("run.started", payload={"request_hash": "abc"}, status="started")
            second = store.emit("step.completed", payload={"value": 3}, status="succeeded")

            self.assertEqual((first.seq, second.seq), (1, 2))
            self.assertEqual(second.prev_hash, first.event_hash)
            verification = store.verify()
            self.assertEqual(verification.event_count, 2)
            self.assertEqual(verification.last_hash, second.event_hash)

            records = [
                json.loads(line)
                for line in store.events_path.read_text(encoding="utf-8").splitlines()
            ]
            records[1]["payload"]["value"] = 4
            store.events_path.write_text(
                "\n".join(
                    json.dumps(item, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
                    for item in records
                )
                + "\n",
                encoding="utf-8",
            )
            with self.assertRaises(InvalidRequestError):
                store.verify()


class PolicyTests(unittest.TestCase):
    def test_range_policy_records_explicit_next_space(self) -> None:
        current = SpaceDefinition("space-1", definition={"temperature": [300, 500]})
        next_space = SpaceDefinition("space-2", definition={"temperature": [350, 450]})
        decision = decide_range(
            RangePolicyConfig(policy_id="manual-range", mode="explicit", rationale="narrowed"),
            current,
            next_space,
            {"selected_yield": 0.42},
        )

        self.assertEqual(decision.next_space, next_space)
        self.assertEqual(decision.method, "explicit")
        self.assertEqual(decision.observed_metrics["selected_yield"], 0.42)
        self.assertEqual(decision.to_dict()["decision_hash"], decision.decision_hash)

        with self.assertRaises(InvalidRequestError):
            decide_range(
                RangePolicyConfig(mode="explicit"),
                current,
                None,
                {},
            )

    def test_training_gate_can_skip_then_schedule_train_or_finetune(self) -> None:
        policy = TrainingPolicyConfig(
            mode="every_n_or_new_labels",
            interval=3,
            min_new_labels=2,
            train_mode="auto",
        )
        skipped = decide_training(
            policy,
            round_index=2,
            last_training_round=1,
            new_label_count=1,
            dataset_before_hash="a",
            dataset_after_hash="a",
            previous_checkpoint="",
        )
        trained = decide_training(
            policy,
            round_index=3,
            last_training_round=1,
            new_label_count=2,
            dataset_before_hash="a",
            dataset_after_hash="b",
            previous_checkpoint="",
        )
        finetuned = decide_training(
            policy,
            round_index=4,
            last_training_round=1,
            new_label_count=0,
            dataset_before_hash="b",
            dataset_after_hash="b",
            previous_checkpoint="checkpoint-1",
        )

        self.assertEqual(skipped.action, "skip")
        self.assertEqual(skipped.reason_code, "training_conditions_not_met")
        self.assertEqual(trained.next_capability, "rits.train")
        self.assertEqual(finetuned.next_capability, "rits.finetune")

    def test_any_source_snapshot_has_a_stable_training_successor_key(self) -> None:
        source = ModelSnapshot(
            "arbitrary-source-snapshot",
            checkpoint_hash="sha256:source",
            metadata={"provider": "test"},
        )
        decision = decide_training(
            TrainingPolicyConfig(mode="always", train_mode="train"),
            round_index=1,
            last_training_round=-1,
            new_label_count=0,
            dataset_before_hash="dataset-a",
            dataset_after_hash="dataset-b",
            previous_checkpoint="",
        )
        training_spec = {
            "seed": 17,
            "dataset_manifest_hash": "sha256:dataset-b",
            "code_revision": "rits-revision-1",
            "container_digest": "sha256:container",
        }
        first = resolve_snapshot_transition(
            source,
            decision,
            round_decision_hash="sha256:round-decision",
            dataset_before_hash="dataset-a",
            dataset_after_hash="dataset-b",
            training_spec=training_spec,
        )
        second = resolve_snapshot_transition(
            source,
            decision,
            round_decision_hash="sha256:round-decision",
            dataset_before_hash="dataset-a",
            dataset_after_hash="dataset-b",
            training_spec=training_spec,
        )

        self.assertEqual(first.status, "planned")
        self.assertEqual(first.replay_key, second.replay_key)
        self.assertEqual(first.next_snapshot, second.next_snapshot)
        self.assertTrue(first.next_snapshot.snapshot_id.startswith("planned-train-"))

        realized = resolve_snapshot_transition(
            source,
            decision,
            round_decision_hash="sha256:round-decision",
            dataset_before_hash="dataset-a",
            dataset_after_hash="dataset-b",
            training_spec=training_spec,
            next_snapshot=ModelSnapshot("rits-output-snapshot", checkpoint_hash="sha256:output"),
        )
        self.assertEqual(realized.status, "realized")
        self.assertEqual(realized.replay_key, first.replay_key)


class ExplorationRoundTests(unittest.TestCase):
    def test_agent_authored_function_is_part_of_round_artifacts_and_resume_check(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            output_dir = root / "adaptive-round"
            downloaded_dir = output_dir / "downloads" / "worker-001"
            downloaded_dir.mkdir(parents=True)
            (downloaded_dir / "result.tsv").write_text("completed\n", encoding="utf-8")
            (downloaded_dir / "download-manifest.txt").write_text("result.tsv\n", encoding="utf-8")
            download_report = output_dir / "download-results.tsv"
            download_report.write_text(
                "worker\ttasks\tjob_id\tjob_name\tstatus\tdownload_dir\n"
                f"worker-001\t1\tbohr-1\tjob-1\tDOWNLOADED\t{downloaded_dir}\n",
                encoding="utf-8",
            )
            status_report = output_dir / "status.tsv"
            status_report.write_text(
                "worker\ttasks\tjob_id\tjob_name\tsubmit_status\tjob_status\tterminal\tresult\tinput_dir\tchecked_at\n"
                "worker-001\t1\tbohr-1\tjob-1\tSUBMITTED\tCOMPLETED\tyes\tSUCCEEDED\tinputs\t2026-09-24\n",
                encoding="utf-8",
            )
            request = {
                "exploration_id": "adaptive",
                "round_index": 1,
                "strategy": "agent-authored-acquisition",
                "budget": 1,
                "seed": 9,
                "output_dir": str(output_dir),
                "space": {"space_id": "adaptive-space", "definition": {"reaction_class": "DA"}},
                "model_snapshot": {
                    "snapshot_id": "sar-model-1",
                    "checkpoint_hash": "sha256:model-checkpoint",
                    "score_artifact": "scores.tsv",
                },
                "observed_metrics": {"validation_mae": 0.2},
                "candidates": [
                    {
                        "candidate_id": "rxn-a",
                        "payload": {
                        "epistemic_variance": 0.9,
                        "product_count": 2,
                        "mean_product_atom_count": 10,
                        "sampled_novelty": 0.9,
                        },
                    },
                    {
                        "candidate_id": "rxn-b",
                        "payload": {
                        "epistemic_variance": 0.8,
                        "product_count": 4,
                        "mean_product_atom_count": 5,
                        "sampled_novelty": 0.3,
                        },
                    },
                ],
                "metadata": {
                    "agent_acquisition": {
                        "schema": "tricycle.agent-acquisition/v1",
                        "function_id": "adaptive-round-0001",
                        "version": "1.0.0",
                        "authored_by": "agent-authored-acquisition",
                        "expression": (
                            "epistemic_variance / estimated_cost + context.model_metrics.validation_mae "
                            "* (sampled_novelty + context.sampled_distribution.coverage_gap) / estimated_cost"
                        ),
                        "cost_expression": "product_count * mean_product_atom_count",
                        "rationale": "Balance uncertainty and under-sampled regions against estimated cost.",
                        "context": {
                            "sampled_distribution": {
                                "coverage_gap": 0.3,
                                "sampled_count": 5,
                                "novelty_definition": "nearest-labeled-pair-distance-normalized-v1",
                            },
                            "model_metrics": {"validation_mae": 0.2},
                        },
                        "evidence": {
                            "sampled_set": "sha256:sampled-set",
                            "candidate_space": "sha256:candidate-space",
                            "model_evaluation": "sha256:model-evaluation",
                            "calculation_status": {
                                "manifest_path": str(status_report.resolve()),
                                "sha256": hashlib.sha256(status_report.read_bytes()).hexdigest(),
                            },
                            "downloaded_results": {
                                "manifest_path": str(download_report.resolve()),
                                "sha256": hashlib.sha256(download_report.read_bytes()).hexdigest(),
                            },
                        },
                    }
                },
            }
            trace = TraceStore(output_dir, run_id="adaptive-run")
            context = ExecutionContext(
                run_id="adaptive-run",
                workdir=root,
                metadata={"run_dir": str(output_dir)},
                trace_sink=trace,
            )

            result = ExplorationPlugin().execute_round(request, context)

            self.assertEqual(result.metadata["selected_ids"], ["rxn-a"])
            round_dir = output_dir / "rounds" / "0001"
            function_path = round_dir / "acquisition-function.toml"
            function = tomllib.loads(function_path.read_text(encoding="utf-8"))
            self.assertEqual(function["function_id"], "adaptive-round-0001")
            self.assertEqual(len(function["function_hash"]), 64)
            self.assertIn("acquisition-function.toml", function_path.name)

            manifest = tomllib.loads(
                (round_dir / "artifacts" / "manifest.toml").read_text(encoding="utf-8")
            )
            artifact_paths = {
                manifest[f"artifact_{index:03d}"]["path"]
                for index in range(manifest["count"])
            }
            self.assertIn("rounds/0001/acquisition-function.toml", artifact_paths)
            self.assertIn(
                "acquisition.function.authored",
                [event["event_type"] for event in trace.read()],
            )

            reused = ExplorationPlugin().execute_round({**request, "resume": True}, context)
            self.assertEqual(reused.metadata["acquisition_function_id"], "adaptive-round-0001")

    def test_round_materializes_strict_trajectory_and_is_reusable(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            output_dir = root / "run"
            trace = TraceStore(output_dir, run_id="run-1")
            request = ExplorationRoundRequest(
                exploration_id="demo",
                round_index=1,
                candidates=(
                    Candidate("rxn-a", {"epistemic_unc": 0.2}),
                    Candidate("rxn-b", {"epistemic_unc": 0.9}),
                    Candidate("rxn-c", {"epistemic_unc": 0.5}),
                    Candidate("rxn-d", {"epistemic_unc": 0.8}),
                ),
                budget=2,
                strategy="epistemic-uncertainty",
                seed=42,
                space=SpaceDefinition("space-1", definition={"temperature": [300, 500]}),
                range_policy=RangePolicyConfig(mode="fixed"),
                model_snapshot=ModelSnapshot(
                    "rits-snapshot-1",
                    checkpoint_hash="sha256:checkpoint",
                    source_run_id="rits-run-1",
                ),
                training_policy=TrainingPolicyConfig(mode="new_labels", min_new_labels=2),
                new_label_count=1,
                excluded_candidates={"rxn-d": "already_labeled"},
                output_dir=output_dir,
            )
            self.assertEqual(
                request.request_hash,
                replace(
                    request,
                    next_model_snapshot=ModelSnapshot(
                        "rits-output", checkpoint_hash="sha256:output"
                    ),
                ).request_hash,
            )
            context = ExecutionContext(
                run_id="run-1",
                workdir=root,
                metadata={"run_dir": str(output_dir)},
                trace_sink=trace,
            )

            result = ExplorationPlugin().execute_round(request, context)
            self.assertEqual(result.status, TaskStatus.SUCCEEDED)
            self.assertEqual(result.metadata["selected_ids"], ["rxn-b", "rxn-c"])
            self.assertEqual(result.metadata["training_decision"]["action"], "skip")

            round_dir = output_dir / "rounds" / "0001"
            expected_files = {
                "space.toml",
                "candidate-pool.tsv",
                "model-scores.tsv",
                "selection-decisions.tsv",
                "selected-candidates.tsv",
                "range-decision.toml",
                "training-decision.toml",
                "snapshot-transition.toml",
                "round.toml",
                "artifacts/manifest.toml",
            }
            self.assertEqual(
                {str(path.relative_to(round_dir)) for path in round_dir.rglob("*") if path.is_file()},
                expected_files,
            )

            round_state = tomllib.loads((round_dir / "round.toml").read_text(encoding="utf-8"))
            self.assertEqual(round_state["selected_ids_json"], '["rxn-b","rxn-c"]')
            self.assertEqual(round_state["training_action"], "skip")
            range_state = tomllib.loads(
                (round_dir / "range-decision.toml").read_text(encoding="utf-8")
            )
            self.assertEqual(range_state["input_space_hash"], round_state["space_hash"])
            self.assertEqual(range_state["next_space_hash"], round_state["space_hash"])
            snapshot_state = tomllib.loads(
                (round_dir / "snapshot-transition.toml").read_text(encoding="utf-8")
            )
            self.assertEqual(snapshot_state["action"], "skip")
            self.assertEqual(snapshot_state["status"], "carried_forward")
            self.assertEqual(snapshot_state["source_snapshot_hash"], snapshot_state["next_snapshot_hash"])
            self.assertEqual(snapshot_state["next_snapshot_json"], snapshot_state["source_snapshot_json"])

            with (round_dir / "selection-decisions.tsv").open(encoding="utf-8", newline="") as handle:
                decisions = list(csv.DictReader(handle, delimiter="\t"))
            by_id = {row["candidate_id"]: row for row in decisions}
            self.assertEqual(by_id["rxn-d"]["exclusion_reason"], "already_labeled")
            self.assertEqual(by_id["rxn-d"]["decision_reason"], "already_labeled")
            self.assertEqual(by_id["rxn-a"]["decision_reason"], "outside_budget")
            self.assertEqual(by_id["rxn-b"]["selection_order"], "1")
            self.assertEqual(by_id["rxn-c"]["selection_order"], "2")

            event_types = [event["event_type"] for event in trace.read()]
            self.assertEqual(
                event_types,
                [
                    "round.started",
                    "space.materialized",
                    "candidate.pool.materialized",
                    "model.scored",
                    "sampling.selected",
                    "range.decided",
                    "training.decided",
                    "snapshot.transitioned",
                    "round.closed",
                ],
            )
            self.assertTrue(trace.verify().valid)

            selected_bytes = (round_dir / "selected-candidates.tsv").read_bytes()
            (round_dir / "selected-candidates.tsv").write_bytes(selected_bytes + b"tampered\n")
            with self.assertRaises(InvalidRequestError):
                ExplorationPlugin().execute_round(replace(request, resume=True), context)
            (round_dir / "selected-candidates.tsv").write_bytes(selected_bytes)

            reused = ExplorationPlugin().execute_round(replace(request, resume=True), context)
            self.assertEqual(reused.status, TaskStatus.SUCCEEDED)
            self.assertEqual(reused.metadata["selected_ids"], ["rxn-b", "rxn-c"])
            self.assertEqual(reused.metadata["snapshot_replay_key"], snapshot_state["replay_key"])
            self.assertEqual(trace.read()[-1]["event_type"], "round.reused")


if __name__ == "__main__":
    unittest.main()
