---
name: bohr-compute-plan
description: "Build an auditable dry-run plan for one Bohr computational chemistry job from a TOML request."
---

# Plan a Bohr Job

The request requires a stable `job_name`, `operation`, `image`, and `command`; input artifacts, project, and machine type are optional. Explicitly select a GPU node for DPA and a CPU node for pure Gaussian. Check the actual computation engine against the resources; a DPA run on CPU is not passing smoke evidence.

```bash
uv run --package tricycle-agent tricycle-agent plan \
  bohr.compute.plan request.toml --run-id <id>
```

This capability only plans: it does not read authentication, access the network, or submit jobs. Use `bohr.compute.submit` for submission.

## Failure recovery and routing

If validation fails, correct the TOML request and replan. A planning failure is not a remote job failure. Use `bohr.compute.submit`, `bohr.job.status`, or `bohr.job.download` for submission, status, or downloads; do not make this skill read credentials.
