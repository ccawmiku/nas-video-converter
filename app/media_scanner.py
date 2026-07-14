from __future__ import annotations

import json
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Callable

from .config import BASE_MEDIA_DIR, CORRUPT_DIR_NAME, EXCLUDED_DIR_NAMES, VIDEO_EXTENSIONS, Settings
from .database import Database, utc_now
from .ffmpeg_tools import MediaToolError, ProcessControl, classify_media, full_verify, probe_media, sample_verify, summarize_probe
from .safety import (
    SafetyError,
    ensure_safe_path,
    ensure_safe_root,
    is_excluded,
    safe_rename,
    unique_preserved_path,
)


ScanProgress = Callable[[dict[str, Any]], None]
FULL_VERIFY_LOCK = threading.Lock()


class ScanCancelled(RuntimeError):
    pass


def _control_checkpoint(control: ProcessControl | None) -> None:
    if not control:
        return
    while control.paused.is_set() and not control.cancelled.is_set():
        time.sleep(0.1)
    if control.cancelled.is_set():
        raise ScanCancelled("扫描任务已取消")


def discover_roots(base: Path = BASE_MEDIA_DIR) -> list[dict[str, str]]:
    if not base.exists():
        return []
    base = ensure_safe_root(base)
    roots: list[dict[str, str]] = []
    for child in sorted(base.iterdir(), key=lambda p: p.name.casefold()):
        if child.name.casefold() in EXCLUDED_DIR_NAMES or child.is_symlink() or not child.is_dir():
            continue
        roots.append({"name": child.name, "path": str(child.resolve())})
    if not roots:
        roots.append({"name": base.name or str(base), "path": str(base)})
    return roots


def allowed_root(requested: str, base: Path = BASE_MEDIA_DIR) -> Path:
    requested_path = Path(requested)
    choices = {item["path"]: Path(item["path"]) for item in discover_roots(base)}
    try:
        resolved = str(requested_path.resolve(strict=True))
    except OSError as exc:
        raise SafetyError("映射根目录不存在") from exc
    if resolved not in choices:
        raise SafetyError("只能选择已映射的媒体根目录")
    return ensure_safe_root(choices[resolved])


def reclassify_stored_files(db: Database) -> int:
    updated = 0
    rows = db.query(
        "SELECT id,path,category,reason,probe_json FROM files WHERE integrity_status='passed'"
    )
    for row in rows:
        try:
            stored_probe = json.loads(row["probe_json"] or "{}")
            probe = stored_probe.get("raw") if isinstance(stored_probe, dict) else None
            if not isinstance(probe, dict) or not probe.get("streams"):
                continue
            category, reason = classify_media(Path(row["path"]), probe)
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        if category == row["category"] and reason == row["reason"]:
            continue
        db.execute(
            "UPDATE files SET category=?,reason=?,updated_at=? WHERE id=?",
            (category, reason, utc_now(), row["id"]),
        )
        updated += 1
    return updated


def enumerate_videos(
    root: Path,
    progress: ScanProgress | None = None,
    control: ProcessControl | None = None,
) -> list[Path]:
    root = ensure_safe_root(root)
    found: list[Path] = []
    directories = 0
    discovered_directories = 1
    for current, dir_names, file_names in os.walk(root, topdown=True, followlinks=False):
        _control_checkpoint(control)
        current_path = Path(current)
        relative_current = current_path.relative_to(root)
        safe_dirs: list[str] = []
        for name in dir_names:
            candidate_rel = relative_current / name
            candidate = current_path / name
            if is_excluded(candidate_rel) or candidate.is_symlink():
                continue
            try:
                ensure_safe_path(root, candidate)
            except SafetyError:
                continue
            safe_dirs.append(name)
        dir_names[:] = safe_dirs
        directories += 1
        discovered_directories += len(safe_dirs)
        for name in file_names:
            path = current_path / name
            relative = path.relative_to(root)
            if (
                is_excluded(relative)
                or (".nvc-" in name and name.endswith(".tmp.mp4"))
                or path.suffix.casefold() not in VIDEO_EXTENSIONS
                or path.is_symlink()
            ):
                continue
            try:
                found.append(ensure_safe_path(root, path))
            except SafetyError:
                continue
        if progress:
            progress({
                "stage": "目录枚举", "directories": directories, "directories_discovered": discovered_directories,
                "files_found": len(found), "completed": directories, "total": discovered_directories,
                "percent": directories / discovered_directories * 100,
            })
    return found


