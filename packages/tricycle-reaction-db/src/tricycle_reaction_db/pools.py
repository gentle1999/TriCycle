"""Export or adapt database pools through an explicit database adapter."""
from __future__ import annotations

import argparse
import json
import os
import shutil
import tomllib
from pathlib import Path

from tricycle_core import timestamped_run_directory
from tricycle_core.io import file_sha256 as _sha256
from tricycle_core.io import write_delimited as _write_csv

from tricycle_reaction_db import ReactionThermodynamicsCsvAdapter

from .substrate_adaptation import adapt_database_export


def _resolve_path(value: object, *, base: Path) -> Path:
    expanded = os.path.expandvars(os.path.expanduser(str(value)))
    path = Path(expanded)
    return (path if path.is_absolute() else base / path).resolve()

def _optional_path(value: object, *, base: Path) -> Path | None:
    if value is None or not str(value).strip():
        return None
    return _resolve_path(value, base=base)

def export_database_collections(
    config_path: Path, *, live: bool = False, adapter: ReactionThermodynamicsCsvAdapter | None = None,
    output_root: Path | None = None,
) -> dict[str, object]:
    """Reuse the native database exporter and structure adaptation for all pools.

    Planning is offline. A database adapter is constructed only for explicit
    live execution; callers can inject one for testing or other runtimes.
    """
    from tricycle_reaction_db import ReactionFilterRequest


    config_path = config_path.resolve()
    config = tomllib.loads(config_path.read_text(encoding="utf-8"))
    registry = _resolve_path(config["substrate_registry"], base=config_path.parent)
    export_filters: dict[str, bool] = {}
    for name, value in config.get("export_filters", {}).items():
        if name not in {"has_activation_gibbs_free_energy", "has_reaction_gibbs_free_energy"} or type(value) is not bool:
            raise ValueError("native export filters must be supported boolean presence filters")
        export_filters[name] = value
    projects = config["projects"]
    if not isinstance(projects, list) or not projects:
        raise ValueError("export configuration needs [[projects]]")
    names = [project["name"] for project in projects]
    if len(set(names)) != len(names) or any(
        not name or Path(name).name != name or name in {".", ".."} for name in names
    ):
        raise ValueError("project names must be unique directory names")
    gt_id = "00000000-0000-7000-8000-000000000201"
    for project in projects:
        if project["role"] not in {"candidate", "evaluation"}:
            raise ValueError("project role must be candidate or evaluation")
        if (project["role"] == "evaluation") != (project["project_id"] == gt_id):
            raise ValueError("ground truth must be isolated as an evaluation project")
    requests = [ReactionFilterRequest(project_id=project["project_id"]) for project in projects]
    plan: dict[str, object] = {
        "config": str(config_path), "substrate_registry": str(registry),
        "tool_name": "export_mapped_reaction_thermodynamics_csv",
        "export_filters": export_filters,
        "profile_policy": "all database-exported profiles; no label selection",
        "projects": projects, "dry_run": not live,
    }
    if not live:
        return plan
    if adapter is None:
        from tricycle_reaction_db import McpReactionDatabaseAdapter

        adapter = McpReactionDatabaseAdapter.from_env(Path.cwd() / ".env")
    root = output_root or timestamped_run_directory(Path("artifacts/datasets"), "database-sampling-pools")
    root = root.resolve()
    root.mkdir(parents=True, exist_ok=False)
    shutil.copyfile(config_path, root / "request.toml")
    manifests = {}
    for project, request in zip(projects, requests, strict=True):
        folder = root / ("evaluation" if project["role"] == "evaluation" else "candidates") / project["name"]
        raw = folder / "database-export.csv"
        print(f"Exporting {project['name']} using the database CSV tool", flush=True)
        exported = adapter.export_thermodynamics_csv(request, raw, **export_filters)
        aliases = _optional_path(project.get("source_aliases"), base=config_path.parent)
        adapted = adapt_database_export(
            raw, registry, folder, source_aliases=aliases, project_id=project["project_id"]
        )
        manifests[project["name"]] = {"export": dict(exported), "adaptation": adapted}
        print(
            f"{project['name']}: {adapted['row_count']} rows; "
            f"matches {adapted['match_status_counts']}", flush=True,
        )
    result = {**plan, "root": str(root), "pools": manifests}
    (root / "manifest.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    (root / "README.md").write_text(
        "# Database-exported sampling pools\n\n"
        "`database-export.csv` is the paginated native database CSV. "
        "`reaction-collection.csv` appends ene/diene matching fields and is sorted by pair. "
        "Every native profile row and original field is preserved. "
        "`unmatched.csv` lists records without a unique pair; these remain in the main table.\n\n"
        "Ground truth lives under evaluation; proxy pools live under candidates. "
        "Matching uses structures only and optional project-specific, database-verified "
        "reactant aliases. No TS input-source reassignment or row expansion is applied.\n\n"
        "Different thermodynamics profiles are preserved separately. This export does not "
        "choose a training-label policy. Phase runtimes are exported values, not a complete "
        "sampling cost ledger; failed attempts and shared resources require the separate ledger. "
        "Missing fields remain blank. Receipts record source hashes and pagination; "
        "the database does not provide a cross-page transaction snapshot.\n",
        encoding="utf-8",
    )
    return result

