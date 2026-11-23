# Live Parking Query Routing Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Route hospital “空车位/剩余车位/停车位” questions into the existing real Amap parking workflow without inventing live availability.

**Architecture:** Extend the deterministic parking intent vocabulary and query cleanup in `map_parking_chat.py`. Reuse the existing hospital-selection and parking-candidate pipeline, where `available_spaces=None` explicitly represents unknown live occupancy.

**Tech Stack:** Python 3.11, FastAPI, Pydantic, pytest, Amap Web Service integration

---

### Task 1: Add failing intent and cleanup regression tests

**Files:**
- Modify: `tests/test_map_parking_chat.py`
- Modify: `tests/test_amap_api.py`

- [x] **Step 1: Write the failing unit regression test**

Add a parameterized test proving hospital phrases containing `空车位`, `剩余车位`, `停车位`, and `车位` match the deterministic map service, plus a handling test asserting the exact cleaned hospital query.

- [x] **Step 2: Run the focused tests and verify failure**

Run: `python -m pytest tests/test_map_parking_chat.py -k "live_space or parking_vocabulary" -q`

Expected: FAIL because `MapParkingChatService.matches()` does not recognize `空车位/剩余车位/停车位/车位`.

- [x] **Step 3: Add an API routing regression test**

Post the real user phrase to `/chat` and assert `intent == "map_parking"`, `engine.process_calls == []`, and one Amap hospital search occurred.

### Task 2: Implement the smallest deterministic routing change

**Files:**
- Modify: `careguide/map_parking_chat.py`

- [x] **Step 1: Expand explicit parking vocabulary**

Recognize `空车位`, `剩余车位`, `停车位`, and `车位` while preserving the existing hospital-context requirement.

- [x] **Step 2: Remove dynamic-space suffixes from the hospital POI query**

Update cleaning so the hospital name remains the Amap search term rather than including “现在还有多少空车位”.

- [x] **Step 3: Run the focused tests**

Run: `python -m pytest tests/test_map_parking_chat.py -k "live_space or parking_vocabulary" -q`

Expected: PASS.

### Task 3: Verify API routing and unknown availability contract

**Files:**
- Test: `tests/test_amap_api.py`
- Test: `tests/test_map_parking_chat.py`

- [x] **Step 1: Run API regression tests**

Run: `python -m pytest tests/test_amap_api.py -q`

Expected: PASS; the general engine is not invoked for the live-space phrase.

- [x] **Step 2: Run all deterministic parking tests**

Run: `python -m pytest tests/test_map_parking_chat.py -q`

Expected: PASS; selected parking candidates retain `available_spaces is None`.

- [x] **Step 3: Run the broader affected suite**

Run: `python -m pytest tests/test_amap_api.py tests/test_map_parking_chat.py tests/test_jiangsu_hospital_evaluation.py -q`

Expected: PASS with no appointment-RAG regression.
