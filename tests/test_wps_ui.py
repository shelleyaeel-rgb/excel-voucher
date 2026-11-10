"""Exercise WPS configuration and chat integration with local doubles only."""
from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from types import SimpleNamespace

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

import pytest
from PySide6.QtCore import QCoreApplication, QEvent, QObject
from PySide6.QtWidgets import QApplication, QDialog, QLineEdit
from PySide6.QtGui import QCloseEvent
from shiboken6 import isValid

from xiaoyu import ai_ui, wps_ui
from xiaoyu.ai_service import AIError, AIResult, RequestCancelled, UploadedFile
from xiaoyu.ai_settings import AISettings, UsageLedger
from xiaoyu.wps_service import WpsBinding, WpsError


URL = 'https://www.kdocs.cn/l/doc-test'
ENDPOINT = 'https://www.kdocs.cn/api/v3/ide/file/doc-test/script/script-test/sync_task'
TOKEN = 'fake-token-never-real'


@pytest.fixture(scope='module')
def app():
    return QApplication.instance() or QApplication([])


def pump(app, predicate):
    deadline = time.monotonic() + 4
    while not predicate() and time.monotonic() < deadline:
        app.processEvents()
        time.sleep(.005)
    app.processEvents()
    assert predicate()


def dispose(app, widget):
    widget.close()
    widget.deleteLater()
    # These tests use processEvents rather than QApplication.exec; explicitly
    # drain DeferredDelete so parentless fixtures cannot outlive QApplication.
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    app.processEvents()


def binding():
    return WpsBinding('doc-one', '收款表', URL, ENDPOINT, TOKEN, '收款表.xlsx')


class ProbeClient:
    calls = []
    release = None

    def __init__(self, config):
        self.config = config

    def call(self, action):
        self.calls.append(action)
        if self.release:
            assert self.release.wait(3)
        return {'workbook_name': '收款表.xlsx', 'sheets': [{'name': '汇总'}]}


def fill(dialog):
    dialog.label_edit.setText('收款表')
    dialog.share_edit.setText(URL)
    dialog.endpoint_edit.setText(ENDPOINT)
    dialog.token_edit.setText(TOKEN)
    dialog.confirm_checkbox.setChecked(True)


def test_binding_requires_user_target_check_and_readonly_probe_before_save(app, tmp_path, monkeypatch):
    writes = []
    monkeypatch.setattr(wps_ui, 'save_bindings', lambda values, path: writes.append((values, path)))
    ProbeClient.calls, ProbeClient.release = [], None
    dialog = wps_ui.WpsDocumentsDialog([], tmp_path / 'wps.json', client_factory=ProbeClient)
    try:
        fill(dialog)
        assert dialog.token_edit.echoMode() == QLineEdit.EchoMode.Password
        dialog.save_binding()
        assert writes == [] and '只读测试' in dialog.status_label.text()
        dialog.confirm_checkbox.setChecked(False)
        dialog.test_binding()
        assert ProbeClient.calls == []
        dialog.confirm_checkbox.setChecked(True)
        dialog.test_binding()
        pump(app, lambda: not dialog.is_busy)
        assert ProbeClient.calls == ['describe']
        assert '收款表.xlsx' in dialog.status_label.text()
        dialog.save_binding()
        assert len(writes) == 1
        saved = writes[0][0][0]
        assert saved.workbook_name == '收款表.xlsx' and saved.token == TOKEN
        assert dialog.changed and TOKEN not in dialog.status_label.text()
        dialog.endpoint_edit.setText(ENDPOINT.replace('script-test', 'new-script'))
        dialog.save_binding()
        assert len(writes) == 1 and '只读测试' in dialog.status_label.text()
    finally:
        dispose(app, dialog)


def test_readonly_probe_locks_inputs_and_failed_save_retains_draft(app, tmp_path, monkeypatch):
    ProbeClient.calls, ProbeClient.release = [], threading.Event()
    dialog = wps_ui.WpsDocumentsDialog([], tmp_path / 'wps.json', client_factory=ProbeClient)
    try:
        fill(dialog)
        dialog.test_binding()
        assert dialog.is_busy and not dialog.token_edit.isEnabled()
        dialog.reject()
        assert dialog.is_busy and '等待' in dialog.status_label.text()
        ProbeClient.release.set()
        pump(app, lambda: not dialog.is_busy)
        monkeypatch.setattr(wps_ui, 'save_bindings', lambda *args: (_ for _ in ()).throw(OSError(TOKEN)))
        dialog.save_binding()
        assert not dialog.changed and not dialog.bindings
        assert dialog.token_edit.text() == TOKEN
        assert TOKEN not in dialog.status_label.text() and '未保存' in dialog.status_label.text()
    finally:
        ProbeClient.release.set()
        ProbeClient.release = None
        dispose(app, dialog)


