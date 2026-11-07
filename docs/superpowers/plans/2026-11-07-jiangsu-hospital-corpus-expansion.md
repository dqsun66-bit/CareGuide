# Jiangsu Hospital Corpus Expansion Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Extend the reviewed Taizhou pilot into an immutable `jiangsu-hospitals-v2` corpus containing 15 hospitals and exactly three core published records per hospital, while leaving every committed v1 byte unchanged and making every new fact independently reviewable.

**Architecture:** Keep `data/jiangsu_hospitals_v1` frozen and reuse its 29 approved sources for identity, grade and campus evidence. Add one reviewed operational source per remaining hospital when official evidence is available, create 36 new identity/campus/service records, and combine them with the nine v1 records through an offline, atomic v2 builder. If a hospital lacks official evidence for an appointment, contact or visit-preparation service record, stop that city batch and record the gap instead of publishing or fabricating a substitute. Extend the corpus contracts for v2 and multi-source evidence, then freeze an evaluation set with at least 45 cases.

**Tech Stack:** Python 3.11, Pydantic v2, pytest, existing `HospitalCorpus`, `BM25Index`, safe snapshot fetcher, JSON/JSONL immutable assets.

---

## Scope and fixed IDs

The expansion publishes exactly three new records for each non-Taizhou hospital:

| Hospital ID | Required records | Preferred service category |
|---|---|---|
| `js_nanjing_001` | identity, campus, service | appointment, otherwise contact |
| `js_nanjing_002` | identity, campus, service | appointment, otherwise contact |
| `js_nanjing_003` | identity, campus, service | appointment, otherwise contact |
| `js_nanjing_004` | identity, campus, service | appointment, otherwise contact |
| `js_suzhou_001` | identity, campus, service | appointment, otherwise contact |
| `js_suzhou_002` | identity, campus, service | appointment, otherwise contact |
| `js_suzhou_003` | identity, campus, service | appointment, otherwise contact |
| `js_wuxi_001` | identity, campus, service | appointment, otherwise contact |
| `js_wuxi_002` | identity, campus, service | appointment, otherwise contact |
| `js_changzhou_001` | identity, campus, service | appointment, otherwise contact |
| `js_nantong_001` | identity, campus, service | appointment, otherwise contact |
| `js_yangzhou_001` | identity, campus, service | appointment, otherwise contact |

The final v2 corpus therefore contains 45 records: nine unchanged Taizhou records plus 36 new records. Extra fourth or fifth records are deliberately excluded from this implementation plan; they can be added in a later reviewed version without weakening this acceptance boundary.

## File map

- Modify `careguide/hospital_corpus/models.py`: accept v1/v2 manifests and record multiple evidence sources.
- Modify `careguide/hospital_corpus/store.py`: validate v2 manifests, supporting sources and multiple snapshot roots.
- Create `careguide/hospital_corpus/review.py`: generic review-manifest loading and evidence-term verification.
- Modify `scripts/build_jiangsu_hospital_pilot.py`: call the shared review helper while leaving the committed v1 assets untouched and retaining semantic compatibility for temporary rebuilds.
- Create `scripts/build_jiangsu_hospital_expansion.py`: combine frozen v1 assets with reviewed v2 additions offline.
- Reuse `scripts/fetch_hospital_sources.py`: collect one reviewed operational source at a time; do not add networking to either builder.
- Create `data/jiangsu_hospital_sources_v2/`: additional sources, snapshots, review manifest and README.
- Create `data/jiangsu_hospitals_v2/`: immutable published v2 corpus.
- Create `tests/test_hospital_corpus_v2_contract.py`: v2 and multi-source contract tests.
- Create `tests/test_jiangsu_hospital_expansion.py`: asset, review and per-hospital coverage tests.
- Create `data/eval/jiangsu_hospitals_v2/`: at least 45 reviewed cases and baseline.
- Create `tests/test_jiangsu_hospital_v2_evaluation.py`: v2 evaluation and hash tests.

---

### Task 1: Add backward-compatible v2 and multi-source contracts

**Files:**
- Modify: `careguide/hospital_corpus/models.py`
- Modify: `careguide/hospital_corpus/store.py`
- Create: `tests/test_hospital_corpus_v2_contract.py`

