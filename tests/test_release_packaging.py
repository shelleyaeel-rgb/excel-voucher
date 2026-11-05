from pathlib import Path
import hashlib
import json
import zipfile

import pytest

from scripts import package_release
from scripts import publish_in_place
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
    write(formal / "_internal" / "私密工资.xlsx", "private salary")
    write(formal / "第三方许可" / "用户资料.txt", "private user note")
    package_release.publish_release(formal, root=root, verifier=verifier)
    verifier(archive)
    assert (formal / "工资.xlsx").is_file()
    with zipfile.ZipFile(archive) as bundle:
        assert '❤/_internal/私密工资.xlsx' not in bundle.namelist()
        assert '❤/第三方许可/用户资料.txt' not in bundle.namelist()
    assert (formal / '_internal' / '私密工资.xlsx').read_text(encoding='utf-8') == 'private salary'


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


def test_manifest_contains_helper_but_not_user_files_or_itself(release_tree):
    root, formal, stage, archive = release_tree
    write(root / 'xiaoyu' / '__init__.py', '__version__ = "0.4.0"\n')
    write(stage / '_internal' / 'update-helper.exe', 'helper')
    write(stage / 'data' / 'finance_ai.json', 'private')
    write(stage / '工资.xlsx', 'source file')
    package_release.prepare_file_manifest(stage, root)
    manifest = json.loads((stage / 'release-files.json').read_text(encoding='utf-8'))
    assert manifest['version'] == '0.4.0'
    assert manifest['files']['_internal/update-helper.exe'] == hashlib.sha256(b'helper').hexdigest()
    assert not {'release-files.json', '工资.xlsx', 'data/finance_ai.json'} & set(manifest['files'])
    output = root / 'test.zip'
    package_release._make_archive(stage, output, root)
    with zipfile.ZipFile(output) as bundle:
        assert '❤/release-files.json' in bundle.namelist()
        assert '❤/data/finance_ai.json' not in bundle.namelist()


def test_retry_staging_does_not_reclassify_migrated_user_files(release_tree):
    root, formal, stage, archive = release_tree
    write(root / 'xiaoyu' / '__init__.py', '__version__ = "0.4.0"\n')
    package_release.prepare_file_manifest(stage, root)
    # A failed publish can already have copied user files into staging.
    write(stage / '_internal' / '工资.xlsx', 'private salary')
    write(stage / 'data' / 'settings.json', 'private config')
    clean = package_release.clean_staged_copy(stage, root)
    assert clean != stage
    assert (clean / '❤.exe').read_text(encoding='utf-8') == 'new exe'
    assert not (clean / '_internal' / '工资.xlsx').exists()
    assert not (clean / 'data').exists()
    assert (stage / '_internal' / '工资.xlsx').read_text(encoding='utf-8') == 'private salary'
    output = root / 'retry.zip'
    package_release._make_archive(stage, output, root)
    with zipfile.ZipFile(output) as bundle:
        assert '❤/_internal/工资.xlsx' not in bundle.namelist()


def test_retry_rejects_changed_program_before_publishing(release_tree):
    root, formal, stage, archive = release_tree
    write(root / 'xiaoyu' / '__init__.py', '__version__ = "0.4.0"\n')
    package_release.prepare_file_manifest(stage, root)
    write(stage / '❤.exe', 'changed after build')
    with pytest.raises(ValueError, match='已改变'):
        package_release.clean_staged_copy(stage, root)
    assert (formal / '❤.exe').read_text(encoding='utf-8') == 'old exe'


def deny_formal_rename(monkeypatch, formal):
    move = package_release._move

    def move_with_locked_directory(source, destination, root):
        if Path(source) == formal:
            raise PermissionError(5, 'directory rename denied')
        return move(source, destination, root)

    monkeypatch.setattr(package_release, '_move', move_with_locked_directory)


def assert_original_program(formal, archive, previous):
    assert (formal / '❤.exe').read_text(encoding='utf-8') == 'old exe'
    assert (formal / '_internal' / 'old.dll').read_text(encoding='utf-8') == 'old runtime'
    assert not (formal / '_internal' / 'new.dll').exists()
    assert archive.read_bytes() == previous
    assert (formal / 'data' / 'finance_ai.json').read_text(encoding='utf-8') == 'private encrypted config'
    assert (formal / '工资.xlsx').read_text(encoding='utf-8') == 'user source'


