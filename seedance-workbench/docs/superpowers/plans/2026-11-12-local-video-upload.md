# Seedance Local Video Upload Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a zero-configuration local video picker that validates MP4/MOV files, exposes them through a narrowly scoped temporary Cloudflare Quick Tunnel, submits the resulting HTTPS references to Seedance, and cleans everything after task completion.

**Architecture:** A `VideoUploadManager` owns validated temporary files and opaque upload IDs. A loopback-only `TemporaryMediaServer` serves tokenized `GET`/`HEAD`/`Range` requests, while `QuickTunnel` provides the public HTTPS origin. FastAPI routes expose upload metadata—not public URLs—to the frontend, and generation submission resolves upload IDs immediately before building the upstream payload. A background cleanup loop retains referenced files through task completion plus a grace period.

**Tech Stack:** Python 3.11+, FastAPI, asyncio, SQLite/aiosqlite, `ffprobe`, `cloudflared`, Vue 3, Vite, Node test runner, pytest.

---

## File structure

- Create `backend/video_probe.py`: safe streamed file validation and `ffprobe` metadata parsing.
- Create `backend/media_server.py`: loopback-only tokenized media server with byte-range support.
- Create `backend/quick_tunnel.py`: supervised `cloudflared` subprocess and public URL discovery.
- Create `backend/video_uploads.py`: upload lifecycle, persistence coordination, resolution, reference tracking, and cleanup.
- Create `backend/tests/test_video_probe.py`: video validation unit tests.
- Create `backend/tests/test_media_server.py`: HTTP method/token/range tests.
- Create `backend/tests/test_quick_tunnel.py`: fake-process tunnel lifecycle tests.
- Create `backend/tests/test_video_uploads.py`: manager and cleanup tests.
- Create `backend/tests/test_video_upload_api.py`: upload/generation integration tests.
- Modify `backend/config.py`: upload, tunnel, timeout, and cleanup settings.
- Modify `backend/store.py`: `video_uploads` table and focused persistence methods.
- Modify `backend/routes.py`: upload/remove routes, generation resolution, application lifecycle.
- Modify `backend/poller.py`: notify the upload manager when tasks reach terminal state.
- Modify `backend/validation.py`: combine resolved local uploads with normal video references.
- Modify `backend/tests/conftest.py`: fake probe/tunnel fixtures and test settings.
- Modify `frontend/src/api.js`: upload/remove requests and `video_upload_ids` form fields.
- Modify `frontend/src/App.vue`: local video draft state and upload orchestration.
- Modify `frontend/src/components/CreatorPanel.vue`: picker, progress/status, metadata, retry, and removal UI.
- Modify `frontend/src/constants.js`: local video formats and size constants.
- Modify `frontend/src/styles.css`: local video item states.
- Modify `frontend/tests/api.test.js`: request and material numbering tests.
- Modify `.env.example` and `README.md`: installation, configuration, privacy, and troubleshooting.

### Task 1: Configuration and media probing

**Files:**
- Create: `backend/video_probe.py`
- Create: `backend/tests/test_video_probe.py`
- Modify: `backend/config.py`
- Modify: `.env.example`

- [ ] **Step 1: Write failing configuration and probe tests**

```python
# backend/tests/test_video_probe.py
from pathlib import Path
from types import SimpleNamespace

import pytest

from backend.config import Settings
from backend.video_probe import VideoProbeError, probe_video


def test_video_upload_defaults(tmp_path):
    settings = Settings(data_dir=tmp_path)
    assert settings.max_video_bytes == 50 * 1024 * 1024
    assert settings.video_upload_ttl == 1800
    assert settings.video_post_task_grace == 300
    assert settings.tunnel_start_timeout == 30


def test_probe_accepts_mp4_with_video_stream(tmp_path: Path, monkeypatch):
    video = tmp_path / "dance.mp4"
    video.write_bytes(b"\x00\x00\x00\x18ftypisom" + b"x" * 64)
    monkeypatch.setattr(
        "backend.video_probe.subprocess.run",
        lambda *a, **k: SimpleNamespace(
            returncode=0,
            stdout='{"format":{"duration":"12.5","format_name":"mov,mp4"},'
            '"streams":[{"codec_type":"video"}]}',
            stderr="",
        ),
    )
    result = probe_video(video, "dance.mp4", "video/mp4")
    assert result.duration == 12.5
    assert result.content_type == "video/mp4"


@pytest.mark.parametrize("duration", [1.99, 15.01])
def test_probe_rejects_duration_outside_platform_limit(tmp_path, monkeypatch, duration):
    video = tmp_path / "dance.mp4"
    video.write_bytes(b"\x00\x00\x00\x18ftypisom" + b"x" * 64)
    monkeypatch.setattr(
        "backend.video_probe.subprocess.run",
        lambda *a, **k: SimpleNamespace(
            returncode=0,
            stdout=f'{{"format":{{"duration":"{duration}"}},"streams":[{{"codec_type":"video"}}]}}',
            stderr="",
        ),
    )
    with pytest.raises(VideoProbeError, match="2～15"):
        probe_video(video, "dance.mp4", "video/mp4")
```

