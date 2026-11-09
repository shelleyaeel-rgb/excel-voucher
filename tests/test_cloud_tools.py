"""Business-flow checks using an in-memory WPS transport, never real accounts."""
from copy import deepcopy
import io
import json
import threading

import pytest

from xiaoyu import cloud_tools as cloud
from xiaoyu.ai_service import FinanceAIClient
from xiaoyu.ai_settings import AISettings
from xiaoyu.cloud_tools import CloudDocumentTools
from xiaoyu.wps_service import WpsBinding, WpsError


URL = "https://www.kdocs.cn/l/example-doc"
SHEET = "汇总"
HEADERS = ["业务单号", "到账金额", "状态"]


def same(a, b):
    return (type(a) is type(b) and a == b) or (type(a) in (int, float) and type(b) in (int, float) and a == b)


def cell(column, value="", *, formula=False, merged=False):
    return {"column": column, "value": value, "formula": "=1+1" if formula else "",
            "number_format": "General", "has_formula": formula, "merged": merged}


class MemoryWps:
    def __init__(self, data=None):
        self.rows = {1: [cell(1, "收款汇总")], 2: [cell(i + 1, h) for i, h in enumerate(HEADERS)]}
        for row, values in enumerate(data or [["001", 10, "已到账"], ["002", 20, "待到账"]], 3):
            self.rows[row] = [cell(i + 1, v) for i, v in enumerate(values)]
        self.calls = []
        self.before_apply = None
        self.after_apply = None
        self.fail_apply_before = False
        self.fail_apply_after = False
        self.fail_read_after_apply = False
        self.applied_requests = 0

    def factory(self, binding):
        return self

    def at(self, row, column):
        found = next((c for c in self.rows.get(row, []) if c["column"] == column), None)
        return found if found is not None else cell(column)

    def call(self, action, **payload):
        self.calls.append((action, deepcopy(payload)))
        if action == "describe":
            return {"workbook_name": "模版.xlsx", "sheets": [{"name": SHEET,
                "used_start_row": 1, "used_end_row": max(self.rows), "used_start_column": 1,
                "used_end_column": max(c["column"] for r in self.rows.values() for c in r)}]}
        if action == "read":
            if self.fail_read_after_apply and self.applied_requests:
                raise WpsError("读取超时", code="NETWORK")
            return {"rows": [{"row": r, "cells": [deepcopy(self.at(r, c)) for c in payload["columns"]]}
                for r in range(payload["start_row"], payload["end_row"] + 1)]}
        assert action == "apply"
        self.applied_requests += 1
        if self.before_apply:
            self.before_apply(self, payload)
        if self.fail_apply_before:
            raise WpsError("写入超时，结果未知", code="NETWORK", mutation_attempted=True)
        if any(not same(self.at(payload["header_row"], h["column"])["value"], h["value"])
               for h in payload["expected_headers"]):
            raise WpsError("表头变化", code="HEADER_MISMATCH")
        outcomes = []
        for index, change in enumerate(payload["changes"]):
            matches = [row for row in range(payload["row_start"], payload["row_end"] + 1)
                if all(same(self.at(row, col)["value"], value)
                       for col, value in zip(payload["key_columns"], change["key_values"]))]
            row = matches[0] if matches else change["row"]
            status = "not_found" if not matches else "duplicate" if len(matches) > 1 else None
            target = self.at(row, payload["target_column"])
            if status is None:
                if target["has_formula"]:
                    status = "formula"
                elif target["merged"]:
                    status = "merged"
                elif any(not same(self.at(row, g["column"])["value"], g["value"])
                         or not same(self.at(row, g["column"])["formula"], g["formula"])
                         for g in change["guards"]):
                    status = "conflict"
                elif target["number_format"] != change["old_number_format"]:
                    status = "conflict"
                else:
                    target["value"] = "" if change["new_value"] is None else change["new_value"]
                    target["formula"] = ""
                    status = "applied"
            outcomes.append({"index": index, "status": status, "row": row, "column": payload["target_column"]})
        if self.after_apply:
            self.after_apply(self, payload)
        if self.fail_apply_after:
            raise WpsError("保存结果未知", code="NETWORK", mutation_attempted=True)
        return {"outcomes": outcomes}


