"""Read-only XLSX formats and date epochs missing from WPS's range API.

The download URL comes from the authorized, public download_file tool. Only
its short-lived signature is used for the download: no account token, browser
cookie, redirects, or saved URLs. A version/hash and live-cell cross-check is
required before a missing cell's format or a live date's epoch can be trusted.
"""
from __future__ import annotations

import hashlib
import io
import math
import posixpath
import re
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from datetime import date, datetime, time, timedelta
from xml.etree import ElementTree

from openpyxl import load_workbook
from openpyxl.utils.cell import get_column_letter
from openpyxl.utils.datetime import CALENDAR_MAC_1904, CALENDAR_WINDOWS_1900, to_excel

from .wps_service import WpsError, safe_wps_diagnostic

MAX_DOWNLOAD_BYTES = 20 * 1024 * 1024
MAX_EXPANDED_BYTES = 64 * 1024 * 1024
MAX_ZIP_ENTRIES = 2048
MAX_XLSX_CELLS = 250000
_ID = re.compile(r"[A-Za-z0-9_-]{1,128}\Z")
_COLORS = {"黑色": "Black", "蓝色": "Blue", "青色": "Cyan", "绿色": "Green",
           "洋红": "Magenta", "洋红色": "Magenta", "红色": "Red", "白色": "White", "黄色": "Yellow"}
_GENERAL_TOKENS = ("G/通用格式", "常规", "General")


def normalize_number_format(value):
    if not isinstance(value, str) or len(value) > 1024:
        raise WpsError("无法核对云表数字格式。", code="UNSUPPORTED_CELL_METADATA")
    # WPS drops optional escapes on literal parentheses and minus signs in
    # accounting/date formats. Keep scientific E\- distinct from E-, bracket
    # directives, quoted text, and spacing/repetition tokens unchanged.
    result, quoted, brackets, index, word = [], False, 0, 0, False
    while index < len(value):
        char = value[index]
        if char in ("\\", "_", "*") and index + 1 < len(value):
            following = value[index + 1]
            optional = following in "()" or (following == "-" and (index == 0 or value[index - 1] not in "Ee"))
            result.append(following if char == "\\" and not quoted and not brackets and optional else char + following)
            index += 2
            word = False
            continue
        # General is a format token, including in General"月" and separate
        # conditional sections. Never rewrite quoted text, directives, an
        # escaped/spacing character, or part of a longer unquoted word.
        if not quoted and not brackets and not word:
            token = next((token for token in _GENERAL_TOKENS
                          if value[index:index + len(token)].casefold() == token.casefold()
                          and (index + len(token) == len(value)
                               or not value[index + len(token)].isalnum())), None)
            if token is not None:
                result.append("General")
                index += len(token)
                word = True
                continue
        if char == '"':
            quoted = not quoted
            word = False
        elif not quoted and char == "[":
            end = value.find("]", index + 1)
            color = value[index + 1:end] if end != -1 else ""
            if not brackets and color in _COLORS:
                result.append("[" + _COLORS[color] + "]")
                index = end + 1
                word = False
                continue
            brackets += 1
            word = False
        elif not quoted and char == "]":
            brackets = max(0, brackets - 1)
            word = False
        elif not quoted and not brackets:
            word = char.isalnum() or char == "_"
        result.append(char)
        index += 1
    return "".join(result)


