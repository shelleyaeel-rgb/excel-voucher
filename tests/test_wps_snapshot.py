"""Bounded, credential-free XLSX download and live-data consistency checks."""
import hashlib
import io
import math
import zipfile
from xml.etree import ElementTree

import pytest
from openpyxl import Workbook, load_workbook
from openpyxl.utils.datetime import CALENDAR_MAC_1904, CALENDAR_WINDOWS_1900, to_excel

from xiaoyu import wps_snapshot as snapshot
from xiaoyu.wps_service import WpsError

URL = "https://ksc-bj.ag.kdocs.cn/download/test?signature=private-test-signature"
FILE_ID = "test-file-id"


def workbook_bytes():
    book = Workbook()
    sheet = book.active
    sheet.title = "汇总"
    sheet["A1"] = "业务单号"
    sheet["B1"] = 12.34
    sheet["B1"].number_format = "0.00"
    sheet["B2"].number_format = "0.0000"
    sheet["B3"].number_format = "@"
    sheet["B4"] = '=""'
    sheet.column_dimensions["C"].number_format = "@"
    sheet.row_dimensions[6].number_format = "0.000"
    sheet.merge_cells("D3:E3")
    stream = io.BytesIO()
    book.save(stream)
    return stream.getvalue()


def envelope(data):
    return {"code": 0, "data": {"code": 0, "data": data}}


class Rpc:
    def __init__(self, raw, *, versions=(7, 7), url=URL, name="测试.xlsx"):
        self.raw, self.versions, self.url, self.name = raw, list(versions), url, name
        self.calls = []
        self.hash = {"type": "sha1", "sum": hashlib.sha1(raw).hexdigest()}

    def call_tool(self, name, args):
        self.calls.append((name, args))
        assert args["file_id"] == FILE_ID
        if name == "get_file_info":
            return envelope(dict(id=FILE_ID, name=self.name, version=self.versions.pop(0), hash=self.hash))
        assert name == "download_file"
        assert args == dict(file_id=FILE_ID, with_hash=True, internal=False, storage_base_domain="wps365.com")
        return envelope(dict(physical_size=len(self.raw), url=self.url, hashes=[self.hash]))


class Opener:
    def __init__(self, raw, error=None):
        self.raw, self.error, self.requests = raw, error, []

    def open(self, request, timeout):
        self.requests.append(request)
        if self.error:
            raise self.error
        return io.BytesIO(self.raw)


def anchors():
    return {(1, 1): dict(value="业务单号", formula="", has_formula=False, number_format="G/通用格式", merged=False),
            (1, 2): dict(value=12.34, formula="", has_formula=False, number_format="0.00", merged=False)}


def load(raw=None, missing=None, known=None, **kwargs):
    raw = raw or workbook_bytes()
    rpc = kwargs.pop("rpc", Rpc(raw))
    opener = kwargs.pop("opener", Opener(raw))
    result = snapshot.load_blank_cells(rpc, FILE_ID, "汇总", missing or {(2, 2), (3, 2)},
                                      anchors() if known is None else known, opener=opener, **kwargs)
    return result, rpc, opener


def test_blank_formats_preserve_text_currency_and_row_column_inheritance_without_auth_headers():
    result, rpc, opener = load(missing={(2, 2), (3, 2), (2, 3), (6, 3), (3, 5)}, expected_version=7)
    assert {key: value["number_format"] for key, value in result.items()} == {
        (2, 2): "0.0000", (3, 2): "@", (2, 3): "@", (6, 3): "0.000", (3, 5): "General"}
    assert result[(3, 5)]["merged"] is True
    assert all(item["value"] == "" and not item["has_formula"] for item in result.values())
    assert [name for name, args in rpc.calls] == ["get_file_info", "download_file", "get_file_info"]
    assert opener.requests[0].header_items() == [("Accept", "application/octet-stream")]
    assert "private-test-signature" not in repr(result)


@pytest.mark.parametrize("url", ["http://ksc-bj.ag.kdocs.cn/a?s=1", "https://evil.example/a?s=1",
    "https://ksc-bj.ag.kdocs.cn.evil.example/a?s=1", "https://user@ksc-bj.ag.kdocs.cn/a?s=1",
    "https://ksc-bj.ag.kdocs.cn:444/a?s=1", "https://ksc-bj.ag.kdocs.cn/a",
    "https://ksc-bj.ag.kdocs.cn/a?s=1#fragment", "https://127.0.0.1/a?s=1"])
