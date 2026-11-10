"""Offline transport checks and execution of the real AirScript in a JS mock.

No WPS request, real document, or user credential is used by these tests.
"""
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import urllib.error

import pytest

from xiaoyu import wps_service as wps


SHARE = "https://www.kdocs.cn/l/example_document"
ENDPOINT = "https://www.kdocs.cn/api/v3/ide/file/test_file/script/test_script/sync_task"
SCRIPT = Path(__file__).resolve().parents[1] / "assets" / "wps_airscript.js"


def binding(**kwargs):
    fields = dict(id="test-id", label="测试文档", share_url=SHARE, endpoint=ENDPOINT,
                  token="fake-token-for-offline-tests", workbook_name="测试.xlsx")
    fields.update(kwargs)
    return wps.WpsBinding(**fields)


def result(action="describe", **kwargs):
    fields = dict(ok=True, protocol=wps.PROTOCOL, action=action, workbook_name="测试.xlsx", mutation_attempted=False)
    if action == "describe":
        fields["sheets"] = [dict(name="汇总", used_start_row=1, used_end_row=5, used_start_column=1, used_end_column=3)]
    fields.update(kwargs)
    return fields


class Opener:
    def __init__(self, body=None, *, raw=None, error=None):
        self.raw = raw if raw is not None else json.dumps({"status": "finished", "data": {"result": body}}).encode()
        self.error = error
        self.requests = []

    def open(self, request, timeout):
        self.requests.append(request)
        if self.error:
            raise self.error
        return io.BytesIO(self.raw)


def test_share_normalization_and_extraction():
    assert wps.normalize_share_url("https://kdocs.cn/l/abc/?from=share#one") == "https://www.kdocs.cn/l/abc"
    assert wps.extract_share_links("填入（https://kdocs.cn/l/abc），再看 https://www.kdocs.cn/l/abc?x=1") == ["https://www.kdocs.cn/l/abc"]
    assert wps.extract_share_links("普通文字") == []


@pytest.mark.parametrize("url", ["http://www.kdocs.cn/l/a", "https://www.kdocs.cn.evil/l/a", "https://user@www.kdocs.cn/l/a",
                                      "https://www.kdocs.cn:443/l/a", "https://www.kdocs.cn/l/a/../b", "https://www.kdocs.cn/l/a\n"])
def test_reject_untrusted_share_urls(url):
    with pytest.raises(wps.WpsError):
        wps.normalize_share_url(url)


@pytest.mark.parametrize("endpoint", [ENDPOINT + "?x=1", ENDPOINT + "#part", ENDPOINT.replace("https:", "http:"),
                                           ENDPOINT.replace("www.kdocs.cn", "user@www.kdocs.cn"), ENDPOINT.replace("sync_task", "task"),
                                           ENDPOINT.replace("www.kdocs.cn", "www.kdocs.cn.evil")])
def test_reject_untrusted_webhooks(endpoint):
    with pytest.raises(wps.WpsError):
        wps.validate_binding(binding(endpoint=endpoint))


def test_binding_token_hidden_and_bad_token_rejected():
    item = binding()
    assert item.token not in repr(item)
    for token in ("", "secret\r\nOther: value", "has space", "中文"):
        with pytest.raises(wps.WpsError):
            wps.validate_binding(binding(token=token))


@pytest.mark.skipif(os.name != "nt", reason="Windows DPAPI")
def test_encrypted_binding_roundtrip(tmp_path):
    path = tmp_path / "wps.json"
    item = binding()
    assert wps.load_bindings(path) == []
    wps.save_bindings([item], path)
    assert item.token not in path.read_text(encoding="utf-8")
    assert wps.load_bindings(path) == [item]
    wps.save_bindings([], path)
    assert wps.load_bindings(path) == []


def test_duplicate_bindings_rejected_before_save(tmp_path):
    with pytest.raises(wps.WpsError, match="重复"):
        wps.save_bindings([binding(), binding(id="other")], tmp_path / "wps.json")
    assert list(tmp_path.iterdir()) == []


