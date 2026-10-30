"""A small server-only Amap client with truthful data normalization."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import httpx
from pydantic import ValidationError

from careguide.amap_models import (
    Coordinate,
    HospitalPoi,
    MapRoute,
    ParkingCandidate,
    RouteRequest,
    RouteStep,
)


_AUTH_INFOCODES = {'10001', '10002', '10007', '10008', '10009', '10010', '10012', '10013'}


class AmapProviderError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def _text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return ''.join(str(item) for item in value)
    return ''


def _coordinate(value: Any) -> Coordinate | None:
    if not isinstance(value, str):
        return None
    try:
        longitude, latitude = (float(item) for item in value.split(',', 1))
        return Coordinate(longitude=longitude, latitude=latitude)
    except (TypeError, ValueError, ValidationError):
        return None


def _optional_int(value: Any) -> int | None:
    try:
        return int(value) if value not in (None, '') else None
    except (TypeError, ValueError):
        return None


def _polyline(value: Any) -> list[Coordinate]:
    if not isinstance(value, str):
        return []
    return [point for token in value.split(';') if (point := _coordinate(token))]


def _hospital(item: Any, observed_at: datetime) -> HospitalPoi | None:
    if not isinstance(item, dict):
        return None
    location = _coordinate(item.get('location'))
    if not location or not item.get('id') or not item.get('name'):
        return None
    navi = item.get('navi') if isinstance(item.get('navi'), dict) else {}
    return HospitalPoi(
        hospital_id=str(item['id']),
        name=str(item['name']),
        address=_text(item.get('address')),
        location=location,
        entrance_location=_coordinate(navi.get('entr_location')),
        observed_at=observed_at,
    )


class AmapClient:
    def __init__(self, api_key: str, *, base_url: str = 'https://restapi.amap.com',
                 timeout_s: float = 8, http_client: httpx.AsyncClient | None = None):
        if not api_key or api_key.startswith('YOUR_'):
            raise ValueError('Amap Web Service API key is not configured')
        self._api_key = api_key
        self._base_url = base_url.rstrip('/')
        self._owns_client = http_client is None
        self._http = http_client or httpx.AsyncClient(
            base_url=self._base_url, timeout=timeout_s, trust_env=False)

    async def _request(self, path: str, params: dict[str, Any]) -> dict[str, Any]:
        try:
            response = await self._http.get(
                f'{self._base_url}{path}',
                params={'key': self._api_key, **params},
            )
            payload = response.json()
        except httpx.TimeoutException as exc:
            raise AmapProviderError('provider_timeout', '地图服务响应超时') from exc
        except (httpx.HTTPError, ValueError) as exc:
            raise AmapProviderError('provider_unavailable', '地图服务暂不可用') from exc
        infocode = str(payload.get('infocode', ''))
        if response.status_code in {401, 403} or infocode in _AUTH_INFOCODES:
            raise AmapProviderError('provider_auth_failed', '地图服务鉴权失败')
        if response.status_code >= 400 or payload.get('status') != '1':
            code = 'provider_rate_limited' if infocode in {'10003', '10004', '10019', '10020'} else 'provider_unavailable'
            raise AmapProviderError(code, '地图服务请求受限' if code == 'provider_rate_limited' else '地图服务暂不可用')
        return payload

    async def search_hospitals(self, query: str, *, city: str) -> list[HospitalPoi]:
        payload = await self._request('/v5/place/text', {
            'keywords': query,
            'region': city,
            'city_limit': 'true',
            'show_fields': 'navi,business',
        })
        observed_at = datetime.now(timezone.utc)
        return [hospital for item in payload.get('pois', [])
                if (hospital := _hospital(item, observed_at))]

    async def get_hospital(self, hospital_id: str) -> HospitalPoi | None:
        payload = await self._request('/v5/place/detail', {
            'id': hospital_id,
            'show_fields': 'navi,business',
        })
        observed_at = datetime.now(timezone.utc)
        return next((hospital for item in payload.get('pois', [])
                     if (hospital := _hospital(item, observed_at))), None)

    async def search_parking(self, hospital: HospitalPoi, *, radius_m: int) -> list[ParkingCandidate]:
        if not 1 <= radius_m <= 50000:
            raise ValueError('parking search radius must be between 1 and 50000 metres')
        origin = hospital.entrance_location or hospital.location
        payload = await self._request('/v5/place/around', {
            'keywords': '停车场',
            'location': f'{origin.longitude:.6f},{origin.latitude:.6f}',
            'radius': str(radius_m),
            'sortrule': 'distance',
            'show_fields': 'navi,business',
        })
        observed_at = datetime.now(timezone.utc)
        results = []
        for item in payload.get('pois', []):
            location = _coordinate(item.get('location'))
            if not location or not item.get('id') or not item.get('name'):
                continue
            navi = item.get('navi') if isinstance(item.get('navi'), dict) else {}
            business = item.get('business') if isinstance(item.get('business'), dict) else {}
            results.append(ParkingCandidate(
                parking_id=str(item['id']),
                name=str(item['name']),
                address=_text(item.get('address')),
                location=location,
                entrance_location=_coordinate(navi.get('entr_location')),
                parking_type=_text(business.get('parking_type')) or None,
                distance_to_hospital_m=_optional_int(item.get('distance')),
                available_spaces=None,
                wheelchair_accessible=None,
                observed_at=observed_at,
            ))
        return results

    async def route(self, request: RouteRequest) -> MapRoute | None:
        origin = f'{request.origin.longitude:.6f},{request.origin.latitude:.6f}'
        destination = f'{request.destination.longitude:.6f},{request.destination.latitude:.6f}'
        path = '/v5/direction/walking' if request.mode == 'walking' else '/v5/direction/driving'
        params = {
            'origin': origin,
            'destination': destination,
            'show_fields': 'cost,polyline',
        }
        payload = await self._request(path, params)
        route = payload.get('route') if isinstance(payload.get('route'), dict) else {}
        paths = route.get('paths') if isinstance(route.get('paths'), list) else []
        if not paths or not isinstance(paths[0], dict):
            return None
        provider_path = paths[0]
        step_items = provider_path.get('steps') if isinstance(provider_path.get('steps'), list) else []
        steps = []
        points = []
        for item in step_items:
            if not isinstance(item, dict):
                continue
            step_points = _polyline(item.get('polyline'))
            points.extend(step_points)
            cost = item.get('cost') if isinstance(item.get('cost'), dict) else {}
            steps.append(RouteStep(
                instruction=_text(item.get('instruction')),
                distance_m=_optional_int(item.get('distance') or item.get('step_distance')),
                duration_seconds=_optional_int(cost.get('duration') or item.get('duration')),
                polyline=step_points,
            ))
        path_cost = provider_path.get('cost') if isinstance(provider_path.get('cost'), dict) else {}
        distance = _optional_int(provider_path.get('distance'))
        if distance is None:
            return None
        return MapRoute(
            mode=request.mode,
            distance_m=distance,
            duration_seconds=_optional_int(path_cost.get('duration') or provider_path.get('duration')),
            steps=steps,
            polyline=points,
            observed_at=datetime.now(timezone.utc),
        )

    async def aclose(self):
        if self._owns_client:
            await self._http.aclose()
