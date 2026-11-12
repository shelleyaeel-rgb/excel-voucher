"""Provider routing checks without account credentials or cloud writes."""
from dataclasses import replace
import copy
import hashlib
import io
import json
import math

import pytest
from openpyxl import Workbook
from openpyxl.utils.datetime import CALENDAR_MAC_1904, CALENDAR_WINDOWS_1900

from xiaoyu import wps_adapter as adapter
from xiaoyu import wps_snapshot as snapshot
from xiaoyu.cloud_tools import CloudDocumentTools
from mapping_test_support import confirm_columns
from xiaoyu.wps_service import WpsBinding, WpsError


def account_binding():
    return WpsBinding("test-document", "测试表", "https://www.kdocs.cn/l/example",
                      workbook_name="测试.xlsx", provider="skillhub",
                      account_id="test-account", file_id="real-file-id")


def test_router_preserves_legacy_provider_and_selects_account_provider(monkeypatch):
    calls = []
    monkeypatch.setattr(adapter, "WpsClient", lambda binding: calls.append(binding) or "legacy")
    legacy = WpsBinding("legacy", "旧连接", "https://www.kdocs.cn/l/legacy",
        "https://www.kdocs.cn/api/v3/ide/file/legacy-file/script/script-id/sync_task", "offline-token")
    assert adapter.create_wps_client(legacy) == "legacy" and calls == [legacy]
    client = adapter.create_wps_client(account_binding())
    assert isinstance(client, adapter.SkillHubWpsClient)
    assert client.binding.provider == "skillhub" and len(calls) == 1


def test_invalid_write_fails_before_rpc_or_account_loading():
    class NeverRpc:
        def call_tool(self, *args, **kwargs):
            raise AssertionError("An unsupported write must not make any RPC")
    client = adapter.SkillHubWpsClient(account_binding(), rpc=NeverRpc())
    with pytest.raises(WpsError) as caught:
        client.call("apply", **payload(new_value="=SUM(A1:A2)"))
    assert caught.value.code == "INVALID_NEW_VALUE"
    assert caught.value.mutation_attempted is False


def test_account_adapter_copies_binding_and_rejects_legacy_identity():
    binding = account_binding()
    client = adapter.SkillHubWpsClient(binding)
    binding.account_id = "another-account"
    assert client.binding.account_id == "test-account"
    with pytest.raises(WpsError):
        adapter.SkillHubWpsClient(replace(binding, provider="airscript"))


def envelope(data):
    return {"code": 0, "message": "成功", "data": data}


def record(row, col, value, *, fmt="G/通用格式", formula="", kind=None):
    item = dict(rowFrom=row - 1, rowTo=row - 1, colFrom=col - 1, colTo=col - 1,
                isMerge=False, isCellPic=False, cellValueType="basic", numFormat=fmt,
                originalCellValue=str(value), understandableType={
                    "type": kind or ("bool" if type(value) is bool else "double" if type(value) in (int, float) else "string"),
                    "value": value})
    if formula:
        item["fmlaText"] = formula
    return item


def payload(**change_overrides):
    change = dict(row=2, key_values=["001"], old_value=10, old_formula="", old_number_format="0.00",
                  new_value=20, guards=[dict(column=3, value="待更新", formula="")])
    change.update(change_overrides)
    return dict(task_id="task-1", batch_id="batch-1", sheet="汇总", header_row=1,
                expected_headers=[dict(column=1, value="单号"), dict(column=2, value="金额"), dict(column=3, value="状态")],
                key_columns=[1], row_start=2, row_end=3, target_column=2, changes=[change])


class Rpc:
    """A typed cell store using the envelopes captured from real SkillHub."""
    def __init__(self):
        self.calls = []
        self.can_edit = True
        self.file_id = "real-file-id"
        self.name = "测试.xlsx"
        self.merged = []
        self.write_error = None
        self.corrupt_write = False
        self.formats = {(2, 2): "0.00"}
        self.cells = {(1, 1): record(1, 1, "单号"), (1, 2): record(1, 2, "金额"),
                      (1, 3): record(1, 3, "状态"), (2, 1): record(2, 1, "001"),
                      (2, 2): record(2, 2, 10, fmt="0.00"), (2, 3): record(2, 3, "待更新")}

    def call_tool(self, name, arguments, mutation=False):
        self.calls.append((name, copy.deepcopy(arguments), mutation))
        if name == "get_file_info":
            return envelope({"code": 0, "data": {"id": self.file_id, "name": self.name,
                "type": "file", "version": 3, "permission": {"update": self.can_edit}}})
        if name == "sheet.get_sheets_info":
            return envelope({"result": "ok", "detail": {"sheetsInfo": [{"sheetName": "汇总",
                "sheetId": 8, "sheetType": "et", "rowFrom": 0, "rowTo": 2, "colFrom": 0, "colTo": 2,
                "isProtected": False}]}})
        if name == "sheet.get_merged_cells":
            return envelope({"merged_ranges": copy.deepcopy(self.merged)})
        if name == "sheet.get_range_data":
            area = arguments["range"]
            items = [copy.deepcopy(item) for (r, c), item in self.cells.items()
                if area["rowFrom"] <= r - 1 <= area["rowTo"] and area["colFrom"] <= c - 1 <= area["colTo"]]
            return envelope({"result": "ok", "detail": {"rangeData": items or None}})
        if name == "sheet.range_data_batch_update":
            assert mutation is True
            if self.write_error:
                raise self.write_error
            for op in arguments["range_data"]:
                assert op["op_type"] == "cell_operation_type_formula"
                position = op["row_from"] + 1, op["col_from"] + 1
                text = op["formula"]
                value = text[1:] if text.startswith("'") else text == "TRUE" if text in ("TRUE", "FALSE") else "" if text == "" else json.loads(text)
                fmt = self.cells.get(position, {}).get("numFormat", self.formats.get(position, "General"))
                self.formats[position] = fmt
                if value == "":
                    self.cells.pop(position, None)
                else:
                    self.cells[position] = record(*position, "wrong" if self.corrupt_write else value, fmt=fmt)
            return envelope({"code": 0, "data": {}, "msg": ""})
        raise AssertionError(name)

    @property
    def writes(self):
        return [c for c in self.calls if c[2]]


