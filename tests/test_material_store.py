"""Company isolation, recoverable local history, and safe material exports."""
from dataclasses import replace
from decimal import Decimal
import json
import sqlite3

import openpyxl
import pytest

from xiaoyu.imports import ImportRecord, ImportResult, SCHEMAS
from xiaoyu.material_store import MaterialStore, annotate_duplicates, export_materials
from xiaoyu.models import Issue


def record(identifier="source-row-1", kind="invoice", source="source.xlsx", values=None, issues=None, raw=None):
    return ImportRecord(
        id=identifier, kind=kind,
        values=values if values is not None else {"invoice_no": "0001234567890123456789", "item": "商品甲", "total": "113.00"},
        raw=raw if raw is not None else ["0001234567890123456789", "商品甲", "113.00"],
        source_path=source, sheet="明细", row=2, issues=issues or [],
    )


def test_company_scoped_history_survives_source_removal_and_reopening(tmp_path):
    path = tmp_path / "资料.sqlite3"
    store = MaterialStore(path)
    issue = Issue("error", "日期无效", "原表 / 第2行")
    first = record(issues=[issue], source=str(tmp_path / "已移走.xlsx"))
    counts = store.save_import("甲公司", ImportResult([first], [issue]))
    assert counts == {"added": 1, "existing": 0, "updated": 0}
    assert store.load_records("乙公司") == []
    store.save_import("乙公司", ImportResult([replace(first, values={"invoice_no": "另一家公司"})], []))

    reopened = MaterialStore(path)
    restored = reopened.load_records("甲公司")
    assert restored[0].values["invoice_no"] == "0001234567890123456789"
    assert restored[0].raw == first.raw
    assert not restored[0].valid
    assert restored[0].issues == [issue]
    assert reopened.get_issues("甲公司") == []  # The row owns its correctable error.
    assert set(reopened.companies()) == {"甲公司", "乙公司"}


def test_reimport_is_idempotent_and_corrected_mapping_updates_same_row(tmp_path):
    store = MaterialStore(tmp_path / "资料.sqlite3")
    bad = record(issues=[Issue("error", "错误对应", "原表 / 第2行")])
    store.save_import("甲", ImportResult([bad], []))
    repeated = store.save_import("甲", ImportResult([bad], []))
    assert repeated == {"added": 0, "existing": 1, "updated": 0}
    corrected = replace(bad, values={**bad.values, "item": "修正后商品"}, issues=[])
    changed = store.save_import("甲", ImportResult([corrected], []))
    assert changed == {"added": 0, "existing": 1, "updated": 1}
    rows = store.load_records("甲")
    assert len(rows) == 1 and rows[0].valid
    assert rows[0].values["item"] == "修正后商品"


def test_corrected_header_removes_rows_no_longer_in_complete_source_scope(tmp_path):
    store = MaterialStore(tmp_path / "资料.sqlite3")
    old_header = replace(record(identifier="old-header"), source_digest="file-v1", row=2)
    detail = replace(record(identifier="detail"), source_digest="file-v1", row=3)
    store.save_import("甲", ImportResult([old_header, detail], []))
    corrected = replace(detail, values={**detail.values, "item": "重新对应的商品"})
    result = store.save_import("甲", ImportResult([corrected], []),
                               replace_scopes=[("file-v1", "invoice", "明细")])
    assert result == {"added": 0, "existing": 1, "updated": 1, "removed": 1}
    assert [row.id for row in store.load_records("甲")] == ["detail"]


