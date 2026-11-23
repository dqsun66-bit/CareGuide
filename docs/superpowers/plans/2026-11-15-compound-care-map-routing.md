# Compound Care and Map Routing Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans and implement each step with tests first.

**Goal:** Preserve medical triage and mobility constraints when a single user request also asks for hospital parking.

**Architecture:** Use a deterministic compound-request detector to decide whether the main Agent must run alongside the existing Amap flow. Compose both results without adding model calls to parking-only requests, and filter Amap POIs to hospital-level candidates before persistence.

**Tech Stack:** Python 3.11, FastAPI, Pydantic, pytest, Amap Web Service

---

### Task 1: Specify compound routing

**Files:**
- Create: `careguide/compound_map_chat.py`
- Modify: `tests/test_amap_api.py`

- [x] Add a failing API test for symptom + wheelchair + parking.
- [x] Add a failing emergency-preemption test.
- [x] Add a regression assertion that pure parking still skips the main Agent.
- [x] Implement deterministic compound detection and result composition.
- [x] Refactor `_execute_chat_result` to run care first only for compound map requests.

### Task 2: Filter and annotate hospital candidates

**Files:**
- Modify: `careguide/map_parking_chat.py`
- Modify: `tests/test_map_parking_chat.py`

- [x] Add failing candidate-filter tests with inactive, sub-facility, unrelated and alias POIs.
- [x] Add a failing wheelchair-constraint propagation test.
- [x] Implement deterministic candidate filtering, deduplication and accessibility annotation.

### Task 3: Verify affected behavior

**Files:**
- Test: `tests/test_amap_api.py`
- Test: `tests/test_map_parking_chat.py`
- Test: `tests/test_medical_triage.py`

- [x] Run focused new tests and confirm they pass.
- [x] Run all map and triage tests.
- [x] Run frontend tests to ensure compound fields remain compatible.
- [ ] Obtain independent code review and address every blocking finding.
