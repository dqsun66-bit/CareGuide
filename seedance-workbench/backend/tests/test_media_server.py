import httpx

from backend.media_server import TemporaryMediaServer


def test_media_server_requires_token_and_supports_head_and_range(tmp_path):
    video = tmp_path / "clip.mp4"
    video.write_bytes(b"0123456789")
    server = TemporaryMediaServer()
    server.start()
    try:
        server.publish("secret-token", video, "video/mp4")
        base = f"http://127.0.0.1:{server.port}"
        assert httpx.get(f"{base}/media/wrong").status_code == 404
        head = httpx.head(f"{base}/media/secret-token")
        assert head.status_code == 200
        assert head.headers["content-length"] == "10"
        assert head.content == b""
        full = httpx.get(f"{base}/media/secret-token")
        assert full.status_code == 200
        assert full.content == b"0123456789"
        ranged = httpx.get(
            f"{base}/media/secret-token", headers={"Range": "bytes=2-5"}
        )
        assert ranged.status_code == 206
        assert ranged.content == b"2345"
        assert ranged.headers["content-range"] == "bytes 2-5/10"
        invalid = httpx.get(
            f"{base}/media/secret-token", headers={"Range": "bytes=20-30"}
        )
        assert invalid.status_code == 416
        assert invalid.headers["content-range"] == "bytes */10"
        assert httpx.post(f"{base}/media/secret-token").status_code == 405
        assert httpx.get(f"{base}/../clip.mp4").status_code == 404
    finally:
        server.stop()


def test_media_server_unpublish_and_idempotent_stop(tmp_path):
    video = tmp_path / "clip.mp4"
    video.write_bytes(b"abc")
    server = TemporaryMediaServer()
    server.start()
    server.publish("token", video, "video/mp4")
    server.unpublish("token")
    assert httpx.get(f"http://127.0.0.1:{server.port}/media/token").status_code == 404
    server.stop()
    server.stop()
