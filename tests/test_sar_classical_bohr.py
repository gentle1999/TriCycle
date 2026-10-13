from __future__ import annotations

import tempfile
import tomllib
import unittest
from pathlib import Path
from unittest.mock import patch

from tricycle_bohrsubmit.task_table import expand_task_specs
from tricycle_core.toml import dump_toml
from tricycle_sar.preparation.classical import prepare


class ClassicalBohrTests(unittest.TestCase):
    def test_smoke_and_training_use_cpu_and_shared_group_without_submission(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run = root / "benchmark"
            for relative in [
                "configs/tricycle-classical.toml",
                "scripts/tricycle_classical.py",
                "data/snapshots/ground-truth/dataset.csv",
                "data/splits/ground-truth-5fold/assignments.csv",
            ]:
                file = run / "workspace" / relative
                file.parent.mkdir(parents=True, exist_ok=True)
                file.write_text("fixture")
            dump_toml(
                run / "run.toml",
                {"models": ["fingerprint_rf", "fingerprint_extra_trees", "fingerprint_ridge"]},
            )
            group_config = root / "group.toml"
            dump_toml(group_config, {"project_id": "test-project", "job_group_id": "test-group"})
            with (
                patch("tricycle_sar.preparation.classical.check") as check,
                patch("subprocess.run", side_effect=AssertionError("unexpected external command")),
            ):
                prepare(run, root / "requests", include_smoke=True, group_config=group_config)
                check.assert_called_once_with(run)
            for phase in ["smoke", "train"]:
                request = tomllib.loads((root / "requests" / f"bohr-{phase}.toml").read_text())
                self.assertEqual(request["machine_type"], "c16_m64_cpu")
                self.assertEqual(request["job_group_id"], "test-group")
                self.assertFalse(request["auto_job_group"])
            train = tomllib.loads((root / "requests/bohr-train.toml").read_text())
            tasks = expand_task_specs(train["task_defaults"], train["task_specs"])
            self.assertEqual(len(tasks), 15)
            self.assertTrue(all(not task["smoke"] for _, task in tasks))
            self.assertFalse((root / "requests/tasks").exists())
            self.assertFalse((root / "requests/scope.toml").exists())
            request = tomllib.loads((root / "requests/bohr-smoke.toml").read_text())
            smoke = expand_task_specs(request["task_defaults"], request["task_specs"])[0][1]
            self.assertTrue(smoke["smoke"])
            self.assertEqual(len(smoke["models"]), 3)
