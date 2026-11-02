import asyncio
import json
from datetime import datetime, timedelta, timezone

import pytest

from careguide.business_store import BusinessStore
from careguide.amap_client import AmapProviderError
from careguide.amap_models import Coordinate, HospitalPoi, ParkingCandidate
from careguide.map_parking_chat import MapParkingChatService
from careguide.trace_store import TRACE_VERSION, TraceStore, validate_trace_completeness


NOW = datetime(2026, 10, 30, 8, tzinfo=timezone.utc)
EXPIRES_AT = NOW + timedelta(minutes=10)
HOSPITAL = {
    'hospital_id': 'H1',
    'name': '泰州市人民医院（总院）',
    'address': '太湖路366号',
    'city': '泰州市',
    'district': '医药高新区',
    'location': {'longitude': 119.923, 'latitude': 32.455},
    'entrance': {'longitude': 119.9228, 'latitude': 32.4552},
    'data_tier': 'map_poi',
    'source_provider': 'amap',
    'observed_at': NOW.isoformat(),
}


def hospital_poi(identity='H1', name='泰州市人民医院（总院）'):
    return HospitalPoi(
        hospital_id=identity,
        name=name,
        address='太湖路366号',
        city_name='泰州市',
        district_name='医药高新区',
        location=Coordinate(longitude=119.923, latitude=32.455),
        entrance_location=Coordinate(longitude=119.9228, latitude=32.4552),
        observed_at=NOW,
    )


def parking_poi(identity='P1', name='泰州市人民医院停车场'):
    return ParkingCandidate(
        parking_id=identity,
        name=name,
        address='太湖路366号',
        location=Coordinate(longitude=119.9227, latitude=32.4551),
        entrance_location=Coordinate(longitude=119.9226, latitude=32.4550),
        parking_type='地下',
        distance_to_hospital_m=85,
        available_spaces=None,
        wheelchair_accessible=None,
        observed_at=NOW,
    )


class ScriptedAmap:
    def __init__(self, hospitals, *, refreshed=None, parking=None, parking_error=None):
        self.hospitals = list(hospitals)
        self.refreshed = refreshed
        self.parking = list(parking or [])
        self.parking_error = parking_error
        self.calls = []
        self.detail_calls = []
        self.parking_calls = []

    async def search_hospitals(self, query, *, city):
        self.calls.append({'query': query, 'city': city})
        return list(self.hospitals)

    async def get_hospital(self, hospital_id):
        self.detail_calls.append(hospital_id)
        return self.refreshed

    async def search_parking(self, hospital, *, radius_m):
        self.parking_calls.append({'hospital': hospital, 'radius_m': radius_m})
        if self.parking_error:
            raise self.parking_error
        return list(self.parking)


def chat_service(tmp_path, hospitals):
    amap = ScriptedAmap(hospitals)
    store = BusinessStore(tmp_path / 'chat-business.sqlite3')
    traces = TraceStore(path=None)
    service = MapParkingChatService(amap, store, traces, now=lambda: NOW)
    return service, amap, store, traces


@pytest.fixture
def store(tmp_path):
    return BusinessStore(tmp_path / 'business.sqlite3')


@pytest.fixture
def query(store):
    return store.create_map_parking_query(
        'alice',
        'conversation-1',
        '泰州市人民医院',
        '泰州市',
        [HOSPITAL],
        expires_at=EXPIRES_AT,
        now=NOW,
    )


def test_create_and_select_map_hospital(store, query):
    assert query['query_id'].startswith('MAP-')
    assert query['candidate_version'] == 1
    assert query['actor'] == 'alice'
    assert query['conversation'] == 'conversation-1'
    assert query['query'] == '泰州市人民医院'
    assert query['city'] == '泰州市'
    assert query['hospitals'] == [HOSPITAL]
    assert query['created_at'] == NOW.isoformat()
    assert query['expires_at'] == EXPIRES_AT.isoformat()

    selected = store.select_map_hospital(
        query['query_id'], 'alice', 'conversation-1', 1, 'H1', now=NOW
    )

    assert selected == HOSPITAL


def test_create_rejects_naive_expires_at(store):
    with pytest.raises(ValueError, match='expires_at must be timezone-aware'):
        store.create_map_parking_query(
            'alice',
            'conversation-1',
            '泰州市人民医院',
            '泰州市',
            [HOSPITAL],
            expires_at=EXPIRES_AT.replace(tzinfo=None),
            now=NOW,
        )


