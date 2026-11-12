from pathlib import Path
from types import SimpleNamespace

import pytest

from backend.config import Settings
from backend.video_probe import VideoProbeError, probe_video


def _mp4(path: Path) -> Path:
    path.write_bytes(b"\x00\x00\x00\x18ftypisom" + b"x" * 64)
    return path


def test_video_upload_defaults(tmp_path):
    settings = Settings(data_dir=tmp_path)
    assert settings.max_video_bytes == 50 * 1024 * 1024
    assert settings.video_upload_ttl == 1800
    assert settings.video_post_task_grace == 300
    assert settings.tunnel_start_timeout == 30


def test_probe_accepts_mp4_with_video_stream(tmp_path, monkeypatch):
    video = _mp4(tmp_path / "dance.mp4")
    monkeypatch.setattr(
        "backend.video_probe.subprocess.run",
        lambda *args, **kwargs: SimpleNamespace(
            returncode=0,
            stdout='{"format":{"duration":"12.5"},"streams":[{"codec_type":"video"}]}',
            stderr="",
        ),
    )
    result = probe_video(video, "dance.mp4", "video/mp4")
    assert result.duration == 12.5
    assert result.content_type == "video/mp4"
    assert result.size == video.stat().st_size


@pytest.mark.parametrize("duration", [1.99, 15.01])
def test_probe_rejects_duration_outside_platform_limit(tmp_path, monkeypatch, duration):
    video = _mp4(tmp_path / "dance.mp4")
    monkeypatch.setattr(
        "backend.video_probe.subprocess.run",
        lambda *args, **kwargs: SimpleNamespace(
            returncode=0,
            stdout=f'{{"format":{{"duration":"{duration}"}},"streams":[{{"codec_type":"video"}}]}}',
            stderr="",
        ),
    )
    with pytest.raises(VideoProbeError, match="2～15"):
        probe_video(video, "dance.mp4", "video/mp4")


def test_probe_rejects_invalid_container_and_missing_video(tmp_path, monkeypatch):
    bad = tmp_path / "bad.mp4"
    bad.write_bytes(b"not-an-mp4")
    with pytest.raises(VideoProbeError, match="内容"):
        probe_video(bad, "bad.mp4", "video/mp4")

    video = _mp4(tmp_path / "audio-only.mp4")
    monkeypatch.setattr(
        "backend.video_probe.subprocess.run",
        lambda *args, **kwargs: SimpleNamespace(
            returncode=0,
            stdout='{"format":{"duration":"5"},"streams":[{"codec_type":"audio"}]}',
            stderr="",
        ),
    )
    with pytest.raises(VideoProbeError, match="没有视频流"):
        probe_video(video, "audio-only.mp4", "video/mp4")
