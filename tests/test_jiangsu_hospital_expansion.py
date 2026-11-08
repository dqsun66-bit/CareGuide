"""Offline expansion of the frozen Taizhou pilot into a 15-hospital v2 corpus."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import html
import json
from pathlib import Path
import re
import socket

import pytest

from careguide.hospital_corpus.normalize import normalize_text
from careguide.hospital_corpus.store import DATA_FILES, HospitalCorpus
from scripts import build_jiangsu_hospital_expansion as command


CN_TZ = timezone(timedelta(hours=8))
NOW = datetime(2026, 11, 7, 12, 0, tzinfo=CN_TZ)
ROOT = Path(__file__).resolve().parents[1]
V1 = ROOT / "data/jiangsu_hospitals_v1"
V2_SOURCES = ROOT / "data/jiangsu_hospital_sources_v2"


def _dump(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _hash_tree(root: Path) -> dict[str, str]:
    files = [root / "manifest.json", *(root / name for name in DATA_FILES)]
    files.extend(path for path in (root / "snapshots").rglob("*") if path.is_file())
    return {
        path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(files)
    }


def _candidate(hospital, source, document_id: str, title: str, content: str) -> dict:
    return {
        "document_id": document_id,
        "hospital_id": hospital.hospital_id,
        "campus_id": hospital.campuses[0].campus_id,
        "city": hospital.city,
        "category": source.category.value,
        "title": title,
        "content": content,
        "source_id": source.source_id,
        "supporting_source_ids": [],
        "source_url": source.url,
        "source_tier": source.source_tier.value,
        "published_at": source.published_at.isoformat() if source.published_at else None,
        "collected_at": source.collected_at.isoformat(),
        "reviewed_at": None,
        "review_status": "needs_review",
        "content_hash": hashlib.sha256(normalize_text(content).encode("utf-8")).hexdigest(),
        "language": "zh-CN",
    }


def _frozen_evidence_term(source) -> str:
    raw = (V1 / source.snapshot_path).read_bytes()
    for encoding in ("utf-8", "gb18030"):
        try:
            decoded = raw.decode(encoding)
            break
        except UnicodeDecodeError:
            continue
    else:
        raise AssertionError(f"test fixture cannot decode {source.snapshot_path}")
    text = html.unescape(re.sub(r"<[^>]+>", " ", decoded))
    match = re.search(r"[\u4e00-\u9fff]{3,}", text)
    assert match is not None, source.source_id
    return match.group(0)


@pytest.fixture
def expansion_inputs(tmp_path):
    v1 = HospitalCorpus.load(V1)
    source_dir = tmp_path / "v2-sources"
    new_sources: list[dict] = []
    candidates: list[dict] = []
    source_reviews: list[dict] = []
    record_reviews: list[dict] = []
    source_by_id = {source.source_id: source for source in v1.sources}

    for hospital in v1.hospitals:
        if hospital.city == "泰州":
            continue
        campus = hospital.campuses[0]
        identity = source_by_id[hospital.grade_source_id]
        address = source_by_id[campus.address_source_id]
        service_id = f"src_{hospital.hospital_id.removeprefix('js_')}_service"
        raw = (
            f"<html><body>{hospital.official_name}官方就医联系页面，"
            "患者出发前应通过医院官方电话确认服务时间。</body></html>"
        ).encode("utf-8")
        snapshot_path = f"snapshots/{hospital.hospital_id}/{service_id}.html"
        snapshot = source_dir / snapshot_path
        snapshot.parent.mkdir(parents=True, exist_ok=True)
        snapshot.write_bytes(raw)
        digest = hashlib.sha256(raw).hexdigest()
        service = {
            "source_id": service_id,
            "hospital_id": hospital.hospital_id,
            "campus_id": campus.campus_id,
            "title": f"{hospital.official_name}官方就医联系",
            "url": f"https://wjw.jiangsu.gov.cn/test/{hospital.hospital_id}",
            "source_tier": "health_authority",
            "category": "contact",
            "published_at": None,
            "valid_until": "2027-11-07",
            "collected_at": NOW.isoformat(),
            "reviewed_at": None,
            "review_status": "needs_review",
            "snapshot_path": snapshot_path,
            "http_status": 200,
            "content_type": "text/html; charset=utf-8",
            "retrieved_sha256": digest,
        }
        new_sources.append(service)
        source_reviews.append({
            "source_id": service_id,
            "retrieved_sha256": digest,
            "reviewer": "合成测试复核员",
            "reviewed_at": NOW.isoformat(),
            "decision": "approved",
        })

        identity_content = (
            f"{hospital.official_name}已登记为三级甲等医院。本条合成测试记录仅用于验证"
            "离线合并、来源绑定和内容哈希，不作为真实项目中的新增医疗资料发布。"
        )
        campus_content = (
            f"{hospital.official_name}{campus.official_name}登记地址为{campus.address}。"
            "本条合成测试记录只验证院区来源绑定，不替代医院官网的实时就医通知。"
        )
        service_content = (
            f"{hospital.official_name}提供官方就医联系渠道。患者出发前应再次确认服务时间、"
            "院区安排及预约要求；本条为离线构建测试数据，不属于真实知识库内容。"
        )
        records = [
            _candidate(
                hospital,
                identity,
                f"{hospital.hospital_id}_identity_v2",
                f"{hospital.official_name}身份信息",
                identity_content,
            ),
            _candidate(
                hospital,
                address,
                f"{hospital.hospital_id}_campus_v2",
                f"{hospital.official_name}院区信息",
                campus_content,
            ),
            _candidate(
                hospital,
                type("SourceView", (), {
                    "source_id": service_id,
                    "category": type("Value", (), {"value": "contact"})(),
                    "url": service["url"],
                    "source_tier": type("Value", (), {"value": "health_authority"})(),
                    "published_at": None,
                    "collected_at": NOW,
                })(),
                f"{hospital.hospital_id}_service_v2",
                f"{hospital.official_name}就医联系",
                service_content,
            ),
        ]
        candidates.extend(records)
        for record in records:
            if record["source_id"] == service_id:
                required_term = hospital.official_name
            else:
                required_term = _frozen_evidence_term(source_by_id[record["source_id"]])
            record_reviews.append({
                "document_id": record["document_id"],
                "source_ids": [record["source_id"]],
                "content_hash": record["content_hash"],
                "required_terms": [required_term],
                "reviewer": "合成测试复核员",
                "reviewed_at": NOW.isoformat(),
                "decision": "approved",
            })

    _dump(source_dir / "sources.json", new_sources)
    _dump(source_dir / "record_candidates.json", candidates)
    _dump(source_dir / "review_manifest.json", {
        "version": "jiangsu-hospital-review-v1",
        "source_reviews": source_reviews,
        "record_reviews": record_reviews,
        "gap_reviews": [],
    })
    _dump(source_dir / "v1_integrity.json", {
        "version": "jiangsu-v1-integrity-v1",
        "files": _hash_tree(V1),
    })
    return type("ExpansionInputs", (), {"v1": V1, "v2_sources": source_dir})()


def _arguments(expansion_inputs, output: Path, *, apply: bool = False) -> list[str]:
    values = [
        "--v1-corpus", str(expansion_inputs.v1),
        "--source-dir", str(expansion_inputs.v2_sources),
        "--output-dir", str(output),
    ]
    if apply:
        values.append("--apply")
    return values


def test_expansion_builder_is_offline(monkeypatch, tmp_path, expansion_inputs, capsys):
    monkeypatch.setattr(
        socket,
        "create_connection",
        lambda *_args, **_kwargs: pytest.fail("network called"),
    )
    output = tmp_path / "v2"
    assert command.main(_arguments(expansion_inputs, output)) == 0
    dry_run = json.loads(capsys.readouterr().out)
    assert dry_run == {
        "external_calls": 0,
        "hospital_count": 15,
        "record_count": 45,
        "source_count": 41,
        "status": "dry_run",
    }
    assert not output.exists()

    assert command.main(_arguments(expansion_inputs, output, apply=True)) == 0
    corpus = HospitalCorpus.load(output)
    assert corpus.manifest.version == "jiangsu-hospitals-v2"
    assert corpus.manifest.stage == "expanded_core_15_of_15"
    assert len(corpus.hospitals) == 15
    assert len(corpus.records) == 45


def test_expansion_preserves_v1_records_and_refuses_overwrite(tmp_path, expansion_inputs):
    output = tmp_path / "v2"
    command.main(_arguments(expansion_inputs, output, apply=True))
    original = HospitalCorpus.load(expansion_inputs.v1)
    expanded = HospitalCorpus.load(output)
    expanded_records = {record.document_id: record for record in expanded.records}
    for record in original.records:
        assert expanded_records[record.document_id].content_hash == record.content_hash
        assert expanded_records[record.document_id].source_id == record.source_id
    with pytest.raises(FileExistsError, match="overwrite"):
        command.main(_arguments(expansion_inputs, output, apply=True))


def test_rejected_record_approval_leaves_no_output(tmp_path, expansion_inputs):
    manifest_path = expansion_inputs.v2_sources / "review_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["record_reviews"][0]["decision"] = "rejected"
    _dump(manifest_path, manifest)
    output = tmp_path / "v2"
    with pytest.raises(ValueError, match="invalid or stale record review"):
        command.main(_arguments(expansion_inputs, output, apply=True))
    assert not output.exists()


def test_integrity_mismatch_stops_before_output(tmp_path, expansion_inputs):
    integrity_path = expansion_inputs.v2_sources / "v1_integrity.json"
    integrity = json.loads(integrity_path.read_text(encoding="utf-8"))
    integrity["files"]["records.json"] = "0" * 64
    _dump(integrity_path, integrity)
    output = tmp_path / "v2"
    with pytest.raises(ValueError, match="v1 integrity mismatch"):
        command.main(_arguments(expansion_inputs, output, apply=True))
    assert not output.exists()


def _assert_city_batch(hospital_ids):
    sources = json.loads((V2_SOURCES / "sources.json").read_text(encoding="utf-8"))
    records = json.loads(
        (V2_SOURCES / "record_candidates.json").read_text(encoding="utf-8")
    )
    manifest = json.loads(
        (V2_SOURCES / "review_manifest.json").read_text(encoding="utf-8")
    )
    sources_by_id = {source["source_id"]: source for source in sources}
    trusted_source_ids = {
        source.source_id for source in HospitalCorpus.load(V1).sources
    }
    reviews_by_id = {
        review["document_id"]: review for review in manifest["record_reviews"]
    }
    for hospital_id in hospital_ids:
        hospital_records = [
            record for record in records if record["hospital_id"] == hospital_id
        ]
        assert {record["document_id"] for record in hospital_records} == {
            f"{hospital_id}_identity_v2",
            f"{hospital_id}_campus_v2",
            f"{hospital_id}_service_v2",
        }
        records_by_suffix = {
            record["document_id"].removeprefix(f"{hospital_id}_"): record
            for record in hospital_records
        }
        assert records_by_suffix["identity_v2"]["category"] == "identity"
        assert records_by_suffix["campus_v2"]["category"] == "campus"
        assert records_by_suffix["service_v2"]["category"] in {
            "appointment", "contact", "visit_preparation"
        }
        for record in hospital_records:
            bound_ids = [record["source_id"], *record.get("supporting_source_ids", [])]
            assert all(source_id in sources_by_id or source_id in trusted_source_ids
                       for source_id in bound_ids)
            review = reviews_by_id[record["document_id"]]
            assert set(review["source_ids"]) == set(bound_ids)
            evidence = " ".join(
                (
                    V2_SOURCES / sources_by_id[source_id]["snapshot_path"]
                ).read_bytes().decode("utf-8", errors="ignore")
                for source_id in bound_ids
                if source_id in sources_by_id
            )
            if record["document_id"].endswith("_service_v2"):
                assert all(term in evidence for term in review["required_terms"])


def test_nanjing_batch_has_three_reviewable_candidates_per_hospital():
    _assert_city_batch((
        "js_nanjing_001",
        "js_nanjing_002",
        "js_nanjing_003",
        "js_nanjing_004",
    ))


def test_suzhou_batch_has_three_reviewable_candidates_per_hospital():
    _assert_city_batch((
        "js_suzhou_001",
        "js_suzhou_002",
        "js_suzhou_003",
    ))


def test_wuxi_batch_has_three_reviewable_candidates_per_hospital():
    _assert_city_batch(("js_wuxi_001", "js_wuxi_002"))
