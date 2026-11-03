"""Local spreadsheet adapters. Excel formulas are read as cached values, never executed."""
import csv
import os
import re
import tempfile
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from collections import defaultdict
import xlrd
import xlwt
import openpyxl
from openpyxl.styles import Font, PatternFill, Alignment
from .models import Account, Entry, Issue, PayrollRow, TemplateSpec, Transaction, money, ZERO

HEADERS = ['日期','凭证号','行号','摘要','科目编码','科目名称','外币代码','借方数量','借方单价','借方外币','借方汇率','借方本币','贷方数量','贷方单价','贷方外币','贷方汇率','贷方本币','客户编码','客户名称','供应商编码','供应商名称','职员编码','职员名称','部门编码','部门名称','项目编码','项目名称','存货编码','存货名称','现金流编码','现金流名称','制单人','审核人','附件数','备注']

def text(value):
    if value is None: return ''
    if isinstance(value, float) and value.is_integer(): return str(int(value))
    return str(value).strip()

def norm(value): return re.sub(r'\s+', '', text(value)).replace('（','(').replace('）',')')

def read_sheets(path, formula_aware=False):
    p = Path(path)
    if not p.is_file(): raise ValueError(f'找不到文件：{p.name}')
    if p.stat().st_size > 100*1024*1024: raise ValueError(f'{p.name} 超过100MB，请按月份拆分后导入。')
    try:
        if p.suffix.lower() == '.xls':
            book = xlrd.open_workbook(str(p))
            result = []
            for s in book.sheets():
                rows=[]
                for r in range(s.nrows):
                    row=[]
                    for c in range(s.ncols):
                        cell=s.cell(r,c)
                        row.append(xlrd.xldate_as_datetime(cell.value,book.datemode) if cell.ctype==xlrd.XL_CELL_DATE else cell.value)
                    rows.append(row)
                result.append((s.name,rows))
            return result
        if p.suffix.lower() == '.xlsx':
            book=openpyxl.load_workbook(p,read_only=True,data_only=True,keep_links=False)
            formulas=None
            try:
                if formula_aware:
                    formulas=openpyxl.load_workbook(p,read_only=True,data_only=False,keep_links=False)
                result=[]
                for s in book:
                    # Some exports incorrectly declare only A1 as the used range.
                    s.reset_dimensions()
                    rows=[list(row) for row in s.iter_rows(values_only=True)]
                    if formulas:
                        raw=formulas[s.title];raw.reset_dimensions()
                        for r,cells in enumerate(raw.iter_rows()):
                            for c,cell in enumerate(cells):
                                if cell.data_type=='f' and r<len(rows) and c<len(rows[r]) and rows[r][c] is None:
                                    rows[r][c]='[公式缺少保存结果]'
                    result.append((s.title,rows))
            finally:
                book.close()
                if formulas:formulas.close()
            return result
        raise ValueError('请选择 .xls 或 .xlsx 文件。')
    except ValueError: raise
    except Exception as exc: raise ValueError(f'无法读取 {p.name}：文件可能加密或格式损坏，请用Excel另存为xlsx后重试。') from exc

def _cell(row, index, default=''):
    return row[index] if index is not None and index < len(row) else default

def _amount(value):
    if value is None or text(value) in ('','-','—'): return ZERO
    n=money(value)
    if not n.is_finite(): raise ValueError('金额不是有限数字')
    return n

def _date(value):
    if isinstance(value,datetime): return value
    if isinstance(value,(float,int)): return datetime(1899,12,30)+timedelta(days=value)
    s=text(value).replace('年','-').replace('月','-').replace('日','').replace('/','-')
    try: return datetime.fromisoformat(s)
    except ValueError:
        for fmt in ('%Y%m%d','%Y-%m-%d %H:%M','%Y-%m-%d'):
            try: return datetime.strptime(s,fmt)
            except ValueError: pass
    raise ValueError('无法识别交易日期')

