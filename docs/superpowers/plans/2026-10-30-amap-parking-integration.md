# Amap Parking Integration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add real Amap hospital/parking POI search, route planning, and an interactive Vue map while keeping real map data and SIM-A/B/C simulation strictly separated.

**Architecture:** A focused asynchronous `AmapClient` owns all server-side Web Service API calls and normalizes external payloads into source-labelled CareGuide records. FastAPI exposes read-only hospital, parking, and route endpoints; Vue uses those endpoints for candidate data and loads Amap JS API only for rendering. Unknown live availability and wheelchair accessibility remain `null`, while the existing deterministic simulation stays behind its current explicit demo endpoint.

**Tech Stack:** Python 3.11+, FastAPI, Pydantic, httpx, pytest, Vue 3, Vite, Node test runner, Amap Web Service API v5, Amap JavaScript API 2.0.

---

## File map

- Create `careguide/amap_client.py`: Amap HTTP client, validation, normalization, and safe provider errors.
- Create `careguide/amap_models.py`: internal Pydantic response/request contracts shared by API handlers.
- Create `tests/test_amap_client.py`: mock-transport unit tests for request shape, normalization, null safety, and redaction.
- Create `tests/test_amap_api.py`: FastAPI endpoint tests using dependency overrides and a scripted Amap client.
- Modify `careguide/api.py`: dependency lifecycle plus hospital, parking, and route endpoints.
- Modify `.env.example`: public Amap server configuration names only.
- Create `frontend/.env.example`: public Vite JS API key variable.
- Modify `frontend/src/lib/parking.js`: real-map API request builders and normalization alongside existing simulation request.
- Create `frontend/src/lib/amap-loader.js`: single-flight JS API loader with explicit missing-key and load-error behavior.
- Create `frontend/src/components/ParkingMap.vue`: render markers and selected route without owning data fetching.
- Modify `frontend/src/App.vue`: real/simulation mode switch, hospital search, candidate list, route selection, and source labels.
- Modify `frontend/src/styles.css`: map layout, mode switch, source badges, unknown-state styling, and responsive behavior.
- Modify `frontend/tests/parking.test.js`: real-mode request and truth-boundary tests.
- Create `frontend/tests/amap-loader.test.js`: loader configuration and failure tests.
- Modify `README.md`: configuration and truthful-data boundary.

### Task 1: Configuration contract and secret boundaries

**Files:**
- Modify: `.env.example`
- Create: `frontend/.env.example`
- Test: `tests/test_amap_client.py`

- [ ] **Step 1: Write the failing configuration/redaction test**

Create `tests/test_amap_client.py` with:

```python
import httpx
import pytest

from careguide.amap_client import AmapClient, AmapProviderError


@pytest.mark.asyncio
async def test_provider_error_never_exposes_server_key():
    secret = 'private-amap-key'

    def handle(request: httpx.Request):
        assert request.url.params['key'] == secret
        return httpx.Response(401, json={
            'status': '0', 'info': f'INVALID_USER_KEY {secret}', 'infocode': '10001'
        })

    transport = httpx.MockTransport(handle)
    async with httpx.AsyncClient(transport=transport) as http:
        client = AmapClient(secret, http_client=http)
        with pytest.raises(AmapProviderError) as caught:
            await client.search_hospitals('泰州市人民医院', city='泰州')

    assert caught.value.code == 'provider_auth_failed'
    assert secret not in str(caught.value)
```

- [ ] **Step 2: Run the test and verify RED**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest -q tests\test_amap_client.py::test_provider_error_never_exposes_server_key
```

Expected: collection error because `careguide.amap_client` does not exist.

- [ ] **Step 3: Add public configuration names**

Append to `.env.example`:

```dotenv
# Amap Web Service API: server-side only. Never expose this value to Vite.
CAREGUIDE_AMAP_WEB_SERVICE_KEY=YOUR_AMAP_WEB_SERVICE_KEY
CAREGUIDE_AMAP_BASE_URL=https://restapi.amap.com
CAREGUIDE_AMAP_TIMEOUT_SECONDS=8
```

Create `frontend/.env.example`:

```dotenv
# Amap Web JS API key. Restrict its domains in the Amap console.
VITE_AMAP_JS_KEY=YOUR_AMAP_JS_KEY
```

- [ ] **Step 4: Add the minimal safe exception and client constructor**

Create the initial `careguide/amap_client.py`:

```python
from __future__ import annotations

import httpx


class AmapProviderError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


