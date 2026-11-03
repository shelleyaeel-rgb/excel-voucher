from pathlib import Path
import zipfile

import pytest

from scripts import package_release
from scripts.verify_portable import validate_members


def write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


@pytest.fixture
def release_tree(tmp_path):
    formal = tmp_path / "dist" / "❤"
    stage = tmp_path / "outputs" / "build-staging" / "new" / "❤"
    archive = tmp_path / "release" / "❤.zip"
    write(formal / "❤.exe", "old exe")
    write(formal / "_internal" / "old.dll", "old runtime")
    write(formal / "data" / "finance_ai.json", "private encrypted config")
    write(formal / "工资.xlsx", "user source")
    write(formal / "导出" / "结果.xlsx", "user output")
    (formal / "empty user folder").mkdir()
    archive.parent.mkdir()
    with zipfile.ZipFile(archive, "w") as bundle:
        bundle.write(formal / "❤.exe", "❤/❤.exe")
        bundle.write(formal / "_internal" / "old.dll", "❤/_internal/old.dll")
    write(stage / "❤.exe", "new exe")
    write(stage / "_internal" / "new.dll", "new runtime")
    write(stage / "使用说明.txt", "instructions")
    return tmp_path, formal, stage, archive


def verifier(path):
    with zipfile.ZipFile(path) as bundle:
        validate_members(bundle)
        assert bundle.testzip() is None
        assert bundle.read("❤/❤.exe") == b"new exe"
        assert not any("data" in name or "工资" in name or "结果" in name for name in bundle.namelist())


def test_publish_preserves_user_files_and_backups_but_never_packages_them(release_tree):
    root, formal, stage, archive = release_tree
    old_archive = archive.read_bytes()
    unrelated = root / "release" / "❤.rar"
    write(unrelated, "leave alone")
    result = package_release.publish_release(stage, root=root, verifier=verifier)
    assert result == archive
    assert (formal / "❤.exe").read_text() == "new exe"
    assert not (formal / "_internal" / "old.dll").exists()
    assert (formal / "data" / "finance_ai.json").read_text() == "private encrypted config"
    assert (formal / "工资.xlsx").read_text() == "user source"
    assert (formal / "导出" / "结果.xlsx").read_text() == "user output"
    assert (formal / "empty user folder").is_dir()
    backups = list((root / "outputs" / "release-backups").iterdir())
    assert len(backups) == 1
    assert (backups[0] / "❤" / "❤.exe").read_text() == "old exe"
    assert (backups[0] / "❤.zip").read_bytes() == old_archive
    assert unrelated.read_text() == "leave alone"
    assert not stage.exists()
    verifier(archive)


def test_verification_failure_does_not_touch_official_artifacts(release_tree):
    root, formal, stage, archive = release_tree
    previous = archive.read_bytes()

    def fail(path):
        raise ValueError("new exe self-test failed")

    with pytest.raises(ValueError, match="self-test"):
        package_release.publish_release(stage, root=root, verifier=fail)
    assert archive.read_bytes() == previous
    assert (formal / "❤.exe").read_text() == "old exe"
    assert not (stage / "data").exists()
    assert not list(archive.parent.glob(".portable-*.tmp"))


@pytest.mark.parametrize("failure", ["promote", "zip"])
def test_publish_failure_rolls_back_formal_and_keeps_old_zip(release_tree, monkeypatch, failure):
    root, formal, stage, archive = release_tree
    previous = archive.read_bytes()
    replace = package_release.os.replace

    def fail_one(source, destination):
        if ((failure == "promote" and Path(source) == stage)
                or (failure == "zip" and Path(destination) == archive)):
            raise OSError("simulated publish failure")
        return replace(source, destination)

    monkeypatch.setattr(package_release.os, "replace", fail_one)
    with pytest.raises(OSError, match="simulated"):
        package_release.publish_release(stage, root=root, verifier=verifier)
    assert archive.read_bytes() == previous
    assert (formal / "❤.exe").read_text() == "old exe"
    assert (formal / "data" / "finance_ai.json").read_text() == "private encrypted config"
    assert (formal / "工资.xlsx").read_text() == "user source"
    assert not list(archive.parent.glob(".portable-*.tmp"))


def test_missing_old_manifest_stops_before_official_changes(release_tree):
    root, formal, stage, archive = release_tree
    archive.unlink()
    with pytest.raises(ValueError, match="缺少旧交付包"):
        package_release.publish_release(stage, root=root, verifier=verifier)
    assert (formal / "❤.exe").read_text() == "old exe"
    assert not archive.exists()


def test_new_install_without_previous_dist_or_zip(tmp_path):
    stage = tmp_path / "outputs" / "new" / "❤"
    write(stage / "❤.exe", "new exe")
    result = package_release.publish_release(stage, root=tmp_path, verifier=verifier)
    assert result.is_file()
    assert (tmp_path / "dist" / "❤" / "❤.exe").read_text() == "new exe"


def test_user_file_collision_stops_instead_of_overwriting(release_tree):
    root, formal, stage, archive = release_tree
    write(formal / "_internal" / "new.dll", "user-owned additional file")
    previous = archive.read_bytes()
    with pytest.raises(ValueError, match="重名"):
        package_release.publish_release(stage, root=root, verifier=verifier)
    assert archive.read_bytes() == previous
    assert (formal / "_internal" / "new.dll").read_text() == "user-owned additional file"


def test_existing_formal_can_be_packaged_without_user_files(release_tree):
    root, formal, stage, archive = release_tree
    write(formal / "❤.exe", "new exe")
    package_release.publish_release(formal, root=root, verifier=verifier)
    verifier(archive)
    assert (formal / "工资.xlsx").is_file()


@pytest.mark.parametrize("bad_name", ["../outside", "❤/../outside", "C:/outside", "❤/x:stream",
                                      "❤\\x", "❤/trailing.", "other/file"])
def test_archive_rejects_windows_path_tricks(tmp_path, bad_name):
    path = tmp_path / "bad.zip"
    with zipfile.ZipFile(path, "w") as bundle:
        bundle.writestr("❤/❤.exe", "exe")
        entry = zipfile.ZipInfo("placeholder")
        entry.filename = bad_name
        bundle.writestr(entry, "bad")
    with zipfile.ZipFile(path) as bundle:
        with pytest.raises(ValueError, match="不安全"):
            validate_members(bundle)


def test_archive_rejects_duplicate_case_insensitive_names(tmp_path):
    path = tmp_path / "bad.zip"
    with zipfile.ZipFile(path, "w") as bundle:
        bundle.writestr("❤/❤.exe", "exe")
        bundle.writestr("❤/a.txt", "first")
        bundle.writestr("❤/A.txt", "second")
    with zipfile.ZipFile(path) as bundle:
        with pytest.raises(ValueError, match="重复"):
            validate_members(bundle)