def test_existing_binding_can_rename_and_delete_without_modifying_cloud(app, tmp_path, monkeypatch):
    writes = []
    monkeypatch.setattr(wps_ui, 'save_bindings', lambda values, path: writes.append(list(values)))
    dialog = wps_ui.WpsDocumentsDialog([binding()], tmp_path / 'wps.json', client_factory=ProbeClient)
    try:
        dialog.label_edit.setText('每月收款')
        dialog.save_binding()
        assert writes[-1][0].label == '每月收款'
        dialog.delete_binding()
        assert writes[-1] == [] and dialog.bindings == []
        assert '云文档内容没有改变' in dialog.status_label.text()
    finally:
        dispose(app, dialog)


def test_probe_remains_busy_until_worker_has_actually_exited(app, tmp_path, monkeypatch):
    ready, release = threading.Event(), threading.Event()
    original = wps_ui._Probe._run

    def paused_after_result(self):
        original(self)
        ready.set()
        assert release.wait(3)

    monkeypatch.setattr(wps_ui._Probe, '_run', paused_after_result)
    ProbeClient.release = None
    dialog = wps_ui.WpsDocumentsDialog([], tmp_path / 'wps.json', client_factory=ProbeClient)
    try:
        fill(dialog)
        dialog.test_binding()
        worker = dialog._probe
        assert not isinstance(worker, QObject)
        pump(app, ready.is_set)
        assert worker.result['workbook_name'] == '收款表.xlsx'
        dialog._poll_probe()
        assert worker.thread.is_alive() and dialog.is_busy
        assert not dialog.save_button.isEnabled() and not dialog.test_button.isEnabled()
        dialog.reject()
        assert dialog.is_busy
        release.set()
        pump(app, lambda: not dialog.is_busy)
        assert not worker.thread.is_alive()
        assert dialog.save_button.isEnabled() and '连接通过' in dialog.status_label.text()
        assert not dialog._probe_timer.isActive()
    finally:
        release.set()
        if dialog.is_busy:
            pump(app, lambda: not dialog.is_busy)
        dispose(app, dialog)


def test_parent_teardown_during_probe_cannot_call_destroyed_qt_objects(app, tmp_path):
    ProbeClient.release = threading.Event()
    dialog = wps_ui.WpsDocumentsDialog([], tmp_path / 'wps.json', client_factory=ProbeClient)
    fill(dialog)
    dialog.test_binding()
    worker = dialog._probe
    try:
        assert not isinstance(worker, QObject)
        # Simulate parent/application teardown, which bypasses a user's Close
        # action. The worker contains no Qt object or callback to this dialog.
        dialog.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        app.processEvents()
        assert not isValid(dialog)
        ProbeClient.release.set()
        worker.thread.join(timeout=3)
        assert not worker.thread.is_alive()
        assert worker.result['workbook_name'] == '收款表.xlsx'
        assert worker.binding is None
    finally:
        ProbeClient.release.set()
        worker.thread.join(timeout=3)
        ProbeClient.release = None
        if isValid(dialog):
            dispose(app, dialog)


class CloudTools:
    instances = []

    def __init__(self, bindings, *, allow_write=False, journal_dir=None):
        self.bindings, self.allow_write, self.journal_dir = bindings, allow_write, journal_dir
        self.turns, self.results = [], []
        self.instances.append(self)

    def begin_turn(self, message, history=None, force_preview=False):
        self.turns.append((message, history, force_preview))
        self.results = []


class AIClient:
    instances = []

    def __init__(self, settings):
        self.settings = settings
        self.calls = []
        self.error = None
        self.result = AIResult('已核对。')
        self.questions = []
        self.uploads = []
        self.instances.append(self)

    def upload_file(self, path, cancel=None):
        self.uploads.append(Path(path))
        return UploadedFile('file-test', Path(path).name, Path(path).stat().st_size)

    def delete_file(self, file_id):
        pass

    def send(self, message, files, history, **kwargs):
        self.calls.append((message, kwargs))
        self.sent_files = list(files)
        if kwargs.get('cloud_tools') is not None:
            kwargs['cloud_tools'].questions = list(self.questions)
        if self.error:
            raise self.error
        return self.result


@pytest.fixture
def page_factory(app, tmp_path, monkeypatch):
    pages = []
    AIClient.instances, CloudTools.instances = [], []
    monkeypatch.setattr(ai_ui, 'load_bindings', lambda path: [binding()])

    def create(**kwargs):
        kwargs.setdefault('wps_account_service', False)
        page = ai_ui.FinanceAIPage(settings=AISettings(api_key='fake-openai-key'),
                                  settings_path=tmp_path / 'settings.json', rules_path=tmp_path / 'rules.json',
                                  ledger=UsageLedger(tmp_path / 'usage.json'), client_factory=AIClient,
                                  cloud_tools_factory=CloudTools, cloud_journal_dir=tmp_path / 'tasks', **kwargs)
        pages.append(page)
        return page

    yield create
    for page in pages:
        if isValid(page):
            page.shutdown()
            dispose(app, page)


