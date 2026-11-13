# Jiangsu Hospital V2 Hybrid Retrieval Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a separately scoped Chroma index for the 15-hospital Jiangsu V2 corpus and connect BM25, Huawei BGE-M3, RRF, and Huawei BGE-Reranker-V2-M3 to CareGuide's hospital-guide tool.

**Architecture:** Add a hospital-specific retrieval module that consumes only authenticated `HospitalCorpus` records, creates a fingerprint-bound Chroma collection, and filters by hospital before lexical or dense ranking. Keep the existing general medical knowledge base unchanged; `MedicalToolRegistry` delegates only `lookup_hospital_guide` to the optional hospital knowledge base.

**Tech Stack:** Python 3.11, Pydantic, ChromaDB, existing `BM25Index`, Huawei MaaS `bge-m3`, Huawei MaaS `bge-reranker-v2-m3`, pytest.

---

## File map

- Create `careguide/hospital_corpus/retrieval.py`: hospital projection, collection identity, indexing, scoped hybrid query, and evidence rendering.
- Create `scripts/index_jiangsu_hospitals.py`: explicit dry-run/apply index command.
- Create `scripts/evaluate_jiangsu_hospital_hybrid.py`: explicit real-provider evaluation command.
- Create `tests/test_hospital_hybrid_retrieval.py`: deterministic retrieval and Chroma tests.
- Create `tests/test_hospital_runtime.py`: runtime collection binding and failure tests.
- Create `tests/test_hospital_chat_integration.py`: tool-to-chat acceptance tests.
- Modify `careguide/runtime.py`, `careguide/tools.py`, `tests/test_medical_tools.py`, `.env.example`, and the V2 evaluation README.

### Task 1: Hospital projection and collection identity

**Files:**
- Create: `careguide/hospital_corpus/retrieval.py`
- Create: `tests/test_hospital_hybrid_retrieval.py`

- [ ] **Step 1: Write the failing projection tests**

```python
from pathlib import Path

from careguide.hospital_corpus.retrieval import (
    corpus_fingerprint, hospital_collection_name,
    hospital_evidence, hospital_retrieval_text,
)
from careguide.hospital_corpus.store import HospitalCorpus

ROOT = Path(__file__).resolve().parents[1]
CORPUS = ROOT / "data/jiangsu_hospitals_v2"

def test_projection_contains_scope_and_official_source():
    corpus = HospitalCorpus.load(CORPUS)
    record = corpus.records[0]
    text = hospital_retrieval_text(corpus, record)
    evidence = hospital_evidence(corpus, record)
    hospital = next(x for x in corpus.hospitals if x.hospital_id == record.hospital_id)
    campus = next(x for x in hospital.campuses if x.campus_id == record.campus_id)
    assert hospital.official_name in text and campus.official_name in text
    assert record.content in text
    assert evidence["scope"]["hospital_id"] == hospital.hospital_id
    assert evidence["scope"]["campus_id"] == campus.campus_id
    assert evidence["source"]["url"] == record.source_url
    assert evidence["source"]["review_status"] == "verified_official"

def test_collection_name_is_manifest_bound():
    corpus = HospitalCorpus.load(CORPUS)
    assert len(corpus_fingerprint(corpus)) == 64
    assert hospital_collection_name(corpus).startswith(
        "careguide_jiangsu_hospitals_v2_")
```

- [ ] **Step 2: Verify RED**

Run: `.\.venv\Scripts\python.exe -m pytest tests\test_hospital_hybrid_retrieval.py -q`

Expected: FAIL because `careguide.hospital_corpus.retrieval` does not exist.

- [ ] **Step 3: Implement the projection contract**

```python
COLLECTION_PREFIX = "careguide_jiangsu_hospitals_v2"

def corpus_fingerprint(corpus):
    return hashlib.sha256((corpus.root / "manifest.json").read_bytes()).hexdigest()

def hospital_collection_name(corpus):
    return f"{COLLECTION_PREFIX}_{corpus_fingerprint(corpus)[:12]}"

def hospital_retrieval_text(corpus, record):
    hospital, campus = _scope(corpus, record)
    return " ".join((hospital.official_name, *hospital.aliases, hospital.city,
                     campus.official_name, campus.address,
                     record.category.value, record.title, record.content))

def hospital_evidence(corpus, record):
    hospital, campus = _scope(corpus, record)
    return {
        "id": record.document_id, "title": record.title, "content": record.content,
        "scope": {"hospital_id": hospital.hospital_id,
                  "hospital": hospital.official_name, "city": hospital.city,
                  "campus_id": campus.campus_id, "campus": campus.official_name,
                  "category": record.category.value},
        "source": {"source_id": record.source_id, "url": record.source_url,
                   "tier": record.source_tier.value,
                   "published_at": record.published_at.isoformat() if record.published_at else None,
                   "reviewed_at": record.reviewed_at.isoformat() if record.reviewed_at else None,
                   "review_status": record.review_status.value},
    }
```