def test_untrusted_or_unsigned_download_never_opens_url(url):
    raw = workbook_bytes()
    opener = Opener(raw)
    with pytest.raises(WpsError) as caught:
        load(raw, rpc=Rpc(raw, url=url), opener=opener)
    assert caught.value.code == "UNTRUSTED_DOWNLOAD_URL"
    assert not opener.requests and url not in str(caught.value)


def test_redirect_and_network_exceptions_do_not_leak_url_or_signature():
    with pytest.raises(WpsError) as caught:
        snapshot._NoDownloadRedirect().redirect_request(None, None, 302, None, None, URL)
    assert caught.value.code == "REDIRECT_BLOCKED"
    with pytest.raises(WpsError) as caught:
        load(opener=Opener(b"", OSError(URL)))
    assert caught.value.code == "DOWNLOAD_FAILED"
    assert "private-test-signature" not in str(caught.value)
    assert caught.value.diagnostic == {"stage": "download", "sheet": "汇总"}


@pytest.mark.parametrize("versions,expected", [((8, 8), 7), ((7, 8), 7)])
def test_file_version_changes_reject_snapshot(versions, expected):
    raw = workbook_bytes()
    with pytest.raises(WpsError) as caught:
        load(raw, rpc=Rpc(raw, versions=versions), expected_version=expected)
    assert caught.value.code == "SNAPSHOT_CHANGED"
    assert caught.value.diagnostic == {"stage": "snapshot", "sheet": "汇总", "field": "version"}


@pytest.mark.parametrize("coordinate,code,field", [((1, 2), "VALUE_MISMATCH", "value"),
    ((4, 2), "FORMULA_MISMATCH", "formula")])
def test_snapshot_cannot_supply_missing_data_or_hide_a_formula(coordinate, code, field):
    with pytest.raises(WpsError) as caught:
        load(missing={coordinate})
    assert caught.value.code == code
    assert caught.value.diagnostic == {"stage": "snapshot", "sheet": "汇总", "field": field,
                                       "row": coordinate[0], "column": coordinate[1]}


@pytest.mark.parametrize("field,value,code,diagnostic_field", [
    ("value", "12.34", "TYPE_MISMATCH", "type"),
    ("value", 12.35, "VALUE_MISMATCH", "value"),
    ("formula", "=SECRET_FORMULA()", "FORMULA_MISMATCH", "formula"),
    ("has_formula", True, "FORMULA_MISMATCH", "formula"),
    ("number_format", '0.00"PRIVATE_FORMAT"', "FORMAT_MISMATCH", "number_format"),
    ("merged", True, "MERGE_MISMATCH", "merged")])
def test_live_nonempty_cell_must_match_value_type_formula_format_and_merge(field, value, code, diagnostic_field):
    known = anchors()
    known[(1, 2)][field] = value
    with pytest.raises(WpsError) as caught:
        load(known=known)
    error = caught.value
    assert error.code == code
    assert error.diagnostic == {"stage": "snapshot", "sheet": "汇总", "row": 1, "column": 2,
                                "field": diagnostic_field}
    message = str(error)
    assert "汇总" in message and "B1" in message
    assert "稍后" not in message
    for private in ("12.34", "12.35", "SECRET_FORMULA", "PRIVATE_FORMAT", "private-test-signature"):
        assert private not in message + repr(error.diagnostic)


def test_empty_anchor_set_is_not_evidence_of_current_version():
    with pytest.raises(WpsError) as caught:
        load(known={})
    assert caught.value.code == "SNAPSHOT_UNVERIFIED"


def test_hash_mismatch_rejects_download():
    raw = workbook_bytes()
    rpc = Rpc(raw)
    rpc.hash = {"type": "sha1", "sum": "0" * 40}
    with pytest.raises(WpsError) as caught:
        load(raw, rpc=rpc)
    assert caught.value.code == "SNAPSHOT_CHANGED"
    assert caught.value.diagnostic == {"stage": "snapshot", "sheet": "汇总", "field": "hash"}


def test_document_identity_change_is_not_a_retryable_snapshot_delay():
    raw = workbook_bytes()
    rpc = Rpc(raw)
    call_tool = rpc.call_tool

    def renamed_after_download(name, args):
        reply = call_tool(name, args)
        if name == "get_file_info" and len(rpc.calls) == 3:
            reply["data"]["data"]["name"] = "改名后的文档.xlsx"
        return reply

    rpc.call_tool = renamed_after_download
    with pytest.raises(WpsError) as caught:
        load(raw, rpc=rpc)
    assert caught.value.code == "WORKBOOK_MISMATCH"
    assert caught.value.diagnostic == {"stage": "snapshot", "sheet": "汇总", "field": "identity"}