def send(page, app, message):
    page.message_edit.setPlainText(message)
    page.send_message()
    pump(app, lambda: not page.is_busy)


def test_link_binds_only_explicit_documents_and_followup_reuses_tools(page_factory, app):
    page = page_factory()
    send(page, app, '解释预付账款')
    assert 'cloud_tools' not in page.client.calls[-1][1]
    send(page, app, '按业务单号更新到账金额 ' + URL)
    tools = page.client.calls[-1][1]['cloud_tools']
    assert tools.bindings[0].share_url == URL and tools.allow_write is False
    assert tools.turns[-1][2] is False
    page.cloud_mode.setCurrentIndex(1)
    send(page, app, '来源为空时不覆盖')
    assert page.client.calls[-1][1]['cloud_tools'] is tools
    assert tools.turns[-1][2] is True
    page.new_chat()
    assert page._cloud_tools is None and page._cloud_links == []


def test_unknown_link_and_known_secret_block_before_network(page_factory, app):
    page = page_factory()
    send(page, app, '更新 https://www.kdocs.cn/l/not-bound')
    assert not AIClient.instances and '尚未绑定' in page.status_label.text()
    send(page, app, '令牌是 ' + TOKEN)
    assert not AIClient.instances and '不要把脚本令牌' in page.status_label.text()
    assert TOKEN not in page.status_label.text()


def test_cloud_results_survive_summary_failure_and_unknown_is_not_success(page_factory, app):
    page = page_factory()
    record = {'task_id': 'task-one', 'document_url': URL, 'document_name': '收款表',
              'sheet': '汇总', 'status': 'uncertain', 'updated': 3, 'unchanged': 0,
              'skipped': 1, 'conflicts': 0, 'unknown': 2, 'summary': '部分结果需重新读取'}
    client = page._get_client()
    error = AIError('总结连接中断', request_may_be_billed=True)
    error.cloud_results = [record]
    client.error = error
    send(page, app, '按业务单号更新 ' + URL)
    assert page.cloud_result_list.count() == 1
    assert '待核对 2' in page.cloud_result_list.item(0).text()
    assert '结果待核对' in page.status_label.text()
    assert '已核对完成' not in page.status_label.text()
    client.error = None
    client.result = AIResult('重新核对完成。', cloud_results=[dict(record, status='completed', updated=5, unknown=0)])
    send(page, app, '核对任务 task-one')
    assert page.cloud_result_list.count() == 1
    assert page.cloud_results[0]['updated'] == 5


def test_result_open_uses_validated_cloud_link_and_history_is_local(page_factory, app, monkeypatch):
    page = page_factory()
    opened = []
    monkeypatch.setattr(ai_ui.QDesktopServices, 'openUrl', lambda url: opened.append(url.toString()))
    page.cloud_journal_dir.mkdir()
    record = {'schema': 1, 'task_id': 'task-one', 'document_url': URL, 'document_name': '收款表',
              'sheet': '汇总', 'status': 'prepared', 'updated': 0, 'summary': '仅预览',
              'changes': [{'key_values': ['0001'], 'old_value': 1, 'new_value': 2}]}
    (page.cloud_journal_dir / 'task-one.json').write_text(json.dumps(record), encoding='utf-8')
    (page.cloud_journal_dir / 'bad.json').write_text('{bad', encoding='utf-8')
    page.show_cloud_history()
    assert page.cloud_result_list.count() == 1
    assert '修改预览' in page.cloud_result_list.item(0).text()
    page.open_cloud_result()
    assert opened == [URL]
    shown = []
    monkeypatch.setattr(QDialog, 'exec', lambda dialog: shown.append(dialog.findChild(ai_ui.QPlainTextEdit).toPlainText()))
    page.show_cloud_details()
    assert '0001：1 → 2' in shown[0]
    assert not AIClient.instances


def test_details_do_not_read_untrusted_outside_path(page_factory, app, tmp_path, monkeypatch):
    page = page_factory()
    outside = tmp_path / 'outside.json'
    outside.write_text('{"private":"never read this"}', encoding='utf-8')
    page._record_cloud_results([{'task_id': 'bad-path', 'document_url': URL, 'status': 'uncertain',
                                'details_path': str(outside)}])
    shown = []
    monkeypatch.setattr(QDialog, 'exec', lambda dialog: shown.append(dialog.findChild(ai_ui.QPlainTextEdit).toPlainText()))
    page.show_cloud_details()
    assert 'never read this' not in shown[0] and '暂不可读取' in shown[0]


