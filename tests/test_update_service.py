import base64
import io
import hashlib
import json
from pathlib import Path
import threading
from urllib.request import Request

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from xiaoyu.update_protocol import (MAX_MANIFEST_BYTES, canonical_json, load_channel,
                                    parse_version, validate_channel, verify_envelope)
from xiaoyu.update_service import HTTPSRedirectHandler, UpdateCancelled, UpdateService
from xiaoyu import update_service


@pytest.fixture
def signed():
    key = Ed25519PrivateKey.generate()
    public = base64.b64encode(key.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw)).decode()
    archive = b"new portable package"
    payload = {"schema": 1, "app": "xiaoyu-voucher", "version": "0.4.0", "data_schema": 1,
               "min_updater": 1, "url": "https://downloads.example.com/0.4.0/package.zip",
               "size": len(archive), "sha256": hashlib.sha256(archive).hexdigest(),
               "notes": "支持自动更新", "published_at": "2026-11-03T00:00:00+00:00"}
    channel = {"manifest_url": "https://downloads.example.com/latest.json", "public_key": public}

    def sign(value=None):
        value = dict(payload if value is None else value)
        return canonical_json({"payload": value, "signature": base64.b64encode(
            key.sign(canonical_json(value))).decode()})
    return key, channel, payload, archive, sign


class Response(io.BytesIO):
    def __init__(self, data, url, headers=None):
        super().__init__(data)
        self.url = url
        self.headers = headers or {}

    def geturl(self):
        return self.url


class Opener:
    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []

    def open(self, request, timeout):
        self.requests.append((request, timeout))
        value = self.responses.pop(0)
        if isinstance(value, Exception):
            raise value
        return value


def service_for(tmp_path, signed, *, archive=None, manifest=None, current="0.3.0", headers=None):
    _, channel, payload, default_archive, sign = signed
    service = UpdateService(channel, current, tmp_path / "updates")
    service.opener = Opener([Response(manifest if manifest is not None else sign(), channel["manifest_url"]),
        Response(default_archive if archive is None else archive, payload["url"], headers)])
    return service


def test_valid_signature_and_new_package_cached_without_second_download(tmp_path, signed):
    service = service_for(tmp_path, signed)
    updates = []
    candidate = service.check_and_download(threading.Event(), lambda *args: updates.append(args))
    assert (candidate / "❤.zip").read_bytes() == signed[3]
    assert verify_envelope((candidate / "release.json").read_bytes(), signed[1]["public_key"])["version"] == "0.4.0"
    assert updates[-1][1] == 100
    service.opener = Opener([Response(signed[4](), signed[1]["manifest_url"])])
    assert service.check_and_download(threading.Event(), lambda *_: None) == candidate
    assert len(service.opener.requests) == 1
    assert not list(candidate.glob("*.part"))


@pytest.mark.parametrize("current", ["0.4.0", "0.9.0", "1.0.0"])
def test_old_or_current_release_never_downloaded(tmp_path, signed, current):
    service = service_for(tmp_path, signed, current=current)
    assert service.check_and_download(threading.Event(), lambda *_: None) is None
    assert len(service.opener.requests) == 1
    assert not (tmp_path / "updates").exists()


@pytest.mark.parametrize("changes", [{"size": True}, {"size": 2 ** 40}, {"url": "http://bad/zip"},
    {"url": "https://username:secret@host/zip"}, {"data_schema": 2}, {"min_updater": 2},
    {"version": "0.04.0"}, {"version": "0.4.0-beta"}, {"notes": "a" * 8001},
    {"published_at": "2026-11-03"}, {"sha256": "a" * 63}, {"app": "another-app"}])
def test_signed_but_invalid_release_is_rejected(signed, changes):
    payload = dict(signed[2], **changes)
    with pytest.raises(ValueError):
        verify_envelope(signed[4](payload), signed[1]["public_key"])


