import base64
import hashlib
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import zipfile

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
import pytest

from xiaoyu import update_installer as installer
from xiaoyu import update_protocol as protocol


@pytest.fixture(autouse=True)
def isolated_lock_directory(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "localappdata"))


def write(path, content):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content.encode("utf-8") if isinstance(content, str) else content)


def make_program(app, version, files):
    for name, content in files.items():
        write(app / name, content)
    manifest = {"schema": 1, "app": "xiaoyu-voucher", "version": version,
                "data_schema": 1,
                "files": {name: hashlib.sha256((app / name).read_bytes()).hexdigest() for name in files}}
    write(app / "release-files.json", json.dumps(manifest, ensure_ascii=False))


def sign_release(candidate, key, **overrides):
    archive = candidate / "❤.zip"
    payload = {"schema": 1, "app": "xiaoyu-voucher", "version": "0.5.0", "data_schema": 1,
               "min_updater": 1, "url": "https://example.com/0.5.0/heart.zip",
               "size": archive.stat().st_size, "sha256": hashlib.sha256(archive.read_bytes()).hexdigest(),
               "notes": "测试更新", "published_at": "2026-11-03T12:00:00+08:00"}
    payload.update(overrides)
    envelope = {"payload": payload,
                "signature": base64.b64encode(key.sign(protocol.canonical_json(payload))).decode("ascii")}
    write(candidate / "release.json", json.dumps(envelope, ensure_ascii=False))


@pytest.fixture
def release_tree(tmp_path, monkeypatch):
    app = tmp_path / "应用程序" / "❤"
    source = tmp_path / "构建目录" / "❤"
    candidate = tmp_path / "下载的更新"
    make_program(app, "0.4.0", {"❤.exe": "old exe", "_internal/old.dll": "old library"})
    write(app / "data" / "materials.sqlite3", "accounting data")
    write(app / "data" / "finance_ai.json", "private settings")
    write(app / "工资.xlsx", "user source")
    write(app / "导出" / "结果.xlsx", "user output")
    (app / "空文件夹").mkdir()
    make_program(source, "0.5.0", {"❤.exe": "new exe", "_internal/new.dll": "new library"})
    candidate.mkdir()
    with zipfile.ZipFile(candidate / "❤.zip", "w", zipfile.ZIP_DEFLATED) as bundle:
        for path in source.rglob("*"):
            if path.is_file():
                bundle.write(path, "❤/" + path.relative_to(source).as_posix())
    key = Ed25519PrivateKey.generate()
    channel = {"manifest_url": "https://example.com/release.json",
               "public_key": base64.b64encode(key.public_key().public_bytes_raw()).decode("ascii")}
    monkeypatch.setattr(protocol, "load_channel", lambda: channel)
    sign_release(candidate, key)
    return app, candidate, key


def passed_probe(app, argument, report, data):
    assert argument in {"--self-test", "--update-health-check"}
    assert not data.is_relative_to(app)
    return {"ok": True, "version": json.loads((app / "release-files.json").read_text(encoding="utf-8"))["version"]}


def assert_old(app):
    assert (app / "❤.exe").read_text() == "old exe"
    assert (app / "_internal" / "old.dll").read_text() == "old library"
    assert (app / "data" / "materials.sqlite3").read_text() == "accounting data"
    assert (app / "工资.xlsx").read_text() == "user source"


def test_install_keeps_all_user_files_and_full_old_program(release_tree):
    app, candidate, _ = release_tree
    with installer.AppLock(app):
        result = installer.install_candidate(candidate, app, app / "data", probe=passed_probe)
    assert (app / "❤.exe").read_text() == "new exe"
    assert not (app / "_internal" / "old.dll").exists()
    assert (app / "data" / "materials.sqlite3").read_text() == "accounting data"
    assert (app / "data" / "finance_ai.json").read_text() == "private settings"
    assert (app / "工资.xlsx").read_text() == "user source"
    assert (app / "导出" / "结果.xlsx").read_text() == "user output"
    assert (app / "空文件夹").is_dir()
    assert_old(Path(result["backup_dir"]))
    assert not (installer._state_dir(app) / "pending.json").exists()
    assert (candidate / "❤.zip").is_file()