def test_explicit_task_check_resolves_bound_document_without_old_write_scope(page_factory, app):
    page = page_factory()
    task_id = 'a1' * 16
    page.cloud_journal_dir.mkdir()
    record = {'schema': 1, 'task_id': task_id, 'document_url': URL, 'status': 'uncertain'}
    (page.cloud_journal_dir / (task_id + '.json')).write_text(json.dumps(record), encoding='utf-8')
    send(page, app, '按业务单号更新 ' + URL)
    original = page._cloud_tools
    send(page, app, '核对任务编号 ' + task_id)
    current = page._cloud_tools
    assert current is not original
    assert current.allow_write is False and current.turns[-1][2] is True
    assert current.bindings[0].share_url == URL
    page.new_chat()
    send(page, app, '核对任务 ' + task_id)
    assert page._cloud_tools is not None
    assert page._cloud_tools.turns[-1][2] is True


@pytest.mark.parametrize('record', [None, '{broken', {'schema': 1, 'task_id': 'wrong', 'document_url': URL},
                                  {'schema': 2, 'task_id': 'b2' * 16, 'document_url': URL},
                                  {'schema': 1, 'task_id': 'b2' * 16, 'document_url': 'file:///private'}])
def test_invalid_history_check_stops_before_ai_or_upload(page_factory, app, tmp_path, record):
    page = page_factory()
    task_id = 'b2' * 16
    page.cloud_journal_dir.mkdir()
    if record is not None:
        content = record if isinstance(record, str) else json.dumps(record)
        (page.cloud_journal_dir / (task_id + '.json')).write_text(content, encoding='utf-8')
    source = tmp_path / 'source.csv'
    source.write_text('number,amount\n0001,10\n', encoding='utf-8')
    page.add_paths([source])
    send(page, app, '核对任务 ' + task_id)
    assert not AIClient.instances and page._cloud_tools is None
    assert page.pending_paths == [source] and '有效的本机任务记录' in page.status_label.text()


def test_task_number_without_check_intent_does_not_read_history(page_factory, app):
    page = page_factory()
    task_id = 'c3' * 16
    page.cloud_journal_dir.mkdir()
    (page.cloud_journal_dir / (task_id + '.json')).write_text('{broken', encoding='utf-8')
    send(page, app, '这个业务编号是 ' + task_id)
    assert page.client.calls and 'cloud_tools' not in page.client.calls[-1][1]


def test_history_check_unknown_binding_and_oversized_record_stops(page_factory, app):
    page = page_factory()
    task_id = 'd4' * 16
    page.cloud_journal_dir.mkdir()
    path = page.cloud_journal_dir / (task_id + '.json')
    path.write_text(json.dumps({'schema': 1, 'task_id': task_id,
                               'document_url': 'https://www.kdocs.cn/l/unknown'}), encoding='utf-8')
    send(page, app, '核对任务 ' + task_id)
    assert not AIClient.instances and '尚未绑定' in page.status_label.text()
    with path.open('wb') as stream:
        stream.truncate(32 * 1024 * 1024 + 1)
    send(page, app, '核对任务 ' + task_id)
    assert not AIClient.instances and '有效的本机任务记录' in page.status_label.text()


def test_history_check_rejects_symlink(page_factory, app, tmp_path):
    page = page_factory()
    task_id = 'e5' * 16
    page.cloud_journal_dir.mkdir()
    outside = tmp_path / 'elsewhere.json'
    outside.write_text(json.dumps({'schema': 1, 'task_id': task_id, 'document_url': URL}), encoding='utf-8')
    try:
        (page.cloud_journal_dir / (task_id + '.json')).symlink_to(outside)
    except OSError:
        pytest.skip('symlinks are unavailable for this Windows account')
    send(page, app, '核对任务 ' + task_id)
    assert not AIClient.instances and '有效的本机任务记录' in page.status_label.text()


def test_clarification_buttons_send_selected_answer_and_clear_old_choices(page_factory, app):
    page = page_factory()
    client = page._get_client()
    client.questions = [{'question': '两张表按哪个字段匹配？', 'options': ['业务单号', '客户名称']}]
    send(page, app, '更新这份云表 ' + URL)
    assert not page.question_scroll.isHidden()
    assert [button.text() for button in page._clarification_buttons] == ['业务单号', '客户名称']
    tools = page._cloud_tools
    client.questions = []
    page._clarification_buttons[0].click()
    pump(app, lambda: not page.is_busy)
    assert client.calls[-1][0] == '针对“两张表按哪个字段匹配？”：业务单号'
    assert client.calls[-1][1]['cloud_tools'] is tools
    assert page.question_scroll.isHidden() and not page._clarification_buttons