def tools(tmp_path, data=None, *, write=True):
    fake = MemoryWps(data)
    binding = WpsBinding("example-doc", "测试表", URL,
        "https://www.kdocs.cn/api/v3/ide/file/example-doc/script/example-script/sync_task",
        "test-token-never-real", "模版.xlsx")
    adapter = CloudDocumentTools([binding], allow_write=write, journal_dir=tmp_path / "tasks", client_factory=fake.factory)
    return adapter, fake


def read(adapter, **extra):
    result = adapter.execute("wps_read_table", {"document_url": URL, "sheet": SHEET, "columns": HEADERS,
        "header_row": None, "start_row": None, "end_row": None, **extra})
    assert result["ok"], result
    return result


def prepare(adapter, snapshot, updates=None, **extra):
    result = adapter.execute("wps_prepare_update", {"snapshot_id": snapshot["snapshot_id"],
        "key_columns": ["业务单号"], "target_column": "到账金额",
        "updates": updates or [{"key_values": ["001"], "new_value": 30, "reason": "已到账"}],
        "allow_blank": False, "summary": "按单号更新到账金额", **extra})
    assert result["ok"], result
    return result


def apply(adapter, plan, **extra):
    return adapter.execute("wps_apply_update", {"plan_id": plan["plan_id"]}, **extra)


def test_header_and_start_are_inferred_without_user_row_numbers(tmp_path):
    adapter, fake = tools(tmp_path)
    snap = read(adapter)
    assert snap["header_row"] == 2 and snap["start_row"] == 3 and snap["end_row"] == 4
    assert snap["rows"][0]["values"] == ["001", 10, "已到账"]
    plan = prepare(adapter, snap)
    outcome = apply(adapter, plan)
    assert outcome["status"] == "completed" and outcome["updated"] == 1
    assert fake.at(3, 2)["value"] == 30 and fake.at(4, 2)["value"] == 20
    assert fake.at(3, 1)["value"] == "001"
    assert adapter.results[0]["status"] == "completed" and len(adapter.results) == 1


def test_zero_is_value_and_numeric_keys_do_not_match_text_or_boolean(tmp_path):
    adapter, fake = tools(tmp_path, [["001", 10, "ok"], [1, 20, "ok"], [True, 30, "ok"], [0, 40, "ok"]])
    updates = [{"key_values": [key], "new_value": value, "reason": "测试类型"}
               for key, value in [("001", 0), (1, 21), (True, 31), (0, 41), ("1", 99)]]
    plan = prepare(adapter, read(adapter), updates)
    result = apply(adapter, plan)
    assert result["updated"] == 4 and result["skipped"] == 1
    assert [fake.at(row, 2)["value"] for row in range(3, 7)] == [0, 21, 31, 41]


@pytest.mark.parametrize("duplicate", ["source", "target"])
def test_duplicate_keys_block_whole_plan_until_clarified(tmp_path, duplicate):
    adapter, fake = tools(tmp_path, [["001", 10, "ok"], ["001" if duplicate == "target" else "002", 20, "ok"]])
    updates = [{"key_values": ["001"], "new_value": 30, "reason": "测试"}]
    if duplicate == "source":
        updates.append({"key_values": ["001"], "new_value": 40, "reason": "重复"})
    plan = prepare(adapter, read(adapter), updates)
    assert plan["needs_clarification"] and not plan["can_execute"]
    assert apply(adapter, plan)["ok"] is False
    assert fake.applied_requests == 0 and fake.at(3, 2)["value"] == 10


def test_blank_formula_merged_and_formula_like_text_are_not_overwritten(tmp_path):
    adapter, fake = tools(tmp_path, [[str(i), i, "ok"] for i in range(1, 6)])
    fake.at(4, 2).update(formula="=1+1", has_formula=True)
    fake.at(5, 2)["merged"] = True
    updates = [{"key_values": [key], "new_value": value, "reason": "测试"}
               for key, value in [("1", None), ("2", 50), ("3", 50), ("4", " =SUM(A1)"), ("5", 0)]]
    plan = prepare(adapter, read(adapter), updates)
    assert [item["status"] for item in plan["preview"]] == ["blank_skipped", "formula", "merged", "formula_text_unsupported", "planned"]
    result = apply(adapter, plan)
    assert result["updated"] == 1 and result["skipped"] == 4
    assert [fake.at(row, 2)["value"] for row in range(3, 8)] == [1, 2, 3, 4, 0]


