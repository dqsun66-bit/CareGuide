# Jiangsu Hospital Official Corpus Pilot Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a validated 15-hospital registry and a working official-source corpus pilot for the three selected Taizhou hospitals, including deterministic cleaning, chunking, deduplication, provenance checks, and a reproducible BM25 retrieval baseline.

**Architecture:** Add an isolated `careguide.hospital_corpus` package for data contracts and deterministic processing instead of widening the existing clinical corpus loader immediately. Build immutable versioned JSON assets from reviewed local source snapshots, validate every record against an official source and hospital/campus scope, then evaluate the published records with the existing `BM25Index`. The remaining 12 hospitals and Huawei Embedding/Chroma/Rerank are separate follow-up plans after this pilot passes.

**Tech Stack:** Python 3.11, Pydantic 2, httpx, standard-library HTML parsing and hashing, pytest, existing CareGuide `BM25Index`.

---

## File map

- Create `careguide/hospital_corpus/__init__.py`: public corpus-pilot interfaces.
- Create `careguide/hospital_corpus/models.py`: strict hospital, campus, source, record, verification and manifest models.
- Create `careguide/hospital_corpus/normalize.py`: URL, whitespace, text and stable identifier normalization.
- Create `careguide/hospital_corpus/chunking.py`: heading-aware deterministic semantic chunking.
- Create `careguide/hospital_corpus/dedup.py`: exact and near-duplicate grouping without model calls.
- Create `careguide/hospital_corpus/store.py`: load, validate, fingerprint and atomically write versioned corpus assets.
- Create `careguide/hospital_corpus/snapshot.py`: bounded official-domain snapshot fetcher and hash metadata.
- Create `careguide/hospital_corpus/evaluation.py`: BM25 case runner and retrieval metrics.
- Create `scripts/build_jiangsu_hospital_pilot.py`: explicit dry-run/apply builder for reviewed assets.
- Create `scripts/fetch_hospital_sources.py`: explicit source snapshot command with domain allowlist.
- Create `scripts/validate_jiangsu_hospital_corpus.py`: schema, provenance, duplication and expiry validation CLI.
- Create `scripts/evaluate_jiangsu_hospital_bm25.py`: deterministic pilot retrieval evaluation CLI.
- Create `data/jiangsu_hospitals_v1/README.md`: usage, review states, safety boundary and update process.
- Create `data/jiangsu_hospitals_v1/hospitals.json`: all 15 approved hospital identities; only verified official domains and campuses.
- Create `data/jiangsu_hospitals_v1/sources.json`: reviewed official sources for the Taizhou pilot.
- Create `data/jiangsu_hospitals_v1/records.json`: published `verified_official` pilot chunks.
- Create `data/jiangsu_hospitals_v1/verification.json`: conflicts, excluded candidates and reviewer decisions.
- Create `data/jiangsu_hospitals_v1/manifest.json`: version, stage, counts and file hashes.
- Create `data/eval/jiangsu_hospitals_v1/retrieval_cases.jsonl`: at least 15 pilot cases, five per Taizhou hospital.
- Create `data/eval/jiangsu_hospitals_v1/README.md`: case format and baseline interpretation.
- Create `tests/fixtures/hospital_corpus/`: small synthetic official-page fixtures; no live network tests.
- Create `tests/test_hospital_corpus_models.py`: strict schema and scope tests.
- Create `tests/test_hospital_corpus_processing.py`: normalization, chunking and deduplication tests.
- Create `tests/test_hospital_corpus_store.py`: integrity, provenance and atomic-build tests.
- Create `tests/test_hospital_snapshot.py`: allowlist, redirect, size and secret-safety tests.
- Create `tests/test_jiangsu_hospital_pilot.py`: real pilot asset validation.
- Create `tests/test_jiangsu_hospital_evaluation.py`: BM25 baseline and alias/campus tests.

## Scope boundary

