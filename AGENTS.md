# TriCycle Agent Development Guide

Before adding, changing, splitting, moving, or reviewing code, use the project
[code-structure](.agents/skills/code-structure/SKILL.md) skill to determine module
ownership, dependency direction, and validation scope. Running existing
experiments or changing experimental data alone does not trigger structural
refactoring.

## Language and collaboration

- English is the primary language for this repository and its agent workflows.
  Reply to the user in Chinese unless they request another conversation language.
  Execute tasks and author project artifacts in English even when the user writes
  in Chinese. A conversation-language request does not change artifact or subagent
  language; apply an exception only when explicitly requested for that output.
- Write documentation, skills, code comments, new human-readable reports,
  decision rationales, work logs, reviews, commit messages, and subagent responses
  in English by default. Keep identifiers, commands, schemas, and scientific
  notation exact.
- Pass this language requirement to every delegated agent and generated agent
  task. Subagents must use English for task analysis, decision explanations,
  plans, handoffs, and records, including when source material or previous
  records are in Chinese. Carry any explicit user language exception into the
  relevant delegation.
- Preserve user data, historical run records, quoted source text, upstream
  licenses, and multilingual parsing or test fixtures in their original form.
  English-first authoring does not authorize translating immutable evidence or
  reducing support for non-English input.

## Engineering conventions

- Use Python `>=3.11` and a `src/` layout for every publishable subproject.
- Manage packages with the `uv` workspace. The root `Makefile` also provides
  basic smoke checks that do not require uv.
- Put shared contracts in the common foundation layer. The foundation must not
  depend on domain modules, and package dependencies must remain acyclic.
- Put domain execution logic in its owning package. Code in `packages/` must
  not import, read, copy, or execute implementations from `apps/`, `tools/`, or
  `analysis/`. Update callers after migration; do not retain forwarding scripts
  or duplicate implementations.
- Keep offline analysis, scientific plotting, and their dependency environment
  in root-level `analysis/`. Logic that affects task decisions or result
  validity belongs in the responsible runtime package.
- Publish `py.typed` with each package. Static interfaces may use adjacent
  `.pyi` files; parameters, default-value positions, and dataclass fields must
  match the implementation and pass interface consistency checks. Stubs do not
  replace runtime validation. Update interfaces and checks with implementation
  changes.
- Inject external services through protocols/adapters. Default dry-run paths
  must not read credentials, access the network, or submit real jobs.
- Write default run records to
  `runs/YYYY-MM-DD/HH-MM-SS.ffffff±ZZZZ/<run-id>` using local dates and times.
  Preserve explicit `run_dir` / `out_dir` values and existing recovery paths.
- Admit only task-owned execution, verification, and recovery records to `runs/`.
  Put offline reports in `reports/`, reusable assets and independent audits in
  `artifacts/`, host-service state in `.tricycle/`, and disposable checks in
  temporary directories. Follow [run record scope](docs/run-records.md) before
  creating output; do not relocate historical evidence merely to enforce it.
- Maintain project skills only at `.agents/skills/<skill-name>/SKILL.md`.
  Do not create parallel skill directories, duplicate names, or forwarding
  entry points. Update related skills when code interfaces change.

## Computation and protocol changes

- Execute the established protocol selected by the user: prepare inputs,
  submit, query, download, validate results, and retry under the established
  policy. Authorization to run computations does not authorize protocol changes.
- Without explicit user authorization, do not change algorithms, model or
  weight bindings, training/inference workflows, data splits, hyperparameters,
  numerical precision, environment dependencies, CPU/GPU policy, or result
  acceptance criteria. Do not change these to pass a smoke check or bypass an
  execution failure.
- If a protocol change is necessary, preserve the failure record, report its
  cause and the proposed change and impact, and wait for explicit authorization.
  Continue authorized work that does not depend on that change. Do not ask
  again for permission to perform routine operations already authorized.

## Ownership of guidance and configuration

- Keep only global engineering and collaboration conventions in `AGENTS.md`.
  Put module responsibilities, interfaces, environments, and runtime details
  in package READMEs or module documentation, and procedures in the relevant skill.
- Maintain concrete source revisions, asset hashes, and image digests in
  configuration or asset manifests. Run artifacts record the bindings actually
  used. Do not copy these literal values into `AGENTS.md` or skills or make a
  particular version a condition for using a skill.
- Skills describe configuration entry points, field semantics, validation, and
  failure handling. Changing configuration values alone does not require a
  skill update; changing configuration interfaces or procedures does.
- Maintain current interfaces, general protocols, and operating guides in
  documentation. Keep experiment scores, job status, figures, and one-off audit
  records in Git-ignored artifact directories. Before removing obsolete
  documentation from maintained directories and Git tracking, archive its
  original bytes and record its original path and hash. Current code, tests,
  and guides must not depend on a machine-local archive.

## Common commands

```bash
make check
make test
make list-plugins
uv sync
uv run pytest
uv run ruff check .
```

## Minimum requirements for a new plugin

Follow the [plugin contract](docs/plugin-contract.md): implement typed requests,
a planning interface, and an injectable adapter; register the capability in the
application; and provide package documentation, an operating skill, example
configuration, and a smoke test. Leave TODOs and extension points where domain
rules are unknown. Do not invent real platform APIs.
