import json
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from careguide.amap_models import (
    Coordinate,
    HospitalPoi,
    MapRoute,
    ParkingCandidate,
)
from careguide.amap_client import AmapProviderError
from careguide.api import app, get_amap_client
from careguide.business_store import BusinessStore
from careguide.runtime import get_chat_runtime
from careguide.task_context import SessionMemory
from careguide.trace_store import TraceStore


NOW = datetime(2026, 10, 30, tzinfo=timezone.utc)
HOSPITAL = HospitalPoi(
    hospital_id='H1',
    name='泰州市人民医院（总院）',
    address='太湖路366号',
    location=Coordinate(longitude=119.923, latitude=32.455),
    observed_at=NOW,
)
PARKING = ParkingCandidate(
    parking_id='P1',
    name='泰州市人民医院停车场',
    address='太湖路366号',
    location=Coordinate(longitude=119.9228, latitude=32.4552),
    distance_to_hospital_m=85,
    observed_at=NOW,
)


class ScriptedAmap:
    def __init__(self, *, hospital_error=None):
        self.hospital_error = hospital_error
        self.hospital_calls = []
        self.detail_calls = []
        self.parking_calls = []

    async def search_hospitals(self, query, *, city):
        self.hospital_calls.append((query, city))
        if self.hospital_error:
            raise self.hospital_error
        return [HOSPITAL]

    async def get_hospital(self, hospital_id):
        self.detail_calls.append(hospital_id)
        assert hospital_id == 'H1'
        return HOSPITAL

    async def search_parking(self, hospital, *, radius_m):
        self.parking_calls.append((hospital, radius_m))
        assert hospital == HOSPITAL
        assert radius_m == 1000
        return [PARKING]

    async def route(self, request):
        return MapRoute(
            mode=request.mode,
            distance_m=120,
            duration_seconds=160,
            polyline=[request.origin, request.destination],
            observed_at=NOW,
        )


class ApiEngine:
    def __init__(self, tmp_path):
        self.business_store = BusinessStore(tmp_path / 'amap-api.sqlite3')
        self.traces = TraceStore(path=None)
        self.process_calls = []

    async def process(self, message, user_id, conv_id, history, **kwargs):
        self.process_calls.append((message, user_id, conv_id, kwargs))
        return {
            'request_id': 'engine-request', 'conversation_id': conv_id,
            'response': '普通医疗回答', 'workflow_status': 'completed',
            'workflow': None, 'simulation': False,
        }


@pytest.fixture
def chat_api(tmp_path, monkeypatch):
    import careguide.api as api_module

    engine = ApiEngine(tmp_path)
    amap = ScriptedAmap()
    app.dependency_overrides[get_chat_runtime] = lambda: (
        engine, SessionMemory(), None
    )
    monkeypatch.setattr(api_module, 'get_amap_client', lambda: amap)
    try:
        yield TestClient(app), engine, amap
    finally:
        app.dependency_overrides.pop(get_chat_runtime, None)


def decode_sse(text):
    output = []
    for block in text.split('\n\n'):
        fields = dict(
            line.split(': ', 1) for line in block.splitlines() if ': ' in line
        )
        if 'data' in fields:
            output.append((fields['event'], json.loads(fields['data'])))
    return output


def test_chat_real_parking_query_returns_hospital_candidates(chat_api):
    client, engine, amap = chat_api

    response = client.post('/chat', json={
        'message': '查询泰州医院停车场',
        'user_id': 'alice',
        'conv_id': 'map-chat',
    })

    assert response.status_code == 200
    result = response.json()
    assert result['intent'] == 'map_parking'
    assert result['workflow_status'] == 'waiting_user'
    assert result['simulation'] is False
    assert result['workflow'] is None
    assert result['appointment'] == []
    assert result['map_selection']['hospitals'][0]['hospital_id'] == 'H1'
    assert engine.process_calls == []
    assert len(amap.hospital_calls) == 1


def test_chat_stream_has_same_real_map_terminal_contract(chat_api):
    client, _engine, _amap = chat_api

    response = client.post('/chat/stream', json={
        'message': '查询泰州医院停车场',
        'user_id': 'alice',
        'conv_id': 'map-stream',
    })

    assert response.status_code == 200
    event, result = decode_sse(response.text)[-1]
    assert event == 'result'
    assert result['map_selection']['hospitals'][0]['hospital_id'] == 'H1'
    assert result['workflow_status'] == 'waiting_user'
    assert result['simulation'] is False


