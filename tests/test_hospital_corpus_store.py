"""Integrity and provenance checks for versioned hospital corpus assets."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path

import pytest

from careguide.hospital_corpus.store import HospitalCorpus, write_corpus


CN_TZ = timezone(timedelta(hours=8))
NOW = datetime(2026, 11, 4, 15, 0, tzinfo=CN_TZ)
SNAPSHOT = b"official hospital snapshot"
SNAPSHOT_SHA = hashlib.sha256(SNAPSHOT).hexdigest()
FILES = ("hospitals.json", "sources.json", "records.json", "verification.json")


def _dump(path: Path, value) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _payloads():
    hospitals = [{
        "hospital_id": "js_taizhou_003",
        "official_name": "泰州市第二人民医院",
        "aliases": ["泰州二院", "姜堰人民医院"],
        "city": "泰州",
        "hospital_type": "general",
        "grade": "三级甲等",
        "grade_source_id": "src_tz2_official",
        "official_domains": ["www.jyry.com"],
        "campuses": [{
            "campus_id": "js_taizhou_003_main",
            "official_name": "主院区",
            "address": "江苏省泰州市姜堰区健康路27号",
            "address_source_id": "src_tz2_official",
        }],
    }]
    sources = [{
        "source_id": "src_tz2_official",
        "hospital_id": "js_taizhou_003",
        "campus_id": "js_taizhou_003_main",
        "title": "泰州市第二人民医院官网",
        "url": "https://www.jyry.com/",
        "source_tier": "hospital_official",
        "category": "contact",
        "published_at": None,
        "valid_until": "2027-02-02",
        "collected_at": NOW.isoformat(),
        "reviewed_at": NOW.isoformat(),
        "review_status": "verified_official",
        "snapshot_path": "snapshots/js_taizhou_003/src_tz2_official.html",
        "http_status": 200,
        "content_type": "text/html; charset=utf-8",
        "retrieved_sha256": SNAPSHOT_SHA,
    }]
    records = [{
        "document_id": "js_taizhou_003_contact_0001",
        "hospital_id": "js_taizhou_003",
        "campus_id": "js_taizhou_003_main",
        "city": "泰州",
        "category": "contact",
        "title": "主院区地址与预约咨询",
        "content": "医院官网列明主院区位于江苏省泰州市姜堰区健康路27号，并公开就医、咨询和预约服务电话；具体服务时间应以医院最新公告为准。",
        "source_id": "src_tz2_official",
        "source_url": "https://www.jyry.com/",
        "source_tier": "hospital_official",
        "published_at": None,
        "collected_at": NOW.isoformat(),
        "reviewed_at": NOW.isoformat(),
        "review_status": "verified_official",
        "content_hash": hashlib.sha256("record".encode()).hexdigest(),
        "language": "zh-CN",
    }]
    return hospitals, sources, records, []


def _write_fixture(root: Path, mutate=None, *, corrupt_manifest=False) -> Path:
    root.mkdir()
    snapshot = root / "snapshots/js_taizhou_003/src_tz2_official.html"
    snapshot.parent.mkdir(parents=True)
    snapshot.write_bytes(SNAPSHOT)
    hospitals, sources, records, verification = _payloads()
    if mutate:
        mutate(hospitals, sources, records, verification, root)
    values = dict(zip(FILES, (hospitals, sources, records, verification)))
    for filename, value in values.items():
        _dump(root / filename, value)
    hashes = {filename: hashlib.sha256((root / filename).read_bytes()).hexdigest()
              for filename in FILES}
    if corrupt_manifest:
        hashes["records.json"] = "0" * 64
    _dump(root / "manifest.json", {
        "version": "jiangsu-hospitals-v1",
        "stage": "pilot_taizhou_3_of_15",
        "built_at": NOW.isoformat(),
        "hospital_count": len(hospitals),
        "source_count": len(sources),
        "record_count": len(records),
        "verification_count": len(verification),
        "files": hashes,
    })
    return root


def test_valid_corpus_loads_and_validates(tmp_path):
    corpus = HospitalCorpus.load(_write_fixture(tmp_path / "corpus"))
    assert corpus.manifest.version == "jiangsu-hospitals-v1"
    assert corpus.record("js_taizhou_003_contact_0001").source_id == "src_tz2_official"
    assert corpus.validate(today=date(2026, 11, 4)).errors == ()


def test_load_rejects_manifest_hash_mismatch(tmp_path):
    root = _write_fixture(tmp_path / "corpus", corrupt_manifest=True)
    with pytest.raises(ValueError, match="integrity mismatch"):
        HospitalCorpus.load(root)


def test_load_rejects_duplicate_document_identity(tmp_path):
    def duplicate(_hospitals, _sources, records, _verification, _root):
        records.append(dict(records[0]))

    root = _write_fixture(tmp_path / "corpus", duplicate)
    with pytest.raises(ValueError, match="Duplicate record identities"):
        HospitalCorpus.load(root)


def test_load_rejects_manifest_count_mismatch(tmp_path):
    root = _write_fixture(tmp_path / "corpus")
    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    manifest["record_count"] = 99
    _dump(root / "manifest.json", manifest)
    with pytest.raises(ValueError, match="Manifest counts"):
        HospitalCorpus.load(root)


@pytest.mark.parametrize(
    "mutation, expected",
    [
        (lambda _h, _s, r, _v, _p: r[0].update(source_id="missing"), "unknown source"),
        (lambda _h, _s, r, _v, _p: r[0].update(campus_id="js_taizhou_003_other"), "unknown campus"),
        (lambda _h, s, _r, _v, _p: s[0].update(url="https://evil.example/"), "unapproved source host"),
        (lambda _h, s, _r, _v, _p: s[0].update(valid_until="2026-01-01"), "expired source"),
        (lambda _h, _s, r, _v, _p: r[0].update(source_url="https://www.jyry.com/other"), "source URL mismatch"),
        (lambda _h, s, _r, _v, _p: s[0].update(review_status="needs_review", reviewed_at=None), "not verified"),
    ],
)
def test_validation_reports_provenance_errors(tmp_path, mutation, expected):
    corpus = HospitalCorpus.load(_write_fixture(tmp_path / "corpus", mutation))
    report = corpus.validate(today=date(2026, 11, 4))
    assert any(expected in error for error in report.errors)


def test_validation_reports_missing_or_modified_snapshot(tmp_path):
    def remove_snapshot(_h, _s, _r, _v, root):
        (root / "snapshots/js_taizhou_003/src_tz2_official.html").unlink()

    missing = HospitalCorpus.load(_write_fixture(tmp_path / "missing", remove_snapshot))
    assert any("missing snapshot" in error for error in missing.validate(today=date(2026, 11, 4)).errors)

    def alter_snapshot(_h, _s, _r, _v, root):
        (root / "snapshots/js_taizhou_003/src_tz2_official.html").write_bytes(b"changed")

    altered = HospitalCorpus.load(_write_fixture(tmp_path / "altered", alter_snapshot))
    assert any("snapshot hash mismatch" in error
               for error in altered.validate(today=date(2026, 11, 4)).errors)


def test_atomic_writer_refuses_invalid_assets_without_partial_target(tmp_path):
    hospitals, sources, records, verification = _payloads()
    records[0]["source_id"] = "missing"
    target = tmp_path / "published"
    with pytest.raises(ValueError, match="unknown source"):
        write_corpus(target, hospitals=hospitals, sources=sources, records=records,
                     verification=verification, built_at=NOW)
    assert not target.exists()


def test_validation_cli_prints_metadata_without_record_bodies(tmp_path, capsys):
    from scripts.validate_jiangsu_hospital_corpus import main

    root = _write_fixture(tmp_path / "corpus")
    assert main(["--corpus-dir", str(root), "--today", "2026-11-04"]) == 0
    output = capsys.readouterr().out
    payload = json.loads(output)
    assert payload == {
        "conflict_ids": [],
        "duplicate_groups": [],
        "errors": [],
        "expired_source_ids": [],
        "hospital_count": 1,
        "record_count": 1,
        "source_count": 1,
        "status": "valid",
        "warnings": [],
    }
    assert "主院区地址与预约咨询" not in output