This plan ends when the Taizhou pilot is validated and has a reproducible BM25 baseline. It does not change the active chat runtime, call paid models, create a Chroma collection, integrate AgentArts, or claim that all 15 hospitals have full content. The approved 15-hospital registry is complete in this plan; content coverage is explicitly marked `pilot_taizhou_3_of_15` until the next expansion plan.

### Task 1: Define strict hospital-corpus contracts

**Files:**
- Create: `careguide/hospital_corpus/__init__.py`
- Create: `careguide/hospital_corpus/models.py`
- Create: `tests/test_hospital_corpus_models.py`

- [ ] **Step 1: Write failing model tests**

Create tests for these exact invariants:

```python
from datetime import date, datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from careguide.hospital_corpus.models import Campus, Hospital, KnowledgeRecord, Source


def test_hospital_requires_unique_campus_identity_and_grade_source():
    hospital = Hospital(
        hospital_id="js_taizhou_003",
        official_name="泰州市第二人民医院",
        aliases=("泰州二院", "姜堰人民医院"),
        city="泰州",
        hospital_type="general",
        grade="三级甲等",
        grade_source_id="src_tz2_grade_2025",
        official_domains=("www.jyry.com",),
        campuses=(Campus(
            campus_id="js_taizhou_003_main",
            official_name="主院区",
            address="江苏省泰州市姜堰区健康路27号",
            address_source_id="src_tz2_contact",
        ),),
    )
    assert hospital.campuses[0].campus_id.endswith("_main")


def test_record_cannot_publish_unreviewed_source():
    with pytest.raises(ValidationError, match="verified_official"):
        KnowledgeRecord(
            document_id="js_taizhou_003_appointment_0001",
            hospital_id="js_taizhou_003",
            campus_id="js_taizhou_003_main",
            city="泰州",
            category="appointment",
            title="预约挂号",
            content="官方页面说明患者可以通过医院服务渠道预约挂号。",
            source_id="src_tz2_appointment",
            source_url="https://www.jyry.com/",
            source_tier="hospital_official",
            published_at=None,
            collected_at=datetime(2026, 11, 4, tzinfo=timezone(timedelta(hours=8))),
            reviewed_at=None,
            review_status="needs_review",
            content_hash="0" * 64,
        )
```

Also test duplicate aliases, duplicate campus IDs, a campus ID belonging to another hospital, HTTP source URLs, credentials in URLs, unknown categories, naive datetimes, invalid SHA-256 values, and `published_at` later than `collected_at`.

- [ ] **Step 2: Run the tests and confirm RED**

Run:

```powershell
D:\codex\CareGuide\.venv\Scripts\python.exe -m pytest -q tests/test_hospital_corpus_models.py
```

Expected: test collection fails because `careguide.hospital_corpus.models` does not exist.

- [ ] **Step 3: Implement the strict Pydantic models**

Use frozen, extra-forbidden models and these exact enums:

```python
class HospitalType(str, Enum):
    GENERAL = "general"
    TCM_GENERAL = "tcm_general"
    CHILDREN = "children"


class SourceTier(str, Enum):
    HOSPITAL_OFFICIAL = "hospital_official"
    HEALTH_AUTHORITY = "health_authority"
    GOVERNMENT = "government"
    OFFICIAL_APPOINTMENT = "official_appointment"
    MAP_PROVIDER = "map_provider"


class ReviewStatus(str, Enum):
    VERIFIED_OFFICIAL = "verified_official"
    NEEDS_REVIEW = "needs_review"
    EXPIRED = "expired"
    CONFLICT = "conflict"


class Category(str, Enum):
    IDENTITY = "identity"
    CAMPUS = "campus"
    APPOINTMENT = "appointment"
    DEPARTMENT = "department"
    VISIT_PREPARATION = "visit_preparation"
    REPORT = "report"
    TRANSPORT = "transport"
    PARKING = "parking"
    ACCESSIBILITY = "accessibility"
    EMERGENCY = "emergency"
    CONTACT = "contact"
```

