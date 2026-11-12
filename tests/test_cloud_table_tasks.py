"""Whole-task execution, durable recovery and native mapping boundaries."""
from copy import deepcopy
import json
import io
import threading

import pytest

from test_cloud_tools import CloudDocumentTools, URL, cell, read, tools, merged_date_tools, read_merged_date
from test_table_task_safety_review import table_plan, apply_table
from xiaoyu.cloud_mapping import mapping_answer_text
from xiaoyu.ai_service import FinanceAIClient
from mapping_test_support import confirm_columns
from xiaoyu.ai_settings import AISettings


def reopen(adapter, fake, *, write=True):
    return CloudDocumentTools(list(adapter.bindings.values()), allow_write=write,
        journal_dir=adapter.journal_dir, client_factory=fake.factory)


def test_whole_task_has_one_result_and_internal_children_cannot_be_applied(tmp_path):
    adapter, fake = tools(tmp_path)
    plan = table_plan(adapter, read(adapter))
    record = adapter.plans[plan["plan_id"]]
    assert len(adapter.results) == 1 and len(plan["columns"]) == 2
    child = adapter.execute("wps_apply_update", {"plan_id": record["children"][0]})
    assert not child["ok"] and fake.applied_requests == 0
    result = apply_table(adapter, plan)
    assert result["ok"] and result["updated"] == 2 and result["record_count"] == 1
    assert result["current_check"]["matched"] == result["final_check"]["matched"] == 2
    assert result["last_action"] == "apply"
    assert len(adapter.results) == 1
    apply_table(adapter, plan)
    assert fake.applied_requests == 2
    checked = reopen(adapter, fake).execute("wps_check_task", {"task_id": plan["plan_id"]})
    assert checked["ok"] and checked["last_action"] == "check"
    assert checked["final_check"] == result["final_check"] and fake.applied_requests == 2


@pytest.mark.parametrize("mode", ["preview", "pending_question", "duplicate_source"])
def test_whole_task_never_writes_without_permission_or_clear_rules(tmp_path, mode):
    adapter, fake = tools(tmp_path, write=mode != "preview")
    plan = table_plan(adapter, read(adapter))
    if mode == "pending_question":
        adapter.execute("wps_ask_clarification", {"question": "是否排除作废？", "options": ["排除", "保留"]})
    elif mode == "duplicate_source":
        adapter.plans[plan["plan_id"]]["needs_clarification"] = True
    result = apply_table(adapter, plan)
    assert not result["ok"] and fake.applied_requests == 0


def test_blank_source_is_intentional_skip_zero_is_written_and_task_completes(tmp_path):
    adapter, fake = tools(tmp_path)
    snapshot = read(adapter)
    confirm_columns(adapter, snapshot, [{"target_column": "到账金额", "source_field": "金额"}, {"target_column": "状态", "source_field": "状态"}])
    plan = adapter.execute("wps_prepare_table_update", {
        "snapshot_id": snapshot["snapshot_id"], "key_columns": ["业务单号"],
        "columns": [{"target_column": "到账金额", "source_field": "金额", "allow_blank": False},
                    {"target_column": "状态", "source_field": "状态", "allow_blank": False}],
        "updates": [{"key_values": ["001"], "values": [0, None], "reason": "空白按规则保留"}], "summary": "两列按规则更新"})
    result = apply_table(adapter, plan)
    assert result["ok"] and result["status"] == "completed" and result["updated"] == result["skipped"] == 1
    assert fake.at(3, 2)["value"] == 0 and fake.at(3, 3)["value"] == "已到账"
    assert fake.applied_requests == 1
    assert result["columns"][1]["status"] == "completed"


def test_formula_target_is_preserved_and_never_reported_fully_completed(tmp_path):
    adapter, fake = tools(tmp_path)
    fake.at(3, 3).update(formula="=1+1", has_formula=True)
    plan = table_plan(adapter, read(adapter))
    result = apply_table(adapter, plan)
    assert not result["ok"] and result["status"] == "partial"
    assert result["columns"][1]["status"] == "partial"
    assert fake.applied_requests == 1 and fake.at(3, 3)["formula"] == "=1+1"


