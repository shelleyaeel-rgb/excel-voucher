"""Confirmed column mappings for local, heterogeneous business workbooks.

Reading a workbook never generates a voucher. Every non-summary source row is
retained, including invalid rows; only valid bank/payroll rows reach the engine.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

import xlrd

from .models import Issue, PayrollRow, Transaction, ZERO, money
from .spreadsheet_io import _date, read_sheets, text


@dataclass(frozen=True)
class FieldSpec:
    key: str
    label: str
    aliases: tuple[str, ...] = ()
    required: bool = False


def _f(key, label, aliases=(), required=False):
    return FieldSpec(key, label, tuple(aliases), required)


KIND_LABELS = {"bank": "银行流水", "payroll": "工资表", "logistics": "物流资料", "invoice": "发票资料"}
SCHEMAS = {
    "bank": [
        _f("date", "交易日期", ("交易时间", "日期", "记账日期", "入账日期", "交易日", "发生日期"), True),
        _f("income", "收入金额", ("收入", "收款金额", "存入金额", "收入(元)", "收款")),
        _f("expense", "支出金额", ("支出", "付款金额", "支取金额", "支出(元)", "付款")),
        _f("amount", "交易金额", ("金额", "发生额", "收支金额", "交易金额(元)")),
        _f("direction", "收支方向", ("收支", "收付标志", "收支标志", "交易方向", "收/支")),
        _f("counterparty", "对方户名", ("交易对方", "对方名称", "对方账户名称", "对方单位", "客户名称")),
        _f("summary", "摘要", ("交易摘要", "用途", "备注", "交易用途", "说明")),
        _f("balance", "账户余额", ("余额", "交易后余额", "可用余额")),
        _f("account_number", "银行账号", ("账号", "本方账号", "账户号码", "本方账户")),
        _f("business_no", "业务单号", ("订单号", "业务编号", "交易流水号", "流水号")),
    ],
    "payroll": [
        _f("period", "工资所属月份", ("工资月份", "所属月份", "月份", "工资年月", "所属年月", "年月")),
        _f("name", "姓名", ("员工姓名", "职工姓名", "人员姓名", "员工", "职员姓名"), True),
        _f("department", "部门", ("所属部门", "部门名称")),
        _f("gross", "应发工资", ("应发", "应发合计", "应发金额", "应发工资合计", "工资总额"), True),
        _f("social", "社保个人部分", ("个人社保", "个人承担社保", "社保个人", "个人社保扣款")),
        _f("housing", "公积金个人部分", ("个人公积金", "公积金个人", "个人住房公积金", "公积金扣款")),
        _f("tax", "个税", ("个人所得税", "代扣个税", "代扣个人所得税")),
        _f("other", "其他扣款", ("其他扣除", "其它扣款", "其他扣减")),
        _f("net", "实发工资", ("实发", "实发合计", "实发金额", "实发工资合计", "实际发放"), True),
    ],
    "logistics": [
        _f("subtype", "物流类型", ("资料类型", "业务类型")),
        _f("business_no", "业务单号", ("订单号", "订单编号", "出入库单号", "出库单号", "入库单号", "发货单号")),
        _f("tracking_no", "运单号", ("物流单号", "快递单号", "快递单编号", "运单编号", "快递编号")),
        _f("date", "收发货日期", ("日期", "发货日期", "发货时间", "寄件时间", "寄件日期", "出入库日期", "出库日期", "入库日期")),
        _f("received_date", "签收日期", ("签收时间", "收货日期", "收货时间")),
        _f("counterparty", "客户或供应商", ("客户", "客户名称", "供应商", "供应商名称", "往来单位", "收件单位")),
        _f("carrier", "承运公司", ("物流公司", "快递公司", "承运商", "运输公司")),
        _f("item", "商品名称", ("商品", "品名", "货物名称", "物品名称", "存货名称", "产品名称")),
        _f("specification", "规格型号", ("规格", "型号", "商品规格")),
        _f("quantity", "数量", ("商品数量", "发货数量", "收货数量", "出库数量", "入库数量", "件数")),
        _f("unit", "单位", ("计量单位", "数量单位")),
        _f("freight", "运费", ("运费合计", "运输费", "运费金额", "快递费", "结算运费")),
        _f("direction", "出入库方向", ("出入库", "出入库类型", "收发方向")),
        _f("status", "物流状态", ("状态", "运输状态", "签收状态")),
    ],
    "invoice": [
        _f("direction", "发票方向", ("收票或开票", "购销方向", "进销项", "收开方向")),
        _f("invoice_no", "发票号码", ("发票号", "票据号码", "数电票号码", "电子发票号码")),
        _f("invoice_code", "发票代码", ("票据代码",)),
        _f("date", "开票日期", ("开票时间", "发票日期", "日期")),
        _f("buyer", "购买方名称", ("购买方", "购方名称", "购方", "购货单位")),
        _f("buyer_tax_id", "购买方税号", ("购方税号", "购买方纳税人识别号", "购方纳税人识别号")),
        _f("seller", "销售方名称", ("销售方", "销方名称", "销方", "销货单位")),
        _f("seller_tax_id", "销售方税号", ("销方税号", "销售方纳税人识别号", "销方纳税人识别号")),
        _f("item", "商品或服务名称", ("货物或应税劳务、服务名称", "货物或应税劳务服务名称", "货物或服务名称", "项目名称", "商品名称", "服务名称", "品名")),
        _f("quantity", "数量", ("开票数量", "商品数量")),
        _f("unit", "单位", ("计量单位",)),
        _f("amount", "不含税金额", ("金额", "金额(不含税)", "税前金额", "金额合计", "不含税金额合计")),
        _f("tax", "税额", ("税额合计", "合计税额")),
        _f("total", "价税合计", ("含税金额", "价税合计(小写)", "价税合计金额", "价税合计(元)", "含税总额")),
        _f("status", "发票状态", ("状态", "正常红字作废", "票据状态")),
        _f("business_no", "业务单号", ("订单号", "订单编号", "业务编号", "关联单号")),
    ],
}


@dataclass
class WorkbookSource:
    path: str
    digest: str
    sheets: list[tuple[str, list[list]]]


@dataclass
class MappingSpec:
    kind: str
    sheet: str
    header_row: int
    mapping: dict[str, int]
    defaults: dict[str, str] = field(default_factory=dict)


@dataclass
class ImportRecord:
    id: str
    kind: str
    values: dict
    raw: list
    source_path: str
    sheet: str
    row: int
    issues: list[Issue] = field(default_factory=list)
    source_digest: str = ""

    @property
    def source(self):
        return f"{Path(self.source_path).name} / {self.sheet} / 第{self.row}行"

    @property
    def valid(self):
        return not any(issue.severity == "error" for issue in self.issues)


@dataclass
class ImportResult:
    records: list[ImportRecord] = field(default_factory=list)
    issues: list[Issue] = field(default_factory=list)


def _normal(value):
    return re.sub(r"[\s：:]", "", text(value)).replace("（", "(").replace("）", ")").casefold()


def header_signature(headers):
    return hashlib.sha256(json.dumps([_normal(h) for h in headers], ensure_ascii=False).encode("utf-8")).hexdigest()


def suggest_mapping(headers, kind):
    """Only suggest unambiguous exact aliases; never infer a monetary meaning."""
    if kind not in SCHEMAS:
        raise ValueError("请选择银行、工资、物流或发票资料类型。")
    normalized = [_normal(value) for value in headers]
    result = {}
    used = set()
    for spec in SCHEMAS[kind]:
        names = {_normal(spec.label), _normal(spec.key), *(_normal(v) for v in spec.aliases)}
        matches = [i for i, value in enumerate(normalized) if value and value in names]
        if len(matches) == 1 and matches[0] not in used:
            result[spec.key] = matches[0]
            used.add(matches[0])
    if kind == "bank" and ("income" in result or "expense" in result):
        result.pop("amount", None)
    return result


def suggest_header(rows, kind):
    if not rows:
        return 0
    def score(row):
        mapping = suggest_mapping(row, kind)
        required = sum(1 for f in SCHEMAS[kind] if f.required and f.key in mapping)
        return (len(mapping) + required * 2, required)
    return max(range(min(60, len(rows))), key=lambda index: score(rows[index]))


def inspect_workbook(path):
    source = Path(path)
    sheets = read_sheets(source, formula_aware=True)
    if source.suffix.lower() == ".xls":
        # xlrd exposes an empty formula result as TEXT "", whereas an ordinary
        # empty cell is EMPTY. Preserve this distinction for numeric mappings;
        # otherwise a missing deduction formula could silently become zero.
        try:
            with xlrd.open_workbook(str(source)) as book:
                for sheet_name, rows in sheets:
                    sheet = book.sheet_by_name(sheet_name)
                    for r, row in enumerate(rows):
                        for c in range(len(row)):
                            cell = sheet.cell(r, c)
                            if cell.ctype == xlrd.XL_CELL_TEXT and cell.value == "":
                                row[c] = "[空文本或公式结果]"
        except Exception as exc:
            raise ValueError(f"无法检查 {source.name} 的公式结果，请用 Excel 另存为 xlsx 后重试。") from exc
    try:
        with source.open("rb") as stream:
            digest = hashlib.file_digest(stream, "sha256").hexdigest()
    except OSError as exc:
        raise ValueError(f"无法读取 {source.name}，请检查文件是否被占用。") from exc
    return WorkbookSource(str(source.resolve()), digest, sheets)


def _serial(value):
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        # Also prevents non-standard JSON NaN/Infinity from entering storage.
        return str(value) if not Decimal(str(value)).is_finite() else value
    return str(value)


def _period(value):
    if isinstance(value, (date, datetime)):
        return value.strftime("%Y-%m")
    s = text(value)
    match = re.search(r"(?<!\d)(20\d{2})\s*[-年/.]\s*(0?[1-9]|1[0-2])(?:月|(?=\D|$))", s)
    if not match:
        match = re.fullmatch(r"(20\d{2})(0[1-9]|1[0-2])", s)
    if not match:
        raise ValueError("请填写完整工资月份，例如 2026-10")
    return f"{int(match[1]):04d}-{int(match[2]):02d}"


def _date_value(value):
    if isinstance(value, date) and not isinstance(value, datetime):
        return value.isoformat()
    # A YYYYMMDD numeric value is a calendar date, not an Excel serial.
    raw = text(value)
    if re.fullmatch(r"20\d{6}", raw):
        dt = datetime.strptime(raw, "%Y%m%d")
    else:
        dt = _date(value)
    return dt.isoformat(sep=" ", timespec="seconds") if dt.time() != datetime.min.time() else dt.date().isoformat()


_MONEY = {
    "bank": {"income", "expense", "amount", "balance"},
    "payroll": {"gross", "social", "housing", "tax", "other", "net"},
    "logistics": {"freight"},
    "invoice": {"amount", "tax", "total"},
}
_IDENTIFIERS = {"invoice_no", "invoice_code", "business_no", "tracking_no", "buyer_tax_id", "seller_tax_id", "account_number"}
_SUMMARIES = {"合计", "小计", "总计", "本页合计", "本月合计", "本期合计", "累计", "总收入笔数", "总支出笔数"}


def _issue(record, severity, message):
    record.issues.append(Issue(severity, message, record.source))


def _number(value, monetary):
    s = text(value).replace(",", "").replace("，", "").replace("￥", "").replace("¥", "").strip()
    if not s or s in {"-", "—"}:
        return ""
    if s.startswith("(") and s.endswith(")"):
        s = "-" + s[1:-1]
    n = Decimal(s)
    if not n.is_finite():
        raise ValueError("金额或数量不是有效数字")
    return format(money(n), "f") if monetary else format(n, "f")


def _bank_direction(value):
    s = _normal(value)
    if s in {"收入", "收款", "收", "入", "转入", "进账", "进", "存入", "贷记", "income", "in"}:
        return "收入"
    if s in {"支出", "付款", "付", "支", "出", "转出", "出账", "支取", "借记", "expense", "out"}:
        return "支出"
    return ""


def _validate_record(record, spec):
    values = record.values
    labels = {f.key: f.label for f in SCHEMAS[record.kind]}
    for f in SCHEMAS[record.kind]:
        if f.required and values.get(f.key, "") == "":
            _issue(record, "error", f"缺少{f.label}，此行保留待核对，不参与生成凭证。")
    if record.kind == "bank":
        split = "income" in spec.mapping or "expense" in spec.mapping
        single = "amount" in spec.mapping
        if split and single:
            _issue(record, "error", "收入/支出列与单列交易金额同时选择，请只保留一种金额读取方式。")
        elif split:
            if values.get("income", "") == values.get("expense", "") == "":
                _issue(record, "error", "收入和支出金额均为空，请核对源表或公式保存结果。")
            if values.get("income") and values.get("expense"):
                try:
                    if Decimal(values["income"]) and Decimal(values["expense"]):
                        _issue(record, "warning", "此行同时有收入和支出，请核对是否为正常收付款记录。")
                except InvalidOperation:
                    pass
        elif single:
            if values.get("amount", "") == "":
                _issue(record, "error", "交易金额为空，请核对源表或公式保存结果。")
            direction = _bank_direction(values.get("direction", ""))
            if not direction:
                _issue(record, "error", "单列金额需要明确收支方向（收入或支出）；不会仅凭正负号猜测。")
            else:
                values["direction"] = direction
            try:
                if values.get("amount") and Decimal(values["amount"]) < ZERO:
                    sign_mode = spec.defaults.get("amount_sign", "")
                    if sign_mode == "absolute":
                        values["amount"] = format(abs(Decimal(values["amount"])), "f")
                    elif sign_mode != "signed":
                        _issue(record, "error", "单列金额含负数，请确认按收支方向取绝对值，还是保留正负表示冲回。")
            except InvalidOperation:
                pass
        else:
            _issue(record, "error", "请对应收入/支出金额，或交易金额与收支方向。")
    elif record.kind == "payroll":
        if not values.get("period"):
            _issue(record, "error", "工资所属年月不明确，请在确认窗口填写，例如 2026-10。")
        if record.valid:
            try:
                gap = Decimal(values["gross"]) - sum((Decimal(values.get(k) or "0") for k in ("social", "housing", "tax", "other", "net")), ZERO)
                if gap:
                    _issue(record, "warning", f"应发减扣款与实发相差 {gap:.2f} 元，保留源表金额，请核对。")
            except InvalidOperation:
                pass
    elif record.kind == "invoice":
        for key in ("invoice_no", "date", "direction"):
            if not values.get(key):
                _issue(record, "warning", f"缺少{labels[key]}，已保留资料，请补充后核对。")
        if not any(values.get(key) for key in ("amount", "tax", "total")):
            _issue(record, "warning", "未读取到发票金额，请确认金额列对应关系。")
        # Whole-invoice totals are frequently repeated on detail rows. Do not
        # compare or sum totals without knowing whether these are line amounts.
        if not values.get("status"):
            _issue(record, "warning", "未提供发票状态，不能据此认定为正常有效发票。")
    elif record.kind == "logistics":
        if not values.get("business_no") and not values.get("tracking_no"):
            _issue(record, "warning", "没有业务单号或运单号，可查看明细，暂时无法按单号关联。")
        if not values.get("subtype") or values.get("subtype") == "暂不确定":
            _issue(record, "warning", "物流类型待确认，请区分商品收发货与运输、运费资料。")


def _parse_row(workbook, spec, row, row_number, inferred):
    values = {f.key: "" for f in SCHEMAS[spec.kind]}
    identity = json.dumps([workbook.digest, spec.kind, spec.sheet, row_number], ensure_ascii=False)
    record = ImportRecord(hashlib.sha256(identity.encode("utf-8")).hexdigest(), spec.kind, values,
                          [_serial(value) for value in row], workbook.path, spec.sheet, row_number,
                          source_digest=workbook.digest)
    for f in SCHEMAS[spec.kind]:
        index = spec.mapping.get(f.key)
        value = row[index] if index is not None and index < len(row) else None
        if index is None:
            value = spec.defaults.get(f.key) or inferred.get(f.key, "")
        if value is None or text(value) == "":
            continue
        values[f.key] = text(value)
        if text(value) == "[空文本或公式结果]":
            values[f.key] = ""
            if f.key in _MONEY[spec.kind] or f.key in {"quantity", "date", "received_date", "period"}:
                _issue(record, "error", f"{f.label}为空文本或公式没有有效结果，请在 Excel 核对、重算并保存。")
            continue
        if "公式缺少保存结果" in text(value):
            _issue(record, "error", f"{f.label}的公式没有已保存结果，请在 Excel 重算并保存。")
            continue
        try:
            if f.key in _MONEY[spec.kind] or f.key == "quantity":
                values[f.key] = _number(value, f.key in _MONEY[spec.kind])
            elif f.key in {"date", "received_date"}:
                values[f.key] = _date_value(value)
            elif f.key == "period":
                values[f.key] = _period(value)
            elif f.key in _IDENTIFIERS and isinstance(value, (int, float)) and abs(value) >= 10 ** 15:
                _issue(record, "warning", f"{f.label}在源表中是超过 15 位的数字，Excel 可能已丢失精度；请核对原始号码并改为文本。")
        except (ValueError, InvalidOperation, OverflowError, TypeError):
            _issue(record, "error", f"{f.label}无法识别（{text(value)[:80]}），已保留原值，请核对。")
    _validate_record(record, spec)
    return record


def parse_workbook(workbook, specs):
    sheets = dict(workbook.sheets)
    result = ImportResult()
    imported = set()
    business_rows = {}
    for spec in specs:
        if spec.kind not in SCHEMAS:
            raise ValueError("资料类型无效，请重新选择。")
        if spec.sheet not in sheets:
            raise ValueError(f"找不到工作表：{spec.sheet}")
        rows = sheets[spec.sheet]
        if not isinstance(spec.header_row, int) or not 0 <= spec.header_row < len(rows):
            raise ValueError(f"{spec.sheet} 的表头行无效。")
        header = rows[spec.header_row]
        allowed = {f.key for f in SCHEMAS[spec.kind]}
        if not spec.mapping or any(key not in allowed or not isinstance(index, int) or index < 0 or index >= len(header) for key, index in spec.mapping.items()):
            raise ValueError(f"{spec.sheet} 的列对应关系无效，请至少选择一列并核对列号。")
        if len(set(spec.mapping.values())) != len(spec.mapping):
            raise ValueError(f"{spec.sheet} 的同一列对应了多个含义，请分别选择。")
        inferred = {}
        titles = " ".join([spec.sheet] + [text(value) for row in rows[:spec.header_row] for value in row])
        if spec.kind == "payroll":
            try:
                inferred["period"] = _period(titles)
            except ValueError:
                pass
        elif spec.kind == "bank":
            account = re.search(r"(?:账号|账户号码)\s*[:：]\s*([\d-]+)", titles)
            if account:
                inferred["account_number"] = account[1]
        sheet_records = []
        payroll_total = None
        for row_number, row in enumerate(rows[spec.header_row + 1:], spec.header_row + 2):
            if not any(text(value) for value in row):
                continue
            # Bank footer grids contain counts on the next line, not business.
            if spec.kind == "bank" and any(_normal(v) in {"总收入笔数", "总支出笔数"} for v in row[:4]):
                break
            summary_values = list(row[:3])
            if spec.kind == "payroll" and "name" in spec.mapping and spec.mapping["name"] < len(row):
                summary_values.append(row[spec.mapping["name"]])
            if any(_normal(value) in _SUMMARIES for value in summary_values):
                if spec.kind == "payroll" and any(_normal(value) in {"合计", "总计", "本月合计", "本期合计"} for value in summary_values):
                    payroll_total = (row_number, row)
                continue
            if all(index < len(row) and _normal(row[index]) == _normal(header[index]) for index in spec.mapping.values()):
                continue
            record = _parse_row(workbook, spec, row, row_number, inferred)
            if record.id in imported:
                result.issues.append(Issue("info", "本次重复选择的相同工作表记录已跳过。", record.source))
                continue
            imported.add(record.id)
            # Same invoice number alone is never a duplicate: line detail is
            # part of the fingerprint. Suspected duplicates stay in the result.
            fingerprint = json.dumps([record.kind, record.values], ensure_ascii=False, sort_keys=True)
            if fingerprint in business_rows:
                _issue(record, "warning", f"与 {business_rows[fingerprint]} 内容相同，可能重复，已保留，请核对。")
            else:
                business_rows[fingerprint] = record.source
            result.records.append(record)
            sheet_records.append(record)
            result.issues.extend(record.issues)
        if payroll_total and sheet_records:
            row_number, total_row = payroll_total
            source = f"{Path(workbook.path).name} / {spec.sheet} / 第{row_number}行"
            for key, label in (("gross", "应发工资"), ("net", "实发工资")):
                column = spec.mapping.get(key)
                if column is None or column >= len(total_row) or not text(total_row[column]):
                    continue
                try:
                    expected = _number(total_row[column], True)
                    if not expected:
                        continue
                    actual = sum((Decimal(r.values[key]) for r in sheet_records if r.valid and r.values.get(key)), ZERO)
                    if actual != Decimal(expected):
                        result.issues.append(Issue("warning", f"{label}有效明细合计 {actual:.2f} 元，表内合计 {expected} 元，相差 {actual - Decimal(expected):.2f} 元，请核对明细范围及待核对记录。", source))
                except (ValueError, InvalidOperation):
                    result.issues.append(Issue("warning", f"{label}合计行金额无法读取或公式无保存结果，请核对源表。", source))
    return result


def accounting_rows(records):
    """Convert confirmed valid accounting rows; logistics/invoices stay data."""
    transactions, payroll, issues = [], [], []
    seen = set()
    for record in records:
        if record.id in seen or record.kind not in {"bank", "payroll"}:
            continue
        seen.add(record.id)
        issues.extend(record.issues)
        if not record.valid:
            continue
        v = record.values
        try:
            if record.kind == "bank":
                income, expense = money(v.get("income") or 0), money(v.get("expense") or 0)
                if v.get("amount"):
                    amount = money(v["amount"])
                    if v.get("direction") == "收入":
                        income = amount
                    elif v.get("direction") == "支出":
                        expense = amount
                    else:
                        raise ValueError("收支方向不明确")
                if not income and not expense:
                    issues.append(Issue("info", "零金额银行记录已保留资料，不生成凭证。", record.source))
                    continue
                transactions.append(Transaction(datetime.fromisoformat(v["date"]), income, expense,
                                                v.get("counterparty", ""), v.get("summary", ""), record.source,
                                                money(v["balance"]) if v.get("balance") else None,
                                                v.get("account_number", "")))
            else:
                period = _period(v["period"])
                payroll.append(PayrollRow(int(period[5:7]), v["name"], v.get("department", ""),
                                          *(money(v.get(key) or 0) for key in ("gross", "social", "housing", "tax", "other", "net")),
                                          record.source, int(period[:4])))
        except (ValueError, KeyError, InvalidOperation, OverflowError) as exc:
            issues.append(Issue("error", f"资料无法转换为凭证来源，已跳过此行：{exc}", record.source))
    return transactions, payroll, issues
