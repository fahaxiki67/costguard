"""不存在某项不能直接等同于漏项；先区分业务范围和结算模式。"""
from pathlib import Path

import pytest

from jiadun.core.engine import aggregate
from jiadun.core.engine import quantity_control as qc
from jiadun.core.models import project as pm


@pytest.mark.parametrize('mode,same_contract,confirmed,expected_status', [
    ('incremental', False, True, 'ok'),
    ('incremental', True, True, 'ok'),
    ('cumulative', True, True, 'incomplete'),
    ('incremental', True, False, 'incomplete'),
])
def test_missing_item_uses_confirmed_scope_and_mode(
    tmp_path, mode, same_contract, confirmed, expected_status,
):
    info = pm.create_project('清单出现范围反例', tmp_path / 'workspace')
    info, conn = pm.open_project(Path(info.workspace_path))
    try:
        for number, key, code, quantity in [
            (1, 'A', 'C1', '100'),
            (2 if same_contract else 1, 'A' if same_contract else 'B', 'C2', '50'),
        ]:
            context = qc.create_period_context(
                conn, info.project_id, direction='downward', contract_key=key,
                business_period_no=number, unit_name=key, doc_kind='settlement',
                amount_mode=mode, work_scope='材料', actor='user', reason='合成业务范围',
            )
            conn.execute(
                'INSERT INTO line_items(period_id,code,name,feature,unit,quantity,unit_price,amount)'
                ' VALUES (?,?,?,?,?,?,?,?)',
                (context['period_id'], code, code, '标准特征', 'm3', quantity, '1', quantity),
            )
            if confirmed:
                qc.confirm_period_context(conn, info.project_id, context['context_id'],
                                          actor='user', reason='范围和计量模式确认')
        item = next(a for a in aggregate.aggregate_project(conn, info.project_id)
                    if a.code == 'C1')
        assert item.status == expected_status
        assert all('疑似漏项' not in warning for warning in item.warnings)
        if expected_status == 'incomplete':
            assert any('覆盖' in warning for warning in item.warnings)
    finally:
        conn.close()
