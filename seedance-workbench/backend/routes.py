"""Seedance 工作台 API 路由与 FastAPI 应用工厂。

响应统一信封：{"ok": true, "data": ...} / {"ok": false, "error": {"code", "message"}}。
"""

from __future__ import annotations

import json
import logging
import sqlite3
import time
import uuid
from collections import deque
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx
from fastapi import FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from fastapi.exceptions import RequestValidationError
from starlette.staticfiles import StaticFiles

from backend.client import (
    TokenHubClient,
    TokenHubError,
    extract_last_frame_url,
    extract_task_id,
    extract_video_url,
)
from backend.config import Settings, load_settings
from backend.mock import MOCK_LAST_FRAME_PNG, MockClient
from backend.poller import Poller
from backend.quick_tunnel import LazyQuickTunnel, TunnelError
from backend.store import TaskStore
from backend.video_uploads import VideoUploadManager
from backend.validation import (
    MIME_TO_EXTENSION,
    ParamsError,
    build_generation_payload,
    describe_materials,
    is_valid_uuid,
    parse_create_form,
    validate_image,
)

logger = logging.getLogger("seedance.workbench")

CORS_ORIGINS = (
    "http://127.0.0.1:5176",
    "http://localhost:5176",
    "http://127.0.0.1:8001",
    "http://localhost:8001",
)
# 本机来源判定（Origin 头校验）：仅允许来自本机页面的写操作，阻断跨站表单 CSRF
_LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1"}
_UPLOAD_CHUNK_BYTES = 64 * 1024
# 请求体总量护栏（官方 ≤64MB，留出 multipart 开销余量）
_MAX_BODY_BYTES = 68 * 1024 * 1024
# 音频字段原始长度上限（15MB 解码 × 4/3 base64 膨胀 ≈ 20MB）
_MAX_AUDIO_FIELD_CHARS = 20 * 1024 * 1024


def _ok(data: Any, status: int = 200, meta: dict | None = None) -> JSONResponse:
    body: dict = {"ok": True, "data": data}
    if meta:
        body["meta"] = meta
    return JSONResponse(body, status_code=status)


def _fail(code: str, message: str, status: int) -> JSONResponse:
    return JSONResponse(
        {"ok": False, "error": {"code": code, "message": message}},
        status_code=status,
    )


class RateLimiter:
    """创建接口的滑动窗口限流：防止换 UUID 无限提交导致无限扣费。"""

    def __init__(self, max_per_hour: int):
        self._max_per_hour = max_per_hour
        self._hits: deque[float] = deque()

    def allow(self) -> bool:
        if self._max_per_hour <= 0:
            return True
        cutoff = time.monotonic() - 3600
        while self._hits and self._hits[0] < cutoff:
            self._hits.popleft()
        if len(self._hits) >= self._max_per_hour:
            return False
        self._hits.append(time.monotonic())
        return True


def _origin_is_local(request: Request) -> bool:
    """浏览器跨站请求必带 Origin；仅允许本机页面（127.0.0.1/localhost/::1）。

    无 Origin 的请求（curl、服务端、测试客户端）放行。
    """
    origin = request.headers.get("origin")
    if not origin:
        return True
    try:
        parts = urlsplit(origin)
    except ValueError:
        return False
    hostname = (parts.hostname or "").lower()
    return hostname in _LOCAL_HOSTS


def build_materials_json(params, local_video_count: int = 0) -> str:
    """素材摘要落库（历史展示用）：只含 kind/role/序号/来源，绝无原始字节。"""
    materials = describe_materials(params)
    if local_video_count:
        videos = [item for item in materials if item.get("kind") == "video"]
        for item in videos[-local_video_count:]:
            item["source"] = "local-upload"
    return json.dumps(materials, ensure_ascii=False)


def _parse_image_paths(value: str | None) -> list[str]:
    """v2 起 image_path 列存 JSON 数组；兼容 v1 的纯路径字符串。"""
    if not value:
        return []
    try:
        parsed = json.loads(value)
        if isinstance(parsed, list):
            return [str(item) for item in parsed]
    except (TypeError, ValueError):
        pass
    return [value]


