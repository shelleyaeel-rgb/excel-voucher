"""Read-only XLSX metadata for cells omitted by WPS's sparse range API.

The download URL comes from the authorized, public download_file tool. Only
its short-lived signature is used for the download: no account token, browser
cookie, redirects, or saved URLs. A version/hash and live-cell cross-check is
required before a missing cell's format can be trusted.
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
from openpyxl.utils.datetime import to_excel

from .wps_service import WpsError

MAX_DOWNLOAD_BYTES = 20 * 1024 * 1024
MAX_EXPANDED_BYTES = 64 * 1024 * 1024
MAX_ZIP_ENTRIES = 2048
MAX_XLSX_CELLS = 250000
_ID = re.compile(r"[A-Za-z0-9_-]{1,128}\Z")
_COLORS = {"黑色": "Black", "蓝色": "Blue", "青色": "Cyan", "绿色": "Green",
           "洋红": "Magenta", "洋红色": "Magenta", "红色": "Red", "白色": "White", "黄色": "Yellow"}


def normalize_number_format(value):
    if not isinstance(value, str) or len(value) > 1024:
        raise WpsError("无法核对云表数字格式。", code="UNSUPPORTED_CELL_METADATA")
    if value in ("G/通用格式", "常规", "General", "general", "GENERAL"):
        return "General"
    # WPS drops optional escapes on literal parentheses and minus signs in
    # accounting/date formats. Keep scientific E\- distinct from E-, bracket
    # directives, quoted text, and spacing/repetition tokens unchanged.
    result, quoted, brackets, index = [], False, 0, 0
    while index < len(value):
        char = value[index]
        if char in ("\\", "_", "*") and index + 1 < len(value):
            following = value[index + 1]
            optional = following in "()" or (following == "-" and (index == 0 or value[index - 1] not in "Ee"))
            result.append(following if char == "\\" and not quoted and not brackets and optional else char + following)
            index += 2
            continue
        if char == '"':
            quoted = not quoted
        elif not quoted and char == "[":
            end = value.find("]", index + 1)
            color = value[index + 1:end] if end != -1 else ""
            if not brackets and color in _COLORS:
                result.append("[" + _COLORS[color] + "]")
                index = end + 1
                continue
            brackets += 1
        elif not quoted and char == "]":
            brackets = max(0, brackets - 1)
        result.append(char)
        index += 1
    return "".join(result)


def _fail(code="SNAPSHOT_CHANGED"):
    raise WpsError("云表下载副本与当前数据尚未一致，无法核对空白格格式；请稍后重新读取。", code=code)


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
    if (info.get("id") != file_id or type(info.get("version")) is not int
            or info["version"] < 1 or not isinstance(info.get("name"), str)
            or not info["name"].lower().endswith(".xlsx")):
        _fail("UNSUPPORTED_CELL_METADATA")
    digest = info.get("hash")
    if not isinstance(digest, dict) or digest.get("type") not in ("md5", "sha1", "sha256"):
        _fail("UNSUPPORTED_CELL_METADATA")
    return info


class _NoDownloadRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        _fail("REDIRECT_BLOCKED")


def _download(url, opener=None):
    try:
        parsed = urllib.parse.urlsplit(url)
        host = parsed.hostname or ""
        if (parsed.scheme != "https" or parsed.username or parsed.password or parsed.fragment
                or parsed.port not in (None, 443) or not parsed.query
                or not host.isascii() or not host.endswith((".ag.kdocs.cn", ".ag.wps.cn"))
                or not re.fullmatch(r"[a-z0-9.-]+", host)
                or any(c.isspace() for c in url)):
            _fail("UNTRUSTED_DOWNLOAD_URL")
        transport = opener or urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoDownloadRedirect())
        # In particular, do not attach SkillHub Authorization to this URL.
        request = urllib.request.Request(url, headers={"Accept": "application/octet-stream"})
        with transport.open(request, timeout=30) as response:
            if getattr(response, "status", 200) != 200:
                _fail("DOWNLOAD_FAILED")
            raw = response.read(MAX_DOWNLOAD_BYTES + 1)
        if not raw or len(raw) > MAX_DOWNLOAD_BYTES:
            _fail("SNAPSHOT_TOO_LARGE")
        return raw
    except WpsError:
        raise
    except (ValueError, TypeError, urllib.error.URLError, OSError, TimeoutError):
        _fail("DOWNLOAD_FAILED")


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


def _explicit_styles(xml, sheet_name):
    book = ElementTree.fromstring(xml["xl/workbook.xml"])
    sheet = next((item for item in book.iter() if item.tag.endswith("}sheet") and item.get("name") == sheet_name), None)
    if sheet is None:
        _fail()
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
    return {item.get("r") for item in root.iter() if item.tag.endswith("}c") and item.get("s") is not None}


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


def load_blank_cells(rpc, file_id, sheet_name, missing_coordinates, known_cells, *, expected_version=None, opener=None):
    """Return metadata only for omitted cells proven empty in the current XLSX.

    Coordinates are one-based. ``known_cells`` must contain at least one
    nonempty/formula cell from the same live API read for a consistency check.
    No downloaded business file or signed URL is persisted.
    """
    workbooks = []
    try:
        missing = set(missing_coordinates)
        coordinates = missing | set(known_cells)
        if (not isinstance(file_id, str) or not _ID.fullmatch(file_id) or not isinstance(sheet_name, str)
                or not missing or len(missing) > 20000 or len(coordinates) > 20001
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
            _fail()
        download = _data(rpc.call_tool("download_file", {"file_id": file_id, "with_hash": True,
                            "internal": False, "storage_base_domain": "wps365.com"}))
        size = download.get("physical_size")
        if type(size) is not int or not 0 < size <= MAX_DOWNLOAD_BYTES:
            _fail("SNAPSHOT_TOO_LARGE")
        raw = _download(download.get("url"), opener)
        hashes = download.get("hashes")
        if (len(raw) != size or not _hash_matches(raw, before["hash"])
                or not isinstance(hashes, list) or not any(_hash_matches(raw, digest) for digest in hashes)):
            _fail()
        xml = _xml_files(raw)
        explicit = _explicit_styles(xml, sheet_name)
        book = load_workbook(io.BytesIO(raw), data_only=False, keep_links=False)
        workbooks.append(book)
        cached = load_workbook(io.BytesIO(raw), data_only=True, keep_links=False)
        workbooks.append(cached)
        if sheet_name not in book.sheetnames or len(book[sheet_name].merged_cells.ranges) > 1024:
            _fail("INVALID_SNAPSHOT")
        sheet, cached_sheet = book[sheet_name], cached[sheet_name]
        merged = list(sheet.merged_cells.ranges)

        def metadata(key):
            row, col = key
            cell = sheet.cell(row, col)
            formula = cell.value if cell.data_type == "f" else ""
            if not isinstance(formula, str):
                _fail("UNSUPPORTED_CELL_METADATA")
            return dict(value=_scalar(cached_sheet.cell(row, col).value, book.epoch),
                        formula=formula, has_formula=bool(formula), number_format=_format(sheet, cell, explicit),
                        merged=any(area.min_row <= row <= area.max_row and area.min_col <= col <= area.max_col for area in merged))

        for key, expected in anchors.items():
            found = metadata(key)
            if (not _same(found["value"], "" if expected["value"] is None else expected["value"])
                    or found["formula"] != expected.get("formula", "")
                    or found["has_formula"] != expected.get("has_formula")
                    or found["number_format"] != normalize_number_format(expected.get("number_format"))
                    or found["merged"] != expected.get("merged")):
                _fail()
        result = {}
        for key in missing:
            found = metadata(key)
            if found["value"] != "" or found["has_formula"]:
                _fail()
            result[key] = found
        after = _info(rpc, file_id)
        if any(before.get(key) != after.get(key) for key in ("id", "name", "version", "hash")):
            _fail()
        return result
    except WpsError:
        raise
    except Exception:
        # Library/network exceptions can contain the signed URL or workbook
        # content. Keep both out of chat, diagnostic logs and task journals.
        _fail("INVALID_SNAPSHOT")
    finally:
        for book in workbooks:
            book.close()
