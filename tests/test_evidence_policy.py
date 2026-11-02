from careguide.retrieval.evidence_policy import accept_evidence


def evidence(*, review_status="developer_source_verified", representation="curated_factual_summary",
             retrieval=None):
    return {
        "representation": representation,
        "citation": {
            "source_id": "source-1",
            "url": "https://example.invalid/source",
            "review_status": review_status,
        },
        "retrieval": retrieval or {},
    }


def test_open_dataset_cannot_alone_support_high_risk_guidance():
    item = evidence(
        review_status="open_dataset_unreviewed",
        representation="open_dataset_qa",
        retrieval={"rerank_score": 0.91},
    )

    accepted, reason = accept_evidence(item, risk="urgent_triage")

    assert accepted is False
    assert reason == "open_dataset_high_risk"


def test_open_dataset_requires_reranking_even_for_general_guidance():
    item = evidence(
        review_status="open_dataset_unreviewed",
        representation="open_dataset_qa",
        retrieval={"mode": "bm25", "bm25_score": 4.2},
    )

    assert accept_evidence(item, risk="general_guidance") == (
        False, "open_dataset_requires_rerank")


def test_low_rerank_score_is_rejected():
    item = evidence(retrieval={"rerank_score": 0.34})

    assert accept_evidence(item, risk="general_guidance") == (
        False, "below_rerank_threshold")


def test_verified_source_with_sufficient_score_is_accepted():
    item = evidence(retrieval={"rerank_score": 0.75})

    assert accept_evidence(item, risk="general_guidance") == (True, "accepted")


def test_missing_source_identity_is_rejected():
    item = evidence(retrieval={"rerank_score": 0.75})
    item["citation"]["source_id"] = ""

    assert accept_evidence(item, risk="general_guidance") == (
        False, "missing_source_identity")
