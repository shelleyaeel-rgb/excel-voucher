"""Real settings-file regressions for saving before an application update."""
from concurrent.futures import ThreadPoolExecutor
import ctypes
from decimal import Decimal
import json
import os
from pathlib import Path
import stat
import threading

import pytest

from xiaoyu import config


@pytest.fixture
def settings_directory(tmp_path, monkeypatch):
    directory = tmp_path / "data"
    monkeypatch.setattr(config, "_TEST_DATA_DIR", directory)
    return directory


def test_save_config_roundtrip_preserves_utf8_and_string_conversion(settings_directory):
    value = {"company": "测试账套", "amount": Decimal("12.30")}

    config.save_config(value)

    target = settings_directory / "settings.json"
    assert config.load_config() == {"company": "测试账套", "amount": "12.30"}
    assert "测试账套" in target.read_text(encoding="utf-8")
    assert list(settings_directory.iterdir()) == [target]


def test_save_ignores_and_preserves_readonly_legacy_temp(settings_directory):
    config.save_config({"company": "原账套"})
    legacy_temp = settings_directory / "settings.tmp"
    legacy_temp.write_bytes(b"leftover settings")
    legacy_temp.chmod(stat.S_IREAD)
    try:
        config.save_config({"company": "新账套"})
        assert config.load_config() == {"company": "新账套"}
        assert legacy_temp.read_bytes() == b"leftover settings"
        assert not legacy_temp.stat().st_mode & stat.S_IWRITE
        assert set(settings_directory.iterdir()) == {
            settings_directory / "settings.json", legacy_temp,
        }
    finally:
        legacy_temp.chmod(stat.S_IREAD | stat.S_IWRITE)


@pytest.mark.skipif(os.name != "nt", reason="Windows file-sharing semantics")
def test_save_ignores_and_preserves_locked_legacy_temp(settings_directory):
    config.save_config({"company": "原账套"})
    legacy_temp = settings_directory / "settings.tmp"
    legacy_temp.write_bytes(b"occupied settings")
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateFileW.argtypes = [
        ctypes.c_wchar_p, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_void_p,
        ctypes.c_ulong, ctypes.c_ulong, ctypes.c_void_p,
    ]
    kernel.CreateFileW.restype = ctypes.c_void_p
    kernel.CloseHandle.argtypes = [ctypes.c_void_p]
    # Open for reading and share only reads, denying writes and replacement.
    handle = kernel.CreateFileW(str(legacy_temp), 0x80000000, 1, None, 3, 0, None)
    assert handle != ctypes.c_void_p(-1).value, ctypes.get_last_error()
    try:
        config.save_config({"company": "新账套"})
        assert config.load_config() == {"company": "新账套"}
        assert legacy_temp.read_bytes() == b"occupied settings"
        assert set(settings_directory.iterdir()) == {
            settings_directory / "settings.json", legacy_temp,
        }
    finally:
        kernel.CloseHandle(handle)


def test_failed_replace_preserves_settings_cleans_temp_and_allows_retry(
        settings_directory, monkeypatch):
    config.save_config({"company": "原账套"})
    target = settings_directory / "settings.json"
    original = target.read_bytes()

    def fail_replace(source, destination):
        assert Path(source).parent == settings_directory
        assert Path(destination) == target
        raise PermissionError("settings file is occupied")

    with monkeypatch.context() as patch:
        patch.setattr(config.os, "replace", fail_replace)
        with pytest.raises(PermissionError, match="occupied"):
            config.save_config({"company": "新账套"})

    assert target.read_bytes() == original
    assert list(settings_directory.iterdir()) == [target]
    config.save_config({"company": "新账套"})
    assert config.load_config() == {"company": "新账套"}
    assert list(settings_directory.iterdir()) == [target]


def test_concurrent_saves_use_distinct_complete_temp_files(settings_directory, monkeypatch):
    config.save_config({"writer": "original"})
    original_replace = config.os.replace
    writers = 4
    barrier = threading.Barrier(writers, timeout=10)
    pending = {}

    def synchronized_replace(source, destination):
        source = Path(source)
        pending[source] = json.loads(source.read_text(encoding="utf-8"))
        barrier.wait()
        original_replace(source, destination)

    monkeypatch.setattr(config.os, "replace", synchronized_replace)
    values = [{"writer": index, "company": f"测试账套{index}"} for index in range(writers)]
    saved = []
    with ThreadPoolExecutor(max_workers=writers) as pool:
        futures = [pool.submit(config.save_config, value) for value in values]
        for value, future in zip(values, futures):
            try:
                future.result()
            except PermissionError:
                # Windows can reject simultaneous replacement of one target.
                assert os.name == "nt"
            else:
                saved.append(value)

    assert len(pending) == writers
    assert sorted(pending.values(), key=lambda value: value["writer"]) == values
    assert all(path.parent == settings_directory for path in pending)
    assert saved
    assert config.load_config() in saved
    assert list(settings_directory.iterdir()) == [settings_directory / "settings.json"]
