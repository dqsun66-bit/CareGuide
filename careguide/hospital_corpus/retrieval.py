"""Scope-first hybrid retrieval for the verified Jiangsu hospital corpus."""
from __future__ import annotations

from datetime import date
from dataclasses import dataclass
import hashlib
from typing import Any

from careguide.retrieval.bm25_index import BM25Index

from .models import Category, Hospital, KnowledgeRecord, ReviewStatus
from .store import HospitalCorpus


COLLECTION_PREFIX = "careguide_jiangsu_hospitals_v2"
CATEGORY_RETRIEVAL_TERMS = {
    Category.IDENTITY: "医院名称 等级 身份",
    Category.CAMPUS: "院区 地址 在哪里 怎么去",
    Category.APPOINTMENT: "怎么挂号 挂号 预约 门诊 一号",
    Category.DEPARTMENT: "科室 门诊 专科",
    Category.VISIT_PREPARATION: "就诊准备 材料 证件 注意事项",
    Category.REPORT: "报告 检查结果 查询",
    Category.TRANSPORT: "交通 路线 公交 地铁",
    Category.PARKING: "停车 车位 停车场",
    Category.ACCESSIBILITY: "无障碍 轮椅 行动不便",
    Category.EMERGENCY: "急诊 紧急就医",
    Category.CONTACT: "电话 联系方式 咨询",
}


@dataclass(frozen=True)
class HospitalRetrievalReport:
    mode: str
    lexical_count: int
    dense_count: int
    fused_count: int
    reranked: bool
    filtered_count: int
    final_count: int
    degraded_reason: str | None = None


@dataclass(frozen=True)
class HospitalRetrievalResult:
    evidence: tuple[dict, ...]
    report: HospitalRetrievalReport


def corpus_fingerprint(corpus: HospitalCorpus) -> str:
    """Return the authenticated manifest fingerprint bound to an index."""
    return hashlib.sha256((corpus.root / "manifest.json").read_bytes()).hexdigest()


def hospital_collection_name(corpus: HospitalCorpus) -> str:
    return f"{COLLECTION_PREFIX}_{corpus_fingerprint(corpus)[:12]}"


def _scope(corpus: HospitalCorpus, record: KnowledgeRecord):
    hospital = next(
        item for item in corpus.hospitals
        if item.hospital_id == record.hospital_id
    )
    campus = next(
        item for item in hospital.campuses
        if item.campus_id == record.campus_id
    )
    return hospital, campus


def hospital_retrieval_text(
    corpus: HospitalCorpus,
    record: KnowledgeRecord,
) -> str:
    hospital, campus = _scope(corpus, record)
    return " ".join((
        hospital.official_name,
        *hospital.aliases,
        hospital.city,
        campus.official_name,
        campus.address,
        record.category.value,
        CATEGORY_RETRIEVAL_TERMS[record.category],
        record.title,
        record.content,
    ))


def hospital_evidence(
    corpus: HospitalCorpus,
    record: KnowledgeRecord,
) -> dict:
    hospital, campus = _scope(corpus, record)
    return {
        "id": record.document_id,
        "title": record.title,
        "content": record.content,
        "scope": {
            "hospital_id": hospital.hospital_id,
            "hospital": hospital.official_name,
            "city": hospital.city,
            "campus_id": campus.campus_id,
            "campus": campus.official_name,
            "category": record.category.value,
        },
        "source": {
            "source_id": record.source_id,
            "url": record.source_url,
            "tier": record.source_tier.value,
            "published_at": (
                record.published_at.isoformat() if record.published_at else None
            ),
            "reviewed_at": (
                record.reviewed_at.isoformat() if record.reviewed_at else None
            ),
            "review_status": record.review_status.value,
        },
    }


def index_hospital_collection(
    client: Any,
    corpus: HospitalCorpus,
    embedding: Any,
    *,
    today: date,
):
    """Explicitly build a verified, fingerprint-bound hospital index."""
    report = corpus.validate(today=today)
    if not report.valid:
        raise ValueError("Hospital corpus validation failed")
    name = hospital_collection_name(corpus)
    if name in {item.name for item in client.list_collections()}:
        raise ValueError("Hospital collection already exists")
    records = [
        record for record in corpus.records
        if record.review_status == ReviewStatus.VERIFIED_OFFICIAL
    ]
    vectors = embedding.embed_documents([
        hospital_retrieval_text(corpus, record) for record in records
    ])
    if len(vectors) != len(records):
        raise ValueError("Hospital embedding count mismatch")
    collection = client.create_collection(
        name,
        metadata={
            "corpus_fingerprint": corpus_fingerprint(corpus),
            "corpus_version": corpus.manifest.version,
            "hnsw:space": "cosine",
            "embedding_model": embedding.model,
            "embedding_dimensions": embedding.dimensions,
        },
        embedding_function=None,
    )
    collection.add(
        ids=[record.document_id for record in records],
        embeddings=vectors,
        documents=[record.content for record in records],
        metadatas=[{
            "record_id": record.document_id,
            "hospital_id": record.hospital_id,
            "campus_id": record.campus_id,
            "city": record.city,
            "category": record.category.value,
            "source_id": record.source_id,
            "source_url": record.source_url,
            "review_status": record.review_status.value,
        } for record in records],
    )
    return collection


