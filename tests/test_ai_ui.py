"""Exercise the actual Qt assistant with an isolated, non-network API double."""
from __future__ import annotations

import os
import threading
import time
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

import pytest
from PySide6.QtCore import QByteArray, QEvent, Qt, QUrl
from PySide6.QtGui import QInputMethodEvent, QKeyEvent, QTextCursor, QTextDocument
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QDialog, QFileDialog

from xiaoyu import ai_ui
from xiaoyu.ai_service import AIError, AIResult, GeneratedFile, RequestCancelled, UploadedFile
from xiaoyu.ai_settings import AISettings, UsageLedger, load_settings
from xiaoyu.ai_rules import MAX_RULES_CHARS, load_rules, save_rules


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
        self.sent_rules = []
        self.cancel_send = False
        self.download_data = b'category,amount\nOffice,30\n'
        self.send_entered = threading.Event()
        self.release = None
        self.instances.append(self)

    def upload_file(self, path, cancel=None):
        self.uploads.append(Path(path))
        return UploadedFile('file-' + str(len(self.uploads)), Path(path).name, Path(path).stat().st_size)

    def send(self, message, files, history, container_id=None, web_search=False,
             cancel=None, on_text=None, on_status=None, *, custom_rules=''):
        self.sends.append((message, list(files), list(history), container_id, web_search))
        self.sent_rules.append(custom_rules)
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
        kwargs.setdefault('rules_path', tmp_path / f'rules-{len(pages)}.json')
        page = ai_ui.FinanceAIPage(**kwargs)
        pages.append(page)
        return page

    yield create
    for page in pages:
        page.shutdown()
        page.close()
        page.deleteLater()
    app.processEvents()


@pytest.mark.parametrize('key,modifiers', [
    (Qt.Key.Key_Return, Qt.KeyboardModifier.NoModifier),
    (Qt.Key.Key_Enter, Qt.KeyboardModifier.KeypadModifier),
])
def test_enter_sends_once_through_page(page_factory, app, key, modifiers):
    page = page_factory()
    page.message_edit.setPlainText('汇总费用')
    QTest.keyClick(page.message_edit, key, modifiers)
    pump(app, lambda: not page.is_busy)
    assert OfflineClient.instances[0].sends[0][0] == '汇总费用'
    assert len(OfflineClient.instances[0].sends) == 1
    assert page.message_edit.toPlainText() == ''


@pytest.mark.parametrize('key', [Qt.Key.Key_Return, Qt.Key.Key_Enter])
@pytest.mark.parametrize('modifiers', [
    Qt.KeyboardModifier.ShiftModifier,
    Qt.KeyboardModifier.AltModifier,
    Qt.KeyboardModifier.ShiftModifier | Qt.KeyboardModifier.AltModifier,
])
def test_modified_enter_inserts_newline_at_selection(page_factory, key, modifiers):
    page = page_factory()
    page.message_edit.setPlainText('前中后')
    cursor = page.message_edit.textCursor()
    cursor.setPosition(1)
    cursor.setPosition(2, QTextCursor.MoveMode.KeepAnchor)
    page.message_edit.setTextCursor(cursor)
    if key == Qt.Key.Key_Enter:
        modifiers |= Qt.KeyboardModifier.KeypadModifier
    QTest.keyClick(page.message_edit, key, modifiers)
    assert page.message_edit.toPlainText() == '前\n后'
    assert not OfflineClient.instances
    page.message_edit.undo()
    assert page.message_edit.toPlainText() == '前中后'


def test_enter_ignores_empty_input_and_auto_repeat(page_factory):
    page = page_factory()
    QTest.keyClick(page.message_edit, Qt.Key.Key_Return)
    assert page.message_edit.toPlainText() == ''
    page.message_edit.setPlainText('  \n  ')
    QTest.keyClick(page.message_edit, Qt.Key.Key_Return)
    assert page.message_edit.toPlainText() == '  \n  '
    page.message_edit.setPlainText('解释费用')
    event = QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_Return,
                      Qt.KeyboardModifier.NoModifier, '\r', True)
    QApplication.sendEvent(page.message_edit, event)
    assert page.message_edit.toPlainText() == '解释费用'
    assert not OfflineClient.instances