def test_external_actual_data_is_backed_up_without_changing_original(release_tree, tmp_path):
    app, candidate, _ = release_tree
    external = tmp_path / "用户目录" / "XiaoYuVoucher"
    write(external / "materials.sqlite3", "external real records")
    write(external / "finance_ai.json", "encrypted key")
    with installer.AppLock(app):
        result = installer.install_candidate(candidate, app, external, probe=passed_probe)
    backup = Path(result["transaction_dir"]) / "external-data-backup"
    assert (backup / "materials.sqlite3").read_text() == "external real records"
    assert (backup / "finance_ai.json").read_text() == "encrypted key"
    assert (external / "materials.sqlite3").read_text() == "external real records"


@pytest.mark.parametrize("problem", ["missing_manifest", "changed_program", "user_collision", "directory_collision"])
def test_unknown_or_conflicting_files_stop_before_mutation(release_tree, problem):
    app, candidate, _ = release_tree
    if problem == "missing_manifest":
        (app / "release-files.json").unlink()
    elif problem == "changed_program":
        write(app / "_internal" / "old.dll", "user modified program file")
    elif problem == "user_collision":
        write(app / "_internal" / "new.dll", "user additional file")
    else:
        (app / "_internal" / "new.dll").mkdir()
        write(app / "_internal" / "new.dll" / "user.xlsx", "records")
    before = {path.relative_to(app).as_posix(): path.read_bytes() for path in app.rglob("*") if path.is_file()}
    with installer.AppLock(app), pytest.raises(installer.UpdateError):
        installer.install_candidate(candidate, app, app / "data", probe=passed_probe)
    assert {path.relative_to(app).as_posix(): path.read_bytes() for path in app.rglob("*") if path.is_file()} == before
    assert not (installer._state_dir(app) / "pending.json").exists()


@pytest.mark.parametrize("phase", ["prepared", "old_moved", "new_moved", "health_checked"])
def test_ordinary_failure_rolls_back_at_each_switch_boundary(release_tree, phase):
    app, candidate, _ = release_tree

    def fault(current):
        if current == phase:
            raise OSError("injected disk failure")

    with installer.AppLock(app), pytest.raises(OSError, match="injected"):
        installer.install_candidate(candidate, app, app / "data", probe=passed_probe, fault=fault)
    assert_old(app)
    assert not (installer._state_dir(app) / "pending.json").exists()


class SimulatedPowerLoss(BaseException):
    pass


@pytest.mark.parametrize("phase", ["prepared", "old_moved", "new_moved", "health_checked", "committed"])
def test_crash_journal_recovers_or_preserves_committed_version(release_tree, phase):
    app, candidate, _ = release_tree

    def crash(current):
        if current == phase:
            raise SimulatedPowerLoss()

    with installer.AppLock(app), pytest.raises(SimulatedPowerLoss):
        installer.install_candidate(candidate, app, app / "data", probe=passed_probe, fault=crash)
    assert (installer._state_dir(app) / "pending.json").is_file()
    with installer.AppLock(app):
        assert installer.recover_pending(app) is (phase != "committed")
        assert installer.recover_pending(app) is False
    if phase == "committed":
        assert (app / "❤.exe").read_text() == "new exe"
    else:
        assert_old(app)
    assert (app / "data" / "materials.sqlite3").read_text() == "accounting data"


def test_recovery_itself_can_resume_after_a_second_crash(release_tree, monkeypatch):
    app, candidate, _ = release_tree

    def crash(current):
        if current == "new_moved":
            raise SimulatedPowerLoss()

    with installer.AppLock(app), pytest.raises(SimulatedPowerLoss):
        installer.install_candidate(candidate, app, app / "data", probe=passed_probe, fault=crash)
    move = installer._move

    def crash_after_moving_new(source, destination):
        move(source, destination)
        if destination.parent.name == "failed":
            raise SimulatedPowerLoss()

    monkeypatch.setattr(installer, "_move", crash_after_moving_new)
    with installer.AppLock(app), pytest.raises(SimulatedPowerLoss):
        installer.recover_pending(app)
    monkeypatch.setattr(installer, "_move", move)
    with installer.AppLock(app):
        installer.recover_pending(app)
    assert_old(app)


@pytest.mark.parametrize("fail_argument", ["--self-test", "--update-health-check"])
def test_probe_failure_preserves_old_data_and_program(release_tree, fail_argument):
    app, candidate, _ = release_tree

    def probe(staged, argument, report, data):
        if argument == fail_argument:
            return {"ok": False, "version": "0.5.0"}
        return passed_probe(staged, argument, report, data)

    with installer.AppLock(app), pytest.raises(installer.UpdateError):
        installer.install_candidate(candidate, app, app / "data", probe=probe)
    assert_old(app)