def test_structured_hospital_selection_returns_real_parking(chat_api):
    client, _engine, amap = chat_api
    first = client.post('/chat', json={
        'message': '查询泰州医院停车场', 'user_id': 'alice', 'conv_id': 'select'
    }).json()
    selection = first['map_selection']

    response = client.post('/chat', json={
        'message': '选择这个院区', 'user_id': 'alice', 'conv_id': 'select',
        'map_event': {
            'kind': 'map_hospital_select',
            'query_id': selection['query_id'],
            'candidate_version': selection['candidate_version'],
            'hospital_id': 'H1',
        },
    })

    assert response.status_code == 200
    result = response.json()
    assert result['map_parking']['candidates'][0]['parking_id'] == 'P1'
    assert result['simulation'] is False
    assert amap.detail_calls == ['H1']
    assert len(amap.parking_calls) == 1

    stream_response = client.post('/chat/stream', json={
        'message': '选择这个院区', 'user_id': 'alice', 'conv_id': 'select',
        'map_event': {
            'kind': 'map_hospital_select',
            'query_id': selection['query_id'],
            'candidate_version': selection['candidate_version'],
            'hospital_id': 'H1',
        },
    })
    event, stream_result = decode_sse(stream_response.text)[-1]
    assert event == 'result'
    assert stream_result['map_parking']['candidates'][0]['parking_id'] == 'P1'


@pytest.mark.parametrize('extra', [
    {'coordinate': {'longitude': 1, 'latitude': 2}},
    {'available_spaces': 5},
])
def test_map_event_rejects_extra_fields(chat_api, extra):
    client, _engine, _amap = chat_api
    event = {
        'kind': 'map_hospital_select', 'query_id': 'MAP-1',
        'candidate_version': 1, 'hospital_id': 'H1', **extra,
    }
    assert client.post('/chat', json={
        'message': '选择', 'map_event': event
    }).status_code == 422


def test_map_event_rejects_wrong_kind_and_mixed_workflow_event(chat_api):
    client, _engine, _amap = chat_api
    event = {
        'kind': 'not_map_select', 'query_id': 'MAP-1',
        'candidate_version': 1, 'hospital_id': 'H1',
    }
    assert client.post('/chat', json={'message': '选择', 'map_event': event}).status_code == 422
    event['kind'] = 'map_hospital_select'
    response = client.post('/chat', json={
        'message': '选择', 'conv_id': 'c', 'map_event': event,
        'workflow_event': {
            'kind': 'run', 'workflow_id': 'W1', 'plan_version': 1,
        },
    })
    assert response.status_code == 422


@pytest.mark.parametrize(('mutation', 'status'), [
    ({'user_id': 'bob'}, 422),
    ({'conv_id': 'other'}, 422),
    ({'candidate_version': 2}, 422),
    ({'query_id': 'MAP-forged'}, 404),
])
def test_map_selection_scope_version_and_identity_are_enforced(
        chat_api, mutation, status):
    client, _engine, amap = chat_api
    first = client.post('/chat', json={
        'message': '查询泰州医院停车场', 'user_id': 'alice', 'conv_id': 'owned'
    }).json()['map_selection']
    payload = {
        'message': '选择', 'user_id': 'alice', 'conv_id': 'owned',
        'map_event': {
            'kind': 'map_hospital_select', 'query_id': first['query_id'],
            'candidate_version': 1, 'hospital_id': 'H1',
        },
    }
    if mutation.keys() & {'user_id', 'conv_id'}:
        payload.update(mutation)
    else:
        payload['map_event'].update(mutation)

    response = client.post('/chat', json=payload)

    assert response.status_code == status
    assert amap.parking_calls == []
    assert 'scope' not in response.text
    assert 'version mismatch' not in response.text


@pytest.mark.parametrize(('code', 'status'), [
    ('provider_auth_failed', 503),
    ('provider_rate_limited', 429),
    ('provider_timeout', 503),
    ('provider_invalid_response', 503),
])
def test_chat_maps_provider_errors_without_leaking_secrets(
        tmp_path, monkeypatch, code, status):
    import careguide.api as api_module
    engine = ApiEngine(tmp_path)
    amap = ScriptedAmap(hospital_error=AmapProviderError(
        code, f'安全地图错误 {code}'
    ))
    app.dependency_overrides[get_chat_runtime] = lambda: (
        engine, SessionMemory(), None
    )
    monkeypatch.setattr(api_module, 'get_amap_client', lambda: amap)
    try:
        response = TestClient(app).post('/chat', json={
            'message': '查询泰州医院停车场'
        })
    finally:
        app.dependency_overrides.pop(get_chat_runtime, None)

    assert response.status_code == status
    assert response.json()['detail'] == {
        'code': code, 'message': f'安全地图错误 {code}'
    }
    assert 'restapi.amap.com' not in response.text
    assert 'key=' not in response.text.lower()


