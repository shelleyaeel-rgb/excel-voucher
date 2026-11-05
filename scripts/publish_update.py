"""Verify locally, upload an immutable package, then publish the signed pointer."""
from __future__ import annotations

import argparse
import base64
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import sys
from typing import Callable
from urllib.error import HTTPError
from urllib.parse import quote, unquote, urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener
import uuid
import zipfile

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from xiaoyu.update_protocol import (APP_ID, DATA_SCHEMA, MAX_PACKAGE_BYTES, canonical_json,
                                    parse_version, validate_channel, validate_https_url,
                                    validate_payload, verify_envelope)
from xiaoyu.update_service import file_sha256
try:
    from .update_keys import load_private_key, public_key_text
    from .verify_portable import validate_members, verify_archive
except ImportError:
    from update_keys import load_private_key, public_key_text
    from verify_portable import validate_members, verify_archive


def inspect_archive(archive: Path, *, verifier: Callable = verify_archive) -> dict:
    archive = Path(archive)
    if not 0 < archive.stat().st_size <= MAX_PACKAGE_BYTES:
        raise ValueError("发行包大小不正确。")
    with zipfile.ZipFile(archive) as bundle:
        validate_members(bundle)
        if bundle.testzip() is not None:
            raise ValueError("发行包完整性检查失败。")
        info = bundle.getinfo("❤/release-files.json")
        if info.file_size > 4 * 1024 * 1024:
            raise ValueError("程序文件清单过大。")
        manifest = json.loads(bundle.read(info).decode("utf-8"))
        if (manifest.get("schema") != 1 or manifest.get("app") != APP_ID
                or manifest.get("data_schema") != DATA_SCHEMA
                or not isinstance(manifest.get("files"), dict)):
            raise ValueError("程序文件清单格式不正确。")
        parse_version(manifest.get("version"))
        members = {str(PurePosixPath(item.filename).relative_to("❤")): item
                   for item in bundle.infolist() if not item.is_dir()
                   and item.filename != "❤/release-files.json"}
        if set(members) != set(manifest["files"]):
            raise ValueError("发行包文件与程序清单不一致。")
        for name, item in members.items():
            if "data" in (part.casefold() for part in PurePosixPath(name).parts):
                raise ValueError("发行包不能包含用户 data 目录。")
            with bundle.open(item) as stream:
                digest = hashlib.file_digest(stream, "sha256").hexdigest()
            if digest != manifest["files"][name]:
                raise ValueError(f"程序文件校验失败：{name}")
        channel_name = "❤/_internal/assets/update-channel.json"
        if channel_name not in bundle.namelist():
            raise ValueError("发行包没有更新地址和公钥配置，请配置后重新构建。")
        channel = validate_channel(json.loads(bundle.read(channel_name).decode("utf-8")))
    report = verifier(archive)
    if not isinstance(report, dict) or report.get("ok") is not True or report.get("version") != manifest["version"]:
        raise ValueError("程序自检版本与发行清单不一致。")
    return {"version": manifest["version"], "size": archive.stat().st_size,
            "sha256": file_sha256(archive), "channel": channel}


def make_release(metadata: dict, url: str, notes: str, private_key) -> bytes:
    payload = validate_payload({"schema": 1, "app": APP_ID, "version": metadata["version"],
        "data_schema": DATA_SCHEMA, "min_updater": 1, "url": url,
        "size": metadata["size"], "sha256": metadata["sha256"], "notes": notes,
        "published_at": datetime.now(timezone.utc).isoformat(timespec="seconds")})
    raw = canonical_json({"payload": payload, "signature": base64.b64encode(
        private_key.sign(canonical_json(payload))).decode("ascii")})
    verify_envelope(raw, public_key_text(private_key))
    return raw


def _check_signer(metadata: dict, private_key, manifest_url: str):
    if metadata["channel"] != {"manifest_url": manifest_url, "public_key": public_key_text(private_key)}:
        raise ValueError("发行包内的更新地址或公钥与发布设置不一致，请重新构建。")


