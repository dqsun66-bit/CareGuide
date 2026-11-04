"""Load, validate and atomically publish versioned hospital corpus assets."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
import hashlib
import json
from pathlib import Path
import shutil
import tempfile
from typing import Iterable, Mapping, Sequence
from urllib.parse import urlparse

from .dedup import duplicate_groups
from .models import (
    CorpusManifest,
    Hospital,
    KnowledgeRecord,
    ReviewStatus,
    Source,
    VerificationEntry,
)
from .normalize import canonical_url


DATA_FILES = ("hospitals.json", "sources.json", "records.json", "verification.json")
GOVERNMENT_SUFFIXES = (".gov.cn",)


@dataclass(frozen=True)
class ValidationReport:
    errors: tuple[str, ...]
    warnings: tuple[str, ...]
    expired_source_ids: tuple[str, ...]
    conflict_ids: tuple[str, ...]
    duplicate_groups: tuple[tuple[str, ...], ...]

    @property
    def valid(self) -> bool:
        return not self.errors


def _read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def _hash_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _unique_index(items: Iterable, identity: str, label: str) -> dict[str, object]:
    result: dict[str, object] = {}
    for item in items:
        value = getattr(item, identity)
        if value in result:
            raise ValueError(f"Duplicate {label} identities: {value}")
        result[value] = item
    return result


def _source_host_allowed(source: Source, hospital: Hospital) -> bool:
    host = urlparse(source.url).hostname or ""
    if host in hospital.official_domains:
        return True
    return any(host.endswith(suffix) and host != suffix.removeprefix(".") for suffix in GOVERNMENT_SUFFIXES)


class HospitalCorpus:
    def __init__(
        self,
        root: Path,
        manifest: CorpusManifest,
        hospitals: tuple[Hospital, ...],
        sources: tuple[Source, ...],
        records: tuple[KnowledgeRecord, ...],
        verification: tuple[VerificationEntry, ...],
    ):
        self.root = root
        self.manifest = manifest
        self.hospitals = hospitals
        self.sources = sources
        self.records = records
        self.verification = verification
        self._hospitals = _unique_index(hospitals, "hospital_id", "hospital")
        self._sources = _unique_index(sources, "source_id", "source")
        self._records = _unique_index(records, "document_id", "record")
        self._verification = _unique_index(verification, "verification_id", "verification")

    @classmethod
    def load(cls, root: Path | str) -> "HospitalCorpus":
        root = Path(root)
        manifest = CorpusManifest.model_validate(_read_json(root / "manifest.json"))
        for filename, expected in manifest.files.items():
            actual = _hash_file(root / filename)
            if actual != expected:
                raise ValueError(f"Corpus integrity mismatch: {filename}")
        hospitals = tuple(Hospital.model_validate(value) for value in _read_json(root / "hospitals.json"))
        sources = tuple(Source.model_validate(value) for value in _read_json(root / "sources.json"))
        records = tuple(KnowledgeRecord.model_validate(value) for value in _read_json(root / "records.json"))
        verification = tuple(
            VerificationEntry.model_validate(value) for value in _read_json(root / "verification.json")
        )
        corpus = cls(root, manifest, hospitals, sources, records, verification)
        actual_counts = (
            len(hospitals), len(sources), len(records), len(verification)
        )
        declared_counts = (
            manifest.hospital_count, manifest.source_count,
            manifest.record_count, manifest.verification_count,
        )
        if actual_counts != declared_counts:
            raise ValueError("Manifest counts disagree with corpus files")
        return corpus

    def record(self, document_id: str) -> KnowledgeRecord:
        return self._records[document_id]

    def validate(self, *, today: date) -> ValidationReport:
        errors: list[str] = []
        warnings: list[str] = []
        expired: list[str] = []
        conflicts = sorted(
            entry.verification_id for entry in self.verification
            if entry.status == ReviewStatus.CONFLICT
        )
        campuses = {
            campus.campus_id: (hospital.hospital_id, campus)
            for hospital in self.hospitals for campus in hospital.campuses
        }
        for hospital in self.hospitals:
            if hospital.grade_source_id not in self._sources:
                errors.append(f"hospital {hospital.hospital_id}: unknown grade source")
            for campus in hospital.campuses:
                if campus.address_source_id not in self._sources:
                    errors.append(f"campus {campus.campus_id}: unknown address source")
        for source in self.sources:
            hospital = self._hospitals.get(source.hospital_id)
            if hospital is None:
                errors.append(f"source {source.source_id}: unknown hospital")
                continue
            if source.campus_id and source.campus_id not in campuses:
                errors.append(f"source {source.source_id}: unknown campus")
            if not _source_host_allowed(source, hospital):
                errors.append(f"source {source.source_id}: unapproved source host")
            if source.valid_until < today:
                expired.append(source.source_id)
                errors.append(f"source {source.source_id}: expired source")
            snapshot = self.root / Path(source.snapshot_path)
            if not snapshot.is_file():
                errors.append(f"source {source.source_id}: missing snapshot")
            elif _hash_file(snapshot) != source.retrieved_sha256:
                errors.append(f"source {source.source_id}: snapshot hash mismatch")
        for record in self.records:
            source = self._sources.get(record.source_id)
            if source is None:
                errors.append(f"record {record.document_id}: unknown source")
                continue
            scope = campuses.get(record.campus_id)
            if scope is None:
                errors.append(f"record {record.document_id}: unknown campus")
            elif scope[0] != record.hospital_id:
                errors.append(f"record {record.document_id}: campus belongs to another hospital")
            if source.hospital_id != record.hospital_id:
                errors.append(f"record {record.document_id}: source hospital mismatch")
            if source.campus_id and source.campus_id != record.campus_id:
                errors.append(f"record {record.document_id}: source campus mismatch")
            if canonical_url(source.url) != canonical_url(record.source_url):
                errors.append(f"record {record.document_id}: source URL mismatch")
            if source.source_tier != record.source_tier:
                errors.append(f"record {record.document_id}: source tier mismatch")
            if source.review_status != ReviewStatus.VERIFIED_OFFICIAL:
                errors.append(f"record {record.document_id}: source is not verified")
        duplicates = duplicate_groups(
            ((record.document_id, record.content) for record in self.records), threshold=0.95
        )
        if duplicates:
            errors.append("record contents contain duplicate groups")
        return ValidationReport(
            errors=tuple(sorted(set(errors))),
            warnings=tuple(sorted(set(warnings))),
            expired_source_ids=tuple(sorted(expired)),
            conflict_ids=tuple(conflicts),
            duplicate_groups=duplicates,
        )


def _json_value(value) -> object:
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    return value


def _write_json(path: Path, value: Sequence | Mapping) -> None:
    payload = [_json_value(item) for item in value] if not isinstance(value, Mapping) else dict(value)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")


def write_corpus(
    target: Path | str,
    *,
    hospitals: Sequence[Hospital | Mapping],
    sources: Sequence[Source | Mapping],
    records: Sequence[KnowledgeRecord | Mapping],
    verification: Sequence[VerificationEntry | Mapping],
    built_at: datetime,
    snapshot_root: Path | None = None,
) -> HospitalCorpus:
    """Publish only a fully valid corpus; never leave a partial target."""
    target = Path(target)
    if target.exists():
        raise FileExistsError(f"Refusing to overwrite corpus: {target}")
    parent = target.parent
    parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix=f".{target.name}-", dir=parent))
    try:
        if snapshot_root and snapshot_root.exists():
            shutil.copytree(snapshot_root, temporary / "snapshots")
        values = {
            "hospitals.json": [Hospital.model_validate(item) for item in hospitals],
            "sources.json": [Source.model_validate(item) for item in sources],
            "records.json": [KnowledgeRecord.model_validate(item) for item in records],
            "verification.json": [VerificationEntry.model_validate(item) for item in verification],
        }
        for filename, items in values.items():
            _write_json(temporary / filename, items)
        files = {filename: _hash_file(temporary / filename) for filename in DATA_FILES}
        manifest = CorpusManifest(
            version="jiangsu-hospitals-v1",
            stage="pilot_taizhou_3_of_15",
            built_at=built_at,
            hospital_count=len(values["hospitals.json"]),
            source_count=len(values["sources.json"]),
            record_count=len(values["records.json"]),
            verification_count=len(values["verification.json"]),
            files=files,
        )
        _write_json(temporary / "manifest.json", manifest.model_dump(mode="json"))
        corpus = HospitalCorpus.load(temporary)
        report = corpus.validate(today=built_at.date())
        if report.errors:
            raise ValueError("; ".join(report.errors))
        temporary.rename(target)
        return HospitalCorpus.load(target)
    except Exception:
        shutil.rmtree(temporary, ignore_errors=True)
        raise
