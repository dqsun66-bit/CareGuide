# Chat Amap Parking Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让 CareGuide 对话页识别真实医院停车查询，要求用户确认高德院区，随后返回真实停车 POI，并可把结果无损交给现有地图页。

**Architecture:** 在普通 Agent 前增加确定性的 `MapParkingChatService`，只处理“医院 + 停车”窄意图和结构化院区选择事件。候选集合按用户、会话、查询版本和有效期存入现有业务 SQLite；前端只提交候选中的 POI ID，不提交坐标。服务复用现有 `AmapClient` 和真实性模型，真实查询失败时不进入 SIM 模拟流程。

**Tech Stack:** Python 3.11、FastAPI、Pydantic v2、SQLite、httpx、pytest、Vue 3、Vite、Node test runner、Chrome DevTools Protocol 浏览器验收。

---

## File map

- Create `careguide/map_parking_chat.py`: 真实停车意图识别、院区查询、候选选择、停车查询、响应与 trace 生成。
- Modify `careguide/business_store.py`: 持久化并校验短期高德院区候选，不改变现有 handoff/knowledge 表语义。
- Modify `careguide/api.py`: 定义结构化地图选择事件，并把真实停车处理器放在模拟停车和通用 Agent 之前。
- Create `tests/test_map_parking_chat.py`: 服务级意图、状态隔离、真实性、错误与 trace 测试。
- Modify `tests/test_amap_api.py`: `/chat` 与 `/chat/stream` 的集成路由测试。
- Create `frontend/src/lib/chat-parking.js`: 构造选择事件、校验真实响应、创建地图接力状态。
- Modify `frontend/src/lib/backends.js`: 发送 `map_event` 并规范化 `map_selection`、`map_parking`。
- Modify `frontend/src/App.vue`: 渲染院区候选、提交结构化选择、将真实结果切换到地图页。
- Modify `frontend/src/styles.css`: 院区候选和停车摘要卡片样式。
- Create `frontend/tests/chat-parking.test.js`: 前端契约和真实/模拟隔离测试。
- Modify `frontend/tests/browser-workflow.mjs`: 在现有构建产物 Chrome/CDP 验收中加入真实停车对话流程。

### Task 1: Persist versioned hospital candidates

**Files:**
- Modify: `careguide/business_store.py`
- Test: `tests/test_map_parking_chat.py`

- [ ] **Step 1: Write failing store tests**

Create `tests/test_map_parking_chat.py` with isolated SQLite coverage:

```python
from datetime import datetime, timedelta, timezone

import pytest

from careguide.business_store import BusinessStore


NOW = datetime(2026, 10, 30, 8, tzinfo=timezone.utc)


def candidate(hospital_id='H1'):
    return {
        'hospital_id': hospital_id,
        'name': '南京医科大学附属泰州人民医院',
        'address': '太湖路366号',
        'city_name': '泰州市',
        'district_name': '医药高新区',
        'location': {'longitude': 119.923, 'latitude': 32.455},
        'entrance_location': None,
        'data_tier': 'map_poi',
        'source_provider': 'amap',
        'observed_at': NOW.isoformat(),
    }


def test_map_candidates_are_scoped_versioned_and_expiring(tmp_path):
    store = BusinessStore(tmp_path / 'business.sqlite3')
    saved = store.create_map_parking_query(
        'alice', 'conv-1', '泰州医院', '泰州', [candidate()],
        expires_at=NOW + timedelta(minutes=10), now=NOW,
    )
    assert saved['candidate_version'] == 1
    assert store.select_map_hospital(
        saved['query_id'], 'alice', 'conv-1', 1, 'H1', now=NOW,
    )['hospital_id'] == 'H1'
    with pytest.raises(PermissionError):
        store.select_map_hospital(saved['query_id'], 'bob', 'conv-1', 1, 'H1', now=NOW)
    with pytest.raises(PermissionError):
        store.select_map_hospital(saved['query_id'], 'alice', 'conv-2', 1, 'H1', now=NOW)
    with pytest.raises(ValueError):
        store.select_map_hospital(saved['query_id'], 'alice', 'conv-1', 2, 'H1', now=NOW)
    with pytest.raises(ValueError):
        store.select_map_hospital(saved['query_id'], 'alice', 'conv-1', 1, 'OTHER', now=NOW)
    with pytest.raises(TimeoutError):
        store.select_map_hospital(
            saved['query_id'], 'alice', 'conv-1', 1, 'H1',
            now=NOW + timedelta(minutes=11),
        )
```

- [ ] **Step 2: Run the test and verify the missing API**

Run:

```powershell
python -m pytest tests/test_map_parking_chat.py -q
```

Expected: FAIL because `BusinessStore` has no `create_map_parking_query` or `select_map_hospital`.

- [ ] **Step 3: Add the SQLite table and guarded methods**

Extend `BusinessStore.__init__` with:

```sql
CREATE TABLE IF NOT EXISTS map_parking_queries (
    id TEXT PRIMARY KEY,
    actor TEXT NOT NULL,
    conversation TEXT NOT NULL,
    version INTEGER NOT NULL,
    expires_at TEXT NOT NULL,
    payload TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS map_parking_query_scope
ON map_parking_queries(actor, conversation, expires_at);
```

