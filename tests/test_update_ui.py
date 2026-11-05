"""Offline Qt checks for startup-only updates and safe exit handoff."""
from __future__ import annotations

import os
from pathlib import Path
import threading
import time
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication, QWidget

from xiaoyu import ui, update_ui


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


def pump(app, predicate, seconds=5):
    deadline = time.monotonic() + seconds
    while not predicate() and time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.005)
    app.processEvents()
    assert predicate(), "Update worker did not finish in time"


class OfflineService:
    calls = []
    candidate = None
    failure = False
    block = False
    entered = threading.Event()
    cancelled = threading.Event()

    def __init__(self, channel, version, cache):
        self.channel, self.version, self.cache = channel, version, cache

    def check_and_download(self, cancel, progress):
        self.calls.append((self.channel, self.version, self.cache))
        self.entered.set()
        progress("正在下载更新 25%", 25)
        if self.block:
            assert cancel.wait(3), "Close did not cancel the download"
            self.cancelled.set()
            return self.candidate
        if self.failure:
            raise OSError("https://secret.example/?credential=must-not-display")
        return self.candidate


@pytest.fixture
def controller_factory(app, tmp_path):
    OfflineService.calls = []
    OfflineService.candidate = None
    OfflineService.failure = False
    OfflineService.block = False
    OfflineService.entered = threading.Event()
    OfflineService.cancelled = threading.Event()
    controls, windows = [], []

    def create(**kwargs):
        window = kwargs.pop("window", None)
        if window is None:
            window = QWidget()
            windows.append(window)
        kwargs.setdefault("channel", {"manifest_url": "https://example.invalid/latest.json", "public_key": "test"})
        kwargs.setdefault("portable", True)
        kwargs.setdefault("service_factory", OfflineService)
        kwargs.setdefault("app_dir", tmp_path / "程序" / "❤")
        kwargs.setdefault("cache_root", tmp_path / "cache")
        kwargs.setdefault("data_path_factory", lambda: tmp_path / "actual-data")
        kwargs.setdefault("envelope_reader", lambda raw, key: {"version": "0.4.1", "notes": "修复导入显示"})
        controller = update_ui.UpdateController(window, **kwargs)
        controls.append(controller)
        return controller

    yield create
    for controller in controls:
        controller.shutdown()
        if controller._thread:
            controller._thread.join(timeout=3)
        controller.panel.close()
        controller.panel.deleteLater()
    for window in windows:
        window.close()
        window.deleteLater()
    app.processEvents()


def ready_candidate(controller, tmp_path):
    candidate = tmp_path / "candidate"
    candidate.mkdir(exist_ok=True)
    (candidate / "release.json").write_text("offline envelope", encoding="utf-8")
    controller._on_complete(candidate)
    assert controller.has_ready_update
    return candidate


def test_startup_is_once_and_has_no_periodic_timer(controller_factory, app):
    controller = controller_factory()
    assert not OfflineService.calls  # Construction and self-test never check.
    controller.startup_check()
    pump(app, lambda: not controller.is_busy)
    controller.startup_check()
    controller.startup_check()
    app.processEvents()
    assert len(OfflineService.calls) == 1
    assert controller.findChildren(QTimer) == []
    assert controller.panel.isHidden()  # No update remains unobtrusive.
    controller.check_now()
    pump(app, lambda: not controller.is_busy)
    assert len(OfflineService.calls) == 2
    assert "最新版本" in controller.status_label.text()


def test_no_channel_or_development_mode_never_starts_network(controller_factory, app):
    controller = controller_factory(channel=None)
    controller.startup_check()
    controller.check_now()
    assert "尚未配置" in controller.status_label.text()
    dev = controller_factory(portable=False)
    dev.startup_check()
    dev.check_now()
    app.processEvents()
    assert "免安装程序" in dev.status_label.text()
    assert not OfflineService.calls


def test_network_error_is_inline_and_never_displays_raw_credentials(controller_factory, app):
    OfflineService.failure = True
    controller = controller_factory()
    controller.startup_check()
    pump(app, lambda: not controller.is_busy)
    assert "手动重试" in controller.status_label.text()
    assert "credential" not in controller.status_label.text()
    assert not controller.has_ready_update
    assert controller.check_button.isEnabled()


def test_close_cancels_worker_and_ignores_queued_results(controller_factory, app, tmp_path):
    OfflineService.block = True
    controller = controller_factory()
    controller.startup_check()
    pump(app, OfflineService.entered.is_set)
    controller.shutdown()
    pump(app, OfflineService.cancelled.is_set)
    controller._on_complete(tmp_path)
    controller._on_progress("must not show", 100)
    assert not controller.has_ready_update
    assert controller.status_label.text() != "must not show"
    assert controller._thread.daemon


