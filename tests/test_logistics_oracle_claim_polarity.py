"""Reviewed literal-claim counterexamples; tool records here are test fixtures."""
import pytest
from skillforge.models import ToolCallRecord
from skillforge.scenarios.logistics import (
    MOCK_ORDERS, verify_logistics_fulfillment,
    verify_logistics_fulfillment_as_findings,
)


@pytest.mark.parametrize('order,text,passed', [
    ('ORD_DEV_0102', '全部为已签收状态，无异常标记。', True),
    ('ORD_DEV_0102', '各包裹 DELIVERED，没有异常。', True),
    ('ORD_DEV_0102', '全部已签收，未发现任何异常。', True),
    ('ORD_DEV_0102', '包裹仍在运输中。', False),
    ('ORD_2026_0902', '未全部签收，PKG_202运输中。', True),
    ('ORD_2026_0902', '全部签收，其中PKG_202运输中。', False),
    ('ORD_2026_0902', '全部为已签收状态。', False),
    ('ORD_2026_0902', '不是全部签收，PKG_202运输中。', True),
    ('ORD_2026_0902', '未全部签收。但确认全部签收。', False),
    ('ORD_2026_0902', '尚未全部签收，不满足“全部签收”条件。', True),
    ('ORD_2026_0902', '不符合\"全部签收\"条件，PKG_202运输中。', True),
])
def test_delivery_claim_polarity_both_oracles(order, text, passed):
    packages = MOCK_ORDERS[order]['packages']
    records = [ToolCallRecord(call_id='order', run_id='fixture',
        tool_name='query_order_packages', status='EXECUTED', input_params={'order_id':order})]
    records += [ToolCallRecord(call_id=p, run_id='fixture',
        tool_name='query_package_tracking', status='EXECUTED', input_params={'package_id':p}) for p in packages]
    answer = ' '.join(packages) + '：' + text
    binary = verify_logistics_fulfillment(answer, order, records)
    findings = verify_logistics_fulfillment_as_findings(answer, order, records)
    assert binary['independent_pass'] is passed, binary
    assert (findings['ROBUST_EVIDENCE_FAITHFUL'].status == 'PASS') is passed
