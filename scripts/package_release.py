"""Verify a clean application ZIP, then publish without discarding user files."""
from __future__ import annotations

import argparse
import hashlib
import importlib.metadata as metadata
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import sys
import tempfile
from typing import Callable
import uuid
import zipfile

try:
    from .verify_portable import verify_archive, validate_members
    from .publish_in_place import publish_in_place, recover_pending_publication
except ImportError:
    from verify_portable import verify_archive, validate_members
    from publish_in_place import publish_in_place, recover_pending_publication


ROOT = Path(__file__).resolve().parents[1]
_RUNTIME_ROOTS = {"❤.exe", "_internal", "使用说明.txt", "第三方许可", "release-files.json"}


def _inside(path: Path, root: Path) -> Path:
    root = root.resolve()
    resolved = path.resolve()
    if resolved == root or not resolved.is_relative_to(root):
        raise ValueError("发布操作路径超出本项目范围。")
    for part in (path, *path.parents):
        if part == root or part == part.parent:
            break
        if part.is_symlink() or (hasattr(part, "is_junction") and part.is_junction()):
            raise ValueError("发布目录不能使用符号链接或目录联接。")
    return resolved


def _move(source: Path, destination: Path, root: Path) -> None:
    source = _inside(source, root)
    destination = _inside(destination, root)
    if destination.exists():
        raise ValueError("发布目标已存在，为保护文件已停止操作。")
    destination.parent.mkdir(parents=True, exist_ok=True)
    os.replace(source, destination)


def prepare_documents(app: Path, root: Path = ROOT) -> None:
    app = _inside(app, root)
    shutil.copyfile(root / "使用说明.txt", app / "使用说明.txt")
    licenses = app / "第三方许可"
    licenses.mkdir(exist_ok=True)
    for name in ("PySide6-Essentials", "shiboken6", "openpyxl", "xlrd", "xlwt", "pyinstaller", "cryptography", "cffi"):
        dist = metadata.distribution(name)
        for item in dist.files or []:
            if (any(word in str(item).lower() for word in ("license", "copying", "copyright"))
                    and Path(item).suffix.lower() not in (".exe", ".dll", ".pyd", ".pyc")):
                source = Path(dist.locate_file(item))
                if source.is_file():
                    # Keep notices identifiable without repeating long wheel
                    # metadata paths inside every client update transaction.
                    identifier = hashlib.sha256(str(item).encode('utf-8')).hexdigest()[:12]
                    destination = licenses / name / (identifier + '-' + Path(item).name)
                    _inside(destination, root)
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(source, destination)
    python_license = Path(sys.base_prefix) / "LICENSE.txt"
    if python_license.is_file():
        shutil.copyfile(python_license, licenses / "Python-LICENSE.txt")
    (licenses / "说明.txt").write_text(
        "本软件使用 Python、PySide6/Qt、openpyxl、xlrd、xlwt、cryptography、cffi，使用 PyInstaller 打包。\n"
        "PySide6/Qt 以动态库方式随包提供，适用许可见本目录。\n"
        "Qt 官方源代码及许可：https://www.qt.io/licensing/\n"
        "Python 许可另见 _internal/python*.dll 及 https://docs.python.org/3/license.html\n",
        encoding="utf-8")


def prepare_file_manifest(app: Path, root: Path = ROOT) -> None:
    """Describe only known bundled files; user data never becomes program-owned."""
    app = _inside(app, root)
    version_file = root / "xiaoyu" / "__init__.py"
    import ast
    version = None
    for statement in ast.parse(version_file.read_text(encoding="utf-8")).body:
        if (isinstance(statement, ast.Assign)
                and any(isinstance(target, ast.Name) and target.id == "__version__"
                        for target in statement.targets)):
            version = ast.literal_eval(statement.value)
    if not isinstance(version, str):
        raise ValueError("无法读取软件版本号。")
    files = {}
    for source in sorted(app.rglob("*")):
        _inside(source, root)
        relative = source.relative_to(app)
        if (source.is_file() and relative.parts[0] in _RUNTIME_ROOTS
                and relative.as_posix() != "release-files.json"
                and "data" not in (part.casefold() for part in relative.parts)):
            with source.open("rb") as stream:
                files[relative.as_posix()] = hashlib.file_digest(stream, "sha256").hexdigest()
    manifest = {"schema": 1, "app": "xiaoyu-voucher", "version": version,
                "data_schema": 1, "files": files}
    (app / "release-files.json").write_text(
        json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2), encoding="utf-8")


