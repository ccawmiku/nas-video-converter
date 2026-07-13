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


def has_encoder(name: str) -> bool:
    completed = subprocess.run(["ffmpeg", "-hide_banner", "-encoders"], capture_output=True, text=True)
    return completed.returncode == 0 and name in completed.stdout


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


def test_vp9_opus_transcodes_to_h264_aac_and_preserves_source(tmp_path: Path) -> None:
    if not has_encoder("libvpx-vp9") or not has_encoder("libopus"):
        pytest.skip("FFmpeg 缺少 VP9/Opus 测试编码器")
    root = tmp_path / "media"
    root.mkdir()
    source = root / "vp9-opus.webm"
    ffmpeg(
        "-f", "lavfi", "-i", "testsrc2=size=96x64:rate=8",
        "-f", "lavfi", "-i", "sine=frequency=440",
        "-t", "1", "-c:v", "libvpx-vp9", "-deadline", "realtime", "-cpu-used", "8",
        "-c:a", "libopus", str(source),
    )
    db = Database(tmp_path / "db.sqlite")
    settings = Settings(scan_concurrency=1, hardware_acceleration="software")
    summary = scan_root(db, root, "scan-vp9-opus", settings)
    assert summary["categories"]["transcode"]["count"] == 1
    row = db.one("SELECT * FROM files WHERE path=?", (str(source.resolve()),))
    manager = TaskManager(db, lambda: settings)
    try:
        plan = manager.create_plan(root, {row["id"]: "transcode"})
        job = wait_job(manager, manager.create_conversion(plan))
        assert job["state"] == "completed", job["error"]
        result = job["result"]["completed"][0]
        output = Path(result["output_path"])
        backup = Path(result["backup_path"])
        media = json.loads(subprocess.check_output([
            "ffprobe", "-v", "error", "-show_streams", "-of", "json", str(output),
        ], text=True))
        codecs = {(stream["codec_type"], stream["codec_name"]) for stream in media["streams"]}
        assert ("video", "h264") in codecs
        assert ("audio", "aac") in codecs
        assert backup.exists()
        assert not source.exists()
        conversion = db.one("SELECT warning FROM conversions WHERE job_id=?", (job["id"],))
        assert "Opus 音频已转为 AAC" in conversion["warning"]
    finally:
        manager.close()
