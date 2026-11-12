"""Bounded WPS personal-account operations, verified with synthetic XLSX cells.

SkillHub has no compare-and-swap. Preflight and independent readback detect
conflicts, but collaborators must not edit the same cells during an update.
"""
from __future__ import annotations

import json
import math
import re
import threading
import time
import uuid
from collections import Counter
from dataclasses import replace

from openpyxl.styles.numbers import is_date_format

from .wps_service import (PROTOCOL, WpsBinding, WpsClient, WpsError, _integer,
                          _scalar, _validate_request, header_matches, normalize_share_url,
                          target_value_matches, validate_binding)
from .wps_snapshot import load_blank_cells, normalize_number_format

UNKNOWN_FORMAT = "[WPS 未返回空白单元格格式]"
_LOCK = threading.RLock()
_FILE_ID = re.compile(r"[A-Za-z0-9_-]{1,128}\Z")


def create_wps_client(binding: WpsBinding, *, account_path=None):
    validate_binding(binding)
    return WpsClient(binding) if binding.provider == "airscript" else SkillHubWpsClient(binding, account_path=account_path)


def _invalid(*, mutation=False):
    raise WpsError("WPS 返回的表格信息不完整，已停止操作。", code="INVALID_RESPONSE", mutation_attempted=mutation)


def _data(response, *, mutation=False):
    """Unwrap the two envelopes observed on the actual SkillHub service."""
    if not isinstance(response, dict) or type(response.get("code")) is not int or response.get("code") not in (0, 200):
        _invalid(mutation=mutation)
    value = response.get("data")
    if not isinstance(value, dict):
        _invalid(mutation=mutation)
    if "code" in value:
        code = value.get("code")
        if code in (401, 400006, "401", "400006"):
            raise WpsError("WPS 授权已失效，请重新连接。", code="AUTH_REQUIRED", mutation_attempted=mutation)
        if code in (403, 403001, "403", "403001"):
            raise WpsError("当前账号无权执行此操作，请核对文档权限。", code="ACCESS_DENIED", mutation_attempted=mutation)
        if code in (429, "429"):
            raise WpsError("WPS 请求过于频繁，请稍后重新读取。", code="RATE_LIMITED", mutation_attempted=mutation)
        if type(code) is not int or code != 0 or not isinstance(value.get("data"), dict):
            raise WpsError("WPS 未完成操作，请核对文档权限和处理记录。", code="TOOL_ERROR", mutation_attempted=mutation)
        value = value["data"]
    elif "result" in value and value["result"] != "ok":
        raise WpsError("WPS 未完成操作，请核对文档权限和处理记录。", code="TOOL_ERROR", mutation_attempted=mutation)
    return value


def _file_info(rpc, **address):
    info = _data(rpc.call_tool("get_file_info", dict(address, with_permission=True)))
    identity, name, permission = info.get("id"), info.get("name"), info.get("permission")
    if (not isinstance(identity, str) or not _FILE_ID.fullmatch(identity)
            or not isinstance(name, str) or not name or len(name) > 512
            or not isinstance(permission, dict) or type(permission.get("update")) is not bool):
        _invalid()
    if info.get("type") != "file" or not name.lower().endswith((".xlsx", ".xls", ".et", ".ksheet")):
        raise WpsError("这份链接不是可处理的 WPS 表格，请选择 Excel 或智能表格。", code="UNSUPPORTED_DOCUMENT")
    return info


def _sheets(rpc, file_id):
    data = _data(rpc.call_tool("sheet.get_sheets_info", {"file_id": file_id}))
    detail = data.get("detail")
    sheets = detail.get("sheetsInfo") if isinstance(detail, dict) else None
    if not isinstance(sheets, list) or not 1 <= len(sheets) <= 1024:
        _invalid()
    result, ids, names = [], set(), set()
    for item in sheets:
        if not isinstance(item, dict):
            _invalid()
        if item.get("sheetType") != "et":
            continue
        name, identity = item.get("sheetName"), item.get("sheetId")
        if (not isinstance(name, str) or not name or len(name) > 255
                or not _integer(identity, 1, 2**31 - 1) or identity in ids or name in names
                or type(item.get("isProtected")) is not bool
                or not all(_integer(item.get(k), 0, 1048575 if k.startswith("row") else 16383)
                           for k in ("rowFrom", "rowTo", "colFrom", "colTo"))
                or item["rowFrom"] > item["rowTo"] or item["colFrom"] > item["colTo"]):
            _invalid()
        ids.add(identity)
        names.add(name)
        result.append(dict(item))
    if not result:
        raise WpsError("这份文档没有可处理的工作表。", code="UNSUPPORTED_DOCUMENT")
    return result


