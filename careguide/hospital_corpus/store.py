"""Load, validate and atomically publish versioned hospital corpus assets."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
import hashlib
import json
import os
from pathlib import Path
from pathlib import PurePosixPath
import shutil
import stat
import tempfile
from typing import Iterable, Mapping, Sequence
from urllib.parse import urlparse

from .dedup import duplicate_groups
from .models import (
    CorpusManifest,
    Category,
    Hospital,
    KnowledgeRecord,
    ReviewStatus,
    Source,
    SourceTier,
    VerificationEntry,
)
from .normalize import canonical_url
from .normalize import normalize_text


DATA_FILES = ("hospitals.json", "sources.json", "records.json", "verification.json")
GOVERNMENT_SUFFIXES = (".gov.cn",)
OFFICIAL_APPOINTMENT_HOSTS = {"js.guahaoe.com"}


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


def _read_authenticated_list(path: Path, expected_hash: str) -> list:
    raw = path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != expected_hash:
        raise ValueError(f"Corpus integrity mismatch: {path.name}")
    value = json.loads(raw.decode("utf-8"))
    if not isinstance(value, list):
        raise ValueError(f"Corpus data file must contain a list: {path.name}")
    return value


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
    if (
        source.source_tier == SourceTier.OFFICIAL_APPOINTMENT
        and source.category != Category.APPOINTMENT
    ):
        return False
    if host in hospital.official_domains:
        return True
    if (
        source.source_tier == SourceTier.OFFICIAL_APPOINTMENT
        and source.category == Category.APPOINTMENT
    ):
        return host in OFFICIAL_APPOINTMENT_HOSTS
    return any(host.endswith(suffix) and host != suffix.removeprefix(".") for suffix in GOVERNMENT_SUFFIXES)


AUTHORITATIVE_TIERS = {
    SourceTier.HOSPITAL_OFFICIAL,
    SourceTier.HEALTH_AUTHORITY,
    SourceTier.GOVERNMENT,
}


def _evidence_errors(
    *,
    label: str,
    source: Source,
    hospital_id: str,
    campus_id: str | None,
    categories: set[Category],
) -> list[str]:
    errors: list[str] = []
    if source.hospital_id != hospital_id:
        errors.append(f"{label}: evidence belongs to another hospital")
    if campus_id is not None and source.campus_id not in {None, campus_id}:
        errors.append(f"{label}: evidence belongs to another campus")
    if source.review_status != ReviewStatus.VERIFIED_OFFICIAL:
        errors.append(f"{label}: evidence is not verified")
    if source.source_tier == SourceTier.MAP_PROVIDER:
        errors.append(f"{label}: map source cannot authorize official facts")
    elif source.source_tier not in AUTHORITATIVE_TIERS:
        errors.append(f"{label}: source tier is not authoritative")
    if source.category not in categories:
        errors.append(f"{label}: evidence category mismatch")
    return errors


def _safe_snapshot(root: Path, portable_path: str) -> tuple[Path | None, str | None]:
    root = root.resolve()
    snapshot_root = (root / "snapshots").resolve()
    relative = PurePosixPath(portable_path)
    candidate = root.joinpath(*relative.parts)
    if not candidate.exists():
        return candidate, None
    try:
        resolved = candidate.resolve(strict=True)
        resolved.relative_to(snapshot_root)
    except (OSError, ValueError):
        return None, "snapshot escapes corpus root"
    cursor = candidate
    while cursor != root:
        if cursor.is_symlink():
            return None, "snapshot symlinks are not allowed"
        cursor = cursor.parent
    return resolved, None


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
        payloads = {
            filename: _read_authenticated_list(root / filename, expected)
            for filename, expected in manifest.files.items()
        }
        hospitals = tuple(Hospital.model_validate(value) for value in payloads["hospitals.json"])
        sources = tuple(Source.model_validate(value) for value in payloads["sources.json"])
        records = tuple(KnowledgeRecord.model_validate(value) for value in payloads["records.json"])
        verification = tuple(
            VerificationEntry.model_validate(value) for value in payloads["verification.json"]
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
            grade_source = self._sources.get(hospital.grade_source_id)
            if grade_source is None:
                errors.append(f"hospital {hospital.hospital_id}: unknown grade source")
            else:
                errors.extend(_evidence_errors(
                    label=f"hospital {hospital.hospital_id} grade",
                    source=grade_source,
                    hospital_id=hospital.hospital_id,
                    campus_id=None,
                    categories={Category.IDENTITY},
                ))
            for campus in hospital.campuses:
                address_source = self._sources.get(campus.address_source_id)
                if address_source is None:
                    errors.append(f"campus {campus.campus_id}: unknown address source")
                else:
                    errors.extend(_evidence_errors(
                        label=f"campus {campus.campus_id} address",
                        source=address_source,
                        hospital_id=hospital.hospital_id,
                        campus_id=campus.campus_id,
                        categories={Category.IDENTITY, Category.CAMPUS, Category.CONTACT},
                    ))
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
            snapshot, snapshot_error = _safe_snapshot(self.root, source.snapshot_path)
            if snapshot_error:
                errors.append(f"source {source.source_id}: {snapshot_error}")
            elif snapshot is None or not snapshot.is_file():
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
            if source.category != record.category:
                errors.append(f"record {record.document_id}: category mismatch")
            if source.review_status != ReviewStatus.VERIFIED_OFFICIAL:
                errors.append(f"record {record.document_id}: source is not verified")
            if source.source_tier == SourceTier.MAP_PROVIDER:
                errors.append(f"record {record.document_id}: map source cannot authorize official facts")
            if (
                source.source_tier == SourceTier.OFFICIAL_APPOINTMENT
                and record.category != Category.APPOINTMENT
            ):
                errors.append(
                    f"record {record.document_id}: appointment platform cannot authorize "
                    "non-appointment facts"
                )
            for supporting_id in record.supporting_source_ids:
                supporting = self._sources.get(supporting_id)
                if supporting is None:
                    errors.append(
                        f"record {record.document_id}: unknown supporting source {supporting_id}"
                    )
                    continue
                if supporting.hospital_id != record.hospital_id:
                    errors.append(
                        f"record {record.document_id}: supporting source hospital mismatch"
                    )
                if supporting.campus_id not in {None, record.campus_id}:
                    errors.append(
                        f"record {record.document_id}: supporting source campus mismatch"
                    )
                if supporting.review_status != ReviewStatus.VERIFIED_OFFICIAL:
                    errors.append(
                        f"record {record.document_id}: supporting source is not verified"
                    )
                if supporting.source_tier == SourceTier.MAP_PROVIDER:
                    errors.append(
                        f"record {record.document_id}: map source cannot support official facts"
                    )
                elif supporting.source_tier == SourceTier.OFFICIAL_APPOINTMENT:
                    if record.category != Category.APPOINTMENT:
                        errors.append(
                            f"record {record.document_id}: appointment platform cannot authorize "
                            "non-appointment facts"
                        )
                elif supporting.source_tier not in AUTHORITATIVE_TIERS:
                    errors.append(
                        f"record {record.document_id}: supporting source tier is not authoritative"
                    )
            expected_hash = hashlib.sha256(normalize_text(record.content).encode("utf-8")).hexdigest()
            if record.content_hash != expected_hash:
                errors.append(f"record {record.document_id}: content hash mismatch")
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


def _is_link_or_junction(path: Path) -> bool:
    """Return whether *path* can redirect traversal outside its parent tree."""
    metadata = os.lstat(path)
    attributes = getattr(metadata, "st_file_attributes", 0)
    reparse_point = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x0400)
    reparse_tag = getattr(metadata, "st_reparse_tag", 0)
    return path.is_symlink() or bool(attributes & reparse_point) or bool(reparse_tag)


def _assert_snapshot_tree_safe(snapshot_root: Path) -> None:
    """Reject linked or escaping snapshot entries before ``copytree`` dereferences them."""
    if _is_link_or_junction(snapshot_root):
        raise ValueError("snapshot source contains a symlink or junction")
    root = snapshot_root.resolve(strict=True)
    pending = [snapshot_root]
    while pending:
        directory = pending.pop()
        with os.scandir(directory) as entries:
            for entry in entries:
                child = Path(entry.path)
                if entry.is_symlink() or _is_link_or_junction(child):
                    raise ValueError("snapshot source contains a symlink or junction")
                try:
                    child.resolve(strict=True).relative_to(root)
                except ValueError as exc:
                    raise ValueError("snapshot source escapes its root") from exc
                if entry.is_dir(follow_symlinks=False):
                    pending.append(child)


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
            _assert_snapshot_tree_safe(snapshot_root)
            copied_snapshots = temporary / "snapshots"
            shutil.copytree(snapshot_root, copied_snapshots, symlinks=True)
            _assert_snapshot_tree_safe(copied_snapshots)
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