class AmapClient:
    def __init__(self, api_key: str, *, base_url: str = 'https://restapi.amap.com',
                 timeout_s: float = 8, http_client: httpx.AsyncClient | None = None):
        if not api_key or api_key.startswith('YOUR_'):
            raise ValueError('Amap Web Service API key is not configured')
        self._api_key = api_key
        self._owns_client = http_client is None
        self._http = http_client or httpx.AsyncClient(
            base_url=base_url.rstrip('/'), timeout=timeout_s, trust_env=False)

    async def search_hospitals(self, query: str, *, city: str):
        response = await self._http.get('/v5/place/text', params={
            'key': self._api_key, 'keywords': query, 'region': city,
            'city_limit': 'true', 'show_fields': 'navi,business',
        })
        payload = response.json()
        if response.status_code in {401, 403} or payload.get('infocode') in {'10001', '10002'}:
            raise AmapProviderError('provider_auth_failed', '地图服务鉴权失败')
        if response.status_code >= 400 or payload.get('status') != '1':
            raise AmapProviderError('provider_unavailable', '地图服务暂不可用')
        return payload.get('pois', [])

    async def aclose(self):
        if self._owns_client:
            await self._http.aclose()
```

- [ ] **Step 5: Run the focused test and verify GREEN**

Run the Step 2 command. Expected: `1 passed`.

- [ ] **Step 6: Commit the configuration boundary**

```powershell
git add .env.example frontend/.env.example careguide/amap_client.py tests/test_amap_client.py
git commit -m "feat: define safe Amap configuration boundary"
```

### Task 2: Normalize hospital and parking POIs without inventing status

**Files:**
- Create: `careguide/amap_models.py`
- Modify: `careguide/amap_client.py`
- Modify: `tests/test_amap_client.py`

- [ ] **Step 1: Add failing normalization tests**

Append tests that script one hospital and one parking response:

```python
@pytest.mark.asyncio
async def test_search_parking_preserves_unknown_live_and_accessibility_fields():
    responses = iter([
        httpx.Response(200, json={'status': '1', 'pois': [{
            'id': 'H1', 'name': '泰州市人民医院（总院）',
            'address': '太湖路366号', 'location': '119.923000,32.455000',
            'navi': {'entr_location': '119.923100,32.455100'},
        }]}),
        httpx.Response(200, json={'status': '1', 'pois': [{
            'id': 'P1', 'name': '泰州市人民医院停车场',
            'address': '太湖路366号', 'location': '119.922800,32.455200',
            'distance': '85', 'business': {'parking_type': '地面'},
            'navi': {'entr_location': '119.922700,32.455300'},
        }]}),
    ])

    async def handle(_request):
        return next(responses)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        client = AmapClient('test-key', http_client=http)
        hospitals = await client.search_hospitals('泰州市人民医院', city='泰州')
        parking = await client.search_parking(hospitals[0], radius_m=1000)

    assert parking[0].model_dump()['available_spaces'] is None
    assert parking[0].model_dump()['wheelchair_accessible'] is None
    assert parking[0].data_tier == 'map_poi'
    assert parking[0].source_provider == 'amap'
    assert parking[0].distance_to_hospital_m == 85
```

- [ ] **Step 2: Run the new test and verify RED**

Expected: `AmapClient` has no `search_parking` method and returns raw dictionaries.

- [ ] **Step 3: Define the internal contracts**

Create `careguide/amap_models.py` with strict Pydantic models:

```python
from datetime import datetime
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field


class Coordinate(BaseModel):
    model_config = ConfigDict(extra='forbid')
    longitude: float = Field(ge=73, le=136)
    latitude: float = Field(ge=3, le=54)


class HospitalPoi(BaseModel):
    model_config = ConfigDict(extra='forbid')
    hospital_id: str
    name: str
    address: str
    location: Coordinate
    entrance_location: Coordinate | None = None
    data_tier: Literal['map_poi'] = 'map_poi'
    source_provider: Literal['amap'] = 'amap'
    observed_at: datetime


class ParkingCandidate(BaseModel):
    model_config = ConfigDict(extra='forbid')
    parking_id: str
    name: str
    address: str
    location: Coordinate
    entrance_location: Coordinate | None = None
    parking_type: str | None = None
    distance_to_hospital_m: int | None = None
    available_spaces: int | None = None
    wheelchair_accessible: bool | None = None
    data_tier: Literal['map_poi'] = 'map_poi'
    source_provider: Literal['amap'] = 'amap'
    observed_at: datetime