- [ ] **Step 2: Run tests and verify RED**

Run: `python -m pytest backend/tests/test_video_probe.py -q`

Expected: collection fails with `ModuleNotFoundError: backend.video_probe` or `Settings` missing the new fields.

- [ ] **Step 3: Implement the minimal probe and settings**

```python
# backend/video_probe.py
from dataclasses import dataclass
import json
from pathlib import Path
import subprocess


class VideoProbeError(ValueError):
    pass


@dataclass(frozen=True)
class VideoMetadata:
    duration: float
    content_type: str
    size: int


def probe_video(path: Path, filename: str, declared_type: str) -> VideoMetadata:
    suffix = Path(filename).suffix.casefold()
    if suffix not in {".mp4", ".mov"}:
        raise VideoProbeError("仅支持 MP4 或 MOV 视频")
    header = path.read_bytes()[:16]
    if len(header) < 12 or header[4:8] != b"ftyp":
        raise VideoProbeError("视频内容与文件扩展名不符")
    completed = subprocess.run(
        ["ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", str(path)],
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    if completed.returncode != 0:
        raise VideoProbeError("无法读取视频信息，请确认 ffprobe 已安装且文件未损坏")
    payload = json.loads(completed.stdout)
    if not any(stream.get("codec_type") == "video" for stream in payload.get("streams", [])):
        raise VideoProbeError("文件中没有视频流")
    duration = float(payload.get("format", {}).get("duration", 0))
    if not 2 <= duration <= 15:
        raise VideoProbeError("视频时长必须为 2～15 秒")
    content_type = "video/quicktime" if suffix == ".mov" else "video/mp4"
    return VideoMetadata(duration=duration, content_type=content_type, size=path.stat().st_size)
```

Add these fields to `Settings` and load their matching environment variables:

```python
max_video_bytes: int = 50 * 1024 * 1024
cloudflared_path: str = ""
video_upload_ttl: float = 1800.0
video_post_task_grace: float = 300.0
tunnel_start_timeout: float = 30.0
```

- [ ] **Step 4: Run tests and verify GREEN**

Run: `python -m pytest backend/tests/test_video_probe.py backend/tests/test_validation.py -q`

Expected: all selected tests pass.

- [ ] **Step 5: Commit**

```powershell
git add seedance-workbench/backend/video_probe.py seedance-workbench/backend/config.py seedance-workbench/backend/tests/test_video_probe.py seedance-workbench/.env.example
git commit -m "feat(seedance): validate local video uploads"
```

### Task 2: Tokenized loopback media server

**Files:**
- Create: `backend/media_server.py`
- Create: `backend/tests/test_media_server.py`

- [ ] **Step 1: Write failing HTTP behavior tests**

```python
# backend/tests/test_media_server.py
import httpx

from backend.media_server import TemporaryMediaServer


def test_media_server_requires_token_and_supports_head_and_range(tmp_path):
    video = tmp_path / "clip.mp4"
    video.write_bytes(b"0123456789")
    server = TemporaryMediaServer()
    server.start()
    try:
        server.publish("secret-token", video, "video/mp4")
        base = f"http://127.0.0.1:{server.port}"
        assert httpx.get(f"{base}/media/wrong").status_code == 404
        head = httpx.head(f"{base}/media/secret-token")
        assert head.status_code == 200
        assert head.headers["content-length"] == "10"
        ranged = httpx.get(
            f"{base}/media/secret-token", headers={"Range": "bytes=2-5"}
        )
        assert ranged.status_code == 206
        assert ranged.content == b"2345"
        assert ranged.headers["content-range"] == "bytes 2-5/10"
        assert httpx.post(f"{base}/media/secret-token").status_code == 405
    finally:
        server.stop()
```

