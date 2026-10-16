# Development

## Local validation

Use temporary directories for tests, smoke probes, and scratch diagnostics. Do not create production runs for repository checks. If diagnostic evidence must be retained, write it under `artifacts/validation/` or `artifacts/audits/`; offline reports belong in `reports/`. Apply the [run admission rule](run-records.md) when reviewing any new writer or output default.

```bash
make check
```

This runs compilation checks, standard-library smoke tests, and Ruff when available. With uv installed, run the full dependency environment:

```bash
uv sync
uv run pytest
uv run ruff format --check .
uv run ruff check .
```

CI uses [`tools/ci_test.sh`](../tools/ci_test.sh) to check locked dependencies, compilation, Ruff, root integration tests, and each workspace subproject's independent tests under `packages/*/tests` and `apps/*/tests`. A subproject without `tests/` or `test_*.py` fails CI.

Bohr platform-task tests follow package ownership:

- `packages/tricycle-bohr/tests/` covers component submission, status, downloads, and the default dry-run guarantee that gateways are not called.
- `packages/tricycle-bohrsubmit/tests/` covers submission, inspection, download commands, and argument mappings for the current `bohr` CLI.
- `packages/tricycle-rits/tests/` covers train/finetune/infer requests, official command builders, source-directory upload fields, and the shared Bohr executor.
- `packages/tricycle-sar/tests/` covers model backends, shared environments, training/evaluation protocols, preparation, result processing, and reuse of RxnResid `sar.*` interfaces by sampling.
- Root `tests/` covers cross-package contracts, workflows, and integration; package tests cover the owning package's interfaces.

These tests use in-memory gateways or subprocess mocks. They require no Bohr credentials and create no remote jobs. Run all tests with:

```bash
uv run pytest
make test
```

## Documentation maintenance

Use English for maintained documentation, skills, agent decisions, and newly authored records unless the user explicitly requests another language. Chinese instructions alone do not change the working language. Include this requirement when delegating work. Preserve machine identifiers, API fields, paths, quoted source material, and historical artifacts when translation would change their meaning or integrity.

Verify entry points, fields, paths, and side-effect boundaries against code and configuration. Link concrete version and resource values to their configuration instead of creating a second binding. Preserve original third-party asset documentation as provenance; its old installation commands are not this project's operating instructions.

Keep experiment scores, one-off smoke summaries, job IDs, and figures in Git-ignored artifact directories. Before removing obsolete documentation, archive its original bytes, path, and hash, then remove it from Git tracking and update references; do not rewrite Git history. Use small synthetic inputs to test result verification rather than depending on real experiment packages. Check relative links and command help for documentation changes; wording changes do not require cloud reruns.

## Adding a live adapter

1. Add the minimum protocol fields required by the owning component.
2. Cover success, retries, duplicate submissions, and failures with fake or in-memory adapters.
3. Read environment variables and credentials only when explicitly enabled by configuration.
4. Add integration tests and separate network/account tests from default smoke tests.
5. Update `.agents/skills/<capability>/SKILL.md` and TOML examples with input artifacts, outputs, and idempotency semantics.