class HospitalKnowledgeBase:
    """Hospital-scoped BM25/dense retrieval over verified official records."""

    def __init__(
        self,
        corpus: HospitalCorpus,
        *,
        collection: Any = None,
        embedding: Any = None,
        mode: str = "hybrid",
        reranker: Any = None,
        dense_weight: float = 4.0,
        lexical_weight: float = 1.0,
        hybrid_candidates: int = 20,
        min_rerank_score: float = 0.35,
    ):
        if mode not in {"bm25", "hybrid"}:
            raise ValueError("Hospital retrieval mode must be bm25 or hybrid")
        if mode == "hybrid" and (collection is None or embedding is None):
            raise ValueError("Hospital hybrid retrieval requires collection and embedding")
        if dense_weight <= 0 or lexical_weight <= 0:
            raise ValueError("Hospital RRF weights must be positive")
        if not 1 <= int(hybrid_candidates) <= 20:
            raise ValueError("Hospital candidate depth must be between 1 and 20")
        if not 0 <= float(min_rerank_score) <= 1:
            raise ValueError("Hospital rerank threshold must be between 0 and 1")
        self.corpus = corpus
        self.mode = mode
        self.collection = collection
        self.embedding = embedding
        self.reranker = reranker
        self.dense_weight = float(dense_weight)
        self.lexical_weight = float(lexical_weight)
        self.hybrid_candidates = int(hybrid_candidates)
        self.min_rerank_score = float(min_rerank_score)
        self._records = {record.document_id: record for record in corpus.records}
        self._hospitals = {hospital.hospital_id: hospital for hospital in corpus.hospitals}
        self._hospital_names: dict[str, Hospital] = {}
        for hospital in corpus.hospitals:
            for identity in (hospital.hospital_id, hospital.official_name, *hospital.aliases):
                key = identity.strip().casefold()
                if key in self._hospital_names:
                    raise ValueError("Hospital identity is ambiguous")
                self._hospital_names[key] = hospital
        self.lexical_index = BM25Index()
        self.lexical_index.upsert({
            "chunk_id": record.document_id,
            "content": record.content,
            "retrieval_text": hospital_retrieval_text(corpus, record),
            "metadata": {
                "hospital_id": record.hospital_id,
                "campus_id": record.campus_id,
                "category": record.category.value,
            },
        } for record in corpus.records)
        if self.mode == "hybrid":
            self._validate_collection()

    def _validate_collection(self) -> None:
        metadata = self.collection.metadata or {}
        expected = {
            "corpus_fingerprint": corpus_fingerprint(self.corpus),
            "corpus_version": self.corpus.manifest.version,
            "embedding_model": self.embedding.model,
            "embedding_dimensions": self.embedding.dimensions,
            "hnsw:space": "cosine",
        }
        if (
            self.collection.name != hospital_collection_name(self.corpus)
            or any(metadata.get(key) != value for key, value in expected.items())
        ):
            raise ValueError("Hospital collection identity or metadata mismatch")
        rows = self.collection.get(include=["metadatas"])
        actual_ids = set(rows["ids"])
        if actual_ids != set(self._records):
            raise ValueError("Hospital collection records disagree with verified corpus")
        for identity, row in zip(rows["ids"], rows["metadatas"]):
            record = self._records[identity]
            expected_scope = {
                "record_id": identity,
                "hospital_id": record.hospital_id,
                "campus_id": record.campus_id,
                "category": record.category.value,
                "source_url": record.source_url,
                "review_status": ReviewStatus.VERIFIED_OFFICIAL.value,
            }
            if any(row.get(key) != value for key, value in expected_scope.items()):
                raise ValueError("Hospital collection row scope disagrees with verified corpus")

    def resolve_hospital(self, identity: str) -> Hospital | None:
        return self._hospital_names.get(str(identity).strip().casefold())

    @staticmethod
    def resolve_campus(hospital: Hospital, identity: str | None = None):
        if identity is None:
            return hospital.campuses[0] if len(hospital.campuses) == 1 else None
        key = str(identity).strip().casefold()
        return next((campus for campus in hospital.campuses if key in {
            campus.campus_id.casefold(), campus.official_name.casefold()
        }), None)

    def _empty(self, reason: str) -> HospitalRetrievalResult:
        return HospitalRetrievalResult((), HospitalRetrievalReport(
            self.mode, 0, 0, 0, False, 0, 0, reason))

    def search_with_report(
        self,
        query: str,
        hospital: str,
        *,
        campus: str | None = None,
        category: Category | str | set[Category | str] | None = None,
        top_k: int = 5,
    ) -> HospitalRetrievalResult:
        if not str(query).strip():
            raise ValueError("Hospital retrieval query cannot be empty")
        if int(top_k) < 1:
            raise ValueError("Hospital retrieval top_k must be positive")
        top_k = min(int(top_k), 5)
        selected_hospital = self.resolve_hospital(hospital)
        if selected_hospital is None:
            return self._empty("hospital_not_covered")
        selected_campus = self.resolve_campus(selected_hospital, campus)
        if selected_campus is None:
            reason = "campus_not_covered" if campus else "campus_required"
            return self._empty(reason)
        try:
            if category is None:
                selected_categories = None
            elif isinstance(category, set):
                selected_categories = {Category(value) for value in category}
            else:
                selected_categories = {Category(category)}
        except ValueError:
            return self._empty("category_not_covered")

        eligible = {
            record.document_id: record for record in self.corpus.records
            if record.hospital_id == selected_hospital.hospital_id
            and record.campus_id == selected_campus.campus_id
            and (selected_categories is None or record.category in selected_categories)
            and record.review_status == ReviewStatus.VERIFIED_OFFICIAL
        }
        if not eligible:
            return self._empty("official_information_not_covered")

        candidate_depth = min(self.hybrid_candidates, 20, len(eligible))
        lexical = self.lexical_index.search(
            query,
            top_k=candidate_depth,
            eligible_ids=set(eligible),
        )
        dense = []
        if self.mode == "hybrid":
            filters = [
                {"record_id": {"$in": sorted(eligible)}},
                {"hospital_id": selected_hospital.hospital_id},
                {"campus_id": selected_campus.campus_id},
            ]
            if selected_categories is not None:
                values = sorted(item.value for item in selected_categories)
                filters.append({
                    "category": values[0] if len(values) == 1 else {"$in": values}
                })
            where = {"$and": filters}
            dense_result = self.collection.query(
                query_embeddings=[self.embedding.embed_query(query)],
                n_results=candidate_depth,
                where=where,
                include=["distances"],
            )
            dense = [
                (identity, 1.0 - float(distance))
                for identity, distance in zip(
                    dense_result["ids"][0], dense_result["distances"][0])
                if identity in eligible
            ]

        rows: dict[str, dict[str, Any]] = {}
        for rank, hit in enumerate(lexical, 1):
            rows[hit.chunk_id] = {
                "mode": "bm25" if self.mode == "bm25" else "hybrid_rrf",
                "bm25_rank": rank,
                "bm25_score": hit.score,
                "rank_score": (
                    hit.score if self.mode == "bm25"
                    else self.lexical_weight / (60 + rank)
                ),
                "rrf_weights": {
                    "dense": self.dense_weight,
                    "bm25": self.lexical_weight,
                },
            }
        for rank, (identity, score) in enumerate(dense, 1):
            row = rows.setdefault(identity, {
                "mode": "hybrid_rrf",
                "rank_score": 0.0,
                "rrf_weights": {
                    "dense": self.dense_weight,
                    "bm25": self.lexical_weight,
                },
            })
            row.update({"dense_rank": rank, "cosine_score": score})
            row["rank_score"] += self.dense_weight / (60 + rank)
        fused_count = len(rows)
        ordered = sorted(
            rows, key=lambda identity: (-rows[identity]["rank_score"], identity)
        )[:20]

        reranked = False
        if self.mode == "hybrid" and self.reranker is not None and ordered:
            identities, scores, _ = self.reranker.rerank(
                query,
                [(identity, hospital_retrieval_text(self.corpus, eligible[identity]))
                 for identity in ordered],
                top_n=min(20, len(ordered)),
            )
            reranked = True
            ordered = identities
            for identity, score in zip(identities, scores):
                rows[identity].update({
                    "mode": "hybrid_rrf_rerank",
                    "rerank_model": self.reranker.model,
                    "rerank_score": float(score),
                })

        evidence: list[dict] = []
        filtered_count = 0
        for identity in ordered:
            if (
                self.mode == "hybrid"
                and self.reranker is not None
                and rows[identity]["rerank_score"] < self.min_rerank_score
            ):
                filtered_count += 1
                continue
            evidence.append({
                **hospital_evidence(self.corpus, eligible[identity]),
                "retrieval": rows[identity],
            })
            if len(evidence) == top_k:
                break
        return HospitalRetrievalResult(tuple(evidence), HospitalRetrievalReport(
            mode=self.mode,
            lexical_count=len(lexical),
            dense_count=len(dense),
            fused_count=fused_count,
            reranked=reranked,
            filtered_count=filtered_count,
            final_count=len(evidence),
        ))
