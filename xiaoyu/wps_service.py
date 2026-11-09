"""Bound WPS AirScript transport. Credentials never enter model tool arguments.

The webhook path identifies the routed document; a share URL alone cannot prove
that association. The user binds both once and the workbook name is then pinned.
This transport never retries a request, especially a possibly completed write.
"""
from __future__ import annotations

import json
import math
import re
import threading
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field, replace
from pathlib import Path

from .ai_settings import _atomic_write, _decrypt_key, _encrypt_key
from .config import data_dir


PROTOCOL = "xiaoyu-wps/1"
MAX_RESPONSE_BYTES = 8 * 1024 * 1024
MAX_REQUEST_BYTES = 4 * 1024 * 1024
_LOCK = threading.RLock()
_ID = re.compile(r"[A-Za-z0-9_-]{1,128}\Z")
_ENDPOINT_PATH = re.compile(r"/api/v3/ide/file/[A-Za-z0-9_-]+/script/[A-Za-z0-9_-]+/sync_task\Z")
_SHARE_PATH = re.compile(r"/l/[A-Za-z0-9_-]{1,128}/?\Z")
_STATUSES = {"applied", "unchanged", "conflict", "not_found", "duplicate", "formula", "merged"}
_SCRIPT_CODES = {
    "INVALID_ARGUMENTS", "INVALID_ACTION", "INVALID_PROTOCOL", "NAME_REQUIRED",
    "WORKBOOK_MISMATCH", "UNSUPPORTED_DOCUMENT_METADATA", "SHEET_NOT_FOUND",
    "UNSUPPORTED_CELL_METADATA", "HEADER_MISMATCH", "INVALID_NEW_VALUE",
    "WRITE_VERIFY_FAILED", "API_ERROR",
}


class WpsError(ValueError):
    """Safe, user-displayable error; unknown writes must be reconciled first."""

    def __init__(self, message: str, *, code: str = "WPS_ERROR", mutation_attempted: bool = False,
                 result: dict | None = None):
        super().__init__(message)
        self.code = code
        self.mutation_attempted = mutation_attempted
        self.result = result


@dataclass
class WpsBinding:
    id: str
    label: str
    share_url: str
    endpoint: str
    token: str = field(repr=False)
    workbook_name: str = ""


def normalize_share_url(url: str) -> str:
    try:
        parsed = urllib.parse.urlsplit(url.strip())
        valid = (parsed.scheme == "https" and parsed.netloc in ("www.kdocs.cn", "kdocs.cn")
                 and _SHARE_PATH.fullmatch(parsed.path) and not any(c.isspace() for c in url))
    except (AttributeError, TypeError, ValueError):
        valid = False
    if not valid:
        raise WpsError("请填写金山文档的完整分享链接，例如 https://www.kdocs.cn/l/文档编号。", code="INVALID_SHARE_URL")
    return "https://www.kdocs.cn" + parsed.path.rstrip("/")


def extract_share_links(text: str) -> list[str]:
    links: list[str] = []
    for match in re.finditer(r"https://(?:www\.)?kdocs\.cn/l/[A-Za-z0-9_-]+(?:/)?", text or ""):
        link = normalize_share_url(match.group())
        if link not in links:
            links.append(link)
    return links


def _endpoint(value: str) -> str:
    try:
        parsed = urllib.parse.urlsplit(value)
        valid = (parsed.scheme == "https" and parsed.netloc == "www.kdocs.cn"
                 and not parsed.query and not parsed.fragment
                 and _ENDPOINT_PATH.fullmatch(parsed.path)
                 and not any(c.isspace() for c in value))
    except (TypeError, ValueError, AttributeError):
        valid = False
    if not valid:
        raise WpsError("共享脚本地址无效；请复制目标文档的 WPS sync_task webhook 地址。", code="INVALID_ENDPOINT")
    return value