def test_explicit_blank_permission_clears_only_matching_cell(tmp_path):
    adapter, fake = tools(tmp_path)
    plan = prepare(adapter, read(adapter), [{"key_values": ["001"], "new_value": None, "reason": "明确清空"}], allow_blank=True)
    result = apply(adapter, plan)
    assert result["status"] == "completed" and fake.at(3, 2)["value"] == ""
    assert fake.at(4, 2)["value"] == 20


@pytest.mark.parametrize("message", ["先预览", "只读这个表", "先不要更新", "不要修改这个文档", "能更新吗", "可以更新吗？", "可以更新？", "如何更新这个表", "解释更新方案",
    "不要填入这张表", "先别回填", "取消更新", "停止更新", "暂停写入", "勿覆盖金额", "查看修改记录"])
def test_read_only_preview_and_questions_never_grant_write(tmp_path, message):
    adapter, fake = tools(tmp_path, write=False)
    adapter.begin_turn(message)
    plan = prepare(adapter, read(adapter))
    assert not adapter.allow_write and not plan["can_execute"]
    assert apply(adapter, plan)["status"] == "preview_only"
    assert fake.applied_requests == 0


@pytest.mark.parametrize("message", ["按业务单号更新金额，未匹配的不动，来源为空不覆盖",
    "更新这个表，未匹配的不修改", "把金额写入云表，未找到的不要更新", "回填金额，未匹配的不填入"])
def test_conditional_exclusions_do_not_cancel_explicit_update(tmp_path, message):
    adapter, _ = tools(tmp_path, write=False)
    adapter.begin_turn(message)
    assert adapter.allow_write


def test_permission_survives_missing_details_but_ends_after_task_completion(tmp_path):
    adapter, _ = tools(tmp_path, write=False)
    adapter.begin_turn("更新这份表")
    assert adapter.allow_write
    adapter.begin_turn("按业务单号匹配，使用到账金额列")
    assert adapter.allow_write
    plan = prepare(adapter, read(adapter))
    assert apply(adapter, plan)["status"] == "completed"
    adapter.begin_turn("再看看结果")
    assert not adapter.allow_write and adapter.results == []
    adapter.begin_turn("来源为空不更新")
    assert not adapter.allow_write


def test_force_preview_overrides_write_words_and_negation_withdraws_permission(tmp_path):
    adapter, fake = tools(tmp_path, write=False)
    adapter.begin_turn("执行更新", force_preview=True)
    assert not adapter.allow_write
    adapter.begin_turn("执行更新")
    assert adapter.allow_write
    adapter.begin_turn("先不要修改")
    assert not adapter.allow_write and fake.applied_requests == 0


@pytest.mark.parametrize("message", ["不要填入这张表", "先别回填", "取消更新", "停止更新", "先等等"])
def test_stop_and_negation_withdraw_existing_unfinished_authorization(tmp_path, message):
    adapter, fake = tools(tmp_path)
    adapter.begin_turn("按单号更新金额")
    assert adapter.allow_write
    adapter.begin_turn(message)
    assert not adapter.allow_write and fake.applied_requests == 0


def test_sort_between_snapshot_and_commit_relocates_by_business_key(tmp_path):
    adapter, fake = tools(tmp_path)
    plan = prepare(adapter, read(adapter))
    def reorder(client, payload):
        client.rows[3], client.rows[4] = client.rows[4], client.rows[3]
    fake.before_apply = reorder
    result = apply(adapter, plan)
    assert result["status"] == "completed"
    assert fake.at(4, 1)["value"] == "001" and fake.at(4, 2)["value"] == 30
    assert fake.at(3, 1)["value"] == "002" and fake.at(3, 2)["value"] == 20
    journal = json.loads((adapter.journal_dir / (plan["plan_id"] + ".json")).read_text(encoding="utf-8"))
    assert journal["items"][0]["verified_row"] == 4


def test_changed_filter_column_conflicts_and_does_not_overwrite(tmp_path):
    adapter, fake = tools(tmp_path)
    plan = prepare(adapter, read(adapter))
    fake.before_apply = lambda client, payload: client.at(3, 3).update(value="已撤回")
    result = apply(adapter, plan)
    assert result["status"] == "partial" and result["conflicts"] == 1
    assert result["updated"] == 0 and fake.at(3, 2)["value"] == 10


