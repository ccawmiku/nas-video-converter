from __future__ import annotations

import ctypes
import errno
import os
import stat
import sys
import uuid
from pathlib import Path

from .config import EXCLUDED_DIR_NAMES


class SafetyError(RuntimeError):
    pass


def _common_path(a: Path, b: Path) -> bool:
    try:
        return os.path.commonpath((str(a), str(b))) == str(a)
    except ValueError:
        return False


def ensure_safe_root(root: Path) -> Path:
    root = Path(root)
    if not root.is_absolute():
        raise SafetyError("媒体根目录必须是绝对路径")
    if not root.exists() or not root.is_dir():
        raise SafetyError("媒体根目录不存在")
    if root.is_symlink():
        raise SafetyError("媒体根目录不能是符号链接")
    return root.resolve(strict=True)


def ensure_safe_path(root: Path, candidate: Path, *, must_exist: bool = True) -> Path:
    root_real = ensure_safe_root(root)
    candidate = Path(candidate)
    if not candidate.is_absolute():
        candidate = root_real / candidate
    absolute = Path(os.path.abspath(candidate))
    if not _common_path(root_real, absolute):
        raise SafetyError("路径越出映射根目录")

    current = root_real
    try:
        relative = absolute.relative_to(root_real)
    except ValueError as exc:
        raise SafetyError("路径越出映射根目录") from exc
    for part in relative.parts:
        if part in {"", ".", ".."}:
            raise SafetyError("路径包含目录穿越")
        current = current / part
        if current.exists() or current.is_symlink():
            mode = current.lstat().st_mode
            if stat.S_ISLNK(mode):
                raise SafetyError(f"拒绝符号链接：{current}")
    if must_exist and not absolute.exists():
        raise SafetyError("路径不存在")
    resolved = absolute.resolve(strict=must_exist)
    if not _common_path(root_real, resolved):
        raise SafetyError("真实路径越出映射根目录")
    return resolved


def is_excluded(relative: Path) -> bool:
    return any(part.casefold() in EXCLUDED_DIR_NAMES for part in relative.parts)


def ensure_directory(root: Path, directory: Path) -> Path:
    safe_target = ensure_safe_path(root, directory, must_exist=False)
    safe_target.mkdir(parents=True, exist_ok=True)
    return ensure_safe_path(root, safe_target, must_exist=True)


def _renameat2_no_replace(source: Path, target: Path) -> bool:
    if not sys.platform.startswith("linux"):
        return False
    libc = ctypes.CDLL(None, use_errno=True)
    renameat2 = getattr(libc, "renameat2", None)
    if renameat2 is None:
        return False
    at_fdcwd = -100
    rename_noreplace = 1
    result = renameat2(
        at_fdcwd,
        os.fsencode(source),
        at_fdcwd,
        os.fsencode(target),
        rename_noreplace,
    )
    if result == 0:
        return True
    error = ctypes.get_errno()
    if error in (errno.EEXIST, errno.ENOTEMPTY):
        raise FileExistsError(str(target))
    if error not in (errno.ENOSYS, errno.EINVAL):
        raise OSError(error, os.strerror(error), str(source), str(target))
    return False


def safe_rename(root: Path, source: Path, target: Path) -> None:
    source = ensure_safe_path(root, source, must_exist=True)
    target = ensure_safe_path(root, target, must_exist=False)
    parent = ensure_directory(root, target.parent)
    target = parent / target.name
    if target.exists() or target.is_symlink():
        raise FileExistsError(f"目标已存在，拒绝覆盖：{target}")
    if source.stat().st_dev != parent.stat().st_dev:
        raise SafetyError("拒绝跨文件系统移动；不会采用复制后删除")
    if not _renameat2_no_replace(source, target):
        if target.exists() or target.is_symlink():
            raise FileExistsError(f"目标已存在，拒绝覆盖：{target}")
        os.rename(source, target)


def unique_output_path(root: Path, source: Path) -> Path:
    source = ensure_safe_path(root, source)
    base = source.with_suffix(".mp4")
    if base == source or not base.exists():
        return base
    index = 2
    while True:
        candidate = source.with_name(f"{source.stem} ({index}).mp4")
        ensure_safe_path(root, candidate, must_exist=False)
        if not candidate.exists() and not candidate.is_symlink():
            return candidate
        index += 1


def unique_temporary_path(root: Path, source: Path) -> Path:
    source = ensure_safe_path(root, source)
    for _ in range(100):
        candidate = source.parent / f".{source.stem}.nvc-{uuid.uuid4().hex}.tmp.mp4"
        ensure_safe_path(root, candidate, must_exist=False)
        if not candidate.exists() and not candidate.is_symlink():
            return candidate
    raise SafetyError("无法生成唯一临时文件名")


def unique_preserved_path(root: Path, directory: Path, relative: Path) -> Path:
    base = directory / relative
    ensure_safe_path(root, base, must_exist=False)
    if not base.exists() and not base.is_symlink():
        return base
    index = 2
    while True:
        candidate = base.with_name(f"{base.stem} ({index}){base.suffix}")
        ensure_safe_path(root, candidate, must_exist=False)
        if not candidate.exists() and not candidate.is_symlink():
            return candidate
        index += 1
