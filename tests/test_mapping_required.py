"""Mandatory user-selected source columns cannot be replaced by model claims."""
from copy import deepcopy

import pytest

from test_cloud_tools import URL, SHEET, read, tools
from test_mapping_gate_review import FILES, QUESTION, SELECTED, start, ask, answer, plan
from xiaoyu.cloud_mapping import KEEP_SOURCE, mapping_answer_text


def test_prepare_without_user_choice_creates_no_executable_or_internal_plan(tmp_path):
    cloud, fake = tools(tmp_path)
    snapshot = start(cloud)
    blocked = plan(cloud, snapshot)
    assert blocked["status"] == "needs_mapping" and blocked["required_action"] == "wps_confirm_mapping"
    assert not cloud.plans and not cloud.results and not list(cloud.journal_dir.glob("*.json"))
    assert fake.applied_requests == 0
    single = cloud.execute("wps_prepare_update", dict(snapshot_id=snapshot["snapshot_id"], key_columns=["业务单号"],
        target_column="到账金额", updates=[dict(key_values=["001"], new_value=99, reason="不能绕过旧工具")],
        allow_blank=False, summary="旧单列也必须确认"))
    assert single["status"] == "needs_mapping" and not cloud.plans and fake.applied_requests == 0


def test_form_cannot_invent_target_without_reading_real_table(tmp_path):
    cloud, fake = tools(tmp_path)
    result = cloud.execute("wps_confirm_mapping", dict(document_url=URL, sheet=SHEET, question=QUESTION,
        mappings=[dict(target_column="到账金额", source_options=["银行金额"], selected_source=None, note="")]))
    assert not result["ok"] and cloud.get_pending_mapping_question() is None
    read(cloud)
    result = cloud.execute("wps_confirm_mapping", dict(document_url=URL, sheet=SHEET, question=QUESTION,
        mappings=[dict(target_column="不存在的列", source_options=["银行金额"], selected_source=None, note="")]))
    assert not result["ok"] and cloud.get_pending_mapping_question() is None and fake.applied_requests == 0


def test_model_preselected_sources_are_cleared_and_do_not_authorize_plan(tmp_path):
    cloud, fake = tools(tmp_path)
    snapshot = start(cloud)
    result = cloud.execute("wps_confirm_mapping", dict(document_url=URL, sheet=SHEET, question=QUESTION,
        mappings=[dict(target_column=target, source_options=[source], selected_source=source, note="AI建议")
                  for target, source in SELECTED]))
    assert result["ok"] and all(m["selected_source"] is None for m in result["mappings"])
    assert plan(cloud, snapshot)["status"] == "needs_mapping" and fake.applied_requests == 0


@pytest.mark.parametrize("selections", [SELECTED[:1], [("到账金额", "虚构的来源列"), SELECTED[1]]])
def test_partial_or_fabricated_native_answer_keeps_pending_and_blocks_write(tmp_path, selections):
    cloud, fake = tools(tmp_path)
    snapshot = start(cloud)
    first = ask(cloud)
    cloud.begin_turn(f"针对“{QUESTION}”：" + mapping_answer_text(SHEET, selections))
    assert cloud.get_pending_mapping_question()["mapping_id"] == first["mapping_id"]
    assert plan(cloud, snapshot)["status"] == "needs_mapping" and fake.applied_requests == 0


def test_user_draft_persists_through_question_but_is_not_confirmation(tmp_path):
    cloud, fake = tools(tmp_path)
    snapshot = start(cloud)
    form = ask(cloud)
    cloud.update_mapping_draft(form["mapping_id"], {"到账金额": "银行金额", "状态": ""})
    cloud.begin_turn("处理结果这一列是什么？")
    pending = cloud.get_pending_mapping_question()
    assert pending["user_selections"] == {"到账金额": "银行金额", "状态": ""}
    assert plan(cloud, snapshot)["status"] == "needs_mapping" and fake.applied_requests == 0


