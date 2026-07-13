from __future__ import annotations

import json
import os
import signal
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from .config import FFMPEG_BIN, FFPROBE_BIN, PROFILE_CRF, Settings


class MediaToolError(RuntimeError):
    pass


ProgressCallback = Callable[[dict[str, Any]], None]


@dataclass
class ProcessControl:
    cancelled: threading.Event = field(default_factory=threading.Event)
    paused: threading.Event = field(default_factory=threading.Event)
    _processes: set[subprocess.Popen[str]] = field(default_factory=set, init=False, repr=False)
    _stopped_pids: set[int] = field(default_factory=set, init=False, repr=False)
    _lock: threading.RLock = field(default_factory=threading.RLock, init=False, repr=False)

    def attach(self, process: subprocess.Popen[str]) -> None:
        with self._lock:
            self._processes.add(process)
            if self.cancelled.is_set() and process.poll() is None:
                try:
                    process.terminate()
                except ProcessLookupError:
                    pass
            elif self.paused.is_set() and process.poll() is None and os.name == "posix":
                try:
                    os.kill(process.pid, signal.SIGSTOP)
                    self._stopped_pids.add(process.pid)
                except ProcessLookupError:
                    pass

    def detach(self, process: subprocess.Popen[str]) -> None:
        with self._lock:
            self._processes.discard(process)
            self._stopped_pids.discard(process.pid)

    def cancel(self) -> None:
        self.cancelled.set()
        with self._lock:
            for process in tuple(self._processes):
                if process.poll() is None:
                    try:
                        process.terminate()
                    except ProcessLookupError:
                        pass

    def pause(self) -> None:
        self.paused.set()
        if os.name != "posix":
            return
        with self._lock:
            for process in tuple(self._processes):
                if process.poll() is None:
                    try:
                        os.kill(process.pid, signal.SIGSTOP)
                        self._stopped_pids.add(process.pid)
                    except ProcessLookupError:
                        pass

    def resume(self) -> None:
        self.paused.clear()
        if os.name != "posix":
            return
        with self._lock:
            for process in tuple(self._processes):
                if process.pid in self._stopped_pids and process.poll() is None:
                    try:
                        os.kill(process.pid, signal.SIGCONT)
                    except ProcessLookupError:
                        pass
            self._stopped_pids.clear()


def probe_media(path: Path, control: ProcessControl | None = None) -> dict[str, Any]:
    command = [
        FFPROBE_BIN, "-v", "error", "-show_error", "-show_format", "-show_streams",
        "-show_chapters", "-print_format", "json", str(path),
    ]
    control = control or ProcessControl()
    process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace")
    control.attach(process)
    try:
        stdout, stderr = process.communicate()
    finally:
        control.detach(process)
    if control.cancelled.is_set():
        raise MediaToolError("任务已取消")
    try:
        data = json.loads(stdout or "{}")
    except json.JSONDecodeError as exc:
        raise MediaToolError(f"FFprobe 返回无效 JSON：{exc}") from exc
    if process.returncode != 0 or data.get("error"):
        detail = stderr.strip() or str(data.get("error") or "未知错误")
        raise MediaToolError(f"FFprobe 失败：{detail}")
    if not data.get("streams"):
        raise MediaToolError("FFprobe 未发现媒体轨道")
    return data


def duration_seconds(probe: dict[str, Any]) -> float:
    values: list[float] = []
    raw = probe.get("format", {}).get("duration")
    try:
        values.append(float(raw))
    except (TypeError, ValueError):
        pass
    for stream in probe.get("streams", []):
        try:
            values.append(float(stream.get("duration")))
        except (TypeError, ValueError):
            pass
    return max(values, default=0.0)


def _bit_depth(stream: dict[str, Any]) -> int:
    for key in ("bits_per_raw_sample", "bits_per_sample"):
        try:
            value = int(stream.get(key) or 0)
            if value:
                return value
        except (TypeError, ValueError):
            pass
    pix_fmt = str(stream.get("pix_fmt") or "").lower()
    for depth in (16, 14, 12, 10, 9):
        if str(depth) in pix_fmt:
            return depth
    return 8