`Hospital` must reject duplicate aliases/domains/campus IDs. `KnowledgeRecord` must require `review_status=verified_official`, a non-empty `reviewed_at`, matching hospital/campus prefixes, HTTPS source URL, 64-character lowercase content hash and 50–1000 characters of content. `Source` must preserve `published_at=None` when no publication date exists and must store snapshot path, HTTP status, content type, retrieved hash, collected time and reviewed time.

- [ ] **Step 4: Run GREEN and existing corpus regression**

Run:

```powershell
D:\codex\CareGuide\.venv\Scripts\python.exe -m pytest -q `
  tests/test_hospital_corpus_models.py tests/test_foundation.py tests/test_zhongda_corpus.py
```

Expected: all selected tests pass; existing corpus types remain unchanged.

- [ ] **Step 5: Commit**

```powershell
git add careguide/hospital_corpus/__init__.py careguide/hospital_corpus/models.py `
  tests/test_hospital_corpus_models.py
git commit -m "feat: define official hospital corpus contracts"
```

### Task 2: Implement deterministic normalization, chunking and deduplication

**Files:**
- Create: `careguide/hospital_corpus/normalize.py`
- Create: `careguide/hospital_corpus/chunking.py`
- Create: `careguide/hospital_corpus/dedup.py`
- Create: `tests/test_hospital_corpus_processing.py`

- [ ] **Step 1: Write failing processing tests**

```python
from careguide.hospital_corpus.chunking import chunk_sections
from careguide.hospital_corpus.dedup import duplicate_groups
from careguide.hospital_corpus.normalize import canonical_url, normalize_text


def test_url_normalization_removes_tracking_but_keeps_meaningful_query():
    url = "https://www.jyry.com/guide?id=12&utm_source=wx#share"
    assert canonical_url(url) == "https://www.jyry.com/guide?id=12"


def test_chunker_keeps_numbered_appointment_steps_together():
    sections = [("预约流程", "1. 实名注册。\n2. 选择科室。\n3. 确认预约。")]
    chunks = chunk_sections(sections, target_min=20, target_max=120, overlap=10)
    assert len(chunks) == 1
    assert "1. 实名注册" in chunks[0].content
    assert "3. 确认预约" in chunks[0].content


def test_near_duplicate_group_ignores_navigation_and_update_stamp():
    rows = [
        ("a", "首页 就医指南 预约须实名办理 更新时间2026-10-01"),
        ("b", "首页 就医指南 预约须实名办理 更新时间2026-11-01"),
    ]
    assert duplicate_groups(rows, threshold=0.90) == (("a", "b"),)
```

Add cases that prove chunks never cross hospital/campus/category boundaries, lists are not split mid-list, exact duplicate hashes group together, unrelated short text does not group, and normalization is idempotent.

- [ ] **Step 2: Run RED**

Run: `D:\codex\CareGuide\.venv\Scripts\python.exe -m pytest -q tests/test_hospital_corpus_processing.py`

Expected: imports fail for the three missing modules.

- [ ] **Step 3: Implement normalization and stable identifiers**

`canonical_url()` must lowercase only the hostname, require HTTPS, remove credentials/default ports/fragments and drop only `utm_*`, `spm`, `from`, `share_token` parameters. Sort remaining query pairs. `normalize_text()` must apply Unicode NFKC, convert CRLF to LF, collapse horizontal whitespace, cap blank lines at one and remove known navigation-only lines from an explicit set.

Expose:

```python
def stable_id(*parts: str) -> str:
    normalized = "|".join(normalize_text(part).casefold() for part in parts)
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:16]
```

- [ ] **Step 4: Implement deterministic chunking and deduplication**

