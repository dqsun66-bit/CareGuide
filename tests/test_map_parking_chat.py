import asyncio
import json
from datetime import datetime, timedelta, timezone

import pytest

from careguide.business_store import BusinessStore
from careguide.amap_models import Coordinate, HospitalPoi
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


class ScriptedAmap:
    def __init__(self, hospitals):
        self.hospitals = list(hospitals)
        self.calls = []

    async def search_hospitals(self, query, *, city):
        self.calls.append({'query': query, 'city': city})
        return list(self.hospitals)


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

    assert amap.calls == [{'query': '医院', 'city': '泰州'}]
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

    assert amap.calls == [{'query': '真实', 'city': '泰州'}]
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
