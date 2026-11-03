"""Round-trip and failure-path checks using synthetic, local Excel workbooks."""
import csv
import shutil
from datetime import datetime
from decimal import Decimal

import openpyxl
import pytest
import xlwt

from xiaoyu.models import Entry, GenerationResult, Voucher
from xiaoyu.service import load_inputs
from xiaoyu.spreadsheet_io import (
    export_vouchers,
    read_bank,
    read_payroll,
    read_sheets,
    read_template,
)


D = Decimal
BANK_HEADERS = ["交易时间", "收入金额", "支出金额", "对方户名", "摘要", "账户余额"]
PAY_HEADERS = ["姓名", "部门", "应发工资", "社保个人部分", "公积金个人部分", "个税", "其他扣款", "实发工资"]
# An intentionally reordered template, including an extra user-defined column.
TEMPLATE_HEADERS = ["摘要", "贷方本币", "日期", "科目名称", "科目编码", "自定义列", "凭证号", "借方本币", "行号"]


def write_workbook(path, sheets):
    if path.suffix == ".xls":
        book = xlwt.Workbook()
        date_style = xlwt.easyxf(num_format_str="yyyy-mm-dd hh:mm:ss")
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


def write_bank(path):
    return write_workbook(path, {
        "流水": [
            ["账户明细查询列表"],
            ["账号：1234567890123456"],
            BANK_HEADERS,
            [datetime(2026, 8, 10, 9, 30), "1,234.56", "", "平台甲", "结算款", "2,000.00"],
            ["2026年8月11日", "", 99.95, "供应商甲", "转账", 1900.05],
            ["合计", 1234.56, 99.95],
        ],
        "说明": [["与银行流水无关的工作表"]],
    })


def write_template(path):
    return write_workbook(path, {
        "自定义凭证模板": [TEMPLATE_HEADERS],
        "科目": [["科目名称", "科目编码"], ["银行存款", "1002001"], ["应付账款-供应商甲", "2202001"]],
    })


@pytest.mark.parametrize("extension", [".xls", ".xlsx"])
def test_bank_recognizes_titles_dates_income_expense_and_source_rows(tmp_path, extension):
    source = write_bank(tmp_path / ("流水" + extension))
    transactions, issues = read_bank(source)

    assert issues == []
    assert len(transactions) == 2
    receipt, payment = transactions
    assert receipt.date == datetime(2026, 8, 10, 9, 30)
    assert receipt.income == D("1234.56")
    assert receipt.expense == D("0.00")
    assert receipt.balance == D("2000.00")
    assert receipt.counterparty == "平台甲"
    assert receipt.account_number == "1234567890123456"
    assert receipt.source == f"流水{extension} / 流水 / 第4行"
    assert payment.date == datetime(2026, 8, 11)
    assert payment.income == D("0.00")
    assert payment.expense == D("99.95")


def test_invalid_bank_row_reports_source_without_losing_valid_rows(tmp_path):
    source = write_workbook(tmp_path / "流水.xlsx", {"流水": [
        BANK_HEADERS,
        ["2026-08-10", "不是金额", "", "错误", "文字金额"],
        ["错误日期", 10, "", "错误", "日期错误"],
        ["2026-08-10", "", "NaN", "错误", "非有限数金额"],
        ["2026-08-11", 100, "", "正常", "收款"],
    ]})
    transactions, issues = read_bank(source)

    assert len(transactions) == 1
    assert transactions[0].income == D("100.00")
    assert len(issues) == 3
    assert all(issue.severity == "error" and "未参与生成" in issue.message for issue in issues)
    assert "第2行" in issues[0].source


@pytest.mark.parametrize("extension", [".xls", ".xlsx"])
@pytest.mark.parametrize("formula_column", ["应发工资", "实发工资"])
def test_payroll_formula_without_saved_result_is_reported_not_treated_as_zero(tmp_path, extension, formula_column):
    invalid = ["公式员工", "运营", 5000, 400, 0, 0, 0, 4600]
    invalid[PAY_HEADERS.index(formula_column)] = "=5000-400" if formula_column == "实发工资" else "=2500+2500"
    source = write_workbook(tmp_path / ("工资" + extension), {"2026年8月": [
        PAY_HEADERS, invalid, ["正常员工", "运营", 6000, 500, 0, 0, 0, 5500],
    ]})
    payroll, issues = read_payroll(source)

    assert [row.name for row in payroll] == ["正常员工"]
    assert payroll[0].month == 8
    assert payroll[0].year == 2026
    assert payroll[0].gross == D("6000.00")
    assert payroll[0].net == D("5500.00")
    assert len(issues) == 1
    assert issues[0].severity == "error"
    assert "公式" in issues[0].message and "保存" in issues[0].message
    assert "第2行" in issues[0].source


def test_payroll_deduction_formula_without_cache_cannot_silently_become_zero(tmp_path):
    source = write_workbook(tmp_path / "工资.xlsx", {"2026年8月": [
        PAY_HEADERS, ["员工甲", "运营", 5000, "=200+200", 0, 0, 0, 4600],
    ]})
    payroll, issues = read_payroll(source)

    assert payroll == []
    assert any(issue.severity == "error" and "公式" in issue.message for issue in issues)


