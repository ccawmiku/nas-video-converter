from __future__ import annotations

import asyncio
import json
import threading
import time
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Literal

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import __version__
from .config import BASE_MEDIA_DIR, DATABASE_PATH, Settings
from .database import Database, utc_now
from .media_scanner import allowed_root, discover_roots
from .safety import SafetyError, ensure_safe_path, is_excluded
from .task_manager import TaskError, TaskManager


db = Database(DATABASE_PATH)
_settings_lock = threading.Lock()


def load_settings() -> Settings:
    with _settings_lock:
        row = db.one("SELECT value_json FROM settings WHERE id=1")
        settings = Settings()
        if row:
            settings.update(json.loads(row["value_json"]))
        return settings


def save_settings(values: dict[str, Any]) -> Settings:
    settings = load_settings().update(values)
    now = utc_now()
    db.execute(
        "INSERT INTO settings(id,value_json,updated_at) VALUES(1,?,?) "
        "ON CONFLICT(id) DO UPDATE SET value_json=excluded.value_json,updated_at=excluded.updated_at",
        (json.dumps(settings.as_dict(), ensure_ascii=False), now),
    )
    db.add_event(None, "settings", settings.as_dict())
    return settings


manager = TaskManager(db, load_settings)


class AutomationLoop:
    def __init__(self) -> None:
        self.stop_event = threading.Event()
        self.last_schedule_key = ""
        self.last_monitor_at = 0.0
        self.thread = threading.Thread(target=self.run, name="nvc-automation", daemon=True)
        self.thread.start()

    def run(self) -> None:
        while not self.stop_event.wait(1):
            try:
                settings = load_settings()
                now = datetime.now().astimezone()
                schedule_key = f"{now.date()} {settings.schedule_time}"
                if settings.schedule_enabled and now.strftime("%H:%M") == settings.schedule_time and schedule_key != self.last_schedule_key:
                    self.last_schedule_key = schedule_key
                    for item in discover_roots(BASE_MEDIA_DIR):
                        manager.create_scan(Path(item["path"]), require_stable=True, trigger="schedule")
                if settings.monitor_enabled and time.monotonic() - self.last_monitor_at >= settings.monitor_interval_seconds:
                    self.last_monitor_at = time.monotonic()
                    for item in discover_roots(BASE_MEDIA_DIR):
                        manager.create_scan(Path(item["path"]), require_stable=True, trigger="monitor")
            except Exception as exc:
                db.log("error", "automation", str(exc))

    def status(self) -> dict[str, Any]:
        settings = load_settings()
        now = datetime.now().astimezone()
        hour, minute = map(int, settings.schedule_time.split(":"))
        next_run = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if next_run <= now:
            next_run += timedelta(days=1)
        return {
            "schedule_enabled": settings.schedule_enabled,
            "monitor_enabled": settings.monitor_enabled,
            "next_schedule_at": next_run.isoformat() if settings.schedule_enabled else None,
            "countdown_seconds": max(0, int((next_run - now).total_seconds())) if settings.schedule_enabled else None,
            "monitor_interval_seconds": settings.monitor_interval_seconds,
            "stable_seconds": settings.stable_seconds,
        }

    def close(self) -> None:
        self.stop_event.set()


automation: AutomationLoop | None = None


@asynccontextmanager
async def lifespan(_: FastAPI):
    global automation
    automation = AutomationLoop()
    yield
    automation.close()
    manager.close()


app = FastAPI(title="NAS Video Converter", version=__version__, lifespan=lifespan)
STATIC_DIR = Path(__file__).parent / "static"
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Cache-Control"] = "no-store" if request.url.path.startswith("/api/") else "no-cache"
    return response


@app.exception_handler(SafetyError)
async def safety_error(_: Request, exc: SafetyError):
    return JSONResponse(status_code=400, content={"detail": str(exc), "type": "safety_error"})


@app.exception_handler(TaskError)
async def task_error(_: Request, exc: TaskError):
    return JSONResponse(status_code=409, content={"detail": str(exc), "type": "task_error"})


