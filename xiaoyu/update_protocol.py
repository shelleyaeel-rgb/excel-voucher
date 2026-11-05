"""Signed, deliberately small wire format shared by the client and updater."""
from __future__ import annotations

import base64
import binascii
from datetime import datetime
import json
from pathlib import Path
import re
from urllib.parse import urlsplit

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from .config import resource_path

APP_ID = "xiaoyu-voucher"
DATA_SCHEMA = 1
UPDATER_VERSION = 1
MAX_MANIFEST_BYTES = 64 * 1024
MAX_PACKAGE_BYTES = 1024 * 1024 * 1024
_VERSION = re.compile(r"(0|[1-9][0-9]{0,8})\.(0|[1-9][0-9]{0,8})\.(0|[1-9][0-9]{0,8})\Z")
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")


def canonical_json(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False).encode("utf-8")


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("更新信息含有重复字段。")
        result[key] = value
    return result


def read_json(raw: bytes) -> dict:
    if not isinstance(raw, bytes) or len(raw) > MAX_MANIFEST_BYTES:
        raise ValueError("更新信息大小不正确。")
    try:
        result = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_object,
                            parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)))
    except (UnicodeError, json.JSONDecodeError, RecursionError) as exc:
        raise ValueError("更新信息格式不正确。") from exc
    if not isinstance(result, dict):
        raise ValueError("更新信息格式不正确。")
    return result


def parse_version(value: str) -> tuple[int, int, int]:
    if not isinstance(value, str) or not _VERSION.fullmatch(value):
        raise ValueError("版本号必须是三段数字，例如 0.4.0。")
    return tuple(int(part) for part in value.split("."))


def validate_https_url(value: str) -> str:
    if (not isinstance(value, str) or len(value) > 4096
            or any(ord(c) <= 32 or ord(c) >= 127 for c in value)):
        raise ValueError("更新地址必须是有效的 HTTPS 地址。")
    try:
        url = urlsplit(value)
        valid = (url.scheme == "https" and url.hostname and not url.username
                 and not url.password and not url.fragment and "\\" not in value)
        _ = url.port
    except ValueError as exc:
        raise ValueError("更新地址格式不正确。") from exc
    if not valid:
        raise ValueError("更新地址必须使用 HTTPS，且不能包含账号或密码。")
    return value


def public_key_bytes(value: str) -> bytes:
    try:
        result = base64.b64decode(value, validate=True)
    except (ValueError, binascii.Error, TypeError) as exc:
        raise ValueError("更新签名公钥格式不正确。") from exc
    if len(result) != 32:
        raise ValueError("更新签名公钥长度不正确。")
    return result


def validate_channel(channel: dict) -> dict:
    if not isinstance(channel, dict) or set(channel) != {"manifest_url", "public_key"}:
        raise ValueError("更新配置只应包含版本地址和签名公钥。")
    validate_https_url(channel["manifest_url"])
    public_key_bytes(channel["public_key"])
    return dict(channel)


def load_channel(path: Path | None = None) -> dict | None:
    path = Path(path) if path is not None else resource_path("assets/update-channel.json")
    if not path.exists():
        return None
    with path.open("rb") as stream:
        raw = stream.read(MAX_MANIFEST_BYTES + 1)
    return validate_channel(read_json(raw))


def validate_payload(payload: dict) -> dict:
    fields = {"schema", "app", "version", "data_schema", "min_updater", "url",
              "size", "sha256", "notes", "published_at"}
    if not isinstance(payload, dict) or set(payload) != fields:
        raise ValueError("更新版本信息缺少字段或包含未知字段。")
    if type(payload["schema"]) is not int or payload["schema"] != 1 or payload["app"] != APP_ID:
        raise ValueError("这份更新不适用于本软件。")
    parse_version(payload["version"])
    if type(payload["data_schema"]) is not int or payload["data_schema"] != DATA_SCHEMA:
        raise ValueError("新版资料格式与当前软件不兼容，请联系发布者。")
    if (type(payload["min_updater"]) is not int
            or not 1 <= payload["min_updater"] <= UPDATER_VERSION):
        raise ValueError("请先手动升级更新程序，再接收这个版本。")
    validate_https_url(payload["url"])
    if type(payload["size"]) is not int or not 0 < payload["size"] <= MAX_PACKAGE_BYTES:
        raise ValueError("更新包大小超出允许范围。")
    if not isinstance(payload["sha256"], str) or not _SHA256.fullmatch(payload["sha256"]):
        raise ValueError("更新包校验值格式不正确。")
    if not isinstance(payload["notes"], str) or len(payload["notes"]) > 8000:
        raise ValueError("更新说明过长或格式不正确。")
    try:
        date = datetime.fromisoformat(payload["published_at"].replace("Z", "+00:00"))
        if date.tzinfo is None:
            raise ValueError("timezone missing")
    except (TypeError, AttributeError, ValueError) as exc:
        raise ValueError("更新时间格式不正确。") from exc
    return payload


def verify_envelope(raw: bytes, public_key: str) -> dict:
    envelope = read_json(raw)
    if set(envelope) != {"payload", "signature"} or not isinstance(envelope["payload"], dict):
        raise ValueError("更新签名信息格式不正确。")
    try:
        signature = base64.b64decode(envelope["signature"], validate=True)
        if len(signature) != 64:
            raise ValueError("signature length")
        Ed25519PublicKey.from_public_bytes(public_key_bytes(public_key)).verify(
            signature, canonical_json(envelope["payload"]))
    except (InvalidSignature, ValueError, TypeError, binascii.Error) as exc:
        raise ValueError("更新签名验证失败，已保留当前版本。") from exc
    return validate_payload(envelope["payload"])