@pytest.fixture(autouse=True)
def offline_snapshot(monkeypatch):
    # Network/hash/export verification itself is covered in test_wps_snapshot;
    # these tests exercise integration with the sparse API's blank cells.
    def load(rpc, file_id, sheet_name, missing, known, *, expected_version, metadata_coordinates=()):
        assert expected_version == 3 and known
        result = {key: dict(value="", formula="", has_formula=False, merged=False,
                            number_format=rpc.formats.get(key, "General")) for key in missing}
        result.update({key: dict(known[key], date_system="1900") for key in metadata_coordinates})
        return result
    monkeypatch.setattr(adapter, "load_blank_cells", load)
    monkeypatch.setattr(adapter.time, "sleep", lambda _: None)


def test_resolve_uses_real_identity_without_credentials_and_describe_separates_permission():
    rpc = Rpc()
    rpc.can_edit = False
    binding = adapter.resolve_skillhub_document("https://kdocs.cn/l/example", "test-account", rpc)
    assert binding.file_id == "real-file-id" and binding.provider == "skillhub"
    assert binding.token == binding.endpoint == ""
    result = adapter.SkillHubWpsClient(binding, rpc).call("describe")
    assert result["can_edit"] is False and result["file_id"] == "real-file-id"
    assert result["sheets"] == [dict(name="汇总", used_start_row=1, used_end_row=3, used_start_column=1, used_end_column=3)]
    assert not rpc.writes


def test_read_preserves_formulas_dates_zero_bool_text_and_merged_blank_children():
    rpc = Rpc()
    rpc.cells = {(1, 1): record(1, 1, "00123"), (1, 2): record(1, 2, 0),
                 (1, 3): record(1, 3, False), (2, 1): record(2, 1, "", formula='=""'),
                 (2, 2): dict(record(2, 2, "2026-11-10", kind="date"), originalCellValue="46336")}
    rpc.merged = [dict(row_from=2, row_to=2, col_from=0, col_to=2)]
    result = adapter.SkillHubWpsClient(account_binding(), rpc).call("read", sheet="汇总", start_row=1, end_row=3, columns=[3, 1, 2])
    first, second, third = [r["cells"] for r in result["rows"]]
    assert [c["value"] for c in first] == [False, "00123", 0]
    assert second[1]["has_formula"] and second[1]["formula"] == '=""'
    assert second[2]["value"] == 46336
    assert all(c["merged"] for c in third)
    assert all(c["number_format"] == adapter.UNKNOWN_FORMAT for c in third)


def test_read_merge_boundaries_keep_adjacent_headers_separate_and_plain_cells_unmarked():
    rpc = Rpc()
    rpc.cells = {(1, 1): record(1, 1, "日期"), (1, 3): record(1, 3, "金额"),
                 (1, 5): record(1, 5, "备注")}
    rpc.merged = [dict(row_from=0, row_to=0, col_from=0, col_to=1),
                  dict(row_from=0, row_to=0, col_from=2, col_to=3)]
    result = adapter.SkillHubWpsClient(account_binding(), rpc).call(
        "read", sheet="汇总", start_row=1, end_row=1, columns=[1, 2, 3, 4, 5])
    first, second, third, fourth, plain = result["rows"][0]["cells"]
    assert first["merge_range"] == second["merge_range"] == dict(
        start_row=1, end_row=1, start_column=1, end_column=2)
    assert third["merge_range"] == fourth["merge_range"] == dict(
        start_row=1, end_row=1, start_column=3, end_column=4)
    assert all(cell["merged"] for cell in (first, second, third, fourth))
    assert plain["merged"] is False and "merge_range" not in plain