def prepare_release(archive: Path, base_url: str, private_key_path: Path, notes: str,
                    output: Path | None = None, *, verifier: Callable = verify_archive) -> Path:
    base_url = validate_https_url(base_url.rstrip("/"))
    if urlsplit(base_url).query:
        raise ValueError("下载根地址不能包含查询参数。")
    key = load_private_key(private_key_path)
    metadata = inspect_archive(archive, verifier=verifier)
    _check_signer(metadata, key, base_url + "/latest.json")
    url = base_url + "/" + metadata["version"] + "/" + quote("❤.zip")
    raw = make_release(metadata, url, notes, key)
    output = Path(output) if output is not None else ROOT / "outputs" / "update-publish" / uuid.uuid4().hex
    output.mkdir(parents=True, exist_ok=False)
    package = output / metadata["version"] / "❤.zip"
    package.parent.mkdir()
    shutil.copyfile(archive, package)
    if package.stat().st_size != metadata["size"] or file_sha256(package) != metadata["sha256"]:
        raise ValueError("复制期间发行包发生变化，已停止发布。")
    (output / "latest.json").write_bytes(raw)
    (output / "channel.json").write_text(json.dumps(metadata["channel"], ensure_ascii=False, indent=2), encoding="utf-8")
    return output


def upload_s3(prepared: Path, bucket: str, *, prefix: str = "", endpoint_url: str | None = None,
              region: str | None = None, client=None) -> str:
    prepared = Path(prepared)
    channel = validate_channel(json.loads((prepared / "channel.json").read_text(encoding="utf-8")))
    raw = (prepared / "latest.json").read_bytes()
    payload = verify_envelope(raw, channel["public_key"])
    archive = prepared / payload["version"] / "❤.zip"
    if archive.stat().st_size != payload["size"] or file_sha256(archive) != payload["sha256"]:
        raise ValueError("待上传更新包校验失败。")
    if endpoint_url:
        validate_https_url(endpoint_url)
    if client is None:
        import boto3
        from botocore.config import Config
        client = boto3.client("s3", endpoint_url=endpoint_url, region_name=region,
                              config=Config(connect_timeout=15, read_timeout=60,
                                            retries={"max_attempts": 3}))
    prefix = prefix.strip("/")
    if ".." in prefix.split("/") or "\\" in prefix:
        raise ValueError("对象存储路径不正确。")
    package_key = "/".join(part for part in (prefix, payload["version"], "❤.zip") if part)
    pointer_key = "/".join(part for part in (prefix, "latest.json") if part)
    # Refuse replacing a published immutable version, including accidental reuse.
    try:
        existing = client.head_object(Bucket=bucket, Key=package_key)
    except Exception as exc:
        response = getattr(exc, "response", {})
        code = str(response.get("Error", {}).get("Code", ""))
        if code not in ("404", "NoSuchKey", "NotFound"):
            raise
    else:
        if existing:
            raise ValueError("远端已存在这个版本，请提高版本号后重新发布。")
    client.upload_file(str(archive), bucket, package_key, ExtraArgs={
        "ContentType": "application/zip", "CacheControl": "public,max-age=31536000,immutable",
        "Metadata": {"sha256": payload["sha256"]}})
    downloaded = client.get_object(Bucket=bucket, Key=package_key)
    body = downloaded["Body"]
    digest, size = hashlib.sha256(), 0
    try:
        while block := body.read(1024 * 1024):
            size += len(block)
            if size > payload["size"]:
                raise ValueError("远端更新包大小不一致。")
            digest.update(block)
    finally:
        body.close()
    if size != payload["size"] or digest.hexdigest() != payload["sha256"]:
        raise ValueError("远端更新包验证失败，最新版信息未更改。")
    client.put_object(Bucket=bucket, Key=pointer_key, Body=raw,
                      ContentType="application/json; charset=utf-8", CacheControl="no-store")
    return channel["manifest_url"]


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Never forward publishing credentials to a redirected host.
        raise ValueError("发布接口出现重定向，已停止以保护发布凭证。")


