"""Financial assistant for selected uploads and explicitly linked cloud documents."""
from __future__ import annotations

import html
import json
import os
import queue
import re
import tempfile
import threading
from dataclasses import replace
from functools import partial
from pathlib import Path

from PySide6.QtCore import QByteArray, QObject, QTimer, Qt, QUrl, Signal, Slot
from PySide6.QtGui import QDesktopServices, QTextDocument
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QDialogButtonBox, QDoubleSpinBox,
    QFileDialog, QFormLayout, QFrame, QGridLayout, QHBoxLayout, QLabel, QLineEdit,
    QListWidget, QListWidgetItem, QMessageBox, QPlainTextEdit, QPushButton,
    QScrollArea, QSizePolicy, QSpinBox, QSplitter, QTabWidget, QTextBrowser, QVBoxLayout, QWidget,
)

from .ai_service import AIError, FinanceAIClient, RequestCancelled
from .ai_settings import AISettings, UsageLedger, load_settings, save_settings, validate_settings
from .ai_rules import MAX_RULES_CHARS, load_rules, save_rules, validate_rules
from .config import data_dir
from .wps_service import WpsError, extract_share_links, load_bindings, normalize_share_url
from .wps_ui import WpsDocumentsDialog, WpsLinkDialog


UPLOAD_EXTENSIONS = {'.xlsx', '.csv', '.pdf', '.docx', '.pptx', '.txt', '.md', '.png', '.jpg', '.jpeg'}
DOWNLOAD_EXTENSIONS = UPLOAD_EXTENSIONS | {'.xls', '.zip'}
MAX_FILE_BYTES = 20 * 1024 * 1024
MAX_SESSION_FILES = 10
MAX_TASK_RECORD_BYTES = 32 * 1024 * 1024
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


