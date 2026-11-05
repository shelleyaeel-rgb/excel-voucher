"""Exercise the packaged Windows updater with synthetic data, entirely locally.

The simulated old installation uses the current binary; only its file manifest
version is changed to 0.0.0. This verifies the real signed-update mechanism and
data preservation, not compatibility with a historical application binary.
Nothing is uploaded, and the supplied archive and dist directory are read-only.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import time
import traceback
from unittest.mock import patch
import uuid

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from PyInstaller.archive.readers import CArchiveReader
from openpyxl import Workbook

from scripts.publish_update import make_release
from scripts.update_keys import load_private_key, public_key_text
from xiaoyu import config
from xiaoyu.update_installer import (_absolute, _extract_archive, _io_path, _manifest, _no_links,
                                     _state_dir, _walk, prepare_helper)
from xiaoyu.update_protocol import validate_channel, verify_envelope

QA_ROOT = ROOT / "outputs" / "qa"


def _inside(path: Path, root: Path) -> Path:
    root, path = _absolute(root), _absolute(path)
    if path == root or not path.is_relative_to(root):
        raise ValueError("更新验证操作超出本次 outputs/qa 沙盒范围。")
    return path


def _sha256(path: Path) -> str:
    _no_links(path)
    with _io_path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _write_json(path: Path, value: dict, run: Path) -> None:
    path = _inside(path, run)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def _read_json(path: Path, run: Path) -> dict:
    path = _inside(path, run)
    result = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(result, dict):
        raise ValueError(f"验证报告格式不正确：{path.name}")
    return result


def _embedded_channel(helper: Path) -> dict:
    reader = CArchiveReader(str(helper))
    names = [name for name in reader.toc if name.replace("\\", "/") == "assets/update-channel.json"]
    if len(names) != 1:
        raise ValueError("冻结更新器没有唯一的内嵌更新公钥配置。")
    return validate_channel(json.loads(reader.extract(names[0]).decode("utf-8")))


def _create_synthetic_data(app: Path, external_data: Path, run: Path) -> tuple[dict, dict]:
    _write_json(app / "data" / "settings.json",
                {"company": "更新验证专用合成公司", "options": {"period": "2026-11"}}, run)
    _write_json(app / "data" / "synthetic-private-config.json",
                {"synthetic": True, "text": "合成配置，不包含任何真实凭证、资料或密钥"}, run)
    for folder, label in ((app / "data", "程序目录资料"), (external_data, "外部实际资料目录")):
        folder = _inside(folder, run)
        folder.mkdir(parents=True, exist_ok=True)
        database = _inside(folder / "materials.sqlite3", run)
        with sqlite3.connect(database) as connection:
            connection.execute("CREATE TABLE synthetic_records (id INTEGER PRIMARY KEY, note TEXT, cents INTEGER)")
            connection.execute("INSERT INTO synthetic_records(note,cents) VALUES (?,?)", (label + "：完全合成的验证记录", 1234))
        _write_json(folder / "synthetic-marker.json", {"synthetic": True, "label": label}, run)
    for relative, rows in (
        ("合成源表.xlsx", [["日期", "收款金额", "摘要"], ["2026-11-03", 12.34, "自动更新验证合成业务"]]),
        ("合成导出/凭证.xlsx", [["科目", "借方", "贷方"], ["合成银行", 12.34, 0], ["合成收入", 0, 12.34]]),
    ):
        target = _inside(app / relative, run)
        target.parent.mkdir(parents=True, exist_ok=True)
        book = Workbook()
        for row in rows:
            book.active.append(row)
        book.save(target)
        book.close()
    note = _inside(app / "用户额外文件.txt", run)
    note.write_text("这是更新验证生成的合成用户文件，必须原样保留。", encoding="utf-8")
    _inside(app / "用户空文件夹", run).mkdir()
    deep = _inside(app.joinpath("合成长路径资料", *("资料目录-" + "x" * 60 for _ in range(3)), "结果.txt"), run)
    if len(str(deep)) <= 300:
        raise AssertionError("长路径回归样本应超过 300 字符。")
    _io_path(deep.parent).mkdir(parents=True, exist_ok=True)
    _io_path(deep).write_text("超过 Windows 传统路径上限的合成用户资料。", encoding="utf-8")
    known = set(_manifest(app)["files"]) | {"release-files.json"}
    app_files = {path.relative_to(app).as_posix(): _sha256(path)
                 for path in _walk(app) if _io_path(path).is_file() and path.relative_to(app).as_posix() not in known}
    external_files = {path.relative_to(external_data).as_posix(): _sha256(path)
                      for path in _walk(external_data) if _io_path(path).is_file()}
    return app_files, external_files


def _clean_environment(run: Path) -> dict:
    env = {name: value for name, value in os.environ.items()
           if name.upper() in {"SYSTEMROOT", "WINDIR", "SYSTEMDRIVE", "COMSPEC"}}
    for name in ("TEMP", "TMP", "LOCALAPPDATA", "APPDATA", "USERPROFILE"):
        folder = _inside(run / "environment" / name.lower(), run)
        folder.mkdir(parents=True, exist_ok=True)
        env[name] = str(folder)
    env["PATH"] = str(Path(os.environ.get("SYSTEMROOT", "C:/Windows")) / "System32")
    env["PYINSTALLER_RESET_ENVIRONMENT"] = "1"
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return env


def _stop_owned_process(process: subprocess.Popen | None, *, tree: bool = False) -> None:
    """Stop only a process handle created by this verification invocation."""
    if process is None or process.poll() is not None:
        return
    if tree and os.name == "nt":
        # A onefile bootloader owns a child process. The Popen handle is still
        # live here, so this PID cannot have been reused for a user's program.
        system = Path(os.environ.get("SYSTEMROOT", "C:/Windows")) / "System32"
        subprocess.run([str(system / "taskkill.exe"), "/PID", str(process.pid), "/T", "/F"],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=15,
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    else:
        process.terminate()
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=10)


def _check_files(folder: Path, expected: dict, run: Path) -> None:
    folder = _inside(folder, run)
    for name, digest in expected.items():
        target = _inside(folder / name, run)
        if not _io_path(target).is_file() or _sha256(target) != digest:
            raise AssertionError(f"合成用户资料未原样保留：{target}")


def verify_update(archive: Path, private_key_path: Path, *, timeout: float = 300) -> Path:
    if os.name != "nt":
        raise ValueError("本验证脚本需要 Windows，以运行真实冻结更新器。")
    archive = _absolute(archive)
    original_archive_hash = _sha256(archive)
    run = _inside(QA_ROOT / "update-smoke" / uuid.uuid4().hex, QA_ROOT)
    run.mkdir(parents=True, exist_ok=False)
    report_path = run / "report.json"
    report = {"ok": False, "local_only": True, "archive": str(archive),
              "archive_sha256": original_archive_hash, "run_dir": str(run),
              "synthetic_old_version": "0.0.0",
              "old_binary_note": "Synthetic old version: only release-files.json is changed to 0.0.0; the old binary is the current release binary.",
              "actual_data_mode": "Synthetic external data directory; internal data is also preserved.",
              "private_key_content_logged": False}
    helper_process = parent_process = None
    try:
        key = load_private_key(private_key_path)
        app = _extract_archive(archive, _inside(run / "client", run))
        expected_manifest = (app / "release-files.json").read_bytes()
        manifest = _manifest(app)
        version = manifest["version"]
        report["binary_version"] = version
        if version == "0.0.0":
            raise ValueError("发行包版本必须高于合成旧版 0.0.0。")
        helper_source = _inside(app / "_internal" / "update-helper.exe", run)
        main_channel = validate_channel(_read_json(app / "_internal" / "assets" / "update-channel.json", run))
        helper_channel = _embedded_channel(helper_source)
        if main_channel != helper_channel or helper_channel["public_key"] != public_key_text(key):
            raise ValueError("冻结更新器、主程序和本地签名私钥的公钥不一致。")
        report["embedded_trust_configuration_matches"] = True
        report["helper_sha256"] = _sha256(helper_source)
        candidate = _inside(run / "candidate", run)
        candidate.mkdir()
        copied_archive = _inside(candidate / "❤.zip", run)
        shutil.copy2(archive, copied_archive)
        if _sha256(copied_archive) != original_archive_hash:
            raise ValueError("验证期间发行包发生变化。")
        metadata = {"version": version, "size": copied_archive.stat().st_size, "sha256": original_archive_hash}
        release = make_release(metadata, f"https://example.invalid/local-update-smoke/{version}/package.zip",
                               "本地冻结更新器验证：完全合成资料，不上传。", key)
        verify_envelope(release, helper_channel["public_key"])
        _inside(candidate / "release.json", run).write_bytes(release)
        # Only this sandbox manifest changes. All binaries and their hashes are
        # copied unchanged from the real archive.
        manifest["version"] = "0.0.0"
        _write_json(app / "release-files.json", manifest, run)
        external_data = _inside(run / "synthetic-user-data" / "XiaoYuVoucher", run)
        app_files, external_files = _create_synthetic_data(app, external_data, run)
        report["synthetic_app_file_hashes"] = app_files
        report["synthetic_external_file_hashes"] = external_files
        report["longest_synthetic_user_path_characters"] = max(len(str(app / name)) for name in app_files)
        # Select the copied installation's packaged resources when preparing the
        # external helper. The helper itself executes its frozen code and uses
        # its own embedded trust configuration; no updater logic is replaced.
        with patch.object(config, "resource_path", side_effect=lambda name: app / "_internal" / name):
            command = prepare_helper(candidate, app, external_data, restart=False)
        helper = _inside(Path(command[0]), run)
        if helper.is_relative_to(app) or _sha256(helper) != report["helper_sha256"]:
            raise AssertionError("更新器没有正确复制到程序目录之外。")
        report["helper_path"] = str(helper)
        environment = _clean_environment(run)
        gate = _inside(run / "allow-parent-exit", run)
        parent_code = ("from pathlib import Path; import sys,time; "
                       "deadline=time.monotonic()+45; gate=Path(sys.argv[1]); "
                       "\nwhile not gate.exists() and time.monotonic()<deadline: time.sleep(0.05)\n")
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        parent_process = subprocess.Popen([sys.executable, "-c", parent_code, str(gate)],
                                          cwd=run, env=environment, creationflags=flags,
                                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        command[command.index("--parent-pid") + 1] = str(parent_process.pid)
        if "--restart" in command:
            raise AssertionError("验证不得启动正式业务界面。")
        started = time.monotonic()
        log = _inside(run / "helper-process.log", run)
        with log.open("wb") as output:
            helper_process = subprocess.Popen(command, cwd=helper.parent, env=environment,
                                              creationflags=flags, stdout=output, stderr=subprocess.STDOUT)
            # Keep the simulated parent alive for a visible checkpoint. The
            # release gate makes its exit deterministic and prevents a deadlock
            # from accidentally passing this verifier's own PID to the helper.
            time.sleep(6)
            if parent_process.poll() is not None or helper_process.poll() is not None:
                raise AssertionError("模拟父进程或更新器过早退出，无法验证等待主程序退出。")
            if _read_json(app / "release-files.json", run)["version"] != "0.0.0":
                raise AssertionError("主程序尚未退出时，更新器就替换了程序。")
            report["live_parent_checkpoint_seconds"] = round(time.monotonic() - started, 2)
            report["parent_exit_wait_observed"] = True
            gate.write_text("synthetic parent may now exit", encoding="utf-8")
            parent_process.wait(timeout=10)
            state = _inside(_state_dir(app), run)
            deadline = time.monotonic() + timeout
            while helper_process.poll() is None:
                error = state / "last-error.json"
                if error.is_file():
                    details = _read_json(error, run)
                    raise AssertionError(f"冻结更新器报告失败：{details.get('error')}；只会关闭本次验证启动的进程。")
                if time.monotonic() >= deadline:
                    raise subprocess.TimeoutExpired(command, timeout)
                time.sleep(0.2)
            returncode = helper_process.returncode
        report["helper_exit_code"] = returncode
        report["elapsed_seconds"] = round(time.monotonic() - started, 2)
        state = _inside(_state_dir(app), run)
        if returncode != 0:
            error = state / "last-error.json"
            details = _read_json(error, run) if error.is_file() else {"error": "no helper error report"}
            raise AssertionError(f"冻结更新器退出码为 {returncode}：{details.get('error')}")
        result = _read_json(state / "last-result.json", run)
        transaction = _inside(Path(result["transaction_dir"]), run)
        backup = _inside(Path(result["backup_dir"]), run)
        if (app / "release-files.json").read_bytes() != expected_manifest:
            raise AssertionError("更新后的程序清单与原发行包不一致。")
        _manifest(app)
        if _manifest(backup)["version"] != "0.0.0":
            raise AssertionError("旧版备份不对应合成旧安装。")
        _check_files(app, app_files, run)
        _check_files(backup, app_files, run)
        _check_files(external_data, external_files, run)
        _check_files(transaction / "external-data-backup", external_files, run)
        if not (app / "用户空文件夹").is_dir() or not (backup / "用户空文件夹").is_dir():
            raise AssertionError("用户空文件夹未保留。")
        if (state / "pending.json").exists():
            raise AssertionError("更新完成后仍有未提交事务记录。")
        transaction_result = _read_json(transaction / "result.json", run)
        if transaction_result.get("phase") != "committed":
            raise AssertionError("更新事务未提交。")
        for name in ("self-test", "health"):
            probe = _read_json(transaction / (name + ".json"), run)
            if probe.get("ok") is not True or probe.get("version") != version:
                raise AssertionError(f"真实冻结程序的 {name} 未通过。")
            report[name + "_report"] = str(transaction / (name + ".json"))
        if _sha256(archive) != original_archive_hash:
            raise AssertionError("原交付压缩包被更改。")
        report.update(ok=True, installed_version=version, synthetic_data_preserved=True,
                      source_and_exports_preserved=True, external_data_backed_up=True,
                      long_windows_path_preserved=True,
                      old_program_backed_up=True, original_archive_unchanged=True,
                      pending_transaction=False, transaction_dir=str(transaction), backup_dir=str(backup))
    except Exception as exc:
        report["error"] = str(exc)
        report["traceback"] = traceback.format_exc()
        _write_json(report_path, report, run)
        raise RuntimeError(f"冻结更新验证失败；报告：{report_path}；原因：{exc}") from exc
    finally:
        try:
            _stop_owned_process(helper_process, tree=True)
        finally:
            _stop_owned_process(parent_process)
            _write_json(report_path, report, run)
    return report_path


def main() -> int:
    parser = argparse.ArgumentParser(description="用合成资料验证真实冻结更新器，不上传、不更改正式产物。")
    parser.add_argument("--archive", type=Path, default=ROOT / "release" / "❤.zip")
    parser.add_argument("--private-key", type=Path, required=True)
    parser.add_argument("--timeout", type=float, default=300)
    args = parser.parse_args()
    try:
        report = verify_update(args.archive, args.private_key, timeout=args.timeout)
    except Exception as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(f"真实冻结更新器验证通过：{report}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
