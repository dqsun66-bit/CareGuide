"""本地病例附件：上传校验、文本提取、临时存储与对话注入。"""

import io
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from careguide.api import app
from careguide.attachment_service import (
    MAX_ATTACHMENT_BYTES,
    MAX_TOTAL_ATTACHMENT_CHARS,
    AttachmentError,
    AttachmentStore,
    compose_attachment_message,
    detect_kind,
    extract_text,
)

PNG_BYTES = (
    b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
)
JPEG_BYTES = b"\xff\xd8\xff\xe0" + b"\x00" * 64
PDF_BYTES = b"%PDF-1.4\n1 0 obj<<>>endobj\ntrailer<<>>\n%%EOF\n"


class TestDetectKind:
    def test_magic_bytes(self):
        assert detect_kind(PNG_BYTES) == "image"
        assert detect_kind(JPEG_BYTES) == "image"
        assert detect_kind(PDF_BYTES) == "pdf"

    def test_unknown(self):
        assert detect_kind(b"not-an-attachment") is None
        assert detect_kind(b"") is None


class TestStore:
    def make_store(self, tmp_path, ttl=3600):
        return AttachmentStore(root=tmp_path / "attachments", ttl=ttl)

    def test_save_and_get(self, tmp_path):
        store = self.make_store(tmp_path)
        record = store.save("病例照片.png", "image/png", PNG_BYTES)
        assert record["kind"] == "image"
        assert record["size"] == len(PNG_BYTES)
        assert record["filename"] == "病例照片.png"
        assert Path(record["path"]).is_file()
        assert store.get(record["attachment_id"]) is not None

    def test_filename_sanitized_to_basename(self, tmp_path):
        store = self.make_store(tmp_path)
        record = store.save("../../etc/passwd.png", "image/png", PNG_BYTES)
        assert record["filename"] == "passwd.png"
        assert ".." not in record["filename"]

    def test_rejects_oversized(self, tmp_path):
        store = self.make_store(tmp_path)
        with pytest.raises(AttachmentError, match="10MB"):
            store.save("big.png", "image/png", PNG_BYTES + b"\x00" * MAX_ATTACHMENT_BYTES)

    def test_rejects_unknown_kind(self, tmp_path):
        store = self.make_store(tmp_path)
        with pytest.raises(AttachmentError, match="不支持"):
            store.save("a.txt", "text/plain", b"hello")

    def test_expiry(self, tmp_path):
        store = self.make_store(tmp_path, ttl=0)
        record = store.save("a.png", "image/png", PNG_BYTES)
        time.sleep(0.01)
        assert store.get(record["attachment_id"]) is None

    def test_delete_removes_file(self, tmp_path):
        store = self.make_store(tmp_path)
        record = store.save("a.png", "image/png", PNG_BYTES)
        path = Path(record["path"])
        assert store.delete(record["attachment_id"]) is True
        assert not path.exists()
        assert store.get(record["attachment_id"]) is None


class TestCompose:
    def test_injects_text_with_disclaimer(self, tmp_path):
        store = TestStoreFactory(tmp_path)
        record = store.save("病例.png", "image/png", PNG_BYTES)
        store.set_text(record["attachment_id"], "血压 130/85", None)
        message = compose_attachment_message(store, "帮我看看这份报告", [record["attachment_id"]])
        assert "帮我看看这份报告" in message
        assert "血压 130/85" in message
        assert "识别误差" in message

    def test_missing_attachment_raises(self, tmp_path):
        store = TestStoreFactory(tmp_path)
        with pytest.raises(AttachmentError, match="不存在或已过期"):
            compose_attachment_message(store, "hi", ["missing-id"])

    def test_total_chars_capped(self, tmp_path):
        store = TestStoreFactory(tmp_path)
        record = store.save("a.png", "image/png", PNG_BYTES)
        store.set_text(record["attachment_id"], "字" * (MAX_TOTAL_ATTACHMENT_CHARS * 2), None)
        message = compose_attachment_message(store, "hi", [record["attachment_id"]])
        assert len(message) <= MAX_TOTAL_ATTACHMENT_CHARS + 500

    def test_empty_text_marks_warning(self, tmp_path):
        store = TestStoreFactory(tmp_path)
        record = store.save("a.png", "image/png", PNG_BYTES)
        store.set_text(record["attachment_id"], "", "未能识别出文字")
        message = compose_attachment_message(store, "hi", [record["attachment_id"]])
        assert "未能识别" in message


class TestExtractText:
    def test_truncates_to_limit(self, tmp_path, monkeypatch):
        import careguide.attachment_service as service

        monkeypatch.setattr(service, "_extract_pdf_text", lambda path: "长" * 99999)
        text, warning = extract_text("pdf", tmp_path / "x.pdf")
        assert len(text) <= service.MAX_ATTACHMENT_TEXT_CHARS
        assert warning is not None


class TestAttachmentApi:
    def test_upload_returns_metadata_and_preview(self, monkeypatch):
        import careguide.api as api

        monkeypatch.setattr(api, "extract_text", lambda kind, path: ("识别出的病例文字", None))
        response = TestClient(app).post(
            "/attachments",
            files={"file": ("病例.png", PNG_BYTES, "image/png")},
        )
        assert response.status_code == 201, response.text
        data = response.json()
        assert data["kind"] == "image"
        assert data["text_preview"] == "识别出的病例文字"
        assert data["text_chars"] == len("识别出的病例文字")
        assert "attachment_id" in data
        # 清理
        TestClient(app).delete(f"/attachments/{data['attachment_id']}")

    def test_upload_rejects_bad_magic(self):
        response = TestClient(app).post(
            "/attachments",
            files={"file": ("病毒.exe", b"MZ\x90\x00", "application/octet-stream")},
        )
        assert response.status_code == 422

    def test_delete_missing_returns_404(self):
        response = TestClient(app).delete("/attachments/nope")
        assert response.status_code == 404


class TestStoreFactory(AttachmentStore):
    def __init__(self, tmp_path):
        super().__init__(root=tmp_path / "attachments", ttl=3600)


# 1x1 有效 PNG（PIL 可打开，模拟真实图片）
PNG = bytes.fromhex('89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4890000000d4944415478da6364f8cf500f00038601805a347d6b0000000049454e44ae426082')


class TestOcrFallback:
    def test_image_fallback_uses_preprocessed_copy(self, tmp_path, monkeypatch):
        """原图 OCR 为空时走预处理重试，且预处理文件会被清理。"""
        import careguide.attachment_service as service

        calls = []

        class FakeEngine:
            def __call__(self, path):
                calls.append(path)
                if str(path).endswith(".pre.png"):
                    return [["box", "预处理识别成功", 0.99]], 0
                return None, 0

        monkeypatch.setattr(service, "_ocr", lambda: FakeEngine())
        image = tmp_path / "blur.png"
        image.write_bytes(PNG)

        text = service._extract_image_text(image)
        assert text == "预处理识别成功"
        assert len(calls) == 2, "应先试原图再试预处理"
        assert not list(tmp_path.glob("*.pre.png")), "预处理临时文件必须清理"

    def test_image_fallback_gives_empty_when_both_fail(self, tmp_path, monkeypatch):
        import careguide.attachment_service as service

        class EmptyEngine:
            def __call__(self, path):
                return None, 0

        monkeypatch.setattr(service, "_ocr", lambda: EmptyEngine())
        image = tmp_path / "blur.png"
        image.write_bytes(PNG)
        assert service._extract_image_text(image) == ""
        assert not list(tmp_path.glob("*.pre.png"))