def test_enter_during_chinese_composition_does_not_send(page_factory, app):
    page = page_factory()
    page.message_edit.setPlainText('解释')
    page.message_edit.moveCursor(QTextCursor.MoveOperation.End)
    QApplication.sendEvent(page.message_edit, QInputMethodEvent('fei', []))
    QTest.keyClick(page.message_edit, Qt.Key.Key_Return)
    assert not OfflineClient.instances
    assert page.message_edit.toPlainText() == '解释'
    commit = QInputMethodEvent()
    commit.setCommitString('费用')
    QApplication.sendEvent(page.message_edit, commit)
    QTest.keyClick(page.message_edit, Qt.Key.Key_Return)
    pump(app, lambda: not page.is_busy)
    assert len(OfflineClient.instances[0].sends) == 1
    assert OfflineClient.instances[0].sends[0][0] == '解释费用'


def test_enter_does_not_send_again_while_busy(page_factory, app):
    page = page_factory()
    client = page._get_client()
    client.release = threading.Event()
    page.message_edit.setPlainText('解释费用')
    try:
        QTest.keyClick(page.message_edit, Qt.Key.Key_Return)
        pump(app, client.send_entered.is_set)
        QTest.keyClick(page.message_edit, Qt.Key.Key_Return)
        assert len(client.sends) == 1
    finally:
        client.release.set()
        pump(app, lambda: not page.is_busy)


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


@pytest.mark.parametrize('discard_partial', [True, False])
def test_mapping_gate_error_discards_only_current_stream_and_keeps_cloud_records(page_factory, app, discard_partial):
    record = {'task_id': 'd' * 32, 'document_url': 'https://www.kdocs.cn/l/testMapping',
              'document_name': '模板.xlsx', 'sheet': '私户', 'status': 'partial',
              'updated': 2, 'unknown': 0, 'conflicts': 1, 'summary': '已核对 2 格，另有冲突。'}

    class GateClient(OfflineClient):
        def send(self, message, files, history, **kwargs):
            kwargs['on_text']('全部已经更新成功。')
            error = AIError('尚未选择全部列对应，请先逐行选择。')
            if discard_partial:
                error.discard_partial_text = True
            error.cloud_results = [dict(record)]
            raise error

    page = page_factory(client_factory=GateClient)
    previous = [('user', '上一次核对结果是什么？'), ('assistant', '上一次的真实结果已记录。')]
    page.messages = list(previous)
    page.history = [{'role': role, 'content': text} for role, text in previous]
    page.message_edit.setPlainText('更新本次流水')
    page.send_message()
    pump(app, lambda: not page.is_busy)
    assert page.messages[:2] == previous
    assert page.history == [{'role': role, 'content': text} for role, text in previous]
    assert (('assistant', '全部已经更新成功。') in page.messages) is (not discard_partial)
    assert page.messages[-1][0] == 'notice' and '尚未选择全部列对应' in page.messages[-1][1]
    assert page.cloud_result_list.count() == 1 and page.cloud_results[0]['updated'] == 2
    if discard_partial:
        assert page._partial_text == ''
        assert '全部已经更新成功。' not in page.transcript.toPlainText()


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


def mapping_question():
    return {'kind': 'mapping', 'question': '请一次确认全部列对应', 'sheet': '私户', 'options': [],
            'document_url': 'https://www.kdocs.cn/l/testMapping', 'mappings': [
                {'target_column': '日期', 'source_options': ['交易日期'], 'selected_source': '交易日期',
                 'note': '保留日期格式'},
                {'target_column': '对方账户', 'source_options': ['对方户名', '对方账号'], 'selected_source': None,
                 'note': '请选择需要显示的内容'},
                {'target_column': '收入', 'source_options': ['交易金额中的正数'],
                 'selected_source': '交易金额中的正数', 'note': '负数填写到支出，取绝对值'},
                {'target_column': '备注', 'source_options': ['摘要'], 'selected_source': '摘要',
                 'note': '空白保留原值'},
            ]}


