# Huawei Hybrid Medical RAG Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace CareGuide's active BM25 single-result path with a truthful Huawei ModelArts hybrid retrieval pipeline and deterministic headache safety triage.

**Architecture:** Keep the existing `MedicalKnowledgeBase` as the retrieval core, add bounded retrieval telemetry and evidence policy around it, and route symptom questions through a deterministic safety/clarification layer before orchestration. Build a versioned Chroma index explicitly with Huawei embeddings, fuse BM25 and dense candidates through RRF, rerank with Huawei ModelArts, and expose exact degradation state through the API and UI.

**Tech Stack:** Python 3.11, FastAPI, Pydantic, ChromaDB 0.5.23, ModelArts MaaS Embeddings/Rerank REST APIs, Vue 3, Node test runner, pytest.

---

## File map

- Create `careguide/medical_triage.py`: structured symptom context, headache red flags, clarification and department navigation.
- Create `careguide/retrieval/evidence_policy.py`: source-tier and score gates for medical evidence.
- Modify `careguide/retrieval/knowledge_base.py`: bounded Top-K, RRF telemetry, rerank threshold and truthful degraded status.
- Modify `careguide/retrieval/embedding_provider.py`: Huawei provider identity, input limits and sanitized failures.
- Modify `careguide/retrieval/reranker.py`: Huawei contract limits, score validation and sanitized failures.
- Modify `careguide/runtime.py`: validated hybrid configuration and explicit readiness/degradation.
- Modify `careguide/tools.py`: query-context use, evidence policy and retrieval metadata.
- Modify `careguide/agent_orchestrator.py`: remove `top_k=1` and preempt unsafe/underspecified triage before planner tasks.
- Modify `careguide/orchestration/runtime.py`: include retrieval and safety telemetry in result and trace.
- Modify `careguide/api.py`: health/readiness and JSON/SSE contract exposure.
- Modify `frontend/src/lib/backends.js` and `frontend/src/App.vue`: normalize and display retrieval/safety state.
- Modify `scripts/index_medical.py`: verified Huawei versioned-index command.
- Create `scripts/validate_huawei_hybrid_rag.py`: explicit paid real-provider validation.
- Modify `.env.example`, `README.md`, and dependency files: document exact configuration without secrets.

### Task 1: Freeze the current failure as regression tests

**Files:**
- Create: `tests/test_medical_triage.py`
- Modify: `tests/test_medical_tools.py`
- Modify: `tests/test_orchestration_acceptance.py`

- [ ] **Step 1: Add failing symptom and orchestration tests**

```python
def test_generic_department_request_without_symptom_asks_for_symptom():
    decision = evaluate_headache_triage(
        "请先判断是否紧急，再告诉我挂什么科", history=[])
    assert decision.status == "needs_clarification"
    assert any("主要症状" in question for question in decision.questions)
    assert decision.department is None
    assert decision.run_rag is False


def test_negated_weakness_is_not_a_positive_red_flag():
    decision = evaluate_headache_triage(
        "今天上午头痛4分，没有发烧、呕吐、肢体无力，也没有受伤", history=[])
    assert "focal_neurologic_deficit" not in decision.red_flags
    assert decision.status == "non_emergency_information"


def test_thunderclap_headache_preempts_retrieval():
    decision = evaluate_headache_triage(
        "突然出现一生中最严重的头痛，几秒钟达到高峰", history=[])
    assert decision.status == "emergency"
    assert decision.run_rag is False
    assert decision.department == "急诊"
```

Add an orchestration test that submits `我头有点疼` followed by `请判断是否紧急并告诉我挂什么科` and asserts the second request does not create a `waiting_input` task whose missing field is `department`.

- [ ] **Step 2: Run RED tests**

Run:

```powershell
D:\codex\CareGuide\.venv\Scripts\python.exe -m pytest -q `
  tests/test_medical_triage.py `
  tests/test_medical_tools.py `
  tests/test_orchestration_acceptance.py