def github_token(repository: str, *, allow_git_credential: bool = False) -> str:
    token = os.environ.get("GITHUB_TOKEN", "").strip()
    if token:
        return token
    if allow_git_credential:
        env = dict(os.environ, GIT_TERMINAL_PROMPT="0", GCM_INTERACTIVE="never")
        result = subprocess.run(["git", "credential", "fill"],
            input=f"protocol=https\nhost=github.com\npath={repository}.git\n\n",
            text=True, capture_output=True, timeout=20, env=env,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        if result.returncode == 0:
            fields = dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)
            token = fields.get("password", "").strip()
            if token:
                return token
    raise ValueError("未找到 GitHub 发布凭证，请设置 GITHUB_TOKEN 或使用 --git-credential。")


class GitHubClient:
    def __init__(self, token: str):
        self._token = token
        self._opener = build_opener(_NoRedirect())

    def request(self, method: str, url: str, *, data=None, file: Path | None = None,
                content_type: str = "application/json"):
        validate_https_url(url)
        if urlsplit(url).hostname not in {"api.github.com", "uploads.github.com"}:
            raise ValueError("发布凭证仅允许发送给 GitHub 官方发布接口。")
        headers = {"Accept": "application/vnd.github+json", "Authorization": "Bearer " + self._token,
                   "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "XiaoYuVoucher-Publisher/1",
                   "Content-Type": content_type}
        stream = None
        try:
            if file is not None:
                stream = Path(file).open("rb")
                body = stream
                headers["Content-Length"] = str(Path(file).stat().st_size)
            else:
                body = None if data is None else canonical_json(data)
            request = Request(url, data=body, headers=headers, method=method)
            with self._opener.open(request, timeout=120) as response:
                raw = response.read(8 * 1024 * 1024 + 1)
                if len(raw) > 8 * 1024 * 1024:
                    raise ValueError("GitHub 响应过大。")
                return json.loads(raw.decode("utf-8")) if raw else None
        finally:
            if stream is not None:
                stream.close()


def _validate_repo(repository: str) -> str:
    if not isinstance(repository, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]{0,38}/[A-Za-z0-9_.-]{1,100}", repository):
        raise ValueError("仓库请填写 owner/repo，例如 your-name/xiaoyu-releases。")
    return repository


def _validate_asset(asset: dict, size: int, sha256: str, *, published: bool = False):
    if (asset.get("state") != "uploaded" or asset.get("size") != size
            or asset.get("digest") != "sha256:" + sha256):
        state = "请检查已公开的发布页面" if published else "发布仍为草稿"
        raise ValueError("GitHub 更新附件大小或 SHA-256 验证失败；" + state + "。")


def _final_asset_url(asset: dict, repository: str, tag: str, *, draft: bool) -> str:
    """Validate GitHub's asset identity, then pin the final release tag URL.

    A new tag does not exist while its release is a draft. GitHub therefore
    returns an untagged-<hex> URL until publication, even for a known tag_name.
    """
    name = asset.get("name")
    if (not isinstance(name, str) or not 1 <= len(name) <= 255 or name in {".", ".."}
            or any(ord(char) < 32 or ord(char) == 127 or char in "/\\" for char in name)):
        raise ValueError("GitHub 附件名称不安全。")
    actual = validate_https_url(asset.get("browser_download_url"))
    prefix = f"https://github.com/{repository}/releases/download/"
    if not actual.startswith(prefix) or urlsplit(actual).query:
        raise ValueError("GitHub 附件地址不属于本次仓库。")
    parts = actual[len(prefix):].split("/")
    if len(parts) != 2 or unquote(parts[1], errors="strict") != name:
        raise ValueError("GitHub 附件地址与附件名称不一致。")
    valid_slug = parts[0] == tag or (draft and re.fullmatch(r"untagged-[0-9a-f]{6,64}", parts[0]))
    if not valid_slug:
        raise ValueError("GitHub 附件地址没有对应本次版本或发布草稿。")
    final_url = prefix + tag + "/" + quote(name, safe="")
    if not draft and actual != final_url:
        raise ValueError("已公开附件的地址与签名版本信息不一致，请检查发布页面。")
    return final_url