def test_encrypt_failure_preserves_file(tmp_path, monkeypatch):
    path = tmp_path / "wps.json"
    path.write_text("existing", encoding="utf-8")

    def fail(value):
        raise ValueError("private diagnostic")

    monkeypatch.setattr(wps, "_encrypt_key", fail)
    with pytest.raises(wps.WpsError) as caught:
        wps.save_bindings([binding()], path)
    assert "private diagnostic" not in str(caught.value)
    assert path.read_text() == "existing"


@pytest.mark.parametrize("content", ["{", "[]", '{"version":1,"documents":[{"token":"plain"}]}', '{"version":3,"documents":[]}'])
def test_bad_config_preserved(tmp_path, content):
    path = tmp_path / "wps.json"
    path.write_text(content, encoding="utf-8")
    with pytest.raises(wps.WpsError):
        wps.load_bindings(path)
    assert path.read_text(encoding="utf-8") == content


def test_account_binding_roundtrip_has_no_account_credential(tmp_path, monkeypatch):
    item = wps.WpsBinding("account-doc", "账号表", SHARE, workbook_name="测试.xlsx",
                          provider="skillhub", account_id="account-1", file_id="file-123")
    path = tmp_path / "wps.json"
    def no_credential_access(*args):
        raise AssertionError("Document bindings must not access account credentials")
    monkeypatch.setattr(wps, "_encrypt_key", no_credential_access)
    monkeypatch.setattr(wps, "_decrypt_key", no_credential_access)
    wps.save_bindings([item], path)
    persisted = json.loads(path.read_text(encoding="utf-8"))
    assert persisted["version"] == 2
    assert not {"token", "token_protected", "access_token", "refresh_token", "endpoint"}.intersection(persisted["documents"][0])
    assert wps.load_bindings(path) == [item]
    assert wps.binding_identity(item) == {"provider": "skillhub", "account_id": "account-1", "file_id": "file-123"}


def test_account_binding_cannot_accidentally_use_legacy_transport():
    item = wps.WpsBinding("account-doc", "账号表", SHARE, workbook_name="测试.xlsx",
                          provider="skillhub", account_id="account-1", file_id="file-123")
    with pytest.raises(wps.WpsError, match="WPS 账号连接"):
        wps.WpsClient(item, Opener(result()))


def test_legacy_v1_binding_migrates_without_losing_credentials(tmp_path, monkeypatch):
    item = binding()
    monkeypatch.setattr(wps, "_encrypt_key", lambda token: "protected:" + token)
    monkeypatch.setattr(wps, "_decrypt_key", lambda token: token.removeprefix("protected:"))
    path = tmp_path / "wps.json"
    path.write_text(json.dumps({"version": 1, "documents": [{"id": item.id, "label": item.label,
        "share_url": item.share_url, "endpoint": item.endpoint, "token_protected": "protected:" + item.token,
        "workbook_name": item.workbook_name}]}), encoding="utf-8")
    loaded = wps.load_bindings(path)
    assert loaded == [item]
    assert wps.binding_identity(loaded[0]) == {"provider": "airscript", "account_id": "", "file_id": "test_file"}
    wps.save_bindings(loaded, path)
    assert json.loads(path.read_text(encoding="utf-8"))["version"] == 2
    assert wps.load_bindings(path) == [item]


@pytest.mark.parametrize("bad", [{"token": "secret"}, {"endpoint": ENDPOINT}, {"account_id": ""},
                                  {"file_id": ""}, {"file_id": "../../other"}, {"provider": "unknown"}])
def test_account_binding_rejects_credentials_and_ambiguous_identity(bad):
    fields = dict(id="account-doc", label="账号表", share_url=SHARE, workbook_name="测试.xlsx",
                  provider="skillhub", account_id="account-1", file_id="file-123")
    fields.update(bad)
    with pytest.raises(wps.WpsError):
        wps.validate_binding(wps.WpsBinding(**fields))