def _serialize_row(row: dict[str, Any]) -> dict[str, Any]:
    usage = None
    if row.get("usage_json"):
        try:
            usage = json.loads(row["usage_json"])
        except (TypeError, ValueError):
            usage = None
    materials = None
    if row.get("materials_json"):
        try:
            materials = json.loads(row["materials_json"])
        except (TypeError, ValueError):
            materials = None
    return {
        "task_id": row["task_id"],
        "api_task_id": row.get("api_task_id"),
        "client_request_id": row["client_request_id"],
        "mode": row["mode"],
        "prompt": row["prompt"],
        "has_image": row["has_image"],
        "materials": materials,
        "duration": row["duration"],
        "resolution": row["resolution"],
        "ratio": row["ratio"],
        "generate_audio": row["generate_audio"],
        "return_last_frame": row.get("return_last_frame") or 0,
        "web_search": row.get("web_search") or 0,
        "status": row["status"],
        "error_message": row.get("error_message"),
        # 注意：不返回 video_url/last_frame_url（限时签名链接）。前端一律走代理端点。
        # 尾帧仅在该任务显式开启 return_last_frame 时对外可见。
        "has_last_frame": 1 if (row.get("last_frame_url") and row.get("return_last_frame")) else 0,
        "usage": usage,
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "finished_at": row.get("finished_at"),
        "mock": row["mock"],
    }


