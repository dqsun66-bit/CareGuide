"""Deterministic BM25 evaluation for the reviewed hospital corpus."""
from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
from typing import Mapping, Sequence

from careguide.retrieval.bm25_index import BM25Index

from .models import ReviewStatus
from .store import HospitalCorpus


@dataclass(frozen=True)
class CaseResult:
    case_id: str
    retrieved_ids: tuple[str, ...]
    relevant_ids: tuple[str, ...]
    reciprocal_rank: float
    hard_negative: bool


@dataclass(frozen=True)
class EvaluationReport:
    recall_at_1: float
    recall_at_3: float
    recall_at_5: float
    mrr: float
    hard_negative_accuracy: float
    scope_violations: int
    cases: tuple[CaseResult, ...]


def load_cases(path: Path | str) -> tuple[dict, ...]:
    """Load a reviewed JSONL case set and reject duplicate or malformed entries."""
    values: list[dict] = []
    seen: set[str] = set()
    for line_number, line in enumerate(Path(path).read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        value = json.loads(line)
        if not isinstance(value, dict):
            raise ValueError(f"case line {line_number} must contain an object")
        case_id = value.get("case_id")
        if not isinstance(case_id, str) or not case_id.strip() or case_id in seen:
            raise ValueError(f"case line {line_number} has an invalid or duplicate case_id")
        seen.add(case_id)
        values.append(value)
    if not values:
        raise ValueError("case file is empty")
    return tuple(values)


def _retrieval_text(corpus: HospitalCorpus, record) -> str:
    hospital = next(item for item in corpus.hospitals if item.hospital_id == record.hospital_id)
    campus = next(item for item in hospital.campuses if item.campus_id == record.campus_id)
    return " ".join((
        hospital.official_name,
        *hospital.aliases,
        campus.official_name,
        campus.address,
        record.category.value,
        record.title,
        record.content,
    ))


def _recall(results: Sequence[CaseResult], cutoff: int) -> float:
    positives = [result for result in results if not result.hard_negative]
    if not positives:
        return 0.0
    recalls = (
        len(set(result.retrieved_ids[:cutoff]) & set(result.relevant_ids))
        / len(set(result.relevant_ids))
        for result in positives
    )
    return sum(recalls) / len(positives)


def evaluate_cases(
    corpus: HospitalCorpus,
    cases: Sequence[Mapping[str, object]],
    *,
    top_k: int = 5,
) -> EvaluationReport:
    """Evaluate reviewed cases after strict hospital/category eligibility filtering."""
    if top_k < 1:
        raise ValueError("top_k must be positive")
    records_by_id = {record.document_id: record for record in corpus.records}
    index = BM25Index()
    index.upsert({
        "chunk_id": record.document_id,
        "content": record.content,
        "retrieval_text": _retrieval_text(corpus, record),
        "metadata": {
            "hospital_id": record.hospital_id,
            "category": record.category.value,
        },
    } for record in corpus.records if record.review_status == ReviewStatus.VERIFIED_OFFICIAL)

    results: list[CaseResult] = []
    scope_violations = 0
    seen_case_ids: set[str] = set()
    for value in cases:
        case_id = str(value.get("case_id", "")).strip()
        query = str(value.get("query", "")).strip()
        hospital_id = str(value.get("hospital_id", "")).strip()
        category = str(value.get("category", "")).strip()
        hard_negative = bool(value.get("hard_negative", False))
        raw_relevant = value.get("relevant_ids", [])
        if not case_id or case_id in seen_case_ids:
            raise ValueError("case_id must be non-empty and unique")
        seen_case_ids.add(case_id)
        if not query or not hospital_id or not category:
            raise ValueError(f"case {case_id}: query, hospital_id and category are required")
        if not isinstance(raw_relevant, (list, tuple)):
            raise ValueError(f"case {case_id}: relevant_ids must be a list")
        relevant_ids = tuple(str(item) for item in raw_relevant)
        unknown = set(relevant_ids) - set(records_by_id)
        if unknown:
            raise ValueError(f"case {case_id}: unknown relevant document: {sorted(unknown)}")
        if hard_negative and relevant_ids:
            raise ValueError(f"case {case_id}: hard negative cannot declare relevant documents")
        if not hard_negative and not relevant_ids:
            raise ValueError(f"case {case_id}: positive case requires relevant documents")
        if any(
            records_by_id[document_id].hospital_id != hospital_id
            or records_by_id[document_id].category.value != category
            for document_id in relevant_ids
        ):
            raise ValueError(f"case {case_id}: relevant document is outside declared scope")

        eligible = {
            record.document_id
            for record in corpus.records
            if record.hospital_id == hospital_id and record.category.value == category
        }
        hits = index.search(query, top_k=top_k, eligible_ids=eligible)
        retrieved_ids = tuple(hit.chunk_id for hit in hits)
        scope_violations += sum(
            hit.metadata.get("hospital_id") != hospital_id
            or hit.metadata.get("category") != category
            for hit in hits
        )
        reciprocal_rank = 0.0
        for rank, document_id in enumerate(retrieved_ids, 1):
            if document_id in relevant_ids:
                reciprocal_rank = 1.0 / rank
                break
        results.append(CaseResult(
            case_id=case_id,
            retrieved_ids=retrieved_ids,
            relevant_ids=relevant_ids,
            reciprocal_rank=reciprocal_rank,
            hard_negative=hard_negative,
        ))

    positives = [result for result in results if not result.hard_negative]
    negatives = [result for result in results if result.hard_negative]
    return EvaluationReport(
        recall_at_1=_recall(results, 1),
        recall_at_3=_recall(results, 3),
        recall_at_5=_recall(results, 5),
        mrr=(sum(result.reciprocal_rank for result in positives) / len(positives)
             if positives else 0.0),
        hard_negative_accuracy=(
            sum(not result.retrieved_ids for result in negatives) / len(negatives)
            if negatives else 0.0
        ),
        scope_violations=scope_violations,
        cases=tuple(results),
    )