`chunk_sections()` accepts already scoped `Section` objects, prefers heading/paragraph/list boundaries, targets 250–600 Chinese characters in production, supports 50–80 characters of overlap and returns `Chunk(content, heading, ordinal)`. It must raise if one input batch mixes scope IDs.

`duplicate_groups()` first groups identical normalized SHA-256 values, then compares character 3-gram Jaccard similarity for near duplicates. Sort all IDs and groups to make output reproducible. Do not use embeddings or an LLM.

- [ ] **Step 5: Run GREEN**

Run:

```powershell
D:\codex\CareGuide\.venv\Scripts\python.exe -m pytest -q `
  tests/test_hospital_corpus_processing.py tests/test_semantic_retrieval.py
```

- [ ] **Step 6: Commit**

```powershell
git add careguide/hospital_corpus/normalize.py careguide/hospital_corpus/chunking.py `
  careguide/hospital_corpus/dedup.py tests/test_hospital_corpus_processing.py
git commit -m "feat: add deterministic hospital corpus processing"
```

### Task 3: Build the integrity store and validation CLI

**Files:**
- Create: `careguide/hospital_corpus/store.py`
- Create: `scripts/validate_jiangsu_hospital_corpus.py`
- Create: `tests/test_hospital_corpus_store.py`

- [ ] **Step 1: Write failing store tests**

Create a temporary corpus with one hospital, two sources and one record. Assert that:

```python
corpus = HospitalCorpus.load(tmp_path)
assert corpus.manifest.version == "jiangsu-hospitals-v1"
assert corpus.record("doc-1").source_id == "source-1"
assert corpus.validate(today=date(2026, 11, 4)).errors == ()
```

Add explicit failures for an unknown source ID, unknown campus ID, mismatched file hash, duplicate document ID, missing snapshot, unapproved source domain, expired source, record/source URL mismatch, a published record backed by `needs_review`, and manifest counts that disagree.

- [ ] **Step 2: Run RED**

Run: `D:\codex\CareGuide\.venv\Scripts\python.exe -m pytest -q tests/test_hospital_corpus_store.py`

- [ ] **Step 3: Implement immutable load and validation**

`HospitalCorpus.load(root)` reads `hospitals.json`, `sources.json`, `records.json`, `verification.json`, then validates `manifest.json` hashes and counts. Build indexes by ID and expose `validate(today) -> ValidationReport` with stable `errors`, `warnings`, `expired_source_ids`, `conflict_ids` and `duplicate_groups` fields.

Domain rules:

```python
GOVERNMENT_SUFFIXES = (
    ".gov.cn",
    "wjw.jiangsu.gov.cn",
    "wjw.taizhou.gov.cn",
)


def source_host_allowed(source, hospital) -> bool:
    host = urlparse(source.url).hostname or ""
    return host in hospital.official_domains or any(
        host == suffix.removeprefix(".") or host.endswith(suffix)
        for suffix in GOVERNMENT_SUFFIXES
    )
```

Do not treat map domains as authoritative for grades, appointment rules, contacts or campuses.

- [ ] **Step 4: Implement atomic writer and CLI**

Write files into a sibling temporary directory, calculate SHA-256 after final newline normalization, then rename only after all validation passes. `validate_jiangsu_hospital_corpus.py --corpus-dir tests/fixtures/hospital_corpus/valid --today 2026-11-04` prints JSON containing only counts, IDs and status; it never prints record bodies, cookies or environment values.

- [ ] **Step 5: Run GREEN and CLI smoke test**

Run:

```powershell
D:\codex\CareGuide\.venv\Scripts\python.exe -m pytest -q tests/test_hospital_corpus_store.py
D:\codex\CareGuide\.venv\Scripts\python.exe scripts/validate_jiangsu_hospital_corpus.py `
  --corpus-dir tests/fixtures/hospital_corpus/valid --today 2026-11-04