def _old_manifest(archive: Path) -> set[str] | None:
    if not archive.exists():
        return None
    try:
        with zipfile.ZipFile(archive) as bundle:
            validate_members(bundle)
            return {str(PurePosixPath(name).relative_to("❤")).casefold()
                    for name in bundle.namelist() if not name.endswith("/")}
    except (OSError, ValueError, zipfile.BadZipFile) as exc:
        raise ValueError("旧交付包无法读取，不能可靠识别旧程序文件；原产物已保留。") from exc


def _copy_user_files(old: Path, staged: Path, manifest: set[str] | None, root: Path) -> None:
    """Retain data and all files not proven to belong to the previous program."""
    if not old.exists():
        return
    if manifest is None:
        raise ValueError("缺少旧交付包的程序清单；为保护现有文件，请先恢复旧交付包后发布。")
    for source in sorted(old.rglob("*")):
        _inside(source, root)
        relative = source.relative_to(old)
        name = relative.as_posix().casefold()
        if source.is_dir():
            if not any(source.iterdir()) and not any(item.startswith(name + "/") for item in manifest):
                destination = staged / relative
                _inside(destination, root)
                destination.mkdir(parents=True, exist_ok=True)
            continue
        if not source.is_file():
            raise ValueError("程序目录含有无法安全迁移的特殊文件，请先检查后重试。")
        if relative.parts[0].casefold() != "data" and name in manifest:
            continue
        destination = _inside(staged / relative, root)
        if destination.exists():
            raise ValueError(f"用户文件与新程序文件重名，已停止发布并保留原文件：{relative}")
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)


def _make_archive(app: Path, target: Path, root: Path, known_files: set[str] | None = None) -> None:
    if known_files is None and (app / 'release-files.json').is_file():
        ownership = json.loads((app / 'release-files.json').read_text(encoding='utf-8'))
        known_files = {name.casefold() for name in ownership['files']} | {'release-files.json'}
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as bundle:
        for source in sorted(app.rglob("*")):
            _inside(source, root)
            relative = source.relative_to(app)
            if known_files is not None and relative.as_posix().casefold() not in known_files:
                continue
            # The runtime directories and notices are reserved for application files.
            if (source.is_file() and relative.parts[0] in _RUNTIME_ROOTS
                    and "data" not in (part.casefold() for part in relative.parts)):
                bundle.write(source, PurePosixPath("❤", *relative.parts))


def _retire_legacy_archives(release: Path, backup: Path, root: Path) -> None:
    for old in release.glob("给小雨的软件*_Windows免安装.zip"):
        if not old.is_file() or old.is_symlink():
            continue
        try:
            with zipfile.ZipFile(old) as bundle:
                names = [PurePosixPath(name) for name in bundle.namelist()]
                roots = {path.parts[0] for path in names if path.parts}
                if len(roots) != 1:
                    continue
                name = next(iter(roots))
                if (not name.startswith("给小雨的软件")
                        or PurePosixPath(name, "使用说明.txt") not in names
                        or not any(path.parent == PurePosixPath(name)
                                   and path.name.startswith("给小雨的软件")
                                   and path.suffix == ".exe" for path in names)):
                    continue
            _move(old, backup / "retired-packages" / old.name, root)
        except (OSError, ValueError, zipfile.BadZipFile):
            continue