def select_mapping_sources(page, sources=None):
    sources = sources or ['交易日期', '对方户名', '交易金额中的正数', '摘要']
    for combo, source in zip(page._mapping_inputs, sources):
        combo.setCurrentText(source)


class PendingMappingBackend:
    """UI contract double: only explicitly edited user drafts survive a turn."""
    def __init__(self, question=None):
        self.question = deepcopy(question or mapping_question())
        self.question.update(mapping_id='mapping-one', scope_id='scope-one', user_selections={})
        self.questions = [deepcopy(self.question)]
        self.invalidated = []

    def pending_mapping_questions(self):
        return [deepcopy(self.question)] if self.question else []

    def update_mapping_draft(self, mapping_id, selections):
        if self.question is None or self.question['mapping_id'] != mapping_id:
            raise ValueError('过期')
        self.question['user_selections'] = dict(selections)

    def invalidate_mapping(self, reason):
        self.invalidated.append(reason)
        self.question = None


def test_mapping_form_requires_all_choices_and_submits_once(page_factory, monkeypatch):
    page = page_factory()
    page._cloud_tools = SimpleNamespace(questions=[mapping_question()])
    sent = []
    monkeypatch.setattr(page, 'send_message', lambda: sent.append(page.message_edit.toPlainText()))
    page._show_cloud_questions()
    assert not page.question_scroll.isHidden()
    assert len(page._mapping_inputs) == 4 and len(page._clarification_buttons) == 1
    assert all(combo.currentText() == '' for combo in page._mapping_inputs)
    page._clarification_buttons[0].click()
    assert not sent
    page._mapping_inputs[1].setCurrentText('对方户名')
    page._clarification_buttons[0].click()
    assert not sent  # One choice cannot silently accept the other AI recommendations.
    select_mapping_sources(page)
    fields = page._mapping_forms[0]['fields']
    page._clarification_buttons[0].click()
    assert len(sent) == 1
    assert sent[0].startswith('针对“请一次确认全部列对应”：')
    for text in ('“日期” ← “交易日期”', '“对方账户” ← “对方户名”', '“收入” ← “交易金额中的正数”',
                 '“备注” ← “摘要”'):
        assert text in sent[0]
    assert '空白保留原值' not in sent[0]  # AI notes are not new user instructions.
    page._sync_controls()
    page._clarification_buttons[0].click()
    page._answer_mapping('请一次确认全部列对应', '私户', fields)
    assert len(sent) == 1  # A consumed form cannot issue a second confirmation.


def test_mapping_supports_typing_existing_source_busy_disable_and_clear(page_factory, monkeypatch):
    page = page_factory()
    page._cloud_tools = SimpleNamespace(questions=[mapping_question()])
    sent = []
    monkeypatch.setattr(page, 'send_message', lambda: sent.append(page.message_edit.toPlainText()))
    page._show_cloud_questions()
    select_mapping_sources(page)
    page._mapping_inputs[1].setEditText('自定义户名字段')
    page._clarification_buttons[0].click()
    assert not sent and '来源不在候选列表' in page.status_label.text()
    page._mapping_inputs[1].setEditText('对方账号')
    fields = page._mapping_forms[0]['fields']
    page._job = SimpleNamespace(cancel=threading.Event())
    page._sync_controls()
    assert all(not field.isEnabled() for field in page._mapping_inputs)
    assert all(not button.isEnabled() for button in page._mapping_choice_buttons)
    assert not page._clarification_buttons[0].isEnabled()
    page._answer_mapping('请一次确认全部列对应', '私户', fields)
    assert not sent
    page._job = None
    page._sync_controls()
    page._clarification_buttons[0].click()
    assert '对方账号' in sent[0]
    page._clear_cloud_questions()
    assert not page._mapping_forms and not page._mapping_inputs
    assert not page._mapping_choice_buttons
    assert not page._clarification_buttons and page.question_scroll.isHidden()


