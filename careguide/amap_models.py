"""Strict, source-labelled contracts for Amap location data."""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class Coordinate(BaseModel):
    model_config = ConfigDict(extra='forbid')
    longitude: float = Field(ge=73, le=136)
    latitude: float = Field(ge=3, le=54)


class HospitalPoi(BaseModel):
    model_config = ConfigDict(extra='forbid')
    hospital_id: str
    name: str
    address: str
    city_name: str | None = None
    district_name: str | None = None
    location: Coordinate
    entrance_location: Coordinate | None = None
    data_tier: Literal['map_poi'] = 'map_poi'
    source_provider: Literal['amap'] = 'amap'
    observed_at: datetime


class ParkingCandidate(BaseModel):
    model_config = ConfigDict(extra='forbid')
    parking_id: str
    name: str
    address: str
    location: Coordinate
    entrance_location: Coordinate | None = None
    parking_type: str | None = None
    distance_to_hospital_m: int | None = None
    available_spaces: int | None = None
    wheelchair_accessible: bool | None = None
    data_tier: Literal['map_poi'] = 'map_poi'
    source_provider: Literal['amap'] = 'amap'
    observed_at: datetime


class RouteRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    origin: Coordinate
    destination: Coordinate
    mode: Literal['walking', 'driving']


class RouteStep(BaseModel):
    model_config = ConfigDict(extra='forbid')
    instruction: str = ''
    distance_m: int | None = None
    duration_seconds: int | None = None
    polyline: list[Coordinate] = Field(default_factory=list)


class MapRoute(BaseModel):
    model_config = ConfigDict(extra='forbid')
    mode: Literal['walking', 'driving']
    distance_m: int
    duration_seconds: int | None = None
    steps: list[RouteStep] = Field(default_factory=list)
    polyline: list[Coordinate] = Field(default_factory=list)
    data_tier: Literal['map_poi'] = 'map_poi'
    source_provider: Literal['amap'] = 'amap'
    wheelchair_verified: Literal[False] = False
    observed_at: datetime
