from __future__ import annotations

import ast
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.safety import SafetyError, ensure_safe_path, safe_rename, unique_output_path
import app.safety as safety


def test_rejects_traversal(tmp_path: Path) -> None:
    root = tmp_path / "媒体"
    root.mkdir()
    outside = tmp_path / "outside.mkv"
    outside.write_bytes(b"x")
    with pytest.raises(SafetyError, match="越出"):
        ensure_safe_path(root, root / ".." / "outside.mkv")


def test_rejects_symlink(tmp_path: Path) -> None:
    root = tmp_path / "媒体"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    link = root / "link"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("当前环境不允许创建符号链接")
    with pytest.raises(SafetyError, match="符号链接"):
        ensure_safe_path(root, link / "movie.mkv", must_exist=False)


def test_safe_rename_never_overwrites(tmp_path: Path) -> None:
    root = tmp_path / "媒体"
    root.mkdir()
    source = root / "源.mkv"
    target = root / "源.mp4"
    source.write_bytes(b"source")
    target.write_bytes(b"existing")
    with pytest.raises(FileExistsError):
        safe_rename(root, source, target)
    assert source.read_bytes() == b"source"
    assert target.read_bytes() == b"existing"


def test_unique_output_uses_numbered_names(tmp_path: Path) -> None:
    root = tmp_path / "媒体"
    root.mkdir()
    source = root / "示例.mkv"
    source.write_bytes(b"source")
    (root / "示例.mp4").write_bytes(b"one")
    (root / "示例 (2).mp4").write_bytes(b"two")
    assert unique_output_path(root, source).name == "示例 (3).mp4"


def test_cross_filesystem_rename_is_refused_without_copy(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "媒体"
    root.mkdir()
    source = root / "source.mkv"
    source.write_bytes(b"source")
    target = root / "backup" / "source.mkv"
    target.parent.mkdir()
    original_stat = Path.stat

    def fake_stat(path: Path, *args, **kwargs):
        if path == source:
            return SimpleNamespace(st_dev=1)
        if path == target.parent:
            return SimpleNamespace(st_dev=2)
        return original_stat(path, *args, **kwargs)

    monkeypatch.setattr(safety, "ensure_safe_path", lambda _root, path, must_exist=True: Path(path))
    monkeypatch.setattr(safety, "ensure_directory", lambda _root, path: Path(path))
    monkeypatch.setattr(Path, "stat", fake_stat)
    with pytest.raises(SafetyError, match="跨文件系统"):
        safe_rename(root, source, target)
    assert source.exists()
    assert not target.exists()


def test_application_has_no_media_delete_calls() -> None:
    forbidden_attributes = {"unlink", "remove", "rmtree"}
    for path in Path("app").rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                assert node.func.attr not in forbidden_attributes, f"forbidden deletion call in {path}:{node.lineno}"
    shell_text = "\n".join(path.read_text(encoding="utf-8") for path in Path("scripts").rglob("*") if path.is_file())
    assert "Remove-Item" not in shell_text
    assert " rm " not in shell_text