def test_mapping_choose_button_opens_candidates_and_picks_a_source(page_factory, app):
    page = page_factory()
    page._cloud_tools = SimpleNamespace(questions=[mapping_question()])
    page.resize(1200, 850)
    page._show_cloud_questions()
    page.show()
    app.processEvents()
    combo = page._mapping_inputs[1]
    choose = page._mapping_choice_buttons[1]
    page.question_scroll.ensureWidgetVisible(choose)
    QTest.mouseClick(choose, Qt.MouseButton.LeftButton)
    app.processEvents()
    assert combo.view().isVisible()
    index = combo.model().index(1, 0)
    QTest.mouseClick(combo.view().viewport(), Qt.MouseButton.LeftButton,
                     pos=combo.view().visualRect(index).center())
    app.processEvents()
    assert combo.currentText() == '对方账号'
    assert not combo.view().isVisible()


def test_mapping_lists_all_actual_sources_and_keeps_user_skip(page_factory, monkeypatch):
    from xiaoyu.cloud_mapping import KEEP_SOURCE
    page = page_factory()
    question = mapping_question()
    question['mappings'][0].update(source_options=[f'源字段{i:03d}' for i in range(300)] + [KEEP_SOURCE],
                                   selected_source='源字段001')
    page._cloud_tools = PendingMappingBackend(question)
    sent = []
    monkeypatch.setattr(page, 'send_message', lambda: sent.append(page.message_edit.toPlainText()))
    page._show_cloud_questions()
    assert all(combo.currentText() == '' for combo in page._mapping_inputs)
    assert page._mapping_inputs[0].count() == 301
    assert page._mapping_inputs[0].findText('源字段299') >= 0
    select_mapping_sources(page, ['源字段299', '对方户名', KEEP_SOURCE, KEEP_SOURCE])
    page._show_cloud_questions()  # An ordinary redraw restores only actual user choices.
    assert [combo.currentText() for combo in page._mapping_inputs] == [
        '源字段299', '对方户名', KEEP_SOURCE, KEEP_SOURCE]
    page._clarification_buttons[0].click()
    assert '“收入” ← “保留不更新”' in sent[0]
    assert '“备注” ← “保留不更新”' in sent[0]


def test_pending_mapping_and_partial_user_choices_survive_other_question(page_factory):
    page = page_factory()
    backend = PendingMappingBackend()
    page._cloud_tools = backend
    page._show_cloud_questions()
    page._mapping_inputs[1].setCurrentText('对方账号')
    backend.questions = [{'question': '只处理本月流水吗？', 'options': ['只处理本月', '全部流水']}]
    page._show_cloud_questions()
    assert len(page._mapping_forms) == 1 and len(page._clarification_buttons) == 3
    assert [combo.currentText() for combo in page._mapping_inputs] == ['', '对方账号', '', '']


def test_expired_mapping_and_changed_attachment_cannot_reuse_selection(page_factory, monkeypatch, tmp_path):
    page = page_factory()
    backend = PendingMappingBackend()
    page._cloud_tools = backend
    sent = []
    monkeypatch.setattr(page, 'send_message', lambda: sent.append(page.message_edit.toPlainText()))
    page._show_cloud_questions()
    select_mapping_sources(page)
    fields = page._mapping_forms[0]['fields']
    source = tmp_path / 'different.csv'
    source.write_text('amount\n42\n', encoding='utf-8')
    page.add_paths([source])
    assert backend.invalidated == ['来源文件已变化']
    assert not page._mapping_forms
    page._answer_mapping('请一次确认全部列对应', '私户', fields)
    page._show_cloud_questions()
    assert not sent and not page._mapping_inputs
    backend.question = dict(mapping_question(), mapping_id='mapping-two', scope_id='scope-two', user_selections={})
    page._show_cloud_questions()
    assert all(combo.currentText() == '' for combo in page._mapping_inputs)
    page.attachment_list.setCurrentRow(0)
    page.remove_selected_file()
    assert backend.invalidated == ['来源文件已变化', '来源文件已变化']
    assert not page._mapping_forms