def _stability(db: Database, path: Path, stat: os.stat_result, stable_seconds: int, require_stable: bool) -> bool:
    now = utc_now()
    previous = db.one("SELECT * FROM stability WHERE path=?", (str(path),))
    if previous and previous["size"] == stat.st_size and previous["mtime_ns"] == stat.st_mtime_ns:
        db.execute("UPDATE stability SET last_seen_at=? WHERE path=?", (now, str(path)))
        if not require_stable:
            return True
        first = previous["first_seen_unchanged_at"]
        from datetime import datetime
        elapsed = datetime.fromisoformat(now).timestamp() - datetime.fromisoformat(first).timestamp()
        return elapsed >= stable_seconds
    db.execute(
        "INSERT INTO stability(path,size,mtime_ns,first_seen_unchanged_at,last_seen_at) VALUES(?,?,?,?,?) "
        "ON CONFLICT(path) DO UPDATE SET size=excluded.size,mtime_ns=excluded.mtime_ns,"
        "first_seen_unchanged_at=excluded.first_seen_unchanged_at,last_seen_at=excluded.last_seen_at",
        (str(path), stat.st_size, stat.st_mtime_ns, now, now),
    )
    return not require_stable


def inspect_file(
    db: Database,
    root: Path,
    path: Path,
    settings: Settings,
    require_stable: bool,
    control: ProcessControl | None = None,
) -> dict[str, Any]:
    started = time.monotonic()
    _control_checkpoint(control)
    path = ensure_safe_path(root, path)
    stat_before = path.stat()
    stable = _stability(db, path, stat_before, settings.stable_seconds, require_stable)
    relative = path.relative_to(root)
    if not stable:
        return {
            "path": str(path), "real_path": str(path), "relative_path": str(relative),
            "size": stat_before.st_size, "mtime_ns": stat_before.st_mtime_ns,
            "category": "skipped", "reason": f"文件尚未连续稳定 {settings.stable_seconds} 秒",
            "probe": {}, "integrity_status": "pending", "integrity": {}, "stable": False,
            "elapsed_seconds": time.monotonic() - started,
        }
    try:
        probe = probe_media(path, control)
        _control_checkpoint(control)
        category, reason = classify_media(path, probe)
        cached = db.one(
            "SELECT integrity_status,integrity_json FROM files WHERE real_path=? AND size=? AND mtime_ns=?",
            (str(path), stat_before.st_size, stat_before.st_mtime_ns),
        )
        if cached and cached["integrity_status"] == "passed":
            integrity_status = "passed"
            integrity = json.loads(cached["integrity_json"])
            integrity["cached"] = True
        else:
            try:
                integrity = sample_verify(path, probe, control)
            except MediaToolError as sample_error:
                _control_checkpoint(control)
                # Abnormal samples are escalated one-at-a-time to a strict full decode.
                with FULL_VERIFY_LOCK:
                    _control_checkpoint(control)
                    integrity = full_verify(path, probe, control)
                integrity["escalated_from_sample_error"] = str(sample_error)
            integrity_status = "passed"
        _control_checkpoint(control)
        stat_after = path.stat()
        if (stat_after.st_size, stat_after.st_mtime_ns) != (stat_before.st_size, stat_before.st_mtime_ns):
            category, reason = "skipped", "检测期间文件发生变化，视为仍在写入"
            integrity_status = "changed"
    except MediaToolError as exc:
        if control and control.cancelled.is_set():
            raise ScanCancelled("扫描任务已取消") from exc
        probe = {}
        category, reason = "skipped", f"文件损坏或严格检测失败：{exc}"
        integrity_status = "failed"
        integrity = {"status": "failed", "error": str(exc)}
        try:
            stat_after = path.stat()
            if (stat_after.st_size, stat_after.st_mtime_ns) != (stat_before.st_size, stat_before.st_mtime_ns):
                reason += "；检测期间文件发生变化，未自动移动"
            else:
                quarantine = unique_preserved_path(root, root / CORRUPT_DIR_NAME, relative)
                safe_rename(root, path, quarantine)
                path = quarantine.resolve(strict=True)
                relative = path.relative_to(root)
                integrity["quarantined_path"] = str(path)
                reason = f"已确认损坏并安全移动到 {relative}：{exc}"
                db.log("warning", "corrupt-file", reason, {"path": str(path)})
        except (OSError, SafetyError) as move_exc:
            reason += f"；自动移动到 {CORRUPT_DIR_NAME} 失败：{move_exc}"
            integrity["quarantine_error"] = str(move_exc)
    except OSError as exc:
        probe = {}
        category, reason = "skipped", f"文件读取或权限失败，未自动移动：{exc}"
        integrity_status = "failed"
        integrity = {"status": "failed", "error": str(exc)}
    return {
        "path": str(path), "real_path": str(path), "relative_path": str(relative),
        "size": stat_before.st_size, "mtime_ns": stat_before.st_mtime_ns,
        "category": category, "reason": reason, "probe": {**summarize_probe(probe), "raw": probe} if probe else {},
        "integrity_status": integrity_status, "integrity": integrity, "stable": stable,
        "elapsed_seconds": time.monotonic() - started,
    }