def test_payroll_empty_optional_deductions_remain_valid(tmp_path):
    source = write_workbook(tmp_path / "工资.xlsx", {"2026年8月": [
        PAY_HEADERS, ["员工甲", "运营", 5000, None, None, None, None, 5000],
    ]})
    payroll, issues = read_payroll(source)

    assert len(payroll) == 1
    assert payroll[0].social == payroll[0].tax == D("0.00")
    assert issues == []


@pytest.mark.parametrize("template_extension", [".xls", ".xlsx"])
@pytest.mark.parametrize("output_extension", [".xls", ".xlsx"])
def test_export_keeps_template_order_and_amounts_and_does_not_block_missing_account(
        tmp_path, template_extension, output_extension):
    template = read_template(write_template(tmp_path / ("模板" + template_extension)))
    assert template.headers == TEMPLATE_HEADERS
    assert template.accounts["1002001"].name == "银行存款"
    voucher = Voucher("2026-08-10", 7, "付款", [
        Entry("", "待确认科目", debit=D("12345678.91")),
        Entry("1002001", "银行存款", credit=D("12345678.91")),
    ], ["流水.xlsx / 流水 / 第4行"])
    result = GenerationResult([voucher], [], [])
    target = tmp_path / ("导出凭证" + output_extension)

    exported = export_vouchers(result, template, target)

    assert target.is_file()
    assert exported["voucher_count"] == 1
    assert exported["line_count"] == 2
    rows = read_sheets(target)[0][1]
    assert rows[0] == TEMPLATE_HEADERS
    first, second = [dict(zip(rows[0], row)) for row in rows[1:]]
    assert first["科目编码"] in (None, "")
    assert first["科目名称"] == "待确认科目"
    assert first["凭证号"] == second["凭证号"] == "记-7"
    assert D(str(first["借方本币"])) == D("12345678.91")
    assert D(str(second["贷方本币"])) == D("12345678.91")
    assert first["贷方本币"] in (None, "")
    assert second["借方本币"] in (None, "")
    assert first["自定义列"] in (None, "")
    with open(exported["issues_path"], encoding="utf-8-sig", newline="") as file:
        report = list(csv.DictReader(file))
    assert any("缺少科目编码" in row["提示"] and "第4行" in row["来源"] for row in report)


def test_service_ignores_repeated_path_and_identical_copy(tmp_path):
    source = write_bank(tmp_path / "银行流水.xls")
    duplicate = tmp_path / "银行流水副本.xls"
    shutil.copyfile(source, duplicate)
    template = write_template(tmp_path / "模板.xlsx")

    bundle = load_inputs([source, source, duplicate], [], template)

    assert len(bundle.transactions) == 2
    assert bundle.months == ["2026-08"]
    assert bundle.source_counts == {"bank_files": 1, "payroll_files": 0, "transactions": 2, "payroll_rows": 0}
    assert sum("重复文件" in issue.message for issue in bundle.issues) == 2


@pytest.mark.parametrize("extension", [".xls", ".xlsx"])
def test_corrupted_excel_error_is_actionable_chinese(tmp_path, extension):
    source = tmp_path / ("损坏文件" + extension)
    source.write_bytes(b"not an excel workbook")

    with pytest.raises(ValueError, match="无法读取.*文件可能加密或格式损坏"):
        read_sheets(source)


def test_unrecognized_bank_workbook_explains_required_headers_in_chinese(tmp_path):
    source = write_workbook(tmp_path / "不是流水.xlsx", {"Sheet1": [["名称", "金额"], ["甲", 10]]})

    with pytest.raises(ValueError, match="交易时间、收入金额、支出金额"):
        read_bank(source)


def test_service_missing_input_file_has_chinese_error(tmp_path):
    template = write_template(tmp_path / "模板.xlsx")

    with pytest.raises(ValueError, match="找不到文件.*已被移动的流水.xlsx"):
        load_inputs([tmp_path / "已被移动的流水.xlsx"], [], template)


def test_template_uses_reference_accounts_without_mixing_sample_accounts(tmp_path):
    source = write_workbook(tmp_path / "带示例的模板.xls", {
        "科目参考表": [["科目编码", "科目名称"], ["1002001", "银行存款-实际账户"], ["2202001", "应付账款-实际供应商"]],
        "凭证模板": [
            TEMPLATE_HEADERS,
            ["示例收款", "", "2026-08-01", "银行存款-示例账户", "1002001", "", "记-1", 100, 1],
            ["示例收款", 100, "2026-08-01", "应收账款-示例客户", "1122009", "", "记-1", "", 2],
        ],
    })

    template = read_template(source)

    assert set(template.accounts) == {"1002001", "2202001"}
    assert template.accounts["1002001"].name == "银行存款-实际账户"


def test_bank_summary_count_rows_are_not_transactions_or_invalid_row_warnings(tmp_path):
    source = write_workbook(tmp_path / "带汇总的流水.xls", {"流水": [
        BANK_HEADERS,
        ["2026-08-10", 100, "", "平台甲", "收款"],
        ["2026-08-11", "", 20, "供应商甲", "转账"],
        ["总收入笔数", "总支出笔数", "总收入金额", "总支出金额"],
        [1, 1, 100, 20],
        ["统计说明", "收入1笔", "支出1笔"],
    ]})

    transactions, issues = read_bank(source)

    assert len(transactions) == 2
    assert sum(row.income for row in transactions) == D("100.00")
    assert sum(row.expense for row in transactions) == D("20.00")
    assert issues == []