def test_in_place_fallback_keeps_directory_user_files_and_complete_backup(release_tree, monkeypatch):
    root, formal, stage, archive = release_tree
    previous = archive.read_bytes()
    write(formal / '_internal' / '用户资料.xlsx', 'private runtime extra')
    write(formal / '第三方许可' / '用户笔记.txt', 'private license extra')
    deny_formal_rename(monkeypatch, formal)
    result = package_release.publish_release(stage, root=root, verifier=verifier)
    assert result == archive
    verifier(archive)
    assert (formal / '❤.exe').read_text(encoding='utf-8') == 'new exe'
    assert (formal / '_internal' / 'new.dll').read_text(encoding='utf-8') == 'new runtime'
    assert not (formal / '_internal' / 'old.dll').exists()
    assert (formal / '_internal' / '用户资料.xlsx').read_text(encoding='utf-8') == 'private runtime extra'
    assert (formal / '第三方许可' / '用户笔记.txt').read_text(encoding='utf-8') == 'private license extra'
    assert (formal / 'empty user folder').is_dir()
    assert (formal / 'data' / 'finance_ai.json').read_text(encoding='utf-8') == 'private encrypted config'
    backups = list((root / 'outputs' / 'release-backups').iterdir())
    assert len(backups) == 1
    assert (backups[0] / '❤.zip').read_bytes() == previous
    assert (backups[0] / '❤' / '❤.exe').read_text(encoding='utf-8') == 'old exe'
    assert (backups[0] / '❤' / '_internal' / '用户资料.xlsx').read_text(encoding='utf-8') == 'private runtime extra'
    assert not (root / 'outputs' / 'pending-publication.json').exists()
    with zipfile.ZipFile(archive) as bundle:
        assert not any('用户' in name or 'data' in name for name in bundle.namelist())


@pytest.mark.parametrize('step', ['backed_up', 'prepared', 'launcher_removed',
    'written:_internal/new.dll', 'removed:_internal/old.dll', 'written:使用说明.txt',
    'written:❤.exe', 'files_verified'])
def test_in_place_exception_restores_old_program_and_zip(release_tree, monkeypatch, step):
    root, formal, stage, archive = release_tree
    previous = archive.read_bytes()
    deny_formal_rename(monkeypatch, formal)

    def fault(value):
        if value == step:
            raise OSError('injected failure: ' + value)

    with pytest.raises(OSError, match='injected failure'):
        package_release.publish_release(stage, root=root, verifier=verifier, fault=fault)
    assert_original_program(formal, archive, previous)
    assert not (root / 'outputs' / 'pending-publication.json').exists()


class PublicationCrash(BaseException):
    pass


@pytest.mark.parametrize('step', ['prepared', 'launcher_removed', 'written:_internal/new.dll',
    'removed:_internal/old.dll', 'written:使用说明.txt', 'written:❤.exe', 'files_verified'])
def test_in_place_crash_recovers_only_program_files_and_is_idempotent(release_tree, monkeypatch, step):
    root, formal, stage, archive = release_tree
    previous = archive.read_bytes()
    deny_formal_rename(monkeypatch, formal)

    def fault(value):
        if value == step:
            raise PublicationCrash()

    with pytest.raises(PublicationCrash):
        package_release.publish_release(stage, root=root, verifier=verifier, fault=fault)
    assert (root / 'outputs' / 'pending-publication.json').is_file()
    write(formal / 'data' / 'new-user-data.json', 'created after crash')
    write(formal / '_internal' / 'new-user-file.txt', 'new user runtime file')
    assert publish_in_place.recover_pending_publication(root, package_release._inside) is True
    assert_original_program(formal, archive, previous)
    assert (formal / 'data' / 'new-user-data.json').read_text(encoding='utf-8') == 'created after crash'
    assert (formal / '_internal' / 'new-user-file.txt').read_text(encoding='utf-8') == 'new user runtime file'
    assert publish_in_place.recover_pending_publication(root, package_release._inside) is False


@pytest.mark.parametrize('step', ['zip_committed', 'committed'])
@pytest.mark.parametrize('crash', [True, False])
def test_zip_commit_is_not_rolled_back_even_without_final_journal(release_tree, monkeypatch, step, crash):
    root, formal, stage, archive = release_tree
    deny_formal_rename(monkeypatch, formal)

    def fault(value):
        if value == step:
            raise PublicationCrash() if crash else OSError('late failure')

    if crash:
        with pytest.raises(PublicationCrash):
            package_release.publish_release(stage, root=root, verifier=verifier, fault=fault)
        assert publish_in_place.recover_pending_publication(root, package_release._inside) is False
    else:
        assert package_release.publish_release(stage, root=root, verifier=verifier, fault=fault) == archive
    assert (formal / '❤.exe').read_text(encoding='utf-8') == 'new exe'
    verifier(archive)
    assert not (root / 'outputs' / 'pending-publication.json').exists()