Add these methods to `BusinessStore`:

```python
def create_map_parking_query(self, actor, conversation, query, city, hospitals,
                             *, expires_at, now=None):
    now = now or datetime.now(timezone.utc)
    identity = 'MAP-'+uuid.uuid4().hex
    item = {
        'query_id': identity,
        'candidate_version': 1,
        'actor': actor,
        'conversation': conversation,
        'query': query,
        'city': city,
        'hospitals': hospitals,
        'created_at': now.isoformat(),
        'expires_at': expires_at.isoformat(),
    }
    with self._lock, self.connect() as connection:
        connection.execute(
            'INSERT INTO map_parking_queries VALUES (?,?,?,?,?,?)',
            (identity, actor, conversation, 1, item['expires_at'],
             json.dumps(item, ensure_ascii=False)),
        )
    return item

def select_map_hospital(self, query_id, actor, conversation, version, hospital_id, *, now=None):
    now = now or datetime.now(timezone.utc)
    with self.connect() as connection:
        row = connection.execute(
            'SELECT actor,conversation,version,expires_at,payload '
            'FROM map_parking_queries WHERE id=?', (query_id,),
        ).fetchone()
    if row is None:
        raise LookupError('Map parking query not found')
    if row['actor'] != actor or row['conversation'] != conversation:
        raise PermissionError('Map parking query scope mismatch')
    if row['version'] != version:
        raise ValueError('Map parking candidate version mismatch')
    if datetime.fromisoformat(row['expires_at']) <= now:
        raise TimeoutError('Map parking query expired')
    payload = json.loads(row['payload'])
    hospital = next(
        (item for item in payload['hospitals'] if item['hospital_id'] == hospital_id), None,
    )
    if hospital is None:
        raise ValueError('Hospital is not in the candidate set')
    return hospital
```

- [ ] **Step 4: Run the store test**

Run `python -m pytest tests/test_map_parking_chat.py -q`.

Expected: PASS.

- [ ] **Step 5: Commit the candidate store**

```powershell
git add careguide/business_store.py tests/test_map_parking_chat.py
git commit -m "feat: persist scoped map hospital candidates"
```

### Task 2: Detect real parking intent and return hospital choices

**Files:**
- Create: `careguide/map_parking_chat.py`
- Modify: `tests/test_map_parking_chat.py`

- [ ] **Step 1: Add failing intent and candidate-response tests**

Append scripted dependencies and these tests:

```python
from careguide.amap_models import Coordinate, HospitalPoi
from careguide.map_parking_chat import MapParkingChatService
from careguide.trace_store import TraceStore


class ScriptedAmap:
    def __init__(self):
        self.searches = []

    async def search_hospitals(self, query, *, city):
        self.searches.append((query, city))
        return [HospitalPoi(
            hospital_id='H1', name='南京医科大学附属泰州人民医院', address='太湖路366号',
            city_name='泰州市', district_name='医药高新区',
            location=Coordinate(longitude=119.923, latitude=32.455), observed_at=NOW,
        )]


@pytest.mark.asyncio
async def test_real_parking_intent_returns_confirmable_amap_hospitals(tmp_path):
    amap = ScriptedAmap()
    service = MapParkingChatService(
        amap, BusinessStore(tmp_path / 'business.sqlite3'), TraceStore(None),
        now=lambda: NOW,
    )
    result = await service.handle('查询泰州医院停车场', 'alice', 'conv-1')
    assert amap.searches == [('泰州医院', '泰州')]
    assert result['intent'] == 'map_parking'
    assert result['workflow_status'] == 'waiting_user'
    assert result['simulation'] is False
    assert result['map_selection']['hospitals'][0]['hospital_id'] == 'H1'
    assert 'SIM-' not in str(result)


@pytest.mark.asyncio
async def test_unrelated_and_explicit_simulation_messages_are_not_claimed(tmp_path):
    service = MapParkingChatService(
        ScriptedAmap(), BusinessStore(tmp_path / 'business.sqlite3'), TraceStore(None),
        now=lambda: NOW,
    )
    assert await service.handle('头痛应该挂什么科', 'alice', 'conv-1') is None
    assert await service.handle(
        '运行SIM-A停车重规划，最多走200米', 'alice', 'conv-1',
    ) is None
```

- [ ] **Step 2: Run the new tests and verify failure**

Run `python -m pytest tests/test_map_parking_chat.py -q`.

Expected: FAIL because `careguide.map_parking_chat` does not exist.

- [ ] **Step 3: Implement the narrow handler**

Create `careguide/map_parking_chat.py` with:

