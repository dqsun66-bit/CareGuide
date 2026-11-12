"""Loopback-only HTTP server for narrowly scoped temporary video access."""

from __future__ import annotations

import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import threading
from urllib.parse import unquote, urlsplit


class TemporaryMediaServer:
    def __init__(self) -> None:
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self._items: dict[str, tuple[Path, str]] = {}
        self._lock = threading.Lock()

    @property
    def port(self) -> int:
        if self._server is None:
            raise RuntimeError("临时素材服务尚未启动")
        return int(self._server.server_port)

    def start(self) -> None:
        if self._server is not None:
            return
        owner = self

        class Handler(BaseHTTPRequestHandler):
            server_version = "SeedanceMedia/1.0"

            def log_message(self, format_string, *args):
                return

            def do_HEAD(self):
                self._serve(send_body=False)

            def do_GET(self):
                self._serve(send_body=True)

            def do_POST(self):
                self.send_error(405)

            def do_PUT(self):
                self.send_error(405)

            def do_DELETE(self):
                self.send_error(405)

            def _serve(self, *, send_body: bool) -> None:
                item = owner._lookup(urlsplit(self.path).path)
                if item is None:
                    self.send_error(404)
                    return
                path, content_type = item
                try:
                    size = path.stat().st_size
                    start, end, partial = _parse_range(self.headers.get("Range"), size)
                except (OSError, ValueError):
                    if self.headers.get("Range"):
                        self.send_response(416)
                        self.send_header("Content-Range", f"bytes */{path.stat().st_size if path.exists() else 0}")
                        self.end_headers()
                    else:
                        self.send_error(404)
                    return
                length = end - start + 1
                self.send_response(206 if partial else 200)
                self.send_header("Content-Type", content_type)
                self.send_header("Accept-Ranges", "bytes")
                self.send_header("Content-Length", str(length))
                self.send_header("Cache-Control", "no-store")
                if partial:
                    self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
                self.end_headers()
                if not send_body:
                    return
                with path.open("rb") as source:
                    source.seek(start)
                    remaining = length
                    while remaining:
                        chunk = source.read(min(64 * 1024, remaining))
                        if not chunk:
                            break
                        self.wfile.write(chunk)
                        remaining -= len(chunk)

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    def publish(self, token: str, path: Path, content_type: str) -> None:
        if self._server is None:
            raise RuntimeError("临时素材服务尚未启动")
        with self._lock:
            self._items[token] = (Path(path), content_type)

    def unpublish(self, token: str) -> None:
        with self._lock:
            self._items.pop(token, None)

    def stop(self) -> None:
        server = self._server
        thread = self._thread
        self._server = None
        self._thread = None
        with self._lock:
            self._items.clear()
        if server is not None:
            server.shutdown()
            server.server_close()
        if thread is not None:
            thread.join(timeout=2)

    def _lookup(self, request_path: str) -> tuple[Path, str] | None:
        prefix = "/media/"
        if not request_path.startswith(prefix):
            return None
        requested = unquote(request_path[len(prefix) :])
        if not requested or "/" in requested or "\\" in requested:
            return None
        with self._lock:
            snapshot = tuple(self._items.items())
        for token, item in snapshot:
            if hmac.compare_digest(requested, token):
                return item
        return None


def _parse_range(header: str | None, size: int) -> tuple[int, int, bool]:
    if size <= 0:
        raise ValueError("empty file")
    if not header:
        return 0, size - 1, False
    if not header.startswith("bytes=") or "," in header:
        raise ValueError("invalid range")
    value = header[6:].strip()
    start_text, separator, end_text = value.partition("-")
    if not separator:
        raise ValueError("invalid range")
    if not start_text:
        suffix = int(end_text)
        if suffix <= 0:
            raise ValueError("invalid suffix")
        start = max(0, size - suffix)
        end = size - 1
    else:
        start = int(start_text)
        end = int(end_text) if end_text else size - 1
    if start < 0 or start >= size or end < start:
        raise ValueError("unsatisfiable range")
    return start, min(end, size - 1), True
