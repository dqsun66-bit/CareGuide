"""Strict, immutable data contracts for verified hospital information."""
from __future__ import annotations

from datetime import date, datetime
from enum import Enum
import ipaddress
from pathlib import PurePosixPath
import re
from types import MappingProxyType
from typing import Literal, Mapping
from urllib.parse import urlparse

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_serializer,
    field_validator,
    model_validator,
)


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class HospitalType(str, Enum):
    GENERAL = "general"
    TCM_GENERAL = "tcm_general"
    CHILDREN = "children"


class SourceTier(str, Enum):
    HOSPITAL_OFFICIAL = "hospital_official"
    HEALTH_AUTHORITY = "health_authority"
    GOVERNMENT = "government"
    OFFICIAL_APPOINTMENT = "official_appointment"
    MAP_PROVIDER = "map_provider"


class ReviewStatus(str, Enum):
    VERIFIED_OFFICIAL = "verified_official"
    NEEDS_REVIEW = "needs_review"
    EXPIRED = "expired"
    CONFLICT = "conflict"


class Category(str, Enum):
    IDENTITY = "identity"
    CAMPUS = "campus"
    APPOINTMENT = "appointment"
    DEPARTMENT = "department"
    VISIT_PREPARATION = "visit_preparation"
    REPORT = "report"
    TRANSPORT = "transport"
    PARKING = "parking"
    ACCESSIBILITY = "accessibility"
    EMERGENCY = "emergency"
    CONTACT = "contact"


def _require_aware(value: datetime | None, field_name: str) -> None:
    if value is not None and (value.tzinfo is None or value.utcoffset() is None):
        raise ValueError(f"{field_name} must be timezone-aware")


def _validate_public_dns(host: str) -> str:
    if host != host.strip() or host != host.casefold() or len(host) > 253:
        raise ValueError("Official domain must be a public DNS hostname")
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        raise ValueError("Official domain must be a public DNS hostname")
    if host == "localhost" or host.endswith(".localhost") or host.endswith(".local"):
        raise ValueError("Official domain must be a public DNS hostname")
    labels = host.split(".")
    if len(labels) < 2 or any(
        not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label)
        for label in labels
    ):
        raise ValueError("Official domain must be a public DNS hostname")
    return host


def _validate_public_https(value: str) -> str:
    parsed = urlparse(value)
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError("Source URL must be public HTTPS") from exc
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or port not in (None, 443)
    ):
        raise ValueError("Source URL must be public HTTPS")
    try:
        _validate_public_dns(parsed.hostname)
    except ValueError as exc:
        raise ValueError("Source URL must be public HTTPS") from exc
    return value


class Campus(StrictModel):
    campus_id: str = Field(pattern=r"^js_[a-z]+_[0-9]{3}_[a-z0-9_]+$")
    official_name: str = Field(min_length=1)
    address: str = Field(min_length=5)
    address_source_id: str = Field(min_length=1)