def _main_export_database(args: argparse.Namespace) -> int:
    result = export_database_collections(
        args.config, live=args.live, output_root=args.output_root
    )
    print(json.dumps({key: value for key, value in result.items() if key != "pools"}, indent=2))
    return 0

def adapt_database_collections(
    config_path: Path, root: Path, *, output_root: Path | None = None,
) -> dict[str, object]:
    """Replay the existing adaptation offline against verified native exports."""

    config_path = config_path.resolve()
    root = root.resolve()
    source_root = root
    if output_root is not None:
        root = output_root.resolve()
        if root == source_root or source_root in root.parents:
            raise ValueError("adaptation output must be a new directory outside its source")
        shutil.copytree(source_root, root)
    config = tomllib.loads(config_path.read_text(encoding="utf-8"))
    manifest_path = root / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["adapted_from"] = str(source_root)
    manifest["root"] = str(root)
    manifest["config"] = str(config_path)
    manifest["adaptation_config_sha256"] = _sha256(config_path)
    manifest["projects"] = config["projects"]
    if config_path != (root / "request.toml").resolve():
        shutil.copyfile(config_path, root / "request.toml")
    registry = _resolve_path(config["substrate_registry"], base=config_path.parent)
    for project in config["projects"]:
        folder = root / ("evaluation" if project["role"] == "evaluation" else "candidates") / project["name"]
        raw = folder / "database-export.csv"
        exported = json.loads(raw.with_suffix(".receipt.json").read_text(encoding="utf-8"))
        if exported["project_id"] != project["project_id"] or exported["sha256"] != _sha256(raw):
            raise ValueError("native export no longer matches its project-scoped receipt")
        aliases = _optional_path(project.get("source_aliases"), base=config_path.parent)
        adapted = adapt_database_export(
            raw, registry, folder, source_aliases=aliases, project_id=project["project_id"]
        )
        manifest["pools"][project["name"]] = {"export": exported, "adaptation": adapted}
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    summary = [
        "# Export and substrate matching statistics", "",
        "Each database export row remains independent; only ene/diene matching fields are appended. Original fields and row counts have been verified row by row.", "",
        "| Pool | Native export rows | Matched rows | Unmatched rows | With activation free energy | Matched labeled rows | Labeled pairs |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    statistics: list[dict[str, object]] = []
    for name, pool in manifest["pools"].items():
        counts = pool["adaptation"]
        matched_rows = counts["match_status_counts"].get("matched", 0)
        unmatched_rows = counts["row_count"] - matched_rows
        statistics.append({
            "pool": name,
            "project_id": counts["project_id"],
            "substrate_match_policy": counts["substrate_match_policy"],
            "native_export_rows": counts["row_count"],
            "matched_rows": matched_rows,
            "unmatched_rows": unmatched_rows,
            "activation_labeled_rows": counts["activation_labeled_rows"],
            "activation_labeled_matched_rows": counts["activation_labeled_matched_rows"],
            "activation_labeled_unmatched_rows": counts["activation_labeled_rows"] - counts["activation_labeled_matched_rows"],
            "missing_activation_rows": counts["row_count"] - counts["activation_labeled_rows"],
            "matched_pairs_all_rows": counts["matched_pair_count"],
            "activation_labeled_pair_count": counts["activation_labeled_pair_count"],
            "match_status_counts": json.dumps(counts["match_status_counts"], sort_keys=True),
            "source_sha256": counts["source_sha256"],
        })
        summary.append(
            f"| {name} | {counts['row_count']} | {matched_rows} | {unmatched_rows} | {counts['activation_labeled_rows']} | "
            f"{counts['activation_labeled_matched_rows']} | {counts['activation_labeled_pair_count']} |"
        )
    _write_csv(root / "matching-statistics.csv", list(statistics[0]), statistics)
    summary.extend([
        "", "Matching requires full molecular graph equivalence, preserving E/Z configuration, chirality, isotopes, charge, and radical state.",
        "Atom-map numbers, atom traversal, and substrate order do not distinguish identities. Connectivity keys support retrieval but do not prove a match.",
        "Pair counts describe substrate pairs matched by the current policy; they do not prove database completeness.",
        "Audit original calculation input coverage, database export completeness, and substrate matching coverage separately. A lower pair count does not establish missing data.",
        "Unmatched and unlabeled records are retained; see each pool's unmatched.csv for details.",
        "This table and matching-statistics.csv are offline statistics, not intelligent sampling inputs.",
    ])
    (root / "summary.md").write_text("\n".join(summary) + "\n", encoding="utf-8")
    return {"root": str(root), "pools": {name: pool["adaptation"] for name, pool in manifest["pools"].items()}}

def _main_adapt_database(args: argparse.Namespace) -> int:
    print(json.dumps(adapt_database_collections(
        args.config, args.root, output_root=args.output_root,
    ), ensure_ascii=False, indent=2))
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="action", required=True)
    export = commands.add_parser("export")
    export.add_argument("--config", type=Path, required=True)
    export.add_argument("--output-root", type=Path)
    export.add_argument("--live", action="store_true")
    export.set_defaults(func=_main_export_database)
    adapt = commands.add_parser("adapt")
    adapt.add_argument("--config", type=Path, required=True)
    adapt.add_argument("--root", type=Path, required=True)
    adapt.add_argument("--output-root", type=Path)
    adapt.set_defaults(func=_main_adapt_database)
    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
