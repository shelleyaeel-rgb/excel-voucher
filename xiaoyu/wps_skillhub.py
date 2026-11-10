"""Personal WPS browser authorization and bounded SkillHub JSON-RPC transport.

Credentials are exchanged only with WPS and encrypted with Windows DPAPI. The
model never sees this transport or its token. Mutating calls are never retried.
"""
from __future__ import annotations

import json
import math
import threading
import time
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from .ai_settings import _atomic_write, _decrypt_key, _encrypt_key
from . import __version__
from .config import data_dir
from .wps_service import WpsError, _NoRedirect, load_bindings, save_bindings, normalize_share_url

MCP_URL = "https://mcp-center.wps.cn/skill_hub/mcp"
AUTH_URL = "https://mcp-center.wps.cn/kdocs-auth/auth-guide"
EXCHANGE_URL = "https://api.wps.cn/office/v5/ai/skill_hub/wps_auth/exchange"
MAX_BYTES = 8 * 1024 * 1024
_LOCK = threading.RLock()


@dataclass
class WpsAccount:
    account_id: str
    token: str = field(repr=False)
    display_name: str = "WPS 个人账号"
    expires_at: float = 0


def _valid_token(token):
    return isinstance(token, str) and 0 < len(token) <= 16384 and all(33 <= ord(c) <= 126 for c in token)


def _valid_account(account):
    try:
        uuid.UUID(account.account_id)
        return (_valid_token(account.token) and isinstance(account.display_name, str)
                and 0 < len(account.display_name) <= 200
                and type(account.expires_at) in (int, float)
                and math.isfinite(account.expires_at) and 0 <= account.expires_at < 1e12)
    except (ValueError, TypeError, AttributeError):
        return False


def load_account(path=None):
    target = Path(path) if path is not None else data_dir() / "wps_account.json"
    with _LOCK:
        try:
            if target.stat().st_size > 100000:
                raise ValueError()
            value = json.loads(target.read_text(encoding="utf-8"))
            if not isinstance(value, dict) or value.get("version") != 1 or "token" in value:
                raise ValueError()
            if value.get("account") is None:
                return None
            item = value["account"]
            token = _decrypt_key(item["token_protected"])
            if not _valid_token(token) or not isinstance(item["account_id"], str):
                raise ValueError()
            uuid.UUID(item["account_id"])
            name, expiry = item.get("display_name", "WPS 个人账号"), item.get("expires_at", 0)
            account = WpsAccount(item["account_id"], token, name, expiry)
            if not _valid_account(account):
                raise ValueError()
            return account
        except FileNotFoundError:
            return None
        except (OSError, ValueError, KeyError, TypeError, UnicodeError):
            raise WpsError("无法读取 WPS 授权，原配置已保留；请在当前 Windows 账户下重新连接。", code="ACCOUNT_READ_FAILED") from None


def save_account(account, path=None):
    target = Path(path) if path is not None else data_dir() / "wps_account.json"
    with _LOCK:
        try:
            if account is not None and not _valid_account(account):
                raise ValueError()
            record = None if account is None else dict(account_id=account.account_id,
                token_protected=_encrypt_key(account.token), display_name=account.display_name,
                expires_at=account.expires_at)
            _atomic_write(target, {"version": 1, "account": record})
        except (OSError, ValueError, TypeError, AttributeError):
            raise WpsError("无法安全保存 WPS 授权，请检查当前 Windows 账户和保存目录。", code="ACCOUNT_SAVE_FAILED") from None


def require_account(account_id=None, path=None):
    account = load_account(path)
    if account is None or (account.expires_at and account.expires_at <= time.time()):
        raise WpsError("请先连接 WPS 个人账号。", code="AUTH_REQUIRED")
    if account_id and account.account_id != account_id:
        raise WpsError("WPS 账号连接已变化，请重新选择这份云文档。", code="ACCOUNT_CHANGED")
    return account


def mark_account_expired(account_id, path=None):
    """Invalidate only the credential scope that actually failed at WPS."""
    with _LOCK:
        account = load_account(path)
        if account is not None and account.account_id == account_id:
            account.expires_at = 1
            save_account(account, path)


def _opener():
    return urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())