```

Expected CLI status: `valid`, with no record body in stdout.

- [ ] **Step 6: Commit**

```powershell
git add careguide/hospital_corpus/store.py scripts/validate_jiangsu_hospital_corpus.py `
  tests/test_hospital_corpus_store.py tests/fixtures/hospital_corpus
git commit -m "feat: validate versioned hospital corpus integrity"
```

### Task 4: Add a safe official-source snapshot command

**Files:**
- Create: `careguide/hospital_corpus/snapshot.py`
- Create: `scripts/fetch_hospital_sources.py`
- Create: `tests/test_hospital_snapshot.py`
- Modify: `.gitignore`

- [ ] **Step 1: Write failing HTTP safety tests**

Use `httpx.MockTransport` and assert:

```python
result = fetch_snapshot(
    "https://www.jyry.com/guide/appointment.html",
    allowed_hosts={"www.jyry.com"},
    client=client,
    max_bytes=2_000_000,
)
assert result.status_code == 200
assert result.sha256 == hashlib.sha256(result.body).hexdigest()
assert result.content_type.startswith("text/html")
```

Reject HTTP, credentials in URL, IP literals, localhost, non-allowlisted hosts, redirects to a different host, more than three redirects, bodies over the limit, unsupported content types, `Set-Cookie` persistence and URLs containing token/key/password query names. Assert raised messages never contain an Authorization value.

- [ ] **Step 2: Run RED**

Run: `D:\codex\CareGuide\.venv\Scripts\python.exe -m pytest -q tests/test_hospital_snapshot.py`

- [ ] **Step 3: Implement bounded fetching**

Allow only `text/html`, `application/pdf`, `text/plain` and UTF-8 JSON published as a document. Use a fixed public User-Agent, 10-second connect timeout, 20-second read timeout, no cookies, no proxy environment inheritance, maximum 2 MB per HTML page and 15 MB per PDF. Save response body exactly as received and write a sibling metadata JSON containing canonical URL, status, content type, collected time and SHA-256.

- [ ] **Step 4: Implement explicit CLI behavior**

The CLI accepts `--registry`, `--source-id`, `--output-dir` and `--apply`. Without `--apply`, print the canonical URL and target path with `external_calls=0`. With `--apply`, load the expected URL and allowed domain from the reviewed registry; never accept an arbitrary URL argument.

Add only temporary download suffixes to `.gitignore`; final reviewed snapshots remain versioned corpus evidence.

- [ ] **Step 5: Run GREEN**

Run: `D:\codex\CareGuide\.venv\Scripts\python.exe -m pytest -q tests/test_hospital_snapshot.py`

- [ ] **Step 6: Commit**

```powershell
git add careguide/hospital_corpus/snapshot.py scripts/fetch_hospital_sources.py `
  tests/test_hospital_snapshot.py .gitignore
git commit -m "feat: fetch allowlisted official source snapshots"
```

### Task 5: Register all 15 hospitals and build the Taizhou pilot assets

**Files:**
- Create: `scripts/build_jiangsu_hospital_pilot.py`
- Create: `data/jiangsu_hospitals_v1/README.md`
- Create: `data/jiangsu_hospitals_v1/hospitals.json`
- Create: `data/jiangsu_hospitals_v1/sources.json`
- Create: `data/jiangsu_hospitals_v1/records.json`
- Create: `data/jiangsu_hospitals_v1/verification.json`
- Create: `data/jiangsu_hospitals_v1/manifest.json`
- Create: `data/jiangsu_hospitals_v1/snapshots/`
- Create: `tests/test_jiangsu_hospital_pilot.py`

- [ ] **Step 1: Write failing real-asset tests**

```python
from datetime import date
from pathlib import Path

from careguide.hospital_corpus.store import HospitalCorpus


ROOT = Path(__file__).resolve().parents[1] / "data" / "jiangsu_hospitals_v1"


