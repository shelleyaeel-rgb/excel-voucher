"""Account selection regressions using real Qt controls and isolated settings."""
import copy
import os
from datetime import datetime
from decimal import Decimal

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
import xlrd
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QCompleter

from xiaoyu import ui
from xiaoyu.models import Account, TemplateSpec, Transaction
from xiaoyu.service import InputBundle, generate_bundle
from xiaoyu.spreadsheet_io import HEADERS


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def make_window(app, monkeypatch, tmp_path):
    windows = []
    saves = []

    def create(config=None):
        monkeypatch.setattr(ui, "load_config", lambda: copy.deepcopy(config or {}))
        monkeypatch.setattr(ui, "save_config", lambda value: saves.append(copy.deepcopy(value)))
        window = ui.MainWindow(material_store=ui.MaterialStore(tmp_path / 'materials.sqlite3'))
        windows.append(window)
        return window, saves

    yield create
    for window in windows:
        window._save_timer.stop()
        window.close()
        window.deleteLater()
    app.processEvents()


@pytest.fixture
def bundle():
    names = {
        "1002001": "银行存款-示例银行账户一",
        "1002002": "银行存款-另一账户",
        "2211": "应付职工薪酬",
        "2241003": "其他应付款-示例员工甲",
        "2241004": "其他应付款-社保（个人）",
        "5602015": "管理费用-社保（单位）",
        "5603002": "财务费用-手续费",
    }
    template = TemplateSpec(HEADERS, {code: Account(code, name) for code, name in names.items()})
    transaction = Transaction(datetime(2026, 8, 10), Decimal("0"), Decimal("100"),
                              "示例员工甲", "转取", "测试银行流水 / 4行")
    return InputBundle(template, [transaction], [], [], ["2026-08"], {})


def attach_bundle(window, bundle, monkeypatch):
    # Exercise the real load callback without starting background work.
    monkeypatch.setattr(window, "_generate", lambda: None)
    window._loaded(bundle)


@pytest.mark.parametrize("text", ["2211", "应付职工薪酬", "2211  应付职工薪酬"])
def test_mapping_forms_resolve_and_save_real_code(make_window, bundle, monkeypatch, text):
    window, saves = make_window()
    attach_bundle(window, bundle, monkeypatch)
    window._add_mapping("示例员工甲", text)
    combo = window.mapping_table.cellWidget(0, 1)
    combo.setEditText(text)
    combo.lineEdit().editingFinished.emit()
    assert combo.currentText() == "2211  应付职工薪酬"
    assert window._read_options().mappings["示例员工甲"] == "2211"
    window._save_settings()
    assert saves[-1]["options"]["mappings"]["示例员工甲"] == "2211"


def test_bank_name_ignores_stale_current_data(make_window, bundle, monkeypatch):
    window, saves = make_window()
    attach_bundle(window, bundle, monkeypatch)
    window.bank_account.setCurrentIndex(window.bank_account.findData("1002001"))
    window.bank_account.setEditText("银行存款-另一账户")
    assert window.bank_account.currentData() == "1002001"
    assert window._read_options().bank_account == "1002002"
    window.bank_account.lineEdit().editingFinished.emit()
    assert window.bank_account.currentText() == "1002002  银行存款-另一账户"
    window._save_settings()
    assert saves[-1]["options"]["bank_account"] == "1002002"


def test_name_edit_does_not_reuse_mapping_selection(make_window, bundle, monkeypatch):
    window, _ = make_window()
    attach_bundle(window, bundle, monkeypatch)
    window._add_mapping("示例员工甲", "2241003")
    combo = window.mapping_table.cellWidget(0, 1)
    combo.setEditText("应付职工薪酬")
    assert combo.currentData() == "2241003"
    assert window._code(combo) == "2211"


def test_saving_normalizes_value_without_rewriting_active_edit(make_window, bundle, monkeypatch):
    window, saves = make_window()
    attach_bundle(window, bundle, monkeypatch)
    window.bank_account.setEditText("银行存款-另一账户")
    window._save_settings()
    assert saves[-1]["options"]["bank_account"] == "1002002"
    assert window.bank_account.currentText() == "银行存款-另一账户"