def test_backend_scope_change_rejects_still_visible_mapping(page_factory, monkeypatch):
    page = page_factory()
    backend = PendingMappingBackend()
    page._cloud_tools = backend
    sent = []
    monkeypatch.setattr(page, 'send_message', lambda: sent.append(page.message_edit.toPlainText()))
    page._show_cloud_questions()
    select_mapping_sources(page)
    backend.invalidate_mapping('任务要求已变化')
    page._clarification_buttons[0].click()
    assert not sent and not page._mapping_forms
    assert '列对应已失效' in page.status_label.text()


def test_invalid_mapping_is_not_displayed_and_markup_stays_plain(page_factory):
    page = page_factory()
    question = mapping_question()
    question['mappings'][0]['target_column'] = '<b>日期</b>'
    question['mappings'][0]['note'] = '<img src="file:///private.png">'
    page._cloud_tools = SimpleNamespace(questions=[question])
    page._show_cloud_questions()
    assert len(page._mapping_inputs) == 4
    assert page._mapping_inputs[0].accessibleName() == '<b>日期</b>的来源字段'
    question['mappings'][1]['source_options'] = []
    page._show_cloud_questions()
    assert not page._mapping_inputs and page.question_scroll.isHidden()


def test_markdown_mapping_table_remains_one_complete_table(page_factory):
    page = page_factory()
    pairs = [('日期', '交易日期'), ('对方账户', '对方户名'), ('收入', '正数金额'),
             ('支出', '负数取绝对值'), ('余额', '保留原公式'), ('备注', '摘要')]
    markdown = '| 云表目标列 | 银行流水来源 |\n| --- | --- |\n' + '\n'.join(
        f'| {target} | {source} |' for target, source in pairs)
    page.messages = [('assistant', markdown)]
    page._render_chat()
    tables = [frame for frame in page.transcript.document().rootFrame().childFrames()
              if hasattr(frame, 'cellAt')]
    assert len(tables) == 1
    table = tables[0]
    assert table.rows() == 7 and table.columns() == 2
    for row, pair in enumerate(pairs, 1):
        for column, expected in enumerate(pair):
            cell = table.cellAt(row, column)
            cursor = cell.firstCursorPosition()
            cursor.setPosition(cell.lastCursorPosition().position(), QTextCursor.MoveMode.KeepAnchor)
            assert cursor.selectedText() == expected


def test_group_task_displays_one_record_and_per_column_status(page_factory):
    page = page_factory()
    base = {'task_id': 'a' * 32, 'schema': 2, 'kind': 'table_update',
            'document_url': 'https://www.kdocs.cn/l/testMapping', 'document_name': '模板.xlsx',
            'sheet': '私户', 'status': 'partial', 'updated': 16, 'unchanged': 0,
            'skipped': 0, 'unknown': 0, 'conflicts': 1, 'summary': '部分完成',
            'columns': [
                {'target_column': '日期', 'source_field': '交易日期', 'status': 'completed', 'updated': 16},
                {'target_column': '收入', 'source_field': '正数金额', 'status': 'partial', 'conflicts': 1},
            ], 'items': [{'key_values': ['001'], 'target_column': '日期', 'status': 'verified',
                         'old_value': '', 'new_value': '2026-11-10'}]}
    child = dict(base, task_id='b' * 32, parent_task_id=base['task_id'])
    page._record_cloud_results([child, base])
    assert page.cloud_result_list.count() == 1
    assert '2 列统一任务' in page.cloud_result_list.item(0).text()
    details = page._format_cloud_details(base)
    assert '日期 ← 交易日期' in details and '收入 ← 正数金额' in details
    assert '已核对完成' in details and '部分完成' in details
    assert '001 · 日期' in details


