from __future__ import annotations

from dataclasses import dataclass

from tricycle_core import (
    CapabilityCatalog,
    CapabilityManifest,
    PluginRegistry,
    SideEffectLevel,
)

from .bootstrap import create_registry


@dataclass(frozen=True, slots=True)
class CapabilityDefinition:
    """Application-owned mapping from a public capability to a component operation."""

    manifest: CapabilityManifest
    component_id: str
    operation: str


def _definition(
    capability_id: str,
    display_name: str,
    description: str,
    *,
    component_id: str,
    operation: str,
    input_kind: str,
    outputs: tuple[str, ...] = (),
    side_effect: SideEffectLevel = SideEffectLevel.NONE,
    requires_auth: bool = False,
    supports_dry_run: bool = True,
    resumable: bool = False,
    dependencies: tuple[str, ...] = (),
) -> CapabilityDefinition:
    return CapabilityDefinition(
        manifest=CapabilityManifest(
            capability_id=capability_id,
            display_name=display_name,
            version="0.1.0",
            description=description,
            component=component_id,
            input_kind=input_kind,
            outputs=outputs,
            side_effect=side_effect,
            requires_auth=requires_auth,
            supports_dry_run=supports_dry_run,
            resumable=resumable,
            dependencies=dependencies,
        ),
        component_id=component_id,
        operation=operation,
    )