- [ ] **Step 1: Write failing v2 manifest and multi-source tests**

```python
from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from careguide.hospital_corpus.models import CorpusManifest, KnowledgeRecord


def test_manifest_accepts_expanded_v2_stage():
    value = CorpusManifest(
        version="jiangsu-hospitals-v2",
        stage="expanded_core_15_of_15",
        built_at=datetime(2026, 11, 7, tzinfo=timezone.utc),
        hospital_count=15,
        source_count=41,
        record_count=45,
        verification_count=98,
        files={name: "0" * 64 for name in (
            "hospitals.json", "sources.json", "records.json", "verification.json"
        )},
    )
    assert value.version == "jiangsu-hospitals-v2"


def test_record_rejects_duplicate_primary_supporting_source(valid_record):
    with pytest.raises(ValidationError, match="supporting source"):
        KnowledgeRecord.model_validate({
            **valid_record,
            "supporting_source_ids": [valid_record["source_id"]],
        })
```

- [ ] **Step 2: Run RED**

Run:

```powershell
D:\codex\CareGuide\.venv\Scripts\python.exe -m pytest -q tests/test_hospital_corpus_v2_contract.py
```

Expected: FAIL because v2 literals and `supporting_source_ids` do not exist.

- [ ] **Step 3: Extend immutable models**

In `careguide/hospital_corpus/models.py`, add the field and validation:

```python
class KnowledgeRecord(StrictModel):
    # existing fields stay unchanged
    supporting_source_ids: tuple[str, ...] = ()

    @model_validator(mode="after")
    def validate_publication(self):
        # retain all existing checks
        if self.source_id in self.supporting_source_ids:
            raise ValueError("primary source cannot be repeated as a supporting source")
        if len(set(self.supporting_source_ids)) != len(self.supporting_source_ids):
            raise ValueError("duplicate supporting source")
        return self


class CorpusManifest(StrictModel):
    version: Literal["jiangsu-hospitals-v1", "jiangsu-hospitals-v2"]
    stage: Literal["pilot_taizhou_3_of_15", "expanded_core_15_of_15"]
```

- [ ] **Step 4: Validate every supporting source**

In the record loop in `HospitalCorpus.validate()`, validate each source ID in `(record.source_id, *record.supporting_source_ids)`. Each supporting source must exist, belong to the same hospital, be `verified_official`, not be a map source, and use an authoritative tier for the fact it supports. `official_appointment` may only support appointment records.

Use this exact error prefix so tests can assert it:

```python
for supporting_id in record.supporting_source_ids:
    supporting = self._sources.get(supporting_id)
    if supporting is None:
        errors.append(f"record {record.document_id}: unknown supporting source {supporting_id}")
        continue
    if supporting.hospital_id != record.hospital_id:
        errors.append(f"record {record.document_id}: supporting source hospital mismatch")
    if supporting.review_status != ReviewStatus.VERIFIED_OFFICIAL:
        errors.append(f"record {record.document_id}: supporting source is not verified")
    if supporting.source_tier == SourceTier.MAP_PROVIDER:
        errors.append(f"record {record.document_id}: map source cannot support official facts")
```

- [ ] **Step 5: Run GREEN and regression**

```powershell
D:\codex\CareGuide\.venv\Scripts\python.exe -m pytest -q `
  tests/test_hospital_corpus_v2_contract.py `
  tests/test_hospital_corpus_models.py `
  tests/test_hospital_corpus_store.py
```

Expected: all pass; existing v1 files still load without `supporting_source_ids`.

- [ ] **Step 6: Commit**

```powershell
git add careguide/hospital_corpus/models.py careguide/hospital_corpus/store.py `
  tests/test_hospital_corpus_v2_contract.py
git commit -m "feat: add hospital corpus v2 contracts"
```

---

### Task 2: Make atomic publishing version-aware and support merged snapshots

**Files:**
- Modify: `careguide/hospital_corpus/store.py`
- Modify: `tests/test_hospital_corpus_store.py`

- [ ] **Step 1: Write failing writer tests**

Add tests that call `write_corpus(..., version="jiangsu-hospitals-v2", stage="expanded_core_15_of_15")` and assert the loaded manifest values. Add a second test with two snapshot roots containing different files and assert both appear under the published `snapshots/` directory. Add a collision test where both roots contain the same relative path with different bytes and assert `ValueError("snapshot collision")` with no target directory left behind.

