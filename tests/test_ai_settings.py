import json
import os
from concurrent.futures import ThreadPoolExecutor

import pytest

from xiaoyu import ai_settings
from xiaoyu.ai_settings import AISettings, UsageLedger, load_settings, save_settings, validate_settings


def test_missing_settings_no_write(tmp_path):
    target = tmp_path / "settings.json"
    assert load_settings(target) == AISettings()
    assert not target.exists()


@pytest.mark.skipif(os.name != "nt", reason="Windows DPAPI")
def test_dpapi_roundtrip_never_saves_plaintext(tmp_path):
    target = tmp_path / "settings.json"
    settings = AISettings(api_key="sk-test-secret-for-isolated-tests", proxy_mode="custom",
                          proxy_url="http://127.0.0.1:7890", monthly_limit_usd=35.0)
    save_settings(settings, target)
    assert settings.api_key not in target.read_text(encoding="utf-8")
    assert settings.api_key not in repr(settings)
    assert load_settings(target) == settings
    save_settings(AISettings(), target)
    assert not load_settings(target).api_key


def test_other_platform_does_not_save_plaintext(tmp_path, monkeypatch):
    target = tmp_path / "settings.json"
    monkeypatch.setattr(ai_settings.os, "name", "posix")
    with pytest.raises(ValueError, match="Windows"):
        save_settings(AISettings(api_key="sk-test"), target)
    assert not target.exists()


@pytest.mark.parametrize("settings", [
    AISettings(api_key=" "), AISettings(api_key="sk-test\n"), AISettings(api_key="sk test"),
    AISettings(proxy_mode="other"), AISettings(proxy_mode="custom"),
    AISettings(proxy_url="socks5://127.0.0.1:7890"), AISettings(proxy_url="http://user:pass@host"),
    AISettings(proxy_url="http://host/path"), AISettings(proxy_url="http://host:0"),
    AISettings(proxy_url="http://host:65536"), AISettings(proxy_url="http://host/?a=1"),
    AISettings(monthly_limit_usd=float("nan")), AISettings(monthly_limit_usd=0),
    AISettings(monthly_limit_usd=True), AISettings(max_output_tokens=999),
    AISettings(max_output_tokens=16001), AISettings(max_output_tokens=1000.5),
])
def test_invalid_settings(settings):
    with pytest.raises(ValueError):
        validate_settings(settings)


def test_failed_replace_preserves_previous_file_and_cleans_temp(tmp_path, monkeypatch):
    target = tmp_path / "settings.json"
    save_settings(AISettings(monthly_limit_usd=10), target)
    original = target.read_bytes()

    def fail(*args):
        raise OSError("test failure")

    monkeypatch.setattr(ai_settings.os, "replace", fail)
    with pytest.raises(ValueError, match="无法保存"):
        save_settings(AISettings(monthly_limit_usd=30), target)
    assert target.read_bytes() == original
    assert list(tmp_path.iterdir()) == [target]


@pytest.mark.parametrize("content", ["{", "[]", '{"version":2}',
                                     '{"version":1,"api_key":"not-allowed"}',
                                     '{"version":1,"api_key_protected":"bad-base64"}'])
def test_corrupt_settings_fail_without_overwrite(tmp_path, content):
    target = tmp_path / "settings.json"
    target.write_text(content, encoding="utf-8")
    with pytest.raises(ValueError):
        load_settings(target)
    assert target.read_text(encoding="utf-8") == content


def test_usage_rates_budget_and_readback(tmp_path, monkeypatch):
    monkeypatch.setattr(ai_settings, "_current_month", lambda: "2026-10")
    path = tmp_path / "usage.json"
    ledger = UsageLedger(path)
    assert ledger.summary()["requests"] == 0
    assert not path.exists()
    total = ledger.record(1_000_000, 2_000_000, cached_input_tokens=200_000,
                          web_search_calls=2, container_sessions=3)
    assert total["estimated_usd"] == pytest.approx(1.192)
    assert total["month"] == "2026-10"
    assert total["requests"] == 1
    assert total["cached_input_tokens"] == 200_000
    assert UsageLedger(path).summary() == total
    with pytest.raises(ValueError, match="已达到预算"):
        ledger.check_budget(1)
    ledger.check_budget(2)
    assert ledger.record(0, 0, uncertain=True)["uncertain_requests"] == 1


def test_usage_month_rollover_retains_history(tmp_path, monkeypatch):
    ledger = UsageLedger(tmp_path / "usage.json")
    monkeypatch.setattr(ai_settings, "_current_month", lambda: "2026-10")
    ledger.record(1000, 2000)
    monkeypatch.setattr(ai_settings, "_current_month", lambda: "2026-11")
    assert ledger.summary()["requests"] == 0
    ledger.record(2000, 3000)
    assert ledger.summary()["requests"] == 1
    monkeypatch.setattr(ai_settings, "_current_month", lambda: "2026-10")
    assert ledger.summary()["input_tokens"] == 1000


def test_usage_concurrent_instances_do_not_lose_totals(tmp_path):
    path = tmp_path / "usage.json"
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(lambda _: UsageLedger(path).record(1000, 1000), range(20)))
    total = UsageLedger(path).summary()
    assert total["requests"] == 20
    assert total["input_tokens"] == 20_000
    assert total["estimated_usd"] == pytest.approx(0.012)


@pytest.mark.parametrize("kwargs", [dict(input_tokens=-1, output_tokens=0),
                                      dict(input_tokens=1, output_tokens=1.2),
                                      dict(input_tokens=1, output_tokens=0, cached_input_tokens=2),
                                      dict(input_tokens=1, output_tokens=0, web_search_calls=True)])
def test_usage_rejects_invalid_counters(tmp_path, kwargs):
    ledger = UsageLedger(tmp_path / "usage.json")
    with pytest.raises(ValueError):
        ledger.record(**kwargs)
    assert not ledger.path.exists()


def test_corrupt_ledger_cannot_reset_or_bypass_budget(tmp_path):
    path = tmp_path / "usage.json"
    path.write_text(json.dumps({"version": 1, "months": {"2026-10": {"month": "2026-10"}}}),
                    encoding="utf-8")
    original = path.read_bytes()
    with pytest.raises(ValueError):
        UsageLedger(path).check_budget(20)
    with pytest.raises(ValueError):
        UsageLedger(path).record(10, 10)
    assert path.read_bytes() == original