def read_bank(path):
    transactions=[];issues=[];found=False
    for name,rows in read_sheets(path):
        header=None
        for i,row in enumerate(rows[:40]):
            keys={norm(v):c for c,v in enumerate(row) if text(v)}
            if '交易时间' in keys and '收入金额' in keys and '支出金额' in keys:
                header=i;break
        if header is None:continue
        found=True
        account=''
        for row in rows[:header]:
            for value in row:
                match=re.search(r'账号[:：]\s*([\d-]+)',text(value))
                if match:account=match.group(1)
        for i,row in enumerate(rows[header+1:],header+2):
            if any('总收入笔数' in text(v) or '总支出笔数' in text(v) for v in row[:4]):break
            raw_date=_cell(row,keys['交易时间'])
            if not text(raw_date) or '总' in text(raw_date) or norm(raw_date) in ('合计','小计'):continue
            source=f'{Path(path).name} / {name} / 第{i}行'
            try:
                dt=_date(raw_date)
                inc=_amount(_cell(row,keys['收入金额']));exp=_amount(_cell(row,keys['支出金额']))
                # Refunds/reversals and unusual double-sided rows remain visible;
                # the engine records a review issue instead of losing cash movements.
                if not inc and not exp:continue
                bal=_cell(row,keys.get('账户余额'))
                transactions.append(Transaction(dt,inc,exp,text(_cell(row,keys.get('对方户名'))),text(_cell(row,keys.get('摘要'))),source,_amount(bal) if text(bal) else None,account))
            except (ValueError,InvalidOperation,OverflowError):
                issues.append(Issue('error','此行日期或金额无效，未参与生成，请核对源表。',source))
    if not found:raise ValueError(f'{Path(path).name} 未找到“交易时间、收入金额、支出金额”表头，请放入银行流水文件。')
    return transactions,issues

def read_payroll(path):
    payroll=[];issues=[];found=False
    for name,rows in read_sheets(path,formula_aware=True):
        header=None
        for i,row in enumerate(rows[:20]):
            keys={norm(v):c for c,v in enumerate(row) if text(v)}
            if {'姓名','应发工资','实发工资'} <= keys.keys():header=i;break
        if header is None:continue
        found=True
        titles=' '.join([name]+[text(v) for row in rows[:header] for v in row])
        mm=re.search(r'(?<!\d)(1[0-2]|0?[1-9])月',titles)
        yy=re.search(r'(20\d{2})年',titles)
        if not mm:
            issues.append(Issue('error','工资表没有明确月份，未参与生成。',f'{Path(path).name} / {name}'));continue
        sums=defaultdict(lambda:ZERO);total_row=None
        for i,row in enumerate(rows[header+1:],header+2):
            if any(norm(v)=='合计' for v in row[:3]):total_row=row;continue
            employee=text(_cell(row,keys['姓名']))
            if not employee:continue
            source=f'{Path(path).name} / {name} / 第{i}行'
            try:
                gross_raw=_cell(row,keys['应发工资']);net_raw=_cell(row,keys['实发工资'])
                if gross_raw is None or net_raw is None or text(gross_raw)=='' or text(net_raw)=='':
                    raise ValueError('missing calculation cache')
                vals={k:_amount(_cell(row,keys.get(k))) for k in ['应发工资','社保个人部分','公积金个人部分','个税','其他扣款','实发工资']}
                p=PayrollRow(int(mm.group(1)),employee,text(_cell(row,keys.get('部门'))),*vals.values(),source,int(yy.group(1)) if yy else None)
                payroll.append(p)
                for k,v in vals.items():sums[k]+=v
                gap=p.gross-p.social-p.housing-p.tax-p.other-p.net
                if gap:
                    issues.append(Issue('warning',f'{employee}应发减扣款与实发相差 {gap:.2f} 元，保留源表金额，不自动平差。',source))
            except (ValueError,InvalidOperation):issues.append(Issue('error','工资金额缺失或公式没有已保存的计算结果，请在Excel重算并保存，此行未参与生成。',source))
        if total_row:
            for k in ['应发工资','实发工资']:
                try:
                    total=_amount(_cell(total_row,keys[k]))
                    if total!=sums[k]:issues.append(Issue('warning',f'{k}逐行按分汇总为{sums[k]:.2f}，表内合计为{total:.2f}，请核对取整或明细范围。',f'{Path(path).name} / {name}'))
                except InvalidOperation:pass
    if not found:raise ValueError(f'{Path(path).name} 未找到工资表表头。')
    return payroll,issues

def read_template(path):
    headers=None;sheet_name='凭证导入模板';accounts={};sample_accounts={}
    for name,rows in read_sheets(path):
        for i,row in enumerate(rows[:20]):
            keys={norm(v):c for c,v in enumerate(row) if text(v)}
            if {'日期','凭证号','科目编码','借方本币','贷方本币'}<=keys.keys() and headers is None:
                headers=[text(v) for v in row]
                while headers and not headers[-1]:headers.pop()
                sheet_name=name
            if {'科目编码','科目名称'}<=keys.keys():
                destination=sample_accounts if '凭证号' in keys else accounts
                for rec in rows[i+1:]:
                    code=text(_cell(rec,keys['科目编码']));label=text(_cell(rec,keys['科目名称']))
                    if code and label and code.isdigit():destination[code]=Account(code,label)
                break
    if headers is None:raise ValueError('凭证模板中没有找到日期、凭证号、科目编码、借方本币、贷方本币列。')
    return TemplateSpec(headers,accounts or sample_accounts,str(path),sheet_name)

