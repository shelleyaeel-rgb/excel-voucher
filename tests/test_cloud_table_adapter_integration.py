"""Whole tasks through the production adapter and verified XLSX metadata.

Only the remote transport is synthetic. Sparse API records, XLSX bytes,
hashes, merge ranges and post-write exports follow separate real code paths.
"""
from copy import deepcopy
from datetime import datetime
import hashlib
import io

from openpyxl import Workbook
from openpyxl.utils.datetime import CALENDAR_MAC_1904, CALENDAR_WINDOWS_1900, to_excel
import pytest

from xiaoyu import wps_adapter, wps_snapshot
from xiaoyu.cloud_tools import CloudDocumentTools
from mapping_test_support import confirm_columns
from test_wps_adapter import Rpc, account_binding, envelope, record


class SparseTableExportRpc(Rpc):
    def __init__(self, epoch):
        super().__init__()
        self.epoch = epoch
        self.version = 3
        self.requests = []
        self.after_write = None
        self.formats = {(row, column): ("yyyy-mm-dd" if column == 3 else "0.00" if column == 5 else "General")
                        for row in (2, 3) for column in (3, 4, 5)}
        self.cells = {(1, 1): record(1, 1, "序号"), (1, 2): dict(record(1, 2, "日期"), isMerge=True),
                      (1, 4): record(1, 4, "对方账户"), (1, 5): record(1, 5, "收入")}
        self.merged = [dict(row_from=0, row_to=0, col_from=1, col_to=2)]
        for row in (2, 3):
            self.cells[(row, 1)] = record(row, 1, row - 1)
            self.cells[(row, 2)] = record(row, 2, "", fmt='General"月"',
                formula=f'=IF(C{row}="","",MONTH(C{row}))')
        self.export()

    def export(self):
        book = Workbook()
        book.epoch = self.epoch
        sheet = book.active
        sheet.title = "汇总"
        for coordinate, fmt in self.formats.items():
            sheet.cell(*coordinate).number_format = fmt
        for (row, column), item in self.cells.items():
            value = item.get("fmlaText", item["understandableType"]["value"])
            target = sheet.cell(row, column, value)
            target.number_format = "General" if item["numFormat"] == "G/通用格式" else item["numFormat"]
        sheet.merge_cells("B1:C1")
        stream = io.BytesIO()
        book.save(stream)
        book.close()
        self.raw = stream.getvalue()
        self.hash = dict(type="sha256", sum=hashlib.sha256(self.raw).hexdigest())

    def call_tool(self, name, arguments, mutation=False):
        if name == "download_file":
            self.calls.append((name, deepcopy(arguments), mutation))
            assert not mutation
            return envelope(dict(physical_size=len(self.raw), hashes=[self.hash],
                url="https://offline.ag.kdocs.cn/whole-task?signature=offline-only"))
        output = super().call_tool(name, arguments, mutation)
        if name == "get_file_info":
            output["data"]["data"].update(hash=self.hash, version=self.version)
        elif name == "sheet.get_sheets_info":
            output["data"]["detail"]["sheetsInfo"][0]["colTo"] = 4
        elif name == "sheet.range_data_batch_update":
            # The live API returns only its ordinary cell record, never the
            # supplemental date_system that the XLSX reader knows.
            for row in (2, 3):
                if (row, 3) in self.cells:
                    self.cells[(row, 2)]["understandableType"].update(type="double", value=11)
                    self.cells[(row, 2)]["originalCellValue"] = "11"
            if self.after_write:
                self.after_write(self)
            self.version += 1
            self.export()
        return output

    def open(self, request, timeout):
        self.requests.append(request)
        assert request.full_url == "https://offline.ag.kdocs.cn/whole-task?signature=offline-only"
        assert request.header_items() == [("Accept", "application/octet-stream")]
        return io.BytesIO(self.raw)