```python
from datetime import datetime, timedelta, timezone
import re
import time
import uuid

from careguide.amap_models import HospitalPoi


PARKING = re.compile(r'停车场|停车|车停|开车去')
HOSPITAL = re.compile(r'医院|院区|就医地点')
SIMULATION = re.compile(r'SIM-[ABC]|模拟重规划|模拟停车', re.I)


def _hospital_query(message: str) -> str:
    value = re.sub(r'请|帮我|查询|查找|搜索|一下|附近|周边|停车场|停车|怎么停|哪里停', '', message)
    return value.strip(' ，。！？')


def _city_hint(message: str) -> str:
    match = re.search(r'([\u4e00-\u9fff]{2,8})市', message)
    if match:
        return match.group(1)
    for city in ('泰州', '南京', '上海', '北京', '广州', '深圳', '杭州', '苏州'):
        if city in message:
            return city
    return '泰州'


class MapParkingChatService:
    def __init__(self, amap, store, traces, *, now=None, ttl_minutes=10):
        self.amap = amap
        self.store = store
        self.traces = traces
        self.now = now or (lambda: datetime.now(timezone.utc))
        self.ttl_minutes = ttl_minutes

    @staticmethod
    def matches(message: str) -> bool:
        return bool(PARKING.search(message) and HOSPITAL.search(message)
                    and not SIMULATION.search(message))

    async def handle(self, message, user_id, conv_id, *, map_event=None):
        if map_event is not None:
            return await self._select(message, user_id, conv_id, map_event)
        if not self.matches(message):
            return None
        started = time.monotonic()
        query, city = _hospital_query(message), _city_hint(message)
        hospitals = await self.amap.search_hospitals(query, city=city)
        if not hospitals:
            return self._result(
                message, user_id, conv_id, started, 'completed',
                f'没有找到“{query}”对应的真实院区，请补充城市和医院正式名称。',
                tool_name='amap_search_hospitals', tool_input={'query': query, 'city': city},
                tool_output={'count': 0},
            )
        serialized = [item.model_dump(mode='json') for item in hospitals]
        saved = self.store.create_map_parking_query(
            user_id, conv_id, query, city, serialized,
            expires_at=self.now() + timedelta(minutes=self.ttl_minutes), now=self.now(),
        )
        response = f'找到 {len(hospitals)} 个高德院区候选，请先确认要前往的院区。'
        return self._result(
            message, user_id, conv_id, started, 'waiting_user', response,
            map_selection={
                'query_id': saved['query_id'],
                'candidate_version': saved['candidate_version'],
                'expires_at': saved['expires_at'],
                'hospitals': serialized,
            },
            tool_name='amap_search_hospitals', tool_input={'query': query, 'city': city},
            tool_output={'count': len(serialized), 'hospital_ids': [item['hospital_id'] for item in serialized]},
        )
```

Import `TRACE_VERSION` and `validate_trace_completeness`, then add this single response/trace builder to the class:

```python
def _result(self, message, user_id, conv_id, started, status, response, *,
            map_selection=None, map_parking=None, tool_name, tool_input, tool_output):
    request_id = uuid.uuid4().hex
    finished = self.now()
    finished_at = finished.isoformat()
    latency_ms = (time.monotonic() - started) * 1000
    started_at = datetime.fromtimestamp(
        finished.timestamp() - latency_ms / 1000, tz=timezone.utc,
    ).isoformat()
    tool_call = {
        'tool_name': tool_name,
        'agent_type': 'map_parking',
        'input': tool_input,
        'success': True,
        'output': tool_output,
        'error': None,
        'latency_ms': latency_ms,
    }
    trace = {
        'trace_version': TRACE_VERSION,
        'request_id': request_id,
        'conversation_id': conv_id,
        'user_id': user_id,
        'timestamp': finished_at,
        'input': message,
        'started_at': started_at,
        'finished_at': finished_at,
        'total_latency_ms': latency_ms,
        'status': status,
        'task_understanding': {
            'task_type': 'map_parking',
            'entities': {'data_tier': 'map_poi'},
            'missing_fields': {'map_parking': ['hospital_id']} if map_selection else {},
            'workflow_status_after_understanding': status,
        },
        'routing': {
            'primary_agent': 'map_parking',
            'supporting_agents': [],
            'routing_reason': '医院停车请求使用真实地图 POI 确定性流程',
            'routing_confidence': 1.0,
        },
        'execution': {
            'execution_waves': [{
                'wave': 1,
                'agents': [{
                    'agent_type': 'map_parking',
                    'role': 'map_parking_lookup',
                    'started_at': started_at,
                    'finished_at': finished_at,
                    'latency_ms': latency_ms,
                    'success': True,
                    'tools_used': [tool_name],
                    'error': None,
                }],
            }],
            'tool_calls': [tool_call],
            'budget': {'model_calls': 0, 'tool_calls': 1},
            'synthesis': {'mode': 'deterministic_map_lookup'},
        },
        'evidence': [],
        'side_effects': [],
        'final': {
            'workflow_status': status,
            'final_response': response,
            'agent_types': ['map_parking'],
            'primary_agent': 'map_parking',
            'supporting_agents': [],
            'total_tool_calls': 1,
            'total_latency_ms': latency_ms,
        },
    }
    completeness = validate_trace_completeness(trace)
    if not completeness['complete']:
        raise RuntimeError(f'Incomplete map parking trace: {completeness["missing_fields"]}')
    self.traces.append(trace)
    result = {
        'request_id': request_id,
        'conv_id': conv_id,
        'conversation_id': conv_id,
        'response': response,
        'workflow_status': status,
        'workflow': None,
        'errors': [],
        'evidence': [],
        'knowledge_used': False,
        'medical_answering_enabled': False,
        'grounded': True,
        'simulation': False,
        'action_requests': [],
        'appointment': [],
        'tools_used': [tool_name],
        'agent_types': ['map_parking'],
        'agent_type': 'map_parking',
        'primary_agent': 'map_parking',
        'supporting_agents': [],
        'intent': 'map_parking',
        'intents': ['map_parking'],
        'intent_group': 'map_parking',
        'routing_reason': '医院停车请求使用真实地图 POI 确定性流程',
        'routing_confidence': 1.0,
        'intent_confidence': 1.0,
        'execution_mode': 'deterministic_tool',
    }
    if map_selection is not None:
        result['map_selection'] = map_selection
    if map_parking is not None:
        result['map_parking'] = map_parking
    return result
```

