"""Portable desktop entry point; --self-test also exercises bundled resources."""
import argparse
import json
import os
import sys
import tempfile
import traceback
from datetime import datetime
from decimal import Decimal
from pathlib import Path


def _self_test(report_path: str) -> int:
    from xiaoyu import __version__
    from xiaoyu.models import Options, Transaction
    from xiaoyu.engine import generate
    from xiaoyu.service import InputBundle, default_template_path
    from xiaoyu.spreadsheet_io import export_vouchers, read_sheets, read_template
    from xiaoyu.config import resource_path
    from PySide6.QtWidgets import QApplication
    from xiaoyu import ui
    report = {"ok": False}
    try:
        template = read_template(default_template_path())
        assert len(template.headers) == 35 and len(template.accounts) > 100
        assert resource_path('assets/app.ico').is_file()
        tx = Transaction(datetime(2026, 8, 10), Decimal('101.00'), Decimal('0.00'),
                         '自检未配置户名', '软件自检', '合成测试记录')
        result = generate([tx], [], template, Options(period='2026-08'))
        assert result.debit == result.credit == Decimal('101.00')
        assert result.issues
        payment = Transaction(datetime(2026, 8, 10), Decimal('0.00'), Decimal('100.00'),
                              '自检员工', '普通付款', '合成科目映射测试')
        named = generate([payment], [], template,
                         Options(period='2026-08', mappings={'自检员工': '应付职工薪酬'}))
        assert named.debit == named.credit == Decimal('100.00')
        assert any(entry.account_code == '2211' and entry.debit == Decimal('100.00')
                   for voucher in named.vouchers for entry in voucher.entries)
        assert not named.issues
        invalid = generate([payment], [], template,
                           Options(period='2026-08', mappings={'自检员工': '不存在的自检科目'}))
        assert invalid.debit == invalid.credit == Decimal('100.00')
        assert any('科目设置未识别' in issue.message for issue in invalid.issues)
        assert any(not entry.account_code for voucher in invalid.vouchers for entry in voucher.entries)
        with tempfile.TemporaryDirectory(prefix='xiaoyu-smoke-') as directory:
            for extension in ('.xls', '.xlsx'):
                for label, case in (('missing', result), ('named', named), ('invalid', invalid)):
                    output = export_vouchers(case, template, Path(directory) / (label + extension))
                    rows = read_sheets(output['output_path'])[0][1]
                    assert rows[0] == template.headers and len(rows) == 3
                    assert Path(output['issues_path']).is_file()
        app = QApplication.instance() or QApplication([])
        ui.load_config = lambda: {}
        ui.save_config = lambda value: None
        with tempfile.TemporaryDirectory(prefix='xiaoyu-material-smoke-') as directory:
            import openpyxl
            from xiaoyu.imports import ImportResult, inspect_workbook, parse_workbook
            from xiaoyu.import_ui import MappingDialog
            from xiaoyu.material_store import MaterialStore, export_materials
            from xiaoyu.service import bundle_from_records
            folder = Path(directory)
            store = MaterialStore(folder / 'materials.sqlite3')
            cases = {
                'bank': ('流水', [['交易日', '收款金额', '付款金额', '交易对方'], ['2026-10-01', 101, '', '自检单位']]),
                'payroll': ('2026年10月', [['员工姓名', '应发金额', '实发金额'], ['自检员工', 5000, 5000]]),
                'logistics': ('运输', [['快递单编号', '寄件时间', '运费合计'], ['001234', '2026-10-01', 12]]),
                'invoice': ('发票', [['发票号', '开票时间', '含税金额'], ['00012345678901234567', '2026-10-01', 101]]),
            }
            records, issues = [], []
            for kind, (sheet, rows) in cases.items():
                path = folder / f'{kind}.xlsx'
                book = openpyxl.Workbook()
                book.active.title = sheet
                for row in rows:
                    book.active.append(row)
                book.save(path)
                book.close()
                workbook = inspect_workbook(path)
                dialog = MappingDialog(workbook, kind)
                assert dialog.confirm_button.isEnabled(), kind
                dialog.accept()
                parsed = parse_workbook(workbook, dialog.specs)
                assert len(parsed.records) == 1
                records.extend(parsed.records)
                issues.extend(parsed.issues)
                store.save_profiles('自检账套', dialog.profiles)
                dialog.deleteLater()
            first = store.save_import('自检账套', ImportResult(records, issues))
            repeated = store.save_import('自检账套', ImportResult(records, issues))
            assert first['added'] == 4 and repeated['added'] == 0
            assert not store.load_records('其他公司')
            loaded = store.load_records('自检账套')
            accounting = bundle_from_records(loaded)
            assert len(accounting.transactions) == len(accounting.payroll) == 1
            export_materials(loaded, store.get_issues('自检账套'), folder / '资料.xlsx')
            assert len(read_sheets(folder / '资料.xlsx')) == 6
            window = ui.MainWindow(material_store=store)
            app.processEvents()
            assert window.windowTitle().startswith('给小雨的软件')
            window.company_combo.setCurrentText('自检账套')
            assert len(window.material_records) == 4
            assert window.records_page.export_button.isEnabled()
            window.bundle = InputBundle(template, [payment], [], [], ['2026-08'], {})
            window._add_mapping('自检员工', '2211')
            account_combo = window.mapping_table.cellWidget(0, 1)
            account_combo.setEditText('应付职工薪酬')
            account_combo.lineEdit().editingFinished.emit()
            assert account_combo.currentText() == '2211  应付职工薪酬'
            assert window._read_options().mappings['自检员工'] == '2211'
            window.close()
            window.deleteLater()
            app.processEvents()
        from xiaoyu.ai_smoke import run_ai_smoke
        ai_report = run_ai_smoke(app)
        report.update(ok=True, version=__version__, accounts=len(template.accounts), columns=len(template.headers),
                      formats=['xls', 'xlsx'], gui='initialized', missing_account_export='passed',
                      account_name_resolution='passed', account_input_gui='passed', invalid_account_export='passed',
                      flexible_imports=['bank', 'payroll', 'logistics', 'invoice'], material_store='passed',
                      mapping_confirmation='passed', company_isolation='passed', material_export='passed',
                      finance_ai=ai_report)
    except Exception:
        report['error'] = traceback.format_exc()
    Path(report_path).parent.mkdir(parents=True, exist_ok=True)
    Path(report_path).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    return 0 if report['ok'] else 1


