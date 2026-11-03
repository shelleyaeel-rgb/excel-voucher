from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal, ROUND_HALF_UP

ZERO = Decimal("0.00")
def money(value=0):
    if value is None or value == "": return ZERO
    return Decimal(str(value).replace(",", "").replace("￥", "").strip()).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

@dataclass
class Issue:
    severity: str
    message: str
    source: str = ""

@dataclass
class Account:
    code: str
    name: str

@dataclass
class Transaction:
    date: datetime
    income: Decimal
    expense: Decimal
    counterparty: str
    summary: str
    source: str
    balance: Decimal | None = None
    account_number: str = ""

@dataclass
class PayrollRow:
    month: int
    name: str
    department: str
    gross: Decimal
    social: Decimal
    housing: Decimal
    tax: Decimal
    other: Decimal
    net: Decimal
    source: str
    year: int | None = None

@dataclass
class TemplateSpec:
    headers: list[str]
    accounts: dict[str, Account]
    path: str = ""
    sheet_name: str = "凭证导入模板"

@dataclass
class Entry:
    account_code: str
    account_name: str
    debit: Decimal = ZERO
    credit: Decimal = ZERO
    summary: str = ""

@dataclass
class Voucher:
    date: str
    number: int
    summary: str
    entries: list[Entry]
    sources: list[str] = field(default_factory=list)
    issues: list[Issue] = field(default_factory=list)
    included: bool = True
    @property
    def debit(self): return sum((e.debit for e in self.entries), ZERO)
    @property
    def credit(self): return sum((e.credit for e in self.entries), ZERO)

@dataclass
class Options:
    period: str = ""  # YYYY-MM; empty means all bank dates
    bank_account: str = "1002001"
    start_number: int = 1
    preparer: str = "001"
    platform_mode: str = "settlement_only"  # or income_and_settlement, one voucher
    platform_fee_mode: str = "none"  # none/rate/amount; per receipt
    platform_fee_value: Decimal = ZERO
    tax_rate: Decimal = Decimal("1")  # percent; inclusive sales total / (1+rate)
    payroll_accrual: bool = False
    mappings: dict[str, str] = field(default_factory=dict)  # exact counterparty -> account code
    social_payment_dates: list[str] = field(default_factory=list)  # explicitly identified batches only
    social_accrual: bool = False
    accrual_period: str = ""  # optional posting month; source amounts still use period

@dataclass
class GenerationResult:
    vouchers: list[Voucher]
    issues: list[Issue]
    transactions: list[Transaction]
    @property
    def selected(self): return [v for v in self.vouchers if v.included]
    @property
    def debit(self): return sum((v.debit for v in self.selected), ZERO)
    @property
    def credit(self): return sum((v.credit for v in self.selected), ZERO)