def test_recovery_preserves_conflicting_user_write_at_program_path(release_tree, monkeypatch):
    root, formal, stage, archive = release_tree
    previous = archive.read_bytes()
    deny_formal_rename(monkeypatch, formal)

    def crash(step):
        if step == 'written:_internal/new.dll':
            raise PublicationCrash()

    with pytest.raises(PublicationCrash):
        package_release.publish_release(stage, root=root, verifier=verifier, fault=crash)
    write(formal / '_internal' / 'new.dll', 'user added different content after crash')
    with pytest.raises(ValueError, match='新的内容'):
        publish_in_place.recover_pending_publication(root, package_release._inside)
    assert (formal / '_internal' / 'new.dll').read_text(encoding='utf-8') == 'user added different content after crash'
    assert archive.read_bytes() == previous
    assert (root / 'outputs' / 'pending-publication.json').exists()


def test_recovery_retries_after_partial_restore_failure(release_tree, monkeypatch):
    root, formal, stage, archive = release_tree
    previous = archive.read_bytes()
    deny_formal_rename(monkeypatch, formal)

    def crash(step):
        if step == 'files_verified':
            raise PublicationCrash()

    with pytest.raises(PublicationCrash):
        package_release.publish_release(stage, root=root, verifier=verifier, fault=crash)
    original = publish_in_place._atomic_copy
    seen = []

    def fail_restore(source, destination, *args, **kwargs):
        seen.append(str(destination))
        if Path(destination).name == '❤.exe':
            raise PermissionError('still in use')
        return original(source, destination, *args, **kwargs)

    monkeypatch.setattr(publish_in_place, '_atomic_copy', fail_restore)
    with pytest.raises(PermissionError):
        publish_in_place.recover_pending_publication(root, package_release._inside)
    assert (root / 'outputs' / 'pending-publication.json').exists()
    monkeypatch.setattr(publish_in_place, '_atomic_copy', original)
    assert publish_in_place.recover_pending_publication(root, package_release._inside)
    assert_original_program(formal, archive, previous)


def test_in_place_backup_failure_never_changes_formal_program(release_tree, monkeypatch):
    root, formal, stage, archive = release_tree
    previous = archive.read_bytes()
    deny_formal_rename(monkeypatch, formal)
    copy = publish_in_place.shutil.copy2

    def fail_backup(source, destination, *args, **kwargs):
        if Path(source) == formal / 'data' / 'finance_ai.json' and 'release-backups' in str(destination):
            raise OSError('backup disk full')
        return copy(source, destination, *args, **kwargs)

    monkeypatch.setattr(publish_in_place.shutil, 'copy2', fail_backup)
    with pytest.raises(OSError, match='backup disk full'):
        package_release.publish_release(stage, root=root, verifier=verifier)
    assert_original_program(formal, archive, previous)
    assert not (root / 'outputs' / 'pending-publication.json').exists()


def test_in_place_zip_replace_failure_restores_files(release_tree, monkeypatch):
    root, formal, stage, archive = release_tree
    previous = archive.read_bytes()
    deny_formal_rename(monkeypatch, formal)
    replace = publish_in_place.os.replace

    def fail_zip(source, destination):
        if Path(destination) == archive:
            raise PermissionError('ZIP locked')
        return replace(source, destination)

    monkeypatch.setattr(publish_in_place.os, 'replace', fail_zip)
    with pytest.raises(PermissionError, match='ZIP locked'):
        package_release.publish_release(stage, root=root, verifier=verifier)
    assert_original_program(formal, archive, previous)


def test_recovery_rejects_journal_path_tampering_without_touching_user_data(release_tree, monkeypatch):
    root, formal, stage, archive = release_tree
    deny_formal_rename(monkeypatch, formal)

    def crash(step):
        if step == 'prepared':
            raise PublicationCrash()

    with pytest.raises(PublicationCrash):
        package_release.publish_release(stage, root=root, verifier=verifier, fault=crash)
    journal = root / 'outputs' / 'pending-publication.json'
    record = json.loads(journal.read_text(encoding='utf-8'))
    record['files']['❤.exe']['path'] = 'data/finance_ai.json'
    journal.write_text(json.dumps(record), encoding='utf-8')
    with pytest.raises(ValueError, match='非程序路径'):
        publish_in_place.recover_pending_publication(root, package_release._inside)
    assert (formal / 'data' / 'finance_ai.json').read_text(encoding='utf-8') == 'private encrypted config'