def test_registry_contains_exact_approved_hospitals():
    corpus = HospitalCorpus.load(ROOT)
    assert len(corpus.hospitals) == 15
    assert {h.city for h in corpus.hospitals} == {
        "泰州", "南京", "苏州", "无锡", "常州", "南通", "扬州"
    }
    assert {h.hospital_id for h in corpus.hospitals if h.city == "泰州"} == {
        "js_taizhou_001", "js_taizhou_002", "js_taizhou_003"
    }


def test_pilot_publishes_only_three_taizhou_hospitals():
    corpus = HospitalCorpus.load(ROOT)
    assert corpus.manifest.stage == "pilot_taizhou_3_of_15"
    assert {record.hospital_id for record in corpus.records} == {
        "js_taizhou_001", "js_taizhou_002", "js_taizhou_003"
    }
    assert corpus.validate(today=date(2026, 11, 4)).errors == ()
```

Also assert at least two distinct official or government sources per Taizhou hospital, every source has a matching snapshot/hash, every record is `verified_official`, no source comes from a commercial health platform, and every hospital grade and campus address has an evidence source.

- [ ] **Step 2: Run RED**

Run: `D:\codex\CareGuide\.venv\Scripts\python.exe -m pytest -q tests/test_jiangsu_hospital_pilot.py`

Expected: the corpus directory does not exist.

- [ ] **Step 3: Register the 15 approved identities**

Use these IDs exactly:

```text
js_taizhou_001  南京医科大学附属泰州人民医院
js_taizhou_002  泰州市中医院
js_taizhou_003  泰州市第二人民医院
js_nanjing_001  江苏省人民医院
js_nanjing_002  南京鼓楼医院
js_nanjing_003  南京市儿童医院
js_nanjing_004  江苏省中医院
js_suzhou_001   苏州大学附属第一医院
js_suzhou_002   苏州大学附属第二医院
js_suzhou_003   苏州大学附属儿童医院
js_wuxi_001     无锡市人民医院
js_wuxi_002     无锡市中医医院
js_changzhou_001 常州市第一人民医院
js_nantong_001  南通大学附属医院
js_yangzhou_001 江苏省苏北人民医院
```

For every official domain, grade and campus address, verify the live page against a hospital or government source before writing the field. If a domain or campus cannot be verified, omit it and add a `needs_review` entry to `verification.json`; never infer it from a map or search-result snippet. The build must fail if a pilot hospital is missing these required fields.

- [ ] **Step 4: Collect and review Taizhou sources**

Process hospitals in this order: `js_taizhou_001`, `js_taizhou_002`, `js_taizhou_003`. For each, register and snapshot official evidence for identity/grade, campus/contact, appointment or outpatient guidance, and at least one transport/parking/accessibility page when officially available.

Use these already verified authority anchors as the starting set:

```text
https://wjw.taizhou.gov.cn/xwzx/gzdt/art/2025/art_f3c495e73d244097a6a78a4e7195906a.html
https://tzhh.taizhou.gov.cn/xxgk/ggtz/art/2024/art_afc11b41f6334450829964f941b9d4b1.html
https://www.jyry.com/
https://wjw.taizhou.gov.cn/xwzx/gzdt/art/2025/art_d97934433e2d40a59f30ffaaae63db3b.html
```

Official pages discovered later must pass the same allowlist and review rules. Do not import `bohe.cn`, `dayi.org.cn`, encyclopedias, commercial registration sites or search snippets.

- [ ] **Step 5: Curate records and verification decisions**

Create 250–600-character scoped records when source material is long enough. Keep short contact/address facts intact rather than padding them. Record excluded stale pages and conflicts in `verification.json` with source IDs, reason, reviewer decision and decision time. The builder must compute IDs, hashes, counts and manifest fingerprints; hand-edited manifest hashes are forbidden.

- [ ] **Step 6: Run the builder in dry-run and apply modes**

```powershell
D:\codex\CareGuide\.venv\Scripts\python.exe scripts/build_jiangsu_hospital_pilot.py `
  --source-dir data/jiangsu_hospitals_v1/snapshots
D:\codex\CareGuide\.venv\Scripts\python.exe scripts/build_jiangsu_hospital_pilot.py `
  --source-dir data/jiangsu_hospitals_v1/snapshots --apply
