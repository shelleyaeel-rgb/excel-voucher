from datetime import datetime
from decimal import Decimal

import pytest

from xiaoyu.engine import generate
from xiaoyu.models import Account, Options, PayrollRow, TemplateSpec, Transaction


D = Decimal


@pytest.fixture
def template():
    names = {
        "1002001": "银行存款-示例银行账户一",
        "1122001": "应收账款-示例平台甲有限公司",
        "1122003": "应收账款-示例平台乙有限公司",
        "2202001": "应付账款-供应商甲",
        "2202002": "应付账款-示例平台甲有限公司",
        "2241002": "其他应付款-示例平台甲有限公司",
        "2241003": "其他应付款-示例员工甲",
        "2241004": "其他应付款-社保（个人）",
        "2211": "应付职工薪酬",
        "2221012": "应交税费-应交个人所得税",
        "2221026": "应交税费-应交增值税（小规模纳税人专用）",
        "5001": "主营业务收入",
        "5601017": "销售费用-示例平台平台手续费",
        "5602014": "管理费用-工资",
        "5602015": "管理费用-社保（单位）",
        "5603002": "财务费用-手续费",
    }
    return TemplateSpec([], {code: Account(code, name) for code, name in names.items()})


def tx(source, expense="0", income="0", name="供应商甲", summary="转取", day="2026-08-10"):
    return Transaction(datetime.fromisoformat(day), D(income), D(expense), name, summary, source)


def pay(name="员工甲", gross="5000", social="477.18", net="4522.82", month=8,
        housing="0", tax="0", other="0", source="工资表!3", year=2026):
    return PayrollRow(month, name, "财务", D(gross), D(social), D(housing), D(tax), D(other),
                      D(net), source, year)


def assert_balanced(result):
    assert result.debit == result.credit
    assert all(v.debit == v.credit for v in result.vouchers)
    assert all(e.debit >= 0 and e.credit >= 0 for v in result.vouchers for e in v.entries)


def amount(voucher, code, side):
    return sum((getattr(e, side) for e in voucher.entries if e.account_code == code), D("0"))


def test_consecutive_fees_match_consecutive_payments_fifo(template):
    rows = [tx("r1", "1700"), tx("r2", "15000", name="示例员工甲"),
            tx("r3", "4.50", summary="费用外收"), tx("r4", "9", summary="费用外收")]
    result = generate(rows, [], template, Options(period="2026-08"))
    assert len(result.vouchers) == 2
    assert result.vouchers[0].sources == ["r1", "r3"]
    assert result.vouchers[1].sources == ["r2", "r4"]
    assert amount(result.vouchers[0], "1002001", "credit") == D("1704.50")
    assert amount(result.vouchers[1], "1002001", "credit") == D("15009.00")
    assert_balanced(result)


def test_fees_never_cross_receipts_dates_or_batch_boundary(template):
    rows = [tx("r1", "100", day="2026-08-09"), tx("r2", "4.5", summary="费用外收"),
            tx("r3", "100"), tx("r4", income="50", summary="退款"),
            tx("r5", "4.5", summary="费用外收"), tx("r6", "100"),
            tx("r7", "142.50", summary="批量扣费")]
    result = generate(rows, [], template, Options())
    assert len(result.vouchers) == 7
    assert all(len(v.sources) == 1 for v in result.vouchers)
    assert_balanced(result)


def test_new_payment_block_does_not_reuse_unpaired_old_payments(template):
    rows = [tx("old1", "100"), tx("old2", "200"), tx("fee1", "4.5", summary="费用外收"),
            tx("new", "300"), tx("fee2", "9", summary="费用外收")]
    result = generate(rows, [], template, Options())
    assert result.vouchers[1].sources == ["old2"]
    assert result.vouchers[2].sources == ["new", "fee2"]


def test_unknown_and_ambiguous_accounts_still_export_balanced(template):
    rows = [tx("unknown", "40", name="新户名"),
            tx("ambiguous", "80", name="示例平台甲有限公司")]
    result = generate(rows, [], template, Options())
    assert len(result.vouchers) == 2
    assert all(any(not e.account_code and e.debit for e in v.entries) for v in result.vouchers)
    assert any("多个科目" in i.message for i in result.issues)
    assert_balanced(result)


def test_explicit_mapping_overrides_suffix_and_platform_default(template):
    rows = [tx("r", income="101", name="示例平台甲有限公司")]
    result = generate(rows, [], template, Options(mappings={"示例平台甲有限公司": "2241002"}))
    assert amount(result.vouchers[0], "2241002", "credit") == D("101")
    assert not any(e.account_code == "5001" for e in result.vouchers[0].entries)