def test_empty_scope_reimport_only_clears_same_company_version_kind_and_sheet(tmp_path):
    store = MaterialStore(tmp_path / "资料.sqlite3")
    current = replace(record(identifier="current"), source_digest="file-v1")
    other_version = replace(current, id="version-2", source_digest="file-v2")
    other_kind = replace(current, id="other-kind", kind="logistics")
    other_sheet = replace(current, id="other-sheet", sheet="另一个表")
    legacy = replace(current, id="legacy", source_digest="")
    store.save_import("甲", ImportResult([current, other_version, other_kind, other_sheet, legacy], []))
    store.save_import("乙", ImportResult([current], []))
    result = store.save_import("甲", ImportResult(), replace_scopes=[("file-v1", "invoice", "明细")])
    assert result["removed"] == 1
    assert {row.id for row in store.load_records("甲")} == {"version-2", "other-kind", "other-sheet", "legacy"}
    assert [row.id for row in store.load_records("乙")] == ["current"]


def test_scope_row_removal_rolls_back_if_reimport_save_fails(tmp_path):
    store = MaterialStore(tmp_path / "资料.sqlite3")
    old = replace(record(identifier="old"), source_digest="file-v1")
    store.save_import("甲", ImportResult([old], []))
    invalid = replace(old, id="new", raw=[object()])
    with pytest.raises(TypeError):
        store.save_import("甲", ImportResult([invalid], []), replace_scopes=[("file-v1", "invoice", "明细")])
    assert [row.id for row in store.load_records("甲")] == ["old"]


def test_failed_batch_rolls_back_all_rows_and_company_registration(tmp_path):
    store = MaterialStore(tmp_path / "资料.sqlite3")
    good = record()
    unserializable = record(identifier="broken", raw=[object()])
    with pytest.raises(TypeError):
        store.save_import("失败公司", ImportResult([good, unserializable], []))
    assert store.load_records("失败公司") == []
    assert "失败公司" not in store.companies()


def test_remove_only_selected_company_copy_and_keep_original_file(tmp_path):
    source = tmp_path / "原始.xlsx"
    source.write_bytes(b"keep me")
    store = MaterialStore(tmp_path / "资料.sqlite3")
    row = record(source=str(source))
    store.save_import("甲", ImportResult([row], []))
    store.save_import("乙", ImportResult([row], []))
    assert store.delete_records("甲", [row.id, row.id, "不存在"]) == 1
    assert store.load_records("甲") == []
    assert len(store.load_records("乙")) == 1
    assert source.read_bytes() == b"keep me"


def test_batch_messages_persist_once(tmp_path):
    store = MaterialStore(tmp_path / "资料.sqlite3")
    issue = Issue("info", "跳过说明工作表", "文件.xlsx / 说明")
    for _ in range(2):
        store.save_import("甲", ImportResult([], [issue]))
    assert store.get_issues("甲") == [issue]


def test_successful_reimport_replaces_only_same_company_sheet_notes_even_without_rows(tmp_path):
    store = MaterialStore(tmp_path / "资料.sqlite3")
    path = str(tmp_path / "工资.xlsx")
    old = Issue("warning", "旧工资合计不符", "工资.xlsx / 工资 / 第8行")
    other_sheet = Issue("warning", "另一个工作表的提示", "工资.xlsx / 工资补充 / 第8行")
    other_file = Issue("error", "另一个文件的提示", "旧工资.xlsx / 工资 / 第8行")
    sources = [(path, "工资"), (path, "工资补充"), (str(tmp_path / "旧工资.xlsx"), "工资")]
    store.save_import("甲", ImportResult([], [old, other_sheet, other_file]), replace_sources=sources)
    store.save_import("乙", ImportResult([], [old]), replace_sources=[(path, "工资")])
    new = Issue("info", "本次读取结果", "工资.xlsx / 工资 / 第9行")
    store.save_import("甲", ImportResult([], [new]), replace_sources=[(path, "工资")])
    assert store.get_issues("甲") == [other_sheet, other_file, new]
    assert store.get_issues("乙") == [old]