def test_journal_precedes_mutation_and_never_contains_auth_credentials(tmp_path):
    adapter, fake = tools(tmp_path)
    plan = prepare(adapter, read(adapter))
    def inspect_journal(client, payload):
        raw = (adapter.journal_dir / (plan["plan_id"] + ".json")).read_text(encoding="utf-8")
        record = json.loads(raw)
        assert record["status"] == "running" and record["items"][0]["status"] == "pending"
        assert record["items"][0]["old_value"] == 10 and record["items"][0]["new_value"] == 30
        assert "test-token-never-real" not in raw and "sync_task" not in raw
    fake.before_apply = inspect_journal
    assert apply(adapter, plan)["status"] == "completed"
    assert apply(adapter, plan)["status"] == "completed" and fake.applied_requests == 1


def test_timeout_is_not_retried_and_read_only_check_reconciles_after_restart(tmp_path):
    adapter, fake = tools(tmp_path)
    plan = prepare(adapter, read(adapter))
    fake.fail_apply_before = True
    result = apply(adapter, plan)
    assert result["status"] == "uncertain" and result["unknown"] == 1
    assert fake.applied_requests == 1
    assert apply(adapter, plan)["status"] == "uncertain" and fake.applied_requests == 1
    second = prepare(adapter, read(adapter), [{"key_values": ["002"], "new_value": 40, "reason": "另一个任务"}])
    assert "尚未核对" in apply(adapter, second)["error"] and fake.applied_requests == 1
    # Emulate a previously timed-out server task eventually completing. The
    # new application instance must only read and reconcile it.
    fake.at(3, 2)["value"] = 30
    reopened = CloudDocumentTools(list(adapter.bindings.values()), journal_dir=adapter.journal_dir, client_factory=fake.factory)
    checked = reopened.execute("wps_check_task", {"task_id": plan["plan_id"]})
    assert checked["status"] == "completed" and checked["updated"] == 1
    assert fake.applied_requests == 1


def test_timeout_after_write_verifies_observed_values_but_stops_next_batch(tmp_path, monkeypatch):
    monkeypatch.setattr(cloud, "BATCH_SIZE", 1)
    adapter, fake = tools(tmp_path)
    updates = [{"key_values": [key], "new_value": 30, "reason": "测试"} for key in ("001", "002")]
    plan = prepare(adapter, read(adapter), updates)
    fake.fail_apply_after = True
    result = apply(adapter, plan)
    assert result["status"] == "partial" and result["updated"] == 1 and result["skipped"] == 1
    assert fake.applied_requests == 1 and fake.at(4, 2)["value"] == 20


def test_cancel_after_submitted_batch_still_verifies_then_stops(tmp_path, monkeypatch):
    monkeypatch.setattr(cloud, "BATCH_SIZE", 1)
    adapter, fake = tools(tmp_path)
    updates = [{"key_values": [key], "new_value": 30, "reason": "测试"} for key in ("001", "002")]
    plan = prepare(adapter, read(adapter), updates)
    cancelled = threading.Event()
    fake.after_apply = lambda client, payload: cancelled.set()
    result = apply(adapter, plan, cancel=cancelled)
    assert result["status"] == "cancelled" and result["updated"] == 1 and result["skipped"] == 1
    assert fake.applied_requests == 1 and fake.at(4, 2)["value"] == 20


def test_header_change_blocks_readback_success(tmp_path):
    adapter, fake = tools(tmp_path)
    plan = prepare(adapter, read(adapter))
    fake.after_apply = lambda client, payload: client.at(2, 2).update(value="被换掉的列")
    result = apply(adapter, plan)
    assert result["status"] == "uncertain" and result["updated"] == 0 and result["unknown"] == 1


def test_repeated_header_outside_first_30_rows_requires_explicit_range(tmp_path):
    adapter, fake = tools(tmp_path, [[str(i), 0, "ok"] for i in range(35)])
    fake.rows[34] = [cell(i + 1, h) for i, h in enumerate(HEADERS)]
    result = adapter.execute("wps_read_table", {"document_url": URL, "sheet": SHEET, "columns": HEADERS,
        "header_row": None, "start_row": None, "end_row": None})
    assert not result["ok"] and "重复表头" in result["error"]
    assert read(adapter, end_row=33)["row_count"] == 31


