"""Validate CareGuide's Huawei hybrid medical RAG without implicit paid calls."""
from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
from datetime import date
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from careguide.corpus import DocumentKind
from careguide.medical_triage import evaluate_headache_triage
from careguide.retrieval.knowledge_base import versioned_collection_name


CASES = (
    "我头有点疼",
    "今天上午头痛4分，没有发烧、呕吐、肢体无力，也没有受伤",
    "突然出现一生中最严重的头痛，几秒达到高峰",
    "请判断是否紧急并告诉我挂什么科",
)


@dataclass(frozen=True)
class ValidationResult:
    status: str
    external_calls: int
    cases: int
    corpus_records: int
    collection: str
    retrieval_mode: str | None = None
    reranked: bool | None = None
    final_evidence: int | None = None


def run_validation(*, config_file: str | None, apply: bool,
                   corpus_dir: str | Path) -> ValidationResult:
    from careguide.runtime import load_runtime_corpus
    corpus = load_runtime_corpus(Path(corpus_dir))
    collection = versioned_collection_name(corpus)
    if not apply:
        return ValidationResult(
            status="dry_run", external_calls=0, cases=len(CASES),
            corpus_records=len(corpus.records), collection=collection)
    if not config_file:
        raise ValueError("--config-file is required with --apply")

    from scripts.private_config import load_config
    load_config(config_file)
    from careguide.runtime import build_retrieval_knowledge

    knowledge, runtime = build_retrieval_knowledge(corpus)
    if runtime.active_mode != "hybrid":
        raise RuntimeError("hybrid_mode_not_active")
    decisions = [evaluate_headache_triage(case, []) for case in CASES]
    if decisions[0].status != "needs_clarification":
        raise RuntimeError("incomplete_headache_not_clarified")
    if decisions[2].status != "emergency" or decisions[2].run_rag:
        raise RuntimeError("urgent_case_did_not_preempt_rag")
    if decisions[3].status != "needs_clarification":
        raise RuntimeError("generic_triage_not_clarified")

    candidates = [record for record in corpus.records
                  if record.kind == DocumentKind.CARE_GUIDANCE]
    result = knowledge.search_with_report(
        CASES[1], candidates, today=date.today(), top_k=5,
        risk="general_guidance")
    if not result.report.reranked:
        raise RuntimeError("rerank_not_used")
    unrelated = ("孕酮", "保胎", "hcg", "HCG")
    if any(any(term in item.get("content", "") for term in unrelated)
           for item in result.evidence):
        raise RuntimeError("unrelated_pregnancy_evidence_survived")
    return ValidationResult(
        status="passed", external_calls=2, cases=len(CASES),
        corpus_records=len(corpus.records), collection=collection,
        retrieval_mode=result.report.mode, reranked=result.report.reranked,
        final_evidence=result.report.final_count)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true")
    mode.add_argument("--apply", action="store_true")
    parser.add_argument("--config-file")
    parser.add_argument("--corpus-dir", default="data/careguide_kuake_rag_v1")
    args = parser.parse_args(argv)
    result = run_validation(
        config_file=args.config_file, apply=args.apply,
        corpus_dir=args.corpus_dir)
    print(json.dumps(asdict(result), ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
