"""Bounded, allowlisted fetching of official hospital source documents."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
from urllib.parse import parse_qsl, urljoin, urlparse

import httpx

from .normalize import canonical_url


USER_AGENT = "CareGuideOfficialCorpus/1.0 (+offline evidence collection)"
REDIRECT_STATUSES = {301, 302, 303, 307, 308}
SECRET_QUERY_NAMES = {
    "access_key",
    "access_token",
    "api_key",
    "apikey",
    "authorization",
    "key",
    "password",
    "secret",
    "token",
}
SUPPORTED_MEDIA_TYPES = {"text/html", "application/pdf", "text/plain", "application/json"}


class SnapshotFetchError(RuntimeError):
    """A safe, credential-free description of a snapshot fetch failure."""


@dataclass(frozen=True)
class SnapshotResult:
    canonical_url: str
    status_code: int
    content_type: str
    collected_at: datetime
    sha256: str
    body: bytes

    def metadata(self) -> dict[str, object]:
        return {
            "canonical_url": self.canonical_url,
            "status_code": self.status_code,
            "content_type": self.content_type,
            "collected_at": self.collected_at.isoformat(),
            "sha256": self.sha256,
            "size_bytes": len(self.body),
        }


def _normalized_hosts(allowed_hosts: set[str]) -> set[str]:
    hosts = {host.casefold().rstrip(".") for host in allowed_hosts if host.strip()}
    if not hosts:
        raise ValueError("allowed_hosts must not be empty")
    for host in hosts:
        canonical_url(f"https://{host}/")
    return hosts


def _validate_request_url(value: str, allowed_hosts: set[str]) -> str:
    parsed = urlparse(value)
    for name, _ in parse_qsl(parsed.query, keep_blank_values=True):
        normalized_name = name.casefold().replace("-", "_")
        if normalized_name in SECRET_QUERY_NAMES or normalized_name.endswith(("_token", "_key", "_password")):
            raise ValueError("URL contains a sensitive query parameter")
    safe_url = canonical_url(value)
    host = urlparse(safe_url).hostname
    if host is None or host.casefold().rstrip(".") not in allowed_hosts:
        raise ValueError("URL host is not allowlisted")
    return safe_url


def _read_bounded(response: httpx.Response, max_bytes: int) -> bytes:
    declared = response.headers.get("content-length")
    if declared:
        try:
            if int(declared) > max_bytes:
                raise SnapshotFetchError("snapshot exceeds size limit")
        except ValueError as exc:
            raise SnapshotFetchError("snapshot has an invalid content length") from exc
    if response.is_stream_consumed:
        body = response.content
        if len(body) > max_bytes:
            raise SnapshotFetchError("snapshot exceeds size limit")
        return body
    body = bytearray()
    for chunk in response.iter_raw():
        body.extend(chunk)
        if len(body) > max_bytes:
            raise SnapshotFetchError("snapshot exceeds size limit")
    return bytes(body)


def fetch_snapshot(
    url: str,
    *,
    allowed_hosts: set[str],
    client: httpx.Client | None = None,
    max_bytes: int = 2_000_000,
    max_redirects: int = 3,
) -> SnapshotResult:
    """Fetch one public official document without credentials, cookies or proxy inheritance."""
    if max_bytes <= 0:
        raise ValueError("max_bytes must be positive")
    if max_redirects < 0:
        raise ValueError("max_redirects must not be negative")
    hosts = _normalized_hosts(allowed_hosts)
    current_url = _validate_request_url(url, hosts)
    owned_client = client is None
    if client is None:
        client = httpx.Client(
            timeout=httpx.Timeout(connect=10.0, read=20.0, write=20.0, pool=10.0),
            follow_redirects=False,
            trust_env=False,
        )
    client.cookies.clear()
    try:
        for redirects in range(max_redirects + 1):
            request = httpx.Request(
                "GET",
                current_url,
                headers={"user-agent": USER_AGENT, "accept": "*/*", "accept-encoding": "identity"},
            )
            try:
                response = client.send(request, stream=True, follow_redirects=False, auth=None)
            except httpx.HTTPError:
                raise SnapshotFetchError("snapshot request failed") from None
            try:
                if response.status_code in REDIRECT_STATUSES:
                    if redirects >= max_redirects:
                        raise SnapshotFetchError("too many redirects")
                    location = response.headers.get("location")
                    if not location:
                        raise SnapshotFetchError("redirect response is missing a target")
                    try:
                        current_url = _validate_request_url(urljoin(current_url, location), hosts)
                    except ValueError as exc:
                        raise SnapshotFetchError("redirect target is not allowed") from exc
                    continue
                if response.status_code < 200 or response.status_code >= 300:
                    raise SnapshotFetchError(f"snapshot request returned HTTP {response.status_code}")
                content_type = response.headers.get("content-type", "").strip()
                media_type = content_type.partition(";")[0].strip().casefold()
                if media_type not in SUPPORTED_MEDIA_TYPES:
                    raise SnapshotFetchError("unsupported content type")
                content_encoding = response.headers.get("content-encoding", "identity").strip().casefold()
                if content_encoding not in {"", "identity"}:
                    raise SnapshotFetchError("unsupported content encoding")
                media_limit = 15_000_000 if media_type == "application/pdf" else 2_000_000
                body = _read_bounded(response, min(max_bytes, media_limit))
                if media_type == "application/json":
                    try:
                        body.decode("utf-8")
                    except UnicodeDecodeError as exc:
                        raise SnapshotFetchError("JSON snapshot is not UTF-8") from exc
                return SnapshotResult(
                    canonical_url=current_url,
                    status_code=response.status_code,
                    content_type=content_type,
                    collected_at=datetime.now(timezone.utc),
                    sha256=hashlib.sha256(body).hexdigest(),
                    body=body,
                )
            finally:
                response.close()
                client.cookies.clear()
    finally:
        client.cookies.clear()
        if owned_client:
            client.close()
    raise SnapshotFetchError("snapshot request did not produce a document")


def default_snapshot_limit(url: str) -> int:
    """Allow the fetcher to select its stricter limit from the response media type."""
    return 15_000_000