```python
def test_writer_rejects_snapshot_collision(tmp_path):
    first = tmp_path / "first"
    second = tmp_path / "second"
    relative = Path("js_nanjing_001/source.html")
    (first / relative).parent.mkdir(parents=True)
    (second / relative).parent.mkdir(parents=True)
    (first / relative).write_bytes(b"first")
    (second / relative).write_bytes(b"second")
    with pytest.raises(ValueError, match="snapshot collision"):
        write_corpus(
            tmp_path / "target",
            hospitals=hospitals,
            sources=sources,
            records=records,
            verification=verification,
            built_at=NOW,
            version="jiangsu-hospitals-v2",
            stage="expanded_core_15_of_15",
            snapshot_roots=(first, second),
        )
    assert not (tmp_path / "target").exists()
```

- [ ] **Step 2: Run RED**

```powershell
D:\codex\CareGuide\.venv\Scripts\python.exe -m pytest -q tests/test_hospital_corpus_store.py
```

Expected: FAIL because the writer has fixed manifest literals and accepts only one snapshot root.

- [ ] **Step 3: Implement explicit version/stage arguments**

Change the signature without breaking existing callers:

```python
def write_corpus(
    target: Path | str,
    *,
    hospitals,
    sources,
    records,
    verification,
    built_at: datetime,
    snapshot_root: Path | None = None,
    snapshot_roots: Sequence[Path] = (),
    version: str = "jiangsu-hospitals-v1",
    stage: str = "pilot_taizhou_3_of_15",
) -> HospitalCorpus:
```

Reject simultaneous `snapshot_root` and `snapshot_roots`. Validate every root with `_assert_snapshot_tree_safe`. Copy files one at a time into the temporary `snapshots/` directory; if a target relative path already exists and its SHA-256 differs, raise `ValueError("snapshot collision")`. Identical duplicate bytes may be reused.

- [ ] **Step 4: Run GREEN**

```powershell
D:\codex\CareGuide\.venv\Scripts\python.exe -m pytest -q tests/test_hospital_corpus_store.py
```

- [ ] **Step 5: Prove committed v1 assets remain unchanged and loadable**

Add a test that hashes every committed file under `data/jiangsu_hospitals_v1`, loads the corpus with the extended models, and verifies the hashes are unchanged after all writer tests. Separately build the pilot into pytest's `tmp_path` and compare semantic hospital/source/record/verification content after excluding the manifest build timestamp and the new empty `supporting_source_ids` default. Never point the compatibility build at `data/jiangsu_hospitals_v1` and never rewrite a committed v1 file.

- [ ] **Step 6: Commit**

```powershell
git add careguide/hospital_corpus/store.py tests/test_hospital_corpus_store.py
git commit -m "feat: publish versioned hospital corpus snapshots"
```

---

### Task 3: Extract a reusable review-manifest verifier

**Files:**
- Create: `careguide/hospital_corpus/review.py`
- Create: `tests/test_hospital_corpus_review.py`
- Modify: `scripts/build_jiangsu_hospital_pilot.py`

- [ ] **Step 1: Write failing review tests**

Create fixtures with two sources and one record using both sources. Tests must prove rejection of: stale source hash, rejected decision, blank reviewer, review before collection, stale content hash, missing required term, unapproved supporting source, and record review missing one bound source ID.

```python
def test_record_review_must_bind_all_record_sources(review_bundle):
    review_bundle.manifest["record_reviews"][0]["source_ids"] = ["primary"]
    with pytest.raises(ValueError, match="record review source set mismatch"):
        apply_review_manifest(
            review_bundle.root,
            review_bundle.sources,
            review_bundle.records,
        )
```

- [ ] **Step 2: Run RED**

```powershell
D:\codex\CareGuide\.venv\Scripts\python.exe -m pytest -q tests/test_hospital_corpus_review.py
```

- [ ] **Step 3: Implement `apply_review_manifest`**

Expose this API:

```python
@dataclass(frozen=True)
class ReviewedBundle:
    sources: tuple[dict, ...]
    records: tuple[dict, ...]
    verification: tuple[dict, ...]


def apply_review_manifest(
    source_dir: Path,
    sources: Sequence[Mapping],
    records: Sequence[Mapping],
    *,
    manifest_name: str = "review_manifest.json",
) -> ReviewedBundle:
```

