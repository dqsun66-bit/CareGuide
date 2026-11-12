"""Local video container validation using ffprobe."""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import subprocess


class VideoProbeError(ValueError):
    """Raised when an uploaded video cannot be safely used by Seedance."""


@dataclass(frozen=True)
class VideoMetadata:
    duration: float
    content_type: str
    size: int


def probe_video(path: Path, filename: str, declared_type: str) -> VideoMetadata:
    """Validate a local MP4/MOV and return the metadata needed by the UI."""
    suffix = Path(filename).suffix.casefold()
    if suffix not in {".mp4", ".mov"}:
        raise VideoProbeError("仅支持 MP4 或 MOV 视频")
    if declared_type and declared_type.casefold() not in {
        "video/mp4",
        "video/quicktime",
        "application/octet-stream",
    }:
        raise VideoProbeError("视频 MIME 类型不受支持")

    with path.open("rb") as source:
        header = source.read(16)
    if len(header) < 12 or header[4:8] != b"ftyp":
        raise VideoProbeError("视频内容与文件扩展名不符")

    try:
        completed = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_streams",
                "-show_format",
                "-of",
                "json",
                str(path),
            ],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise VideoProbeError("无法运行 ffprobe，请先安装 FFmpeg") from exc
    if completed.returncode != 0:
        raise VideoProbeError("无法读取视频信息，请确认文件未损坏")
    try:
        payload = json.loads(completed.stdout)
        duration = float(payload.get("format", {}).get("duration", 0))
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise VideoProbeError("ffprobe 返回了无效的视频信息") from exc
    if not any(stream.get("codec_type") == "video" for stream in payload.get("streams", [])):
        raise VideoProbeError("文件中没有视频流")
    if not 2 <= duration <= 15:
        raise VideoProbeError("视频时长必须为 2～15 秒")
    content_type = "video/quicktime" if suffix == ".mov" else "video/mp4"
    return VideoMetadata(duration=duration, content_type=content_type, size=path.stat().st_size)
