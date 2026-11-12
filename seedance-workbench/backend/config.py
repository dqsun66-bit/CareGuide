"""环境变量与运行配置。

所有环境变量通过 seedance-workbench/.env 或进程环境读取；
TOKENHUB_API_KEY 缺失且未强制关闭 Mock 时自动进入 Mock 模式（不会真实扣费）。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent  # seedance-workbench/


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, "").strip() or default)
    except ValueError:
        return default


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "").strip() or default)
    except ValueError:
        return default


def _env_price(name: str) -> float | None:
    value = os.environ.get(name, "").strip()
    if not value:
        return None
    try:
        return float(value)
    except ValueError:
        return None


@dataclass(frozen=True)
class Settings:
    """工作台全部运行配置。测试可直接构造本对象，无需真实环境变量。"""

    api_key: str = ""
    base_url: str = "https://aigw.telecomjs.com"
    model: str = "doubao-seedance-2-0-260128"
    # auto：无 Key 自动 Mock；true：强制 Mock；false：强制真实（无 Key 会 401）
    mock_override: str = "auto"
    poll_interval: float = 10.0
    max_wait: float = 900.0
    resume_window: float = 1800.0
    price_cny_per_second: float | None = None
    max_image_bytes: int = 8 * 1024 * 1024
    max_video_bytes: int = 50 * 1024 * 1024
    cloudflared_path: str = ""
    video_upload_ttl: float = 1800.0
    video_post_task_grace: float = 300.0
    tunnel_start_timeout: float = 30.0
    max_generations_per_hour: int = 20
    data_dir: Path = field(default_factory=lambda: BASE_DIR / "data")
    mock_video_path: Path | None = None
    mock_queued_seconds: float = 2.0
    mock_running_seconds: float = 10.0
    frontend_dist: Path | None = None

    @property
    def mock_mode(self) -> bool:
        if self.mock_override.casefold() == "true":
            return True
        if self.mock_override.casefold() == "false":
            return False
        return not self.api_key.strip()


def load_settings() -> Settings:
    """从 .env / 进程环境构建配置。"""
    try:
        from dotenv import load_dotenv

        load_dotenv(BASE_DIR / ".env")
    except ImportError:  # python-dotenv 未安装时退化为纯进程环境
        pass

    data_dir = Path(os.environ.get("SEEDANCE_DATA_DIR", "").strip() or (BASE_DIR / "data"))
    if not data_dir.is_absolute():
        # 相对路径一律以项目根（seedance-workbench/）为基准，避免换目录启动写错位置
        data_dir = (BASE_DIR / data_dir).resolve()
    dist = BASE_DIR / "frontend" / "dist"
    return Settings(
        api_key=os.environ.get("TOKENHUB_API_KEY", "").strip(),
        base_url=os.environ.get("TOKENHUB_BASE_URL", "https://aigw.telecomjs.com").rstrip("/"),
        model=os.environ.get("TOKENHUB_SEEDANCE_MODEL", "doubao-seedance-2-0-260128"),
        mock_override=os.environ.get("SEEDANCE_MOCK", "auto").strip().casefold(),
        poll_interval=_env_float("SEEDANCE_POLL_INTERVAL_SECONDS", 10.0),
        max_wait=_env_float("SEEDANCE_MAX_WAIT_SECONDS", 900.0),
        resume_window=_env_float("SEEDANCE_RESUME_WINDOW_SECONDS", 1800.0),
        price_cny_per_second=_env_price("SEEDANCE_PRICE_CNY_PER_SECOND"),
        max_image_bytes=_env_int("SEEDANCE_MAX_IMAGE_BYTES", 8 * 1024 * 1024),
        max_video_bytes=_env_int("SEEDANCE_MAX_VIDEO_BYTES", 50 * 1024 * 1024),
        cloudflared_path=os.environ.get("SEEDANCE_CLOUDFLARED_PATH", "").strip(),
        video_upload_ttl=_env_float("SEEDANCE_VIDEO_UPLOAD_TTL_SECONDS", 1800.0),
        video_post_task_grace=_env_float("SEEDANCE_VIDEO_POST_TASK_GRACE_SECONDS", 300.0),
        tunnel_start_timeout=_env_float("SEEDANCE_TUNNEL_START_TIMEOUT_SECONDS", 30.0),
        max_generations_per_hour=_env_int("SEEDANCE_MAX_GENERATIONS_PER_HOUR", 20),
        data_dir=data_dir,
        mock_video_path=data_dir / "assets" / "mock-sample.mp4",
        frontend_dist=dist if dist.is_dir() else None,
    )
