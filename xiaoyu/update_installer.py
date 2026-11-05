"""Install verified releases while retaining the old program and every user file.

The lock lives in the user's local application directory; the recovery journal
lives beside the application, never inside the directory being replaced. No
installation or recovery path recursively deletes a directory: old programs,
failed programs, and data backups remain available.
"""
from __future__ import annotations

import ctypes
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import subprocess
import sys
import time
from typing import Callable
import uuid
import zipfile

APP_ID = "xiaoyu-voucher"
DATA_SCHEMA = 1
MAX_PACKAGE_BYTES = 1024 ** 3
MAX_EXPANDED_BYTES = 3 * 1024 ** 3
MAX_ENTRIES = 25000
MAX_FILE_MANIFEST_BYTES = 8 * 1024 ** 2
_HELD_LOCKS: set[str] = set()
_RESERVED_NAMES = {"con", "prn", "aux", "nul", "conin$", "conout$",
                   *(f"com{i}" for i in "123456789¹²³"), *(f"lpt{i}" for i in "123456789¹²³")}


class UpdateError(ValueError):
    """An update cannot be safely completed."""


def _io_path(path: Path) -> Path:
    """Use extended Windows paths for I/O, independent of machine policy.

    Journals, identity hashes and UI text keep ordinary paths. Prefixing only
    filesystem calls also avoids depending on LongPathsEnabled or an EXE's
    manifest, both of which vary across clients.
    """
    if os.name != "nt":
        return path
    name = os.path.abspath(path)
    if name.startswith("\\\\?\\"):
        return Path(name)
    return Path("\\\\?\\UNC\\" + name[2:] if name.startswith("\\\\") else "\\\\?\\" + name)


def _ordinary_path(path: Path) -> Path:
    name = str(path)
    if os.name == "nt":
        if name.startswith("\\\\?\\UNC\\"):
            name = "\\\\" + name[8:]
        elif name.startswith("\\\\?\\"):
            name = name[4:]
    return Path(name)


def _no_links(path: Path) -> None:
    for part in (path, *path.parents):
        try:
            info = _io_path(part).lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise UpdateError(f"更新路径不能包含符号链接或目录联接：{part}")


def _absolute(path: Path) -> Path:
    result = Path(os.path.abspath(path))
    _no_links(result)
    return _ordinary_path(_io_path(result).resolve())


def _key(app_dir: Path) -> str:
    return os.path.normcase(str(_absolute(app_dir)))


def _state_dir(app_dir: Path) -> Path:
    app = _absolute(app_dir)
    if app == app.parent:
        raise UpdateError("不能把磁盘根目录作为程序目录。")
    digest = hashlib.sha256(os.path.normcase(str(app)).encode("utf-8")).hexdigest()[:20]
    state = app.parent / f".xiaoyu-update-{digest}"
    _no_links(state)
    return state


def _lock_path(app_dir: Path) -> Path:
    # Keep this outside XiaoYuVoucher's fallback data directory: a backup of
    # that directory must not attempt to read the actively locked first byte.
    local = Path(os.environ.get("LOCALAPPDATA", str(Path.home() / ".local" / "share")))
    digest = hashlib.sha256(_key(app_dir).encode("utf-8")).hexdigest()
    return _absolute(local) / "XiaoYuVoucherUpdateLocks" / (digest + ".lock")


def _mkdir(path: Path) -> None:
    _no_links(path)
    _io_path(path).mkdir(parents=True, exist_ok=True)


def _write_json(path: Path, value: dict) -> None:
    _mkdir(path.parent)
    _no_links(path)
    temporary = path.with_name(path.name + ".tmp-" + uuid.uuid4().hex)
    with _io_path(temporary).open("x", encoding="utf-8") as stream:
        json.dump(value, stream, ensure_ascii=False, sort_keys=True, indent=2)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(_io_path(temporary), _io_path(path))


def _read_json(path: Path, limit: int) -> dict:
    _no_links(path)
    if _io_path(path).stat().st_size > limit:
        raise UpdateError(f"更新说明文件过大：{path.name}")
    try:
        value = json.loads(_io_path(path).read_text(encoding="utf-8"))
    except (UnicodeError, ValueError) as exc:
        raise UpdateError(f"更新说明文件无法读取：{path.name}") from exc
    if not isinstance(value, dict):
        raise UpdateError("更新说明必须是对象。")
    return value