@pytest.mark.parametrize("area,row,column,expected", [
    (dict(row_from=0, row_to=0, col_from=0, col_to=1), 1, 2,
     dict(start_row=1, end_row=1, start_column=1, end_column=2)),
    (dict(row_from=0, row_to=1, col_from=0, col_to=0), 2, 1,
     dict(start_row=1, end_row=2, start_column=1, end_column=1)),
])
def test_read_merge_range_preserves_full_bounds_when_anchor_is_outside_read(area, row, column, expected):
    rpc = Rpc()
    rpc.cells = {(1, 1): record(1, 1, "合并标题")}
    rpc.merged = [area]
    result = adapter.SkillHubWpsClient(account_binding(), rpc).call(
        "read", sheet="汇总", start_row=row, end_row=row, columns=[column])
    cell = result["rows"][0]["cells"][0]
    assert cell["merge_range"] == expected and cell["merged"] is True
    assert cell["value"] == ""


def test_sparse_merged_flag_alone_does_not_claim_trusted_merge_boundaries():
    rpc = Rpc()
    rpc.cells[(1, 1)]["isMerge"] = True
    result = adapter.SkillHubWpsClient(account_binding(), rpc).call(
        "read", sheet="汇总", start_row=1, end_row=1, columns=[1])
    cell = result["rows"][0]["cells"][0]
    assert cell["merged"] is True and "merge_range" not in cell


def test_conflicting_overlapping_merge_ranges_are_rejected_without_writes():
    rpc = Rpc()
    rpc.merged = [dict(row_from=0, row_to=0, col_from=0, col_to=1),
                  dict(row_from=0, row_to=0, col_from=1, col_to=2)]
    with pytest.raises(WpsError) as caught:
        adapter.SkillHubWpsClient(account_binding(), rpc).call(
            "read", sheet="汇总", start_row=1, end_row=1, columns=[1, 2, 3])
    assert caught.value.code == "INVALID_RESPONSE" and not rpc.writes


def date_column_case():
    rpc = Rpc()
    rpc.cells = {(1, 6): record(1, 6, "序号"), (1, 7): record(1, 7, "日期"),
                 (2, 6): record(2, 6, 1), (3, 6): record(3, 6, 2)}
    rpc.cells[(1, 7)]["isMerge"] = True
    for row in (2, 3):
        rpc.cells[(row, 7)] = record(row, 7, "", fmt='G/通用格式"月"',
                                    formula=f'=IF(H{row}="","",MONTH(H{row}))')
        rpc.formats[(row, 8)] = "yyyy-mm-dd"
    rpc.merged = [dict(row_from=0, row_to=0, col_from=6, col_to=7)]
    area = dict(start_row=1, end_row=1, start_column=7, end_column=8)
    args = dict(task_id="date-task", batch_id="date-batch", sheet="汇总", header_row=1,
        expected_headers=[dict(column=6, value="序号"), dict(column=7, value="日期", merge_range=area),
                          dict(column=8, value="", merge_range=area)],
        key_columns=[6], row_start=2, row_end=3, target_column=8,
        changes=[dict(row=2, key_values=[1], old_value="", old_formula="", old_number_format="yyyy-mm-dd",
                      new_value=46332, guards=[dict(column=6, value=1, formula=""),
                          dict(column=7, value="", formula=rpc.cells[(2, 7)]["fmlaText"])])])
    return rpc, args


def test_date_input_under_merged_header_writes_child_column_and_preserves_month_formula():
    rpc, args = date_column_case()
    before = copy.deepcopy(rpc.cells)
    result = adapter.SkillHubWpsClient(account_binding(), rpc).call("apply", **args)
    assert result["outcomes"][0]["status"] == "applied" and len(rpc.writes) == 1
    assert rpc.writes[0][1]["range_data"] == [dict(op_type="cell_operation_type_formula",
        row_from=1, row_to=1, col_from=7, col_to=7, formula="46332")]
    assert rpc.cells[(2, 8)]["understandableType"]["value"] == 46332
    assert rpc.cells[(2, 8)]["numFormat"] == "yyyy-mm-dd"
    assert {key: cell for key, cell in rpc.cells.items() if key != (2, 8)} == before


@pytest.mark.parametrize("change", ["unmerged", "expanded", "adjacent_shift"])
def test_date_header_merge_structure_change_blocks_write(change):
    rpc, args = date_column_case()
    if change == "unmerged":
        rpc.merged = []
        rpc.cells[(1, 7)]["isMerge"] = False
    elif change == "expanded":
        rpc.merged[0]["col_to"] = 8
    else:
        rpc.merged = [dict(row_from=0, row_to=0, col_from=7, col_to=8)]
        rpc.cells[(1, 7)]["isMerge"] = False
    with pytest.raises(WpsError) as caught:
        adapter.SkillHubWpsClient(account_binding(), rpc).call("apply", **args)
    assert caught.value.code == "HEADER_MISMATCH" and not caught.value.mutation_attempted
    assert not rpc.writes


def test_merged_header_allowance_never_allows_a_merged_data_target():
    rpc, args = date_column_case()
    rpc.merged.append(dict(row_from=1, row_to=1, col_from=6, col_to=7))
    result = adapter.SkillHubWpsClient(account_binding(), rpc).call("apply", **args)
    assert result["outcomes"][0]["status"] == "merged" and not rpc.writes