def _fail(code="SNAPSHOT_CHANGED", *, sheet=None, coordinate=None, field=None, stage="snapshot"):
    diagnostic = {"stage": stage, "sheet": sheet, "field": field}
    if coordinate is not None:
        diagnostic.update(row=coordinate[0], column=coordinate[1])
    diagnostic = safe_wps_diagnostic(diagnostic)
    position = "工作表“" + diagnostic["sheet"] + "”" if "sheet" in diagnostic else "云表"
    if "row" in diagnostic and "column" in diagnostic:
        position += "的 " + get_column_letter(diagnostic["column"]) + str(diagnostic["row"])
    kinds = {"VALUE_MISMATCH": "数据", "TYPE_MISMATCH": "数据类型", "FORMULA_MISMATCH": "公式",
             "FORMAT_MISMATCH": "单元格格式", "MERGE_MISMATCH": "合并状态"}
    if code in kinds:
        message = f"{position}的{kinds[code]}与下载副本不一致，已停止继续处理；请核对该单元格。"
    else:
        message = {
            "SNAPSHOT_CHANGED": "云表文件版本或校验值暂未一致，无法核对空白格格式；请稍后重新读取。",
            "SNAPSHOT_UNVERIFIED": "没有找到可用于核对的单元格，暂不能确认空白格格式。",
            "UNSUPPORTED_CELL_METADATA": "暂时无法识别云表的单元格类型或格式，已停止继续处理。",
            "UNTRUSTED_DOWNLOAD_URL": "WPS 返回的下载地址无法验证，已停止读取。",
            "REDIRECT_BLOCKED": "WPS 下载地址发生了未预期的跳转，已停止读取。",
            "DOWNLOAD_FAILED": "下载云表副本失败，请检查网络后重新读取。",
            "SNAPSHOT_TOO_LARGE": "云表副本超过可读取的文件大小或内容范围，暂不能核验空白格格式。",
            "INVALID_RESPONSE": "WPS 返回的文件信息不完整，无法继续核验。",
            "INVALID_SNAPSHOT": "下载副本无法解析或格式不受支持，已停止读取。",
            "INVALID_ARGUMENTS": "云表核对范围不正确，已停止操作。",
            "WORKBOOK_MISMATCH": "下载副本对应的文档或工作表已变化，请重新选择文档。",
        }.get(code, "云表副本核对失败，已停止继续处理。")
    raise WpsError(message, code=code, diagnostic=diagnostic)


def _data(reply):
    if not isinstance(reply, dict) or reply.get("code") not in (0, 200):
        _fail("INVALID_RESPONSE")
    data = reply.get("data")
    if not isinstance(data, dict):
        _fail("INVALID_RESPONSE")
    if "code" in data:
        code = data.get("code")
        if code in (401, 400006, "401", "400006"):
            raise WpsError("WPS 授权已失效，请重新连接。", code="AUTH_REQUIRED")
        if code in (403, 403001, "403", "403001"):
            raise WpsError("当前账号无权读取此文档，请核对文档权限。", code="ACCESS_DENIED")
        if code in (429, "429"):
            raise WpsError("WPS 请求过于频繁，请稍后重新读取。", code="RATE_LIMITED")
        if type(code) is not int or code != 0 or not isinstance(data.get("data"), dict):
            _fail("INVALID_RESPONSE")
        data = data["data"]
    return data


def _info(rpc, file_id):
    info = _data(rpc.call_tool("get_file_info", {"file_id": file_id}))
    if info.get("id") != file_id:
        _fail("WORKBOOK_MISMATCH", field="identity")
    if (type(info.get("version")) is not int
            or info["version"] < 1 or not isinstance(info.get("name"), str)
            or not info["name"].lower().endswith(".xlsx")):
        _fail("UNSUPPORTED_CELL_METADATA")
    digest = info.get("hash")
    if not isinstance(digest, dict) or digest.get("type") not in ("md5", "sha1", "sha256"):
        _fail("UNSUPPORTED_CELL_METADATA")
    return info


class _NoDownloadRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        _fail("REDIRECT_BLOCKED", stage="download")


def _download(url, opener=None):
    try:
        parsed = urllib.parse.urlsplit(url)
        host = parsed.hostname or ""
        if (parsed.scheme != "https" or parsed.username or parsed.password or parsed.fragment
                or parsed.port not in (None, 443) or not parsed.query
                or not host.isascii() or not host.endswith((".ag.kdocs.cn", ".ag.wps.cn"))
                or not re.fullmatch(r"[a-z0-9.-]+", host)
                or any(c.isspace() for c in url)):
            _fail("UNTRUSTED_DOWNLOAD_URL", stage="download")
        transport = opener or urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoDownloadRedirect())
        # In particular, do not attach SkillHub Authorization to this URL.
        request = urllib.request.Request(url, headers={"Accept": "application/octet-stream"})
        with transport.open(request, timeout=30) as response:
            if getattr(response, "status", 200) != 200:
                _fail("DOWNLOAD_FAILED", stage="download")
            raw = response.read(MAX_DOWNLOAD_BYTES + 1)
        if not raw or len(raw) > MAX_DOWNLOAD_BYTES:
            _fail("SNAPSHOT_TOO_LARGE", stage="download")
        return raw
    except WpsError:
        raise
    except (ValueError, TypeError, urllib.error.URLError, OSError, TimeoutError):
        _fail("DOWNLOAD_FAILED", stage="download")


