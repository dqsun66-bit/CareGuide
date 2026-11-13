from datetime import date
from pathlib import Path

import pytest

from careguide.hospital_corpus.retrieval import (
    HospitalKnowledgeBase,
    corpus_fingerprint,
    hospital_collection_name,
    hospital_evidence,
    hospital_retrieval_text,
    index_hospital_collection,
)
from careguide.hospital_corpus.models import Category
from careguide.hospital_corpus.store import HospitalCorpus


ROOT = Path(__file__).resolve().parents[1]
CORPUS = ROOT / "data" / "jiangsu_hospitals_v2"


class TestEmbedding:
    model = "test-bge-m3"
    dimensions = 4
    provider_name = "test-provider"

    def embed_documents(self, texts):
        return [[1.0, 0.0, 0.0, 0.0] for _ in texts]

    def embed_query(self, query):
        return [1.0, 0.0, 0.0, 0.0]


class TestReranker:
    model = "test-bge-reranker-v2-m3"
    candidate_limit = 20

    def rerank(self, query, candidates, *, top_n):
        assert len(candidates) <= 20
        ordered = sorted(
            candidates,
            key=lambda item: ("一号管三天" not in item[1], item[0]),
        )[:top_n]
        return (
            [identity for identity, _ in ordered],
            [0.9 - index * 0.01 for index in range(len(ordered))],
            {},
        )


def test_projection_contains_scope_and_official_source():
    corpus = HospitalCorpus.load(CORPUS)
    record = corpus.records[0]
    text = hospital_retrieval_text(corpus, record)
    evidence = hospital_evidence(corpus, record)
    hospital = next(
        item for item in corpus.hospitals
        if item.hospital_id == record.hospital_id
    )
    campus = next(
        item for item in hospital.campuses
        if item.campus_id == record.campus_id
    )

    assert hospital.official_name in text
    assert campus.official_name in text
    assert record.content in text
    assert evidence["scope"] == {
        "hospital_id": hospital.hospital_id,
        "hospital": hospital.official_name,
        "city": hospital.city,
        "campus_id": campus.campus_id,
        "campus": campus.official_name,
        "category": record.category.value,
    }
    assert evidence["source"]["url"] == record.source_url
    assert evidence["source"]["review_status"] == "verified_official"


def test_collection_name_is_manifest_bound():
    corpus = HospitalCorpus.load(CORPUS)

    assert len(corpus_fingerprint(corpus)) == 64
    name = hospital_collection_name(corpus)
    assert name.startswith("careguide_jiangsu_hospitals_v2_")
    assert len(name.rsplit("_", 1)[1]) == 12


def test_index_contains_exact_reviewed_records_and_scope_metadata(tmp_path):
    chromadb = pytest.importorskip("chromadb")
    corpus = HospitalCorpus.load(CORPUS)
    client = chromadb.PersistentClient(path=str(tmp_path))

    collection = index_hospital_collection(
        client,
        corpus,
        TestEmbedding(),
        today=date(2026, 11, 13),
    )

    assert collection.name == hospital_collection_name(corpus)
    assert collection.count() == 45
    assert collection.metadata["corpus_fingerprint"] == corpus_fingerprint(corpus)
    assert collection.metadata["corpus_version"] == "jiangsu-hospitals-v2"
    assert collection.metadata["embedding_model"] == "test-bge-m3"
    assert collection.metadata["embedding_dimensions"] == 4
    assert collection.metadata["hnsw:space"] == "cosine"
    rows = collection.get(include=["metadatas"])
    assert {row["review_status"] for row in rows["metadatas"]} == {
        "verified_official"
    }
    assert {row["hospital_id"] for row in rows["metadatas"]} == {
        hospital.hospital_id for hospital in corpus.hospitals
    }
    assert all(
        row["campus_id"] and row["category"] and row["source_url"]
        for row in rows["metadatas"]
    )


def test_index_refuses_existing_collection(tmp_path):
    chromadb = pytest.importorskip("chromadb")
    corpus = HospitalCorpus.load(CORPUS)
    client = chromadb.PersistentClient(path=str(tmp_path))
    embedding = TestEmbedding()
    index_hospital_collection(
        client, corpus, embedding, today=date(2026, 11, 13))

    with pytest.raises(ValueError, match="already exists"):
        index_hospital_collection(
            client, corpus, embedding, today=date(2026, 11, 13))


def test_hybrid_search_is_scope_first_and_reranked(tmp_path):
    chromadb = pytest.importorskip("chromadb")
    corpus = HospitalCorpus.load(CORPUS)
    client = chromadb.PersistentClient(path=str(tmp_path))
    embedding = TestEmbedding()
    collection = index_hospital_collection(
        client, corpus, embedding, today=date(2026, 11, 13))
    knowledge = HospitalKnowledgeBase(
        corpus,
        collection=collection,
        embedding=embedding,
        reranker=TestReranker(),
    )

    result = knowledge.search_with_report(
        "怎么挂号",
        "js_taizhou_001",
        campus="js_taizhou_001_main",
        category=Category.APPOINTMENT,
        top_k=2,
    )

    assert result.report.lexical_count > 0
    assert result.report.dense_count > 0
    assert result.report.fused_count <= 20
    assert result.report.reranked is True
    assert result.report.final_count <= 2
    assert result.evidence
    assert {
        item["scope"]["hospital_id"] for item in result.evidence
    } == {"js_taizhou_001"}
    assert {
        item["scope"]["campus_id"] for item in result.evidence
    } == {"js_taizhou_001_main"}
    assert {
        item["scope"]["category"] for item in result.evidence
    } == {"appointment"}
    assert all(item["retrieval"]["rerank_score"] >= 0.35
               for item in result.evidence)


def test_hospital_resolution_accepts_id_official_name_and_alias(tmp_path):
    chromadb = pytest.importorskip("chromadb")
    corpus = HospitalCorpus.load(CORPUS)
    client = chromadb.PersistentClient(path=str(tmp_path))
    embedding = TestEmbedding()
    collection = index_hospital_collection(
        client, corpus, embedding, today=date(2026, 11, 13))
    knowledge = HospitalKnowledgeBase(
        corpus, collection=collection, embedding=embedding)

    assert knowledge.resolve_hospital("js_taizhou_003").hospital_id == "js_taizhou_003"
    assert knowledge.resolve_hospital("泰州市第二人民医院").hospital_id == "js_taizhou_003"
    assert knowledge.resolve_hospital("泰州二院").hospital_id == "js_taizhou_003"
    assert knowledge.resolve_hospital("不存在医院") is None


def test_collection_rejects_cross_hospital_row(tmp_path):
    chromadb = pytest.importorskip("chromadb")
    corpus = HospitalCorpus.load(CORPUS)
    client = chromadb.PersistentClient(path=str(tmp_path))
    embedding = TestEmbedding()
    collection = index_hospital_collection(
        client, corpus, embedding, today=date(2026, 11, 13))
    collection.add(
        ids=["rogue-cross-hospital"],
        embeddings=[[1.0, 0.0, 0.0, 0.0]],
        documents=["不属于审核语料的跨医院内容"],
        metadatas=[{
            "record_id": "rogue-cross-hospital",
            "hospital_id": "js_taizhou_002",
            "campus_id": "js_taizhou_002_main",
            "city": "泰州",
            "category": "appointment",
            "source_id": "rogue",
            "source_url": "https://example.com/rogue",
            "review_status": "verified_official",
        }],
    )

    with pytest.raises(ValueError, match="records disagree"):
        HospitalKnowledgeBase(
            corpus, collection=collection, embedding=embedding)