def simulate_date_storage(rpc, stored_value):
    original = rpc.call_tool

    def call_tool(name, arguments, mutation=False):
        reply = original(name, arguments, mutation)
        if name == "sheet.range_data_batch_update":
            rpc.cells[(2, 8)] = record(2, 8, stored_value, fmt=rpc.formats[(2, 8)])
        return reply

    rpc.call_tool = call_tool


def test_date_write_accepts_only_known_storage_truncation_preserving_millisecond():
    rpc, args = date_column_case()
    args["changes"][0]["new_value"] = 46332.12345678917
    simulate_date_storage(rpc, 46332.1234567891)
    result = adapter.SkillHubWpsClient(account_binding(), rpc).call("apply", **args)
    assert result["outcomes"][0]["status"] == "applied" and len(rpc.writes) == 1


@pytest.mark.parametrize("saved,fmt", [
    (46332.1234567892, "yyyy-mm-dd"),  # A nearby rounded value is not the observed storage rule.
    (46332.1234567800, "yyyy-mm-dd"),  # An actual timestamp change is never accepted.
    (46332.1234567891, "0.000000000000"),  # The same truncation is not allowed for ordinary numbers.
])
def test_date_storage_rule_cannot_mask_unexpected_timestamp_or_numeric_changes(saved, fmt):
    rpc, args = date_column_case()
    rpc.formats[(2, 8)] = fmt
    args["changes"][0].update(new_value=46332.12345678917, old_number_format=fmt)
    simulate_date_storage(rpc, saved)
    with pytest.raises(WpsError) as caught:
        adapter.SkillHubWpsClient(account_binding(), rpc).call("apply", **args)
    assert caught.value.code == "WRITE_VERIFY_FAILED" and caught.value.mutation_attempted
    assert len(rpc.writes) == 1


@pytest.mark.parametrize("location", ["old_value", "key", "guard"])
def test_date_storage_equivalence_never_relaxes_prewrite_matching(location):
    rpc, args = date_column_case()
    expected, truncated = 46332.12345678917, 46332.1234567891
    if location == "old_value":
        rpc.cells[(2, 8)] = record(2, 8, truncated, fmt="yyyy-mm-dd")
        args["changes"][0]["old_value"] = expected
    elif location == "key":
        rpc.cells[(2, 6)] = record(2, 6, truncated, fmt="yyyy-mm-dd")
        args["changes"][0]["key_values"] = [expected]
    else:
        rpc.cells[(2, 7)] = record(2, 7, truncated, fmt="yyyy-mm-dd")
        args["changes"][0]["guards"][1].update(value=expected, formula="")
    result = adapter.SkillHubWpsClient(account_binding(), rpc).call("apply", **args)
    assert result["outcomes"][0]["status"] == ("not_found" if location == "key" else "conflict")
    assert not rpc.writes and result["mutation_attempted"] is False


@pytest.mark.parametrize("new,wire", [(0, "0"), (12.34, "12.34"), (False, "FALSE"), (None, ""),
    ("00123", "'00123"), ("2026-11-10", "'2026-11-10"), ("'abc", "''abc")])
def test_apply_typed_content_only_batch_then_independent_readback(new, wire):
    rpc = Rpc()
    result = adapter.SkillHubWpsClient(account_binding(), rpc).call("apply", **payload(new_value=new))
    assert result["mutation_attempted"] and result["outcomes"][0]["status"] == "applied"
    assert len(rpc.writes) == 1
    op = rpc.writes[0][1]["range_data"][0]
    assert op == dict(op_type="cell_operation_type_formula", row_from=1, row_to=1, col_from=1, col_to=1, formula=wire)
    assert rpc.calls[-1][0] == "sheet.get_merged_cells"
    assert rpc.formats[(2, 2)] == "0.00"


@pytest.mark.parametrize("case,status", [("changed", "conflict"), ("guard", "conflict"),
    ("formula", "formula"), ("merged", "merged"), ("duplicate", "duplicate"), ("missing", "not_found")])
def test_apply_protects_old_values_guards_formulas_merges_and_business_keys(case, status):
    rpc = Rpc()
    if case == "changed":
        rpc.cells[(2, 2)] = record(2, 2, 99, fmt="0.00")
    elif case == "guard":
        rpc.cells[(2, 3)] = record(2, 3, "不更新")
    elif case == "formula":
        rpc.cells[(2, 2)]["fmlaText"] = "=5+5"
    elif case == "merged":
        rpc.merged = [dict(row_from=1, row_to=1, col_from=1, col_to=2)]
    elif case == "duplicate":
        rpc.cells[(3, 1)] = record(3, 1, "001")
    else:
        rpc.cells[(2, 1)] = record(2, 1, "002")
    result = adapter.SkillHubWpsClient(account_binding(), rpc).call("apply", **payload())
    assert result["outcomes"][0]["status"] == status
    assert not result["mutation_attempted"] and not rpc.writes