def test_large_integer_is_rejected_before_plan_or_network_write(tmp_path):
    adapter, fake = tools(tmp_path)
    snap = read(adapter)
    result = adapter.execute("wps_prepare_update", {"snapshot_id": snap["snapshot_id"], "key_columns": ["业务单号"],
        "target_column": "到账金额", "updates": [{"key_values": ["001"], "new_value": 9007199254740992, "reason": "测试"}],
        "allow_blank": False, "summary": "测试"})
    assert not result["ok"] and "精确" in result["error"] and fake.applied_requests == 0


def test_service_collects_same_task_replaced_preview_as_final_result(tmp_path):
    adapter, fake = tools(tmp_path, write=False)
    adapter.begin_turn("按业务单号更新到账金额")
    class ModelOpener:
        def __init__(self):
            self.calls = 0
        def open(self, request, timeout=None):
            self.calls += 1
            payload = json.loads(request.data)
            outputs = [json.loads(item["output"]) for item in payload["input"] if item.get("type") == "function_call_output"]
            if self.calls == 1:
                name, args = "wps_read_table", {"document_url": URL, "sheet": SHEET, "columns": HEADERS,
                    "header_row": None, "start_row": None, "end_row": None}
            elif self.calls == 2:
                name, args = "wps_prepare_update", {"snapshot_id": outputs[-1]["snapshot_id"], "key_columns": ["业务单号"],
                    "target_column": "到账金额", "updates": [{"key_values": ["001"], "new_value": 30, "reason": "已到账"}],
                    "allow_blank": False, "summary": "按单号更新"}
            elif self.calls == 3:
                name, args = "wps_apply_update", {"plan_id": outputs[-1]["plan_id"]}
            else:
                output = [{"type": "message", "content": [{"type": "output_text", "text": "已核对更新。"}]}]
            if self.calls <= 3:
                output = [{"type": "function_call", "name": name, "call_id": "call-" + str(self.calls),
                    "arguments": json.dumps(args, ensure_ascii=False)}]
            event = {"type": "response.completed", "response": {"status": "completed", "output": output,
                "usage": {"input_tokens": 10, "output_tokens": 5}}}
            return io.BytesIO(b"data: " + json.dumps(event, ensure_ascii=False).encode() + b"\n\n")
    opener = ModelOpener()
    client = FinanceAIClient(AISettings(api_key="sk-test-never-real"), opener=opener)
    result = client.send("按业务单号更新到账金额", [], [], cloud_tools=adapter)
    assert len(result.cloud_results) == 1 and result.cloud_results[0]["status"] == "completed"
    assert result.cloud_results[0]["updated"] == 1 and fake.applied_requests == 1
    assert result.input_tokens == 40 and opener.calls == 4


def test_clarification_offers_clickable_choices_and_blocks_same_turn_commit(tmp_path):
    adapter, fake = tools(tmp_path)
    adapter.begin_turn("更新这个表")
    plan = prepare(adapter, read(adapter))
    question = adapter.execute("wps_ask_clarification", {"question": "按哪个字段匹配？", "options": ["业务单号", "客户名称"]})
    assert question["status"] == "needs_user_input"
    assert adapter.questions == [{"question": "按哪个字段匹配？", "options": ["业务单号", "客户名称"]}]
    assert apply(adapter, plan)["status"] == "needs_user_input" and fake.applied_requests == 0
    adapter.begin_turn("业务单号")
    assert adapter.questions == [] and adapter.allow_write
    assert apply(adapter, plan)["status"] == "completed" and fake.applied_requests == 1


def test_clarification_can_accept_free_text_without_forced_choices(tmp_path):
    adapter, _ = tools(tmp_path)
    result = adapter.execute("wps_ask_clarification", {"question": "哪些状态需要更新？", "options": []})
    assert result["status"] == "needs_user_input" and adapter.questions[0]["options"] == []


@pytest.mark.parametrize("question,options", [("", []), ("选哪个？", ["同一个", "同一个"]),
    ("选哪个？", ["a", "b", "c", "d", "e"]), ("选哪个？", [1]), ("字" * 501, [])])
def test_invalid_clarification_does_not_change_permission_state(tmp_path, question, options):
    adapter, fake = tools(tmp_path)
    result = adapter.execute("wps_ask_clarification", {"question": question, "options": options})
    assert not result["ok"] and adapter.questions == [] and fake.applied_requests == 0