def test_create_rejects_naive_now(store):
    with pytest.raises(ValueError, match='now must be timezone-aware'):
        store.create_map_parking_query(
            'alice',
            'conversation-1',
            '泰州市人民医院',
            '泰州市',
            [HOSPITAL],
            expires_at=EXPIRES_AT,
            now=NOW.replace(tzinfo=None),
        )


def test_create_normalizes_offset_times_and_select_expires_at_boundary(store):
    china_timezone = timezone(timedelta(hours=8))
    local_now = NOW.astimezone(china_timezone)
    local_expires_at = EXPIRES_AT.astimezone(china_timezone)
    item = store.create_map_parking_query(
        'alice',
        'conversation-1',
        '泰州市人民医院',
        '泰州市',
        [HOSPITAL],
        expires_at=local_expires_at,
        now=local_now,
    )

    assert item['created_at'] == NOW.isoformat()
    assert item['expires_at'] == EXPIRES_AT.isoformat()
    with pytest.raises(TimeoutError):
        store.select_map_hospital(
            item['query_id'],
            'alice',
            'conversation-1',
            1,
            'H1',
            now=local_expires_at,
        )


def test_create_prunes_expired_map_parking_queries_in_same_transaction(store):
    expired = store.create_map_parking_query(
        'alice',
        'conversation-1',
        '旧医院',
        '泰州市',
        [HOSPITAL],
        expires_at=NOW + timedelta(minutes=1),
        now=NOW,
    )

    current = store.create_map_parking_query(
        'alice',
        'conversation-1',
        '新医院',
        '泰州市',
        [HOSPITAL],
        expires_at=NOW + timedelta(minutes=20),
        now=NOW + timedelta(minutes=2),
    )

    with store.connect() as connection:
        identities = [
            row['id'] for row in connection.execute(
                'SELECT id FROM map_parking_queries ORDER BY id'
            )
        ]

    assert expired['query_id'] not in identities
    assert current['query_id'] in identities


@pytest.mark.parametrize(
    ('actor', 'conversation'),
    [
        ('mallory', 'conversation-1'),
        ('alice', 'conversation-2'),
    ],
)
def test_select_rejects_other_scope(store, query, actor, conversation):
    with pytest.raises(PermissionError):
        store.select_map_hospital(
            query['query_id'], actor, conversation, 1, 'H1', now=NOW
        )


def test_select_rejects_stale_version(store, query):
    with pytest.raises(ValueError):
        store.select_map_hospital(
            query['query_id'], 'alice', 'conversation-1', 2, 'H1', now=NOW
        )


def test_select_rejects_hospital_outside_stored_candidates(store, query):
    with pytest.raises(ValueError):
        store.select_map_hospital(
            query['query_id'], 'alice', 'conversation-1', 1, 'OTHER', now=NOW
        )


def test_select_rejects_expired_query_after_eleven_minutes(store, query):
    with pytest.raises(TimeoutError):
        store.select_map_hospital(
            query['query_id'],
            'alice',
            'conversation-1',
            1,
            'H1',
            now=NOW + timedelta(minutes=11),
        )


def test_select_rejects_missing_query(store):
    with pytest.raises(LookupError):
        store.select_map_hospital(
            'MAP-missing', 'alice', 'conversation-1', 1, 'H1', now=NOW
        )


def test_select_rejects_naive_now(store, query):
    with pytest.raises(ValueError, match='now must be timezone-aware'):
        store.select_map_hospital(
            query['query_id'],
            'alice',
            'conversation-1',
            1,
            'H1',
            now=NOW.replace(tzinfo=None),
        )


def test_select_rejects_naive_stored_expiry(store, query):
    with store.connect() as connection:
        connection.execute(
            'UPDATE map_parking_queries SET expires_at=? WHERE id=?',
            (EXPIRES_AT.replace(tzinfo=None).isoformat(), query['query_id']),
        )

    with pytest.raises(ValueError, match='Stored expires_at must be timezone-aware'):
        store.select_map_hospital(
            query['query_id'], 'alice', 'conversation-1', 1, 'H1', now=NOW
        )


def test_select_treats_expiry_instant_as_expired(store, query):
    with pytest.raises(TimeoutError):
        store.select_map_hospital(
            query['query_id'],
            'alice',
            'conversation-1',
            1,
            'H1',
            now=EXPIRES_AT,
        )


