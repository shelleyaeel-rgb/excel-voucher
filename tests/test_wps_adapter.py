"""Provider routing checks without account credentials or cloud writes."""
from dataclasses import replace
import copy
import json

import pytest

from xiaoyu import wps_adapter as adapter
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
    def load(rpc, file_id, sheet_name, missing, known, *, expected_version):
        assert expected_version == 3 and known
        return {key: dict(value="", formula="", has_formula=False, merged=False,
                          number_format=rpc.formats.get(key, "General")) for key in missing}
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
