"""Bind canonical local submissions to a complete, sanitized platform snapshot.

Default operation reads a supplied snapshot. --query performs only one job-list
query; it cannot submit, restart, terminate or modify a scientific trajectory.
"""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

from tricycle_core import record_io as rio

from tricycle_sampling.replay.campaign import digest, read, save
from tricycle_sampling.replay.suite import verified_plan


def audit(root: Path, output: Path, *, snapshot: Path | None = None,
          query: bool = False, require_complete: bool = False) -> dict[str, Any]:
    if query == (snapshot is not None):
        raise ValueError('choose exactly one of --query and --snapshot')
    root = rio.resolve(root)
    plan = verified_plan(root)
    group = read(root / 'shared-bohr/job-group.json')
    sources: dict[str, str] = {}
    submissions = []
    for path in sorted(rio.glob(root, '*/seed-*/round-*/submission.json')):
        value = read(path)
        if str(value['job_group_id']) != str(group['job_group_id']):
            raise ValueError('canonical submission belongs to another group')
        sources[str(path)] = digest(path)
        submissions.append({'path': str(path), **value})
    if query:
        from tricycle_bohrsubmit.gateway import BohrCliConfig, BohrCliGateway
        gateway = BohrCliGateway(BohrCliConfig(retry_max=1, submit_retry_mode='none'))
        raw = gateway._json_value(gateway._run(
            ['job', 'list', '-j', str(group['job_group_id']), '-n', '1000', '-o', 'json'],
            mode='transient'))
        if not isinstance(raw, dict) or not isinstance(raw.get('data'), dict):
            raise ValueError('unrecognized platform listing envelope')
        payload = raw['data']
        jobs = gateway._json_items(raw)
        listing = {'observed_at': datetime.now().astimezone().isoformat(),
                   'job_group_id': str(group['job_group_id']),
                   'pagination': payload.get('pagination'),
                   'jobs': [{key: row.get(key) for key in
                             ('bohrId', 'id', 'jobName', 'jobGroupId', 'createTime', 'phase')}
                            for row in jobs if isinstance(row, dict)]}
    else:
        assert snapshot is not None
        listing = read(snapshot)
    if listing['job_group_id'] != str(group['job_group_id']):
        raise ValueError('platform snapshot belongs to another group')
    page = listing['pagination']
    jobs = listing['jobs']
    if (not isinstance(page, dict) or page.get('has_more') is not False
            or page.get('page') != 1 or page.get('total') != len(jobs)):
        raise ValueError('listing does not prove complete group coverage')
    ids = [str(row['bohrId']) for row in jobs]
    if len(set(ids)) != len(ids) or 'None' in ids:
        raise ValueError('platform listing lacks unique canonical Bohr IDs')
    by_id = dict(zip(ids, jobs, strict=True))
    matched, failures = [], []
    local_ids = [str(row['external_id']) for row in submissions]
    if len(set(local_ids)) != len(local_ids):
        failures.append('multiple canonical submissions share an external ID')
    for local in submissions:
        external_id = str(local['external_id'])
        job = by_id.get(external_id)
        if job is None or job['jobName'] != local['job_name']:
            failures.append(f'canonical job missing or name differs: {external_id}')
            continue
        duplicates = [row for row in jobs if row['jobName'] == local['job_name']]
        if len(duplicates) != 1:
            failures.append(f'multiple platform jobs share canonical name: {local["job_name"]}')
        matched.append({'submission': local['path'], 'external_id': external_id,
                        'job_name': job['jobName'], 'phase': job['phase']})
    unclaimed = [row for row in jobs if str(row['bohrId']) not in set(local_ids)]
    complete = (len(matched) == plan['maximum_sar_jobs'] and not failures
                and all(row['phase'] == 'completed' for row in matched)
                and all(row['phase'] in ('completed', 'failed', 'terminated', 'stopped',
                                         'cancelled', 'canceled') for row in unclaimed))
    if require_complete and not complete:
        failures.append(f"all {plan['maximum_sar_jobs']} canonical jobs and unclaimed task termination are not proven")
    rio.mkdir(output, parents=True, exist_ok=False)
    save(output / 'platform-snapshot.json', listing)
    result = {'schema': 'tricycle.mae-platform-audit/v1', 'suite_sha256': digest(root / 'suite.json'),
              'source_sha256': digest(Path(__file__)),
              'snapshot_sha256': digest(output / 'platform-snapshot.json'),
              'submission_hashes': sources, 'matched_jobs': matched, 'unclaimed_jobs': unclaimed,
              'phase_counts': dict(Counter(row['phase'] for row in matched)),
              'failures': failures, 'complete': complete,
              'scope': 'canonical submissions captured before the complete platform listing query'}
    save(output / 'platform-audit.json', result)
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run-dir', type=Path, required=True)
    parser.add_argument('--out-dir', type=Path, required=True)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument('--snapshot', type=Path)
    source.add_argument('--query', action='store_true')
    parser.add_argument('--require-complete', action='store_true')
    args = parser.parse_args()
    value = audit(args.run_dir, args.out_dir, snapshot=args.snapshot, query=args.query,
                  require_complete=args.require_complete)
    print({'matched_jobs': len(value['matched_jobs']), 'unclaimed_jobs': len(value['unclaimed_jobs']),
           'phase_counts': value['phase_counts'], 'failures': value['failures'], 'complete': value['complete']})
    if value['failures']:
        raise SystemExit(1)


if __name__ == '__main__':
    main()