def resolve_skillhub_document(link: str, account_id: str, client) -> WpsBinding:
    link = normalize_share_url(link)
    info = _file_info(client, url=link)
    _sheets(client, info["id"])
    binding = WpsBinding(uuid.uuid4().hex, info["name"][:200], link, workbook_name=info["name"],
                         provider="skillhub", account_id=account_id, file_id=info["id"])
    validate_binding(binding)
    return binding


def _same(a, b):
    return ((type(a) in (int, float) and type(b) in (int, float)) or type(a) is type(b)) and a == b


def _key(values):
    return json.dumps([["number" if type(v) in (int, float) else type(v).__name__,
                        int(v) if type(v) is float and v.is_integer() else v] for v in values],
                      ensure_ascii=False, allow_nan=False, separators=(",", ":"))


def _bounds(item, keys):
    if not isinstance(item, dict) or not all(_integer(item.get(k), 0, 1048575 if i < 2 else 16383) for i, k in enumerate(keys)):
        _invalid()
    r0, r1, c0, c1 = (item[k] for k in keys)
    if r0 > r1 or c0 > c1:
        _invalid()
    return r0, r1, c0, c1


def _number(value):
    if type(value) in (int, float) and _scalar(value):
        return value
    if isinstance(value, str) and re.fullmatch(r"-?(?:0|[1-9]\d*)(?:\.\d+)?(?:[eE][+-]?\d+)?", value):
        number = float(value)
        if math.isfinite(number) and abs(number) <= 9007199254740991:
            return int(number) if number.is_integer() else number
    _invalid()


def _cell(record):
    typed, fmt, formula = record.get("understandableType"), record.get("numFormat"), record.get("fmlaText", "")
    if (not isinstance(typed, dict) or not isinstance(fmt, str) or len(fmt) > 1024
            or not isinstance(formula, str) or len(formula) > 32767
            or type(record.get("isMerge")) is not bool or record.get("isCellPic") is not False
            or record.get("cellValueType") != "basic"):
        _invalid()
    kind, value = typed.get("type"), typed.get("value")
    if kind == "date":
        value = _number(record.get("originalCellValue"))
    elif kind == "double":
        value = _number(value)
    elif kind == "string":
        if not isinstance(value, str):
            _invalid()
    elif kind == "bool":
        if type(value) is not bool:
            _invalid()
    elif kind == "" and value in (None, ""):
        value = ""
    else:
        _invalid()
    if not _scalar(value):
        _invalid()
    return dict(value=value, formula=formula, has_formula=bool(formula),
                number_format=normalize_number_format(fmt), merged=record["isMerge"])


def _column_groups(columns):
    groups = []
    for column in sorted(set(columns)):
        if groups and column == groups[-1][-1] + 1:
            groups[-1].append(column)
        else:
            groups.append([column])
    return groups