def make_task(rpc, monkeypatch, tmp_path):
    def load(*args, **kwargs):
        return wps_snapshot.load_blank_cells(*args, **kwargs, opener=rpc)
    monkeypatch.setattr(wps_adapter, "load_blank_cells", load)
    monkeypatch.setattr(wps_adapter.time, "sleep", lambda _: None)
    binding = account_binding()
    cloud = CloudDocumentTools([binding], allow_write=True, journal_dir=tmp_path / "tasks",
        client_factory=lambda b: wps_adapter.SkillHubWpsClient(b, rpc=rpc))
    read = cloud.execute("wps_read_table", dict(document_url=binding.share_url, sheet="汇总",
        columns=["序号", "日期", "对方账户", "收入"], header_row=None, start_row=None, end_row=None,
        column_overrides=[dict(name="日期", column=3)]))
    assert read["ok"], read
    expected_epoch = "1904" if rpc.epoch == CALENDAR_MAC_1904 else "1900"
    date_profile = next(p for p in read["column_profiles"] if p["name"] == "日期")
    assert date_profile["date_systems"] == [expected_epoch]
    dates = [to_excel(datetime(2026, 11, day), rpc.epoch) for day in (6, 7)]
    confirm_columns(cloud, read, [{"target_column": "日期", "source_field": "交易日期"},
        {"target_column": "对方账户", "source_field": "对方户名"}, {"target_column": "收入", "source_field": "正金额"}])
    prepared = cloud.execute("wps_prepare_table_update", dict(snapshot_id=read["snapshot_id"], key_columns=["序号"],
        columns=[dict(target_column="日期", source_field="交易日期", allow_blank=False),
                 dict(target_column="对方账户", source_field="对方户名", allow_blank=False),
                 dict(target_column="收入", source_field="正金额", allow_blank=False)],
        updates=[dict(key_values=[i + 1], values=[dates[i], f"测试户名{i + 1}", (i + 1) * 100], reason="已确认按序填空模板")
                 for i in range(2)], summary="一次填入日期、户名和收入，保留月份公式"))
    assert prepared["ok"], prepared
    return cloud, prepared, dates


@pytest.mark.parametrize("epoch", [CALENDAR_WINDOWS_1900, CALENDAR_MAC_1904], ids=["1900", "1904"])
def test_sparse_blank_to_populated_columns_complete_through_real_adapter_and_snapshot(monkeypatch, tmp_path, epoch):
    rpc = SparseTableExportRpc(epoch)
    cloud, plan, dates = make_task(rpc, monkeypatch, tmp_path)
    baseline = cloud.plans[plan["plan_id"]]["baseline"]
    # All three empty inputs get XLSX supplemental metadata before writing.
    assert all(next(c for c in row["cells"] if c["column"] == 4).get("date_system") in ("1900", "1904")
               for row in baseline["rows"])
    result = cloud.execute("wps_apply_table_update", dict(plan_id=plan["plan_id"]))
    assert result["ok"] and result["status"] == "completed", result
    assert result["updated"] == 6 and result["final_check"]["matched"] == 6
    assert len(rpc.writes) == 3 and len(cloud.results) == 1
    for row, date in zip((2, 3), dates):
        assert rpc.cells[(row, 3)]["understandableType"]["value"] == date
        assert "date_system" not in rpc.cells[(row, 3)]
        assert rpc.cells[(row, 2)]["fmlaText"] == f'=IF(C{row}="","",MONTH(C{row}))'
    # No cells are missing now. Date metadata must still be verified against
    # the current export rather than disappearing with the last empty input.
    before_downloads = len(rpc.requests)
    readback = wps_adapter.SkillHubWpsClient(account_binding(), rpc).call(
        "read", sheet="汇总", start_row=2, end_row=3, columns=[1, 2, 3, 4, 5])
    assert len(rpc.requests) > before_downloads
    expected_epoch = "1904" if epoch == CALENDAR_MAC_1904 else "1900"
    for row in readback["rows"]:
        cells = {c["column"]: c for c in row["cells"]}
        assert cells[3]["date_system"] == expected_epoch
        assert all(cells[c]["value"] not in (None, "") for c in (1, 2, 3, 4, 5))
    reopened = CloudDocumentTools(list(cloud.bindings.values()), allow_write=True,
        journal_dir=cloud.journal_dir, client_factory=lambda b: wps_adapter.SkillHubWpsClient(b, rpc))
    checked = reopened.execute("wps_check_task", dict(task_id=plan["plan_id"]))
    assert checked["ok"] and checked["current_check"]["matched"] == 6 and len(rpc.writes) == 3


def test_real_epoch_change_after_first_column_stops_remaining_columns(monkeypatch, tmp_path):
    rpc = SparseTableExportRpc(CALENDAR_WINDOWS_1900)
    cloud, plan, _ = make_task(rpc, monkeypatch, tmp_path)
    rpc.after_write = lambda transport: setattr(transport, "epoch", CALENDAR_MAC_1904)
    result = cloud.execute("wps_apply_table_update", dict(plan_id=plan["plan_id"]))
    assert not result["ok"] and result["status"] == "partial", result
    assert len(rpc.writes) == 1 and result["retryable"] is False
    assert not any((row, column) in rpc.cells for row in (2, 3) for column in (4, 5))
