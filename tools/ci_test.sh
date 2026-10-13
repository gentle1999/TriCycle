#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
CI_PYTHON_VERSION="${CI_PYTHON_VERSION:-3.11}"
UV_BIN="${UV_BIN:-uv}"

cd "${ROOT_DIR}"

if ! command -v "${UV_BIN}" >/dev/null 2>&1; then
    printf '%s\n' "ci: uv executable not found: ${UV_BIN}" >&2
    exit 1
fi

printf '%s\n' "ci: syncing workspace with Python ${CI_PYTHON_VERSION}"
"${UV_BIN}" sync --frozen --all-packages --extra classical --python "${CI_PYTHON_VERSION}"

printf '%s\n' 'ci: compiling source and tests'
"${UV_BIN}" run --frozen --all-packages python -m compileall -q -x '(^|/)(\.venv|__pycache__)(/|$)' packages apps tests analysis

printf '%s\n' 'ci: running Ruff'
"${UV_BIN}" run --frozen --all-packages ruff check .

printf '%s\n' 'ci: running mypy across the workspace, tools, and tests'
"${UV_BIN}" run --frozen --all-packages mypy

printf '%s\n' 'ci: running Pyright across the workspace, tools, and tests'
"${UV_BIN}" run --frozen --all-packages pyright

printf '%s\n' 'ci: running root integration tests'
"${UV_BIN}" run --frozen --all-packages pytest tests

project_files=()
while IFS= read -r project_file; do
    project_files+=("${project_file}")
done < <(find packages apps -mindepth 2 -maxdepth 2 -type f -name pyproject.toml | sort)

if (( ${#project_files[@]} == 0 )); then
    printf '%s\n' 'ci: no workspace projects found' >&2
    exit 1
fi

for project_file in "${project_files[@]}"; do
    project_dir="${project_file%/pyproject.toml}"
    tests_dir="${project_dir}/tests"

    if [[ ! -d "${tests_dir}" ]]; then
        printf '%s\n' "ci: missing test project: ${tests_dir}" >&2
        exit 1
    fi

    test_files=("${tests_dir}"/test_*.py)
    if [[ ! -e "${test_files[0]}" ]]; then
        printf '%s\n' "ci: no test_*.py files found in ${tests_dir}" >&2
        exit 1
    fi

    printf '%s\n' "ci: testing ${project_dir}"
    "${UV_BIN}" run --frozen --all-packages pytest "${tests_dir}"
done

printf '%s\n' 'ci: all workspace projects passed'
