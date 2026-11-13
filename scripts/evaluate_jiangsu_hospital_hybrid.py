"""Evaluate the frozen Jiangsu hospital corpus with Huawei hybrid retrieval."""
from __future__ import annotations

import argparse
from datetime import date
import hashlib
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import chromadb

from careguide.hospital_corpus.evaluation import load_cases
from careguide.hospital_corpus.retrieval import (
    HospitalKnowledgeBase,
    hospital_collection_name,
)
from careguide.hospital_corpus.store import HospitalCorpus
from careguide.runtime import semantic_embedding, semantic_reranker
from scripts.private_config import load_config


def _recall(results, cutoff):
    positives = [item for item in results if not item["hard_negative"]]
    return sum(
        len(set(item["retrieved_ids"][:cutoff]) & set(item["relevant_ids"]))
        / len(set(item["relevant_ids"]))
        for item in positives
    ) / len(positives)


def evaluate(knowledge, corpus, cases):
    records = {record.document_id: record for record in corpus.records}
    hospitals = {hospital.hospital_id: hospital for hospital in corpus.hospitals}
    outcomes = []
    scope_violations = 0
    for case in cases:
        relevant = tuple(case["relevant_ids"])
        hospital = hospitals[case["hospital_id"]]
        campus_id = (
            records[relevant[0]].campus_id if relevant
            else hospital.campuses[0].campus_id
        )
        result = knowledge.search_with_report(
            case["query"],
            case["hospital_id"],
            campus=campus_id,
            category=case["category"],
            top_k=5,
        )
        retrieved = tuple(item["id"] for item in result.evidence)
        scope_violations += sum(
            item["scope"]["hospital_id"] != case["hospital_id"]
            or item["scope"]["campus_id"] != campus_id
            or item["scope"]["category"] != case["category"]
            or item["source"]["review_status"] != "verified_official"
            for item in result.evidence
        )
        reciprocal_rank = next((
            1.0 / rank for rank, identity in enumerate(retrieved, 1)
            if identity in relevant
        ), 0.0)
        outcomes.append({
            "case_id": case["case_id"],
            "hard_negative": bool(case.get("hard_negative", False)),
            "relevant_ids": list(relevant),
            "retrieved_ids": list(retrieved),
            "reciprocal_rank": reciprocal_rank,
            "retrieval": {
                "lexical_count": result.report.lexical_count,
                "dense_count": result.report.dense_count,
                "fused_count": result.report.fused_count,
                "reranked": result.report.reranked,
                "filtered_count": result.report.filtered_count,
            },
        })
    positives = [item for item in outcomes if not item["hard_negative"]]
    negatives = [item for item in outcomes if item["hard_negative"]]
    return {
        "recall_at_1": _recall(outcomes, 1),
        "recall_at_3": _recall(outcomes, 3),
        "recall_at_5": _recall(outcomes, 5),
        "mrr": sum(item["reciprocal_rank"] for item in positives) / len(positives),
        "hard_negative_accuracy": (
            sum(not item["retrieved_ids"] for item in negatives) / len(negatives)
        ),
        "scope_violations": scope_violations,
        "cases": outcomes,
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config-file", required=True, type=Path)
    parser.add_argument("--corpus-dir", required=True, type=Path)
    parser.add_argument("--cases", required=True, type=Path)
    parser.add_argument("--today", required=True, type=date.fromisoformat)
    parser.add_argument("--chroma-dir", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)

    corpus = HospitalCorpus.load(args.corpus_dir)
    validation = corpus.validate(today=args.today)
    if not validation.valid:
        raise ValueError("Hospital corpus validation failed")
    cases = load_cases(args.cases)
    load_config(args.config_file)
    embedding = semantic_embedding()
    reranker = semantic_reranker()
    try:
        client = chromadb.PersistentClient(
            path=str(args.chroma_dir or Path(__file__).resolve().parents[1] / "data" / "chroma"),
            settings=chromadb.Settings(anonymized_telemetry=False),
        )
        name = hospital_collection_name(corpus)
        names = {item.name for item in client.list_collections()}
        if name not in names:
            raise ValueError("hospital_hybrid_index_unavailable")
        knowledge = HospitalKnowledgeBase(
            corpus,
            collection=client.get_collection(name, embedding_function=None),
            embedding=embedding,
            reranker=reranker,
            min_rerank_score=0.35,
            hybrid_candidates=20,
        )
        report = evaluate(knowledge, corpus, cases)
        payload = {
            "corpus_version": corpus.manifest.version,
            "corpus_manifest_sha256": hashlib.sha256(
                (args.corpus_dir / "manifest.json").read_bytes()).hexdigest(),
            "case_file_sha256": hashlib.sha256(args.cases.read_bytes()).hexdigest(),
            "case_count": len(cases),
            "collection": name,
            "embedding_provider": embedding.provider_name,
            "embedding_model": embedding.model,
            "rerank_provider": reranker.provider_name,
            "rerank_model": reranker.model,
            **report,
        }
        passed = (
            payload["recall_at_5"] >= 0.90
            and payload["mrr"] >= 0.75
            and payload["hard_negative_accuracy"] == 1.0
            and payload["scope_violations"] == 0
        )
        payload["passed"] = passed
        rendered = json.dumps(
            payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        if passed and args.output is not None:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(rendered, encoding="utf-8", newline="\n")
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
        return 0 if passed else 1
    finally:
        embedding.close()


if __name__ == "__main__":
    raise SystemExit(main())
