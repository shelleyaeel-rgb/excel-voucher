"""Local publisher fallback when Windows refuses to rename the app directory.

Only authenticated-by-the-local-ZIP program paths are changed. The directory,
user data, unknown files, and empty directories are never removed.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import uuid
import zipfile

try:
    from .verify_portable import validate_members
except ImportError:
    from verify_portable import validate_members

_PROGRAM_ROOTS = {"❤.exe", "_internal", "使用说明.txt", "第三方许可", "release-files.json"}


def _hash(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _program_name(name: str) -> str:
    if not isinstance(name, str):
        raise ValueError("发布恢复清单的路径不正确。")
    path = PurePosixPath(name)
    if (not path.parts or path.is_absolute() or ".." in path.parts or "\\" in name
            or ":" in name or path.parts[0].casefold() not in _PROGRAM_ROOTS
            or "data" in (part.casefold() for part in path.parts)
            or str(path) != name or any(part.endswith((" ", ".")) for part in path.parts)):
        raise ValueError("发布恢复清单包含非程序路径。")
    return name


def _zip_inventory(archive: Path, *, old: bool = False) -> dict[str, tuple[str, str]]:
    result = {}
    with zipfile.ZipFile(archive) as bundle:
        validate_members(bundle)
        if bundle.testzip() is not None:
            raise ValueError("发布备份中的压缩包已损坏。")
        for item in bundle.infolist():
            if item.is_dir():
                continue
            name = str(PurePosixPath(item.filename).relative_to("❤"))
            try:
                _program_name(name)
            except ValueError:
                if old:
                    continue  # Historical data/unknown files never become mutable.
                raise
            with bundle.open(item) as stream:
                digest = hashlib.file_digest(stream, "sha256").hexdigest()
            result[name.casefold()] = (name, digest)
    if "❤.exe" not in result:
        raise ValueError("程序文件清单缺少启动程序。")
    return result


def _write_journal(path: Path, record: dict, root: Path, guard):
    path = guard(path, root)
    temporary = guard(path.with_name(".publication-" + uuid.uuid4().hex + ".tmp"), root)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with temporary.open("x", encoding="utf-8") as stream:
            json.dump(record, stream, ensure_ascii=False, sort_keys=True)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            guard(temporary, root).unlink()


def _atomic_copy(source: Path, destination: Path, expected: str, workspace: Path,
                 root: Path, guard, *, expected_current: str | None):
    source, destination = guard(source, root), guard(destination, root)
    workspace = guard(workspace, root)
    workspace.mkdir(parents=True, exist_ok=True)
    temporary = guard(workspace / (uuid.uuid4().hex + ".tmp"), root)
    try:
        shutil.copy2(source, temporary)
        if _hash(temporary) != expected:
            raise ValueError("文件复制校验失败，已停止发布。")
        destination.parent.mkdir(parents=True, exist_ok=True)
        guard(destination, root)
        if _current_hash(destination, root, guard) != expected_current:
            raise ValueError("复制期间程序同名文件出现新内容，已保留该文件。")
        os.replace(temporary, destination)
    finally:
        if temporary.exists():
            guard(temporary, root).unlink()


def _snapshot(directory: Path, root: Path, guard) -> tuple[dict[str, str], set[str]]:
    files, directories = {}, set()
    guard(directory, root)
    for path in sorted(directory.rglob("*")):
        guard(path, root)
        relative = path.relative_to(directory).as_posix()
        if path.is_dir():
            directories.add(relative)
        elif path.is_file():
            files[relative] = _hash(path)
        else:
            raise ValueError("程序目录含特殊文件，不能安全备份。")
    return files, directories


def _copy_full_backup(formal: Path, backup: Path, root: Path, guard) -> dict[str, str]:
    files, directories = _snapshot(formal, root, guard)
    if backup.exists():
        raise ValueError("完整备份目录已经存在，已停止发布。")
    guard(backup, root).mkdir(parents=True)
    for name in sorted(directories):
        guard(backup / name, root).mkdir(parents=True, exist_ok=True)
    for name, digest in files.items():
        source, target = guard(formal / name, root), guard(backup / name, root)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
        if _hash(target) != digest:
            raise ValueError("旧程序及资料完整备份校验失败。")
    if _snapshot(formal, root, guard) != (files, directories):
        raise ValueError("备份期间程序目录发生变化，请关闭软件后重试。")
    if _snapshot(backup, root, guard) != (files, directories):
        raise ValueError("旧程序及资料完整备份不完整。")
    return files


def _current_hash(path: Path, root: Path, guard) -> str | None:
    guard(path, root)
    if not path.exists():
        return None
    if not path.is_file():
        raise ValueError("程序文件路径被目录占用，已保留所有文件。")
    return _hash(path)


def _read_record(root: Path, guard):
    journal = guard(root / "outputs" / "pending-publication.json", root)
    if not journal.exists():
        return None
    if journal.stat().st_size > 16 * 1024 * 1024:
        raise ValueError("发布恢复记录过大。")
    record = json.loads(journal.read_text(encoding="utf-8"))
    if (not isinstance(record, dict) or record.get("schema") != 1
            or record.get("mode") != "program-files"
            or not isinstance(record.get("transaction"), str)
            or not re.fullmatch(r"[0-9a-f]{32}", record["transaction"])
            or record.get("phase") not in {"prepared", "files_verified", "committed"}
            or not isinstance(record.get("files"), dict)):
        raise ValueError("发布恢复记录无法识别，已保留全部备份。")
    backup = guard(root / "outputs" / "release-backups" / record["transaction"], root)
    old_zip, new_zip = guard(backup / "❤.zip", root), guard(backup / "new-package.zip", root)
    if _hash(old_zip) != record.get("old_zip_hash") or _hash(new_zip) != record.get("new_zip_hash"):
        raise ValueError("发布备份压缩包校验失败，已停止自动恢复。")
    old_names, new_names = _zip_inventory(old_zip, old=True), _zip_inventory(new_zip)
    if set(record["files"]) != set(old_names) | set(new_names):
        raise ValueError("发布恢复记录与原始程序清单不一致。")
    for normalized, entry in record["files"].items():
        if not isinstance(entry, dict) or set(entry) != {"path", "old_hash", "new_hash"}:
            raise ValueError("发布恢复文件记录格式不正确。")
        if _program_name(entry["path"]).casefold() != normalized:
            raise ValueError("发布恢复路径与程序清单不一致。")
        old = entry["old_hash"]
        original = guard(backup / "❤" / entry["path"], root)
        if _current_hash(original, root, guard) != old:
            raise ValueError("旧程序文件备份与恢复记录不一致。")
        if old is not None:
            if normalized not in old_names or not isinstance(old, str) or not re.fullmatch(r"[0-9a-f]{64}", old):
                raise ValueError("发布恢复的旧文件信息不正确。")
        expected_new = new_names[normalized][1] if normalized in new_names else None
        if entry["new_hash"] != expected_new:
            raise ValueError("发布恢复的新文件信息不正确。")
    return journal, record, backup


def recover_pending_publication(root: Path, path_guard) -> bool:
    """Recover only uncommitted program writes; never restore user data copies."""
    root = Path(root).resolve()
    loaded = _read_record(root, path_guard)
    if loaded is None:
        return False
    journal, record, backup = loaded
    formal = path_guard(root / "dist" / "❤", root)
    destination = path_guard(root / "release" / "❤.zip", root)
    actual_zip = _current_hash(destination, root, path_guard)
    # ZIP replacement is the commit point. It can precede the final journal write.
    if _is_committed(record, actual_zip):
        path_guard(journal, root).unlink()
        return False
    if actual_zip != record["old_zip_hash"]:
        raise ValueError("交付 ZIP 在发布中断后发生未知变化，已保留全部备份。")
    # Preflight every affected path before changing any: user changes win.
    for entry in record["files"].values():
        current = _current_hash(formal / entry["path"], root, path_guard)
        if current not in {None, entry["old_hash"], entry["new_hash"]}:
            raise ValueError("程序同名文件出现新的内容，未覆盖该文件；请检查发布备份：" + entry["path"])
    workspace = backup / "restore-writes"
    ordered = sorted(record["files"].items(), key=lambda item: (item[0] == "❤.exe", item[0]))
    for _, entry in ordered:
        target = path_guard(formal / entry["path"], root)
        current = _current_hash(target, root, path_guard)
        old = entry["old_hash"]
        if current == old:
            continue
        if current not in {None, entry["new_hash"]}:
            raise ValueError("恢复期间程序文件发生变化，已停止并保留备份。")
        if old is None:
            if current is not None:
                path_guard(target, root).unlink()
        else:
            _atomic_copy(backup / "❤" / entry["path"], target, old, workspace, root, path_guard,
                         expected_current=current)
    path_guard(journal, root).unlink()
    return True


def _is_committed(record: dict, actual_zip: str | None) -> bool:
    return (actual_zip == record["new_zip_hash"]
            and (record["new_zip_hash"] != record["old_zip_hash"]
                 or record["phase"] in {"files_verified", "committed"}))


def publish_in_place(formal: Path, staged: Path, backup: Path, destination_zip: Path,
                     temporary_zip: Path, old_manifest: set[str], root: Path,
                     path_guard, *, fault=None) -> None:
    root = Path(root).resolve()
    formal, staged, backup, destination_zip, temporary_zip = (
        path_guard(Path(path), root) for path in (formal, staged, backup, destination_zip, temporary_zip))
    if (formal != root / "dist" / "❤" or destination_zip != root / "release" / "❤.zip"
            or backup.parent != root / "outputs" / "release-backups"
            or not re.fullmatch(r"[0-9a-f]{32}", backup.name)):
        raise ValueError("原地发布只能用于本项目固定的正式目录。")
    journal = path_guard(root / "outputs" / "pending-publication.json", root)
    if journal.exists():
        raise ValueError("存在尚未恢复的发布记录。")
    checkpoint = fault or (lambda step: None)
    old_zip = path_guard(backup / "❤.zip", root)
    if not old_zip.is_file() or _hash(old_zip) != _hash(destination_zip):
        raise ValueError("缺少完整的旧交付 ZIP 备份。")
    old_names = _zip_inventory(old_zip, old=True)
    if not set(old_names).issubset(old_manifest or set()):
        raise ValueError("旧程序文件清单不可靠。")
    new_names = _zip_inventory(temporary_zip)
    # Check program paths before the full backup, including case collisions.
    for name, _ in old_names.values():
        target = path_guard(formal / name, root)
        if target.exists() and not target.is_file():
            raise ValueError("旧程序文件已变为目录，请先核对后再发布：" + name)
    for normalized, (name, digest) in new_names.items():
        source = path_guard(staged / name, root)
        if not source.is_file() or _hash(source) != digest:
            raise ValueError("暂存程序与已校验 ZIP 不一致：" + name)
        target = path_guard(formal / name, root)
        if normalized not in old_names and target.exists():
            raise ValueError("用户文件与新程序重名，已停止原地发布：" + name)
        if target.exists() and not target.is_file():
            raise ValueError("新程序文件与现有目录冲突：" + name)
        for parent in target.parents:
            if parent == formal:
                break
            if parent.exists() and not parent.is_dir():
                raise ValueError("新程序目录与现有文件冲突：" + name)
    original = _copy_full_backup(formal, path_guard(backup / "❤", root), root, path_guard)
    checkpoint("backed_up")
    saved_new_zip = path_guard(backup / "new-package.zip", root)
    shutil.copy2(temporary_zip, saved_new_zip)
    if _hash(saved_new_zip) != _hash(temporary_zip):
        raise ValueError("新版 ZIP 备份校验失败。")
    files = {}
    original_by_name = {name.casefold(): (name, digest) for name, digest in original.items()}
    for normalized in sorted(set(old_names) | set(new_names)):
        previous = original_by_name.get(normalized) if normalized in old_names else None
        name = previous[0] if previous else (new_names.get(normalized) or old_names[normalized])[0]
        files[normalized] = {"path": name, "old_hash": previous[1] if previous else None,
                             "new_hash": new_names[normalized][1] if normalized in new_names else None}
    record = {"schema": 1, "mode": "program-files", "transaction": backup.name,
              "phase": "prepared", "old_zip_hash": _hash(old_zip),
              "new_zip_hash": _hash(saved_new_zip), "files": files}
    _write_journal(journal, record, root, path_guard)
    try:
        checkpoint("prepared")
        # Avoid offering a launchable old EXE while its runtime files change.
        launcher = files["❤.exe"]
        if _current_hash(formal / launcher["path"], root, path_guard) != launcher["old_hash"]:
            raise ValueError("备份后启动程序发生变化，已停止发布。")
        if launcher["old_hash"] is not None:
            path_guard(formal / launcher["path"], root).unlink()
        checkpoint("launcher_removed")
        ordered = sorted(files.items(), key=lambda item: (item[0] == "❤.exe", item[0]))
        for normalized, entry in ordered:
            target = path_guard(formal / entry["path"], root)
            before = None if normalized == "❤.exe" else entry["old_hash"]
            if _current_hash(target, root, path_guard) != before:
                raise ValueError("备份后程序同名文件出现新内容，已停止发布：" + entry["path"])
            new_hash = entry["new_hash"]
            if new_hash is None:
                if before is not None:
                    path_guard(target, root).unlink()
                    checkpoint("removed:" + entry["path"])
            else:
                source = staged / new_names[normalized][0]
                _atomic_copy(source, target, new_hash, backup / "publish-writes", root, path_guard,
                             expected_current=before)
                checkpoint("written:" + entry["path"])
        for entry in files.values():
            if _current_hash(formal / entry["path"], root, path_guard) != entry["new_hash"]:
                raise ValueError("正式程序文件校验未通过，已停止替换交付包。")
        record["phase"] = "files_verified"
        _write_journal(journal, record, root, path_guard)
        checkpoint("files_verified")
        if _hash(destination_zip) != record["old_zip_hash"]:
            raise ValueError("交付 ZIP 在发布期间发生变化，已停止替换。")
        os.replace(path_guard(temporary_zip, root), path_guard(destination_zip, root))
        checkpoint("zip_committed")
        record["phase"] = "committed"
        _write_journal(journal, record, root, path_guard)
        checkpoint("committed")
        path_guard(journal, root).unlink()
    except Exception:
        # The current ZIP hash decides whether this was before or after commit.
        committed = _is_committed(record, _current_hash(destination_zip, root, path_guard))
        recover_pending_publication(root, path_guard)
        if not committed:
            raise