def test_historical_prepared_journal_never_becomes_executable_after_check(tmp_path):
    adapter, fake = tools(tmp_path)
    plan = prepare(adapter, read(adapter))
    reopened = CloudDocumentTools(list(adapter.bindings.values()), allow_write=True,
        journal_dir=adapter.journal_dir, client_factory=fake.factory)
    result = reopened.execute("wps_check_task", {"task_id": plan["plan_id"]})
    assert result["status"] == "prepared"
    assert not apply(reopened, plan)["ok"] and fake.applied_requests == 0


def test_rebinding_same_title_to_different_file_prevents_recovery_reads(tmp_path):
    adapter, fake = tools(tmp_path)
    plan = prepare(adapter, read(adapter))
    fake.fail_apply_before = True
    assert apply(adapter, plan)["status"] == "uncertain"
    old_call_count = len(fake.calls)
    binding = deepcopy(next(iter(adapter.bindings.values())))
    binding.endpoint = binding.endpoint.replace("/file/example-doc/", "/file/other-file/")
    reopened = CloudDocumentTools([binding], journal_dir=adapter.journal_dir, client_factory=fake.factory)
    result = reopened.execute("wps_check_task", {"task_id": plan["plan_id"]})
    assert not result["ok"] and "身份" in result["error"] and len(fake.calls) == old_call_count


def test_cancel_before_apply_leaves_prepared_plan_and_does_not_write(tmp_path):
    adapter, fake = tools(tmp_path)
    plan = prepare(adapter, read(adapter))
    cancel = threading.Event()
    cancel.set()
    result = apply(adapter, plan, cancel=cancel)
    assert result["status"] == "cancelled" and fake.applied_requests == 0
    assert adapter.plans[plan["plan_id"]]["status"] == "prepared"


def test_failed_readback_preserves_unknown_and_check_never_writes(tmp_path):
    adapter, fake = tools(tmp_path)
    plan = prepare(adapter, read(adapter))
    fake.fail_read_after_apply = True
    result = apply(adapter, plan)
    assert result["status"] == "uncertain" and result["unknown"] == 1 and fake.applied_requests == 1
    fake.fail_read_after_apply = False
    checked = adapter.execute("wps_check_task", {"task_id": plan["plan_id"]})
    assert checked["status"] == "completed" and checked["updated"] == 1 and fake.applied_requests == 1


def test_corrupted_journal_is_rejected_without_cloud_reads(tmp_path):
    adapter, fake = tools(tmp_path)
    task_id = "a" * 32
    adapter.journal_dir.mkdir(parents=True)
    (adapter.journal_dir / (task_id + ".json")).write_text(json.dumps({"schema": 1, "task_id": task_id}), encoding="utf-8")
    result = adapter.execute("wps_check_task", {"task_id": task_id})
    assert not result["ok"] and "格式" in result["error"] and fake.calls == []


def test_prepared_plan_and_snapshot_catalog_survive_next_turn_without_secrets(tmp_path):
    adapter, fake = tools(tmp_path, write=False)
    adapter.begin_turn("先预览更新规则")
    snapshot = read(adapter)
    plan = prepare(adapter, snapshot)
    adapter.begin_turn("确认执行")
    instructions = adapter.instructions()
    assert plan["plan_id"] in instructions and snapshot["snapshot_id"] in instructions
    assert "到账金额" in instructions and "按单号更新到账金额" in instructions
    assert "test-token-never-real" not in instructions and "sync_task" not in instructions
    assert str(adapter.journal_dir) not in instructions
    assert apply(adapter, plan)["status"] == "completed" and fake.applied_requests == 1


@pytest.mark.parametrize("code", ["HEADER_MISMATCH", "REQUEST_TOO_LARGE"])
def test_explicit_prewrite_rejection_is_not_unknown_or_a_permanent_lock(tmp_path, code):
    adapter, fake = tools(tmp_path)
    plan = prepare(adapter, read(adapter))
    def reject(client, payload):
        raise WpsError("本批未写入", code=code, mutation_attempted=False)
    fake.before_apply = reject
    old_reads = sum(action == "read" for action, _ in fake.calls)
    result = apply(adapter, plan)
    assert result["status"] == "partial" and result["unknown"] == 0 and result["updated"] == 0
    assert result["skipped"] == 1 and code in result["error"]
    assert sum(action == "read" for action, _ in fake.calls) == old_reads
    fake.before_apply = None
    second = prepare(adapter, read(adapter))
    assert apply(adapter, second)["status"] == "completed"