```

- [ ] **Step 4: Implement parsing and parking search**

Add `_coordinate`, `_request`, normalized `search_hospitals`, and `search_parking` to `AmapClient`. `search_parking` must call `/v5/place/around` with `keywords=停车场`, `location=<hospital coordinate>`, `radius`, `sortrule=distance`, and `show_fields=navi,business`. It must always construct `available_spaces=None` and `wheelchair_accessible=None`.

- [ ] **Step 5: Run client tests**

```powershell
.\.venv\Scripts\python.exe -m pytest -q tests\test_amap_client.py
```

Expected: all Amap client tests pass.

- [ ] **Step 6: Commit normalized POIs**

```powershell
git add careguide/amap_models.py careguide/amap_client.py tests/test_amap_client.py
git commit -m "feat: normalize Amap hospital and parking POIs"
```

### Task 3: Add read-only hospital, parking, and route APIs

**Files:**
- Modify: `careguide/amap_models.py`
- Modify: `careguide/amap_client.py`
- Modify: `careguide/api.py`
- Create: `tests/test_amap_api.py`

- [ ] **Step 1: Write failing API tests with a scripted dependency**

Create tests for:

```python
def test_real_parking_endpoint_never_returns_simulation_or_fake_availability():
    response = client.get('/map/hospitals/H1/parking?radius_m=1000')
    assert response.status_code == 200
    payload = response.json()
    assert payload['simulation'] is False
    assert payload['candidates'][0]['available_spaces'] is None
    assert payload['candidates'][0]['wheelchair_accessible'] is None
    assert 'SIM-A' not in response.text


def test_missing_provider_configuration_is_explicit(monkeypatch):
    monkeypatch.delenv('CAREGUIDE_AMAP_WEB_SERVICE_KEY', raising=False)
    get_amap_client.cache_clear()
    response = TestClient(app).get('/map/hospitals/search?q=泰州市人民医院&city=泰州')
    assert response.status_code == 503
    assert response.json()['detail']['code'] == 'provider_unconfigured'
```

Use `app.dependency_overrides[get_amap_client]` for the success path so tests make no network calls.

- [ ] **Step 2: Run endpoint tests and verify RED**

Expected: 404 because the `/map/...` routes do not exist.

- [ ] **Step 3: Define route contracts**

Add `RouteRequest`, `RouteStep`, and `MapRoute` to `amap_models.py`. `RouteRequest` accepts origin, destination, and `mode: Literal['walking', 'driving']`; `MapRoute` returns distance, duration when supplied, polyline points, `data_tier='map_poi'`, and `wheelchair_verified=False`.

- [ ] **Step 4: Implement Amap walking/driving routing**

Add `route(request)` to `AmapClient`:

- walking: `/v5/direction/walking`
- driving: `/v5/direction/driving`
- required params: `key`, `origin`, `destination`
- optional `show_fields`: `cost,polyline` for driving
- normalize provider paths and never label a walking route wheelchair accessible.

- [ ] **Step 5: Add cached dependency and endpoints**

In `careguide/api.py`, add an `@lru_cache` dependency that reads `CAREGUIDE_AMAP_WEB_SERVICE_KEY`, `CAREGUIDE_AMAP_BASE_URL`, and timeout. Add:

```python
@app.get('/map/hospitals/search')
async def search_map_hospitals(q: str, city: str = '泰州', amap=Depends(get_amap_client)): ...

@app.get('/map/hospitals/{hospital_id}/parking')
async def search_map_parking(hospital_id: str, radius_m: int = 1000,
                             amap=Depends(get_amap_client)): ...

