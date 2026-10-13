"""Keep offline research tooling outside deployable task packages."""

from __future__ import annotations

import ast
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_runtime_packages_do_not_import_plotting_or_analysis() -> None:
    forbidden = {"analysis", "matplotlib", "seaborn", "plotly", "bokeh"}
    imports = []
    for path in (ROOT / "packages").glob("*/src/**/*.py"):
        if {"archive", "assets", "__pycache__"}.intersection(path.parts):
            continue
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""]
            else:
                continue
            for name in names:
                if name.split(".")[0] in forbidden:
                    imports.append(f"{path.relative_to(ROOT)}:{node.lineno}: {name}")
    assert not imports, "Runtime packages import offline analysis:\n" + "\n".join(imports)


def test_sar_result_cli_works_without_the_analysis_tree_or_plotting_dependencies(
    tmp_path: Path,
) -> None:
    environment = {
        **os.environ,
        "PYTHONPATH": os.pathsep.join(
            str(ROOT / "packages" / name / "src")
            for name in ("tricycle-core", "tricycle-bohr", "tricycle-sar")
        ),
    }
    completed = subprocess.run(
        [sys.executable, "-S", "-m", "tricycle_sar", "results", "--help"],
        cwd=tmp_path,
        env=environment,
        text=True,
        capture_output=True,
        check=True,
    )
    assert "{verify,prepare,assets}" in completed.stdout
    assert "render" not in completed.stdout
    assert "distributions" not in completed.stdout


def test_analysis_cli_help_does_not_load_plotting_or_model_dependencies(tmp_path: Path) -> None:
    code = (
        f"import sys; sys.path.insert(0, {str(ROOT)!r}); "
        "from analysis.sar.__main__ import main; "
        "assert 'matplotlib' not in sys.modules and 'torch' not in sys.modules; "
        "main(['--help'])"
    )
    completed = subprocess.run(
        [sys.executable, "-I", "-S", "-c", code],
        cwd=tmp_path,
        text=True,
        capture_output=True,
        check=True,
    )
    assert "{render,distributions}" in completed.stdout
