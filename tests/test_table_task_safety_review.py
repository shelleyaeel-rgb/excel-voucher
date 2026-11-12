"""Independent financial-safety checks for a whole-table task, without networking."""
from copy import deepcopy

import pytest
from mapping_test_support import confirm_columns

from test_cloud_tools import (CloudDocumentTools, HEADERS, SHEET, URL, cell, read,
                             tools)


def table_plan(adapter, snapshot):
    snapshot = confirm_columns(adapter, snapshot, [{"target_column": "到账金额", "source_field": "银行金额"}, {"target_column": "状态", "source_field": "处理状态"}])
    result = adapter.execute("wps_prepare_table_update", {
        "snapshot_id": snapshot["snapshot_id"], "key_columns": ["业务单号"],
        "columns": [
            {"target_column": "到账金额", "source_field": "银行金额", "allow_blank": False},
            {"target_column": "状态", "source_field": "处理状态", "allow_blank": False},
        ],
        "updates": [{"key_values": ["001"], "values": [30, "已处理"], "reason": "用户已确认列对应"}],
        "summary": "按业务单号同时更新金额和状态",
    })
    assert result["ok"], result
    return result


def apply_table(adapter, plan):
    return adapter.execute("wps_apply_table_update", {"plan_id": plan["plan_id"]})


@pytest.mark.parametrize("changed_field", ["value", "number_format", "formula"])
def test_second_column_does_not_adopt_concurrent_target_edits(tmp_path, changed_field):
    adapter, fake = tools(tmp_path)
    plan = table_plan(adapter, read(adapter))

    def edit_after_first(transport, payload):
        if payload["target_column"] != 2:
            return
        target = transport.at(3, 3)
        if changed_field == "value":
            target["value"] = "人工校正"
        elif changed_field == "number_format":
            target["number_format"] = "@"
        else:
            target.update(formula="=1+1", has_formula=True)

    fake.after_apply = edit_after_first
    result = apply_table(adapter, plan)
    assert result["status"] != "completed" and not result["ok"], result
    assert fake.applied_requests == 1
    assert fake.at(3, 2)["value"] == 30
    assert fake.at(3, 3)["value"] == ("人工校正" if changed_field == "value" else "已到账")


def test_explicit_formula_filter_is_not_silently_refreshed_between_columns(tmp_path):
    adapter, fake = tools(tmp_path)
    fake.rows[2].append(cell(4, "可更新"))
    for row in (3, 4):
        fake.rows[row].append(cell(4, True, formula=True))
    snapshot = read(adapter, columns=HEADERS + ["可更新"])
    plan = table_plan(adapter, snapshot)

    def filter_changes(transport, payload):
        if payload["target_column"] == 2:
            transport.at(3, 4)["value"] = False

    fake.after_apply = filter_changes
    result = apply_table(adapter, plan)
    assert result["status"] != "completed" and not result["ok"], result
    assert fake.applied_requests == 1
    assert fake.at(3, 3)["value"] == "已到账"
    assert fake.at(3, 4)["formula"] == "=1+1"


def test_final_overall_check_detects_earlier_column_changed_during_later_write(tmp_path):
    adapter, fake = tools(tmp_path)
    plan = table_plan(adapter, read(adapter))

    def edit_earlier_column(transport, payload):
        if payload["target_column"] == 3:
            transport.at(3, 2)["value"] = 777

    fake.after_apply = edit_earlier_column
    result = apply_table(adapter, plan)
    assert fake.applied_requests == 2
    assert result["status"] != "completed" and not result["ok"], result
    assert fake.at(3, 2)["value"] == 777
    assert fake.at(3, 3)["value"] == "已处理"
    apply_table(adapter, plan)
    assert fake.applied_requests == 2


def test_business_key_relocation_between_columns_preserves_other_record(tmp_path):
    adapter, fake = tools(tmp_path)
    untouched = deepcopy(fake.rows[4])
    plan = table_plan(adapter, read(adapter))

    def reorder_after_first(transport, payload):
        if payload["target_column"] == 2:
            transport.rows[3], transport.rows[4] = transport.rows[4], transport.rows[3]

    fake.after_apply = reorder_after_first
    result = apply_table(adapter, plan)
    assert result["ok"] and result["status"] == "completed", result
    assert fake.rows[3] == untouched
    assert [fake.at(4, col)["value"] for col in (1, 2, 3)] == ["001", 30, "已处理"]
    assert fake.applied_requests == 2


def test_historical_prepared_group_check_cannot_restore_write_authority(tmp_path):
    adapter, fake = tools(tmp_path)
    plan = table_plan(adapter, read(adapter))
    reopened = CloudDocumentTools(list(adapter.bindings.values()), allow_write=True,
        journal_dir=adapter.journal_dir, client_factory=fake.factory)
    checked = reopened.execute("wps_check_task", {"task_id": plan["plan_id"]})
    assert "error" not in checked, checked
    result = apply_table(reopened, plan)
    assert not result["ok"]
    assert fake.applied_requests == 0


def test_completed_group_recheck_remains_read_only_after_user_clears_cells(tmp_path):
    adapter, fake = tools(tmp_path)
    plan = table_plan(adapter, read(adapter))
    result = apply_table(adapter, plan)
    assert result["ok"], result
    fake.at(3, 2)["value"] = ""
    reopened = CloudDocumentTools(list(adapter.bindings.values()), allow_write=True,
        journal_dir=adapter.journal_dir, client_factory=fake.factory)
    checked = reopened.execute("wps_check_task", {"task_id": plan["plan_id"]})
    assert not checked["ok"] and checked["current_check"]["changed"] == 1, checked
    assert checked["status"] == "completed", checked
    assert fake.at(3, 2)["value"] == "" and fake.applied_requests == 2
