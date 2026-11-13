"""本地病例附件服务：上传校验、PDF/图片文本提取、临时存储与对话注入。

隐私约定：所有识别都在本机完成（pdfplumber / RapidOCR 离线），文件与识别文本
不发送到任何外部服务；附件默认 2 小时过期，后端重启时清理全部残留。
"""

from __future__ import annotations

import os
import threading
import time
import uuid
from pathlib import Path

MAX_ATTACHMENT_BYTES = 10 * 1024 * 1024
MAX_ATTACHMENT_TEXT_CHARS = 8000  # 单个附件提取文本上限
MAX_TOTAL_ATTACHMENT_CHARS = 20000  # 注入对话的总上限
ATTACHMENT_TTL_SECONDS = 2 * 60 * 60
MAX_ATTACHMENTS_PER_MESSAGE = 3

_BASE_DIR = Path(__file__).resolve().parent.parent
_DEFAULT_ROOT = _BASE_DIR / "data" / "runtime" / "attachments"

_MAGIC_CHECKS = (
    ("pdf", b"%PDF-", ".pdf"),
    ("image", b"\x89PNG\r\n\x1a\n", ".png"),
    ("image", b"\xff\xd8\xff", ".jpg"),
)
_WEBP_PREFIX = b"RIFF"

_TEXT_DISCLAIMER = (
    "（以下是用户上传文件的自动识别结果，可能存在识别误差，请以原件为准）"
)


class AttachmentError(ValueError):
    """附件校验或识别失败；message 为可直接展示的中文说明。"""


def detect_kind(data: bytes) -> str | None:
    """按魔数判断附件类型：pdf / image；未知返回 None（不信任扩展名与 Content-Type）。"""
    if not data:
        return None
    for kind, magic, _ext in _MAGIC_CHECKS:
        if data.startswith(magic):
            return kind
    if len(data) >= 12 and data[:4] == _WEBP_PREFIX and data[8:12] == b"WEBP":
        return "image"
    return None


def _extension_for(data: bytes) -> str:
    for _kind, magic, ext in _MAGIC_CHECKS:
        if data.startswith(magic):
            return ext
    return ".webp"


def _extract_pdf_text(path: Path) -> str:
    import pdfplumber

    parts: list[str] = []
    with pdfplumber.open(path) as pdf:
        for page in pdf.pages:
            parts.append(page.extract_text() or "")
    return "\n".join(part for part in parts if part.strip())


_ocr_engine = None
_ocr_lock = threading.Lock()


def _ocr():
    global _ocr_engine
    with _ocr_lock:
        if _ocr_engine is None:
            from rapidocr_onnxruntime import RapidOCR

            _ocr_engine = RapidOCR()
    return _ocr_engine


def _extract_image_text(path: Path) -> str:
    result, _elapsed = _ocr()(str(path))
    if not result:
        return ""
    lines = [line[1] for line in result if len(line) >= 2 and line[1]]
    return "\n".join(lines)


def extract_text(kind: str, path: Path) -> tuple[str, str | None]:
    """提取文本，返回 (文本, 警告)。文本截断到单附件上限。"""
    if kind == "pdf":
        text = _extract_pdf_text(path)
        warning = None if text.strip() else "PDF 中未提取到文字（可能是扫描件，建议上传照片用 OCR 识别）"
    else:
        text = _extract_image_text(path)
        warning = None if text.strip() else "未能从图片中识别出文字，请确认照片清晰、文字完整"
    if len(text) > MAX_ATTACHMENT_TEXT_CHARS:
        text = text[:MAX_ATTACHMENT_TEXT_CHARS]
        warning = (warning + "；" if warning else "") + "识别文本过长，已截断"
    return text, warning


