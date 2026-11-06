from __future__ import annotations

import importlib
import importlib.util
import hashlib
import json
from pathlib import Path

import pytest

from careguide.hospital_corpus.store import HospitalCorpus


ROOT = Path(__file__).resolve().parents[1]
CASES = ROOT / "data" / "eval" / "jiangsu_hospitals_v1" / "retrieval_cases.jsonl"


@pytest.fixture(scope="module")
def pilot_corpus() -> HospitalCorpus:
    return HospitalCorpus.load(ROOT / "data" / "jiangsu_hospitals_v1")


def _evaluation_module():
    module_name = "careguide.hospital_corpus.evaluation"
    assert importlib.util.find_spec(module_name) is not None, "evaluation module is missing"
    return importlib.import_module(module_name)


def _cli_module():
    module_name = "scripts.evaluate_jiangsu_hospital_bm25"
    assert importlib.util.find_spec(module_name) is not None, "evaluation CLI is missing"
    return importlib.import_module(module_name)


def test_alias_query_retrieves_taizhou_second_people_hospital(pilot_corpus):
    report = _evaluation_module().evaluate_cases(pilot_corpus, [{
        "case_id": "tz2-alias-appointment",
        "query": "姜堰市人民医院怎么预约挂号",
        "relevant_ids": ["js_taizhou_003_appointment_0009"],
        "hospital_id": "js_taizhou_003",
        "category": "appointment",
    }], top_k=5)
    assert report.recall_at_5 == 1.0
    assert report.mrr == 1.0
    assert report.scope_violations == 0


def test_unknown_parking_rule_returns_no_official_evidence(pilot_corpus):
    report = _evaluation_module().evaluate_cases(pilot_corpus, [{
        "case_id": "tz2-live-parking-negative",
        "query": "泰州二院现在还有几个空车位",
        "relevant_ids": [],
        "hard_negative": True,
        "hospital_id": "js_taizhou_003",
        "category": "parking",
    }], top_k=5)
    assert report.hard_negative_accuracy == 1.0
    assert report.cases[0].retrieved_ids == ()
    assert report.scope_violations == 0


def test_scoped_positive_cases_report_recall_and_rank(pilot_corpus):
    report = _evaluation_module().evaluate_cases(pilot_corpus, [
        {
            "case_id": "tz1-campus",
            "query": "南京医科大学附属泰州人民医院总院地址在哪里",
            "relevant_ids": ["js_taizhou_001_campus_0002"],
            "hospital_id": "js_taizhou_001",
            "category": "campus",
        },
        {
            "case_id": "tcm-east-appointment",
            "query": "泰州市中医院东院怎么预约",
            "relevant_ids": ["js_taizhou_002_appointment_0006"],
            "hospital_id": "js_taizhou_002",
            "category": "appointment",
        },
        {
            "case_id": "tz2-contact",
            "query": "泰州市第二人民医院地址和预约咨询渠道",
            "relevant_ids": ["js_taizhou_003_contact_0008"],
            "hospital_id": "js_taizhou_003",
            "category": "contact",
        },
    ], top_k=5)
    assert report.recall_at_1 == 1.0
    assert report.recall_at_3 == 1.0
    assert report.recall_at_5 == 1.0
    assert report.mrr == 1.0
    assert report.scope_violations == 0


def test_recall_averages_fraction_of_all_relevant_documents():
    module = _evaluation_module()
    result = module.CaseResult(
        case_id="multi-relevant",
        retrieved_ids=("doc-a",),
        relevant_ids=("doc-a", "doc-b"),
        reciprocal_rank=1.0,
        hard_negative=False,
    )
    assert module._recall([result], 1) == 0.5


def test_evaluator_rejects_unknown_relevant_document(pilot_corpus):
    with pytest.raises(ValueError, match="unknown relevant document"):
        _evaluation_module().evaluate_cases(pilot_corpus, [{
            "case_id": "bad-id",
            "query": "预约",
            "relevant_ids": ["missing-document"],
            "hospital_id": "js_taizhou_003",
            "category": "appointment",
        }], top_k=5)


def test_evaluator_rejects_relevant_document_outside_declared_scope(pilot_corpus):
    with pytest.raises(ValueError, match="outside declared scope"):
        _evaluation_module().evaluate_cases(pilot_corpus, [{
            "case_id": "wrong-scope",
            "query": "预约",
            "relevant_ids": ["js_taizhou_001_appointment_0003"],
            "hospital_id": "js_taizhou_003",
            "category": "appointment",
        }], top_k=5)


def test_reviewed_case_file_has_balanced_pilot_coverage():
    cases = _evaluation_module().load_cases(CASES)
    assert len(cases) >= 15
    counts = {
        hospital_id: sum(case["hospital_id"] == hospital_id for case in cases)
        for hospital_id in {"js_taizhou_001", "js_taizhou_002", "js_taizhou_003"}
    }
    assert all(count >= 5 for count in counts.values())
    for hospital_id in counts:
        hospital_cases = [case for case in cases if case["hospital_id"] == hospital_id]
        assert any(case.get("hard_negative") is True for case in hospital_cases)
        assert any("alias" in case["case_id"] for case in hospital_cases)


def test_frozen_bm25_cases_meet_pilot_thresholds(pilot_corpus):
    cases = _evaluation_module().load_cases(CASES)
    report = _evaluation_module().evaluate_cases(pilot_corpus, cases, top_k=5)
    assert report.recall_at_5 >= 0.90
    assert report.mrr >= 0.75
    assert report.hard_negative_accuracy == 1.0
    assert report.scope_violations == 0


def test_cli_writes_reproducible_baseline_report(tmp_path):
    output = tmp_path / "baseline.json"
    assert _cli_module().main([
        "--corpus-dir", str(ROOT / "data" / "jiangsu_hospitals_v1"),
        "--cases", str(CASES),
        "--output", str(output),
    ]) == 0
    payload = json.loads(output.read_text(encoding="utf-8"))
    assert payload["case_count"] == 18
    assert payload["recall_at_5"] >= 0.90
    assert payload["mrr"] >= 0.75
    assert payload["hard_negative_accuracy"] == 1.0
    assert payload["scope_violations"] == 0
    assert len(payload["cases"]) == 18


def test_case_file_hash_is_frozen_in_readme():
    readme = CASES.with_name("README.md").read_text(encoding="utf-8")
    digest = hashlib.sha256(CASES.read_bytes()).hexdigest()
    assert digest in readme