def test_clarification_is_disabled_while_busy_and_new_chat_clears_it(page_factory, app):
    page = page_factory()
    tools = CloudTools([binding()])
    tools.questions = [{'question': '空白金额怎样处理？', 'options': ['保留原值', '写为空白']}]
    page._cloud_tools = tools
    page._job = SimpleNamespace(cancel=threading.Event())
    try:
        page._show_cloud_questions()
        assert all(not button.isEnabled() for button in page._clarification_buttons)
        page._answer_cloud_question('空白金额怎样处理？', '保留原值')
        assert not AIClient.instances
    finally:
        page._job = None
    page._sync_controls()
    assert all(button.isEnabled() for button in page._clarification_buttons)
    page.new_chat()
    assert page.question_scroll.isHidden() and not page._clarification_buttons


def test_free_text_clarification_and_questions_survive_failed_summary(page_factory, app):
    page = page_factory()
    client = page._get_client()
    client.questions = [{'question': '请说明需要更新哪一列。', 'options': []}]
    client.error = AIError('总结暂时中断', request_may_be_billed=True)
    send(page, app, '更新云文档 ' + URL)
    assert not page.question_scroll.isHidden() and not page._clarification_buttons
    client.error = None
    client.questions = []
    send(page, app, '更新到账金额这一列')
    assert client.calls[-1][0] == '更新到账金额这一列'
    assert page.question_scroll.isHidden()


@pytest.mark.parametrize('check,expected', [
    ({'checked': 0, 'matched': 0, 'changed': 0, 'missing': 0, 'duplicate': 0, 'failed': True},
     '历史结果已保留 · 本次核对未完成'),
    ({'checked': 6, 'matched': 2, 'changed': 1, 'missing': 2, 'duplicate': 1, 'failed': False},
     '历史更新已完成 · 当前有 4 项不同/无法定位'),
    ({'checked': 8, 'matched': 8, 'changed': 0, 'missing': 0, 'duplicate': 0, 'failed': False},
     '本次核对一致 · 共 8 项'),
    ({'checked': 0, 'matched': 0, 'changed': 0, 'missing': 0, 'duplicate': 0, 'failed': False},
     '本次没有可核对项目'),
])
def test_historical_success_does_not_hide_current_recheck_outcome(page_factory, check, expected):
    page = page_factory()
    record = {'task_id': 'f6'*16, 'document_url': URL, 'document_name': '收款表',
              'status': 'completed', 'updated': 8, 'unknown': 0, 'current_check': check}
    page._record_cloud_results([record])
    shown = page.cloud_result_list.item(0).text()
    assert expected in shown and '历史已核对更新 8 条' in shown
    assert '已核对完成' not in shown


def test_history_reload_preserves_current_check_state(page_factory):
    page = page_factory()
    task_id = 'a8'*16
    page.cloud_journal_dir.mkdir()
    record = {'schema': 1, 'task_id': task_id, 'document_url': URL,
              'status': 'completed', 'updated': 4,
              'current_check': {'checked': 4, 'matched': 3, 'changed': 1,
                                'missing': 0, 'duplicate': 0, 'failed': False}}
    (page.cloud_journal_dir / (task_id+'.json')).write_text(json.dumps(record), encoding='utf-8')
    page.show_cloud_history()
    assert '当前有 1 项不同/无法定位' in page.cloud_result_list.item(0).text()
    assert page.cloud_results[0]['current_check']['changed'] == 1


NEW_URL = 'https://www.kdocs.cn/l/first-account-document'
ACCOUNT_ID = '1731a7bb-22ac-4819-8833-20aa36c1c456'


class AccountService:
    """An explicit browser-login boundary; no credentials or real WPS calls."""
    def __init__(self, connected=False):
        self.connected = connected
        self.documents = []
        self.connect_calls = 0
        self.resolve_calls = []
        self.release = threading.Event()
        self.entered = threading.Event()
        self.auth_url = 'https://mcp-center.wps.cn/kdocs-auth/auth-guide?auth_code=' + ACCOUNT_ID

    def status(self):
        return {'connected': self.connected, 'account_id': ACCOUNT_ID if self.connected else '',
                'display_name': 'WPS 个人账号'}

    def list_documents(self):
        return list(self.documents)

    def disconnect(self):
        self.connected = False

    def connect(self, *, cancel, on_status, open_url):
        self.connect_calls += 1
        self.entered.set()
        open_url(self.auth_url)
        on_status('请完成 WPS 登录')
        while not self.release.wait(.005):
            if cancel.is_set():
                raise RequestCancelled()
        if cancel.is_set():
            raise RequestCancelled()
        self.connected = True

    def resolve_documents(self, links, *, cancel, on_status):
        self.resolve_calls.append(list(links))
        self.documents = [WpsBinding('personal-document', '个人收款表', link,
                                    workbook_name='收款表.xlsx', provider='skillhub',
                                    account_id=ACCOUNT_ID, file_id='123456') for link in links]
        return list(self.documents)