@app.get("/", include_in_schema=False)
def index():
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/health")
def health():
    return {"status": "ok", "version": __version__, "database": str(DATABASE_PATH)}


@app.get("/api/roots")
def roots():
    return {"roots": discover_roots(BASE_MEDIA_DIR)}


@app.get("/api/settings")
def get_settings():
    return load_settings().as_dict()


@app.put("/api/settings")
def put_settings(values: dict[str, Any]):
    try:
        return save_settings(values).as_dict()
    except (ValueError, TypeError) as exc:
        raise HTTPException(422, str(exc)) from exc


class ScanRequest(BaseModel):
    root: str
    require_stable: bool = False


@app.post("/api/scans", status_code=202)
def create_scan(body: ScanRequest):
    root = allowed_root(body.root, BASE_MEDIA_DIR)
    return {"job_id": manager.create_scan(root, require_stable=body.require_stable), "state": "queued"}


@app.get("/api/files")
def files(
    root: str,
    category: str | None = None,
    q: str | None = None,
    integrity: str | None = None,
    sort: Literal["relative_path", "size", "category", "updated_at"] = "relative_path",
    order: Literal["asc", "desc"] = "asc",
    page: int = Query(1, ge=1),
    page_size: int = Query(100, ge=1, le=500),
):
    safe_root = allowed_root(root, BASE_MEDIA_DIR)
    clauses = ["root=?", "scan_id=(SELECT last_scan_id FROM root_state WHERE root=?)"]
    params: list[Any] = [str(safe_root), str(safe_root)]
    if category:
        clauses.append("category=?")
        params.append(category)
    if q:
        clauses.append("relative_path LIKE ? ESCAPE '\\'")
        escaped = q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        params.append(f"%{escaped}%")
    if integrity:
        clauses.append("integrity_status=?")
        params.append(integrity)
    where = " AND ".join(clauses)
    total = db.one(f"SELECT COUNT(*) AS count FROM files WHERE {where}", tuple(params))["count"]
    rows = db.query(
        f"SELECT * FROM files WHERE {where} ORDER BY {sort} {order.upper()} LIMIT ? OFFSET ?",
        (*params, page_size, (page - 1) * page_size),
    )
    for row in rows:
        row["probe"] = json.loads(row.pop("probe_json"))
        row["integrity"] = json.loads(row.pop("integrity_json"))
    return {"items": rows, "total": total, "page": page, "page_size": page_size}


@app.get("/api/stats")
def stats(root: str):
    safe_root = allowed_root(root, BASE_MEDIA_DIR)
    rows = db.query(
        "SELECT category,COUNT(*) AS count,COALESCE(SUM(size),0) AS bytes FROM files "
        "WHERE root=? AND scan_id=(SELECT last_scan_id FROM root_state WHERE root=?) GROUP BY category",
        (str(safe_root), str(safe_root)),
    )
    categories = {name: {"count": 0, "bytes": 0} for name in ("no_conversion", "remux", "transcode", "unsupported", "skipped")}
    for row in rows:
        categories[row["category"]] = {"count": row["count"], "bytes": row["bytes"]}
    return {
        "total_count": sum(item["count"] for item in categories.values()),
        "total_bytes": sum(item["bytes"] for item in categories.values()),
        "categories": categories,
    }


class PlanAction(BaseModel):
    file_id: int
    action: Literal["remux", "transcode"]


class PlanRequest(BaseModel):
    root: str
    actions: list[PlanAction] = Field(min_length=1)


@app.post("/api/plans", status_code=201)
def create_plan(body: PlanRequest):
    root = allowed_root(body.root, BASE_MEDIA_DIR)
    actions = {item.file_id: item.action for item in body.actions}
    plan_id = manager.create_plan(root, actions)
    return {"plan_id": plan_id, "state": "confirmed", "count": len(actions)}


class ConversionRequest(BaseModel):
    plan_id: str
    profile: Literal["quality", "recommended", "space"] = "recommended"