def _hdr_reason(probe: dict[str, Any]) -> str | None:
    for stream in probe.get("streams", []):
        if stream.get("codec_type") != "video" or stream.get("disposition", {}).get("attached_pic"):
            continue
        depth = _bit_depth(stream)
        if depth >= 10:
            return f"{depth}-bit 视频按安全规则跳过"
        profile = str(stream.get("profile") or "").lower()
        tags = " ".join(str(v) for v in stream.get("tags", {}).values()).lower()
        side_data = json.dumps(stream.get("side_data_list", []), ensure_ascii=False).lower()
        transfer = str(stream.get("color_transfer") or "").lower()
        if "dolby vision" in profile or "dovi" in tags or "dovi" in side_data:
            return "检测到杜比视界，按安全规则跳过"
        if transfer in {"smpte2084", "arib-std-b67"} or any(
            marker in side_data for marker in ("mastering display metadata", "content light level metadata", "smpte2084")
        ):
            return "检测到 HDR，按安全规则跳过"
    return None


def summarize_probe(probe: dict[str, Any]) -> dict[str, Any]:
    videos = [s for s in probe.get("streams", []) if s.get("codec_type") == "video"]
    audios = [s for s in probe.get("streams", []) if s.get("codec_type") == "audio"]
    subtitles = [s for s in probe.get("streams", []) if s.get("codec_type") == "subtitle"]
    primary = next((s for s in videos if not s.get("disposition", {}).get("attached_pic")), videos[0] if videos else {})
    return {
        "container": probe.get("format", {}).get("format_name", ""),
        "format_long_name": probe.get("format", {}).get("format_long_name", ""),
        "duration": duration_seconds(probe),
        "video_codec": primary.get("codec_name", ""),
        "video_profile": primary.get("profile", ""),
        "pix_fmt": primary.get("pix_fmt", ""),
        "bit_depth": _bit_depth(primary) if primary else None,
        "width": primary.get("width"),
        "height": primary.get("height"),
        "frame_rate": primary.get("avg_frame_rate") or primary.get("r_frame_rate"),
        "audio_tracks": [s.get("codec_name", "") for s in audios],
        "subtitle_tracks": [s.get("codec_name", "") for s in subtitles],
        "stream_count": len(probe.get("streams", [])),
        "chapter_count": len(probe.get("chapters", [])),
        "streams": probe.get("streams", []),
    }


MP4_VIDEO_CODECS = {"h264", "hevc", "mpeg4", "av1", "vp9", "mjpeg"}
MP4_AUDIO_CODECS = {"aac", "mp3", "ac3", "eac3", "alac", "flac"}
MP4_SUBTITLE_CODECS = {"mov_text"}


def incompatible_streams(probe: dict[str, Any]) -> list[str]:
    reasons: list[str] = []
    for stream in probe.get("streams", []):
        kind = stream.get("codec_type", "unknown")
        codec = stream.get("codec_name", "unknown")
        index = stream.get("index", "?")
        if kind == "video" and stream.get("disposition", {}).get("attached_pic"):
            reasons.append(f"轨道 {index}：封面附件不能保证原样写入 MP4")
        elif kind == "video" and codec not in MP4_VIDEO_CODECS:
            continue
        elif kind == "audio" and codec not in MP4_AUDIO_CODECS:
            reasons.append(f"轨道 {index}：音频 {codec} 不能按规则原样写入 MP4")
        elif kind == "subtitle" and codec not in MP4_SUBTITLE_CODECS:
            reasons.append(f"轨道 {index}：字幕 {codec} 不能按规则原样写入 MP4")
        elif kind in {"attachment", "data", "unknown"}:
            reasons.append(f"轨道 {index}：{kind} / {codec} 不能按规则原样写入 MP4")
    return reasons


def classify_media(path: Path, probe: dict[str, Any]) -> tuple[str, str]:
    hdr = _hdr_reason(probe)
    if hdr:
        return "skipped", hdr
    videos = [
        s for s in probe.get("streams", [])
        if s.get("codec_type") == "video" and not s.get("disposition", {}).get("attached_pic")
    ]
    if not videos:
        return "unsupported", "没有可处理的视频轨道"
    format_names = set(str(probe.get("format", {}).get("format_name", "")).split(","))
    if path.suffix.lower() == ".mp4" or "mp4" in format_names:
        return "no_conversion", "已是 MP4，仅执行完整性检测"
    incompatible = incompatible_streams(probe)
    if incompatible:
        return "unsupported", "；".join(incompatible)
    if all(s.get("codec_name") in MP4_VIDEO_CODECS for s in videos):
        return "remux", "全部轨道可原样写入 MP4（-map 0 -c copy）"
    return "transcode", "普通 SDR 8-bit 视频需转为 H.264，其他轨道保持原样"