def _isolated_probe(function, report_path: str) -> int:
    from xiaoyu import config
    previous = config._TEST_DATA_DIR
    with tempfile.TemporaryDirectory(prefix='xiaoyu-probe-') as directory:
        config._TEST_DATA_DIR = Path(os.environ.get('XIAOYU_UPDATE_TEST_DATA') or directory)
        try:
            return function(report_path)
        finally:
            config._TEST_DATA_DIR = previous


def self_test(report_path: str) -> int:
    return _isolated_probe(_self_test, report_path)


def _update_health_check(report_path: str) -> int:
    from xiaoyu import __version__
    from PySide6.QtWidgets import QApplication
    from xiaoyu.ui import MainWindow
    report = {'ok': False, 'version': __version__}
    try:
        app = QApplication.instance() or QApplication([])
        window = MainWindow()
        app.processEvents()
        window.close()
        window.deleteLater()
        app.processEvents()
        report['ok'] = True
    except Exception:
        report['error'] = traceback.format_exc()
    Path(report_path).parent.mkdir(parents=True, exist_ok=True)
    Path(report_path).write_text(json.dumps(report, ensure_ascii=False), encoding='utf-8')
    return 0 if report['ok'] else 1


def main() -> int:
    parser = argparse.ArgumentParser(description='给小雨的软件 — Excel 生成凭证')
    parser.add_argument('--self-test', metavar='REPORT_JSON', help='运行本地安装包自检并保存报告')
    parser.add_argument('--update-health-check', metavar='REPORT_JSON', help='隔离验证更新后的界面能启动')
    args = parser.parse_args()
    if args.self_test:
        return self_test(args.self_test)
    if args.update_health_check:
        return _isolated_probe(_update_health_check, args.update_health_check)
    from xiaoyu.ui import run_app
    return run_app()


if __name__ == '__main__':
    try:
        sys.exit(main())
    except Exception:
        from xiaoyu.config import data_dir
        log = data_dir() / 'error.log'
        log.write_text(traceback.format_exc(), encoding='utf-8')
        from PySide6.QtWidgets import QApplication, QMessageBox
        app = QApplication.instance() or QApplication([])
        QMessageBox.critical(None, '启动遇到问题', f'软件未能启动，详细信息已保存到：\n{log}')
        sys.exit(1)
