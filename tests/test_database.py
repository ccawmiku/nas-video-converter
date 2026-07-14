import json
import sqlite3
from pathlib import Path

from app.database import Database, utc_now
from app.media_scanner import reclassify_stored_files


def test_restart_marks_running_job_interrupted(tmp_path: Path) -> None:
    path = tmp_path / "config" / "db.sqlite"
    db = Database(path)
    now = utc_now()
    db.execute(
        "INSERT INTO jobs(id,type,state,file_ids_json,options_json,progress_json,result_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
        ("running-job", "scan", "running", "[]", "{}", "{}", "{}", now, now),
    )
    Database(path)
    row = db.one("SELECT state,error FROM jobs WHERE id='running-job'")
    assert row["state"] == "interrupted"
    assert "中断" in row["error"]


def test_settings_and_events_survive_reopen(tmp_path: Path) -> None:
    path = tmp_path / "db.sqlite"
    db = Database(path)
    event_id = db.add_event(None, "settings", {"auto_remux": False})
    reopened = Database(path)
    assert reopened.one("SELECT id FROM events WHERE id=?", (event_id,))


def test_existing_database_adds_conversion_backend_column(tmp_path: Path) -> None:
    path = tmp_path / "legacy.db"
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE conversions (id INTEGER PRIMARY KEY AUTOINCREMENT)")
    Database(path)
    with sqlite3.connect(path) as conn:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(conversions)")}
    assert "backend" in columns


def test_startup_reclassifies_legacy_opus_result(tmp_path: Path) -> None:
    db = Database(tmp_path / "db.sqlite")
    now = utc_now()
    probe = {
        "format": {"format_name": "matroska,webm"},
        "streams": [
            {"index": 0, "codec_type": "video", "codec_name": "vp9", "pix_fmt": "yuv420p", "disposition": {}},
            {"index": 1, "codec_type": "audio", "codec_name": "opus", "channels": 2, "disposition": {}},
        ],
        "chapters": [],
    }
    db.execute(
        "INSERT INTO files(root,path,real_path,relative_path,size,mtime_ns,category,reason,probe_json,"
        "integrity_status,integrity_json,stable,scan_id,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        ("/media/test", "/media/test/old.webm", "/media/test/old.webm", "old.webm", 1, 1,
         "unsupported", "轨道 1：音频 opus 不能按规则原样写入 MP4",
         json.dumps({"raw": probe}), "passed", "{}", 1, "old-scan", now),
    )
    assert reclassify_stored_files(db) == 1
    row = db.one("SELECT category,reason FROM files WHERE path='/media/test/old.webm'")
    assert row["category"] == "transcode"
    assert "Opus 音频转为 AAC" in row["reason"]
