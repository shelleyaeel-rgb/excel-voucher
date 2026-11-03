"""Independent finance-assistant settings and local, estimated usage totals.

Only the API credential is encrypted, using Windows DPAPI for the current user.
Neither this module nor its JSON files access the accounting application's data.
The usage ledger is an estimate, not an OpenAI balance or billing statement.
"""
from __future__ import annotations

import base64
import ctypes
import json
import math
import os
import tempfile
import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from urllib.parse import urlsplit

from .config import data_dir


_LOCK = threading.RLock()
_ENTROPY = b"XiaoYuVoucher/FinanceAI/APIKey/v1"
_CST = timezone(timedelta(hours=8))


@dataclass
class AISettings:
    api_key: str = field(default="", repr=False)
    proxy_mode: str = "direct"
    proxy_url: str = ""
    monthly_limit_usd: float = 20.0
    max_output_tokens: int = 8000


def validate_settings(settings: AISettings) -> None:
    if not isinstance(settings.api_key, str):
        raise ValueError("API 密钥格式不正确，请重新填写。")
    if settings.api_key and any(c.isspace() for c in settings.api_key):
        raise ValueError("API 密钥不能包含空格或换行，请重新复制完整密钥。")
    if settings.proxy_mode not in ("direct", "system", "custom"):
        raise ValueError("请选择直连、系统代理或自定义代理。")
    if not isinstance(settings.proxy_url, str):
        raise ValueError("代理地址格式不正确。")
    if settings.proxy_mode == "custom" and not settings.proxy_url:
        raise ValueError("请填写自定义代理地址，例如 http://127.0.0.1:7890。")
    if settings.proxy_url:
        try:
            parsed = urlsplit(settings.proxy_url)
            valid = (
                not any(c.isspace() for c in settings.proxy_url)
                and parsed.scheme in ("http", "https")
                and bool(parsed.hostname)
                and parsed.username is None and parsed.password is None
                and not parsed.path and not parsed.query and not parsed.fragment
                and (parsed.port is None or 1 <= parsed.port <= 65535)
            )
        except ValueError:
            valid = False
        if not valid:
            raise ValueError("代理地址仅支持 http:// 或 https://，不能含账号、密码或路径。")
    _validate_limit(settings.monthly_limit_usd)
    if (isinstance(settings.max_output_tokens, bool)
            or not isinstance(settings.max_output_tokens, int)
            or not 1000 <= settings.max_output_tokens <= 16000):
        raise ValueError("每次最大输出量应为 1000 到 16000 之间的整数。")


def _validate_limit(value: float) -> None:
    if (isinstance(value, bool) or not isinstance(value, (int, float))
            or not math.isfinite(value) or not 1 <= value <= 1000):
        raise ValueError("每月使用预算应为 1 到 1000 美元。")


def _crypt(data: bytes, *, decrypt: bool) -> bytes:
    if os.name != "nt":
        raise ValueError("当前系统不支持安全保存 API 密钥；请在 Windows 中配置。")
    from ctypes import wintypes

    class DataBlob(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_ubyte))]

    def blob(value: bytes):
        buffer = (ctypes.c_ubyte * len(value)).from_buffer_copy(value)
        return DataBlob(len(value), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte))), buffer

    source, source_buffer = blob(data)
    entropy, entropy_buffer = blob(_ENTROPY)
    output = DataBlob()
    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.LocalFree.restype = ctypes.c_void_p
    function = crypt32.CryptUnprotectData if decrypt else crypt32.CryptProtectData
    function.argtypes = [ctypes.POINTER(DataBlob), ctypes.c_void_p,
                         ctypes.POINTER(DataBlob), ctypes.c_void_p, ctypes.c_void_p,
                         wintypes.DWORD, ctypes.POINTER(DataBlob)]
    function.restype = wintypes.BOOL
    # Keep both backing buffers alive until the Windows API has consumed them.
    assert source_buffer is not None and entropy_buffer is not None
    if not function(ctypes.byref(source), None, ctypes.byref(entropy), None, None,
                    0x1, ctypes.byref(output)):
        action = "读取" if decrypt else "保存"
        raise ValueError(f"无法安全{action} API 密钥，请在当前 Windows 账户下重新配置。")
    try:
        return ctypes.string_at(output.pbData, output.cbData)
    finally:
        kernel32.LocalFree(ctypes.cast(output.pbData, ctypes.c_void_p))


def _encrypt_key(key: str) -> str:
    return base64.b64encode(_crypt(key.encode("utf-8"), decrypt=False)).decode("ascii")


def _decrypt_key(value: str) -> str:
    try:
        data = base64.b64decode(value, validate=True)
        return _crypt(data, decrypt=True).decode("utf-8")
    except (ValueError, UnicodeError, TypeError) as exc:
        raise ValueError("无法读取已保存的 API 密钥，请在当前 Windows 账户下重新配置。") from exc


def _atomic_write(path: Path, value: dict) -> None:
    temporary: Path | None = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
        temporary = Path(name)
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2, allow_nan=False)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except OSError as exc:
        raise ValueError("无法保存财务助手设置或用量，请检查保存目录是否可写。") from exc
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _read_json(path: Path, *, kind: str) -> dict | None:
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise ValueError(f"无法读取财务助手{kind}，请检查文件权限。") from exc
    try:
        value = json.loads(text)
        if not isinstance(value, dict) or value.get("version") != 1:
            raise ValueError("invalid schema")
        return value
    except (ValueError, TypeError) as exc:
        raise ValueError(f"财务助手{kind}文件已损坏，原文件已保留，请重新配置或恢复备份。") from exc