- [ ] **Step 2: Run test and verify RED**

Run: `python -m pytest backend/tests/test_media_server.py -q`

Expected: import fails because `backend.media_server` does not exist.

- [ ] **Step 3: Implement the server**

Implement `TemporaryMediaServer` with five public operations: a read-only integer `port` property, synchronous `start()`, `publish(token, path, content_type)`, `unpublish(token)`, and idempotent `stop()`.

Use `ThreadingHTTPServer(("127.0.0.1", 0), Handler)`, an in-memory immutable snapshot of token mappings protected by a lock, `hmac.compare_digest` when matching tokens, and explicit byte-range parsing. Override request logging so tokens never enter application logs. Return `416` with `Content-Range: bytes */<size>` for invalid ranges.

- [ ] **Step 4: Run tests and verify GREEN**

Run: `python -m pytest backend/tests/test_media_server.py -q`

Expected: the method, token, HEAD, full GET, valid range, and invalid range tests pass.

- [ ] **Step 5: Commit**

```powershell
git add seedance-workbench/backend/media_server.py seedance-workbench/backend/tests/test_media_server.py
git commit -m "feat(seedance): serve temporary tokenized videos"
```

### Task 3: Supervised Cloudflare Quick Tunnel

**Files:**
- Create: `backend/quick_tunnel.py`
- Create: `backend/tests/test_quick_tunnel.py`

- [ ] **Step 1: Write failing tunnel lifecycle tests**

```python
# backend/tests/test_quick_tunnel.py
import asyncio
from pathlib import Path

import pytest

from backend.quick_tunnel import QuickTunnel, TunnelError


@pytest.mark.asyncio
async def test_tunnel_discovers_https_url_from_stderr(tmp_path: Path):
    fake = tmp_path / "fake-cloudflared.py"
    fake.write_text(
        "import sys,time\n"
        "print('https://unit-test.trycloudflare.com', file=sys.stderr, flush=True)\n"
        "time.sleep(60)\n",
        encoding="utf-8",
    )
    tunnel = QuickTunnel(["python", str(fake)], timeout=2)
    try:
        assert await tunnel.start(43210) == "https://unit-test.trycloudflare.com"
        assert tunnel.alive
    finally:
        await tunnel.stop()
    assert not tunnel.alive


@pytest.mark.asyncio
async def test_tunnel_timeout_terminates_process(tmp_path):
    fake = tmp_path / "silent.py"
    fake.write_text("import time; time.sleep(60)", encoding="utf-8")
    tunnel = QuickTunnel(["python", str(fake)], timeout=0.1)
    with pytest.raises(TunnelError, match="超时"):
        await tunnel.start(43210)
    assert not tunnel.alive
```

- [ ] **Step 2: Run tests and verify RED**

Run: `python -m pytest backend/tests/test_quick_tunnel.py -q`

Expected: import failure for `backend.quick_tunnel`.

- [ ] **Step 3: Implement `QuickTunnel`**

Use `asyncio.create_subprocess_exec` with arguments equivalent to:

```python
[cloudflared, "tunnel", "--no-autoupdate", "--protocol", "http2", "--url", loopback_url]
```

Read both stdout and stderr concurrently, extract only URLs matching `https://[a-z0-9-]+.trycloudflare.com`, reject other output, and terminate/kill with bounded waits. `QuickTunnel(command_prefix, timeout)` exposes a read-only `alive` property plus async `start(port)` and idempotent `stop()` methods. `resolve_cloudflared(configured_path)` returns the executable command prefix.

`resolve_cloudflared` checks the configured path, `shutil.which("cloudflared")`, then `work/cloudflared/cloudflared.exe` relative to the project. It raises `TunnelError` with setup instructions and never downloads software.

- [ ] **Step 4: Run tests and verify GREEN**

Run: `python -m pytest backend/tests/test_quick_tunnel.py -q`

Expected: all tunnel tests pass and no fake child process remains.

- [ ] **Step 5: Commit**

```powershell
git add seedance-workbench/backend/quick_tunnel.py seedance-workbench/backend/tests/test_quick_tunnel.py
git commit -m "feat(seedance): supervise temporary video tunnel"
```

