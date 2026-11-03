"""Exercise the actual Qt assistant with an isolated, non-network API double."""
from __future__ import annotations

import os
import threading
import time
from pathlib import Path
from types import SimpleNamespace

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

import pytest
from PySide6.QtCore import QByteArray, QUrl
from PySide6.QtGui import QTextDocument
from PySide6.QtWidgets import QApplication, QDialog, QFileDialog

from xiaoyu import ai_ui
from xiaoyu.ai_service import AIError, AIResult, GeneratedFile, RequestCancelled, UploadedFile
from xiaoyu.ai_settings import AISettings, UsageLedger, load_settings


@pytest.fixture(scope='module')
def app():
    return QApplication.instance() or QApplication([])


def pump(app, predicate, seconds=5):
    deadline = time.monotonic() + seconds
    while not predicate() and time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.005)
    app.processEvents()
    assert predicate(), 'Qt worker did not finish in time'


class OfflineClient:
    instances = []

    def __init__(self, settings):
        self.settings = settings
        self.uploads, self.sends, self.deleted = [], [], []
        self.cancel_send = False
        self.download_data = b'category,amount\nOffice,30\n'
        self.send_entered = threading.Event()
        self.release = None
        self.instances.append(self)

    def upload_file(self, path, cancel=None):
        self.uploads.append(Path(path))
        return UploadedFile('file-' + str(len(self.uploads)), Path(path).name, Path(path).stat().st_size)

    def send(self, message, files, history, container_id=None, web_search=False,
             cancel=None, on_text=None, on_status=None):
        self.sends.append((message, list(files), list(history), container_id, web_search))
        self.send_entered.set()
        if self.cancel_send:
            assert cancel.wait(3)
            raise RequestCancelled(request_may_be_billed=True)
        if self.release is not None:
            assert self.release.wait(3)
        if on_status:
            on_status('正在云端核对合成数据')
        if on_text:
            on_text('费用合计 ')
            on_text('30.00 元。')
        return AIResult(text='费用合计 **30.00** 元。',
                        files=[GeneratedFile('cntr-test', 'cfile-test', '汇总.csv')],
                        input_tokens=400, output_tokens=80, container_sessions=1,
                        container_id='cntr-test')

    def test_connection(self):
        return '连接与模型权限通过，余额请在 OpenAI 账单查看。'

    def download_file(self, file, cancel=None):
        return self.download_data

    def delete_file(self, file_id):
        self.deleted.append(file_id)


@pytest.fixture
def page_factory(app, tmp_path):
    pages = []
    OfflineClient.instances = []

    def create(**kwargs):
        kwargs.setdefault('settings', AISettings(api_key='fake-key-only-for-test'))
        kwargs.setdefault('ledger', UsageLedger(tmp_path / f'usage-{len(pages)}.json'))
        kwargs.setdefault('client_factory', OfflineClient)
        kwargs.setdefault('settings_path', tmp_path / f'settings-{len(pages)}.json')
        page = ai_ui.FinanceAIPage(**kwargs)
        pages.append(page)
        return page

    yield create
    for page in pages:
        page.shutdown()
        page.close()
        page.deleteLater()
    app.processEvents()


def test_selection_does_not_upload_and_missing_key_cannot_send(page_factory, tmp_path):
    source = tmp_path / '工资.csv'
    source.write_text('姓名,工资\n员工,100\n', encoding='utf-8')
    page = page_factory(settings=AISettings())
    page.add_paths([source])
    assert page.pending_paths == [source.resolve()]
    assert not OfflineClient.instances
    page.message_edit.setPlainText('汇总工资')
    page.send_button.click()
    assert not OfflineClient.instances
    assert '密钥' in page.status_label.text()
    assert source.read_text(encoding='utf-8').startswith('姓名')


def test_upload_reply_and_followup_reuse_same_client(page_factory, app, tmp_path):
    source = tmp_path / 'expenses.csv'
    source.write_bytes(b'amount\n10\n20\n')
    original = source.read_bytes()
    page = page_factory()
    page.add_paths([source, source])
    assert len(page.pending_paths) == 1
    page.message_edit.setPlainText('汇总费用')
    page.send_message()
    pump(app, lambda: not page.is_busy)
    client = OfflineClient.instances[0]
    assert client.uploads == [source]
    assert len(page.uploaded_files) == 1 and not page.pending_paths
    assert '30.00' in page.transcript.toPlainText()
    assert page.result_list.count() == 1
    assert page.ledger.summary()['requests'] == 1
    assert source.read_bytes() == original
    page.web_search_checkbox.setChecked(True)
    page.message_edit.setPlainText('再核对一下费用是否符合政策')
    page.send_message()
    pump(app, lambda: not page.is_busy)
    assert len(OfflineClient.instances) == 1 and client.uploads == [source]
    assert len(client.sends[1][2]) == 2
    assert client.sends[1][3:] == ('cntr-test', True)
    assert client.sends[1][1][0].file_id == 'file-1'
    assert page.result_list.count() == 1  # Same cloud file is not duplicated.