@pytest.mark.parametrize("problem", ["changed_zip", "wrong_signature", "data_schema", "downgrade"])
def test_signed_candidate_checks_happen_before_execution(release_tree, problem):
    app, candidate, key = release_tree
    if problem == "changed_zip":
        with (candidate / "❤.zip").open("ab") as stream:
            stream.write(b"modified")
    elif problem == "wrong_signature":
        other = Ed25519PrivateKey.generate()
        sign_release(candidate, other)
        write(candidate / "update-channel.json", json.dumps({"public_key": base64.b64encode(other.public_key().public_bytes_raw()).decode()}))
    elif problem == "data_schema":
        sign_release(candidate, key, data_schema=2)
    else:
        sign_release(candidate, key, version="0.3.0")

    def must_not_run(*args):
        pytest.fail("Untrusted candidate was executed")

    with installer.AppLock(app), pytest.raises(ValueError):
        installer.install_candidate(candidate, app, app / "data", probe=must_not_run)
    assert_old(app)


@pytest.mark.parametrize("name", ["../outside", "❤/../outside", "❤/file:stream", "❤\\other",
                                   "❤/CON.txt", "❤/CONIN$", "❤/COM¹.txt", "❤/trailing.", "❤/trailing ", "❤//extra", "❤/./extra",
                                   "❤/a\x00hidden", "❤/data/private.json", "other/file"])
def test_archive_rejects_windows_unsafe_paths_before_extracting(tmp_path, name):
    archive = tmp_path / "bad.zip"
    with zipfile.ZipFile(archive, "w") as bundle:
        bundle.writestr("❤/❤.exe", "exe")
        bundle.writestr("❤/release-files.json", "{}")
        item = zipfile.ZipInfo("safe")
        item.filename = name
        bundle.writestr(item, "bad")
    with pytest.raises(installer.UpdateError):
        installer._extract_archive(archive, tmp_path / "stage")
    assert not (tmp_path / "stage" / "❤" / "❤.exe").exists()


def test_archive_rejects_symlinks_and_expansion_limit(tmp_path, monkeypatch):
    archive = tmp_path / "bad.zip"
    with zipfile.ZipFile(archive, "w") as bundle:
        bundle.writestr("❤/❤.exe", "exe")
        link = zipfile.ZipInfo("❤/link")
        link.create_system = 3
        link.external_attr = (stat.S_IFLNK | 0o777) << 16
        bundle.writestr(link, "../../outside")
    with pytest.raises(installer.UpdateError, match="链接"):
        installer._extract_archive(archive, tmp_path / "stage")
    with zipfile.ZipFile(archive, "w") as bundle:
        bundle.writestr("❤/❤.exe", "large content")
    monkeypatch.setattr(installer, "MAX_EXPANDED_BYTES", 4)
    with pytest.raises(installer.UpdateError, match="大小"):
        installer._extract_archive(archive, tmp_path / "stage2")


def test_application_lock_rejects_other_instances_and_requires_ownership(tmp_path):
    app = tmp_path / "❤"
    app.mkdir()
    with pytest.raises(installer.UpdateError, match="程序锁"):
        installer.recover_pending(app)
    lock = installer.AppLock(app)
    second = installer.AppLock(app)
    assert lock.acquire()
    assert not second.acquire()
    script = ("import sys; from pathlib import Path; from xiaoyu.update_installer import AppLock; "
              "lock=AppLock(Path(sys.argv[1])); sys.exit(0 if lock.acquire() else 9)")
    result = subprocess.run([sys.executable, "-c", script, str(app)], capture_output=True)
    assert result.returncode == 9, result.stderr
    lock.release()
    assert second.acquire()
    second.release()
    result = subprocess.run([sys.executable, "-c", script, str(app)], capture_output=True)
    assert result.returncode == 0, result.stderr


def test_lock_does_not_need_write_access_to_installation_parent(tmp_path, monkeypatch):
    app = tmp_path / "read-only-parent" / "❤"
    app.mkdir(parents=True)

    def must_not_touch_transaction_directory(*unused):
        pytest.fail("Normal startup must not write to the installation parent")

    monkeypatch.setattr(installer, "_state_dir", must_not_touch_transaction_directory)
    with installer.AppLock(app):
        assert not any(app.iterdir())
    assert [path.name for path in app.parent.iterdir()] == ["❤"]
    assert installer._lock_path(app).parent.name == "XiaoYuVoucherUpdateLocks"


