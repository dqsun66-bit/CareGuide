from __future__ import annotations

from datetime import date
import json
from pathlib import Path
import re
import shutil
import socket

import pytest

from careguide.hospital_corpus.models import ReviewStatus, SourceTier
from careguide.hospital_corpus.store import HospitalCorpus
from scripts import build_jiangsu_hospital_pilot


ROOT = Path(__file__).resolve().parents[1] / "data" / "jiangsu_hospitals_v1"
APPROVED_IDS = {
    "js_taizhou_001", "js_taizhou_002", "js_taizhou_003",
    "js_nanjing_001", "js_nanjing_002", "js_nanjing_003", "js_nanjing_004",
    "js_suzhou_001", "js_suzhou_002", "js_suzhou_003",
    "js_wuxi_001", "js_wuxi_002", "js_changzhou_001",
    "js_nantong_001", "js_yangzhou_001",
}
PILOT_IDS = {"js_taizhou_001", "js_taizhou_002", "js_taizhou_003"}


def test_registry_contains_exact_approved_hospitals():
    corpus = HospitalCorpus.load(ROOT)
    assert {hospital.hospital_id for hospital in corpus.hospitals} == APPROVED_IDS
    assert len(corpus.hospitals) == 15
    assert {hospital.city for hospital in corpus.hospitals} == {
        "泰州", "南京", "苏州", "无锡", "常州", "南通", "扬州"
    }


def test_pilot_publishes_only_three_taizhou_hospitals():
    corpus = HospitalCorpus.load(ROOT)
    assert corpus.manifest.stage == "pilot_taizhou_3_of_15"
    assert {record.hospital_id for record in corpus.records} == PILOT_IDS
    assert corpus.validate(today=date(2026, 11, 5)).errors == ()
    categories = {
        hospital_id: {record.category.value for record in corpus.records if record.hospital_id == hospital_id}
        for hospital_id in PILOT_IDS
    }
    assert categories == {
        "js_taizhou_001": {"identity", "campus", "appointment"},
        "js_taizhou_002": {"identity", "campus", "appointment"},
        "js_taizhou_003": {"identity", "contact", "appointment"},
    }


def test_pilot_has_two_traceable_official_sources_per_hospital():
    corpus = HospitalCorpus.load(ROOT)
    for hospital_id in PILOT_IDS:
        sources = [source for source in corpus.sources if source.hospital_id == hospital_id]
        assert len({source.url for source in sources}) >= 2
        assert all(source.review_status == ReviewStatus.VERIFIED_OFFICIAL for source in sources)
        assert all(source.source_tier != SourceTier.MAP_PROVIDER for source in sources)
        assert all((ROOT / source.snapshot_path).is_file() for source in sources)


def test_every_published_record_is_verified_and_traceable():
    corpus = HospitalCorpus.load(ROOT)
    source_ids = {source.source_id for source in corpus.sources}
    assert corpus.records
    assert all(record.review_status == ReviewStatus.VERIFIED_OFFICIAL for record in corpus.records)
    assert all(record.source_id in source_ids for record in corpus.records)
    assert all(record.reviewed_at >= record.collected_at for record in corpus.records)


