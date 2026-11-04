"""Deterministic normalization for official source text and URLs."""
from __future__ import annotations

import hashlib
import re
import unicodedata
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from .models import _validate_public_https


TRACKING_KEYS = {"spm", "from", "share_token"}
NAVIGATION_LINES = {
    "首页",
    "就医指南",
    "当前位置",
    "返回顶部",
    "打印本页",
    "关闭窗口",
    "分享",
}


def canonical_url(value: str) -> str:
    """Return a stable public HTTPS URL without tracking parameters."""
    _validate_public_https(value)
    parsed = urlparse(value)
    host = parsed.hostname.casefold()
    netloc = host if parsed.port in (None, 443) else f"{host}:{parsed.port}"
    query = [
        (key, item)
        for key, item in parse_qsl(parsed.query, keep_blank_values=True)
        if not key.casefold().startswith("utm_") and key.casefold() not in TRACKING_KEYS
    ]
    query.sort()
    path = parsed.path or "/"
    return urlunparse(("https", netloc, path, "", urlencode(query, doseq=True), ""))


def normalize_text(value: str) -> str:
    """Normalize Unicode and layout without changing factual wording."""
    text = unicodedata.normalize("NFKC", str(value)).replace("\r\n", "\n").replace("\r", "\n")
    normalized_lines: list[str] = []
    prior_blank = False
    for raw_line in text.split("\n"):
        line = re.sub(r"[\t\f\v ]+", " ", raw_line).strip()
        if line in NAVIGATION_LINES:
            continue
        if not line:
            if normalized_lines and not prior_blank:
                normalized_lines.append("")
            prior_blank = True
            continue
        normalized_lines.append(line)
        prior_blank = False
    while normalized_lines and not normalized_lines[-1]:
        normalized_lines.pop()
    return "\n".join(normalized_lines)


def stable_id(*parts: str) -> str:
    normalized = "|".join(normalize_text(part).casefold() for part in parts)
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:16]