@app.post("/api/conversions", status_code=202)
def create_conversion(body: ConversionRequest):
    return {"job_id": manager.create_conversion(body.plan_id, body.profile), "state": "queued"}


class VerifyRequest(BaseModel):
    file_id: int
    full: bool = True


@app.post("/api/verifications", status_code=202)
def create_verification(body: VerifyRequest):
    return {"job_id": manager.create_verify(body.file_id, body.full), "state": "queued"}


class RecoveryVerifyRequest(BaseModel):
    root: str
    path: str
    full: bool = False


@app.post("/api/recovery/verifications", status_code=202)
def create_recovery_verification(body: RecoveryVerifyRequest):
    root = allowed_root(body.root, BASE_MEDIA_DIR)
    path = ensure_safe_path(root, Path(body.path))
    return {"job_id": manager.create_recovery_verify(root, path, body.full), "state": "queued"}


@app.get("/api/jobs")
def jobs(limit: int = Query(100, ge=1, le=500)):
    return {"items": manager.list(limit)}


@app.get("/api/jobs/{job_id}")
def job(job_id: str):
    value = manager.get(job_id)
    if not value:
        raise HTTPException(404, "任务不存在")
    return value


@app.post("/api/jobs/{job_id}/pause", status_code=202)
def pause_job(job_id: str):
    manager.pause(job_id)
    return {"id": job_id, "state": "paused"}


@app.post("/api/jobs/{job_id}/resume", status_code=202)
def resume_job(job_id: str):
    manager.resume(job_id)
    return {"id": job_id, "state": "running"}


@app.post("/api/jobs/{job_id}/cancel", status_code=202)
def cancel_job(job_id: str):
    manager.cancel(job_id)
    return {"id": job_id, "state": "cancelling"}


@app.get("/api/events")
async def events(request: Request, after: int = 0):
    header = request.headers.get("last-event-id")
    cursor = max(after, int(header) if header and header.isdigit() else 0)
    async def generate():
        nonlocal cursor
        while True:
            if await request.is_disconnected():
                return
            rows = db.query("SELECT * FROM events WHERE id>? ORDER BY id LIMIT 100", (cursor,))
            if rows:
                for row in rows:
                    cursor = row["id"]
                    payload = {"job_id": row["job_id"], **json.loads(row["data_json"])}
                    yield f"id: {cursor}\nevent: {row['event']}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"
            else:
                yield ": keepalive\n\n"
            await asyncio.sleep(1)
    return StreamingResponse(generate(), media_type="text/event-stream", headers={"X-Accel-Buffering": "no"})


@app.get("/api/conversions")
def conversions(limit: int = Query(100, ge=1, le=500)):
    rows = db.query("SELECT * FROM conversions ORDER BY created_at DESC LIMIT ?", (limit,))
    for row in rows:
        row["source_probe"] = json.loads(row.pop("source_probe_json"))
        row["output_probe"] = json.loads(row.pop("output_probe_json"))
    return {"items": rows}


@app.get("/api/logs")
def logs(limit: int = Query(200, ge=1, le=1000)):
    rows = db.query("SELECT * FROM logs ORDER BY id DESC LIMIT ?", (limit,))
    for row in rows:
        row["details"] = json.loads(row.pop("details_json"))
    return {"items": rows}


@app.get("/api/automation")
def automation_status():
    return automation.status() if automation else {"schedule_enabled": False, "monitor_enabled": False}


@app.get("/api/recovery")
def recovery(root: str):
    safe_root = allowed_root(root, BASE_MEDIA_DIR)
    items = []
    for path in safe_root.rglob("*.nvc-*.tmp.mp4"):
        try:
            safe = ensure_safe_path(safe_root, path)
            relative = safe.relative_to(safe_root)
            if not is_excluded(relative):
                items.append({"path": str(safe), "relative_path": str(relative), "size": safe.stat().st_size})
        except (SafetyError, OSError):
            continue
    return {"items": items, "message": "服务不会自动移动、覆盖或删除中断产生的临时文件；请人工核对后重新扫描。"}
