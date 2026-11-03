import pytest

from xiaoyu.accounts import AccountResolution, resolve_account_input
from xiaoyu.models import Account


@pytest.fixture
def accounts():
    return {
        "2211": Account("2211", "应付职工薪酬"),
        "2241004": Account("2241004", "其他应付款-社保（个人）"),
        "5602014": Account("5602014", "管理费用-工资"),
        "6601001": Account("6601001", "销售费用-推广 服务"),
    }


@pytest.mark.parametrize("value", ["2211", " 2211 ", "应付职工薪酬", "  应付职工薪酬  ",
                                   "2211  应付职工薪酬", "\t2211\t 应付职工薪酬\n"])
def test_screenshot_account_name_and_supported_code_forms_resolve(accounts, value):
    result = resolve_account_input(value, accounts)
    assert isinstance(result, AccountResolution)
    assert result.account is accounts["2211"]
    assert result.message == ""


@pytest.mark.parametrize("value", ["其他应付款-社保(个人)", " 2241004  其他应付款-社保(个人) ",
                                   "其他应付款-社保（个人）"])
def test_chinese_and_english_parentheses_are_equivalent(accounts, value):
    assert resolve_account_input(value, accounts).account is accounts["2241004"]


def test_repeated_internal_whitespace_is_normalised_without_dropping_words(accounts):
    assert resolve_account_input("  销售费用-推广    服务  ", accounts).account is accounts["6601001"]
    assert resolve_account_input("销售费用-推广", accounts).account is None


@pytest.mark.parametrize("value", ["", "   \t ", "221", "9999", "应付", "职工薪酬", "不存在的科目"])
def test_unknown_partial_and_empty_input_never_guesses(accounts, value):
    result = resolve_account_input(value, accounts)
    assert result.account is None
    assert result.message


def test_duplicate_complete_names_require_a_code(accounts):
    accounts["2211001"] = Account("2211001", "应付职工薪酬")
    unresolved = resolve_account_input("应付职工薪酬", accounts)
    assert unresolved.account is None
    assert "名称不唯一" in unresolved.message
    assert "2211" in unresolved.message and "2211001" in unresolved.message
    assert resolve_account_input("2211001  应付职工薪酬", accounts).account is accounts["2211001"]


@pytest.mark.parametrize("value", ["2211 管理费用-工资", "2211 职工薪酬", "5602014 应付职工薪酬"])
def test_code_and_name_conflict_does_not_resolve_either_side(accounts, value):
    result = resolve_account_input(value, accounts)
    assert result.account is None
    assert "不一致" in result.message


def test_unknown_display_code_cannot_fall_back_to_valid_name(accounts):
    result = resolve_account_input("9999 应付职工薪酬", accounts)
    assert result.account is None
    assert "9999" in result.message and "未找到科目编码" in result.message


def test_normalised_duplicate_names_remain_ambiguous(accounts):
    accounts["2241005"] = Account("2241005", "其他应付款-社保(个人)")
    result = resolve_account_input("其他应付款-社保（个人）", accounts)
    assert result.account is None
    assert "名称不唯一" in result.message