def test_implicit_month_formula_cache_refresh_is_permitted_after_own_date_write(tmp_path):
    adapter, fake = merged_date_tools(tmp_path)
    snapshot = read_merged_date(adapter)
    confirm_columns(adapter, snapshot, [{"target_column": "日期", "source_field": "交易日期"}, {"target_column": "备注", "source_field": "摘要"}])
    plan = adapter.execute("wps_prepare_table_update", {
        "snapshot_id": snapshot["snapshot_id"], "key_columns": ["业务单号"],
        "columns": [{"target_column": "日期", "source_field": "交易日期", "allow_blank": False},
                    {"target_column": "备注", "source_field": "摘要", "allow_blank": False}],
        "updates": [{"key_values": ["001"], "values": [46332, "测试"], "reason": "已明确输入日期列"}], "summary": "日期和备注一起填"})
    fake.after_apply = lambda transport, payload: transport.at(3, 2).update(value=11)
    result = apply_table(adapter, plan)
    assert result["ok"] and fake.applied_requests == 2
    assert fake.at(3, 2)["has_formula"] and fake.at(3, 3)["value"] == 46332


def test_merged_header_profile_distinguishes_formula_from_blank_date_input(tmp_path):
    adapter, fake = merged_date_tools(tmp_path)
    wrong = read_merged_date(adapter, column_overrides=[{"name": "日期", "column": 2}])
    profile = next(p for p in wrong["column_profiles"] if p["name"] == "日期")
    assert profile["formula_only"] and profile["writable_cells"] == profile["blank_input_cells"] == 0
    right = read_merged_date(adapter)
    profile = next(p for p in right["column_profiles"] if p["name"] == "日期")
    assert not profile["formula_only"] and profile["writable_cells"] == profile["blank_input_cells"] == 2


def test_airscript_constant_formula_representation_follows_verified_value(tmp_path):
    adapter, fake = tools(tmp_path)
    for row in fake.rows.values():
        for target in row:
            target["formula"] = target["value"]
    fake.after_apply = lambda transport, payload: transport.at(3, payload["target_column"]).update(
        formula=transport.at(3, payload["target_column"])["value"])
    result = apply_table(adapter, table_plan(adapter, read(adapter)))
    assert result["ok"] and fake.applied_requests == 2


def test_date_system_change_invalidates_whole_plan_before_any_write(tmp_path):
    adapter, fake = tools(tmp_path)
    fake.at(3, 2).update(date_system="1900", number_format="yyyy-mm-dd")
    plan = table_plan(adapter, read(adapter))
    fake.at(3, 2)["date_system"] = "1904"
    result = apply_table(adapter, plan)
    assert not result["ok"] and fake.applied_requests == 0


def test_legacy_date_without_epoch_preserves_existing_protocol(tmp_path):
    adapter, fake = tools(tmp_path)
    fake.at(3, 2)["number_format"] = "yyyy-mm-dd"
    plan = table_plan(adapter, read(adapter))
    result = apply_table(adapter, plan)
    assert result["ok"] and fake.applied_requests == 2


def test_personal_account_date_requires_verified_epoch_before_any_write(tmp_path):
    from xiaoyu.wps_service import WpsBinding
    adapter, fake = tools(tmp_path)
    adapter.bindings[URL] = WpsBinding("example-doc", "测试表", URL, workbook_name="模版.xlsx",
        provider="skillhub", account_id="test-account", file_id="test-file")
    fake.at(3, 2)["number_format"] = "yyyy-mm-dd"
    plan = table_plan(adapter, read(adapter))
    result = apply_table(adapter, plan)
    assert not result["ok"] and fake.applied_requests == 0
    assert "日期" in result["error"]


def test_unchanged_target_format_must_survive_later_column_write(tmp_path):
    adapter, fake = tools(tmp_path)
    fake.at(3, 2)["value"] = 30
    plan = table_plan(adapter, read(adapter))
    fake.after_apply = lambda transport, payload: transport.at(3, 2).update(number_format="0.00")
    result = apply_table(adapter, plan)
    assert not result["ok"] and result["status"] == "partial"
    assert result["current_check"]["changed"] == 1 and fake.applied_requests == 1