- [ ] **Step 4: Run focused tests**

Run `python -m pytest tests/test_map_parking_chat.py -q`.

Expected: PASS, including one complete trace per handled request.

- [ ] **Step 5: Commit intent and candidate handling**

```powershell
git add careguide/map_parking_chat.py tests/test_map_parking_chat.py
git commit -m "feat: add real map parking chat intent"
```

### Task 3: Validate the selection and return truthful parking POIs

**Files:**
- Modify: `careguide/map_parking_chat.py`
- Modify: `tests/test_map_parking_chat.py`

- [ ] **Step 1: Add failing selection tests**

Extend `ScriptedAmap` with these exact methods:

```python
async def get_hospital(self, hospital_id):
    assert hospital_id == 'H1'
    return (await self.search_hospitals('泰州医院', city='泰州'))[0]

async def search_parking(self, hospital, *, radius_m):
    assert hospital.hospital_id == 'H1'
    assert radius_m == 1000
    return [ParkingCandidate(
        parking_id='P1', name='医院停车场', address='太湖路366号',
        location=Coordinate(longitude=119.9228, latitude=32.4552),
        distance_to_hospital_m=85, available_spaces=None,
        wheelchair_accessible=None, observed_at=NOW,
    )]
```

Add the following selection tests:

```python
from careguide.amap_models import ParkingCandidate


@pytest.mark.asyncio
async def test_selected_candidate_returns_truthful_parking_and_no_sim_ids(tmp_path):
    amap = ScriptedAmap()
    store = BusinessStore(tmp_path / 'business.sqlite3')
    service = MapParkingChatService(amap, store, TraceStore(None), now=lambda: NOW)
    first = await service.handle('查询泰州医院停车场', 'alice', 'conv-1')
    event = {
        'kind': 'map_hospital_select',
        'query_id': first['map_selection']['query_id'],
        'candidate_version': 1,
        'hospital_id': 'H1',
    }
    result = await service.handle('选择该院区', 'alice', 'conv-1', map_event=event)
    candidate = result['map_parking']['candidates'][0]
    assert result['workflow_status'] == 'completed'
    assert result['simulation'] is False
    assert candidate['available_spaces'] is None
    assert candidate['wheelchair_accessible'] is None
    assert candidate['data_tier'] == 'map_poi'
    assert 'SIM-' not in str(result)


@pytest.mark.asyncio
async def test_selection_rejects_forged_cross_scope_and_expired_candidates(tmp_path):
    amap = ScriptedAmap()
    store = BusinessStore(tmp_path / 'business.sqlite3')
    service = MapParkingChatService(amap, store, TraceStore(None), now=lambda: NOW)
    first = await service.handle('查询泰州医院停车场', 'alice', 'conv-1')
    base = {'kind': 'map_hospital_select', 'query_id': first['map_selection']['query_id'],
            'candidate_version': 1, 'hospital_id': 'H1'}
    with pytest.raises(PermissionError):
        await service.handle('选择', 'bob', 'conv-1', map_event=base)
    with pytest.raises(ValueError):
        await service.handle('选择', 'alice', 'conv-1', map_event={**base, 'hospital_id': 'FORGED'})
```

- [ ] **Step 2: Run and verify selection is unimplemented**

Run `python -m pytest tests/test_map_parking_chat.py -q`.

Expected: FAIL because `_select` is missing.

- [ ] **Step 3: Implement `_select` without trusting frontend coordinates**

Add this flow to `MapParkingChatService`:

```python
async def _select(self, message, user_id, conv_id, event):
    started = time.monotonic()
    stored = self.store.select_map_hospital(
        event['query_id'], user_id, conv_id,
        event['candidate_version'], event['hospital_id'], now=self.now(),
    )
    hospital = HospitalPoi.model_validate(stored)
    refreshed = await self.amap.get_hospital(hospital.hospital_id)
    hospital = refreshed or hospital
    candidates = await self.amap.search_parking(hospital, radius_m=1000)
    parking = [item.model_dump(mode='json') for item in candidates]
    response = (
        f'已确认院区：{hospital.name}。找到 {len(parking)} 个周边停车地点。'
        '高德不提供本项目可验证的实时余位；无障碍通行也尚未核实。'
    )
    return self._result(
        message, user_id, conv_id, started, 'completed', response,
        map_parking={
            'hospital': hospital.model_dump(mode='json'),
            'candidates': parking,
            'data_tier': 'map_poi',
            'source_provider': 'amap',
            'observed_at': self.now().isoformat(),
        },
        tool_name='amap_search_parking',
        tool_input={'hospital_id': hospital.hospital_id, 'radius_m': 1000},
        tool_output={'count': len(parking), 'parking_ids': [item['parking_id'] for item in parking]},
    )
```