def validate_binding(binding: WpsBinding) -> None:
    if (not isinstance(binding, WpsBinding) or not isinstance(binding.id, str)
            or not _ID.fullmatch(binding.id) or not isinstance(binding.label, str)
            or not binding.label.strip() or len(binding.label) > 200):
        raise WpsError("云文档名称或编号无效，请重新添加。", code="INVALID_BINDING")
    normalize_share_url(binding.share_url)
    _endpoint(binding.endpoint)
    if (not isinstance(binding.token, str) or not binding.token or len(binding.token) > 8192
            or any(ord(c) < 33 or ord(c) > 126 for c in binding.token)):
        raise WpsError("脚本令牌不能为空，也不能含空格、换行或非英文字符。", code="INVALID_TOKEN")
    if not isinstance(binding.workbook_name, str) or len(binding.workbook_name) > 512:
        raise WpsError("云文档校验名称无效，请重新连接。", code="INVALID_BINDING")


def _validate_collection(bindings: list[WpsBinding]) -> None:
    if not isinstance(bindings, list) or len(bindings) > 100:
        raise WpsError("云文档配置格式不正确或超过 100 份。", code="INVALID_BINDING")
    ids, urls = set(), set()
    for binding in bindings:
        validate_binding(binding)
        url = normalize_share_url(binding.share_url)
        if binding.id in ids or url in urls:
            raise WpsError("云文档编号或分享链接重复，请保留一份配置。", code="DUPLICATE_BINDING")
        ids.add(binding.id)
        urls.add(url)


def load_bindings(path: Path | None = None) -> list[WpsBinding]:
    target = Path(path) if path is not None else data_dir() / "wps_documents.json"
    with _LOCK:
        try:
            if target.stat().st_size > 2 * 1024 * 1024:
                raise ValueError("oversized")
            body = json.loads(target.read_text(encoding="utf-8"))
            if not isinstance(body, dict) or body.get("version") != 1 or not isinstance(body.get("documents"), list):
                raise ValueError("schema")
            bindings = []
            for item in body["documents"]:
                if not isinstance(item, dict) or "token" in item or not isinstance(item.get("token_protected"), str):
                    raise ValueError("credential schema")
                bindings.append(WpsBinding(id=item["id"], label=item["label"], share_url=item["share_url"],
                                           endpoint=item["endpoint"], token=_decrypt_key(item["token_protected"]),
                                           workbook_name=item.get("workbook_name", "")))
            _validate_collection(bindings)
            return bindings
        except FileNotFoundError:
            return []
        except (OSError, ValueError, TypeError, KeyError, UnicodeError):
            raise WpsError("无法读取云文档配置；原文件已保留，请在当前 Windows 账户下重新配置或恢复备份。", code="CONFIG_READ_FAILED") from None


def save_bindings(bindings: list[WpsBinding], path: Path | None = None) -> None:
    _validate_collection(bindings)
    target = Path(path) if path is not None else data_dir() / "wps_documents.json"
    with _LOCK:
        try:
            documents = [dict(id=b.id, label=b.label, share_url=normalize_share_url(b.share_url),
                              endpoint=b.endpoint, token_protected=_encrypt_key(b.token),
                              workbook_name=b.workbook_name) for b in bindings]
            _atomic_write(target, {"version": 1, "documents": documents})
        except (OSError, ValueError, TypeError):
            raise WpsError("无法安全保存云文档配置；请检查当前 Windows 账户和保存目录。", code="CONFIG_SAVE_FAILED") from None


def _integer(value, minimum=1, maximum=100000):
    return type(value) is int and minimum <= value <= maximum


def _scalar(value):
    return (value is None or type(value) is bool or (isinstance(value, str) and len(value) <= 32767)
            or (type(value) is int and abs(value) <= 9007199254740991)
            or (type(value) is float and math.isfinite(value)))


def _columns(value):
    return (isinstance(value, list) and 1 <= len(value) <= 30
            and all(_integer(c, 1, 16384) for c in value) and len(set(value)) == len(value))