def test_first_login_resumes_same_request_attachment_and_preview(page_factory, app, tmp_path, monkeypatch):
    service = AccountService()
    opened = []
    monkeypatch.setattr(ai_ui.QDesktopServices, 'openUrl', lambda url: opened.append(url.toString()) or True)
    page = page_factory(wps_account_service=service)
    source = tmp_path / '来源.csv'
    source.write_text('单号,金额\n0001,0\n', encoding='utf-8')
    page.add_paths([source])
    page.cloud_mode.setCurrentIndex(1)
    message = '按单号匹配，更新金额 ' + NEW_URL
    page.message_edit.setPlainText(message)
    page.send_message()
    try:
        pump(app, lambda: bool(opened))
        assert page.is_busy and not page.message_edit.isEnabled()
        assert not page.input_attach_button.isEnabled() and not page.add_cloud_button.isEnabled()
        assert page.pending_paths == [source] and not AIClient.instances
        assert '发送时需要登录' in page.current_cloud_list.item(0).text()
        service.release.set()
        pump(app, lambda: not page.is_busy)
        assert service.connect_calls == 1 and service.resolve_calls == [[NEW_URL]]
        assert page.client.uploads == [source]
        assert len(page.client.calls) == 1 and page.client.calls[0][0] == message
        assert page.client.sent_files[0].file_id == 'file-test'
        assert page._cloud_tools.turns[0][2] is True
        assert page._selected_cloud_links == [NEW_URL] and not page.cloud_chip.isHidden()
        assert '已连接' in page.account_status_label.text()
        assert '处理前检查编辑权限' in page.current_cloud_list.item(0).text()
    finally:
        service.release.set()


def test_cancel_login_retains_request_and_files_then_retry_resumes(page_factory, app, tmp_path, monkeypatch):
    service = AccountService()
    monkeypatch.setattr(ai_ui.QDesktopServices, 'openUrl', lambda url: True)
    page = page_factory(wps_account_service=service)
    source = tmp_path / 'source.csv'
    source.write_text('key,value\n001,9\n', encoding='utf-8')
    page.add_paths([source])
    message = '更新到账金额 ' + NEW_URL
    page.message_edit.setPlainText(message)
    page.send_message()
    pump(app, service.entered.is_set)
    page.cancel_request()
    pump(app, lambda: not page.is_busy)
    assert page.pending_paths == [source] and page.message_edit.toPlainText() == message
    assert not AIClient.instances and not service.resolve_calls
    assert '要求和附件已保留' in page.status_label.text()
    service.release.set()
    page.send_message()
    pump(app, lambda: not page.is_busy)
    assert service.connect_calls == 2 and len(page.client.calls) == 1
    assert page.client.uploads == [source]


def test_recent_selection_never_executes_and_new_chat_keeps_records(page_factory):
    service = AccountService(connected=True)
    service.resolve_documents([NEW_URL], cancel=threading.Event(), on_status=lambda value: None)
    page = page_factory(wps_account_service=service)
    page.recent_cloud_list.setCurrentRow(0)
    page.use_recent_cloud()
    assert page._selected_cloud_links == [NEW_URL] and not AIClient.instances
    assert service.connect_calls == 0 and service.resolve_calls == [[NEW_URL]]
    page._record_cloud_results([{'task_id': 'task-one', 'document_url': NEW_URL,
                                'document_name': '收款表', 'status': 'prepared'}])
    recent_count = page.recent_cloud_list.count()
    page.new_chat()
    assert not page._selected_cloud_links and page.current_cloud_list.count() == 0
    assert page.cloud_chip.isHidden() and page.recent_cloud_list.count() == recent_count
    assert page.cloud_result_list.count() == 1 and page.cloud_results[0]['task_id'] == 'task-one'
    assert not AIClient.instances


def test_new_link_reuses_connected_account_and_shows_real_sheet_permission(page_factory, app):
    service = AccountService(connected=True)
    page = page_factory(wps_account_service=service)
    send(page, app, '先预览 ' + NEW_URL)
    assert service.connect_calls == 0 and service.resolve_calls == [[NEW_URL]]
    page._cloud_tools.document_metadata = {NEW_URL: {'can_edit': False, 'sheets': ['汇总']}}
    page._cloud_tools.snapshots = {'snapshot': {'document_url': NEW_URL, 'sheet': '汇总'}}
    page._refresh_cloud_summary()
    text = page.current_cloud_list.item(0).text()
    assert '当前账号仅可读取' in text and '工作表：汇总' in text
    assert '已连接' in page.account_status_label.text()


