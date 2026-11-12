"""Synthetic bank-to-template workflow; no real workbook or account is opened."""
from copy import deepcopy

from test_cloud_tools import SHEET, URL, cell, merged_date_tools
from xiaoyu.cloud_mapping import mapping_answer_text


def test_sixteen_bank_transactions_complete_as_one_five_column_task(tmp_path):
    adapter, fake = merged_date_tools(tmp_path)
    fake.rows[2].extend([cell(5, "对方账户"), cell(6, "收入"), cell(7, "支出"), cell(8, "余额")])
    for row in range(3, 20):
        fake.rows[row] = [cell(1, f"{row - 2:03d}"),
            dict(cell(2, formula=True), formula=f'=MONTH(C{row})'),
            dict(cell(3), number_format="yyyy-mm-dd", date_system="1900"),
            cell(4), cell(5), cell(6), cell(7),
            dict(cell(8, 1000, formula=True), formula=f'=1000+F{row}-G{row}')]
    original = deepcopy(fake.rows)
    names = ["日期", "对方账户", "收入", "支出", "备注"]
    columns = [3, 5, 6, 7, 4]
    updates = [{"key_values": [f"{index + 1:03d}"],
        "values": [46332 + index, f"合成对方{index + 1}", 10 if index < 13 else None,
                   None if index < 13 else 5, f"合成摘要{index + 1}"],
        "reason": "按已确认规则填写模板预留序号"} for index in range(16)]

    def recalculate(transport, payload):
        for row in range(3, 19):
            if transport.at(row, 3)["value"] != "":
                transport.at(row, 2)["value"] = 11
            income, expense = transport.at(row, 6)["value"], transport.at(row, 7)["value"]
            transport.at(row, 8)["value"] = 1000 + (income or 0) - (expense or 0)

    fake.after_apply = recalculate
    snapshot = adapter.execute("wps_read_table", {"document_url": URL, "sheet": SHEET,
        "columns": ["业务单号"] + names, "header_row": None, "start_row": None, "end_row": None,
        "column_overrides": [{"name": "日期", "column": 3}]})
    assert snapshot["ok"], snapshot
    sources = ["交易日期", "对方户名", "收支金额", "收支金额", "摘要"]
    question = "请逐行选择银行流水来源列"
    mapping = adapter.execute("wps_confirm_mapping", {"document_url": URL, "sheet": SHEET,
        "question": question, "mappings": [{"target_column": name,
            "source_options": ["交易日期", "对方户名", "对方账号", "收支金额", "摘要", "余额"],
            "selected_source": None, "note": "按交易时间填写，正数收入、负数转正填写支出，保留公式。"}
            for name in names]})
    assert mapping["ok"] and mapping["status"] == "needs_user_input", mapping
    assert all(item["selected_source"] is None for item in mapping["mappings"])
    assert fake.rows == original
    adapter.begin_turn(f"针对“{question}”：" + mapping_answer_text(SHEET, list(zip(names, sources))))
    plan = adapter.execute("wps_prepare_table_update", {"snapshot_id": snapshot["snapshot_id"],
        "key_columns": ["业务单号"], "columns": [{"target_column": name,
            "source_field": source, "allow_blank": False} for name, source in zip(names, sources)],
        "updates": updates, "summary": "一次填写16笔银行流水，正数收入、负数支出，保留公式。"})
    assert plan["ok"] and plan["planned"] == 64, plan
    assert fake.rows == original
    result = adapter.execute("wps_apply_table_update", {"plan_id": plan["plan_id"]})
    assert result["ok"] and result["status"] == "completed", result
    assert result["updated"] == 64 and result["skipped"] == 16
    assert result["conflicts"] == result["unknown"] == 0
    assert len(result["columns"]) == 5 and len(adapter.results) == 1
    assert result["current_check"]["matched"] == 64
    for row, update in zip(range(3, 19), updates):
        for column, value in zip(columns, update["values"]):
            assert fake.at(row, column)["value"] == ("" if value is None else value)
        for column in (2, 8):
            assert fake.at(row, column)["formula"] == original[row][column - 1]["formula"]
            assert fake.at(row, column)["has_formula"]
        assert fake.at(row, 3)["number_format"] == "yyyy-mm-dd"
    assert fake.rows[19] == original[19]
    assert fake.rows[2] == original[2]
    count = fake.applied_requests
    adapter.execute("wps_apply_table_update", {"plan_id": plan["plan_id"]})
    assert fake.applied_requests == count
