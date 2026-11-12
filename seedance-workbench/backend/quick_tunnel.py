"""Supervised Cloudflare Quick Tunnel process."""

from __future__ import annotations

import asyncio
from pathlib import Path
import re
import shutil

from backend.config import BASE_DIR


_TUNNEL_URL = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com", re.IGNORECASE)


class TunnelError(RuntimeError):
    pass


def resolve_cloudflared(configured_path: str) -> list[str]:
    candidates: list[Path] = []
    if configured_path:
        candidates.append(Path(configured_path))
    discovered = shutil.which("cloudflared")
    if discovered:
        candidates.append(Path(discovered))
    candidates.extend(
        [
            BASE_DIR / "tools" / "cloudflared.exe",
            BASE_DIR / "work" / "cloudflared" / "cloudflared.exe",
        ]
    )
    for candidate in candidates:
        if candidate.is_file():
            return [str(candidate.resolve())]
    raise TunnelError(
        "未找到 cloudflared。请安装 cloudflared，或设置 SEEDANCE_CLOUDFLARED_PATH。"
    )


class QuickTunnel:
    def __init__(self, command_prefix: list[str], timeout: float = 30.0) -> None:
        self._command_prefix = list(command_prefix)
        self._timeout = timeout
        self._process: asyncio.subprocess.Process | None = None
        self._reader_tasks: list[asyncio.Task] = []

    @property
    def alive(self) -> bool:
        return self._process is not None and self._process.returncode is None

    async def start(self, port: int) -> str:
        if self.alive:
            raise TunnelError("临时通道已经启动")
        args = [
            *self._command_prefix,
            "tunnel",
            "--no-autoupdate",
            "--protocol",
            "http2",
            "--url",
            f"http://127.0.0.1:{port}",
        ]
        try:
            self._process = await asyncio.create_subprocess_exec(
                *args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except OSError as exc:
            self._process = None
            raise TunnelError(f"无法启动 cloudflared：{exc}") from exc

        queue: asyncio.Queue[str | None] = asyncio.Queue()

        async def read_stream(stream) -> None:
            try:
                while True:
                    raw = await stream.readline()
                    if not raw:
                        break
                    await queue.put(raw.decode("utf-8", errors="replace"))
            finally:
                await queue.put(None)

        assert self._process.stdout is not None
        assert self._process.stderr is not None
        self._reader_tasks = [
            asyncio.create_task(read_stream(self._process.stdout)),
            asyncio.create_task(read_stream(self._process.stderr)),
        ]
        try:
            async with asyncio.timeout(self._timeout):
                closed_streams = 0
                while closed_streams < 2:
                    line = await queue.get()
                    if line is None:
                        closed_streams += 1
                        continue
                    match = _TUNNEL_URL.search(line)
                    if match:
                        return match.group(0).rstrip("/")
                    if self._process.returncode is not None:
                        break
        except TimeoutError as exc:
            await self.stop()
            raise TunnelError("建立临时 HTTPS 通道超时") from exc
        await self.stop()
        raise TunnelError("cloudflared 未返回可用的临时 HTTPS 地址")

    async def stop(self) -> None:
        process = self._process
        self._process = None
        if process is not None and process.returncode is None:
            process.terminate()
            try:
                await asyncio.wait_for(process.wait(), timeout=3)
            except asyncio.TimeoutError:
                process.kill()
                await process.wait()
        tasks, self._reader_tasks = self._reader_tasks, []
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)


class LazyQuickTunnel:
    """Resolve cloudflared only when the first local video needs a tunnel."""

    def __init__(self, configured_path: str, timeout: float = 30.0) -> None:
        self._configured_path = configured_path
        self._timeout = timeout
        self._inner: QuickTunnel | None = None

    @property
    def alive(self) -> bool:
        return self._inner is not None and self._inner.alive

    async def start(self, port: int) -> str:
        if self._inner is None:
            self._inner = QuickTunnel(
                resolve_cloudflared(self._configured_path), timeout=self._timeout
            )
        return await self._inner.start(port)

    async def stop(self) -> None:
        inner, self._inner = self._inner, None
        if inner is not None:
            await inner.stop()
