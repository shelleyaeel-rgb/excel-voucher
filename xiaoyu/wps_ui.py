"""Local WPS connection editor. Tokens stay in masked fields and encrypted storage."""
from __future__ import annotations

import threading
import uuid

from PySide6.QtCore import QTimer, Qt, Slot
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QDialog, QDialogButtonBox, QFormLayout,
    QHBoxLayout, QLabel, QLineEdit, QListWidget, QPushButton, QVBoxLayout,
)

from .config import resource_path
from .wps_service import WpsBinding, WpsClient, WpsError, normalize_share_url, save_bindings, validate_binding


def _text(value, *, wrap=True):
    label = QLabel(value)
    label.setTextFormat(Qt.TextFormat.PlainText)
    label.setWordWrap(wrap)
    return label


class _Probe:
    """Pure Python I/O worker; the background thread never owns or touches Qt."""
    def __init__(self, binding, client_factory):
        self.binding = binding
        self.client_factory = client_factory
        self.thread = None
        self.result = None
        self.error = None

    def start(self):
        self.thread = threading.Thread(target=self._run, name='XiaoYuWpsReadOnly', daemon=True)
        self.thread.start()

    def _run(self):
        result, error = None, None
        try:
            result = self.client_factory(self.binding).call('describe')
        except (WpsError, ValueError, OSError) as exc:
            error = str(exc).replace(self.binding.token, '[令牌已隐藏]') if self.binding.token else str(exc)
        except Exception:
            error = '连接测试未完成，请检查地址、令牌和云端脚本。'
        finally:
            self.binding = None
        self.result, self.error = result, error