```

Dry-run expected: planned counts, `external_calls=0`, no file changes. Apply expected: all JSON files written atomically and validation status `valid`.

- [ ] **Step 7: Run GREEN**

```powershell
D:\codex\CareGuide\.venv\Scripts\python.exe -m pytest -q tests/test_jiangsu_hospital_pilot.py
D:\codex\CareGuide\.venv\Scripts\python.exe scripts/validate_jiangsu_hospital_corpus.py `
  --corpus-dir data/jiangsu_hospitals_v1 --today 2026-11-04
```

- [ ] **Step 8: Commit**

```powershell
git add scripts/build_jiangsu_hospital_pilot.py data/jiangsu_hospitals_v1 `
  tests/test_jiangsu_hospital_pilot.py
git commit -m "data: add verified Taizhou hospital corpus pilot"
```

### Task 6: Add the reproducible BM25 baseline

**Files:**
- Create: `careguide/hospital_corpus/evaluation.py`
- Create: `scripts/evaluate_jiangsu_hospital_bm25.py`
- Create: `data/eval/jiangsu_hospitals_v1/retrieval_cases.jsonl`
- Create: `data/eval/jiangsu_hospitals_v1/README.md`
- Create: `tests/test_jiangsu_hospital_evaluation.py`

- [ ] **Step 1: Write failing evaluator tests**

```python
def test_alias_query_retrieves_taizhou_second_people_hospital(pilot_corpus):
    report = evaluate_cases(pilot_corpus, [{
        "case_id": "tz2-alias-appointment",
        "query": "姜堰人民医院怎么预约挂号",
        "relevant_ids": ["js_taizhou_003_appointment_0001"],
        "hospital_id": "js_taizhou_003",
        "category": "appointment",
    }], top_k=5)
    assert report.recall_at_5 == 1.0
    assert report.scope_violations == 0


def test_unknown_parking_rule_returns_no_official_evidence(pilot_corpus):
    report = evaluate_cases(pilot_corpus, [{
        "case_id": "tz2-live-parking-negative",
        "query": "泰州二院现在还有几个空车位",
        "relevant_ids": [],
        "hard_negative": True,
        "hospital_id": "js_taizhou_003",
        "category": "parking",
    }], top_k=5)
    assert report.hard_negative_accuracy == 1.0
```

Add alias, exact hospital, appointment, campus/contact and transport cases for each pilot hospital. Include hard negatives for live parking spaces, doctor schedules, diagnoses and treatment advice.

- [ ] **Step 2: Run RED**

Run: `D:\codex\CareGuide\.venv\Scripts\python.exe -m pytest -q tests/test_jiangsu_hospital_evaluation.py`

- [ ] **Step 3: Implement BM25-only scoped evaluation**

Build retrieval text from official name, aliases, campus, category, title and content. Use existing `BM25Index`; filter eligible records by declared hospital/category before scoring. Return `recall_at_1`, `recall_at_3`, `recall_at_5`, `mrr`, `hard_negative_accuracy`, `scope_violations` and per-case IDs. Do not generate natural-language answers or call a model.

- [ ] **Step 4: Add at least 15 reviewed cases**

Create at least five cases per pilot hospital, including one alias query and one hard negative per hospital. Every positive ID must exist in `records.json`; every negative must declare `hard_negative=true`. Freeze the case-file SHA-256 in its README after review.

- [ ] **Step 5: Run GREEN and save the baseline report**

```powershell
D:\codex\CareGuide\.venv\Scripts\python.exe -m pytest -q tests/test_jiangsu_hospital_evaluation.py
D:\codex\CareGuide\.venv\Scripts\python.exe scripts/evaluate_jiangsu_hospital_bm25.py `
  --corpus-dir data/jiangsu_hospitals_v1 `
  --cases data/eval/jiangsu_hospitals_v1/retrieval_cases.jsonl `
  --output data/eval/jiangsu_hospitals_v1/bm25_baseline.json
```

