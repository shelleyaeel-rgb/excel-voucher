"""Synthetic source data exercises user-confirmed import mappings."""
import json
from dataclasses import asdict
from datetime import datetime
from decimal import Decimal

import openpyxl
import pytest
import xlwt

from xiaoyu.imports import (
    MappingSpec, accounting_rows, header_signature, inspect_workbook,
    parse_workbook, suggest_header, suggest_mapping,
)


def write_book(path, sheets):
    if path.suffix == ".xls":
        book = xlwt.Workbook()
        date_style = xlwt.easyxf(num_format_str="yyyy-mm-dd")
        for name, rows in sheets.items():
            sheet = book.add_sheet(name)
            for r, row in enumerate(rows):
                for c, value in enumerate(row):
                    if isinstance(value, datetime):
                        sheet.write(r, c, value, date_style)
                    elif isinstance(value, str) and value.startswith("="):
                        sheet.write(r, c, xlwt.Formula(value[1:]))
                    else:
                        sheet.write(r, c, value)
        book.save(str(path))
    else:
        book = openpyxl.Workbook()
        book.remove(book.active)
        for name, rows in sheets.items():
            sheet = book.create_sheet(name)
            for row in rows:
                sheet.append(row)
        book.save(path)
        book.close()
    return path


def spec_for(workbook, kind, sheet=None, defaults=None):
    sheet = sheet or workbook.sheets[0][0]
    rows = dict(workbook.sheets)[sheet]
    header = suggest_header(rows, kind)
    return MappingSpec(kind, sheet, header, suggest_mapping(rows[header], kind), defaults or {})


@pytest.mark.parametrize("suffix", [".xls", ".xlsx"])
def test_bank_aliases_reordered_columns_and_source_are_preserved(tmp_path, suffix):
    source = inspect_workbook(write_book(tmp_path / ("银行" + suffix), {"明细": [
        ["银行收支明细"], ["账号：001234"],
        ["对方名称", "存入金额", "日期", "支取金额", "余额", "用途"],
        ["客户甲", "1,200.50", datetime(2026, 10, 1), None, 1200.50, "收款"],
        ["供应商乙", None, "20261002", 20, 1180.50, "付款"],
        ["合计", 1200.50, None, 20],
    ]}))
    spec = spec_for(source, "bank")
    assert spec.header_row == 2
    result = parse_workbook(source, [spec])
    transactions, payroll, issues = accounting_rows(result.records)
    assert len(transactions) == len(result.records) == 2
    assert transactions[0].income == Decimal("1200.50")
    assert transactions[1].expense == Decimal("20.00")
    assert transactions[0].account_number == "001234"
    assert result.records[0].row == 4
    assert result.records[0].source_digest == source.digest
    assert "第4行" in result.records[0].source
    assert result.records[0].values["expense"] == ""
    assert payroll == issues == []
    json.dumps(asdict(result), ensure_ascii=False, allow_nan=False)


def test_single_amount_requires_direction_and_never_guesses_from_sign(tmp_path):
    source = inspect_workbook(write_book(tmp_path / "单金额.xlsx", {"流水": [
        ["交易日期", "金额", "收支", "交易对方"],
        ["2026-10-01", 100, "收入", "甲"],
        ["2026-10-02", 50, "付款", "乙"],
        ["2026-10-03", -40, "", "丙"],
    ]}))
    result = parse_workbook(source, [spec_for(source, "bank")])
    assert len(result.records) == 3
    assert not result.records[2].valid
    assert any("收支方向" in issue.message for issue in result.records[2].issues)
    tx, _, _ = accounting_rows(result.records)
    assert [(v.income, v.expense) for v in tx] == [(Decimal("100"), Decimal("0")), (Decimal("0"), Decimal("50"))]


def test_bank_invalid_date_and_amount_remain_reviewable(tmp_path):
    source = inspect_workbook(write_book(tmp_path / "错误.xlsx", {"流水": [
        ["日期", "收入金额", "支出金额"],
        ["日期不明", 100, None], ["2026-10-01", "不是金额", None],
        ["2026-10-02", None, None], ["2026-10-03", 0, 0],
    ]}))
    result = parse_workbook(source, [spec_for(source, "bank")])
    assert len(result.records) == 4
    assert [r.valid for r in result.records] == [False, False, False, True]
    assert result.records[1].values["income"] == "不是金额"
    assert accounting_rows(result.records)[0] == []