def _snapshot_text(corpus: HospitalCorpus, source_id: str) -> str:
    source = next(source for source in corpus.sources if source.source_id == source_id)
    raw = (ROOT / source.snapshot_path).read_bytes()
    for encoding in ("utf-8", "gb18030"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            pass
    return raw.decode("utf-8", errors="ignore")


def _address_tail(address: str) -> str:
    parts = re.split(r"[市区县镇]", address)
    return parts[-1] if parts[-1] else address[-8:]


def test_registry_facts_are_present_in_their_frozen_evidence():
    corpus = HospitalCorpus.load(ROOT)
    for hospital in corpus.hospitals:
        identities = (hospital.official_name, *hospital.aliases)
        grade_text = _snapshot_text(corpus, hospital.grade_source_id)
        assert any(identity in grade_text for identity in identities), hospital.hospital_id
        assert any(
            marker in grade_text
            for marker in ("三级甲等", "三甲", "Grade III Level A")
        ), hospital.hospital_id
        for campus in hospital.campuses:
            address_text = _snapshot_text(corpus, campus.address_source_id)
            assert any(identity in address_text for identity in identities), campus.campus_id
            assert _address_tail(campus.address) in address_text, campus.campus_id


def test_every_published_alias_has_frozen_evidence():
    corpus = HospitalCorpus.load(ROOT)
    for hospital in corpus.hospitals:
        hospital_evidence = "\n".join(
            _snapshot_text(corpus, source.source_id)
            for source in corpus.sources
            if source.hospital_id == hospital.hospital_id
        )
        for alias in hospital.aliases:
            assert alias in hospital_evidence, (hospital.hospital_id, alias)


def test_verification_log_covers_sources_and_explicit_mobility_gaps():
    corpus = HospitalCorpus.load(ROOT)
    verified_subjects = {
        subject
        for entry in corpus.verification
        if entry.status == ReviewStatus.VERIFIED_OFFICIAL
        for subject in entry.subject_ids
    }
    assert {source.source_id for source in corpus.sources} <= verified_subjects
    assert {record.document_id for record in corpus.records} <= verified_subjects
    gaps = [entry for entry in corpus.verification if entry.status == ReviewStatus.NEEDS_REVIEW]
    assert {entry.subject_ids[0] for entry in gaps} == PILOT_IDS
    assert all("交通、停车或无障碍" in entry.reason for entry in gaps)
    assert all(source.reviewed_at >= source.collected_at for source in corpus.sources)


def test_builder_uses_only_frozen_inputs(monkeypatch, tmp_path):
    def reject_network(*_args, **_kwargs):
        raise AssertionError("offline corpus build attempted a network connection")

    monkeypatch.setattr(socket, "create_connection", reject_network)
    output = tmp_path / "corpus"
    source_dir = Path(__file__).resolve().parents[1] / "data" / "jiangsu_hospital_sources_v1"
    assert build_jiangsu_hospital_pilot.main([
        "--apply",
        "--source-dir", str(source_dir),
        "--output-dir", str(output),
    ]) == 0
    built = HospitalCorpus.load(output)
    assert len(built.hospitals) == 15
    assert len(built.sources) == 29
    assert len(built.records) == 9
    assert len(built.verification) == 41


def test_frozen_source_bundle_has_no_embedded_credentials():
    source_dir = Path(__file__).resolve().parents[1] / "data" / "jiangsu_hospital_sources_v1"
    payload = "\n".join(
        (source_dir / filename).read_text(encoding="utf-8")
        for filename in ("sources.json", "review_manifest.json")
    )
    lowered = payload.casefold()
    assert "api_key" not in lowered
    assert "secret_key" not in lowered
    assert "authorization" not in lowered


def test_review_manifest_binds_every_source_and_record_hash():
    source_dir = Path(__file__).resolve().parents[1] / "data" / "jiangsu_hospital_sources_v1"
    manifest = json.loads((source_dir / "review_manifest.json").read_text(encoding="utf-8"))
    corpus = HospitalCorpus.load(ROOT)
    source_reviews = {review["source_id"]: review for review in manifest["source_reviews"]}
    record_reviews = {review["document_id"]: review for review in manifest["record_reviews"]}
    assert set(source_reviews) == {source.source_id for source in corpus.sources}
    assert set(record_reviews) == {record.document_id for record in corpus.records}
    for source in corpus.sources:
        assert source_reviews[source.source_id]["retrieved_sha256"] == source.retrieved_sha256
    for record in corpus.records:
        review = record_reviews[record.document_id]
        assert set(review["source_ids"]) == {
            record.source_id,
            *record.supporting_source_ids,
        }
        assert review["content_hash"] == record.content_hash
        assert review["required_terms"]


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("decision", "rejected"),
        ("reviewer", ""),
        ("reviewed_at", "2020-01-01T00:00:00+08:00"),
    ],
)
def test_builder_rejects_invalid_record_approval(field, value, tmp_path):
    source = Path(__file__).resolve().parents[1] / "data" / "jiangsu_hospital_sources_v1"
    frozen = tmp_path / "frozen"
    shutil.copytree(source, frozen)
    manifest_path = frozen / "review_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["record_reviews"][0][field] = value
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    output = tmp_path / "corpus"
    with pytest.raises(ValueError, match="invalid or stale record review"):
        build_jiangsu_hospital_pilot.main([
            "--apply",
            "--source-dir", str(frozen),
            "--output-dir", str(output),
        ])
    assert not output.exists()
