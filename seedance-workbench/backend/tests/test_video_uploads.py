import asyncio
from io import BytesIO
from pathlib import Path
import time

from starlette.datastructures import UploadFile

from backend.media_server import TemporaryMediaServer
from backend.poller import Poller
from backend.store import TaskStore
from backend.video_probe import VideoMetadata
from backend.video_uploads import VideoUploadManager


class FakeTunnel:
    def __init__(self):
        self.alive = False
        self.stops = 0

    async def start(self, port):
        self.alive = True
        return "https://fake.trycloudflare.com"

    async def stop(self):
        self.alive = False
        self.stops += 1


def test_store_persists_and_releases_video_upload(tmp_path):
    async def exercise():
        store = TaskStore(tmp_path / "db.sqlite")
        await store.open()
        try:
            await store.create_video_upload(
                {
                    "upload_id": "u1",
                    "filename": "clip.mp4",
                    "storage_path": str(tmp_path / "clip.mp4"),
                    "content_type": "video/mp4",
                    "size": 10,
                    "duration": 5.0,
                    "status": "ready",
                    "created_at": 100.0,
                    "expires_at": 200.0,
                }
            )
            assert (await store.get_video_upload("u1"))["filename"] == "clip.mp4"
            await store.attach_video_uploads(("u1",), "task-1")
            assert (await store.get_video_upload("u1"))["task_id"] == "task-1"
            await store.release_video_uploads("task-1", 250.0)
            expired = await store.list_video_uploads_for_cleanup(251.0)
            assert [item["upload_id"] for item in expired] == ["u1"]
        finally:
            await store.close()

    asyncio.run(exercise())


def test_manager_resolves_and_cleans_released_upload(tmp_path, monkeypatch):
    async def exercise():
        store = TaskStore(tmp_path / "db.sqlite")
        await store.open()
        tunnel = FakeTunnel()
        manager = VideoUploadManager(
            store=store,
            root=tmp_path / "videos",
            max_bytes=1024,
            ttl=10,
            grace=0,
            tunnel=tunnel,
            media_server=TemporaryMediaServer(),
        )
        monkeypatch.setattr(
            "backend.video_uploads.probe_video",
            lambda path, filename, declared: VideoMetadata(5.0, "video/mp4", path.stat().st_size),
        )
        await manager.start()
        try:
            upload_file = UploadFile(
                filename="clip.mp4",
                file=BytesIO(b"\x00\x00\x00\x18ftypisom" + b"x" * 16),
                headers={"content-type": "video/mp4"},
            )
            item = await manager.accept(upload_file)
            url = await manager.resolve(item.upload_id)
            assert url.startswith("https://fake.trycloudflare.com/media/")
            assert "fake.trycloudflare.com" not in item.public_dict().values()
            await manager.attach_many((item.upload_id,), "task-1")
            await manager.release_task("task-1", finished_at=time.time() - 1)
            await manager.cleanup_once(now=time.time())
            assert not item.path.exists()
            assert await store.get_video_upload(item.upload_id) is None
            assert tunnel.stops == 1
        finally:
            await manager.stop()
            await store.close()

    asyncio.run(exercise())


def test_manager_rejects_oversized_stream_and_removes_partial(tmp_path):
    async def exercise():
        store = TaskStore(tmp_path / "db.sqlite")
        await store.open()
        manager = VideoUploadManager(
            store=store,
            root=tmp_path / "videos",
            max_bytes=8,
            ttl=10,
            grace=0,
            tunnel=FakeTunnel(),
            media_server=TemporaryMediaServer(),
        )
        await manager.start()
        try:
            upload_file = UploadFile(filename="big.mp4", file=BytesIO(b"x" * 20))
            try:
                await manager.accept(upload_file)
            except ValueError as exc:
                assert "大小" in str(exc)
            else:
                raise AssertionError("expected oversized upload failure")
            assert list((tmp_path / "videos").glob("*")) == []
        finally:
            await manager.stop()
            await store.close()

    asyncio.run(exercise())


def test_poller_notifies_upload_manager_on_terminal_state():
    class Store:
        async def get(self, task_id):
            return {"task_id": task_id, "api_task_id": "upstream", "status": "running"}

        async def set_succeeded(self, *args, **kwargs):
            return None

    class Client:
        async def get_task(self, task_id):
            return {
                "status": "succeeded",
                "content": {"video_url": "https://cdn.example/result.mp4"},
            }

    async def exercise():
        released = []

        async def on_terminal(task_id):
            released.append(task_id)

        poller = Poller(
            Store(),
            Client(),
            poll_interval=0,
            max_wait=1,
            on_terminal=on_terminal,
        )
        await poller._loop("task-1")
        assert released == ["task-1"]

    asyncio.run(exercise())