def test_manual_mapping_overrides_unrecognized_headers_and_id_is_stable(tmp_path):
    source = inspect_workbook(write_book(tmp_path / "自定义.xlsx", {"表1": [
        ["D", "A", "B"], ["2026-10-01", 100, 200],
    ]}))
    a = MappingSpec("bank", "表1", 0, {"date": 0, "income": 1})
    b = MappingSpec("bank", "表1", 0, {"date": 0, "income": 2})
    first = parse_workbook(source, [a]).records[0]
    corrected = parse_workbook(source, [b]).records[0]
    assert first.id == corrected.id
    assert first.source_digest == corrected.source_digest == source.digest
    assert first.values["income"] == "100.00"
    assert corrected.values["income"] == "200.00"


@pytest.mark.parametrize("suffix", [".xls", ".xlsx"])
def test_payroll_default_period_and_flexible_columns(tmp_path, suffix):
    source = inspect_workbook(write_book(tmp_path / ("工资" + suffix), {"员工薪资": [
        ["员工姓名", "应发合计", "个人社保", "实际发放"],
        ["员工甲", 5000, 400, 4600], ["合计", 5000, 400, 4600],
    ]}))
    result = parse_workbook(source, [spec_for(source, "payroll", defaults={"period": "2026-10"})])
    tx, rows, issues = accounting_rows(result.records)
    assert tx == issues == []
    assert len(rows) == 1
    assert (rows[0].year, rows[0].month, rows[0].gross, rows[0].social, rows[0].net) == (2026, 10, Decimal("5000"), Decimal("400"), Decimal("4600"))


def test_payroll_infers_only_complete_year_month_and_keeps_formula_errors(tmp_path):
    source = inspect_workbook(write_book(tmp_path / "工资.xlsx", {
        "2026年10月工资": [["姓名", "应发工资", "实发工资"], ["甲", 100, 100]],
        "10月工资": [["姓名", "应发工资", "实发工资"], ["乙", 100, 100]],
        "公式": [["姓名", "应发工资", "实发工资"], ["丙", "=50+50", 100]],
    }))
    result = parse_workbook(source, [
        spec_for(source, "payroll", "2026年10月工资"),
        spec_for(source, "payroll", "10月工资"),
        spec_for(source, "payroll", "公式", {"period": "2026-10"}),
    ])
    assert [r.valid for r in result.records] == [True, False, False]
    assert any("公式" in issue.message for issue in result.records[2].issues)
    assert [p.name for p in accounting_rows(result.records)[1]] == ["甲"]


def test_invoice_details_keep_leading_zeroes_and_do_not_sum_repeated_total(tmp_path):
    source = inspect_workbook(write_book(tmp_path / "发票.xlsx", {"发票明细": [
        ["发票号", "开票时间", "品名", "金额", "税额", "含税金额", "状态"],
        ["0012345678901234567890", "2026-10-01", "商品甲", 100, 13, 339, "正常"],
        ["0012345678901234567890", "2026-10-01", "商品乙", 200, 26, 339, "正常"],
    ]}))
    result = parse_workbook(source, [spec_for(source, "invoice", defaults={"direction": "收到"})])
    assert len(result.records) == 2
    assert all(r.valid for r in result.records)
    assert all(r.values["invoice_no"] == "0012345678901234567890" for r in result.records)
    assert not any("重复" in issue.message for issue in result.issues)
    assert accounting_rows(result.records) == ([], [], [])


def test_same_workbook_multiple_types_and_identical_business_rows_are_retained(tmp_path):
    source = inspect_workbook(write_book(tmp_path / "综合.xlsx", {
        "运输": [["快递单编号", "运费合计", "寄件时间"], ["0001", 10, "2026-10-01"], ["0001", 10, "2026-10-01"]],
        "收款": [["日期", "收入金额"], ["2026-10-01", 100]],
    }))
    specs = [spec_for(source, "logistics", "运输", {"subtype": "运输与运费"}), spec_for(source, "bank", "收款")]
    result = parse_workbook(source, specs)
    assert len(result.records) == 3
    assert result.records[0].values["tracking_no"] == "0001"
    assert any("可能重复" in issue.message for issue in result.records[1].issues)
    assert len(accounting_rows(result.records)[0]) == 1
    assert len(parse_workbook(source, specs + specs).records) == 3


