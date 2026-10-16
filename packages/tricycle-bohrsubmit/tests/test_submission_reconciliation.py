import json
from typing import Any
from unittest.mock import patch

import pytest
from tricycle_bohrsubmit.gateway import BohrCliConfig, BohrCliGateway, BohrGatewayError


def inventory(items: list[dict[str, Any]], *, has_more: bool = False,
              total: int | None = None) -> str:
    return json.dumps({'ok': True, 'data': {'items': items, 'pagination': {
        'total': len(items) if total is None else total, 'has_more': has_more}}})


def test_reconciliation_requires_complete_inventory_for_absence() -> None:
    gateway = BohrCliGateway(BohrCliConfig(), environ={})
    for value in [inventory([], has_more=True), inventory([], total=5), '{}']:
        with patch.object(gateway, '_run', return_value=value), pytest.raises(BohrGatewayError, match='incomplete'):
            gateway.reconcile_submission('stable', '42')
    with patch.object(gateway, '_run', return_value=inventory([])):
        assert gateway.reconcile_submission('stable', '42')['status'] == 'absent'


def test_reconciliation_checks_name_group_uniqueness_and_public_id() -> None:
    gateway = BohrCliGateway(BohrCliConfig(), environ={})
    job = {'jobName': 'stable', 'jobGroupId': 142, 'bohrId': 99, 'id': 5}
    with patch.object(gateway, '_run', return_value=inventory([job])):
        result = gateway.reconcile_submission('stable', '42')
        assert result['external_id'] == '99'
    for jobs in [[job, job], [job, {**job, 'jobName': 'other', 'jobGroupId': 143}]]:
        with patch.object(gateway, '_run', return_value=inventory(jobs)), pytest.raises(BohrGatewayError):
            gateway.reconcile_submission('stable', '42')