def test_selected_hospital_cannot_mutate_stored_candidate(store, query):
    selected = store.select_map_hospital(
        query['query_id'], 'alice', 'conversation-1', 1, 'H1', now=NOW
    )
    selected['name'] = '被篡改的医院'
    selected['location']['longitude'] = 0

    selected_again = store.select_map_hospital(
        query['query_id'], 'alice', 'conversation-1', 1, 'H1', now=NOW
    )

    assert selected_again == HOSPITAL


def test_real_parking_query_searches_exact_hospital_and_waits_for_confirmation(tmp_path):
    hospitals = [hospital_poi(), hospital_poi('H2', '泰州市中医院')]
    service, amap, store, traces = chat_service(tmp_path, hospitals)

    result = asyncio.run(service.handle(
        '查询泰州医院停车场', 'alice', 'conversation-map'))

    assert amap.calls == [{'query': '泰州医院', 'city': '泰州'}]
    assert result['intent'] == 'map_parking'
    assert result['intents'] == ['map_parking']
    assert result['intent_group'] == 'map_parking'
    assert result['workflow_status'] == 'waiting_user'
    assert result['simulation'] is False
    assert result['execution_mode'] == 'deterministic_tool'
    selection = result['map_selection']
    assert selection['candidate_version'] == 1
    assert selection['expires_at'] == EXPIRES_AT.isoformat()
    assert [item['hospital_id'] for item in selection['hospitals']] == ['H1', 'H2']
    assert set(selection['hospitals'][0]) == {
        'hospital_id', 'name', 'address', 'city_name', 'district_name', 'location',
        'entrance_location', 'data_tier', 'source_provider', 'observed_at',
    }
    stored = store.select_map_hospital(
        selection['query_id'], 'alice', 'conversation-map', 1, 'H1', now=NOW)
    assert stored == selection['hospitals'][0]
    assert HospitalPoi.model_validate(stored) == hospitals[0]

    recent = traces.list_recent()
    assert len(recent) == 1
    trace = recent[0]
    assert trace['trace_version'] == TRACE_VERSION
    assert validate_trace_completeness(trace)['complete']
    assert trace['task_understanding']['task_type'] == 'map_parking'
    assert trace['task_understanding']['missing_fields'] == {'map_parking': ['hospital_id']}
    assert trace['routing']['primary_agent'] == 'map_parking'
    assert trace['execution']['budget']['model_calls'] == 0
    assert trace['execution']['budget']['tool_calls'] == 1
    assert trace['final']['total_tool_calls'] == 1
    assert result['tools_used'] == ['amap_search_hospitals']
    assert trace['execution']['execution_waves'][0]['agents'][0]['tools_used'] == [
        'amap_search_hospitals'
    ]
    tool_call = trace['execution']['tool_calls'][0]
    assert tool_call['tool_name'] == 'amap_search_hospitals'
    assert tool_call['input'] == {'query': '泰州医院', 'city': '泰州'}
    assert tool_call['output'] == {'count': 2, 'hospital_ids': ['H1', 'H2']}
    serialized = json.dumps(trace, ensure_ascii=False).lower()
    assert 'key' not in serialized
    assert 'url' not in serialized
    assert 'sim-' not in serialized


def test_single_hospital_candidate_still_waits_for_user_confirmation(tmp_path):
    service, amap, _store, traces = chat_service(tmp_path, [hospital_poi()])

    result = asyncio.run(service.handle(
        '开车去南京市第一医院，停车怎么停', 'alice', 'conversation-single'))

    assert amap.calls == [{'query': '南京市第一医院', 'city': '南京'}]
    assert result['workflow_status'] == 'waiting_user'
    assert len(result['map_selection']['hospitals']) == 1
    assert len(traces.list_recent()) == 1


def test_city_suffix_hint_is_extracted_from_conversational_message(tmp_path):
    service, amap, _store, _traces = chat_service(tmp_path, [hospital_poi()])

    asyncio.run(service.handle(
        '我在无锡市人民医院，车停哪里', 'alice', 'conversation-city'))

    assert amap.calls == [{'query': '无锡市人民医院', 'city': '无锡'}]


