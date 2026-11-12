import json

import pytest

from xiaoyu import wps_diagnostics as diagnostics
from xiaoyu.wps_service import WpsError


def test_failure_record_keeps_location_without_payload_or_credentials(tmp_path):
    path = tmp_path / "wps_diagnostics.json"
    location = {"stage": "snapshot", "field": "number_format", "sheet": "私户", "row": 11, "column": 2}
    error = WpsError("secret-message", code="FORMAT_MISMATCH", diagnostic={**location,
        "value": "secret-value", "formula": "=secret-formula", "token": "secret-token",
        "number_format": "secret-format", "url": "https://secret-url"})
    identifier = diagnostics.record_failure(path, action="wps_read_table", code=error.code,
        diagnostic=error.diagnostic, document_url="https://www.kdocs.cn/l/secret-document")
    raw = path.read_text(encoding="utf8")
    record = json.loads(raw)["records"][0]
    assert record["id"] == identifier and record["diagnostic"] == location
    assert len(record["document_ref"]) == 16 and not record["mutation_attempted"]
    assert "secret" not in raw and "https://" not in raw


def test_history_is_bounded_and_retains_latest_failure(tmp_path, monkeypatch):
    monkeypatch.setattr(diagnostics, "MAX_RECORDS", 3)
    path = tmp_path / "wps_diagnostics.json"
    identifiers = [diagnostics.record_failure(path, action="wps_apply_update", code="NETWORK",
        mutation_attempted=True) for _ in range(6)]
    records = json.loads(path.read_text(encoding="utf8"))["records"]
    assert [record["id"] for record in records] == identifiers[-3:]
    assert all(record["mutation_attempted"] for record in records)


@pytest.mark.parametrize("original", ["broken", '{"schema":2,"records":[]}', '{"schema":1,"records":{}}'])
def test_unreadable_existing_log_is_preserved(tmp_path, original):
    path = tmp_path / "wps_diagnostics.json"
    path.write_text(original, encoding="utf8")
    assert diagnostics.record_failure(path, action="wps_read_table", code="NETWORK") is None
    assert path.read_text(encoding="utf8") == original


def test_logging_io_failure_is_optional(tmp_path, monkeypatch):
    def unavailable(*args, **kwargs):
        raise OSError("disk unavailable")
    monkeypatch.setattr(diagnostics, "_atomic_write", unavailable)
    assert diagnostics.record_failure(tmp_path / "log.json", action="wps_read_table", code="NETWORK") is None


def test_invalid_diagnostic_fields_are_not_serialized(tmp_path):
    path = tmp_path / "log.json"
    diagnostics.record_failure(path, action="wps_read_table", code="secret-url", diagnostic={
        "stage": [], "field": {}, "row": True, "column": 999999, "sheet": "bad\nsheet"})
    record = json.loads(path.read_text(encoding="utf8"))["records"][0]
    assert record["diagnostic"] == {} and record["code"] == "WPS_ERROR"
    assert diagnostics.record_failure(path, action=[], code="NETWORK") is None
