"""Dedicated provider reranker for retrieval candidates."""
from __future__ import annotations

import asyncio
import math
from typing import Any, Sequence

import httpx


DEFAULT_RERANK_INSTRUCTION = (
    "根据中文医疗查询与候选段落能否直接回答该问题进行相关性排序。"
)


class OpenAICompatibleReranker:
    """Call a provider's ``/rerank`` endpoint and preserve candidate IDs."""

    def __init__(self, *, api_key: str, base_url: str, model: str,
                 instruction: str = DEFAULT_RERANK_INSTRUCTION,
                 timeout: float = 30.0, concurrency: int = 8,
                 max_retries: int = 2, candidate_limit: int = 20,
                 provider_name: str = "huawei-modelarts",
                 transport: httpx.BaseTransport | None = None):
        if not str(api_key).strip():
            raise ValueError("reranker api_key cannot be empty")
        if not str(base_url).strip() or not str(model).strip():
            raise ValueError("reranker base_url and model cannot be empty")
        if concurrency <= 0 or not 0 <= max_retries <= 2:
            raise ValueError("reranker concurrency/retries are invalid")
        if not 0 < float(timeout) <= 30:
            raise ValueError("reranker timeout must be between 0 and 30 seconds")
        if not 1 <= int(candidate_limit) <= 20:
            raise ValueError("reranker candidate_limit must be between 1 and 20")
        self.api_key = str(api_key)
        self.base_url = str(base_url).rstrip("/")
        self.model = str(model).strip()
        self.instruction = str(instruction).strip()
        self.timeout = float(timeout)
        self.concurrency = int(concurrency)
        self.max_retries = int(max_retries)
        self.candidate_limit = int(candidate_limit)
        self.provider_name = str(provider_name).strip() or "huawei-modelarts"
        self.transport = transport

    @property
    def endpoint(self) -> str:
        return self.base_url + "/rerank"

    @staticmethod
    def _parse(data: dict[str, Any], candidates: Sequence[tuple[str, str]],
               expected: int) -> tuple[list[str], list[float], dict[str, Any]]:
        results = data.get("results")
        if not isinstance(results, list):
            raise RuntimeError("rerank response is missing results")
        ids, scores, seen = [], [], set()
        for item in results:
            index = int(item["index"])
            if index < 0 or index >= len(candidates) or index in seen:
                raise RuntimeError("rerank response contains an invalid index")
            seen.add(index)
            ids.append(candidates[index][0])
            score = float(item["relevance_score"])
            if not math.isfinite(score) or not 0 <= score <= 1:
                raise RuntimeError("rerank response contains an invalid score")
            scores.append(score)
        if len(ids) != expected:
            raise RuntimeError("rerank response returned the wrong result count")
        return ids, scores, data.get("meta") or {}

    def rerank(self, query: str, candidates: Sequence[tuple[str, str]],
               *, top_n: int) -> tuple[list[str], list[float], dict[str, Any]]:
        if not query.strip() or not candidates:
            raise ValueError("reranking requires a query and candidates")
        if int(top_n) < 1:
            raise ValueError("reranking top_n must be positive")
        candidates = list(candidates)[:self.candidate_limit]
        expected = min(int(top_n), len(candidates))
        payload = {"model": self.model, "query": query,
                   "documents": [text for _, text in candidates],
                   "top_n": expected, "return_documents": False}
        if self.instruction:
            payload["instruction"] = self.instruction
        headers = {"Authorization": f"Bearer {self.api_key}",
                   "Content-Type": "application/json"}
        with httpx.Client(headers=headers, timeout=self.timeout, trust_env=False,
                          transport=self.transport) as client:
            for attempt in range(self.max_retries + 1):
                try:
                    response = client.post(self.endpoint, json=payload)
                except httpx.TimeoutException:
                    raise RuntimeError("rerank_timeout") from None
                if response.status_code in {429, 503, 504} and attempt < self.max_retries:
                    retry_after = response.headers.get("retry-after")
                    delay = float(retry_after) if retry_after else min(2 ** attempt, 2)
                    __import__('time').sleep(max(0.25, delay))
                    continue
                if response.status_code in {401, 403}:
                    raise RuntimeError("rerank_authentication_failed")
                if response.status_code == 429:
                    raise RuntimeError("rerank_rate_limited")
                if response.status_code in {503, 504}:
                    raise RuntimeError("rerank_unavailable")
                if not response.is_success:
                    raise RuntimeError("rerank_provider_error")
                try:
                    return self._parse(response.json(), candidates, expected)
                except (KeyError, TypeError, ValueError, RuntimeError):
                    raise RuntimeError("provider_invalid_response") from None
        raise RuntimeError("unreachable reranker retry state")

    async def _one(self, client: httpx.AsyncClient, semaphore: asyncio.Semaphore,
                   query: str, candidates: Sequence[tuple[str, str]],
                   top_n: int) -> tuple[list[str], list[float], dict[str, Any]]:
        if not query.strip() or not candidates:
            raise ValueError("reranking requires a query and candidates")
        if int(top_n) < 1:
            raise ValueError("reranking top_n must be positive")
        candidates = list(candidates)[:self.candidate_limit]
        payload = {
            "model": self.model,
            "query": query,
            "documents": [text for _, text in candidates],
            "top_n": min(int(top_n), len(candidates)),
            "return_documents": False,
        }
        if self.instruction:
            payload["instruction"] = self.instruction
        async with semaphore:
            for attempt in range(self.max_retries + 1):
                try:
                    response = await client.post(self.endpoint, json=payload)
                except httpx.TimeoutException:
                    raise RuntimeError("rerank_timeout") from None
                if response.status_code in {429, 503, 504} and attempt < self.max_retries:
                    retry_after = response.headers.get("retry-after")
                    delay = float(retry_after) if retry_after else min(2 ** attempt, 2)
                    await asyncio.sleep(max(0.25, delay))
                    continue
                if response.status_code in {401, 403}:
                    raise RuntimeError("rerank_authentication_failed")
                if response.status_code == 429:
                    raise RuntimeError("rerank_rate_limited")
                if response.status_code in {503, 504}:
                    raise RuntimeError("rerank_unavailable")
                if not response.is_success:
                    raise RuntimeError("rerank_provider_error")
                try:
                    return self._parse(response.json(), candidates, payload["top_n"])
                except (KeyError, TypeError, ValueError, RuntimeError):
                    raise RuntimeError("provider_invalid_response") from None
        raise RuntimeError("unreachable reranker retry state")

    async def rerank_many(self, jobs: Sequence[tuple[str, Sequence[tuple[str, str]]]],
                          *, top_n: int) -> list[tuple[list[str], list[float], dict[str, Any]]]:
        semaphore = asyncio.Semaphore(self.concurrency)
        headers = {"Authorization": f"Bearer {self.api_key}",
                   "Content-Type": "application/json"}
        async with httpx.AsyncClient(headers=headers, timeout=self.timeout,
                                     trust_env=False) as client:
            return await asyncio.gather(*[
                self._one(client, semaphore, query, candidates, top_n)
                for query, candidates in jobs
            ])