Pilot acceptance thresholds: Recall@5 at least `0.90`, MRR at least `0.75`, hard-negative accuracy `1.00`, scope violations `0`.

- [ ] **Step 6: Commit**

```powershell
git add careguide/hospital_corpus/evaluation.py scripts/evaluate_jiangsu_hospital_bm25.py `
  data/eval/jiangsu_hospitals_v1 tests/test_jiangsu_hospital_evaluation.py
git commit -m "test: add Taizhou hospital BM25 baseline"
```

### Task 7: Pilot verification and handoff

**Files:**
- Modify only files whose failing checks prove a defect.

- [ ] **Step 1: Run focused backend tests**

```powershell
D:\codex\CareGuide\.venv\Scripts\python.exe -m pytest -q `
  tests/test_hospital_corpus_models.py `
  tests/test_hospital_corpus_processing.py `
  tests/test_hospital_corpus_store.py `
  tests/test_hospital_snapshot.py `
  tests/test_jiangsu_hospital_pilot.py `
  tests/test_jiangsu_hospital_evaluation.py
```

Expected: zero failures, zero live network calls from tests and zero paid-model calls.

- [ ] **Step 2: Validate the real pilot assets**

```powershell
D:\codex\CareGuide\.venv\Scripts\python.exe scripts/validate_jiangsu_hospital_corpus.py `
  --corpus-dir data/jiangsu_hospitals_v1 --today 2026-11-04
D:\codex\CareGuide\.venv\Scripts\python.exe scripts/evaluate_jiangsu_hospital_bm25.py `
  --corpus-dir data/jiangsu_hospitals_v1 `
  --cases data/eval/jiangsu_hospitals_v1/retrieval_cases.jsonl
```

Expected: corpus status `valid`; evaluation meets all Task 6 thresholds.

- [ ] **Step 3: Run broad regression**

```powershell
D:\codex\CareGuide\.venv\Scripts\python.exe -m pytest -q
```

Do not overwrite or revert the user's existing modified files to make tests pass. Classify unrelated failures separately.

- [ ] **Step 4: Run integrity and secret checks**

```powershell
git diff --check
git status --short
rg -n "(Authorization:|Bearer [A-Za-z0-9_-]{12,}|api[_-]?key[=:][^$])" `
  careguide/hospital_corpus scripts data/jiangsu_hospitals_v1 tests
```

Confirm there are no API keys, cookies, login tokens, patient data, unreviewed third-party medical claims or raw search-result summaries in tracked assets.

- [ ] **Step 5: Review completion against the pilot definition**

Only claim the pilot complete when all 15 identities are registered, the three Taizhou hospitals have validated official records, every published record is traceable, no exact duplicates remain and the BM25 thresholds pass. Do not claim the remaining 12 hospitals, Huawei vector index or AgentArts integration complete.

- [ ] **Step 6: Commit verification-only corrections if needed**

If verification reveals a defect, return to the task that owns the failing file, repeat its focused test and use that task's exact `git add` list before committing `fix: close hospital corpus pilot verification gaps`. If no corrections are needed, do not create an empty commit.

## Follow-up plans after this pilot

1. `jiangsu-hospital-corpus-expansion`: apply the validated pipeline to the remaining 12 hospitals and reach 60–100 sources plus 300–600 records.
2. `huawei-hospital-rag-index`: generate Huawei embeddings, build the versioned Chroma collection and run Huawei Rerank against the frozen evaluation set.
3. `agentarts-careguide-tools`: expose the verified search, hospital, parking and route tools through public HTTPS endpoints and orchestrate them in AgentArts.
