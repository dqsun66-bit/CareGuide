"""Safety policy for deciding whether retrieved medical evidence may be used."""
from __future__ import annotations


HIGH_RISK_USES = frozenset({"urgent_triage", "diagnosis", "medication", "pregnancy"})


def accept_evidence(
    item: dict,
    *,
    risk: str,
    min_rerank_score: float = 0.35,
) -> tuple[bool, str]:
    citation = item.get("citation") or {}
    retrieval = item.get("retrieval") or {}
    if not citation.get("source_id") or not citation.get("url"):
        return False, "missing_source_identity"

    is_open = (
        item.get("representation") == "open_dataset_qa"
        or citation.get("review_status") == "open_dataset_unreviewed"
    )
    if is_open and risk in HIGH_RISK_USES:
        return False, "open_dataset_high_risk"

    rerank_score = retrieval.get("rerank_score")
    # The legacy/raw retrieval API is allowed to expose candidates for offline
    # evaluation. User-facing medical guidance must call search_with_report
    # with an explicit risk and therefore cannot bypass reranking.
    if is_open and risk != "raw_retrieval" and rerank_score is None:
        return False, "open_dataset_requires_rerank"
    if rerank_score is not None and float(rerank_score) < min_rerank_score:
        return False, "below_rerank_threshold"

    if rerank_score is None:
        matched_terms = retrieval.get("matched_terms")
        bm25_score = retrieval.get("bm25_score")
        cosine_score = retrieval.get("cosine_score")
        if matched_terms is not None and int(matched_terms) < 1:
            return False, "no_lexical_overlap"
        if bm25_score is not None and float(bm25_score) <= 0:
            return False, "non_positive_bm25_score"
        if (retrieval.get("mode") == "dense" and cosine_score is not None
                and float(cosine_score) < 0):
            return False, "negative_dense_score"
    return True, "accepted"