Before `_select`, reject events whose `kind` is not `map_hospital_select`. Do not accept a hospital object, address, coordinate, radius, availability, or accessibility value from the browser.

- [ ] **Step 4: Add provider-error assertions**

Add a provider that fails with a safe code and verify the exception contains no credential or URL:

```python
from careguide.amap_client import AmapProviderError


class FailingAmap:
    def __init__(self, code):
        self.code = code

    async def search_hospitals(self, query, *, city):
        raise AmapProviderError(self.code, '地图服务暂不可用')


@pytest.mark.asyncio
@pytest.mark.parametrize('code', [
    'provider_auth_failed', 'provider_rate_limited', 'provider_timeout',
])
async def test_provider_errors_are_safe_and_never_fall_back_to_simulation(tmp_path, code):
    service = MapParkingChatService(
        FailingAmap(code), BusinessStore(tmp_path / f'{code}.sqlite3'), TraceStore(None),
        now=lambda: NOW,
    )
    with pytest.raises(AmapProviderError) as caught:
        await service.handle('查询泰州医院停车场', 'alice', 'conv-1')
    assert caught.value.code == code
    assert 'key=' not in str(caught.value)
    assert 'http' not in str(caught.value)
    assert 'SIM-' not in str(caught.value)
```

Also make `ScriptedAmap.search_parking` return `[]` in one test and assert the completed `map_parking` preserves the confirmed hospital, has `candidates == []`, and tells the user that no result was found within 1 kilometre and that they may retry with a wider range on the map page.

- [ ] **Step 5: Run the service suite**

Run `python -m pytest tests/test_map_parking_chat.py -q`.

Expected: PASS.

- [ ] **Step 6: Commit selection and parking results**

```powershell
git add careguide/map_parking_chat.py tests/test_map_parking_chat.py
git commit -m "feat: resolve selected hospital parking in chat"
```

### Task 4: Route `/chat` and `/chat/stream` through the service

**Files:**
- Modify: `careguide/api.py`
- Modify: `tests/test_amap_api.py`

- [ ] **Step 1: Add failing API routing tests**

Add a `decode_sse(text)` helper that parses `event:` and `data:` lines into `(event, payload)` tuples. Override `get_amap_client` with `ScriptedAmap`, send this initial request to both `/chat` and `/chat/stream`, and assert:

```python
initial = {'message': '查询泰州医院停车场', 'user_id': 'alice', 'conv_id': 'map-conv'}
plain = client.post('/chat', json=initial)
assert plain.status_code == 200
assert plain.json()['intent'] == 'map_parking'
assert plain.json()['workflow_status'] == 'waiting_user'
assert plain.json()['workflow'] is None
assert plain.json()['simulation'] is False

streamed = client.post('/chat/stream', json={**initial, 'conv_id': 'stream-map-conv'})
final = decode_sse(streamed.text)[-1]
assert final[0] == 'result'
assert final[1]['intent'] == 'map_parking'
assert final[1]['map_selection']['hospitals'][0]['hospital_id'] == 'H1'
```

Use the `query_id` returned by the plain response in the selection request:

```json
{
  "message": "选择这个院区",
  "user_id": "alice",
  "conv_id": "map-conv",
  "map_event": {
    "kind": "map_hospital_select",
    "query_id": "<returned query id>",
    "candidate_version": 1,
    "hospital_id": "H1"
  }
}
```

Assert `map_parking.candidates[0].data_tier == 'map_poi'` and `simulation is False`.

- [ ] **Step 2: Run the API tests and observe the schema rejection**

Run:

```powershell
python -m pytest tests/test_amap_api.py -q
```

Expected: FAIL with 422 for `map_event` or the old appointment route.

- [ ] **Step 3: Add the strict event model and service dependency**

In `careguide/api.py`, define:

```python
class MapHospitalSelectEvent(BaseModel):
    model_config = ConfigDict(extra='forbid')
    kind: Literal['map_hospital_select']
    query_id: str = Field(min_length=1, max_length=100)
    candidate_version: int = Field(ge=1)
    hospital_id: str = Field(min_length=1, max_length=100)
```

Add `map_event: MapHospitalSelectEvent | None = None` to `ChatRequest`. Import `Literal` and `MapParkingChatService`.

In `_execute_chat_result`, before `parking_chat_response`, run:

```python
map_result = None
if request.map_event or MapParkingChatService.matches(request.message):
    map_result = await MapParkingChatService(
        get_amap_client(), engine.business_store, engine.traces,
    ).handle(
        request.message, request.user_id, conv_id,
        map_event=request.map_event.model_dump() if request.map_event else None,
    )
result = map_result or parking_chat_response(
    request.message, request.user_id, conv_id, engine.traces,
) or await engine.process(request.message, request.user_id, conv_id, history)
```

Only call `get_amap_client()` when `request.map_event` exists or `MapParkingChatService.matches(request.message)` is true, so unrelated medical chat still works without a configured map key.

Map exceptions as follows:

```python
except AmapProviderError as exc:
    raise _map_provider_error(exc) from exc
except TimeoutError as exc:
    raise HTTPException(410, '地图候选已过期，请重新查询医院') from exc
```

- [ ] **Step 4: Verify JSON, SSE, ordinary chat, and simulation routing**

Run:

```powershell
python -m pytest tests/test_amap_api.py tests/test_chat_stream.py tests/test_parking_replan_demo.py -q
```

Expected: PASS. Ordinary drug/appointment requests remain independent of Amap configuration; explicit SIM requests still use the simulation handler.

- [ ] **Step 5: Commit API integration**

```powershell
git add careguide/api.py tests/test_amap_api.py
git commit -m "feat: route chat parking queries to Amap"
```

### Task 5: Add frontend structured-event and handoff contracts

**Files:**
- Create: `frontend/src/lib/chat-parking.js`
- Modify: `frontend/src/lib/backends.js`
- Create: `frontend/tests/chat-parking.test.js`

- [ ] **Step 1: Write failing frontend contract tests**

Create `frontend/tests/chat-parking.test.js`:

```javascript
import test from 'node:test'
import assert from 'node:assert/strict'
import { hospitalSelectionEvent, mapHandoff } from '../src/lib/chat-parking.js'

test('hospital selection submits only the scoped candidate identity', () => {
  assert.deepEqual(hospitalSelectionEvent({ query_id: 'Q1', candidate_version: 2 }, { hospital_id: 'H1' }), {
    kind: 'map_hospital_select', query_id: 'Q1', candidate_version: 2, hospital_id: 'H1',
  })
  assert.throws(() => hospitalSelectionEvent({ query_id: 'Q1', candidate_version: 2 }, { hospital_id: 'SIM-A' }))
})

test('map handoff accepts only real map POIs and preserves unknown truth fields', () => {
  const value = mapHandoff({ hospital: { hospital_id: 'H1', data_tier: 'map_poi' }, candidates: [{
    parking_id: 'P1', data_tier: 'map_poi', available_spaces: null,
    wheelchair_accessible: null,
  }] })
  assert.equal(value.mode, 'real')
  assert.equal(value.candidates[0].available_spaces, null)
  assert.throws(() => mapHandoff({ hospital: { hospital_id: 'H1' }, candidates: [{ parking_id: 'SIM-A' }] }))
})
```

- [ ] **Step 2: Run and verify the missing module**

Run `npm test --prefix frontend`.

Expected: FAIL because `chat-parking.js` does not exist.

- [ ] **Step 3: Implement the pure helpers**

Create `frontend/src/lib/chat-parking.js`:

```javascript
export function hospitalSelectionEvent(selection, hospital) {
  const event = {
    kind: 'map_hospital_select',
    query_id: selection?.query_id,
    candidate_version: selection?.candidate_version,
    hospital_id: hospital?.hospital_id,
  }
  if (!event.query_id || !Number.isSafeInteger(event.candidate_version) ||
      !event.hospital_id || event.hospital_id.startsWith('SIM-')) {
    throw new Error('医院候选已失效，请重新查询')
  }
  return event
}

export function mapHandoff(value) {
  const hospital = value?.hospital
  const candidates = Array.isArray(value?.candidates) ? value.candidates : []
  if (hospital?.data_tier !== 'map_poi' || candidates.some(item =>
    item?.data_tier !== 'map_poi' || String(item.parking_id || '').startsWith('SIM-'))) {
    throw new Error('对话结果不是可用的真实地图数据')
  }
  return {
    mode: 'real', hospital,
    candidates: candidates.map(item => ({
      ...item,
      available_spaces: item.available_spaces ?? null,
      wheelchair_accessible: item.wheelchair_accessible ?? null,
    })),
  }
}
```

- [ ] **Step 4: Extend the backend adapter**

Change the two request functions to these signatures and payload handling:

```javascript
export async function requestChat(type, settings, message, signal, workflowEvent, onEvent, mapEvent) {
  const payload = buildChatPayload(type, settings, message)
  if (workflowEvent) payload.workflow_event = workflowEvent
  if (mapEvent) payload.map_event = mapEvent
  const base = backendMeta(type, settings).baseUrl
  const raw = await postChatStream(`${base}/chat/stream`, payload, {
    signal, onEvent, headers: authHeaders(base),
  })
  return normalizeChatResponse(type, raw)
}

export async function requestChatJson(type, settings, message, signal, workflowEvent, mapEvent) {
  const meta = backendMeta(type, settings)
  const payload = buildChatPayload(type, settings, message)
  if (workflowEvent) payload.workflow_event = workflowEvent
  if (mapEvent) payload.map_event = mapEvent
  const raw = await requestJson(meta.baseUrl, '/chat', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload), signal, timeoutMs: 90000,
  })
  return normalizeChatResponse(type, raw)
}
```

Extend `normalizeChatResponse` with:

```javascript
mapSelection: payload.map_selection || payload.mapSelection || null,
mapParking: payload.map_parking || payload.mapParking || null,
```

Do not map `map_event` into `workflow_event`; the two state machines remain separate.

- [ ] **Step 5: Run frontend unit tests and build**

```powershell
npm test --prefix frontend
npm run build --prefix frontend
```

Expected: all tests PASS and Vite build succeeds.

