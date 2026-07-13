from __future__ import annotations

from pathlib import Path

import pytest

from app.config import Settings
from app.ffmpeg_tools import MediaToolError, conversion_args, transcode_backend


def qsv_status(available: bool) -> dict:
    return {
        "available": available,
        "reason": "available" if available else "device unavailable",
    }


def test_hardware_acceleration_defaults_to_safe_auto() -> None:
    settings = Settings()
    assert settings.hardware_acceleration == "auto"
    with pytest.raises(ValueError):
        settings.update({"hardware_acceleration": "unknown"})


def test_auto_uses_qsv_when_available(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("app.ffmpeg_tools.intel_qsv_status", lambda: qsv_status(True))
    assert transcode_backend(Settings(hardware_acceleration="auto")) == "intel_qsv"


def test_auto_falls_back_but_forced_qsv_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("app.ffmpeg_tools.intel_qsv_status", lambda: qsv_status(False))
    assert transcode_backend(Settings(hardware_acceleration="auto")) == "software"
    with pytest.raises(MediaToolError, match="强制启用"):
        transcode_backend(Settings(hardware_acceleration="intel_qsv"))


def test_conversion_arguments_select_requested_encoder() -> None:
    source = Path("input.avi")
    output = Path("output.mp4")
    settings = Settings()
    software = conversion_args(source, output, "transcode", settings, "recommended", "software")
    assert "libx264" in software
    assert software[software.index("-crf") + 1] == "18"
    assert "yuv420p" in software

    hardware = conversion_args(source, output, "transcode", settings, "recommended", "intel_qsv")
    assert "h264_qsv" in hardware
    assert hardware[hardware.index("-global_quality") + 1] == "18"
    assert "nv12" in hardware
    assert "-crf" not in hardware
    assert software[software.index("-map", software.index("-map") + 1) + 1] == "-0:d?"


def test_remux_excludes_only_preclassified_data_tracks() -> None:
    args = conversion_args(Path("input.ts"), Path("output.mp4"), "remux", Settings(), "recommended")
    assert ["-map", "0", "-map", "-0:d?"] == args[args.index("-map"):args.index("-map") + 4]
    assert args[args.index("-c") + 1] == "copy"


def test_transcode_converts_only_opus_audio_tracks_to_aac() -> None:
    source_probe = {"streams": [
        {"index": 0, "codec_type": "video", "codec_name": "vp9"},
        {"index": 1, "codec_type": "audio", "codec_name": "opus", "channels": 2},
        {"index": 2, "codec_type": "audio", "codec_name": "aac", "channels": 2},
    ]}
    args = conversion_args(
        Path("input.webm"), Path("output.mp4"), "transcode", Settings(), "recommended", "software",
        source_probe=source_probe,
    )
    assert args[args.index("-c:a:0") + 1] == "aac"
    assert args[args.index("-b:a:0") + 1] == "192k"
    assert "-c:a:1" not in args


def test_entrypoint_preserves_qsv_supplementary_group() -> None:
    entrypoint = (Path(__file__).parents[1] / "scripts" / "docker-entrypoint.sh").read_text(encoding="utf-8")
    assert 'exec gosu "$NVC_USER" "$@"' in entrypoint
    assert 'exec gosu "$NVC_USER:$PGID" "$@"' not in entrypoint
