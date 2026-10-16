---
name: bohr-batch-download
description: "Download submitted worker artifacts, including partial outputs from failed workers, and preserve the batch download report."
---

# Download Bohr Batch Results

Use for completed workers or when existing results need retrieving. Do not submit, terminate, or regenerate jobs through this capability.

## Inputs and outputs

Input is a TOML request containing `run_dir`, or a directory with existing `run.toml`. Outputs are `downloads/`, `downloads/download-results.tsv`, and updated run state. Bohr credentials are required for remote reads and local writes.

Confirm `status.tsv` reflects remote worker state and `submit-results.tsv` contains current CLI public `BOHR_ID` values. Legacy directories relying on internal-ID mapping are rejected. Inspect first; use the following only when `resume.next_action = "bohr.batch.download"`:

```bash
uv run --package tricycle-agent tricycle-agent resume \
  --run-dir 'runs/<date>/<timestamp>/<id>' --live
```

`resume` is not a fixed download command. If its next action differs, use `BohrSubmitPlugin.download` with an injected gateway independently, without advancing submission or another stage.

The report lives at `downloads/download-results.tsv`. Exploration campaigns additionally copy it to the batch root for acquisition-evidence validation; ordinary downloads do not. Every worker with a job ID in `submit-results.tsv` is attempted, including `FAILED` workers: they may contain many successful task outputs. Requests reject `only_succeeded`.

`resume=True` reuses only integrity-checked results. Corrupt/partial directories are replaced atomically from same-filesystem temporary directories after successful download; previous directories remain as `.replaced-*` backups. Gateway `out.zip` downloads receive CRC checks, and extraction markers bind the archive fingerprint. An empty marker does not prove complete extraction: validate and extract again. Corrupt archives are redownloaded according to recovery options. Credentials come from the environment or automatically discovered project `.env`; shell variables take precedence.

## Failure recovery

New downloads are checked in temporary storage for installed environments and
caches, including ZIP/tar members, before publication. A contaminated remote
package produces a worker failure in the download report; existing local evidence
remains unchanged. The CLI cannot filter old archive members before transfer.
Arrange a narrower remote result package rather than retrying the same package
or modifying historical archives. Valid partial scientific results remain eligible.

Preserve reports after network or worker failures. Inspect first, repair environment variables, `.env`, or adapter configuration, then repeat the same download action. Repairing incomplete directories requires explicit `download(..., resume=True)` in the package API. CLI `resume` reuses the original request's `resume` value; it does not set it to true automatically. Do not delete reports or overwrite original inputs.
