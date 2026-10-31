import asyncio

import httpx
import pytest

from careguide.amap_client import AmapClient, AmapProviderError
from careguide.amap_models import Coordinate, RouteRequest


def test_provider_error_never_exposes_server_key():
    secret = 'private-amap-key'

    def handle(request: httpx.Request):
        assert request.url.params['key'] == secret
        return httpx.Response(401, json={
            'status': '0',
            'info': f'INVALID_USER_KEY {secret}',
            'infocode': '10001',
        })

    async def exercise():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
            client = AmapClient(secret, http_client=http)
            with pytest.raises(AmapProviderError) as caught:
                await client.search_hospitals('泰州市人民医院', city='泰州')
        return caught

    caught = asyncio.run(exercise())

    assert caught.value.code == 'provider_auth_failed'
    assert secret not in str(caught.value)


def test_search_parking_preserves_unknown_live_and_accessibility_fields():
    requests = []

    def handle(request: httpx.Request):
        requests.append(request)
        if request.url.path == '/v5/place/text':
            return httpx.Response(200, json={'status': '1', 'pois': [{
                'id': 'H1',
                'name': '泰州市人民医院（总院）',
                'address': '太湖路366号',
                'cityname': '泰州市',
                'adname': '海陵区',
                'location': '119.923000,32.455000',
                'navi': {'entr_location': '119.923100,32.455100'},
            }]})
        if request.url.path == '/v5/place/around':
            return httpx.Response(200, json={'status': '1', 'pois': [{
                'id': 'P1',
                'name': '泰州市人民医院停车场',
                'address': '太湖路366号',
                'location': '119.922800,32.455200',
                'distance': '85',
                'business': {'parking_type': '地面'},
                'navi': {'entr_location': '119.922700,32.455300'},
            }]})
        raise AssertionError(f'unexpected request: {request.url}')

    async def exercise():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
            client = AmapClient('test-key', http_client=http)
            hospitals = await client.search_hospitals('泰州市人民医院', city='泰州')
            parking = await client.search_parking(hospitals[0], radius_m=1000)
        return hospitals, parking

    hospitals, parking = asyncio.run(exercise())

    assert hospitals[0].hospital_id == 'H1'
    assert hospitals[0].city_name == '泰州市'
    assert hospitals[0].district_name == '海陵区'
    assert hospitals[0].entrance_location.longitude == 119.9231
    assert parking[0].available_spaces is None
    assert parking[0].wheelchair_accessible is None
    assert parking[0].data_tier == 'map_poi'
    assert parking[0].source_provider == 'amap'
    assert parking[0].distance_to_hospital_m == 85
    assert requests[1].url.params['keywords'] == '停车场'
    assert requests[1].url.params['radius'] == '1000'


def test_invalid_coordinate_is_rejected_instead_of_invented():
    def handle(_request: httpx.Request):
        return httpx.Response(200, json={'status': '1', 'pois': [{
            'id': 'H1', 'name': '坏数据医院', 'address': '', 'location': 'not-a-coordinate'
        }]})

    async def exercise():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
            client = AmapClient('test-key', http_client=http)
            return await client.search_hospitals('坏数据医院', city='泰州')

    hospitals = asyncio.run(exercise())
    assert hospitals == []


def test_hospital_search_params_support_scoped_and_nationwide_queries():
    requests = []

    def handle(request: httpx.Request):
        requests.append(request)
        return httpx.Response(200, json={'status': '1', 'pois': []})

    async def exercise():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
            client = AmapClient('test-key', http_client=http)
            await client.search_hospitals('泰州市人民医院', city='泰州')
            await client.search_hospitals('武汉协和医院', city=None)

    asyncio.run(exercise())

    scoped = requests[0].url.params
    assert scoped['region'] == '泰州'
    assert scoped['city_limit'] == 'true'
    nationwide = requests[1].url.params
    assert 'region' not in nationwide
    assert nationwide['city_limit'] == 'false'


def test_walking_route_requests_polyline_and_never_claims_wheelchair_verification():
    requests = []

    def handle(request: httpx.Request):
        requests.append(request)
        return httpx.Response(200, json={'status': '1', 'route': {'paths': [{
            'distance': '120', 'cost': {'duration': '160'}, 'steps': [{
                'instruction': '向东步行', 'step_distance': '120',
                'polyline': '119.923000,32.455000;119.922800,32.455200',
            }],
        }]}})

    async def exercise():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
            client = AmapClient('test-key', http_client=http)
            return await client.route(RouteRequest(
                origin=Coordinate(longitude=119.923, latitude=32.455),
                destination=Coordinate(longitude=119.9228, latitude=32.4552),
                mode='walking',
            ))

    route = asyncio.run(exercise())
    assert requests[0].url.params['show_fields'] == 'cost,polyline'
    assert route.distance_m == 120
    assert route.steps[0].distance_m == 120
    assert len(route.polyline) == 2
    assert route.wheelchair_verified is False