### Task 4: Persist upload metadata and manage lifecycle

**Files:**
- Create: `backend/video_uploads.py`
- Create: `backend/tests/test_video_uploads.py`
- Modify: `backend/store.py`

- [ ] **Step 1: Write failing store and manager tests**

```python
# backend/tests/test_video_uploads.py
import time
from pathlib import Path

import pytest

from backend.store import TaskStore
from backend.video_uploads import VideoUploadManager


class FakeTunnel:
    alive = False
    async def start(self, port):
        self.alive = True
        return "https://fake.trycloudflare.com"
    async def stop(self):
        self.alive = False


@pytest.mark.asyncio
async def test_resolve_ready_upload_and_release_after_grace(tmp_path: Path):
    store = TaskStore(tmp_path / "db.sqlite")
    await store.open()
    manager = VideoUploadManager.for_tests(
        store=store,
        root=tmp_path / "videos",
        tunnel=FakeTunnel(),
        ttl=10,
        grace=0,
    )
    await manager.start()
    try:
        upload = await manager.register_test_video(b"video", "clip.mp4", 5.0, "video/mp4")
        url = await manager.resolve(upload.upload_id)
        assert url.startswith("https://fake.trycloudflare.com/media/")
        await manager.attach(upload.upload_id, "task-1")
        await manager.release_task("task-1", finished_at=time.time())
        await manager.cleanup_once(now=time.time() + 1)
        assert not upload.path.exists()
    finally:
        await manager.stop()
        await store.close()
```

Add store tests for `create_video_upload`, `get_video_upload`, `attach_video_uploads`, `list_expired_video_uploads`, and `delete_video_upload`.

- [ ] **Step 2: Run tests and verify RED**

Run: `python -m pytest backend/tests/test_video_uploads.py backend/tests/test_store.py -q`

Expected: missing manager and store method failures.

- [ ] **Step 3: Add the `video_uploads` schema**

```sql
CREATE TABLE IF NOT EXISTS video_uploads (
    upload_id TEXT PRIMARY KEY,
    filename TEXT NOT NULL,
    storage_path TEXT NOT NULL,
    content_type TEXT NOT NULL,
    size INTEGER NOT NULL,
    duration REAL NOT NULL,
    task_id TEXT,
    status TEXT NOT NULL,
    created_at REAL NOT NULL,
    expires_at REAL NOT NULL,
    release_at REAL
);
```

Implement focused parameterized SQL methods; never persist public URLs or media tokens.

- [ ] **Step 4: Implement `VideoUploadManager`**

The manager owns one `TemporaryMediaServer` and one `QuickTunnel`. It exposes async lifecycle methods `start()`/`stop()`, ingestion via `accept(upload)`, resolution via `resolve(upload_id)`, task association via `attach_many(upload_ids, task_id)` and `release_task(task_id, finished_at=None)`, explicit removal via `remove(upload_id)`, and deterministic expiry through `cleanup_once(now=None)`.

`accept` writes 64KiB chunks to a `.partial` file, stops once `max_video_bytes` is exceeded, atomically renames after probing, generates `upload_id = uuid.uuid4().hex` and `token = secrets.token_urlsafe(32)`, and rolls back file/server/store state on every failure.

- [ ] **Step 5: Run tests and verify GREEN**

Run: `python -m pytest backend/tests/test_video_uploads.py backend/tests/test_store.py -q`

Expected: all selected tests pass, including idempotent cleanup and shared-tunnel retention.

- [ ] **Step 6: Commit**

```powershell
git add seedance-workbench/backend/store.py seedance-workbench/backend/video_uploads.py seedance-workbench/backend/tests/test_video_uploads.py seedance-workbench/backend/tests/test_store.py
git commit -m "feat(seedance): manage temporary video lifecycle"
```

### Task 5: Upload API and generation resolution

**Files:**
- Create: `backend/tests/test_video_upload_api.py`
- Modify: `backend/routes.py`
- Modify: `backend/validation.py`
- Modify: `backend/tests/conftest.py`

- [ ] **Step 1: Write failing API tests**