def publish_github(archive: Path, repository: str, private_key_path: Path, notes: str,
                   *, allow_git_credential: bool = False, client=None,
                   verifier: Callable = verify_archive, resume_draft: int | None = None) -> str:
    repository = _validate_repo(repository)
    if resume_draft is not None and (type(resume_draft) is not int or resume_draft <= 0):
        raise ValueError("请提供有效的 GitHub 草稿编号。")
    key = load_private_key(private_key_path)
    metadata = inspect_archive(archive, verifier=verifier)
    manifest_url = f"https://github.com/{repository}/releases/latest/download/latest.json"
    _check_signer(metadata, key, manifest_url)
    # Validate note length before creating a remote draft.
    make_release(metadata, f"https://github.com/{repository}/releases/download/v{metadata['version']}/package.zip", notes, key)
    client = client or GitHubClient(github_token(repository, allow_git_credential=allow_git_credential))
    api = f"https://api.github.com/repos/{repository}"
    repo = client.request("GET", api)
    if repo.get("private") is not False or repo.get("archived") is True:
        raise ValueError("自动更新需要可公开下载的、未归档的发布仓库。")
    tag = "v" + metadata["version"]
    # A draft with the same tag is not returned by /tags for every token. List all
    # releases visible to the publisher too, and leave existing drafts untouched.
    for page in range(1, 101):
        releases = client.request("GET", api + f"/releases?per_page=100&page={page}")
        if not isinstance(releases, list):
            raise ValueError("GitHub 发布列表格式不正确。")
        if any(item.get("tag_name") == tag
               and (item.get("id") != resume_draft or item.get("draft") is not True)
               for item in releases):
            raise ValueError("这个版本已经存在（可能是未完成的草稿），请核对后使用新版本号。")
        if len(releases) < 100:
            break
    else:
        raise ValueError("发布记录过多，无法确认版本唯一性。")
    # Refuse publishing a downgrade as latest.
    try:
        latest = client.request("GET", api + "/releases/latest")
    except HTTPError as exc:
        if exc.code != 404:
            raise
    else:
        latest_tag = latest.get("tag_name", "")
        if latest_tag.startswith("v"):
            try:
                latest_version = parse_version(latest_tag[1:])
            except ValueError:
                latest_version = None
            if latest_version is not None and latest_version >= parse_version(metadata["version"]):
                raise ValueError("不能把相同或更旧的版本发布为最新版。")
    if resume_draft is not None:
        draft = client.request("GET", api + f"/releases/{resume_draft}")
        if (draft.get("id") != resume_draft or draft.get("tag_name") != tag
                or draft.get("draft") is not True or draft.get("prerelease") is not False
                or draft.get("url") != api + f"/releases/{resume_draft}"):
            raise ValueError("指定草稿的仓库、版本或状态与本次发布不一致。")
    else:
        draft = client.request("POST", api + "/releases", data={"tag_name": tag,
            "name": "给小雨的软件 " + metadata["version"], "body": notes,
            "draft": True, "prerelease": False})
    release_id = draft.get("id")
    if type(release_id) is not int or draft.get("draft") is not True:
        raise ValueError("GitHub 未返回有效的发布草稿。")
    upload = draft.get("upload_url", "").split("{", 1)[0]
    expected_upload = f"https://uploads.github.com/repos/{repository}/releases/{release_id}/assets"
    if upload.casefold() != expected_upload.casefold():
        raise ValueError("GitHub 上传地址与本次仓库不一致。")
    if resume_draft is not None:
        existing_assets = client.request("GET", api + f"/releases/{release_id}/assets?per_page=100")
        if (not isinstance(existing_assets, list) or len(existing_assets) != 1
                or not isinstance(existing_assets[0].get("name"), str)
                or not existing_assets[0]["name"].lower().endswith(".zip")):
            raise ValueError("只支持继续仅有一份 ZIP、尚无 latest.json 的草稿；不会覆盖已有附件。")
        package_asset = existing_assets[0]
    else:
        package_asset = client.request("POST", upload + "?" + urlencode({"name": "❤.zip"}),
                                       file=Path(archive), content_type="application/zip")
    _validate_asset(package_asset, metadata["size"], metadata["sha256"])
    package_url = _final_asset_url(package_asset, repository, tag, draft=True)
    # Sign the real attachment name under the final tag, never its temporary slug.
    raw = make_release(metadata, package_url, notes, key)
    output = ROOT / "outputs" / "update-publish" / uuid.uuid4().hex
    output.mkdir(parents=True)
    manifest = output / "latest.json"
    manifest.write_bytes(raw)
    manifest_asset = client.request("POST", upload + "?" + urlencode({"name": "latest.json"}),
                                    file=manifest, content_type="application/json")
    _validate_asset(manifest_asset, len(raw), hashlib.sha256(raw).hexdigest())
    if manifest_asset.get("name") != "latest.json":
        raise ValueError("GitHub 修改了版本清单名称，发布仍为草稿。")
    _final_asset_url(manifest_asset, repository, tag, draft=True)
    # Re-read both assets immediately before committing the public pointer.
    assets = client.request("GET", api + f"/releases/{release_id}/assets?per_page=100")
    by_id = {item.get("id"): item for item in assets}
    if len(assets) != 2 or set(by_id) != {package_asset.get("id"), manifest_asset.get("id")}:
        raise ValueError("草稿出现未知附件，已停止发布且未覆盖附件。")
    _validate_asset(by_id.get(package_asset.get("id"), {}), metadata["size"], metadata["sha256"])
    _validate_asset(by_id.get(manifest_asset.get("id"), {}), len(raw), hashlib.sha256(raw).hexdigest())
    published = client.request("PATCH", api + f"/releases/{release_id}",
                               data={"draft": False, "prerelease": False, "make_latest": "true"})
    if published.get("draft") is not False or published.get("tag_name") != tag:
        raise ValueError("未能确认 GitHub 发布状态，请检查该版本页面后再操作。")
    public_assets = client.request("GET", api + f"/releases/{release_id}/assets?per_page=100")
    public_by_id = {item.get("id"): item for item in public_assets}
    final_package = public_by_id.get(package_asset.get("id"), {})
    final_manifest = public_by_id.get(manifest_asset.get("id"), {})
    _validate_asset(final_package, metadata["size"], metadata["sha256"], published=True)
    _validate_asset(final_manifest, len(raw), hashlib.sha256(raw).hexdigest(), published=True)
    if (_final_asset_url(final_package, repository, tag, draft=False) != package_url
            or final_manifest.get("name") != "latest.json"):
        raise ValueError("已公开的附件与签名版本信息不一致，请检查发布页面。")
    _final_asset_url(final_manifest, repository, tag, draft=False)
    return published.get("html_url", f"https://github.com/{repository}/releases/tag/{tag}")