def test_uncertain_first_column_stops_following_and_reopen_only_reconciles(tmp_path):
    adapter, fake = tools(tmp_path)
    plan = table_plan(adapter, read(adapter))
    fake.fail_read_after_apply = True
    result = apply_table(adapter, plan)
    assert result["status"] == "uncertain" and result["unknown"] == 1
    assert result["pending"] == 1 and fake.applied_requests == 1
    fake.fail_read_after_apply = False
    fresh = reopen(adapter, fake)
    other_plan = table_plan(fresh, read(fresh))
    blocked = apply_table(fresh, other_plan)
    assert not blocked["ok"] and fake.applied_requests == 1
    checked = fresh.execute("wps_check_task", {"task_id": plan["plan_id"]})
    assert checked["status"] == "partial" and checked["unknown"] == 0 and checked["pending"] == 1
    assert fake.applied_requests == 1 and fake.at(3, 3)["value"] == "已到账"
    assert not apply_table(fresh, plan)["ok"] and fake.applied_requests == 1


def test_final_read_failure_can_be_reconciled_without_stale_error_or_rewrite(tmp_path, monkeypatch):
    adapter, fake = tools(tmp_path)
    plan = table_plan(adapter, read(adapter))
    original = adapter._table_final_check
    def fail(*args, **kwargs):
        raise ValueError("最终读取临时失败")
    monkeypatch.setattr(adapter, "_table_final_check", fail)
    result = apply_table(adapter, plan)
    assert not result["ok"] and result["final_check"]["failed"] and "error" in result
    monkeypatch.setattr(adapter, "_table_final_check", original)
    checked = adapter.execute("wps_check_task", {"task_id": plan["plan_id"]})
    assert checked["ok"] and "error" not in checked and fake.applied_requests == 2
    assert checked["final_check"]["failed"] and not checked["current_check"]["failed"]


def test_cancel_after_first_column_records_remaining_without_replay(tmp_path):
    adapter, fake = tools(tmp_path)
    plan = table_plan(adapter, read(adapter))
    cancel = threading.Event()
    fake.after_apply = lambda transport, payload: cancel.set()
    result = adapter.execute("wps_apply_table_update", {"plan_id": plan["plan_id"]}, cancel=cancel)
    assert result["status"] == "cancelled" and result["updated"] == result["pending"] == 1
    assert fake.applied_requests == 1
    checked = reopen(adapter, fake).execute("wps_check_task", {"task_id": plan["plan_id"]})
    assert checked["status"] == "partial" and fake.applied_requests == 1


def test_duplicate_source_keys_block_all_columns_before_first_write(tmp_path):
    adapter, fake = tools(tmp_path)
    snapshot = read(adapter)
    confirm_columns(adapter, snapshot, [{"target_column": "到账金额", "source_field": "金额"}])
    plan = adapter.execute("wps_prepare_table_update", {
        "snapshot_id": snapshot["snapshot_id"], "key_columns": ["业务单号"],
        "columns": [{"target_column": "到账金额", "source_field": "金额", "allow_blank": False}],
        "updates": [{"key_values": ["001"], "values": [i], "reason": "重复测试"} for i in (30, 40)], "summary": "不能重复"})
    assert plan["needs_clarification"]
    result = apply_table(adapter, plan)
    assert not result["ok"] and fake.applied_requests == 0


def test_crash_before_first_column_is_closed_by_read_only_check(tmp_path):
    adapter, fake = tools(tmp_path)
    plan = table_plan(adapter, read(adapter))
    record = adapter.plans[plan["plan_id"]]
    record["status"] = "running"
    adapter._save(record)
    checked = reopen(adapter, fake).execute("wps_check_task", {"task_id": plan["plan_id"]})
    assert checked["status"] == "partial" and checked["pending"] == 2
    assert not checked["unknown"] and fake.applied_requests == 0


