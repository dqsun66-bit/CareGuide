"""Strict contracts for the official Jiangsu hospital corpus."""
from datetime import date, datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from careguide.hospital_corpus.models import (
    Campus,
    Category,
    CorpusManifest,
    Hospital,
    KnowledgeRecord,
    ReviewStatus,
    Source,
    VerificationEntry,
)


CN_TZ = timezone(timedelta(hours=8))
COLLECTED = datetime(2026, 11, 4, 9, 0, tzinfo=CN_TZ)
REVIEWED = datetime(2026, 11, 4, 15, 0, tzinfo=CN_TZ)
SHA256 = "a" * 64


def valid_campus(**changes):
    payload = {
        "campus_id": "js_taizhou_003_main",
        "official_name": "主院区",
        "address": "江苏省泰州市姜堰区健康路27号",
        "address_source_id": "src_tz2_contact",
    }
    payload.update(changes)
    return Campus(**payload)


def valid_hospital(**changes):
    payload = {
        "hospital_id": "js_taizhou_003",
        "official_name": "泰州市第二人民医院",
        "aliases": ("泰州二院", "姜堰人民医院"),
        "city": "泰州",
        "hospital_type": "general",
        "grade": "三级甲等",
        "grade_source_id": "src_tz2_grade_2025",
        "official_domains": ("www.jyry.com",),
        "campuses": (valid_campus(),),
    }
    payload.update(changes)
    return Hospital(**payload)


def valid_source(**changes):
    payload = {
        "source_id": "src_tz2_contact",
        "hospital_id": "js_taizhou_003",
        "campus_id": "js_taizhou_003_main",
        "title": "泰州市第二人民医院联系我们",
        "url": "https://www.jyry.com/",
        "source_tier": "hospital_official",
        "category": "contact",
        "published_at": None,
        "collected_at": COLLECTED,
        "reviewed_at": REVIEWED,
        "review_status": "verified_official",
        "snapshot_path": "snapshots/js_taizhou_003/src_tz2_contact.html",
        "http_status": 200,
        "content_type": "text/html; charset=utf-8",
        "retrieved_sha256": SHA256,
    }
    payload.update(changes)
    return Source(**payload)


def valid_record(**changes):
    payload = {
        "document_id": "js_taizhou_003_contact_0001",
        "hospital_id": "js_taizhou_003",
        "campus_id": "js_taizhou_003_main",
        "city": "泰州",
        "category": "contact",
        "title": "主院区地址与预约咨询",
        "content": "医院官网列明主院区位于江苏省泰州市姜堰区健康路27号，并公开了就医、咨询和预约服务电话。具体服务时间和接听状态应以医院最新公告为准。",
        "source_id": "src_tz2_contact",
        "source_url": "https://www.jyry.com/",
        "source_tier": "hospital_official",
        "published_at": None,
        "collected_at": COLLECTED,
        "reviewed_at": REVIEWED,
        "review_status": "verified_official",
        "content_hash": SHA256,
        "language": "zh-CN",
    }
    payload.update(changes)
    return KnowledgeRecord(**payload)


def test_hospital_requires_scoped_campus_and_grade_source():
    hospital = valid_hospital()
    assert hospital.campuses[0].campus_id.endswith("_main")
    assert hospital.grade_source_id == "src_tz2_grade_2025"


@pytest.mark.parametrize(
    "changes, message",
    [
        ({"aliases": ("泰州二院", "泰州二院")}, "Duplicate hospital aliases"),
        ({"official_domains": ("www.jyry.com", "www.jyry.com")}, "Duplicate official domains"),
        ({"campuses": (valid_campus(), valid_campus())}, "Duplicate campus identities"),
        ({"campuses": (valid_campus(campus_id="js_nanjing_001_main"),)}, "Campus does not belong"),
    ],
)
def test_hospital_rejects_ambiguous_identity(changes, message):
    with pytest.raises(ValidationError, match=message):
        valid_hospital(**changes)


@pytest.mark.parametrize(
    "url",
    [
        "http://www.jyry.com/",
        "https://user:password@www.jyry.com/",
        "https://www.jyry.com:444/",
    ],
)
def test_source_requires_public_https_url(url):
    with pytest.raises(ValidationError, match="public HTTPS"):
        valid_source(url=url)


def test_source_preserves_unknown_publication_date():
    source = valid_source(published_at=None)
    assert source.published_at is None


def test_source_rejects_publication_after_collection():
    with pytest.raises(ValidationError, match="publication is later"):
        valid_source(published_at=date(2026, 11, 5))


@pytest.mark.parametrize("field", ["collected_at", "reviewed_at"])
def test_source_rejects_naive_datetimes(field):
    with pytest.raises(ValidationError, match="timezone-aware"):
        valid_source(**{field: datetime(2026, 11, 4, 9, 0)})


def test_record_cannot_publish_unreviewed_source():
    with pytest.raises(ValidationError, match="verified_official"):
        valid_record(review_status="needs_review", reviewed_at=None)


def test_record_rejects_wrong_campus_scope():
    with pytest.raises(ValidationError, match="Campus does not belong"):
        valid_record(campus_id="js_nanjing_001_main")


def test_record_rejects_invalid_hash_and_unknown_category():
    with pytest.raises(ValidationError):
        valid_record(content_hash="not-a-hash")
    with pytest.raises(ValidationError):
        valid_record(category="doctor_schedule")


def test_verification_entry_requires_decision_for_resolved_state():
    with pytest.raises(ValidationError, match="decision"):
        VerificationEntry(
            verification_id="verify-1",
            subject_ids=("src-a", "src-b"),
            status="conflict",
            reason="两个官方页面公布的服务时间不同",
            decision=None,
            reviewed_at=REVIEWED,
        )


def test_manifest_uses_fixed_stage_and_sha256_file_map():
    manifest = CorpusManifest(
        version="jiangsu-hospitals-v1",
        stage="pilot_taizhou_3_of_15",
        built_at=REVIEWED,
        hospital_count=15,
        source_count=6,
        record_count=12,
        verification_count=1,
        files={
            "hospitals.json": "a" * 64,
            "sources.json": "b" * 64,
            "records.json": "c" * 64,
            "verification.json": "d" * 64,
        },
    )
    assert manifest.stage == "pilot_taizhou_3_of_15"
    assert set(manifest.files) == {
        "hospitals.json", "sources.json", "records.json", "verification.json"
    }


def test_enums_keep_the_approved_public_values():
    assert Category.APPOINTMENT.value == "appointment"
    assert ReviewStatus.VERIFIED_OFFICIAL.value == "verified_official"
