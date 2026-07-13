from __future__ import annotations

import importlib
import subprocess
import time
from pathlib import Path

from fastapi.testclient import TestClient

import app.config as config


def test_http_scan_settings_and_recovery(tmp_path: Path) -> None:
    base = tmp_path / "media"
    root = base / "视频库"
    root.mkdir(parents=True)
    video = root / "已有 MP4.mp4"
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i",
         "testsrc2=size=96x64:rate=8", "-t", "0.5", "-c:v", "libx264", str(video)],
        check=True,
    )
    config.BASE_MEDIA_DIR = base
    config.DATABASE_PATH = tmp_path / "config" / "api.db"
    main = importlib.import_module("app.main")
    with TestClient(main.app) as client:
        assert client.get("/health").json()["status"] == "ok"
        roots = client.get("/api/roots").json()["roots"]
        assert roots[0]["name"] == "视频库"
        settings = client.put("/api/settings", json={"schedule_time": "03:15", "auto_remux": False}).json()
        assert settings["schedule_time"] == "03:15"
        response = client.post("/api/scans", json={"root": str(root), "require_stable": False})
        assert response.status_code == 202
        job_id = response.json()["job_id"]
        for _ in range(100):
            job = client.get(f"/api/jobs/{job_id}").json()
            if job["state"] in {"completed", "failed"}:
                break
            time.sleep(0.05)
        assert job["state"] == "completed", job
        assert job["progress"]["stage_timings"]
        stats = client.get("/api/stats", params={"root": str(root)}).json()
        assert stats["categories"]["no_conversion"]["count"] == 1
        files = client.get("/api/files", params={"root": str(root)}).json()["items"]
        assert files[0]["integrity_status"] == "passed"
        preserved = root / "转换前原文件"
        preserved.mkdir()
        video.rename(preserved / video.name)
        second = client.post("/api/scans", json={"root": str(root), "require_stable": False}).json()["job_id"]
        for _ in range(100):
            second_job = client.get(f"/api/jobs/{second}").json()
            if second_job["state"] in {"completed", "failed"}:
                break
            time.sleep(0.05)
        assert second_job["state"] == "completed"
        assert client.get("/api/stats", params={"root": str(root)}).json()["total_count"] == 0
        temporary = root / ".orphan.nvc-deadbeef.tmp.mp4"
        temporary.write_bytes(b"preserved")
        recovery = client.get("/api/recovery", params={"root": str(root)}).json()
        assert recovery["items"][0]["path"] == str(temporary.resolve())
        assert temporary.read_bytes() == b"preserved"
