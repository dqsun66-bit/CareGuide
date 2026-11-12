from uuid import uuid4

from fastapi.testclient import TestClient

from backend.routes import create_app
from backend.video_probe import VideoMetadata


class FakeTunnel:
    def __init__(self):
        self.alive = False

    async def start(self, port):
        self.alive = True
        return "https://fake.trycloudflare.com"

    async def stop(self):
        self.alive = False


def _client(settings, monkeypatch):
    monkeypatch.setattr(
        "backend.video_uploads.probe_video",
        lambda path, filename, declared: VideoMetadata(5.0, "video/mp4", path.stat().st_size),
    )
    app = create_app(settings, tunnel_factory=lambda current: FakeTunnel())
    return TestClient(app)


def _upload(client):
    return client.post(
        "/api/seedance/video-uploads",
        files={
            "video": (
                "clip.mp4",
                b"\x00\x00\x00\x18ftypisom" + b"x" * 64,
                "video/mp4",
            )
        },
    )


def test_upload_returns_opaque_metadata_without_public_url(settings, monkeypatch):
    with _client(settings, monkeypatch) as client:
        response = _upload(client)
        assert response.status_code == 201, response.text
        data = response.json()["data"]
        assert data["filename"] == "clip.mp4"
        assert data["status"] == "ready"
        assert data["duration"] == 5.0
        assert "upload_id" in data
        assert "url" not in data
        assert "token" not in data
        assert "trycloudflare" not in response.text


def test_generation_accepts_local_upload_id(settings, monkeypatch):
    with _client(settings, monkeypatch) as client:
        upload_id = _upload(client).json()["data"]["upload_id"]
        response = client.post(
            "/api/seedance/generations",
            data={
                "mode": "edit",
                "prompt": "复刻视频1",
                "duration": "5",
                "resolution": "480p",
                "ratio": "9:16",
                "generate_audio": "false",
                "client_request_id": str(uuid4()),
                "video_upload_ids": upload_id,
            },
        )
        assert response.status_code == 201, response.text
        materials = response.json()["data"]["materials"]
        assert materials[0]["kind"] == "video"
        assert materials[0]["source"] == "local-upload"


def test_non_local_origin_cannot_upload(settings, monkeypatch):
    with _client(settings, monkeypatch) as client:
        response = client.post(
            "/api/seedance/video-uploads",
            headers={"Origin": "https://evil.example"},
            files={"video": ("clip.mp4", b"x", "video/mp4")},
        )
        assert response.status_code == 403
