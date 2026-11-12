"""Independent checks that real user selections remain a mandatory write gate."""
from copy import deepcopy
import json

import pytest

from test_cloud_tools import SHEET, URL, read, tools
from test_ai_service import completed, function_call, function_round, sse
from xiaoyu.ai_service import AIError, FinanceAIClient, UploadedFile
from xiaoyu.ai_settings import AISettings
from xiaoyu.cloud_mapping import KEEP_SOURCE, mapping_answer_text, validate_mappings


FILES = [{"file_id": "file-review-a", "name": "银行.xlsx", "size": 128}]
QUESTION = "请逐行选择来源列"
SELECTED = [("到账金额", "银行金额"), ("状态", "处理结果")]


def start(adapter):
    adapter.set_source_context(deepcopy(FILES))
    adapter.begin_turn("按业务单号更新到账金额和状态")
    return read(adapter)


def ask(adapter):
    result = adapter.execute("wps_confirm_mapping", {
        "document_url": URL, "sheet": SHEET, "question": QUESTION,
        "mappings": [
            {"target_column": "到账金额", "source_options": ["银行金额", "应收金额"],
             "selected_source": None, "note": "请选择金额来源。"},
            {"target_column": "状态", "source_options": ["处理结果", "银行状态"],
             "selected_source": None, "note": "请选择状态来源。"},
        ],
    })
    assert result["ok"] and result["status"] == "needs_user_input", result
    return result


def answer(adapter, selections=SELECTED):
    adapter.begin_turn(f"针对“{QUESTION}”：" + mapping_answer_text(SHEET, selections))


def plan(adapter, snapshot, *, selected=SELECTED):
    return adapter.execute("wps_prepare_table_update", {
        "snapshot_id": snapshot["snapshot_id"], "key_columns": ["业务单号"],
        "columns": [{"target_column": target, "source_field": source, "allow_blank": False}
                    for target, source in selected],
        "updates": [{"key_values": ["001"], "values": [30 if target == "到账金额" else "已处理"
                     for target, _ in selected], "reason": "合成银行来源"}],
        "summary": "按已选择的来源更新全部目标列",
    })


def assert_no_writable_plan(result, fake):
    assert not result.get("can_execute", False), result
    assert not result.get("plan_id"), result
    assert result.get("status") in ("needs_mapping", "needs_user_input"), result
    assert fake.applied_requests == 0


def test_ordinary_followup_keeps_pending_mapping_and_cannot_write(tmp_path):
    adapter, fake = tools(tmp_path)
    snapshot = start(adapter)
    ask(adapter)
    original = deepcopy(adapter.get_pending_mapping_question())
    adapter.begin_turn("银行金额和应收金额有什么区别？")
    pending = adapter.get_pending_mapping_question()
    assert pending is not None and pending["question"] == original["question"]
    assert pending["mappings"] == original["mappings"]
    assert_no_writable_plan(plan(adapter, snapshot), fake)


def test_complete_source_catalog_plus_keep_option_is_accepted_by_ui_validator(tmp_path):
    adapter, fake = tools(tmp_path)
    start(adapter)
    result = adapter.execute("wps_confirm_mapping", {
        "document_url": URL, "sheet": SHEET, "question": QUESTION,
        "mappings": [{"target_column": "到账金额",
            "source_options": [f"来源列{index}" for index in range(300)],
            "selected_source": None, "note": "完整来源列目录。"}],
    })
    assert result["ok"], result
    displayed = validate_mappings(result["mappings"])
    assert len(displayed[0]["source_options"]) == 301
    assert KEEP_SOURCE in displayed[0]["source_options"] and fake.applied_requests == 0


@pytest.mark.parametrize("selections", [SELECTED[:1], [("到账金额", "应收金额"), SELECTED[1]]])
def test_model_cannot_drop_target_or_change_source_after_confirmation(tmp_path, selections):
    adapter, fake = tools(tmp_path)
    snapshot = start(adapter)
    ask(adapter)
    answer(adapter)
    assert_no_writable_plan(plan(adapter, snapshot, selected=selections), fake)


def test_fresh_read_with_same_structure_keeps_confirmed_selections(tmp_path):
    adapter, fake = tools(tmp_path)
    start(adapter)
    ask(adapter)
    answer(adapter)
    prepared = plan(adapter, read(adapter))
    assert prepared["ok"] and prepared["can_execute"], prepared
    assert adapter.get_pending_mapping_question() is None
    result = adapter.execute("wps_apply_table_update", {"plan_id": prepared["plan_id"]})
    assert result["ok"] and result["updated"] == 2, result
    assert fake.applied_requests == 2