def test_completed_task_check_reads_current_state_without_rewriting_history(tmp_path):
    adapter, fake = tools(tmp_path)
    plan = prepare(adapter, read(adapter))
    assert apply(adapter, plan)["status"] == "completed"
    reads_before = sum(action == "read" for action, _ in fake.calls)
    fake.at(3, 2)["value"] = 99
    checked = adapter.execute("wps_check_task", {"task_id": plan["plan_id"]})
    assert checked["ok"] is False and checked["status"] == "completed"
    assert checked["updated"] == 1 and checked["unknown"] == 0
    assert checked["current_check"]["changed"] == 1 and checked["current_check"]["matched"] == 0
    assert "历史记录" in checked["summary"] and "当前不同" in checked["summary"]
    assert sum(action == "read" for action, _ in fake.calls) > reads_before and fake.applied_requests == 1
    # A later human change is a conflict with the old desired state, not an
    # unresolved network mutation that would lock all later tasks.
    new_plan = prepare(adapter, read(adapter))
    assert apply(adapter, new_plan)["status"] == "completed" and fake.applied_requests == 2


def test_completed_check_read_failure_keeps_history_and_reports_failure(tmp_path):
    adapter, fake = tools(tmp_path)
    plan = prepare(adapter, read(adapter))
    assert apply(adapter, plan)["status"] == "completed"
    fake.fail_read_after_apply = True
    checked = adapter.execute("wps_check_task", {"task_id": plan["plan_id"]})
    assert checked["ok"] is False and checked["status"] == "completed" and checked["unknown"] == 0
    assert checked["current_check"]["failed"] is True and "未能核对当前" in checked["summary"]
    assert fake.applied_requests == 1


def test_unresolved_file_is_blocked_through_another_share_link(tmp_path):
    adapter, fake = tools(tmp_path)
    plan = prepare(adapter, read(adapter))
    fake.fail_apply_before = True
    assert apply(adapter, plan)["status"] == "uncertain"
    binding = deepcopy(next(iter(adapter.bindings.values())))
    binding.id = "another-share"
    binding.share_url = "https://www.kdocs.cn/l/another-share"
    alias = CloudDocumentTools([binding], allow_write=True, journal_dir=adapter.journal_dir, client_factory=fake.factory)
    snapshot = read(alias, document_url=binding.share_url)
    second = prepare(alias, snapshot)
    result = apply(alias, second)
    assert result["ok"] is False and "尚未核对" in result["error"] and fake.applied_requests == 1


def test_restart_closes_running_journal_before_first_submission_without_write(tmp_path):
    adapter, fake = tools(tmp_path)
    plan = prepare(adapter, read(adapter))
    record = adapter.plans[plan["plan_id"]]
    record["status"] = "running"
    adapter._save(record)  # Crash window: durable running, no submitted batch.
    reopened = CloudDocumentTools(list(adapter.bindings.values()), allow_write=True,
        journal_dir=adapter.journal_dir, client_factory=fake.factory)
    reads_before = sum(action == "read" for action, _ in fake.calls)
    checked = reopened.execute("wps_check_task", {"task_id": plan["plan_id"]})
    assert checked["status"] == "partial" and checked["skipped"] == 1 and checked["unknown"] == 0
    assert fake.applied_requests == 0 and sum(action == "read" for action, _ in fake.calls) == reads_before
    saved = json.loads((adapter.journal_dir / (plan["plan_id"] + ".json")).read_text(encoding="utf-8"))
    assert saved["items"][0]["status"] == "not_submitted" and saved["batches"] == []
    replacement = prepare(reopened, read(reopened))
    assert apply(reopened, replacement)["status"] == "completed" and fake.applied_requests == 1


def test_restart_closes_running_journal_after_last_verified_batch_without_replay(tmp_path):
    adapter, fake = tools(tmp_path)
    plan = prepare(adapter, read(adapter))
    assert apply(adapter, plan)["status"] == "completed"
    record = adapter.plans[plan["plan_id"]]
    record["status"] = "running"
    adapter._save(record)  # Crash window: checked batch saved, final status not.
    reopened = CloudDocumentTools(list(adapter.bindings.values()), allow_write=True,
        journal_dir=adapter.journal_dir, client_factory=fake.factory)
    reads_before = sum(action == "read" for action, _ in fake.calls)
    checked = reopened.execute("wps_check_task", {"task_id": plan["plan_id"]})
    assert checked["ok"] is True and checked["status"] == "completed" and checked["updated"] == 1
    assert checked["current_check"]["matched"] == 1 and checked["unknown"] == 0
    assert fake.applied_requests == 1 and sum(action == "read" for action, _ in fake.calls) > reads_before
    saved = json.loads((adapter.journal_dir / (plan["plan_id"] + ".json")).read_text(encoding="utf-8"))
    assert saved["status"] == "completed" and saved["batches"][0]["status"] == "checked"


