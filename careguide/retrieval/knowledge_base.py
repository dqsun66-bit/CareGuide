"""Scope-first BM25 and optional real-embedding Chroma/RRF medical retrieval.

No deterministic hash embeddings or legacy operations corpora are used. Querying
never modifies source files or the Chroma collection. Dense indexing is explicit.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any

from careguide.corpus import Corpus, Record
from careguide.retrieval.bm25_index import BM25Index
from careguide.retrieval.evidence_policy import accept_evidence

COLLECTION_NAME = 'careguide_medical_v1'


def versioned_collection_name(corpus):
    return COLLECTION_NAME+'_'+corpus.fingerprint[:12]


@dataclass(frozen=True)
class RetrievalReport:
    mode: str
    lexical_count: int
    dense_count: int
    fused_count: int
    reranked: bool
    filtered_count: int
    final_count: int
    degraded_reason: str | None = None


@dataclass(frozen=True)
class RetrievalResult:
    evidence: tuple[dict, ...]
    report: RetrievalReport


class MedicalKnowledgeBase:
    def __init__(self, corpus: Corpus, *, collection: Any = None, embedding: Any = None,
                 mode: str = 'bm25', min_dense_score: float = -1.0,
                 reranker: Any = None, rerank_candidates: int = 20,
                 dense_weight: float = 4.0, lexical_weight: float = 1.0,
                 hybrid_candidates: int = 20, min_rerank_score: float = 0.35):
        if mode not in {'bm25', 'dense', 'hybrid'}:
            raise ValueError('Retrieval mode must be bm25, dense or hybrid')
        if not -1 <= min_dense_score <= 1:
            raise ValueError('Dense threshold must be between -1 and 1')
        if dense_weight <= 0 or lexical_weight <= 0:
            raise ValueError('Hybrid RRF weights must be positive')
        if hybrid_candidates < 1:
            raise ValueError('Hybrid candidate depth must be positive')
        if not 0 <= min_rerank_score <= 1:
            raise ValueError('Rerank threshold must be between 0 and 1')
        if mode != 'bm25' and (collection is None or embedding is None):
            raise ValueError('Dense retrieval requires an explicitly indexed collection and real embedding provider')
        self.corpus, self.collection, self.embedding = corpus, collection, embedding
        self.mode, self.min_dense_score = mode, min_dense_score
        self.reranker = reranker
        self.rerank_candidates = max(1, int(rerank_candidates))
        self.dense_weight = float(dense_weight)
        self.lexical_weight = float(lexical_weight)
        self.hybrid_candidates = int(hybrid_candidates)
        self.min_rerank_score = float(min_rerank_score)
        self.lexical_index = BM25Index()
        self.lexical_index.upsert({
            'chunk_id': record.id, 'content': record.content,
            'retrieval_text': self.retrieval_text(record), 'metadata': {}}
            for record in corpus.records)
        if mode != 'bm25':
            self._validate_collection()

    def _validate_collection(self):
        metadata = self.collection.metadata or {}
        if metadata.get('reuse_complete') is False:
            raise ValueError('Medical index reuse has not completed verification')
        expected = {'corpus_fingerprint': self.corpus.fingerprint,
                    'embedding_model': self.embedding.model,
                    'embedding_dimensions': self.embedding.dimensions,
                    'hnsw:space': 'cosine'}
        if self.collection.name not in {COLLECTION_NAME, versioned_collection_name(self.corpus)} or any(metadata.get(k) != v for k, v in expected.items()):
            raise ValueError('Medical collection identity, corpus or embedding metadata mismatch')
        actual_ids = set(self.collection.get(include=[])['ids'])
        if actual_ids != {r.id for r in self.corpus.records}:
            raise ValueError('Medical collection records disagree with verified corpus')

    @staticmethod
    def retrieval_text(record: Record) -> str:
        return ' '.join([record.title, *record.keywords, record.content])

    def search(self, query: str, candidates: list[Record], *, today: date,
               top_k: int = 5) -> list[dict]:
        return list(self.search_with_report(
            query, candidates, today=today, top_k=top_k,
            risk='raw_retrieval').evidence)

    def search_with_report(self, query: str, candidates: list[Record], *, today: date,
                           top_k: int = 5, risk: str = 'general_guidance') -> RetrievalResult:
        if not str(query).strip():
            raise ValueError('Retrieval query cannot be empty')
        if not 1 <= int(top_k) <= 10:
            raise ValueError('Retrieval top_k must be between 1 and 10')
        # Select scope BEFORE lexical scoring and vector querying. A different
        # hospital or product cannot occupy candidate slots or re-enter via RRF.
        eligible = {r.id: r for r in candidates if self.corpus.fresh(r, today)}
        if not eligible:
            return RetrievalResult((), RetrievalReport(
                self.mode, 0, 0, 0, False, 0, 0))
        candidate_depth = min(self.hybrid_candidates, 20)
        lexical_limit = candidate_depth if self.mode == 'hybrid' else min(len(eligible), 20)
        lexical = self.lexical_index.search(
            query, top_k=min(len(eligible), lexical_limit),
            eligible_ids=set(eligible)) if self.mode != 'dense' else []
        preferred_ids: set[str] = set()
        if lexical:
            # When an official/curated note is essentially tied with an open
            # dataset QA row, prefer the narrower reviewed source.  This is a
            # source-tier tie-break, not a claim that curated data is always
            # semantically relevant; the 90% score gate preserves clear wins.
            top_score = lexical[0].score
            preferred = [hit for hit in lexical
                         if eligible[hit.chunk_id].representation == 'curated_factual_summary'
                         and hit.score >= top_score * 0.9]
            if preferred:
                preferred_ids = {hit.chunk_id for hit in preferred}
                lexical = preferred + [hit for hit in lexical if hit.chunk_id not in preferred_ids]
        dense = []
        if self.mode != 'bm25':
            dense_limit = (candidate_depth if self.mode == 'hybrid' else
                           max(top_k, self.rerank_candidates if self.reranker else top_k))
            dense_limit = min(dense_limit, 20)
            result = self.collection.query(
                query_embeddings=[self.embedding.embed_query(query)],
                n_results=min(dense_limit, len(eligible)), where={'record_id': {'$in': list(eligible)}},
                include=['distances'])
            dense = [(identity, 1-float(distance)) for identity, distance
                     in zip(result['ids'][0], result['distances'][0])
                     if identity in eligible and 1-float(distance) >= self.min_dense_score]
        rows = {}
        if self.mode == 'bm25':
            for hit in lexical:
                tiebreak = lexical[0].score if hit.chunk_id in preferred_ids else 0.0
                rows[hit.chunk_id] = {'mode': 'bm25', 'bm25_score': hit.score,
                                     'matched_terms': hit.matched_term_count,
                                     'source_tier_tiebreak': bool(tiebreak),
                                     'rank_score': hit.score + tiebreak}
        elif self.mode == 'dense':
            rows = {identity: {'mode': 'dense', 'cosine_score': score, 'rank_score': score}
                    for identity, score in dense}
        else:
            # RRF ranking is not a relevance probability or clinical confidence.
            # Weights are development-set ranking parameters, not confidence.
            for rank, hit in enumerate(lexical, 1):
                rows[hit.chunk_id] = {'mode': 'hybrid_rrf', 'bm25_rank': rank,
                                     'bm25_score': hit.score,
                                     'rrf_weights': {'dense': self.dense_weight,
                                                     'bm25': self.lexical_weight},
                                     'rank_score': self.lexical_weight/(60+rank)}
            for rank, (identity, score) in enumerate(dense, 1):
                row = rows.setdefault(identity, {
                    'mode': 'hybrid_rrf', 'rank_score': 0.0,
                    'rrf_weights': {'dense': self.dense_weight,
                                    'bm25': self.lexical_weight}})
                row.update({'dense_rank': rank, 'cosine_score': score})
                row['rank_score'] += self.dense_weight/(60+rank)
        fused_count = len(rows)
        initial_k = min(20, max(top_k, self.rerank_candidates)) if self.reranker else top_k
        ordered = sorted(rows, key=lambda key: (-rows[key]['rank_score'], key))[:initial_k]
        reranked = False
        if self.reranker and ordered:
            rerank_top_n = min(10, len(ordered))
            identities, scores, _ = self.reranker.rerank(
                query, [(key, eligible[key].content) for key in ordered], top_n=rerank_top_n)
            ordered = identities
            reranked = True
            for key, score in zip(ordered, scores):
                rows[key]['rerank_model'] = self.reranker.model
                rows[key]['rerank_score'] = score
                rows[key]['pre_rerank_mode'] = rows[key]['mode']
                rows[key]['mode'] = f"{rows[key]['mode']}_rerank"
        evidence, filtered_count = [], 0
        for key in ordered:
            item = {**self.corpus.evidence(eligible[key]), 'retrieval': rows[key]}
            accepted, reason = accept_evidence(
                item, risk=risk, min_rerank_score=self.min_rerank_score)
            if not accepted:
                rows[key]['filtered_reason'] = reason
                filtered_count += 1
                continue
            evidence.append(item)
            if len(evidence) == top_k:
                break
        return RetrievalResult(tuple(evidence), RetrievalReport(
            mode=self.mode,
            lexical_count=len(lexical),
            dense_count=len(dense),
            fused_count=fused_count,
            reranked=reranked,
            filtered_count=filtered_count,
            final_count=len(evidence),
        ))


def index_collection(client: Any, corpus: Corpus, embedding: Any, *, versioned=False):
    """Explicit offline ingestion operation; never called by query handlers."""
    name = versioned_collection_name(corpus) if versioned else COLLECTION_NAME
    if name in {c.name for c in client.list_collections()}:
        raise ValueError('Medical collection already exists; refusing implicit overwrite')
    metadata = {'corpus_fingerprint': corpus.fingerprint, 'hnsw:space': 'cosine',
                'embedding_model': embedding.model, 'embedding_dimensions': embedding.dimensions}
    # Embedding validation occurs before creating any collection.
    records = list(corpus.records)
    vectors = embedding.embed_documents([MedicalKnowledgeBase.retrieval_text(r) for r in records])
    if len(vectors) != len(records):
        raise ValueError('Embedding count mismatch')
    collection = client.create_collection(name, metadata=metadata, embedding_function=None)
    collection.add(ids=[r.id for r in records], embeddings=vectors,
                   documents=[r.content for r in records],
                   metadatas=[{'record_id': r.id, 'source_id': r.source_id, 'kind': r.kind.value} for r in records])
    return collection