- [ ] **Step 4: Verify GREEN and commit**

```powershell
.\.venv\Scripts\python.exe -m pytest tests\test_hospital_hybrid_retrieval.py -q
git add careguide/hospital_corpus/retrieval.py tests/test_hospital_hybrid_retrieval.py
git commit -m "feat: add hospital retrieval projection"
```

Expected: 2 passed.

### Task 2: Fingerprint-bound Chroma index

**Files:**
- Modify: `careguide/hospital_corpus/retrieval.py`
- Modify: `tests/test_hospital_hybrid_retrieval.py`
- Create: `scripts/index_jiangsu_hospitals.py`

- [ ] **Step 1: Write failing indexing tests**

Use a `TestEmbedding` with model `test-bge-m3`, dimension 4, and deterministic vectors. Assert that `index_hospital_collection(client, corpus, embedding, today=date(2026, 11, 13))` creates exactly 45 rows, stores the corpus fingerprint/model/dimension/cosine metadata, writes `hospital_id`, `campus_id`, `category`, `source_url`, and `review_status` on every row, and refuses an existing collection.

- [ ] **Step 2: Verify RED**

Run: `.\.venv\Scripts\python.exe -m pytest tests\test_hospital_hybrid_retrieval.py -q`

Expected: FAIL because `index_hospital_collection` is absent.

- [ ] **Step 3: Implement explicit indexing**

```python
def index_hospital_collection(client, corpus, embedding, *, today):
    report = corpus.validate(today=today)
    if not report.valid:
        raise ValueError("Hospital corpus validation failed")
    name = hospital_collection_name(corpus)
    if name in {item.name for item in client.list_collections()}:
        raise ValueError("Hospital collection already exists")
    records = [r for r in corpus.records
               if r.review_status == ReviewStatus.VERIFIED_OFFICIAL]
    vectors = embedding.embed_documents([
        hospital_retrieval_text(corpus, record) for record in records])
    if len(vectors) != len(records):
        raise ValueError("Hospital embedding count mismatch")
    collection = client.create_collection(name, metadata={
        "corpus_fingerprint": corpus_fingerprint(corpus),
        "corpus_version": corpus.manifest.version, "hnsw:space": "cosine",
        "embedding_model": embedding.model,
        "embedding_dimensions": embedding.dimensions}, embedding_function=None)
    collection.add(
        ids=[r.document_id for r in records], embeddings=vectors,
        documents=[r.content for r in records],
        metadatas=[{"record_id": r.document_id, "hospital_id": r.hospital_id,
                    "campus_id": r.campus_id, "city": r.city,
                    "category": r.category.value, "source_id": r.source_id,
                    "source_url": r.source_url,
                    "review_status": r.review_status.value} for r in records])
    return collection
```

- [ ] **Step 4: Create the safe index CLI**

`scripts/index_jiangsu_hospitals.py` accepts `--apply`, `--config-file`, `--corpus-dir`, and `--today`. Dry run loads and validates the corpus and prints `external_calls: 0`. Apply mode requires the config file, uses `semantic_embedding()`, calls `index_hospital_collection`, closes the provider, and prints only collection, count, provider, and model.

- [ ] **Step 5: Verify and commit**

```powershell
.\.venv\Scripts\python.exe scripts\index_jiangsu_hospitals.py --corpus-dir data\jiangsu_hospitals_v2 --today 2026-11-13
.\.venv\Scripts\python.exe -m pytest tests\test_hospital_hybrid_retrieval.py -q
git add careguide/hospital_corpus/retrieval.py tests/test_hospital_hybrid_retrieval.py scripts/index_jiangsu_hospitals.py
git commit -m "feat: index verified hospital corpus"
```

Expected dry-run fields: 15 hospitals, 45 records, zero external calls.

### Task 3: Scope-first hybrid retrieval and reranking

**Files:**
- Modify: `careguide/hospital_corpus/retrieval.py`
- Modify: `tests/test_hospital_hybrid_retrieval.py`

- [ ] **Step 1: Write failing scope and ranking tests**

Create a deterministic test collection and reranker. Query `怎么挂号` with hospital `js_taizhou_001`, campus `js_taizhou_001_main`, category `appointment`, and Top-2. Assert all evidence stays inside that scope, lexical and dense counts are positive, fused count is at most 20, rerank is true, and final count is at most 2. Test exact ID, official-name and alias resolution, unknown hospital behavior, and rejection of a cross-hospital Chroma row.