def publish_release(app_dir: Path, *, root: Path = ROOT,
                    verifier: Callable[[Path], object] | None = None,
                    fault: Callable[[str], None] | None = None) -> Path:
    root = root.resolve()
    recover_pending_publication(root, _inside)
    app = _inside(Path(app_dir), root)
    if app.name != "❤" or not (app / "❤.exe").is_file():
        raise ValueError("请选择本项目中已构建的 ❤ 程序目录。")
    formal = _inside(root / "dist" / "❤", root)
    if app != formal and (app.is_relative_to(formal) or formal.is_relative_to(app)):
        raise ValueError("暂存目录与正式程序目录不能互相包含。")
    release = _inside(root / "release", root)
    release.mkdir(parents=True, exist_ok=True)
    destination = _inside(release / "❤.zip", root)
    descriptor, filename = tempfile.mkstemp(prefix=".portable-", suffix=".tmp", dir=release)
    os.close(descriptor)
    temporary = _inside(Path(filename), root)
    backup = _inside(root / "outputs" / "release-backups" / uuid.uuid4().hex, root)
    old_moved = False
    new_moved = False
    try:
        known_files = _old_manifest(destination) if app == formal else None
        if app == formal and known_files is None:
            raise ValueError("缺少旧交付包的程序清单，不能从已使用的正式目录重新打包。")
        _make_archive(app, temporary, root, known_files)
        (verifier or verify_archive)(temporary)
        # Validation precedes every mutation of the official artifacts.
        manifest = _old_manifest(destination) if app != formal else None
        if app != formal:
            _copy_user_files(formal, app, manifest, root)
        backup.mkdir(parents=True, exist_ok=True)
        if destination.exists():
            shutil.copy2(destination, backup / "❤.zip")
        if app != formal:
            if formal.exists():
                try:
                    _move(formal, backup / "❤", root)
                except PermissionError:
                    if not formal.is_dir() or (backup / "❤").exists():
                        raise
                    publish_in_place(formal, app, backup, destination, temporary,
                                     manifest, root, _inside, fault=fault)
                    _retire_legacy_archives(release, backup, root)
                    return destination
                else:
                    old_moved = True
            _move(app, formal, root)
            new_moved = True
        os.replace(temporary, destination)
    except BaseException:
        if new_moved:
            _move(formal, backup / "unpublished" / "❤", root)
        if old_moved:
            _move(backup / "❤", formal, root)
        raise
    finally:
        if temporary.exists():
            _inside(temporary, root).unlink()
    _retire_legacy_archives(release, backup, root)
    return destination


def clean_staged_copy(app: Path, root: Path = ROOT) -> Path:
    """Retry a staged publish without reclassifying previously migrated data."""
    app = _inside(app, root)
    manifest_path = app / 'release-files.json'
    if not manifest_path.is_file():
        return app
    manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
    if (manifest.get('schema') != 1 or manifest.get('app') != 'xiaoyu-voucher'
            or not isinstance(manifest.get('files'), dict)):
        raise ValueError('暂存目录中的程序清单无效，请重新构建。')
    destination = _inside(root / 'outputs' / 'build-staging' / uuid.uuid4().hex / '❤', root)
    destination.mkdir(parents=True)
    for name, digest in manifest['files'].items():
        relative = PurePosixPath(name)
        if (relative.is_absolute() or '..' in relative.parts or '\\' in name or ':' in name
                or relative.parts[0] not in _RUNTIME_ROOTS
                or 'data' in (part.casefold() for part in relative.parts)
                or name == 'release-files.json'):
            raise ValueError('暂存程序清单中的路径不安全。')
        source = _inside(app.joinpath(*relative.parts), app)
        with source.open('rb') as stream:
            if hashlib.file_digest(stream, 'sha256').hexdigest() != digest:
                raise ValueError('暂存程序文件已改变，请重新构建：' + name)
        target = _inside(destination.joinpath(*relative.parts), destination)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
    return destination


def main() -> None:
    parser = argparse.ArgumentParser(description="校验、备份并发布免安装程序。")
    parser.add_argument("--app-dir", type=Path, default=ROOT / "dist" / "❤",
                        help="已构建的程序目录；用于更新正式目录，受限情况下保留暂存副本。")
    app = _inside(parser.parse_args().app_dir, ROOT)
    if app.name != "❤" or not (app / "❤.exe").is_file():
        raise ValueError("请先构建名为 ❤ 的免安装程序。")
    if app == _inside(ROOT / "dist" / "❤", ROOT):
        raise ValueError("请使用 scripts/build.ps1，或通过 --app-dir 选择干净的构建暂存目录；不能重新认领正式目录中的用户文件。")
    app = clean_staged_copy(app)
    prepare_documents(app)
    # Always generate the ownership list before user files are migrated.
    prepare_file_manifest(app)
    destination = publish_release(app)
    sys.stdout.reconfigure(encoding="utf-8")
    print(destination)
    print(f"{destination.stat().st_size / 1024 / 1024:.1f} MB")


if __name__ == "__main__":
    main()