def _sha256(path: Path) -> str:
    _no_links(path)
    digest = hashlib.sha256()
    with _io_path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 ** 2), b""):
            digest.update(block)
    return digest.hexdigest()


class AppLock:
    """A non-blocking, cross-process lock shared by the app and its updater.

    The persistent lock file is deliberately never unlinked; deleting a locked
    file can allow two processes to lock different files at the same pathname.
    """
    def __init__(self, app_dir: Path):
        self.app_dir = _absolute(app_dir)
        self._key = _key(self.app_dir)
        self._stream = None

    def acquire(self) -> bool:
        if self._stream is not None:
            return True
        if self._key in _HELD_LOCKS:
            return False
        path = _lock_path(self.app_dir)
        _mkdir(path.parent)
        _no_links(path)
        stream = _io_path(path).open("a+b")
        if stream.tell() == 0:
            stream.write(b"\0")
            stream.flush()
        stream.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            stream.close()
            return False
        self._stream = stream
        _HELD_LOCKS.add(self._key)
        return True

    def release(self) -> None:
        if self._stream is None:
            return
        try:
            self._stream.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self._stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(self._stream.fileno(), fcntl.LOCK_UN)
        finally:
            self._stream.close()
            self._stream = None
            _HELD_LOCKS.discard(self._key)

    def __enter__(self):
        if not self.acquire():
            raise UpdateError("软件正在运行或另一项更新尚未结束，请关闭后重试。")
        return self

    def __exit__(self, *unused):
        self.release()


def _require_lock(app_dir: Path) -> None:
    if _key(app_dir) not in _HELD_LOCKS:
        raise UpdateError("更新或恢复前必须先取得程序锁。")


def _relative_name(name: str) -> PurePosixPath:
    if not isinstance(name, str) or not name or "\\" in name or ":" in name:
        raise UpdateError("更新包含有不安全的文件路径。")
    parts = name.split("/")
    if (any(not part or part in {".", ".."} or part.endswith((" ", "."))
            or any(ord(char) < 32 or char in '<>"|?*' for char in part)
            or part.split(".", 1)[0].casefold() in _RESERVED_NAMES for part in parts)
            or PurePosixPath(name).is_absolute()):
        raise UpdateError("更新包含有不安全的文件路径。")
    return PurePosixPath(name)


def _version(value: object) -> tuple[int, int, int]:
    if not isinstance(value, str) or not re.fullmatch(r"(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)", value):
        raise UpdateError("更新版本号格式不正确。")
    return tuple(int(part) for part in value.split("."))


def _manifest(app: Path, *, verify_files: bool = True) -> dict:
    try:
        value = _read_json(app / "release-files.json", MAX_FILE_MANIFEST_BYTES)
    except FileNotFoundError as exc:
        raise UpdateError("缺少旧程序文件清单，无法区分程序和用户资料；请先手动安装支持更新的版本。") from exc
    if (value.get("schema") != 1 or value.get("app") != APP_ID
            or value.get("data_schema") != DATA_SCHEMA):
        raise UpdateError("程序清单或资料格式不兼容，已保留原程序。")
    _version(value.get("version"))
    files = value.get("files")
    if not isinstance(files, dict) or not files or len(files) > MAX_ENTRIES:
        raise UpdateError("程序文件清单不完整。")
    seen = set()
    for name, digest in files.items():
        relative = _relative_name(name)
        normalized = name.casefold()
        if (relative.parts[0].casefold() == "data" or normalized == "release-files.json"
                or normalized in seen or not isinstance(digest, str)
                or not re.fullmatch("[0-9a-f]{64}", digest)):
            raise UpdateError("程序文件清单不安全或包含重复文件。")
        seen.add(normalized)
        if verify_files:
            source = app.joinpath(*relative.parts)
            if not _io_path(source).is_file() or _sha256(source) != digest:
                raise UpdateError(f"程序文件与清单不一致，为保护现有文件已停止：{name}")
    if "❤.exe" not in seen:
        raise UpdateError("程序文件清单缺少启动程序。")
    return value


