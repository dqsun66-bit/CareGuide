"""任务持久化（SQLite / aiosqlite）。

client_request_id 唯一约束是防重复扣费的关键：
前端同一份表单始终携带同一个 UUID，重复提交只会命中已有记录，
绝不会再次调用上游创建接口。
"""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path
from typing import Any

import aiosqlite

ACTIVE_STATUSES = {"submitted", "queued", "running"}

_SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    task_id          TEXT PRIMARY KEY,
    api_task_id      TEXT,
    client_request_id TEXT NOT NULL UNIQUE,
    mode             TEXT NOT NULL,
    prompt           TEXT NOT NULL,
    has_image        INTEGER NOT NULL DEFAULT 0,
    image_path       TEXT,
    materials_json   TEXT,
    duration         INTEGER NOT NULL,
    resolution       TEXT NOT NULL,
    ratio            TEXT NOT NULL,
    generate_audio   INTEGER NOT NULL DEFAULT 0,
    return_last_frame INTEGER NOT NULL DEFAULT 0,
    web_search       INTEGER NOT NULL DEFAULT 0,
    status           TEXT NOT NULL DEFAULT 'submitted',
    error_message    TEXT,
    video_url        TEXT,
    last_frame_url   TEXT,
    usage_json       TEXT,
    created_at       REAL NOT NULL,
    updated_at       REAL NOT NULL,
    finished_at      REAL,
    mock             INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS video_uploads (
    upload_id       TEXT PRIMARY KEY,
    filename        TEXT NOT NULL,
    storage_path    TEXT NOT NULL,
    content_type    TEXT NOT NULL,
    size            INTEGER NOT NULL,
    duration        REAL NOT NULL,
    task_id         TEXT,
    status          TEXT NOT NULL,
    created_at      REAL NOT NULL,
    expires_at      REAL NOT NULL,
    release_at      REAL
);
"""

# 存量库迁移：v1 建的 tasks 表缺少 v2 列，逐列补齐（幂等）。
_MIGRATIONS = (
    "ALTER TABLE tasks ADD COLUMN materials_json TEXT",
    "ALTER TABLE tasks ADD COLUMN return_last_frame INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE tasks ADD COLUMN web_search INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE tasks ADD COLUMN last_frame_url TEXT",
)


class TaskStore:
    def __init__(self, db_path: str | Path):
        self._path = Path(db_path)
        self._conn: aiosqlite.Connection | None = None

    async def open(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = await aiosqlite.connect(self._path)
        self._conn.row_factory = sqlite3.Row
        await self._conn.executescript(_SCHEMA)
        for migration in _MIGRATIONS:
            try:
                await self._conn.execute(migration)
            except sqlite3.OperationalError as exc:
                # 仅"列已存在"可忽略；锁定等其他 OperationalError 必须向上抛，
                # 否则迁移静默失败会在请求期才爆出"no such column"。
                if "duplicate column name" not in str(exc).lower():
                    raise
        await self._conn.commit()

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    @staticmethod
    def _row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
        return {key: row[key] for key in row.keys()}

    async def create_row(self, record: dict[str, Any]) -> None:
        now = time.time()
        created_at = record.get("created_at", now)
        await self._conn.execute(
            """INSERT INTO tasks
               (task_id, api_task_id, client_request_id, mode, prompt, has_image,
                image_path, materials_json, duration, resolution, ratio, generate_audio,
                return_last_frame, web_search, mock, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                record["task_id"],
                record.get("api_task_id"),
                record["client_request_id"],
                record["mode"],
                record["prompt"],
                int(record.get("has_image", 0)),
                record.get("image_path"),
                record.get("materials_json"),
                int(record["duration"]),
                record["resolution"],
                record["ratio"],
                int(record.get("generate_audio", 0)),
                int(record.get("return_last_frame", 0)),
                int(record.get("web_search", 0)),
                int(record.get("mock", 0)),
                created_at,
                created_at,
            ),
        )
        await self._conn.commit()

    async def find_by_client_request_id(self, client_request_id: str) -> dict[str, Any] | None:
        cursor = await self._conn.execute(
            "SELECT * FROM tasks WHERE client_request_id = ?", (client_request_id,)
        )
        row = await cursor.fetchone()
        return self._row_to_dict(row) if row is not None else None

    async def get(self, task_id: str) -> dict[str, Any] | None:
        cursor = await self._conn.execute(
            "SELECT * FROM tasks WHERE task_id = ?", (task_id,)
        )
        row = await cursor.fetchone()
        return self._row_to_dict(row) if row is not None else None

    async def set_api_task_id(self, task_id: str, api_task_id: str) -> None:
        await self._conn.execute(
            "UPDATE tasks SET api_task_id = ?, updated_at = ? WHERE task_id = ?",
            (api_task_id, time.time(), task_id),
        )
        await self._conn.commit()

    async def update_status(self, task_id: str, status: str) -> None:
        await self._conn.execute(
            "UPDATE tasks SET status = ?, updated_at = ? WHERE task_id = ?",
            (status, time.time(), task_id),
        )
        await self._conn.commit()

    async def set_succeeded(
        self,
        task_id: str,
        video_url: str,
        usage_json: str | None,
        last_frame_url: str | None = None,
    ) -> None:
        now = time.time()
        await self._conn.execute(
            """UPDATE tasks
               SET status = 'succeeded', video_url = ?, usage_json = ?,
                   last_frame_url = ?, error_message = NULL, updated_at = ?, finished_at = ?
               WHERE task_id = ?""",
            (video_url, usage_json, last_frame_url, now, now, task_id),
        )
        await self._conn.commit()

    async def set_failed(self, task_id: str, message: str) -> None:
        now = time.time()
        await self._conn.execute(
            """UPDATE tasks
               SET status = 'failed', error_message = ?, updated_at = ?, finished_at = ?
               WHERE task_id = ?""",
            (message, now, now, task_id),
        )
        await self._conn.commit()

    async def set_unknown(self, task_id: str, message: str) -> None:
        now = time.time()
        await self._conn.execute(
            """UPDATE tasks
               SET status = 'unknown', error_message = ?, updated_at = ?
               WHERE task_id = ?""",
            (message, now, task_id),
        )
        await self._conn.commit()

    async def set_video_url(self, task_id: str, video_url: str) -> None:
        await self._conn.execute(
            "UPDATE tasks SET video_url = ?, updated_at = ? WHERE task_id = ?",
            (video_url, time.time(), task_id),
        )
        await self._conn.commit()

    async def list_tasks(
        self, page: int = 1, page_size: int = 20
    ) -> tuple[list[dict[str, Any]], int]:
        cursor = await self._conn.execute("SELECT COUNT(*) FROM tasks")
        total = (await cursor.fetchone())[0]
        offset = (page - 1) * page_size
        cursor = await self._conn.execute(
            "SELECT * FROM tasks ORDER BY created_at DESC, rowid DESC LIMIT ? OFFSET ?",
            (page_size, offset),
        )
        rows = await cursor.fetchall()
        return [self._row_to_dict(row) for row in rows], int(total)

    async def resumable(self, now: float, window_seconds: float) -> list[dict[str, Any]]:
        cursor = await self._conn.execute(
            """SELECT * FROM tasks
               WHERE status IN ('submitted', 'queued', 'running')
                 AND created_at >= ?
               ORDER BY created_at DESC""",
            (now - window_seconds,),
        )
        rows = await cursor.fetchall()
        return [self._row_to_dict(row) for row in rows]

    async def delete(self, task_id: str) -> bool:
        cursor = await self._conn.execute(
            "DELETE FROM tasks WHERE task_id = ?", (task_id,)
        )
        await self._conn.commit()
        return cursor.rowcount > 0

    async def create_video_upload(self, record: dict[str, Any]) -> None:
        await self._conn.execute(
            """INSERT INTO video_uploads
               (upload_id, filename, storage_path, content_type, size, duration,
                task_id, status, created_at, expires_at, release_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                record["upload_id"],
                record["filename"],
                record["storage_path"],
                record["content_type"],
                int(record["size"]),
                float(record["duration"]),
                record.get("task_id"),
                record.get("status", "ready"),
                float(record["created_at"]),
                float(record["expires_at"]),
                record.get("release_at"),
            ),
        )
        await self._conn.commit()

    async def get_video_upload(self, upload_id: str) -> dict[str, Any] | None:
        cursor = await self._conn.execute(
            "SELECT * FROM video_uploads WHERE upload_id = ?", (upload_id,)
        )
        row = await cursor.fetchone()
        return self._row_to_dict(row) if row is not None else None

    async def list_video_uploads(self) -> list[dict[str, Any]]:
        cursor = await self._conn.execute("SELECT * FROM video_uploads ORDER BY created_at")
        return [self._row_to_dict(row) for row in await cursor.fetchall()]

    async def attach_video_uploads(self, upload_ids: tuple[str, ...], task_id: str) -> None:
        if not upload_ids:
            return
        placeholders = ",".join("?" for _ in upload_ids)
        cursor = await self._conn.execute(
            f"""UPDATE video_uploads SET task_id = ?
                WHERE upload_id IN ({placeholders}) AND task_id IS NULL AND status = 'ready'""",
            (task_id, *upload_ids),
        )
        if cursor.rowcount != len(upload_ids):
            await self._conn.rollback()
            raise ValueError("一个或多个本地视频素材不可用")
        await self._conn.commit()

    async def release_video_uploads(self, task_id: str, release_at: float) -> None:
        await self._conn.execute(
            "UPDATE video_uploads SET release_at = ? WHERE task_id = ?",
            (release_at, task_id),
        )
        await self._conn.commit()

    async def list_video_uploads_for_cleanup(self, now: float) -> list[dict[str, Any]]:
        cursor = await self._conn.execute(
            """SELECT * FROM video_uploads
               WHERE (task_id IS NULL AND expires_at IS NOT NULL AND expires_at <= ?)
                  OR (release_at IS NOT NULL AND release_at <= ?)
               ORDER BY created_at""",
            (now, now),
        )
        return [self._row_to_dict(row) for row in await cursor.fetchall()]

    async def delete_video_upload(self, upload_id: str) -> bool:
        cursor = await self._conn.execute(
            "DELETE FROM video_uploads WHERE upload_id = ?", (upload_id,)
        )
        await self._conn.commit()
        return cursor.rowcount > 0
