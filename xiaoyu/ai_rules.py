"""User-editable finance-assistant preferences, separate from credentials."""
from __future__ import annotations

import threading
from pathlib import Path

from .ai_settings import _atomic_write, _read_json
from .config import data_dir


MAX_RULES_CHARS = 4000
_LOCK = threading.RLock()


def validate_rules(rules: str) -> None:
    if not isinstance(rules, str):
        raise ValueError("长期规则应为文字，请重新填写。")
    if len(rules) > MAX_RULES_CHARS:
        raise ValueError(f"长期规则最多 {MAX_RULES_CHARS} 字，请精简后保存。")


def load_rules(path: Path | None = None) -> str:
    try:
        with _LOCK:
            target = Path(path) if path is not None else data_dir() / "finance_ai_rules.json"
            value = _read_json(target, kind="长期规则")
    except (OSError, UnicodeError) as exc:
        raise ValueError("无法读取长期规则，请检查文件是否可读。") from exc
    if value is None:
        return ""
    rules = value.get("rules")
    try:
        validate_rules(rules)
    except ValueError as exc:
        raise ValueError("长期规则文件已损坏，原文件已保留，请重新设置或恢复备份。") from exc
    return rules


def save_rules(rules: str, path: Path | None = None) -> None:
    validate_rules(rules)
    try:
        with _LOCK:
            target = Path(path) if path is not None else data_dir() / "finance_ai_rules.json"
            _atomic_write(target, {"version": 1, "rules": rules})
    except (OSError, ValueError) as exc:
        raise ValueError("无法保存长期规则，请检查保存目录是否可写。") from exc