class AttachmentStore:
    """内存索引 + 磁盘临时文件；线程安全，惰性清理过期项。"""

    def __init__(self, root: Path | None = None, ttl: float = ATTACHMENT_TTL_SECONDS) -> None:
        configured = os.environ.get("CAREGUIDE_ATTACHMENTS_DIR", "").strip()
        self.root = Path(root or configured or _DEFAULT_ROOT)
        self.ttl = ttl
        self._items: dict[str, dict] = {}
        self._lock = threading.Lock()
        self.root.mkdir(parents=True, exist_ok=True)
        # 重启后内存索引失效：清掉上一进程留下的所有文件
        self._clear_files()

    def _clear_files(self) -> None:
        try:
            entries = list(self.root.iterdir())
        except OSError:
            return
        for entry in entries:
            if entry.is_file():
                try:
                    entry.unlink(missing_ok=True)
                except OSError:
                    pass

    def sweep(self) -> None:
        now = time.time()
        expired: list[dict] = []
        with self._lock:
            for attachment_id, record in list(self._items.items()):
                if now - record["created_at"] > self.ttl:
                    expired.append(self._items.pop(attachment_id))
        for record in expired:
            try:
                Path(record["path"]).unlink(missing_ok=True)
            except OSError:
                pass

    def save(self, filename: str, content_type: str, data: bytes) -> dict:
        if len(data) > MAX_ATTACHMENT_BYTES:
            raise AttachmentError(
                f"附件超过大小上限（最大 {MAX_ATTACHMENT_BYTES // (1024 * 1024)}MB）"
            )
        kind = detect_kind(data)
        if kind is None:
            raise AttachmentError("附件格式不支持：仅支持 PDF、JPG、PNG、WebP")
        safe_name = Path(filename or "attachment").name[:120] or "attachment"
        attachment_id = uuid.uuid4().hex
        path = self.root / f"{attachment_id}{_extension_for(data)}"
        try:
            path.write_bytes(data)
        except OSError as exc:
            raise AttachmentError("附件保存失败，请稍后重试") from exc
        record = {
            "attachment_id": attachment_id,
            "filename": safe_name,
            "kind": kind,
            "size": len(data),
            "path": str(path),
            "created_at": time.time(),
            "text": "",
            "warning": None,
        }
        with self._lock:
            self._items[attachment_id] = record
        self.sweep()
        return dict(record)

    def set_text(self, attachment_id: str, text: str, warning: str | None) -> dict | None:
        with self._lock:
            record = self._items.get(attachment_id)
            if record is None:
                return None
            record["text"] = text
            record["warning"] = warning
            return dict(record)

    def get(self, attachment_id: str) -> dict | None:
        with self._lock:
            record = self._items.get(attachment_id)
            if record is None:
                return None
            if time.time() - record["created_at"] > self.ttl:
                self._items.pop(attachment_id, None)
                record = None
            result = dict(record) if record is not None else None
        if result is None:
            return None
        return result

    def delete(self, attachment_id: str) -> bool:
        with self._lock:
            record = self._items.pop(attachment_id, None)
        if record is None:
            return False
        try:
            Path(record["path"]).unlink(missing_ok=True)
        except OSError:
            pass
        return True


def compose_attachment_message(
    store: AttachmentStore, message: str, attachment_ids: list[str]
) -> str:
    """把附件识别文本拼进用户消息；未知附件抛 AttachmentError。总文本量有上限。"""
    if not attachment_ids:
        return message
    blocks: list[str] = []
    remaining = MAX_TOTAL_ATTACHMENT_CHARS
    for index, attachment_id in enumerate(attachment_ids, start=1):
        record = store.get(attachment_id)
        if record is None:
            raise AttachmentError("附件不存在或已过期，请重新上传")
        text = (record.get("text") or "").strip()
        warning = record.get("warning")
        if text:
            if len(text) > remaining:
                text = text[:remaining]
                warning = (warning + "；" if warning else "") + "内容过长，已截断"
            remaining = max(0, remaining - len(text))
            body = text
        else:
            body = f"（{warning or '未识别出文字'}）"
        blocks.append(
            f"【附件{index}：{record['filename']} {_TEXT_DISCLAIMER}】\n{body}"
        )
    return "\n\n".join(blocks) + "\n\n" + message
