from __future__ import annotations

from collections import Counter
import hashlib
from pathlib import Path

from careguide.hospital_corpus.evaluation import evaluate_cases, load_cases
from careguide.hospital_corpus.store import HospitalCorpus
from scripts import evaluate_jiangsu_hospital_bm25 as command


ROOT = Path(__file__).resolve().parents[1]
CORPUS_DIR = ROOT / "data/jiangsu_hospitals_v2"
EVAL_DIR = ROOT / "data/eval/jiangsu_hospitals_v2"
CASES = EVAL_DIR / "retrieval_cases.jsonl"
BASELINE = EVAL_DIR / "bm25_baseline.json"


def test_v2_case_set_has_balanced_reviewed_coverage():
    corpus = HospitalCorpus.load(CORPUS_DIR)
    cases = load_cases(CASES)
    assert len(cases) >= 45
    counts = Counter(case["hospital_id"] for case in cases)
    assert counts == {hospital.hospital_id: 3 for hospital in corpus.hospitals}

    records = {record.document_id: record for record in corpus.records}
    for hospital in corpus.hospitals:
        hospital_cases = [
            case for case in cases if case["hospital_id"] == hospital.hospital_id
        ]
        assert any(case.get("hard_negative") is True for case in hospital_cases)
        assert sum(not case.get("hard_negative", False) for case in hospital_cases) == 2
        for case in hospital_cases:
            for document_id in case["relevant_ids"]:
                record = records[document_id]
                assert record.hospital_id == case["hospital_id"]
                assert record.category.value == case["category"]


def test_v2_cases_meet_retrieval_acceptance_thresholds():
    corpus = HospitalCorpus.load(CORPUS_DIR)
    report = evaluate_cases(corpus, load_cases(CASES), top_k=5)
    assert report.recall_at_5 >= 0.90
    assert report.mrr >= 0.75
    assert report.hard_negative_accuracy == 1.0
    assert report.scope_violations == 0


def test_v2_case_hash_is_frozen_in_readme():
    digest = hashlib.sha256(CASES.read_bytes()).hexdigest()
    assert digest in (EVAL_DIR / "README.md").read_text(encoding="utf-8")


def test_v2_baseline_regenerates_byte_for_byte(tmp_path):
    output = tmp_path / "bm25_baseline.json"
    assert command.main([
        "--corpus-dir", str(CORPUS_DIR),
        "--cases", str(CASES),
        "--output", str(output),
    ]) == 0
    assert output.read_bytes() == BASELINE.read_bytes()