def test_cancellation_keeps_uploaded_files_and_records_uncertain_usage(page_factory, app, tmp_path):
    source = tmp_path / 'payroll.csv'
    source.write_bytes(b'amount\n10\n')
    page = page_factory()
    client = page._get_client()
    client.cancel_send = True
    page.add_paths([source])
    page.message_edit.setPlainText('核对工资')
    page.send_message()
    pump(app, client.send_entered.is_set)
    page.cancel_button.click()
    pump(app, lambda: not page.is_busy)
    assert page.uploaded_files[0].file_id == 'file-1'
    assert page.pending_paths == []
    assert page.ledger.summary()['uncertain_requests'] == 1
    assert page.send_button.isEnabled()
    client.cancel_send = False
    page.send_message()
    pump(app, lambda: not page.is_busy)
    assert client.uploads == [source]
    assert page.ledger.summary()['requests'] == 2


def test_budget_blocks_new_request_before_client_creation(page_factory):
    page = page_factory(settings=AISettings(api_key='fake', monthly_limit_usd=1))
    page.ledger.record(0, 2_000_000)
    page.message_edit.setPlainText('解释会计科目')
    page.send_message()
    assert not OfflineClient.instances
    assert not page.is_busy
    assert '预算' in page.status_label.text()


def test_invalid_client_error_is_shown_without_secret(page_factory, monkeypatch, tmp_path):
    def invalid(settings):
        raise AIError('连接设置错误：' + settings.api_key)

    page = page_factory(client_factory=invalid)
    page.message_edit.setPlainText('解释费用')
    page.send_message()
    assert '连接设置错误' in page.status_label.text()
    assert page.settings.api_key not in page.status_label.text()
    page.test_connection()
    assert not page.is_busy
    page.generated_files.append(GeneratedFile('cntr-test', 'cfile-test', 'result.csv'))
    page._refresh_files()
    page.result_list.setCurrentRow(0)
    monkeypatch.setattr(QFileDialog, 'getSaveFileName', lambda *args: (str(tmp_path / 'result.csv'), ''))
    page.download_selected()
    assert '连接设置错误' in page.status_label.text()
    assert not (tmp_path / 'result.csv').exists()


def test_invalid_files_and_pending_removal(page_factory, tmp_path):
    page = page_factory()
    old = tmp_path / 'old.xls'
    old.write_bytes(b'fake xls')
    page.add_paths([old])
    assert not page.pending_paths and '另存为 .xlsx' in page.status_label.text()
    unsupported = tmp_path / 'script.py'
    unsupported.write_text('print(1)')
    page.add_paths([unsupported, tmp_path])
    assert not page.pending_paths
    files = []
    for i in range(11):
        path = tmp_path / f'{i}.csv'
        path.write_bytes(b'a,b\n1,2\n')
        files.append(path)
    page.add_paths(files)
    assert len(page.pending_paths) == 10
    page.attachment_list.setCurrentRow(0)
    page.remove_button.click()
    assert len(page.pending_paths) == 9


def test_download_is_explicit_and_never_overwrites_uploaded_source(page_factory, app, tmp_path, monkeypatch):
    source = tmp_path / 'source.csv'
    source.write_bytes(b'original\n')
    page = page_factory()
    page.source_paths.add(source)
    page.generated_files.append(GeneratedFile('cntr-test', 'cfile-test', '汇总.csv'))
    page._refresh_files()
    page.result_list.setCurrentRow(0)
    monkeypatch.setattr(QFileDialog, 'getSaveFileName', lambda *args: (str(source), ''))
    page.download_selected()
    pump(app, lambda: not page.is_busy)
    assert source.read_bytes() == b'original\n'
    assert '不能覆盖' in page.status_label.text()
    destination = tmp_path / 'result.csv'
    monkeypatch.setattr(QFileDialog, 'getSaveFileName', lambda *args: (str(destination), ''))
    page.download_selected()
    pump(app, lambda: not page.is_busy)
    assert destination.read_bytes() == page.client.download_data
    assert source.read_bytes() == b'original\n'


def test_download_preserves_existing_target_on_write_failure(tmp_path, monkeypatch):
    target = tmp_path / 'result.xlsx'
    target.write_bytes(b'previous-result')

    def fail_replace(*args):
        raise PermissionError('target is locked')

    monkeypatch.setattr(ai_ui.os, 'replace', fail_replace)
    with pytest.raises(PermissionError):
        ai_ui.save_download(b'new-result', target, [])
    assert target.read_bytes() == b'previous-result'
    assert list(tmp_path.glob('.xiaoyu-download-*')) == []