- [ ] **Step 6: Commit frontend contracts**

```powershell
git add frontend/src/lib/chat-parking.js frontend/src/lib/backends.js frontend/tests/chat-parking.test.js
git commit -m "feat: add chat parking frontend contracts"
```

### Task 6: Render hospital choices and open the real map

**Files:**
- Modify: `frontend/src/App.vue`
- Modify: `frontend/src/styles.css`
- Modify: `frontend/tests/browser-workflow.mjs`

- [ ] **Step 1: Add a failing built-browser acceptance scenario**

In `frontend/tests/browser-workflow.mjs`, add these immutable fixtures beside the existing workflow fixtures:

```javascript
const mapHospital = {
  hospital_id: 'H1', name: '南京医科大学附属泰州人民医院', address: '太湖路366号',
  city_name: '泰州市', district_name: '医药高新区',
  location: { longitude: 119.923, latitude: 32.455 },
  entrance_location: null, data_tier: 'map_poi', source_provider: 'amap',
  observed_at: '2026-10-30T08:00:00Z',
}
const mapCandidate = {
  parking_id: 'P1', name: '医院停车场', address: '太湖路366号',
  location: { longitude: 119.9228, latitude: 32.4552 }, entrance_location: null,
  parking_type: null, distance_to_hospital_m: 85,
  available_spaces: null, wheelchair_accessible: null,
  data_tier: 'map_poi', source_provider: 'amap', observed_at: '2026-10-30T08:00:00Z',
}
```

At the start of the `/api/chat/stream` branch, before existing workflow-event handling, choose this terminal result when the request is a map query or selection:

```javascript
if (body.message === '查询泰州医院停车场') {
  result = {
    conversation_id: 'browser-conversation', request_id: 'map-search',
    response: '找到 1 个高德院区候选，请先确认要前往的院区。',
    intent: 'map_parking', workflow_status: 'waiting_user', workflow: null,
    simulation: false,
    map_selection: { query_id: 'Q1', candidate_version: 1,
      expires_at: '2026-10-30T08:10:00Z', hospitals: [mapHospital] },
  }
} else if (body.map_event) {
  assert.deepEqual(body.map_event, {
    kind: 'map_hospital_select', query_id: 'Q1', candidate_version: 1, hospital_id: 'H1',
  })
  assert.equal('location' in body.map_event, false)
  result = {
    conversation_id: 'browser-conversation', request_id: 'map-parking',
    response: '已确认院区，找到 1 个周边停车地点。',
    intent: 'map_parking', workflow_status: 'completed', workflow: null,
    simulation: false,
    map_parking: { hospital: mapHospital, candidates: [mapCandidate],
      data_tier: 'map_poi', source_provider: 'amap' },
  }
} else {
  result = response()
}
```

Keep the existing SSE `session`, `stage`, and terminal `result` emission. Only gate the original first workflow request on `releaseFirstStream`; do not gate either map response.

Append this browser scenario after the existing workflow assertions and before the final request-count assertions:

```javascript
await send('查询泰州医院停车场')
await until(`document.querySelector('[data-testid="map-hospital-H1"]') !== null`)
await evaluate(`document.querySelector('[data-testid="map-hospital-H1"]').click()`)
await until(`document.querySelector('[data-testid="open-map-parking"]') !== null`)
await evaluate(`document.querySelector('[data-testid="open-map-parking"]').click()`)
await until(`document.body.textContent.includes('医院停车与行程规划')`)
assert.equal(await evaluate(`document.body.textContent.includes('实时车位未知')`), true)
assert.equal(await evaluate(`document.body.textContent.includes('无障碍通行未核实')`), true)
assert.equal(await evaluate(`document.body.textContent.includes('SIM-A')`), false)
```

- [ ] **Step 2: Run the browser test and verify the UI is absent**

```powershell
npm run build --prefix frontend
node frontend/tests/browser-workflow.mjs
```

Expected: FAIL because the candidate button is not rendered.

- [ ] **Step 3: Render candidate and parking cards in each assistant message**

In `App.vue`, add below `AnswerBody`:

```vue
<section v-if="item.mapSelection" class="chat-map-card">
  <strong>请选择真实院区</strong>
  <button v-for="hospital in item.mapSelection.hospitals" :key="hospital.hospital_id"
    :data-testid="`map-hospital-${hospital.hospital_id}`"
    :disabled="busy || item.mapSelection.resolved"
    @click="selectChatHospital(item, hospital)">
    <span>{{ hospital.name }}</span>
    <small>{{ hospital.address || '地址未提供' }} · {{ hospital.city_name || '' }} {{ hospital.district_name || '' }}</small>
    <small>高德地图 · map_poi</small>
  </button>
</section>
<section v-if="item.mapParking" class="chat-map-card">
  <strong>{{ item.mapParking.hospital.name }}</strong>
  <article v-for="candidate in item.mapParking.candidates" :key="candidate.parking_id">
    <span>{{ candidate.name }}</span>
    <small>{{ parkingTruthLabels(candidate).availability }} · {{ parkingTruthLabels(candidate).accessibility }}</small>
  </article>
  <button data-testid="open-map-parking" @click="openChatParkingMap(item.mapParking)">在地图中查看</button>
</section>
```