def test_plain_chat_claim_of_confirmation_cannot_unlock_write(tmp_path):
    cloud, fake = tools(tmp_path)
    snapshot = start(cloud)
    ask(cloud)
    cloud.begin_turn("都按你说的对应，确认执行")
    assert plan(cloud, snapshot)["status"] == "needs_mapping" and fake.applied_requests == 0


def test_rule_change_after_confirmation_requires_new_selection(tmp_path):
    cloud, fake = tools(tmp_path)
    start(cloud)
    ask(cloud)
    answer(cloud)
    cloud.begin_turn("金额都取绝对值")
    blocked = plan(cloud, read(cloud))
    assert blocked["status"] == "needs_mapping" and fake.applied_requests == 0


def test_keep_target_can_be_excluded_from_fresh_read_without_reselecting_inputs(tmp_path):
    cloud, fake = tools(tmp_path)
    start(cloud)
    ask(cloud)
    answer(cloud, [("到账金额", "银行金额"), ("状态", KEEP_SOURCE)])
    snapshot = read(cloud, columns=["业务单号", "到账金额"])
    prepared = plan(cloud, snapshot, selected=SELECTED[:1])
    assert prepared["ok"], prepared
    result = cloud.execute("wps_apply_table_update", dict(plan_id=prepared["plan_id"]))
    assert result["ok"] and result["updated"] == 1
    assert fake.at(3, 3)["value"] == "已到账"


def test_formula_only_column_is_not_offered_as_an_update_choice(tmp_path):
    cloud, fake = tools(tmp_path)
    for row in (3, 4):
        fake.at(row, 3).update(formula="=1+1", has_formula=True)
    start(cloud)
    result = cloud.execute("wps_confirm_mapping", dict(document_url=URL, sheet=SHEET, question=QUESTION,
        mappings=[dict(target_column="状态", source_options=["银行状态"], selected_source=None, note="")]))
    assert not result["ok"] and "整列为公式" in result["error"]
    assert cloud.get_pending_mapping_question() is None and fake.applied_requests == 0


def test_source_context_order_does_not_invalidate_but_size_change_does(tmp_path):
    cloud, fake = tools(tmp_path)
    files = deepcopy(FILES) + [dict(file_id="other", name="补充.xlsx", size=256)]
    cloud.set_source_context(files)
    cloud.begin_turn("更新两列")
    snapshot = read(cloud)
    ask(cloud)
    answer(cloud)
    cloud.set_source_context(list(reversed(files)))
    prepared = plan(cloud, snapshot)
    assert prepared["ok"]
    files[0]["size"] += 1
    cloud.set_source_context(files)
    blocked = cloud.execute("wps_apply_table_update", dict(plan_id=prepared["plan_id"]))
    assert blocked["status"] == "needs_mapping" and fake.applied_requests == 0


def test_preview_mapping_confirmation_keeps_preview_until_explicit_execution(tmp_path):
    cloud, fake = tools(tmp_path, write=False)
    cloud.set_source_context(FILES)
    cloud.begin_turn("只预览这两列")
    snapshot = read(cloud)
    ask(cloud)
    answer(cloud)
    prepared = plan(cloud, snapshot)
    assert prepared["ok"] and not prepared["can_execute"]
    assert cloud.execute("wps_apply_table_update", dict(plan_id=prepared["plan_id"]))["status"] == "preview_only"
    cloud.begin_turn("确认执行")
    assert cloud.execute("wps_apply_table_update", dict(plan_id=prepared["plan_id"]))["ok"]
    assert fake.applied_requests == 2


def test_finished_task_consumes_selection_and_old_native_text_cannot_reuse_it(tmp_path):
    cloud, fake = tools(tmp_path)
    snapshot = start(cloud)
    ask(cloud)
    answer(cloud)
    prepared = plan(cloud, snapshot)
    assert cloud.execute("wps_apply_table_update", dict(plan_id=prepared["plan_id"]))["ok"]
    answer(cloud)
    assert plan(cloud, read(cloud))["status"] == "needs_mapping"
    assert fake.applied_requests == 2
