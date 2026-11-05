import base64
import hashlib
import io
import json
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import quote
import zipfile

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from scripts import publish_update, update_keys
from xiaoyu.update_protocol import canonical_json, verify_envelope


@pytest.fixture
def package(tmp_path):
    key = Ed25519PrivateKey.generate()
    private = tmp_path / "private.pem"
    private.write_bytes(key.private_bytes(serialization.Encoding.PEM,
                       serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    channel = {"manifest_url": "https://github.com/example/xiaoyu/releases/latest/download/latest.json",
               "public_key": update_keys.public_key_text(key)}
    files = {"❤.exe": b"test exe", "_internal/update-helper.exe": b"helper",
             "_internal/assets/update-channel.json": canonical_json(channel)}
    metadata = {"schema": 1, "app": "xiaoyu-voucher", "version": "0.4.0", "data_schema": 1,
                "files": {name: hashlib.sha256(data).hexdigest() for name, data in files.items()}}
    archive = tmp_path / "❤.zip"
    with zipfile.ZipFile(archive, "w") as bundle:
        for name, data in files.items():
            bundle.writestr("❤/" + name, data)
        bundle.writestr("❤/release-files.json", canonical_json(metadata))
    return archive, private, key, channel


def verifier(path):
    return {"ok": True, "version": "0.4.0"}


class FakeGitHub:
    def __init__(self, *, corrupt=None, duplicate=False, fail_upload=False,
                 draft_slug="v0.4.0"):
        self.calls = []
        self.assets = []
        self.corrupt = corrupt
        self.duplicate = duplicate
        self.fail_upload = fail_upload
        self.manifest = None
        self.draft_slug = draft_slug
        self.public_slug = "v0.4.0"
        self.draft = {"id": 123, "draft": True, "prerelease": False, "tag_name": "v0.4.0",
                      "url": "https://api.github.com/repos/example/xiaoyu/releases/123",
                      "upload_url": "https://uploads.github.com/repos/example/xiaoyu/releases/123/assets{?name,label}"}

    def request(self, method, url, *, data=None, file=None, content_type=None):
        self.calls.append((method, url, data))
        if method == "GET" and url.endswith("/example/xiaoyu"):
            return {"private": False, "archived": False}
        if method == "GET" and "/releases?" in url:
            return [self.draft] if self.duplicate else []
        if method == "GET" and url.endswith("/releases/123"):
            return self.draft
        if method == "GET" and url.endswith("/releases/latest"):
            raise HTTPError(url, 404, "Not Found", {}, None)
        if method == "POST" and url.endswith("/releases"):
            assert data["draft"] is True
            return self.draft
        if method == "POST" and "/assets?" in url:
            if self.fail_upload:
                raise OSError("upload interrupted")
            raw = Path(file).read_bytes()
            name = "latest.json" if Path(file).name == "latest.json" else "_.zip"
            if name == "latest.json":
                self.manifest = raw
            asset = {"id": len(self.assets) + 1, "name": name, "state": "uploaded", "size": len(raw),
                     "digest": "sha256:" + hashlib.sha256(raw).hexdigest(),
                     "browser_download_url": "https://github.com/example/xiaoyu/releases/download/" + self.draft_slug + "/" + quote(name, safe="")}
            if self.corrupt == name:
                asset["digest"] = "sha256:" + "0" * 64
            self.assets.append(asset)
            return asset
        if method == "GET" and "/assets?" in url:
            return self.assets
        if method == "PATCH":
            assert data == {"draft": False, "prerelease": False, "make_latest": "true"}
            for asset in self.assets:
                asset["browser_download_url"] = ("https://github.com/example/xiaoyu/releases/download/"
                                                  + self.public_slug + "/" + quote(asset["name"], safe=""))
            return {"draft": False, "tag_name": "v0.4.0",
                    "html_url": "https://github.com/example/xiaoyu/releases/tag/v0.4.0"}
        raise AssertionError((method, url))


def test_publish_github_checks_package_then_signs_actual_asset_url(package, monkeypatch, tmp_path):
    archive, private, _, channel = package
    monkeypatch.setattr(publish_update, "ROOT", tmp_path)
    client = FakeGitHub()
    result = publish_update.publish_github(archive, "example/xiaoyu", private, "支持更新", client=client, verifier=verifier)
    assert result.endswith("/tag/v0.4.0")
    payload = verify_envelope(client.manifest, channel["public_key"])
    assert payload["url"].endswith("/v0.4.0/_.zip")
    assert payload["sha256"] == hashlib.sha256(archive.read_bytes()).hexdigest()
    assert client.calls[-2][0] == "PATCH"
    assert client.calls[-1][0] == "GET"


@pytest.mark.parametrize("corrupt", ["_.zip", "latest.json"])
def test_upload_checksum_failure_keeps_existing_latest(package, monkeypatch, tmp_path, corrupt):
    monkeypatch.setattr(publish_update, "ROOT", tmp_path)
    client = FakeGitHub(corrupt=corrupt)
    with pytest.raises(ValueError, match="SHA-256"):
        publish_update.publish_github(package[0], "example/xiaoyu", package[1], "notes", client=client, verifier=verifier)
    assert not any(method == "PATCH" for method, _, _ in client.calls)


def test_network_failure_never_publishes_draft(package):
    client = FakeGitHub(fail_upload=True)
    with pytest.raises(OSError):
        publish_update.publish_github(package[0], "example/xiaoyu", package[1], "notes", client=client, verifier=verifier)
    assert not any(method == "PATCH" for method, _, _ in client.calls)


def test_existing_version_is_not_overwritten(package):
    client = FakeGitHub(duplicate=True)
    with pytest.raises(ValueError, match="已经存在"):
        publish_update.publish_github(package[0], "example/xiaoyu", package[1], "notes", client=client, verifier=verifier)
    assert not any(method == "POST" for method, _, _ in client.calls)


def test_new_tag_draft_temporary_url_is_never_signed(package, monkeypatch, tmp_path):
    monkeypatch.setattr(publish_update, "ROOT", tmp_path)
    client = FakeGitHub(draft_slug="untagged-1dac0614b8e3d4f4179f")
    publish_update.publish_github(package[0], "example/xiaoyu", package[1], "notes", client=client, verifier=verifier)
    payload = verify_envelope(client.manifest, package[3]["public_key"])
    assert payload["url"] == "https://github.com/example/xiaoyu/releases/download/v0.4.0/_.zip"
    assert b"untagged" not in client.manifest


def existing_draft(package):
    client = FakeGitHub(duplicate=True, draft_slug="untagged-1dac0614b8e3d4f4179f")
    client.assets = [{"id": 1, "name": "default.zip", "state": "uploaded",
        "size": package[0].stat().st_size, "digest": "sha256:" + hashlib.sha256(package[0].read_bytes()).hexdigest(),
        "browser_download_url": "https://github.com/example/xiaoyu/releases/download/untagged-1dac0614b8e3d4f4179f/default.zip"}]
    return client


def test_resume_reuses_valid_package_and_only_uploads_manifest(package, monkeypatch, tmp_path):
    monkeypatch.setattr(publish_update, "ROOT", tmp_path)
    client = existing_draft(package)
    result = publish_update.publish_github(package[0], "example/xiaoyu", package[1], "notes",
        client=client, verifier=verifier, resume_draft=123)
    assert result.endswith("/tag/v0.4.0")
    uploads = [(method, url) for method, url, _ in client.calls if method == "POST"]
    assert len(uploads) == 1 and uploads[0][1].endswith("?name=latest.json")
    payload = verify_envelope(client.manifest, package[3]["public_key"])
    assert payload["url"].endswith("/v0.4.0/default.zip")


@pytest.mark.parametrize("change", [{"tag_name": "v0.5.0"}, {"draft": False}, {"prerelease": True},
    {"url": "https://api.github.com/repos/another/repo/releases/123"}, {"id": 999}])
def test_resume_rejects_other_release_identity_or_state(package, change):
    client = existing_draft(package)
    client.draft.update(change)
    with pytest.raises(ValueError):
        publish_update.publish_github(package[0], "example/xiaoyu", package[1], "notes",
            client=client, verifier=verifier, resume_draft=123)
    assert not any(method in {"POST", "PATCH"} for method, _, _ in client.calls)


@pytest.mark.parametrize("changes", [{"size": 1}, {"digest": "sha256:" + "0" * 64},
    {"browser_download_url": "https://github.com/another/repo/releases/download/untagged-abcdef/default.zip"},
    {"browser_download_url": "https://github.com/example/xiaoyu/releases/download/v0.3.0/default.zip"},
    {"browser_download_url": "https://github.com/example/xiaoyu/releases/download/untagged-abcdef/different.zip"},
    {"name": "../default.zip"}])
def test_resume_rejects_wrong_package_or_download_address(package, changes):
    client = existing_draft(package)
    client.assets[0].update(changes)
    with pytest.raises(ValueError):
        publish_update.publish_github(package[0], "example/xiaoyu", package[1], "notes",
            client=client, verifier=verifier, resume_draft=123)
    assert not any(method in {"POST", "PATCH"} for method, _, _ in client.calls)


@pytest.mark.parametrize("name", ["latest.json", "unexpected.txt"])
def test_resume_never_overwrites_existing_manifest_or_unknown_attachment(package, name):
    client = existing_draft(package)
    client.assets.append({"id": 2, "name": name})
    with pytest.raises(ValueError, match="不会覆盖"):
        publish_update.publish_github(package[0], "example/xiaoyu", package[1], "notes",
            client=client, verifier=verifier, resume_draft=123)
    assert not any(method in {"POST", "PATCH"} for method, _, _ in client.calls)


def test_public_url_is_rechecked_after_draft_publication(package, monkeypatch, tmp_path):
    monkeypatch.setattr(publish_update, "ROOT", tmp_path)
    client = FakeGitHub(draft_slug="untagged-abcdef")
    client.public_slug = "v0.5.0"
    with pytest.raises(ValueError, match="本次版本"):
        publish_update.publish_github(package[0], "example/xiaoyu", package[1], "notes", client=client, verifier=verifier)
    assert any(method == "PATCH" for method, _, _ in client.calls)


def test_final_asset_name_is_url_encoded_without_using_temporary_slug():
    name = "测试 file.zip"
    asset = {"name": name, "browser_download_url":
        "https://github.com/example/xiaoyu/releases/download/untagged-abcdef/" + quote(name, safe="")}
    assert publish_update._final_asset_url(asset, "example/xiaoyu", "v0.4.0", draft=True) == (
        "https://github.com/example/xiaoyu/releases/download/v0.4.0/" + quote(name, safe=""))


def test_self_test_version_mismatch_stops_before_upload(package):
    client = FakeGitHub()
    with pytest.raises(ValueError, match="版本"):
        publish_update.publish_github(package[0], "example/xiaoyu", package[1], "notes", client=client,
                                     verifier=lambda _: {"ok": True, "version": "0.3.0"})
    assert not client.calls


def test_wrong_signing_key_stops_before_upload(package, tmp_path):
    wrong_key = tmp_path / "wrong.pem"
    wrong_key.write_bytes(Ed25519PrivateKey.generate().private_bytes(serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    client = FakeGitHub()
    with pytest.raises(ValueError, match="公钥"):
        publish_update.publish_github(package[0], "example/xiaoyu", wrong_key, "notes", client=client, verifier=verifier)
    assert not client.calls


def test_channel_configuration_reuses_existing_private_key(tmp_path):
    private = tmp_path / "private.pem"
    channel = tmp_path / "channel.json"
    url = "https://github.com/example/xiaoyu/releases/latest/download/latest.json"
    first = update_keys.configure_channel(private, url, channel, generate=True)
    original = private.read_bytes()
    assert update_keys.configure_channel(private, url, channel, generate=True) == first
    assert private.read_bytes() == original
    assert "PRIVATE" not in channel.read_text()
    with pytest.raises(ValueError, match="不同"):
        update_keys.configure_channel(private, "https://example.com/latest.json", channel)
    assert private.read_bytes() == original


def test_private_key_inside_checkout_rejected(tmp_path, monkeypatch):
    monkeypatch.setattr(update_keys, "ROOT", tmp_path)
    with pytest.raises(ValueError, match="之外"):
        update_keys.configure_channel(tmp_path / "private.pem", "https://example.com/latest.json",
                                      tmp_path / "channel.json", generate=True)
    assert not list(tmp_path.iterdir())


def test_github_client_never_sends_credential_to_arbitrary_host():
    client = publish_update.GitHubClient("private-token")
    with pytest.raises(ValueError, match="官方"):
        client.request("GET", "https://another.example.com/releases")


def test_github_git_credentials_are_requested_without_interaction(monkeypatch):
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    calls = []

    def run(command, **kwargs):
        calls.append((command, kwargs))
        return type("Result", (), {"returncode": 0, "stdout": "protocol=https\nhost=github.com\npassword=test-secret\n"})()

    monkeypatch.setattr(publish_update.subprocess, "run", run)
    assert publish_update.github_token("example/xiaoyu", allow_git_credential=True) == "test-secret"
    command, options = calls[0]
    assert command == ["git", "credential", "fill"]
    assert "path=example/xiaoyu.git\n" in options["input"]
    assert options["env"]["GIT_TERMINAL_PROMPT"] == "0"
    assert options["env"]["GCM_INTERACTIVE"] == "never"
    assert options["capture_output"] is True


def test_github_env_token_does_not_query_credential_manager(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "env-secret")
    monkeypatch.setattr(publish_update.subprocess, "run", lambda *_args, **_kwargs: pytest.fail("unexpected git process"))
    assert publish_update.github_token("example/xiaoyu", allow_git_credential=True) == "env-secret"


class S3Missing(Exception):
    response = {"Error": {"Code": "NoSuchKey"}}


class FakeS3:
    def __init__(self, corrupt=False):
        self.calls = []
        self.package = b""
        self.corrupt = corrupt

    def head_object(self, **kwargs):
        raise S3Missing()

    def upload_file(self, filename, bucket, key, **kwargs):
        self.calls.append("package")
        self.package = Path(filename).read_bytes()

    def get_object(self, **kwargs):
        self.calls.append("verify")
        return {"Body": io.BytesIO(self.package + (b"bad" if self.corrupt else b""))}

    def put_object(self, **kwargs):
        self.calls.append("latest")


@pytest.mark.parametrize("corrupt", [False, True])
def test_s3_publishes_pointer_only_after_remote_verification(tmp_path, package, corrupt):
    raw_package = b"archive"
    metadata = {"version": "0.4.0", "size": len(raw_package), "sha256": hashlib.sha256(raw_package).hexdigest()}
    channel = {"manifest_url": "https://example.com/latest.json", "public_key": package[3]["public_key"]}
    prepared = tmp_path / "prepared"
    (prepared / "0.4.0").mkdir(parents=True)
    (prepared / "0.4.0" / "❤.zip").write_bytes(raw_package)
    (prepared / "channel.json").write_bytes(canonical_json(channel))
    (prepared / "latest.json").write_bytes(publish_update.make_release(metadata, "https://example.com/0.4.0/package.zip", "notes", package[2]))
    client = FakeS3(corrupt)
    if corrupt:
        with pytest.raises(ValueError):
            publish_update.upload_s3(prepared, "bucket", client=client)
        assert client.calls == ["package", "verify"]
    else:
        publish_update.upload_s3(prepared, "bucket", client=client)
        assert client.calls == ["package", "verify", "latest"]
