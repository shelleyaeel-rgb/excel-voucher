"""Bounded, credential-free XLSX download and live-data consistency checks."""
import hashlib
import io
import zipfile

import pytest
from openpyxl import Workbook

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


@pytest.mark.parametrize("versions,expected", [((8, 8), 7), ((7, 8), 7)])
def test_file_version_changes_reject_snapshot(versions, expected):
    raw = workbook_bytes()
    with pytest.raises(WpsError) as caught:
        load(raw, rpc=Rpc(raw, versions=versions), expected_version=expected)
    assert caught.value.code == "SNAPSHOT_CHANGED"


def test_snapshot_cannot_supply_missing_data_or_hide_a_formula():
    for coordinates in ({(1, 2)}, {(4, 2)}):
        with pytest.raises(WpsError) as caught:
            load(missing=coordinates)
        assert caught.value.code == "SNAPSHOT_CHANGED"


@pytest.mark.parametrize("field,value", [("value", "12.34"), ("value", 12.35),
    ("formula", "=1"), ("number_format", "@"), ("merged", True)])
def test_live_nonempty_cell_must_match_value_type_formula_format_and_merge(field, value):
    known = anchors()
    known[(1, 2)][field] = value
    with pytest.raises(WpsError) as caught:
        load(known=known)
    assert caught.value.code == "SNAPSHOT_CHANGED"


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