def test_apply_finds_moved_row_by_key_and_numeric_key_matches_json_number_type():
    rpc = Rpc()
    for col in (1, 2, 3):
        rpc.cells[(3, col)] = rpc.cells.pop((2, col))
        rpc.cells[(3, col)].update(rowFrom=2, rowTo=2)
    rpc.cells[(3, 1)] = record(3, 1, 1)
    result = adapter.SkillHubWpsClient(account_binding(), rpc).call("apply", **payload(key_values=[1.0]))
    assert result["outcomes"][0]["row"] == 3
    assert result["outcomes"][0]["status"] == "applied"


@pytest.mark.parametrize("case,code", [("permission", "ACCESS_DENIED"), ("header", "HEADER_MISMATCH"),
    ("identity", "WORKBOOK_MISMATCH"),
    ("text_format", "UNSUPPORTED_CELL_METADATA")])
def test_prewrite_failures_are_definitely_unsubmitted(case, code):
    rpc, args = Rpc(), payload()
    if case == "permission":
        rpc.can_edit = False
    elif case == "header":
        rpc.cells[(1, 2)] = record(1, 2, "别的列")
    elif case == "identity":
        rpc.file_id = "different-file"
    else:
        rpc.cells[(2, 2)]["numFormat"] = "@"
        args = payload(old_number_format="@")
    with pytest.raises(WpsError) as caught:
        adapter.SkillHubWpsClient(account_binding(), rpc).call("apply", **args)
    assert caught.value.code == code and not caught.value.mutation_attempted
    assert not rpc.writes


def test_timeout_is_never_replayed_and_retains_unknown_mutation():
    rpc = Rpc()
    rpc.write_error = WpsError("timeout", code="NETWORK_ERROR", mutation_attempted=True)
    with pytest.raises(WpsError) as caught:
        adapter.SkillHubWpsClient(account_binding(), rpc).call("apply", **payload())
    assert caught.value.mutation_attempted and caught.value.result["task_id"] == "task-1"
    assert len(rpc.writes) == 1


def test_typed_readback_mismatch_is_uncertain_even_after_success_response():
    rpc = Rpc()
    rpc.corrupt_write = True
    with pytest.raises(WpsError) as caught:
        adapter.SkillHubWpsClient(account_binding(), rpc).call("apply", **payload())
    assert caught.value.code == "WRITE_VERIFY_FAILED" and caught.value.mutation_attempted
    assert len(rpc.writes) == 1


def test_malformed_metadata_cannot_be_assumed_blank():
    rpc = Rpc()
    rpc.cells[(2, 2)].pop("understandableType")
    with pytest.raises(WpsError) as caught:
        adapter.SkillHubWpsClient(account_binding(), rpc).call("read", sheet="汇总", start_row=2, end_row=2, columns=[2])
    assert caught.value.code == "INVALID_RESPONSE"


def test_configured_account_path_is_used_for_loading_and_expiry(monkeypatch, tmp_path):
    from xiaoyu import wps_skillhub
    seen = []
    target = tmp_path / "account.json"
    def require(account_id, path):
        seen.append(("load", account_id, path))
        raise WpsError("已过期", code="AUTH_REQUIRED")
    monkeypatch.setattr(wps_skillhub, "require_account", require)
    monkeypatch.setattr(wps_skillhub, "mark_account_expired", lambda account_id, path: seen.append(("expire", account_id, path)))
    with pytest.raises(WpsError) as caught:
        adapter.create_wps_client(account_binding(), account_path=target).call("describe")
    assert caught.value.code == "AUTH_REQUIRED"
    assert seen == [("load", "test-account", target), ("expire", "test-account", target)]


def test_formatted_blank_write_uses_verified_export_format():
    rpc = Rpc()
    del rpc.cells[(2, 2)]
    result = adapter.SkillHubWpsClient(account_binding(), rpc).call("apply", **payload(old_value=""))
    assert result["outcomes"][0]["status"] == "applied"
    assert rpc.cells[(2, 2)]["understandableType"]["value"] == 20
    assert rpc.cells[(2, 2)]["numFormat"] == "0.00"


def test_pure_blank_range_obtains_independent_live_header_anchor():
    rpc = Rpc()
    del rpc.cells[(2, 2)]
    result = adapter.SkillHubWpsClient(account_binding(), rpc).call("read", sheet="汇总", start_row=2, end_row=2, columns=[2])
    assert result["rows"][0]["cells"][0]["number_format"] == "0.00"
    reads = [c[1]["range"] for c in rpc.calls if c[0] == "sheet.get_range_data"]
    assert len(reads) == 2 and reads[-1]["rowFrom"] == 0


def test_stale_export_is_rejected_before_writing(monkeypatch):
    rpc = Rpc()
    del rpc.cells[(2, 2)]
    attempts = []
    def stale(*args, **kwargs):
        attempts.append(1)
        raise WpsError("stale", code="SNAPSHOT_CHANGED")
    monkeypatch.setattr(adapter, "load_blank_cells", stale)
    with pytest.raises(WpsError) as caught:
        adapter.SkillHubWpsClient(account_binding(), rpc).call("apply", **payload(old_value=""))
    assert caught.value.code == "SNAPSHOT_CHANGED" and not caught.value.mutation_attempted
    assert not rpc.writes and len(attempts) == 3


