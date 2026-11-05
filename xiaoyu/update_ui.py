"""A single startup check and a non-blocking portable-update UI.

Workers only download into the update cache. The application is replaced by a
separate helper after the main process exits; no timer polls during a session.
"""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import subprocess
import sys
import threading

from PySide6.QtCore import QObject, Qt, Signal, Slot
from PySide6.QtWidgets import (
    QDialog, QDialogButtonBox, QFrame, QHBoxLayout, QLabel, QProgressBar,
    QPushButton, QTextEdit, QVBoxLayout,
)

from . import __version__
from .config import data_dir
from .update_protocol import load_channel, verify_envelope


_AUTO = object()


def _cache_root(app_dir: Path) -> Path:
    base = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
    key = hashlib.sha256(str(app_dir.resolve()).casefold().encode("utf-8")).hexdigest()[:20]
    return base / "XiaoYuVoucher" / "updates" / key


class UpdateController(QObject):
    """Own update controls and daemon download work; construction never checks."""

    _progress_signal = Signal(str, int)
    _complete_signal = Signal(object)
    _error_signal = Signal(str)

    def __init__(self, window, *, channel=_AUTO, portable=None,
                 current_version=__version__, service_factory=None,
                 helper_factory=None, launcher=None, app_dir=None,
                 cache_root=None, data_path_factory=data_dir,
                 envelope_reader=verify_envelope):
        super().__init__(window)
        self.window = window
        self.current_version = current_version
        self.portable = bool(getattr(sys, "frozen", False)) if portable is None else portable
        self.app_dir = Path(app_dir or (Path(sys.executable).parent if self.portable
                                       else Path(__file__).resolve().parents[1]))
        self.cache_root = Path(cache_root) if cache_root is not None else _cache_root(self.app_dir)
        self._config_error = False
        try:
            self.channel = load_channel() if channel is _AUTO else channel
        except (OSError, ValueError, TypeError):
            self.channel = None
            self._config_error = True
        self._service_factory = service_factory
        self._helper_factory = helper_factory
        self._launcher = launcher or subprocess.Popen
        self._data_path_factory = data_path_factory
        self._envelope_reader = envelope_reader
        self._startup_checked = False
        self._closed = False
        self._manual = False
        self._busy = False
        self._cancel = threading.Event()
        self._thread = None
        self.candidate_dir = None
        self.release = None
        self._helper_started = False
        self._build_controls()
        self._progress_signal.connect(self._on_progress)
        self._complete_signal.connect(self._on_complete)
        self._error_signal.connect(self._on_error)

    def _build_controls(self):
        self.version_label = QLabel(f"版本 {self.current_version}")
        self.version_label.setObjectName("small")
        self.check_button = QPushButton("检查更新")
        self.check_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.check_button.clicked.connect(self.check_now)
        self.panel = QFrame()
        self.panel.setObjectName("card")
        row = QHBoxLayout(self.panel)
        row.setContentsMargins(12, 8, 12, 8)
        row.setSpacing(10)
        self.status_label = QLabel()
        self.status_label.setWordWrap(True)
        self.status_label.setTextFormat(Qt.TextFormat.PlainText)
        row.addWidget(self.status_label, 1)
        self.progress = QProgressBar()
        self.progress.setFixedWidth(100)
        self.progress.setRange(0, 100)
        self.progress.setTextVisible(False)
        row.addWidget(self.progress)
        self.notes_button = QPushButton("更新说明")
        self.notes_button.setObjectName("link")
        self.notes_button.clicked.connect(self.show_notes)
        row.addWidget(self.notes_button)
        self.restart_button = QPushButton("立即重启更新")
        self.restart_button.setObjectName("primary")
        self.restart_button.clicked.connect(self._request_restart)
        row.addWidget(self.restart_button)
        self.progress.hide()
        self.notes_button.hide()
        self.restart_button.hide()
        self.panel.hide()

    @property
    def is_busy(self):
        return self._busy

    @property
    def has_ready_update(self):
        return self.candidate_dir is not None and self.release is not None

    def startup_check(self):
        """Called by the application entry point once, never from self-tests."""
        if self._startup_checked or self._closed:
            return
        self._startup_checked = True
        if self.portable and self.channel is not None:
            self._begin(manual=False)

    def check_now(self):
        if self._closed or self._busy:
            return
        self._startup_checked = True
        if self.has_ready_update:
            self._show_ready()
            return
        if not self.portable:
            self._status("请在免安装程序中检查更新。")
            return
        if self.channel is None:
            self._status("更新配置暂不可用，请联系发布者。" if self._config_error
                         else "尚未配置更新地址，当前版本可以正常使用。")
            return
        self._begin(manual=True)

    def _begin(self, *, manual):
        if self._closed or self._busy:
            return
        self._manual = manual
        self._busy = True
        self._cancel = threading.Event()
        self.check_button.setEnabled(False)
        self._status("正在检查更新…")
        self.progress.setRange(0, 0)
        self.progress.show()
        cancel = self._cancel

        def emit(signal, *args):
            # The native window can be deleted while the network request is
            # finishing. Never keep the GUI alive or emit to a deleted QObject.
            if cancel.is_set():
                return
            try:
                signal.emit(*args)
            except RuntimeError:
                pass

        def work():
            try:
                factory = self._service_factory
                if factory is None:
                    from .update_service import UpdateService
                    factory = UpdateService
                service = factory(self.channel, self.current_version, self.cache_root)
                candidate = service.check_and_download(
                    cancel, lambda text, percent: emit(self._progress_signal, text, percent))
                emit(self._complete_signal, candidate)
            except Exception:
                # Display a useful, non-blocking message without exposing URL
                # query strings, credentials, or raw server responses.
                emit(self._error_signal, "暂时无法检查或下载更新，稍后可手动重试。")

        self._thread = threading.Thread(target=work, name="XiaoYuUpdateDownload", daemon=True)
        self._thread.start()

    def _status(self, text):
        self.status_label.setText(text)
        self.panel.show()

    @Slot(str, int)
    def _on_progress(self, text, percent):
        if self._closed or self._cancel.is_set() or not self._busy:
            return
        self._status(text)
        self.progress.show()
        if percent < 0:
            self.progress.setRange(0, 0)
        else:
            self.progress.setRange(0, 100)
            self.progress.setValue(max(0, min(100, percent)))

    @Slot(object)
    def _on_complete(self, candidate):
        if self._closed or self._cancel.is_set():
            return
        self._busy = False
        self.check_button.setEnabled(True)
        self.progress.hide()
        if candidate is None:
            if self._manual:
                self._status("当前已是最新版本。")
            else:
                self.panel.hide()
            return
        try:
            path = Path(candidate)
            manifest = path / "release.json"
            if manifest.stat().st_size > 256 * 1024:
                raise ValueError("更新信息过大。")
            release = self._envelope_reader(manifest.read_bytes(), self.channel["public_key"])
        except Exception:
            self._on_error("更新验证未通过，继续使用当前版本。")
            return
        self.candidate_dir = path
        self.release = release
        self._show_ready()

    def _show_ready(self):
        self._status(f"新版 {self.release['version']} 已准备好，关闭软件后自动更新。")
        self.notes_button.show()
        self.restart_button.show()

    @Slot(str)
    def _on_error(self, message):
        if self._closed or self._cancel.is_set():
            return
        self._busy = False
        self.check_button.setEnabled(True)
        self.progress.hide()
        # No pop-up interrupts accounting work, including automatic failures.
        self._status(message)

    def show_notes(self):
        if not self.release or self._closed:
            return
        dialog = QDialog(self.window)
        dialog.setWindowTitle(f"版本 {self.release['version']} · 更新说明")
        dialog.resize(560, 380)
        layout = QVBoxLayout(dialog)
        notes = QTextEdit()
        notes.setReadOnly(True)
        notes.setPlainText(self.release.get("notes") or "本次更新未提供说明。")
        layout.addWidget(notes)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)
        dialog.exec()

    def _request_restart(self):
        if self.has_ready_update and not self._closed:
            self.window._restart_for_update()

    def prepare_close(self, restart=False):
        """Start the external helper after the window has saved its settings.

        Failure keeps the window open once to show the result. Its candidate is
        discarded, so the next ordinary close exits without attempting updates.
        """
        if not self.has_ready_update or self._helper_started:
            return True
        try:
            if not self.portable:
                raise ValueError("源码运行不能替换程序。")
            factory = self._helper_factory
            if factory is None:
                from .update_installer import prepare_helper
                factory = prepare_helper
            command = factory(self.candidate_dir, self.app_dir,
                              self._data_path_factory(), restart=bool(restart))
            # An inherited current directory inside the app prevents Windows
            # from renaming it. The onefile helper also needs its own frozen
            # runtime instead of inheriting the main executable's state.
            helper_dir = Path(command[0]).resolve().parent
            env = {key: value for key, value in os.environ.items() if not key.startswith("_PYI_")}
            env["PYINSTALLER_RESET_ENVIRONMENT"] = "1"
            env.pop("XIAOYU_UPDATE_TEST_DATA", None)
            self._launcher(command, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                           close_fds=True, cwd=helper_dir, env=env)
            self._helper_started = True
            return True
        except Exception:
            self.candidate_dir = None
            self.release = None
            self.notes_button.hide()
            self.restart_button.hide()
            self._status("更新暂未完成，旧版已保留。可以继续使用，或再次关闭软件直接退出。")
            return False

    def shutdown(self):
        if self._closed:
            return
        self._closed = True
        self._cancel.set()
        self._busy = False