def test_group_final_check_is_not_described_as_historical(page_factory):
    page = page_factory()
    result = {'status': 'completed', 'updated': 64, 'unknown': 0, 'conflicts': 0,
              'final_check': {'checked': 64, 'matched': 64, 'changed': 0, 'missing': 0,
                              'duplicate': 0, 'failed': False}}
    result['current_check'] = dict(result['final_check'])
    assert '全部列已核对完成' in page._cloud_result_text(result)
    assert '历史' not in page._cloud_result_text(result)
    result['unknown'] = 1
    assert '结果待核对' in page._cloud_result_text(result)
    result['unknown'] = 0
    result['last_action'] = 'check'
    result['current_check'].update(changed=1, matched=63)
    assert '历史更新已完成' in page._cloud_result_text(result)
    assert '当前有 1 项不同' in page._cloud_result_text(result)


def test_history_accepts_v2_groups_and_legacy_records_hides_children(page_factory, tmp_path):
    import json
    root = tmp_path / 'cloud-history'
    root.mkdir()
    page = page_factory(cloud_journal_dir=root)
    common = {'document_url': 'https://www.kdocs.cn/l/testMapping', 'document_name': '模板.xlsx',
              'sheet': '私户', 'status': 'completed', 'summary': '完成', 'updated': 16}
    for task, fields in [
        ('a' * 32, {'schema': 1}), ('b' * 32, {'schema': 2}),
        ('c' * 32, {'schema': 2, 'kind': 'table_update', 'columns': [{'target_column': '日期'}]}),
        ('d' * 32, {'schema': 2, 'parent_task_id': 'c' * 32}),
    ]:
        (root / (task + '.json')).write_text(json.dumps(dict(common, task_id=task, **fields)), encoding='utf-8')
    page.show_cloud_history()
    assert page.cloud_result_list.count() == 3
    assert {r['task_id'] for r in page.cloud_results} == {'a' * 32, 'b' * 32, 'c' * 32}
    links, historical = page._requested_cloud_links('核对任务编号 ' + 'c' * 32)
    assert historical and links == [common['document_url']]


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
    monkeypatch.setattr(ai_ui, 'load_rules', lambda path: '')
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


def edit_rules(monkeypatch, page, text, *, save=True):
    def execute(dialog):
        dialog.rules_edit.setPlainText(text)
        if save:
            dialog.buttons.button(ai_ui.QDialogButtonBox.StandardButton.Save).click()
        else:
            dialog.reject()
        return dialog.result()

    monkeypatch.setattr(ai_ui.AIRulesDialog, 'exec', execute)
    page.rules_button.click()


def test_rules_save_without_key_and_survive_new_chat_and_restart(page_factory, app, tmp_path, monkeypatch):
    rules_path = tmp_path / 'shared-rules.json'
    page = page_factory(settings=AISettings(), rules_path=rules_path)
    edit_rules(monkeypatch, page, '回答简洁，先给结论。\n需要操作时分步骤说明。')
    assert load_rules(rules_path) == page.custom_rules
    assert '已启用' in page.rules_summary.text()
    assert '长期规则已保存' in page.status_label.text()
    assert not page.settings_path.exists()  # Saving preferences never touches credentials.
    page.history.append({'role': 'user', 'content': '之前的临时要求'})
    page.messages.append(('user', '之前的临时要求'))
    page.new_chat()
    assert page.custom_rules == load_rules(rules_path)
    assert not page.history and not page.messages
    page.shutdown()
    restarted = page_factory(rules_path=rules_path)
    assert restarted.custom_rules == page.custom_rules
    restarted.message_edit.setPlainText('解释预付账款')
    restarted.send_message()
    pump(app, lambda: not restarted.is_busy)
    assert restarted.client.sent_rules == [page.custom_rules]


