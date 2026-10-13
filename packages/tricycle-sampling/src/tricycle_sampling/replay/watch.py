"""Supervise one reconciled MAE trajectory with the current campaign controller."""
from __future__ import annotations

import argparse
import fcntl
import os
import time
from pathlib import Path
from typing import Any

from tricycle_sampling.replay.campaign import ReplayGateway, check_hashes, digest, read, save, tick
from tricycle_sampling.replay.report import update as update_reports
from tricycle_sampling.replay.suite import AGENT_VARIANTS as VARIANTS
from tricycle_sampling.replay.suite import status, verified_plan


def validate_recovery(directory: Path, initial: dict[str, Any], record: Path) -> None:
    archived = read(record)
    if archived['phase'] != 'blocked' or archived['seed'] != initial['seed']:
        raise ValueError('recovery record does not bind this trajectory')
    if archived['round'] == initial['round']:
        if (archived['selected_ids'] != initial['selected_ids']
                or archived['new_ids'] != initial['new_ids']):
            raise ValueError('recovery record does not bind this unchanged trajectory')
        if initial['phase'] != 'complete':
            return
    config = read(directory.parent / 'experiment.json')
    if not 0 <= archived['round'] <= initial['round'] < config['round_count']:
        raise ValueError('recovery round exceeds the frozen trajectory')
    if initial['phase'] == 'complete' and initial['round'] + 1 != config['round_count']:
        raise ValueError('recovery completed before the final frozen round')
    owner_path = directory / 'recovery-supervisor.json'
    if initial['round'] < archived['round'] or not owner_path.exists():
        raise ValueError('advanced recovery requires its original supervisor record')
    owner = read(owner_path)
    if (owner.get('recovery_record') != str(record.resolve())
            or owner.get('recovery_record_sha256', digest(record)) != digest(record)):
        raise ValueError('recovery supervisor belongs to a different archived operation')
    expected = set(archived['selected_ids'])
    if len(expected) != config['batch_size'] * (archived['round'] + 1):
        raise ValueError('archived recovery budget is invalid')
    from tricycle_sampling.replay.audit import audit_decision

    last = initial['round'] + (initial['phase'] == 'complete')
    last_additions: set[str] = set(archived['new_ids'])
    for index in range(archived['round'], last):
        current = directory / f'round-{index:04d}'
        point = read(current / 'evaluation-summary.json')
        if (point['seed'] != initial['seed'] or point['round_index'] != index
                or set(point['selected_ids']) != expected or len(point['selected_ids']) != len(expected)):
            raise ValueError('recovery evaluation ancestry differs')
        if index == archived['round'] and read(current / 'reveal.json')['new_ids'] != archived['new_ids']:
            raise ValueError('archived recovery additions differ from the recorded reveal')
        check_hashes(current / 'input', read(current / 'input-hashes.json'))
        request = read(current / 'input/request.json')
        if request['round_index'] != index:
            raise ValueError('recovery worker round differs')
        check_hashes(current / 'input', request['input_hashes'])
        worker = read(current / 'result/worker-report.json')
        if worker['request_sha256'] != digest(current / 'input/request.json'):
            raise ValueError('recovery worker request binding differs')
        check_hashes(current / 'result', worker['outputs'])
        if index and digest(current / 'input/previous.pt') != digest(
                directory / f'round-{index-1:04d}' / 'result/checkpoint.pt'):
            raise ValueError('recovery checkpoint belongs to a different trajectory')
        if index < initial['round']:
            selection_path = current / 'decision/selection.json'
            audit_decision(selection_path)
            additions = read(selection_path)['candidate_ids']
            if len(set(additions)) != config['batch_size'] or expected & set(additions):
                raise ValueError('recovery selection budget or exclusions differ')
            last_additions = set(additions)
            expected.update(additions)
    if (set(initial['selected_ids']) != expected or len(initial['selected_ids']) != len(expected)
            or set(initial['new_ids']) != last_additions or len(initial['new_ids']) != config['batch_size']):
        raise ValueError('current recovery state is not the accepted descendant')


def watch_seed(
    root: Path, variant: str, seed: int, *, recovery_record: Path | None = None,
    live: bool = False, allow_submit: bool = False, once: bool = False,
    gateway: ReplayGateway | None = None,
) -> dict[str, Any]:
    plan = verified_plan(root)
    if variant not in VARIANTS or seed not in range(5):
        raise ValueError("invalid suite trajectory")
    experiment = Path(next(e["root"] for e in plan["experiments"] if e["variant"] == variant))
    directory = experiment / f"seed-{seed:02d}"
    initial = read(directory / "state.json")
    if not live:
        return {k: initial.get(k) for k in ("seed", "round", "phase", "external_id", "last_error")}
    if recovery_record is None:
        raise ValueError("live recovery requires an archived blocked state")
    if initial["phase"] == "blocked":
        raise ValueError("reconcile the blocked operation before starting its watcher")
    with (directory / ".recovery-supervisor.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        initial = read(directory / 'state.json')
        if initial['phase'] == 'blocked':
            raise ValueError('reconcile the blocked operation before starting its watcher')
        validate_recovery(directory, initial, recovery_record)
        if initial['phase'] == 'complete':
            return {k: initial.get(k) for k in ('seed', 'round', 'phase', 'external_id', 'last_error')}
        if gateway is None:
            from tricycle_bohrsubmit.gateway import BohrCliConfig, BohrCliGateway

            gateway = BohrCliGateway(BohrCliConfig(submit_retry_mode="none"))
        save(directory / "recovery-supervisor.json", {
            "pid": os.getpid(), "started_at": time.time(),
            "recovery_record": str(recovery_record.resolve()),
            'recovery_record_sha256': digest(recovery_record),
            'supervisor_source_sha256': digest(Path(__file__)),
            "controller": "tricycle_sampling.replay.campaign.tick", "allow_submit": allow_submit,
        })
        last_status = 0.0
        while True:
            current = tick(experiment, directory, gateway, allow_submit=allow_submit)
            try:
                update_reports(experiment)
            except (OSError, ValueError, KeyError) as exc:
                save(experiment / "report-error.json", {"error": str(exc), "at": time.time()})
            terminal = current["phase"] in ("complete", "blocked")
            if terminal or time.monotonic() - last_status >= 30:
                status(root)
                last_status = time.monotonic()
            if (once or terminal or (experiment / "PAUSE").exists()
                    or (current["phase"] == "submit" and not allow_submit)):
                return {k: current.get(k) for k in ("seed", "round", "phase", "external_id", "last_error")}
            time.sleep(2)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--variant", choices=VARIANTS, required=True)
    parser.add_argument("--seed", type=int, choices=range(5), required=True)
    parser.add_argument("--recovery-record", type=Path)
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--allow-submit", action="store_true")
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    print(watch_seed(args.run_dir.resolve(), args.variant, args.seed,
                     recovery_record=args.recovery_record, live=args.live,
                     allow_submit=args.allow_submit, once=args.once))


if __name__ == "__main__":
    main()
