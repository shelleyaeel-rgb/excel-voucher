"""Build balanced voucher drafts from bank evidence and explicit accounting rules.

Unresolved accounts remain blank.  Missing evidence is reported, never replaced
with an invented account, a payment, or an unexplained rounding adjustment.
"""

from __future__ import annotations

import calendar
import re
from collections import Counter, defaultdict, deque
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation

from .accounts import resolve_account_input
from .models import (
    Account, Entry, GenerationResult, Issue, Options, PayrollRow, TemplateSpec,
    Transaction, Voucher, ZERO, money,
)


PLATFORMS = {
    "示例平台甲有限公司": "1122001",
    "示例平台乙有限公司": "1122003",
}


def _text(value: str) -> str:
    return re.sub(r"\s+", "", value or "")


def _suffix(value: str) -> str:
    return _text(re.split(r"[-－—/>＞]", value or "")[-1])


def _month_end(year: int, month: int) -> str:
    return f"{year:04d}-{month:02d}-{calendar.monthrange(year, month)[1]:02d}"


@dataclass
class _Pay:
    name: str
    rows: list[PayrollRow]
    gross: Decimal
    social: Decimal
    housing: Decimal
    tax: Decimal
    other: Decimal
    net: Decimal

    @property
    def difference(self) -> Decimal:
        return self.gross - self.social - self.housing - self.tax - self.other - self.net

    @property
    def nonnegative(self) -> bool:
        return all(getattr(self, field) >= ZERO for field in
                   ("gross", "social", "housing", "tax", "other", "net"))