def test_same_physical_columns_in_different_read_order_do_not_ask_again(tmp_path):
    adapter, fake = tools(tmp_path)
    start(adapter)
    ask(adapter)
    answer(adapter)
    reordered = read(adapter, columns=["状态", "业务单号", "到账金额"])
    prepared = plan(adapter, reordered)
    assert prepared["ok"] and prepared["can_execute"], prepared
    assert adapter.get_pending_mapping_question() is None
    result = adapter.execute("wps_apply_table_update", {"plan_id": prepared["plan_id"]})
    assert result["ok"] and fake.applied_requests == 2, result


def test_same_filename_with_new_uploaded_id_invalidates_prepared_plan(tmp_path):
    adapter, fake = tools(tmp_path)
    snapshot = start(adapter)
    ask(adapter)
    answer(adapter)
    prepared = plan(adapter, snapshot)
    assert prepared["can_execute"], prepared
    adapter.set_source_context([{**FILES[0], "file_id": "file-review-b"}])
    result = adapter.execute("wps_apply_table_update", {"plan_id": prepared["plan_id"]})
    assert not result["ok"] and fake.applied_requests == 0, result


def test_reordered_real_target_columns_require_new_selection(tmp_path):
    adapter, fake = tools(tmp_path)
    start(adapter)
    ask(adapter)
    answer(adapter)
    for row in fake.rows.values():
        for cell in row:
            if cell["column"] in (2, 3):
                cell["column"] = 5 - cell["column"]
    assert_no_writable_plan(plan(adapter, read(adapter)), fake)


def test_keeping_column_does_not_allow_model_to_write_it(tmp_path):
    adapter, fake = tools(tmp_path)
    snapshot = start(adapter)
    ask(adapter)
    answer(adapter, [("到账金额", "银行金额"), ("状态", KEEP_SOURCE)])
    assert_no_writable_plan(plan(adapter, snapshot), fake)


def test_new_update_after_completion_requires_new_selections(tmp_path):
    adapter, fake = tools(tmp_path)
    snapshot = start(adapter)
    ask(adapter)
    answer(adapter)
    prepared = plan(adapter, snapshot)
    assert adapter.execute("wps_apply_table_update", {"plan_id": prepared["plan_id"]})["ok"]
    before = fake.applied_requests
    adapter.begin_turn("现在按新规则再更新到账金额和状态")
    result = plan(adapter, read(adapter))
    assert not result.get("can_execute", False) and not result.get("plan_id"), result
    assert result.get("status") in ("needs_mapping", "needs_user_input"), result
    assert fake.applied_requests == before


class RealBackendResponseScript:
    """Model only the Responses transport; all mapping/write behavior is real."""

    def __init__(self):
        self.steps = []
        self.payloads = []

    def open(self, request, timeout=None):
        assert request.full_url == "https://api.openai.com/v1/responses"
        payload = json.loads(request.data)
        self.payloads.append(payload)
        assert self.steps, "Service unexpectedly asked the model for another response"
        event = self.steps.pop(0)(payload)
        return sse(*event) if isinstance(event, list) else sse(event)


def latest_tool_result(payload):
    return json.loads(next(item["output"] for item in reversed(payload["input"])
                           if item.get("type") == "function_call_output"))


def model_call(name, arguments):
    return function_round(function_call(name, "call-" + name, json.dumps(arguments, ensure_ascii=False)))


def read_response(payload):
    assert "tool_choice" not in payload
    return model_call("wps_read_table", {
        "document_url": URL, "sheet": SHEET, "columns": ["业务单号", "到账金额", "状态"],
        "header_row": None, "start_row": None, "end_row": None, "column_overrides": None,
    })


def prepare_response(payload):
    snapshot = latest_tool_result(payload)
    assert snapshot["ok"] and snapshot["snapshot_id"], snapshot
    return model_call("wps_prepare_table_update", {
        "snapshot_id": snapshot["snapshot_id"], "key_columns": ["业务单号"],
        "columns": [{"target_column": target, "source_field": source, "allow_blank": False}
                    for target, source in SELECTED],
        "updates": [{"key_values": ["001"], "values": [30, "已处理"], "reason": "合成来源"}],
        "summary": "按用户选择的两列来源更新",
    })


def forced_mapping_response(payload):
    blocked = latest_tool_result(payload)
    assert blocked["status"] == "needs_mapping" and not blocked.get("plan_id"), blocked
    assert payload["tool_choice"] == {"type": "function", "name": "wps_confirm_mapping"}
    return model_call("wps_confirm_mapping", {
        "document_url": URL, "sheet": SHEET, "question": QUESTION,
        "mappings": [{"target_column": target, "source_options": [source],
                      "selected_source": source, "note": "请选择来源。"} for target, source in SELECTED],
    })


