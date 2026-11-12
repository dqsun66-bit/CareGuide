"""表单参数校验与生成请求构造（纯函数，无 IO）。

v2：支持首尾帧（first_frame/last_frame）、全模态参考（图片 0~9 / 视频 0~3 / 音频 0~3）、
编辑视频、延长视频、尾帧图返回（return_last_frame）、联网搜索（web_search）。
"""

from __future__ import annotations

import base64
import re
from dataclasses import dataclass
from ipaddress import ip_address
from urllib.parse import urlsplit

MODES = ("text", "image", "video", "edit")
RESOLUTIONS = ("480p", "720p", "1080p", "4k")
RATIOS = ("16:9", "9:16", "1:1", "4:3", "3:4", "21:9")
ALLOWED_IMAGE_MIMES = ("image/jpeg", "image/png", "image/webp")
ALLOWED_IMAGE_EXTENSIONS = (".jpg", ".jpeg", ".png", ".webp")
MIME_TO_EXTENSION = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
}
IMAGE_ROLES = ("first_frame", "last_frame", "reference_image")
MAX_PROMPT_CHARS = 8000
MAX_IMAGE_COUNT = 9
MAX_VIDEO_COUNT = 3
MAX_AUDIO_COUNT = 3
MAX_AUDIO_BYTES = 15 * 1024 * 1024  # 官方：单个音频 ≤15MB，请求体 ≤64MB
_UUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
    r"|^[0-9a-fA-F]{32}$"
)
# 魔数嗅探：Content-Type 与扩展名均为客户端可控，必须核对文件真实格式
_MAGIC_CHECKS = {
    "image/jpeg": lambda data: data[:3] == b"\xff\xd8\xff",
    "image/png": lambda data: data[:8] == b"\x89PNG\r\n\x1a\n",
    "image/webp": lambda data: len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP",
}
_PRIVATE_HOSTNAMES = {"localhost"}


def is_valid_uuid(value: str | None) -> bool:
    return isinstance(value, str) and bool(_UUID_RE.match(value.strip()))


class ParamsError(ValueError):
    """参数校验失败；message 为可直接展示给用户的中文说明。"""

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


@dataclass(frozen=True)
class ImagePayload:
    filename: str
    mime: str
    data: bytes
    role: str = "first_frame"


@dataclass(frozen=True)
class CreateParams:
    mode: str
    prompt: str
    duration: int
    resolution: str
    ratio: str
    generate_audio: bool
    client_request_id: str
    mock_outcome: str | None
    images: tuple[ImagePayload, ...]
    videos: tuple[str, ...]
    audios: tuple[str, ...]
    return_last_frame: bool
    web_search: bool


def validate_image(
    filename: str, content_type: str, data: bytes, max_bytes: int, role: str
) -> ImagePayload:
    extension = ("." + filename.rsplit(".", 1)[-1] if "." in filename else "").lower()
    if extension not in ALLOWED_IMAGE_EXTENSIONS or content_type not in ALLOWED_IMAGE_MIMES:
        raise ParamsError("图片格式不支持：仅支持 JPG、PNG、WebP")
    if role not in IMAGE_ROLES:
        raise ParamsError(f"图片角色无效：仅支持 {'、'.join(IMAGE_ROLES)}")
    if len(data) > max_bytes:
        limit_mb = max_bytes // (1024 * 1024)
        raise ParamsError(f"图片大小超过限制（最大 {limit_mb}MB）")
    magic_check = _MAGIC_CHECKS[content_type]
    if not magic_check(data):
        raise ParamsError("图片内容与声明格式不符：仅支持真实的 JPG、PNG、WebP 文件")
    return ImagePayload(filename=filename, mime=content_type, data=data, role=role)


def _reject_private_host(parts, label: str) -> None:
    """公网 HTTPS 校验：拒绝内网/环回/链路本地/保留地址；尾点主机名先归一。"""
    host = (parts.hostname or "").lower().rstrip(".")
    if not host:
        raise ParamsError(f"{label}地址缺少主机名")
    if host in _PRIVATE_HOSTNAMES or host.endswith(".localhost") or host.endswith(".local"):
        raise ParamsError(f"{label}地址指向本地主机，已拒绝")
    try:
        address = ip_address(host)
    except ValueError:
        address = None
    if address is not None and (
        address.is_private
        or address.is_loopback
        or address.is_link_local
        or address.is_reserved
        or address.is_multicast
    ):
        raise ParamsError(f"{label}地址指向内网或保留地址，已拒绝")


