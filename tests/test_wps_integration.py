"""Cloud tools -> HTTP client -> real AirScript -> durable journal, offline.

The fake opener runs the shipped JavaScript on each HTTP request and retains its
synthetic workbook state between requests, just as separate WPS requests would.
It never opens a socket or uses a user document/credential.
"""
import io
import json

from xiaoyu.cloud_tools import CloudDocumentTools
from mapping_test_support import confirm_columns
from xiaoyu.wps_service import WpsClient

from test_wps_service import binding, run_script, SHARE


class ScriptOpener:
    def __init__(self, cells, journal_dir, *, used_rows=5):
        self.cells = cells
        self.journal_dir = journal_dir
        self.used_rows = used_rows
        self.calls = []
        self.writes = []
        self.saves = 0
        self.drop_apply_response = False
        self.drop_reconcile_read = False
        self._applied_without_response = False

    def open(self, request, timeout):
        argv = json.loads(request.data)["Context"]["argv"]
        action = argv["action"]
        self.calls.append(argv)
        if action == "read" and self._applied_without_response and self.drop_reconcile_read:
            self.drop_reconcile_read = False
            raise TimeoutError("offline test: reconciliation unavailable")
        if action == "apply":
            # The journal must already exist before any cloud mutation can run.
            pending = json.loads((self.journal_dir / (argv["task_id"] + ".json")).read_text(encoding="utf-8"))
            assert pending["status"] == "running"
            batch = next(b for b in pending["batches"] if b["batch_id"] == argv["batch_id"])
            assert batch["status"] == "submitted"
            assert all(pending["items"][i]["status"] == "pending" for i in batch["item_indexes"])
        output = run_script(action, argv, self.cells, used_rows=self.used_rows)
        self.cells = output["cells"]
        self.writes.extend(output["writes"])
        self.saves += output["saves"]
        if action == "apply" and self.drop_apply_response:
            self.drop_apply_response = False
            self._applied_without_response = True
            raise TimeoutError("offline test: write reply lost")
        return io.BytesIO(json.dumps({"status": "finished", "data": {"result": output["result"]}}).encode("utf-8"))

    def factory(self, item):
        return WpsClient(item, self)


def titled_table():
    return {"A1": {"value": "本月业务汇总", "merged": True},
            "B1": {"merged": True, "formula_missing": True, "format_missing": True, "has_formula_missing": True},
            "C1": {"merged": True},
            "A2": {"value": "单号"}, "B2": {"value": "结果"}, "C2": {"value": "状态"},
            "A3": {"value": "001"}, "B3": {"value": "原值"}, "C3": {"value": "符合"},
            "A4": {"value": "002"}, "B4": {"value": "别动"}, "C4": {"value": "不符合"},
            "A5": {"value": "003"}, "B5": {"value": 10}, "C5": {"value": "符合"}}


def make_tools(tmp_path, cells=None, *, used_rows=5, allow_write=True):
    opener = ScriptOpener(cells if cells is not None else titled_table(), tmp_path, used_rows=used_rows)
    tools = CloudDocumentTools([binding()], allow_write=allow_write, journal_dir=tmp_path, client_factory=opener.factory)
    return tools, opener


def read_table(tools):
    read = tools.execute("wps_read_table", dict(document_url=SHARE, sheet="汇总", columns=["单号", "结果", "状态"],
                                                header_row=None, start_row=None, end_row=None))
    assert read["ok"] is True, read
    return read


def prepare(tools, snapshot_id, updates=None, *, allow_blank=False):
    confirmed = confirm_columns(tools, {"snapshot_id": snapshot_id}, [{"target_column": "结果", "source_field": "来源结果"}])
    snapshot_id = confirmed["snapshot_id"]
    prepared = tools.execute("wps_prepare_update", dict(snapshot_id=snapshot_id, key_columns=["单号"], target_column="结果",
                                                        updates=updates if updates is not None else [dict(key_values=["001"], new_value="新值", reason="符合本次条件")],
                                                        allow_blank=allow_blank, summary="按单号匹配，仅更新符合条件的结果"))
    assert prepared["ok"] is True, prepared
    return prepared


def test_no_row_numbers_merged_title_relocation_and_journal(tmp_path):
    tools, opener = make_tools(tmp_path)
    inspected = tools.execute("wps_inspect", dict(document_url=SHARE, sheet="汇总"))
    assert inspected["ok"] is True
    assert inspected["preview"][0]["cells"][0]["value"] == "本月业务汇总"
    read = read_table(tools)
    assert read["header_row"] == 2 and read["start_row"] == 3 and read["row_count"] == 3
    prepared = prepare(tools, read["snapshot_id"])
    assert prepared["can_execute"] and prepared["planned"] == 1
    assert not opener.writes
    # A collaborator moved the record after preview; use its key, not old row 3.
    for column in "ABC":
        opener.cells[column + "3"], opener.cells[column + "4"] = opener.cells[column + "4"], opener.cells[column + "3"]
    applied = tools.execute("wps_apply_update", {"plan_id": prepared["plan_id"]})
    assert applied["ok"] is True and applied["status"] == "completed", applied
    assert applied["updated"] == 1 and applied["unknown"] == 0
    assert opener.writes == ["B4"] and opener.saves == 1
    assert opener.cells["B3"]["value"] == "别动"
    assert opener.cells["B5"]["value"] == 10
    assert opener.cells["A1"]["value"] == "本月业务汇总"
    actions = [call["action"] for call in opener.calls]
    after_apply = actions[actions.index("apply") + 1:]
    assert after_apply and all(action == "read" for action in after_apply)
    path = tmp_path / (prepared["plan_id"] + ".json")
    text = path.read_text(encoding="utf-8")
    journal = json.loads(text)
    assert journal["status"] == "completed" and journal["items"][0]["status"] == "verified"
    assert journal["items"][0]["verified_row"] == 4 and journal["batches"][0]["status"] == "checked"
    assert binding().token not in text and binding().endpoint not in text
    repeated = tools.execute("wps_apply_update", {"plan_id": prepared["plan_id"]})
    assert repeated["updated"] == 1 and opener.writes == ["B4"]
    assert [call["action"] for call in opener.calls].count("apply") == 1


