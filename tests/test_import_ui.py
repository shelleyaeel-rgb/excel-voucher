"""Column confirmation and material review behavior using isolated Qt controls."""
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import QItemSelectionModel
from PySide6.QtWidgets import QApplication, QDialog, QMessageBox

from xiaoyu.import_ui import MappingDialog, RecordsPage
from xiaoyu.imports import ImportRecord, WorkbookSource, header_signature
from xiaoyu.models import Issue


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


def source(sheets, name="测试.xlsx"):
    return WorkbookSource(name, "synthetic", sheets)


def test_bank_requires_confirmation_and_keeps_user_column_choices(app):
    dialog = MappingDialog(source([("流水", [
        ["银行对账单"], ["日期", "收款", "付款", "交易对方"],
        ["2026-10-01", 100, "", "甲公司"],
    ])]), "bank")
    assert dialog.specs == []
    editor = dialog.editors[0]
    assert editor.header_row.value() == 2
    assert editor.include.isChecked()
    assert editor.mapping() == {"date": 0, "income": 1, "expense": 2, "counterparty": 3}
    editor.columns["counterparty"].setCurrentIndex(0)
    assert dialog.confirm_button.isEnabled()
    dialog.accept()
    assert dialog.result() == QDialog.DialogCode.Accepted
    assert dialog.specs[0].header_row == 1
    assert "counterparty" not in dialog.specs[0].mapping
    assert len(dialog.profiles) == 1
    dialog.close()


def test_unrelated_sheet_is_not_selected_and_can_be_added_manually(app):
    dialog = MappingDialog(source([
        ("流水", [["日期", "收入"], ["2026-10-01", 100]]),
        ("说明", [["请按照实际资料填写"], ["不包含业务数据"]]),
    ]), "bank")
    assert [e.include.isChecked() for e in dialog.editors] == [True, False]
    assert dialog.confirm_button.isEnabled()
    dialog.editors[1].include.setChecked(True)
    assert not dialog.confirm_button.isEnabled()
    assert "说明" in dialog.validation.text()
    dialog.editors[1].include.setChecked(False)
    dialog.accept()
    assert [s.sheet for s in dialog.specs] == ["流水"]
    dialog.close()


def test_single_bank_amount_requires_explicit_direction(app):
    dialog = MappingDialog(source([("明细", [["日期", "金额"], ["2026-10-01", 100]])]), "bank")
    assert not dialog.confirm_button.isEnabled()
    editor = dialog.editors[0]
    editor.direction.setCurrentIndex(editor.direction.findData("支出"))
    assert dialog.confirm_button.isEnabled()
    dialog.accept()
    assert dialog.specs[0].defaults["direction"] == "支出"
    dialog.close()


def test_bank_two_amount_modes_cannot_be_confirmed_together(app):
    dialog = MappingDialog(source([("流水", [
        ["日期", "收入", "支出", "金额"], ["2026-10-01", 100, "", 100],
    ])]), "bank")
    editor = dialog.editors[0]
    editor.columns["amount"].setCurrentIndex(editor.columns["amount"].findData(3))
    assert not dialog.confirm_button.isEnabled()
    assert "只能选择一种" in dialog.validation.text()
    editor.columns["amount"].setCurrentIndex(0)
    assert dialog.confirm_button.isEnabled()
    dialog.close()


@pytest.mark.parametrize("strategy", ["absolute", "signed"])
def test_single_bank_negative_amount_strategy_is_explicit_and_remembered(app, strategy):
    workbook = source([("流水", [["日期", "金额", "收支"], ["2026-10-01", -100, "支出"]])])
    dialog = MappingDialog(workbook, "bank")
    editor = dialog.editors[0]
    assert editor.amount_sign.isEnabled()
    assert editor.amount_sign.currentData() == ""
    assert "amount_sign" not in editor.defaults()
    editor.amount_sign.setCurrentIndex(editor.amount_sign.findData(strategy))
    dialog.accept()
    assert dialog.specs[0].defaults["amount_sign"] == strategy
    assert dialog.profiles[0]["defaults"]["amount_sign"] == strategy
    remembered = MappingDialog(workbook, "bank", dialog.profiles)
    assert remembered.editors[0].amount_sign.currentData() == strategy
    assert remembered.specs == []  # A remembered strategy still requires confirmation.
    remembered.close()
    dialog.close()


def test_bank_negative_amount_strategy_only_applies_to_single_amount_column(app):
    dialog = MappingDialog(source([("流水", [["日期", "收入", "支出", "金额"]])]), "bank")
    editor = dialog.editors[0]
    assert not editor.amount_sign.isEnabled()
    editor.amount_sign.setCurrentIndex(editor.amount_sign.findData("absolute"))
    assert "amount_sign" not in editor.defaults()
    editor.columns["income"].setCurrentIndex(0)
    editor.columns["expense"].setCurrentIndex(0)
    editor.columns["amount"].setCurrentIndex(editor.columns["amount"].findData(3))
    assert editor.amount_sign.isEnabled()
    assert editor.defaults()["amount_sign"] == "absolute"
    editor.include.setChecked(False)
    assert not editor.amount_sign.isEnabled()
    dialog.close()


def test_mixed_workbook_does_not_auto_select_other_categories_by_shared_date(app):
    workbook = source([
        ("流水", [["日期", "收入", "支出"], ["2026-10-01", 100, ""]]),
        ("说明", [["日期", "本月记录说明"], ["2026-10-01", "提醒"]]),
        ("发票", [["开票日期", "发票号码", "金额"], ["2026-10-01", "001", 100]]),
        ("物流", [["日期", "运单号", "数量"], ["2026-10-01", "002", 1]]),
    ])
    expected = {"bank": [True, False, False, False],
                "invoice": [False, False, True, False],
                "logistics": [False, False, False, True]}
    for kind, selection in expected.items():
        dialog = MappingDialog(workbook, kind)
        assert [editor.include.isChecked() for editor in dialog.editors] == selection
        dialog.close()


