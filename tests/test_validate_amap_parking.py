from datetime import datetime, timezone

from careguide.amap_models import Coordinate, HospitalPoi
from scripts.validate_amap_parking import select_taizhou_hospital


def hospital(identity: str, name: str, city: str | None) -> HospitalPoi:
    return HospitalPoi(
        hospital_id=identity,
        name=name,
        address='test',
        city_name=city,
        location=Coordinate(longitude=119.9, latitude=32.4),
        observed_at=datetime.now(timezone.utc),
    )


def test_validator_requires_name_and_city_match():
    result = select_taizhou_hospital([
        hospital('wrong-city', '泰州市人民医院', '南京市'),
        hospital('wrong-name', '泰州市中医院', '泰州市'),
        hospital('match', '南京医科大学附属泰州人民医院', '泰州市'),
    ])
    assert result.hospital_id == 'match'
