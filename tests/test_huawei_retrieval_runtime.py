from types import SimpleNamespace

import pytest

import careguide.runtime as runtime
from careguide.corpus import Corpus


class FakeEmbedding:
    model = "embedding-model"
    dimensions = 4
    provider_name = "huawei-modelarts"


class FakeClient:
    def __init__(self, collections=(), collection=None):
        self._collections = [SimpleNamespace(name=name) for name in collections]
        self._collection = collection

    def list_collections(self):
        return self._collections

    def get_collection(self, name, embedding_function=None):
        return self._collection


def configure_hybrid(monkeypatch):
    monkeypatch.setenv("CAREGUIDE_RETRIEVAL_MODE", "hybrid")
    monkeypatch.setenv("CAREGUIDE_RERANK_ENABLED", "true")
    monkeypatch.setenv("CAREGUIDE_EMBEDDING_API_KEY", "test-only")
    monkeypatch.setenv("CAREGUIDE_EMBEDDING_MODEL", "embedding-model")
    monkeypatch.setenv("CAREGUIDE_EMBEDDING_DIMENSIONS", "4")
    monkeypatch.setenv("CAREGUIDE_RERANK_API_KEY", "test-only")
    monkeypatch.setenv("CAREGUIDE_RERANK_MODEL", "bge-reranker-v2-m3")
    monkeypatch.setattr(runtime, "semantic_embedding", lambda: FakeEmbedding())
    monkeypatch.setattr(runtime, "semantic_reranker", lambda: SimpleNamespace(
        model="bge-reranker-v2-m3"))


def test_required_secret_rejects_placeholder_without_echo(monkeypatch):
    monkeypatch.setenv("CAREGUIDE_EMBEDDING_API_KEY", "YOUR_REAL_SECRET")

    with pytest.raises(ValueError) as caught:
        runtime._required_secret("CAREGUIDE_EMBEDDING_API_KEY")

    assert str(caught.value) == "CAREGUIDE_EMBEDDING_API_KEY is required"
    assert "YOUR_REAL_SECRET" not in str(caught.value)


def test_hybrid_runtime_rejects_missing_versioned_collection(monkeypatch):
    configure_hybrid(monkeypatch)
    corpus = Corpus()

    with pytest.raises(ValueError, match="hybrid_index_unavailable"):
        runtime.build_retrieval_knowledge(corpus, client=FakeClient())


def test_hybrid_runtime_sanitizes_index_metadata_mismatch(monkeypatch):
    configure_hybrid(monkeypatch)
    corpus = Corpus()
    name = runtime.versioned_collection_name(corpus)
    collection = SimpleNamespace(
        name=name,
        metadata={"corpus_fingerprint": "wrong"},
        get=lambda include: {"ids": []},
    )

    with pytest.raises(ValueError) as caught:
        runtime.build_retrieval_knowledge(
            corpus, client=FakeClient([name], collection))

    assert str(caught.value) == "hybrid_index_invalid"
    assert corpus.fingerprint not in str(caught.value)


@pytest.mark.parametrize("value", ["", "0", "-1", "not-a-number"])
def test_embedding_dimensions_must_be_positive_integer(monkeypatch, value):
    monkeypatch.setenv("CAREGUIDE_EMBEDDING_API_KEY", "test-only")
    monkeypatch.setenv("CAREGUIDE_EMBEDDING_BASE_URL", "https://example.invalid/v1")
    monkeypatch.setenv("CAREGUIDE_EMBEDDING_MODEL", "embedding-model")
    monkeypatch.setenv("CAREGUIDE_EMBEDDING_DIMENSIONS", value)

    with pytest.raises(ValueError, match="CAREGUIDE_EMBEDDING_DIMENSIONS"):
        runtime.semantic_embedding()


def test_runtime_status_identifies_huawei_hybrid_provider():
    status = runtime.RetrievalRuntimeStatus(
        requested_mode="hybrid", active_mode="hybrid",
        embedding_provider="huawei-modelarts",
        rerank_provider="huawei-modelarts",
        index_collection="careguide_medical_v1_deadbeef",
        degraded_reason=None,
    )

    assert status.active_mode == "hybrid"
    assert status.embedding_provider == "huawei-modelarts"
    assert status.degraded_reason is None
