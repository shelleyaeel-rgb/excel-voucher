"""One native form for the source choices that still need a user's decision."""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import uuid

from .wps_service import binding_identity


KEEP_SOURCE = '保留不更新'

MAPPING_ANSWER_END = '按这些对应关系继续本次任务，其他已确认的筛选和处理规则保持不变。'


def mapping_answer_text(sheet, selections):
    """Readable answer; explanatory model notes are not re-sent as user intent."""
    lines = [f'{index}. “{target}” ← “{source}”'
             for index, (target, source) in enumerate(selections, 1)]
    return (f'工作表“{sheet}”的全部列对应已确认：\n' + '\n'.join(lines)
            + '\n' + MAPPING_ANSWER_END)


def mapping_tool_definition():
    fields = {
        "document_url": {"type": "string", "minLength": 1, "maxLength": 300},
        "sheet": {"type": "string", "minLength": 1, "maxLength": 100},
        "question": {"type": "string", "minLength": 1, "maxLength": 500},
        "mappings": {
            "type": "array", "minItems": 1, "maxItems": 30,
            "items": {
                "type": "object",
                "properties": {
                    "target_column": {"type": "string", "minLength": 1, "maxLength": 200},
                    "source_options": {"type": "array", "minItems": 1, "maxItems": 300,
                        "items": {"type": "string", "minLength": 1, "maxLength": 200}},
                    "selected_source": {"type": ["string", "null"], "maxLength": 200},
                    "note": {"type": "string", "maxLength": 500},
                },
                "required": ["target_column", "source_options", "selected_source", "note"],
                "additionalProperties": False,
            },
        },
    }
    return {
        "type": "function", "name": "wps_confirm_mapping", "strict": True,
        "description": (
            "每次更新都必须先read_table读取真实目标列，再调用此工具一次列出全部目标，等待用户逐行选择来源列。"
            "每行source_options应列出实际读取来源表所得的全部可选列，多文件多工作表用明确标签区分，不能只放推荐列。"
            "selected_source必须null；推荐对应、正负拆分、日期转换写入note，来源字段仅用实际列标签。"
            "软件自动加入保留不更新选项。用户已在聊天中描述对应也不能跳过。本工具不写入，完整原生表单确认后才能生成修改计划。"
        ),
        "parameters": {"type": "object", "properties": fields,
                       "required": list(fields), "additionalProperties": False},
    }


def _text(value, maximum, *, blank=False):
    return (isinstance(value, str) and len(value) <= maximum
            and (blank or bool(value.strip()))
            and not any(ord(character) < 32 for character in value))


def validate_mappings(mappings):
    """Validate the whole form before it can become an active question."""
    if not isinstance(mappings, list) or not 1 <= len(mappings) <= 30:
        raise ValueError("请一次提供 1 到 30 个目标列的对应关系。")
    cleaned = []
    targets = set()
    for item in mappings:
        if (not isinstance(item, dict)
                or set(item) != {"target_column", "source_options", "selected_source", "note"}
                or not _text(item["target_column"], 200)
                or not _text(item["note"], 500, blank=True)
                or not isinstance(item["source_options"], list)
                or not 1 <= len(item["source_options"]) <= 301
                or sum(option != KEEP_SOURCE for option in item["source_options"]) > 300
                or any(not _text(option, 200) for option in item["source_options"])):
            raise ValueError("列对应信息不完整，请提供目标列、来源候选和处理说明。")
        target = item["target_column"].strip()
        options = [option.strip() for option in item["source_options"]]
        selected = item["selected_source"]
        if target in targets or len(set(options)) != len(options):
            raise ValueError("目标列和同一列的来源候选不能重复。")
        if selected is not None:
            if not _text(selected, 200) or selected.strip() not in options:
                raise ValueError("预选的来源必须属于这一列的候选，未确定时请留空。")
            selected = selected.strip()
        targets.add(target)
        cleaned.append({"target_column": target, "source_options": options,
                        "selected_source": None, "note": item["note"].strip()})
    return cleaned