```

Expected: collection fails because `careguide.medical_triage` does not exist, proving the safety layer is absent.

- [ ] **Step 3: Commit only the failing tests**

```powershell
git add tests/test_medical_triage.py tests/test_medical_tools.py tests/test_orchestration_acceptance.py
git commit -m "test: define safe symptom triage contract"
```

### Task 2: Implement deterministic headache safety triage

**Files:**
- Create: `careguide/medical_triage.py`
- Test: `tests/test_medical_triage.py`

- [ ] **Step 1: Implement immutable triage contracts and negation-aware rules**

```python
from dataclasses import dataclass
import re


@dataclass(frozen=True)
class TriageDecision:
    status: str
    symptoms: tuple[str, ...]
    negated: tuple[str, ...]
    red_flags: tuple[str, ...]
    questions: tuple[str, ...]
    department: str | None
    run_rag: bool


NEGATION = re.compile(r"(?:没有|无|未出现)([^。；！？\n]*?)(?=但|不过|然而|[。；！？\n]|$)")


def _negated_terms(text: str, terms: tuple[str, ...]) -> set[str]:
    spans = [match.group(1) for match in NEGATION.finditer(text)]
    return {term for term in terms if any(term in span for span in spans)}


def evaluate_headache_triage(message: str, history: list[dict]) -> TriageDecision:
    prior = "\n".join(
        str(turn.get("content", "")) for turn in history[-6:]
        if turn.get("role") == "user"
    )
    text = f"{prior}\n{message}".strip()
    has_headache = any(term in text for term in ("头痛", "头疼", "头有点疼"))
    if not has_headache:
        return TriageDecision("needs_clarification", (), (), (),
            ("请描述主要症状、开始时间和严重程度。",), None, False)

    danger_terms = ("肢体无力", "面歪", "说话困难", "意识不清", "抽搐",
                    "高热", "颈部僵硬", "颈硬", "畏光", "头部外伤", "撞到头")
    negated = _negated_terms(text, danger_terms)
    present = lambda term: term in text and term not in negated
    flags = []
    if re.search(r"突然|几秒|雷击|一生中最严重", text) and re.search(r"剧烈|严重|最痛", text):
        flags.append("thunderclap")
    if any(present(term) for term in ("肢体无力", "面歪", "说话困难", "意识不清", "抽搐")):
        flags.append("focal_neurologic_deficit")
    if present("高热") and any(present(term) for term in ("颈部僵硬", "颈硬", "畏光")):
        flags.append("meningism")
    if present("头部外伤") or present("撞到头"):
        flags.append("head_injury")
    if flags:
        return TriageDecision("emergency", ("headache",), tuple(sorted(negated)),
                              tuple(flags), (), "急诊", False)

    missing = tuple(label for pattern, label in (
        (r"今天|昨天|小时|天|周|突然|逐渐", "头痛从什么时候开始？"),
        (r"[0-9一二三四五六七八九十]+分|轻|中|重", "疼痛程度0到10分是多少？"),
        (r"发热|呕吐|视力|肢体|说话|外伤|没有|无", "是否伴随发热、呕吐、视力变化、肢体无力或近期外伤？"),
    ) if not re.search(pattern, text))
    if missing:
        return TriageDecision("needs_clarification", ("headache",), tuple(sorted(negated)),
                              (), missing, None, False)
    return TriageDecision("non_emergency_information", ("headache",), tuple(sorted(negated)),
                          (), (), "神经内科", True)
```

- [ ] **Step 2: Run GREEN tests**

Run: `D:\codex\CareGuide\.venv\Scripts\python.exe -m pytest -q tests/test_medical_triage.py`

Expected: all triage cases pass, including explicit negations and urgent preemption.

- [ ] **Step 3: Commit**

```powershell
git add careguide/medical_triage.py tests/test_medical_triage.py
git commit -m "feat: add deterministic headache safety triage"
```

### Task 3: Add evidence policy and retrieval telemetry

**Files:**
- Create: `careguide/retrieval/evidence_policy.py`
- Modify: `careguide/retrieval/knowledge_base.py`
- Modify: `tests/test_semantic_retrieval.py`
- Create: `tests/test_evidence_policy.py`

- [ ] **Step 1: Write failing policy and telemetry tests**

```python
def test_open_dataset_cannot_alone_support_high_risk_guidance():
    evidence = {"representation": "open_dataset_qa", "citation": {
        "review_status": "open_dataset_unreviewed"}, "retrieval": {"rerank_score": .91}}
    accepted, reason = accept_evidence(evidence, risk="urgent_triage")
    assert accepted is False
    assert reason == "open_dataset_high_risk"


