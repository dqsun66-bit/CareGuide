import asyncio
import json
from datetime import date
from pathlib import Path
from types import SimpleNamespace

import pytest

from careguide.agent_orchestrator import AgentOrchestrator
from careguide.corpus import Corpus
from careguide.domain import IntentCategory
from careguide.hospital_corpus.retrieval import (
    HospitalKnowledgeBase,
    index_hospital_collection,
)
from careguide.hospital_corpus.store import HospitalCorpus
from careguide.intent_recognizer import IntentResult
from careguide.retrieval.knowledge_base import MedicalKnowledgeBase
from careguide.tools import MedicalToolRegistry


ROOT = Path(__file__).resolve().parents[1]
CORPUS_DIR = ROOT / "data" / "jiangsu_hospitals_v2"
HOSPITAL_ID = "js_taizhou_001"
CAMPUS_ID = "js_taizhou_001_main"
HOSPITAL_NAME = "南京医科大学附属泰州人民医院"


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

    def rerank(self, query, candidates, *, top_n):
        return (
            [identity for identity, _ in candidates[:top_n]],
            [0.9 for _ in candidates[:top_n]],
            {},
        )


class ScriptedClient:
    def __init__(self, tool_arguments):
        self.replies = [
            {
                "content": None,
                "tool_calls": [{
                    "id": "hospital-call",
                    "type": "function",
                    "function": {
                        "name": "lookup_hospital_guide",
                        "arguments": json.dumps(tool_arguments, ensure_ascii=False),
                    },
                }],
            },
            {"content": "工具查询已经完成。"},
        ]
        self.chat = SimpleNamespace(
            completions=SimpleNamespace(create=self.create))

    async def create(self, **kwargs):
        return {"choices": [{"message": self.replies.pop(0)}]}


class FixedRecognizer:
    async def recognize(self, message, history):
        return IntentResult(
            IntentCategory.APPOINTMENT,
            1.0,
            [IntentCategory.APPOINTMENT],
            "route",
            entities={
                "hospital_id": HOSPITAL_NAME,
                "campus": CAMPUS_ID,
            },
            fine_intent="appointment_process",
        )


@pytest.fixture(scope="module")
def registry(tmp_path_factory):
    chromadb = pytest.importorskip("chromadb")
    corpus = HospitalCorpus.load(CORPUS_DIR)
    client = chromadb.PersistentClient(
        path=str(tmp_path_factory.mktemp("hospital-chat-chroma")))
    embedding = TestEmbedding()
    collection = index_hospital_collection(
        client, corpus, embedding, today=date(2026, 11, 13))
    hospital_knowledge = HospitalKnowledgeBase(
        corpus,
        collection=collection,
        embedding=embedding,
        reranker=TestReranker(),
    )
    return MedicalToolRegistry(
        MedicalKnowledgeBase(Corpus()),
        hospital_knowledge=hospital_knowledge,
    )


def run_chat(registry, message, **arguments):
    client = ScriptedClient({
        "hospital_id": HOSPITAL_NAME,
        "campus": CAMPUS_ID,
        "query": message,
        **arguments,
    })
    engine = AgentOrchestrator(
        FixedRecognizer(), registry, client=client, max_tool_rounds=2)
    return asyncio.run(engine.process(message, "u001", "hospital-v2"))


def assert_official_scoped(answer):
    assert answer["evidence"]
    assert answer["retrieval"]["mode"] == "hybrid"
    assert answer["retrieval"]["reranked"] is True
    assert answer["side_effects"] == []
    assert all(item["scope"]["hospital_id"] == HOSPITAL_ID
               for item in answer["evidence"])
    assert all(item["source"]["review_status"] == "verified_official"
               and item["source"]["url"].startswith("https://")
               for item in answer["evidence"])
    assert "来源：https://" in answer["response"]


def test_chat_returns_official_appointment_materials(registry):
    answer = run_chat(
        registry,
        "泰州市人民医院怎么挂号，需要带什么？",
        section="materials",
        top_k=10,
    )

    assert_official_scoped(answer)
    assert len(answer["evidence"]) <= 5


def test_chat_returns_address_and_does_not_invent_accessibility(registry):
    answer = run_chat(
        registry,
        "行动不便去泰州市人民医院，院区地址和无障碍信息有哪些？",
        section="all",
    )

    assert_official_scoped(answer)
    call = next(item for item in answer["tools_used"]
                if item == "lookup_hospital_guide")
    assert call == "lookup_hospital_guide"
    assert {item["scope"]["category"] for item in answer["evidence"]} == {"campus"}


def test_chat_parking_without_official_record_is_not_covered(registry):
    answer = run_chat(
        registry,
        "泰州市人民医院停车信息有官方资料吗？",
        section="all",
    )

    assert answer["workflow_status"] == "not_covered"
    assert answer["evidence"] == []
    assert answer["side_effects"] == []
    assert "未覆盖" in answer["response"]