def _hash_matches(raw, digest):
    return (isinstance(digest, dict) and digest.get("type") in ("md5", "sha1", "sha256")
            and isinstance(digest.get("sum"), str)
            and hashlib.new(digest["type"], raw).hexdigest() == digest["sum"].lower())


def _xml_files(raw):
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        entries = archive.infolist()
        if (len(entries) > MAX_ZIP_ENTRIES or len({entry.filename for entry in entries}) != len(entries)
                or sum(entry.file_size for entry in entries) > MAX_EXPANDED_BYTES
                or any(entry.flag_bits & 1 or entry.file_size > 32 * 1024 * 1024
                       or (entry.file_size > 1024 * 1024 and entry.file_size > max(1, entry.compress_size) * 300)
                       for entry in entries)):
            _fail("SNAPSHOT_TOO_LARGE")
        xml = {}
        cells = 0
        for entry in entries:
            if not entry.filename.endswith((".xml", ".rels")):
                continue
            body = archive.read(entry)
            # WPS's XLSX XML is UTF-8; reject DTD/entity declarations rather
            # than allowing a workbook to expand arbitrary XML entities.
            text = body.decode("utf-8-sig")
            upper = text.upper()
            if "<!DOCTYPE" in upper or "<!ENTITY" in upper:
                _fail("INVALID_SNAPSHOT")
            cells += len(re.findall(r"<(?:[A-Za-z_][\w.-]*:)?c(?:\s|>)", text))
            if cells > MAX_XLSX_CELLS:
                _fail("SNAPSHOT_TOO_LARGE")
            xml[entry.filename] = body
        if not {"xl/workbook.xml", "xl/_rels/workbook.xml.rels", "xl/styles.xml"}.issubset(xml):
            _fail("INVALID_SNAPSHOT")
        return xml


def _worksheet_metadata(xml, sheet_name):
    book = ElementTree.fromstring(xml["xl/workbook.xml"])
    sheet = next((item for item in book.iter() if item.tag.endswith("}sheet") and item.get("name") == sheet_name), None)
    if sheet is None:
        _fail("WORKBOOK_MISMATCH", sheet=sheet_name, field="identity")
    rid = sheet.get("{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id")
    links = ElementTree.fromstring(xml["xl/_rels/workbook.xml.rels"])
    link = next((item for item in links if item.get("Id") == rid), None)
    if link is None or link.get("TargetMode") == "External":
        _fail("INVALID_SNAPSHOT")
    target = link.get("Target", "")
    path = posixpath.normpath(target.lstrip("/") if target.startswith("/") else "xl/" + target)
    if not path.startswith("xl/worksheets/") or path not in xml:
        _fail("INVALID_SNAPSHOT")
    root = ElementTree.fromstring(xml[path])
    namespace = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
    if root.tag != namespace + "worksheet":
        _fail("INVALID_SNAPSHOT")
    explicit, numbers, seen = set(), {}, set()
    for item in root.findall(namespace + "sheetData/" + namespace + "row/" + namespace + "c"):
        coordinate = item.get("r")
        if (not isinstance(coordinate, str) or not re.fullmatch(r"[A-Z]{1,3}[1-9][0-9]{0,6}", coordinate)
                or coordinate in seen):
            _fail("INVALID_SNAPSHOT")
        seen.add(coordinate)
        if item.get("s") is not None:
            explicit.add(coordinate)
        # Preserve the actual stored serial, before openpyxl turns date and
        # time formats into datetime/timedelta and rounds to milliseconds.
        # Text, booleans and error cells must retain their original types.
        if item.get("t", "n") != "n":
            continue
        values = item.findall(namespace + "v")
        if len(values) > 1:
            _fail("INVALID_SNAPSHOT")
        if not values or values[0].text is None:
            continue
        text = values[0].text
        if (len(text) > 1024 or not re.fullmatch(r"[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[Ee][+-]?[0-9]+)?", text)):
            _fail("INVALID_SNAPSHOT")
        value = float(text) if any(char in text for char in ".Ee") else int(text)
        if type(value) is float and not math.isfinite(value):
            _fail("INVALID_SNAPSHOT")
        numbers[coordinate] = value
    return explicit, numbers