def test_legacy_names_survive_before_template_then_migrate(make_window, bundle, monkeypatch):
    legacy = {"options": {"bank_account": "银行存款-另一账户",
                           "mappings": {"示例员工甲": "其他应付款-示例员工甲",
                                        "社保": "其他应付款-社保(个人)"}}}
    window, saves = make_window(legacy)
    window._save_settings()
    assert saves[-1]["options"]["bank_account"] == "银行存款-另一账户"
    assert saves[-1]["options"]["mappings"] == legacy["options"]["mappings"]
    attach_bundle(window, bundle, monkeypatch)
    # The load callback itself persists the migration, even if generation waits.
    assert saves[-1]["options"]["bank_account"] == "1002002"
    assert saves[-1]["options"]["mappings"] == {"示例员工甲": "2241003", "社保": "2241004"}
    assert window.mapping_table.cellWidget(1, 1).currentText() == "2241004  其他应付款-社保（个人）"


@pytest.mark.parametrize("text, message", [
    ("2211  其他应付款-示例员工甲", "不一致"),
    ("应付职工", "未找到"),
    ("999999", "未找到"),
    ("", "尚未选择"),
])
def test_invalid_or_incomplete_selection_is_not_guessed(make_window, bundle, monkeypatch, text, message):
    window, saves = make_window()
    attach_bundle(window, bundle, monkeypatch)
    window._add_mapping("示例员工甲", "2241003")
    combo = window.mapping_table.cellWidget(0, 1)
    combo.setEditText(text)
    combo.lineEdit().editingFinished.emit()
    assert combo.currentText() == text
    assert combo.property("accountError") is True
    assert message in combo.toolTip()
    options = window._read_options()
    assert options.mappings["示例员工甲"] == text
    assert any(message in issue.message for issue in window._account_input_issues())
    result = generate_bundle(bundle, options)
    assert any(entry.account_code == "" for voucher in result.vouchers for entry in voucher.entries)
    assert all(entry.account_code != text or not text for voucher in result.vouchers for entry in voucher.entries)
    window._save_settings()
    assert saves[-1]["options"]["mappings"]["示例员工甲"] == text


def test_duplicate_names_require_code_selection(make_window, bundle, monkeypatch):
    bundle.template.accounts["2241009"] = Account("2241009", "其他应付款-示例员工甲")
    window, _ = make_window()
    attach_bundle(window, bundle, monkeypatch)
    window._add_mapping("示例员工甲", "其他应付款-示例员工甲")
    combo = window.mapping_table.cellWidget(0, 1)
    assert "科目名称不唯一" in combo.toolTip()
    assert window._mapping_values()["示例员工甲"] == "其他应付款-示例员工甲"
    combo.setCurrentIndex(combo.findData("2241009"))
    combo.activated.emit(combo.currentIndex())
    assert window._mapping_values()["示例员工甲"] == "2241009"
    assert not combo.property("accountError")


def test_completer_searches_substrings_of_name_and_code(make_window, bundle, monkeypatch):
    window, _ = make_window()
    attach_bundle(window, bundle, monkeypatch)
    window._add_mapping("供应商")
    completer = window.mapping_table.cellWidget(0, 1).completer()
    assert completer.filterMode() == Qt.MatchFlag.MatchContains
    assert completer.completionMode() == QCompleter.CompletionMode.PopupCompletion
    completer.setCompletionPrefix("社保")
    assert completer.completionCount() == 2
    completer.setCompletionPrefix("2241")
    assert completer.completionCount() == 2


def test_account_edit_invalidates_preview_and_export(make_window, bundle, monkeypatch):
    window, _ = make_window()
    attach_bundle(window, bundle, monkeypatch)
    window._generated(generate_bundle(bundle, window._read_options()))
    assert window.export_button.isEnabled()
    window.bank_account.setEditText("银行存款-另一账户")
    assert window.result is None
    assert not window.export_button.isEnabled()


def test_unresolved_account_can_still_export_without_name_in_code(make_window, bundle, monkeypatch, tmp_path):
    window, _ = make_window()
    attach_bundle(window, bundle, monkeypatch)
    window._add_mapping("示例员工甲", "2211  其他应付款-示例员工甲")
    result = generate_bundle(bundle, window._read_options())
    window._generated(result, window._account_input_issues())
    assert window.export_button.isEnabled()
    destination = tmp_path / "待核对凭证.xls"
    monkeypatch.setattr(ui.QFileDialog, "getSaveFileName", lambda *a, **k: (str(destination), ""))
    monkeypatch.setattr(window, "_run", lambda function, callback, message: callback(function()))
    window._export()
    sheet = xlrd.open_workbook(str(destination)).sheet_by_index(0)
    codes = [sheet.cell_value(row, 4) for row in range(1, sheet.nrows)]
    assert "1002001" in codes and "" in codes
    assert "2211  其他应付款-示例员工甲" not in codes
    assert destination.exists()