@pytest.mark.parametrize("wire,expected", [(401, "AUTH_REQUIRED"), (400006, "AUTH_REQUIRED"),
    (403, "ACCESS_DENIED"), (429, "RATE_LIMITED")])
def test_nested_service_errors_preserve_actionable_codes(wire, expected):
    with pytest.raises(WpsError) as caught:
        adapter._data(envelope({"code": wire, "data": {}}), mutation=True)
    assert caught.value.code == expected and caught.value.mutation_attempted


def test_stale_export_retry_restarts_live_read_without_replaying_write(monkeypatch):
    rpc = Rpc()
    original = adapter.load_blank_cells
    attempts = []
    def lag(*args, **kwargs):
        attempts.append(1)
        if len(attempts) == 1:
            raise WpsError("saving", code="SNAPSHOT_CHANGED")
        return original(*args, **kwargs)
    monkeypatch.setattr(adapter, "load_blank_cells", lag)
    result = adapter.SkillHubWpsClient(account_binding(), rpc).call("apply", **payload())
    assert result["outcomes"][0]["status"] == "applied" and len(rpc.writes) == 1
    assert len(attempts) >= 2


def test_nonxlsx_read_keeps_unknown_blanks_but_clear_fails_before_write():
    rpc = Rpc()
    rpc.name = "测试.ksheet"
    client = adapter.SkillHubWpsClient(replace(account_binding(), workbook_name=rpc.name), rpc)
    result = client.call("read", sheet="汇总", start_row=2, end_row=3, columns=[2])
    assert result["rows"][0]["cells"][0]["value"] == 10
    assert result["rows"][1]["cells"][0]["number_format"] == adapter.UNKNOWN_FORMAT
    with pytest.raises(WpsError) as caught:
        client.call("apply", **payload(new_value=None))
    assert caught.value.code == "UNSUPPORTED_CELL_METADATA" and not rpc.writes


def test_postwrite_snapshot_lag_retries_only_reads(monkeypatch):
    rpc = Rpc()
    original = adapter.load_blank_cells
    lag_count = []
    def lag(*args, **kwargs):
        if rpc.writes and len(lag_count) < 2:
            lag_count.append(1)
            raise WpsError("saving", code="SNAPSHOT_CHANGED")
        return original(*args, **kwargs)
    monkeypatch.setattr(adapter, "load_blank_cells", lag)
    result = adapter.SkillHubWpsClient(account_binding(), rpc).call("apply", **payload(new_value=None))
    assert result["outcomes"][0]["status"] == "applied"
    assert len(lag_count) == 2 and len(rpc.writes) == 1


@pytest.mark.parametrize("code,field", [
    ("FORMAT_MISMATCH", "number_format"), ("VALUE_MISMATCH", "value"),
    ("TYPE_MISMATCH", "type"), ("FORMULA_MISMATCH", "formula"),
    ("MERGE_MISMATCH", "merged"),
])
@pytest.mark.parametrize("phase", ["before", "after"])
def test_deterministic_snapshot_failure_preserves_diagnostic_without_retry_or_write_replay(monkeypatch, code, field, phase):
    rpc = Rpc()
    original = adapter.load_blank_cells
    diagnostic = dict(stage="snapshot", sheet="汇总", row=2, column=2, field=field)
    error = WpsError("下载副本核对失败", code=code, diagnostic=diagnostic)
    failures = []

    def mismatch(*args, **kwargs):
        if phase == "before" or rpc.writes:
            failures.append(1)
            raise error
        return original(*args, **kwargs)

    monkeypatch.setattr(adapter, "load_blank_cells", mismatch)
    with pytest.raises(WpsError) as caught:
        adapter.SkillHubWpsClient(account_binding(), rpc).call("apply", **payload(new_value=None))
    assert caught.value is error and caught.value.diagnostic == diagnostic
    assert len(failures) == 1
    assert caught.value.mutation_attempted is (phase == "after")
    assert len(rpc.writes) == (1 if phase == "after" else 0)
    if phase == "after":
        assert caught.value.result["task_id"] == "task-1"
        assert caught.value.result["batch_id"] == "batch-1"
        assert caught.value.result["mutation_attempted"] is True
    else:
        assert caught.value.result is None


@pytest.mark.parametrize("phase", ["before", "after"])
def test_snapshot_retry_exhaustion_retains_final_diagnostic_and_never_replays_write(monkeypatch, phase):
    rpc = Rpc()
    original = adapter.load_blank_cells
    failures = []

    def lag(*args, **kwargs):
        if phase == "before" or rpc.writes:
            error = WpsError("云端保存尚未同步", code="SNAPSHOT_CHANGED",
                             diagnostic=dict(stage="snapshot", sheet="汇总", field="version"))
            failures.append(error)
            raise error
        return original(*args, **kwargs)

    monkeypatch.setattr(adapter, "load_blank_cells", lag)
    with pytest.raises(WpsError) as caught:
        adapter.SkillHubWpsClient(account_binding(), rpc).call("apply", **payload(new_value=None))
    assert len(failures) == 3 and caught.value is failures[-1]
    assert caught.value.diagnostic == dict(stage="snapshot", sheet="汇总", field="version")
    assert caught.value.mutation_attempted is (phase == "after")
    assert len(rpc.writes) == (1 if phase == "after" else 0)


