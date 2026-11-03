"""End-to-end desktop callbacks with synthetic data and isolated persistence."""
import copy
import os

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

import openpyxl
import pytest
from PySide6.QtWidgets import QApplication

from xiaoyu import ui
from xiaoyu.imports import ImportResult, MappingSpec, inspect_workbook, parse_workbook, suggest_mapping
from xiaoyu.material_store import MaterialStore
from xiaoyu.service import bundle_from_records


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    app = QApplication.instance() or QApplication([])
    saved = {}
    monkeypatch.setattr(ui, 'load_config', lambda: copy.deepcopy(saved))
    monkeypatch.setattr(ui, 'save_config', lambda value: saved.update(copy.deepcopy(value)))
    store = MaterialStore(tmp_path / 'materials.sqlite3')
    windows = []

    def create():
        window = ui.MainWindow(material_store=store)
        monkeypatch.setattr(window, '_run', lambda function, callback, message: callback(function()))
        windows.append(window)
        return window

    def confirm(dialog):
        assert dialog.confirm_button.isEnabled(), dialog.validation.text()
        dialog.accept()
        return dialog.result()

    monkeypatch.setattr(ui.MappingDialog, 'exec', confirm)
    yield create, store, saved
    for window in windows:
        window._save_timer.stop()
        window.close()
        window.deleteLater()
    app.processEvents()


def workbook(tmp_path, name, headers, rows, sheet='明细'):
    path = tmp_path / name
    book = openpyxl.Workbook()
    book.active.title = sheet
    book.active.append(headers)
    for row in rows:
        book.active.append(row)
    book.save(path)
    book.close()
    return path


def test_four_kinds_confirmation_persistence_export_and_accounting(workspace, tmp_path, monkeypatch):
    create, store, saved = workspace
    window = create()
    cases = {
        'bank': ('流水', ['记账日期', '收款金额', '付款金额', '交易对方'], [['2026-10-03', 100, '', '测试客户']]),
        'payroll': ('2026年10月', ['员工姓名', '应发金额', '实发金额'], [['测试员工', 5000, 5000]]),
        'logistics': ('物流', ['快递单编号', '寄件时间', '运费合计'], [['000001', '2026-10-03', 12]]),
        'invoice': ('发票', ['发票号', '开票时间', '含税金额'], [['00012345678901234567', '2026-10-03', 100]]),
    }
    for kind, (sheet, headers, rows) in cases.items():
        path = workbook(tmp_path, kind + '.xlsx', headers, rows, sheet)
        window.file_panels[kind].add_paths([str(path)])
    window._load_inputs()
    assert len(window.material_records) == 4
    assert window.result is None
    assert window.pages.currentIndex() == 3
    assert all(store.load_profiles('默认账套', kind) for kind in cases)
    window._load_inputs()
    assert len(window.material_records) == 4
    output = tmp_path / '资料.xlsx'
    monkeypatch.setattr(ui.QFileDialog, 'getSaveFileName', lambda *a, **k: (str(output), ''))
    window._export_materials()
    check = openpyxl.load_workbook(output)
    assert len(check.sheetnames) == 6
    check.close()
    window._prepare_accounting()
    assert len(window.bundle.transactions) == len(window.bundle.payroll) == 1
    assert len(window.result.vouchers) == 1
    assert window.result.debit == window.result.credit == 100
    window._save_settings()
    restored = create()
    assert len(restored.material_records) == 4
    assert restored.invoice_panel.paths == window.invoice_panel.paths


def test_logistics_only_does_not_read_voucher_template(workspace, tmp_path, monkeypatch):
    create, store, _ = workspace
    window = create()
    path = workbook(tmp_path, '物流.xlsx', ['运单号', '发货日期'], [['001', '2026-10-03']])
    window.logistics_panel.add_paths([str(path)])
    window._template_path = str(tmp_path / '不存在的凭证模板.xls')
    window._load_inputs()
    assert len(window.material_records) == 1
    assert window.records_page.export_button.isEnabled()
    assert not window.records_page.generate_button.isEnabled()
    assert window.bundle is None


def test_company_switch_restores_separate_files_rules_and_records(workspace, tmp_path):
    create, store, saved = workspace
    window = create()
    path = workbook(tmp_path, '流水.xlsx', ['日期', '收入'], [['2026-10-03', 100]])
    window.bank_panel.add_paths([str(path)])
    window._load_inputs()
    window.bank_account.setEditText('公司甲科目')
    window.period_combo.setEditText('2026-09')
    window._add_mapping('测试客户', '1122')
    window.company_combo.addItem('另一公司')
    window.company_combo.setCurrentText('另一公司')
    assert window.material_records == []
    assert not window.bank_panel.paths
    assert window.mapping_table.rowCount() == 0
    assert window.bundle is None and window.result is None
    assert window._current_period() == ''
    window.bank_account.setEditText('公司乙科目')
    window.company_combo.setCurrentText('默认账套')
    assert len(window.material_records) == 1
    assert window.bank_panel.paths == [str(path.resolve())]
    assert window.bank_account.currentText() == '公司甲科目'
    assert window._current_period() == '2026-09'
    assert window.mapping_table.rowCount() == 1
    assert saved['company_settings']['另一公司']['options']['bank_account'] == '公司乙科目'


def test_bad_file_does_not_discard_another_confirmed_file(workspace, tmp_path):
    create, store, _ = workspace
    window = create()
    bad = tmp_path / '损坏.xlsx'
    bad.write_bytes(b'not an excel')
    good = workbook(tmp_path, '流水.xlsx', ['日期', '收入'], [['2026-10-03', 100]])
    window.bank_panel.add_paths([str(bad), str(good)])
    window._load_inputs()
    assert len(window.material_records) == 1
    assert any('损坏.xlsx' in issue.message for issue in window.material_issues)


def test_reconfirmed_mapping_updates_voucher_amount_not_duplicate(tmp_path):
    path = workbook(tmp_path, '流水.xlsx', ['日期', '金额甲', '金额乙'], [['2026-10-03', 100, 200]])
    source = inspect_workbook(path)
    store = MaterialStore(tmp_path / 'db.sqlite3')
    for column in (1, 2):
        result = parse_workbook(source, [MappingSpec('bank', '明细', 0, {'date': 0, 'income': column})])
        store.save_import('公司', result)
    records = store.load_records('公司')
    assert len(records) == 1
    assert bundle_from_records(records).transactions[0].income == 200


def test_remove_saved_copy_invalidates_old_voucher_but_preserves_input(workspace, tmp_path):
    create, store, _ = workspace
    window = create()
    path = workbook(tmp_path, '流水.xlsx', ['日期', '收入'], [['2026-10-03', 100]])
    window.bank_panel.add_paths([str(path)])
    window._load_inputs()
    window._prepare_accounting()
    assert window.result is not None
    window._remove_materials([window.material_records[0].id])
    assert window.result is None and not window.material_records
    assert path.is_file()


def test_payroll_summary_warning_reaches_voucher_preview(workspace, tmp_path):
    create, store, _ = workspace
    window = create()
    path = workbook(tmp_path, '工资.xlsx', ['员工姓名', '应发金额', '实发金额'],
                    [['合成员工', 5000, 5000], ['合计', 9000, 9000]], '2026年10月')
    window.payroll_panel.add_paths([str(path)])
    window._load_inputs()
    assert any('合计' in issue.message for issue in window.material_issues)
    window.payroll_accrual.setChecked(True)
    window._prepare_accounting()
    assert any('合计' in issue.message for issue in window.result.issues)