@pytest.mark.parametrize("name", ["文档.ksheet", "文档.xls", "文档.xlsm"])
def test_non_xlsx_download_is_not_attempted(name):
    raw = workbook_bytes()
    rpc = Rpc(raw, name=name)
    with pytest.raises(WpsError) as caught:
        load(raw, rpc=rpc)
    assert caught.value.code == "UNSUPPORTED_CELL_METADATA"
    assert [name for name, args in rpc.calls] == ["get_file_info"]


@pytest.mark.parametrize("limit,value", [("MAX_DOWNLOAD_BYTES", 100), ("MAX_EXPANDED_BYTES", 100),
    ("MAX_ZIP_ENTRIES", 2), ("MAX_XLSX_CELLS", 2)])
def test_download_and_expansion_limits_reject_oversized_files(monkeypatch, limit, value):
    monkeypatch.setattr(snapshot, limit, value)
    with pytest.raises(WpsError) as caught:
        load()
    assert caught.value.code == "SNAPSHOT_TOO_LARGE"


def test_xml_entities_rejected_before_workbook_parse():
    original = workbook_bytes()
    stream = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(original)) as old, zipfile.ZipFile(stream, "w") as new:
        for name in old.namelist():
            body = old.read(name)
            if name == "xl/workbook.xml":
                body = b'<!DOCTYPE workbook [<!ENTITY attack "unsafe">]>' + body
            new.writestr(name, body)
    with pytest.raises(WpsError) as caught:
        load(stream.getvalue())
    assert caught.value.code == "INVALID_SNAPSHOT"


def test_chinese_number_format_color_names_normalize_without_changing_numeric_pattern():
    assert snapshot.normalize_number_format('[红色]0.00;[蓝色]-0.00') == '[Red]0.00;[Blue]-0.00'
    assert snapshot.normalize_number_format('[$￥-804]#,##0.00') == '[$￥-804]#,##0.00'


@pytest.mark.parametrize("live,xlsx", [
    ('G/通用格式"月"', 'General"月"'),
    ('常规"月"', 'General"月"'),
    ('gEnErAl"月"', 'General"月"'),
    ('[红色][>=0]G/通用格式"月";[蓝色]常规"月";gEnErAl;@',
     '[Red][>=0]General"月";[Blue]General"月";General;@'),
    ('[$-804]常规"月"', '[$-804]General"月"'),
    ('G/通用格式_);常规* ', 'General_);General* '),
    ('_ G/通用格式', '_ General'),
])
def test_general_tokens_normalize_with_suffixes_conditions_and_sections(live, xlsx):
    assert snapshot.normalize_number_format(live) == xlsx
    assert snapshot.normalize_number_format(xlsx) == xlsx


@pytest.mark.parametrize("literal", [
    '"G/通用格式"', '"常规"', '"gEnErAl"', '[G/通用格式]', '[常规]',
    r'\G/通用格式', r'\gEnErAl', r'_G/通用格式', r'*常规',
    r'"\"G/通用格式"', 'XG/通用格式', 'G/通用格式后缀',
    '常规后缀', 'SomeGeneral', 'generalText',
])
def test_general_normalization_preserves_literals_directives_consumed_characters_and_words(literal):
    assert snapshot.normalize_number_format(literal) == literal


@pytest.mark.parametrize("different", ['General', 'General"日"', '0"月"', '0.00', 'yyyy-mm-dd', '@',
    '"G/通用格式""月"'])
def test_general_month_alias_does_not_hide_meaningful_format_changes(different):
    assert snapshot.normalize_number_format('G/通用格式"月"') != snapshot.normalize_number_format(different)


def test_month_format_header_and_number_match_live_data_and_fill_blank_metadata():
    book = Workbook()
    sheet = book.active
    sheet.title = "汇总"
    sheet["A1"] = "月份"
    sheet["A2"] = 11
    for coordinate in ("A1", "A2", "A3"):
        sheet[coordinate].number_format = 'General"月"'
    stream = io.BytesIO()
    book.save(stream)
    book.close()
    known = {(row, 1): dict(value=value, formula="", has_formula=False,
                            number_format='G/通用格式"月"', merged=False)
             for row, value in ((1, "月份"), (2, 11))}
    result, rpc, _ = load(stream.getvalue(), missing={(3, 1)}, known=known, expected_version=7)
    assert result == {(3, 1): dict(value="", formula="", has_formula=False,
                                  number_format='General"月"', merged=False, date_system="1900")}
    assert [name for name, args in rpc.calls] == ["get_file_info", "download_file", "get_file_info"]


