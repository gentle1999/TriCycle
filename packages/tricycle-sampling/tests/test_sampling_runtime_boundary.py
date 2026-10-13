"""Runtime packages must work independently of repository maintenance scripts."""

from __future__ import annotations

import ast
import re
from pathlib import Path


def test_packages_do_not_depend_on_tools() -> None:
    packages = Path(__file__).resolve().parents[2]
    imports = []
    for path in packages.glob("*/src/**/*.py"):
        if {"assets", "__pycache__"}.intersection(path.parts):
            continue
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.Call):
                for argument in [*node.args, *(item.value for item in node.keywords)]:
                    for value in ast.walk(argument):
                        if not isinstance(value, ast.Constant) or not isinstance(value.value, str):
                            continue
                        if value.value in {"tools/list", "tools/call"}:
                            continue  # MCP protocol method names, not filesystem dependencies.
                        if re.search(r"(?:^|/)tools/", value.value) or value.value.startswith("tools."):
                            imports.append(f"{path.relative_to(packages)}:{value.lineno}: {value.value}")
            if (isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div)
                    and isinstance(node.right, ast.Constant) and node.right.value == "tools"):
                imports.append(f"{path.relative_to(packages)}:{node.lineno}: tools directory")
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""]
            else:
                continue
            for name in names:
                if name.split(".")[0] == "tools":
                    imports.append(f"{path.relative_to(packages)}:{node.lineno}: {name}")
    assert not imports, "Runtime packages import tools:\n" + "\n".join(imports)
