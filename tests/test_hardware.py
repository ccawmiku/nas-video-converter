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
