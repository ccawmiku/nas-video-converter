from __future__ import annotations

from pathlib import Path

from app.ffmpeg_tools import classify_media, incompatible_streams


def probe(video="h264", audio="aac", subtitle=None, pix_fmt="yuv420p", transfer=None):
    streams = [{"index": 0, "codec_type": "video", "codec_name": video, "pix_fmt": pix_fmt, "color_transfer": transfer, "disposition": {}}]
    if audio:
        streams.append({"index": 1, "codec_type": "audio", "codec_name": audio, "disposition": {}})
    if subtitle:
        streams.append({"index": 2, "codec_type": "subtitle", "codec_name": subtitle, "disposition": {}})
    return {"format": {"format_name": "matroska"}, "streams": streams, "chapters": []}


def test_h264_mkv_can_remux() -> None:
    category, _ = classify_media(Path("示例.mkv"), probe())
    assert category == "remux"


def test_mp4_needs_only_integrity_check() -> None:
    category, _ = classify_media(Path("hevc.mp4"), probe(video="hevc"))
    assert category == "no_conversion"


def test_sdr_incompatible_video_transcodes() -> None:
    category, _ = classify_media(Path("old.avi"), probe(video="mpeg2video"))
    assert category == "transcode"


def test_hdr_and_10_bit_skip() -> None:
    assert classify_media(Path("ten.mkv"), probe(video="hevc", pix_fmt="yuv420p10le"))[0] == "skipped"
    assert classify_media(Path("hdr.mkv"), probe(video="hevc", transfer="smpte2084"))[0] == "skipped"


def test_incompatible_tracks_are_explained() -> None:
    for codec in ("dts", "truehd"):
        category, reason = classify_media(Path("audio.mkv"), probe(audio=codec))
        assert category == "unsupported"
        assert codec in reason
    category, reason = classify_media(Path("subs.mkv"), probe(subtitle="hdmv_pgs_subtitle"))
    assert category == "unsupported"
    assert "hdmv_pgs_subtitle" in reason


def test_attachment_is_unsupported() -> None:
    data = probe()
    data["streams"].append({"index": 3, "codec_type": "attachment", "codec_name": "ttf", "disposition": {}})
    assert incompatible_streams(data)
    assert classify_media(Path("attachment.mkv"), data)[0] == "unsupported"