def test_tampering_and_unknown_signer_are_rejected(tmp_path, signed):
    envelope = json.loads(signed[4]())
    envelope["payload"]["url"] = "https://wrong.example.com/bad.zip"
    service = service_for(tmp_path, signed, manifest=canonical_json(envelope))
    with pytest.raises(ValueError, match="签名"):
        service.check_and_download(threading.Event(), lambda *_: None)
    assert len(service.opener.requests) == 1
    wrong_key = base64.b64encode(Ed25519PrivateKey.generate().public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw)).decode()
    with pytest.raises(ValueError, match="签名"):
        verify_envelope(signed[4](), wrong_key)


@pytest.mark.parametrize("body", [b"truncated", b"new portable packagE", b"new portable package plus unexpected bytes"])
def test_bad_download_does_not_become_ready(tmp_path, signed, body):
    service = service_for(tmp_path, signed, archive=body)
    with pytest.raises(ValueError):
        service.check_and_download(threading.Event(), lambda *_: None)
    assert not list((tmp_path / "updates").rglob("❤.zip"))
    assert not list((tmp_path / "updates").rglob("release.json"))
    assert not list((tmp_path / "updates").rglob("*.part"))


def test_declared_size_mismatch_is_rejected(tmp_path, signed):
    service = service_for(tmp_path, signed, headers={"Content-Length": "999999999"})
    with pytest.raises(ValueError, match="大小"):
        service.check_and_download(threading.Event(), lambda *_: None)


def test_cancellation_during_download_removes_partial_file(tmp_path, signed):
    service = service_for(tmp_path, signed)
    cancel = threading.Event()

    def progress(text, percent):
        if "正在下载" in text:
            cancel.set()
    with pytest.raises(UpdateCancelled):
        service.check_and_download(cancel, progress)
    assert not list((tmp_path / "updates").rglob("*.part"))
    assert not list((tmp_path / "updates").rglob("❤.zip"))


def test_cancel_before_check_never_connects(tmp_path, signed):
    service = service_for(tmp_path, signed)
    cancel = threading.Event()
    cancel.set()
    with pytest.raises(UpdateCancelled):
        service.check_and_download(cancel, lambda *_: None)
    assert not service.opener.requests


def test_oversized_or_ambiguous_manifest_rejected(signed):
    with pytest.raises(ValueError):
        verify_envelope(b"x" * (MAX_MANIFEST_BYTES + 1), signed[1]["public_key"])
    with pytest.raises(ValueError, match="重复"):
        verify_envelope(b'{"payload":{},"payload":{},"signature":"x"}', signed[1]["public_key"])


def test_redirect_must_remain_https():
    handler = HTTPSRedirectHandler()
    with pytest.raises(ValueError, match="HTTPS"):
        handler.redirect_request(Request("https://example.com"), None, 302, "Found", {}, "http://example.com/bad")
    assert handler.redirect_request(Request("https://example.com"), None, 302, "Found", {},
                                    "https://cdn.example.com/package.zip").full_url.startswith("https:")


def test_missing_channel_disables_updates(tmp_path):
    assert load_channel(tmp_path / "missing.json") is None


def test_config_cannot_include_publishing_credentials(signed):
    with pytest.raises(ValueError):
        validate_channel(dict(signed[1], token="must-not-be-bundled"))


def test_numeric_versions_are_not_compared_as_text():
    assert parse_version("0.10.0") > parse_version("0.9.0")


def test_manifest_deadline_and_network_error_leave_installed_files_untouched(tmp_path, signed, monkeypatch):
    installed = tmp_path / "❤.exe"
    installed.write_bytes(b"usable old program")
    service = service_for(tmp_path, signed)
    ticks = iter((0, 31))
    monkeypatch.setattr(update_service.time, "monotonic", lambda: next(ticks))
    with pytest.raises(TimeoutError, match="检查更新超时"):
        service.check_and_download(threading.Event(), lambda *_: None)
    assert installed.read_bytes() == b"usable old program"
    assert not (tmp_path / "updates").exists()


def test_streamed_oversized_manifest_is_bounded(tmp_path, signed):
    service = service_for(tmp_path, signed, manifest=b" " * (MAX_MANIFEST_BYTES + 100))
    with pytest.raises(ValueError, match="大小"):
        service.check_and_download(threading.Event(), lambda *_: None)
    assert len(service.opener.requests) == 1