@pytest.mark.parametrize("credential", ["token", "token_protected", "access_token", "refresh_token"])
def test_account_binding_file_rejects_embedded_credentials(tmp_path, credential):
    item = dict(id="account-doc", label="账号表", share_url=SHARE, provider="skillhub",
                account_id="account-1", file_id="file-123", **{credential: "never-real-secret"})
    path = tmp_path / "wps.json"
    path.write_text(json.dumps({"version": 2, "documents": [item]}), encoding="utf-8")
    with pytest.raises(wps.WpsError) as caught:
        wps.load_bindings(path)
    assert "never-real-secret" not in str(caught.value)


def test_http_contract_and_unbound_describe():
    opener = Opener(result())
    item = binding(workbook_name="")
    client = wps.WpsClient(item, opener)
    assert client.call("describe")["workbook_name"] == "测试.xlsx"
    assert client.endpoint_file_id == "test_file"
    request = opener.requests[0]
    assert request.get_header("Airscript-token") == item.token
    assert json.loads(request.data)["Context"]["argv"] == {"action": "describe", "protocol": wps.PROTOCOL, "expected_workbook_name": ""}
    with pytest.raises(wps.WpsError) as caught:
        client.call("read", sheet="汇总", start_row=1, end_row=1, columns=[1])
    assert caught.value.code == "NAME_REQUIRED"
    assert len(opener.requests) == 1


@pytest.mark.parametrize("kwargs", [dict(start_row=1, end_row=501, columns=[1]), dict(start_row=True, end_row=1, columns=[1]),
                                   dict(start_row=1, end_row=1, columns=[1, 1]), dict(start_row=1, end_row=1, columns=list(range(1, 32))),
                                   dict(start_row=1, end_row=100001, columns=[1]), dict(start_row=1, end_row=1, columns=[16385])])
def test_read_limits_before_network(kwargs):
    opener = Opener()
    with pytest.raises(wps.WpsError):
        wps.WpsClient(binding(), opener).call("read", sheet="汇总", **kwargs)
    assert not opener.requests


def test_string_result_supported_and_wrong_name_rejected():
    assert wps.WpsClient(binding(), Opener(json.dumps(result()))).call("describe")["ok"]
    with pytest.raises(wps.WpsError):
        wps.WpsClient(binding(), Opener(result(workbook_name="另一个.xlsx"))).call("describe")


def test_error_is_redacted_and_does_not_retry():
    opener = Opener(error=urllib.error.URLError("secret-token example source cell"))
    with pytest.raises(wps.WpsError) as caught:
        wps.WpsClient(binding(), opener).call("apply", **apply_args())
    assert caught.value.mutation_attempted is True
    assert "secret-token" not in str(caught.value)
    assert len(opener.requests) == 1


def test_redirect_handler_refuses_forwarding():
    with pytest.raises(wps.WpsError) as caught:
        wps._NoRedirect().redirect_request(None, None, 302, "", {}, "https://other.example")
    assert caught.value.code == "REDIRECT_BLOCKED"


def test_response_limit(monkeypatch):
    monkeypatch.setattr(wps, "MAX_RESPONSE_BYTES", 20)
    with pytest.raises(wps.WpsError) as caught:
        wps.WpsClient(binding(), Opener(raw=b"x" * 21)).call("describe")
    assert caught.value.code == "RESPONSE_TOO_LARGE"


def test_unknown_server_error_not_echoed():
    bad = result(ok=False, code="secret-token confidential data")
    with pytest.raises(wps.WpsError) as caught:
        wps.WpsClient(binding(), Opener(bad)).call("describe")
    assert caught.value.code == "SCRIPT_ERROR"
    assert "secret-token" not in str(caught.value)


def test_malformed_row_response_not_accepted():
    bad = result("read", rows=[{"row": True, "cells": []}])
    with pytest.raises(wps.WpsError):
        wps.WpsClient(binding(), Opener(bad)).call("read", sheet="汇总", start_row=1, end_row=1, columns=[1])


def test_mutable_binding_cannot_switch_existing_client_target():
    item = binding()
    opener = Opener(result())
    client = wps.WpsClient(item, opener)
    item.endpoint = ENDPOINT.replace("test_file", "other_file")
    item.workbook_name = "另一个.xlsx"
    assert client.call("describe")["ok"]
    assert opener.requests[0].full_url == ENDPOINT