def _preexec(nice: int) -> Callable[[], None] | None:
    if os.name != "posix":
        return None
    def set_nice() -> None:
        if nice:
            os.nice(nice)
    return set_nice


def run_ffmpeg(
    args: list[str],
    *,
    control: ProcessControl | None = None,
    progress: ProgressCallback | None = None,
    duration: float = 0,
    nice: int = 0,
    cpu_percent: int | None = None,
) -> list[str]:
    control = control or ProcessControl()
    command = [FFMPEG_BIN, "-hide_banner", "-nostdin", "-nostats", "-progress", "pipe:1", *args]
    process = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
        preexec_fn=_preexec(nice),
    )
    control.attach(process)
    throttle_done = threading.Event()
    def throttle() -> None:
        if os.name != "posix" or not cpu_percent or cpu_percent >= 100:
            return
        cycle = 0.25
        running = cycle * cpu_percent / 100
        stopped = cycle - running
        while not throttle_done.is_set() and process.poll() is None:
            if control.paused.is_set():
                throttle_done.wait(0.1)
                continue
            if throttle_done.wait(running) or process.poll() is not None or control.paused.is_set():
                continue
            try:
                os.kill(process.pid, signal.SIGSTOP)
                if throttle_done.wait(stopped) or process.poll() is not None:
                    break
                if not control.paused.is_set():
                    os.kill(process.pid, signal.SIGCONT)
            except ProcessLookupError:
                break
    throttle_thread = threading.Thread(target=throttle, name=f"ffmpeg-throttle-{process.pid}", daemon=True)
    throttle_thread.start()
    messages: list[str] = []
    block: dict[str, Any] = {}
    try:
        assert process.stdout is not None
        for raw_line in process.stdout:
            if control.cancelled.is_set() and process.poll() is None:
                process.terminate()
            line = raw_line.rstrip()
            if "=" in line:
                key, value = line.split("=", 1)
                if key in {"frame", "fps", "bitrate", "total_size", "out_time_us", "out_time_ms", "out_time", "speed", "progress"}:
                    block[key] = value
                    if key == "progress":
                        try:
                            # FFmpeg versions disagree whether out_time_ms is microseconds; out_time_us is explicit.
                            micros = int(block.get("out_time_us") or block.get("out_time_ms") or 0)
                        except (TypeError, ValueError):
                            micros = 0
                        current = max(0.0, micros / 1_000_000)
                        block["current_seconds"] = current
                        block["duration_seconds"] = duration
                        block["percent"] = min(100.0, current / duration * 100) if duration else 0.0
                        if progress:
                            progress(dict(block))
                        block = {}
                    continue
            if line:
                messages.append(line)
                if len(messages) > 200:
                    messages = messages[-200:]
        return_code = process.wait()
    finally:
        throttle_done.set()
        if process.poll() is None:
            if os.name == "posix":
                try:
                    os.kill(process.pid, signal.SIGCONT)
                except ProcessLookupError:
                    pass
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        throttle_thread.join(timeout=1)
        control.detach(process)
    if control.cancelled.is_set():
        raise MediaToolError("任务已取消")
    if return_code != 0:
        raise MediaToolError("FFmpeg 失败：" + "\n".join(messages[-20:]))
    return messages


def sample_verify(path: Path, probe: dict[str, Any], control: ProcessControl | None = None) -> dict[str, Any]:
    duration = duration_seconds(probe)
    positions = [0.0]
    if duration > 2:
        positions.extend([duration * 0.5, max(0.0, duration - min(3.0, duration * 0.05))])
    started = time.monotonic()
    for position in positions:
        args = ["-v", "error"]
        if position:
            args.extend(["-ss", f"{position:.3f}"])
        args.extend(["-i", str(path), "-map", "0:v:0", "-frames:v", "3", "-f", "null", "-"])
        run_ffmpeg(args, control=control, duration=duration)
    return {"status": "passed", "mode": "sample", "positions": positions, "elapsed_seconds": time.monotonic() - started}