def test_historical_recheck_reconnects_before_ai_and_keeps_preview(page_factory, app, monkeypatch):
    service = AccountService()
    service.release.set()
    monkeypatch.setattr(ai_ui.QDesktopServices, 'openUrl', lambda url: True)
    page = page_factory(wps_account_service=service)
    task_id = 'b9' * 16
    page.cloud_journal_dir.mkdir()
    record = {'schema': 1, 'task_id': task_id, 'document_url': NEW_URL, 'status': 'uncertain'}
    (page.cloud_journal_dir / (task_id + '.json')).write_text(json.dumps(record), encoding='utf-8')
    send(page, app, '核对任务 ' + task_id)
    assert service.connect_calls == 1 and service.resolve_calls == [[NEW_URL]]
    assert len(page.client.calls) == 1 and page._cloud_tools.turns[0][2] is True
    assert page._cloud_tools.allow_write is False


def test_login_browser_failure_preserves_helpful_error_and_draft(page_factory, app, monkeypatch):
    service = AccountService()
    monkeypatch.setattr(ai_ui.QDesktopServices, 'openUrl', lambda url: False)
    page = page_factory(wps_account_service=service)
    send(page, app, '更新 ' + NEW_URL)
    assert '无法打开 WPS 登录页' in page.status_label.text()
    assert page.message_edit.toPlainText() == '更新 ' + NEW_URL
    assert not AIClient.instances and not service.resolve_calls


def test_connection_worker_survives_parent_teardown_without_qt_calls(page_factory, app, monkeypatch):
    service = AccountService()
    monkeypatch.setattr(ai_ui.QDesktopServices, 'openUrl', lambda url: True)
    page = page_factory(wps_account_service=service)
    page.message_edit.setPlainText('更新 ' + NEW_URL)
    page.send_message()
    pump(app, service.entered.is_set)
    worker = page._connection_job
    assert not isinstance(worker, QObject)
    page.shutdown()
    worker.thread.join(timeout=3)
    assert not worker.thread.is_alive() and worker.cancel.is_set()
    assert not service.resolve_calls and not AIClient.instances


def test_legacy_dialog_preserves_account_documents(app, tmp_path, monkeypatch):
    service = AccountService(connected=True)
    personal = service.resolve_documents([NEW_URL], cancel=threading.Event(), on_status=lambda value: None)[0]
    saved = []
    monkeypatch.setattr(wps_ui, 'save_bindings', lambda values, path: saved.append(list(values)))
    dialog = wps_ui.WpsDocumentsDialog([personal, binding()], tmp_path / 'wps.json', client_factory=ProbeClient)
    try:
        assert dialog.list.count() == 1 and dialog.share_edit.text() == URL
        dialog.delete_binding()
        assert saved == [[personal]] and dialog.bindings == [personal]
    finally:
        dispose(app, dialog)


def test_recent_document_from_previous_login_is_rebound_before_send(page_factory, app):
    service = AccountService(connected=True)
    service.documents = [WpsBinding('old-personal', '旧连接的收款表', NEW_URL,
                                   workbook_name='收款表.xlsx', provider='skillhub',
                                   account_id='previous-login', file_id='123456')]
    page = page_factory(wps_account_service=service)
    page.recent_cloud_list.setCurrentRow(0)
    page.use_recent_cloud()
    assert '重新核对文档权限' in page.current_cloud_list.item(0).text()
    assert not AIClient.instances and not service.resolve_calls
    send(page, app, '更新到账金额')
    assert service.connect_calls == 0 and service.resolve_calls == [[NEW_URL]]
    assert page._cloud_tools.bindings[0].account_id == ACCOUNT_ID
    assert page.client.calls[0][0] == '更新到账金额\n本次云文档：\n' + NEW_URL
    previous_tools = page._cloud_tools
    page.connect_wps_account()
    assert page._cloud_tools is None and not service.connected
    assert previous_tools is not page._cloud_tools


def test_remove_current_document_removes_pasted_link_and_stale_write_scope(page_factory, app):
    page = page_factory()
    send(page, app, '更新到账金额 ' + URL)
    page.message_edit.setPlainText('这次再核对 ' + URL + '。')
    page.remove_current_cloud()
    assert not page._selected_cloud_links and page._cloud_tools is None
    assert URL not in page.message_edit.toPlainText() and page.cloud_chip.isHidden()
    page.send_message()
    pump(app, lambda: not page.is_busy)
    assert 'cloud_tools' not in page.client.calls[-1][1]