def test_new_publication_recovers_previous_crash_before_starting(release_tree, monkeypatch):
    root, formal, stage, archive = release_tree
    deny_formal_rename(monkeypatch, formal)

    def crash(step):
        if step == 'written:_internal/new.dll':
            raise PublicationCrash()

    with pytest.raises(PublicationCrash):
        package_release.publish_release(stage, root=root, verifier=verifier, fault=crash)
    # A fresh stage has no previously migrated user extras.
    retry = root / 'outputs' / 'retry' / '❤'
    write(retry / '❤.exe', 'new exe')
    write(retry / '_internal' / 'new.dll', 'new runtime')
    write(retry / '使用说明.txt', 'instructions')
    assert package_release.publish_release(retry, root=root, verifier=verifier) == archive
    verifier(archive)
    assert not (root / 'outputs' / 'pending-publication.json').exists()


def test_historical_zip_data_is_never_program_owned_in_fallback(release_tree, monkeypatch):
    root, formal, stage, archive = release_tree
    with zipfile.ZipFile(archive, 'a') as bundle:
        bundle.write(formal / 'data' / 'finance_ai.json', '❤/data/finance_ai.json')
    deny_formal_rename(monkeypatch, formal)
    package_release.publish_release(stage, root=root, verifier=verifier)
    assert (formal / 'data' / 'finance_ai.json').read_text(encoding='utf-8') == 'private encrypted config'
    with zipfile.ZipFile(archive) as bundle:
        assert '❤/data/finance_ai.json' not in bundle.namelist()


def test_committed_recovery_does_not_touch_later_user_program_edits(release_tree, monkeypatch):
    root, formal, stage, archive = release_tree
    deny_formal_rename(monkeypatch, formal)

    def crash(step):
        if step == 'zip_committed':
            raise PublicationCrash()

    with pytest.raises(PublicationCrash):
        package_release.publish_release(stage, root=root, verifier=verifier, fault=crash)
    write(formal / '_internal' / 'new.dll', 'later deliberate change')
    assert publish_in_place.recover_pending_publication(root, package_release._inside) is False
    assert (formal / '_internal' / 'new.dll').read_text(encoding='utf-8') == 'later deliberate change'
    verifier(archive)


def test_same_zip_hash_does_not_hide_precommit_exception(release_tree, monkeypatch):
    root, formal, stage, archive = release_tree
    package_release._make_archive(stage, archive, root)
    # Both program copies correspond to the identical ZIP at the commit point.
    write(formal / '❤.exe', 'new exe')
    write(formal / '_internal' / 'new.dll', 'new runtime')
    write(formal / '使用说明.txt', 'instructions')
    deny_formal_rename(monkeypatch, formal)

    def fail(step):
        if step == 'launcher_removed':
            raise OSError('same-zip failure')

    with pytest.raises(OSError, match='same-zip failure'):
        package_release.publish_release(stage, root=root, verifier=verifier, fault=fail)
    assert (formal / '❤.exe').read_text(encoding='utf-8') == 'new exe'


def test_user_write_during_atomic_copy_is_not_overwritten(release_tree, monkeypatch):
    root, formal, stage, archive = release_tree
    previous = archive.read_bytes()
    deny_formal_rename(monkeypatch, formal)
    copy = publish_in_place.shutil.copy2

    def write_during_copy(source, destination, *args, **kwargs):
        result = copy(source, destination, *args, **kwargs)
        if Path(source) == stage / '_internal' / 'new.dll' and 'publish-writes' in str(destination):
            write(formal / '_internal' / 'new.dll', 'user write while copying')
        return result

    monkeypatch.setattr(publish_in_place.shutil, 'copy2', write_during_copy)
    with pytest.raises(ValueError, match='新的内容'):
        package_release.publish_release(stage, root=root, verifier=verifier)
    assert (formal / '_internal' / 'new.dll').read_text(encoding='utf-8') == 'user write while copying'
    assert archive.read_bytes() == previous
    assert (root / 'outputs' / 'pending-publication.json').exists()


def test_recovery_rejects_erased_original_hash_in_journal(release_tree, monkeypatch):
    root, formal, stage, archive = release_tree
    deny_formal_rename(monkeypatch, formal)

    def crash(step):
        if step == 'prepared':
            raise PublicationCrash()

    with pytest.raises(PublicationCrash):
        package_release.publish_release(stage, root=root, verifier=verifier, fault=crash)
    journal = root / 'outputs' / 'pending-publication.json'
    record = json.loads(journal.read_text(encoding='utf-8'))
    record['files']['❤.exe']['old_hash'] = None
    journal.write_text(json.dumps(record), encoding='utf-8')
    with pytest.raises(ValueError, match='备份与恢复记录不一致'):
        publish_in_place.recover_pending_publication(root, package_release._inside)
    assert (formal / '❤.exe').read_text(encoding='utf-8') == 'old exe'