def _walk(root: Path):
    _no_links(root)
    for parent, directories, filenames in os.walk(_io_path(root), followlinks=False):
        directories.sort()
        filenames.sort()
        for name in [*directories, *filenames]:
            path = _ordinary_path(Path(parent) / name)
            _no_links(path)
            mode = _io_path(path).stat().st_mode
            if not (stat.S_ISDIR(mode) or stat.S_ISREG(mode)):
                raise UpdateError(f"资料中含有无法安全复制的特殊文件：{path}")
            yield path


def _copy_tree(source: Path, destination: Path) -> None:
    _mkdir(destination)
    for path in _walk(source):
        target = destination / path.relative_to(source)
        _no_links(target)
        if _io_path(path).is_dir():
            _mkdir(target)
        else:
            _mkdir(target.parent)
            if _io_path(target).exists():
                raise UpdateError(f"备份目标已存在，已停止覆盖：{target}")
            shutil.copy2(_io_path(path), _io_path(target))


def _copy_user_files(old: Path, staged: Path, manifest: dict) -> None:
    known = {name.casefold() for name in manifest["files"]} | {"release-files.json"}
    # Windows paths are case-insensitive, including during tests on other OSes.
    staged_paths = {path.relative_to(staged).as_posix().casefold(): path
                    for path in _walk(staged)}
    for source in _walk(old):
        relative = source.relative_to(old)
        name = relative.as_posix().casefold()
        if _io_path(source).is_file() and name in known:
            continue
        target = staged / relative
        existing = staged_paths.get(name)
        if _io_path(source).is_dir():
            if existing and not _io_path(existing).is_dir():
                raise UpdateError(f"用户资料与新版程序文件重名：{relative}")
            _mkdir(existing or target)
            continue
        if existing is not None or any(parent.as_posix() in staged_paths and not _io_path(staged_paths[parent.as_posix()]).is_dir()
                                       for parent in PurePosixPath(name).parents):
            raise UpdateError(f"用户资料与新版程序文件重名：{relative}")
        _no_links(target)
        _mkdir(target.parent)
        shutil.copy2(_io_path(source), _io_path(target))


def _extract_archive(archive: Path, destination: Path) -> Path:
    _mkdir(destination)
    total = 0
    paths: dict[str, bool] = {}
    with zipfile.ZipFile(_io_path(archive)) as bundle:
        items = bundle.infolist()
        if len(items) > MAX_ENTRIES:
            raise UpdateError("更新包文件数量超出上限。")
        for item in items:
            raw = item.orig_filename
            path = _relative_name(raw[:-1] if raw.endswith("/") else raw)
            if path.parts[0] != "❤" or (len(path.parts) == 1 and not item.is_dir()):
                raise UpdateError("更新包顶层目录必须为 ❤。")
            kind = (item.external_attr >> 16) & 0o170000
            if kind not in (0, stat.S_IFDIR, stat.S_IFREG) or item.flag_bits & 1:
                raise UpdateError("更新包不能包含链接、特殊文件或加密文件。")
            if len(path.parts) > 1 and path.parts[1].casefold() == "data":
                raise UpdateError("更新包不能包含用户 data 资料。")
            normalized = path.as_posix().casefold()
            if normalized in paths:
                raise UpdateError("更新包存在重复文件路径。")
            paths[normalized] = item.is_dir()
            total += item.file_size
            if item.file_size > MAX_PACKAGE_BYTES or total > MAX_EXPANDED_BYTES:
                raise UpdateError("更新包解压大小超出上限。")
        for name in paths:
            if any(parent.as_posix() in paths and not paths[parent.as_posix()]
                   for parent in PurePosixPath(name).parents):
                raise UpdateError("更新包文件与目录路径冲突。")
        if "❤/❤.exe" not in paths or "❤/release-files.json" not in paths:
            raise UpdateError("更新包缺少启动程序或程序文件清单。")
        for item in items:
            target = destination.joinpath(*PurePosixPath(item.filename).parts)
            _no_links(target)
            if item.is_dir():
                _mkdir(target)
                continue
            _mkdir(target.parent)
            written = 0
            with bundle.open(item) as source, _io_path(target).open("xb") as stream:
                while block := source.read(1024 ** 2):
                    written += len(block)
                    if written > item.file_size:
                        raise UpdateError("更新包解压内容超过声明大小。")
                    stream.write(block)
            if written != item.file_size:
                raise UpdateError("更新包解压内容不完整。")
    app = destination / "❤"
    manifest = _manifest(app)
    actual = {path.relative_to(app).as_posix() for path in _walk(app) if _io_path(path).is_file()}
    if actual != set(manifest["files"]) | {"release-files.json"}:
        raise UpdateError("更新包内容与程序文件清单不一致。")
    return app