def load_settings(path: Path | None = None) -> AISettings:
    target = Path(path) if path is not None else data_dir() / "finance_ai.json"
    with _LOCK:
        value = _read_json(target, kind="设置")
        if value is None:
            return AISettings()
        if "api_key" in value:
            raise ValueError("设置文件含有未加密的密钥字段，请移除该文件并重新安全配置。")
        protected = value.get("api_key_protected", "")
        if not isinstance(protected, str):
            raise ValueError("财务助手密钥保存格式不正确，请重新配置。")
        settings = AISettings(
            api_key=_decrypt_key(protected) if protected else "",
            proxy_mode=value.get("proxy_mode", "direct"),
            proxy_url=value.get("proxy_url", ""),
            monthly_limit_usd=value.get("monthly_limit_usd", 20.0),
            max_output_tokens=value.get("max_output_tokens", 8000),
        )
        validate_settings(settings)
        return settings


def save_settings(settings: AISettings, path: Path | None = None) -> None:
    validate_settings(settings)
    target = Path(path) if path is not None else data_dir() / "finance_ai.json"
    with _LOCK:
        value = {
            "version": 1,
            "api_key_protected": _encrypt_key(settings.api_key) if settings.api_key else "",
            "proxy_mode": settings.proxy_mode,
            "proxy_url": settings.proxy_url,
            "monthly_limit_usd": settings.monthly_limit_usd,
            "max_output_tokens": settings.max_output_tokens,
        }
        _atomic_write(target, value)


def _current_month() -> str:
    return datetime.now(_CST).strftime("%Y-%m")


def _counter(value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError("用量计数必须为非负整数。")
    return value


def _empty_summary(month: str) -> dict:
    return dict(month=month, requests=0, input_tokens=0, output_tokens=0,
                cached_input_tokens=0, web_search_calls=0, container_sessions=0,
                estimated_usd=0.0, uncertain_requests=0)


class UsageLedger:
    """Per-calendar-month estimate for this installation only (UTC+8)."""

    def __init__(self, path: Path | None = None):
        self.path = Path(path) if path is not None else data_dir() / "finance_ai_usage.json"

    def _load(self) -> dict:
        value = _read_json(self.path, kind="用量")
        if value is None:
            return {"version": 1, "months": {}}
        months = value.get("months")
        if not isinstance(months, dict):
            raise ValueError("财务助手用量文件已损坏，请恢复备份后重试。")
        for month, summary in months.items():
            if not isinstance(summary, dict) or summary.get("month") != month:
                raise ValueError("财务助手用量文件已损坏，请恢复备份后重试。")
            for name in ("requests", "input_tokens", "output_tokens", "cached_input_tokens",
                         "web_search_calls", "container_sessions", "uncertain_requests"):
                _counter(summary.get(name))
            cost = summary.get("estimated_usd")
            if (isinstance(cost, bool) or not isinstance(cost, (float, int))
                    or not math.isfinite(cost) or cost < 0):
                raise ValueError("财务助手用量金额不正确，请恢复备份后重试。")
        return value

    def summary(self) -> dict:
        with _LOCK:
            month = _current_month()
            return dict(self._load()["months"].get(month, _empty_summary(month)))

    def record(self, input_tokens: int, output_tokens: int, cached_input_tokens: int = 0,
               web_search_calls: int = 0, container_sessions: int = 0,
               uncertain: bool = False) -> dict:
        for value in (input_tokens, output_tokens, cached_input_tokens, web_search_calls, container_sessions):
            _counter(value)
        if cached_input_tokens > input_tokens:
            raise ValueError("缓存输入量不能超过总输入量。")
        if not isinstance(uncertain, bool):
            raise ValueError("用量不确定标记格式不正确。")
        cost = (
            Decimal(input_tokens - cached_input_tokens) * Decimal("0.10") / 1_000_000
            + Decimal(cached_input_tokens) * Decimal("0.01") / 1_000_000
            + Decimal(output_tokens) * Decimal("0.50") / 1_000_000
            + Decimal(web_search_calls) * Decimal("0.01")
            + Decimal(container_sessions) * Decimal("0.03")
        )
        with _LOCK:
            value = self._load()
            month = _current_month()
            summary = value["months"].setdefault(month, _empty_summary(month))
            summary["requests"] += 1
            summary["input_tokens"] += input_tokens
            summary["output_tokens"] += output_tokens
            summary["cached_input_tokens"] += cached_input_tokens
            summary["web_search_calls"] += web_search_calls
            summary["container_sessions"] += container_sessions
            summary["uncertain_requests"] += int(uncertain)
            summary["estimated_usd"] = float(Decimal(str(summary["estimated_usd"])) + cost)
            _atomic_write(self.path, value)
            return dict(summary)

    def check_budget(self, limit_usd: float) -> None:
        _validate_limit(limit_usd)
        if self.summary()["estimated_usd"] >= limit_usd:
            raise ValueError("本机本月估算用量已达到预算，已暂停发送。请检查 OpenAI 账单或调整预算。")