def _fingerprint(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def _structure(snapshot, targets=None):
    result = {name: deepcopy(snapshot[name]) for name in ("document_url", "document_identity", "workbook_name",
        "sheet", "header_row", "row_start", "row_end", "columns", "expected_headers")}
    if targets is not None:
        result["columns"] = {name: column for name, column in result["columns"].items() if name in targets}
        selected = set(result["columns"].values())
        result["expected_headers"] = [h for h in result["expected_headers"] if h["column"] in selected
            or any(h.get("merge_range", {}).get("start_column", 0) <= c <= h.get("merge_range", {}).get("end_column", -1)
                   for c in selected)]
    result["expected_headers"] = sorted(result["expected_headers"], key=lambda h: h["column"])
    return result


class MappingToolsMixin:
    def _init_mapping_state(self):
        self._mapping_scope_id = uuid.uuid4().hex
        self._mapping_source_context = []
        self._pending_mapping = None
        self._confirmed_mapping = None

    def invalidate_mapping(self, reason="处理范围已变化，请重新选择列对应。"):
        self._mapping_scope_id = uuid.uuid4().hex
        self._pending_mapping = None
        self._confirmed_mapping = None
        self.questions = [q for q in self.questions if q.get("kind") != "mapping"]
        self._pending_clarification = bool(self.questions)

    def set_source_context(self, files):
        if (not isinstance(files, list) or len(files) > 100
                or any(not isinstance(f, dict) or set(f) != {"file_id", "name", "size"}
                    or not _text(f["file_id"], 300) or not _text(f["name"], 500)
                    or type(f["size"]) is not int or f["size"] < 0 for f in files)
                or len({f["file_id"] for f in files}) != len(files)):
            raise ValueError("来源附件记录不完整，无法确认列对应。")
        normalized = sorted((deepcopy(f) for f in files), key=lambda f: f["file_id"])
        if normalized != self._mapping_source_context:
            self.invalidate_mapping("来源附件已变化，请重新选择列对应。")
            self._mapping_source_context = normalized

    def _source_fingerprint(self):
        return _fingerprint(self._mapping_source_context)

    def get_pending_mapping_question(self):
        return deepcopy(self._pending_mapping["question"]) if self._pending_mapping else None

    def pending_mapping_questions(self):
        question = self.get_pending_mapping_question()
        return [question] if question else []

    def update_mapping_draft(self, mapping_id, selections):
        pending = self._pending_mapping
        if (pending is None or pending["mapping_id"] != mapping_id or not isinstance(selections, dict)
                or any(target not in pending["targets"] or not _text(source, 200, blank=True)
                       for target, source in selections.items())):
            raise ValueError("列对应表已失效，请使用当前表单。")
        pending["question"]["user_selections"] = deepcopy(selections)
        self._restore_pending_mapping()

    def _restore_pending_mapping(self):
        self.questions = [q for q in self.questions if q.get("kind") != "mapping"]
        if self._pending_mapping:
            self.questions.append(self.get_pending_mapping_question())
            self._pending_clarification = True

    def _mapping_snapshot(self, document_url, sheet, targets):
        binding = self._binding(document_url)
        candidates = [s for s in self.snapshots.values()
            if s["document_url"] == binding.share_url and s["sheet"] == sheet
            and s["document_identity"] == binding_identity(binding)
            and s.get("mapping_scope_id") == self._mapping_scope_id
            and s.get("source_fingerprint") == self._source_fingerprint()]
        if not candidates or not set(targets).issubset(candidates[-1]["columns"]):
            raise ValueError("请先读取本次实际目标列，再显示完整的列对应选择表。")
        return candidates[-1]

    def _confirm_mapping(self, document_url, sheet, question, mappings, *, cancel=None, on_status=None):
        binding = self._binding(document_url)
        if not _text(sheet, 100) or not _text(question, 500):
            raise ValueError("请提供工作表名称和一个明确的列对应问题。")
        cleaned = validate_mappings(mappings)
        targets = [m["target_column"] for m in cleaned]
        snapshot = self._mapping_snapshot(document_url, sheet, targets)
        for target in targets:
            column = snapshot["columns"][target]
            cells = [self._cells(row)[column] for row in snapshot["rows"]]
            if cells and all(cell["has_formula"] for cell in cells):
                raise ValueError(f"“{target}”整列为公式，会保留自动计算。请从待更新列和选择表中移除，不要作为来源映射。")
        structure = _structure(snapshot, targets)
        if self._pending_mapping:
            if (self._pending_mapping["structure"] != structure or self._pending_mapping["targets"] != targets):
                raise ValueError("已有尚未完成的列对应表，请等待用户选择；处理范围变化后需重新开始。")
            self._restore_pending_mapping()
            return {"ok": True, "status": "needs_user_input", **self.get_pending_mapping_question(),
                "note": "仍需用户完成全部逐行选择；不能自动确认或写入。"}
        if len(self.questions) >= 3:
            raise ValueError("请先等待用户回答已有问题。")
        for mapping in cleaned:
            mapping["source_options"] = [x for x in mapping["source_options"] if x != KEEP_SOURCE] + [KEEP_SOURCE]
        mapping_id = uuid.uuid4().hex
        entry = {"kind": "mapping", "mapping_id": mapping_id, "scope_id": self._mapping_scope_id,
            "document_url": binding.share_url, "sheet": sheet.strip(), "question": question.strip(), "options": [],
            "mappings": cleaned, "user_selections": {}}
        self._pending_mapping = dict(mapping_id=mapping_id, scope_id=self._mapping_scope_id,
            source_fingerprint=self._source_fingerprint(), structure=structure, targets=targets, question=entry)
        self._confirmed_mapping = None
        self._restore_pending_mapping()
        return {"ok": True, "status": "needs_user_input", **deepcopy(entry),
            "note": "请用户逐行选择来源或保留不更新，再一次确认。本轮不能生成可执行计划或写入。"}

    def _accept_mapping_answer(self, message):
        pending = self._pending_mapping
        if pending is None:
            return False
        question = pending["question"]
        prefix = f"针对“{question['question']}”："
        if not message.startswith(prefix):
            return False
        body = message[len(prefix):].strip()
        lines = body.splitlines()
        if len(lines) != len(question["mappings"]) + 2:
            return False
        selections = []
        for index, (mapping, line) in enumerate(zip(question["mappings"], lines[1:-1]), 1):
            start = f'{index}. “{mapping["target_column"]}” ← “'
            if not line.startswith(start) or not line.endswith('”'):
                return False
            source = line[len(start):-1]
            if source not in mapping["source_options"]:
                return False
            selections.append((mapping["target_column"], source))
        if body != mapping_answer_text(question["sheet"], selections):
            return False
        try:
            current = self._mapping_snapshot(question["document_url"], question["sheet"], pending["targets"])
        except ValueError:
            self.invalidate_mapping()
            return False
        if (pending["scope_id"] != self._mapping_scope_id or pending["source_fingerprint"] != self._source_fingerprint()
                or _structure(current, pending["targets"]) != pending["structure"]):
            self.invalidate_mapping()
            return False
        self._confirmed_mapping = dict(deepcopy(pending), selections=dict(selections))
        self._pending_mapping = None
        return True

    def _mapping_requirement(self, snapshot, columns, confirmation_id=None):
        confirmed = self._confirmed_mapping
        selected = {c["target_column"]: c["source_field"] for c in columns}
        valid = bool(confirmed and confirmed["scope_id"] == self._mapping_scope_id
            and confirmed["source_fingerprint"] == self._source_fingerprint()
            and snapshot.get("mapping_scope_id") == self._mapping_scope_id
            and snapshot.get("source_fingerprint") == self._source_fingerprint()
            and _structure(confirmed["structure"], set(selected)) == _structure(snapshot, set(selected))
            and selected == {k: v for k, v in confirmed["selections"].items() if v != KEEP_SOURCE}
            and (confirmation_id is None or confirmation_id == confirmed["mapping_id"]))
        if valid:
            try:
                binding = self._binding(snapshot["document_url"])
                valid = binding_identity(binding) == snapshot["document_identity"] and binding.workbook_name == snapshot["workbook_name"]
            except ValueError:
                valid = False
        if valid:
            return None
        return {"ok": False, "status": "needs_mapping", "required_action": "wps_confirm_mapping",
            "document_url": snapshot["document_url"], "sheet": snapshot["sheet"],
            "target_columns": list(selected), "error": "每次更新都需要先逐行选择全部目标列的来源；已有选择与本次文档、附件、范围或来源列不一致。",
            "can_execute": False}

    def _single_mapping_columns(self, target_column):
        selected = self._confirmed_mapping.get("selections", {}) if self._confirmed_mapping else {}
        return [{"target_column": target_column, "source_field": selected.get(target_column, "")}]

    def _mapping_summary(self):
        if self._confirmed_mapping:
            return {"status": "confirmed", "mapping_id": self._confirmed_mapping["mapping_id"],
                    "selections": deepcopy(self._confirmed_mapping["selections"])}
        if self._pending_mapping:
            return {"status": "pending", "mapping_id": self._pending_mapping["mapping_id"]}
        return {"status": "required"}