@pytest.mark.parametrize(('message', 'query', 'city'), [
    ('去无锡市人民医院停车', '无锡市人民医院', '无锡'),
    ('前往常州市第一人民医院停车场', '常州市第一人民医院', '常州'),
    ('麻烦去常州市第一人民医院停车场', '常州市第一人民医院', '常州'),
    ('能不能前往无锡市人民医院停车', '无锡市人民医院', '无锡'),
    ('去江苏省无锡市人民医院停车', '江苏省无锡市人民医院', '无锡'),
    ('前往广西壮族自治区南宁市医院停车', '广西壮族自治区南宁市医院', '南宁'),
])
def test_city_suffix_hint_does_not_absorb_leading_travel_verbs(
        tmp_path, message, query, city):
    service, amap, _store, _traces = chat_service(tmp_path, [hospital_poi()])

    asyncio.run(service.handle(message, 'alice', 'conversation-city-verb'))

    assert amap.calls == [{'query': query, 'city': city}]


def test_negated_simulation_word_still_uses_real_map_handler(tmp_path):
    service, amap, _store, traces = chat_service(tmp_path, [hospital_poi()])

    result = asyncio.run(service.handle(
        '不要模拟结果，查询泰州医院停车场', 'alice', 'conversation-real-map'))

    assert amap.calls == [{'query': '泰州医院', 'city': '泰州'}]
    assert result['workflow_status'] == 'waiting_user'
    assert result['simulation'] is False
    assert len(traces.list_recent()) == 1


def test_drive_to_hospital_with_medical_request_is_not_claimed(tmp_path):
    service, amap, _store, traces = chat_service(tmp_path, [hospital_poi()])

    result = asyncio.run(service.handle(
        '开车去医院挂号需要什么材料', 'alice', 'conversation-medical'))

    assert result is None
    assert amap.calls == []
    assert traces.list_recent() == []


@pytest.mark.parametrize('message', [
    '开车去南京鼓楼医院复诊要带什么',
    '开车去南京鼓楼医院住院怎么办',
    '开车去南京鼓楼医院手术要准备什么',
])
def test_drive_to_hospital_with_non_travel_suffix_is_not_claimed(tmp_path, message):
    service, amap, _store, traces = chat_service(tmp_path, [hospital_poi()])

    assert asyncio.run(service.handle(
        message, 'alice', 'conversation-medical-structure')) is None
    assert amap.calls == []
    assert traces.list_recent() == []


@pytest.mark.parametrize('message', [
    '开车去南京鼓楼医院',
    '开车去南京鼓楼医院怎么走',
])
def test_drive_to_hospital_with_only_travel_suffix_is_claimed(tmp_path, message):
    service, amap, _store, traces = chat_service(tmp_path, [hospital_poi()])

    result = asyncio.run(service.handle(
        message, 'alice', 'conversation-travel-structure'))

    assert amap.calls == [{'query': '南京鼓楼医院', 'city': '南京'}]
    assert result['workflow_status'] == 'waiting_user'
    assert len(traces.list_recent()) == 1


def test_explicit_parking_still_wins_when_message_mentions_medical_care(tmp_path):
    service, amap, _store, traces = chat_service(tmp_path, [hospital_poi()])

    result = asyncio.run(service.handle(
        '开车去医院看病，停车场在哪', 'alice', 'conversation-parking'))

    assert amap.calls == [{'query': '医院', 'city': ''}]
    assert result['workflow_status'] == 'waiting_user'
    assert len(traces.list_recent()) == 1


def test_directional_target_preserves_full_hospital_campus_name(tmp_path):
    service, amap, _store, _traces = chat_service(tmp_path, [hospital_poi()])

    asyncio.run(service.handle(
        '开车去南京鼓楼医院江北院区停车', 'alice', 'conversation-campus'))

    assert amap.calls == [{'query': '南京鼓楼医院江北院区', 'city': '南京'}]


def test_extended_negated_simulation_still_calls_real_map_handler(tmp_path):
    service, amap, _store, traces = chat_service(tmp_path, [hospital_poi()])

    result = asyncio.run(service.handle(
        '不要再模拟医院停车，查询真实停车场', 'alice', 'conversation-real-only'))

    assert amap.calls == [{'query': '真实', 'city': ''}]
    assert result['workflow_status'] == 'waiting_user'
    assert result['simulation'] is False
    assert len(traces.list_recent()) == 1


