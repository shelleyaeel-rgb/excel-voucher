import json

import pytest

from xiaoyu import ai_rules, ai_settings
from xiaoyu.ai_rules import MAX_RULES_CHARS, load_rules, save_rules, validate_rules


def test_missing_rules_default_to_empty_without_creating_file(tmp_path):
    target = tmp_path / "finance_ai_rules.json"
    assert load_rules(target) == ""
    assert not target.exists()


def test_default_rules_path_is_separate_from_other_settings(tmp_path, monkeypatch):
    monkeypatch.setattr(ai_rules, "data_dir", lambda: tmp_path)
    unrelated = tmp_path / "finance_ai.json"
    unrelated.write_text("unrelated settings must remain untouched", encoding="utf-8")
    save_rules("金额保留两位小数。")
    target = tmp_path / "finance_ai_rules.json"
    assert json.loads(target.read_text(encoding="utf-8")) == {
        "version": 1, "rules": "金额保留两位小数。"}
    assert load_rules() == "金额保留两位小数。"
    assert unrelated.read_text(encoding="utf-8") == "unrelated settings must remain untouched"


def test_rules_roundtrip_and_clear_persist(tmp_path):
    target = tmp_path / "data" / "finance_ai_rules.json"
    rules = "金额保留两位小数。\n汇总时列出部门小计。"
    save_rules(rules, target)
    assert load_rules(target) == rules
    save_rules("", target)
    assert load_rules(target) == ""
    assert json.loads(target.read_text(encoding="utf-8")) == {"version": 1, "rules": ""}


def test_maximum_rules_length_uses_characters(tmp_path):
    rules = "账" * MAX_RULES_CHARS
    validate_rules(rules)
    target = tmp_path / "rules.json"
    save_rules(rules, target)
    assert load_rules(target) == rules


@pytest.mark.parametrize("rules", [None, 1, True, {}, [], "字" * (MAX_RULES_CHARS + 1)])
def test_invalid_rules_cannot_replace_previous_value(tmp_path, rules):
    target = tmp_path / "rules.json"
    save_rules("原规则", target)
    original = target.read_bytes()
    with pytest.raises(ValueError, match="长期规则"):
        save_rules(rules, target)
    assert target.read_bytes() == original
    assert load_rules(target) == "原规则"


def test_failed_replace_preserves_previous_rules_and_cleans_temp(tmp_path, monkeypatch):
    target = tmp_path / "rules.json"
    save_rules("按部门汇总", target)
    original = target.read_bytes()

    def fail(*args):
        raise PermissionError("synthetic file replacement failure")

    monkeypatch.setattr(ai_settings.os, "replace", fail)
    with pytest.raises(ValueError, match="无法保存长期规则"):
        save_rules("按人员汇总", target)
    assert target.read_bytes() == original
    assert load_rules(target) == "按部门汇总"
    assert list(tmp_path.iterdir()) == [target]


@pytest.mark.parametrize("content", [
    "{", "[]", '{"version":2,"rules":"old"}', '{"version":1}',
    '{"version":1,"rules":null}', '{"version":1,"rules":[]}',
    json.dumps({"version": 1, "rules": "字" * (MAX_RULES_CHARS + 1)}),
])
def test_corrupt_rules_are_reported_without_overwrite(tmp_path, content):
    target = tmp_path / "rules.json"
    target.write_text(content, encoding="utf-8")
    original = target.read_bytes()
    with pytest.raises(ValueError, match="长期规则"):
        load_rules(target)
    assert target.read_bytes() == original


def test_unreadable_rules_error_names_long_term_rules(tmp_path):
    target = tmp_path / "rules.json"
    target.mkdir()
    with pytest.raises(ValueError, match="长期规则"):
        load_rules(target)
    assert target.is_dir()