class _Builder:
    def __init__(self, template: TemplateSpec, options: Options, payroll: list[PayrollRow]):
        self.template = template
        self.options = options
        self.payroll = payroll
        self.accounts = {str(a.code): a for a in template.accounts.values()}
        self.issues: list[Issue] = []
        self.vouchers: list[Voucher] = []
        self._pay_cache: dict[tuple[int, int], dict[str, _Pay]] = {}
        self._paid: Counter = Counter()

    def issue(self, message: str, source: str = "", voucher: Voucher | None = None,
              severity: str = "warning") -> None:
        issue = Issue(severity, message, source)
        if issue not in self.issues:
            self.issues.append(issue)
        if voucher is not None and issue not in voucher.issues:
            voucher.issues.append(issue)

    def new(self, date: str, summary: str, sources: list[str]) -> Voucher:
        voucher = Voucher(date, 0, summary, [], list(dict.fromkeys(sources)))
        self.vouchers.append(voucher)
        return voucher

    def account(self, code: str, label: str, voucher: Voucher) -> Account:
        value = str(code).strip()
        # Preserve the diagnostic for a genuinely missing numerical code.
        # Legacy settings may instead contain a full account name; resolve it
        # before deciding the chart is missing an account.
        if value.isdigit() and value not in self.accounts:
            self.issue(f"模板缺少科目 {value}（{label}），对应分录保留空科目。",
                       "; ".join(voucher.sources), voucher)
            return Account("", "")
        resolution = resolve_account_input(value, self.accounts)
        if resolution.account is None:
            self.issue(f"科目设置未识别（{label}）：{resolution.message}对应分录保留空科目。",
                       "; ".join(voucher.sources), voucher)
            return Account("", "")
        return resolution.account

    def named_account(self, label: str, keywords: tuple[str, ...], voucher: Voucher) -> Account:
        candidates = [a for a in self.accounts.values()
                      if any(word in a.name for word in keywords)]
        if len(candidates) == 1:
            return candidates[0]
        self.issue(f"{label}没有唯一可用的科目，请补充科目对应关系；分录已保留。",
                   "; ".join(voucher.sources), voucher)
        return Account("", "")

    def add(self, voucher: Voucher, account: Account, debit=ZERO, credit=ZERO,
            summary: str | None = None) -> None:
        debit, credit = money(debit), money(credit)
        # A reversal changes the side, not the sign of the exported amount.
        if debit < ZERO:
            credit -= debit
            debit = ZERO
        if credit < ZERO:
            debit -= credit
            credit = ZERO
        if debit != ZERO or credit != ZERO:
            voucher.entries.append(Entry(account.code, account.name, debit, credit,
                                         summary if summary is not None else voucher.summary))

    def counterpart(self, tx: Transaction, voucher: Voucher, incoming: bool) -> Account:
        mapped = self.options.mappings.get(tx.counterparty)
        if mapped is not None:
            return self.account(mapped, tx.counterparty, voucher)
        name = _text(tx.counterparty)
        if incoming and name in PLATFORMS:
            return self.account(PLATFORMS[name], tx.counterparty, voucher)
        matches = [a for a in self.accounts.values()
                   if name and (_suffix(a.name) == name or _text(a.name) == name)]
        if len(matches) == 1:
            return matches[0]
        if matches:
            self.issue(f"户名“{tx.counterparty}”对应多个科目（"
                       + "、".join(a.code for a in matches)
                       + "），无法判断本笔用途，已保留空科目。", tx.source, voucher)
        else:
            self.issue(f"户名“{tx.counterparty or '未填写'}”尚未匹配会计科目，已保留空科目。",
                       tx.source, voucher)
        return Account("", "")

    def pays(self, year: int, month: int) -> dict[str, _Pay]:
        key = (year, month)
        if key not in self._pay_cache:
            groups: dict[str, list[PayrollRow]] = defaultdict(list)
            for row in self.payroll:
                if row.month == month and row.year in (None, year) and _text(row.name):
                    groups[_text(row.name)].append(row)
            result = {}
            for name, rows in groups.items():
                values = {field: money(sum((getattr(r, field) for r in rows), ZERO))
                          for field in ("gross", "social", "housing", "tax", "other", "net")}
                result[name] = _Pay(rows[0].name, rows, **values)
            self._pay_cache[key] = result
        return self._pay_cache[key]

    def pay_issues(self, pay: _Pay, voucher: Voucher) -> None:
        source = "; ".join(r.source for r in pay.rows)
        if pay.difference != ZERO:
            self.issue(f"{pay.name}应发减扣款与实发相差 {pay.difference:.2f} 元，未自动调整。",
                       source, voucher, "error")
        if not pay.nonnegative:
            self.issue(f"{pay.name}工资或扣款存在负数，需要核对调整依据。", source, voucher, "error")
        if any(row.year is None for row in pay.rows):
            self.issue("工资表未明确年份，已按所选银行月份匹配，请核对工资所属期。", source, voucher)

    def salary(self, tx: Transaction, voucher: Voucher) -> bool:
        pay = self.pays(tx.date.year, tx.date.month).get(_text(tx.counterparty))
        if pay is None or money(tx.income) != ZERO or money(tx.expense) <= ZERO:
            return False
        self.pay_issues(pay, voucher)
        if pay.net != money(tx.expense) or pay.difference != ZERO or not pay.nonnegative:
            self.issue(f"{tx.counterparty}付款 {money(tx.expense):.2f} 元未完整匹配本月工资"
                       f"（表内实发 {pay.net:.2f} 元），未按工资拆分。", tx.source, voucher)
            return False
        voucher.summary = "发放工资"
        voucher.sources.extend(r.source for r in pay.rows if r.source not in voucher.sources)
        self.add(voucher, self.account("2211", "应付职工薪酬", voucher), debit=pay.gross)
        self.add(voucher, self.account(self.options.bank_account, "银行存款", voucher), credit=pay.net)
        if pay.social:
            self.add(voucher, self.account("2241004", "社保个人部分", voucher), credit=pay.social)
        if pay.housing:
            self.add(voucher, self.named_account("公积金个人部分", ("公积金（个人）", "公积金(个人)",
                                                                      "个人公积金", "公积金个人"), voucher),
                     credit=pay.housing)
        if pay.tax:
            self.add(voucher, self.account("2221012", "应交个人所得税", voucher), credit=pay.tax)
        if pay.other:
            self.add(voucher, self.named_account("工资其他扣款", ("工资其他扣款", "其他工资扣款"), voucher),
                     credit=pay.other)
        key = (tx.date.year, tx.date.month, _text(tx.counterparty))
        self._paid[key] += 1
        if self._paid[key] > 1:
            self.issue("同一员工本月工资已匹配另一笔付款，请核对是否重复发放；银行流水均已保留。",
                       tx.source, voucher)
        return True

    def platform(self, tx: Transaction, voucher: Voucher, receivable: Account) -> bool:
        if self.options.platform_mode != "income_and_settlement" or _text(tx.counterparty) not in PLATFORMS:
            return False
        if money(tx.income) <= ZERO or money(tx.expense) != ZERO:
            return False
        try:
            fee_value = Decimal(str(self.options.platform_fee_value))
            tax_rate = Decimal(str(self.options.tax_rate)) / Decimal(100)
            if not fee_value.is_finite() or not tax_rate.is_finite() or fee_value < ZERO or tax_rate < ZERO:
                raise ValueError
            net = money(tx.income)
            if self.options.platform_fee_mode == "rate":
                rate = fee_value / Decimal(100)
                if rate >= 1:
                    raise ValueError
                gross = money(net / (1 - rate))
                fee = gross - net
            elif self.options.platform_fee_mode == "amount":
                fee = money(fee_value)
                gross = net + fee
            elif self.options.platform_fee_mode == "none":
                gross, fee = net, ZERO
            else:
                raise ValueError
            revenue = money(gross / (1 + tax_rate))
            tax = gross - revenue
        except (ValueError, InvalidOperation, ArithmeticError):
            self.issue("平台手续费或税率设置无效，仅生成已有收款的往来结算，未推算收入。",
                       tx.source, voucher, "error")
            return False
        voucher.summary = "平台收入及结算"
        self.add(voucher, receivable, debit=gross, summary="确认平台收入")
        self.add(voucher, self.account("5001", "主营业务收入", voucher), credit=revenue, summary="确认平台收入")
        if tax:
            # This chart explicitly uses the small-taxpayer VAT account.  A
            # vaguely similar input-VAT account must never be substituted.
            tax_account = self.account("2221026", "应交增值税（小规模纳税人专用）", voucher)
            self.add(voucher, tax_account, credit=tax, summary="确认平台收入")
        self.add(voucher, self.account(self.options.bank_account, "银行存款", voucher), debit=net,
                 summary="平台结算")
        if fee:
            self.add(voucher, self.account("5601017", "平台手续费", voucher), debit=fee, summary="平台结算手续费")
        self.add(voucher, receivable, credit=gross, summary="平台结算")
        return True

    def ordinary(self, tx: Transaction) -> Voucher:
        voucher = self.new(tx.date.strftime("%Y-%m-%d"), tx.summary or "银行收支", [tx.source])
        if self.salary(tx, voucher):
            return voucher
        bank = self.account(self.options.bank_account, "银行存款", voucher)
        income, expense = money(tx.income), money(tx.expense)
        if income and expense:
            self.issue("同一条流水同时有收入和支出，已分别保留两侧金额，请核对。", tx.source, voucher)
        if income < ZERO or expense < ZERO:
            self.issue("流水包含负数，已按冲回方向保留，请核对原始记录。", tx.source, voucher)
        if income:
            account = self.counterpart(tx, voucher, incoming=True)
            if expense == ZERO and self.platform(tx, voucher, account):
                return voucher
            self.add(voucher, bank, debit=income)
            self.add(voucher, account, credit=income)
        if expense:
            account = self.counterpart(tx, voucher, incoming=False)
            self.add(voucher, bank, credit=expense)
            self.add(voucher, account, debit=expense)
        return voucher

    def fee(self, tx: Transaction, voucher: Voucher | None = None) -> Voucher:
        if voucher is None:
            voucher = self.new(tx.date.strftime("%Y-%m-%d"), "银行手续费", [tx.source])
        elif tx.source not in voucher.sources:
            voucher.sources.append(tx.source)
        self.add(voucher, self.account("5603002", "财务费用—手续费", voucher), debit=tx.expense, summary="手续费")
        self.add(voucher, self.account(self.options.bank_account, "银行存款", voucher), credit=tx.expense, summary="手续费")
        return voucher

    def social(self, rows: list[Transaction], batch_count: int) -> tuple[Voucher, Decimal | None]:
        tx = rows[0]
        total = money(sum((r.expense for r in rows), ZERO))
        voucher = self.new(tx.date.strftime("%Y-%m-%d"), "缴纳社保", [r.source for r in rows])
        pays = self.pays(tx.date.year, tx.date.month)
        personal = money(sum((pay.social for pay in pays.values()), ZERO))
        if batch_count > 1:
            self.issue("本月选择了多批社保缴费，各批均引用当月工资个人社保，请核对各批所属月份，避免重复扣除。",
                       "; ".join(voucher.sources), voucher)
        if not pays or personal < ZERO or personal > total:
            reason = "没有对应月份的工资个人社保数据" if not pays else "个人社保超过缴费总额或为负数"
            self.issue(f"{reason}，无法拆分单位与个人部分；已保留银行付款及空科目分录。",
                       "; ".join(voucher.sources), voucher, "error")
            self.add(voucher, Account("", ""), debit=total)
            self.add(voucher, self.account(self.options.bank_account, "银行存款", voucher), credit=total)
            return voucher, None
        unit = total - personal
        for pay in pays.values():
            self.pay_issues(pay, voucher)
            voucher.sources.extend(r.source for r in pay.rows if r.source not in voucher.sources)
        if unit:
            self.add(voucher, self.account("2211", "应付职工薪酬", voucher), debit=unit)
        if personal:
            self.add(voucher, self.account("2241004", "社保个人部分", voucher), debit=personal)
        self.add(voucher, self.account(self.options.bank_account, "银行存款", voucher), credit=total)
        return voucher, unit

    def accrue_payroll(self, year: int, month: int,
                       booking_period: tuple[int, int] | None = None) -> None:
        pays = self.pays(year, month)
        if not pays:
            self.issue("所选月份没有工资数据，未生成工资计提凭证。")
            return
        gross = money(sum((p.gross for p in pays.values()), ZERO))
        if gross <= ZERO or any(not p.nonnegative for p in pays.values()):
            self.issue("工资计提金额非正数或工资明细存在负数，未猜测计提金额。", severity="error")
            return
        voucher = self.new(_month_end(*(booking_period or (year, month))), "计提工资",
                           [r.source for p in pays.values() for r in p.rows])
        for pay in pays.values():
            self.pay_issues(pay, voucher)
        self.add(voucher, self.account("5602014", "管理费用—工资", voucher), debit=gross)
        self.add(voucher, self.account("2211", "应付职工薪酬", voucher), credit=gross)