class SkillHubWpsClient:
    def __init__(self, binding: WpsBinding, rpc=None, *, account_path=None):
        validate_binding(binding)
        if binding.provider != "skillhub":
            raise WpsError("此文档未使用 WPS 账号连接。", code="INVALID_BINDING")
        self.binding = replace(binding)
        self.rpc = rpc
        self.account_path = account_path

    def _client(self):
        if self.rpc is not None:
            return self.rpc
        from .wps_skillhub import SkillHubClient, require_account
        return SkillHubClient(require_account(self.binding.account_id, path=self.account_path).token)

    def _metadata(self, rpc):
        info = _file_info(rpc, file_id=self.binding.file_id)
        if info["id"] != self.binding.file_id or (self.binding.workbook_name and info["name"] != self.binding.workbook_name):
            raise WpsError("云文档身份或名称已变化，请重新添加后再处理。", code="WORKBOOK_MISMATCH")
        return info, _sheets(rpc, self.binding.file_id)

    def _read(self, rpc, sheet, start, end, columns, *, complete_blanks=True):
        # WPS's saved XLSX can briefly lag its live cells after an update.
        # Re-read the entire snapshot, never replay a write or retry auth errors.
        for attempt in range(3):
            try:
                return self._read_once(rpc, sheet, start, end, columns, complete_blanks=complete_blanks)
            except WpsError as exc:
                if exc.code != "SNAPSHOT_CHANGED" or attempt == 2:
                    raise
                time.sleep(attempt + 1)

    def _read_once(self, rpc, sheet, start, end, columns, *, complete_blanks=True):
        # The sparse API omits formatted blanks and the workbook date epoch.
        # Capture the version before reading, then verify an XLSX export
        # against live cells whenever either kind of metadata is needed.
        info = _file_info(rpc, file_id=self.binding.file_id) if complete_blanks else None
        if info is not None and (info["id"] != self.binding.file_id or info["name"] != self.binding.workbook_name):
            raise WpsError("云文档身份或名称已变化，请重新添加后再处理。", code="WORKBOOK_MISMATCH")
        cells = {(row, col): dict(column=col, value="", formula="", has_formula=False,
                                 number_format=UNKNOWN_FORMAT, merged=False)
                 for row in range(start, end + 1) for col in columns}
        for group in _column_groups(columns):
            c0, c1 = group[0] - 1, group[-1] - 1
            for r0 in range(start - 1, end, 500):
                r1 = min(r0 + 499, end - 1)
                address = dict(file_id=self.binding.file_id, worksheet_id=sheet["sheetId"])
                data = _data(rpc.call_tool("sheet.get_range_data", dict(address,
                    range=dict(rowFrom=r0, rowTo=r1, colFrom=c0, colTo=c1))))
                detail = data.get("detail")
                if not isinstance(detail, dict) or "rangeData" not in detail:
                    _invalid()
                records = detail["rangeData"]
                if records is None:
                    records = []
                if not isinstance(records, list) or len(records) > (r1 - r0 + 1) * len(group):
                    _invalid()
                covered = set()
                for record in records:
                    a, b, c, d = _bounds(record, ("rowFrom", "rowTo", "colFrom", "colTo"))
                    if b < r0 or a > r1 or d < c0 or c > c1:
                        _invalid()
                    item = _cell(record)
                    if not item["merged"] and (a != b or c != d):
                        _invalid()
                    for row in range(max(a, r0), min(b, r1) + 1):
                        for col in range(max(c, c0), min(d, c1) + 1):
                            location = (row + 1, col + 1)
                            if location in covered:
                                _invalid()
                            covered.add(location)
                            value = item if row == a and col == c else dict(item, value="", formula="", has_formula=True)
                            cells[location] = dict(value, column=col + 1)
                merged = _data(rpc.call_tool("sheet.get_merged_cells", dict(address,
                    start_row=r0, end_row=r1, start_col=c0, end_col=c1))).get("merged_ranges")
                if not isinstance(merged, list) or len(merged) > 50000:
                    _invalid()
                for record in merged:
                    a, b, c, d = _bounds(record, ("row_from", "row_to", "col_from", "col_to"))
                    area = dict(start_row=a + 1, end_row=b + 1,
                                start_column=c + 1, end_column=d + 1)
                    for row in range(max(a, r0), min(b, r1) + 1):
                        for col in range(max(c, c0), min(d, c1) + 1):
                            cell = cells[(row + 1, col + 1)]
                            if cell.get("merge_range", area) != area:
                                _invalid()
                            cell["merged"] = True
                            # Only the independent merge-range API supplies
                            # trusted boundaries, including an anchor outside
                            # the requested columns. Sparse cell flags alone
                            # must never establish a header-to-column mapping.
                            cell["merge_range"] = dict(area)
        missing = {key for key, cell in cells.items() if cell["number_format"] == UNKNOWN_FORMAT and not cell["merged"]}
        date_cells = {key for key, cell in cells.items()
                      if not cell["merged"] and cell["number_format"] != UNKNOWN_FORMAT
                      and is_date_format(cell["number_format"])}
        if complete_blanks and (missing or date_cells) and self.binding.workbook_name.lower().endswith(".xlsx"):
            def anchors(items):
                # External-book references have different textual spellings in
                # live API formulas and OOXML. Prefer ordinary, typed values.
                return {key: cell for key, cell in items.items()
                        if cell["value"] not in (None, "") and not cell["has_formula"]}
            known = anchors(cells)
            extra = {}
            if not known:
                # A requested column may be entirely blank after clearing.
                # Obtain a small live header anchor solely for verification.
                extra = self._read(rpc, sheet, sheet["rowFrom"] + 1,
                    min(sheet["rowFrom"] + 20, sheet["rowTo"] + 1, 100000),
                    list(range(sheet["colFrom"] + 1, min(sheet["colFrom"] + 30, sheet["colTo"] + 1) + 1)),
                    complete_blanks=False)
                candidates = anchors(extra)
                known = dict([next(iter(candidates.items()))]) if candidates else {}
            if not known:
                known = {key: cell for key, cell in (cells | extra).items() if cell["formula"]}
            # The live API does not include the workbook's date system. Verify
            # filled dates against the same XLSX used for sparse blanks rather
            # than losing their epoch as soon as they become nonempty.
            known.update({key: cells[key] for key in date_cells})
            if type(info.get("version")) is not int or info["version"] < 1:
                _invalid()
            blanks = load_blank_cells(rpc, self.binding.file_id, sheet["sheetName"], missing, known,
                                      expected_version=info["version"], metadata_coordinates=date_cells)
            for (row, column), cell in blanks.items():
                cells[(row, column)] = dict(cell, column=column)
        return cells

    def call(self, action: str, **payload) -> dict:
        try:
            return self._call(action, payload)
        except WpsError as exc:
            if exc.code == "AUTH_REQUIRED" and self.rpc is None:
                from .wps_skillhub import mark_account_expired
                mark_account_expired(self.binding.account_id, path=self.account_path)
            raise

    def _call(self, action, payload):
        validate_binding(self.binding)
        _validate_request(action, payload)
        with _LOCK:
            rpc = self._client()
            info, sheets = self._metadata(rpc)
            base = dict(ok=True, protocol=PROTOCOL, action=action, workbook_name=info["name"],
                        mutation_attempted=False, file_id=info["id"], can_edit=info["permission"]["update"])
            if action == "describe":
                return dict(base, sheets=[dict(name=s["sheetName"], used_start_row=s["rowFrom"] + 1,
                    used_end_row=s["rowTo"] + 1, used_start_column=s["colFrom"] + 1,
                    used_end_column=s["colTo"] + 1) for s in sheets])
            sheet = next((s for s in sheets if s["sheetName"] == payload["sheet"]), None)
            if sheet is None:
                raise WpsError("找不到指定工作表，请重新读取云文档。", code="SHEET_NOT_FOUND")
            if action == "read":
                cells = self._read(rpc, sheet, payload["start_row"], payload["end_row"], payload["columns"])
                return dict(base, rows=[dict(row=r, cells=[cells[(r, c)] for c in payload["columns"]])
                                        for r in range(payload["start_row"], payload["end_row"] + 1)])
            return self._apply(rpc, sheet, info, base, payload)

    def _apply(self, rpc, sheet, info, base, args):
        if not info["permission"]["update"] or sheet["isProtected"]:
            raise WpsError("当前账号没有这张工作表的编辑权限，本次未写入。", code="ACCESS_DENIED")
        if args["row_end"] - args["row_start"] >= 5000:
            raise WpsError("本次核对范围超过 5000 行，请缩小范围。", code="INVALID_ARGUMENTS")
        columns = sorted({h["column"] for h in args["expected_headers"]})
        headers = self._read(rpc, sheet, args["header_row"], args["header_row"], columns)
        for expected in args["expected_headers"]:
            item = headers[(args["header_row"], expected["column"])]
            if not header_matches(item, expected, args["header_row"]):
                raise WpsError("云表表头已变化，请重新读取后处理。", code="HEADER_MISMATCH")
        key_columns, target = args["key_columns"], args["target_column"]
        cells = self._read(rpc, sheet, args["row_start"], args["row_end"], columns)
        counts = Counter(_key(c["key_values"]) for c in args["changes"])
        positions = {key: [] for key in counts}
        for row in range(args["row_start"], args["row_end"] + 1):
            keys = [cells[(row, col)] for col in key_columns]
            if any(k["merged"] or k["value"] in (None, "") for k in keys):
                continue
            token = _key([k["value"] for k in keys])
            if token in positions:
                positions[token].append(row)
        result = dict(base, task_id=args["task_id"], batch_id=args["batch_id"], outcomes=[])
        pending, operations = [], []
        for index, change in enumerate(args["changes"]):
            token, outcome = _key(change["key_values"]), dict(index=index, status="conflict", row=change["row"], column=target)
            matches = positions[token]
            result["outcomes"].append(outcome)
            if not matches:
                outcome["status"] = "not_found"
                continue
            if len(matches) != 1 or counts[token] != 1:
                outcome["status"] = "duplicate"
                continue
            row = matches[0]
            outcome["row"] = row
            current = cells[(row, target)]
            if current["merged"]:
                outcome["status"] = "merged"
                continue
            if current["has_formula"]:
                outcome["status"] = "formula"
                continue
            if (not _same(current["value"], change["old_value"])
                    or not _same(current["formula"], change["old_formula"])
                    or current["number_format"] != change["old_number_format"]
                    or any(cells[(row, g["column"])]["merged"]
                           or not _same(cells[(row, g["column"])]["value"], g["value"])
                           or not _same(cells[(row, g["column"])]["formula"], g["formula"]) for g in change["guards"])):
                continue
            new = "" if change["new_value"] is None else change["new_value"]
            if _same(current["value"], new):
                outcome["status"] = "unchanged"
                continue
            if new == "" and not self.binding.workbook_name.lower().endswith(".xlsx"):
                raise WpsError("这类云表暂不能核对清空后的格式，请使用 .xlsx 表格处理清空操作。", code="UNSUPPORTED_CELL_METADATA")
            # A sparse blank is not evidence of a General-format cell.
            if current["number_format"] == UNKNOWN_FORMAT:
                raise WpsError("WPS 没有提供目标空白格的格式，暂不能安全写入；本批未修改。", code="UNSUPPORTED_CELL_METADATA")
            if type(new) in (int, float, bool) and current["number_format"] == "@":
                raise WpsError("目标单元格是文本格式，不能保证数字类型；请先调整目标格式后重新处理。", code="UNSUPPORTED_CELL_METADATA")
            formula = ("" if new == "" else "'" + new if isinstance(new, str)
                       else "TRUE" if new is True else "FALSE" if new is False else json.dumps(new, allow_nan=False))
            operations.append(dict(op_type="cell_operation_type_formula", row_from=row - 1, row_to=row - 1,
                                   col_from=target - 1, col_to=target - 1, formula=formula))
            pending.append((outcome, current, new))
        if not operations:
            return result
        if self.rpc is None:
            from .wps_skillhub import require_account
            require_account(self.binding.account_id, path=self.account_path)
        result["mutation_attempted"] = True
        try:
            _data(rpc.call_tool("sheet.range_data_batch_update", dict(file_id=self.binding.file_id,
                worksheet_id=sheet["sheetId"], range_data=operations), mutation=True), mutation=True)
            after = self._read(rpc, sheet, min(o[0]["row"] for o in pending), max(o[0]["row"] for o in pending), columns)
            for outcome, before, new in pending:
                item = after[(outcome["row"], target)]
                if (not target_value_matches(item["value"], new, before["number_format"])
                        or item["has_formula"] or item["merged"]
                        or item["number_format"] != before["number_format"]):
                    raise WpsError("云表写入后核对不一致，请先查看处理记录。", code="WRITE_VERIFY_FAILED", mutation_attempted=True)
                outcome["status"] = "applied"
        except WpsError as exc:
            exc.mutation_attempted = True
            exc.result = result
            raise
        except Exception:
            raise WpsError("云表更新结果待核对，请先查看处理记录。", code="TRANSPORT_ERROR", mutation_attempted=True, result=result) from None
        return result