- [ ] **Step 2: Verify RED**

Run: `.\.venv\Scripts\python.exe -m pytest tests\test_hospital_hybrid_retrieval.py -q`

Expected: FAIL because `HospitalKnowledgeBase` is absent.

- [ ] **Step 3: Implement the hospital retrieval engine**

Add immutable `HospitalRetrievalReport` and `HospitalRetrievalResult` dataclasses and a `HospitalKnowledgeBase` that:

- validates collection name, fingerprint, model, dimension, and exact IDs;
- builds BM25 from verified records;
- resolves ID, official name, and aliases case-insensitively;
- automatically selects a campus only when the hospital has one campus;
- computes eligible IDs before both retrieval paths;
- passes eligible IDs to BM25 and hospital/campus/category filters to Chroma;
- retrieves no more than 20 results per path;
- fuses with RRF weights dense 4 and lexical 1 using denominator 60;
- sends no more than 20 candidates to rerank;
- filters rerank scores below 0.35;
- returns at most Top-5 with retrieval diagnostics attached to evidence, even when the legacy tool argument requests a larger value.

- [ ] **Step 4: Verify and commit**

```powershell
.\.venv\Scripts\python.exe -m pytest tests\test_hospital_hybrid_retrieval.py -q
git add careguide/hospital_corpus/retrieval.py tests/test_hospital_hybrid_retrieval.py
git commit -m "feat: add scoped hospital hybrid retrieval"
```

### Task 4: Runtime binding and hospital tool delegation

**Files:**
- Modify: `careguide/runtime.py`
- Modify: `careguide/tools.py`
- Create: `tests/test_hospital_runtime.py`
- Modify: `tests/test_medical_tools.py`
- Modify: `.env.example`

- [ ] **Step 1: Write failing runtime tests**

Test `build_hospital_knowledge` with fake Chroma and providers. Hybrid mode must reject a missing collection with `hospital_hybrid_index_unavailable`, reject mismatched metadata with `hospital_hybrid_index_invalid`, and return provider/model/collection status for a valid collection.

- [ ] **Step 2: Write failing tool tests**

Construct `MedicalToolRegistry(MedicalKnowledgeBase(Corpus()), hospital_knowledge=fake)`. Assert `lookup_hospital_guide` accepts a hospital name, returns `hospital_corpus_version=jiangsu-hospitals-v2`, exposes hybrid diagnostics, returns no more than five verified official evidence rows, and clamps a legacy `top_k=10` request to five. Assert unknown hospitals are `not_covered`, ambiguous campuses are `needs_clarification`, and a registry without hospital knowledge preserves existing behavior.

- [ ] **Step 3: Verify RED**

Run: `.\.venv\Scripts\python.exe -m pytest tests\test_hospital_runtime.py tests\test_medical_tools.py -q`

Expected: new tests fail while existing tool tests remain green.

- [ ] **Step 4: Implement runtime construction**

Add `HOSPITAL_CORPUS_ROOT`, `load_hospital_corpus()`, and `build_hospital_knowledge()` to `careguide/runtime.py`. Support `disabled`, `bm25`, and `hybrid`; bind hybrid mode to `hospital_collection_name(corpus)` and the existing Huawei embedding/reranker factories. Pass the optional result into `MedicalToolRegistry` from `get_registry()`.

- [ ] **Step 5: Implement safe delegation**

Update `MedicalToolRegistry.__init__` to accept `hospital_knowledge=None`. When configured, `lookup_hospital_guide` delegates to it and wraps results with the existing `result()` envelope. Map legacy sections as follows:

```python
HOSPITAL_SECTION_CATEGORIES = {
    "channels": {"appointment", "contact"},
    "process": {"appointment", "visit_preparation"},
    "materials": {"visit_preparation", "appointment"},
    "cancellation": {"appointment"},
    "followup": {"report", "contact"},
    "all": None,
}
```

Empty results say `当前官方医院资料未覆盖此问题，无法核实。` Existing general medical retrieval remains unchanged.

- [ ] **Step 6: Add explicit configuration**

```dotenv
CAREGUIDE_HOSPITAL_CORPUS_DIR=data/jiangsu_hospitals_v2
CAREGUIDE_HOSPITAL_RETRIEVAL_MODE=hybrid
CAREGUIDE_HOSPITAL_RERANK_ENABLED=true
CAREGUIDE_HOSPITAL_MIN_RERANK_SCORE=0.35
CAREGUIDE_HOSPITAL_HYBRID_CANDIDATES=20
```

