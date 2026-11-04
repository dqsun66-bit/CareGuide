from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone

import httpx
import pytest

from careguide.hospital_corpus.snapshot import SnapshotFetchError, SnapshotResult, fetch_snapshot


ALLOWED = {"www.jyry.com"}
URL = "https://www.jyry.com/guide/appointment.html"


def _client(handler, *, headers=None) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler), headers=headers)


def test_fetches_allowlisted_document_and_hashes_exact_body():
    body = b"<html><body>official appointment guide</body></html>"

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["user-agent"].startswith("CareGuideOfficialCorpus/")
        return httpx.Response(200, headers={"content-type": "text/html; charset=utf-8"}, content=body)

    with _client(handler) as client:
        result = fetch_snapshot(URL, allowed_hosts=ALLOWED, client=client, max_bytes=2_000_000)

    assert result.status_code == 200
    assert result.body == body
    assert result.sha256 == hashlib.sha256(body).hexdigest()
    assert result.content_type.startswith("text/html")
    assert result.canonical_url == URL


@pytest.mark.parametrize(
    "url",
    (
        "http://www.jyry.com/guide.html",
        "https://user:password@www.jyry.com/guide.html",
        "https://127.0.0.1/guide.html",
        "https://localhost/guide.html",
        "https://evil.example/guide.html",
    ),
)
def test_rejects_unsafe_or_non_allowlisted_urls_without_request(url):
    with _client(lambda request: pytest.fail(f"unexpected request: {request.url}")) as client:
        with pytest.raises(ValueError):
            fetch_snapshot(url, allowed_hosts=ALLOWED, client=client)


@pytest.mark.parametrize("name", ("token", "access_token", "api_key", "apikey", "password", "key"))
def test_rejects_secret_query_parameter_names(name):
    with _client(lambda request: pytest.fail(f"unexpected request: {request.url}")) as client:
        with pytest.raises(ValueError, match="sensitive query parameter"):
            fetch_snapshot(f"{URL}?{name}=secret-value", allowed_hosts=ALLOWED, client=client)


def test_rejects_redirect_to_non_allowlisted_host():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"location": "https://evil.example/page"})

    with _client(handler) as client:
        with pytest.raises(SnapshotFetchError, match="redirect target is not allowed"):
            fetch_snapshot(URL, allowed_hosts=ALLOWED, client=client)


def test_rejects_more_than_three_redirects():
    def handler(request: httpx.Request) -> httpx.Response:
        count = int(request.url.params.get("step", "0"))
        return httpx.Response(302, headers={"location": f"{URL}?step={count + 1}"})

    with _client(handler) as client:
        with pytest.raises(SnapshotFetchError, match="too many redirects"):
            fetch_snapshot(URL, allowed_hosts=ALLOWED, client=client)


def test_rejects_body_over_limit():
    with _client(lambda request: httpx.Response(200, headers={"content-type": "text/html"}, content=b"12345")) as client:
        with pytest.raises(SnapshotFetchError, match="size limit"):
            fetch_snapshot(URL, allowed_hosts=ALLOWED, client=client, max_bytes=4)


def test_rejects_unsupported_content_type():
    with _client(lambda request: httpx.Response(200, headers={"content-type": "image/png"}, content=b"png")) as client:
        with pytest.raises(SnapshotFetchError, match="unsupported content type"):
            fetch_snapshot(URL, allowed_hosts=ALLOWED, client=client)


def test_does_not_retain_set_cookie_from_response():
    def handler(request: httpx.Request) -> httpx.Response:
        assert "cookie" not in request.headers
        return httpx.Response(
            200,
            headers={"content-type": "text/plain", "set-cookie": "session=private; Path=/"},
            content=b"official text",
        )

    with _client(handler) as client:
        fetch_snapshot(URL, allowed_hosts=ALLOWED, client=client)
        assert len(client.cookies) == 0


def test_transport_error_never_exposes_authorization_value():
    secret = "Bearer should-never-appear"

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("failed while using " + secret, request=request)

    with _client(handler, headers={"authorization": secret}) as client:
        with pytest.raises(SnapshotFetchError) as caught:
            fetch_snapshot(URL, allowed_hosts=ALLOWED, client=client)
    assert secret not in str(caught.value)


def _write_registry(path):
    path.write_text(json.dumps([{
        "source_id": "src_tz2_appointment",
        "hospital_id": "js_taizhou_003",
        "campus_id": "js_taizhou_003_main",
        "title": "预约挂号",
        "url": URL,
        "source_tier": "hospital_official",
        "category": "appointment",
        "published_at": None,
        "valid_until": "2027-05-03",
        "collected_at": "2026-11-04T15:00:00+08:00",
        "reviewed_at": None,
        "review_status": "needs_review",
        "snapshot_path": "snapshots/js_taizhou_003/src_tz2_appointment.html",
        "http_status": 200,
        "content_type": "text/html",
        "retrieved_sha256": "0" * 64,
    }]), encoding="utf-8")


