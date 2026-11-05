"""Independent upload-only financial assistant. No accounting-store access."""
from __future__ import annotations

import html
import os
import re
import tempfile
import threading
from dataclasses import replace
from pathlib import Path

from PySide6.QtCore import QByteArray, QObject, Qt, QUrl, Signal, Slot
from PySide6.QtGui import QDesktopServices, QTextDocument
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QDialogButtonBox, QDoubleSpinBox,
    QFileDialog, QFormLayout, QFrame, QHBoxLayout, QLabel, QLineEdit,
    QListWidget, QListWidgetItem, QMessageBox, QPlainTextEdit, QPushButton,
    QSizePolicy, QSpinBox, QSplitter, QTextBrowser, QVBoxLayout, QWidget,
)

from .ai_service import AIError, FinanceAIClient, RequestCancelled
from .ai_settings import AISettings, UsageLedger, load_settings, save_settings, validate_settings
from .ai_rules import MAX_RULES_CHARS, load_rules, save_rules, validate_rules


UPLOAD_EXTENSIONS = {'.xlsx', '.csv', '.pdf', '.docx', '.pptx', '.txt', '.md', '.png', '.jpg', '.jpeg'}
DOWNLOAD_EXTENSIONS = UPLOAD_EXTENSIONS | {'.xls', '.zip'}
MAX_FILE_BYTES = 20 * 1024 * 1024
MAX_SESSION_FILES = 10
FILE_FILTER = '文档和图片 (*.xlsx *.csv *.pdf *.docx *.pptx *.txt *.md *.png *.jpg *.jpeg)'


def _label(text, name='', wrap=False):
    widget = QLabel(text)
    widget.setObjectName(name)
    widget.setWordWrap(wrap)
    return widget


def _button(text, callback=None, primary=False):
    widget = QPushButton(text)
    widget.setCursor(Qt.CursorShape.PointingHandCursor)
    if primary:
        widget.setObjectName('primary')
    if callback:
        widget.clicked.connect(callback)
    return widget


def _safe_name(name):
    name = str(name).replace('\\', '/').rsplit('/', 1)[-1]
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', name).strip(' .')
    return name[:180] or '生成的文档.xlsx'


def save_download(content: bytes, destination: Path, source_paths) -> Path:
    """Save only to the user-chosen destination; never replace an uploaded source."""
    destination = Path(destination)
    if destination.is_symlink():
        raise ValueError('请选择普通文件位置，不能覆盖快捷链接指向的文件。')
    if destination.suffix.lower() not in DOWNLOAD_EXTENSIONS:
        raise ValueError('请保留文档或图片的文件扩展名。')
    resolved = destination.resolve()
    for source in source_paths:
        source = Path(source)
        if str(source.resolve()).casefold() == str(resolved).casefold():
            raise ValueError('请换一个文件名保存，不能覆盖本次上传的原文件。')
        try:
            if destination.exists() and source.exists() and destination.samefile(source):
                raise ValueError('请换一个位置保存，不能覆盖本次上传的原文件。')
        except OSError:
            pass
    if not isinstance(content, bytes) or not content:
        raise ValueError('下载内容为空，文件尚未保存，请重新下载。')
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(prefix='.xiaoyu-download-', suffix='.tmp',
                                         dir=destination.parent, delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, destination)
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()
    return destination


class MessageEdit(QPlainTextEdit):
    send_requested = Signal()

    def keyPressEvent(self, event):
        if (event.key() not in (Qt.Key.Key_Return, Qt.Key.Key_Enter)
                or not self.isEnabled() or self.isReadOnly()):
            super().keyPressEvent(event)
            return
        if self.textCursor().block().layout().preeditAreaText():
            # Candidate selection belongs to the input method, not chat shortcuts.
            event.accept()
            return

        modifiers = event.modifiers() & ~Qt.KeyboardModifier.KeypadModifier
        if modifiers & (Qt.KeyboardModifier.AltModifier | Qt.KeyboardModifier.ShiftModifier):
            self.insertPlainText('\n')
            event.accept()
            return
        if modifiers == Qt.KeyboardModifier.NoModifier:
            event.accept()
            if not event.isAutoRepeat():
                self.send_requested.emit()
            return
        super().keyPressEvent(event)


class _SafeDocument(QTextDocument):
    def loadResource(self, resource_type, url):
        return QByteArray()