class _CloudConnectionJob:
    """Only plain Python state crosses the login worker/UI boundary."""
    def __init__(self, service, links, *, resume_message=None):
        self.service = service
        self.links = list(links)
        self.resume_message = resume_message
        self.cancel = threading.Event()
        self.events = queue.SimpleQueue()
        self.result = None
        self.error = None
        self.launch_error = None
        self.thread = threading.Thread(target=self._run, name='XiaoYuWpsConnect', daemon=True)

    def _run(self):
        try:
            on_status = lambda value: self.events.put(('status', str(value)))
            try:
                connected = self.service.status().get('connected')
            except (WpsError, ValueError, OSError):
                connected = False
            if not connected:
                self.service.connect(cancel=self.cancel, on_status=on_status,
                                     open_url=lambda value: self.events.put(('open_url', str(value))))
            if self.cancel.is_set():
                raise RequestCancelled()
            self.result = (self.service.resolve_documents(self.links, cancel=self.cancel, on_status=on_status)
                           if self.links else self.service.list_documents())
        except Exception as exc:
            self.error = exc


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
                 client_factory=FinanceAIClient, settings_path=None, rules_path=None,
                 wps_path=None, cloud_journal_dir=None, cloud_tools_factory=None,
                 wps_account_service=None):
        super().__init__(parent)
        self.setAcceptDrops(True)
        self.setStyleSheet('QListWidget::item:selected {color:#632E49;background:#F8DAE8;}')
        self.settings_path = settings_path
        self.rules_path = rules_path
        if wps_path is None:
            wps_path = getattr(wps_account_service, 'documents_path', None)
        self.wps_path = (Path(settings_path).parent / 'wps_documents.json'
                         if wps_path is None and settings_path is not None else wps_path)
        self.cloud_journal_dir = (Path(cloud_journal_dir) if cloud_journal_dir is not None else
                                  (Path(settings_path).parent if settings_path is not None else data_dir()) / 'wps_tasks')
        self.cloud_tools_factory = cloud_tools_factory
        self._cloud_tools = None
        self._cloud_links = []
        self._selected_cloud_links = []
        self._connection_job = None
        self._connection_timer = QTimer(self)
        self._connection_timer.setInterval(40)
        self._connection_timer.timeout.connect(self._poll_cloud_connection)
        self._account_error = ''
        self.wps_account_service = wps_account_service or None
        if wps_account_service is None:
            try:
                from .wps_skillhub import WpsAccountService
                account_path = (Path(settings_path).parent / 'wps_account.json'
                                if settings_path is not None else None)
                self.wps_account_service = WpsAccountService(path=account_path, documents_path=self.wps_path)
            except (ImportError, WpsError, ValueError, OSError) as exc:
                self._account_error = str(exc)
        self.cloud_results = []
        self._clarification_buttons = []
        self._wps_load_error = ''
        try:
            self.wps_bindings = load_bindings(self.wps_path)
        except (WpsError, ValueError, OSError) as exc:
            self.wps_bindings = []
            self._wps_load_error = str(exc)
        if self.wps_account_service is not None:
            try:
                self._merge_cloud_bindings(self.wps_account_service.list_documents())
            except (WpsError, ValueError, OSError) as exc:
                self._account_error = str(exc)
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
        self._refresh_cloud_summary()
        self._refresh_files()
        self._refresh_usage()
        self._render_chat()
        self._sync_controls()
        errors = []
        if self._load_error:
            errors.append(self._load_error + ' 请打开连接设置重新配置。')
        if self._rules_load_error:
            errors.append(self._rules_load_error + ' 请打开“长期规则”重新保存或恢复备份。')
        if self._wps_load_error:
            errors.append(self._wps_load_error + ' 请打开“云文档”检查连接配置。')
        if errors:
            self._status(' '.join(errors), error=True)
        elif not self.settings.api_key:
            self._status('首次使用请打开“连接设置”，填写 API 密钥。')

    @property
    def is_busy(self):
        return self._job is not None or self._connection_job is not None

    def _build(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)
        title = QHBoxLayout()
        title.addWidget(_label('财务 AI 助手', 'hero'))
        title.addWidget(_label('Luna · 云端处理', 'muted'))
        title.addStretch()
        self.new_chat_button = _button('新对话', self.new_chat)
        self.settings_button = _button('AI 连接设置', self.open_settings)
        self.test_button = _button('测试 AI 连接', self.test_connection)
        for button in (self.new_chat_button, self.settings_button, self.test_button):
            title.addWidget(button)
        layout.addLayout(title)
        layout.addWidget(_label('处理你上传的文件和本次关联的云文档，所需内容发送到 OpenAI 云端。生成文件请及时下载。', 'muted', True))
        preferences = QHBoxLayout()
        self.usage_label = _label('', 'small', True)
        preferences.addWidget(self.usage_label, 1)
        self.rules_summary = _label('', 'small')
        preferences.addWidget(self.rules_summary)
        self.rules_button = _button('长期规则', self.open_rules)
        self.rules_button.setToolTip('设置回答习惯，新对话和重启后继续使用。')
        preferences.addWidget(self.rules_button)
        layout.addLayout(preferences)
        self.main_splitter = QSplitter(Qt.Orientation.Horizontal)
        self.main_splitter.setHandleWidth(10)
        self.main_splitter.setChildrenCollapsible(False)
        self.main_splitter.setStyleSheet('QSplitter::handle {margin:0;background:#FFF1F6;}')
        chat_panel = QWidget()
        chat = QVBoxLayout(chat_panel)
        chat.setContentsMargins(0, 0, 0, 0)
        chat.setSpacing(8)
        self.transcript = SafeTranscript()
        self.transcript.setMinimumWidth(320)
        chat.addWidget(self.transcript, 1)
        self.question_panel = QFrame()
        self.question_panel.setObjectName('card')
        self.question_layout = QVBoxLayout(self.question_panel)
        self.question_layout.setContentsMargins(10, 8, 10, 8)
        self.question_scroll = QScrollArea()
        self.question_scroll.setWidgetResizable(True)
        self.question_scroll.setMinimumHeight(80)
        self.question_scroll.setMaximumHeight(180)
        self.question_scroll.setWidget(self.question_panel)
        self.question_scroll.hide()
        chat.addWidget(self.question_scroll)
        self.cloud_chip = QFrame()
        self.cloud_chip.setObjectName('card')
        chip_row = QHBoxLayout(self.cloud_chip)
        chip_row.setContentsMargins(8, 4, 8, 4)
        self.cloud_chip_label = _label('', 'small', True)
        self.cloud_chip_label.setTextFormat(Qt.TextFormat.PlainText)
        chip_row.addWidget(self.cloud_chip_label, 1)
        self.cloud_chip_button = _button('查看', lambda: self.cloud_tabs.setCurrentIndex(1))
        chip_row.addWidget(self.cloud_chip_button)
        self.cloud_chip.hide()
        chat.addWidget(self.cloud_chip)
        self.message_edit = MessageEdit()
        self.message_edit.setPlaceholderText('描述要处理的内容，或粘贴 WPS 云文档链接。\n例如：按业务单号更新到账金额，未匹配的数据保留，无需填写行号。')
        self.message_edit.setMinimumHeight(80)
        self.message_edit.setMaximumHeight(110)
        self.message_edit.setStyleSheet('QPlainTextEdit {background:#FFFCFD;border:1px solid #ECCDDD;'
                                       'border-radius:10px;padding:8px;selection-background-color:#EDBBD1;}')
        self.message_edit.textChanged.connect(self._message_changed)
        chat.addWidget(self.message_edit)
        input_tools = QHBoxLayout()
        self.input_attach_button = _button('上传文件', self.choose_files)
        self.add_cloud_button = _button('添加云文档', self.add_cloud_link)
        input_tools.addWidget(self.input_attach_button)
        input_tools.addWidget(self.add_cloud_button)
        input_tools.addStretch()
        self.web_search_checkbox = QCheckBox('联网核实政策和来源')
        self.web_search_checkbox.setToolTip('开启后允许助手查询公开网页，会增加搜索费用。不要在搜索中填写个人敏感信息。')
        input_tools.addWidget(self.web_search_checkbox)
        chat.addLayout(input_tools)
        actions = QHBoxLayout()
        actions.addWidget(_label('Enter 发送 · Shift/Alt+Enter 换行', 'small'))
        actions.addStretch()
        self.cancel_button = _button('停止等待', self.cancel_request)
        self.send_button = _button('发送', self.send_message, primary=True)
        self.message_edit.send_requested.connect(self.send_button.click)
        actions.addWidget(self.cancel_button)
        actions.addWidget(self.send_button)
        chat.addLayout(actions)
        self.status_label = _label('可以提问，或添加文件后发送处理要求。', 'notice', True)
        self.status_label.setTextFormat(Qt.TextFormat.PlainText)
        chat.addWidget(self.status_label)
        self.main_splitter.addWidget(chat_panel)

        self.cloud_tabs = QTabWidget()
        self.cloud_tabs.setMinimumWidth(305)
        self.cloud_tabs.setMaximumWidth(520)
        self.cloud_tabs.setStyleSheet('QTabWidget::pane {border:1px solid #ECCDDD;border-radius:10px;'
                                     'background:#FFFCFD;} QTabBar::tab {padding:8px 24px;'
                                     'border:0;color:#796571;} QTabBar::tab:selected '
                                     '{background:#F8DAE8;color:#632E49;border-radius:8px;}')
        file_panel = QWidget()
        self.cloud_tabs.addTab(file_panel, '文件')
        side = QVBoxLayout(file_panel)
        side.setContentsMargins(10, 10, 10, 10)
        attachment_header = QHBoxLayout()
        attachment_header.addWidget(_label('本次文件', 'sectionTitle'))
        attachment_header.addStretch()
        self.attach_button = _button('添加', self.choose_files)
        attachment_header.addWidget(self.attach_button)
        side.addLayout(attachment_header)
        side.addWidget(_label('每个最多 20 MB，每次会话最多 10 个\n可直接拖入 Excel、PDF、Word 等文件', 'small', True))
        self.attachment_list = QListWidget()
        self.attachment_list.setMinimumHeight(80)
        side.addWidget(self.attachment_list, 1)
        self.remove_button = _button('移除选中的待发送文件', self.remove_selected_file)
        side.addWidget(self.remove_button)
        side.addWidget(_label('生成的文件', 'sectionTitle'))
        self.result_list = QListWidget()
        self.result_list.setMinimumHeight(60)
        side.addWidget(self.result_list, 1)
        self.download_button = _button('下载选中的文件', self.download_selected)
        side.addWidget(self.download_button)
        side.addWidget(_label('已上传附件可在本次对话中继续使用。\n生成文件请及时下载。', 'small', True))

        cloud_panel = QWidget()
        side = QVBoxLayout(cloud_panel)
        side.setContentsMargins(10, 10, 10, 10)
        side.setSpacing(8)
        account_header = QHBoxLayout()
        account_header.addWidget(_label('WPS 账号', 'sectionTitle'))
        account_header.addStretch()
        self.account_connect_button = _button('连接 WPS', self.connect_wps_account)
        account_header.addWidget(self.account_connect_button)
        side.addLayout(account_header)
        self.account_status_label = _label('', 'small', True)
        self.account_status_label.setTextFormat(Qt.TextFormat.PlainText)
        side.addWidget(self.account_status_label)
        side.addWidget(_label('本次使用', 'sectionTitle'))
        self.cloud_summary = _label('', 'small', True)
        self.cloud_summary.setTextFormat(Qt.TextFormat.PlainText)
        side.addWidget(self.cloud_summary)
        self.current_cloud_list = QListWidget()
        self.current_cloud_list.setWordWrap(True)
        self.current_cloud_list.setMinimumHeight(66)
        self.current_cloud_list.setMaximumHeight(112)
        self.current_cloud_list.setAccessibleName('本次使用的云文档')
        side.addWidget(self.current_cloud_list)
        current_actions = QHBoxLayout()
        self.open_current_cloud_button = _button('打开文档', self.open_current_cloud)
        self.remove_current_cloud_button = _button('移出本次', self.remove_current_cloud)
        current_actions.addWidget(self.open_current_cloud_button)
        current_actions.addWidget(self.remove_current_cloud_button)
        side.addLayout(current_actions)
        self.cloud_mode = QComboBox()
        self.cloud_mode.addItem('按要求自动处理', False)
        self.cloud_mode.addItem('只预览，不写入', True)
        self.cloud_mode.setToolTip('作用于本次使用的云文档。自动模式仅在你明确要求更新时写入。')
        side.addWidget(self.cloud_mode)
        side.addWidget(_label('最近使用', 'sectionTitle'))
        self.recent_cloud_list = QListWidget()
        self.recent_cloud_list.setMinimumHeight(54)
        self.recent_cloud_list.setMaximumHeight(110)
        self.recent_cloud_list.setAccessibleName('最近使用的云文档')
        side.addWidget(self.recent_cloud_list)
        self.use_recent_cloud_button = _button('将选中文档用于本次', self.use_recent_cloud)
        side.addWidget(self.use_recent_cloud_button)
        history_header = QHBoxLayout()
        history_header.addWidget(_label('处理记录', 'sectionTitle'))
        history_header.addStretch()
        self.cloud_history_button = _button('查看历史', self.show_cloud_history)
        history_header.addWidget(self.cloud_history_button)
        side.addLayout(history_header)
        self.cloud_result_list = QListWidget()
        self.cloud_result_list.setWordWrap(True)
        self.cloud_result_list.setMinimumHeight(85)
        side.addWidget(self.cloud_result_list, 1)
        cloud_actions = QHBoxLayout()
        self.open_cloud_button = _button('打开文档', self.open_cloud_result)
        self.cloud_details_button = _button('修改明细', self.show_cloud_details)
        cloud_actions.addWidget(self.open_cloud_button)
        cloud_actions.addWidget(self.cloud_details_button)
        side.addLayout(cloud_actions)
        self.cloud_button = _button('旧版连接设置', self.open_cloud_documents)
        self.cloud_button.setToolTip('兼容之前使用脚本调用地址和令牌连接的文档。')
        side.addWidget(self.cloud_button)
        cloud_scroll = QScrollArea()
        cloud_scroll.setWidgetResizable(True)
        cloud_scroll.setFrameShape(QFrame.Shape.NoFrame)
        cloud_scroll.setWidget(cloud_panel)
        self.cloud_tabs.addTab(cloud_scroll, '云文档')
        self.main_splitter.addWidget(self.cloud_tabs)
        self.main_splitter.setStretchFactor(0, 3)
        self.main_splitter.setStretchFactor(1, 1)
        self.main_splitter.setSizes([650, 330])
        layout.addWidget(self.main_splitter, 1)
        self.attachment_list.currentRowChanged.connect(self._sync_controls)
        self.result_list.currentRowChanged.connect(self._sync_controls)
        self.cloud_result_list.currentRowChanged.connect(self._sync_controls)
        self.current_cloud_list.currentRowChanged.connect(self._sync_controls)
        self.recent_cloud_list.currentRowChanged.connect(self._sync_controls)

    def _status(self, text, error=False):
        self.status_label.setText(self._redact(text))
        self.status_label.setProperty('tone', 'error' if error else '')
        self.status_label.style().unpolish(self.status_label)
        self.status_label.style().polish(self.status_label)

    def _redact(self, text):
        value = str(text)
        for key in (self.settings.api_key, *(item.token for item in self.wps_bindings)):
            if key:
                value = value.replace(key, '[密钥已隐藏]')
        return value

    def _refresh_cloud_summary(self):
        state = self._account_state()
        if self._connection_job is not None:
            account_text = '正在连接，请按浏览器提示完成登录。'
        elif state.get('connected'):
            account_text = '已连接' + (' · ' + str(state['display_name']) if state.get('display_name') else '')
            account_text += '\n每份文档仍按你的编辑权限处理。'
        elif state.get('status') == 'expired':
            account_text = '登录已过期，重新连接后可继续。'
        elif self._account_error:
            account_text = 'WPS 连接暂不可用，请重新连接。'
        else:
            account_text = '尚未连接。首次使用时完成 WPS 登录即可。'
        self.account_status_label.setText(account_text)
        self.account_connect_button.setText('断开连接' if state.get('connected') else '连接 WPS')
        self.cloud_button.setVisible(any(getattr(item, 'provider', 'airscript') == 'airscript'
                                        for item in self.wps_bindings))
        by_url = {normalize_share_url(item.share_url): item for item in self.wps_bindings}
        previous = self.current_cloud_list.currentRow()
        self.current_cloud_list.blockSignals(True)
        self.current_cloud_list.clear()
        names = []
        for link in self._selected_cloud_links:
            binding = by_url.get(link)
            name = binding.label if binding is not None else '待识别的云文档'
            names.append(name)
            if binding is None:
                status = '发送时连接并识别' if state.get('connected') else '发送时需要登录 WPS'
            elif getattr(binding, 'provider', 'airscript') == 'skillhub' and not state.get('connected'):
                status = '需要重新连接 WPS'
            elif (getattr(binding, 'provider', 'airscript') == 'skillhub'
                  and getattr(binding, 'account_id', '') != state.get('account_id')):
                status = '发送时重新核对文档权限'
            else:
                status = '已关联 · 处理前检查编辑权限'
                metadata = getattr(self._cloud_tools, 'document_metadata', {}).get(link, {})
                if metadata.get('can_edit') is True:
                    status = '已核实可编辑 · 执行时再次检查'
                elif metadata.get('can_edit') is False:
                    status = '当前账号仅可读取，无法更新'
            snapshots = getattr(self._cloud_tools, 'snapshots', {})
            sheet_names = []
            for snapshot in snapshots.values():
                if snapshot.get('document_url') == link and snapshot.get('sheet') not in sheet_names:
                    sheet_names.append(snapshot['sheet'])
            if sheet_names:
                status += '\n工作表：' + '、'.join(sheet_names)
            item = QListWidgetItem(name + '\n' + status)
            item.setData(Qt.ItemDataRole.UserRole, link)
            item.setToolTip(link)
            self.current_cloud_list.addItem(item)
        self.current_cloud_list.setCurrentRow(min(max(previous, 0), len(names) - 1))
        current_height = sum(max(34, self.current_cloud_list.sizeHintForRow(row))
                             for row in range(self.current_cloud_list.count()))
        self.current_cloud_list.setFixedHeight(min(112, max(54, current_height + 8)))
        self.current_cloud_list.blockSignals(False)
        self.cloud_summary.setText('仅处理下方选定的文档。' if names else '粘贴链接，或从最近使用中添加。')
        self.cloud_chip_label.setText('本次云表 · ' + '、'.join(names))
        self.cloud_chip_label.setToolTip('\n'.join(self._selected_cloud_links))
        self.cloud_chip.setVisible(bool(names))
        self.cloud_mode.setVisible(bool(names))
        previous_recent = self.recent_cloud_list.currentRow()
        self.recent_cloud_list.blockSignals(True)
        self.recent_cloud_list.clear()
        for binding in reversed(self.wps_bindings):
            item = QListWidgetItem(binding.label)
            item.setData(Qt.ItemDataRole.UserRole, normalize_share_url(binding.share_url))
            item.setToolTip(binding.share_url)
            self.recent_cloud_list.addItem(item)
        self.recent_cloud_list.setCurrentRow(min(max(previous_recent, 0), self.recent_cloud_list.count() - 1))
        recent_height = sum(max(22, self.recent_cloud_list.sizeHintForRow(row))
                            for row in range(self.recent_cloud_list.count()))
        self.recent_cloud_list.setFixedHeight(min(96, max(34, recent_height + 8)))
        self.recent_cloud_list.blockSignals(False)

    def _account_state(self):
        if self.wps_account_service is None:
            return {'connected': False, 'status': 'disconnected'}
        try:
            state = self.wps_account_service.status()
            return state if isinstance(state, dict) else {'connected': False}
        except (WpsError, ValueError, OSError):
            return {'connected': False, 'status': 'expired'}

    def _merge_cloud_bindings(self, bindings):
        by_url = {normalize_share_url(item.share_url): item for item in self.wps_bindings}
        for binding in bindings:
            link = normalize_share_url(binding.share_url)
            by_url.pop(link, None)
            by_url[link] = binding
        self.wps_bindings = list(by_url.values())

    def _message_changed(self):
        links = extract_share_links(self.message_edit.toPlainText())
        if links and links != self._selected_cloud_links:
            self._selected_cloud_links = links
            self._refresh_cloud_summary()
            self.cloud_tabs.setCurrentIndex(1)
        self._sync_controls()

    def add_cloud_link(self):
        if self.is_busy or self._closed:
            return
        dialog = WpsLinkDialog(self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self._select_cloud_link(dialog.value())

    def _select_cloud_link(self, link):
        link = normalize_share_url(link)
        if link not in self._selected_cloud_links:
            self._selected_cloud_links.append(link)
        self._refresh_cloud_summary()
        self.cloud_tabs.setCurrentIndex(1)
        self._sync_controls()
        self.message_edit.setFocus()

    def use_recent_cloud(self):
        if self.is_busy or self._closed:
            return
        item = self.recent_cloud_list.currentItem()
        if item is not None:
            self._select_cloud_link(item.data(Qt.ItemDataRole.UserRole))

    def open_current_cloud(self):
        item = self.current_cloud_list.currentItem()
        if not self.is_busy and item is not None:
            QDesktopServices.openUrl(QUrl(normalize_share_url(item.data(Qt.ItemDataRole.UserRole))))

    def remove_current_cloud(self):
        item = self.current_cloud_list.currentItem()
        if self.is_busy or item is None:
            return
        link = item.data(Qt.ItemDataRole.UserRole)
        self._selected_cloud_links = [value for value in self._selected_cloud_links if value != link]
        # A link left in the draft would otherwise silently re-associate it on Send.
        pattern = r'https://(?:www\.)?kdocs\.cn/l/[A-Za-z0-9_-]+/?(?:\?[^\s<>]+)?(?:#[^\s<>]+)?'
        text = re.sub(pattern, lambda match: '' if normalize_share_url(match.group()) == link else match.group(),
                      self.message_edit.toPlainText())
        self.message_edit.blockSignals(True)
        self.message_edit.setPlainText(text)
        self.message_edit.blockSignals(False)
        self._cloud_tools = None
        self._cloud_links = []
        self._refresh_cloud_summary()
        self._sync_controls()

    def connect_wps_account(self):
        if not self.is_busy and not self._closed:
            if self.wps_account_service is not None and self._account_state().get('connected'):
                try:
                    self.wps_account_service.disconnect()
                except (WpsError, ValueError, OSError) as exc:
                    self._status('暂时无法断开 WPS 连接：' + str(exc), error=True)
                    return
                self._cloud_tools = None
                self._refresh_cloud_summary()
                self._sync_controls()
                self._status('已断开本机 WPS 连接。文档记录仍保留，再次使用时重新登录。')
                return
            self._start_cloud_connection([])

    def _start_cloud_connection(self, links, *, resume_message=None):
        if self.wps_account_service is None:
            self._status('WPS 账号连接组件暂不可用，请检查软件文件。旧版文档仍可通过连接设置使用。', error=True)
            return False
        job = _CloudConnectionJob(self.wps_account_service, links, resume_message=resume_message)
        self._connection_job = job
        self._account_error = ''
        self.cloud_tabs.setCurrentIndex(1)
        self._refresh_cloud_summary()
        self._sync_controls()
        self._status('正在准备 WPS 连接；本次要求和附件已保留，连接后将继续。'
                     if resume_message is not None else '正在连接 WPS，请按浏览器提示完成登录。')
        try:
            job.thread.start()
        except RuntimeError:
            self._connection_job = None
            self._refresh_cloud_summary()
            self._sync_controls()
            self._status('无法开始 WPS 连接，请稍后重试。', error=True)
            return False
        self._connection_timer.start()
        return True

    @Slot()
    def _poll_cloud_connection(self):
        job = self._connection_job
        if job is None or self._closed:
            self._connection_timer.stop()
            return
        while not job.events.empty():
            kind, value = job.events.get()
            if job.cancel.is_set():
                continue
            if kind == 'status':
                self._status(value)
            elif kind == 'open_url':
                url = QUrl(value)
                if (url.scheme() == 'https' and url.host() == 'mcp-center.wps.cn'
                        and url.path() == '/kdocs-auth/auth-guide' and not url.userInfo()
                        and url.port(-1) in (-1, 443)):
                    if not QDesktopServices.openUrl(url):
                        job.launch_error = '无法打开 WPS 登录页，请检查默认浏览器后重试。'
                        job.cancel.set()
                        self._status(job.launch_error, error=True)
                else:
                    job.launch_error = 'WPS 登录地址无效，已停止连接。'
                    job.cancel.set()
                    self._status(job.launch_error, error=True)
        if job.thread.is_alive():
            return
        self._connection_timer.stop()
        self._connection_job = None
        cancelled = (job.cancel.is_set() or isinstance(job.error, RequestCancelled)
                     or getattr(job.error, 'code', None) == 'CANCELLED')
        if not cancelled and job.error is None:
            try:
                found = {normalize_share_url(item.share_url) for item in (job.result or [])}
                if any(link not in found for link in job.links):
                    raise WpsError('没有识别到全部目标文档，请检查链接和编辑权限。')
                self._merge_cloud_bindings(job.result or [])
                state = self._account_state()
                if job.links and any(getattr(item, 'provider', 'airscript') == 'skillhub' and (
                        not state.get('connected') or item.account_id != state.get('account_id'))
                        for item in job.result or []):
                    raise WpsError('WPS 登录状态已变化，请重新连接后再发送。')
                self._wps_load_error = ''
                self._account_error = ''
                self._cloud_tools = None
            except (WpsError, ValueError, OSError) as exc:
                job.error = exc
        if not cancelled and job.error is not None:
            self._account_error = str(job.error)
        self._refresh_cloud_summary()
        self._sync_controls()
        if job.launch_error:
            self._status(job.launch_error + '\n本次要求和附件已保留。', error=True)
        elif cancelled:
            self._status('已停止 WPS 连接。本次要求和附件已保留。')
        elif job.error is not None:
            self._status('WPS 连接未完成：' + str(job.error) + '\n本次要求和附件已保留。', error=True)
        elif job.resume_message is not None:
            # The editor and attachment controls stayed locked throughout login.
            if self.message_edit.toPlainText().strip() == job.resume_message:
                self.send_message()
            else:
                self._status('WPS 已连接。本次要求已变化，请检查后发送。')
        else:
            self._status('WPS 已连接。粘贴云文档链接并说明处理要求即可。')

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
                        self.attach_button, self.web_search_checkbox, self.rules_button,
                        self.cloud_button, self.cloud_mode, self.cloud_history_button,
                        self.input_attach_button, self.add_cloud_button, self.account_connect_button):
            control.setEnabled(not busy)
        self.test_button.setEnabled(not busy and bool(self.settings.api_key))
        self.send_button.setEnabled(not busy and bool(self.message_edit.toPlainText().strip()))
        active_job = self._job or self._connection_job
        self.cancel_button.setEnabled(active_job is not None and not active_job.cancel.is_set())
        row = self.attachment_list.currentRow()
        self.remove_button.setEnabled(not busy and 0 <= row < len(self.pending_paths))
        self.download_button.setEnabled(not busy and self.result_list.currentRow() >= 0)
        selected = 0 <= self.cloud_result_list.currentRow() < len(self.cloud_results)
        self.open_cloud_button.setEnabled(not busy and selected)
        self.cloud_details_button.setEnabled(not busy and selected)
        selected_current = self.current_cloud_list.currentItem() is not None
        self.open_current_cloud_button.setEnabled(not busy and selected_current)
        self.remove_current_cloud_button.setEnabled(not busy and selected_current)
        self.use_recent_cloud_button.setEnabled(not busy and self.recent_cloud_list.currentItem() is not None)
        for button in self._clarification_buttons:
            button.setEnabled(not busy)

    def _render_chat(self):
        if not self.messages:
            self.transcript.setHtml('<h3 style="color:#96385E">今天需要处理什么？</h3>'
                                    '<p>你可以询问财务知识，上传文件生成结果，或关联 WPS 云文档按规则更新。</p>'
                                    '<p style="color:#796571">例如：</p><ul>'
                                    '<li>解释“其他应收款”和“其他应付款”的区别。</li>'
                                    '<li>检查工资表的应发、扣款和实发是否一致。</li>'
                                    '<li>生成一份带合计的费用报销汇总表。</li>'
                                    '<li>按业务单号，把来源表的实收金额更新到指定云文档。</li></ul>'
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

    def _requested_cloud_links(self, message):
        """Resolve history before login, so a recheck can resume after reconnecting."""
        links = extract_share_links(message)
        historical_check = False
        # A task number alone does not authorize reading local history. Resolve
        # only an explicit request to check the named cloud-document task.
        task_match = re.search(r'(?:核对|检查|查询|复核)\s*(?:一下)?\s*任务(?:编号)?\s*[:：]?\s*'
                               r'([A-Fa-f0-9]{32})(?![A-Za-z0-9])', message)
        if not links and task_match:
            task_id = task_match.group(1).lower()
            try:
                root = self.cloud_journal_dir.resolve()
                path = self.cloud_journal_dir / (task_id + '.json')
                if (path.is_symlink() or not path.resolve().is_relative_to(root)
                        or path.stat().st_size > MAX_TASK_RECORD_BYTES):
                    raise ValueError('invalid task path')
                record = json.loads(path.read_text(encoding='utf-8'))
                if (not isinstance(record, dict) or type(record.get('schema')) is not int
                        or record['schema'] != 1 or record.get('task_id') != task_id):
                    raise ValueError('invalid task record')
                links = [normalize_share_url(record.get('document_url', ''))]
            except (WpsError, ValueError, OSError, TypeError):
                raise ValueError('找不到这条有效的本机任务记录。请核对任务编号，或在“历史任务”中选择要检查的任务。') from None
            historical_check = True
        if not links:
            links = list(self._selected_cloud_links)
        return links, historical_check

    def _cloud_context_for(self, message, history):
        if any(binding.token and binding.token in message for binding in self.wps_bindings):
            raise ValueError('请不要把脚本令牌写在聊天里；令牌仅需填写在“云文档”连接设置中。')
        links, historical_check = self._requested_cloud_links(message)
        if links:
            if self._wps_load_error:
                raise ValueError('云文档配置读取失败，请先打开“云文档”检查。')
            by_url = {normalize_share_url(binding.share_url): binding for binding in self.wps_bindings}
            unknown = [link for link in links if link not in by_url]
            if unknown:
                raise ValueError('这次链接中有尚未绑定的 WPS 文档。请先打开“云文档”添加并只读测试，再发送这条要求。')
            if links != self._cloud_links or self._cloud_tools is None or historical_check:
                factory = self.cloud_tools_factory
                options = {'allow_write': False, 'journal_dir': self.cloud_journal_dir}
                if factory is None:
                    from .cloud_tools import CloudDocumentTools
                    from .wps_adapter import create_wps_client
                    factory = CloudDocumentTools
                    options['client_factory'] = partial(create_wps_client,
                        account_path=getattr(self.wps_account_service, 'path', None))
                self._cloud_tools = factory([by_url[link] for link in links], **options)
                self._cloud_links = list(links)
                self._selected_cloud_links = list(links)
                self._refresh_cloud_summary()
        if self._cloud_tools is not None:
            self._cloud_tools.begin_turn(message, history=history,
                                          force_preview=historical_check or bool(self.cloud_mode.currentData()))
        return self._cloud_tools

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
            if any(binding.token and binding.token in message for binding in self.wps_bindings):
                raise ValueError('请不要把脚本令牌写在聊天里；令牌仅需填写在“云文档”连接设置中。')
            links, _ = self._requested_cloud_links(message)
            if links and self.wps_account_service is not None and not self._wps_load_error:
                by_url = {normalize_share_url(item.share_url): item for item in self.wps_bindings}
                state = self._account_state()
                unresolved = [link for link in links if link not in by_url or (
                    getattr(by_url[link], 'provider', 'airscript') == 'skillhub' and (
                        not state.get('connected') or by_url[link].account_id != state.get('account_id')))]
                if unresolved:
                    self._start_cloud_connection(unresolved, resume_message=message)
                    return
            previous_links = list(self._cloud_links)
            cloud_tools = self._cloud_context_for(message, self.history[-20:])
            if self._cloud_links and self._cloud_links != previous_links and not extract_share_links(message):
                message += '\n本次云文档：\n' + '\n'.join(self._cloud_links)
            client = self._get_client()
        except (AIError, WpsError, ValueError, OSError) as exc:
            self._status(exc, error=True)
            return
        pending = list(self.pending_paths)
        files = list(self.uploaded_files)
        history = [dict(item) for item in self.history[-20:]]
        container_id = self.container_id
        web_search = self.web_search_checkbox.isChecked()
        custom_rules = self.custom_rules
        self._clear_cloud_questions()
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
                options = {'cloud_tools': cloud_tools} if cloud_tools is not None else {}
                return client.send(message, files, history, container_id=container_id,
                                   web_search=web_search, cancel=job.cancel,
                                   custom_rules=custom_rules,
                                   on_text=lambda text: job.emit_safe(job.text, text),
                                   on_status=lambda text: job.emit_safe(job.status, text), **options)
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
        if self._connection_job is not None:
            self._connection_job.cancel.set()
            self._status('正在停止 WPS 连接；本次要求和附件会保留。')
            self._sync_controls()
            return
        if self._job:
            self._job.cancel.set()
            self._status('正在停止后续操作；已提交的云端请求仍可能完成。云文档结果请查看任务记录。'
                         if self._cloud_tools is not None else
                         '正在停止等待；已发出的云端请求仍可能完成并产生费用。')
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
                self.cloud_tabs.setCurrentIndex(0)
            self._render_chat()
            self._refresh_usage()
            cloud_results = getattr(result, 'cloud_results', [])
            self._record_cloud_results(cloud_results)
            self._show_cloud_questions()
            if cloud_results:
                self._status('云文档处理结果已记录：' + self._cloud_result_text(cloud_results[-1]))
            else:
                self._status('处理完成。生成文件请及时下载，金额和结论请核对。' if result.files else '回答完成。')
        elif kind == 'test':
            self._status(str(result) if not self._job.cancel.is_set() else '已停止等待连接测试。')
        elif kind == 'download':
            self._status('已保存到：' + str(result))

    @Slot(int, object)
    def _job_error(self, job_id, exc):
        if not self._current_job(job_id):
            return
        text = self._redact(str(exc) or '操作未完成，请稍后重试。')
        if self._job.kind == 'send':
            cloud_results = getattr(exc, 'cloud_results', [])
            if not cloud_results and self._cloud_tools is not None:
                cloud_results = getattr(self._cloud_tools, 'results', [])
            self._record_cloud_results(cloud_results)
            self._show_cloud_questions()
            if cloud_results:
                text += '\n云文档执行记录已保留：' + self._cloud_result_text(cloud_results[-1])
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
            self._refresh_cloud_summary()
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

    def open_cloud_documents(self):
        if self.is_busy or self._closed:
            return
        dialog = WpsDocumentsDialog(self.wps_bindings, self.wps_path, self)
        dialog.exec()
        if dialog.changed:
            self.wps_bindings = list(dialog.bindings)
            self._wps_load_error = ''
            self._cloud_tools = None
            self._cloud_links = []
            self._refresh_cloud_summary()
            self._sync_controls()
            self._status('云文档连接已更新。请在聊天里粘贴目标链接并说明处理要求。')

    def _clear_cloud_questions(self):
        self._clarification_buttons.clear()
        while self.question_layout.count():
            item = self.question_layout.takeAt(0)
            if item.widget() is not None:
                item.widget().deleteLater()
        self.question_scroll.hide()

    def _show_cloud_questions(self):
        self._clear_cloud_questions()
        questions = getattr(self._cloud_tools, 'questions', []) if self._cloud_tools is not None else []
        if not isinstance(questions, list):
            return
        shown = 0
        for item in questions:
            if not isinstance(item, dict) or not isinstance(item.get('question'), str) or not item['question'].strip():
                continue
            question = self._redact(item['question'])
            card = QWidget()
            box = QVBoxLayout(card)
            box.setContentsMargins(0, 0, 0, 0)
            label = _label(question, 'sectionTitle', True)
            label.setTextFormat(Qt.TextFormat.PlainText)
            box.addWidget(label)
            choices = QGridLayout()
            options = item.get('options', [])
            if not isinstance(options, list):
                options = []
            for index, option in enumerate(options[:4]):
                if not isinstance(option, str) or not option.strip():
                    continue
                option = self._redact(option)
                caption = option if len(option) <= 28 else option[:27] + '…'
                button = _button(caption.replace('&', '&&'))
                button.setToolTip(option)
                button.setAccessibleName(option)
                button.clicked.connect(lambda checked=False, q=question, answer=option:
                                       self._answer_cloud_question(q, answer))
                choices.addWidget(button, index // 2, index % 2)
                self._clarification_buttons.append(button)
            box.addLayout(choices)
            box.addWidget(_label('可点击选项发送，也可以在下方输入自己的回答。', 'small', True))
            self.question_layout.addWidget(card)
            shown += 1
        self.question_scroll.setVisible(shown > 0)
        self._sync_controls()

    def _answer_cloud_question(self, question, answer):
        if self.is_busy or self._closed:
            return
        self.message_edit.setPlainText(f'针对“{question}”：{answer}')
        self.send_message()

    @staticmethod
    def _cloud_result_text(result):
        if not isinstance(result, dict):
            return '结果待核对'
        def count(key):
            value = result.get(key, 0)
            return value if type(value) is int and value >= 0 else 0
        current = result.get('current_check')
        if isinstance(current, dict):
            def checked_count(key):
                value = current.get(key, 0)
                return value if type(value) is int and value >= 0 else 0
            differences = sum(checked_count(key) for key in ('changed', 'missing', 'duplicate'))
            if current.get('failed') is True:
                state = '历史结果已保留 · 本次核对未完成'
            elif differences:
                history = '历史更新已完成' if result.get('status') in ('completed', 'complete', 'success') else '历史结果已保留'
                state = f'{history} · 当前有 {differences} 项不同/无法定位'
            elif checked_count('checked') and checked_count('matched') == checked_count('checked'):
                state = f"本次核对一致 · 共 {checked_count('matched')} 项"
            elif not checked_count('checked'):
                state = '历史结果已保留 · 本次没有可核对项目'
            else:
                state = '历史结果已保留 · 本次核对未完成'
            return state + f" · 历史已核对更新 {count('updated')} 条"
        labels = {'complete': '已核对完成', 'completed': '已核对完成', 'success': '已核对完成',
                  'preview': '修改预览', 'prepared': '修改预览', 'running': '正在处理',
                  'partial': '部分完成', 'unknown': '结果待核对', 'uncertain': '结果待核对',
                  'failed': '未完成', 'cancelled': '已停止', 'conflict': '有冲突待处理'}
        state = labels.get(result.get('status'), '请查看明细')
        if count('unknown'):
            state = '结果待核对'
        elif count('conflicts') and state == '已核对完成':
            state = '部分完成，有冲突'
        if result.get('status') in ('prepared', 'preview') and isinstance(result.get('summary'), str):
            return state + ' · ' + result['summary']
        return (f"{state} · 更新 {count('updated')} · 未变化 {count('unchanged')}"
                f" · 跳过 {count('skipped')} · 冲突 {count('conflicts')} · 待核对 {count('unknown')}")

    def _record_cloud_results(self, results):
        if not isinstance(results, list):
            return
        for result in results:
            if not isinstance(result, dict):
                continue
            try:
                normalize_share_url(result.get('document_url', ''))
            except (WpsError, ValueError, TypeError):
                continue
            identity = (result.get('task_id'), result.get('document_url'))
            old = next((i for i, item in enumerate(self.cloud_results)
                        if identity[0] and (item.get('task_id'), item.get('document_url')) == identity), None)
            if old is None:
                self.cloud_results.append(dict(result))
            else:
                self.cloud_results[old] = dict(result)
        self.cloud_result_list.clear()
        for result in self.cloud_results:
            name = str(result.get('document_name') or '云文档')
            item = QListWidgetItem(name + '\n' + self._cloud_result_text(result))
            item.setToolTip(self._redact(result.get('summary') or self._cloud_result_text(result)))
            self.cloud_result_list.addItem(item)
        if self.cloud_results:
            self.cloud_result_list.setCurrentRow(len(self.cloud_results) - 1)
            self.cloud_tabs.setCurrentIndex(1)
        self._sync_controls()

    def open_cloud_result(self):
        row = self.cloud_result_list.currentRow()
        if self.is_busy or not 0 <= row < len(self.cloud_results):
            return
        try:
            url = normalize_share_url(self.cloud_results[row]['document_url'])
        except (WpsError, ValueError, KeyError, TypeError):
            self._status('任务记录中的文档链接无效。', error=True)
            return
        QDesktopServices.openUrl(QUrl(url))

    def show_cloud_details(self):
        row = self.cloud_result_list.currentRow()
        if self.is_busy or not 0 <= row < len(self.cloud_results):
            return
        result = self.cloud_results[row]
        record = result
        detail_error = ''
        if result.get('details_path'):
            try:
                path = Path(result['details_path'])
                root = self.cloud_journal_dir.resolve()
                if (path.is_symlink() or path.suffix.lower() != '.json'
                        or not path.resolve().is_relative_to(root) or path.stat().st_size > MAX_TASK_RECORD_BYTES):
                    raise ValueError('invalid journal path')
                loaded = json.loads(path.read_text(encoding='utf-8'))
                if not isinstance(loaded, dict):
                    raise ValueError('invalid journal')
                record = loaded
            except (OSError, ValueError, TypeError):
                detail_error = '\n\n详细记录暂不可读取。上方为已收到的执行结果。'
        dialog = QDialog(self)
        dialog.setWindowTitle('云文档修改明细')
        dialog.resize(720, 540)
        layout = QVBoxLayout(dialog)
        layout.addWidget(_label(self._cloud_result_text(result), 'notice', True))
        editor = QPlainTextEdit()
        editor.setReadOnly(True)
        editor.setPlainText(self._redact(self._format_cloud_details(record) + detail_error))
        layout.addWidget(editor, 1)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)
        dialog.exec()

    @staticmethod
    def _format_cloud_details(record):
        lines = [
            '任务编号：' + str(record.get('task_id', '')),
            '文档：' + str(record.get('document_name', '')),
            '工作表：' + str(record.get('sheet', '')),
            '链接：' + str(record.get('document_url', '')),
            FinanceAIPage._cloud_result_text(record),
            str(record.get('summary', '')),
        ]
        if record.get('rule_summary'):
            lines.append('处理规则：' + str(record['rule_summary']))
        current = record.get('current_check')
        if isinstance(current, dict):
            lines.append('本次核对：' + '；'.join(f'{label} {current.get(key, 0)} 项' for key, label in (
                ('checked', '检查'), ('matched', '一致'), ('changed', '已变化'),
                ('missing', '未找到'), ('duplicate', '编号重复'))))
        if record.get('created_at'):
            lines.append('记录时间：' + str(record['created_at']))
        status_names = {'applied': '已写入', 'verified': '已回读核对', 'unchanged': '未变化',
                        'conflict': '原值有变化', 'not_found': '未找到', 'duplicate': '编号重复',
                        'formula': '保留原公式', 'merged': '合并单元格跳过', 'unknown': '待核对',
                        'pending': '待核对', 'planned': '待执行', 'skipped': '已跳过',
                        'empty_key': '业务编号为空，已跳过', 'duplicate_source': '来源编号重复',
                        'duplicate_target': '云表编号重复', 'blank_skipped': '来源空白，保留原值',
                        'formula_text_unsupported': '公式样式文本暂不写入', 'not_submitted': '尚未提交'}
        entries = record.get('items', [])
        if not isinstance(entries, list) or not entries:
            changes = record.get('changes', [])
            entries = [item.get('request', item) for item in changes if isinstance(item, dict)] if isinstance(changes, list) else []
        if entries:
            lines.append('\n本次处理明细：')
            for index, change in enumerate(entries[:2000]):
                if not isinstance(change, dict):
                    continue
                key = change.get('key_values', [])
                key_text = ' / '.join(str(value) for value in key) if isinstance(key, list) else str(key)
                status = change.get('status', '')
                explanation = status_names.get(status, status)
                before = change.get('old_value', '')
                after = change.get('new_value', '')
                before = '（空白）' if before is None or before == '' else before
                after = '（空白）' if after is None or after == '' else after
                lines.append(f'{index + 1}. {key_text}：{before!s} → {after!s}' +
                             (f'（{explanation}）' if explanation else ''))
                if change.get('reason'):
                    lines.append('   原因：' + str(change['reason']))
            if len(entries) > 2000:
                lines.append(f'共 {len(entries)} 项，窗口显示前 2000 项。')
        if record.get('status') in ('running', 'uncertain', 'partial'):
            lines.append('\n如需重新核对，在聊天中输入“核对任务编号 ' + str(record.get('task_id', '')) + '”。')
        return '\n'.join(lines)

    def show_cloud_history(self):
        if self.is_busy or self._closed:
            return
        records = []
        try:
            root = self.cloud_journal_dir.resolve()
            paths = sorted(self.cloud_journal_dir.glob('*.json'), key=lambda item: item.stat().st_mtime,
                           reverse=True)[:200]
            for path in paths:
                if path.is_symlink() or not path.resolve().is_relative_to(root) or path.stat().st_size > MAX_TASK_RECORD_BYTES:
                    continue
                try:
                    record = json.loads(path.read_text(encoding='utf-8'))
                    if not isinstance(record, dict) or record.get('schema') != 1 or not isinstance(record.get('task_id'), str):
                        continue
                    normalize_share_url(record.get('document_url', ''))
                    result = {key: record[key] for key in ('task_id', 'document_url', 'document_name', 'sheet',
                              'status', 'summary', 'updated', 'unchanged', 'skipped', 'conflicts', 'unknown',
                              'current_check') if key in record}
                    result['details_path'] = str(path.resolve())
                    records.append(result)
                except (WpsError, ValueError, OSError, TypeError):
                    continue
        except OSError:
            self._status('任务记录暂不可读取，请检查数据目录。', error=True)
            return
        if not records:
            self._status('这台电脑还没有可查看的云文档任务记录。')
            return
        self._record_cloud_results(list(reversed(records)))
        self._status(f'已载入最近 {len(records)} 条云文档任务，选择后可查看修改明细。')

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
        self._cloud_tools = None
        self._cloud_links.clear()
        self._selected_cloud_links.clear()
        self._clear_cloud_questions()
        self._refresh_cloud_summary()
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
        self._connection_timer.stop()
        if self._connection_job is not None:
            self._connection_job.cancel.set()
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