def validate_video_ref(url: str) -> str:
    """参考视频仅支持公网 HTTPS URL 或平台素材 ID（asset://）。

    官方文档明确视频不支持 Base64；内网地址一律拒绝（平台侧也无法拉取）。
    """
    if not isinstance(url, str) or not url.strip():
        raise ParamsError("参考视频地址为空")
    url = url.strip()
    if url.startswith("asset://") and len(url) > len("asset://"):
        return url
    try:
        parts = urlsplit(url)
    except ValueError as exc:
        raise ParamsError("参考视频地址格式无效") from exc
    if parts.scheme != "https":
        raise ParamsError("参考视频仅支持 HTTPS 公网地址（不支持 Base64 或 HTTP）")
    _reject_private_host(parts, "参考视频")
    return url


def validate_audio_ref(value: str) -> str:
    """参考音频：公网 HTTPS URL，或 data:audio/<wav|mp3>;base64, 的 Data URI。

    浏览器 FileReader 对 mp3 产出 audio/mpeg、部分环境对 wav 产出 audio/x-wav，
    均需兼容；仅平台支持 mp3/wav 时上游会自行拒绝，本地校验只保证结构安全。
    """
    if not isinstance(value, str) or not value.strip():
        raise ParamsError("参考音频地址为空")
    value = value.strip()
    if value.lower().startswith("data:audio/"):
        match = re.match(
            r"^data:audio/(wav|x-wav|mp3|mpeg);base64,([A-Za-z0-9+/=]+)$",
            value,
            re.IGNORECASE,
        )
        if not match:
            raise ParamsError(
                "参考音频 Data URI 无效：仅支持 data:audio/wav 或 data:audio/mp3 的 Base64 编码"
            )
        encoded = match.group(2)
        # 解码前先按 base64 膨胀系数做长度上限，避免超大字符串先占内存再解码
        if len(encoded) > MAX_AUDIO_BYTES * 4 // 3 + 8:
            raise ParamsError(f"音频大小超过限制（最大 {MAX_AUDIO_BYTES // (1024 * 1024)}MB）")
        try:
            decoded = base64.b64decode(encoded, validate=True)
        except Exception as exc:
            raise ParamsError("参考音频 Base64 解码失败") from exc
        if len(decoded) > MAX_AUDIO_BYTES:
            raise ParamsError(f"音频大小超过限制（最大 {MAX_AUDIO_BYTES // (1024 * 1024)}MB）")
        return value
    try:
        parts = urlsplit(value)
    except ValueError as exc:
        raise ParamsError("参考音频地址格式无效") from exc
    if parts.scheme != "https":
        raise ParamsError("参考音频仅支持 HTTPS 公网地址或 Base64 Data URI")
    _reject_private_host(parts, "参考音频")
    return value


def _parse_int(value: str, label: str) -> int:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError) as exc:
        raise ParamsError(f"参数 {label} 必须是数字") from exc


def _parse_bool(value: str, label: str, default: bool = False) -> bool:
    if value is None:
        return default
    normalized = str(value).strip().casefold()
    if normalized in ("true", "1", "yes", "on"):
        return True
    if normalized in ("false", "0", "no", "off"):
        return False
    raise ParamsError(f"参数 {label} 无效：仅支持 true 或 false")