class ExportRpc(Rpc):
    """Live cells and a separately encoded XLSX, served entirely in memory."""
    def __init__(self, *, live_month_format='G/通用格式"月"', export_month_format='General"月"'):
        super().__init__()
        self.export_month_format = export_month_format
        self.cells[(1, 3)] = record(1, 3, "月份")
        self.cells[(2, 3)] = record(2, 3, 11, fmt=live_month_format)
        self.cells[(3, 1)] = record(3, 1, "002")
        self.cells[(3, 2)] = record(3, 2, 100, fmt="0.00")
        self.requests = []
        self._export()

    def _export(self):
        book = Workbook()
        sheet = book.active
        sheet.title = "汇总"
        for (row, column), item in self.cells.items():
            cell = sheet.cell(row, column, item["understandableType"]["value"])
            cell.number_format = (self.export_month_format if (row, column) == (2, 3)
                                  else "General" if item["numFormat"] == "G/通用格式" else item["numFormat"])
        stream = io.BytesIO()
        book.save(stream)
        book.close()
        self.raw = stream.getvalue()
        self.hash = dict(type="sha256", sum=hashlib.sha256(self.raw).hexdigest())

    def call_tool(self, name, arguments, mutation=False):
        if name == "download_file":
            assert mutation is False
            self.calls.append((name, copy.deepcopy(arguments), mutation))
            return envelope(dict(physical_size=len(self.raw), hashes=[self.hash],
                                 url="https://offline.ag.kdocs.cn/test?signature=offline-only"))
        result = super().call_tool(name, arguments, mutation)
        if name == "get_file_info":
            result["data"]["data"]["hash"] = self.hash
        elif name == "sheet.range_data_batch_update":
            self._export()
        return result

    def open(self, request, timeout):
        self.requests.append(request)
        assert request.full_url == "https://offline.ag.kdocs.cn/test?signature=offline-only"
        assert request.header_items() == [("Accept", "application/octet-stream")]
        return io.BytesIO(self.raw)


class DateExportRpc(ExportRpc):
    """A real dated XLSX export, with no network and no assumed date epoch."""
    def __init__(self, *, epoch, filled=True):
        Rpc.__init__(self)
        self.epoch = epoch
        self.requests = []
        self.formats[(2, 2)] = "yyyy-mm-dd"
        self.cells[(1, 2)] = record(1, 2, "日期")
        if filled:
            self.cells[(2, 2)] = record(2, 2, 46000.5, fmt="yyyy-mm-dd")
        else:
            self.cells.pop((2, 2))
        self._export()

    def _export(self):
        book = Workbook()
        book.epoch = self.epoch
        sheet = book.active
        sheet.title = "汇总"
        for (row, column), fmt in self.formats.items():
            sheet.cell(row, column).number_format = fmt
        for (row, column), item in self.cells.items():
            sheet.cell(row, column, item["understandableType"]["value"])
            sheet.cell(row, column).number_format = snapshot.normalize_number_format(item["numFormat"])
        stream = io.BytesIO()
        book.save(stream)
        book.close()
        self.raw = stream.getvalue()
        self.hash = dict(type="sha256", sum=hashlib.sha256(self.raw).hexdigest())


@pytest.mark.parametrize("epoch,date_system", [(CALENDAR_WINDOWS_1900, "1900"), (CALENDAR_MAC_1904, "1904")])
def test_nonempty_date_only_range_has_verified_epoch_without_sparse_blanks(monkeypatch, tmp_path, epoch, date_system):
    rpc = DateExportRpc(epoch=epoch)
    export_cloud_tools(rpc, monkeypatch, tmp_path)
    result = adapter.SkillHubWpsClient(account_binding(), rpc).call(
        "read", sheet="汇总", start_row=2, end_row=2, columns=[2])
    value = result["rows"][0]["cells"][0]
    assert value["value"] == 46000.5 and value["date_system"] == date_system
    assert value["number_format"] == "yyyy-mm-dd"
    assert len(rpc.requests) == 1 and not rpc.writes


@pytest.mark.parametrize("epoch,date_system", [(CALENDAR_WINDOWS_1900, "1900"), (CALENDAR_MAC_1904, "1904")])
def test_date_epoch_survives_blank_to_nonempty_and_last_empty_cell_filled(monkeypatch, tmp_path, epoch, date_system):
    rpc = DateExportRpc(epoch=epoch, filled=False)
    export_cloud_tools(rpc, monkeypatch, tmp_path)
    client = adapter.SkillHubWpsClient(account_binding(), rpc)
    before = client.call("read", sheet="汇总", start_row=2, end_row=2, columns=[2])["rows"][0]["cells"][0]
    assert before["value"] == "" and before["date_system"] == date_system
    request = payload(old_value="", old_number_format="yyyy-mm-dd", new_value=46000.5)
    request["expected_headers"][1]["value"] = "日期"
    request["row_end"] = 2
    result = client.call("apply", **request)
    assert result["outcomes"][0]["status"] == "applied" and len(rpc.writes) == 1
    count = len(rpc.requests)
    after = client.call("read", sheet="汇总", start_row=2, end_row=2, columns=[2])["rows"][0]["cells"][0]
    assert after["value"] == 46000.5 and after["date_system"] == date_system
    assert len(rpc.requests) == count + 1