def test_extra_response_data_is_not_returned_to_tools():
    body = result(private_log="private data")
    body["sheets"][0]["private_log"] = "private data"
    clean = wps.WpsClient(binding(), Opener(body)).call("describe")
    assert "private data" not in repr(clean)


def test_unexpected_transport_exception_redacted():
    opener = Opener(error=RuntimeError("secret-token"))
    with pytest.raises(wps.WpsError) as caught:
        wps.WpsClient(binding(), opener).call("describe")
    assert caught.value.code == "TRANSPORT_ERROR"
    assert "secret-token" not in str(caught.value)


def test_unhashable_outcome_status_safely_rejected():
    body = result("apply", task_id="task-1", batch_id="batch-1", mutation_attempted=True,
                  outcomes=[dict(index=0, status=["applied"], row=2, column=2)])
    with pytest.raises(wps.WpsError) as caught:
        wps.WpsClient(binding(), Opener(body)).call("apply", **apply_args())
    assert caught.value.code == "INVALID_RESPONSE"


NODE_MOCK = r"""
const fs = require('fs');
const input = JSON.parse(fs.readFileSync(0, 'utf8'));
const cells = input.cells || {};
let saves = 0, writes = [], format_writes = [];
function getCell(address) {
  if (!cells[address]) cells[address] = {};
  const entry = cells[address];
  const cell = {
    get Value2() { return entry.value; },
    set Value2(value) {
      writes.push(address);
      if (input.fail_write === address) throw new Error('secret-token native error');
      const shouldParse = input.coerce === address || (input.auto_parse === address && cell.NumberFormat !== '@');
      entry.value = shouldParse && typeof value === 'string' ? Number(value) : value;
      entry.formula = ''; entry.has_formula = false;
    },
    get Formula() { return entry.formula_missing ? undefined : (Object.hasOwn(entry, 'formula') ? entry.formula : ''); },
    get NumberFormat() { return entry.format_missing ? undefined : (entry.number_format || 'General'); },
    set NumberFormat(value) {
      format_writes.push({address, value});
      if ((input.fail_text_format === address && value === '@') ||
          (input.fail_restore_format === address && value !== '@')) throw new Error('native private format error');
      if (input.ignore_text_format === address && value === '@') return;
      entry.number_format = value;
    },
    get HasFormula() { return entry.has_formula_missing ? undefined : (entry.has_formula || false); },
    get MergeCells() { return entry.merged || false; },
    get Count() { return Object.hasOwn(entry, 'count') ? entry.count : 1; },
    ClearContents() { writes.push(address); entry.value = ''; entry.formula = ''; entry.has_formula = false; },
    _entry: entry
  };
  return cell;
}
const sheet = {Name:'汇总', Range:getCell, UsedRange:{Row:1, Column:1, Rows:{Count:input.used_rows || 5}, Columns:{Count:input.used_columns || 3}}};
const Application = {
  ActiveWorkbook: {Name: input.workbook_name || '测试.xlsx', Save() { saves++; if(input.fail_save) throw new Error('native private diagnostic'); }},
  Worksheets: {Count:1, Item(name) { if(name !== '汇总' && name !== 1) throw new Error('no sheet'); return sheet; }},
  WorksheetFunction: {CountBlank(cell) {
    if(Object.hasOwn(cell._entry, 'count_blank')) return cell._entry.count_blank;
    return cell.Value2 === undefined || cell.Value2 === null || cell.Value2 === '' ? 1 : 0;
  }}
};
const code = fs.readFileSync(input.script, 'utf8');
const output = new Function('Application','Context',code)(Application,{argv:input.args});
process.stdout.write(JSON.stringify({result:output,cells,saves,writes,format_writes}));
"""


def run_script(action, payload=None, cells=None, **options):
    node = shutil.which("node")
    if not node:
        pytest.skip("Node is required to execute the real AirScript mock")
    args = dict(action=action, protocol=wps.PROTOCOL, expected_workbook_name="测试.xlsx")
    args.update(payload or {})
    command = subprocess.run([node, "-e", NODE_MOCK], input=json.dumps(dict(script=str(SCRIPT), args=args, cells=cells or {}, **options)),
                             text=True, encoding="utf-8", capture_output=True, check=True, timeout=20)
    return json.loads(command.stdout)