```python
# backend/tests/test_video_upload_api.py
from uuid import uuid4


def test_upload_returns_opaque_metadata_without_public_url(client, monkeypatch):
    monkeypatch.setattr(
        "backend.video_uploads.probe_video",
        lambda path, filename, content_type: type(
            "Meta", (), {"duration": 5.0, "size": path.stat().st_size, "content_type": "video/mp4"}
        )(),
    )
    response = client.post(
        "/api/seedance/video-uploads",
        files={"video": ("clip.mp4", b"\x00\x00\x00\x18ftypisom" + b"x" * 64, "video/mp4")},
    )
    assert response.status_code == 201
    data = response.json()["data"]
    assert data["filename"] == "clip.mp4"
    assert data["status"] == "ready"
    assert "upload_id" in data
    assert "url" not in data
    assert "token" not in data


def test_generation_accepts_local_upload_id(client, ready_video_upload):
    response = client.post(
        "/api/seedance/generations",
        data={
            "mode": "edit",
            "prompt": "复刻视频1",
            "duration": "5",
            "resolution": "480p",
            "ratio": "9:16",
            "generate_audio": "false",
            "client_request_id": str(uuid4()),
            "video_upload_ids": [ready_video_upload],
        },
    )
    assert response.status_code == 201
    assert response.json()["data"]["materials"][0]["source"] == "local-upload"
```

- [ ] **Step 2: Run tests and verify RED**

Run: `python -m pytest backend/tests/test_video_upload_api.py -q`

Expected: upload route returns 404 and generation rejects missing video.

- [ ] **Step 3: Add upload and removal routes**

```python
@app.post("/api/seedance/video-uploads", status_code=201)
async def upload_video(request: Request, video: UploadFile = File(...)):
    item = await request.app.state.video_uploads.accept(video)
    return _ok(item.public_dict(), 201)


@app.delete("/api/seedance/video-uploads/{upload_id}")
async def remove_video_upload(request: Request, upload_id: str):
    removed = await request.app.state.video_uploads.remove(upload_id)
    if not removed:
        raise HTTPException(404, detail={"code": "not_found", "message": "本地视频素材不存在"})
    return _ok({"deleted": True})
```

Initialize and stop the manager inside the FastAPI lifespan, after the store opens and before it closes.

- [ ] **Step 4: Resolve IDs before rate limiting and upstream submission**

Add `video_upload_ids: list[str] = Form([])`. Enforce `len(video_urls) + len(video_upload_ids) <= 3`, resolve IDs before the rate limiter is consumed, and pass resolved HTTPS URLs into `parse_create_form`. Extend material description with a parallel `video_sources` tuple so history reports `local-upload` without persisting the public URL. Attach uploads only after the local task row exists; release them if upstream submission is definitively rejected.

- [ ] **Step 5: Run tests and verify GREEN**

Run: `python -m pytest backend/tests/test_video_upload_api.py backend/tests/test_v2_multimodal.py backend/tests/test_api_mock_flow.py -q`

Expected: all selected API flows pass and existing HTTPS/`asset://` behavior remains green.

- [ ] **Step 6: Commit**

```powershell
git add seedance-workbench/backend/routes.py seedance-workbench/backend/validation.py seedance-workbench/backend/tests/conftest.py seedance-workbench/backend/tests/test_video_upload_api.py seedance-workbench/backend/tests/test_v2_multimodal.py
git commit -m "feat(seedance): accept local video uploads in generations"
```

### Task 6: Terminal-state cleanup integration

**Files:**
- Modify: `backend/poller.py`
- Modify: `backend/routes.py`
- Modify: `backend/tests/test_video_uploads.py`

- [ ] **Step 1: Write failing cleanup notification tests**

Add a fake listener with `release_task(task_id, finished_at)` and assert it is called once when the poller sets `succeeded` or `failed`, including timeout failure, but not for `queued`, `running`, or `unknown`.

```python
class ReleaseRecorder:
    def __init__(self): self.calls = []
    async def release_task(self, task_id, finished_at=None):
        self.calls.append(task_id)
```

- [ ] **Step 2: Run test and verify RED**

Run: `python -m pytest backend/tests/test_video_uploads.py -q`

Expected: recorder has no calls because Poller has no terminal callback.

- [ ] **Step 3: Add a terminal callback to `Poller`**

```python
class Poller:
    def __init__(self, store, client, *, poll_interval=10.0, max_wait=900.0,
                 resume_window=1800.0, on_terminal=None):
        self._on_terminal = on_terminal

    async def _notify_terminal(self, task_id: str) -> None:
        if self._on_terminal is not None:
            await self._on_terminal(task_id)
```