def test_login_success_then_access_denied_retains_request_and_attachment(page_factory, app, tmp_path, monkeypatch):
    service = AccountService()
    service.release.set()
    original_resolve = service.resolve_documents
    monkeypatch.setattr(ai_ui.QDesktopServices, 'openUrl', lambda url: True)

    def denied(*args, **kwargs):
        raise WpsError('当前账号没有访问这份文档的权限。', code='ACCESS_DENIED')

    service.resolve_documents = denied
    page = page_factory(wps_account_service=service)
    source = tmp_path / 'source.csv'
    source.write_text('key,value\n001,9\n', encoding='utf-8')
    page.add_paths([source])
    message = '按单号更新金额 ' + NEW_URL
    send(page, app, message)
    assert service.connected and service.connect_calls == 1
    assert page.pending_paths == [source] and page.message_edit.toPlainText() == message
    assert not AIClient.instances and '没有访问' in page.status_label.text()
    assert '已连接' in page.account_status_label.text()
    service.resolve_documents = original_resolve
    page.send_message()
    pump(app, lambda: not page.is_busy)
    assert service.connect_calls == 1 and len(page.client.calls) == 1
    assert page.client.uploads == [source]


def test_expired_account_is_shown_after_request_and_keeps_retry_enabled(page_factory, app, tmp_path, monkeypatch):
    from xiaoyu.wps_skillhub import WpsAccount, WpsAccountService, save_account, mark_account_expired

    account_path = tmp_path / 'account-scope' / 'wps_account.json'
    account_path.parent.mkdir()
    save_account(WpsAccount(ACCOUNT_ID, 'fake-account-token'), account_path)
    service = WpsAccountService(path=account_path, documents_path=tmp_path / 'wps.json')
    page = page_factory(wps_account_service=service)
    page.wps_bindings = [WpsBinding('personal', '收款表', NEW_URL, workbook_name='收款表.xlsx',
                                   provider='skillhub', account_id=ACCOUNT_ID, file_id='123456')]

    def expired(*args, **kwargs):
        mark_account_expired(ACCOUNT_ID, account_path)
        raise AIError('WPS 授权已失效，请重新连接。')

    monkeypatch.setattr(page._get_client(), 'send', expired)
    send(page, app, '更新金额 ' + NEW_URL)
    assert '登录已过期' in page.account_status_label.text()
    assert page.account_connect_button.text() == '连接 WPS' and page.account_connect_button.isEnabled()
    assert page.message_edit.toPlainText() == '更新金额 ' + NEW_URL
    assert page.send_button.isEnabled() and '需要重新连接' in page.current_cloud_list.item(0).text()


def test_default_cloud_tools_uses_same_account_store_as_ui(page_factory, tmp_path, monkeypatch):
    from xiaoyu import wps_adapter

    service = AccountService(connected=True)
    service.path = tmp_path / 'isolated-account.json'
    service.documents_path = tmp_path / 'isolated-documents.json'
    service.resolve_documents([NEW_URL], cancel=threading.Event(), on_status=lambda value: None)
    page = page_factory(wps_account_service=service)
    page.cloud_tools_factory = None
    received = []
    monkeypatch.setattr(wps_adapter, 'create_wps_client',
                        lambda binding, **kwargs: received.append((binding, kwargs)))
    tools = page._cloud_context_for('只读取 ' + NEW_URL, [])
    tools.client_factory(next(iter(tools.bindings.values())))
    assert page.wps_path == service.documents_path
    assert received[0][1] == {'account_path': service.path}


def test_main_window_blocks_update_during_login_and_normal_close_cancels(page_factory, app, tmp_path, monkeypatch):
    from xiaoyu import ui
    from xiaoyu.material_store import MaterialStore

    service = AccountService()
    monkeypatch.setattr(ai_ui.QDesktopServices, 'openUrl', lambda url: True)
    page = page_factory(wps_account_service=service)
    monkeypatch.setattr(ui, 'load_config', lambda: {})
    monkeypatch.setattr(ui, 'save_config', lambda value: None)
    monkeypatch.setattr(ai_ui, 'FinanceAIPage', lambda: page)
    window = ui.MainWindow(material_store=MaterialStore(tmp_path / 'materials.sqlite3'))
    window._navigate(4)
    monkeypatch.setattr(window, '_save_settings', lambda: True)
    try:
        page.message_edit.setPlainText('更新 ' + NEW_URL)
        page.send_message()
        pump(app, service.entered.is_set)
        worker = page._connection_job
        window.update_controller.candidate_dir = tmp_path / 'ready'
        window.update_controller.release = {'version': '1.0.0'}
        update_close = QCloseEvent()
        window.closeEvent(update_close)
        assert not update_close.isAccepted() and not worker.cancel.is_set()
        assert '当前操作结束后再更新' in window.notice.text()
        assert '当前操作结束后再更新' in page.status_label.text()
        window.update_controller.candidate_dir = None
        window.update_controller.release = None
        normal_close = QCloseEvent()
        window.closeEvent(normal_close)
        assert normal_close.isAccepted() and page._closed and worker.cancel.is_set()
        worker.thread.join(timeout=3)
        assert not worker.thread.is_alive() and not service.resolve_calls
    finally:
        page.shutdown()
        service.release.set()
        window.update_controller.candidate_dir = None
        window.update_controller.release = None
        dispose(app, window)