def _post(opener, url, body, headers=None, *, mutation=False, timeout=30):
    try:
        data = json.dumps(body, ensure_ascii=False, allow_nan=False).encode("utf-8")
        if len(data) > MAX_BYTES:
            raise WpsError("WPS 请求过大，请缩小处理范围。", code="REQUEST_TOO_LARGE")
        req = urllib.request.Request(url, data=data, method="POST", headers={
            "Content-Type": "application/json", "Accept": "application/json, text/event-stream", **(headers or {})})
        with opener.open(req, timeout=timeout) as response:
            raw = response.read(MAX_BYTES + 1)
            response_headers = dict(response.headers)
            status = response.status
        if len(raw) > MAX_BYTES:
            raise WpsError("WPS 返回内容过大，请缩小读取范围。", code="RESPONSE_TOO_LARGE", mutation_attempted=mutation)
        if status == 202 and not raw.strip():
            return None, response_headers
        if status != 200:
            raise WpsError("WPS 接口返回异常，请稍后检查连接。", code="HTTP_ERROR", mutation_attempted=mutation)
        decoded = raw.decode("utf-8")
        if decoded.lstrip().startswith(("event:", "data:", ":")):
            messages = []
            for event in decoded.replace("\r\n", "\n").split("\n\n"):
                parts = [line[5:].lstrip() for line in event.splitlines() if line.startswith("data:")]
                if parts:
                    messages.append(json.loads("\n".join(parts)))
            matches = [m for m in messages if isinstance(m, dict) and m.get("id") == body.get("id")]
            if len(matches) != 1:
                raise ValueError()
            value = matches[0]
        else:
            value = json.loads(decoded)
        if not isinstance(value, dict):
            raise ValueError()
        return value, response_headers
    except WpsError as exc:
        exc.mutation_attempted = exc.mutation_attempted or mutation
        raise
    except urllib.error.HTTPError as exc:
        code = "AUTH_REQUIRED" if exc.code == 401 else "ACCESS_DENIED" if exc.code == 403 else "HTTP_ERROR"
        message = "WPS 授权已失效，请重新连接。" if exc.code == 401 else "WPS 拒绝访问，请核对账号和文档权限。" if exc.code == 403 else "WPS 请求未完成，请核对处理记录。"
        raise WpsError(message, code=code, mutation_attempted=mutation) from None
    except (urllib.error.URLError, TimeoutError, OSError):
        raise WpsError("WPS 连接失败或超时；已发出的更新需先核对结果。", code="NETWORK_ERROR", mutation_attempted=mutation) from None
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise WpsError("WPS 返回内容无法识别，请检查连接和处理记录。", code="INVALID_RESPONSE", mutation_attempted=mutation) from None


class SkillHubClient:
    def __init__(self, token, opener=None):
        if not _valid_token(token):
            raise WpsError("请重新连接 WPS 账号。", code="AUTH_REQUIRED")
        self._token = token
        self.opener = opener or _opener()
        self._session = ""
        self._protocol = "2025-03-26"
        self._seq = 0
        self._lock = threading.RLock()

    def _rpc(self, method, params, *, notification=False, mutation=False):
        self._seq += 1
        body = {"jsonrpc": "2.0", "method": method, "params": params}
        if not notification:
            body["id"] = self._seq
        headers = {"Authorization": "Bearer " + self._token}
        if self._session:
            headers.update({"Mcp-Session-Id": self._session, "MCP-Protocol-Version": self._protocol})
        value, response_headers = _post(self.opener, MCP_URL, body, headers, mutation=mutation, timeout=60)
        if notification:
            return None
        if not isinstance(value, dict) or value.get("jsonrpc") != "2.0" or value.get("id") != body["id"] or "error" in value:
            raise WpsError("WPS 调用失败，请核对账号权限和处理记录。", code="RPC_ERROR", mutation_attempted=mutation)
        result = value.get("result")
        if not isinstance(result, dict):
            raise WpsError("WPS 响应格式不正确。", code="INVALID_RESPONSE", mutation_attempted=mutation)
        if method == "initialize":
            session = next((v for k, v in response_headers.items() if k.lower() == "mcp-session-id"), "")
            protocol = result.get("protocolVersion")
            if not isinstance(session, str) or not session or len(session) > 512 or any(ord(c) < 33 or ord(c) > 126 for c in session) or protocol not in ("2024-11-05", "2025-03-26", "2025-06-18"):
                raise WpsError("WPS 会话未能建立。", code="INVALID_RESPONSE")
            self._session, self._protocol = session, protocol
        return result

    def initialize(self):
        with self._lock:
            if not self._session:
                self._rpc("initialize", {"protocolVersion": self._protocol, "capabilities": {},
                    "clientInfo": {"name": "xiaoyu-finance", "version": __version__}})
                self._rpc("notifications/initialized", {}, notification=True)

    def call_tool(self, name, arguments, mutation=False):
        with self._lock:
            self.initialize()
            result = self._rpc("tools/call", {"name": name, "arguments": arguments}, mutation=mutation)
            content = result.get("structuredContent")
            if content is None:
                texts = [c.get("text") for c in result.get("content", []) if isinstance(c, dict) and c.get("type") == "text"]
                try:
                    if len(texts) != 1:
                        raise ValueError()
                    content = json.loads(texts[0])
                except (ValueError, TypeError):
                    raise WpsError("WPS 返回的文档信息无法识别。", code="INVALID_RESPONSE", mutation_attempted=mutation) from None
            if not isinstance(content, dict):
                raise WpsError("WPS 返回的文档信息无法识别。", code="INVALID_RESPONSE", mutation_attempted=mutation)
            code = content.get("code")
            if result.get("isError") or code not in (None, 0, 200, "0", "200"):
                auth = code in (400006, "400006", 401, "401")
                denied = code in (403, "403", 403001, "403001")
                limited = code in (429, "429", 429001, "429001", 429002, "429002")
                raise WpsError("WPS 授权已失效，请重新连接。" if auth else "当前账号无权执行此操作，请核对文档权限。" if denied else "WPS 暂时限制了调用频率，请稍后再试；已发出的更新先核对记录。" if limited else "WPS 未能完成此操作，请核对文档状态和处理记录。",
                    code="AUTH_REQUIRED" if auth else "ACCESS_DENIED" if denied else "RATE_LIMITED" if limited else "TOOL_ERROR", mutation_attempted=mutation)
            return content