def _format(sheet, cell, explicit):
    if cell.coordinate in explicit:
        return normalize_number_format(cell.number_format)
    row = sheet.row_dimensions.get(cell.row)
    if row is not None and row.has_style:
        return normalize_number_format(row.number_format)
    columns = [item for item in sheet.column_dimensions.values()
               if item.min <= cell.column <= item.max and item.has_style]
    if len(columns) > 1:
        _fail("UNSUPPORTED_CELL_METADATA")
    return normalize_number_format(columns[0].number_format if columns else cell.number_format)


def _scalar(value, epoch):
    if value is None:
        return ""
    if isinstance(value, (datetime, date, time, timedelta)):
        return to_excel(value, epoch)
    if type(value) not in (str, int, float, bool) or (type(value) is float and not math.isfinite(value)):
        _fail("UNSUPPORTED_CELL_METADATA")
    return value


def _same(a, b):
    return (type(a) is type(b) or type(a) in (int, float) and type(b) in (int, float)) and a == b


def load_blank_cells(rpc, file_id, sheet_name, missing_coordinates, known_cells, *,
                     expected_version=None, opener=None, metadata_coordinates=()):
    """Return verified metadata for omitted blanks and requested live cells.

    Coordinates are one-based. ``known_cells`` must contain at least one
    nonempty/formula cell from the same live API read for a consistency check.
    Additional ``metadata_coordinates`` must be supplied in ``known_cells``
    and are checked exactly against that live read before exposing the XLSX
    date system. They may be nonempty, so filled dates retain their epoch even
    when the requested range no longer contains an omitted blank.
    No downloaded business file or signed URL is persisted.
    """
    workbooks = []
    try:
        missing = set(missing_coordinates)
        requested_metadata = set(metadata_coordinates)
        requested = missing | requested_metadata
        coordinates = requested | set(known_cells)
        if (not isinstance(file_id, str) or not _ID.fullmatch(file_id) or not isinstance(sheet_name, str)
                or not requested or len(requested) > 20000 or len(coordinates) > 20001
                or not requested_metadata.issubset(known_cells)
                or any(not isinstance(key, tuple) or len(key) != 2
                       or type(key[0]) is not int or not 1 <= key[0] <= 100000
                       or type(key[1]) is not int or not 1 <= key[1] <= 16384 for key in coordinates)):
            _fail("INVALID_ARGUMENTS")
        anchors = {key: cell for key, cell in known_cells.items()
                   if cell.get("has_formula") or cell.get("value") not in (None, "")}
        if not anchors:
            _fail("SNAPSHOT_UNVERIFIED")
        before = _info(rpc, file_id)
        if expected_version is not None and before["version"] != expected_version:
            _fail(sheet=sheet_name, field="version")
        download = _data(rpc.call_tool("download_file", {"file_id": file_id, "with_hash": True,
                            "internal": False, "storage_base_domain": "wps365.com"}))
        size = download.get("physical_size")
        if type(size) is not int or not 0 < size <= MAX_DOWNLOAD_BYTES:
            _fail("SNAPSHOT_TOO_LARGE")
        raw = _download(download.get("url"), opener)
        hashes = download.get("hashes")
        if (len(raw) != size or not _hash_matches(raw, before["hash"])
                or not isinstance(hashes, list) or not any(_hash_matches(raw, digest) for digest in hashes)):
            _fail(sheet=sheet_name, field="hash")
        xml = _xml_files(raw)
        explicit, numbers = _worksheet_metadata(xml, sheet_name)
        book = load_workbook(io.BytesIO(raw), data_only=False, keep_links=False)
        workbooks.append(book)
        cached = load_workbook(io.BytesIO(raw), data_only=True, keep_links=False)
        workbooks.append(cached)
        if sheet_name not in book.sheetnames or len(book[sheet_name].merged_cells.ranges) > 1024:
            _fail("INVALID_SNAPSHOT")
        sheet, cached_sheet = book[sheet_name], cached[sheet_name]
        merged = list(sheet.merged_cells.ranges)
        date_system = {CALENDAR_WINDOWS_1900: "1900", CALENDAR_MAC_1904: "1904"}.get(book.epoch)
        if date_system is None:
            _fail("UNSUPPORTED_CELL_METADATA")

        def metadata(key):
            row, col = key
            cell = sheet.cell(row, col)
            formula = cell.value if cell.data_type == "f" else ""
            if not isinstance(formula, str):
                _fail("UNSUPPORTED_CELL_METADATA")
            value = numbers[cell.coordinate] if cell.coordinate in numbers else _scalar(cached_sheet.cell(row, col).value, book.epoch)
            return dict(value=value,
                        formula=formula, has_formula=bool(formula), number_format=_format(sheet, cell, explicit),
                        merged=any(area.min_row <= row <= area.max_row and area.min_col <= col <= area.max_col for area in merged))

        checked = anchors | {key: known_cells[key] for key in requested_metadata}
        for key, expected in checked.items():
            found = metadata(key)
            expected_value = "" if expected["value"] is None else expected["value"]
            if not _same(found["value"], expected_value):
                same_type = (type(found["value"]) is type(expected_value)
                             or type(found["value"]) in (int, float) and type(expected_value) in (int, float))
                _fail("VALUE_MISMATCH" if same_type else "TYPE_MISMATCH", sheet=sheet_name,
                      coordinate=key, field="value" if same_type else "type")
            if found["formula"] != expected.get("formula", "") or found["has_formula"] != expected.get("has_formula"):
                _fail("FORMULA_MISMATCH", sheet=sheet_name, coordinate=key, field="formula")
            if found["number_format"] != normalize_number_format(expected.get("number_format")):
                _fail("FORMAT_MISMATCH", sheet=sheet_name, coordinate=key, field="number_format")
            if found["merged"] != expected.get("merged"):
                _fail("MERGE_MISMATCH", sheet=sheet_name, coordinate=key, field="merged")
        result = {}
        for key in missing:
            found = metadata(key)
            if found["has_formula"]:
                _fail("FORMULA_MISMATCH", sheet=sheet_name, coordinate=key, field="formula")
            if found["value"] != "":
                _fail("VALUE_MISMATCH", sheet=sheet_name, coordinate=key, field="value")
            # A date's displayed format does not reveal its numeric epoch.
            # Expose only the date system read from this verified workbook.
            result[key] = dict(found, date_system=date_system)
        for key in requested_metadata:
            # These live values were compared exactly above; returning their
            # metadata must never substitute unverified downloaded business data.
            result[key] = dict(metadata(key), date_system=date_system)
        after = _info(rpc, file_id)
        if any(before.get(key) != after.get(key) for key in ("id", "name")):
            _fail("WORKBOOK_MISMATCH", sheet=sheet_name, field="identity")
        for field in ("version", "hash"):
            if before.get(field) != after.get(field):
                _fail(sheet=sheet_name, field=field)
        return result
    except WpsError as exc:
        exc.diagnostic = safe_wps_diagnostic({"stage": "snapshot", "sheet": sheet_name, **exc.diagnostic})
        raise
    except Exception:
        # Library/network exceptions can contain the signed URL or workbook
        # content. Keep both out of chat, diagnostic logs and task journals.
        _fail("INVALID_SNAPSHOT", sheet=sheet_name)
    finally:
        for book in workbooks:
            book.close()