def test_download_rejects_hardlink_to_source_and_supports_zip(tmp_path):
    source = tmp_path / 'source.csv'
    source.write_bytes(b'original')
    alias = tmp_path / 'alias.csv'
    os.link(source, alias)
    with pytest.raises(ValueError, match='原文件'):
        ai_ui.save_download(b'new', alias, [source])
    archive = tmp_path / 'documents.zip'
    ai_ui.save_download(b'zip-bytes', archive, [source])
    assert archive.read_bytes() == b'zip-bytes'
    with pytest.raises(ValueError):
        ai_ui.save_download(b'executable', tmp_path / 'generated.exe', [])


def test_chat_resources_cannot_load_local_files(page_factory, monkeypatch):
    page = page_factory()
    opened = []
    monkeypatch.setattr(ai_ui.QDesktopServices, 'openUrl', lambda url: opened.append(url.toString()))
    assert isinstance(page.transcript.loadResource(QTextDocument.ResourceType.ImageResource,
                                                   QUrl('file:///private.csv')), QByteArray)
    assert isinstance(ai_ui._SafeDocument().loadResource(QTextDocument.ResourceType.ImageResource,
                                                       QUrl('https://example.test/tracker.png')), QByteArray)
    page.messages.append(('assistant', '![image](file:///private.csv)\n[run](file:///bad.exe)'))
    page._render_chat()
    assert '<img' not in page.transcript.toHtml()
    page.transcript._open_link(QUrl('file:///bad.exe'))
    page.transcript._open_link(QUrl('https://example.test/reference'))
    assert opened == ['https://example.test/reference']


def test_session_only_key_never_saved_to_settings(page_factory, monkeypatch):
    page = page_factory()
    settings = AISettings(api_key='another-secret-key', monthly_limit_usd=25)

    class Dialog:
        def __init__(self, *args):
            self.remember_key = SimpleNamespace(isChecked=lambda: False)

        def exec(self):
            return QDialog.DialogCode.Accepted

        def value(self):
            return settings

    monkeypatch.setattr(ai_ui, 'AISettingsDialog', Dialog)
    page.open_settings()
    assert page.settings.api_key == 'another-secret-key'
    assert load_settings(page.settings_path).api_key == ''
    assert 'another-secret-key' not in page.settings_path.read_text(encoding='utf-8')
    assert page.settings.monthly_limit_usd == 25


def test_shutdown_does_not_wait_for_active_network_job(page_factory, app):
    page = page_factory()
    client = page._get_client()
    client.release = threading.Event()
    page.message_edit.setPlainText('解释科目')
    page.send_message()
    pump(app, client.send_entered.is_set)
    before = time.monotonic()
    page.shutdown()
    assert time.monotonic() - before < 0.2
    client.release.set()
    pump(app, lambda: not page._job.thread.is_alive())
    assert not page.generated_files
    assert page.ledger.summary()['requests'] == 1
    assert page.ledger.summary()['uncertain_requests'] == 1
    page.shutdown()
    assert page.ledger.summary()['requests'] == 1


def test_main_window_ai_is_lazy_and_drop_does_not_enter_accounting(app, tmp_path, monkeypatch):
    from xiaoyu import ui
    loads = []
    monkeypatch.setattr(ui, 'load_config', lambda: {})
    monkeypatch.setattr(ui, 'save_config', lambda config: None)
    monkeypatch.setattr(ai_ui, 'load_settings', lambda path: loads.append(path) or AISettings())
    monkeypatch.setattr(ai_ui, 'UsageLedger', lambda: UsageLedger(tmp_path / 'usage.json'))
    window = ui.MainWindow(material_store=ui.MaterialStore(tmp_path / 'materials.sqlite3'))
    try:
        assert window.pages.count() == 5 and window.ai_page is None and loads == []
        window._navigate(4)
        assert window.ai_page is window.pages.widget(4) and len(loads) == 1
        assert window.company_bar.isHidden() and window.steps_bar.isHidden() and window.notice.isHidden()
        source = tmp_path / 'source.csv'
        source.write_bytes(b'amount\n1\n')
        accepted = []
        event = SimpleNamespace(mimeData=lambda: SimpleNamespace(urls=lambda: [QUrl.fromLocalFile(str(source))]),
                                acceptProposedAction=lambda: accepted.append(True), ignore=lambda: None)
        monkeypatch.setattr(ui.QInputDialog, 'getItem', lambda *args: pytest.fail('Accounting import must not run'))
        window.dropEvent(event)
        assert window.ai_page.pending_paths == [source] and accepted == [True]
        assert not window.material_records
        window._navigate(0)
        assert not window.company_bar.isHidden()
        window._navigate(4)
        assert len(loads) == 1
    finally:
        window.close()
        window.deleteLater()
        app.processEvents()