@pytest.mark.parametrize(('message', 'query', 'city'), [
    ('我在南京，想开车去上海市第一人民医院停车', '上海市第一人民医院', '上海'),
    ('从苏州前往无锡市人民医院附近停车', '无锡市人民医院', '无锡'),
])
def test_destination_hospital_fragment_wins_over_origin_city(
        tmp_path, message, query, city):
    service, amap, _store, _traces = chat_service(tmp_path, [hospital_poi()])

    asyncio.run(service.handle(message, 'alice', 'conversation-destination'))

    assert amap.calls == [{'query': query, 'city': city}]


def test_local_parking_clause_wins_without_direction_word(tmp_path):
    service, amap, _store, _traces = chat_service(tmp_path, [hospital_poi()])

    asyncio.run(service.handle(
        '我在南京，上海市第一人民医院停车场在哪',
        'alice',
        'conversation-local-clause',
    ))

    assert amap.calls == [{'query': '上海市第一人民医院', 'city': '上海'}]


@pytest.mark.parametrize(('message', 'query', 'city'), [
    ('查询北京大学深圳医院停车场', '北京大学深圳医院', '深圳'),
    ('查询上海交大附属苏州九龙医院停车场', '上海交大附属苏州九龙医院', '苏州'),
    ('查询武汉协和医院停车场', '武汉协和医院', ''),
    ('查询成都华西医院停车场', '成都华西医院', ''),
])
def test_city_hint_uses_nearest_reliable_target_city_or_nationwide_search(
        tmp_path, message, query, city):
    service, amap, _store, _traces = chat_service(tmp_path, [hospital_poi()])

    asyncio.run(service.handle(message, 'alice', 'conversation-city-reliability'))

    assert amap.calls == [{'query': query, 'city': city}]


def test_no_hospital_candidate_completes_without_simulated_data(tmp_path):
    service, amap, _store, traces = chat_service(tmp_path, [])

    result = asyncio.run(service.handle(
        '帮我查找上海医院附近停车场', 'alice', 'conversation-empty'))

    assert amap.calls == [{'query': '上海医院', 'city': '上海'}]
    assert result['workflow_status'] == 'completed'
    assert result['simulation'] is False
    assert '没有找到' in result['response']
    assert '城市' in result['response'] and '正式名称' in result['response']
    assert 'map_selection' not in result
    assert 'SIM-' not in json.dumps(result, ensure_ascii=False)
    assert len(traces.list_recent()) == 1
    assert validate_trace_completeness(traces.list_recent()[0])['complete']


@pytest.mark.parametrize('message', [
    '医院怎么挂号',
    '最近总是头痛怎么办',
    '请模拟停车方案',
    '请模拟医院停车方案',
    '请模拟一下医院停车方案',
    '医院停车模拟一下',
    '这个医院停车方案做个模拟',
    '请对医院停车做模拟重规划',
    '医院停车场 SIM-A 满了怎么办',
])
def test_unrelated_or_explicit_simulation_requests_are_not_claimed(tmp_path, message):
    service, amap, _store, traces = chat_service(tmp_path, [hospital_poi()])

    assert asyncio.run(service.handle(message, 'alice', 'conversation-other')) is None
    assert amap.calls == []
    assert traces.list_recent() == []


def _selection_event(result, hospital_id='H1', **extra):
    selection = result['map_selection']
    return {
        'kind': 'map_hospital_select',
        'query_id': selection['query_id'],
        'candidate_version': selection['candidate_version'],
        'hospital_id': hospital_id,
        **extra,
    }