def _validate_request(action, payload):
    valid = action in ("describe", "read", "apply")
    allowed = {
        "describe": set(), "read": {"sheet", "start_row", "end_row", "columns"},
        "apply": {"task_id", "batch_id", "sheet", "header_row", "expected_headers", "key_columns",
                  "row_start", "row_end", "target_column", "changes"},
    }
    valid = valid and set(payload) == allowed[action]
    if valid and action != "describe":
        valid = isinstance(payload["sheet"], str) and 0 < len(payload["sheet"]) <= 255
    if valid and action == "read":
        start, end = payload["start_row"], payload["end_row"]
        valid = (_integer(start) and _integer(end) and start <= end and end - start < 500
                 and _columns(payload["columns"]))
    if valid and action == "apply":
        start, end = payload["row_start"], payload["row_end"]
        valid = (_integer(start) and _integer(end) and start <= end and _integer(payload["header_row"])
                 and payload["header_row"] < start and _integer(payload["target_column"], 1, 16384)
                 and _columns(payload["key_columns"]) and all(isinstance(payload[k], str) and _ID.fullmatch(payload[k]) for k in ("task_id", "batch_id")))
        headers, changes = payload["expected_headers"], payload["changes"]
        valid = valid and isinstance(headers, list) and 1 <= len(headers) <= 60 and isinstance(changes, list) and 1 <= len(changes) <= 100
        if valid:
            valid = all(isinstance(h, dict) and set(h) == {"column", "value"} and _integer(h["column"], 1, 16384) and _scalar(h["value"]) for h in headers)
            cols = [h["column"] for h in headers] if valid else []
            valid = valid and len(set(cols)) == len(cols) and set(payload["key_columns"] + [payload["target_column"]]).issubset(cols)
        if valid:
            for change in changes:
                if not isinstance(change, dict) or set(change) != {"row", "key_values", "old_value", "old_formula", "old_number_format", "new_value", "guards"}:
                    valid = False
                    break
                keys, guards = change["key_values"], change["guards"]
                valid = (_integer(change["row"], start, end) and isinstance(keys, list) and len(keys) == len(payload["key_columns"])
                         and all(_scalar(v) and v is not None and v != "" for v in keys) and all(_scalar(change[k]) for k in ("old_value", "old_formula", "new_value"))
                         and isinstance(change["old_number_format"], str) and len(change["old_number_format"]) <= 1024
                         and isinstance(guards, list) and len(guards) <= 30
                         and all(isinstance(g, dict) and set(g) == {"column", "value", "formula"}
                                 and _integer(g["column"], 1, 16384) and _scalar(g["value"]) and _scalar(g["formula"]) for g in guards))
                if valid:
                    guard_cols = [g["column"] for g in guards]
                    valid = len(set(guard_cols)) == len(guard_cols) and set(guard_cols).issubset(cols)
                if not valid:
                    break
                if isinstance(change["new_value"], str) and change["new_value"].startswith("="):
                    raise WpsError("本版暂不写入以等号开头的文本，以免被云文档当成公式。", code="INVALID_NEW_VALUE")
    if not valid:
        raise WpsError("云文档操作参数不完整或超出允许范围，尚未发出请求。", code="INVALID_ARGUMENTS")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise WpsError("WPS 接口要求跳转，已停止并禁止转发令牌。", code="REDIRECT_BLOCKED")