def test_ui_choice_answer_ignores_quoted_question_for_write_intent(tmp_path):
    adapter, _ = tools(tmp_path, write=False)
    adapter.begin_turn("更新这个表")
    adapter.execute("wps_ask_clarification", {"question": "重复单号怎么处理？", "options": ["跳过重复项", "先不要更新"]})
    message = "针对“重复单号怎么处理？”：跳过重复项"
    adapter.begin_turn(message)
    assert adapter.allow_write and adapter.questions == []
    assert message == "针对“重复单号怎么处理？”：跳过重复项"


def test_ui_choice_answer_can_still_withdraw_write_permission(tmp_path):
    adapter, _ = tools(tmp_path, write=False)
    adapter.begin_turn("更新这个表")
    adapter.execute("wps_ask_clarification", {"question": "重复单号怎么处理？", "options": ["跳过重复项", "先不要更新"]})
    adapter.begin_turn("针对“重复单号怎么处理？”：先不要更新")
    assert not adapter.allow_write


def test_unrecognized_quoted_question_is_not_stripped_for_authorization(tmp_path):
    adapter, _ = tools(tmp_path, write=False)
    adapter.begin_turn("更新这个表")
    adapter.begin_turn("针对“如何更新这张表？”：继续")
    assert not adapter.allow_write


@pytest.mark.parametrize("message", ["处理上传表格并生成Excel", "执行金额汇总并导出文件",
    "开始执行金额汇总并导出文件", "按这个规则处理云文档", "确认执行", "开始执行", "继续"])
def test_calculation_export_and_unscoped_execution_do_not_grant_cloud_writes(tmp_path, message):
    adapter, fake = tools(tmp_path, write=False)
    adapter.begin_turn(message)
    assert not adapter.allow_write
    plan = prepare(adapter, read(adapter))
    assert apply(adapter, plan)["status"] == "preview_only" and fake.applied_requests == 0


@pytest.mark.parametrize("message", ["按业务单号更新云文档金额", "将来源金额写入这份云表", "把已到账金额回填到云文档"])
def test_explicit_cloud_update_still_grants_write_without_extra_confirmation(tmp_path, message):
    adapter, _ = tools(tmp_path, write=False)
    adapter.begin_turn(message)
    assert adapter.allow_write


@pytest.mark.parametrize("answer", ["确认执行", "开始执行", "按这个规则处理云文档"])
def test_short_execution_answer_requires_concrete_preview_plan(tmp_path, answer):
    adapter, fake = tools(tmp_path, write=False)
    adapter.begin_turn("先预览更新结果")
    plan = prepare(adapter, read(adapter))
    assert not adapter.allow_write
    adapter.begin_turn(answer)
    assert adapter.allow_write
    assert apply(adapter, plan)["status"] == "completed" and fake.applied_requests == 1


def test_clarification_answer_then_continue_retains_unfinished_update_scope(tmp_path):
    adapter, fake = tools(tmp_path, write=False)
    adapter.begin_turn("按来源更新这份云文档")
    adapter.execute("wps_ask_clarification", {"question": "按哪个字段匹配？", "options": ["业务单号", "客户名称"]})
    adapter.begin_turn("针对“按哪个字段匹配？”：业务单号")
    assert adapter.allow_write
    adapter.begin_turn("继续")
    assert adapter.allow_write
    plan = prepare(adapter, read(adapter))
    assert apply(adapter, plan)["status"] == "completed" and fake.applied_requests == 1


def test_generic_calculation_execution_does_not_accept_an_existing_preview(tmp_path):
    adapter, fake = tools(tmp_path, write=False)
    adapter.begin_turn("先预览更新结果")
    plan = prepare(adapter, read(adapter))
    adapter.begin_turn("开始执行金额汇总并导出文件")
    assert not adapter.allow_write
    assert apply(adapter, plan)["status"] == "preview_only" and fake.applied_requests == 0