def test_selected_hospital_refreshes_detail_and_returns_real_parking(tmp_path):
    stored = hospital_poi()
    refreshed = hospital_poi(name='泰州市人民医院总院（刷新）')
    parking = parking_poi()
    amap = ScriptedAmap([stored], refreshed=refreshed, parking=[parking])
    store = BusinessStore(tmp_path / 'selected.sqlite3')
    traces = TraceStore(path=None)
    service = MapParkingChatService(amap, store, traces, now=lambda: NOW)
    waiting = asyncio.run(service.handle(
        '查询泰州市人民医院停车场', 'alice', 'conversation-select'))
    trace_count = len(traces.list_recent())

    result = asyncio.run(service.handle(
        '确认该院区', 'alice', 'conversation-select',
        map_event=_selection_event(waiting),
    ))

    assert amap.detail_calls == ['H1']
    assert amap.parking_calls == [{'hospital': refreshed, 'radius_m': 1000}]
    assert result['workflow_status'] == 'completed'
    assert result['simulation'] is False
    assert result['intent'] == 'map_parking'
    assert result['map_parking'] == {
        'hospital': refreshed.model_dump(mode='json'),
        'candidates': [parking.model_dump(mode='json')],
        'data_tier': 'map_poi',
        'source_provider': 'amap',
        'observed_at': NOW.isoformat(),
    }
    assert '确认院区' in result['response']
    assert '1 个停车地点' in result['response']
    assert '无法核实实时余位和无障碍通行' in result['response']
    assert parking.available_spaces is None
    assert parking.wheelchair_accessible is None
    assert 'SIM' not in json.dumps(result, ensure_ascii=False)

    assert len(traces.list_recent()) == trace_count + 1
    trace = traces.list_recent()[0]
    tool_calls = trace['execution']['tool_calls']
    assert len(tool_calls) == 2
    assert tool_calls[0] == {
        'tool_name': 'amap_get_hospital',
        'agent_type': 'map_parking',
        'input': {'hospital_id': 'H1'},
        'success': True,
        'output': {'found': True, 'hospital_id': 'H1'},
        'error': None,
        'latency_ms': tool_calls[0]['latency_ms'],
    }
    assert tool_calls[1] == {
        'tool_name': 'amap_search_parking',
        'agent_type': 'map_parking',
        'input': {'hospital_id': 'H1', 'radius_m': 1000},
        'success': True,
        'output': {'count': 1, 'parking_ids': ['P1']},
        'error': None,
        'latency_ms': tool_calls[1]['latency_ms'],
    }
    assert trace['execution']['budget']['tool_calls'] == 2
    assert trace['final']['total_tool_calls'] == 2
    assert result['tools_used'] == ['amap_get_hospital', 'amap_search_parking']
    assert trace['execution']['execution_waves'][0]['agents'][0]['tools_used'] == [
        'amap_get_hospital', 'amap_search_parking'
    ]
    assert 'SIM' not in json.dumps(trace, ensure_ascii=False)


def test_selection_uses_stored_candidate_when_detail_refresh_is_missing(tmp_path):
    stored = hospital_poi()
    amap = ScriptedAmap([stored], refreshed=None, parking=[])
    service = MapParkingChatService(
        amap,
        BusinessStore(tmp_path / 'fallback.sqlite3'),
        TraceStore(path=None),
        now=lambda: NOW,
    )
    waiting = asyncio.run(service.handle(
        '查询泰州市人民医院停车场', 'alice', 'conversation-fallback'))

    result = asyncio.run(service.handle(
        '确认', 'alice', 'conversation-fallback',
        map_event=_selection_event(waiting),
    ))

    assert amap.parking_calls == [{'hospital': stored, 'radius_m': 1000}]
    assert result['map_parking']['hospital'] == stored.model_dump(mode='json')
    assert result['map_parking']['candidates'] == []
    assert '1 公里内未找到' in result['response']
    assert '地图页扩大范围' in result['response']
    assert result['simulation'] is False
    trace = service.traces.list_recent()[0]
    assert trace['execution']['tool_calls'][0]['output'] == {
        'found': False,
        'hospital_id': None,
    }


def test_selection_rejects_mismatched_refreshed_hospital_id(tmp_path):
    stored = hospital_poi()
    mismatched = hospital_poi(identity='H2', name='错误院区')
    amap = ScriptedAmap([stored], refreshed=mismatched, parking=[parking_poi()])
    traces = TraceStore(path=None)
    service = MapParkingChatService(
        amap,
        BusinessStore(tmp_path / 'mismatched-refresh.sqlite3'),
        traces,
        now=lambda: NOW,
    )
    waiting = asyncio.run(service.handle(
        '查询泰州市人民医院停车场', 'alice', 'conversation-mismatch'))
    trace_count = len(traces.list_recent())

    with pytest.raises(AmapProviderError) as caught:
        asyncio.run(service.handle(
            '确认', 'alice', 'conversation-mismatch',
            map_event=_selection_event(waiting),
        ))

    assert caught.value.code == 'provider_invalid_response'
    assert str(caught.value) == '地图服务返回的医院信息不一致'
    assert amap.detail_calls == ['H1']
    assert amap.parking_calls == []
    assert len(traces.list_recent()) == trace_count


