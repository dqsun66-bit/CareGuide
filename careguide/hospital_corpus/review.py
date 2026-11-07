"""Apply independent, hash-bound approvals to offline hospital corpus inputs."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import hashlib
import html
from html.parser import HTMLParser
import json
from pathlib import Path
import re
from typing import Mapping, Sequence


@dataclass(frozen=True)
class ReviewedBundle:
    sources: tuple[dict, ...]
    records: tuple[dict, ...]
    verification: tuple[dict, ...]


class _EvidenceTextParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._ignored_depth = 0

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag in {"script", "style"}:
            self._ignored_depth += 1
        for name, value in attrs:
            if name in {"content", "title", "alt", "aria-label"} and value:
                self.parts.append(value)

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style"} and self._ignored_depth:
            self._ignored_depth -= 1

    def handle_data(self, data: str) -> None:
        if not self._ignored_depth:
            self.parts.append(data)


def _unique_by(items: Sequence[Mapping], key: str, label: str) -> dict[str, Mapping]:
    indexed: dict[str, Mapping] = {}
    for item in items:
        identity = item.get(key)
        if not isinstance(identity, str) or not identity or identity in indexed:
            raise ValueError(f"{label} identities must be present and unique")
        indexed[identity] = item
    return indexed


def _parse_aware(value: object, label: str) -> datetime:
    if not isinstance(value, str):
        raise ValueError(f"{label} must be a timezone-aware datetime")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"{label} must be a timezone-aware datetime") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{label} must be a timezone-aware datetime")
    return parsed


def _snapshot_bytes(source_dir: Path, source: Mapping) -> bytes:
    source_root = source_dir.resolve(strict=True)
    portable = source.get("snapshot_path")
    if not isinstance(portable, str) or not portable:
        raise ValueError(f"source {source.get('source_id')}: invalid snapshot path")
    candidate = source_dir.joinpath(*Path(portable).parts)
    try:
        resolved = candidate.resolve(strict=True)
        resolved.relative_to(source_root)
    except (OSError, ValueError) as exc:
        raise ValueError(f"source {source.get('source_id')}: invalid snapshot path") from exc
    if not resolved.is_file():
        raise ValueError(f"source {source.get('source_id')}: invalid snapshot path")
    raw = resolved.read_bytes()
    if hashlib.sha256(raw).hexdigest() != source.get("retrieved_sha256"):
        raise ValueError(f"source {source.get('source_id')}: snapshot hash mismatch")
    return raw


def _snapshot_text(source_dir: Path, source: Mapping) -> str:
    raw = _snapshot_bytes(source_dir, source)
    content_type = source.get("content_type")
    charset = None
    if isinstance(content_type, str):
        match = re.search(r"charset\s*=\s*['\"]?([\w-]+)", content_type, re.I)
        if match:
            charset = match.group(1).lower().replace("_", "-")
    if charset in {"utf-8", "utf8"}:
        decoded = raw.decode("utf-8", errors="replace")
    elif charset in {"gb18030", "gbk", "gb2312"}:
        decoded = raw.decode("gb18030", errors="replace")
    else:
        decoded = None
        for encoding in ("utf-8", "gb18030"):
            try:
                decoded = raw.decode(encoding)
                break
            except UnicodeDecodeError:
                continue
        if decoded is None:
            raise ValueError(
                f"source {source.get('source_id')}: unsupported snapshot encoding"
            )
    parser = _EvidenceTextParser()
    parser.feed(decoded)
    return re.sub(r"\s+", " ", html.unescape(" ".join(parser.parts))).strip()


def apply_review_manifest(
    source_dir: Path,
    sources: Sequence[Mapping],
    records: Sequence[Mapping],
    *,
    manifest_name: str = "review_manifest.json",
    trusted_sources: Sequence[Mapping] = (),
    trusted_source_dir: Path | None = None,
) -> ReviewedBundle:
    source_dir = Path(source_dir)
    manifest = json.loads((source_dir / manifest_name).read_text(encoding="utf-8"))
    if manifest.get("version") != "jiangsu-hospital-review-v1":
        raise ValueError("unsupported hospital review manifest version")
    source_reviews = manifest.get("source_reviews")
    record_reviews = manifest.get("record_reviews")
    gap_reviews = manifest.get("gap_reviews")
    if not all(isinstance(section, list) for section in (
        source_reviews, record_reviews, gap_reviews
    )):
        raise ValueError("review manifest sections must be lists")

    sources_by_id = _unique_by(sources, "source_id", "source")
    trusted_by_id = _unique_by(trusted_sources, "source_id", "trusted source")
    if set(sources_by_id) & set(trusted_by_id):
        raise ValueError("new and trusted source identities overlap")
    if trusted_by_id and trusted_source_dir is None:
        raise ValueError("trusted_source_dir is required for trusted sources")
    all_sources = {**trusted_by_id, **sources_by_id}
    source_approvals = _unique_by(source_reviews, "source_id", "source review")
    if set(source_approvals) != set(sources_by_id):
        raise ValueError("source review approvals do not match the frozen source set")

    reviewed_sources: list[dict] = []
    verification: list[dict] = []
    evidence_by_source: dict[str, str] = {}
    if trusted_source_dir is not None:
        for source_id, source in trusted_by_id.items():
            if source.get("review_status") != "verified_official":
                raise ValueError(f"trusted source {source_id} is not verified")
            _parse_aware(source.get("reviewed_at"), f"trusted source {source_id} reviewed_at")
            evidence_by_source[source_id] = _snapshot_text(Path(trusted_source_dir), source)
    for source_id, source in sources_by_id.items():
        approval = source_approvals[source_id]
        reviewed_at = _parse_aware(
            approval.get("reviewed_at"), f"source review {source_id} reviewed_at"
        )
        collected_at = _parse_aware(
            source.get("collected_at"), f"source {source_id} collected_at"
        )
        reviewer = approval.get("reviewer")
        if (
            approval.get("retrieved_sha256") != source.get("retrieved_sha256")
            or approval.get("decision") != "approved"
            or not isinstance(reviewer, str)
            or not reviewer.strip()
            or reviewed_at < collected_at
        ):
            raise ValueError(f"invalid or stale source review for {source_id}")
        evidence_by_source[source_id] = _snapshot_text(source_dir, source)
        reviewed = dict(source)
        reviewed["reviewed_at"] = approval["reviewed_at"]
        reviewed["review_status"] = "verified_official"
        reviewed_sources.append(reviewed)
        verification.append({
            "verification_id": f"verify_{source_id}",
            "subject_ids": [source_id],
            "status": "verified_official",
            "reason": f"{reviewer.strip()}已核对冻结快照及其绑定哈希。",
            "decision": approval.get("note") or "纳入江苏医院官方知识库。",
            "reviewed_at": reviewed_at,
        })

    records_by_id = _unique_by(records, "document_id", "record")
    record_approvals = _unique_by(record_reviews, "document_id", "record review")
    if set(record_approvals) != set(records_by_id):
        raise ValueError("record review approvals do not match the candidate record set")

    reviewed_records: list[dict] = []
    for document_id, record in records_by_id.items():
        approval = record_approvals[document_id]
        primary_id = record.get("source_id")
        supporting_ids = record.get("supporting_source_ids", ())
        if not isinstance(primary_id, str) or not isinstance(supporting_ids, (list, tuple)):
            raise ValueError(f"record {document_id} has invalid source bindings")
        bound_source_ids = (primary_id, *supporting_ids)
        approved_source_ids = approval.get("source_ids")
        if (
            not isinstance(approved_source_ids, list)
            or len(approved_source_ids) != len(set(approved_source_ids))
            or set(approved_source_ids) != set(bound_source_ids)
        ):
            raise ValueError(f"record review source set mismatch for {document_id}")
        if any(source_id not in all_sources for source_id in bound_source_ids):
            raise ValueError(f"record {document_id} references an unapproved source")

        required_terms = approval.get("required_terms")
        reviewer = approval.get("reviewer")
        reviewed_at = _parse_aware(
            approval.get("reviewed_at"), f"record review {document_id} reviewed_at"
        )
        collected_times = [
            _parse_aware(
                all_sources[source_id].get("collected_at"),
                f"source {source_id} collected_at",
            )
            for source_id in bound_source_ids
        ]
        if (
            approval.get("content_hash") != record.get("content_hash")
            or approval.get("decision") != "approved"
            or not isinstance(reviewer, str)
            or not reviewer.strip()
            or reviewed_at < max(collected_times)
            or not isinstance(required_terms, list)
            or not required_terms
            or any(not isinstance(term, str) or not term.strip() for term in required_terms)
        ):
            raise ValueError(f"invalid or stale record review for {document_id}")
        evidence = " ".join(evidence_by_source[source_id] for source_id in bound_source_ids)
        missing = [term for term in required_terms if term not in evidence]
        if missing:
            raise ValueError(f"record {document_id} lacks reviewed evidence terms: {missing}")

        reviewed = dict(record)
        reviewed["reviewed_at"] = approval["reviewed_at"]
        reviewed["review_status"] = "verified_official"
        reviewed_records.append(reviewed)
        verification.append({
            "verification_id": f"verify_{document_id}",
            "subject_ids": [document_id, *bound_source_ids],
            "status": "verified_official",
            "reason": f"{reviewer.strip()}已逐项核对发布正文与绑定快照证据词。",
            "decision": "批准发布该知识记录。",
            "reviewed_at": reviewed_at,
        })

    gaps = _unique_by(gap_reviews, "hospital_id", "gap review") if gap_reviews else {}
    for hospital_id, review in gaps.items():
        reviewed_at = _parse_aware(
            review.get("reviewed_at"), f"gap review {hospital_id} reviewed_at"
        )
        reviewer = review.get("reviewer")
        reason = review.get("reason")
        if (
            review.get("decision") != "needs_review"
            or not isinstance(reviewer, str)
            or not reviewer.strip()
            or not isinstance(reason, str)
            or not reason.strip()
        ):
            raise ValueError(f"invalid gap review for {hospital_id}")
        verification.append({
            "verification_id": f"verify_{hospital_id}_mobility_gap",
            "subject_ids": [hospital_id],
            "status": "needs_review",
            "reason": reason,
            "decision": None,
            "reviewed_at": reviewed_at,
        })

    return ReviewedBundle(
        sources=tuple(reviewed_sources),
        records=tuple(reviewed_records),
        verification=tuple(verification),
    )