- [ ] **Step 4: Submit structured events and attach result fields**

Import `hospitalSelectionEvent` and `mapHandoff`. Extend `sendMessage` to accept `mapEvent`, pass it separately to `requestChat`, and attach terminal fields:

```javascript
mapSelection: response.mapSelection,
mapParking: response.mapParking,
```

Implement:

```javascript
async function selectChatHospital(item, hospital) {
  const event = hospitalSelectionEvent(item.mapSelection, hospital)
  item.mapSelection.resolved = true
  try {
    await sendMessage(undefined, event, `选择院区：${hospital.name}`)
  } catch (error) {
    item.mapSelection.resolved = false
    throw error
  }
}

function openChatParkingMap(value) {
  const handoff = mapHandoff(value)
  parkingMode.value = handoff.mode
  selectedHospital.value = handoff.hospital
  mapHospitals.value = [handoff.hospital]
  mapParking.value = handoff.candidates
  selectedParking.value = null
  mapRoute.value = null
  activeView.value = 'parking'
}
```

When rendering the synthetic user message for a selection, use the supplied display string, not a reconstruction from event fields.

- [ ] **Step 5: Add scoped styles**

In `frontend/src/styles.css`, add:

```css
.chat-map-card {
  display: grid;
  gap: 10px;
  margin-top: 12px;
  padding: 14px;
  border: 1px solid var(--line);
  border-radius: 14px;
  background: var(--panel-2);
}

.chat-map-card > button,
.chat-map-card > article {
  display: grid;
  gap: 4px;
  width: 100%;
  padding: 12px;
  text-align: left;
  border-radius: 12px;
}

.chat-map-card > button:focus-visible {
  outline: 3px solid color-mix(in srgb, var(--accent) 35%, transparent);
  outline-offset: 2px;
}

.chat-map-card small {
  color: var(--muted);
}

@media (max-width: 760px) {
  .chat-map-card { padding: 10px; }
}
```

These are existing theme tokens; do not add duplicate color variables.

- [ ] **Step 6: Run browser, unit, and build checks**

```powershell
npm test --prefix frontend
npm run build --prefix frontend
node frontend/tests/browser-workflow.mjs
```

Expected: PASS. The browser test records exactly two chat requests and the second contains one scoped `map_event`.

- [ ] **Step 7: Commit UI integration**

```powershell
git add frontend/src/App.vue frontend/src/styles.css frontend/tests/browser-workflow.mjs
git commit -m "feat: connect chat parking results to map"
```

### Task 7: Full regression and live-provider acceptance

**Files:**
- Modify only if a failing test exposes a defect in files already listed above.

- [ ] **Step 1: Run focused backend tests**

```powershell
python -m pytest tests/test_map_parking_chat.py tests/test_amap_client.py tests/test_amap_api.py tests/test_chat_stream.py tests/test_parking_replan_demo.py -q
```

Expected: PASS.

- [ ] **Step 2: Run frontend regression and production build**

```powershell
npm test --prefix frontend
npm run build --prefix frontend
node frontend/tests/browser-workflow.mjs
```

Expected: PASS.

- [ ] **Step 3: Run the broader stable backend suite**

Run the repository's stable suite while excluding already documented optional/mutated dataset suites:

```powershell
python -m pytest tests -q --ignore=tests/test_careguide_kuake_rag.py --ignore=tests/test_kuake_ir_dense.py --ignore=tests/test_real_provider_gate.py
```

Expected: no new failures. Record existing environment-only failures separately; do not edit frozen datasets or unrelated dirty files to make them disappear.

- [ ] **Step 4: Validate against the configured live Amap provider**

With backend key loaded from the ignored `.env`, start the application and verify:

1. “查询泰州医院停车场” produces real candidate buttons.
2. Selecting “南京医科大学附属泰州人民医院” returns real parking POIs.
3. “在地图中查看” transfers the same hospital and candidate IDs to the map page.
4. The UI states “实时车位未知” and “无障碍通行未核实”.
5. Network payloads, trace view, logs, and built assets contain no backend Web Service key.

- [ ] **Step 5: Review the diff for scope and secrets**

```powershell
git diff --check
git status --short
git diff --stat HEAD~6..HEAD
rg -n "CAREGUIDE_AMAP_WEB_SERVICE_KEY|api_key|secret" careguide frontend/src tests -g '!frontend/dist/**'
```

Expected: only intentional source/test/docs changes; no key value, `.env`, logs, runtime database, generated `dist`, frozen corpus, or unrelated user changes staged.

- [ ] **Step 6: Final code review and handoff**

Review these invariants directly in the diff:

- real parking handler precedes simulation and generic Agent routing;
- unrelated chat does not require Amap configuration;
- selection is bound to user, conversation, query ID, version, expiry, and stored POI set;
- frontend never submits coordinates or truth fields;
- all real candidates use `map_poi`, unknown availability/accessibility remain `null`;
- no provider failure produces SIM data;
- both `/chat` and `/chat/stream` return identical terminal contracts;
- backend key is absent from response, trace, logs, and frontend source/build.

If all checks pass, report the commits, exact test counts, live-provider result, and any unrelated pre-existing dirty files left untouched.