def test_helper_changes_inherited_current_directory_before_waiting(release_tree, monkeypatch, tmp_path):
    import update_helper
    app, _, _ = release_tree
    runner = tmp_path / "external-helper"
    runner.mkdir()
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(runner / "update-helper.exe"))
    monkeypatch.chdir(app)
    observed = []
    monkeypatch.setattr(update_helper, "wait_for_parent", lambda pid: observed.append(Path.cwd()))
    assert update_helper.main(["--app-dir", str(app), "--recover", "--parent-pid", "123"]) == 0
    assert observed == [runner]


def test_helper_runs_outside_target_and_survives_missing_app_directory(release_tree, monkeypatch, tmp_path):
    from xiaoyu import config
    app, candidate, _ = release_tree
    source = tmp_path / "bundled" / "update-helper.exe"
    write(source, "signed helper placeholder")
    monkeypatch.setattr(config, "resource_path", lambda name: source)
    command = installer.prepare_helper(candidate, app, app / "data")
    helper = Path(command[0])
    assert helper.is_file() and not helper.is_relative_to(app)
    assert helper.read_text() == "signed helper placeholder"
    assert command[command.index("--parent-pid") + 1] == str(os.getpid())
    assert installer.startup_recovery_command(app) is None

    def crash(current):
        if current == "old_moved":
            raise SimulatedPowerLoss()

    with installer.AppLock(app), pytest.raises(SimulatedPowerLoss):
        installer.install_candidate(candidate, app, app / "data", probe=passed_probe, fault=crash)
    assert not app.exists()
    recovery = installer.startup_recovery_command(app)
    assert recovery[0] == command[0] and "--recover" in recovery
    request = json.loads((helper.parent / "request.json").read_text(encoding="utf-8"))
    assert request["app_dir"] == str(app.resolve())
    assert "public_key" not in request
    with installer.AppLock(app):
        installer.recover_pending(app)
    assert_old(app)


def test_recovery_rejects_journal_path_escape(release_tree, tmp_path):
    app, _, _ = release_tree
    state = installer._state_dir(app)
    write(state / "pending.json", json.dumps({"schema": 1, "app_dir": str(app.resolve()),
                                             "transaction": "../../elsewhere", "phase": "moving_new"}))
    with installer.AppLock(app), pytest.raises(installer.UpdateError, match="无法识别"):
        installer.recover_pending(app)
    assert_old(app)


@pytest.mark.parametrize("rollback", [False, True])
def test_long_windows_paths_survive_extraction_copy_backup_and_recovery(release_tree, rollback):
    app, candidate, key = release_tree
    archive = candidate / "❤.zip"
    long_program = "/".join(["第三方许可", *("library-" + "x" * 55 for _ in range(4)), "LICENSE.txt"])
    long_user = "/".join(["用户资料", *("records-" + "y" * 55 for _ in range(4)), "结果.xlsx"])
    user_path = app / long_user
    assert len(str(user_path)) > 300
    write(installer._io_path(user_path), "synthetic deep user file")
    with zipfile.ZipFile(archive) as bundle:
        entries = {name: bundle.read(name) for name in bundle.namelist()}
    manifest = json.loads(entries["❤/release-files.json"].decode("utf-8"))
    content = b"synthetic deeply nested license"
    manifest["files"][long_program] = hashlib.sha256(content).hexdigest()
    entries["❤/release-files.json"] = json.dumps(manifest, ensure_ascii=False).encode("utf-8")
    entries["❤/" + long_program] = content
    with zipfile.ZipFile(archive, "w") as bundle:
        for name, value in entries.items():
            bundle.writestr(name, value)
    sign_release(candidate, key)

    def fail_after_switch(phase):
        if rollback and phase == "new_moved":
            raise OSError("long-path rollback check")

    with installer.AppLock(app):
        if rollback:
            with pytest.raises(OSError, match="long-path rollback"):
                installer.install_candidate(candidate, app, app / "data", probe=passed_probe, fault=fail_after_switch)
            assert_old(app)
        else:
            result = installer.install_candidate(candidate, app, app / "data", probe=passed_probe)
            assert installer._io_path(app / long_program).read_bytes() == content
            assert installer._io_path(Path(result["backup_dir"]) / long_user).read_text() == "synthetic deep user file"
    assert installer._io_path(user_path).read_text() == "synthetic deep user file"