def test_edit_and_clear_rules_apply_to_next_request_without_losing_chat(page_factory, app, monkeypatch):
    page = page_factory()
    edit_rules(monkeypatch, page, '回答简洁。')
    page.message_edit.setPlainText('解释预付账款')
    page.send_message()
    pump(app, lambda: not page.is_busy)
    client = page.client
    old_history = list(page.history)
    old_files = list(page.generated_files)
    edit_rules(monkeypatch, page, '有操作时列出步骤。')
    assert page.client is client and page.history == old_history
    assert page.generated_files == old_files and page.container_id == 'cntr-test'
    page.message_edit.setPlainText('那应该如何核对？')
    page.send_message()
    pump(app, lambda: not page.is_busy)
    assert client.sent_rules == ['回答简洁。', '有操作时列出步骤。']
    edit_rules(monkeypatch, page, '')
    assert load_rules(page.rules_path) == '' and page.custom_rules == ''
    assert '未设置' in page.rules_summary.text()
    page.message_edit.setPlainText('继续说明')
    page.send_message()
    pump(app, lambda: not page.is_busy)
    assert client.sent_rules[-1] == ''


def test_rules_cancel_does_not_change_saved_or_active_rules(page_factory, monkeypatch):
    page = page_factory()
    edit_rules(monkeypatch, page, '回答简洁。')
    before = page.rules_path.read_bytes()
    edit_rules(monkeypatch, page, '不需要保存的修改', save=False)
    assert page.custom_rules == '回答简洁。'
    assert page.rules_path.read_bytes() == before


def test_rules_save_failure_keeps_dialog_and_draft_for_retry(app, tmp_path, monkeypatch):
    path = tmp_path / 'rules.json'
    save_rules('原规则', path)
    original = path.read_bytes()
    dialog = ai_ui.AIRulesDialog('原规则', path)
    dialog.show()
    dialog.rules_edit.setPlainText('回答简洁，先给结论。')
    try:
        with monkeypatch.context() as patch:
            patch.setattr(ai_ui, 'save_rules', lambda *args: (_ for _ in ()).throw(OSError('文件被占用')))
            dialog.accept()
        assert dialog.isVisible() and dialog.result() != QDialog.DialogCode.Accepted
        assert '规则未保存' in dialog.error_label.text()
        assert dialog.value() == '回答简洁，先给结论。'
        assert path.read_bytes() == original
        dialog.accept()
        assert dialog.result() == QDialog.DialogCode.Accepted
        assert load_rules(path) == '回答简洁，先给结论。'
    finally:
        dialog.close()
        dialog.deleteLater()
        app.processEvents()


def test_rules_length_limit_and_clear_button(app, tmp_path):
    path = tmp_path / 'rules.json'
    dialog = ai_ui.AIRulesDialog('', path)
    try:
        dialog.rules_edit.setPlainText('简' * (MAX_RULES_CHARS + 1))
        dialog.accept()
        assert dialog.result() != QDialog.DialogCode.Accepted
        assert str(MAX_RULES_CHARS) in dialog.error_label.text()
        assert not path.exists()
        dialog.buttons.button(ai_ui.QDialogButtonBox.StandardButton.Reset).click()
        assert dialog.value() == ''
        dialog.accept()
        assert load_rules(path) == ''
    finally:
        dialog.close()
        dialog.deleteLater()
        app.processEvents()


def test_busy_request_disables_rules_editor(page_factory, app):
    page = page_factory()
    client = page._get_client()
    client.release = threading.Event()
    page.message_edit.setPlainText('解释科目')
    page.send_message()
    pump(app, client.send_entered.is_set)
    assert not page.rules_button.isEnabled()
    client.release.set()
    pump(app, lambda: not page.is_busy)
    assert page.rules_button.isEnabled()


def test_corrupt_rules_remain_visible_until_successful_save(page_factory, tmp_path, monkeypatch):
    path = tmp_path / 'broken-rules.json'
    path.write_text('{broken', encoding='utf-8')
    page = page_factory(rules_path=path)
    assert '长期规则' in page.status_label.text() and '损坏' in page.status_label.text()
    assert page.rules_summary.text() == '规则读取失败'
    assert path.read_text(encoding='utf-8') == '{broken'
    edit_rules(monkeypatch, page, '回答简洁。')
    assert not page._rules_load_error
    assert page.rules_summary.text().startswith('已启用')
    assert load_rules(path) == '回答简洁。'
