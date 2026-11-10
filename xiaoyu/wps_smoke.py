"""Portable-package WPS check using only synthetic data, with no network."""
from __future__ import annotations

from copy import deepcopy
from pathlib import Path
import tempfile
import uuid


def run_wps_smoke():
    from .cloud_tools import CloudDocumentTools
    from .config import resource_path
    from .wps_service import WpsBinding, load_bindings, save_bindings
    from .wps_skillhub import WpsAccount, save_account, require_account
    from .wps_adapter import SkillHubWpsClient, resolve_skillhub_document

    script = resource_path("assets/wps_airscript.js").read_text(encoding="utf-8")
    assert "xiaoyu-wps/1" in script and "return runXiaoyuWps();" in script
    binding = WpsBinding("smoke", "合成云表", "https://www.kdocs.cn/l/synthetic",
                         "https://www.kdocs.cn/api/v3/ide/file/synthetic/script/smoke/sync_task",
                         "offline-wps-token-never-real", "合成云表.xlsx")
    cells = {1: ["业务单号", "到账金额", "状态"], 2: ["0001", 0, "已到账"],
             3: ["0002", 18.5, "待到账"]}
    calls = []

    class OfflineWps:
        def __init__(self, document):
            assert document.share_url == binding.share_url

        def call(self, action, **payload):
            calls.append(action)
            base = {"ok": True, "protocol": "xiaoyu-wps/1", "action": action,
                    "workbook_name": binding.workbook_name, "mutation_attempted": action == "apply"}
            if action == "describe":
                return dict(base, sheets=[{"name": "收款", "used_start_row": 1, "used_end_row": 3,
                                          "used_start_column": 1, "used_end_column": 3}])
            if action == "read":
                rows = []
                for row in range(payload["start_row"], payload["end_row"] + 1):
                    entries = []
                    for column in payload["columns"]:
                        value = cells[row][column - 1]
                        entries.append({"column": column, "value": value, "formula": value,
                                        "number_format": "General", "has_formula": False, "merged": False})
                    rows.append({"row": row, "cells": entries})
                return dict(base, rows=rows)
            assert action == "apply" and payload["target_column"] == 2
            outcomes = []
            for index, change in enumerate(payload["changes"]):
                matched = [r for r in (2, 3) if cells[r][0] == change["key_values"][0]]
                assert len(matched) == 1
                row = matched[0]
                assert cells[row][1] == change["old_value"]
                cells[row][1] = change["new_value"]
                outcomes.append({"index": index, "row": row, "column": 2, "status": "applied"})
            return dict(base, task_id=payload["task_id"], batch_id=payload["batch_id"], outcomes=outcomes)

    original = deepcopy(cells)
    with tempfile.TemporaryDirectory(prefix="xiaoyu-wps-smoke-") as directory:
        root = Path(directory)
        config = root / "wps.json"
        save_bindings([binding], config)
        assert binding.token not in config.read_text(encoding="utf-8")
        assert load_bindings(config)[0].token == binding.token
        tools = CloudDocumentTools([binding], journal_dir=root / "tasks", client_factory=OfflineWps)
        tools.begin_turn("按业务单号更新到账金额，未匹配的不动，来源为空不覆盖")
        snapshot = tools.execute("wps_read_table", {"document_url": binding.share_url, "sheet": "收款",
            "columns": ["业务单号", "到账金额", "状态"], "header_row": None, "start_row": None, "end_row": None})
        assert snapshot["ok"] and snapshot["header_row"] == 1
        plan = tools.execute("wps_prepare_update", {"snapshot_id": snapshot["snapshot_id"],
            "key_columns": ["业务单号"], "target_column": "到账金额", "allow_blank": False,
            "summary": "仅处理来源已到账记录", "updates": [{"key_values": ["0001"], "new_value": 12.34, "reason": "合成来源已到账"}]})
        assert plan["ok"] and cells == original
        result = tools.execute("wps_apply_update", {"plan_id": plan["plan_id"]})
        assert result["status"] == "completed" and result["updated"] == 1, result
        assert cells[2][1] == 12.34 and cells[3] == original[3]
        again = tools.execute("wps_apply_update", {"plan_id": plan["plan_id"]})
        assert again["status"] == "completed" and calls.count("apply") == 1
        journal = (root / "tasks" / (plan["plan_id"] + ".json")).read_text(encoding="utf-8")
        assert binding.token not in journal and binding.endpoint not in journal
        assert "verified" in journal
        account = WpsAccount(str(uuid.uuid4()), "offline-account-token-never-real")
        account_path = root / "wps_account.json"
        save_account(account, account_path)
        assert require_account(account.account_id, account_path) == account
        assert account.token not in account_path.read_text(encoding="utf-8")

        class OfflineSkillHub:
            def call_tool(self, name, arguments, mutation=False):
                assert not mutation
                if name == "get_file_info":
                    return {"code": 0, "data": {"code": 0, "data": {
                        "id": "synthetic-file", "name": "合成云表.xlsx", "type": "file",
                        "permission": {"update": True}}}}
                assert name == "sheet.get_sheets_info"
                return {"code": 0, "data": {"detail": {"sheetsInfo": [{
                    "sheetName": "收款", "sheetId": 1, "sheetType": "et", "isProtected": False,
                    "rowFrom": 0, "rowTo": 2, "colFrom": 0, "colTo": 2}]}}}

        rpc = OfflineSkillHub()
        connected = resolve_skillhub_document("https://www.kdocs.cn/l/synthetic-account", account.account_id, rpc)
        assert connected.provider == "skillhub" and not connected.token and not connected.endpoint
        save_bindings([binding, connected], root / "providers.json")
        assert len(load_bindings(root / "providers.json")) == 2
        description = SkillHubWpsClient(connected, rpc=rpc).call("describe")
        assert description["can_edit"] and description["file_id"] == "synthetic-file"
        assert description["sheets"][0]["name"] == "收款"
    return {"mode": "offline_mock", "bundled_script": "passed", "encrypted_binding": "passed",
            "automatic_header_and_key_matching": "passed", "keyed_update_readback": "passed",
            "journal_and_no_repeat": "passed", "encrypted_account": "passed", "account_provider": "passed"}