Call `_notify_terminal` immediately after every `set_succeeded`/`set_failed`, never after `set_unknown`. Construct the poller with `on_terminal=video_uploads.release_task`.

- [ ] **Step 4: Run tests and verify GREEN**

Run: `python -m pytest backend/tests/test_video_uploads.py backend/tests/test_api_mock_flow.py -q`

Expected: terminal callbacks and existing poller behavior pass.

- [ ] **Step 5: Commit**

```powershell
git add seedance-workbench/backend/poller.py seedance-workbench/backend/routes.py seedance-workbench/backend/tests/test_video_uploads.py
git commit -m "feat(seedance): clean local videos after task completion"
```

### Task 7: Frontend request model and material numbering

**Files:**
- Modify: `frontend/src/api.js`
- Modify: `frontend/tests/api.test.js`

- [ ] **Step 1: Write failing API helper tests**

```javascript
test('formDataFromParams carries local video upload ids in material order', () => {
  const form = formDataFromParams({
    mode: 'edit', prompt: '复刻视频1', duration: 5, resolution: '720p', ratio: '9:16',
    generateAudio: false, clientRequestId: crypto.randomUUID(),
    videoUrls: ['https://cdn.example.com/a.mp4'],
    videoUploads: [{ uploadId: 'upload-1', status: 'ready' }],
  })
  assert.deepEqual(form.getAll('video_urls'), ['https://cdn.example.com/a.mp4'])
  assert.deepEqual(form.getAll('video_upload_ids'), ['upload-1'])
})

test('video material chips count URLs and ready local uploads', () => {
  assert.deepEqual(materialRefChips({
    videoUrls: ['https://cdn.example.com/a.mp4'],
    videoUploads: [{ uploadId: 'u1', status: 'ready' }, { uploadId: 'u2', status: 'uploading' }],
  }), [
    { kind: 'video', label: '视频1' },
    { kind: 'video', label: '视频2' },
  ])
})
```

- [ ] **Step 2: Run tests and verify RED**

Run: `npm test -- --test-name-pattern="local video|video material"`

Workdir: `frontend`

Expected: missing `video_upload_ids` and incorrect chip count.

- [ ] **Step 3: Implement API helpers**

Add ready upload IDs in `formDataFromParams`; update `materialRefLabels` and `materialRefChips` to count URL items plus all visible local upload items. Add:

```javascript
export function uploadVideo(file, { signal } = {}) {
  const form = new FormData()
  form.append('video', file, file.name)
  return request('/video-uploads', { method: 'POST', body: form, signal })
}

export function removeVideoUpload(uploadId) {
  return request(`/video-uploads/${encodeURIComponent(uploadId)}`, { method: 'DELETE' })
}
```

- [ ] **Step 4: Run tests and verify GREEN**

Run: `npm test`

Workdir: `frontend`

Expected: all Node tests pass.

- [ ] **Step 5: Commit**

```powershell
git add seedance-workbench/frontend/src/api.js seedance-workbench/frontend/tests/api.test.js
git commit -m "feat(seedance): submit local video material ids"
```

### Task 8: Local video picker and status UI

**Files:**
- Modify: `frontend/src/App.vue`
- Modify: `frontend/src/components/CreatorPanel.vue`
- Modify: `frontend/src/constants.js`
- Modify: `frontend/src/styles.css`

- [ ] **Step 1: Add a failing component-independent state test**

Extract and test these pure helpers in `api.js` if direct Vue component testing is unavailable:

```javascript
test('validateLocalVideoFile rejects unsupported or oversized files', async () => {
  const { validateLocalVideoFile } = await import('../src/api.js')
  assert.equal(validateLocalVideoFile(new File(['x'], 'a.txt', { type: 'text/plain' })), '仅支持 MP4 或 MOV 视频')
  const oversized = new File([new Uint8Array(51 * 1024 * 1024)], 'a.mp4', { type: 'video/mp4' })
  assert.match(validateLocalVideoFile(oversized), /50MB/)
  assert.equal(validateLocalVideoFile(new File(['x'], 'a.mp4', { type: 'video/mp4' })), null)
})
```

- [ ] **Step 2: Run test and verify RED**

Run: `npm test -- --test-name-pattern="validateLocalVideoFile"`

Workdir: `frontend`

Expected: helper is missing.

- [ ] **Step 3: Implement upload orchestration in `App.vue`**