def main():
    parser = argparse.ArgumentParser(description="校验并签名发布远程更新。")
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("prepare", "github"):
        command = commands.add_parser(name)
        command.add_argument("--archive", type=Path, default=ROOT / "release" / "❤.zip")
        command.add_argument("--private-key", type=Path, required=True)
        command.add_argument("--notes-file", type=Path, required=True)
        if name == "prepare":
            command.add_argument("--base-url", required=True)
            command.add_argument("--output", type=Path)
        else:
            command.add_argument("--repo", required=True)
            command.add_argument("--git-credential", action="store_true")
            command.add_argument("--resume-draft", type=int,
                                 help="继续指定的同版本草稿，仅支持已上传ZIP且尚无版本清单的草稿")
    upload = commands.add_parser("upload")
    upload.add_argument("--prepared", type=Path, required=True)
    upload.add_argument("--bucket", required=True)
    upload.add_argument("--prefix", default="")
    upload.add_argument("--endpoint-url")
    upload.add_argument("--region")
    args = parser.parse_args()
    if args.command == "prepare":
        result = prepare_release(args.archive, args.base_url, args.private_key,
                                 args.notes_file.read_text(encoding="utf-8-sig"), args.output)
    elif args.command == "github":
        result = publish_github(args.archive, args.repo, args.private_key,
                                args.notes_file.read_text(encoding="utf-8-sig"),
                                allow_git_credential=args.git_credential, resume_draft=args.resume_draft)
    else:
        result = upload_s3(args.prepared, args.bucket, prefix=args.prefix,
                           endpoint_url=args.endpoint_url, region=args.region)
    print(result)


if __name__ == "__main__":
    main()
