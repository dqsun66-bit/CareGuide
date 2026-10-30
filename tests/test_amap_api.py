from datetime import datetime, timezone

from fastapi.testclient import TestClient

from careguide.amap_models import (
    Coordinate,
    HospitalPoi,
    MapRoute,
    ParkingCandidate,
)
from careguide.api import app, get_amap_client


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
    async def search_hospitals(self, query, *, city):
        assert query == '泰州市人民医院'
        assert city == '泰州'
        return [HOSPITAL]

    async def get_hospital(self, hospital_id):
        assert hospital_id == 'H1'
        return HOSPITAL

    async def search_parking(self, hospital, *, radius_m):
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