- [ ] **Step 7: Verify and commit**

```powershell
.\.venv\Scripts\python.exe -m pytest tests\test_hospital_runtime.py tests\test_medical_tools.py tests\test_huawei_retrieval_runtime.py -q
git add careguide/runtime.py careguide/tools.py tests/test_hospital_runtime.py tests/test_medical_tools.py .env.example
git commit -m "feat: connect hospital retrieval to CareGuide"
```

### Task 5: Real Huawei index and frozen evaluation

**Files:**
- Create: `scripts/evaluate_jiangsu_hospital_hybrid.py`
- Modify: `data/eval/jiangsu_hospitals_v2/README.md`
- Create after passing: `data/eval/jiangsu_hospitals_v2/hybrid_huawei_result.json`

- [ ] **Step 1: Implement the explicit evaluation command**

Require `--config-file`, load the frozen corpus and 45 cases, open the fingerprinted collection, create `HospitalKnowledgeBase` with the Huawei providers, and compute Recall@1/3/5, MRR, hard-negative accuracy, and scope violations. Output provider names and collection name, never keys or headers.

- [ ] **Step 2: Document commands and acceptance thresholds**

```powershell
.\.venv\Scripts\python.exe scripts\index_jiangsu_hospitals.py --apply --config-file .env --corpus-dir data\jiangsu_hospitals_v2 --today 2026-11-13
.\.venv\Scripts\python.exe scripts\evaluate_jiangsu_hospital_hybrid.py --config-file .env --corpus-dir data\jiangsu_hospitals_v2 --cases data\eval\jiangsu_hospitals_v2\retrieval_cases.jsonl --today 2026-11-13
```

Acceptance: Recall@5 at least 0.90, MRR at least 0.75, hard-negative accuracy 1.0, scope violations 0.

- [ ] **Step 3: Build the real index once**

Run the documented index command. Expected: one fingerprinted collection containing exactly 45 records with provider `huawei-modelarts` and model `bge-m3`. If that exact verified collection exists, reuse it without deleting it or issuing another paid embedding request.

- [ ] **Step 4: Run and save the real evaluation**

Run the documented evaluation command and save the passing JSON result. Confirm no source or scope violation manually before committing.

- [ ] **Step 5: Commit evaluation tooling and artifact**

```powershell
git add scripts/evaluate_jiangsu_hospital_hybrid.py data/eval/jiangsu_hospitals_v2/README.md data/eval/jiangsu_hospitals_v2/hybrid_huawei_result.json
git commit -m "test: validate Huawei hospital hybrid retrieval"
```

Do not commit `data/chroma`, `.env`, logs, or API keys.

### Task 6: End-to-end acceptance and final verification

**Files:**
- Create: `tests/test_hospital_chat_integration.py`

- [ ] **Step 1: Write controlled end-to-end chat tests**

Use a scripted planner call to `lookup_hospital_guide`. Cover appointment materials, campus/address, and parking/accessibility. Assert every result remains in the selected hospital, includes an official URL, reports hybrid retrieval, has no side effects, and returns `not_covered` rather than invented facts when evidence is absent.

- [ ] **Step 2: Run focused tests**

Run: `.\.venv\Scripts\python.exe -m pytest tests\test_hospital_chat_integration.py tests\test_hospital_runtime.py tests\test_hospital_hybrid_retrieval.py -q`

Expected: all pass without paid network calls.

- [ ] **Step 3: Run three manual real-provider queries**

1. `泰州市人民医院怎么挂号，需要带什么？`
2. `行动不便去泰州市人民医院，院区地址和无障碍信息有哪些？`
3. `泰州市人民医院停车信息有官方资料吗？`

Expected: official citations, strict hospital scope, and explicit non-coverage where applicable.

- [ ] **Step 4: Run the complete suite**

Run: `.\.venv\Scripts\python.exe -m pytest -q`

Expected: all tests pass; only the two known dependency deprecation warnings are allowed.

- [ ] **Step 5: Audit secrets and artifacts**

```powershell
git status --short
git diff --check
git grep -n -E "(sk-[A-Za-z0-9]{16,}|CAREGUIDE_.*API_KEY=.+)" -- ':!.env.example'
```

Expected: no secrets, `.env` remains ignored, `data/chroma` is not staged, and unrelated user changes remain untouched.

- [ ] **Step 6: Commit acceptance tests**

```powershell
git add tests/test_hospital_chat_integration.py
git commit -m "test: cover hospital guide end to end"
```

Final evidence reports collection name/count, evaluation metrics, three query outcomes, total passing tests, and remaining dependency warnings.