class WpsDocumentsDialog(QDialog):
    """Each explicit save/delete persists its complete, validated local registry."""

    def __init__(self, bindings, path=None, parent=None, *, client_factory=WpsClient):
        super().__init__(parent)
        self.bindings = list(bindings)
        self.path = path
        self.client_factory = client_factory
        self._editing_id = None
        self._verified = None
        self._verified_name = ''
        self._probe = None
        self._probe_timer = QTimer(self)
        self._probe_timer.setInterval(20)
        self._probe_timer.timeout.connect(self._poll_probe)
        self.changed = False
        self.setWindowTitle('财务 AI 助手 · WPS 云文档')
        self.resize(760, 630)
        layout = QVBoxLayout(self)
        layout.addWidget(_text('首次连接每份云文档，之后在聊天里粘贴链接并说明更新规则即可。'))
        self.list = QListWidget()
        self.list.setMaximumHeight(125)
        self.list.currentRowChanged.connect(self._select)
        layout.addWidget(self.list)
        row = QHBoxLayout()
        self.add_button = QPushButton('添加云文档')
        self.delete_button = QPushButton('移除选中绑定')
        self.add_button.clicked.connect(self.new_binding)
        self.delete_button.clicked.connect(self.delete_binding)
        row.addWidget(self.add_button)
        row.addWidget(self.delete_button)
        row.addStretch()
        layout.addLayout(row)
        self.label_edit = QLineEdit()
        self.share_edit = QLineEdit()
        self.share_edit.setPlaceholderText('https://www.kdocs.cn/l/…')
        self.endpoint_edit = QLineEdit()
        self.endpoint_edit.setPlaceholderText('从这份文档的 AirScript 2.0 共享脚本复制 webhook')
        self.token_edit = QLineEdit()
        self.token_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.token_edit.setPlaceholderText('脚本令牌，仅由本机调用 WPS，不发送给 AI')
        form = QFormLayout()
        form.addRow('便于识别的名称', self.label_edit)
        form.addRow('云文档分享链接', self.share_edit)
        form.addRow('脚本调用地址', self.endpoint_edit)
        form.addRow('AirScript 令牌', self.token_edit)
        layout.addLayout(form)
        layout.addWidget(_text('在目标文档的 AirScript 2.0 文档共享脚本中粘贴配套脚本，'
                               '按 Ctrl+S 保存到云端，再复制该脚本的 webhook。'))
        self.copy_button = QPushButton('复制配套云端脚本')
        self.copy_button.clicked.connect(self.copy_script)
        layout.addWidget(self.copy_button)
        self.confirm_checkbox = QCheckBox('这个脚本调用地址确实从上方分享链接对应的文档复制')
        layout.addWidget(self.confirm_checkbox)
        row = QHBoxLayout()
        self.test_button = QPushButton('只读测试连接')
        self.save_button = QPushButton('保存当前绑定')
        self.save_button.setObjectName('primary')
        self.test_button.clicked.connect(self.test_binding)
        self.save_button.clicked.connect(self.save_binding)
        row.addWidget(self.test_button)
        row.addWidget(self.save_button)
        layout.addLayout(row)
        self.status_label = _text('令牌加密保存在这台电脑。测试只读取文档信息，不修改单元格。')
        self.status_label.setObjectName('notice')
        layout.addWidget(self.status_label)
        self.close_buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        self.close_buttons.button(QDialogButtonBox.StandardButton.Close).setText('完成')
        self.close_buttons.rejected.connect(self.reject)
        layout.addWidget(self.close_buttons)
        for edit in (self.share_edit, self.endpoint_edit, self.token_edit):
            edit.textChanged.connect(self._invalidate)
        self._refresh()
        if self.bindings:
            self.list.setCurrentRow(0)
        else:
            self.new_binding()

    @property
    def is_busy(self):
        return self._probe is not None

    def _status(self, message):
        value = str(message)
        tokens = [self.token_edit.text(), *(item.token for item in self.bindings)]
        for token in tokens:
            if token:
                value = value.replace(token, '[令牌已隐藏]')
        self.status_label.setText(value)

    def _fingerprint(self):
        return (self.share_edit.text().strip(), self.endpoint_edit.text().strip(),
                self.token_edit.text().strip())

    def _invalidate(self):
        self._verified = None
        self._verified_name = ''

    def _refresh(self, selected_id=None):
        self.list.blockSignals(True)
        self.list.clear()
        for binding in self.bindings:
            self.list.addItem(binding.label + ' · ' + binding.workbook_name)
        self.list.blockSignals(False)
        if selected_id is not None:
            index = next((i for i, item in enumerate(self.bindings) if item.id == selected_id), -1)
            self.list.setCurrentRow(index)

    def _select(self, row):
        if self.is_busy or not 0 <= row < len(self.bindings):
            return
        binding = self.bindings[row]
        self._editing_id = binding.id
        self.label_edit.setText(binding.label)
        self.share_edit.setText(binding.share_url)
        self.endpoint_edit.setText(binding.endpoint)
        self.token_edit.setText(binding.token)
        self.confirm_checkbox.setChecked(True)
        self._verified_name = binding.workbook_name
        self._verified = self._fingerprint() if binding.workbook_name else None
        self._status('已保存：' + binding.label + '。更改地址或令牌后，请重新只读测试。')

    def new_binding(self):
        if self.is_busy:
            return
        self.list.setCurrentRow(-1)
        self._editing_id = None
        self.label_edit.clear()
        self.share_edit.clear()
        self.endpoint_edit.clear()
        self.token_edit.clear()
        self.confirm_checkbox.setChecked(False)
        self._invalidate()
        self._status('填写链接和授权信息，完成只读测试后保存。')
        self.label_edit.setFocus()

    def _value(self, *, verified=False):
        return WpsBinding(
            id=self._editing_id or uuid.uuid4().hex,
            label=self.label_edit.text().strip(),
            share_url=normalize_share_url(self.share_edit.text().strip()),
            endpoint=self.endpoint_edit.text().strip(),
            token=self.token_edit.text().strip(),
            workbook_name=self._verified_name if verified else '',
        )

    def _sync_controls(self):
        for control in (self.list, self.add_button, self.delete_button, self.label_edit,
                        self.share_edit, self.endpoint_edit, self.token_edit, self.test_button,
                        self.save_button, self.confirm_checkbox, self.copy_button):
            control.setEnabled(not self.is_busy)

    def copy_script(self):
        try:
            content = resource_path('assets/wps_airscript.js').read_text(encoding='utf-8')
        except (OSError, UnicodeError):
            self._status('配套云端脚本无法读取，请检查软件文件是否完整。')
            return
        QApplication.clipboard().setText(content)
        self._status('脚本已复制。粘贴到目标文档的 AirScript 2.0 共享脚本后，按 Ctrl+S 保存。')

    def test_binding(self):
        if self.is_busy:
            return
        try:
            binding = self._value()
            validate_binding(binding)
            if not self.confirm_checkbox.isChecked():
                raise ValueError('请先勾选确认脚本调用地址来自这份云文档。')
        except (ValueError, WpsError) as exc:
            self._status(exc)
            return
        self._invalidate()
        self._probe = _Probe(binding, self.client_factory)
        self._sync_controls()
        self._status('正在只读检查授权、云端脚本和文档名称…')
        try:
            self._probe.start()
        except RuntimeError:
            self._probe.binding = None
            self._probe = None
            self._sync_controls()
            self._status('无法启动连接测试，请稍后重试。')
            return
        self._probe_timer.start()

    @Slot()
    def _poll_probe(self):
        probe = self._probe
        if probe is None:
            self._probe_timer.stop()
            return
        # A result can be ready before Thread.run has actually returned. Keep
        # ownership/busy until the worker has exited, then update Qt only here.
        if probe.thread.is_alive():
            return
        self._probe_timer.stop()
        self._tested(probe.result, probe.error)

    @Slot(object, object)
    def _tested(self, result, error):
        self._probe = None
        self._sync_controls()
        if error:
            self._status(error)
            return
        if (not isinstance(result, dict) or not isinstance(result.get('workbook_name'), str)
                or not result['workbook_name'] or not isinstance(result.get('sheets'), list)):
            self._status('云端返回的文档信息不完整，请检查配套脚本已保存。')
            return
        self._verified = self._fingerprint()
        self._verified_name = result['workbook_name']
        self._status('连接通过，实际文档：' + self._verified_name + '。请核对名称后保存当前绑定。')

    def save_binding(self):
        if self.is_busy:
            return
        try:
            if not self.confirm_checkbox.isChecked():
                raise ValueError('请先确认脚本调用地址来自这份云文档。')
            if self._verified != self._fingerprint() or not self._verified_name:
                raise ValueError('请先完成只读测试，确认实际文档名称。')
            binding = self._value(verified=True)
            validate_binding(binding)
            remaining = [item for item in self.bindings if item.id != self._editing_id]
            if any(item.share_url == binding.share_url for item in remaining):
                raise ValueError('这份文档已经绑定，请在列表中选择它后修改。')
            updated = [*remaining, binding]
            save_bindings(updated, self.path)
        except (WpsError, ValueError, OSError) as exc:
            self._status('绑定未保存：' + str(exc))
            return
        self.bindings = updated
        self.changed = True
        self._refresh(binding.id)
        self._status('绑定已保存，令牌已加密。现在可在聊天里使用这份文档链接。')

    def delete_binding(self):
        if self.is_busy or self._editing_id is None:
            return
        updated = [item for item in self.bindings if item.id != self._editing_id]
        try:
            save_bindings(updated, self.path)
        except (WpsError, ValueError, OSError) as exc:
            self._status('绑定未移除：' + str(exc))
            return
        self.bindings = updated
        self.changed = True
        self._refresh()
        self.new_binding()
        self._status('已移除本机绑定，云文档内容没有改变。')

    def reject(self):
        if self.is_busy:
            self._status('正在只读测试，请等待结果后关闭。')
            return
        self.token_edit.clear()
        super().reject()

    def closeEvent(self, event):
        if self.is_busy:
            self._status('正在只读测试，请等待结果后关闭。')
            event.ignore()
            return
        self.token_edit.clear()
        super().closeEvent(event)