@pytest.mark.parametrize("mapped", ["应付职工薪酬", "  应付职工薪酬  ", "2211  应付职工薪酬", "2211"])
def test_existing_name_mapping_resolves_to_actual_code(template, mapped):
    result = generate([tx("bank", "100", name="示例员工乙")], [], template,
                      Options(mappings={"示例员工乙": mapped}))
    assert amount(result.vouchers[0], "2211", "debit") == D("100")
    assert not any("模板缺少科目" in i.message or "未识别" in i.message for i in result.issues)
    assert_balanced(result)


@pytest.mark.parametrize("mapped", ["未知科目", "应付", "2211 管理费用-工资", "9999 应付职工薪酬"])
def test_invalid_explicit_mapping_stays_blank_instead_of_falling_back(template, mapped):
    # The supplier otherwise has an unambiguous 2202001 account.  Invalid
    # explicit input must remain visible rather than be silently overridden.
    result = generate([tx("bank", "100", name="供应商甲")], [], template,
                      Options(mappings={"供应商甲": mapped}))
    assert amount(result.vouchers[0], "", "debit") == D("100")
    assert amount(result.vouchers[0], "2202001", "debit") == 0
    assert any("科目设置未识别" in i.message for i in result.issues)
    assert not any(f"模板缺少科目 {mapped}" in i.message for i in result.issues)
    assert_balanced(result)


def test_duplicate_account_name_mapping_requires_code_and_keeps_balanced(template):
    template.accounts["2211001"] = Account("2211001", "应付职工薪酬")
    result = generate([tx("bank", "100", name="示例员工乙")], [], template,
                      Options(mappings={"示例员工乙": "应付职工薪酬"}))
    assert amount(result.vouchers[0], "", "debit") == D("100")
    assert any("名称不唯一" in i.message for i in result.issues)
    assert not any("模板缺少科目 应付职工薪酬" in i.message for i in result.issues)
    assert_balanced(result)


def test_missing_numeric_account_retains_specific_diagnostic(template):
    result = generate([tx("bank", "100", name="示例员工乙")], [], template,
                      Options(mappings={"示例员工乙": "9999"}))
    assert any("模板缺少科目 9999" in i.message for i in result.issues)
    assert_balanced(result)


def test_name_mapping_fix_does_not_hide_independent_counterparty_issues(template):
    template.accounts["2202005"] = Account("2202005", "应付账款-示例供应商甲酒业有限公司")
    rows = [tx("salary", "100", name="示例员工乙"),
            tx("company", "200", name="示例供应商品牌管理有限公司"), tx("empty", "300", name="")]
    result = generate(rows, [], template, Options(mappings={"示例员工乙": "应付职工薪酬"}))
    assert amount(result.vouchers[0], "2211", "debit") == D("100")
    assert amount(result.vouchers[1], "", "debit") == D("200")
    assert amount(result.vouchers[2], "", "debit") == D("300")
    assert any("示例供应商品牌管理有限公司" in i.message for i in result.issues)
    assert any("未填写" in i.message for i in result.issues)
    assert not any("模板缺少科目 应付职工薪酬" in i.message for i in result.issues)
    assert_balanced(result)


def test_platform_default_is_collection_not_new_income(template):
    rows = [tx("r", income="101", name="示例平台乙有限公司")]
    result = generate(rows, [], template, Options())
    assert amount(result.vouchers[0], "1122003", "credit") == D("101")
    assert not any(e.account_code in ("5001", "2221026") for e in result.vouchers[0].entries)


@pytest.mark.parametrize("fee_mode,fee_value,net", [("rate", "5", "959.50"), ("amount", "50.50", "959.50")])
def test_platform_income_and_settlement_balance_independently(template, fee_mode, fee_value, net):
    rows = [tx("r", income=net, name="示例平台甲有限公司")]
    options = Options(platform_mode="income_and_settlement", platform_fee_mode=fee_mode,
                      platform_fee_value=D(fee_value), tax_rate=D("1"))
    result = generate(rows, [], template, options)
    v = result.vouchers[0]
    assert amount(v, "5001", "credit") == D("1000")
    assert amount(v, "2221026", "credit") == D("10")
    assert amount(v, "5601017", "debit") == D("50.50")
    assert amount(v, "1122001", "debit") == amount(v, "1122001", "credit") == D("1010")
    for group in (v.entries[:3], v.entries[3:]):
        assert sum(e.debit for e in group) == sum(e.credit for e in group)
    assert_balanced(result)