@pytest.mark.parametrize("epoch,date_system", [(CALENDAR_WINDOWS_1900, "1900"), (CALENDAR_MAC_1904, "1904")])
def test_blank_date_cell_reports_verified_workbook_date_system(epoch, date_system):
    book = Workbook()
    book.epoch = epoch
    sheet = book.active
    sheet.title = "汇总"
    sheet["A1"] = "业务单号"
    sheet["H2"].number_format = "yyyy-mm-dd"
    stream = io.BytesIO()
    book.save(stream)
    book.close()
    result, _, _ = load(stream.getvalue(), missing={(2, 8)}, known={(1, 1): anchors()[(1, 1)]},
                        expected_version=7)
    assert result[(2, 8)] == dict(value="", formula="", has_formula=False,
                                 number_format="yyyy-mm-dd", merged=False, date_system=date_system)


def precise_numeric_workbook(*, epoch=CALENDAR_WINDOWS_1900, number_format="yyyy-mm-dd hh:mm:ss.000", formula=False):
    book = Workbook()
    book.epoch = epoch
    sheet = book.active
    sheet.title = "汇总"
    serial = 46332.57658023727
    sheet["H7"] = "=1+1" if formula else serial
    sheet["H7"].number_format = number_format
    sheet["H8"].number_format = number_format
    stream = io.BytesIO()
    book.save(stream)
    book.close()
    if formula:
        cached = io.BytesIO()
        with zipfile.ZipFile(io.BytesIO(stream.getvalue())) as old, zipfile.ZipFile(cached, "w") as new:
            for entry in old.infolist():
                body = old.read(entry.filename)
                if entry.filename == "xl/worksheets/sheet1.xml":
                    root = ElementTree.fromstring(body)
                    cell = next(item for item in root.iter() if item.get("r") == "H7")
                    next(item for item in cell if item.tag.endswith("}v")).text = repr(serial)
                    body = ElementTree.tostring(root)
                new.writestr(entry, body)
        stream = cached
    known = {(7, 8): dict(value=serial, formula="=1+1" if formula else "", has_formula=formula,
                          number_format=number_format, merged=False)}
    return stream.getvalue(), known


@pytest.mark.parametrize("epoch,date_system", [(CALENDAR_WINDOWS_1900, "1900"), (CALENDAR_MAC_1904, "1904")])
@pytest.mark.parametrize("formula", [False, True])
def test_precise_date_serial_uses_raw_xlsx_number_without_datetime_roundtrip(epoch, date_system, formula):
    raw, known = precise_numeric_workbook(epoch=epoch, formula=formula)
    cached = load_workbook(io.BytesIO(raw), data_only=True)
    assert to_excel(cached["汇总"]["H7"].value, cached.epoch) != known[(7, 8)]["value"]
    cached.close()
    result, _, _ = load(raw, missing={(8, 8)}, known=known)
    assert result[(8, 8)] == dict(value="", formula="", has_formula=False,
        number_format="yyyy-mm-dd hh:mm:ss.000", merged=False, date_system=date_system)


@pytest.mark.parametrize("epoch,date_system", [(CALENDAR_WINDOWS_1900, "1900"), (CALENDAR_MAC_1904, "1904")])
@pytest.mark.parametrize("formula", [False, True])
def test_nonempty_date_metadata_without_any_missing_cells_is_verified(epoch, date_system, formula):
    raw, known = precise_numeric_workbook(epoch=epoch, formula=formula)
    rpc, opener = Rpc(raw), Opener(raw)
    result = snapshot.load_blank_cells(rpc, FILE_ID, "汇总", set(), known,
        metadata_coordinates={(7, 8)}, expected_version=7, opener=opener)
    assert result == {(7, 8): dict(known[(7, 8)], date_system=date_system)}
    assert len(opener.requests) == 1
    assert [name for name, _ in rpc.calls] == ["get_file_info", "download_file", "get_file_info"]


def test_date_and_sparse_blank_metadata_share_one_verified_download():
    raw, known = precise_numeric_workbook(epoch=CALENDAR_MAC_1904)
    result, rpc, opener = load(raw, missing={(8, 8)}, known=known, metadata_coordinates={(7, 8)})
    assert set(result) == {(7, 8), (8, 8)}
    assert all(cell["date_system"] == "1904" for cell in result.values())
    assert result[(7, 8)]["value"] == known[(7, 8)]["value"]
    assert result[(8, 8)]["value"] == "" and len(opener.requests) == 1