def test_snapshot_cli_dry_run_makes_no_external_call(tmp_path, monkeypatch, capsys):
    from scripts import fetch_hospital_sources as command

    registry = tmp_path / "sources.json"
    _write_registry(registry)
    monkeypatch.setattr(command, "fetch_snapshot", lambda *args, **kwargs: pytest.fail("network called"))
    result = command.main([
        "--registry", str(registry),
        "--source-id", "src_tz2_appointment",
        "--output-dir", str(tmp_path / "snapshots"),
    ])
    payload = json.loads(capsys.readouterr().out)
    assert result == 0
    assert payload["external_calls"] == 0
    assert payload["canonical_url"] == URL
    assert payload["target"].endswith("js_taizhou_003/src_tz2_appointment.html")
    assert not (tmp_path / "snapshots").exists()


def test_snapshot_cli_apply_writes_exact_body_and_metadata(tmp_path, monkeypatch, capsys):
    from scripts import fetch_hospital_sources as command

    registry = tmp_path / "sources.json"
    _write_registry(registry)
    body = b"official snapshot bytes"
    fake = SnapshotResult(
        canonical_url=URL,
        status_code=200,
        content_type="text/html; charset=utf-8",
        collected_at=datetime(2026, 11, 4, 7, 0, tzinfo=timezone.utc),
        sha256=hashlib.sha256(body).hexdigest(),
        body=body,
    )
    monkeypatch.setattr(command, "fetch_snapshot", lambda *args, **kwargs: fake)
    output_dir = tmp_path / "snapshots"
    result = command.main([
        "--registry", str(registry),
        "--source-id", "src_tz2_appointment",
        "--output-dir", str(output_dir),
        "--apply",
    ])
    payload = json.loads(capsys.readouterr().out)
    target = output_dir / "js_taizhou_003" / "src_tz2_appointment.html"
    assert result == 0
    assert payload["external_calls"] == 1
    assert target.read_bytes() == body
    metadata = json.loads(target.with_suffix(".html.metadata.json").read_text(encoding="utf-8"))
    assert metadata["sha256"] == fake.sha256
    assert metadata["canonical_url"] == URL


def test_snapshot_cli_does_not_call_network_when_target_exists(tmp_path, monkeypatch, capsys):
    from scripts import fetch_hospital_sources as command

    registry = tmp_path / "sources.json"
    _write_registry(registry)
    output_dir = tmp_path / "snapshots"
    target = output_dir / "js_taizhou_003" / "src_tz2_appointment.html"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"reviewed existing evidence")
    monkeypatch.setattr(command, "fetch_snapshot", lambda *args, **kwargs: pytest.fail("network called"))
    result = command.main([
        "--registry", str(registry),
        "--source-id", "src_tz2_appointment",
        "--output-dir", str(output_dir),
        "--apply",
    ])
    payload = json.loads(capsys.readouterr().out)
    assert result == 1
    assert payload == {"error_type": "snapshot_fetch_failed", "status": "error"}
    assert target.read_bytes() == b"reviewed existing evidence"


def test_snapshot_cli_removes_body_if_metadata_write_fails(tmp_path, monkeypatch, capsys):
    from scripts import fetch_hospital_sources as command

    registry = tmp_path / "sources.json"
    _write_registry(registry)
    body = b"official snapshot bytes"
    fake = SnapshotResult(
        canonical_url=URL,
        status_code=200,
        content_type="text/html",
        collected_at=datetime(2026, 11, 4, 7, 0, tzinfo=timezone.utc),
        sha256=hashlib.sha256(body).hexdigest(),
        body=body,
    )
    monkeypatch.setattr(command, "fetch_snapshot", lambda *args, **kwargs: fake)
    real_write = command._atomic_write
    writes = 0

    def fail_second_write(path, content):
        nonlocal writes
        writes += 1
        if writes == 2:
            raise OSError("simulated metadata failure")
        real_write(path, content)

    monkeypatch.setattr(command, "_atomic_write", fail_second_write)
    output_dir = tmp_path / "snapshots"
    result = command.main([
        "--registry", str(registry),
        "--source-id", "src_tz2_appointment",
        "--output-dir", str(output_dir),
        "--apply",
    ])
    capsys.readouterr()
    target = output_dir / "js_taizhou_003" / "src_tz2_appointment.html"
    assert result == 1
    assert not target.exists()
    assert not target.with_suffix(".html.metadata.json.part").exists()