@pytest.mark.parametrize(
    ('event_change', 'actor', 'conversation', 'error_type'),
    [
        ({'hospital_id': 'FORGED'}, 'alice', 'conversation-secure', ValueError),
        ({}, 'mallory', 'conversation-secure', PermissionError),
        ({}, 'alice', 'conversation-other', PermissionError),
        ({'candidate_version': 2}, 'alice', 'conversation-secure', ValueError),
        ({'kind': 'other'}, 'alice', 'conversation-secure', ValueError),
    ],
)
def test_selection_rejects_forged_or_wrong_scope_event(
        tmp_path, event_change, actor, conversation, error_type):
    amap = ScriptedAmap([hospital_poi()], refreshed=hospital_poi())
    service = MapParkingChatService(
        amap, BusinessStore(tmp_path / 'secure.sqlite3'), TraceStore(path=None),
        now=lambda: NOW,
    )
    waiting = asyncio.run(service.handle(
        '查询泰州市人民医院停车场', 'alice', 'conversation-secure'))
    event = _selection_event(waiting)
    event.update(event_change)

    with pytest.raises(error_type):
        asyncio.run(service.handle(
            '确认', actor, conversation, map_event=event))

    assert amap.detail_calls == []
    assert amap.parking_calls == []


def test_selection_rejects_expired_event(tmp_path):
    amap = ScriptedAmap([hospital_poi()])
    current = [NOW]
    service = MapParkingChatService(
        amap, BusinessStore(tmp_path / 'expired.sqlite3'), TraceStore(path=None),
        now=lambda: current[0],
    )
    waiting = asyncio.run(service.handle(
        '查询泰州市人民医院停车场', 'alice', 'conversation-expired'))
    current[0] = EXPIRES_AT

    with pytest.raises(TimeoutError):
        asyncio.run(service.handle(
            '确认', 'alice', 'conversation-expired',
            map_event=_selection_event(waiting),
        ))

    assert amap.detail_calls == []
    assert amap.parking_calls == []


def test_selection_ignores_forged_event_data_and_uses_stored_candidate_id(tmp_path):
    stored = hospital_poi()
    refreshed = hospital_poi(name='可信刷新详情')
    parking = parking_poi()
    amap = ScriptedAmap([stored], refreshed=refreshed, parking=[parking])
    service = MapParkingChatService(
        amap, BusinessStore(tmp_path / 'ignore-forged.sqlite3'), TraceStore(path=None),
        now=lambda: NOW,
    )
    waiting = asyncio.run(service.handle(
        '查询泰州市人民医院停车场', 'alice', 'conversation-forged-data'))
    event = _selection_event(
        waiting,
        location={'longitude': 73, 'latitude': 3},
        address='伪造地址',
        hospital={'hospital_id': 'EVIL'},
        radius_m=50000,
        available_spaces=999,
        wheelchair_accessible=True,
    )

    result = asyncio.run(service.handle(
        '确认', 'alice', 'conversation-forged-data', map_event=event))

    assert amap.detail_calls == ['H1']
    assert amap.parking_calls == [{'hospital': refreshed, 'radius_m': 1000}]
    candidate = result['map_parking']['candidates'][0]
    assert candidate['available_spaces'] is None
    assert candidate['wheelchair_accessible'] is None
    assert result['map_parking']['hospital']['address'] == refreshed.address


@pytest.mark.parametrize('code', [
    'provider_auth_failed', 'provider_rate_limited', 'provider_timeout',
])
def test_selection_propagates_provider_errors_without_trace_or_simulation(tmp_path, code):
    provider_error = AmapProviderError(code, '安全地图错误')
    amap = ScriptedAmap(
        [hospital_poi()], refreshed=hospital_poi(), parking_error=provider_error)
    traces = TraceStore(path=None)
    service = MapParkingChatService(
        amap, BusinessStore(tmp_path / f'{code}.sqlite3'), traces, now=lambda: NOW)
    waiting = asyncio.run(service.handle(
        '查询泰州市人民医院停车场', 'alice', f'conversation-{code}'))
    trace_count = len(traces.list_recent())

    with pytest.raises(AmapProviderError) as caught:
        asyncio.run(service.handle(
            '确认', 'alice', f'conversation-{code}',
            map_event=_selection_event(waiting),
        ))

    assert caught.value is provider_error
    assert caught.value.code == code
    assert 'key' not in str(caught.value).lower()
    assert 'http' not in str(caught.value).lower()
    assert 'sim' not in str(caught.value).lower()
    assert len(traces.list_recent()) == trace_count
