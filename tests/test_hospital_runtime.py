from pathlib import Path
from types import SimpleNamespace

import pytest

import careguide.runtime as runtime
from careguide.hospital_corpus.retrieval import (
    corpus_fingerprint,
    hospital_collection_name,
)


ROOT = Path(__file__).resolve().parents[1]
CORPUS_DIR = ROOT / "data" / "jiangsu_hospitals_v2"


class FakeEmbedding:
    model = "bge-m3"
    dimensions = 1024
    provider_name = "huawei-modelarts"


class FakeReranker:
    model = "bge-reranker-v2-m3"
    provider_name = "huawei-modelarts"


class FakeCollection:
    def __init__(self, corpus, *, valid=True):
        self.name = hospital_collection_name(corpus)
        self.metadata = {
            "corpus_fingerprint": corpus_fingerprint(corpus),
            "corpus_version": corpus.manifest.version,
            "embedding_model": "bge-m3",
            "embedding_dimensions": 1024,
            "hnsw:space": "cosine",
        }
        if not valid:
            self.metadata["corpus_fingerprint"] = "wrong"
        self._corpus = corpus

    def get(self, include):
        return {
            "ids": [record.document_id for record in self._corpus.records],
            "metadatas": [{
                "record_id": record.document_id,
                "hospital_id": record.hospital_id,
                "campus_id": record.campus_id,
                "category": record.category.value,
                "source_url": record.source_url,
                "review_status": record.review_status.value,
            } for record in self._corpus.records],
        }


class FakeClient:
    def __init__(self, collection=None):
        self.collection = collection

    def list_collections(self):
        return [] if self.collection is None else [self.collection]

    def get_collection(self, name, embedding_function=None):
        return self.collection


def configure_hybrid(monkeypatch):
    monkeypatch.setenv("CAREGUIDE_HOSPITAL_RETRIEVAL_MODE", "hybrid")
    monkeypatch.setenv("CAREGUIDE_HOSPITAL_RERANK_ENABLED", "true")
    monkeypatch.setattr(runtime, "semantic_embedding", lambda: FakeEmbedding())
    monkeypatch.setattr(runtime, "semantic_reranker", lambda: FakeReranker())


def test_hospital_hybrid_runtime_rejects_missing_collection(monkeypatch):
    configure_hybrid(monkeypatch)
    corpus = runtime.load_hospital_corpus(CORPUS_DIR)

    with pytest.raises(ValueError, match="hospital_hybrid_index_unavailable"):
        runtime.build_hospital_knowledge(corpus, client=FakeClient())


def test_hospital_hybrid_runtime_sanitizes_invalid_collection(monkeypatch):
    configure_hybrid(monkeypatch)
    corpus = runtime.load_hospital_corpus(CORPUS_DIR)
    collection = FakeCollection(corpus, valid=False)

    with pytest.raises(ValueError) as caught:
        runtime.build_hospital_knowledge(
            corpus, client=FakeClient(collection))

    assert str(caught.value) == "hospital_hybrid_index_invalid"
    assert corpus_fingerprint(corpus) not in str(caught.value)


def test_hospital_hybrid_runtime_reports_bound_providers(monkeypatch):
    configure_hybrid(monkeypatch)
    corpus = runtime.load_hospital_corpus(CORPUS_DIR)
    collection = FakeCollection(corpus)

    knowledge, status = runtime.build_hospital_knowledge(
        corpus, client=FakeClient(collection))

    assert knowledge.collection is collection
    assert status.active_mode == "hybrid"
    assert status.embedding_provider == "huawei-modelarts"
    assert status.rerank_provider == "huawei-modelarts"
    assert status.index_collection == hospital_collection_name(corpus)


def test_hospital_runtime_can_be_explicitly_disabled(monkeypatch):
    monkeypatch.setenv("CAREGUIDE_HOSPITAL_RETRIEVAL_MODE", "disabled")

    knowledge, status = runtime.build_hospital_knowledge()

    assert knowledge is None
    assert status.active_mode == "disabled"
    assert status.embedding_provider is None


def test_hospital_runtime_bm25_needs_no_remote_provider(monkeypatch):
    monkeypatch.setenv("CAREGUIDE_HOSPITAL_RETRIEVAL_MODE", "bm25")
    monkeypatch.setattr(
        runtime,
        "semantic_embedding",
        lambda: pytest.fail("BM25 must not construct a remote provider"),
    )
    corpus = runtime.load_hospital_corpus(CORPUS_DIR)

    knowledge, status = runtime.build_hospital_knowledge(corpus)

    assert knowledge.mode == "bm25"
    assert status.active_mode == "bm25"
    assert status.embedding_provider is None