def test_payroll_month_is_required_and_never_saved_in_format(app):
    headers = ["员工姓名", "应发", "实发"]
    profile = {"kind": "payroll", "signature": header_signature(headers), "name": "常用工资格式",
               "mapping": {"name": 0, "gross": 1, "net": 2}, "defaults": {"period": "2026-01"}}
    dialog = MappingDialog(source([("工资", [headers, ["甲", 100, 100]])]), "payroll", [profile])
    editor = dialog.editors[0]
    assert editor.period.text() == ""
    assert not dialog.confirm_button.isEnabled()
    editor.period.setText("2026-13")
    assert not dialog.confirm_button.isEnabled()
    editor.period.setText("2026-10")
    assert dialog.confirm_button.isEnabled()
    dialog.accept()
    assert dialog.specs[0].defaults["period"] == "2026-10"
    assert "period" not in dialog.profiles[0]["defaults"]
    dialog.close()


def test_payroll_title_month_is_suggested_and_changed_header_invalidates_profile(app):
    headers = ["职员", "应付", "到手"]
    profile = {"kind": "payroll", "signature": header_signature(headers), "name": "特殊格式",
               "mapping": {"name": 0, "gross": 1, "net": 2}, "defaults": {}}
    workbook = source([("2026年10月", [headers, ["甲", 100, 100]])])
    dialog = MappingDialog(workbook, "payroll", [profile])
    editor = dialog.editors[0]
    assert editor.period.text() == "2026-10"
    assert "特殊格式" in editor.profile_hint.text()
    assert dialog.confirm_button.isEnabled()
    editor.header_row.setValue(2)
    assert editor.mapping() == {}
    assert not dialog.confirm_button.isEnabled()
    dialog.close()


def test_one_source_column_cannot_be_reused_as_two_money_fields(app):
    dialog = MappingDialog(source([("发票", [["发票号码", "含税金额"], ["001", 106]])]), "invoice")
    editor = dialog.editors[0]
    editor.columns["amount"].setCurrentIndex(editor.columns["amount"].findData(1))
    assert not dialog.confirm_button.isEnabled()
    assert "两种含义" in dialog.validation.text()
    dialog.close()


def test_remember_unchecked_produces_no_profile(app):
    dialog = MappingDialog(source([("物流", [["快递单号"], ["0000123"]])]), "logistics")
    editor = dialog.editors[0]
    editor.remember.setChecked(False)
    editor.subtype.setCurrentIndex(editor.subtype.findData("运输与运费"))
    dialog.accept()
    assert dialog.profiles == []
    assert dialog.specs[0].defaults["subtype"] == "运输与运费"
    dialog.close()


def record(identifier, kind, values, issues=None):
    return ImportRecord(identifier, kind, values, list(values.values()), "测试.xlsx", "资料", 2, issues or [])


def test_review_keeps_invalid_rows_and_filters_without_changing_export_scope(app):
    page = RecordsPage()
    rows = [
        record("bank", "bank", {"date": "2026-10-01", "income": "100", "counterparty": "甲"}),
        record("invoice", "invoice", {"date": "2026-09-01", "invoice_no": "0001", "total": "106"},
               [Issue("warning", "发票方向待确认")]),
        record("invalid", "logistics", {"date": "不明", "tracking_no": "0002"},
               [Issue("error", "日期无效")]),
    ]
    page.set_records(rows)
    assert page.table.rowCount() == 3
    assert "待核对 2 条" in page.statistics.text()
    assert page.generate_button.isEnabled()
    page.review_only.setChecked(True)
    assert page.table.rowCount() == 2
    page.period_filter.setCurrentIndex(page.period_filter.findData("unknown"))
    assert page.table.rowCount() == 1
    assert page.records == rows
    assert page.export_button.isEnabled()
    assert page.generate_button.isEnabled()  # Entire company, not only the filtered logistics row.
    page.set_busy(True)
    assert not page.export_button.isEnabled()
    assert not page.generate_button.isEnabled()
    page.close()


def test_review_search_and_selected_removal_only_emit_highlighted_ids(app, monkeypatch):
    page = RecordsPage()
    rows = [record("one", "logistics", {"tracking_no": "0001", "item": "小雨商品"}),
            record("two", "logistics", {"tracking_no": "0002", "item": "小雨配件"}),
            record("three", "invoice", {"invoice_no": "0003"})]
    page.set_records(rows)
    page.search.setText("小雨")
    assert page.table.rowCount() == 2
    assert page.selected_records() == []
    selection = page.table.selectionModel()
    selection.select(page.table.model().index(0, 0),
                     QItemSelectionModel.SelectionFlag.Select | QItemSelectionModel.SelectionFlag.Rows)
    assert [r.id for r in page.selected_records()] == ["one"]
    removed = []
    page.removeRequested.connect(removed.append)
    monkeypatch.setattr(QMessageBox, "question", lambda *args: QMessageBox.StandardButton.No)
    page._remove_selected()
    assert removed == []
    monkeypatch.setattr(QMessageBox, "question", lambda *args: QMessageBox.StandardButton.Yes)
    page._remove_selected()
    assert removed == [["one"]]
    assert page.records == rows  # Main-window store callback owns persistence and refresh.
    page.close()


def test_invoice_only_never_enables_accounting_generation(app):
    page = RecordsPage()
    page.set_records([record("invoice", "invoice", {"invoice_no": "001"})])
    assert page.export_button.isEnabled()
    assert not page.generate_button.isEnabled()
    page.close()
