from pathlib import Path

from app.database import Database
from app.sse import event_cursor


def test_initial_sse_connection_starts_after_historical_events(tmp_path: Path) -> None:
    database = Database(tmp_path / "events.db")
    historical_id = database.add_event("old-job", "job", {"id": "old-job", "state": "cancelled"})
    assert event_cursor(database, 0, None) == historical_id


def test_sse_reconnect_resumes_from_client_cursor(tmp_path: Path) -> None:
    database = Database(tmp_path / "events.db")
    database.add_event("old-job", "job", {"id": "old-job", "state": "cancelled"})
    assert event_cursor(database, 7, None) == 7
    assert event_cursor(database, 2, "9") == 9
