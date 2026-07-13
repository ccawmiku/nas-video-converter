from __future__ import annotations

import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from app import media_scanner
from app.config import Settings
from app.database import Database
from app.ffmpeg_tools import ProcessControl


def _scan_item(path: Path, root: Path) -> dict:
    stat = path.stat()
    return {
        "path": str(path),
        "real_path": str(path),
        "relative_path": str(path.relative_to(root)),
        "size": stat.st_size,
        "mtime_ns": stat.st_mtime_ns,
        "category": "no_conversion",
        "reason": "test",
        "probe": {},
        "integrity_status": "passed",
        "integrity": {"status": "passed"},
        "stable": True,
        "elapsed_seconds": 0.0,
    }


def test_scan_pause_blocks_work_until_resume(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "media"
    root.mkdir()
    video = root / "暂停 测试.mp4"
    video.write_bytes(b"video")
    db = Database(tmp_path / "config" / "app.db")
    control = ProcessControl()
    entered = threading.Event()
    proceed = threading.Event()
    finished = threading.Event()

    def fake_inspect(db, received_root, path, settings, require_stable, received_control):
        entered.set()
        proceed.wait(2)
        media_scanner._control_checkpoint(received_control)
        finished.set()
        return _scan_item(path, received_root)

    monkeypatch.setattr(media_scanner, "inspect_file", fake_inspect)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(
            media_scanner.scan_root,
            db,
            root,
            "scan-paused",
            Settings(scan_concurrency=1),
            None,
            False,
            control,
        )
        assert entered.wait(2)
        control.pause()
        proceed.set()
        try:
            assert not finished.wait(0.2)
        finally:
            control.resume()
        assert future.result(timeout=2)["total_count"] == 1
    assert finished.is_set()


def test_scan_cancel_does_not_publish_partial_snapshot(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "media"
    root.mkdir()
    (root / "cancel.mp4").write_bytes(b"video")
    db = Database(tmp_path / "config" / "app.db")
    control = ProcessControl()
    entered = threading.Event()

    def fake_inspect(db, received_root, path, settings, require_stable, received_control):
        entered.set()
        while True:
            media_scanner._control_checkpoint(received_control)
            time.sleep(0.01)

    monkeypatch.setattr(media_scanner, "inspect_file", fake_inspect)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(
            media_scanner.scan_root,
            db,
            root,
            "scan-cancelled",
            Settings(scan_concurrency=1),
            None,
            False,
            control,
        )
        assert entered.wait(2)
        control.cancel()
        with pytest.raises(media_scanner.ScanCancelled):
            future.result(timeout=2)
    assert db.one("SELECT * FROM root_state WHERE root=?", (str(root),)) is None


def test_shared_process_control_terminates_all_children() -> None:
    control = ProcessControl()
    children = [
        subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
        for _ in range(2)
    ]
    try:
        for child in children:
            control.attach(child)
        control.cancel()
        for child in children:
            child.wait(timeout=5)
            assert child.returncode is not None
    finally:
        for child in children:
            if child.poll() is None:
                child.kill()
                child.wait(timeout=5)
            control.detach(child)