class WpsAccountService:
    def __init__(self, path=None, documents_path=None, *, opener=None, client_factory=SkillHubClient):
        self.path = Path(path) if path is not None else data_dir() / "wps_account.json"
        self.documents_path = documents_path
        self.opener = opener or _opener()
        self.client_factory = client_factory

    def status(self):
        account = load_account(self.path)
        expired = bool(account and account.expires_at and account.expires_at <= time.time())
        return {"connected": bool(account and not expired), "status": "expired" if expired else "connected" if account else "disconnected",
                "account_id": account.account_id if account else "", "display_name": account.display_name if account else ""}

    def disconnect(self):
        save_account(None, self.path)

    def connect(self, cancel, on_status, open_url):
        auth_code = str(uuid.uuid4())
        if cancel.is_set():
            raise WpsError("已取消 WPS 连接。", code="CANCELLED")
        on_status("请在浏览器中登录 WPS 个人账号并同意授权，完成后会自动继续。")
        if open_url(AUTH_URL + "?auth_code=" + auth_code) is False:
            raise WpsError("无法打开 WPS 登录页，请检查默认浏览器后重新连接。", code="BROWSER_FAILED")
        deadline = time.monotonic() + 600
        while time.monotonic() < deadline:
            if cancel.is_set():
                raise WpsError("已取消 WPS 连接，当前要求和附件已保留。", code="CANCELLED")
            body, _ = _post(self.opener, EXCHANGE_URL, {"code": auth_code}, timeout=15)
            if not isinstance(body, dict):
                raise WpsError("WPS 授权返回格式无法识别，请重新连接。", code="INVALID_RESPONSE")
            code = body.get("code")
            if code == 200:
                credentials = body.get("data")
                if not isinstance(credentials, dict):
                    raise WpsError("WPS 未返回有效授权，请重新连接。", code="INVALID_RESPONSE")
                token = credentials.get("token")
                if not _valid_token(token):
                    raise WpsError("WPS 未返回有效授权，请重新连接。", code="INVALID_RESPONSE")
                if cancel.is_set():
                    raise WpsError("已取消 WPS 连接。", code="CANCELLED")
                # No browser cookies are read. A new local scope prevents a new
                # login/account from silently reusing old document identities.
                account = WpsAccount(str(uuid.uuid4()), token)
                expiry = credentials.get("expires_in")
                if type(expiry) in (int, float) and 0 < expiry < 366 * 86400:
                    account.expires_at = time.time() + expiry
                self.client_factory(token).initialize()
                if cancel.is_set():
                    raise WpsError("已取消 WPS 连接。", code="CANCELLED")
                save_account(account, self.path)
                on_status("WPS 账号已连接，正在继续处理。")
                return
            if code == 403:
                raise WpsError("请切换到 WPS 个人账号后重新连接。", code="ACCESS_DENIED")
            if code == 409:
                raise WpsError("已取消 WPS 授权，当前要求和附件已保留。", code="CANCELLED")
            if code != 202:
                raise WpsError("WPS 授权未完成，请重新连接。", code="AUTH_FAILED")
            cancel.wait(1.5)
        raise WpsError("等待 WPS 登录超时，当前要求和附件已保留，请重新连接。", code="AUTH_TIMEOUT")

    def list_documents(self):
        return load_bindings(self.documents_path)

    def resolve_documents(self, links, cancel, on_status):
        from .wps_adapter import resolve_skillhub_document
        account = require_account(path=self.path)
        client = self.client_factory(account.token)
        resolved = []
        for link in dict.fromkeys(normalize_share_url(url) for url in links):
            if cancel.is_set():
                raise WpsError("已取消添加云文档，当前要求和附件已保留。", code="CANCELLED")
            on_status("正在读取云文档名称和访问权限……")
            try:
                resolved.append(resolve_skillhub_document(link, account.account_id, client))
            except WpsError as exc:
                if exc.code == "AUTH_REQUIRED":
                    mark_account_expired(account.account_id, self.path)
                raise
        if cancel.is_set():
            raise WpsError("已取消添加云文档。", code="CANCELLED")
        require_account(account.account_id, self.path)
        with _LOCK:
            known = self.list_documents()
            by_url = {normalize_share_url(b.share_url): b for b in known}
            for binding in resolved:
                previous = by_url.get(binding.share_url)
                if previous and previous.provider == "skillhub":
                    binding.id = previous.id
                by_url[binding.share_url] = binding
            save_bindings(list(by_url.values()), self.documents_path)
        return resolved