def table():
    return {"A1": {"value": "单号"}, "B1": {"value": "结果"}, "C1": {"value": "状态"},
            "A2": {"value": "001"}, "B2": {"value": "原值"}, "C2": {"value": "符合"},
            "A3": {"value": "002"}, "B3": {"value": "别动"}, "C3": {"value": "不符合"}}


def change(**overrides):
    value = dict(row=2, key_values=["001"], old_value="原值", old_formula="", old_number_format="General", new_value="新值",
                 guards=[dict(column=1, value="001", formula=""), dict(column=3, value="符合", formula="")])
    value.update(overrides)
    return value


def apply_args(**overrides):
    value = dict(task_id="task-1", batch_id="batch-1", sheet="汇总", header_row=1,
                 expected_headers=[dict(column=1, value="单号"), dict(column=2, value="结果"), dict(column=3, value="状态")],
                 key_columns=[1], row_start=2, row_end=5, target_column=2, changes=[change()])
    value.update(overrides)
    return value


def test_real_script_describe_and_read_blank_zero_false():
    described = run_script("describe")
    assert described["result"]["sheets"] == result()["sheets"]
    read = run_script("read", dict(sheet="汇总", start_row=2, end_row=2, columns=[1, 2, 3]),
                      {"B2": {"value": 0, "formula": 0}, "C2": {"value": False, "formula": False}})
    assert [c["value"] for c in read["result"]["rows"][0]["cells"]] == ["", 0, False]
    assert read["saves"] == 0 and read["writes"] == []


@pytest.mark.parametrize("cell", [{"count_blank": 0}, {"has_formula": True}, {"count": 2}, {"formula": "=A1"}])
def test_undefined_not_assumed_blank(cell):
    output = run_script("read", dict(sheet="汇总", start_row=2, end_row=2, columns=[1]), {"A2": cell})
    assert output["result"]["ok"] is False
    assert output["result"]["code"] == "UNSUPPORTED_CELL_METADATA"


def test_merged_title_children_are_readable_but_always_protected():
    payload = dict(sheet="汇总", start_row=1, end_row=1, columns=[1, 2])
    cells = {"A1": {"value": "汇总标题", "merged": True},
             "B1": {"merged": True, "formula_missing": True, "format_missing": True, "has_formula_missing": True}}
    output = run_script("read", payload, cells)
    assert output["result"]["ok"] is True
    read_cells = output["result"]["rows"][0]["cells"]
    assert read_cells[0]["value"] == "汇总标题" and read_cells[0]["merged"] is True
    assert read_cells[1] == dict(column=2, value="", formula="", number_format="", has_formula=True, merged=True)
    assert wps.WpsClient(binding(), Opener(output["result"])).call("read", **payload)["ok"]
    assert not output["writes"]


@pytest.mark.parametrize("address,status", [("B2", "merged"), ("C2", "conflict"), ("A2", "not_found")])
def test_merged_missing_metadata_not_tolerated_for_apply(address, status):
    cells = table()
    cells[address] = {"merged": True, "formula_missing": True, "format_missing": True, "has_formula_missing": True}
    output = run_script("apply", apply_args(), cells)
    assert output["result"]["outcomes"][0]["status"] == status
    assert not output["writes"] and output["saves"] == 0


def test_real_script_writes_only_target_and_client_accepts_reply():
    cells = table()
    output = run_script("apply", apply_args(), cells)
    assert output["result"]["ok"] is True
    assert output["writes"] == ["B2"] and output["saves"] == 1
    assert output["cells"]["B2"]["value"] == "新值"
    assert output["cells"]["B3"] == cells["B3"]
    assert wps.WpsClient(binding(), Opener(output["result"])).call("apply", **apply_args())["outcomes"][0]["status"] == "applied"


def test_real_script_relocates_by_business_key():
    cells = table()
    for col in "ABC":
        cells[col + "2"], cells[col + "3"] = cells[col + "3"], cells[col + "2"]
    output = run_script("apply", apply_args(), cells)
    assert output["writes"] == ["B3"]
    assert output["result"]["outcomes"][0]["row"] == 3


