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
from shiboken6 import isValid

from xiaoyu import ai_ui, wps_ui
from xiaoyu.ai_service import AIError, AIResult
from xiaoyu.ai_settings import AISettings, UsageLedger
from xiaoyu.wps_service import WpsBinding


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
        self.instances.append(self)

    def send(self, message, files, history, **kwargs):
        self.calls.append((message, kwargs))
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

    def create():
        page = ai_ui.FinanceAIPage(settings=AISettings(api_key='fake-openai-key'),
                                  settings_path=tmp_path / 'settings.json', rules_path=tmp_path / 'rules.json',
                                  ledger=UsageLedger(tmp_path / 'usage.json'), client_factory=AIClient,
                                  cloud_tools_factory=CloudTools, cloud_journal_dir=tmp_path / 'tasks')
        pages.append(page)
        return page

    yield create
    for page in pages:
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