def full_verify(
    path: Path,
    probe: dict[str, Any],
    control: ProcessControl | None = None,
    progress: ProgressCallback | None = None,
    settings: Settings | None = None,
) -> dict[str, Any]:
    started = time.monotonic()
    duration = duration_seconds(probe)
    args = ["-v", "error", "-xerror", "-err_detect", "explode", "-i", str(path),
            "-map", "0:v:0", "-map", "0:a?"]
    if settings and settings.ffmpeg_threads:
        args.extend(["-threads", str(settings.ffmpeg_threads)])
    args.extend(["-f", "null", "-"])
    run_ffmpeg(
        args, control=control, progress=progress, duration=duration,
        nice=settings.ffmpeg_nice if settings else 0,
        cpu_percent=settings.cpu_percent if settings and settings.cpu_limit_enabled else None,
    )
    return {"status": "passed", "mode": "full", "elapsed_seconds": time.monotonic() - started}


def intel_qsv_status(device: Path | None = None) -> dict[str, Any]:
    device = device or Path(os.getenv("QSV_DEVICE", "/dev/dri/renderD128"))
    try:
        completed = subprocess.run(
            [FFMPEG_BIN, "-hide_banner", "-encoders"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
        )
        encoder_available = completed.returncode == 0 and "h264_qsv" in completed.stdout
        encoder_error = completed.stderr.strip() if completed.returncode else ""
    except (OSError, subprocess.TimeoutExpired) as exc:
        encoder_available = False
        encoder_error = str(exc)
    device_exists = device.exists()
    device_readable = device_exists and os.access(device, os.R_OK)
    device_writable = device_exists and os.access(device, os.W_OK)
    available = encoder_available and device_readable and device_writable
    if not encoder_available:
        reason = f"FFmpeg 不提供 h264_qsv 编码器{f'：{encoder_error}' if encoder_error else ''}"
    elif not device_exists:
        reason = f"未映射 Intel 渲染设备 {device}"
    elif not device_readable or not device_writable:
        reason = f"容器用户对 {device} 没有读写权限"
    else:
        reason = "Intel Quick Sync H.264 可用"
    return {
        "available": available,
        "backend": "intel_qsv",
        "device": str(device),
        "device_exists": device_exists,
        "device_readable": device_readable,
        "device_writable": device_writable,
        "encoder_available": encoder_available,
        "reason": reason,
    }


def transcode_backend(settings: Settings) -> str:
    if settings.hardware_acceleration == "software":
        return "software"
    status = intel_qsv_status()
    if status["available"]:
        return "intel_qsv"
    if settings.hardware_acceleration == "intel_qsv":
        raise MediaToolError(f"Intel Quick Sync 已被强制启用，但当前不可用：{status['reason']}")
    return "software"


def conversion_args(
    source: Path,
    output: Path,
    action: str,
    settings: Settings,
    profile: str,
    backend: str | None = None,
) -> list[str]:
    args = ["-n", "-i", str(source), "-map", "0", "-map_metadata", "0", "-map_chapters", "0"]
    if action == "remux":
        args.extend(["-c", "copy"])
    elif action == "transcode":
        quality = PROFILE_CRF[profile]
        backend = backend or transcode_backend(settings)
        if backend == "intel_qsv":
            args.extend([
                "-c", "copy", "-c:v", "h264_qsv", "-global_quality", str(quality),
                "-preset", "medium", "-pix_fmt", "nv12",
            ])
        elif backend == "software":
            args.extend([
                "-c", "copy", "-c:v", "libx264", "-crf", str(quality),
                "-preset", "medium", "-pix_fmt", "yuv420p",
            ])
            if settings.ffmpeg_threads:
                args.extend(["-threads", str(settings.ffmpeg_threads)])
        else:
            raise ValueError(f"未知转码后端：{backend}")
    else:
        raise ValueError("未知处理类型")
    args.extend(["-movflags", "+faststart", "-f", "mp4", str(output)])
    return args