@app.post('/map/routes')
async def create_map_route(request: RouteRequest, amap=Depends(get_amap_client)): ...
```

Provider errors map to safe structured HTTP responses. Extend `/health` with an Amap configuration status that contains only `configured: bool`, never the key.

- [ ] **Step 6: Run API and regression tests**

```powershell
.\.venv\Scripts\python.exe -m pytest -q tests\test_amap_api.py tests\test_amap_client.py tests\test_parking_replan_demo.py tests\test_auth_and_timing.py
```

Expected: all tests pass.

- [ ] **Step 7: Commit read-only map APIs**

```powershell
git add careguide/amap_models.py careguide/amap_client.py careguide/api.py tests/test_amap_api.py
git commit -m "feat: expose read-only Amap parking APIs"
```

### Task 4: Add frontend request and truth-boundary state

**Files:**
- Modify: `frontend/src/lib/parking.js`
- Modify: `frontend/tests/parking.test.js`

- [ ] **Step 1: Add failing frontend library tests**

Add tests that assert exact GET/POST paths and normalization:

```javascript
test('real parking candidates retain unknown live and accessibility state', async () => {
  const requester = async (path) => {
    assert.equal(path, '/map/hospitals/H1/parking?radius_m=1000')
    return { simulation: false, candidates: [{
      parking_id: 'P1', name: '总院停车场', available_spaces: null,
      wheelchair_accessible: null, data_tier: 'map_poi', source_provider: 'amap'
    }] }
  }
  const result = await requestParkingCandidates(requester, 'H1', 1000)
  assert.equal(result.candidates[0].availableSpaces, null)
  assert.equal(result.candidates[0].wheelchairAccessible, null)
  assert.equal(result.simulation, false)
})
```

- [ ] **Step 2: Run the test and verify RED**

```powershell
& 'D:\chrome\npm.cmd' test -- --run
```

Expected: import failure because `requestParkingCandidates` does not exist.

- [ ] **Step 3: Implement request builders and normalization**

Extend `frontend/src/lib/parking.js` with:

- `requestHospitalSearch(requester, query, city)`
- `requestParkingCandidates(requester, hospitalId, radiusM)`
- `requestMapRoute(requester, origin, destination, mode)`
- `normalizeHospital`, `normalizeParkingCandidate`, `normalizeRoute`

The normalizer must not convert `null` to zero or `false`.

- [ ] **Step 4: Run frontend tests and verify GREEN**

Expected: all existing and new tests pass.

- [ ] **Step 5: Commit frontend data boundary**

```powershell
git add frontend/src/lib/parking.js frontend/tests/parking.test.js
git commit -m "feat: add real parking map request client"
```

### Task 5: Load Amap JS API and render an isolated map component

**Files:**
- Create: `frontend/src/lib/amap-loader.js`
- Create: `frontend/tests/amap-loader.test.js`
- Create: `frontend/src/components/ParkingMap.vue`

- [ ] **Step 1: Write failing single-flight loader tests**

Test three behaviors using a minimal fake `document` and `window`: missing `VITE_AMAP_JS_KEY` rejects with `地图展示未配置`; two concurrent calls append one script; script error rejects with `地图加载失败`.

- [ ] **Step 2: Run loader tests and verify RED**

Expected: import failure because `amap-loader.js` does not exist.

- [ ] **Step 3: Implement the loader**

`loadAmap({ key, documentRef=globalThis.document, windowRef=globalThis.window })` must:

- return existing `windowRef.AMap` immediately;
- keep one module-level pending promise;
- append `https://webapi.amap.com/maps?v=2.0&key=<encoded>&plugin=AMap.Scale`;
- resolve only after `windowRef.AMap` exists;
- clear the pending promise after a load failure so retry is possible;
- never log the key.

- [ ] **Step 4: Implement `ParkingMap.vue`**

Props:

```javascript
hospital: { type: Object, default: null }
candidates: { type: Array, default: () => [] }
selectedParkingId: { type: String, default: '' }
route: { type: Object, default: null }
```

The component owns only Amap objects and emits `select-parking`. It creates hospital and parking markers, fits the view, highlights the selected marker, and draws a polyline from normalized route points. If loading fails, it emits `map-error` and renders a non-blocking message while the parent list stays visible.

- [ ] **Step 5: Run loader tests and build**

```powershell
& 'D:\chrome\npm.cmd' test -- --run
& 'D:\chrome\npm.cmd' run build
```

Expected: tests pass and Vite build exits 0.

- [ ] **Step 6: Commit the map renderer**

```powershell
git add frontend/src/lib/amap-loader.js frontend/tests/amap-loader.test.js frontend/src/components/ParkingMap.vue
git commit -m "feat: render Amap parking candidates"
```

### Task 6: Integrate real and simulation modes into the parking page

**Files:**
- Modify: `frontend/src/App.vue`
- Modify: `frontend/src/styles.css`
- Modify: `frontend/tests/parking.test.js`

- [ ] **Step 1: Add failing state tests**

Move mode/state transformations that do not require Vue DOM into `parking.js` and test:

- real mode results reject any candidate whose ID starts with `SIM-`;
- simulation mode never accepts `map_poi` candidates;
- labels are `实时车位未知` and `无障碍通行未核实` for `null` values;
- selecting a hospital clears previous parking candidates and route;
- selecting a parking candidate clears the previous route before loading another.

- [ ] **Step 2: Run tests and verify RED**

Expected: missing real-mode state helpers or incorrect labels.

- [ ] **Step 3: Add parking mode UI and state**

In `App.vue`:

- add `parkingMode = ref('real')`;
- add hospital query, hospital results, selected hospital, candidate list, selected parking, route, loading, and error state;
- import and render `ParkingMap` only in real mode;
- keep the existing simulation form and result only in simulation mode;
- show `map_poi · 高德地图 · 查询时间` on every real candidate;
- show unknown labels instead of `0`/`否`;
- request a route only after explicit candidate selection;
- never call `/demo/parking/replan` from real mode.

