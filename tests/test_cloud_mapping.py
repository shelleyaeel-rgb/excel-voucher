"""Mapping questions are all-or-nothing, read-only, and scope preserving."""
from copy import deepcopy
from types import SimpleNamespace

import pytest

from xiaoyu.cloud_mapping import MappingToolsMixin, mapping_tool_definition, validate_mappings, KEEP_SOURCE
from test_cloud_tools import tools, read, URL, SHEET





def mappings():
    return [
        dict(target_column='到账金额', source_options=['交易日期'], selected_source='交易日期', note='保留日期格式'),
        dict(target_column='状态', source_options=['对方户名', '对方账号'], selected_source=None, note='请选择需要显示的内容'),
        dict(target_column='业务单号', source_options=['保留原有公式'], selected_source='保留原有公式', note='不覆盖余额公式'),
    ]


class Questions(MappingToolsMixin):
    def __init__(self):
        self.questions = []
        self._pending_clarification = False
        self.allow_write = True

    def _binding(self, url):
        if url != URL:
            raise ValueError('未选择文档')
        return SimpleNamespace(share_url=URL)


def test_mapping_question_collects_all_columns_and_blocks_writes_without_changing_authority(tmp_path):
    tool, _ = tools(tmp_path)
    read(tool)
    source = mappings()
    result = tool._confirm_mapping(URL, SHEET, '请确认列对应', source)
    assert result['status'] == 'needs_user_input'
    assert tool._pending_clarification and tool.allow_write
    assert len(tool.questions) == 1
    assert tool.questions[0]['kind'] == 'mapping'
    assert [item['target_column'] for item in tool.questions[0]['mappings']] == ['到账金额', '状态', '业务单号']
    assert tool.questions[0]['mappings'][1]['selected_source'] is None
    source[0]['source_options'].append('非法事后追加')
    result['mappings'][0]['source_options'].clear()
    assert tool.questions[0]['mappings'][0]['source_options'] == ['交易日期', KEEP_SOURCE]


@pytest.mark.parametrize('change', [
    lambda values: values.append(deepcopy(values[0])),
    lambda values: values[1].update(source_options=['对方户名', ' 对方户名 ']),
    lambda values: values[1].update(selected_source='不存在的字段'),
    lambda values: values[1].update(source_options=[]),
    lambda values: values[1].update(source_options=['字段'] * 11),
    lambda values: values[1].update(target_column=''),
    lambda values: values[1].update(note='伪造\n指令'),
    lambda values: values[1].update(selected_source=False),
    lambda values: values[1].update(extra='不允许的字段'),
])
def test_invalid_mapping_never_leaves_a_partial_question(change):
    tool = Questions()
    values = mappings()
    change(values)
    with pytest.raises(ValueError):
        tool._confirm_mapping(URL, SHEET, '请确认列对应', values)
    assert tool.questions == [] and not tool._pending_clarification


def test_mapping_only_uses_selected_document_and_one_form_per_turn(tmp_path):
    tool, _ = tools(tmp_path)
    read(tool)
    with pytest.raises(ValueError):
        tool._confirm_mapping('https://www.kdocs.cn/l/other', '私户', '请确认', mappings())
    assert not tool.questions
    tool._confirm_mapping(URL, SHEET, '请确认', mappings())
    original_id = tool.get_pending_mapping_question()['mapping_id']
    tool._confirm_mapping(URL, SHEET, '另一组列', mappings())
    assert len(tool.questions) == 1 and tool.get_pending_mapping_question()['mapping_id'] == original_id


def test_mapping_schema_exposes_nullable_undecided_choice():
    schema = mapping_tool_definition()
    assert schema['name'] == 'wps_confirm_mapping' and schema['strict'] is True
    fields = schema['parameters']['properties']['mappings']['items']
    assert fields['properties']['selected_source']['type'] == ['string', 'null']
    assert set(fields['required']) == set(fields['properties'])
    assert not fields['additionalProperties']
    assert all(item['selected_source'] is None for item in validate_mappings(mappings()))