def parse_create_form(
    *,
    mode: str,
    prompt: str,
    duration: str,
    resolution: str,
    ratio: str,
    generate_audio: str,
    client_request_id: str,
    mock_outcome: str | None = None,
    images: tuple[ImagePayload, ...] = (),
    videos: tuple[str, ...] = (),
    audios: tuple[str, ...] = (),
    return_last_frame: str | None = None,
    web_search: str | None = None,
) -> CreateParams:
    normalized_mode = (mode or "").strip().casefold()
    if normalized_mode not in MODES:
        raise ParamsError(
            "生成模式无效：仅支持 text（文生视频）、image（图生视频）、"
            "video（视频参考）、edit（编辑视频）"
        )

    normalized_prompt = (prompt or "").strip()
    if len(normalized_prompt) > MAX_PROMPT_CHARS:
        raise ParamsError(f"提示词长度超过上限（{MAX_PROMPT_CHARS} 字符）")

    seconds = _parse_int(duration, "时长")
    if not 4 <= seconds <= 15:
        raise ParamsError("时长必须在 4~15 秒之间")

    if resolution not in RESOLUTIONS:
        raise ParamsError(f"分辨率无效：仅支持 {'、'.join(RESOLUTIONS)}")
    if ratio not in RATIOS:
        raise ParamsError(f"画面比例无效：仅支持 {'、'.join(RATIOS)}")

    audio = _parse_bool(generate_audio, "音频生成")
    want_last_frame = _parse_bool(return_last_frame or "false", "尾帧图返回")
    want_web_search = _parse_bool(web_search or "false", "联网搜索")

    normalized_crid = (client_request_id or "").strip()
    if not _UUID_RE.match(normalized_crid):
        raise ParamsError("client_request_id 无效：必须是 UUID 格式（用于防止重复提交扣费）")

    normalized_outcome = (mock_outcome or "").strip().casefold() or None
    if normalized_outcome not in (None, "success", "fail"):
        raise ParamsError("mock_outcome 无效：仅支持 success 或 fail")

    if len(images) > MAX_IMAGE_COUNT:
        raise ParamsError(f"参考图片数量超过上限（最多 {MAX_IMAGE_COUNT} 张）")

    validated_videos = tuple(validate_video_ref(v) for v in videos)
    if len(validated_videos) > MAX_VIDEO_COUNT:
        raise ParamsError(f"参考视频数量超过上限（最多 {MAX_VIDEO_COUNT} 个）")

    validated_audios = tuple(validate_audio_ref(a) for a in audios)
    if len(validated_audios) > MAX_AUDIO_COUNT:
        raise ParamsError(f"参考音频数量超过上限（最多 {MAX_AUDIO_COUNT} 个）")

    if normalized_mode == "text" and not normalized_prompt:
        raise ParamsError("文生视频模式必须填写提示词")
    if normalized_mode == "image" and not images and not normalized_prompt:
        raise ParamsError("图生视频模式必须上传图片或填写提示词")
    if normalized_mode == "edit" and not validated_videos:
        raise ParamsError("编辑视频模式必须提供至少一个待编辑的参考视频")
    if normalized_mode == "video" and not validated_videos and not normalized_prompt:
        raise ParamsError("视频参考模式必须提供参考视频或提示词")

    return CreateParams(
        mode=normalized_mode,
        prompt=normalized_prompt,
        duration=seconds,
        resolution=resolution,
        ratio=ratio,
        generate_audio=audio,
        client_request_id=normalized_crid,
        mock_outcome=normalized_outcome,
        images=images,
        videos=validated_videos,
        audios=validated_audios,
        return_last_frame=want_last_frame,
        web_search=want_web_search,
    )


def image_data_uri(image: ImagePayload) -> str:
    encoded = base64.b64encode(image.data).decode("ascii")
    return f"data:{image.mime};base64,{encoded}"


def build_generation_payload(params: CreateParams, model: str) -> dict:
    """构造 POST /v1/videos/generations 的请求体（content[] 格式，含全模态参考）。"""
    content: list[dict] = []
    if params.prompt:
        content.append({"type": "text", "text": params.prompt})
    for image in params.images:
        content.append(
            {
                "type": "image_url",
                "image_url": {"url": image_data_uri(image)},
                "role": image.role,
            }
        )
    for video in params.videos:
        content.append(
            {"type": "video_url", "video_url": {"url": video}, "role": "reference_video"}
        )
    for audio in params.audios:
        content.append(
            {"type": "audio_url", "audio_url": {"url": audio}, "role": "reference_audio"}
        )
    payload: dict = {
        "model": model,
        "content": content,
        "duration": params.duration,
        "resolution": params.resolution,
        "ratio": params.ratio,
        "generate_audio": params.generate_audio,
    }
    if params.return_last_frame:
        payload["return_last_frame"] = True
    if (
        params.web_search
        and params.mode == "text"
        and not params.images
        and not params.videos
        and not params.audios
    ):
        # 官方：联网搜索仅适用于纯文本输入
        payload["tools"] = [{"type": "web_search"}]
    return payload


def describe_materials(params: CreateParams) -> list[dict]:
    """任务历史展示用的素材摘要：绝不包含图片/音频的原始字节与 Base64。"""
    materials: list[dict] = []
    for index, image in enumerate(params.images, start=1):
        materials.append(
            {"kind": "image", "role": image.role, "n": index, "filename": image.filename}
        )
    for index, video in enumerate(params.videos, start=1):
        materials.append(
            {
                "kind": "video",
                "role": "reference_video",
                "n": index,
                "source": "asset" if video.startswith("asset://") else "https",
            }
        )
    for index, audio in enumerate(params.audios, start=1):
        materials.append(
            {
                "kind": "audio",
                "role": "reference_audio",
                "n": index,
                "source": "data-uri" if audio.startswith("data:") else "https",
            }
        )
    return materials
