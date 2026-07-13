from __future__ import annotations

import json
import queue
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import psutil

from .config import PROFILE_CRF, Settings
from .database import Database, utc_now
from .ffmpeg_tools import (
    MediaToolError,
    ProcessControl,
    conversion_args,
    duration_seconds,
    full_verify,
    probe_media,
    run_ffmpeg,
    sample_verify,
    summarize_probe,
    transcode_backend,
)
from .media_scanner import scan_root
from .safety import (
    SafetyError,
    ensure_safe_path,
    ensure_safe_root,
    safe_rename,
    unique_output_path,
    unique_preserved_path,
    unique_temporary_path,
)


class TaskError(RuntimeError):
    pass


class TaskManager:
    def __init__(self, db: Database, settings_getter):
        self.db = db
        self.settings_getter = settings_getter
        self.pending: queue.Queue[str] = queue.Queue()
        self.controls: dict[str, ProcessControl] = {}
        self._stop = threading.Event()
        self._gate = threading.Condition()
        self._active_conversions = 0
        self._active_verifications = 0
        self._threads = [threading.Thread(target=self._worker, name=f"nvc-task-worker-{i}", daemon=True) for i in range(16)]
        for thread in self._threads:
            thread.start()
        for row in db.query("SELECT id FROM jobs WHERE state='queued' ORDER BY created_at"):
            self.pending.put(row["id"])

    def close(self) -> None:
        self._stop.set()
        with self._gate:
            self._gate.notify_all()

    def _acquire_slot(self, job_type: str) -> None:
        if job_type not in {"convert", "verify"}:
            return
        with self._gate:
            while not self._stop.is_set():
                settings: Settings = self.settings_getter()
                if job_type == "convert" and self._active_verifications == 0 and self._active_conversions < settings.process_concurrency:
                    self._active_conversions += 1
                    return
                if job_type == "verify" and self._active_conversions == 0 and self._active_verifications < settings.verify_concurrency:
                    self._active_verifications += 1
                    return
                self._gate.wait(timeout=0.5)
        raise TaskError("任务管理器正在停止")

    def _release_slot(self, job_type: str) -> None:
        if job_type not in {"convert", "verify"}:
            return
        with self._gate:
            if job_type == "convert":
                self._active_conversions -= 1
            else:
                self._active_verifications -= 1
            self._gate.notify_all()

    def _new_job(self, job_type: str, root: str | None, file_ids: list[int], options: dict[str, Any], state: str = "queued") -> str:
        settings: Settings = self.settings_getter()
        queued_count = self.db.one("SELECT COUNT(*) AS count FROM jobs WHERE state='queued'")["count"]
        if state == "queued" and queued_count >= settings.queue_limit:
            raise TaskError("任务队列已达到上限")
        job_id = uuid.uuid4().hex
        now = utc_now()
        self.db.execute(
            "INSERT INTO jobs(id,type,state,root,file_ids_json,options_json,progress_json,result_json,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?)",
            (job_id, job_type, state, root, json.dumps(file_ids), json.dumps(options, ensure_ascii=False), "{}", "{}", now, now),
        )
        self.db.add_event(job_id, "job", {"id": job_id, "type": job_type, "state": state})
        if state == "queued":
            self.pending.put(job_id)
        return job_id

    def create_scan(self, root: Path, *, require_stable: bool = False, trigger: str = "manual") -> str:
        return self._new_job("scan", str(root), [], {"require_stable": require_stable, "trigger": trigger})

    def create_plan(self, root: Path, actions: dict[int, str]) -> str:
        if not actions:
            raise TaskError("请至少选择一个文件")
        rows = self.db.query(
            f"SELECT id,root,category,size,mtime_ns,path FROM files WHERE id IN ({','.join('?' for _ in actions)})",
            tuple(actions),
        )
        if len(rows) != len(actions):
            raise TaskError("计划包含不存在的文件")
        active = self.db.one("SELECT last_scan_id FROM root_state WHERE root=?", (str(root),))
        if not active:
            raise TaskError("必须先完成全量扫描")
        newer_scan = self.db.one(
            "SELECT id FROM jobs WHERE root=? AND type='scan' AND state IN ('queued','running','paused','cancelling') AND id<>? LIMIT 1",
            (str(root), active["last_scan_id"]),
        )
        if newer_scan:
            raise TaskError("新的全量扫描尚未完成，当前不能确认旧计划")
        snapshots: dict[str, Any] = {}
        for row in rows:
            if row["root"] != str(root):
                raise TaskError("计划中的文件不属于所选根目录")
            current = self.db.one("SELECT scan_id FROM files WHERE id=?", (row["id"],))
            if current["scan_id"] != active["last_scan_id"]:
                raise TaskError("计划包含非当前完整扫描快照中的文件")
            requested = actions[row["id"]]
            if requested not in {"remux", "transcode"}:
                raise TaskError("计划只允许无损换封装或 H.264 转码")
            if requested == "remux" and row["category"] != "remux":
                raise TaskError(f"文件 {row['path']} 不符合无损换封装规则")
            if requested == "transcode" and row["category"] != "transcode":
                raise TaskError(f"文件 {row['path']} 不符合转码规则")
            snapshots[str(row["id"])] = {"action": requested, "size": row["size"], "mtime_ns": row["mtime_ns"]}
        return self._new_job("plan", str(root), list(actions), {"snapshots": snapshots}, state="confirmed")

    def create_conversion(self, plan_id: str, profile: str = "recommended") -> str:
        if profile not in PROFILE_CRF:
            raise TaskError("未知转码档位")
        plan = self.db.one("SELECT * FROM jobs WHERE id=? AND type='plan' AND state='confirmed'", (plan_id,))
        if not plan:
            raise TaskError("必须先创建并确认处理计划")
        existing = self.db.one("SELECT id FROM jobs WHERE type='convert' AND json_extract(options_json,'$.plan_id')=?", (plan_id,))
        if existing:
            raise TaskError("该计划已经创建过执行任务，禁止重复转换")
        ids = json.loads(plan["file_ids_json"])
        snapshots = json.loads(plan["options_json"])["snapshots"]
        return self._new_job("convert", plan["root"], ids, {"plan_id": plan_id, "profile": profile, "snapshots": snapshots})

    def create_verify(self, file_id: int, full: bool = True) -> str:
        row = self.db.one("SELECT root FROM files WHERE id=?", (file_id,))
        if not row:
            raise TaskError("文件不存在")
        return self._new_job("verify", row["root"], [file_id], {"full": full})

    def create_recovery_verify(self, root: Path, path: Path, full: bool = False) -> str:
        root = ensure_safe_root(root)
        path = ensure_safe_path(root, path)
        if ".nvc-" not in path.name or not path.name.endswith(".tmp.mp4"):
            raise TaskError("恢复验证只接受服务遗留的隐藏临时 MP4")
        return self._new_job("verify", str(root), [], {"full": full, "path": str(path), "recovery": True})

    def get(self, job_id: str) -> dict[str, Any] | None:
        row = self.db.one("SELECT * FROM jobs WHERE id=?", (job_id,))
        if not row:
            return None
        for key in ("file_ids_json", "options_json", "progress_json", "result_json"):
            row[key[:-5] if key.endswith("_json") else key] = json.loads(row.pop(key) or "{}")
        return row

    def list(self, limit: int = 100) -> list[dict[str, Any]]:
        return [self.get(row["id"]) for row in self.db.query("SELECT id FROM jobs ORDER BY created_at DESC LIMIT ?", (limit,))]

    def _set_state(self, job_id: str, state: str, *, error: str | None = None, result: dict[str, Any] | None = None) -> None:
        now = utc_now()
        completed = now if state in {"completed", "failed", "cancelled", "interrupted"} else None
        if completed:
            row = self.db.one("SELECT progress_json FROM jobs WHERE id=?", (job_id,))
            progress = json.loads(row["progress_json"] or "{}") if row else {}
            stage = progress.get("stage")
            stage_started = progress.get("stage_started_at")
            if stage and stage_started:
                try:
                    seconds = max(0.0, datetime.fromisoformat(now).timestamp() - datetime.fromisoformat(stage_started).timestamp())
                    timings = progress.setdefault("stage_timings", {})
                    timings[stage] = timings.get(stage, 0) + seconds
                    progress["stage_elapsed_seconds"] = seconds
                    self.db.execute("UPDATE jobs SET progress_json=? WHERE id=?", (json.dumps(progress, ensure_ascii=False), job_id))
                except ValueError:
                    pass
        self.db.execute(
            "UPDATE jobs SET state=?,error=?,result_json=COALESCE(?,result_json),completed_at=COALESCE(?,completed_at),updated_at=? WHERE id=?",
            (state, error, json.dumps(result, ensure_ascii=False) if result is not None else None, completed, now, job_id),
        )
        self.db.add_event(job_id, "job", {"id": job_id, "state": state, "error": error, "result": result or {}})

    def _progress(self, job_id: str, data: dict[str, Any], started: float) -> None:
        data = dict(data)
        prior_row = self.db.one("SELECT progress_json FROM jobs WHERE id=?", (job_id,))
        prior = json.loads(prior_row["progress_json"] or "{}") if prior_row else {}
        now_iso = utc_now()
        stage = data.get("stage") or prior.get("stage")
        prior_stage = prior.get("stage")
        timings = dict(prior.get("stage_timings") or {})
        if stage != prior_stage:
            prior_started = prior.get("stage_started_at")
            if prior_stage and prior_started:
                try:
                    spent = max(0.0, datetime.fromisoformat(now_iso).timestamp() - datetime.fromisoformat(prior_started).timestamp())
                    timings[prior_stage] = timings.get(prior_stage, 0) + spent
                except ValueError:
                    pass
            stage_started_at = now_iso
        else:
            stage_started_at = prior.get("stage_started_at") or now_iso
        elapsed = max(0.0, time.monotonic() - started)
        percent = float(data.get("percent") or 0)
        data["elapsed_seconds"] = elapsed
        data["started_at"] = datetime.fromtimestamp(time.time() - elapsed, timezone.utc).isoformat()
        data["eta_seconds"] = elapsed * (100 - percent) / percent if 0 < percent < 100 else 0
        data["stage"] = stage
        data["stage_started_at"] = stage_started_at
        data["stage_timings"] = timings
        try:
            data["stage_elapsed_seconds"] = max(0.0, datetime.fromisoformat(now_iso).timestamp() - datetime.fromisoformat(stage_started_at).timestamp())
        except ValueError:
            data["stage_elapsed_seconds"] = 0
        data["updated_at"] = now_iso
        self.db.execute("UPDATE jobs SET progress_json=?,updated_at=? WHERE id=?", (json.dumps(data, ensure_ascii=False), now_iso, job_id))
        self.db.add_event(job_id, "progress", data)

    def pause(self, job_id: str) -> None:
        job = self.get(job_id)
        if not job or job["state"] != "running":
            raise TaskError("只有运行中的任务可以暂停")
        control = self.controls.get(job_id)
        if not control:
            raise TaskError("任务当前没有可暂停的执行进程")
        control.pause()
        self._set_state(job_id, "paused")

    def resume(self, job_id: str) -> None:
        job = self.get(job_id)
        if not job or job["state"] != "paused":
            raise TaskError("只有已暂停的任务可以继续")
        control = self.controls.get(job_id)
        if not control:
            raise TaskError("任务执行上下文已不存在")
        control.resume()
        self._set_state(job_id, "running")

    def cancel(self, job_id: str) -> None:
        job = self.get(job_id)
        if not job or job["state"] not in {"queued", "running", "paused"}:
            raise TaskError("任务当前不能取消")
        control = self.controls.get(job_id)
        if control:
            control.resume()
            control.cancel()
            self._set_state(job_id, "cancelling")
        else:
            self._set_state(job_id, "cancelled")

    def _worker(self) -> None:
        while not self._stop.is_set():
            try:
                job_id = self.pending.get(timeout=0.5)
            except queue.Empty:
                continue
            job = self.get(job_id)
            if not job or job["state"] != "queued":
                continue
            started = time.monotonic()
            control = ProcessControl()
            self.controls[job_id] = control
            self.db.execute("UPDATE jobs SET state='running',started_at=?,updated_at=? WHERE id=?", (utc_now(), utc_now(), job_id))
            self.db.add_event(job_id, "job", {"id": job_id, "state": "running"})
            slot_acquired = False
            try:
                self._acquire_slot(job["type"])
                slot_acquired = True
                if job["type"] == "scan":
                    result = self._run_scan(job, control, started)
                elif job["type"] == "convert":
                    result = self._run_conversion(job, control, started)
                elif job["type"] == "verify":
                    result = self._run_verify(job, control, started)
                else:
                    raise TaskError("未知任务类型")
                if control.cancelled.is_set():
                    self._set_state(job_id, "cancelled", result=result)
                else:
                    self._set_state(job_id, "completed", result=result)
            except Exception as exc:
                state = "cancelled" if control.cancelled.is_set() else "failed"
                self.db.log("error", job["type"], str(exc), {"job_id": job_id})
                self._set_state(job_id, state, error=str(exc))
            finally:
                if slot_acquired:
                    self._release_slot(job["type"])
                self.controls.pop(job_id, None)
                self.pending.task_done()

    def _run_scan(self, job: dict[str, Any], control: ProcessControl, started: float) -> dict[str, Any]:
        root = ensure_safe_root(Path(job["root"]))
        settings = self.settings_getter()
        result = scan_root(
            self.db, root, job["id"], settings,
            progress=lambda data: self._progress(job["id"], data, started),
            require_stable=bool(job["options"].get("require_stable")),
            control=control,
        )
        if job["options"].get("trigger") != "manual" and (settings.auto_remux or settings.auto_transcode):
            rows = self.db.query("SELECT id,category FROM files WHERE root=? AND scan_id=?", (str(root), job["id"]))
            actions: dict[int, str] = {}
            for row in rows:
                if row["category"] == "remux" and settings.auto_remux:
                    actions[row["id"]] = "remux"
                elif row["category"] == "transcode" and settings.auto_transcode:
                    actions[row["id"]] = "transcode"
            if actions:
                plan_id = self.create_plan(root, actions)
                result["auto_plan_id"] = plan_id
                result["auto_job_id"] = self.create_conversion(plan_id, settings.transcode_profile)
        return result

    def _guard_resources(self, settings: Settings) -> None:
        if psutil.virtual_memory().percent >= settings.memory_guard_percent:
            raise TaskError(f"内存使用达到保护水位 {settings.memory_guard_percent}%")

    def _run_verify(self, job: dict[str, Any], control: ProcessControl, started: float) -> dict[str, Any]:
        row = self.db.one("SELECT * FROM files WHERE id=?", (job["file_ids"][0],)) if job["file_ids"] else None
        if not row and not job["options"].get("recovery"):
            raise TaskError("待检测文件不存在")
        root = ensure_safe_root(Path(row["root"] if row else job["root"]))
        path = ensure_safe_path(root, Path(row["path"] if row else job["options"]["path"]))
        relative_path = row["relative_path"] if row else str(path.relative_to(root))
        before = path.stat()
        try:
            probe = probe_media(path)
            callback = lambda data: self._progress(job["id"], {"stage": "完整解码", "current_file": relative_path, **data}, started)
            settings = self.settings_getter()
            integrity = full_verify(path, probe, control, callback, settings) if job["options"].get("full", True) else sample_verify(path, probe, control)
            after = path.stat()
            if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
                raise TaskError("完整性检测期间文件发生变化")
        except Exception as exc:
            if row:
                self.db.execute(
                    "UPDATE files SET integrity_status='failed',integrity_json=?,updated_at=? WHERE id=?",
                    (json.dumps({"status": "failed", "error": str(exc)}, ensure_ascii=False), utc_now(), row["id"]),
                )
            raise
        if row:
            self.db.execute(
                "UPDATE files SET integrity_status='passed',integrity_json=?,updated_at=? WHERE id=?",
                (json.dumps(integrity, ensure_ascii=False), utc_now(), row["id"]),
            )
        return {"file_id": row["id"] if row else None, "path": str(path), "recovery": not bool(row), "integrity": integrity}

    def _move_failed_output(self, root: Path, source: Path, temporary: Path) -> str | None:
        if not temporary.exists():
            return None
        relative_parent = source.parent.relative_to(root)
        failed_root = root / "转换失败输出"
        proposed = failed_root / relative_parent / f"{source.stem}.failed.mp4"
        target = unique_preserved_path(root, failed_root, proposed.relative_to(failed_root))
        safe_rename(root, temporary, target)
        return str(target)

    def _run_conversion(self, job: dict[str, Any], control: ProcessControl, started: float) -> dict[str, Any]:
        root = ensure_safe_root(Path(job["root"]))
        settings = self.settings_getter()
        profile = job["options"]["profile"]
        snapshots = job["options"]["snapshots"]
        selected_backend = (
            transcode_backend(settings)
            if any(snapshot["action"] == "transcode" for snapshot in snapshots.values())
            else "stream_copy"
        )
        rows = self.db.query(
            f"SELECT * FROM files WHERE id IN ({','.join('?' for _ in job['file_ids'])}) ORDER BY relative_path",
            tuple(job["file_ids"]),
        )
        completed: list[dict[str, Any]] = []
        total_bytes = sum(row["size"] for row in rows)
        processed_bytes = 0
        for index, row in enumerate(rows):
            self._guard_resources(settings)
            if control.cancelled.is_set():
                break
            while control.paused.is_set() and not control.cancelled.is_set():
                time.sleep(0.2)
            source = ensure_safe_path(root, Path(row["path"]))
            snapshot = snapshots[str(row["id"])]
            action = snapshot["action"]
            current_backend = "stream_copy" if action == "remux" else selected_backend
            stat_before = source.stat()
            if (stat_before.st_size, stat_before.st_mtime_ns) != (snapshot["size"], snapshot["mtime_ns"]):
                raise TaskError(f"源文件在计划确认后发生变化：{row['relative_path']}")
            source_probe = probe_media(source)
            target = unique_output_path(root, source)
            temporary = unique_temporary_path(root, source)
            output_probe: dict[str, Any] = {}
            backup: Path | None = None
            conversion_id = self.db.execute(
                "INSERT INTO conversions(job_id,file_id,source_path,source_probe_json,source_size,backend,status,created_at) VALUES(?,?,?,?,?,?,?,?)",
                (job["id"], row["id"], str(source), json.dumps(summarize_probe(source_probe), ensure_ascii=False),
                 stat_before.st_size, current_backend, "running", utc_now()),
            )
            try:
                duration = duration_seconds(source_probe)
                def on_ffmpeg(data: dict[str, Any]) -> None:
                    file_fraction = float(data.get("percent") or 0) / 100
                    overall = (index + file_fraction) / len(rows) * 100
                    self._progress(job["id"], {
                        "stage": "无损换封装" if action == "remux" else (
                            "视频重新编码（Intel QSV）" if selected_backend == "intel_qsv" else "视频重新编码（libx264）"
                        ),
                        "current_file": row["relative_path"], "file_percent": data.get("percent", 0),
                        "percent": overall, "completed": index, "total": len(rows),
                        "processed_bytes": processed_bytes, "total_bytes": total_bytes,
                        "speed": data.get("speed"), "out_time": data.get("out_time"),
                        "duration_seconds": duration, "backend": current_backend,
                    }, started)
                run_ffmpeg(
                    conversion_args(source, temporary, action, settings, profile, selected_backend),
                    control=control, progress=on_ffmpeg, duration=duration, nice=settings.ffmpeg_nice,
                    cpu_percent=settings.cpu_percent if settings.cpu_limit_enabled else None,
                )
                self._progress(job["id"], {
                    "stage": "输出验证", "current_file": row["relative_path"], "file_percent": 0,
                    "percent": (index + 0.9) / len(rows) * 100, "completed": index, "total": len(rows),
                    "backend": current_backend,
                }, started)
                output_probe = probe_media(temporary)
                sample_verify(temporary, output_probe, control)
                output_duration = duration_seconds(output_probe)
                tolerance = max(2.0, duration * 0.02)
                if duration and output_duration + tolerance < duration:
                    raise TaskError(f"输出时长不完整：{output_duration:.3f}s / {duration:.3f}s")
                stat_check = source.stat()
                if (stat_check.st_size, stat_check.st_mtime_ns) != (stat_before.st_size, stat_before.st_mtime_ns):
                    raise TaskError("转换期间源文件发生变化")
                relative = source.relative_to(root)
                preserved_root = root / "转换前原文件"
                backup = unique_preserved_path(root, preserved_root, relative)
                self._progress(job["id"], {"stage": "移动原文件", "current_file": row["relative_path"], "percent": (index + 0.96) / len(rows) * 100, "backend": current_backend}, started)
                safe_rename(root, source, backup)
                try:
                    self._progress(job["id"], {"stage": "临时文件正式改名", "current_file": row["relative_path"], "percent": (index + 0.99) / len(rows) * 100, "backend": current_backend}, started)
                    safe_rename(root, temporary, target)
                except Exception:
                    # Compensating same-filesystem rename restores the source path; no copy or deletion is used.
                    safe_rename(root, backup, source)
                    backup = None
                    raise
                output_size = target.stat().st_size
                warning = None
                if output_size > stat_before.st_size:
                    difference = output_size - stat_before.st_size
                    warning = f"输出比原文件大 {difference} 字节"
                self.db.execute(
                    "UPDATE conversions SET output_path=?,backup_path=?,output_probe_json=?,output_size=?,warning=?,status='completed',completed_at=? WHERE id=?",
                    (str(target), str(backup), json.dumps(summarize_probe(output_probe), ensure_ascii=False), output_size, warning, utc_now(), conversion_id),
                )
                self.db.execute("UPDATE files SET scan_id=NULL,updated_at=? WHERE id=?", (utc_now(), row["id"]))
                completed.append({
                    "file_id": row["id"], "source_path": str(source), "output_path": str(target),
                    "backup_path": str(backup), "source_size": stat_before.st_size, "output_size": output_size,
                    "size_difference": output_size - stat_before.st_size, "warning": warning, "action": action,
                    "backend": current_backend,
                })
                processed_bytes += stat_before.st_size
                self._progress(job["id"], {
                    "stage": "整体批次", "current_file": row["relative_path"], "file_percent": 100,
                    "percent": (index + 1) / len(rows) * 100, "completed": index + 1, "total": len(rows),
                    "processed_bytes": processed_bytes, "total_bytes": total_bytes,
                    "backend": current_backend,
                }, started)
            except Exception as exc:
                failed_path = None
                try:
                    failed_path = self._move_failed_output(root, source if source.exists() else backup or source, temporary)
                except Exception as move_exc:
                    self.db.log("critical", "failed-output", str(move_exc), {"temporary": str(temporary)})
                self.db.execute(
                    "UPDATE conversions SET output_path=?,output_probe_json=?,status=?,completed_at=? WHERE id=?",
                    (failed_path, json.dumps(summarize_probe(output_probe), ensure_ascii=False) if output_probe else "{}",
                     "cancelled" if control.cancelled.is_set() else "failed", utc_now(), conversion_id),
                )
                raise TaskError(f"{row['relative_path']}：{exc}") from exc
            if settings.cooldown_seconds and index + 1 < len(rows):
                time.sleep(settings.cooldown_seconds)
        return {"completed": completed, "count": len(completed), "total": len(rows), "backend": selected_backend}
