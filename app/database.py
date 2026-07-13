from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Database:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._init_lock = threading.Lock()
        self.initialize()

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=30000")
        try:
            yield conn
        finally:
            conn.close()

    def initialize(self) -> None:
        with self._init_lock, self.connect() as conn:
            conn.executescript(
                """
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS settings (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    value_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS files (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    root TEXT NOT NULL,
                    path TEXT NOT NULL UNIQUE,
                    real_path TEXT NOT NULL,
                    relative_path TEXT NOT NULL,
                    size INTEGER NOT NULL,
                    mtime_ns INTEGER NOT NULL,
                    category TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    probe_json TEXT NOT NULL,
                    integrity_status TEXT NOT NULL DEFAULT 'pending',
                    integrity_json TEXT NOT NULL DEFAULT '{}',
                    stable INTEGER NOT NULL DEFAULT 1,
                    scan_id TEXT,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_files_root_category ON files(root, category);
                CREATE TABLE IF NOT EXISTS root_state (
                    root TEXT PRIMARY KEY,
                    last_scan_id TEXT NOT NULL,
                    summary_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS jobs (
                    id TEXT PRIMARY KEY,
                    type TEXT NOT NULL,
                    state TEXT NOT NULL,
                    root TEXT,
                    file_ids_json TEXT NOT NULL DEFAULT '[]',
                    options_json TEXT NOT NULL DEFAULT '{}',
                    progress_json TEXT NOT NULL DEFAULT '{}',
                    result_json TEXT NOT NULL DEFAULT '{}',
                    error TEXT,
                    created_at TEXT NOT NULL,
                    started_at TEXT,
                    completed_at TEXT,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_jobs_state_created ON jobs(state, created_at);
                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    job_id TEXT,
                    event TEXT NOT NULL,
                    data_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    level TEXT NOT NULL,
                    scope TEXT NOT NULL,
                    message TEXT NOT NULL,
                    details_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS stability (
                    path TEXT PRIMARY KEY,
                    size INTEGER NOT NULL,
                    mtime_ns INTEGER NOT NULL,
                    first_seen_unchanged_at TEXT NOT NULL,
                    last_seen_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS conversions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    job_id TEXT NOT NULL,
                    file_id INTEGER NOT NULL,
                    source_path TEXT NOT NULL,
                    output_path TEXT,
                    backup_path TEXT,
                    source_probe_json TEXT NOT NULL,
                    output_probe_json TEXT NOT NULL DEFAULT '{}',
                    source_size INTEGER NOT NULL,
                    output_size INTEGER,
                    warning TEXT,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    completed_at TEXT
                );
                """
            )
            conn.execute(
                "UPDATE jobs SET state='interrupted', error=COALESCE(error, '容器或服务在任务运行时中断'), "
                "completed_at=?, updated_at=? WHERE state IN ('running','pausing','paused','cancelling')",
                (utc_now(), utc_now()),
            )

    def execute(self, sql: str, params: tuple[Any, ...] = ()) -> int:
        with self.connect() as conn:
            cur = conn.execute(sql, params)
            return int(cur.lastrowid or 0)

    def query(self, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
        with self.connect() as conn:
            return [dict(row) for row in conn.execute(sql, params).fetchall()]

    def one(self, sql: str, params: tuple[Any, ...] = ()) -> dict[str, Any] | None:
        rows = self.query(sql, params)
        return rows[0] if rows else None

    def add_event(self, job_id: str | None, event: str, data: dict[str, Any]) -> int:
        return self.execute(
            "INSERT INTO events(job_id,event,data_json,created_at) VALUES(?,?,?,?)",
            (job_id, event, json.dumps(data, ensure_ascii=False), utc_now()),
        )

    def log(self, level: str, scope: str, message: str, details: dict[str, Any] | None = None) -> None:
        self.execute(
            "INSERT INTO logs(level,scope,message,details_json,created_at) VALUES(?,?,?,?,?)",
            (level, scope, message, json.dumps(details or {}, ensure_ascii=False), utc_now()),
        )