The verifier must bind every source approval to `source_id + retrieved_sha256`, and every record approval to `document_id + content_hash + exact set(source_id, supporting_source_ids)`. Decode each bound snapshot as UTF-8 then GB18030, remove HTML tags, unescape entities and normalize whitespace before checking every required evidence term. It returns copied dictionaries upgraded to `verified_official`; inputs remain unchanged.

- [ ] **Step 4: Replace the private pilot implementation**

Remove `_snapshot_text` and `_apply_review_manifest` from `scripts/build_jiangsu_hospital_pilot.py`. Import the shared helper and adapt the existing v1 review manifest to use `source_ids` arrays. Rebuild v1 in a temporary directory and assert all authenticated JSON and snapshot hashes match the committed v1 corpus. The manifest `built_at` may differ only in the temporary output and must not replace committed v1.

- [ ] **Step 5: Run GREEN**

```powershell
D:\codex\CareGuide\.venv\Scripts\python.exe -m pytest -q `
  tests/test_hospital_corpus_review.py `
  tests/test_jiangsu_hospital_pilot.py
```

- [ ] **Step 6: Commit**

```powershell
git add careguide/hospital_corpus/review.py scripts/build_jiangsu_hospital_pilot.py `
  data/jiangsu_hospital_sources_v1/review_manifest.json `
  tests/test_hospital_corpus_review.py tests/test_jiangsu_hospital_pilot.py
git commit -m "refactor: share hospital corpus review verification"
```

---

### Task 4: Add the offline v2 builder and frozen merge tests

**Files:**
- Create: `scripts/build_jiangsu_hospital_expansion.py`
- Create: `tests/test_jiangsu_hospital_expansion.py`
- Create: `data/jiangsu_hospital_sources_v2/README.md`

- [ ] **Step 1: Write failing offline-builder tests**

Tests must use synthetic v1/v2 directories and monkeypatch network entry points to fail. Assert: dry-run reports zero external calls; apply refuses existing output; v1 records retain source/content hashes; every non-Taizhou hospital has exactly three records; all output records are reviewed; and a rejected record approval leaves no output directory.

```python
def test_expansion_builder_is_offline(monkeypatch, tmp_path, expansion_inputs):
    monkeypatch.setattr(
        socket,
        "create_connection",
        lambda *_args, **_kwargs: pytest.fail("network called"),
    )
    output = tmp_path / "v2"
    assert command.main([
        "--v1-corpus", str(expansion_inputs.v1),
        "--source-dir", str(expansion_inputs.v2_sources),
        "--output-dir", str(output),
        "--apply",
    ]) == 0
    corpus = HospitalCorpus.load(output)
    assert corpus.manifest.version == "jiangsu-hospitals-v2"
    assert corpus.manifest.stage == "expanded_core_15_of_15"
```

- [ ] **Step 2: Run RED**

```powershell
D:\codex\CareGuide\.venv\Scripts\python.exe -m pytest -q tests/test_jiangsu_hospital_expansion.py
```

- [ ] **Step 3: Implement deterministic candidate generation**

The builder reads:

- committed `data/jiangsu_hospitals_v1` as immutable base;
- `data/jiangsu_hospital_sources_v2/sources.json` for additional `needs_review` sources;
- `data/jiangsu_hospital_sources_v2/record_candidates.json` for 36 unreviewed records;
- `data/jiangsu_hospital_sources_v2/review_manifest.json` for approvals and gaps.

It must compare a committed `v1_integrity.json` map against every v1 data file and snapshot before merging. It then applies `apply_review_manifest`, combines sources/records/verification, rejects duplicate IDs, verifies exactly 15 hospitals and exactly three records per hospital, and calls:

```python
write_corpus(
    args.output_dir,
    hospitals=hospitals,
    sources=merged_sources,
    records=merged_records,
    verification=merged_verification,
    built_at=built_at,
    snapshot_roots=(args.v1_corpus / "snapshots", args.source_dir / "snapshots"),
    version="jiangsu-hospitals-v2",
    stage="expanded_core_15_of_15",
)
```

