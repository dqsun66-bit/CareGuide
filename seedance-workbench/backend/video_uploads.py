"""Lifecycle manager for local videos exposed through a temporary HTTPS tunnel."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
import secrets
import time
import uuid

from fastapi import UploadFile
import httpx

from backend.media_server import TemporaryMediaServer
from backend.store import TaskStore
from backend.video_probe import VideoProbeError, probe_video


_CHUNK_SIZE = 64 * 1024


@dataclass(frozen=True)
class VideoUpload:
    upload_id: str
    filename: str
    path: Path
    content_type: str
    size: int
    duration: float
    status: str = "ready"

    def public_dict(self) -> dict:
        return {
            "upload_id": self.upload_id,
            "filename": self.filename,
            "content_type": self.content_type,
            "size": self.size,
            "duration": self.duration,
            "status": self.status,
        }


class VideoUploadManager:
    def __init__(
        self,
        *,
        store: TaskStore,
        root: Path,
        max_bytes: int,
        ttl: float,
        grace: float,
        tunnel,
        media_server: TemporaryMediaServer | None = None,
        public_probe=None,
        public_check_timeout: float = 30.0,
        public_check_interval: float = 0.5,
    ) -> None:
        self._store = store
        self._root = Path(root)
        self._max_bytes = max_bytes
        self._ttl = ttl
        self._grace = grace
        self._tunnel = tunnel
        self._media = media_server or TemporaryMediaServer()
        self._public_probe = public_probe or self._probe_public_url
        self._public_check_timeout = public_check_timeout
        self._public_check_interval = public_check_interval
        self._tokens: dict[str, str] = {}
        self._public_base: str | None = None
        self._lock = asyncio.Lock()
        self._cleanup_task: asyncio.Task | None = None

    async def start(self) -> None:
        self._root.mkdir(parents=True, exist_ok=True)
        self._media.start()
        # Tokens are intentionally not persisted. Rows left by a previous process
        # cannot be safely re-published, so reclaim them at startup.
        for row in await self._store.list_video_uploads():
            try:
                Path(row["storage_path"]).unlink(missing_ok=True)
            except OSError:
                pass
            await self._store.delete_video_upload(row["upload_id"])
        self._cleanup_task = asyncio.create_task(self._cleanup_loop())

    async def stop(self) -> None:
        task, self._cleanup_task = self._cleanup_task, None
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await self._tunnel.stop()
        self._public_base = None
        self._media.stop()

    async def accept(self, upload: UploadFile) -> VideoUpload:
        filename = Path(upload.filename or "video.mp4").name
        upload_id = uuid.uuid4().hex
        partial = self._root / f"{upload_id}.partial"
        final = self._root / f"{upload_id}{Path(filename).suffix.casefold()}"
        total = 0
        try:
            with partial.open("wb") as target:
                while True:
                    chunk = await upload.read(_CHUNK_SIZE)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > self._max_bytes:
                        raise ValueError(
                            f"视频大小超过限制（最大 {self._max_bytes // (1024 * 1024)}MB）"
                        )
                    target.write(chunk)
            partial.replace(final)
            metadata = await asyncio.to_thread(
                probe_video,
                final,
                filename,
                upload.content_type or "application/octet-stream",
            )
            async with self._lock:
                await self._ensure_tunnel()
                token = secrets.token_urlsafe(32)
                self._tokens[upload_id] = token
                self._media.publish(token, final, metadata.content_type)
                assert self._public_base is not None
                await self._wait_until_public(
                    f"{self._public_base}/media/{token}"
                )
                now = time.time()
                await self._store.create_video_upload(
                    {
                        "upload_id": upload_id,
                        "filename": filename,
                        "storage_path": str(final),
                        "content_type": metadata.content_type,
                        "size": metadata.size,
                        "duration": metadata.duration,
                        "status": "ready",
                        "created_at": now,
                        "expires_at": now + self._ttl,
                    }
                )
            return VideoUpload(
                upload_id=upload_id,
                filename=filename,
                path=final,
                content_type=metadata.content_type,
                size=metadata.size,
                duration=metadata.duration,
            )
        except VideoProbeError as exc:
            raise ValueError(str(exc)) from exc
        except Exception:
            partial.unlink(missing_ok=True)
            final.unlink(missing_ok=True)
            token = self._tokens.pop(upload_id, None)
            if token:
                self._media.unpublish(token)
            await self._store.delete_video_upload(upload_id)
            await self._stop_tunnel_if_idle()
            raise

    async def resolve(self, upload_id: str) -> str:
        row = await self._store.get_video_upload(upload_id)
        token = self._tokens.get(upload_id)
        if row is None or row.get("status") != "ready" or token is None:
            raise ValueError("本地视频素材不存在或已过期")
        async with self._lock:
            await self._ensure_tunnel()
            assert self._public_base is not None
            return f"{self._public_base}/media/{token}"

    async def attach_many(self, upload_ids: tuple[str, ...], task_id: str) -> None:
        await self._store.attach_video_uploads(upload_ids, task_id)

    async def release_task(self, task_id: str, finished_at: float | None = None) -> None:
        base = time.time() if finished_at is None else finished_at
        await self._store.release_video_uploads(task_id, base + self._grace)

    async def remove(self, upload_id: str) -> bool:
        row = await self._store.get_video_upload(upload_id)
        if row is None or row.get("task_id") is not None:
            return False
        await self._delete_row(row)
        await self._stop_tunnel_if_idle()
        return True

    async def cleanup_once(self, now: float | None = None) -> None:
        for row in await self._store.list_video_uploads_for_cleanup(time.time() if now is None else now):
            await self._delete_row(row)
        await self._stop_tunnel_if_idle()

    async def _delete_row(self, row: dict) -> None:
        token = self._tokens.pop(row["upload_id"], None)
        if token:
            self._media.unpublish(token)
        try:
            Path(row["storage_path"]).unlink(missing_ok=True)
        finally:
            await self._store.delete_video_upload(row["upload_id"])

    async def _ensure_tunnel(self) -> None:
        if self._tunnel.alive and self._public_base:
            return
        self._public_base = await self._tunnel.start(self._media.port)

    async def _stop_tunnel_if_idle(self) -> None:
        if await self._store.list_video_uploads():
            return
        await self._tunnel.stop()
        self._public_base = None

    async def _cleanup_loop(self) -> None:
        while True:
            await asyncio.sleep(30)
            await self.cleanup_once()

    async def _wait_until_public(self, url: str) -> None:
        deadline = time.monotonic() + self._public_check_timeout
        while True:
            if await self._public_probe(url):
                return
            if time.monotonic() >= deadline:
                raise ValueError("临时 HTTPS 通道已建立，但公网读取检查超时")
            await asyncio.sleep(self._public_check_interval)

    @staticmethod
    async def _probe_public_url(url: str) -> bool:
        try:
            async with httpx.AsyncClient(timeout=5, follow_redirects=False) as client:
                response = await client.head(url)
            return response.status_code == 200
        except httpx.HTTPError:
            return False