def create_app(
    settings: Settings | None = None,
    client_factory=None,
    tunnel_factory=None,
) -> FastAPI:
    settings = settings or load_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        store = TaskStore(settings.data_dir / "seedance.db")
        await store.open()
        tunnel = (
            tunnel_factory(settings)
            if tunnel_factory is not None
            else LazyQuickTunnel(settings.cloudflared_path, settings.tunnel_start_timeout)
        )
        async def mock_public_probe(url: str) -> bool:
            return True

        video_uploads = VideoUploadManager(
            store=store,
            root=settings.data_dir / "video_uploads",
            max_bytes=settings.max_video_bytes,
            ttl=settings.video_upload_ttl,
            grace=settings.video_post_task_grace,
            tunnel=tunnel,
            public_probe=mock_public_probe if settings.mock_mode else None,
            public_check_timeout=settings.tunnel_start_timeout,
        )
        await video_uploads.start()
        if client_factory is not None:
            client = client_factory(settings)
        elif settings.mock_mode:
            client = MockClient(
                queued_seconds=settings.mock_queued_seconds,
                running_seconds=settings.mock_running_seconds,
            )
        else:
            client = TokenHubClient(
                api_key=settings.api_key,
                base_url=settings.base_url,
                model=settings.model,
            )
        poller = Poller(
            store,
            client,
            poll_interval=settings.poll_interval,
            max_wait=settings.max_wait,
            resume_window=settings.resume_window,
            on_terminal=video_uploads.release_task,
        )
        app.state.settings = settings
        app.state.store = store
        app.state.client = client
        app.state.poller = poller
        app.state.video_uploads = video_uploads
        app.state.rate_limiter = RateLimiter(settings.max_generations_per_hour)
        await poller.start()
        yield
        await poller.stop()
        await video_uploads.stop()
        await client.close()
        await store.close()

    app = FastAPI(title="Seedance 视频工作台", version="0.1.0", lifespan=lifespan)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(CORS_ORIGINS),
        allow_methods=["GET", "POST", "DELETE"],
        allow_headers=["Content-Type"],
    )

    @app.middleware("http")
    async def security_headers_middleware(request: Request, call_next):
        response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
        response.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; "
            "media-src 'self' blob:; connect-src 'self'; font-src 'self'; frame-ancestors 'none'",
        )
        return response

    @app.middleware("http")
    async def origin_guard_middleware(request: Request, call_next):
        if request.method in ("POST", "DELETE") and not _origin_is_local(request):
            logger.warning("已拒绝来自 %s 的 %s 请求", request.headers.get("origin"), request.method)
            return _fail("forbidden_origin", "请求来源不被允许：请通过本机页面操作。", 403)
        return await call_next(request)

    @app.exception_handler(HTTPException)
    async def http_exception_handler(request: Request, exc: HTTPException):
        detail = exc.detail
        if isinstance(detail, dict):
            return _fail(
                str(detail.get("code", "error")),
                str(detail.get("message", "请求失败")),
                exc.status_code,
            )
        return _fail("error", str(detail), exc.status_code)

    @app.exception_handler(RequestValidationError)
    async def validation_exception_handler(request: Request, exc: RequestValidationError):
        messages = "; ".join(str(error.get("msg", "")) for error in exc.errors())
        return _fail("invalid_params", f"参数校验失败：{messages}", 422)

    @app.exception_handler(ParamsError)
    async def params_exception_handler(request: Request, exc: ParamsError):
        return _fail("invalid_params", exc.message, 422)

    # ---------------------------------------------------------------- health

    @app.get("/api/seedance/health")
    async def health(request: Request):
        current = request.app.state.settings
        return _ok(
            {
                "mode": "mock" if current.mock_mode else "real",
                "model": current.model,
                "api_key_configured": bool(current.api_key.strip()),
                "price_cny_per_second": current.price_cny_per_second,
            }
        )

    # ------------------------------------------------------------ generation

    @app.post("/api/seedance/video-uploads", status_code=201)
    async def upload_video(request: Request, video: UploadFile = File(...)):
        try:
            item = await request.app.state.video_uploads.accept(video)
        except ValueError as exc:
            raise HTTPException(
                422, detail={"code": "invalid_video", "message": str(exc)}
            ) from exc
        except TunnelError as exc:
            raise HTTPException(
                503, detail={"code": "tunnel_unavailable", "message": str(exc)}
            ) from exc
        return _ok(item.public_dict(), 201)

    @app.delete("/api/seedance/video-uploads/{upload_id}")
    async def remove_video_upload(request: Request, upload_id: str):
        removed = await request.app.state.video_uploads.remove(upload_id)
        if not removed:
            raise HTTPException(
                404,
                detail={"code": "not_found", "message": "本地视频素材不存在或正在被任务使用"},
            )
        return _ok({"deleted": True})

    @app.post("/api/seedance/generations", status_code=201)
    async def create_generation(
        request: Request,
        mode: str = Form(""),
        prompt: str = Form(""),
        duration: str = Form(""),
        resolution: str = Form(""),
        ratio: str = Form(""),
        generate_audio: str = Form(""),
        client_request_id: str = Form(""),
        mock_outcome: str = Form(""),
        return_last_frame: str = Form(""),
        web_search: str = Form(""),
        image_roles: str = Form(""),
        video_urls: list[str] = Form([]),
        video_upload_ids: list[str] = Form([]),
        audio_urls: list[str] = Form([]),
        images: list[UploadFile] = File([]),
    ):
        current: Settings = request.app.state.settings
        store: TaskStore = request.app.state.store

        # Resolve idempotency before touching ephemeral local-upload IDs. A retry
        # must still return its original task after the temporary video is cleaned.
        if is_valid_uuid(client_request_id):
            existing = await store.find_by_client_request_id(client_request_id)
            if existing is not None:
                return _ok(_serialize_row(existing), 200, meta={"reused": True})

        # 请求体总量护栏：官方上限 64MB，本地预检拒绝明显超限的请求，
        # 避免超大表单先占满内存再被平台拒绝。
        content_length = request.headers.get("content-length")
        if content_length and content_length.isdigit() and int(content_length) > _MAX_BODY_BYTES:
            raise HTTPException(
                413,
                detail={
                    "code": "payload_too_large",
                    "message": f"请求体超过上限（最大 {_MAX_BODY_BYTES // (1024 * 1024)}MB），请压缩素材后重试。",
                },
            )

        # 音频字段原始长度上限：base64 膨胀后单条上限约 20MB，超长字符串直接拒绝
        for url in audio_urls:
            if len(url) > _MAX_AUDIO_FIELD_CHARS:
                raise HTTPException(
                    422,
                    detail={"code": "invalid_params", "message": "参考音频数据过大，请压缩后重试。"},
                )

        # 角色映射：image_roles 为空时全部视为首帧；单角色多图则全部沿用该角色
        raw_roles = [r.strip() for r in (image_roles or "").split(",") if r.strip()]
        if len(raw_roles) == 1 and len(images) > 1:
            raw_roles = raw_roles * len(images)

        image_payloads = []
        for index, upload in enumerate(images):
            role = raw_roles[index] if index < len(raw_roles) else "first_frame"
            try:
                # 分块读取并在超限时立即中止，避免超大表单耗尽内存
                chunks: list[bytes] = []
                total_read = 0
                while True:
                    chunk = await upload.read(_UPLOAD_CHUNK_BYTES)
                    if not chunk:
                        break
                    total_read += len(chunk)
                    if total_read > current.max_image_bytes:
                        limit_mb = current.max_image_bytes // (1024 * 1024)
                        raise ParamsError(f"图片大小超过限制（最大 {limit_mb}MB）")
                    chunks.append(chunk)
                image_payloads.append(
                    validate_image(
                        upload.filename or "",
                        upload.content_type or "",
                        b"".join(chunks),
                        current.max_image_bytes,
                        role,
                    )
                )
            except ParamsError as exc:
                raise HTTPException(
                    422, detail={"code": "invalid_params", "message": exc.message}
                ) from exc

        external_video_urls = tuple(
            url.strip() for url in video_urls if url and url.strip()
        )
        local_upload_ids = tuple(
            item.strip() for item in video_upload_ids if item and item.strip()
        )
        if len(external_video_urls) + len(local_upload_ids) > 3:
            raise HTTPException(
                422,
                detail={"code": "invalid_params", "message": "参考视频数量超过上限（最多 3 个）"},
            )
        local_video_urls: list[str] = []
        try:
            for upload_id in local_upload_ids:
                local_video_urls.append(
                    await request.app.state.video_uploads.resolve(upload_id)
                )
        except (ValueError, TunnelError) as exc:
            raise HTTPException(
                422, detail={"code": "invalid_video_upload", "message": str(exc)}
            ) from exc

        try:
            params = parse_create_form(
                mode=mode,
                prompt=prompt,
                duration=duration,
                resolution=resolution,
                ratio=ratio,
                generate_audio=generate_audio,
                client_request_id=client_request_id,
                mock_outcome=mock_outcome or None,
                images=tuple(image_payloads),
                videos=external_video_urls + tuple(local_video_urls),
                audios=tuple(url.strip() for url in audio_urls if url and url.strip()),
                return_last_frame=return_last_frame or None,
                web_search=web_search or None,
            )
        except ParamsError as exc:
            raise HTTPException(422, detail={"code": "invalid_params", "message": exc.message}) from exc

        if params.mock_outcome and not current.mock_mode:
            raise HTTPException(
                422,
                detail={"code": "invalid_params", "message": "mock_outcome 仅在模拟模式下可用"},
            )

        # 幂等：同一表单重复提交直接返回已有任务，绝不再次调用上游
        existing = await store.find_by_client_request_id(params.client_request_id)
        if existing is not None:
            return _ok(_serialize_row(existing), 200, meta={"reused": True})

        # 限流：防止换 UUID 无限提交导致无限扣费
        if not request.app.state.rate_limiter.allow():
            raise HTTPException(
                429,
                detail={
                    "code": "rate_limited",
                    "message": f"提交过于频繁，已触发保护（每小时最多 {current.max_generations_per_hour} 次），请稍后再试。",
                },
            )

        image_paths: list[str] = []
        if params.images:
            upload_dir = current.data_dir / "uploads"
            upload_dir.mkdir(parents=True, exist_ok=True)
            for image in params.images:
                extension = MIME_TO_EXTENSION[image.mime]
                image_path = str(upload_dir / f"{uuid.uuid4().hex}{extension}")
                Path(image_path).write_bytes(image.data)
                image_paths.append(image_path)

        local_task_id = ("mock-" if current.mock_mode else "") + uuid.uuid4().hex[:16]
        try:
            await store.create_row(
                {
                    "task_id": local_task_id,
                    "client_request_id": params.client_request_id,
                    "mode": params.mode,
                    "prompt": params.prompt,
                    "has_image": 1 if params.images else 0,
                    "image_path": json.dumps(image_paths) if image_paths else None,
                    "materials_json": build_materials_json(
                        params, local_video_count=len(local_upload_ids)
                    ),
                    "duration": params.duration,
                    "resolution": params.resolution,
                    "ratio": params.ratio,
                    "generate_audio": 1 if params.generate_audio else 0,
                    "return_last_frame": 1 if params.return_last_frame else 0,
                    "web_search": 1 if params.web_search else 0,
                    "mock": 1 if current.mock_mode else 0,
                }
            )
        except sqlite3.IntegrityError:
            # 并发竞态：同一幂等键的请求同时到达，唯一约束兜底。清理孤儿文件后返回已有任务。
            for image_path in image_paths:
                try:
                    Path(image_path).unlink(missing_ok=True)
                except OSError:
                    pass
            winner = await store.find_by_client_request_id(params.client_request_id)
            if winner is not None:
                return _ok(_serialize_row(winner), 200, meta={"reused": True})
            raise

        if local_upload_ids:
            try:
                await request.app.state.video_uploads.attach_many(
                    local_upload_ids, local_task_id
                )
            except ValueError as exc:
                await store.set_failed(local_task_id, str(exc))
                return _ok(_serialize_row(await store.get(local_task_id)), 201)

        payload = build_generation_payload(params, current.model)
        client = request.app.state.client
        try:
            created = await client.create_generation(
                payload, mock_outcome=params.mock_outcome
            )
        except TokenHubError as exc:
            if exc.retriable:
                await store.set_unknown(
                    local_task_id,
                    "提交请求超时或网络错误，结果未知。任务可能已创建并计费，"
                    "请不要重复提交；稍后到 TokenHub 平台调用记录核实，"
                    f"或点击“刷新状态”只读查询。详情：{exc.message}",
                )
            else:
                await store.set_failed(local_task_id, exc.message)
                await request.app.state.video_uploads.release_task(local_task_id)
            logger.warning(
                "创建任务 %s 上游失败：%s", local_task_id, exc.message[:200]
            )
            return _ok(_serialize_row(await store.get(local_task_id)), 201)

        api_task_id = extract_task_id(created)
        if not api_task_id:
            await store.set_failed(
                local_task_id,
                "服务器已回应，但未识别到任务 ID。为避免重复扣费已停止提交，"
                "请先到 TokenHub 平台核对调用记录。",
            )
            await request.app.state.video_uploads.release_task(local_task_id)
            logger.warning("创建任务 %s 未识别到上游任务 ID", local_task_id)
            return _ok(_serialize_row(await store.get(local_task_id)), 201)

        await store.set_api_task_id(local_task_id, api_task_id)
        request.app.state.poller.schedule(local_task_id)
        logger.info(
            "创建任务 %s（上游 %s，mode=%s，mock=%s）",
            local_task_id,
            api_task_id,
            params.mode,
            current.mock_mode,
        )
        return _ok(_serialize_row(await store.get(local_task_id)), 201)

    # ----------------------------------------------------------------- tasks

    @app.get("/api/seedance/tasks/{task_id}")
    async def get_task(task_id: str, request: Request):
        row = await request.app.state.store.get(task_id)
        if row is None:
            raise HTTPException(404, detail={"code": "not_found", "message": "任务不存在"})
        return _ok(_serialize_row(row))

    @app.get("/api/seedance/tasks/by-crid/{client_request_id}")
    async def get_task_by_crid(client_request_id: str, request: Request):
        """按幂等键精确找回任务（页面刷新恢复用）。"""
        if not is_valid_uuid(client_request_id):
            raise HTTPException(404, detail={"code": "not_found", "message": "任务不存在"})
        row = await request.app.state.store.find_by_client_request_id(client_request_id)
        if row is None:
            raise HTTPException(404, detail={"code": "not_found", "message": "任务不存在"})
        return _ok(_serialize_row(row))

    @app.get("/api/seedance/tasks")
    async def list_tasks(
        request: Request,
        page: int = Query(1, ge=1),
        page_size: int = Query(20, ge=1, le=100),
    ):
        items, total = await request.app.state.store.list_tasks(page, page_size)
        return _ok(
            {
                "items": [_serialize_row(row) for row in items],
                "total": total,
                "page": page,
                "page_size": page_size,
            }
        )

    @app.get("/api/seedance/tasks/{task_id}/refresh")
    async def refresh_task(task_id: str, request: Request):
        """只读刷新：重新查询一次上游（GET 不产生费用），不创建新任务。"""
        store = request.app.state.store
        row = await store.get(task_id)
        if row is None:
            raise HTTPException(404, detail={"code": "not_found", "message": "任务不存在"})
        api_task_id = row.get("api_task_id")
        if not api_task_id:
            raise HTTPException(
                409,
                detail={
                    "code": "unknown_submit",
                    "message": "该任务提交时结果未知、未获得上游任务 ID，无法只读查询。"
                    "请到 TokenHub 平台调用记录核对。",
                },
            )
        try:
            data = await request.app.state.client.get_task(api_task_id)
        except TokenHubError as exc:
            return _fail("upstream_error", f"查询上游失败：{exc.message}", 502)

        from backend.client import (
            FAILED_STATUSES,
            SUCCEEDED_STATUSES,
            extract_error_message,
            extract_status,
            extract_usage,
        )

        status = extract_status(data)
        if status in SUCCEEDED_STATUSES:
            video_url = extract_video_url(data)
            if video_url:
                usage = extract_usage(data)
                await store.set_succeeded(
                    task_id,
                    video_url,
                    json.dumps(usage, ensure_ascii=False) if usage else None,
                    last_frame_url=extract_last_frame_url(data),
                )
            else:
                await store.set_failed(task_id, "任务返回成功，但响应中没有视频地址。")
        elif status in FAILED_STATUSES:
            await store.set_failed(
                task_id, extract_error_message(data) or f"任务失败（状态：{status}）。"
            )
        else:
            from backend.store import ACTIVE_STATUSES

            mapped = status if status in ACTIVE_STATUSES else "running"
            await store.update_status(task_id, mapped)
        return _ok(_serialize_row(await store.get(task_id)))

    @app.delete("/api/seedance/tasks/{task_id}")
    async def delete_task(task_id: str, request: Request):
        current: Settings = request.app.state.settings
        store = request.app.state.store
        row = await store.get(task_id)
        if row is None:
            raise HTTPException(404, detail={"code": "not_found", "message": "任务不存在"})
        image_paths = _parse_image_paths(row.get("image_path"))
        await store.delete(task_id)
        logger.info("删除任务 %s", task_id)
        uploads_root = (current.data_dir / "uploads").resolve()
        for image_path in image_paths:
            # 纵深防御：只允许删除 uploads 目录内的文件
            path = Path(image_path)
            try:
                if path.resolve().is_relative_to(uploads_root) and path.is_file():
                    path.unlink(missing_ok=True)
            except OSError as exc:
                logger.warning("清理上传图片 %s 失败：%s", image_path, exc)
        return _ok({"deleted": True})

    # ----------------------------------------------------------------- video

    @app.get("/api/seedance/tasks/{task_id}/video")
    async def get_video(task_id: str, request: Request):
        current: Settings = request.app.state.settings
        store = request.app.state.store
        row = await store.get(task_id)
        if row is None:
            raise HTTPException(404, detail={"code": "not_found", "message": "任务不存在"})
        if row["status"] != "succeeded" or not row.get("video_url"):
            raise HTTPException(
                409,
                detail={"code": "not_ready", "message": "视频尚未生成成功或已失败，无法播放。"},
            )
        video_url = row["video_url"]
        client = request.app.state.client

        if video_url.startswith("mock://"):
            sample = current.mock_video_path
            if sample is None or not Path(sample).is_file():
                raise HTTPException(
                    404,
                    detail={
                        "code": "missing_asset",
                        "message": "Mock 样例视频缺失，请按 README 用 ffmpeg 生成。",
                    },
                )
            return FileResponse(
                Path(sample),
                media_type="video/mp4",
                filename=f"seedance-{task_id}.mp4",
            )

        try:
            response = await client.stream_video(video_url)
            response.raise_for_status()
        except (httpx.HTTPError, TokenHubError):
            # 限时链接可能已过期或地址失效：用只读 GET 刷新一次视频地址
            try:
                api_task_id = row.get("api_task_id") or task_id
                fresh = await client.get_task(api_task_id)
                new_url = extract_video_url(fresh)
                if new_url and new_url != video_url:
                    await store.set_video_url(task_id, new_url)
                    response = await client.stream_video(new_url)
                    response.raise_for_status()
                else:
                    raise RuntimeError("no fresh url")
            except (httpx.HTTPError, TokenHubError, RuntimeError) as exc:
                raise HTTPException(
                    410,
                    detail={
                        "code": "video_expired",
                        "message": "视频链接已过期且无法重新获取，请重新生成视频。",
                    },
                ) from exc

        return StreamingResponse(
            response.aiter_bytes(),
            media_type="video/mp4",
            headers={
                "Content-Disposition": f'attachment; filename="seedance-{task_id}.mp4"'
            },
        )

    @app.get("/api/seedance/tasks/{task_id}/last-frame")
    async def get_last_frame(task_id: str, request: Request):
        """尾帧图代理：与视频同规则——地址不出现在接口响应，仅此端点可访问。"""
        store = request.app.state.store
        row = await store.get(task_id)
        if row is None:
            raise HTTPException(404, detail={"code": "not_found", "message": "任务不存在"})
        last_frame_url = row.get("last_frame_url")
        if (
            row["status"] != "succeeded"
            or not last_frame_url
            or not row.get("return_last_frame")
        ):
            raise HTTPException(
                409,
                detail={"code": "not_ready", "message": "尾帧图尚未生成或任务未开启尾帧返回。"},
            )
        if last_frame_url.startswith("mock://"):
            return Response(
                content=MOCK_LAST_FRAME_PNG,
                media_type="image/png",
                headers={
                    "Content-Disposition": f'attachment; filename="seedance-{task_id}-last-frame.png"'
                },
            )
        try:
            response = await request.app.state.client.stream_video(last_frame_url)
            response.raise_for_status()
        except (httpx.HTTPError, TokenHubError) as exc:
            raise HTTPException(
                410,
                detail={
                    "code": "last_frame_expired",
                    "message": "尾帧图链接已过期，请重新生成视频。",
                },
            ) from exc
        content_type = (response.headers.get("content-type") or "").split(";")[0].strip()
        if not content_type.startswith("image/"):
            # 上游返回了非图片内容（如错误页）：绝不把 HTML 当图片转发
            await response.aclose()
            raise HTTPException(
                502,
                detail={
                    "code": "bad_upstream",
                    "message": "上游尾帧地址返回了非图片内容，已拒绝。",
                },
            )
        return StreamingResponse(
            response.aiter_bytes(),
            media_type=content_type,
            headers={
                "Content-Disposition": f'attachment; filename="seedance-{task_id}-last-frame.png"'
            },
        )

    # ---------------------------------------------------------------- static

    if settings.frontend_dist and Path(settings.frontend_dist).is_dir():
        # Windows 上 mimetypes 依赖注册表且经常缺 .js 映射（返回 text/plain），
        # 配合 nosniff 安全头会导致浏览器拒绝执行模块脚本（整页黑屏）。显式补齐。
        import mimetypes

        mimetypes.add_type("application/javascript", ".js")
        mimetypes.add_type("application/javascript", ".mjs")
        mimetypes.add_type("text/css", ".css")
        mimetypes.add_type("application/json", ".json")
        app.mount(
            "/",
            StaticFiles(directory=str(settings.frontend_dist), html=True),
            name="static",
        )

    return app
