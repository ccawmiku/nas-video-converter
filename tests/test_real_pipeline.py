from __future__ import annotations

import json
import shutil
import subprocess
import time
from pathlib import Path

import pytest

from app.config import Settings
from app.database import Database
from app.media_scanner import scan_root
from app.task_manager import TaskManager


pytestmark = pytest.mark.skipif(not shutil.which("ffmpeg") or not shutil.which("ffprobe"), reason="需要 FFmpeg")


def ffmpeg(*args: str) -> None:
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", *args], check=True)


def wait_job(manager: TaskManager, job_id: str, timeout: float = 40) -> dict:
    end = time.time() + timeout
    while time.time() < end:
        job = manager.get(job_id)
        if job["state"] in {"completed", "failed", "cancelled", "interrupted"}:
            return job
        time.sleep(0.1)
    pytest.fail("job timeout")


def test_scan_plan_remux_without_overwrite(tmp_path: Path) -> None:
    root = tmp_path / "中文 媒体库"
    deep = root / "电影" / "深层 [测试]"
    deep.mkdir(parents=True)
    source = deep / "示例 #1.mkv"
    ffmpeg("-f", "lavfi", "-i", "testsrc2=size=160x90:rate=12", "-f", "lavfi", "-i", "sine=frequency=440", "-t", "1", "-c:v", "libx264", "-c:a", "aac", str(source))
    db = Database(tmp_path / "config" / "db.sqlite")
    settings = Settings(scan_concurrency=1)
    summary = scan_root(db, root, "scan-1", settings)
    assert summary["categories"]["remux"]["count"] == 1
    row = db.one("SELECT * FROM files WHERE path=?", (str(source.resolve()),))
    existing = source.with_suffix(".mp4")
    existing.write_bytes(b"do-not-overwrite")
    manager = TaskManager(db, lambda: settings)
    try:
        plan = manager.create_plan(root, {row["id"]: "remux"})
        job_id = manager.create_conversion(plan)
        job = wait_job(manager, job_id)
        assert job["state"] == "completed", job["error"]
        result = job["result"]["completed"][0]
        output = Path(result["output_path"])
        backup = Path(result["backup_path"])
        assert output.name == "示例 #1 (2).mp4"
        assert existing.read_bytes() == b"do-not-overwrite"
        assert backup.exists()
        assert "转换前原文件" in backup.parts
        assert not source.exists()
        subprocess.run(["ffmpeg", "-v", "error", "-i", str(output), "-frames:v", "2", "-f", "null", "-"], check=True)
    finally:
        manager.close()


def test_source_changed_after_plan_is_never_moved(tmp_path: Path) -> None:
    root = tmp_path / "media"
    root.mkdir()
    source = root / "changing.mkv"
    ffmpeg("-f", "lavfi", "-i", "testsrc2=size=96x64:rate=8", "-t", "0.5", "-c:v", "libx264", str(source))
    db = Database(tmp_path / "db.sqlite")
    settings = Settings(scan_concurrency=1)
    scan_root(db, root, "scan-change", settings)
    row = db.one("SELECT * FROM files WHERE path=?", (str(source.resolve()),))
    manager = TaskManager(db, lambda: settings)
    try:
        plan = manager.create_plan(root, {row["id"]: "remux"})
        with source.open("ab") as stream:
            stream.write(b"changed-after-confirmation")
        job = wait_job(manager, manager.create_conversion(plan))
        assert job["state"] == "failed"
        assert "计划确认后发生变化" in job["error"]
        assert source.exists()
        assert not (root / "转换前原文件").exists()
    finally:
        manager.close()
