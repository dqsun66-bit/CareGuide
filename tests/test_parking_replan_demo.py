from fastapi.testclient import TestClient

from careguide.api import app
from careguide.runtime import get_chat_runtime
from careguide.task_context import SessionMemory
from careguide.trace_store import TraceStore


def test_parking_event_replans_to_confirmed_accessible_alternative():
    response = TestClient(app).post('/demo/parking/replan', json={
        'hospital_id': 'tzph-taihu-demo',
        'max_walking_distance_m': 200,
        'requires_wheelchair_access': True,
        'event': {'parking_lot_id': 'SIM-A', 'available_spaces': 0},
    })

    assert response.status_code == 200
    result = response.json()
    assert result['simulation'] is True
    assert result['before']['selected_lot_id'] == 'SIM-A'
    assert result['after']['selected_lot_id'] == 'SIM-B'
    assert result['after']['candidates'][0]['reasons'] == ['无空位']
    assert '不可用于实际就医或导航' in result['notice']


def test_parking_demo_rejects_an_unknown_event_target():
    response = TestClient(app).post('/demo/parking/replan', json={
        'hospital_id': 'tzph-taihu-demo',
        'max_walking_distance_m': 200,
        'requires_wheelchair_access': True,
        'event': {'parking_lot_id': 'SIM-UNKNOWN', 'available_spaces': 1},
    })

    assert response.status_code == 422
    assert response.json()['detail'] == '模拟事件中的停车场不存在'


def test_parking_demo_requires_its_explicit_simulation_dataset():
    response = TestClient(app).post('/demo/parking/replan', json={
        'hospital_id': 'real-hospital',
        'max_walking_distance_m': 200,
        'requires_wheelchair_access': True,
        'event': {'parking_lot_id': 'SIM-A', 'available_spaces': 0},
    })

    assert response.status_code == 422
    assert response.json()['detail'] == '当前仅支持明确标识的模拟医院数据'


def test_chat_routes_explicit_taizhou_mobility_request_to_parking_tool():
    class EngineThatMustNotPlan:
        def __init__(self):
            self.traces = TraceStore(path=None)

        async def process(self, *args, **kwargs):
            raise AssertionError('明确的停车演示请求不应落入普通模型规划')

    engine = EngineThatMustNotPlan()
    memory = SessionMemory()
    app.dependency_overrides[get_chat_runtime] = lambda: (engine, memory, None)
    try:
        response = TestClient(app).post('/chat', json={
            'message': '我明天想去泰州市医院看病，头有点晕，需要轮椅，最多走200米',
            'user_id': 'u001',
            'conv_id': 'parking-chat',
        })
    finally:
        app.dependency_overrides.pop(get_chat_runtime, None)

    assert response.status_code == 200
    result = response.json()
    assert result['simulation'] is True
    assert result['intent'] == 'appointment'
    assert result['tools_used'] == ['replan_parking_demo']
    assert result['parking_plan']['before']['selected_lot_id'] == 'SIM-A'
    assert result['parking_plan']['after']['selected_lot_id'] == 'SIM-B'
    assert '最多步行 200 米' in result['response']
    assert '不可用于实际就医或导航' in result['response']
    trace = engine.traces.get(result['request_id'])
    assert trace['execution']['tool_calls'][0]['input']['max_walking_distance_m'] == 200
    assert trace['execution']['tool_calls'][0]['tool_name'] == 'replan_parking_demo'


def test_chat_does_not_invent_distance_or_parking_event_when_constraints_are_missing():
    class EngineThatMustNotPlan:
        def __init__(self):
            self.traces = TraceStore(path=None)

        async def process(self, *args, **kwargs):
            raise AssertionError('明确的行动约束澄清不应落入普通模型规划')

    engine = EngineThatMustNotPlan()
    app.dependency_overrides[get_chat_runtime] = lambda: (engine, SessionMemory(), None)
    try:
        response = TestClient(app).post('/chat', json={
            'message': '我明天想去泰州市医院，腿有点不舒服，现在不方便坐着轮椅',
            'user_id': 'u001',
            'conv_id': 'parking-clarify',
        })
    finally:
        app.dependency_overrides.pop(get_chat_runtime, None)

    assert response.status_code == 200
    result = response.json()
    assert result['workflow_status'] == 'waiting_user'
    assert result['tools_used'] == []
    assert '太湖路总院区' in result['response']
    assert '最多可以移动多远' in result['response']
    assert '200 米' not in result['response']
    assert 'SIM-A' not in result['response']
    assert 'parking_plan' not in result
    trace = engine.traces.get(result['request_id'])
    assert trace['execution']['tool_calls'] == []
