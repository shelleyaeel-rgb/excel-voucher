"""Local, company-scoped import history and review-list export.

The database contains copies of the parsed records and their original rows. It
does not depend on the source workbooks remaining at their original paths.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import asdict
from decimal import Decimal, InvalidOperation
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import tempfile

import openpyxl
from openpyxl.cell.cell import ILLEGAL_CHARACTERS_RE
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

from .config import data_dir
from .imports import ImportRecord, KIND_LABELS, SCHEMAS
from .models import Issue


KINDS = ("bank", "payroll", "logistics", "invoice")
SHEET_NAMES = {"bank": "银行流水", "payroll": "工资", "logistics": "物流", "invoice": "发票"}
NUMBER_FIELDS = {
    "income", "expense", "amount", "balance", "gross", "social", "housing",
    "tax", "other", "net", "quantity", "freight", "total",
}
DUPLICATE_PREFIX = "疑似重复资料："


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _company(value):
    name = str(value).strip()
    if not name:
        raise ValueError("请先填写公司或账套名称。")
    return name


def _record_from_json(payload):
    value = json.loads(payload)
    value["issues"] = [Issue(**issue) for issue in value.get("issues", [])]
    return ImportRecord(**value)


def _source_key(path, sheet):
    return str(Path(path).resolve()).casefold(), str(sheet)


def _issue_origin(source, candidates):
    """Resolve a display source only when it identifies exactly one workbook.

    Absolute file errors are unambiguous. Legacy basename/sheet messages need
    evidence from known source records; two same-named files remain ambiguous.
    """
    source = str(source)
    if " / " not in source and Path(source).is_absolute():
        return str(Path(source).resolve()).casefold(), ""
    matches = set()
    for path, sheet in candidates:
        prefix = f"{Path(path).name} / {sheet}".casefold()
        shown = source.casefold()
        if shown == prefix or shown.startswith(prefix + " / "):
            matches.add((path, sheet))
    return next(iter(matches)) if len(matches) == 1 else (None, None)


class MaterialStore:
    def __init__(self, path=None):
        self.path = Path(path) if path is not None else data_dir() / "materials.sqlite3"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.executescript("""
                CREATE TABLE IF NOT EXISTS companies (
                    name TEXT PRIMARY KEY
                );
                CREATE TABLE IF NOT EXISTS records (
                    company TEXT NOT NULL,
                    record_id TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
                    PRIMARY KEY (company, record_id)
                );
                CREATE TABLE IF NOT EXISTS profiles (
                    company TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    signature TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    PRIMARY KEY (company, kind, signature)
                );
                CREATE TABLE IF NOT EXISTS batch_issues (
                    company TEXT NOT NULL,
                    issue_id TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    source_path TEXT,
                    sheet_name TEXT,
                    PRIMARY KEY (company, issue_id)
                );
            """)
            columns = {row[1] for row in connection.execute("PRAGMA table_info(batch_issues)")}
            for column in ("source_path", "sheet_name"):
                if column not in columns:
                    connection.execute(f"ALTER TABLE batch_issues ADD COLUMN {column} TEXT")

    @contextmanager
    def _connect(self):
        connection = sqlite3.connect(self.path, timeout=30)
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def companies(self):
        with self._connect() as connection:
            return [row[0] for row in connection.execute("SELECT name FROM companies ORDER BY name")]

    def load_records(self, company):
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT payload FROM records WHERE company=? ORDER BY created_at, rowid", (_company(company),)
            ).fetchall()
        records = [_record_from_json(row[0]) for row in rows]
        annotate_duplicates(records)
        return records

    def save_import(self, company, result, *, replace_sources=None, replace_scopes=None):
        """Commit a whole batch; corrected mappings update the same source row.

        Record IDs are based on file content, kind, sheet, and source row. A
        different version of a workbook has different IDs and is retained.
        replace_sources lists successfully parsed (absolute path, sheet) pairs,
        including empty sheets, whose old batch-level notes may be replaced.
        replace_scopes lists complete (file digest, kind, sheet) reads. Source
        rows no longer returned after correcting a header are removed only in
        those exact scopes; other file versions and legacy rows are retained.
        """
        company = _company(company)
        replacements = {_source_key(path, sheet) for path, sheet in (replace_sources or [])}
        replaced_paths = {path for path, _ in replacements}
        scopes = {tuple(scope) for scope in (replace_scopes or []) if len(scope) == 3 and scope[0]}
        incoming_ids = {}
        for record in result.records:
            scope = (getattr(record, "source_digest", ""), record.kind, record.sheet)
            incoming_ids.setdefault(scope, set()).add(record.id)
        counts = {"added": 0, "existing": 0, "updated": 0}
        with self._connect() as connection:
            connection.execute("INSERT OR IGNORE INTO companies(name) VALUES (?)", (company,))
            known_sources = set()
            removed_ids = []
            for (payload,) in connection.execute("SELECT payload FROM records WHERE company=?", (company,)):
                saved = json.loads(payload)
                known_sources.add(_source_key(saved["source_path"], saved["sheet"]))
                scope = (saved.get("source_digest", ""), saved["kind"], saved["sheet"])
                if scope in scopes and saved["id"] not in incoming_ids.get(scope, set()):
                    removed_ids.append(saved["id"])
            if removed_ids:
                connection.executemany("DELETE FROM records WHERE company=? AND record_id=?",
                                       ((company, record_id) for record_id in removed_ids))
                counts["removed"] = len(removed_ids)
            if replacements:
                for issue_id, payload, source_path, sheet_name in connection.execute(
                    "SELECT issue_id,payload,source_path,sheet_name FROM batch_issues WHERE company=?", (company,)
                ).fetchall():
                    if source_path is None:
                        source_path, sheet_name = _issue_origin(json.loads(payload).get("source", ""), known_sources)
                    if (source_path, sheet_name) in replacements or (sheet_name == "" and source_path in replaced_paths):
                        connection.execute("DELETE FROM batch_issues WHERE company=? AND issue_id=?", (company, issue_id))
            current_sources = replacements | known_sources | {
                _source_key(record.source_path, record.sheet) for record in result.records
            }
            for record in result.records:
                if record.kind not in KINDS:
                    raise ValueError(f"无法保存未知资料类型：{record.kind}")
                if not record.id:
                    raise ValueError("资料缺少记录编号，未保存本批资料。")
                payload = _json(asdict(record))
                previous = connection.execute(
                    "SELECT payload FROM records WHERE company=? AND record_id=?", (company, record.id)
                ).fetchone()
                if previous is not None:
                    counts["existing"] += 1
                    if previous[0] != payload:
                        connection.execute(
                            "UPDATE records SET payload=? WHERE company=? AND record_id=?",
                            (payload, company, record.id),
                        )
                        counts["updated"] += 1
                else:
                    connection.execute(
                        "INSERT INTO records(company,record_id,payload) VALUES (?,?,?)",
                        (company, record.id, payload),
                    )
                    counts["added"] += 1
            # Per-record issues are already stored with the record. Keeping them
            # out of batch_issues allows a corrected row to clear its old errors.
            record_issues = {_json(asdict(issue)) for r in result.records for issue in r.issues}
            for issue in result.issues:
                payload = _json(asdict(issue))
                if payload in record_issues:
                    continue
                source_path, sheet_name = _issue_origin(issue.source, current_sources)
                identity = _json([payload, source_path, sheet_name]) if source_path is not None else payload
                issue_id = hashlib.sha256(identity.encode("utf-8")).hexdigest()
                connection.execute(
                    "INSERT OR IGNORE INTO batch_issues(company,issue_id,payload,source_path,sheet_name) VALUES (?,?,?,?,?)",
                    (company, issue_id, payload, source_path, sheet_name),
                )
        return counts

    def delete_records(self, company, ids):
        """Remove selected saved copies only; never touch the source workbooks."""
        company = _company(company)
        removed = 0
        with self._connect() as connection:
            for record_id in set(ids):
                removed += connection.execute(
                    "DELETE FROM records WHERE company=? AND record_id=?", (company, str(record_id))
                ).rowcount
        return removed

    def get_issues(self, company):
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT payload FROM batch_issues WHERE company=? ORDER BY rowid", (_company(company),)
            ).fetchall()
        return [Issue(**json.loads(row[0])) for row in rows]

    def load_profiles(self, company, kind):
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT payload FROM profiles WHERE company=? AND kind=? ORDER BY rowid", (_company(company), kind)
            ).fetchall()
        return [json.loads(row[0]) for row in rows]

    def save_profiles(self, company, profiles):
        company = _company(company)
        with self._connect() as connection:
            connection.execute("INSERT OR IGNORE INTO companies(name) VALUES (?)", (company,))
            for source in profiles:
                profile = dict(source)
                kind, signature = profile.get("kind"), profile.get("signature")
                if kind not in KINDS or not isinstance(signature, str) or not signature:
                    raise ValueError("读取格式缺少资料类型或表头信息，未保存。")
                mapping = profile.get("mapping")
                if not isinstance(mapping, dict) or any(
                    not isinstance(key, str) or type(index) is not int or index < 0
                    for key, index in mapping.items()
                ):
                    raise ValueError("列对应规则无效，未保存读取格式。")
                # Payroll months belong to a particular source, not its format.
                defaults = dict(profile.get("defaults") or {})
                defaults.pop("period", None)
                profile["defaults"] = defaults
                connection.execute(
                    """INSERT INTO profiles(company,kind,signature,payload) VALUES (?,?,?,?)
                    ON CONFLICT(company,kind,signature) DO UPDATE SET payload=excluded.payload""",
                    (company, kind, signature, _json(profile)),
                )


def annotate_duplicates(records):
    """Flag identical business data in different files, preserving every row.

    An invoice number alone is never a duplicate key: one invoice can contain
    many item lines. No candidate is discarded or marked as definitely wrong.
    """
    seen = {}
    added = []
    for record in records:
        record.issues[:] = [i for i in record.issues if not i.message.startswith(DUPLICATE_PREFIX)]
        values = {key: value for key, value in record.values.items() if value not in (None, "")}
        identifiers = {
            "bank": ("date", "income", "expense", "amount"),
            "payroll": ("period", "name"),
            "logistics": ("tracking_no", "business_no"),
            "invoice": ("invoice_no",),
        }.get(record.kind, ())
        if not any(values.get(key) for key in identifiers) or len(values) < 2:
            continue
        key = (record.kind, _json(values))
        previous = seen.get(key)
        if previous is None:
            seen[key] = record
            continue
        if str(Path(previous.source_path).resolve()).casefold() == str(Path(record.source_path).resolve()).casefold():
            continue
        issue = Issue(
            "warning", f"{DUPLICATE_PREFIX}与 {previous.source} 的全部已识别内容相同，请核对是否重复导入；未自动删除。", record.source
        )
        record.issues.append(issue)
        added.append(issue)
    return added


def _write_text(cell, value):
    value = "" if value is None else str(value)
    value = ILLEGAL_CHARACTERS_RE.sub(lambda match: f"\\u{ord(match.group()):04x}", value)
    # Setting the cell type explicitly prevents Excel from interpreting imported
    # strings such as '=HYPERLINK(...)' as formulas.
    cell.value = value
    cell.data_type = "s"
    cell.number_format = "@"


def _write_value(cell, value, key=None):
    if key in NUMBER_FIELDS and value not in (None, ""):
        try:
            number = Decimal(str(value))
            if number.is_finite() and len(number.as_tuple().digits) <= 15:
                cell.value = number
                cell.number_format = "0.00" if key != "quantity" else "0.########"
                return
        except (InvalidOperation, ValueError):
            pass
    _write_text(cell, value)


def _append(sheet, values, keys=None):
    # max_row scans all allocated cells; keep our own append counter so large
    # imports do not turn an otherwise linear export into quadratic work.
    row = getattr(sheet, "_material_next_row", 1)
    if row > 1048576:
        raise ValueError("资料超过单张 Excel 工作表的行数上限，请按月份分批导出。")
    sheet._material_next_row = row + 1
    for column, value in enumerate(values, 1):
        key = keys[column - 1] if keys and column <= len(keys) else None
        _write_value(sheet.cell(row, column), value, key)


def _style_sheet(sheet, widths=None):
    sheet.freeze_panes = "A2"
    sheet.auto_filter.ref = sheet.dimensions
    for cell in sheet[1]:
        cell.fill = PatternFill("solid", fgColor="F4D9E4")
        cell.font = Font(bold=True, color="5B3545")
        cell.alignment = Alignment(vertical="center", wrap_text=True)
    sheet.row_dimensions[1].height = 32
    for column in range(1, sheet.max_column + 1):
        sheet.column_dimensions[get_column_letter(column)].width = (widths or {}).get(column, 20)


def export_materials(records, issues, path):
    """Atomically save all material rows and review notes as a local XLSX file."""
    target = Path(path)
    if target.suffix.lower() != ".xlsx":
        raise ValueError("资料清单请保存为 .xlsx 文件。")
    source_paths = {str(Path(record.source_path).resolve()).casefold() for record in records}
    if str(target.resolve()).casefold() in source_paths:
        raise ValueError("不能覆盖导入的原始表格，请选择其他文件名。")
    if not target.parent.is_dir():
        raise ValueError("保存文件夹不存在，请重新选择保存位置。")
    book = openpyxl.Workbook()
    book.remove(book.active)
    try:
        for kind in KINDS:
            sheet = book.create_sheet(SHEET_NAMES[kind])
            fields = SCHEMAS[kind]
            headers = [field.label for field in fields] + ["资料状态", "来源文件", "工作表", "原始行号", "待核对提示", "记录编号"]
            _append(sheet, headers)
            for record in records:
                if record.kind != kind:
                    continue
                status = "待核对" if record.issues else "已导入"
                if not record.valid:
                    status = "存在错误，未参与凭证生成"
                values = [record.values.get(field.key, "") for field in fields] + [
                    status, record.source_path, record.sheet, record.row,
                    "；".join(issue.message for issue in record.issues), record.id,
                ]
                _append(sheet, values, [field.key for field in fields])
            _style_sheet(sheet, {len(fields) + 2: 44, len(fields) + 5: 65})

        issue_sheet = book.create_sheet("待核对")
        _append(issue_sheet, ["提示级别", "提示内容", "来源"])
        all_issues = list(issues or []) + [issue for record in records for issue in record.issues]
        seen_issues = set()
        for issue in all_issues:
            key = (issue.severity, issue.message, issue.source)
            if key in seen_issues:
                continue
            seen_issues.add(key)
            _append(issue_sheet, [{"error": "错误", "warning": "待核对", "info": "提示"}.get(issue.severity, issue.severity), issue.message, issue.source])
        _style_sheet(issue_sheet, {1: 14, 2: 90, 3: 60})

        raw_sheet = book.create_sheet("原始数据来源")
        _append(raw_sheet, ["记录编号", "资料类型", "来源文件", "工作表", "原始行号", "原始列号", "内容分段", "原始内容（文本）"])
        for record in records:
            for column, value in enumerate(record.raw, 1):
                content = "" if value is None else str(value)
                content = ILLEGAL_CHARACTERS_RE.sub(lambda match: f"\\u{ord(match.group()):04x}", content)
                # Excel cells allow at most 32767 characters. Splitting long
                # original values makes the provenance export lossless.
                chunks = [content[start:start + 30000] for start in range(0, len(content), 30000)] or [""]
                for segment, chunk in enumerate(chunks, 1):
                    _append(raw_sheet, [record.id, KIND_LABELS.get(record.kind, record.kind), record.source_path, record.sheet, record.row, column, segment, chunk])
        _style_sheet(raw_sheet, {1: 22, 2: 18, 3: 44, 4: 24, 5: 12, 6: 12, 7: 12, 8: 70})
        handle, temporary_name = tempfile.mkstemp(prefix=".materials-", suffix=".xlsx", dir=target.parent)
        os.close(handle)
        temporary = Path(temporary_name)
        try:
            book.save(temporary)
            os.replace(temporary, target)
        finally:
            if temporary.exists():
                temporary.unlink()
        return {"path": str(target), "record_count": len(records), "issue_count": len(seen_issues)}
    finally:
        book.close()
