"""Background download only; this module never changes installed program files."""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import threading
import time
from typing import Callable
from urllib.request import HTTPRedirectHandler, Request, build_opener
import uuid

from .update_protocol import (MAX_MANIFEST_BYTES, parse_version, validate_channel,
                              validate_https_url, verify_envelope)


class UpdateCancelled(Exception):
    pass


class HTTPSRedirectHandler(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        validate_https_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def secure_opener():
    return build_opener(HTTPSRedirectHandler())


def file_sha256(path: Path, cancel: threading.Event | None = None) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            if cancel is not None and cancel.is_set():
                raise UpdateCancelled()
            digest.update(block)
    return digest.hexdigest()


def _safe_path(path: Path, root: Path) -> Path:
    root = root.absolute()
    path = path.absolute()
    if path != root and not path.is_relative_to(root):
        raise ValueError("更新缓存路径超出范围。")
    # Check parents as well, including the cache root itself.
    for part in (path, *path.parents):
        if part.is_symlink() or (hasattr(part, "is_junction") and part.is_junction()):
            raise ValueError("更新缓存不能使用链接目录。")
    return path


class UpdateService:
    def __init__(self, channel: dict, current_version: str, cache_root: Path):
        self.channel = validate_channel(channel)
        self.current_version = parse_version(current_version)
        self.cache_root = Path(cache_root).absolute()
        self.opener = secure_opener()

    def _request(self, url: str, accept: str):
        validate_https_url(url)
        return self.opener.open(Request(url, headers={
            "Accept": accept, "User-Agent": "XiaoYuVoucher-Updater/1",
            "Cache-Control": "no-cache", "Accept-Encoding": "identity",
        }), timeout=15)

    @staticmethod
    def _cancelled(cancel: threading.Event):
        if cancel.is_set():
            raise UpdateCancelled()

    def check_and_download(self, cancel: threading.Event,
                           progress: Callable[[str, int], None]) -> Path | None:
        self._cancelled(cancel)
        progress("正在检查更新…", -1)
        manifest_started = time.monotonic()
        with self._request(self.channel["manifest_url"], "application/json") as response:
            validate_https_url(response.geturl())
            chunks, length = [], 0
            read = getattr(response, "read1", response.read)
            while True:
                self._cancelled(cancel)
                if time.monotonic() - manifest_started > 30:
                    raise TimeoutError("检查更新超时，下次打开时会重新检查。")
                block = read(min(16 * 1024, MAX_MANIFEST_BYTES + 1 - length))
                if not block:
                    break
                chunks.append(block)
                length += len(block)
                if length > MAX_MANIFEST_BYTES:
                    raise ValueError("更新信息大小不正确。")
            raw = b"".join(chunks)
        self._cancelled(cancel)
        payload = verify_envelope(raw, self.channel["public_key"])
        if parse_version(payload["version"]) <= self.current_version:
            progress("当前已是最新版本。", 100)
            return None
        root = _safe_path(self.cache_root, self.cache_root)
        root.mkdir(parents=True, exist_ok=True)
        candidate = _safe_path(root / (payload["version"] + "-" + payload["sha256"][:16]), root)
        candidate.mkdir(exist_ok=True)
        archive = _safe_path(candidate / "❤.zip", root)
        manifest = _safe_path(candidate / "release.json", root)
        if (archive.is_file() and archive.stat().st_size == payload["size"]
                and file_sha256(archive, cancel) == payload["sha256"]):
            self._write_manifest(manifest, raw, root)
            progress(f"新版 {payload['version']} 已准备好，关闭软件后自动更新。", 100)
            return candidate
        temporary = _safe_path(candidate / ("download-" + uuid.uuid4().hex + ".part"), root)
        received = 0
        digest = hashlib.sha256()
        started = time.monotonic()
        try:
            with self._request(payload["url"], "application/zip") as response, temporary.open("xb") as stream:
                validate_https_url(response.geturl())
                declared_size = response.headers.get("Content-Length")
                if declared_size is not None and int(declared_size) != payload["size"]:
                    raise ValueError("更新包大小与发布信息不一致。")
                read = getattr(response, "read1", response.read)
                while True:
                    self._cancelled(cancel)
                    if time.monotonic() - started > 600:
                        raise TimeoutError("更新下载超时，下次打开时会重新检查。")
                    block = read(256 * 1024)
                    if not block:
                        break
                    received += len(block)
                    if received > payload["size"]:
                        raise ValueError("更新包超过发布信息中的大小。")
                    digest.update(block)
                    stream.write(block)
                    progress(f"正在下载新版 {payload['version']}…", int(received * 100 / payload["size"]))
                stream.flush()
                os.fsync(stream.fileno())
            self._cancelled(cancel)
            if received != payload["size"] or digest.hexdigest() != payload["sha256"]:
                raise ValueError("更新包不完整或校验失败，已保留当前版本。")
            _safe_path(archive, root)
            os.replace(temporary, archive)
            self._write_manifest(manifest, raw, root)
            progress(f"新版 {payload['version']} 已准备好，关闭软件后自动更新。", 100)
            return candidate
        finally:
            if temporary.exists():
                _safe_path(temporary, root).unlink()

    @staticmethod
    def _write_manifest(path: Path, raw: bytes, root: Path):
        temporary = _safe_path(path.with_name("manifest-" + uuid.uuid4().hex + ".tmp"), root)
        try:
            with temporary.open("xb") as stream:
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
            _safe_path(path, root)
            os.replace(temporary, path)
        finally:
            if temporary.exists():
                _safe_path(temporary, root).unlink()