@pytest.mark.parametrize("update,status", [({"C2": {"value": "已变"}}, "conflict"), ({"B2": {"value": "他人更新"}}, "conflict"),
                                         ({"B2": {"value": "原值", "number_format": "@"}}, "conflict"),
                                         ({"B2": {"value": "原值", "formula": "=A1", "has_formula": True}}, "formula"),
                                         ({"B2": {"value": "原值", "merged": True}}, "merged"),
                                         ({"A2": {"value": "missing"}}, "not_found"), ({"A3": {"value": "001"}}, "duplicate")])
def test_real_script_skips_conflicts(update, status):
    cells = table()
    cells.update(update)
    output = run_script("apply", apply_args(), cells)
    assert output["result"]["ok"] is True
    assert output["result"]["outcomes"][0]["status"] == status
    assert output["writes"] == [] and output["saves"] == 0


def test_real_script_duplicate_requests_do_not_write_twice():
    output = run_script("apply", apply_args(changes=[change(), change(new_value="另一个")]), table())
    assert [o["status"] for o in output["result"]["outcomes"]] == ["duplicate", "duplicate"]
    assert not output["writes"]


@pytest.mark.parametrize("new_value", [None, "", 0, False])
def test_clear_and_falsey_values_remain_distinct(new_value):
    args = apply_args(changes=[change(new_value=new_value)])
    output = run_script("apply", args, table())
    assert output["result"]["ok"] is True
    expected = "" if new_value is None else new_value
    assert type(output["cells"]["B2"]["value"]) is type(expected)
    assert output["cells"]["B2"]["value"] == expected
    assert wps.WpsClient(binding(), Opener(output["result"])).call("apply", **args)["ok"]


def test_numeric_and_text_business_keys_differ():
    cells = table()
    cells["A2"]["value"] = 1
    cells["A3"]["value"] = "1"
    args = apply_args(changes=[change(key_values=[1], guards=[])])
    output = run_script("apply", args, cells)
    assert output["writes"] == ["B2"]


def test_unchanged_does_not_save():
    output = run_script("apply", apply_args(changes=[change(new_value="原值")]), table())
    assert output["result"]["outcomes"][0]["status"] == "unchanged"
    assert not output["writes"] and output["saves"] == 0


def test_all_arguments_validated_before_mutation():
    args = apply_args(changes=[change(), change(key_values=["002"], new_value="=1+1")])
    output = run_script("apply", args, table())
    assert output["result"]["code"] == "INVALID_NEW_VALUE"
    assert not output["writes"]
    opener = Opener()
    with pytest.raises(wps.WpsError):
        wps.WpsClient(binding(), opener).call("apply", **args)
    assert not opener.requests


def test_header_change_rejects_whole_batch():
    cells = table()
    cells["B1"]["value"] = "插入的新列"
    output = run_script("apply", apply_args(), cells)
    assert output["result"]["code"] == "HEADER_MISMATCH"
    assert output["result"]["mutation_attempted"] is False
    assert not output["writes"]


def test_save_failure_retains_safe_partial_outcomes():
    output = run_script("apply", apply_args(), table(), fail_save=True)
    assert output["result"]["ok"] is False
    assert output["result"]["mutation_attempted"] is True
    assert output["cells"]["B2"]["value"] == "新值"
    output["result"]["outcomes"][0]["private_value"] = "sensitive"
    with pytest.raises(wps.WpsError) as caught:
        wps.WpsClient(binding(), Opener(output["result"])).call("apply", **apply_args())
    assert caught.value.mutation_attempted is True
    assert caught.value.result["outcomes"] == [dict(index=0, status="applied", row=2, column=2)]
    assert "sensitive" not in repr(caught.value.result)
    assert "native private" not in str(caught.value)


def test_write_exception_and_coercion_remain_uncertain():
    failed = run_script("apply", apply_args(), table(), fail_write="B2")
    assert failed["result"]["mutation_attempted"] is True
    assert failed["result"]["ok"] is False
    coerced = run_script("apply", apply_args(changes=[change(new_value="001")]), table(), coerce="B2")
    assert coerced["result"]["code"] == "WRITE_VERIFY_FAILED"
    assert coerced["result"]["mutation_attempted"] is True