def test_successful_parse_clears_prior_absolute_file_error_but_not_other_paths(tmp_path):
    store = MaterialStore(tmp_path / "资料.sqlite3")
    fixed = str(tmp_path / "a" / "文件.xlsx")
    other = str(tmp_path / "b" / "文件.xlsx")
    old = Issue("error", "文件损坏", fixed)
    unrelated = Issue("error", "文件损坏", other)
    store.save_import("甲", ImportResult([], [old, unrelated]))
    store.save_import("甲", ImportResult(), replace_sources=[(fixed, "明细")])
    assert store.get_issues("甲") == [unrelated]


def test_ambiguous_basename_notes_are_preserved(tmp_path):
    store = MaterialStore(tmp_path / "资料.sqlite3")
    one = record(identifier="one", source=str(tmp_path / "a" / "文件.xlsx"))
    two = record(identifier="two", source=str(tmp_path / "b" / "文件.xlsx"))
    ambiguous = Issue("warning", "无法确定来自哪份同名文件", "文件.xlsx / 明细 / 第8行")
    store.save_import("甲", ImportResult([one, two], [ambiguous]),
                      replace_sources=[(one.source_path, one.sheet), (two.source_path, two.sheet)])
    store.save_import("甲", ImportResult([one], []), replace_sources=[(one.source_path, one.sheet)])
    assert store.get_issues("甲") == [ambiguous]


def test_failed_reimport_rolls_back_removal_of_previous_notes(tmp_path):
    store = MaterialStore(tmp_path / "资料.sqlite3")
    path = str(tmp_path / "工资.xlsx")
    old = Issue("warning", "工资合计不符", "工资.xlsx / 明细 / 第8行")
    store.save_import("甲", ImportResult([], [old]), replace_sources=[(path, "明细")])
    bad = record(source=path, raw=[object()])
    with pytest.raises(TypeError):
        store.save_import("甲", ImportResult([bad], []), replace_sources=[(path, "明细")])
    assert store.get_issues("甲") == [old]


def test_legacy_database_migrates_and_preserves_unresolved_notes(tmp_path):
    path = tmp_path / "旧资料.sqlite3"
    old = Issue("warning", "旧提示来源无法判断", "工资.xlsx / 明细 / 第8行")
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE batch_issues(company TEXT, issue_id TEXT, payload TEXT, PRIMARY KEY(company,issue_id))")
        connection.execute("INSERT INTO batch_issues VALUES (?,?,?)", ("甲", "legacy", json.dumps(old.__dict__, ensure_ascii=False)))
    store = MaterialStore(path)
    store.save_import("甲", ImportResult(), replace_sources=[(str(tmp_path / "工资.xlsx"), "明细")])
    assert store.get_issues("甲") == [old]


def test_formats_are_scoped_and_replace_exact_signature_without_saving_payroll_month(tmp_path):
    store = MaterialStore(tmp_path / "资料.sqlite3")
    profile = {"kind": "payroll", "signature": "同一表头", "name": "工资格式", "mapping": {"name": 0, "gross": 1, "net": 2}, "defaults": {"period": "2026-10"}}
    store.save_profiles("甲", [profile])
    restored = store.load_profiles("甲", "payroll")
    assert restored[0]["defaults"] == {}
    assert profile["defaults"]["period"] == "2026-10"  # Caller data is untouched.
    assert store.load_profiles("甲", "bank") == []
    assert store.load_profiles("乙", "payroll") == []
    store.save_profiles("甲", [{**profile, "name": "修正版", "mapping": {"name": 1, "gross": 2, "net": 3}}])
    assert len(store.load_profiles("甲", "payroll")) == 1
    assert store.load_profiles("甲", "payroll")[0]["mapping"]["name"] == 1
    store.save_profiles("甲", [{**profile, "signature": "新的表头"}])
    assert len(store.load_profiles("甲", "payroll")) == 2


