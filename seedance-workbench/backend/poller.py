"""后台轮询器：每个进行中的任务一个 asyncio 协程，服务重启后自动恢复。"""

from __future__ import annotations

import asyncio
import json
import logging
import time

from backend.client import (
    extract_last_frame_url,
    FAILED_STATUSES,
    SUCCEEDED_STATUSES,
    TokenHubError,
    extract_error_message,
    extract_status,
    extract_usage,
    extract_video_url,
)
from backend.store import ACTIVE_STATUSES, TaskStore

logger = logging.getLogger("seedance.poller")


class Poller:
    def __init__(
        self,
        store: TaskStore,
        client,
        *,
        poll_interval: float = 10.0,
        max_wait: float = 900.0,
        resume_window: float = 1800.0,
        on_terminal=None,
    ):
        self._store = store
        self._client = client
        self._poll_interval = poll_interval
        self._max_wait = max_wait
        self._resume_window = resume_window
        self._on_terminal = on_terminal
        self._tasks: dict[str, asyncio.Task] = {}

    async def start(self) -> None:
        """服务启动：恢复窗口内仍在进行中的任务。"""
        for row in await self._store.resumable(time.time(), self._resume_window):
            self.schedule(row["task_id"])

    def schedule(self, task_id: str) -> None:
        existing = self._tasks.get(task_id)
        if existing is not None and not existing.done():
            return
        task = asyncio.create_task(self._loop(task_id))
        self._tasks[task_id] = task
        task.add_done_callback(lambda done: self._tasks.pop(task_id, None))

    async def stop(self) -> None:
        for task in tuple(self._tasks.values()):
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks.values(), return_exceptions=True)
        self._tasks.clear()

    async def _loop(self, task_id: str) -> None:
        deadline = time.monotonic() + self._max_wait
        while True:
            row = await self._store.get(task_id)
            if row is None or row["status"] not in ACTIVE_STATUSES:
                return
            api_task_id = row.get("api_task_id") or task_id
            try:
                data = await self._client.get_task(api_task_id)
            except TokenHubError:
                # 查询失败是瞬时问题：继续轮询，直到窗口耗尽
                if time.monotonic() >= deadline:
                    await self._store.set_failed(
                        task_id,
                        f"等待超时（{self._max_wait:.0f} 秒）。任务可能仍在后台运行，"
                        "可稍后点击“刷新状态”只读查询，不会重复扣费。",
                    )
                    await self._notify_terminal(task_id)
                    return
                await asyncio.sleep(self._poll_interval)
                continue
            except Exception:
                # 任何未预期异常都不能杀死轮询任务：记录日志后按瞬时错误继续
                logger.exception("轮询任务 %s 出现未预期异常，将继续重试", task_id)
                if time.monotonic() >= deadline:
                    await self._store.set_failed(
                        task_id,
                        f"等待超时（{self._max_wait:.0f} 秒）且轮询出现内部错误。"
                        "任务可能仍在后台运行，可稍后点击“刷新状态”只读查询。",
                    )
                    await self._notify_terminal(task_id)
                    return
                await asyncio.sleep(self._poll_interval)
                continue

            status = extract_status(data)
            if status in SUCCEEDED_STATUSES:
                video_url = extract_video_url(data)
                if video_url:
                    usage = extract_usage(data)
                    await self._store.set_succeeded(
                        task_id,
                        video_url,
                        json.dumps(usage, ensure_ascii=False) if usage else None,
                        last_frame_url=extract_last_frame_url(data),
                    )
                else:
                    await self._store.set_failed(
                        task_id, "任务返回成功，但响应中没有视频地址。"
                    )
                await self._notify_terminal(task_id)
                return
            if status in FAILED_STATUSES:
                await self._store.set_failed(
                    task_id, extract_error_message(data) or f"任务失败（状态：{status}）。"
                )
                await self._notify_terminal(task_id)
                return

            mapped = status if status in ACTIVE_STATUSES else "running"
            await self._store.update_status(task_id, mapped)
            if time.monotonic() >= deadline:
                await self._store.set_failed(
                    task_id,
                    f"等待超时（{self._max_wait:.0f} 秒）。任务可能仍在后台运行，"
                    "可稍后点击“刷新状态”只读查询，不会重复扣费。",
                )
                await self._notify_terminal(task_id)
                return
            await asyncio.sleep(self._poll_interval)

    async def _notify_terminal(self, task_id: str) -> None:
        if self._on_terminal is not None:
            try:
                await self._on_terminal(task_id)
            except Exception:
                logger.exception("任务 %s 的本地视频清理回调失败", task_id)