class SafeTranscript(QTextBrowser):
    """Model text cannot fetch an image or read a local resource while rendering."""
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setOpenLinks(False)
        self.setOpenExternalLinks(False)
        self.anchorClicked.connect(self._open_link)
        self.setStyleSheet('QTextBrowser {background:#FFFCFD;border:1px solid #ECCDDD;'
                           'border-radius:12px;padding:10px;selection-background-color:#EDBBD1;}')

    def loadResource(self, resource_type, url):
        return QByteArray()

    @Slot(QUrl)
    def _open_link(self, url):
        if url.scheme().lower() in ('https', 'http') and url.host() and not url.userInfo():
            QDesktopServices.openUrl(url)


class _Job(QObject):
    result = Signal(int, object)
    error = Signal(int, object)
    finished = Signal(int)
    text = Signal(int, str)
    status = Signal(int, str)
    uploaded = Signal(int, object)

    def __init__(self, job_id, kind, function):
        super().__init__()
        self.job_id, self.kind, self.function = job_id, kind, function
        self.cancel = threading.Event()
        self.abandon = False
        self.request_started = False
        self.result_accounted = False

    def emit_safe(self, signal, *args):
        try:
            signal.emit(self.job_id, *args)
        except RuntimeError:
            # The application may already have closed while a network call returns.
            pass

    def start(self):
        self.thread = threading.Thread(target=self._run, name='XiaoYuFinanceAI', daemon=True)
        self.thread.start()

    def _run(self):
        try:
            result = self.function(self)
            self.emit_safe(self.result, result)
        except Exception as exc:
            self.emit_safe(self.error, exc)
        finally:
            self.emit_safe(self.finished)


class AISettingsDialog(QDialog):
    def __init__(self, settings, remember_key=False, parent=None):
        super().__init__(parent)
        self.setWindowTitle('财务 AI 助手 · 连接设置')
        self.setMinimumWidth(520)
        layout = QVBoxLayout(self)
        layout.addWidget(_label('连接 OpenAI', 'sectionTitle'))
        layout.addWidget(_label('模型固定为 Luna。更换密钥或连接方式后，会开启新对话。', 'muted', True))
        form = QFormLayout()
        self.key_edit = QLineEdit(settings.api_key)
        self.key_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.key_edit.setPlaceholderText('输入你的 OpenAI API 密钥')
        form.addRow('API 密钥', self.key_edit)
        self.remember_key = QCheckBox('在这台电脑上加密保存密钥')
        self.remember_key.setChecked(remember_key)
        form.addRow('', self.remember_key)
        self.proxy_combo = QComboBox()
        for text, value in [('直接连接（默认）', 'direct'), ('使用系统代理', 'system'), ('手动设置代理', 'custom')]:
            self.proxy_combo.addItem(text, value)
        self.proxy_combo.setCurrentIndex(max(0, self.proxy_combo.findData(settings.proxy_mode)))
        self.proxy_edit = QLineEdit(settings.proxy_url)
        self.proxy_edit.setPlaceholderText('例如 http://127.0.0.1:7890')
        self.proxy_edit.setEnabled(settings.proxy_mode == 'custom')
        self.proxy_combo.currentIndexChanged.connect(
            lambda: self.proxy_edit.setEnabled(self.proxy_combo.currentData() == 'custom'))
        form.addRow('连接方式', self.proxy_combo)
        form.addRow('代理地址', self.proxy_edit)
        self.budget_spin = QDoubleSpinBox()
        self.budget_spin.setRange(1, 1000)
        self.budget_spin.setDecimals(2)
        self.budget_spin.setSuffix(' 美元')
        self.budget_spin.setValue(settings.monthly_limit_usd)
        form.addRow('每月估算费用上限', self.budget_spin)
        self.output_spin = QSpinBox()
        self.output_spin.setRange(1000, 16000)
        self.output_spin.setSingleStep(1000)
        self.output_spin.setValue(settings.max_output_tokens)
        form.addRow('单次输出上限（token）', self.output_spin)
        layout.addLayout(form)
        layout.addWidget(_label('token 是模型读写内容的计费单位。费用为本软件估算，实际金额以 OpenAI 账单为准。'
                                '达到上限后停止发起新请求，已发出的请求仍可能产生费用。', 'small', True))
        self.error_label = _label('', 'notice', True)
        self.error_label.hide()
        layout.addWidget(self.error_label)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Save).setText('保存设置')
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText('取消')
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def value(self):
        return AISettings(api_key=self.key_edit.text().strip(), proxy_mode=self.proxy_combo.currentData(),
                          proxy_url=self.proxy_edit.text().strip(), monthly_limit_usd=self.budget_spin.value(),
                          max_output_tokens=self.output_spin.value())

    def accept(self):
        try:
            validate_settings(self.value())
        except ValueError as exc:
            self.error_label.setText(str(exc))
            self.error_label.show()
            return
        super().accept()