def test_bad_format_rolls_back_whole_profile_update(tmp_path):
    store = MaterialStore(tmp_path / "资料.sqlite3")
    good = {"kind": "bank", "signature": "头", "mapping": {"date": 0}, "defaults": {}}
    bad = {**good, "signature": "坏", "mapping": {"date": -1}}
    with pytest.raises(ValueError, match="列对应规则"):
        store.save_profiles("甲", [good, bad])
    assert store.load_profiles("甲", "bank") == []


def test_invoice_lines_are_retained_and_only_full_cross_file_matches_warn():
    first = record(source="a.xlsx")
    other_line = record(identifier="line-2", source="a.xlsx", values={**first.values, "item": "商品乙"})
    same_number_other_item = record(identifier="file-2", source="b.xlsx", values={**first.values, "item": "商品丙"})
    repeated = record(identifier="file-3", source="c.xlsx")
    records = [first, other_line, same_number_other_item, repeated]
    issues = annotate_duplicates(records)
    assert len(records) == 4
    assert len(issues) == 1
    assert repeated.issues == issues
    assert first.issues == other_line.issues == same_number_other_item.issues == []
    assert repeated.valid  # A possible duplicate is not proof of an invalid row.
    annotate_duplicates(records)
    assert len(repeated.issues) == 1
    annotate_duplicates([repeated])
    assert repeated.issues == []


def test_export_keeps_invoice_lines_identifiers_numbers_provenance_and_formula_text(tmp_path):
    first = record(values={"invoice_no": "0001234567890123456789", "item": '=HYPERLINK("https://example.invalid")', "total": "113.00"}, raw=["0001234567890123456789", "=1+1", "长" * 33000])
    second = record(identifier="line-2", values={**first.values, "item": "商品乙", "total": "226.00"})
    warning = Issue("warning", "请核对", first.source)
    first.issues.append(warning)
    target = tmp_path / "资料.xlsx"
    result = export_materials([first, second], [warning], target)
    assert result["record_count"] == 2 and result["issue_count"] == 1
    book = openpyxl.load_workbook(target, data_only=False)
    try:
        assert book.sheetnames == ["银行流水", "工资", "物流", "发票", "待核对", "原始数据来源"]
        sheet = book["发票"]
        columns = {field.key: index + 1 for index, field in enumerate(SCHEMAS["invoice"])}
        assert sheet.max_row == 3
        assert sheet.cell(2, columns["invoice_no"]).value == "0001234567890123456789"
        assert sheet.cell(2, columns["invoice_no"]).data_type == "s"
        assert sheet.cell(2, columns["item"]).value.startswith("=HYPERLINK")
        assert sheet.cell(2, columns["item"]).data_type == "s"
        assert Decimal(str(sheet.cell(2, columns["total"]).value)) == Decimal("113.00")
        assert book["待核对"].max_row == 2
        raw_rows = list(book["原始数据来源"].iter_rows(min_row=2, values_only=True))
        long_chunks = [row[7] for row in raw_rows if row[0] == first.id and str(row[5]) == "3"]
        assert "".join(long_chunks) == "长" * 33000
        for sheet in book:
            assert all(cell.data_type != "f" for row in sheet for cell in row)
    finally:
        book.close()


def test_export_never_overwrites_an_input_workbook(tmp_path):
    target = tmp_path / "输入.xlsx"
    target.write_bytes(b"original")
    with pytest.raises(ValueError, match="不能覆盖"):
        export_materials([record(source=str(target))], [], target)
    assert target.read_bytes() == b"original"


def test_failed_export_preserves_existing_output_and_removes_staging(tmp_path, monkeypatch):
    target = tmp_path / "清单.xlsx"
    target.write_bytes(b"previous usable output")
    def fail_save(*args, **kwargs):
        raise OSError("磁盘写入失败")
    monkeypatch.setattr(openpyxl.Workbook, "save", fail_save)
    with pytest.raises(OSError, match="磁盘写入失败"):
        export_materials([record()], [], target)
    assert target.read_bytes() == b"previous usable output"
    assert list(tmp_path.glob(".materials-*")) == []
