"""Integrity and provenance checks for versioned hospital corpus assets."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import subprocess

import pytest

from careguide.hospital_corpus import store as corpus_store
from careguide.hospital_corpus.store import HospitalCorpus, write_corpus
from careguide.hospital_corpus.normalize import normalize_text


CN_TZ = timezone(timedelta(hours=8))
NOW = datetime(2026, 11, 4, 15, 0, tzinfo=CN_TZ)
SNAPSHOT = b"official hospital snapshot"
SNAPSHOT_SHA = hashlib.sha256(SNAPSHOT).hexdigest()
GRADE_SNAPSHOT = b"official health authority grade notice"
GRADE_SNAPSHOT_SHA = hashlib.sha256(GRADE_SNAPSHOT).hexdigest()
FILES = ("hospitals.json", "sources.json", "records.json", "verification.json")
RECORD_CONTENT = "医院官网列明主院区位于江苏省泰州市姜堰区健康路27号，并公开就医、咨询和预约服务电话；具体服务时间应以医院最新公告为准。"


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
        "grade_source_id": "src_tz2_grade",
        "official_domains": ["www.jyry.com"],
        "campuses": [{
            "campus_id": "js_taizhou_003_main",
            "official_name": "主院区",
            "address": "江苏省泰州市姜堰区健康路27号",
            "address_source_id": "src_tz2_contact",
        }],
    }]
    sources = [{
        "source_id": "src_tz2_grade",
        "hospital_id": "js_taizhou_003",
        "campus_id": None,
        "title": "泰州二院成功晋升三级甲等综合医院",
        "url": "https://wjw.taizhou.gov.cn/grade-notice",
        "source_tier": "health_authority",
        "category": "identity",
        "published_at": "2025-07-24",
        "valid_until": "2027-07-24",
        "collected_at": NOW.isoformat(),
        "reviewed_at": NOW.isoformat(),
        "review_status": "verified_official",
        "snapshot_path": "snapshots/js_taizhou_003/src_tz2_grade.html",
        "http_status": 200,
        "content_type": "text/html; charset=utf-8",
        "retrieved_sha256": GRADE_SNAPSHOT_SHA,
    }, {
        "source_id": "src_tz2_contact",
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
        "snapshot_path": "snapshots/js_taizhou_003/src_tz2_contact.html",
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
        "content": RECORD_CONTENT,
        "source_id": "src_tz2_contact",
        "source_url": "https://www.jyry.com/",
        "source_tier": "hospital_official",
        "published_at": None,
        "collected_at": NOW.isoformat(),
        "reviewed_at": NOW.isoformat(),
        "review_status": "verified_official",
        "content_hash": hashlib.sha256(normalize_text(RECORD_CONTENT).encode()).hexdigest(),
        "language": "zh-CN",
    }]
    return hospitals, sources, records, []


def _write_snapshot_source(root: Path) -> Path:
    hospital_dir = root / "js_taizhou_003"
    hospital_dir.mkdir(parents=True)
    (hospital_dir / "src_tz2_grade.html").write_bytes(GRADE_SNAPSHOT)
    (hospital_dir / "src_tz2_contact.html").write_bytes(SNAPSHOT)
    return root


def _write_fixture(root: Path, mutate=None, *, corrupt_manifest=False) -> Path:
    root.mkdir()
    snapshot = root / "snapshots/js_taizhou_003/src_tz2_contact.html"
    snapshot.parent.mkdir(parents=True)
    snapshot.write_bytes(SNAPSHOT)
    (snapshot.parent / "src_tz2_grade.html").write_bytes(GRADE_SNAPSHOT)
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
    assert corpus.record("js_taizhou_003_contact_0001").source_id == "src_tz2_contact"
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
        (lambda _h, s, _r, _v, _p: s[1].update(url="https://evil.example/"), "unapproved source host"),
        (lambda _h, s, _r, _v, _p: s[1].update(valid_until="2026-01-01"), "expired source"),
        (lambda _h, _s, r, _v, _p: r[0].update(source_url="https://www.jyry.com/other"), "source URL mismatch"),
        (lambda _h, s, _r, _v, _p: s[1].update(review_status="needs_review", reviewed_at=None), "not verified"),
        (lambda _h, s, _r, _v, _p: s[0].update(source_tier="map_provider"), "map source cannot authorize"),
        (lambda _h, s, _r, _v, _p: s[1].update(category="appointment"), "category mismatch"),
        (lambda _h, _s, r, _v, _p: r[0].update(content=RECORD_CONTENT + "信息被修改。"), "content hash mismatch"),
    ],
)
def test_validation_reports_provenance_errors(tmp_path, mutation, expected):
    corpus = HospitalCorpus.load(_write_fixture(tmp_path / "corpus", mutation))
    report = corpus.validate(today=date(2026, 11, 4))
    assert any(expected in error for error in report.errors)


def test_validation_reports_missing_or_modified_snapshot(tmp_path):
    def remove_snapshot(_h, _s, _r, _v, root):
        (root / "snapshots/js_taizhou_003/src_tz2_contact.html").unlink()

    missing = HospitalCorpus.load(_write_fixture(tmp_path / "missing", remove_snapshot))
    assert any("missing snapshot" in error for error in missing.validate(today=date(2026, 11, 4)).errors)

    def alter_snapshot(_h, _s, _r, _v, root):
        (root / "snapshots/js_taizhou_003/src_tz2_contact.html").write_bytes(b"changed")

    altered = HospitalCorpus.load(_write_fixture(tmp_path / "altered", alter_snapshot))
    assert any("snapshot hash mismatch" in error
               for error in altered.validate(today=date(2026, 11, 4)).errors)


def test_validation_rejects_snapshot_symlink_escaping_root(tmp_path):
    outside = tmp_path / "outside.html"
    outside.write_bytes(SNAPSHOT)

    def link_outside(_h, sources, _r, _v, root):
        original = root / "snapshots/js_taizhou_003/src_tz2_contact.html"
        original.unlink()
        try:
            original.symlink_to(outside)
        except OSError as exc:
            pytest.skip(f"Symlink creation is unavailable: {exc}")
        sources[1]["retrieved_sha256"] = hashlib.sha256(outside.read_bytes()).hexdigest()

    corpus = HospitalCorpus.load(_write_fixture(tmp_path / "corpus", link_outside))
    assert any("snapshot escapes corpus root" in error
               for error in corpus.validate(today=date(2026, 11, 4)).errors)


def test_load_authenticates_and_parses_each_data_file_from_one_read(tmp_path, monkeypatch):
    root = _write_fixture(tmp_path / "corpus")
    original_open = Path.open
    reads = {filename: 0 for filename in FILES}

    def counting_open(path, *args, **kwargs):
        if path.name in reads:
            reads[path.name] += 1
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", counting_open)
    HospitalCorpus.load(root)
    assert reads == {filename: 1 for filename in FILES}


def test_atomic_writer_refuses_invalid_assets_without_partial_target(tmp_path):
    hospitals, sources, records, verification = _payloads()
    records[0]["source_id"] = "missing"
    target = tmp_path / "published"
    with pytest.raises(ValueError, match="unknown source"):
        write_corpus(target, hospitals=hospitals, sources=sources, records=records,
                     verification=verification, built_at=NOW)
    assert not target.exists()


def test_atomic_writer_publishes_explicit_v2_manifest(tmp_path):
    hospitals, sources, records, verification = _payloads()
    target = tmp_path / "published"
    corpus = write_corpus(
        target,
        hospitals=hospitals,
        sources=sources,
        records=records,
        verification=verification,
        built_at=NOW,
        snapshot_root=_write_snapshot_source(tmp_path / "snapshot-source"),
        version="jiangsu-hospitals-v2",
        stage="expanded_core_15_of_15",
    )
    assert corpus.manifest.version == "jiangsu-hospitals-v2"
    assert corpus.manifest.stage == "expanded_core_15_of_15"


def test_v1_writer_omits_empty_v2_supporting_source_field(tmp_path):
    hospitals, sources, records, verification = _payloads()
    target = tmp_path / "published"
    write_corpus(
        target,
        hospitals=hospitals,
        sources=sources,
        records=records,
        verification=verification,
        built_at=NOW,
        snapshot_root=_write_snapshot_source(tmp_path / "snapshot-source"),
    )
    serialized = json.loads((target / "records.json").read_text(encoding="utf-8"))
    assert "supporting_source_ids" not in serialized[0]


def test_atomic_writer_merges_multiple_snapshot_roots(tmp_path):
    hospitals, sources, records, verification = _payloads()
    first = tmp_path / "first"
    second = tmp_path / "second"
    (first / "js_taizhou_003").mkdir(parents=True)
    (second / "js_taizhou_003").mkdir(parents=True)
    (first / "js_taizhou_003/src_tz2_grade.html").write_bytes(GRADE_SNAPSHOT)
    (second / "js_taizhou_003/src_tz2_contact.html").write_bytes(SNAPSHOT)

    target = tmp_path / "published"
    corpus = write_corpus(
        target,
        hospitals=hospitals,
        sources=sources,
        records=records,
        verification=verification,
        built_at=NOW,
        snapshot_roots=(first, second),
    )

    assert corpus.validate(today=NOW.date()).errors == ()
    assert (target / "snapshots/js_taizhou_003/src_tz2_grade.html").read_bytes() == GRADE_SNAPSHOT
    assert (target / "snapshots/js_taizhou_003/src_tz2_contact.html").read_bytes() == SNAPSHOT


def test_atomic_writer_rejects_snapshot_collision_without_partial_target(tmp_path):
    hospitals, sources, records, verification = _payloads()
    first = tmp_path / "first"
    second = tmp_path / "second"
    relative = Path("js_taizhou_003/src_tz2_contact.html")
    (first / relative).parent.mkdir(parents=True)
    (second / relative).parent.mkdir(parents=True)
    (first / relative).write_bytes(SNAPSHOT)
    (second / relative).write_bytes(b"different bytes")
    target = tmp_path / "published"

    with pytest.raises(ValueError, match="snapshot collision"):
        write_corpus(
            target,
            hospitals=hospitals,
            sources=sources,
            records=records,
            verification=verification,
            built_at=NOW,
            snapshot_roots=(first, second),
        )

    assert not target.exists()


def test_atomic_writer_rejects_ambiguous_snapshot_root_arguments(tmp_path):
    hospitals, sources, records, verification = _payloads()
    first = _write_snapshot_source(tmp_path / "first")
    second = _write_snapshot_source(tmp_path / "second")
    target = tmp_path / "published"

    with pytest.raises(ValueError, match="snapshot_root and snapshot_roots"):
        write_corpus(
            target,
            hospitals=hospitals,
            sources=sources,
            records=records,
            verification=verification,
            built_at=NOW,
            snapshot_root=first,
            snapshot_roots=(second,),
        )

    assert not target.exists()


def test_official_appointment_platform_cannot_authorize_identity_evidence(tmp_path):
    hospitals, sources, records, verification = _payloads()
    sources[0]["url"] = "https://js.guahaoe.com/hospital/desc/example"
    sources[0]["source_tier"] = "official_appointment"
    target = tmp_path / "published"
    with pytest.raises(ValueError, match="not authoritative"):
        write_corpus(
            target,
            hospitals=hospitals,
            sources=sources,
            records=records,
            verification=verification,
            built_at=NOW,
            snapshot_root=_write_snapshot_source(tmp_path / "snapshot-source"),
        )
    assert not target.exists()


def test_unapproved_appointment_platform_is_rejected(tmp_path):
    hospitals, sources, records, verification = _payloads()
    sources[0]["url"] = "https://appointments.example.com/hospital/example"
    sources[0]["source_tier"] = "official_appointment"
    target = tmp_path / "published"
    with pytest.raises(ValueError, match="unapproved source host"):
        write_corpus(
            target,
            hospitals=hospitals,
            sources=sources,
            records=records,
            verification=verification,
            built_at=NOW,
            snapshot_root=_write_snapshot_source(tmp_path / "snapshot-source"),
        )
    assert not target.exists()


def test_official_appointment_tier_cannot_authorize_contact_on_hospital_domain(tmp_path):
    hospitals, sources, records, verification = _payloads()
    sources[1]["source_tier"] = "official_appointment"
    target = tmp_path / "published"
    with pytest.raises(ValueError, match="unapproved source host|appointment platform"):
        write_corpus(
            target,
            hospitals=hospitals,
            sources=sources,
            records=records,
            verification=verification,
            built_at=NOW,
            snapshot_root=_write_snapshot_source(tmp_path / "snapshot-source"),
        )
    assert not target.exists()


def test_identity_overview_can_evidence_its_hospital_campus_address(tmp_path):
    hospitals, sources, records, verification = _payloads()
    hospitals[0]["campuses"][0]["address_source_id"] = "src_tz2_grade"
    target = tmp_path / "published"
    corpus = write_corpus(
        target,
        hospitals=hospitals,
        sources=sources,
        records=records,
        verification=verification,
        built_at=NOW,
        snapshot_root=_write_snapshot_source(tmp_path / "snapshot-source"),
    )
    assert corpus.validate(today=NOW.date()).errors == ()


def test_verified_contact_source_can_publish_a_campus_record(tmp_path):
    hospitals, sources, records, verification = _payloads()
    records[0]["category"] = "campus"
    target = tmp_path / "published"
    corpus = write_corpus(
        target,
        hospitals=hospitals,
        sources=sources,
        records=records,
        verification=verification,
        built_at=NOW,
        snapshot_root=_write_snapshot_source(tmp_path / "snapshot-source"),
    )
    assert corpus.validate(today=NOW.date()).errors == ()


def test_atomic_writer_rejects_snapshot_source_symlink(tmp_path):
    hospitals, sources, records, verification = _payloads()
    snapshot_root = tmp_path / "snapshot-source"
    hospital_dir = snapshot_root / "js_taizhou_003"
    hospital_dir.mkdir(parents=True)
    (hospital_dir / "src_tz2_grade.html").write_bytes(GRADE_SNAPSHOT)
    outside = tmp_path / "outside-contact.html"
    outside.write_bytes(SNAPSHOT)
    try:
        (hospital_dir / "src_tz2_contact.html").symlink_to(outside)
    except OSError as exc:
        pytest.skip(f"Symlink creation is unavailable: {exc}")

    target = tmp_path / "published"
    with pytest.raises(ValueError, match="snapshot source contains a symlink"):
        write_corpus(target, hospitals=hospitals, sources=sources, records=records,
                     verification=verification, built_at=NOW, snapshot_root=snapshot_root)
    assert not target.exists()


def test_atomic_writer_rejects_symlink_swapped_in_after_source_check(tmp_path, monkeypatch):
    hospitals, sources, records, verification = _payloads()
    snapshot_root = tmp_path / "snapshot-source"
    hospital_dir = snapshot_root / "js_taizhou_003"
    hospital_dir.mkdir(parents=True)
    (hospital_dir / "src_tz2_grade.html").write_bytes(GRADE_SNAPSHOT)
    contact = hospital_dir / "src_tz2_contact.html"
    contact.write_bytes(SNAPSHOT)
    outside = tmp_path / "outside-contact.html"
    outside.write_bytes(b"must not be ingested")
    original_check = corpus_store._assert_snapshot_tree_safe
    checks = 0

    def check_then_swap(path):
        nonlocal checks
        checks += 1
        original_check(path)
        if checks == 1:
            contact.unlink()
            try:
                contact.symlink_to(outside)
            except OSError as exc:
                pytest.skip(f"Symlink creation is unavailable: {exc}")

    monkeypatch.setattr(corpus_store, "_assert_snapshot_tree_safe", check_then_swap)
    target = tmp_path / "published"
    with pytest.raises(ValueError, match="snapshot source contains a symlink"):
        write_corpus(target, hospitals=hospitals, sources=sources, records=records,
                     verification=verification, built_at=NOW, snapshot_root=snapshot_root)
    assert checks == 2
    assert not target.exists()


@pytest.mark.skipif(os.name != "nt", reason="Windows junction behavior")
def test_atomic_writer_rejects_snapshot_root_junction(tmp_path):
    hospitals, sources, records, verification = _payloads()
    real_root = tmp_path / "real-snapshot-source"
    hospital_dir = real_root / "js_taizhou_003"
    hospital_dir.mkdir(parents=True)
    (hospital_dir / "src_tz2_grade.html").write_bytes(GRADE_SNAPSHOT)
    (hospital_dir / "src_tz2_contact.html").write_bytes(SNAPSHOT)
    junction = tmp_path / "junction-snapshot-source"
    created = subprocess.run(
        ["cmd.exe", "/d", "/c", "mklink", "/J", str(junction), str(real_root)],
        capture_output=True,
        text=True,
        check=False,
    )
    if created.returncode != 0:
        pytest.skip("Junction creation is unavailable")

    target = tmp_path / "published"
    with pytest.raises(ValueError, match="symlink or junction"):
        write_corpus(target, hospitals=hospitals, sources=sources, records=records,
                     verification=verification, built_at=NOW, snapshot_root=junction)
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
        "source_count": 2,
        "status": "valid",
        "warnings": [],
    }
    assert "主院区地址与预约咨询" not in output


def test_validation_cli_handles_non_list_top_level_json(tmp_path, capsys):
    root = _write_fixture(tmp_path / "corpus")
    _dump(root / "records.json", None)
    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    manifest["files"]["records.json"] = hashlib.sha256((root / "records.json").read_bytes()).hexdigest()
    _dump(root / "manifest.json", manifest)

    from scripts.validate_jiangsu_hospital_corpus import main

    assert main(["--corpus-dir", str(root), "--today", "2026-11-04"]) == 1
    assert json.loads(capsys.readouterr().out) == {
        "error_type": "corpus_load_failed", "status": "invalid"
    }