def test_invalid_platform_fee_does_not_invent_sales(template):
    result = generate([tx("r", income="100", name="示例平台甲有限公司")], [], template,
                      Options(platform_mode="income_and_settlement", platform_fee_mode="rate",
                              platform_fee_value=D("100")))
    assert amount(result.vouchers[0], "1122001", "credit") == D("100")
    assert any("设置无效" in i.message for i in result.issues)
    assert_balanced(result)


def test_missing_small_taxpayer_vat_account_does_not_use_input_vat(template):
    del template.accounts["2221026"]
    template.accounts["2221001"] = Account("2221001", "应交税费-应交增值税（进项税额）")
    result = generate([tx("r", income="101", name="示例平台甲有限公司")], [], template,
                      Options(platform_mode="income_and_settlement"))
    assert amount(result.vouchers[0], "", "credit") == D("1")
    assert not any(e.account_code == "2221001" for e in result.vouchers[0].entries)
    assert_balanced(result)


def test_salary_aggregates_same_employee_but_not_other_employees(template):
    payroll = [pay(gross="3225.81", social="0", net="3225.81", source="工资!3"),
               pay(gross="967.74", social="477.18", net="490.56", source="工资!4"),
               pay(name="未付款员工", gross="5000", social="0", net="5000")]
    result = generate([tx("bank", "3716.37", name="员工甲")], payroll, template, Options(period="2026-08"))
    v = result.vouchers[0]
    assert v.summary == "发放工资"
    assert amount(v, "2211", "debit") == D("4193.55")
    assert amount(v, "2241004", "credit") == D("477.18")
    assert amount(v, "1002001", "credit") == D("3716.37")
    assert len(result.vouchers) == 1  # No invented payment for the second employee.
    assert_balanced(result)


def test_salary_deducts_housing_tax_and_other_without_inventing_accounts(template):
    payroll = [pay(gross="5000", social="400", housing="200", tax="50", other="10", net="4340")]
    result = generate([tx("bank", "4340", name="员工甲")], payroll, template, Options())
    v = result.vouchers[0]
    assert amount(v, "", "credit") == D("210")
    assert amount(v, "2221012", "credit") == D("50")
    assert any("公积金" in i.message for i in result.issues)
    assert any("其他扣款" in i.message for i in result.issues)
    assert_balanced(result)


@pytest.mark.parametrize("payroll,paid", [([pay(gross="2666.67", social="0", net="2667")], "2667"),
                                         ([pay()], "4500")])
def test_inconsistent_or_partial_salary_is_not_guessed(template, payroll, paid):
    result = generate([tx("bank", paid, name="员工甲")], payroll, template, Options())
    assert result.vouchers[0].summary != "发放工资"
    assert not any(e.account_code == "2211" for e in result.vouchers[0].entries)
    assert any("未按工资拆分" in i.message for i in result.issues)
    assert_balanced(result)


def test_payroll_accrual_is_optional_and_uses_month_end_and_gross(template):
    payroll = [pay(), pay(month=9, gross="9000", net="8522.82")]
    assert generate([], payroll, template, Options(period="2026-08")).vouchers == []
    result = generate([], payroll, template, Options(period="2026-08", payroll_accrual=True))
    assert result.vouchers[0].date == "2026-08-31"
    assert amount(result.vouchers[0], "5602014", "debit") == D("5000")
    assert amount(result.vouchers[0], "2211", "credit") == D("5000")
    assert_balanced(result)


def test_accrual_does_not_silently_fix_employee_rounding_difference(template):
    result = generate([], [pay(gross="2666.67", social="0", net="2667")], template,
                      Options(period="2026-08", payroll_accrual=True))
    assert amount(result.vouchers[0], "5602014", "debit") == D("2666.67")
    assert any("-0.33" in i.message for i in result.issues)


def test_separate_accrual_month_changes_date_not_payroll_source_month(template):
    payroll = [pay(), pay(month=7, gross="9000", net="8522.82")]
    rows = [tx("bank", "1630.57", summary="公共缴费")]
    result = generate(rows, payroll, template,
                      Options(period="2026-08", payroll_accrual=True, social_accrual=True,
                              accrual_period="2026-07", social_payment_dates=["2026-08-10"]))
    wage = next(v for v in result.vouchers if v.summary == "计提工资")
    social = next(v for v in result.vouchers if v.summary == "计提单位社保")
    payment = next(v for v in result.vouchers if v.summary == "缴纳社保")
    assert wage.date == social.date == "2026-07-31"
    assert payment.date == "2026-08-10"
    assert amount(wage, "5602014", "debit") == D("5000")
    assert amount(social, "5602015", "debit") == D("1153.39")
    assert [v.number for v in result.vouchers] == [1, 2, 3]
    assert result.vouchers[-1] is payment
    assert_balanced(result)