@pytest.mark.parametrize("restart", [False, True])
def test_ready_update_hands_off_correct_restart_mode(controller_factory, tmp_path, restart, monkeypatch):
    preparations, launches = [], []
    monkeypatch.setenv("_PYI_APPLICATION_HOME_DIR", "must-not-inherit")
    monkeypatch.setenv("_PYI_PARENT_PROCESS_LEVEL", "1")
    monkeypatch.setenv("XIAOYU_UPDATE_TEST_DATA", "isolated-data-only")
    external_helper = tmp_path / "external-runner" / "update-helper.exe"

    def prepare(candidate, app_dir, data_path, *, restart):
        preparations.append((candidate, app_dir, data_path, restart))
        return [str(external_helper), "--candidate-dir", str(candidate)]

    controller = controller_factory(helper_factory=prepare,
                                    launcher=lambda args, **kwargs: launches.append((args, kwargs)))
    candidate = ready_candidate(controller, tmp_path)
    assert "关闭软件后" in controller.status_label.text()
    assert controller.prepare_close(restart=restart)
    assert preparations == [(candidate, controller.app_dir, tmp_path / "actual-data", restart)]
    assert launches[0][1]["close_fds"] is True
    assert "creationflags" in launches[0][1]
    assert launches[0][1]["cwd"] == external_helper.parent
    environment = launches[0][1]["env"]
    assert environment["PYINSTALLER_RESET_ENVIRONMENT"] == "1"
    assert not any(key.startswith("_PYI_") for key in environment)
    assert "XIAOYU_UPDATE_TEST_DATA" not in environment
    assert controller.prepare_close(restart=restart)
    assert len(preparations) == 1  # Repeated close cannot start a second helper.


def test_bad_candidate_is_not_offered_for_install(controller_factory, tmp_path):
    def reject(raw, key):
        raise ValueError("signature mismatch")

    controller = controller_factory(envelope_reader=reject)
    candidate = tmp_path / "bad"
    candidate.mkdir()
    (candidate / "release.json").write_bytes(b"invalid")
    controller._on_complete(candidate)
    assert "验证未通过" in controller.status_label.text()
    assert not controller.has_ready_update
    assert controller.restart_button.isHidden()


def test_helper_failure_keeps_old_app_and_allows_next_close(controller_factory, tmp_path):
    def fail(*args, **kwargs):
        raise PermissionError("occupied")

    controller = controller_factory(helper_factory=fail)
    ready_candidate(controller, tmp_path)
    assert controller.prepare_close() is False
    assert "旧版已保留" in controller.status_label.text()
    assert not controller.has_ready_update
    assert controller.prepare_close() is True


@pytest.fixture
def window_factory(controller_factory, monkeypatch, tmp_path, app):
    windows = []
    preparations = []

    def prepare(candidate, app_dir, data_path, *, restart):
        preparations.append(restart)
        return ["fake-helper.exe"]

    def make(**kwargs):
        monkeypatch.setattr(ui, "load_config", lambda: {})
        monkeypatch.setattr(ui, "save_config", lambda value: None)
        monkeypatch.setattr(ui, "UpdateController", lambda window: controller_factory(
            window=window, helper_factory=prepare, launcher=lambda *args, **kwargs: None))
        store = ui.MaterialStore(tmp_path / f"materials-{len(windows)}.sqlite3")
        window = ui.MainWindow(material_store=store)
        windows.append(window)
        window.show()
        app.processEvents()
        return window, preparations

    yield make
    for window in windows:
        window._busy = False
        window.ai_page = None
        window.update_controller.candidate_dir = None
        window.close()
        window.deleteLater()
    app.processEvents()


def test_busy_business_or_ai_prevents_update_handoff(window_factory, tmp_path, app):
    window, preparations = window_factory()
    ready_candidate(window.update_controller, tmp_path)
    window._busy = True
    assert window.close() is False
    assert window.isVisible()
    window._busy = False
    window.ai_page = SimpleNamespace(is_busy=True, shutdown=lambda: None)
    assert window.close() is False
    assert "AI 助手" in window.notice.text()
    assert not preparations
    window.ai_page.is_busy = False
    assert window.close() is True
    assert preparations == [False]


def test_save_failure_prevents_update_until_settings_are_saved(window_factory, tmp_path, monkeypatch):
    window, preparations = window_factory()
    ready_candidate(window.update_controller, tmp_path)

    def fail(value):
        raise OSError("disk full")

    monkeypatch.setattr(ui, "save_config", fail)
    assert window.close() is False
    assert "设置暂未保存" in window.notice.text()
    assert not preparations
    monkeypatch.setattr(ui, "save_config", lambda value: None)
    assert window.close() is True
    assert preparations == [False]


def test_restart_button_only_restarts_when_close_succeeds(window_factory, tmp_path):
    window, preparations = window_factory()
    ready_candidate(window.update_controller, tmp_path)
    window._busy = True
    window.update_controller.restart_button.click()
    assert window.isVisible()
    assert not preparations
    assert not window._restart_after_update
    window._busy = False
    window.update_controller.restart_button.click()
    assert preparations == [True]
    assert not window.isVisible()