def scan_root(
    db: Database,
    root: Path,
    scan_id: str,
    settings: Settings,
    progress: ScanProgress | None = None,
    require_stable: bool = False,
    control: ProcessControl | None = None,
) -> dict[str, Any]:
    root = ensure_safe_root(root)
    started = time.monotonic()
    _control_checkpoint(control)
    paths = enumerate_videos(root, progress, control)
    _control_checkpoint(control)
    total_bytes = sum(path.stat().st_size for path in paths)
    if progress:
        progress({"stage": "文件统计", "completed": len(paths), "total": len(paths), "total_bytes": total_bytes, "percent": 100})
    results: list[dict[str, Any]] = []
    processed_bytes = 0
    with ThreadPoolExecutor(max_workers=settings.scan_concurrency) as pool:
        futures = {pool.submit(inspect_file, db, root, path, settings, require_stable, control): path for path in paths}
        for index, future in enumerate(as_completed(futures), 1):
            _control_checkpoint(control)
            item = future.result()
            results.append(item)
            processed_bytes += item["size"]
            if progress:
                progress({
                    "stage": "FFprobe 分析与抽样解码", "current_file": item["relative_path"],
                    "completed": index, "total": len(paths), "processed_bytes": processed_bytes,
                    "total_bytes": total_bytes, "percent": index / len(paths) * 100 if paths else 100,
                })
    _control_checkpoint(control)
    now = utc_now()
    for item in results:
        db.execute(
            "INSERT INTO files(root,path,real_path,relative_path,size,mtime_ns,category,reason,probe_json,"
            "integrity_status,integrity_json,stable,scan_id,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(path) DO UPDATE SET root=excluded.root,real_path=excluded.real_path,relative_path=excluded.relative_path,"
            "size=excluded.size,mtime_ns=excluded.mtime_ns,category=excluded.category,reason=excluded.reason,"
            "probe_json=excluded.probe_json,integrity_status=excluded.integrity_status,integrity_json=excluded.integrity_json,"
            "stable=excluded.stable,scan_id=excluded.scan_id,updated_at=excluded.updated_at",
            (str(root), item["path"], item["real_path"], item["relative_path"], item["size"], item["mtime_ns"],
             item["category"], item["reason"], json.dumps(item["probe"], ensure_ascii=False), item["integrity_status"],
             json.dumps(item["integrity"], ensure_ascii=False), int(item["stable"]), scan_id, now),
        )
    # Files that disappeared are retained in history but not attributed to the current scan.
    categories = {name: {"count": 0, "bytes": 0} for name in ("no_conversion", "remux", "transcode", "unsupported", "skipped")}
    for item in results:
        categories[item["category"]]["count"] += 1
        categories[item["category"]]["bytes"] += item["size"]
    summary = {
        "root": str(root), "scan_id": scan_id, "total_count": len(results), "total_bytes": total_bytes,
        "categories": categories, "elapsed_seconds": time.monotonic() - started,
    }
    db.execute(
        "INSERT INTO root_state(root,last_scan_id,summary_json,updated_at) VALUES(?,?,?,?) "
        "ON CONFLICT(root) DO UPDATE SET last_scan_id=excluded.last_scan_id,summary_json=excluded.summary_json,updated_at=excluded.updated_at",
        (str(root), scan_id, json.dumps(summary, ensure_ascii=False), utc_now()),
    )
    if progress:
        progress({"stage": "生成处理计划", "percent": 100, "completed": len(results), "total": len(results), "summary": summary})
    return summary