def test_hybrid_report_counts_every_stage(hybrid_knowledge, candidates):
    result = hybrid_knowledge.search_with_report(
        "头痛", candidates, today=date(2026, 11, 2), top_k=5)
    assert result.report.mode == "hybrid"
    assert result.report.lexical_count <= 20
    assert result.report.dense_count <= 20
    assert result.report.reranked is True
    assert len(result.evidence) <= 5
```

- [ ] **Step 2: Run RED tests**

Run:

```powershell
D:\codex\CareGuide\.venv\Scripts\python.exe -m pytest -q `
  tests/test_evidence_policy.py tests/test_semantic_retrieval.py
```

Expected: failures for missing `accept_evidence` and `search_with_report`.

- [ ] **Step 3: Implement typed report and policy**

```python
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
```

`search_with_report()` must cap lexical and dense candidate depths at 20, fuse by the existing weighted RRF formula, rerank at most 20 fused candidates, then call `accept_evidence()` before returning at most `top_k` results. Keep `search()` as a compatibility wrapper returning `list(result.evidence)`.

Implement `accept_evidence()` with these rules:

```python
def accept_evidence(item: dict, *, risk: str) -> tuple[bool, str]:
    citation = item.get("citation") or {}
    retrieval = item.get("retrieval") or {}
    if risk in {"urgent_triage", "diagnosis", "medication"} and \
            citation.get("review_status") == "open_dataset_unreviewed":
        return False, "open_dataset_high_risk"
    score = retrieval.get("rerank_score")
    if score is not None and float(score) < 0.35:
        return False, "below_rerank_threshold"
    if not citation.get("source_id") or not citation.get("url"):
        return False, "missing_source_identity"
    return True, "accepted"
```

- [ ] **Step 4: Run GREEN tests and existing retrieval regression**

Run:

```powershell
D:\codex\CareGuide\.venv\Scripts\python.exe -m pytest -q `
  tests/test_evidence_policy.py tests/test_semantic_retrieval.py `
  tests/test_reranker.py tests/test_medical_tools.py
```

- [ ] **Step 5: Commit**

```powershell
git add careguide/retrieval/evidence_policy.py careguide/retrieval/knowledge_base.py `
  tests/test_evidence_policy.py tests/test_semantic_retrieval.py
git commit -m "feat: gate and trace hybrid medical evidence"
```

### Task 4: Harden Huawei ModelArts provider contracts

**Files:**
- Modify: `careguide/retrieval/embedding_provider.py`
- Modify: `careguide/retrieval/reranker.py`
- Modify: `careguide/runtime.py`
- Modify: `tests/test_semantic_retrieval.py`
- Modify: `tests/test_reranker.py`
- Create: `tests/test_huawei_retrieval_runtime.py`

- [ ] **Step 1: Write failing provider tests**

Add MockTransport/client tests asserting:

```python
assert request.headers["authorization"] == "Bearer test-only"
assert request.url.path == "/v1/rerank"
assert payload["model"] == "bge-reranker-v2-m3"
assert len(payload["documents"]) <= 20
assert "test-only" not in public_error
```

Add runtime tests asserting hybrid startup rejects missing Chroma, mismatched corpus fingerprint, missing model names, invalid dimensions, and placeholder keys. Assert no environment values are present in error messages.

- [ ] **Step 2: Run RED tests**

Run: `D:\codex\CareGuide\.venv\Scripts\python.exe -m pytest -q tests/test_huawei_retrieval_runtime.py tests/test_reranker.py tests/test_semantic_retrieval.py`

Expected: candidate-limit and runtime-readiness assertions fail.

- [ ] **Step 3: Implement validated runtime configuration**

```python
@dataclass(frozen=True)
class RetrievalRuntimeStatus:
    requested_mode: str
    active_mode: str
    embedding_provider: str | None
    rerank_provider: str | None
    index_collection: str | None
    degraded_reason: str | None


