"""Run a redacted, read-only Amap smoke test using the local .env file."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
import sys

from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from careguide.amap_client import AmapClient
from careguide.amap_models import RouteRequest


async def main() -> None:
    load_dotenv()
    client = AmapClient(os.environ.get('CAREGUIDE_AMAP_WEB_SERVICE_KEY', ''))
    try:
        hospitals = await client.search_hospitals('泰州市人民医院', city='泰州')
        print(f'HOSPITAL_COUNT={len(hospitals)}')
        for hospital in hospitals[:5]:
            print(f'HOSPITAL={hospital.hospital_id}|{hospital.name}|{hospital.address}')
        if not hospitals:
            return
        parking = await client.search_parking(hospitals[0], radius_m=1000)
        print(f'PARKING_COUNT={len(parking)}')
        for item in parking[:5]:
            print(f'PARKING={item.parking_id}|{item.name}|DISTANCE_M={item.distance_to_hospital_m}')
        if parking:
            route = await client.route(RouteRequest(
                origin=hospitals[0].entrance_location or hospitals[0].location,
                destination=parking[0].entrance_location or parking[0].location,
                mode='walking',
            ))
            print('ROUTE=' + ('FOUND' if route else 'NOT_FOUND'))
            if route:
                print(f'ROUTE_DISTANCE_M={route.distance_m}|POINTS={len(route.polyline)}')
    finally:
        await client.aclose()


if __name__ == '__main__':
    asyncio.run(main())
