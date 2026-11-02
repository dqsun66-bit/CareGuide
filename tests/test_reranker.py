import asyncio
import json

import httpx
import pytest

from careguide.retrieval.reranker import OpenAICompatibleReranker


def test_huawei_reranker_contract_caps_candidates_and_authenticates():
    observed = {}

    def handler(request):
        observed["path"] = request.url.path
        observed["authorization"] = request.headers["authorization"]
        observed["payload"] = json.loads(request.content)
        return httpx.Response(200, json={"results": [
            {"index": 0, "relevance_score": 0.9},
        ]})

    reranker = OpenAICompatibleReranker(
        api_key="test-only", base_url="https://provider.example/v1",
        model="bge-reranker-v2-m3", max_retries=0,
        transport=httpx.MockTransport(handler),
    )
    candidates = [(str(index), f"document-{index}") for index in range(25)]

    reranker.rerank("query", candidates, top_n=1)

    assert observed["path"] == "/v1/rerank"
    assert observed["authorization"] == "Bearer test-only"
    assert observed["payload"]["model"] == "bge-reranker-v2-m3"
    assert len(observed["payload"]["documents"]) == 20


def test_reranker_sanitizes_provider_error():
    def handler(request):
        return httpx.Response(401, text="upstream leaked test-only and patient text")

    reranker = OpenAICompatibleReranker(
        api_key="test-only", base_url="https://provider.example/v1",
        model="bge-reranker-v2-m3", max_retries=0,
        transport=httpx.MockTransport(handler),
    )

    with pytest.raises(RuntimeError) as caught:
        reranker.rerank("private symptom", [("a", "private evidence")], top_n=1)
    assert str(caught.value) == "rerank_authentication_failed"
    assert "test-only" not in str(caught.value)
    assert "private" not in str(caught.value)


def test_reranker_preserves_provider_order_and_candidate_identity():
    async def handler(request):
        payload = json.loads(request.content)
        assert payload["documents"] == ["first", "second", "third"]
        return httpx.Response(200, json={"results": [
            {"index": 1, "relevance_score": 0.9},
            {"index": 0, "relevance_score": 0.4},
        ], "meta": {"tokens": {"input_tokens": 3}}})

    async def run():
        reranker = OpenAICompatibleReranker(
            api_key="test", base_url="https://provider.example/v1",
            model="reranker", concurrency=1)
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await reranker._one(
                client, asyncio.Semaphore(1), "query",
                [("a", "first"), ("b", "second"), ("c", "third")], 2)

    ids, scores, meta = asyncio.run(run())
    assert ids == ["b", "a"]
    assert scores == [0.9, 0.4]
    assert meta["tokens"]["input_tokens"] == 3


def test_reranker_rejects_invalid_indices():
    async def handler(request):
        return httpx.Response(200, json={"results": [
            {"index": 2, "relevance_score": 0.9},
        ]})

    async def run():
        reranker = OpenAICompatibleReranker(
            api_key="test", base_url="https://provider.example/v1", model="reranker")
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await reranker._one(
                client, asyncio.Semaphore(1), "query", [("a", "first")], 1)

    with pytest.raises(RuntimeError, match="provider_invalid_response"):
        asyncio.run(run())