class WpsClient:
    def __init__(self, binding: WpsBinding, opener=None):
        validate_binding(binding)
        self.binding = replace(binding)
        self.endpoint_file_id = urllib.parse.urlsplit(binding.endpoint).path.split("/")[5]
        self.opener = opener if opener is not None else urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())

    def call(self, action: str, **payload) -> dict:
        validate_binding(self.binding)
        _validate_request(action, payload)
        if not self.binding.workbook_name and action != "describe":
            raise WpsError("请先连接并校验云文档，再读取或更新内容。", code="NAME_REQUIRED")
        argv = dict(payload, action=action, protocol=PROTOCOL, expected_workbook_name=self.binding.workbook_name)
        try:
            data = json.dumps({"Context": {"argv": argv}}, ensure_ascii=False, allow_nan=False).encode("utf-8")
        except (ValueError, TypeError, UnicodeError):
            raise WpsError("云文档请求无法编码。", code="INVALID_ARGUMENTS") from None
        if len(data) > MAX_REQUEST_BYTES:
            raise WpsError("本批更新内容过大，请缩小批次。", code="REQUEST_TOO_LARGE")
        request = urllib.request.Request(self.binding.endpoint, data=data, method="POST",
                                         headers={"Content-Type": "application/json", "AirScript-Token": self.binding.token})
        mutation = action == "apply"
        try:
            with self.opener.open(request, timeout=60) as response:
                status = getattr(response, "status", 200)
                if status != 200:
                    raise WpsError("WPS 接口未返回正常响应，请核对任务记录。", code="HTTP_ERROR", mutation_attempted=mutation)
                raw = response.read(MAX_RESPONSE_BYTES + 1)
            if len(raw) > MAX_RESPONSE_BYTES:
                raise WpsError("WPS 响应过大，已停止；请缩小读取范围。", code="RESPONSE_TOO_LARGE", mutation_attempted=mutation)
            envelope = json.loads(raw)
        except WpsError as exc:
            # A blocked redirect follows the initial request; do not assume that
            # an apply request had no effect merely because no reply was accepted.
            exc.mutation_attempted = exc.mutation_attempted or mutation
            raise
        except urllib.error.HTTPError as exc:
            message = "WPS 授权失败或权限不足，请检查文档连接。" if exc.code in (401, 403) else "WPS 接口请求失败，请核对任务记录。"
            raise WpsError(message, code="HTTP_ERROR", mutation_attempted=mutation) from None
        except (urllib.error.URLError, TimeoutError, OSError):
            raise WpsError("WPS 请求失败或超时；更新结果可能未知，请先核对任务记录，不能直接重复写入。", code="NETWORK_ERROR", mutation_attempted=mutation) from None
        except (ValueError, UnicodeError, RecursionError):
            raise WpsError("WPS 返回内容无法识别，请核对任务记录。", code="INVALID_RESPONSE", mutation_attempted=mutation) from None
        except Exception:
            raise WpsError("WPS 请求未完成，请核对任务记录。", code="TRANSPORT_ERROR", mutation_attempted=mutation) from None
        return self._result(envelope, action, payload)

    def _result(self, envelope, action, payload):
        mutation = action == "apply"

        def invalid():
            raise WpsError("WPS 返回内容与约定不符，请核对脚本版本和任务记录。", code="INVALID_RESPONSE", mutation_attempted=mutation)

        if not isinstance(envelope, dict) or envelope.get("status") != "finished" or envelope.get("error"):
            invalid()
        data = envelope.get("data")
        result = data.get("result") if isinstance(data, dict) else None
        if isinstance(result, str):
            try:
                result = json.loads(result)
            except (ValueError, RecursionError):
                invalid()
        if (not isinstance(result, dict) or result.get("protocol") != PROTOCOL or result.get("action") != action
                or type(result.get("ok")) is not bool or type(result.get("mutation_attempted")) is not bool):
            invalid()
        if result["ok"] is False:
            code = result.get("code")
            code = code if isinstance(code, str) and code in _SCRIPT_CODES else "SCRIPT_ERROR"
            safe_result = None
            if action == "apply" and result.get("task_id") == payload["task_id"] and result.get("batch_id") == payload["batch_id"]:
                outcomes = result.get("outcomes")
                if isinstance(outcomes, list) and len(outcomes) <= len(payload["changes"]):
                    safe_outcomes = []
                    for index, item in enumerate(outcomes):
                        if (not isinstance(item, dict) or type(item.get("index")) is not int or item["index"] != index
                                or not isinstance(item.get("status"), str) or item["status"] not in _STATUSES or not _integer(item.get("row"), payload["row_start"], payload["row_end"])
                                or type(item.get("column")) is not int or item["column"] != payload["target_column"]):
                            break
                        safe_outcomes.append({k: item[k] for k in ("index", "status", "row", "column")})
                    safe_result = dict(protocol=PROTOCOL, action=action, task_id=payload["task_id"], batch_id=payload["batch_id"],
                                       mutation_attempted=result["mutation_attempted"], outcomes=safe_outcomes)
            raise WpsError(f"云文档脚本未完成操作（{code}），请核对任务记录。", code=code,
                           mutation_attempted=result["mutation_attempted"], result=safe_result)
        name = result.get("workbook_name")
        if not isinstance(name, str) or not name or len(name) > 512 or (self.binding.workbook_name and name != self.binding.workbook_name):
            invalid()
        if action != "apply" and result["mutation_attempted"]:
            invalid()
        if action == "describe":
            sheets = result.get("sheets")
            if not isinstance(sheets, list) or len(sheets) > 1024:
                invalid()
            for sheet in sheets:
                if not isinstance(sheet, dict) or not isinstance(sheet.get("name"), str) or not sheet["name"]:
                    invalid()
                if not all(_integer(sheet.get(k), 1, 1048576 if "row" in k else 16384) for k in ("used_start_row", "used_end_row", "used_start_column", "used_end_column")):
                    invalid()
                if sheet["used_start_row"] > sheet["used_end_row"] or sheet["used_start_column"] > sheet["used_end_column"]:
                    invalid()
        elif action == "read":
            rows = result.get("rows")
            if not isinstance(rows, list) or len(rows) != payload["end_row"] - payload["start_row"] + 1:
                invalid()
            for number, row in enumerate(rows, payload["start_row"]):
                if not isinstance(row, dict) or type(row.get("row")) is not int or row["row"] != number or not isinstance(row.get("cells"), list) or len(row["cells"]) != len(payload["columns"]):
                    invalid()
                for column, cell in zip(payload["columns"], row["cells"]):
                    if (not isinstance(cell, dict) or type(cell.get("column")) is not int or cell["column"] != column
                            or "value" not in cell or "formula" not in cell or not _scalar(cell["value"]) or not _scalar(cell["formula"])
                            or not isinstance(cell.get("number_format"), str) or type(cell.get("has_formula")) is not bool or type(cell.get("merged")) is not bool):
                        invalid()
        else:
            outcomes = result.get("outcomes")
            if (result.get("task_id") != payload["task_id"] or result.get("batch_id") != payload["batch_id"]
                    or not isinstance(outcomes, list) or len(outcomes) != len(payload["changes"])):
                invalid()
            for index, item in enumerate(outcomes):
                if (not isinstance(item, dict) or type(item.get("index")) is not int or item["index"] != index
                        or not isinstance(item.get("status"), str) or item["status"] not in _STATUSES or not _integer(item.get("row"), payload["row_start"], payload["row_end"])
                        or type(item.get("column")) is not int or item["column"] != payload["target_column"]):
                    invalid()
            if any(item["status"] == "applied" for item in outcomes) and not result["mutation_attempted"]:
                invalid()
        clean = {k: result[k] for k in ("ok", "protocol", "action", "workbook_name", "mutation_attempted")}
        if action == "describe":
            clean["sheets"] = [{k: sheet[k] for k in ("name", "used_start_row", "used_end_row", "used_start_column", "used_end_column")}
                               for sheet in result["sheets"]]
        elif action == "read":
            clean["rows"] = [dict(row=row["row"], cells=[{k: cell[k] for k in ("column", "value", "formula", "number_format", "has_formula", "merged")}
                                                       for cell in row["cells"]]) for row in result["rows"]]
        else:
            clean.update(task_id=result["task_id"], batch_id=result["batch_id"],
                         outcomes=[{k: item[k] for k in ("index", "status", "row", "column")} for item in result["outcomes"]])
        return clean
