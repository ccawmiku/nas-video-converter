from pathlib import Path


def test_live_progress_uses_sse_job_id_without_api_roundtrip() -> None:
    javascript = (Path(__file__).parents[1] / "app" / "static" / "app.js").read_text(encoding="utf-8")
    assert 'addEventListener("progress"' in javascript
    assert "id=progress.job_id" in javascript
    assert "renderProgress(job)" in javascript
    assert "terminalJobStates.has(prior.state)" in javascript
    assert 'events.onopen=()=>{$("#connection").textContent="SSE 已连接";loadHistory()' in javascript


def test_actual_backend_is_visible_in_live_progress() -> None:
    root = Path(__file__).parents[1]
    javascript = (root / "app" / "static" / "app.js").read_text(encoding="utf-8")
    html = (root / "app" / "static" / "index.html").read_text(encoding="utf-8")
    assert 'id="hardwareBadge"' in html
    assert 'id="backend"' in html
    assert "backendLabels" in javascript


def test_settings_are_grouped_and_switches_use_native_modern_component() -> None:
    root = Path(__file__).parents[1]
    javascript = (root / "app" / "static" / "app.js").read_text(encoding="utf-8")
    html = (root / "app" / "static" / "index.html").read_text(encoding="utf-8")
    assert 'class="settings-groups"' in html
    assert "settingsGroups" in javascript
    assert 'class="setting-toggle"' in javascript
    assert 'class="modern-switch"' in javascript
    assert 'role="switch"' in javascript
    assert "switch-control" not in javascript


def test_live_task_is_above_settings_and_home_ends_with_compact_links() -> None:
    html = (Path(__file__).parents[1] / "app" / "static" / "index.html").read_text(encoding="utf-8")
    assert html.index("实时任务") < html.index("自动化与资源")
    assert html.index("自动化与资源") < html.index("文件与记录")
    assert 'id="versionBadge"' in html


def test_log_levels_use_semantic_color_classes() -> None:
    root = Path(__file__).parents[1]
    javascript = (root / "app" / "static" / "records.js").read_text(encoding="utf-8")
    stylesheet = (root / "app" / "static" / "styles.css").read_text(encoding="utf-8")
    assert 'log-${level}' in javascript
    assert ".log.log-info" in stylesheet
    assert ".log.log-warning" in stylesheet
    assert ".log.log-error" in stylesheet


def test_long_lists_are_moved_from_home_to_records_page() -> None:
    root = Path(__file__).parents[1]
    home = (root / "app" / "static" / "index.html").read_text(encoding="utf-8")
    records = (root / "app" / "static" / "records.html").read_text(encoding="utf-8")
    files = (root / "app" / "static" / "files.html").read_text(encoding="utf-8")
    files_javascript = (root / "app" / "static" / "files.js").read_text(encoding="utf-8")
    assert 'href="/files"' in home
    assert 'href="/records?view=history"' in home
    assert 'href="/records?view=logs"' in home
    assert 'href="/records?view=conversions"' in home
    assert 'href="/records?view=recovery"' in home
    for element_id in ('id="history"', 'id="logs"', 'id="conversionRows"', 'id="recovery"', 'id="fileRows"'):
        assert element_id not in home
    for element_id in ('id="recordsHistory"', 'id="recordsLogs"', 'id="recordsConversionRows"', 'id="recordsRecovery"'):
        assert element_id in records
    assert 'id="fileRows"' in files
    assert 'id="previousPage"' in files
    assert "pageSize:100" in files_javascript
