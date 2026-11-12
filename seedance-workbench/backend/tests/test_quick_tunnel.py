from pathlib import Path
import asyncio
import sys

from backend.quick_tunnel import QuickTunnel, TunnelError


def test_tunnel_discovers_https_url_from_stderr(tmp_path: Path):
    fake = tmp_path / "fake_cloudflared.py"
    fake.write_text(
        "import sys,time\n"
        "print('https://unit-test.trycloudflare.com', file=sys.stderr, flush=True)\n"
        "time.sleep(60)\n",
        encoding="utf-8",
    )
    async def exercise():
        tunnel = QuickTunnel([sys.executable, str(fake)], timeout=2)
        try:
            assert await tunnel.start(43210) == "https://unit-test.trycloudflare.com"
            assert tunnel.alive
        finally:
            await tunnel.stop()
        assert not tunnel.alive

    asyncio.run(exercise())


def test_tunnel_timeout_terminates_process(tmp_path):
    fake = tmp_path / "silent.py"
    fake.write_text("import time; time.sleep(60)", encoding="utf-8")
    async def exercise():
        tunnel = QuickTunnel([sys.executable, str(fake)], timeout=0.1)
        try:
            await tunnel.start(43210)
        except TunnelError as exc:
            assert "超时" in str(exc)
        else:
            raise AssertionError("expected TunnelError")
        assert not tunnel.alive

    asyncio.run(exercise())