class Hospital(StrictModel):
    hospital_id: str = Field(pattern=r"^js_[a-z]+_[0-9]{3}$")
    official_name: str = Field(min_length=2)
    aliases: tuple[str, ...] = ()
    city: str = Field(min_length=2)
    hospital_type: HospitalType
    grade: Literal["三级甲等"]
    grade_source_id: str = Field(min_length=1)
    official_domains: tuple[str, ...] = ()
    campuses: tuple[Campus, ...] = Field(min_length=1)

    @field_validator("official_domains")
    @classmethod
    def validate_domains(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        for value in values:
            _validate_public_dns(value)
        if len(set(values)) != len(values):
            raise ValueError("Duplicate official domains")
        return values

    @model_validator(mode="after")
    def validate_identity(self):
        folded_aliases = [alias.strip().casefold() for alias in self.aliases]
        if len(set(folded_aliases)) != len(folded_aliases):
            raise ValueError("Duplicate hospital aliases")
        campus_ids = [campus.campus_id for campus in self.campuses]
        if len(set(campus_ids)) != len(campus_ids):
            raise ValueError("Duplicate campus identities")
        if any(not identity.startswith(f"{self.hospital_id}_") for identity in campus_ids):
            raise ValueError("Campus does not belong to hospital")
        return self


class Source(StrictModel):
    source_id: str = Field(min_length=1)
    hospital_id: str = Field(pattern=r"^js_[a-z]+_[0-9]{3}$")
    campus_id: str | None = None
    title: str = Field(min_length=2)
    url: str
    source_tier: SourceTier
    category: Category
    published_at: date | None
    valid_until: date
    collected_at: datetime
    reviewed_at: datetime | None
    review_status: ReviewStatus
    snapshot_path: str = Field(min_length=1)
    http_status: int = Field(ge=100, le=599)
    content_type: str = Field(min_length=1)
    retrieved_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")

    @field_validator("url")
    @classmethod
    def validate_url(cls, value: str) -> str:
        return _validate_public_https(value)

    @field_validator("snapshot_path")
    @classmethod
    def validate_snapshot_path(cls, value: str) -> str:
        if "\\" in value:
            raise ValueError("Snapshot path must be a portable relative path")
        path = PurePosixPath(value)
        if path.is_absolute() or ".." in path.parts or not path.parts or path.parts[0] != "snapshots":
            raise ValueError("Snapshot path must be a portable relative path under snapshots")
        return value

    @model_validator(mode="after")
    def validate_dates_and_scope(self):
        _require_aware(self.collected_at, "collected_at")
        _require_aware(self.reviewed_at, "reviewed_at")
        if self.published_at and self.published_at > self.collected_at.date():
            raise ValueError("Source publication is later than collection")
        if self.campus_id and not self.campus_id.startswith(f"{self.hospital_id}_"):
            raise ValueError("Campus does not belong to hospital")
        if self.review_status == ReviewStatus.VERIFIED_OFFICIAL and self.reviewed_at is None:
            raise ValueError("verified_official source requires reviewed_at")
        if self.reviewed_at is not None and self.reviewed_at < self.collected_at:
            raise ValueError("reviewed_at cannot predate collection")
        return self


class KnowledgeRecord(StrictModel):
    document_id: str = Field(min_length=1)
    hospital_id: str = Field(pattern=r"^js_[a-z]+_[0-9]{3}$")
    campus_id: str
    city: str = Field(min_length=2)
    category: Category
    title: str = Field(min_length=2)
    content: str = Field(min_length=50, max_length=1000)
    source_id: str = Field(min_length=1)
    supporting_source_ids: tuple[str, ...] = ()
    source_url: str
    source_tier: SourceTier
    published_at: date | None
    collected_at: datetime
    reviewed_at: datetime | None
    review_status: ReviewStatus
    content_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    language: Literal["zh-CN"] = "zh-CN"

    @field_validator("source_url")
    @classmethod
    def validate_source_url(cls, value: str) -> str:
        return _validate_public_https(value)

    @model_validator(mode="after")
    def validate_publication(self):
        _require_aware(self.collected_at, "collected_at")
        _require_aware(self.reviewed_at, "reviewed_at")
        if not self.campus_id.startswith(f"{self.hospital_id}_"):
            raise ValueError("Campus does not belong to hospital")
        if self.published_at and self.published_at > self.collected_at.date():
            raise ValueError("Record publication is later than collection")
        if self.review_status != ReviewStatus.VERIFIED_OFFICIAL or self.reviewed_at is None:
            raise ValueError("Published records require verified_official review")
        if self.reviewed_at < self.collected_at:
            raise ValueError("reviewed_at cannot predate collection")
        if self.source_id in self.supporting_source_ids:
            raise ValueError("primary source cannot be repeated as a supporting source")
        if len(set(self.supporting_source_ids)) != len(self.supporting_source_ids):
            raise ValueError("duplicate supporting source")
        return self


class VerificationEntry(StrictModel):
    verification_id: str = Field(min_length=1)
    subject_ids: tuple[str, ...] = Field(min_length=1)
    status: ReviewStatus
    reason: str = Field(min_length=2)
    decision: str | None
    reviewed_at: datetime

    @model_validator(mode="after")
    def validate_decision(self):
        _require_aware(self.reviewed_at, "reviewed_at")
        if self.status in {ReviewStatus.CONFLICT, ReviewStatus.EXPIRED} and not self.decision:
            raise ValueError("Resolved verification status requires a decision")
        return self


class CorpusManifest(StrictModel):
    version: Literal["jiangsu-hospitals-v1", "jiangsu-hospitals-v2"]
    stage: Literal["pilot_taizhou_3_of_15", "expanded_core_15_of_15"]
    built_at: datetime
    hospital_count: int = Field(ge=0)
    source_count: int = Field(ge=0)
    record_count: int = Field(ge=0)
    verification_count: int = Field(ge=0)
    files: Mapping[str, str]

    @model_validator(mode="after")
    def validate_manifest(self):
        _require_aware(self.built_at, "built_at")
        expected = {"hospitals.json", "sources.json", "records.json", "verification.json"}
        if set(self.files) != expected:
            raise ValueError("Manifest files must match the corpus contract")
        if any(len(value) != 64 or any(char not in "0123456789abcdef" for char in value)
               for value in self.files.values()):
            raise ValueError("Manifest file hashes must be lowercase SHA-256")
        object.__setattr__(self, "files", MappingProxyType(dict(self.files)))
        return self

    @field_serializer("files")
    def serialize_files(self, value: Mapping[str, str]) -> dict[str, str]:
        return dict(value)
