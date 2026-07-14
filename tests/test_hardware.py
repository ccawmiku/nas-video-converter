from __future__ import annotations

from pathlib import Path
import subprocess

import pytest

from app.config import Settings
from app.ffmpeg_tools import (
    MediaToolError,
    conversion_args,
    intel_qsv_status,
    intel_vaapi_status,
    transcode_backend,
)


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


def test_auto_falls_back_to_vaapi_but_forced_qsv_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("app.ffmpeg_tools.intel_qsv_status", lambda: qsv_status(False))
    monkeypatch.setattr("app.ffmpeg_tools.intel_vaapi_status", lambda: qsv_status(True))
    assert transcode_backend(Settings(hardware_acceleration="auto")) == "intel_vaapi"
    with pytest.raises(MediaToolError, match="强制启用"):
        transcode_backend(Settings(hardware_acceleration="intel_qsv"))


def test_auto_falls_back_to_software_and_forced_vaapi_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("app.ffmpeg_tools.intel_qsv_status", lambda: qsv_status(False))
    monkeypatch.setattr("app.ffmpeg_tools.intel_vaapi_status", lambda: qsv_status(False))
    assert transcode_backend(Settings(hardware_acceleration="auto")) == "software"
    with pytest.raises(MediaToolError, match="VAAPI 已被强制启用"):
        transcode_backend(Settings(hardware_acceleration="intel_vaapi"))


def test_conversion_arguments_select_requested_encoder() -> None:
    source = Path("input.avi")
    output = Path("output.mp4")
    settings = Settings()
    software = conversion_args(source, output, "transcode", settings, "recommended", "software")
    assert "libx264" in software
    assert software[software.index("-crf") + 1] == "18"
    assert "yuv420p" in software
    assert software[software.index("-vf") + 1] == "pad=ceil(iw/2)*2:ceil(ih/2)*2"

    hardware = conversion_args(source, output, "transcode", settings, "recommended", "intel_qsv")
    assert "h264_qsv" in hardware
    assert hardware[hardware.index("-global_quality") + 1] == "18"
    assert "nv12" in hardware
    assert hardware[hardware.index("-vf") + 1] == "pad=ceil(iw/2)*2:ceil(ih/2)*2"
    assert hardware[hardware.index("-qsv_device") + 1] == "/dev/dri/renderD128"
    assert hardware.index("-qsv_device") < hardware.index("-i")
    assert "-crf" not in hardware
    assert software[software.index("-map", software.index("-map") + 1) + 1] == "-0:d?"

    vaapi = conversion_args(source, output, "transcode", settings, "recommended", "intel_vaapi")
    assert "h264_vaapi" in vaapi
    assert vaapi[vaapi.index("-qp") + 1] == "18"
    assert vaapi[vaapi.index("-vf") + 1] == "pad=ceil(iw/2)*2:ceil(ih/2)*2,format=nv12,hwupload"
    assert vaapi[vaapi.index("-vaapi_device") + 1] == "/dev/dri/renderD128"
    assert vaapi.index("-vaapi_device") < vaapi.index("-i")
    assert "libx264" not in vaapi
    assert "h264_qsv" not in vaapi


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


def test_qsv_status_runs_an_actual_encode_probe(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    device = tmp_path / "renderD128"
    device.write_bytes(b"device-placeholder")
    calls: list[list[str]] = []

    def fake_run(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        if "-encoders" in command:
            return subprocess.CompletedProcess(command, 0, stdout=" V..... h264_qsv Intel QSV", stderr="")
        return subprocess.CompletedProcess(command, 1, stdout="", stderr="Error creating a MFX session")

    monkeypatch.setattr("app.ffmpeg_tools.subprocess.run", fake_run)
    status = intel_qsv_status(device)
    assert not status["available"]
    assert status["runtime_tested"]
    assert "MFX session" in status["reason"]
    assert calls[1][calls[1].index("-qsv_device") + 1] == str(device)

    calls.clear()
    base = intel_qsv_status(device, probe_encode=False)
    assert base["available"]
    assert not base["runtime_tested"]
    assert len(calls) == 1


def test_vaapi_status_runs_an_actual_encode_probe(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    device = tmp_path / "renderD128"
    device.write_bytes(b"device-placeholder")
    calls: list[list[str]] = []

    def fake_run(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        if "-encoders" in command:
            return subprocess.CompletedProcess(command, 0, stdout=" V..... h264_vaapi Intel VAAPI", stderr="")
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr("app.ffmpeg_tools.subprocess.run", fake_run)
    status = intel_vaapi_status(device)
    assert status["available"]
    assert status["runtime_tested"]
    probe = calls[1]
    assert probe[probe.index("-vaapi_device") + 1] == str(device)
    assert probe[probe.index("-vf") + 1] == "format=nv12,hwupload"
    assert "h264_vaapi" in probe