def test_changed_filter_guard_and_merged_target_never_written(tmp_path):
    tools, opener = make_tools(tmp_path)
    read = read_table(tools)
    prepared = prepare(tools, read["snapshot_id"], [dict(key_values=["001"], new_value="新值", reason="状态符合"),
                                                 dict(key_values=["003"], new_value=0, reason="状态符合")])
    opener.cells["C3"]["value"] = "已取消"
    opener.cells["B5"] = {"merged": True, "formula_missing": True, "has_formula_missing": True, "format_missing": True}
    applied = tools.execute("wps_apply_update", {"plan_id": prepared["plan_id"]})
    assert applied["status"] == "partial" and applied["updated"] == 0, applied
    assert applied["conflicts"] == 1 and applied["skipped"] == 1
    assert not opener.writes and opener.saves == 0
    journal = json.loads((tmp_path / (prepared["plan_id"] + ".json")).read_text(encoding="utf-8"))
    assert [item["status"] for item in journal["items"]] == ["conflict", "merged"]


def test_lost_write_reply_and_failed_read_recover_after_restart_without_retry(tmp_path):
    tools, opener = make_tools(tmp_path)
    read = read_table(tools)
    prepared = prepare(tools, read["snapshot_id"])
    opener.drop_apply_response = True
    opener.drop_reconcile_read = True
    applied = tools.execute("wps_apply_update", {"plan_id": prepared["plan_id"]})
    assert applied["status"] == "uncertain" and applied["unknown"] == 1, applied
    assert opener.cells["B3"]["value"] == "新值" and opener.writes == ["B3"]
    reloaded = CloudDocumentTools([binding()], journal_dir=tmp_path, client_factory=opener.factory)
    checked = reloaded.execute("wps_check_task", {"task_id": prepared["task_id"]})
    assert checked["status"] == "completed" and checked["updated"] == 1 and checked["unknown"] == 0, checked
    assert [call["action"] for call in opener.calls].count("apply") == 1
    assert opener.writes == ["B3"]
    journal = json.loads((tmp_path / (prepared["plan_id"] + ".json")).read_text(encoding="utf-8"))
    assert journal["status"] == "completed" and journal["items"][0]["status"] == "verified"


def test_multiple_batches_save_and_verify_each_batch(tmp_path):
    cells = titled_table()
    updates = []
    for i in range(102):
        row, key = i + 3, str(i).zfill(5)
        cells["A" + str(row)] = {"value": key}
        cells["B" + str(row)] = {"value": 100 + i}
        cells["C" + str(row)] = {"value": "符合"}
        updates.append(dict(key_values=[key], new_value=i, reason="来源表已核对"))
    tools, opener = make_tools(tmp_path, cells, used_rows=104)
    read = read_table(tools)
    prepared = prepare(tools, read["snapshot_id"], updates)
    applied = tools.execute("wps_apply_update", {"plan_id": prepared["plan_id"]})
    assert applied["status"] == "completed" and applied["updated"] == 102, applied
    calls = [c for c in opener.calls if c["action"] == "apply"]
    assert [len(c["changes"]) for c in calls] == [100, 2]
    assert calls[0]["batch_id"] != calls[1]["batch_id"]
    assert opener.saves == 2 and len(opener.writes) == len(set(opener.writes)) == 102
    actions = [c["action"] for c in opener.calls]
    positions = [i for i, action in enumerate(actions) if action == "apply"]
    assert "read" in actions[positions[0] + 1:positions[1]]
    assert "read" in actions[positions[1] + 1:]
    journal = json.loads((tmp_path / (prepared["plan_id"] + ".json")).read_text(encoding="utf-8"))
    assert len(journal["batches"]) == 2 and all(b["status"] == "checked" for b in journal["batches"])


def test_preview_only_pipeline_never_sends_apply(tmp_path):
    tools, opener = make_tools(tmp_path, allow_write=False)
    read = read_table(tools)
    prepared = prepare(tools, read["snapshot_id"])
    assert prepared["can_execute"] is False
    applied = tools.execute("wps_apply_update", {"plan_id": prepared["plan_id"]})
    assert applied["status"] == "preview_only"
    assert "apply" not in [call["action"] for call in opener.calls]
    assert not opener.writes
