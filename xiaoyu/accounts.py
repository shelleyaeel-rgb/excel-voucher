"""Resolve explicit account input without guessing similar accounting names."""

from __future__ import annotations

import re
from dataclasses import dataclass

from .models import Account


@dataclass
class AccountResolution:
    account: Account | None
    message: str = ""


def _normalise(value: str) -> str:
    return " ".join((value or "").replace("（", "(").replace("）", ")").split())


def resolve_account_input(value: str, accounts: dict[str, Account]) -> AccountResolution:
    """Accept a code, an exact display label, or a unique complete account name.

    Whitespace and Chinese/English parentheses are presentation differences.
    Missing words, different account names, and conflicting code/name pairs are
    not.  Callers can preserve unresolved input and show ``message`` to the user.
    """
    text = _normalise(value)
    if not text:
        return AccountResolution(None, "尚未选择科目。请填写科目编码、完整名称，或从下拉列表选择。")

    by_code = {str(account.code).strip(): account for account in accounts.values()
               if str(account.code).strip()}
    if text in by_code:
        return AccountResolution(by_code[text])

    # A display label has an explicit code.  Never ignore a wrong code and
    # silently select an account with the requested name instead.
    parts = text.split(" ", 1)
    looks_like_code = bool(re.fullmatch(r"[0-9][0-9A-Za-z._-]*", parts[0]))
    if len(parts) == 2 and (parts[0] in by_code or looks_like_code):
        code, name = parts
        account = by_code.get(code)
        if account is None:
            return AccountResolution(None, f"未找到科目编码“{code}”。请核对模板中的编码。")
        if _normalise(account.name) != name:
            return AccountResolution(
                None, f"编码 {code} 对应“{account.name}”，与填写的名称“{name}”不一致。"
                      "请重新选择带编码的完整科目。")
        return AccountResolution(account)

    matches = [account for account in by_code.values() if _normalise(account.name) == text]
    if len(matches) == 1:
        return AccountResolution(matches[0])
    if len(matches) > 1:
        codes = "、".join(sorted(account.code for account in matches))
        return AccountResolution(None, f"科目名称不唯一：“{text}”对应编码 {codes}。"
                                       "请从下拉列表选择带编码的具体科目。")
    if looks_like_code and len(parts) == 1:
        return AccountResolution(None, f"未找到科目编码“{text}”。请核对模板中的编码。")
    return AccountResolution(None, f"未找到“{text}”。请填写完整科目名称或准确编码，或从下拉列表选择。")