def _safe(value):
    return "'"+value if isinstance(value,str) and value.lstrip().startswith(('=','+','-','@')) else value

def export_vouchers(result,template,path,preparer='001'):
    target=Path(path)
    if target.suffix.lower() not in ('.xls','.xlsx'):raise ValueError('导出格式请选择 .xls 或 .xlsx。')
    selected=result.selected
    if not selected:raise ValueError('请至少选择一张凭证。')
    all_issues=list(result.issues)
    rows=[]
    for v in selected:
        all_issues.extend(v.issues)
        if v.debit!=v.credit:all_issues.append(Issue('error',f'记-{v.number}借贷相差{v.debit-v.credit:.2f}元。','；'.join(v.sources)))
        for i,e in enumerate(v.entries,1):
            data={'日期':v.date,'凭证号':f'记-{v.number}','行号':str(i),'摘要':e.summary or v.summary,'科目编码':e.account_code,'科目名称':e.account_name,'借方本币':float(e.debit) if e.debit else '', '贷方本币':float(e.credit) if e.credit else '', '制单人':preparer,'附件数':0}
            rows.append([_safe(data.get(h,'')) for h in template.headers])
            if not e.account_code:all_issues.append(Issue('warning',f'记-{v.number}第{i}行缺少科目编码，请补齐后导入财务软件。','；'.join(v.sources)))
    target.parent.mkdir(parents=True,exist_ok=True)
    fd,temp=tempfile.mkstemp(suffix=target.suffix,dir=target.parent);os.close(fd)
    try:
        widths={h:(36 if h in ('摘要','科目名称','备注') else 14) for h in template.headers}
        if target.suffix.lower()=='.xls':
            book=xlwt.Workbook();s=book.add_sheet('凭证导入模板')
            header=xlwt.easyxf('font: name Microsoft YaHei, bold on; pattern: pattern solid, fore_colour rose; alignment: vert centre;')
            normal=xlwt.easyxf('font: name Microsoft YaHei; alignment: vert centre;')
            amount=xlwt.easyxf('font: name Microsoft YaHei; alignment: vert centre;',num_format_str='#,##0.00')
            if len(rows)+1>65536:raise ValueError('XLS最多65536行，请选择XLSX导出。')
            for c,h in enumerate(template.headers):s.write(0,c,h,header);s.col(c).width=widths[h]*256
            for r,row in enumerate(rows,1):
                for c,value in enumerate(row):s.write(r,c,value,amount if template.headers[c] in ('借方本币','贷方本币') else normal)
            s.panes_frozen=True;s.horz_split_pos=1;book.save(temp)
        else:
            book=openpyxl.Workbook();s=book.active;s.title='凭证导入模板';s.append(template.headers)
            for row in rows:s.append(row)
            for c,h in enumerate(template.headers,1):
                s.column_dimensions[openpyxl.utils.get_column_letter(c)].width=widths[h]
                s.cell(1,c).font=Font(name='Microsoft YaHei',bold=True,color='6B304C');s.cell(1,c).fill=PatternFill('solid',fgColor='F9DFEA')
                for r in range(2,len(rows)+2):
                    s.cell(r,c).font=Font(name='Microsoft YaHei',size=10)
                    if h in ('借方本币','贷方本币'):s.cell(r,c).number_format='#,##0.00'
            s.freeze_panes='A2';book.save(temp);book.close()
        os.replace(temp,target)
    finally:
        if os.path.exists(temp):os.unlink(temp)
    unique={(i.severity,i.message,i.source):i for i in all_issues}
    issue_path=target.with_name(target.stem+'_待核对.csv')
    # Always write a companion report so an older warning report cannot be mistaken for this export.
    with issue_path.open('w',encoding='utf-8-sig',newline='') as f:
        writer=csv.writer(f);writer.writerow(['等级','提示','来源'])
        for i in unique.values():writer.writerow([_safe(i.severity),_safe(i.message),_safe(i.source)])
        if not unique:writer.writerow(['通过','本次未发现待核对项。',''])
    return dict(output_path=str(target),issues_path=str(issue_path),voucher_count=len(selected),line_count=len(rows))
