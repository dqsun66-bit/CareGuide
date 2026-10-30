"""Read-only real-provider gate for CareGuide's Amap parking integration."""

from __future__ import annotations

import argparse
import asyncio
from pathlib import Path
import sys

from dotenv import dotenv_values

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from careguide.amap_client import AmapClient, AmapProviderError
from careguide.amap_models import HospitalPoi, RouteRequest


def select_taizhou_hospital(hospitals: list[HospitalPoi]) -> HospitalPoi:
    for hospital in hospitals:
        city = hospital.city_name or ''
        if '泰州' in hospital.name and '人民医院' in hospital.name and '泰州' in city:
            return hospital
    raise LookupError('高德未返回名称和城市同时匹配的泰州人民医院')


async def validate(config_file: Path) -> int:
    config = dotenv_values(config_file)
    key = str(config.get('CAREGUIDE_AMAP_WEB_SERVICE_KEY') or '').strip()
    if not key or key.startswith('YOUR_'):
        print('STATUS=FAILED CODE=provider_unconfigured')
        return 2
    try:
        timeout = float(config.get('CAREGUIDE_AMAP_TIMEOUT_SECONDS') or 8)
    except (TypeError, ValueError):
        print('STATUS=FAILED CODE=invalid_timeout')
        return 2

    client = AmapClient(
        key,
        base_url=str(config.get('CAREGUIDE_AMAP_BASE_URL') or 'https://restapi.amap.com'),
        timeout_s=timeout,
    )
    try:
        hospitals = await client.search_hospitals('泰州市人民医院', city='泰州')
        hospital = select_taizhou_hospital(hospitals)
        print(f'HOSPITAL={hospital.hospital_id}|{hospital.name}|CITY={hospital.city_name}|TIER={hospital.data_tier}')
        parking = await client.search_parking(hospital, radius_m=1000)
        if not parking:
            print('STATUS=FAILED CODE=no_parking_candidates')
            return 3
        for item in parking[:10]:
            print(f'PARKING={item.parking_id}|{item.name}|DISTANCE_M={item.distance_to_hospital_m}|TIER={item.data_tier}')
        route = await client.route(RouteRequest(
            origin=hospital.entrance_location or hospital.location,
            destination=parking[0].entrance_location or parking[0].location,
            mode='walking',
        ))
        if route:
            print(f'ROUTE=walking|DISTANCE_M={route.distance_m}|POINTS={len(route.polyline)}|WHEELCHAIR_VERIFIED={route.wheelchair_verified}')
        else:
            print('ROUTE=unavailable')
        print(f'STATUS=OK HOSPITALS={len(hospitals)} PARKING={len(parking)}')
        return 0
    except AmapProviderError as exc:
        print(f'STATUS=FAILED CODE={exc.code}')
        return 4
    except LookupError as exc:
        print(f'STATUS=FAILED CODE=hospital_mismatch MESSAGE={exc}')
        return 5
    finally:
        await client.aclose()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('--config-file', type=Path, required=True)
    args = parser.parse_args()
    return asyncio.run(validate(args.config_file))


if __name__ == '__main__':
    raise SystemExit(main())