CAPABILITY_DEFINITIONS: tuple[CapabilityDefinition, ...] = (
    _definition(
        "reactionopt.generate",
        "Generate ReactionOpt configuration",
        "Generate a versioned ReactionOpt configuration from a TOML reaction request.",
        component_id="tricycle.reactionopt",
        operation="generate",
        input_kind="reactionopt.request",
        outputs=("reaction-config",),
    ),
    _definition(
        "reactionopt.enumerate-da",
        "Enumerate Diels–Alder reactions",
        (
            "Expand ene/diene substrate pairs into atom-mapped Diels–Alder reaction "
            "candidates and write a local CSV without contacting external services."
        ),
        component_id="tricycle.reactionopt",
        operation="enumerate-da",
        input_kind="reactionopt.enumerate-da.request",
        outputs=("mapped-reactions.csv",),
        side_effect=SideEffectLevel.LOCAL_WRITE,
    ),
    _definition(
        "reactionopt.export",
        "Export ReactionOpt configuration",
        "Export a generated ReactionOpt configuration through an explicitly injected adapter.",
        component_id="tricycle.reactionopt",
        operation="generate",
        input_kind="reactionopt.request",
        outputs=("reactionopt-artifacts",),
        side_effect=SideEffectLevel.LOCAL_WRITE,
    ),
    _definition(
        "reactionopt.profile",
        "Build a mapped ReactionOpt profile",
        (
            "Fan out into independent explicit-hydrogen, fully mapped reactant and product "
            "conformer branches plus a reaction-level RitS pipeline that consumes mapped "
            "connectivity, filters/clusters candidates, and ranks them with g-xTB; aggregate "
            "the three pools only at profile assembly."
        ),
        component_id="tricycle.reactionopt",
        operation="profile",
        input_kind="reactionopt.profile.request",
        outputs=("reactant-conformers", "product-conformers", "rits-ts-conformers"),
        side_effect=SideEffectLevel.REMOTE_MUTATE,
        requires_auth=True,
        resumable=True,
        dependencies=("rits.infer",),
    ),
    _definition(
        "candidate.select",
        "Select reaction candidates",
        "Select the next reaction candidates with random, first-n, or uncertainty-aware strategies.",
        component_id="tricycle.sampling",
        operation="select",
        input_kind="sampling.request",
        outputs=("selected-candidates", "remaining-candidates"),
    ),
    _definition(
        "exploration.round",
        "Record an exploration round",
        (
            "Materialize the complete candidate pool, model scores, selection decisions, "
            "next-space policy, and the RitS train-or-skip decision for one round. Adaptive "
            "sampling also records the agent-authored acquisition function and local download evidence."
        ),
        component_id="tricycle.exploration",
        operation="round",
        input_kind="exploration.round.request",
        outputs=(
            "round.toml",
            "space.toml",
            "candidate-pool.tsv",
            "model-scores.tsv",
            "selection-decisions.tsv",
            "selected-candidates.tsv",
            "acquisition-function.toml",
            "range-decision.toml",
            "training-decision.toml",
            "snapshot-transition.toml",
            "artifacts/manifest.toml",
        ),
        side_effect=SideEffectLevel.LOCAL_WRITE,
        resumable=True,
    ),
    _definition(
        "bohr.compute.plan",
        "Plan a Bohr computation",
        "Build an auditable Bohr job plan without contacting the remote platform.",
        component_id="tricycle.bohr.compute",
        operation="compute",
        input_kind="bohr.compute.request",
        outputs=("TaskPlan",),
    ),
    _definition(
        "bohr.compute.submit",
        "Submit a Bohr computation",
        "Submit one planned computational chemistry job through the Bohr gateway.",
        component_id="tricycle.bohr.compute",
        operation="compute",
        input_kind="bohr.compute.request",
        outputs=("external-job-id",),
        side_effect=SideEffectLevel.REMOTE_MUTATE,
        requires_auth=True,
        resumable=True,
    ),
    _definition(
        "bohr.job.status",
        "Inspect a Bohr job",
        "Read the status of an existing Bohr job without changing its remote state.",
        component_id="tricycle.bohr.compute",
        operation="status",
        input_kind="bohr.job-reference",
        outputs=("job-status",),
        side_effect=SideEffectLevel.REMOTE_READ,
        requires_auth=True,
        resumable=True,
    ),
    _definition(
        "bohr.job.download",
        "Download a Bohr job",
        "Download artifacts from an existing Bohr job into a local directory.",
        component_id="tricycle.bohr.compute",
        operation="download",
        input_kind="bohr.job-reference",
        outputs=("download-directory",),
        side_effect=SideEffectLevel.REMOTE_READ,
        requires_auth=True,
        resumable=True,
    ),
    _definition(
        "bohr.batch.prepare",
        "Prepare a Bohr batch",
        "Stage a local Bohr batch directory from a TOML request without submitting jobs.",
        component_id="tricycle.bohr.submit",
        operation="batch.prepare",
        input_kind="bohr.batch.request",
        outputs=("run.toml", "batch.toml", "submit-plan.tsv"),
        side_effect=SideEffectLevel.LOCAL_WRITE,
    ),
    _definition(
        "bohr.batch.submit",
        "Submit a prepared Bohr batch",
        "Submit non-empty workers from an existing prepared run through the Bohr gateway.",
        component_id="tricycle.bohr.submit",
        operation="batch.submit",
        input_kind="bohr.run-reference",
        outputs=("submit-results.tsv", "external-job-ids"),
        side_effect=SideEffectLevel.REMOTE_MUTATE,
        requires_auth=True,
        resumable=True,
        dependencies=("bohr.batch.prepare",),
    ),
    _definition(
        "bohr.batch.status",
        "Inspect a Bohr batch",
        "Query all submitted workers in a prepared Bohr run and write status.tsv.",
        component_id="tricycle.bohr.submit",
        operation="batch.status",
        input_kind="bohr.run-reference",
        outputs=("status.tsv",),
        side_effect=SideEffectLevel.REMOTE_READ,
        requires_auth=True,
        resumable=True,
        dependencies=("bohr.batch.submit",),
    ),
    _definition(
        "bohr.batch.download",
        "Download a Bohr batch",
        "Download artifacts from every submitted worker, including partial outputs from failed workers.",
        component_id="tricycle.bohr.submit",
        operation="batch.download",
        input_kind="bohr.run-reference",
        outputs=("downloads", "download-results.tsv"),
        side_effect=SideEffectLevel.REMOTE_READ,
        requires_auth=True,
        resumable=True,
        dependencies=("bohr.batch.status",),
    ),
    _definition(
        "bohr.batch.watch",
        "Watch a Bohr batch",
        "Poll a prepared Bohr run until completion or a configured iteration limit.",
        component_id="tricycle.bohr.submit",
        operation="batch.watch",
        input_kind="bohr.run-reference",
        outputs=("status.tsv",),
        side_effect=SideEffectLevel.REMOTE_READ,
        requires_auth=True,
        resumable=True,
        dependencies=("bohr.batch.status",),
    ),
    _definition(
        "bohr.batch.terminate",
        "Terminate incomplete Bohr jobs",
        "Terminate selected incomplete workers after explicit confirmation.",
        component_id="tricycle.bohr.submit",
        operation="batch.terminate",
        input_kind="bohr.run-reference",
        outputs=("terminate-results.tsv",),
        side_effect=SideEffectLevel.DESTRUCTIVE,
        requires_auth=True,
        resumable=True,
        dependencies=("bohr.batch.status",),
    ),
    _definition(
        "dpa-berny.prepare",
        "Prepare DPA-Berny or Gaussian tasks",
        "Generate local DPA-Berny or Gaussian worker inputs from a TOML request.",
        component_id="tricycle.bohr.submit",
        operation="dpa-berny.prepare",
        input_kind="dpa-berny.request",
        outputs=("run.toml", "batch.toml", "tasks.tsv"),
        side_effect=SideEffectLevel.LOCAL_WRITE,
    ),
    _definition(
        "dpa-berny.rescue",
        "Prepare a DPA-Berny rescue run",
        "Create a retry run from a completed DPA-Berny or Gaussian run directory.",
        component_id="tricycle.bohr.submit",
        operation="dpa-berny.rescue",
        input_kind="dpa-berny.rescue-request",
        outputs=("run.toml", "batch.toml", "tasks.tsv"),
        side_effect=SideEffectLevel.LOCAL_WRITE,
    ),
    _definition(
        "reaction-db.ingest",
        "Ingest reaction records",
        "Validate and upsert reaction records through an injected repository adapter.",
        component_id="tricycle.reaction-database",
        operation="ingest",
        input_kind="reaction-ingest.request",
        outputs=("reaction-record",),
        side_effect=SideEffectLevel.REMOTE_MUTATE,
        requires_auth=True,
    ),
    _definition(
        "reaction-db.batch-import",
        "Import one calculation batch",
        (
            "Create one internal database project and submit only the recursively "
            "selected calculation logs from one declared Bohr batch."
        ),
        component_id="tricycle.reaction-database",
        operation="batch-import",
        input_kind="reaction-db.batch-import.request",
        outputs=("database-project-id", "artifact-ingestion"),
        side_effect=SideEffectLevel.REMOTE_MUTATE,
        requires_auth=True,
        resumable=True,
    ),
    _definition(
        "reaction-db.filter",
        "Filter cleaned reactions by rxn-SMARTS",
        (
            "Ask the database to apply the configured reaction_smarts predicate to "
            "cleaned data in one internal project; reaction_class is not used."
        ),
        component_id="tricycle.reaction-database",
        operation="filter",
        input_kind="reaction-db.filter.request",
        outputs=("filtered-reaction-count",),
        side_effect=SideEffectLevel.REMOTE_READ,
        requires_auth=True,
    ),
    _definition(
        "reaction-db.export-rits",
        "Export RitS TS dataset",
        (
            "Export database-filtered mapped reaction SMILES with corresponding "
            "transition-state structures for RitS training or fine-tuning."
        ),
        component_id="tricycle.reaction-database",
        operation="export-rits",
        input_kind="reaction-db.rits-export.request",
        outputs=("rits-reaction-space.csv",),
        side_effect=SideEffectLevel.REMOTE_READ,
        requires_auth=True,
        resumable=True,
    ),
    _definition(
        "reaction-db.export-rxnresid",
        "Export RxnResid energy dataset",
        (
            "Export database-filtered mapped reaction SMILES, reaction and activation "
            "free energies, phase-resolved compute times, and cost statistics."
        ),
        component_id="tricycle.reaction-database",
        operation="export-rxnresid",
        input_kind="reaction-db.rxnresid-export.request",
        outputs=("rxnresid-reaction-space.csv", "calculation-cost-summary"),
        side_effect=SideEffectLevel.REMOTE_READ,
        requires_auth=True,
        resumable=True,
    ),
    _definition(
        "sar.train",
        "Plan or submit RxnResid training",
        "Create an RxnResid training job and optionally submit it through the Bohr gateway.",
        component_id="tricycle.sar",
        operation="train",
        input_kind="sar.request",
        outputs=("bohr-job",),
        side_effect=SideEffectLevel.REMOTE_MUTATE,
        requires_auth=True,
        resumable=True,
        dependencies=("bohr.compute.submit",),
    ),
    _definition(
        "sar.infer",
        "Plan or submit RxnResid inference",
        "Create an RxnResid inference job and optionally submit it through the Bohr gateway.",
        component_id="tricycle.sar",
        operation="infer",
        input_kind="sar.request",
        outputs=("bohr-job",),
        side_effect=SideEffectLevel.REMOTE_MUTATE,
        requires_auth=True,
        resumable=True,
        dependencies=("bohr.compute.submit",),
    ),
    _definition(
        "sar.finetune",
        "Plan or submit RxnResid fine-tuning",
        (
            "Create an RxnResid checkpoint-refit job using the upstream "
            "train.py --refit protocol and optionally submit it through Bohr."
        ),
        component_id="tricycle.sar",
        operation="finetune",
        input_kind="sar.request",
        outputs=("bohr-job",),
        side_effect=SideEffectLevel.REMOTE_MUTATE,
        requires_auth=True,
        resumable=True,
        dependencies=("bohr.compute.submit",),
    ),
    _definition(
        "rits.train",
        "Plan or submit RitS training",
        "Create a RitS training job, upload its task directory, and optionally submit it through the Bohr gateway.",
        component_id="tricycle.rits",
        operation="train",
        input_kind="rits.request",
        outputs=("bohr-job",),
        side_effect=SideEffectLevel.REMOTE_MUTATE,
        requires_auth=True,
        resumable=True,
        dependencies=("bohr.compute.submit",),
    ),
    _definition(
        "rits.infer",
        "Plan or submit RitS inference",
        "Create a RitS inference job, upload its task directory, and optionally submit it through the Bohr gateway.",
        component_id="tricycle.rits",
        operation="infer",
        input_kind="rits.request",
        outputs=("bohr-job",),
        side_effect=SideEffectLevel.REMOTE_MUTATE,
        requires_auth=True,
        resumable=True,
        dependencies=("bohr.compute.submit",),
    ),
    _definition(
        "rits.finetune",
        "Plan or submit RitS fine-tuning",
        (
            "Create a RitS fine-tuning job using the upstream finetune_from "
            "weights-only protocol and optionally submit it through Bohr."
        ),
        component_id="tricycle.rits",
        operation="finetune",
        input_kind="rits.request",
        outputs=("bohr-job",),
        side_effect=SideEffectLevel.REMOTE_MUTATE,
        requires_auth=True,
        resumable=True,
        dependencies=("bohr.compute.submit",),
    ),
    _definition(
        "rits.ssh.finetune",
        "Submit RitS fine-tuning over SSH",
        (
            "Transfer a local RitS training bundle to an internal GPU host using OpenSSH/scp, "
            "then launch the requested command in a detached GPU Docker container through a named context."
        ),
        component_id="tricycle.rits.ssh",
        operation="submit",
        input_kind="rits.ssh.finetune.request",
        outputs=("ssh-job-id", "remote-directory"),
        side_effect=SideEffectLevel.REMOTE_MUTATE,
        requires_auth=True,
        resumable=True,
    ),
    _definition(
        "rits.ssh.status",
        "Inspect a RitS SSH job",
        "Read the state file and process liveness for a detached RitS job on an internal host.",
        component_id="tricycle.rits.ssh",
        operation="status",
        input_kind="rits.ssh.job-reference",
        outputs=("ssh-job-status",),
        side_effect=SideEffectLevel.REMOTE_READ,
        requires_auth=True,
        resumable=True,
    ),
    _definition(
        "rits.ssh.logs",
        "Read RitS SSH training logs",
        "Read the configured tail of a remote RitS training log over OpenSSH.",
        component_id="tricycle.rits.ssh",
        operation="logs",
        input_kind="rits.ssh.job-reference",
        outputs=("training-log-tail",),
        side_effect=SideEffectLevel.REMOTE_READ,
        requires_auth=True,
        resumable=True,
    ),
    _definition(
        "rits.ssh.terminate",
        "Terminate a RitS SSH job",
        "Send SIGTERM to the detached remote training process after explicit confirmation.",
        component_id="tricycle.rits.ssh",
        operation="terminate",
        input_kind="rits.ssh.job-reference",
        outputs=("termination-state",),
        side_effect=SideEffectLevel.DESTRUCTIVE,
        requires_auth=True,
        resumable=True,
    ),
    _definition(
        "rits.ssh.download",
        "Download RitS SSH outputs",
        "Download explicitly selected output paths from a remote RitS run using scp.",
        component_id="tricycle.rits.ssh",
        operation="download",
        input_kind="rits.ssh.download.request",
        outputs=("local-training-outputs",),
        side_effect=SideEffectLevel.REMOTE_READ,
        requires_auth=True,
        resumable=True,
    ),
)


def create_capability_catalog(registry: PluginRegistry | None = None) -> CapabilityCatalog:
    registry = registry or create_registry()
    catalog = CapabilityCatalog()
    for definition in CAPABILITY_DEFINITIONS:
        catalog.register(
            registry.get(definition.component_id),
            definition.manifest,
            operation=definition.operation,
        )
    return catalog
