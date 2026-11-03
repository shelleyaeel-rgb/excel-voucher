from dataclasses import dataclass
from pathlib import Path
import hashlib
from .models import Issue, Options, TemplateSpec, Transaction, PayrollRow
from .config import resource_path
from .spreadsheet_io import read_bank,read_payroll,read_template,export_vouchers

@dataclass
class InputBundle:
    template: TemplateSpec
    transactions: list[Transaction]
    payroll: list[PayrollRow]
    issues: list[Issue]
    months: list[str]
    source_counts: dict

def default_template_path():return str(resource_path('assets/凭证导入模板.xls'))

def load_inputs(bank_paths, payroll_paths, template_path=None):
    template=read_template(template_path or default_template_path())
    transactions=[];payroll=[];issues=[];seen=set()
    counts={'bank_files':0,'payroll_files':0,'transactions':0,'payroll_rows':0}
    for paths,reader,key,dest in [(bank_paths,read_bank,'bank_files',transactions),(payroll_paths,read_payroll,'payroll_files',payroll)]:
        for filename in paths:
            source=Path(filename)
            if not source.is_file():raise ValueError(f'找不到文件：{source.name}')
            if source.stat().st_size>100*1024*1024:raise ValueError(f'{source.name} 超过100MB，请按月份拆分后导入。')
            try:
                with source.open('rb') as stream:
                    digest=hashlib.file_digest(stream,'sha256').hexdigest()
            except OSError as exc:
                raise ValueError(f'无法读取 {source.name}，请检查文件是否被占用或没有访问权限。') from exc
            if digest in seen:
                issues.append(Issue('info','内容完全相同的重复文件已跳过。',Path(filename).name));continue
            seen.add(digest)
            values,errors=reader(filename);dest.extend(values);issues.extend(errors);counts[key]+=1
    counts.update(transactions=len(transactions),payroll_rows=len(payroll))
    months=sorted({t.date.strftime('%Y-%m') for t in transactions})
    accounts={t.account_number for t in transactions if t.account_number}
    if len(accounts)>1:
        issues.append(Issue('warning','本次流水包含多个银行账号，当前设置只使用一个银行科目，请按银行分别生成或核对科目。'))
    if not transactions:issues.append(Issue('warning','没有读取到银行收付记录，请检查文件或仅生成工资计提。'))
    return InputBundle(template,transactions,payroll,issues,months,counts)

def generate_bundle(bundle,options):
    from .engine import generate
    result=generate(bundle.transactions,bundle.payroll,bundle.template,options)
    result.issues=bundle.issues+result.issues
    return result

def export_result(result,template,path,preparer='001'):
    return export_vouchers(result,template,path,preparer)


def bundle_from_records(records, template_path=None, additional_issues=None):
    """Prepare accounting only on request; materials never require a voucher template."""
    from .imports import accounting_rows
    accounting = [record for record in records if record.kind in ('bank', 'payroll')]
    transactions, payroll, issues = accounting_rows(accounting)
    known_issues = {(issue.severity, issue.message, issue.source) for issue in issues}
    for issue in additional_issues or []:
        key = (issue.severity, issue.message, issue.source)
        if key not in known_issues:
            issues.append(issue)
            known_issues.add(key)
    if not transactions and not payroll:
        raise ValueError('没有可用于生成凭证的银行或工资记录，请先核对日期、金额和工资所属年月。')
    template = read_template(template_path or default_template_path())
    months = {t.date.strftime('%Y-%m') for t in transactions}
    months.update(f'{p.year:04d}-{p.month:02d}' for p in payroll if p.year)
    accounts = {t.account_number for t in transactions if t.account_number}
    if len(accounts) > 1:
        issues.append(Issue('warning', '资料中包含多个银行账号，当前只使用一个银行科目，请按银行分别生成或核对科目。'))
    counts = {'transactions': len(transactions), 'payroll_rows': len(payroll)}
    return InputBundle(template, transactions, payroll, issues, sorted(months), counts)