Dry-run validates file presence and planned counts without writing or applying approvals. Both modes report `external_calls: 0`.

- [ ] **Step 4: Add source-bundle documentation**

`data/jiangsu_hospital_sources_v2/README.md` must state that the directory is an unpublishable offline input bundle, sources remain `needs_review`, service-page absence becomes a gap, and only the v2 builder can combine signed-off additions with the frozen v1 corpus.

- [ ] **Step 5: Run GREEN**

```powershell
D:\codex\CareGuide\.venv\Scripts\python.exe -m pytest -q tests/test_jiangsu_hospital_expansion.py
```

- [ ] **Step 6: Commit**

```powershell
git add scripts/build_jiangsu_hospital_expansion.py `
  tests/test_jiangsu_hospital_expansion.py `
  data/jiangsu_hospital_sources_v2/README.md
git commit -m "feat: add offline Jiangsu hospital v2 builder"
```

---

### Task 5: Collect and review the Nanjing batch

**Files:**
- Create/Modify: `data/jiangsu_hospital_sources_v2/sources.json`
- Create: `data/jiangsu_hospital_sources_v2/snapshots/js_nanjing_*/`
- Create/Modify: `data/jiangsu_hospital_sources_v2/record_candidates.json`
- Create/Modify: `data/jiangsu_hospital_sources_v2/review_manifest.json`
- Modify: `tests/test_jiangsu_hospital_expansion.py`

- [ ] **Step 1: Write failing Nanjing coverage tests**

Assert each of the four Nanjing IDs has candidate categories `identity`, `campus`, and exactly one of `appointment`/`contact`; every candidate has at least one bound source and every term in its review entry appears in a frozen snapshot.

- [ ] **Step 2: Run RED**

```powershell
D:\codex\CareGuide\.venv\Scripts\python.exe -m pytest -q `
  tests/test_jiangsu_hospital_expansion.py -k nanjing
```

- [ ] **Step 3: Research one operational source per hospital**

For each hospital, search only its verified official domain, Jiangsu/Nanjing health-authority domains, government domains, or the allowlisted official appointment platform. Prefer appointment instructions; if unavailable, select an official contact or visit-preparation page. Record the canonical URL, publisher, category, valid-until date and exact claims before fetching. Do not use search snippets as evidence.

If no authoritative page supports any of those three service categories for one hospital, add a `needs_review` gap and stop the Nanjing batch before publishing it. Do not turn a directory listing, search snippet, map result or inferred workflow into a service record merely to reach the target count.

- [ ] **Step 4: Register and fetch each source**

Add `needs_review` source IDs:

- `src_nanjing_001_service`
- `src_nanjing_002_service`
- `src_nanjing_003_service`
- `src_nanjing_004_service`

Run dry-run first for all four exact IDs, then repeat the same loop with `--apply` only after inspecting the proposed host and target path:

```powershell
$sourceIds = @(
  'src_nanjing_001_service',
  'src_nanjing_002_service',
  'src_nanjing_003_service',
  'src_nanjing_004_service'
)
foreach ($sourceId in $sourceIds) {
  D:\codex\CareGuide\.venv\Scripts\python.exe scripts/fetch_hospital_sources.py `
    --registry data/jiangsu_hospital_sources_v2/sources.json `
    --source-id $sourceId `
    --output-dir data/jiangsu_hospital_sources_v2/snapshots
}
foreach ($sourceId in $sourceIds) {
  D:\codex\CareGuide\.venv\Scripts\python.exe scripts/fetch_hospital_sources.py `
    --registry data/jiangsu_hospital_sources_v2/sources.json `
    --source-id $sourceId `
    --output-dir data/jiangsu_hospital_sources_v2/snapshots --apply
}
```

After fetching, copy canonical URL, status, content type, collected time and SHA-256 from the metadata sidecar into `sources.json`, then remove only the sidecar after the values are independently compared to the snapshot bytes.

- [ ] **Step 5: Add 12 candidate records and approvals**

Use deterministic IDs:

- `js_nanjing_001_identity_v2`, `js_nanjing_001_campus_v2`, `js_nanjing_001_service_v2`
- repeat the same suffixes for `js_nanjing_002` through `js_nanjing_004`.

Identity records bind both v1 identity and grade sources when grade is not present in the identity snapshot. Campus records bind the v1 address source. Service records bind only the new operational source. Add source approvals for the four new snapshots, record approvals with exact `source_ids`, content hashes and required terms, plus `needs_review` gaps for appointment/parking/transport/accessibility categories not supported by the chosen service page.

- [ ] **Step 6: Run GREEN and commit**

```powershell
D:\codex\CareGuide\.venv\Scripts\python.exe -m pytest -q `
  tests/test_jiangsu_hospital_expansion.py -k nanjing
git add data/jiangsu_hospital_sources_v2 tests/test_jiangsu_hospital_expansion.py
git commit -m "data: add reviewed Nanjing hospital core records"
```