class AIRulesDialog(QDialog):
    def __init__(self, rules, rules_path=None, parent=None):
        super().__init__(parent)
        self.rules_path = rules_path
        self.setWindowTitle('财务 AI 助手 · 长期规则')
        self.setMinimumSize(580, 420)
        self.resize(640, 460)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(22, 20, 22, 20)
        layout.setSpacing(12)
        layout.addWidget(_label('让助手按你的习惯回答', 'sectionTitle'))
        layout.addWidget(_label('保存后，每次提问都会带上这些规则，新对话和重启软件后仍保留。'
                                '规则保存在这台电脑，提问时会发送给 AI。', 'muted', True))
        self.rules_edit = QPlainTextEdit()
        self.rules_edit.setAccessibleName('长期规则内容')
        self.rules_edit.setPlaceholderText('例如：\n回答简洁，先给结论，非必要不展开。\n'
                                          '使用通俗中文，专业术语用一句话解释。\n'
                                          '需要我操作时，用简短步骤说明。')
        self.rules_edit.setPlainText(rules)
        self.rules_edit.setStyleSheet('QPlainTextEdit {background:#FFFCFD;border:1px solid #ECCDDD;'
                                     'border-radius:10px;padding:10px;selection-background-color:#EDBBD1;}'
                                     'QPlainTextEdit:focus {border-color:#BA6A8C;}')
        layout.addWidget(self.rules_edit, 1)
        self.counter_label = _label('', 'small')
        self.counter_label.setAlignment(Qt.AlignmentFlag.AlignRight)
        layout.addWidget(self.counter_label)
        layout.addWidget(_label('填写表达习惯、回答格式等通用要求。本次提问中的具体要求优先；'
                                '留空并保存即可取消长期规则。', 'small', True))
        self.error_label = _label('', 'notice', True)
        self.error_label.setTextFormat(Qt.TextFormat.PlainText)
        self.error_label.setProperty('tone', 'error')
        self.error_label.hide()
        layout.addWidget(self.error_label)
        self.buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save |
                                       QDialogButtonBox.StandardButton.Cancel |
                                       QDialogButtonBox.StandardButton.Reset)
        self.buttons.button(QDialogButtonBox.StandardButton.Save).setText('保存规则')
        self.buttons.button(QDialogButtonBox.StandardButton.Save).setObjectName('primary')
        self.buttons.button(QDialogButtonBox.StandardButton.Cancel).setText('取消')
        self.buttons.button(QDialogButtonBox.StandardButton.Reset).setText('清空规则')
        self.buttons.button(QDialogButtonBox.StandardButton.Reset).clicked.connect(self.rules_edit.clear)
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        layout.addWidget(self.buttons)
        self.rules_edit.textChanged.connect(self._update_counter)
        self._update_counter()

    def value(self):
        return self.rules_edit.toPlainText().strip()

    def _update_counter(self):
        count = len(self.value())
        self.counter_label.setText(f'{count} / {MAX_RULES_CHARS} 字')
        self.counter_label.setStyleSheet('color:#99502F;' if count > MAX_RULES_CHARS else '')

    def accept(self):
        try:
            rules = self.value()
            validate_rules(rules)
            save_rules(rules, self.rules_path)
        except (ValueError, OSError) as exc:
            self.error_label.setText('规则未保存：' + str(exc))
            self.error_label.show()
            return
        super().accept()


