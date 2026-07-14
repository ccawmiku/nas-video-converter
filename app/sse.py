from __future__ import annotations

from .database import Database


def event_cursor(database: Database, after: int, last_event_id: str | None) -> int:
    cursor = max(after, int(last_event_id) if last_event_id and last_event_id.isdigit() else 0)
    if cursor:
        return cursor
    latest = database.one("SELECT COALESCE(MAX(id), 0) AS id FROM events")
    return int(latest["id"]) if latest else 0