Add `videoUploads: []` to the draft. The upload handler inserts a local item immediately:

```javascript
{
  localId: crypto.randomUUID(), fileName: file.name, size: file.size,
  status: 'uploading', uploadId: null, duration: null, error: null,
}
```

Then call `uploadVideo`, replace status with `ready` and server metadata, or set `failed` with the `ApiError` message. On removal, abort an in-flight upload or call `removeVideoUpload` for a ready item. `submit` rejects any `uploading`/`failed` items and sends only ready IDs. Mode changes remove unreferenced uploads through the API before clearing draft state.

- [ ] **Step 4: Implement the picker in `CreatorPanel.vue`**

Add a hidden file input accepting `.mp4,.mov,video/mp4,video/quicktime`, an “上传本地视频” button, and one card per local item. Emit `upload-video`, `retry-video`, and `remove-video` events. Present status text exactly as:

- `uploading`: “正在上传并建立安全通道…”
- `ready`: filename, formatted size, duration, “素材可用”
- `failed`: error message and “重试”

The existing “+ 添加” button remains “添加链接”. Disable both add controls when combined URL and local item count reaches 3.

- [ ] **Step 5: Build and run all frontend tests**

Run: `npm test && npm run build`

Workdir: `frontend`

Expected: tests pass and Vite build exits 0 without Vue compilation errors.

- [ ] **Step 6: Commit**

```powershell
git add seedance-workbench/frontend/src/App.vue seedance-workbench/frontend/src/components/CreatorPanel.vue seedance-workbench/frontend/src/constants.js seedance-workbench/frontend/src/styles.css seedance-workbench/frontend/src/api.js seedance-workbench/frontend/tests/api.test.js
git commit -m "feat(seedance): add local video upload interface"
```

### Task 9: Documentation, security regression, and full verification

**Files:**
- Modify: `README.md`
- Modify: `backend/tests/test_api_real_error.py`
- Modify: `backend/tests/test_static_assets.py`

- [ ] **Step 1: Add security regression tests**

Test that a non-local `Origin` cannot upload or delete a video, upload responses never contain `trycloudflare.com`, media tokens, or local filesystem paths, and the media service cannot access `../seedance.db` or arbitrary paths.

- [ ] **Step 2: Run security tests and verify they pass after any minimal fixes**

Run: `python -m pytest backend/tests/test_video_upload_api.py backend/tests/test_media_server.py backend/tests/test_api_real_error.py -q`

Expected: all selected tests pass.

- [ ] **Step 3: Document setup and runtime behavior**

Update `README.md` with:

```text
1. Install ffmpeg/ffprobe and cloudflared, or set SEEDANCE_CLOUDFLARED_PATH.
2. Click “上传本地视频”; the workbench validates MP4/MOV ≤50MB and 2–15s.
3. During generation, the file is available to TokenHub/Seedance through a random temporary HTTPS path.
4. The tunnel and file are removed after task completion plus a five-minute grace period.
5. If tunnel startup fails, use an existing public HTTPS URL or asset:// ID.
```

Do not claim end-to-end zero configuration when neither a bundled nor PATH-resolvable `cloudflared` exists.

- [ ] **Step 4: Run the complete verification suite**

Run from `seedance-workbench`:

```powershell
python -m pytest backend/tests -q
Push-Location frontend
npm test
npm run build
Pop-Location
```

Expected: pytest reports zero failures; Node tests report zero failures; Vite exits 0.

- [ ] **Step 5: Perform a local smoke test without spending quota**

Start the app in mock mode, upload a generated 2-second MP4 fixture, confirm the UI reaches “素材可用”, submit a mock edit task, wait for success, then confirm the upload row/file becomes eligible for cleanup. Use a fake/local tunnel command in automated smoke testing; reserve a real Quick Tunnel check for explicit manual acceptance.

- [ ] **Step 6: Review the final diff**

Run:

```powershell
git diff --check
git status --short
git diff --stat HEAD~8..HEAD
```

Confirm no unrelated parent-repository changes are staged or committed.

- [ ] **Step 7: Commit documentation and final regression tests**

```powershell
git add seedance-workbench/README.md seedance-workbench/backend/tests/test_api_real_error.py seedance-workbench/backend/tests/test_static_assets.py
git commit -m "docs(seedance): explain temporary local video uploads"
```