class FinanceAIPage(QWidget):
    def __init__(self, parent=None, *, settings=None, ledger=None,
                 client_factory=FinanceAIClient, settings_path=None, rules_path=None):
        super().__init__(parent)
        self.setAcceptDrops(True)
        self.setStyleSheet('QListWidget::item:selected {color:#632E49;background:#F8DAE8;}')
        self.settings_path = settings_path
        self.rules_path = rules_path
        self.client_factory = client_factory
        self._load_error = ''
        self._rules_load_error = ''
        try:
            self.custom_rules = load_rules(rules_path)
        except (ValueError, OSError) as exc:
            self.custom_rules = ''
            self._rules_load_error = str(exc)
        if settings is None:
            try:
                settings = load_settings(settings_path)
            except (ValueError, OSError) as exc:
                settings = AISettings()
                self._load_error = str(exc)
        self.settings = settings
        self._remember_key = bool(settings.api_key)
        self.ledger = ledger if ledger is not None else UsageLedger()
        self.client = None
        self.pending_paths = []
        self.uploaded_files = []
        self.source_paths = set()
        self.generated_files = []
        self.history = []
        self.messages = []
        self.container_id = None
        self._job = None
        self._job_counter = 0
        self._closed = False
        self._partial_text = ''
        self._current_message = ''
        self._build()
        self._refresh_rules_summary()
        self._refresh_files()
        self._refresh_usage()
        self._render_chat()
        self._sync_controls()
        errors = []
        if self._load_error:
            errors.append(self._load_error + ' 请打开连接设置重新配置。')
        if self._rules_load_error:
            errors.append(self._rules_load_error + ' 请打开“长期规则”重新保存或恢复备份。')
        if errors:
            self._status(' '.join(errors), error=True)
        elif not self.settings.api_key:
            self._status('首次使用请打开“连接设置”，填写 API 密钥。')

    @property
    def is_busy(self):
        return self._job is not None

    def _build(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)
        title = QHBoxLayout()
        title.addWidget(_label('财务 AI 助手', 'hero'))
        title.addWidget(_label('Luna · 云端处理', 'muted'))
        title.addStretch()
        self.new_chat_button = _button('新对话', self.new_chat)
        self.settings_button = _button('连接设置', self.open_settings)
        self.test_button = _button('测试连接', self.test_connection)
        for button in (self.new_chat_button, self.settings_button, self.test_button):
            title.addWidget(button)
        layout.addLayout(title)
        layout.addWidget(_label('仅处理你上传的文件，内容发送到 OpenAI 云端。生成结果请及时下载。', 'muted', True))
        preferences = QHBoxLayout()
        self.usage_label = _label('', 'small', True)
        preferences.addWidget(self.usage_label, 1)
        self.rules_summary = _label('', 'small')
        preferences.addWidget(self.rules_summary)
        self.rules_button = _button('长期规则', self.open_rules)
        self.rules_button.setToolTip('设置回答习惯，新对话和重启后继续使用。')
        preferences.addWidget(self.rules_button)
        layout.addLayout(preferences)
        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.setHandleWidth(10)
        splitter.setStyleSheet('QSplitter::handle {margin:0;background:#FFF1F6;} '
                              'QSplitter::handle:horizontal {width:10px;margin:0;}')
        self.transcript = SafeTranscript()
        self.transcript.setMinimumWidth(320)
        splitter.addWidget(self.transcript)
        panel = QFrame()
        panel.setObjectName('card')
        panel.setMinimumWidth(230)
        side = QVBoxLayout(panel)
        side.setContentsMargins(14, 14, 14, 14)
        attachment_header = QHBoxLayout()
        attachment_header.addWidget(_label('本次文件', 'sectionTitle'))
        attachment_header.addStretch()
        self.attach_button = _button('添加', self.choose_files)
        attachment_header.addWidget(self.attach_button)
        side.addLayout(attachment_header)
        side.addWidget(_label('可拖入 Excel、PDF、Word 等文件\n每个最多 20 MB，每次会话最多 10 个', 'small', True))
        self.attachment_list = QListWidget()
        self.attachment_list.setMinimumHeight(100)
        side.addWidget(self.attachment_list, 1)
        self.remove_button = _button('移除选中的待发送文件', self.remove_selected_file)
        side.addWidget(self.remove_button)
        side.addWidget(_label('生成的文件', 'sectionTitle'))
        self.result_list = QListWidget()
        self.result_list.setMinimumHeight(80)
        side.addWidget(self.result_list, 1)
        self.download_button = _button('下载选中的文件', self.download_selected)
        side.addWidget(self.download_button)
        side.addWidget(_label('云端文件约 20 分钟无活动后过期。\n已上传附件可在本次对话中继续使用。', 'small', True))
        splitter.addWidget(panel)
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([620, 260])
        layout.addWidget(splitter, 1)
        self.attachment_list.currentRowChanged.connect(self._sync_controls)
        self.result_list.currentRowChanged.connect(self._sync_controls)
        self.message_edit = MessageEdit()
        self.message_edit.setPlaceholderText('例如：按部门汇总这份工资表，并生成一个新的 Excel。\n也可以直接提问财务知识。')
        self.message_edit.setMinimumHeight(80)
        self.message_edit.setMaximumHeight(110)
        self.message_edit.setStyleSheet('QPlainTextEdit {background:#FFFCFD;border:1px solid #ECCDDD;'
                                       'border-radius:10px;padding:8px;selection-background-color:#EDBBD1;}')
        self.message_edit.textChanged.connect(self._sync_controls)
        layout.addWidget(self.message_edit)
        actions = QHBoxLayout()
        self.web_search_checkbox = QCheckBox('联网核实政策和来源')
        self.web_search_checkbox.setToolTip('开启后允许助手查询公开网页，会增加搜索费用。不要在搜索中填写个人敏感信息。')
        actions.addWidget(self.web_search_checkbox)
        actions.addStretch()
        actions.addWidget(_label('Enter 发送 · Shift/Alt+Enter 换行', 'small'))
        self.cancel_button = _button('停止等待', self.cancel_request)
        self.send_button = _button('发送', self.send_message, primary=True)
        self.message_edit.send_requested.connect(self.send_button.click)
        actions.addWidget(self.cancel_button)
        actions.addWidget(self.send_button)
        layout.addLayout(actions)
        self.status_label = _label('可以提问，或添加文件后发送处理要求。', 'notice', True)
        self.status_label.setTextFormat(Qt.TextFormat.PlainText)
        layout.addWidget(self.status_label)

    def _status(self, text, error=False):
        self.status_label.setText(str(text).replace(self.settings.api_key, '[密钥已隐藏]')
                                  if self.settings.api_key else str(text))
        self.status_label.setProperty('tone', 'error' if error else '')
        self.status_label.style().unpolish(self.status_label)
        self.status_label.style().polish(self.status_label)

    def _refresh_usage(self):
        try:
            summary = self.ledger.summary()
            uncertain = summary.get('uncertain_requests', 0)
            text = (f"本月 {summary.get('requests', 0)} 次请求 · 估算 ${summary.get('estimated_usd', 0):.4f}"
                    f' / ${self.settings.monthly_limit_usd:.2f} · 实际费用以 OpenAI 账单为准')
            if uncertain:
                text += f' · {uncertain} 次中断费用待查'
            self.usage_label.setText(text)
        except (ValueError, OSError):
            self.usage_label.setText('用量记录暂不可读取，发送前请检查记录文件。')

    def _refresh_rules_summary(self):
        if self._rules_load_error:
            text = '规则读取失败'
        elif self.custom_rules:
            text = f'已启用 · {len(self.custom_rules)} 字'
        else:
            text = '未设置规则'
        self.rules_summary.setText(text)

    def _sync_controls(self, *_):
        busy = self.is_busy or self._closed
        for control in (self.message_edit, self.settings_button, self.new_chat_button,
                        self.attach_button, self.web_search_checkbox, self.rules_button):
            control.setEnabled(not busy)
        self.test_button.setEnabled(not busy and bool(self.settings.api_key))
        self.send_button.setEnabled(not busy and bool(self.message_edit.toPlainText().strip()))
        self.cancel_button.setEnabled(self.is_busy and not self._job.cancel.is_set())
        row = self.attachment_list.currentRow()
        self.remove_button.setEnabled(not busy and 0 <= row < len(self.pending_paths))
        self.download_button.setEnabled(not busy and self.result_list.currentRow() >= 0)

    def _render_chat(self):
        if not self.messages:
            self.transcript.setHtml('<h3 style="color:#96385E">今天需要处理什么？</h3>'
                                    '<p>你可以询问财务知识，也可以上传文件，让助手分析或生成新的文档。</p>'
                                    '<p style="color:#796571">例如：</p><ul>'
                                    '<li>解释“其他应收款”和“其他应付款”的区别。</li>'
                                    '<li>检查工资表的应发、扣款和实发是否一致。</li>'
                                    '<li>生成一份带合计的费用报销汇总表。</li></ul>'
                                    '<p style="color:#796571">会话仅在本次打开期间保留。财务结论和金额请结合原始资料核对。</p>')
            return
        parts = ['<html><body style="font-family:Microsoft YaHei UI;color:#392F38">']
        for role, text in self.messages:
            label = {'user': '你', 'assistant': '财务助手', 'notice': '提示'}[role]
            parts.append(f'<p style="color:#96385E;font-weight:bold">{label}</p>')
            if role == 'assistant':
                doc = _SafeDocument()
                doc.setDefaultFont(self.font())
                doc.setMarkdown(text)
                body = re.search(r'<body[^>]*>(.*)</body>', doc.toHtml(), re.S)
                rendered = body.group(1) if body else html.escape(text)
                # Images and local file references never cause background file reads.
                rendered = re.sub(r'<img\b[^>]*>', '', rendered, flags=re.I)
                parts.append(rendered)
            else:
                parts.append('<p>' + html.escape(text).replace('\n', '<br>') + '</p>')
            parts.append('<hr style="color:#F2DFE9">')
        parts.append('</body></html>')
        self.transcript.setHtml(''.join(parts))
        bar = self.transcript.verticalScrollBar()
        bar.setValue(bar.maximum())

    def _refresh_files(self):
        self.attachment_list.clear()
        for path in self.pending_paths:
            item = QListWidgetItem('待发送 · ' + Path(path).name)
            item.setToolTip(str(path))
            self.attachment_list.addItem(item)
        for uploaded in self.uploaded_files:
            item = QListWidgetItem('已上传 · ' + uploaded.name)
            item.setToolTip('本次会话可继续使用；点击“新对话”清空。')
            self.attachment_list.addItem(item)
        self.result_list.clear()
        for file in self.generated_files:
            self.result_list.addItem(_safe_name(file.name))
        self._sync_controls()

    def choose_files(self):
        paths, _ = QFileDialog.getOpenFileNames(self, '选择要交给财务助手的文件', '', FILE_FILTER)
        self.add_paths(paths)

    def add_paths(self, paths):
        if self.is_busy or self._closed:
            return
        errors = []
        existing = {str(Path(path).resolve()).casefold() for path in self.pending_paths} | {
            str(Path(path).resolve()).casefold() for path in self.source_paths}
        for value in paths:
            path = Path(value)
            try:
                if path.suffix.lower() == '.xls':
                    raise ValueError('旧版 .xls 请先在 Excel 中另存为 .xlsx，再上传。')
                if path.suffix.lower() not in UPLOAD_EXTENSIONS or not path.is_file():
                    raise ValueError('请选择支持的文档或图片文件，不支持文件夹。')
                if path.stat().st_size > MAX_FILE_BYTES:
                    raise ValueError('单个文件不能超过 20 MB。')
                if path.stat().st_size == 0:
                    raise ValueError('不能上传空文件。')
                path = path.resolve()
                if str(path).casefold() in existing:
                    continue
                if len(self.pending_paths) + len(self.uploaded_files) >= MAX_SESSION_FILES:
                    raise ValueError('每次会话最多 10 个文件；可开启新对话继续。')
                existing.add(str(path).casefold())
                self.pending_paths.append(path)
            except (OSError, ValueError) as exc:
                errors.append(str(exc))
        self._refresh_files()
        if errors:
            self._status(' '.join(dict.fromkeys(errors)), error=True)
        elif self.pending_paths:
            self._status('文件已选好，点击“发送”后才会上传。')

    def remove_selected_file(self):
        row = self.attachment_list.currentRow()
        if not self.is_busy and 0 <= row < len(self.pending_paths):
            del self.pending_paths[row]
            self._refresh_files()

    def dragEnterEvent(self, event):
        if not self.is_busy and event.mimeData().hasUrls():
            event.acceptProposedAction()

    def dropEvent(self, event):
        if self.is_busy:
            event.ignore()
            return
        self.add_paths([url.toLocalFile() for url in event.mimeData().urls() if url.isLocalFile()])
        event.acceptProposedAction()

    def _get_client(self):
        if self.client is None:
            self.client = self.client_factory(self.settings)
        return self.client

    def _start_job(self, kind, function):
        self._job_counter += 1
        job = _Job(self._job_counter, kind, function)
        self._job = job
        job.result.connect(self._job_result)
        job.error.connect(self._job_error)
        job.finished.connect(self._job_finished)
        job.text.connect(self._job_text)
        job.status.connect(self._job_status)
        job.uploaded.connect(self._job_uploaded)
        self._sync_controls()
        job.start()

    def _current_job(self, job_id):
        return not self._closed and self._job is not None and self._job.job_id == job_id

    def send_message(self):
        if self.is_busy or self._closed:
            return
        message = self.message_edit.toPlainText().strip()
        if not message:
            return
        if not self.settings.api_key:
            self._status('请先在“连接设置”中填写 API 密钥。', error=True)
            return
        try:
            validate_settings(self.settings)
            self.ledger.check_budget(self.settings.monthly_limit_usd)
            client = self._get_client()
        except (AIError, ValueError, OSError) as exc:
            self._status(exc, error=True)
            return
        pending = list(self.pending_paths)
        files = list(self.uploaded_files)
        history = [dict(item) for item in self.history[-20:]]
        container_id = self.container_id
        web_search = self.web_search_checkbox.isChecked()
        custom_rules = self.custom_rules
        self._current_message = message
        self._partial_text = ''
        self.messages.append(('user', message + ('\n附件：' + '、'.join(p.name for p in pending) if pending else '')))
        self.messages.append(('assistant', ''))
        self._render_chat()
        self._status('正在上传并处理…' if pending else '正在连接 Luna…')

        def work(job):
            new_uploads = []
            try:
                for path in pending:
                    if job.cancel.is_set():
                        raise RequestCancelled()
                    job.emit_safe(job.status, '正在上传：' + path.name)
                    uploaded = client.upload_file(path, cancel=job.cancel)
                    new_uploads.append(uploaded)
                    files.append(uploaded)
                    job.emit_safe(job.uploaded, (path, uploaded))
                if job.cancel.is_set():
                    raise RequestCancelled()
                job.request_started = True
                return client.send(message, files, history, container_id=container_id,
                                   web_search=web_search, cancel=job.cancel,
                                   custom_rules=custom_rules,
                                   on_text=lambda text: job.emit_safe(job.text, text),
                                   on_status=lambda text: job.emit_safe(job.status, text))
            finally:
                if job.abandon:
                    _cleanup_files(client, new_uploads)

        self._start_job('send', work)

    def test_connection(self):
        if self.is_busy or not self.settings.api_key:
            return
        try:
            client = self._get_client()
        except (AIError, ValueError) as exc:
            self._status(exc, error=True)
            return
        self._status('正在检测连接和 Luna 模型权限…')
        self._start_job('test', lambda job: client.test_connection())

    def cancel_request(self):
        if self._job:
            self._job.cancel.set()
            self._status('正在停止等待；已发出的云端请求仍可能完成并产生费用。')
            self._sync_controls()

    @Slot(int, str)
    def _job_status(self, job_id, text):
        if self._current_job(job_id) and not self._job.cancel.is_set():
            self._status(text)

    @Slot(int, str)
    def _job_text(self, job_id, text):
        if self._current_job(job_id) and not self._job.cancel.is_set():
            self._partial_text += text
            self.messages[-1] = ('assistant', self._partial_text)
            self._render_chat()

    @Slot(int, object)
    def _job_uploaded(self, job_id, payload):
        if self._current_job(job_id):
            path, uploaded = payload
            if path in self.pending_paths:
                self.pending_paths.remove(path)
            self.source_paths.add(path)
            if not any(item.file_id == uploaded.file_id for item in self.uploaded_files):
                self.uploaded_files.append(uploaded)
            self._refresh_files()

    @Slot(int, object)
    def _job_result(self, job_id, result):
        if not self._current_job(job_id):
            return
        kind = self._job.kind
        if kind == 'send':
            try:
                self.ledger.record(result.input_tokens, result.output_tokens,
                                   cached_input_tokens=result.cached_input_tokens,
                                   web_search_calls=result.web_search_calls,
                                   container_sessions=result.container_sessions)
            except (ValueError, OSError):
                self.messages.append(('notice', '本次用量未能保存，请以 OpenAI 账单为准。'))
            self._job.result_accounted = True
            self.container_id = result.container_id
            # Completed results remain useful even if Stop was clicked just as the response ended.
            assistant_index = next((i for i in range(len(self.messages) - 1, -1, -1)
                                    if self.messages[i][0] == 'assistant'), None)
            if assistant_index is not None:
                self.messages[assistant_index] = ('assistant', result.text or '处理完成，请查看生成的文件。')
            self.history.extend([{'role': 'user', 'content': self._current_message},
                                 {'role': 'assistant', 'content': result.text}])
            self.history = self.history[-20:]
            known = {(f.container_id, f.file_id) for f in self.generated_files}
            for file in result.files:
                if (file.container_id, file.file_id) not in known:
                    self.generated_files.append(file)
                    known.add((file.container_id, file.file_id))
            self.message_edit.clear()
            self._refresh_files()
            if result.files:
                self.result_list.setCurrentRow(len(self.generated_files) - 1)
            self._render_chat()
            self._refresh_usage()
            self._status('处理完成。生成文件请及时下载，金额和结论请核对。' if result.files else '回答完成。')
        elif kind == 'test':
            self._status(str(result) if not self._job.cancel.is_set() else '已停止等待连接测试。')
        elif kind == 'download':
            self._status('已保存到：' + str(result))

    @Slot(int, object)
    def _job_error(self, job_id, exc):
        if not self._current_job(job_id):
            return
        text = str(exc) or '操作未完成，请稍后重试。'
        if self.settings.api_key:
            text = text.replace(self.settings.api_key, '[密钥已隐藏]')
        if self._job.kind == 'send':
            if getattr(exc, 'request_may_be_billed', False):
                try:
                    self.ledger.record(0, 0, uncertain=True)
                except (ValueError, OSError):
                    pass
                self._job.result_accounted = True
                self._refresh_usage()
            if self.messages and self.messages[-1][0] == 'assistant' and not self.messages[-1][1]:
                self.messages.pop()
            self.messages.append(('notice', text))
            self._render_chat()
        self._status(text, error=not isinstance(exc, RequestCancelled))

    @Slot(int)
    def _job_finished(self, job_id):
        if self._current_job(job_id):
            self._job = None
            self._sync_controls()

    def open_rules(self):
        if self.is_busy or self._closed:
            return
        dialog = AIRulesDialog(self.custom_rules, self.rules_path, self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        self.custom_rules = dialog.value()
        self._rules_load_error = ''
        self._refresh_rules_summary()
        self._status('长期规则已保存，从下一次提问起生效，新对话也会继续使用。'
                     if self.custom_rules else '长期规则已清空，之后的提问将不再附带自定义规则。')

    def open_settings(self):
        if self.is_busy:
            return
        dialog = AISettingsDialog(self.settings, self._remember_key, self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        settings = dialog.value()
        remember = dialog.remember_key.isChecked()
        try:
            save_settings(settings if remember else replace(settings, api_key=''), self.settings_path)
        except (ValueError, OSError) as exc:
            self._status('设置未保存：' + str(exc), error=True)
            return
        changed = (settings.api_key, settings.proxy_mode, settings.proxy_url) != (
            self.settings.api_key, self.settings.proxy_mode, self.settings.proxy_url)
        if changed:
            self._clear_session()
        self.settings, self._remember_key = settings, remember
        # Refresh output limits on the persistent client without losing its container knowledge.
        if self.client is not None and not changed:
            self.client.settings = settings
        self._refresh_usage()
        self._sync_controls()
        self._status('设置已保存。密钥已在本机加密保存。' if remember else '设置已保存。密钥只在本次打开期间使用。')

    def new_chat(self):
        if self.is_busy:
            return
        if self.generated_files:
            answer = QMessageBox.question(self, '开启新对话', '请先下载需要保留的文件。开启新对话后，此页对话和文件列表将清空。',
                                          QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                                          QMessageBox.StandardButton.No)
            if answer != QMessageBox.StandardButton.Yes:
                return
        self._clear_session()
        self._status('已开启新对话。云端附件将尝试清理，未清理成功的上传附件按服务期限自动到期。')

    def _clear_session(self):
        if self.client is not None and self.uploaded_files:
            client, uploads = self.client, list(self.uploaded_files)
            threading.Thread(target=_cleanup_files, args=(client, uploads), daemon=True,
                             name='XiaoYuAICleanup').start()
        self.client = None
        self.container_id = None
        self.pending_paths.clear()
        self.uploaded_files.clear()
        self.source_paths.clear()
        self.generated_files.clear()
        self.history.clear()
        self.messages.clear()
        self.message_edit.clear()
        self._refresh_files()
        self._render_chat()

    def download_selected(self):
        row = self.result_list.currentRow()
        if self.is_busy or not 0 <= row < len(self.generated_files):
            return
        file = self.generated_files[row]
        name = _safe_name(file.name)
        if Path(name).suffix.lower() not in DOWNLOAD_EXTENSIONS:
            self._status('只支持下载文档和图片，请让助手生成 Excel、PDF 或 Word 等文件。', error=True)
            return
        destination, _ = QFileDialog.getSaveFileName(self, '保存生成的文件', name,
                                                    f'生成文件 (*{Path(name).suffix})')
        if not destination:
            return
        target = Path(destination)
        if not target.suffix:
            target = target.with_suffix(Path(name).suffix)
        sources = set(self.source_paths) | set(self.pending_paths)
        try:
            client = self._get_client()
        except (AIError, ValueError) as exc:
            self._status(exc, error=True)
            return
        self._status('正在下载：' + name)

        def work(job):
            content = client.download_file(file, cancel=job.cancel)
            if job.cancel.is_set():
                raise RequestCancelled()
            return save_download(content, target, sources)

        self._start_job('download', work)

    def shutdown(self):
        if self._closed:
            return
        self._closed = True
        if self._job:
            self._job.abandon = True
            self._job.cancel.set()
            if self._job.kind == 'send' and self._job.request_started and not self._job.result_accounted:
                try:
                    self.ledger.record(0, 0, uncertain=True)
                except (ValueError, OSError):
                    pass
                self._job.result_accounted = True
        if self.client is not None and self.uploaded_files:
            threading.Thread(target=_cleanup_files, args=(self.client, list(self.uploaded_files)),
                             daemon=True, name='XiaoYuAICleanup').start()


def _cleanup_files(client, files):
    for file in files:
        try:
            client.delete_file(file.file_id)
        except Exception:
            # Files also have a service-side expiry; cleanup must never block application exit.
            pass