def test_requested_metadata_cannot_supply_a_cell_missing_from_live_read():
    raw, known = precise_numeric_workbook()
    rpc, opener = Rpc(raw), Opener(raw)
    with pytest.raises(WpsError) as caught:
        snapshot.load_blank_cells(rpc, FILE_ID, "汇总", set(), known,
            metadata_coordinates={(8, 8)}, opener=opener)
    assert caught.value.code == "INVALID_ARGUMENTS"
    assert not rpc.calls and not opener.requests


@pytest.mark.parametrize("changed", ["one_ulp", "text", "empty"])
def test_requested_nonempty_date_metadata_keeps_strict_live_value_check(changed):
    raw, known = precise_numeric_workbook()
    original_value = known[(7, 8)]["value"]
    known[(7, 8)]["value"] = (math.nextafter(known[(7, 8)]["value"], math.inf) if changed == "one_ulp"
                              else str(known[(7, 8)]["value"]) if changed == "text" else "")
    # A separate anchor is essential for the empty live value case: the
    # requested metadata itself must still be compared, not just the anchors.
    book = Workbook()
    book.active.title = "汇总"
    book["汇总"]["A1"] = "业务单号"
    book["汇总"]["H7"] = original_value
    book["汇总"]["H7"].number_format = known[(7, 8)]["number_format"]
    stream = io.BytesIO()
    book.save(stream)
    book.close()
    raw = stream.getvalue()
    known[(1, 1)] = anchors()[(1, 1)]
    with pytest.raises(WpsError) as caught:
        snapshot.load_blank_cells(Rpc(raw), FILE_ID, "汇总", set(), known,
            metadata_coordinates={(7, 8)}, opener=Opener(raw))
    assert caught.value.code == ("VALUE_MISMATCH" if changed == "one_ulp" else "TYPE_MISMATCH")
    assert caught.value.diagnostic["row"] == 7 and caught.value.diagnostic["column"] == 8


def test_nonempty_date_epoch_is_not_accepted_if_export_version_changes():
    raw, known = precise_numeric_workbook()
    with pytest.raises(WpsError) as caught:
        snapshot.load_blank_cells(Rpc(raw, versions=(7, 8)), FILE_ID, "汇总", set(), known,
            metadata_coordinates={(7, 8)}, expected_version=7, opener=Opener(raw))
    assert caught.value.code == "SNAPSHOT_CHANGED"


@pytest.mark.parametrize("number_format", ["yyyy-mm-dd hh:mm:ss.000", "0.00", "General"])
def test_raw_numeric_check_still_rejects_one_ulp_date_amount_or_key_difference(number_format):
    raw, known = precise_numeric_workbook(number_format=number_format)
    known[(7, 8)]["value"] = math.nextafter(known[(7, 8)]["value"], math.inf)
    with pytest.raises(WpsError) as caught:
        load(raw, missing={(8, 8)}, known=known)
    assert caught.value.code == "VALUE_MISMATCH"
    assert caught.value.diagnostic["row"] == 7 and caught.value.diagnostic["column"] == 8


def test_raw_numeric_check_does_not_coerce_text_key_to_number():
    raw, known = precise_numeric_workbook(number_format="General")
    known[(7, 8)]["value"] = str(known[(7, 8)]["value"])
    with pytest.raises(WpsError) as caught:
        load(raw, missing={(8, 8)}, known=known)
    assert caught.value.code == "TYPE_MISMATCH"


@pytest.mark.parametrize("fault", ["nan", "1e309", "1_000", "duplicate_value", "duplicate_cell"])
def test_ambiguous_or_invalid_raw_numeric_cells_are_rejected(fault):
    raw, known = precise_numeric_workbook()
    stream = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(raw)) as old, zipfile.ZipFile(stream, "w") as new:
        for entry in old.infolist():
            body = old.read(entry.filename)
            if entry.filename == "xl/worksheets/sheet1.xml":
                root = ElementTree.fromstring(body)
                row = next(item for item in root.iter() if item.tag.endswith("}row") and item.get("r") == "7")
                cell = next(item for item in row if item.get("r") == "H7")
                value = next(item for item in cell if item.tag.endswith("}v"))
                if fault == "duplicate_value":
                    cell.append(ElementTree.fromstring(ElementTree.tostring(value)))
                elif fault == "duplicate_cell":
                    row.append(ElementTree.fromstring(ElementTree.tostring(cell)))
                else:
                    value.text = fault
                body = ElementTree.tostring(root)
            new.writestr(entry, body)
    with pytest.raises(WpsError) as caught:
        load(stream.getvalue(), missing={(8, 8)}, known=known)
    assert caught.value.code == "INVALID_SNAPSHOT"