def test_date_epoch_is_verified_again_after_workbook_epoch_changes(monkeypatch, tmp_path):
    rpc = DateExportRpc(epoch=CALENDAR_WINDOWS_1900)
    export_cloud_tools(rpc, monkeypatch, tmp_path)
    client = adapter.SkillHubWpsClient(account_binding(), rpc)
    read = lambda: client.call("read", sheet="汇总", start_row=2, end_row=2, columns=[2])["rows"][0]["cells"][0]
    assert read()["date_system"] == "1900"
    rpc.epoch = CALENDAR_MAC_1904
    rpc._export()
    assert read()["date_system"] == "1904" and len(rpc.requests) == 2


def test_nonempty_date_metadata_rejects_one_ulp_live_export_difference(monkeypatch, tmp_path):
    rpc = DateExportRpc(epoch=CALENDAR_WINDOWS_1900)
    export_cloud_tools(rpc, monkeypatch, tmp_path)
    # Only the live value changes. The signed, hashed export remains unchanged.
    rpc.cells[(2, 2)] = record(2, 2, math.nextafter(46000.5, math.inf), fmt="yyyy-mm-dd")
    with pytest.raises(WpsError) as caught:
        adapter.SkillHubWpsClient(account_binding(), rpc).call(
            "read", sheet="汇总", start_row=2, end_row=2, columns=[2])
    assert caught.value.code == "VALUE_MISMATCH"
    assert len(rpc.requests) == 1 and not rpc.writes


def test_plain_numeric_range_does_not_download_for_date_metadata(monkeypatch, tmp_path):
    rpc = ExportRpc()
    export_cloud_tools(rpc, monkeypatch, tmp_path)
    result = adapter.SkillHubWpsClient(account_binding(), rpc).call(
        "read", sheet="汇总", start_row=2, end_row=2, columns=[1, 2, 3])
    assert [cell["value"] for cell in result["rows"][0]["cells"]] == ["001", 10, 11]
    assert not rpc.requests and not rpc.writes


def export_cloud_tools(rpc, monkeypatch, tmp_path):
    def load(*args, **kwargs):
        return snapshot.load_blank_cells(*args, **kwargs, opener=rpc)
    monkeypatch.setattr(adapter, "load_blank_cells", load)
    binding = account_binding()
    return CloudDocumentTools([binding], allow_write=True, journal_dir=tmp_path / "tasks",
                              client_factory=lambda item: adapter.SkillHubWpsClient(item, rpc))


def test_month_alias_read_plan_and_filtered_update_use_real_snapshot_validation(monkeypatch, tmp_path):
    rpc = ExportRpc()
    cloud = export_cloud_tools(rpc, monkeypatch, tmp_path)
    before = copy.deepcopy(rpc.cells)
    document = dict(document_url=account_binding().share_url, sheet="汇总")
    inspected = cloud.execute("wps_inspect", document)
    assert inspected["ok"], inspected
    read = cloud.execute("wps_read_table", dict(document, columns=["单号", "金额", "月份"],
                                               header_row=None, start_row=None, end_row=None))
    assert read["ok"], read
    assert (read["header_row"], read["start_row"], read["end_row"]) == (1, 2, 3)
    assert [row["values"] for row in read["rows"]] == [["001", 10, 11], ["002", 100, ""]]
    confirm_columns(cloud, read, [{"target_column": "金额", "source_field": "来源金额"}])
    plan = cloud.execute("wps_prepare_update", dict(snapshot_id=read["snapshot_id"],
        key_columns=["单号"], target_column="金额", allow_blank=False, summary="按单号更新金额",
        updates=[dict(key_values=["001"], new_value=20, reason="匹配到已上传来源记录")]))
    assert plan["ok"], plan
    result = cloud.execute("wps_apply_update", dict(plan_id=plan["plan_id"]))
    assert result["status"] == "completed" and result["updated"] == 1, result
    assert len(rpc.writes) == 1 and rpc.requests
    assert rpc.cells[(2, 2)]["understandableType"]["value"] == 20
    assert rpc.cells[(2, 2)]["numFormat"] == "0.00"
    assert {key: item for key, item in rpc.cells.items() if key != (2, 2)} == {
        key: item for key, item in before.items() if key != (2, 2)}


def test_real_general_text_difference_still_blocks_inspect_without_retry(monkeypatch, tmp_path):
    rpc = ExportRpc(live_month_format="G/通用格式", export_month_format="@")
    cloud = export_cloud_tools(rpc, monkeypatch, tmp_path)
    result = cloud.execute("wps_inspect", dict(document_url=account_binding().share_url, sheet="汇总"))
    assert result["ok"] is False and result["code"] == "FORMAT_MISMATCH", result
    assert len(rpc.requests) == 1 and not rpc.writes