def _copy_verified_package(candidate: Path, target: Path, release: dict) -> None:
    source = candidate / "❤.zip"
    _no_links(source)
    size = release.get("size")
    if type(size) is not int or not 0 < size <= MAX_PACKAGE_BYTES or _io_path(source).stat().st_size != size:
        raise UpdateError("更新包大小与签名说明不一致。")
    digest = hashlib.sha256()
    copied = 0
    with _io_path(source).open("rb") as incoming, _io_path(target).open("xb") as outgoing:
        while block := incoming.read(1024 ** 2):
            copied += len(block)
            if copied > size:
                raise UpdateError("更新包大小超过签名说明。")
            digest.update(block)
            outgoing.write(block)
        outgoing.flush()
        os.fsync(outgoing.fileno())
    if copied != size or digest.hexdigest() != release.get("sha256"):
        raise UpdateError("更新包校验失败，原程序已保留。")


def _probe(app: Path, argument: str, report: Path, isolated_data: Path) -> dict:
    _mkdir(isolated_data)
    allowed = {"SYSTEMROOT", "WINDIR", "SYSTEMDRIVE", "COMSPEC"}
    env = {name: value for name, value in os.environ.items() if name.upper() in allowed}
    for name in ("TEMP", "TMP", "LOCALAPPDATA", "APPDATA", "USERPROFILE"):
        env[name] = str(isolated_data)
    env["PATH"] = str(Path(os.environ.get("SYSTEMROOT", "C:/Windows")) / "System32")
    env["XIAOYU_UPDATE_TEST_DATA"] = str(isolated_data)
    env["PYINSTALLER_RESET_ENVIRONMENT"] = "1"
    env["QT_QPA_PLATFORM"] = "offscreen"
    with _io_path(report.with_suffix(".log")).open("wb") as output:
        process = subprocess.run([str(app / "❤.exe"), argument, str(report)], cwd=app,
                                 env=env, timeout=120, stdout=output, stderr=subprocess.STDOUT,
                                 creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    if process.returncode != 0 or not _io_path(report).is_file():
        raise UpdateError("新版程序启动自检未通过，已停止更新。")
    result = _read_json(report, 1024 ** 2)
    if result.get("ok") is not True:
        raise UpdateError("新版程序启动自检未通过，已停止更新。")
    return result


def _move(source: Path, destination: Path) -> None:
    _no_links(source)
    _no_links(destination)
    if _io_path(destination).exists():
        raise UpdateError(f"恢复目标已存在，为保护文件已停止：{destination}")
    _mkdir(destination.parent)
    os.replace(_io_path(source), _io_path(destination))


def _finish(state: Path, transaction: Path, record: dict) -> None:
    _write_json(transaction / "result.json", record)
    pending = state / "pending.json"
    _no_links(pending)
    _io_path(pending).unlink()


def recover_pending(app_dir: Path) -> bool:
    """Roll back an interrupted switch; caller must hold AppLock.

    A committed release is never rolled back. In particular, recovery does not
    overwrite data after the user has started using an already committed build.
    """
    app = _absolute(app_dir)
    _require_lock(app)
    state = _state_dir(app)
    pending = state / "pending.json"
    if not _io_path(pending).exists():
        return False
    record = _read_json(pending, 65536)
    identifier = record.get("transaction")
    if (record.get("schema") != 1 or record.get("app_dir") != str(app)
            or not isinstance(identifier, str) or not re.fullmatch("[0-9a-f]{32}", identifier)
            or record.get("phase") not in {"prepared", "moving_old", "moving_new", "checking", "committed", "rolled_back"}):
        raise UpdateError("更新恢复记录无法识别，已保留全部文件，请使用目录外的更新恢复程序检查。")
    transaction = state / "transactions" / identifier
    _no_links(transaction)
    backup = transaction / "backup" / "❤"
    if record["phase"] in {"committed", "rolled_back"}:
        _finish(state, transaction, record)
        return False
    if _io_path(backup).exists():
        if _sha256(backup / "release-files.json") != record.get("old_manifest_sha256"):
            raise UpdateError("旧程序备份清单异常，已停止自动恢复。")
        if _io_path(app).exists():
            if _sha256(app / "release-files.json") != record.get("new_manifest_sha256"):
                raise UpdateError("程序目录出现未知文件变动，已停止自动恢复并保留备份。")
            _move(app, transaction / "failed" / "❤")
        _move(backup, app)
    elif not _io_path(app).is_dir() or _sha256(app / "release-files.json") != record.get("old_manifest_sha256"):
        raise UpdateError("无法找到完整旧程序，请从目录外的更新备份恢复。")
    record["phase"] = "rolled_back"
    _write_json(pending, record)
    _finish(state, transaction, record)
    return True


def install_candidate(candidate_dir: Path, app_dir: Path, data_path: Path, *,
                      probe: Callable | None = None,
                      fault: Callable[[str], None] | None = None) -> dict:
    """Verify, stage, back up and install a candidate while holding AppLock.

    ``probe`` and ``fault`` are explicit test injection points. A hard crash
    (BaseException/os._exit) intentionally leaves the journal for recovery.
    """
    from .update_protocol import load_channel, verify_envelope
    app, candidate, actual_data = map(_absolute, (app_dir, candidate_dir, data_path))
    _require_lock(app)
    recover_pending(app)
    channel = load_channel()  # Trust only resources embedded in this helper.
    if not channel:
        raise UpdateError("此版本尚未配置受信任的更新来源。")
    manifest_path = candidate / "release.json"
    _no_links(manifest_path)
    if _io_path(manifest_path).stat().st_size > 65536:
        raise UpdateError("更新说明过大。")
    release = verify_envelope(_io_path(manifest_path).read_bytes(), channel["public_key"])
    old_manifest = _manifest(app)
    if release.get("data_schema") != DATA_SCHEMA or _version(release.get("version")) <= _version(old_manifest["version"]):
        raise UpdateError("更新版本或资料格式不兼容，原程序已保留。")
    state = _state_dir(app)
    if actual_data == app or app.is_relative_to(actual_data) or state.is_relative_to(actual_data) or actual_data.is_relative_to(state):
        raise UpdateError("实际资料目录与更新工作目录冲突。")
    identifier = uuid.uuid4().hex
    transaction = state / "transactions" / identifier
    _mkdir(transaction)
    payload = transaction / "❤.zip"
    _copy_verified_package(candidate, payload, release)
    staged = _extract_archive(payload, transaction / "stage")
    new_manifest = _manifest(staged, verify_files=False)
    if new_manifest["version"] != release["version"]:
        raise UpdateError("包内版本与签名说明不一致。")
    run_probe = probe or _probe
    result = run_probe(staged, "--self-test", transaction / "self-test.json", transaction / "probe-data")
    if result.get("ok") is not True or result.get("version") != release["version"]:
        raise UpdateError("新版程序自检结果或版本不正确。")
    # Self-test must not modify any bundled program files.
    _manifest(staged)
    _copy_user_files(app, staged, old_manifest)
    if _io_path(actual_data).exists() and not actual_data.is_relative_to(app):
        if not _io_path(actual_data).is_dir():
            raise UpdateError("实际资料目录不是文件夹。")
        _copy_tree(actual_data, transaction / "external-data-backup")
        _write_json(transaction / "external-data-source.json", {"path": str(actual_data)})
    record = {"schema": 1, "transaction": identifier, "app_dir": str(app),
              "phase": "prepared", "old_version": old_manifest["version"],
              "new_version": release["version"], "data_schema": DATA_SCHEMA,
              "old_manifest_sha256": _sha256(app / "release-files.json"),
              "new_manifest_sha256": _sha256(staged / "release-files.json")}
    pending = state / "pending.json"
    _write_json(pending, record)
    checkpoint = fault or (lambda stage: None)
    try:
        checkpoint("prepared")
        record["phase"] = "moving_old"
        _write_json(pending, record)
        _move(app, transaction / "backup" / "❤")
        checkpoint("old_moved")
        record["phase"] = "moving_new"
        _write_json(pending, record)
        _move(staged, app)
        checkpoint("new_moved")
        record["phase"] = "checking"
        _write_json(pending, record)
        result = run_probe(app, "--update-health-check", transaction / "health.json", transaction / "health-data")
        if result.get("ok") is not True or result.get("version") != release["version"]:
            raise UpdateError("新版程序启动检查未通过。")
        checkpoint("health_checked")
        record["phase"] = "committed"
        _write_json(pending, record)
        checkpoint("committed")
        _finish(state, transaction, record)
    except Exception:
        recover_pending(app)
        raise
    return {"version": release["version"], "backup_dir": str(transaction / "backup" / "❤"),
            "transaction_dir": str(transaction)}


def prepare_helper(candidate_dir: Path, app_dir: Path, data_path: Path, restart: bool = True) -> list[str]:
    """Copy the trusted onefile helper outside the target and return its argv.

    Launching this copied helper without arguments later performs recovery only.
    The request file is routing metadata, never a source of trusted public keys.
    """
    from .config import resource_path
    app, candidate, actual_data = map(_absolute, (app_dir, candidate_dir, data_path))
    source = _absolute(resource_path("update-helper.exe"))
    if not _io_path(source).is_file():
        raise UpdateError("更新器尚未随软件打包，请手动安装本次新版。")
    runner = _state_dir(app) / "helpers" / uuid.uuid4().hex
    _mkdir(runner)
    executable = runner / "update-helper.exe"
    shutil.copy2(_io_path(source), _io_path(executable))
    _write_json(runner / "request.json", {"schema": 1, "app_dir": str(app), "restart": restart})
    _write_json(_state_dir(app) / "recovery-helper.json", {"schema": 1, "helper": runner.name})
    command = [str(executable), "--candidate-dir", str(candidate), "--app-dir", str(app),
               "--data-path", str(actual_data), "--parent-pid", str(os.getpid())]
    if restart:
        command.append("--restart")
    return command


def startup_recovery_command(app_dir: Path) -> list[str] | None:
    """Return an external recovery command; the running GUI must then exit.

    Windows cannot move a program directory while its executable/DLLs are in
    use, so startup detects recovery here instead of restoring itself in place.
    """
    app = _absolute(app_dir)
    state = _state_dir(app)
    if not _io_path(state / "pending.json").exists():
        return None
    try:
        value = _read_json(state / "recovery-helper.json", 65536)
        identifier = value.get("helper")
        if value.get("schema") != 1 or not isinstance(identifier, str) or not re.fullmatch("[0-9a-f]{32}", identifier):
            raise UpdateError("更新恢复入口记录无法识别。")
        helper = state / "helpers" / identifier / "update-helper.exe"
        _no_links(helper)
        if not _io_path(helper).is_file():
            raise UpdateError("目录外的更新恢复程序不存在。")
    except (OSError, ValueError) as exc:
        raise UpdateError(f"上次更新未完成，请从此目录中的备份恢复：{state}") from exc
    return [str(helper), "--recover", "--app-dir", str(app), "--parent-pid", str(os.getpid()), "--restart"]


def wait_for_parent(pid: int, timeout: float = 180) -> None:
    if pid <= 0 or pid == os.getpid():
        raise UpdateError("更新器收到无效的主程序编号。")
    if os.name == "nt":
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
        kernel.OpenProcess.restype = ctypes.c_void_p
        kernel.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
        kernel.WaitForSingleObject.restype = ctypes.c_uint32
        kernel.CloseHandle.argtypes = [ctypes.c_void_p]
        handle = kernel.OpenProcess(0x00100000, False, pid)
        if not handle:
            if ctypes.get_last_error() == 87:  # ERROR_INVALID_PARAMETER: exited.
                return
            raise UpdateError("无法确认主程序是否退出，已停止更新。")
        try:
            if kernel.WaitForSingleObject(handle, int(timeout * 1000)) != 0:
                raise UpdateError("主程序未在等待时间内退出，请关闭软件后重试。")
        finally:
            kernel.CloseHandle(handle)
    else:
        deadline = time.monotonic() + timeout
        while True:
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                return
            if time.monotonic() >= deadline:
                raise UpdateError("主程序未在等待时间内退出，请关闭软件后重试。")
            time.sleep(0.1)


def restart_application(app_dir: Path) -> None:
    app = _absolute(app_dir)
    env = {key: value for key, value in os.environ.items() if not key.startswith("_PYI_")}
    env["PYINSTALLER_RESET_ENVIRONMENT"] = "1"
    env.pop("XIAOYU_UPDATE_TEST_DATA", None)
    subprocess.Popen([str(app / "❤.exe")], cwd=app, env=env,
                     creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