def first_service_turn(adapter):
    script = RealBackendResponseScript()
    script.steps = [read_response, prepare_response, forced_mapping_response]
    client = FinanceAIClient(AISettings(api_key="sk-test-never-real"), opener=script)
    files = [UploadedFile(**entry) for entry in FILES]
    message = "按业务单号更新到账金额和状态"
    # Match the real UI: begin_turn precedes send(), and send sets sources.
    adapter.begin_turn(message)
    result = client.send(message, files, [], cloud_tools=adapter)
    assert not script.steps and len(script.payloads) == 3
    assert "确认全部列对应" in result.text and not result.cloud_results
    pending = adapter.get_pending_mapping_question()
    assert pending and all(row["selected_source"] is None for row in pending["mappings"])
    assert not adapter.plans
    return client, script, files


def test_service_real_backend_pauses_then_native_answer_completes_one_multicolumn_task(tmp_path):
    adapter, fake = tools(tmp_path)
    client, script, files = first_service_turn(adapter)
    assert fake.applied_requests == 0
    native_answer = f"针对“{QUESTION}”：" + mapping_answer_text(SHEET, SELECTED)
    adapter.begin_turn(native_answer)

    def apply_response(payload):
        prepared = latest_tool_result(payload)
        assert prepared["ok"] and prepared["can_execute"], prepared
        assert "tool_choice" not in payload
        return model_call("wps_apply_table_update", {"plan_id": prepared["plan_id"]})

    def final_response(payload):
        outcome = latest_tool_result(payload)
        assert outcome["ok"] and outcome["status"] == "completed" and outcome["updated"] == 2, outcome
        return completed("两列已更新并核对。")

    script.steps = [read_response, prepare_response, apply_response, final_response]
    result = client.send(native_answer, files, [], cloud_tools=adapter)
    assert not script.steps and len(script.payloads) == 7
    assert adapter.get_pending_mapping_question() is None
    assert len(result.cloud_results) == 1 and result.cloud_results[0]["updated"] == 2
    assert fake.applied_requests == 2
    assert fake.at(3, 2)["value"] == 30 and fake.at(3, 3)["value"] == "已处理"


def test_service_new_upload_after_native_answer_requires_a_new_blank_mapping_form(tmp_path):
    adapter, fake = tools(tmp_path)
    client, script, files = first_service_turn(adapter)
    previous_form = adapter.get_pending_mapping_question()["mapping_id"]
    native_answer = f"针对“{QUESTION}”：" + mapping_answer_text(SHEET, SELECTED)
    adapter.begin_turn(native_answer)
    # A pending upload succeeds between the UI's begin_turn and send.
    files = [UploadedFile("file-review-replaced", files[0].name, files[0].size)]
    script.steps = [read_response, prepare_response, forced_mapping_response]
    result = client.send(native_answer, files, [], cloud_tools=adapter)
    assert not script.steps and len(script.payloads) == 6
    pending = adapter.get_pending_mapping_question()
    assert pending and pending["mapping_id"] != previous_form
    assert all(row["selected_source"] is None for row in pending["mappings"])
    assert "确认全部列对应" in result.text and fake.applied_requests == 0
    assert not result.cloud_results and not adapter.plans


def test_forced_form_failure_marks_streamed_fake_completion_for_ui_removal(tmp_path):
    adapter, fake = tools(tmp_path)
    script = RealBackendResponseScript()
    fake_success = "已完成全部云文档更新。"

    def fake_completed_response(payload):
        assert latest_tool_result(payload)["status"] == "needs_mapping"
        assert payload["tool_choice"] == {"type": "function", "name": "wps_confirm_mapping"}
        return [{"type": "response.output_text.delta", "delta": fake_success}, completed(fake_success)]

    script.steps = [read_response, prepare_response, fake_completed_response]
    client = FinanceAIClient(AISettings(api_key="sk-test-never-real"), opener=script)
    files = [UploadedFile(**entry) for entry in FILES]
    streamed = []
    adapter.begin_turn("按业务单号更新到账金额和状态")
    with pytest.raises(AIError, match="列对应选择尚未准备完成") as error:
        client.send("按业务单号更新到账金额和状态", files, [], cloud_tools=adapter, on_text=streamed.append)
    assert streamed == [fake_success]  # A genuine SSE delta reached the display callback.
    assert error.value.discard_partial_text is True
    assert error.value.partial_result.text == "" and not error.value.cloud_results
    assert not adapter.plans and fake.applied_requests == 0 and not script.steps