---

### Task 6: Collect and review the Suzhou batch

**Files:** same v2 source-bundle files and `tests/test_jiangsu_hospital_expansion.py`.

- [ ] **Step 1: Add failing coverage tests** for `js_suzhou_001`, `js_suzhou_002`, and `js_suzhou_003`, requiring identity, campus and service records.
- [ ] **Step 2: Run RED** with `pytest -q tests/test_jiangsu_hospital_expansion.py -k suzhou`.
- [ ] **Step 3: Research official operational pages** using the same source hierarchy and no search-result evidence.
- [ ] **Step 4: Register and fetch** `src_suzhou_001_service`, `src_suzhou_002_service`, and `src_suzhou_003_service` by assigning those exact values to `$sourceIds` and running both loops from Task 5. If any hospital has no authoritative service evidence, record the gap and stop the Suzhou batch before publication.
- [ ] **Step 5: Add nine candidates** using the exact prefixes `js_suzhou_001`, `js_suzhou_002`, and `js_suzhou_003`, each with the suffixes `_identity_v2`, `_campus_v2`, and `_service_v2`; add exact source bindings, evidence terms, source/record approvals and explicit gaps.
- [ ] **Step 6: Run GREEN and commit**:

```powershell
D:\codex\CareGuide\.venv\Scripts\python.exe -m pytest -q `
  tests/test_jiangsu_hospital_expansion.py -k suzhou
git add data/jiangsu_hospital_sources_v2 tests/test_jiangsu_hospital_expansion.py
git commit -m "data: add reviewed Suzhou hospital core records"
```

---

### Task 7: Collect and review the Wuxi batch

**Files:** same v2 source-bundle files and `tests/test_jiangsu_hospital_expansion.py`.

- [ ] **Step 1: Add failing coverage tests** for `js_wuxi_001` and `js_wuxi_002`.
- [ ] **Step 2: Run RED** with `pytest -q tests/test_jiangsu_hospital_expansion.py -k wuxi`.
- [ ] **Step 3: Research official operational pages**; the existing Wuxi government hospital directory may authorize identity/grade/address but a separate official appointment/contact page is required for the service record.
- [ ] **Step 4: Register and fetch** `src_wuxi_001_service` and `src_wuxi_002_service` by assigning those exact values to `$sourceIds` and running both loops from Task 5. If either hospital has no authoritative service evidence, record the gap and stop the Wuxi batch before publication.
- [ ] **Step 5: Add six candidates** using the exact prefixes `js_wuxi_001` and `js_wuxi_002`, each with the suffixes `_identity_v2`, `_campus_v2`, and `_service_v2`; add approvals, evidence terms and gaps.
- [ ] **Step 6: Run GREEN and commit**:

```powershell
D:\codex\CareGuide\.venv\Scripts\python.exe -m pytest -q `
  tests/test_jiangsu_hospital_expansion.py -k wuxi
git add data/jiangsu_hospital_sources_v2 tests/test_jiangsu_hospital_expansion.py
git commit -m "data: add reviewed Wuxi hospital core records"
```

---

### Task 8: Collect and review Changzhou, Nantong and Yangzhou

**Files:** same v2 source-bundle files and `tests/test_jiangsu_hospital_expansion.py`.