def test_expired_map_candidate_is_gone_and_does_not_query_parking(chat_api):
    client, engine, amap = chat_api
    expired = engine.business_store.create_map_parking_query(
        'alice', 'expired-conv', '泰州医院', '泰州',
        [HOSPITAL.model_dump(mode='json')],
        expires_at=datetime.now(timezone.utc) - timedelta(seconds=1),
    )
    response = client.post('/chat', json={
        'message': '选择', 'user_id': 'alice', 'conv_id': 'expired-conv',
        'map_event': {
            'kind': 'map_hospital_select', 'query_id': expired['query_id'],
            'candidate_version': 1, 'hospital_id': 'H1',
        },
    })

    assert response.status_code == 410
    assert '地图候选' in response.json()['detail']
    assert amap.parking_calls == []


def test_non_map_chat_never_constructs_amap_client(chat_api, monkeypatch):
    import careguide.api as api_module
    client, engine, _amap = chat_api
    monkeypatch.delenv('CAREGUIDE_AMAP_WEB_SERVICE_KEY', raising=False)
    monkeypatch.setattr(
        api_module, 'get_amap_client',
        lambda: (_ for _ in ()).throw(AssertionError('must stay lazy')),
    )

    response = client.post('/chat', json={'message': '感冒应该注意什么？'})

    assert response.status_code == 200
    assert response.json()['response'] == '普通医疗回答'
    assert len(engine.process_calls) == 1


def test_explicit_simulation_precedes_real_map_and_real_map_precedes_engine(
        chat_api):
    client, engine, amap = chat_api
    simulated = client.post('/chat', json={
        'message': '我明天想去泰州市医院看病，需要轮椅，最多走200米'
    })
    real = client.post('/chat', json={
        'message': '查询泰州医院停车场并预约'
    })

    assert simulated.status_code == 200
    assert simulated.json()['simulation'] is True
    assert real.status_code == 200
    assert real.json()['intent'] == 'map_parking'
    assert engine.process_calls == []
    assert len(amap.hospital_calls) == 1


def test_workflow_resume_remains_prior_to_map_message_matching(chat_api):
    client, engine, amap = chat_api
    engine.store = object()

    response = client.post('/chat', json={
        'message': '继续查询泰州医院停车场',
        'user_id': 'alice',
        'conv_id': 'workflow-map-text',
        'workflow_event': {
            'kind': 'run', 'workflow_id': 'W1', 'plan_version': 1,
        },
    })

    assert response.status_code == 200
    assert response.json()['response'] == '普通医疗回答'
    assert len(engine.process_calls) == 1
    assert amap.hospital_calls == []


def test_real_parking_endpoint_never_returns_simulation_or_fake_availability():
    app.dependency_overrides[get_amap_client] = ScriptedAmap
    try:
        response = TestClient(app).get('/map/hospitals/H1/parking?radius_m=1000')
    finally:
        app.dependency_overrides.pop(get_amap_client, None)

    assert response.status_code == 200
    payload = response.json()
    assert payload['simulation'] is False
    assert payload['candidates'][0]['available_spaces'] is None
    assert payload['candidates'][0]['wheelchair_accessible'] is None
    assert 'SIM-A' not in response.text


def test_hospital_search_and_route_are_source_labelled():
    app.dependency_overrides[get_amap_client] = ScriptedAmap
    try:
        client = TestClient(app)
        hospitals = client.get('/map/hospitals/search', params={
            'q': '泰州市人民医院', 'city': '泰州'
        })
        route = client.post('/map/routes', json={
            'origin': {'longitude': 119.923, 'latitude': 32.455},
            'destination': {'longitude': 119.9228, 'latitude': 32.4552},
            'mode': 'walking',
        })
    finally:
        app.dependency_overrides.pop(get_amap_client, None)

    assert hospitals.status_code == 200
    assert hospitals.json()['hospitals'][0]['data_tier'] == 'map_poi'
    assert hospitals.json()['simulation'] is False
    assert route.status_code == 200
    assert route.json()['route']['wheelchair_verified'] is False
    assert route.json()['simulation'] is False


def test_missing_provider_configuration_is_explicit(monkeypatch):
    monkeypatch.delenv('CAREGUIDE_AMAP_WEB_SERVICE_KEY', raising=False)
    get_amap_client.cache_clear()
    try:
        response = TestClient(app).get(
            '/map/hospitals/search?q=%E6%B3%B0%E5%B7%9E%E5%B8%82%E4%BA%BA%E6%B0%91%E5%8C%BB%E9%99%A2&city=%E6%B3%B0%E5%B7%9E'
        )
    finally:
        get_amap_client.cache_clear()

    assert response.status_code == 503
    assert response.json()['detail']['code'] == 'provider_unconfigured'