@pytest.mark.parametrize("booking_period", ["2026-13", "2026-7", "wrong"])
def test_invalid_accrual_month_does_not_stop_bank_export(template, booking_period):
    rows = [tx("bank", "1630.57", summary="公共缴费")]
    result = generate(rows, [pay()], template,
                      Options(period="2026-08", payroll_accrual=True, social_accrual=True,
                              accrual_period=booking_period, social_payment_dates=["2026-08-10"]))
    assert len(result.vouchers) == 1
    assert result.vouchers[0].summary == "缴纳社保"
    assert result.transactions == rows
    assert any("计提入账月份无效" in i.message for i in result.issues)
    assert_balanced(result)


def test_social_only_groups_explicitly_selected_date_and_splits_by_payroll(template):
    rows = [tx("a", "1158.01", summary="公共缴费", day="2026-08-03"),
            tx("b", "472.56", summary="公共缴费", day="2026-08-03"),
            tx("c", "1158.01", summary="公共缴费"), tx("d", "472.56", summary="公共缴费")]
    result = generate(rows, [pay()], template, Options(period="2026-08", social_payment_dates=["2026-08-10"],
                                                     social_accrual=True))
    assert len(result.vouchers) == 4  # two unidentified payments, one batch, one accrual
    social = next(v for v in result.vouchers if v.summary == "缴纳社保")
    assert amount(social, "2211", "debit") == D("1153.39")
    assert amount(social, "2241004", "debit") == D("477.18")
    assert amount(social, "1002001", "credit") == D("1630.57")
    accrual = result.vouchers[-1]
    assert accrual.date == "2026-08-31"
    assert amount(accrual, "5602015", "debit") == D("1153.39")
    assert_balanced(result)


def test_multiple_social_dates_report_reused_personal_amount(template):
    rows = [tx("a", "1630.57", summary="公共缴费", day="2026-08-03"),
            tx("b", "1630.57", summary="公共缴费")]
    result = generate(rows, [pay()], template,
                      Options(social_payment_dates=["2026-08-03", "2026-08-10"]))
    assert len(result.vouchers) == 2
    assert all(any("多批" in i.message for i in v.issues) for v in result.vouchers)
    assert_balanced(result)


@pytest.mark.parametrize("payroll", [[], [pay(social="2000", net="3000")]])
def test_unknown_or_negative_unit_social_keeps_payment_without_fake_accrual(template, payroll):
    result = generate([tx("bank", "1000", summary="公共缴费")], payroll, template,
                      Options(social_payment_dates=["2026-08-10"], social_accrual=True))
    assert len(result.vouchers) == 1
    assert amount(result.vouchers[0], "", "debit") == D("1000")
    assert not any(e.account_code == "5602015" for e in result.vouchers[0].entries)
    assert_balanced(result)


def test_period_numbering_and_repeated_refunds_are_preserved(template):
    rows = [tx("later", income="20", summary="退款", day="2026-08-11"),
            tx("earlier1", income="20", summary="退款", day="2026-08-10"),
            tx("earlier2", income="20", summary="退款", day="2026-08-10"),
            tx("july", income="100", day="2026-07-31")]
    result = generate(rows, [], template, Options(period="2026-08", start_number=7))
    assert len(result.transactions) == len(result.vouchers) == 3
    assert [v.number for v in result.vouchers] == [7, 8, 9]
    assert [v.sources[0] for v in result.vouchers] == ["earlier1", "earlier2", "later"]
    assert result.debit == D("60")


def test_missing_bank_account_does_not_block_export(template):
    result = generate([tx("bank", "100")], [], template, Options(bank_account="missing"))
    assert amount(result.vouchers[0], "", "credit") == D("100")
    assert any("missing" in i.message for i in result.issues)
    assert_balanced(result)


def test_invalid_period_cannot_accidentally_export_all_bank_months(template):
    result = generate([tx("bank", "100")], [], template, Options(period="2026-13"))
    assert result.vouchers == []
    assert result.transactions == []
    assert result.issues
