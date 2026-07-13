from __future__ import annotations

import os
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any


@dataclass(slots=True)
class Settings:
    process_concurrency: int = 1
    ffmpeg_threads: int = 0
    ffmpeg_nice: int = 0
    scan_concurrency: int = 2
    verify_concurrency: int = 1
    cpu_limit_enabled: bool = False
    cpu_percent: int = 80
    cooldown_seconds: int = 0
    memory_guard_percent: int = 90
    monitor_interval_seconds: int = 300
    stable_seconds: int = 900
    schedule_time: str = "02:00"
    schedule_enabled: bool = False
    monitor_enabled: bool = False
    auto_remux: bool = False
    auto_transcode: bool = False
    queue_limit: int = 100
    transcode_profile: str = "recommended"

    def update(self, values: dict[str, Any]) -> "Settings":
        allowed = set(asdict(self))
        for key, value in values.items():
            if key in allowed:
                setattr(self, key, value)
        self.validate()
        return self

    def validate(self) -> None:
        for name in ("process_concurrency", "scan_concurrency", "verify_concurrency"):
            value = int(getattr(self, name))
            if not 1 <= value <= 16:
                raise ValueError(f"{name} 必须在 1 到 16 之间")
            setattr(self, name, value)
        self.ffmpeg_threads = int(self.ffmpeg_threads)
        if not 0 <= self.ffmpeg_threads <= 256:
            raise ValueError("ffmpeg_threads 必须在 0 到 256 之间")
        self.ffmpeg_nice = int(self.ffmpeg_nice)
        if not -20 <= self.ffmpeg_nice <= 19:
            raise ValueError("ffmpeg_nice 必须在 -20 到 19 之间")
        self.cpu_percent = int(self.cpu_percent)
        if not 10 <= self.cpu_percent <= 100:
            raise ValueError("cpu_percent 必须在 10 到 100 之间")
        self.memory_guard_percent = int(self.memory_guard_percent)
        if not 50 <= self.memory_guard_percent <= 99:
            raise ValueError("memory_guard_percent 必须在 50 到 99 之间")
        self.queue_limit = int(self.queue_limit)
        if not 1 <= self.queue_limit <= 1000:
            raise ValueError("queue_limit 必须在 1 到 1000 之间")
        hours, minutes = self.schedule_time.split(":", 1)
        if not (0 <= int(hours) <= 23 and 0 <= int(minutes) <= 59):
            raise ValueError("schedule_time 必须为 HH:MM")
        if self.transcode_profile not in {"quality", "recommended", "space"}:
            raise ValueError("未知转码档位")

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


BASE_MEDIA_DIR = Path(os.getenv("MEDIA_ROOT", "/media"))
CONFIG_DIR = Path(os.getenv("CONFIG_DIR", "/config"))
DATABASE_PATH = Path(os.getenv("DATABASE_PATH", str(CONFIG_DIR / "nas-video-converter.db")))
FFMPEG_BIN = os.getenv("FFMPEG_BIN", "ffmpeg")
FFPROBE_BIN = os.getenv("FFPROBE_BIN", "ffprobe")
TIMEZONE = os.getenv("TZ", "Asia/Shanghai")

VIDEO_EXTENSIONS = {
    ".3gp", ".avi", ".flv", ".m2ts", ".m4v", ".mkv", ".mov", ".mp4",
    ".mpeg", ".mpg", ".mts", ".ogm", ".rm", ".rmvb", ".ts", ".vob", ".webm", ".wmv",
}

EXCLUDED_DIR_NAMES = {
    "@eadir", "#recycle", ".snapshot", "@snapshot", "转换前原文件", "转换失败输出",
}

PROFILE_CRF = {"quality": 16, "recommended": 18, "space": 20}