- [ ] **Step 1: Add failing coverage tests** for `js_changzhou_001`, `js_nantong_001`, and `js_yangzhou_001`.
- [ ] **Step 2: Run RED** with `pytest -q tests/test_jiangsu_hospital_expansion.py -k "changzhou or nantong or yangzhou"`.
- [ ] **Step 3: Research official operational pages**. Reuse the frozen Yangzhou contact page only if its v1 source approval and hash already authorize the service facts; do not duplicate it under a new source ID. Collect new sources only where the required service facts are absent from v1.
- [ ] **Step 4: Register and fetch required source IDs** `src_changzhou_001_service` and `src_nantong_001_service`; add `src_yangzhou_001_service` only if v1 evidence cannot authorize the selected service record. Assign only the required exact IDs to `$sourceIds` and run both loops from Task 5. If one hospital has no authoritative service evidence, record the gap and stop this combined batch before publication.
- [ ] **Step 5: Add nine candidates** using the exact prefixes `js_changzhou_001`, `js_nantong_001`, and `js_yangzhou_001`, each with the suffixes `_identity_v2`, `_campus_v2`, and `_service_v2`; add exact approvals and gaps.
- [ ] **Step 6: Run GREEN and commit**:

```powershell
D:\codex\CareGuide\.venv\Scripts\python.exe -m pytest -q `
  tests/test_jiangsu_hospital_expansion.py -k "changzhou or nantong or yangzhou"
git add data/jiangsu_hospital_sources_v2 tests/test_jiangsu_hospital_expansion.py
git commit -m "data: add remaining Jiangsu hospital core records"
```

---

### Task 9: Build and validate the immutable v2 corpus

**Files:**
- Create: `data/jiangsu_hospitals_v2/`
- Modify: `tests/test_jiangsu_hospital_expansion.py`
- Modify: `.gitattributes`

- [ ] **Step 1: Add failing final-asset assertions**

Assert exactly 15 hospitals, 45 records, three records per hospital, v2 manifest values, no duplicate groups, no expired sources on the build date, no unresolved conflicts, every record covered by a record verification entry, every retained alias present in at least one bound snapshot, and v1 manifest/data/snapshot hashes equal the committed integrity map.

- [ ] **Step 2: Run RED**

```powershell
D:\codex\CareGuide\.venv\Scripts\python.exe -m pytest -q tests/test_jiangsu_hospital_expansion.py
```

- [ ] **Step 3: Build offline**

```powershell
D:\codex\CareGuide\.venv\Scripts\python.exe scripts/build_jiangsu_hospital_expansion.py
D:\codex\CareGuide\.venv\Scripts\python.exe scripts/build_jiangsu_hospital_expansion.py `
  --apply --output-dir data/jiangsu_hospitals_v2
```

Expected dry-run: `external_calls=0`, 15 hospitals and 45 records. Expected apply: valid v2 output and no existing-directory overwrite.

- [ ] **Step 4: Preserve exact snapshot bytes**

Add:

```gitattributes
data/jiangsu_hospital_sources_v2/snapshots/** binary
data/jiangsu_hospitals_v2/snapshots/** binary
```

- [ ] **Step 5: Run GREEN and validate**

```powershell
D:\codex\CareGuide\.venv\Scripts\python.exe -m pytest -q tests/test_jiangsu_hospital_expansion.py
D:\codex\CareGuide\.venv\Scripts\python.exe scripts/validate_jiangsu_hospital_corpus.py `
  --corpus-dir data/jiangsu_hospitals_v2 --today 2026-11-07
```

- [ ] **Step 6: Commit**

```powershell
git add .gitattributes data/jiangsu_hospitals_v2 `
  scripts/build_jiangsu_hospital_expansion.py tests/test_jiangsu_hospital_expansion.py
git commit -m "data: publish reviewed Jiangsu hospital corpus v2"
```

---

### Task 10: Freeze the v2 BM25 evaluation baseline

**Files:**
- Create: `data/eval/jiangsu_hospitals_v2/retrieval_cases.jsonl`
- Create: `data/eval/jiangsu_hospitals_v2/README.md`
- Create: `data/eval/jiangsu_hospitals_v2/bm25_baseline.json`
- Create: `tests/test_jiangsu_hospital_v2_evaluation.py`

- [ ] **Step 1: Write failing evaluation-set tests**

Assert at least 45 cases, at least three per hospital, at least one hard negative per hospital, positive IDs exist in the declared scope, the README contains the exact JSONL SHA-256, and the baseline regenerates byte-for-byte.

- [ ] **Step 2: Run RED**

