from pathlib import Path


def test_live_progress_uses_sse_job_id_without_api_roundtrip() -> None:
    javascript = (Path(__file__).parents[1] / "app" / "static" / "app.js").read_text(encoding="utf-8")
    assert 'addEventListener("progress"' in javascript
    assert "id=progress.job_id" in javascript
    assert "renderProgress(job)" in javascript


def test_actual_backend_is_visible_in_live_progress() -> None:
    root = Path(__file__).parents[1]
    javascript = (root / "app" / "static" / "app.js").read_text(encoding="utf-8")
    html = (root / "app" / "static" / "index.html").read_text(encoding="utf-8")
    assert 'id="hardwareBadge"' in html
    assert 'id="backend"' in html
    assert "backendLabels" in javascript


def test_settings_are_grouped_and_switches_use_compact_layout() -> None:
    root = Path(__file__).parents[1]
    javascript = (root / "app" / "static" / "app.js").read_text(encoding="utf-8")
    html = (root / "app" / "static" / "index.html").read_text(encoding="utf-8")
    assert 'class="settings-groups"' in html
    assert "settingsGroups" in javascript
    assert 'class="setting-toggle"' in javascript


def test_settings_are_above_and_scan_plan_is_last() -> None:
    html = (Path(__file__).parents[1] / "app" / "static" / "index.html").read_text(encoding="utf-8")
    assert html.index("自动化与资源") < html.index("实时任务")
    assert html.index("中断临时文件") < html.index("文件与处理计划")
    assert 'id="versionBadge"' in html
