from pathlib import Path

from app.database import Database, utc_now


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

