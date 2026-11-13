"""Scope-first hybrid retrieval for the verified Jiangsu hospital corpus."""
from __future__ import annotations

import hashlib

from .models import KnowledgeRecord
from .store import HospitalCorpus


COLLECTION_PREFIX = "careguide_jiangsu_hospitals_v2"


def corpus_fingerprint(corpus: HospitalCorpus) -> str:
    """Return the authenticated manifest fingerprint bound to an index."""
    return hashlib.sha256((corpus.root / "manifest.json").read_bytes()).hexdigest()


def hospital_collection_name(corpus: HospitalCorpus) -> str:
    return f"{COLLECTION_PREFIX}_{corpus_fingerprint(corpus)[:12]}"


def _scope(corpus: HospitalCorpus, record: KnowledgeRecord):
    hospital = next(
        item for item in corpus.hospitals
        if item.hospital_id == record.hospital_id
    )
    campus = next(
        item for item in hospital.campuses
        if item.campus_id == record.campus_id
    )
    return hospital, campus


def hospital_retrieval_text(
    corpus: HospitalCorpus,
    record: KnowledgeRecord,
) -> str:
    hospital, campus = _scope(corpus, record)
    return " ".join((
        hospital.official_name,
        *hospital.aliases,
        hospital.city,
        campus.official_name,
        campus.address,
        record.category.value,
        record.title,
        record.content,
    ))


def hospital_evidence(
    corpus: HospitalCorpus,
    record: KnowledgeRecord,
) -> dict:
    hospital, campus = _scope(corpus, record)
    return {
        "id": record.document_id,
        "title": record.title,
        "content": record.content,
        "scope": {
            "hospital_id": hospital.hospital_id,
            "hospital": hospital.official_name,
            "city": hospital.city,
            "campus_id": campus.campus_id,
            "campus": campus.official_name,
            "category": record.category.value,
        },
        "source": {
            "source_id": record.source_id,
            "url": record.source_url,
            "tier": record.source_tier.value,
            "published_at": (
                record.published_at.isoformat() if record.published_at else None
            ),
            "reviewed_at": (
                record.reviewed_at.isoformat() if record.reviewed_at else None
            ),
            "review_status": record.review_status.value,
        },
    }