def test_foreign_namespace_cells_cannot_supply_verified_numeric_values():
    raw, known = precise_numeric_workbook()
    stream = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(raw)) as old, zipfile.ZipFile(stream, "w") as new:
        for entry in old.infolist():
            body = old.read(entry.filename)
            if entry.filename == "xl/worksheets/sheet1.xml":
                root = ElementTree.fromstring(body)
                fake = ElementTree.SubElement(root, "{urn:untrusted}c", r="H7")
                ElementTree.SubElement(fake, "{urn:untrusted}v").text = "1"
                body = ElementTree.tostring(root)
            new.writestr(entry, body)
    result, _, _ = load(stream.getvalue(), missing={(8, 8)}, known=known)
    assert result[(8, 8)]["value"] == "" and result[(8, 8)]["date_system"] == "1900"


def test_color_normalization_does_not_change_literal_or_escaped_color_text():
    for literal in ('0 "[红色]"', r'0 \[红色]', r'0 _[红色]', r'0 "转义\"[红色]"'):
        assert snapshot.normalize_number_format(literal) == literal
    assert snapshot.normalize_number_format('0 "[红色]"') != snapshot.normalize_number_format('0 "[Red]"')


def test_optional_parenthesis_escapes_match_real_wps_format_but_quoted_literals_stay_intact():
    live = '#,##0.00;[Red](#,##0.00);"—"'
    xlsx = r'#,##0.00;[Red]\(#,##0.00\);"—"'
    assert snapshot.normalize_number_format(xlsx) == snapshot.normalize_number_format(live)
    literal = r'0.00"\(文字\)";0.00"引号\"\(文字\)"'
    assert snapshot.normalize_number_format(literal) == literal
    escaped_backslash = r'0.00\\(0.00\\)'
    assert snapshot.normalize_number_format(escaped_backslash) == escaped_backslash
    spacing = r'0.00_\(0.00*\)'
    assert snapshot.normalize_number_format(spacing) == spacing


def test_optional_minus_escapes_match_real_wps_dates_and_accounting_formats():
    assert snapshot.normalize_number_format(r'yyyy\-mm\-dd') == 'yyyy-mm-dd'
    xlsx = r'_ * #,##0.00_ ;_ * \-#,##0.00_ ;_ * "-"??_ ;_ @_ '
    live = '_ * #,##0.00_ ;_ * -#,##0.00_ ;_ * "-"??_ ;_ @_ '
    assert snapshot.normalize_number_format(xlsx) == snapshot.normalize_number_format(live)
    for special in (r'0.00E\-00', r'0.00e\-00', r'[$USD\-409]0.00', r'0.00"\-"', r'0.00_\-', r'0.00*\-'):
        assert snapshot.normalize_number_format(special) == special
    assert snapshot.normalize_number_format(r'0.00E\-00') != snapshot.normalize_number_format('0.00E-00')


def test_full_20000_blank_cell_request_accepts_one_outside_anchor():
    known = {(1, 1): anchors()[(1, 1)]}
    result, _, _ = load(missing={(row, 8) for row in range(1, 20001)}, known=known)
    assert len(result) == 20000
    with pytest.raises(WpsError) as caught:
        load(missing={(row, 8) for row in range(1, 20002)}, known=known)
    assert caught.value.code == "INVALID_ARGUMENTS"


@pytest.mark.parametrize("server_code,error_code", [(401, "AUTH_REQUIRED"), ("400006", "AUTH_REQUIRED"),
    (403, "ACCESS_DENIED"), ("403001", "ACCESS_DENIED"), (429, "RATE_LIMITED"), ("429", "RATE_LIMITED")])
def test_nested_account_access_and_rate_errors_are_distinguished_and_sanitized(server_code, error_code):
    with pytest.raises(WpsError) as caught:
        snapshot._data({"code": 0, "data": {"code": server_code, "msg": URL}})
    assert caught.value.code == error_code
    assert "private-test-signature" not in str(caught.value)
    assert not caught.value.mutation_attempted