- [ ] **Step 4: Add styles**

Add focused classes for `.parking-mode-switch`, `.parking-map-shell`, `.parking-search-results`, `.source-badge`, `.unknown-status`, `.parking-detail-grid`, and mobile stacking. Preserve existing visual variables and responsive breakpoint.

- [ ] **Step 5: Run frontend tests and build**

```powershell
& 'D:\chrome\npm.cmd' test -- --run
& 'D:\chrome\npm.cmd' run build
```

Expected: all tests pass and build exits 0.

- [ ] **Step 6: Commit the integrated page**

```powershell
git add frontend/src/App.vue frontend/src/styles.css frontend/src/lib/parking.js frontend/tests/parking.test.js
git commit -m "feat: separate real map and parking simulation modes"
```

### Task 7: Configure local keys and run real-provider verification

**Files:**
- Modify locally only: `.env`
- Create locally only: `frontend/.env.local`
- Modify: `README.md`
- Create: `scripts/validate_amap_parking.py`

- [ ] **Step 1: Import keys without printing them**

Read the user-authorized local key file. Map the Web Service Key to `CAREGUIDE_AMAP_WEB_SERVICE_KEY` in `.env` and the JS API Key to `VITE_AMAP_JS_KEY` in `frontend/.env.local`. Print only `SET length=<n>` diagnostics. Confirm both local files remain ignored by Git.

- [ ] **Step 2: Add a real-provider validation script**

Create `scripts/validate_amap_parking.py` that:

- loads an explicitly supplied `--config-file`;
- refuses placeholder or missing keys;
- searches `泰州市人民医院` in `泰州`;
- selects only a result whose returned name and city match;
- searches parking within 1000 m;
- optionally creates one walking route to a returned candidate;
- prints IDs, names, distances, data tiers, and status only;
- never prints keys or full request URLs;
- exits nonzero on provider error, zero only when hospital and parking candidates are returned;
- writes no dataset or index files.

- [ ] **Step 3: Write README configuration and truth-boundary text**

Document the two key types, local filenames, start commands, real/simulation mode separation, and the rule that Amap POIs do not prove live availability or wheelchair accessibility.

- [ ] **Step 4: Run real-provider gate**

```powershell
.\.venv\Scripts\python.exe -m scripts.validate_amap_parking --config-file .env
```

Expected: matched Taizhou hospital, one or more parking candidates, no key in output, exit 0. If no parking POI is returned, report that real provider result without fabricating fallback data.

- [ ] **Step 5: Start services and verify through the frontend proxy**

Start backend on `127.0.0.1:8010` and frontend on `127.0.0.1:5175`. Verify:

- `/api/map/hospitals/search?q=泰州市人民医院&city=泰州` returns `simulation=false`;
- selected hospital parking candidates contain no `SIM-` IDs;
- candidate availability/accessibility are `null` unless a future trusted source supplies them;
- route response has `wheelchair_verified=false`;
- browser displays map markers and the candidate list.

- [ ] **Step 6: Run the complete regression set**

```powershell
.\.venv\Scripts\python.exe -m pytest -q tests\test_amap_client.py tests\test_amap_api.py tests\test_parking_replan_demo.py tests\test_chat_stream.py tests\test_auth_and_timing.py tests\test_real_provider_gate.py
& 'D:\chrome\npm.cmd' test -- --run
& 'D:\chrome\npm.cmd' run build
git diff --check
```

Expected: all commands exit 0; only existing dependency deprecation warnings may remain.

- [ ] **Step 7: Commit documentation and validation script**

```powershell
git add README.md scripts/validate_amap_parking.py
git commit -m "docs: add Amap parking verification guide"
```

## Final acceptance checklist

- [ ] Real mode searches a real Taizhou hospital through Amap Web Service API.
- [ ] Real mode shows only Amap-normalized parking candidates, never SIM-A/B/C.
- [ ] The interactive map renders hospital, candidates, and a selected route.
- [ ] The UI shows source, observation time, unknown live availability, and unverified accessibility.
- [ ] Simulation mode still demonstrates deterministic event replanning and stays visibly labelled.
- [ ] Backend key is absent from frontend files, responses, logs, traces, and build output.
- [ ] Missing/invalid keys, provider failures, no results, and map load failures are explicit and non-fabricating.
- [ ] Backend tests, frontend tests, production build, real-provider gate, and `git diff --check` pass.
