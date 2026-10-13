"""Static declarations must describe the runtime implementation they accompany."""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]


def declarations(tree: ast.Module | ast.ClassDef, prefix: str = "") -> dict[str, Any]:
    result: dict[str, Any] = {}
    for node in tree.body:
        name = prefix + getattr(node, "name", "")
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            arguments = node.args
            result[name] = (
                [arg.arg for arg in arguments.posonlyargs],
                [arg.arg for arg in arguments.args],
                [arg.arg for arg in arguments.kwonlyargs],
                arguments.vararg.arg if arguments.vararg else None,
                arguments.kwarg.arg if arguments.kwarg else None,
                len(arguments.defaults),
                [default is not None for default in arguments.kw_defaults],
            )
        elif isinstance(node, ast.ClassDef):
            result.update(declarations(node, name + "."))
            result[name + ".fields"] = {
                field.target.id: field.value is not None
                for field in node.body
                if isinstance(field, ast.AnnAssign) and isinstance(field.target, ast.Name)
            }
    return result


def test_static_interfaces_preserve_functions_parameters_and_dataclass_defaults() -> None:
    stubs = list((ROOT / "packages").glob("*/src/**/*.pyi"))
    assert stubs, "No package interfaces found"
    for stub in stubs:
        source = stub.with_suffix(".py")
        assert source.is_file(), f"Orphan static interface: {stub}"
        assert declarations(ast.parse(source.read_text())) == declarations(
            ast.parse(stub.read_text())
        ), source