@pytest.mark.parametrize("text_value", ["00123", "12345678901234567890", "1E3", "2026-01-02"])
def test_strings_enter_as_text_and_restore_original_format(text_value):
    output = run_script("apply", apply_args(changes=[change(new_value=text_value)]), table(), auto_parse="B2")
    assert output["result"]["ok"] is True
    assert output["cells"]["B2"]["value"] == text_value
    assert type(output["cells"]["B2"]["value"]) is str
    assert output["cells"]["B2"]["number_format"] == "General"
    assert output["format_writes"] == [dict(address="B2", value="@"), dict(address="B2", value="General")]
    assert output["writes"] == ["B2"] and output["saves"] == 1


@pytest.mark.parametrize("value", [0, False, 123.45, None, ""])
def test_nontext_or_clear_does_not_change_format(value):
    output = run_script("apply", apply_args(changes=[change(new_value=value)]), table())
    assert output["result"]["ok"] is True
    assert output["format_writes"] == []


def test_text_write_failure_restores_custom_format():
    cells = table()
    cells["B2"]["number_format"] = "0.00"
    args = apply_args(changes=[change(new_value="00123", old_number_format="0.00")])
    output = run_script("apply", args, cells, fail_write="B2")
    assert output["result"]["ok"] is False and output["result"]["mutation_attempted"] is True
    assert output["cells"]["B2"]["value"] == "原值"
    assert output["cells"]["B2"]["number_format"] == "0.00"
    assert output["format_writes"][-1] == dict(address="B2", value="0.00")
    assert output["saves"] == 0


@pytest.mark.parametrize("failure", ["fail_text_format", "ignore_text_format"])
def test_text_format_must_take_effect_before_value_is_written(failure):
    output = run_script("apply", apply_args(changes=[change(new_value="00123")]), table(), **{failure: "B2"})
    assert output["result"]["ok"] is False
    assert output["result"]["mutation_attempted"] is True
    assert output["cells"]["B2"]["value"] == "原值"
    assert output["cells"]["B2"]["number_format"] == "General"
    assert not output["writes"] and output["saves"] == 0


def test_format_restore_failure_is_not_reported_as_success():
    output = run_script("apply", apply_args(changes=[change(new_value="00123")]), table(), fail_restore_format="B2")
    assert output["result"]["ok"] is False and output["result"]["mutation_attempted"] is True
    assert output["cells"]["B2"]["value"] == "00123"
    assert output["cells"]["B2"]["number_format"] == "@"
    assert output["saves"] == 0


def test_existing_text_format_needs_no_temporary_change():
    cells = table()
    cells["B2"]["number_format"] = "@"
    output = run_script("apply", apply_args(changes=[change(new_value="00123", old_number_format="@")]), cells, auto_parse="B2")
    assert output["result"]["ok"] is True and output["cells"]["B2"]["value"] == "00123"
    assert output["format_writes"] == []


@pytest.mark.parametrize("changes", [[change(key_values=[""])], [change(key_values=[None])], [change(row=1)],
                                    [change(guards=[dict(column=4, value="", formula="")])]])
def test_invalid_scope_or_key_rejected_by_both_sides(changes):
    args = apply_args(changes=changes)
    output = run_script("apply", args, table())
    assert output["result"]["code"] == "INVALID_ARGUMENTS"
    assert not output["writes"]
    opener = Opener()
    with pytest.raises(wps.WpsError):
        wps.WpsClient(binding(), opener).call("apply", **args)
    assert not opener.requests


def test_wrong_document_name_and_extra_runtime_args():
    output = run_script("apply", apply_args(), table(), workbook_name="另一个.xlsx")
    assert output["result"]["code"] == "WORKBOOK_MISMATCH" and not output["writes"]
    payload = dict(sheet="汇总", start_row=1, end_row=1, columns=[1], injected_runtime_field="inert")
    assert run_script("read", payload, table())["result"]["ok"] is True
