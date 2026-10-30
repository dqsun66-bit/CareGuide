from datetime import datetime, timedelta, timezone

import pytest

from careguide.business_store import BusinessStore


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