```powershell
D:\codex\CareGuide\.venv\Scripts\python.exe -m pytest -q tests/test_jiangsu_hospital_v2_evaluation.py
```

- [ ] **Step 3: Add reviewed cases**

For each hospital add: one identity query, one campus or service query, and one hard negative. Add verified alias variants only when the alias exists in frozen evidence. Hard negatives must declare categories without static evidence and ask for live parking, live routes, doctor schedules, diagnosis or treatment.

- [ ] **Step 4: Generate and freeze the report**

```powershell
D:\codex\CareGuide\.venv\Scripts\python.exe scripts/evaluate_jiangsu_hospital_bm25.py `
  --corpus-dir data/jiangsu_hospitals_v2 `
  --cases data/eval/jiangsu_hospitals_v2/retrieval_cases.jsonl `
  --output data/eval/jiangsu_hospitals_v2/bm25_baseline.json
```

Freeze the lowercase SHA-256 of `retrieval_cases.jsonl` in the README. Document that hospital/category pre-filtering and the small three-record scope make the baseline easier than a future nationwide ranking benchmark.

- [ ] **Step 5: Run GREEN**

```powershell
D:\codex\CareGuide\.venv\Scripts\python.exe -m pytest -q tests/test_jiangsu_hospital_v2_evaluation.py
```

Acceptance: Recall@5 >= 0.90, MRR >= 0.75, hard-negative accuracy = 1.00, scope violations = 0.

- [ ] **Step 6: Commit**

```powershell
git add data/eval/jiangsu_hospitals_v2 tests/test_jiangsu_hospital_v2_evaluation.py
git commit -m "test: add Jiangsu hospital v2 BM25 baseline"
```

---

### Task 11: Final verification and handoff

**Files:** Modify only files whose failing checks prove a defect.

- [ ] **Step 1: Run focused tests**

```powershell
D:\codex\CareGuide\.venv\Scripts\python.exe -m pytest -q `
  tests/test_hospital_corpus_models.py `
  tests/test_hospital_corpus_processing.py `
  tests/test_hospital_corpus_store.py `
  tests/test_hospital_snapshot.py `
  tests/test_hospital_corpus_review.py `
  tests/test_hospital_corpus_v2_contract.py `
  tests/test_jiangsu_hospital_pilot.py `
  tests/test_jiangsu_hospital_evaluation.py `
  tests/test_jiangsu_hospital_expansion.py `
  tests/test_jiangsu_hospital_v2_evaluation.py
```

- [ ] **Step 2: Validate both versions**

```powershell
D:\codex\CareGuide\.venv\Scripts\python.exe scripts/validate_jiangsu_hospital_corpus.py `
  --corpus-dir data/jiangsu_hospitals_v1 --today 2026-11-07
D:\codex\CareGuide\.venv\Scripts\python.exe scripts/validate_jiangsu_hospital_corpus.py `
  --corpus-dir data/jiangsu_hospitals_v2 --today 2026-11-07
```

- [ ] **Step 3: Run broad regression**

```powershell
D:\codex\CareGuide\.venv\Scripts\python.exe -m pytest -q
```

- [ ] **Step 4: Run integrity and secret checks**

```powershell
git diff --check
git status --short
git grep -n -I -E "Authorization:|Bearer [A-Za-z0-9_-]{12,}|api[_-]?key[=:][^$]" -- `
  careguide/hospital_corpus scripts data/jiangsu_hospital_sources_v2 `
  data/jiangsu_hospitals_v2 data/eval/jiangsu_hospitals_v2 tests
```

Classify existing offline fixture strings and parameter names separately; fail if any real credential, cookie, login token, patient data or unreviewed third-party claim appears in new assets.

- [ ] **Step 5: Review completion definition**

Only claim completion when v1 remains unchanged, v2 has exactly 15 hospitals and 45 reviewed records, every record is traceable, all missing service categories are explicit gaps, fixed evaluation meets thresholds, and all focused/broad tests pass. Do not claim Huawei Embedding, Chroma, Rerank, AgentArts or real hospital-system integration complete.

- [ ] **Step 6: Commit only proven corrections**

If verification exposes a defect, return to the task that owns the file, add a failing regression test, fix it, rerun the owning tests and commit `fix: close Jiangsu hospital v2 verification gaps`. If no correction is needed, do not create an empty commit.