def test_child_record_identity_mismatch_prevents_group_check(tmp_path):
    adapter, fake = tools(tmp_path)
    plan = table_plan(adapter, read(adapter))
    parent = adapter.plans[plan["plan_id"]]
    path = adapter._path(parent["children"][0])
    child = json.loads(path.read_text(encoding="utf-8"))
    child["document_identity"]["file_id"] = "wrong-document"
    path.write_text(json.dumps(child), encoding="utf-8")
    result = reopen(adapter, fake).execute("wps_check_task", {"task_id": plan["plan_id"]})
    assert not result["ok"] and fake.applied_requests == 0


@pytest.mark.parametrize("write,force_preview,expected", [(True, False, True), (False, False, False), (True, True, False)])
def test_mapping_form_answers_preserve_only_existing_authority(tmp_path, write, force_preview, expected):
    adapter, fake = tools(tmp_path, write=write)
    read(adapter)
    question = "请一次确认这些列对应"
    result = adapter.execute("wps_confirm_mapping", {"document_url": URL, "sheet": "汇总", "question": question,
        "mappings": [{"target_column": "到账金额", "source_options": ["不更新金额", "金额"], "selected_source": None, "note": "测试字段名称"},
                     {"target_column": "状态", "source_options": ["保留公式（不更新）"], "selected_source": "保留公式（不更新）", "note": "保留自动计算"}]})
    assert result["status"] == "needs_user_input"
    answer = mapping_answer_text("汇总", [("到账金额", "不更新金额"), ("状态", "保留公式（不更新）")])
    adapter.begin_turn(f"针对“{question}”：{answer}", force_preview=force_preview)
    assert adapter.allow_write is expected and not adapter._pending_clarification
    assert fake.applied_requests == 0


def test_free_text_cancellation_after_mapping_still_revokes_authority(tmp_path):
    adapter, fake = tools(tmp_path)
    read(adapter)
    adapter.execute("wps_confirm_mapping", {"document_url": URL, "sheet": "汇总", "question": "请确认",
        "mappings": [{"target_column": "到账金额", "source_options": ["金额"], "selected_source": None, "note": ""}]})
    adapter.begin_turn("针对“请确认”：取消更新")
    assert not adapter.allow_write and fake.applied_requests == 0


def test_confirmed_mapping_allows_one_full_task_in_next_turn(tmp_path):
    adapter, fake = tools(tmp_path, write=False)
    adapter.begin_turn("按单号更新到账金额和状态")
    snapshot = read(adapter)
    result = adapter.execute("wps_confirm_mapping", {"document_url": URL, "sheet": "汇总", "question": "请确认",
        "mappings": [{"target_column": "到账金额", "source_options": ["银行金额", "应收金额"], "selected_source": None, "note": ""},
                     {"target_column": "状态", "source_options": ["处理状态"], "selected_source": None, "note": ""}]})
    assert result["status"] == "needs_user_input" and fake.applied_requests == 0
    adapter.begin_turn("针对“请确认”：" + mapping_answer_text("汇总", [("到账金额", "银行金额"), ("状态", "处理状态")]))
    result = apply_table(adapter, table_plan(adapter, read(adapter)))
    assert result["ok"] and len(adapter.results) == 1 and fake.applied_requests == 2