def generate(transactions: list[Transaction], payroll: list[PayrollRow],
             template: TemplateSpec, options: Options) -> GenerationResult:
    """Generate drafts without changing inputs or discarding repeated transfers.

    Both tax_rate and platform_fee_value in rate mode use percentage points:
    Decimal('1') means 1 percent.  Bank source order controls fee pairing;
    final date order controls voucher numbering.
    """
    builder = _Builder(template, options, payroll)
    period: tuple[int, int] | None = None
    if options.period:
        try:
            if not re.fullmatch(r"\d{4}-\d{2}", options.period):
                raise ValueError
            date = datetime.strptime(options.period, "%Y-%m")
            period = (date.year, date.month)
        except ValueError:
            builder.issue("月份格式无效，请使用 YYYY-MM；未扩大导出到其他月份。", severity="error")
            return GenerationResult([], builder.issues, [])
    selected = [tx for tx in transactions
                if period is None or (tx.date.year, tx.date.month) == period]
    accrual_period: tuple[int, int] | None = None
    accrual_allowed = True
    if options.accrual_period:
        try:
            if not re.fullmatch(r"\d{4}-\d{2}", options.accrual_period):
                raise ValueError
            booking_date = datetime.strptime(options.accrual_period, "%Y-%m")
            accrual_period = (booking_date.year, booking_date.month)
        except ValueError:
            accrual_allowed = False
            builder.issue("计提入账月份无效，请使用 YYYY-MM；未生成计提凭证，银行收支仍正常生成。",
                          severity="error")
    social_groups: dict[str, list[tuple[int, Transaction]]] = defaultdict(list)
    for index, tx in enumerate(selected):
        day = tx.date.strftime("%Y-%m-%d")
        if (day in options.social_payment_dates and "公共缴费" in tx.summary
                and tx.expense > ZERO and tx.income == ZERO):
            social_groups[day].append((index, tx))
    for day in dict.fromkeys(options.social_payment_dates):
        if day not in social_groups:
            builder.issue(f"所选社保日期 {day} 没有本次范围内的公共缴费支出。")
    social_index = {index: day for day, group in social_groups.items() for index, _ in group}
    social_counts = Counter(day[:7] for day in social_groups)
    social_accruals: dict[tuple[int, int], list[tuple[Decimal, Voucher]]] = defaultdict(list)
    queue: deque[Voucher] = deque()
    last_day, last_kind = "", ""
    for index, tx in enumerate(selected):
        day = tx.date.strftime("%Y-%m-%d")
        if day != last_day:
            queue.clear()
            last_kind = ""
            last_day = day
        if index in social_index:
            queue.clear()
            last_kind = "social"
            group = social_groups[day]
            if index == group[0][0]:
                voucher, unit = builder.social([r for _, r in group], social_counts[day[:7]])
                if unit is not None:
                    social_accruals[(tx.date.year, tx.date.month)].append((unit, voucher))
            continue
        if money(tx.income) == ZERO and money(tx.expense) == ZERO:
            builder.issue("流水收入和支出均为零，未生成零金额凭证。", tx.source)
            queue.clear()
            last_kind = "other"
            continue
        is_fee = (tx.expense > ZERO and tx.income == ZERO
                  and any(word in tx.summary for word in ("费用外收", "手续费", "批量扣费")))
        if is_fee:
            if "批量" in tx.summary:
                builder.fee(tx)
                queue.clear()
                last_kind = "other"
            else:
                parent = queue.popleft() if queue and last_kind in ("payment", "fee") else None
                builder.fee(tx, parent)
                last_kind = "fee"
            continue
        voucher = builder.ordinary(tx)
        if tx.expense > ZERO and tx.income == ZERO and "公共缴费" not in tx.summary:
            if last_kind != "payment":
                queue.clear()
            queue.append(voucher)
            last_kind = "payment"
        else:
            queue.clear()
            last_kind = "other"
    if options.payroll_accrual and accrual_allowed:
        if period is None:
            builder.issue("请先选择工资所属月份，未自动生成跨月工资计提。")
        else:
            builder.accrue_payroll(*period, booking_period=accrual_period)
    if options.social_accrual and accrual_allowed:
        if not social_accruals:
            builder.issue("没有可确认单位部分的社保缴费批次，未生成单位社保计提。")
        for (year, month), batches in social_accruals.items():
            unit = money(sum((amount for amount, _ in batches), ZERO))
            if unit == ZERO:
                continue
            voucher = builder.new(_month_end(*(accrual_period or (year, month))), "计提单位社保",
                                  [s for _, original in batches for s in original.sources])
            for _, original in batches:
                for issue in original.issues:
                    builder.issue(issue.message, issue.source, voucher, issue.severity)
            builder.add(voucher, builder.account("5602015", "管理费用—社保（单位）", voucher), debit=unit)
            builder.add(voucher, builder.account("2211", "应付职工薪酬", voucher), credit=unit)
    builder.vouchers.sort(key=lambda voucher: voucher.date)
    for number, voucher in enumerate(builder.vouchers, start=options.start_number):
        voucher.number = number
        if voucher.debit != voucher.credit:
            builder.issue(f"凭证借贷相差 {voucher.debit - voucher.credit:.2f} 元，未自动平差。",
                          "; ".join(voucher.sources), voucher, "error")
    return GenerationResult(builder.vouchers, builder.issues, selected)