def _required_secret(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value or value.startswith("YOUR_"):
        raise ValueError(f"{name} is required")
    return value
```

Use `provider_name="huawei-modelarts"`, default candidate limit 20, timeout at most 30 seconds, and retry only 429/503/504 with a maximum of two retries. Sanitize provider exceptions into stable codes such as `embedding_timeout`, `rerank_rate_limited`, and `provider_invalid_response`.

- [ ] **Step 4: Run GREEN tests**

Run the same command and require zero failures.

- [ ] **Step 5: Commit**

```powershell
git add careguide/retrieval/embedding_provider.py careguide/retrieval/reranker.py `
  careguide/runtime.py tests/test_huawei_retrieval_runtime.py `
  tests/test_reranker.py tests/test_semantic_retrieval.py
git commit -m "feat: validate Huawei retrieval providers"
```

### Task 5: Connect safe triage and hybrid evidence to the Agent

**Files:**
- Modify: `careguide/tools.py`
- Modify: `careguide/agent_orchestrator.py`
- Modify: `careguide/orchestration/runtime.py`
- Modify: `careguide/orchestration/answering.py`
- Modify: `tests/test_medical_tools.py`
- Modify: `tests/test_orchestration_acceptance.py`
- Modify: `tests/test_unified_synthesis_and_sources.py`

- [ ] **Step 1: Add failing end-to-end orchestration tests**

Cover these exact assertions:

```python
assert generic_department_result["workflow_status"] == "waiting_input"
assert "主要症状" in generic_department_result["response"]
assert generic_department_result["evidence"] == []

assert thunderclap_result["primary_agent"] == "triage"
assert thunderclap_result["safety"]["disposition"] == "emergency"
assert thunderclap_result["tools_used"] == []

assert mild_headache_result["retrieval"]["mode"] == "hybrid"
assert mild_headache_result["retrieval"]["reranked"] is True
assert mild_headache_result["recommended_department"] in {"全科", "普通内科", "神经内科"}
```

- [ ] **Step 2: Run RED tests**

Run:

```powershell
D:\codex\CareGuide\.venv\Scripts\python.exe -m pytest -q `
  tests/test_orchestration_acceptance.py `
  tests/test_unified_synthesis_and_sources.py `
  tests/test_medical_tools.py
```

Expected: current planner creates the wrong department input task and has no safety/retrieval metadata.

- [ ] **Step 3: Implement pre-planner triage and remove single-result forcing**

In both online and offline tool paths, delete the assignments that force `search_care_guidance` to `top_k=1`. Use bounded configuration with default final `top_k=5` and candidate depth 20.

Before model planning, call `evaluate_headache_triage(message, history)`. Return a deterministic clarification/emergency result when `run_rag` is false. For non-emergency information, add the recommended department as navigation metadata, not as a required user field. Pass `risk="general_guidance"` to the evidence policy and include the retrieval report in tool output.

The final answering system prompt must state that open unreviewed evidence cannot support diagnosis, urgent safety, pregnancy management, prescriptions, or dosage decisions.

- [ ] **Step 4: Run GREEN tests and chat regression**

Run:

```powershell
D:\codex\CareGuide\.venv\Scripts\python.exe -m pytest -q `
  tests/test_orchestration_acceptance.py tests/test_unified_synthesis_and_sources.py `
  tests/test_medical_tools.py tests/test_chat_runtime.py tests/test_chat_stream.py
```

- [ ] **Step 5: Commit**

```powershell
git add careguide/tools.py careguide/agent_orchestrator.py `
  careguide/orchestration/runtime.py careguide/orchestration/answering.py `
  tests/test_medical_tools.py tests/test_orchestration_acceptance.py `
  tests/test_unified_synthesis_and_sources.py
git commit -m "feat: route symptoms through safe hybrid triage"
```

### Task 6: Expose truthful retrieval state in API and UI

**Files:**
- Modify: `careguide/api.py`
- Modify: `frontend/src/lib/backends.js`
- Modify: `frontend/src/App.vue`
- Create: `frontend/tests/backend-contracts.test.js`
- Modify: `frontend/tests/browser-workflow.mjs`
- Modify: `tests/test_chat_stream.py`

- [ ] **Step 1: Write failing API and frontend contract tests**

Expected normalized response:

```javascript
assert.deepEqual(result.retrieval, {
  mode: 'hybrid', lexicalCount: 20, denseCount: 20,
  fusedCount: 27, reranked: true, filteredCount: 7,
  finalCount: 3, degradedReason: null
})
assert.equal(result.safety.disposition, 'non_emergency_information')
```

Add a browser assertion that the Last trace card displays `混合检索`, `BM25 20`, `向量 20`, `Rerank 已完成`, `证据 3`, and never displays API keys or provider response bodies.

- [ ] **Step 2: Run RED tests**

Run:

```powershell
D:\codex\CareGuide\.venv\Scripts\python.exe -m pytest -q tests/test_chat_stream.py
Push-Location frontend
npm test
npm run test:browser
Pop-Location
```

- [ ] **Step 3: Implement normalized contracts and UI**

Add `retrieval` and `safety` to JSON and final SSE results. Normalize snake/camel fields in `normalizeChatResponse()` and render a compact trace block. Degraded states must render `BM25降级` or `Rerank不可用`, never `混合检索成功`.

- [ ] **Step 4: Run GREEN tests and build**

Run:

```powershell
D:\codex\CareGuide\.venv\Scripts\python.exe -m pytest -q tests/test_chat_stream.py
Push-Location frontend
npm test
npm run build
npm run test:browser
Pop-Location
```

- [ ] **Step 5: Commit**

```powershell
git add careguide/api.py frontend/src/lib/backends.js frontend/src/App.vue `
  frontend/tests/backend-contracts.test.js frontend/tests/browser-workflow.mjs `
  tests/test_chat_stream.py
git commit -m "feat: show hybrid retrieval and safety status"
```

### Task 7: Build and validate the versioned Huawei index explicitly

**Files:**
- Modify: `requirements-semantic.txt`
- Modify: `scripts/index_medical.py`
- Create: `scripts/validate_huawei_hybrid_rag.py`
- Create: `tests/test_validate_huawei_hybrid_rag.py`
- Modify: `.env.example`
- Modify: `README.md`

- [ ] **Step 1: Add failing dry-run and secret-safety tests**

```python
def test_validator_dry_run_makes_no_provider_calls(tmp_path, monkeypatch):
    result = run_validation(config_file=None, apply=False, corpus_dir=CORPUS)
    assert result.status == "dry_run"
    assert result.external_calls == 0


def test_validator_output_never_contains_key(monkeypatch, capsys):
    monkeypatch.setenv("CAREGUIDE_EMBEDDING_API_KEY", "secret-test-value")
    main(["--dry-run"])
    assert "secret-test-value" not in capsys.readouterr().out
```

- [ ] **Step 2: Run RED tests**

Run: `D:\codex\CareGuide\.venv\Scripts\python.exe -m pytest -q tests/test_validate_huawei_hybrid_rag.py`

- [ ] **Step 3: Implement explicit indexing and validation commands**

`requirements-semantic.txt` keeps the pinned `chromadb==0.5.23`. `index_medical.py` must refuse an existing collection and print only record count, collection name, corpus fingerprint prefix, provider name and model name.

The real validation script must require both `--config-file` and `--apply`, then run this fixed set:

```python
CASES = (
    "我头有点疼",
    "今天上午头痛4分，没有发烧、呕吐、肢体无力，也没有受伤",
    "突然出现一生中最严重的头痛，几秒达到高峰",
    "请判断是否紧急并告诉我挂什么科",
)
```

It exits non-zero if hybrid mode is not active, rerank is not used for eligible non-emergency retrieval, unrelated pregnancy evidence survives, or urgent cases call RAG.

- [ ] **Step 4: Document exact configuration without values**

Add these names to `.env.example`:

```dotenv
CAREGUIDE_RETRIEVAL_MODE=hybrid
CAREGUIDE_EMBEDDING_API_KEY=YOUR_MODELARTS_API_KEY
CAREGUIDE_EMBEDDING_BASE_URL=https://api.modelarts-maas.com/v1
CAREGUIDE_EMBEDDING_MODEL=YOUR_MODELARTS_EMBEDDING_MODEL
CAREGUIDE_EMBEDDING_DIMENSIONS=1024
CAREGUIDE_RERANK_ENABLED=true
CAREGUIDE_RERANK_API_KEY=YOUR_MODELARTS_API_KEY
CAREGUIDE_RERANK_BASE_URL=https://api.modelarts-maas.com/v1
CAREGUIDE_RERANK_MODEL=bge-reranker-v2-m3
CAREGUIDE_RERANK_CANDIDATES=20
```

Document commands:

```powershell
.venv\Scripts\python.exe -m pip install -r requirements-semantic.txt
.venv\Scripts\python.exe scripts/index_medical.py --config-file .env `
  --corpus-dir data/careguide_kuake_rag_v1 --versioned --apply
.venv\Scripts\python.exe scripts/validate_huawei_hybrid_rag.py `
  --config-file .env --apply
```

- [ ] **Step 5: Run GREEN tests and dry run**

Run:

```powershell
D:\codex\CareGuide\.venv\Scripts\python.exe -m pytest -q tests/test_validate_huawei_hybrid_rag.py tests/test_index_reuse.py
D:\codex\CareGuide\.venv\Scripts\python.exe scripts/validate_huawei_hybrid_rag.py --dry-run
```

- [ ] **Step 6: Commit**

```powershell
git add requirements-semantic.txt scripts/index_medical.py `
  scripts/validate_huawei_hybrid_rag.py tests/test_validate_huawei_hybrid_rag.py `
  .env.example README.md
git commit -m "feat: add explicit Huawei hybrid index validation"
```

### Task 8: Full verification, security audit and real-provider gate

**Files:**
- No production edits unless a failing test proves a defect.

- [ ] **Step 1: Run focused backend verification**

```powershell
D:\codex\CareGuide\.venv\Scripts\python.exe -m pytest -q `
  tests/test_medical_triage.py tests/test_evidence_policy.py `
  tests/test_semantic_retrieval.py tests/test_reranker.py `
  tests/test_huawei_retrieval_runtime.py tests/test_medical_tools.py `
  tests/test_orchestration_acceptance.py tests/test_chat_stream.py `
  tests/test_validate_huawei_hybrid_rag.py
```

Expected: zero failures and no external paid calls.

- [ ] **Step 2: Run frontend verification**

```powershell
Push-Location frontend
npm test
npm run build
npm run test:browser
Pop-Location
```

Expected: all unit and Chrome checks pass.

- [ ] **Step 3: Run broad regression**

```powershell
D:\codex\CareGuide\.venv\Scripts\python.exe -m pytest -q
```

Classify optional dependency, external corpus-integrity and changed-code failures separately; do not overwrite user corpus files to make tests pass.

- [ ] **Step 4: Perform security checks**

```powershell
git diff --check
git status --short
rg -n "(sk-|Bearer [A-Za-z0-9]|API_KEY=.+[^_])" careguide frontend scripts tests .env.example
```

Confirm no real key, Authorization header, user symptom text, full provider body or stack trace appears in tracked output, trace fixtures or logs.

- [ ] **Step 5: Run explicit real Huawei gate after credentials are configured**

```powershell
D:\codex\CareGuide\.venv\Scripts\python.exe scripts/index_medical.py `
  --config-file .env --corpus-dir data/careguide_kuake_rag_v1 --versioned --apply
D:\codex\CareGuide\.venv\Scripts\python.exe scripts/validate_huawei_hybrid_rag.py `
  --config-file .env --apply
```

Expected: versioned collection contains exactly the verified corpus records; mild headache uses hybrid plus rerank; generic missing-symptom prompt asks a question; thunderclap case preempts retrieval; no pregnancy evidence survives.

- [ ] **Step 6: Final review and commit any verification-only documentation**

Only claim completion when Step 5 passes with real Huawei credentials. If credentials are unavailable, report code/offline tests complete and real provider gate pending.