def test_model_cannot_bypass_conflict_with_fresh_plan_until_next_user_turn(tmp_path):
    adapter, fake = tools(tmp_path)
    adapter.begin_turn("按单号更新到账金额和状态")
    confirm_columns(adapter, read(adapter), [{"target_column": "到账金额", "source_field": "银行金额"}, {"target_column": "状态", "source_field": "处理结果"}])
    def manual_change(transport, payload):
        if payload["target_column"] == 2:
            transport.at(3, 3)["value"] = "人工校正"
    fake.after_apply = manual_change
    class ModelOpener:
        def __init__(self):
            self.calls = 0
            self.latest_plan = None
            self.first_task = None
        def open(self, request, timeout=None):
            payload = json.loads(request.data)
            outputs = [json.loads(i["output"]) for i in payload["input"] if i.get("type") == "function_call_output"]
            phase = self.calls % 3
            if self.calls == 5:
                assert outputs[-1]["status"] == "needs_mapping" and "plan_id" not in outputs[-1]
                name, args = "wps_confirm_mapping", {"document_url": URL, "sheet": "汇总", "question": "请重新逐行选择",
                    "mappings": [{"target_column": t, "source_options": [source], "selected_source": None, "note": ""}
                        for t, source in [("到账金额", "银行金额"), ("状态", "处理结果")]]}
                output = [{"type": "function_call", "name": name, "call_id": f"call-{self.calls}",
                           "arguments": json.dumps(args, ensure_ascii=False)}]
            else:
                if self.calls == 3:
                    assert outputs[-1]["status"] == "partial" and outputs[-1]["retryable"] is False
                    self.first_task = outputs[-1]["task_id"]
                if phase == 0:
                    name, args = "wps_read_table", {"document_url": URL, "sheet": "汇总",
                        "columns": ["业务单号", "到账金额", "状态"], "header_row": None,
                        "start_row": None, "end_row": None, "column_overrides": None}
                elif phase == 1:
                    name, args = "wps_prepare_table_update", {"snapshot_id": outputs[-1]["snapshot_id"],
                        "key_columns": ["业务单号"], "columns": [
                            {"target_column": "到账金额", "source_field": "银行金额", "allow_blank": False},
                            {"target_column": "状态", "source_field": "处理结果", "allow_blank": False}],
                        "updates": [{"key_values": ["001"], "values": [30, "已处理"], "reason": "同一来源"}],
                        "summary": "更新两列"}
                else:
                    self.latest_plan = outputs[-1]["plan_id"]
                    name, args = "wps_apply_table_update", {"plan_id": self.latest_plan}
                output = [{"type": "function_call", "name": name, "call_id": f"call-{self.calls}",
                           "arguments": json.dumps(args, ensure_ascii=False)}]
            self.calls += 1
            event = {"type": "response.completed", "response": {"status": "completed", "output": output,
                "usage": {"input_tokens": 10, "output_tokens": 5}}}
            return io.BytesIO(b"data: " + json.dumps(event, ensure_ascii=False).encode() + b"\n\n")
    opener = ModelOpener()
    FinanceAIClient(AISettings(api_key="sk-test-never-real"), opener=opener).send(
        "按单号更新到账金额和状态", [], [], cloud_tools=adapter)
    assert fake.applied_requests == 1 and fake.at(3, 3)["value"] == "人工校正"
    checked = adapter.execute("wps_check_task", {"task_id": opener.first_task})
    assert not checked["ok"] and fake.applied_requests == 1
    original_again = adapter.execute("wps_apply_table_update", {"plan_id": opener.first_task})
    assert original_again["status"] == "partial" and "不重复写入" in original_again["note"]
    assert adapter.get_pending_mapping_question() is not None
    adapter.begin_turn("已核对人工修改，按原来源重新更新这两列")
    fake.after_apply = None
    result = apply_table(adapter, table_plan(adapter, read(adapter)))
    assert result["ok"] and fake.applied_requests == 2 and fake.at(3, 3)["value"] == "已处理"


def test_new_user_turn_does_not_remove_unresolved_write_protection(tmp_path):
    adapter, fake = tools(tmp_path)
    plan = table_plan(adapter, read(adapter))
    fake.fail_read_after_apply = True
    failed = apply_table(adapter, plan)
    assert failed["status"] == "uncertain" and failed["retryable"] is False
    fake.fail_read_after_apply = False
    assert adapter._mapping_summary()["status"] == "required"
    adapter.begin_turn("请重新更新两列")
    blocked = apply_table(adapter, table_plan(adapter, read(adapter)))
    assert not blocked["ok"] and "尚未核对" in blocked["error"] and blocked["retryable"] is False
    assert fake.applied_requests == 1


def test_preview_and_normal_completion_do_not_stop_later_plan(tmp_path):
    adapter, fake = tools(tmp_path, write=False)
    preview = apply_table(adapter, table_plan(adapter, read(adapter)))
    assert preview["status"] == "preview_only" and not adapter._table_write_stopped
    adapter.begin_turn("更新两列")
    assert apply_table(adapter, table_plan(adapter, read(adapter)))["ok"]
    assert not adapter._table_write_stopped
    another = apply_table(adapter, table_plan(adapter, read(adapter)))
    assert another["ok"] and another["updated"] == 0 and fake.applied_requests == 2
