"""Contracts added by the reviewed Jiangsu hospital corpus v2 expansion."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
import hashlib

import pytest
from pydantic import ValidationError

from careguide.hospital_corpus.models import (
    CorpusManifest,
    Hospital,
    KnowledgeRecord,
    Source,
)
from careguide.hospital_corpus.normalize import normalize_text
from careguide.hospital_corpus.store import HospitalCorpus


CN_TZ = timezone(timedelta(hours=8))
NOW = datetime(2026, 11, 7, 10, 0, tzinfo=CN_TZ)
SNAPSHOT = b"reviewed official evidence"
SNAPSHOT_SHA = hashlib.sha256(SNAPSHOT).hexdigest()
CONTENT = (
    "医院官网公开主院区地址和咨询方式，患者就诊前应通过医院官方渠道确认"
    "最新服务时间、院区安排及预约要求，避免依据过期资料安排行程。"
)


def _manifest() -> CorpusManifest:
    return CorpusManifest(
        version="jiangsu-hospitals-v2",
        stage="expanded_core_15_of_15",
        built_at=NOW,
        hospital_count=15,
        source_count=41,
        record_count=45,
        verification_count=98,
        files={name: "0" * 64 for name in (
            "hospitals.json",
            "sources.json",
            "records.json",
            "verification.json",
        )},
    )


def _record_payload(**changes) -> dict:
    payload = {
        "document_id": "js_nanjing_001_service_v2",
        "hospital_id": "js_nanjing_001",
        "campus_id": "js_nanjing_001_main",
        "city": "南京",
        "category": "contact",
        "title": "主院区就医联系信息",
        "content": CONTENT,
        "source_id": "src_contact",
        "source_url": "https://hospital.example.com/contact",
        "source_tier": "hospital_official",
        "published_at": None,
        "collected_at": NOW,
        "reviewed_at": NOW,
        "review_status": "verified_official",
        "content_hash": hashlib.sha256(normalize_text(CONTENT).encode("utf-8")).hexdigest(),
        "language": "zh-CN",
    }
    payload.update(changes)
    return payload


def _source(source_id: str, **changes) -> Source:
    payload = {
        "source_id": source_id,
        "hospital_id": "js_nanjing_001",
        "campus_id": "js_nanjing_001_main",
        "title": "医院官方资料",
        "url": f"https://hospital.example.com/{source_id}",
        "source_tier": "hospital_official",
        "category": "contact",
        "published_at": None,
        "valid_until": date(2027, 11, 7),
        "collected_at": NOW,
        "reviewed_at": NOW,
        "review_status": "verified_official",
        "snapshot_path": f"snapshots/js_nanjing_001/{source_id}.html",
        "http_status": 200,
        "content_type": "text/html; charset=utf-8",
        "retrieved_sha256": SNAPSHOT_SHA,
    }
    payload.update(changes)
    return Source.model_validate(payload)


def _corpus(tmp_path, supporting: Source, *, supporting_id: str | None = None):
    identity = _source(
        "src_identity",
        campus_id=None,
        category="identity",
        url="https://hospital.example.com/about",
    )
    contact = _source("src_contact", url="https://hospital.example.com/contact")
    hospital = Hospital.model_validate({
        "hospital_id": "js_nanjing_001",
        "official_name": "南京示例医院",
        "aliases": [],
        "city": "南京",
        "hospital_type": "general",
        "grade": "三级甲等",
        "grade_source_id": "src_identity",
        "official_domains": ["hospital.example.com"],
        "campuses": [{
            "campus_id": "js_nanjing_001_main",
            "official_name": "主院区",
            "address": "江苏省南京市示例路一号",
            "address_source_id": "src_contact",
        }],
    })
    sources = {source.source_id: source for source in (identity, contact, supporting)}
    for source in sources.values():
        snapshot = tmp_path / source.snapshot_path
        snapshot.parent.mkdir(parents=True, exist_ok=True)
        snapshot.write_bytes(SNAPSHOT)
    record = KnowledgeRecord.model_validate(_record_payload(
        supporting_source_ids=[supporting_id or supporting.source_id],
    ))
    return HospitalCorpus(
        tmp_path,
        _manifest(),
        (hospital,),
        tuple(sources.values()),
        (record,),
        (),
    )


def test_manifest_accepts_expanded_v2_stage():
    assert _manifest().version == "jiangsu-hospitals-v2"


def test_v1_record_defaults_to_no_supporting_sources():
    assert KnowledgeRecord.model_validate(_record_payload()).supporting_source_ids == ()


@pytest.mark.parametrize(
    "supporting_ids, message",
    [
        (["src_contact"], "primary source cannot be repeated as a supporting source"),
        (["src_identity", "src_identity"], "duplicate supporting source"),
    ],
)
def test_record_rejects_duplicate_supporting_source_ids(supporting_ids, message):
    with pytest.raises(ValidationError, match=message):
        KnowledgeRecord.model_validate(_record_payload(supporting_source_ids=supporting_ids))


def test_validation_rejects_unknown_supporting_source(tmp_path):
    corpus = _corpus(tmp_path, _source("src_support"), supporting_id="src_missing")
    assert "record js_nanjing_001_service_v2: unknown supporting source src_missing" in (
        corpus.validate(today=date(2026, 11, 7)).errors
    )


@pytest.mark.parametrize(
    "changes, expected",
    [
        (
            {"hospital_id": "js_nanjing_002", "campus_id": None},
            "record js_nanjing_001_service_v2: supporting source hospital mismatch",
        ),
        (
            {"review_status": "needs_review"},
            "record js_nanjing_001_service_v2: supporting source is not verified",
        ),
        (
            {"source_tier": "map_provider", "url": "https://www.amap.com/place"},
            "record js_nanjing_001_service_v2: map source cannot support official facts",
        ),
        (
            {
                "source_tier": "official_appointment",
                "category": "appointment",
                "url": "https://js.guahaoe.com/hospital",
            },
            "record js_nanjing_001_service_v2: appointment platform cannot authorize non-appointment facts",
        ),
    ],
)
def test_validation_rejects_unfit_supporting_sources(tmp_path, changes, expected):
    corpus = _corpus(tmp_path, _source("src_support", **changes))
    assert expected in corpus.validate(today=date(2026, 11, 7)).errors