def test_long_numeric_invoice_identifier_warns_about_excel_precision(tmp_path):
    source = inspect_workbook(write_book(tmp_path / "长号.xlsx", {"发票": [
        ["发票号码", "价税合计"], [12345678901234567890, 100],
    ]}))
    result = parse_workbook(source, [spec_for(source, "invoice")])
    assert any("精度" in issue.message for issue in result.records[0].issues)


def test_duplicate_header_alias_does_not_make_a_silent_column_choice():
    assert "amount" not in suggest_mapping(["金额", "金额"], "invoice")
    assert header_signature([" 发票号码 ", "金额（不含税）"]) == header_signature(["发票号码", "金额(不含税)"])


def test_invalid_mapping_has_actionable_error(tmp_path):
    source = inspect_workbook(write_book(tmp_path / "流水.xlsx", {"表": [["日期", "金额"], ["2026-10-01", 100]]}))
    with pytest.raises(ValueError, match="同一列"):
        parse_workbook(source, [MappingSpec("bank", "表", 0, {"date": 0, "income": 1, "expense": 1})])
    with pytest.raises(ValueError, match="列对应关系"):
        parse_workbook(source, [MappingSpec("bank", "表", 0, {"date": 10})])


def test_bank_suggestion_prefers_separate_income_and_expense_columns(tmp_path):
    source = inspect_workbook(write_book(tmp_path / "多金额列.xlsx", {"流水": [
        ["日期", "收入", "支出", "交易金额", "收支"],
        ["2026-10-01", None, 100, 100, "支出"],
    ]}))
    spec = spec_for(source, "bank")
    assert "amount" not in spec.mapping
    result = parse_workbook(source, [spec])
    assert result.records[0].valid
    assert accounting_rows(result.records)[0][0].expense == Decimal("100")


@pytest.mark.parametrize("sign_mode, valid, expected", [("", False, None), ("absolute", True, Decimal("100")), ("signed", True, Decimal("-100"))])
def test_single_negative_amount_needs_explicit_sign_convention(tmp_path, sign_mode, valid, expected):
    source = inspect_workbook(write_book(tmp_path / "负数.xlsx", {"流水": [
        ["日期", "金额", "收支"], ["2026-10-01", -100, "支出"],
    ]}))
    result = parse_workbook(source, [spec_for(source, "bank", defaults={"amount_sign": sign_mode})])
    assert result.records[0].valid is valid
    assert result.records[0].raw[1] == -100
    transactions = accounting_rows(result.records)[0]
    if valid:
        assert transactions[0].expense == expected
    else:
        assert transactions == []
        assert any("负数" in issue.message for issue in result.issues)


def test_payroll_final_total_compares_details_without_counting_subtotals(tmp_path):
    source = inspect_workbook(write_book(tmp_path / "合计.xlsx", {"2026年10月": [
        ["姓名", "应发合计", "实发合计"],
        ["甲", 100, 100], ["小计", 100, 100],
        ["乙", 200, 200], ["小计", 200, 200], ["合计", 400, 300],
    ]}))
    result = parse_workbook(source, [spec_for(source, "payroll")])
    assert len(result.records) == 2
    warnings = [i for i in result.issues if "表内合计" in i.message]
    assert len(warnings) == 1
    assert "应发工资" in warnings[0].message and "相差 -100.00" in warnings[0].message
    assert "第6行" in warnings[0].source


@pytest.mark.parametrize("suffix", [".xls", ".xlsx"])
def test_payroll_optional_formula_without_result_is_not_zero(tmp_path, suffix):
    source = inspect_workbook(write_book(tmp_path / ("扣款" + suffix), {"2026年10月": [
        ["姓名", "应发工资", "个人社保", "实发工资"],
        ["甲", 100, "=10+10", 80], ["乙", 100, None, 100],
    ]}))
    result = parse_workbook(source, [spec_for(source, "payroll")])
    assert [r.valid for r in result.records] == [False, True]
    assert any("公式" in i.message for i in result.records[0].issues)
    assert [p.name for p in accounting_rows(result.records)[1]] == ["乙"]
