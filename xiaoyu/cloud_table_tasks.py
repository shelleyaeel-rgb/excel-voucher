"""One durable task for a set of guarded, keyed column updates.

The single-column writer remains the only mutation path. The coordinator
checks the original read set before each column, accepting only its own
verified writes and recalculated implicit merge-header formula cells.
"""
from __future__ import annotations

from collections import Counter
from copy import deepcopy
import json
import uuid

from openpyxl.styles.numbers import is_date_format

from .wps_service import WpsError, binding_identity, header_matches, target_value_matches


def _date_system_matches(before, after, provider):
    if not is_date_format(before["number_format"]):
        return True
    old, current = before.get("date_system"), after.get("date_system")
    if old in ("1900", "1904"):
        return current == old
    # Legacy AirScript never exposed this metadata. Preserve that protocol's
    # previous comparison; personal-account XLSX reads must verify the epoch.
    return provider == "airscript" and old is None and current is None


def table_tool_definitions(function, names, scalar, max_rows):
    return [
        function("wps_prepare_table_update", "一次生成本次全部目标列的完整修改计划，不写入。先集中确定列对应、匹配和筛选规则，实际计算来源数据，再按columns顺序提供每条记录的values。只更新已有记录，保留公式；空白默认跳过，0有效。不要把一次多列任务拆成多次提问或多个计划。", {
            "snapshot_id": {"type": "string"}, "key_columns": names(),
            "columns": {"type": "array", "minItems": 1, "maxItems": 30, "items": {
                "type": "object", "properties": {
                    "target_column": {"type": "string", "minLength": 1, "maxLength": 200},
                    "source_field": {"type": "string", "minLength": 1, "maxLength": 300},
                    "allow_blank": {"type": "boolean"}},
                "required": ["target_column", "source_field", "allow_blank"], "additionalProperties": False}},
            "updates": {"type": "array", "minItems": 1, "maxItems": max_rows, "items": {
                "type": "object", "properties": {
                    "key_values": {"type": "array", "minItems": 1, "maxItems": 30, "items": scalar},
                    "values": {"type": "array", "minItems": 1, "maxItems": 30, "items": scalar},
                    "reason": {"type": "string", "maxLength": 500}},
                "required": ["key_values", "values", "reason"], "additionalProperties": False}},
            "summary": {"type": "string", "maxLength": 1000}}),
        function("wps_apply_table_update", "执行完整多列计划；软件自动逐批核对、写入和最终全列读回，只需调用一次。冲突或中断会停止后续，返回每列完成状态；同一plan_id绝不重复写入。", {"plan_id": {"type": "string"}}),
    ]


class TableTaskToolsMixin:
    def _prepare_table(self, snapshot_id, key_columns, columns, updates, summary, *, cancel=None, on_status=None):
        from .cloud_tools import MAX_SOURCE_ROWS, _scalar, _now
        snapshot = self.snapshots.get(snapshot_id)
        if snapshot is None:
            raise ValueError("表格快照不存在，请重新读取后生成完整计划。")
        if (not isinstance(columns, list) or not 1 <= len(columns) <= 30
                or any(not isinstance(c, dict) or set(c) != {"target_column", "source_field", "allow_blank"}
                       or not isinstance(c["target_column"], str) or c["target_column"] not in snapshot["columns"]
                       or not isinstance(c["source_field"], str) or not c["source_field"].strip()
                       or len(c["source_field"]) > 300 or type(c["allow_blank"]) is not bool for c in columns)
                or len({c["target_column"] for c in columns}) != len(columns)):
            raise ValueError("请一次提供不重复的目标列、来源字段及空白处理规则。")
        if (not isinstance(key_columns, list) or not key_columns or len(set(key_columns)) != len(key_columns)
                or any(k not in snapshot["columns"] for k in key_columns)
                or any(c["target_column"] in key_columns for c in columns)):
            raise ValueError("匹配列必须来自已读取的列，且不能同时修改匹配列。")
        if not isinstance(summary, str) or len(summary) > 1000:
            raise ValueError("请提供本次完整更新的简短说明。")
        if not isinstance(updates, list) or not 1 <= len(updates) <= MAX_SOURCE_ROWS:
            raise ValueError(f"本次完整计划需要1到{MAX_SOURCE_ROWS}条来源记录。")
        for item in updates:
            if (not isinstance(item, dict) or set(item) != {"key_values", "values", "reason"}
                    or not isinstance(item["key_values"], list) or len(item["key_values"]) != len(key_columns)
                    or not isinstance(item["values"], list) or len(item["values"]) != len(columns)
                    or not isinstance(item["reason"], str) or len(item["reason"]) > 500):
                raise ValueError("每条来源记录的匹配字段和更新值数量必须与列对应清单一致。")
            for value in item["key_values"] + item["values"]:
                _scalar(value)
        requirement = self._mapping_requirement(snapshot, columns)
        if requirement:
            return requirement
        confirmation_id = self._confirmed_mapping["mapping_id"]
        parent_id = uuid.uuid4().hex
        children = []
        for index, column in enumerate(columns):
            prepared = self._prepare(snapshot_id, key_columns, column["target_column"],
                [dict(key_values=item["key_values"], new_value=item["values"][index], reason=item["reason"]) for item in updates],
                column["allow_blank"], summary, cancel=cancel, on_status=on_status, _parent_task_id=parent_id)
            children.append(prepared["plan_id"])
        record = dict(schema=2, kind="table_update", task_id=parent_id,
            document_url=snapshot["document_url"], document_name=snapshot["workbook_name"],
            document_identity=deepcopy(snapshot["document_identity"]), sheet=snapshot["sheet"],
            created_at=_now(), updated_at=_now(), status="prepared", rule_summary=summary,
            header_row=snapshot["header_row"], row_start=snapshot["row_start"], row_end=snapshot["row_end"],
            expected_headers=deepcopy(snapshot["expected_headers"]),
            key_columns=[snapshot["columns"][name] for name in key_columns], target_column=None,
            baseline=deepcopy(snapshot), children=children, mappings=deepcopy(columns),
            mapping_confirmation_id=confirmation_id,
            needs_clarification=any(self.plans[c]["needs_clarification"] for c in children))
        self._summarize_table(record)
        self._save(record)
        self.plans[parent_id] = record
        self._session_plan_ids.add(parent_id)
        self._record_result(record)
        return dict(ok=True, **self._public(record), plan_id=parent_id,
            planned=sum(len(self.plans[c]["changes"]) for c in children),
            can_execute=self.allow_write and not record["needs_clarification"] and not self._pending_clarification,
            needs_clarification=record["needs_clarification"],
            preview=[dict(key_values=deepcopy(item["key_values"]), values=deepcopy(item["values"])) for item in updates[:30]],
            note="这是全部目标列的完整预览，尚未修改云文档。")

    def _table_children(self, record, *, disk=False):
        from .cloud_tools import _ID, _record_identity
        ids, mappings = record.get("children"), record.get("mappings")
        if (record.get("kind") != "table_update" or record.get("schema") != 2
                or not isinstance(ids, list) or not 1 <= len(ids) <= 30
                or any(not isinstance(i, str) or not _ID.fullmatch(i) for i in ids) or len(set(ids)) != len(ids)
                or not isinstance(mappings, list) or len(mappings) != len(ids)):
            raise ValueError("完整任务的列记录不完整，请先检查处理记录。")
        children = []
        for task_id, mapping in zip(ids, mappings):
            child = None if disk else self.plans.get(task_id)
            if child is None:
                path = self._path(task_id)
                if not path.is_file() or path.stat().st_size > 32 * 1024 * 1024:
                    raise ValueError("完整任务的子记录缺失，请先检查处理记录。")
                child = json.loads(path.read_text(encoding="utf-8"))
            if (not isinstance(child, dict) or child.get("task_id") != task_id
                    or child.get("parent_task_id") != record["task_id"] or child.get("kind") == "table_update"
                    or _record_identity(child) != _record_identity(record)
                    or child.get("document_name") != record.get("document_name") or child.get("sheet") != record.get("sheet")
                    or child.get("key_columns") != record.get("key_columns")
                    or child.get("expected_headers") != record.get("expected_headers")
                    or not isinstance(mapping, dict) or child.get("target_column_name") != mapping.get("target_column")
                    or not isinstance(mapping.get("source_field"), str)
                    or not isinstance(child.get("items"), list) or not isinstance(child.get("changes"), list)):
                raise ValueError("完整任务与子记录不一致，已停止核对。")
            children.append(child)
        if len({child["target_column"] for child in children}) != len(children):
            raise ValueError("完整任务含重复目标列。")
        return children

    def _summarize_table(self, record, children=None):
        from .cloud_tools import _key
        children = children if children is not None else self._table_children(record)
        columns, items = [], []
        good = {"verified", "unchanged", "blank_skipped"}
        for child, mapping in zip(children, record["mappings"]):
            self._summarize(child)
            counts = Counter(item["status"] for item in child["items"])
            state = child["status"]
            if state not in ("prepared", "running", "uncertain", "cancelled"):
                state = "completed" if all(i["status"] in good for i in child["items"]) else "partial"
            columns.append(dict(target_column=mapping["target_column"], source_field=mapping["source_field"], status=state,
                **{k: child[k] for k in ("updated", "unchanged", "skipped", "conflicts", "unknown")},
                pending=counts["planned"] + counts["not_submitted"],
                summary=f"更新 {child['updated']} 格；原值相同 {child['unchanged']} 格；按规则保留空值 {counts['blank_skipped']} 格；未完成 {sum(v for k,v in counts.items() if k not in good)} 格。"))
            items.extend(dict(item, target_column=mapping["target_column"]) for item in child["items"])
        record["items"] = items
        record["columns"] = columns
        self._summarize(record)
        record["pending"] = sum(c["pending"] for c in columns)
        record["planned"] = sum(i["status"] == "planned" for i in items)
        record["record_count"] = len({_key(i["key_values"]) for i in items if i["status"] in {"verified", "unchanged"}})
        record["summary"] = (f"完整任务共 {len(columns)} 列；已核对更新 {record['updated']} 格，原值相同 {record['unchanged']} 格；"
            f"跳过 {record['skipped']} 格，冲突 {record['conflicts']} 格，待核对 {record['unknown']} 格，未提交 {record['pending']} 格。")
        if record["status"] == "prepared":
            record["summary"] = f"完整预览：计划更新 {record['planned']} 格，尚未写入。" + record["summary"]

    def _table_read_index(self, binding, record, cancel=None):
        from .cloud_tools import _key
        columns = sorted(h["column"] for h in record["expected_headers"])
        rows = self._read_rows(binding, record["sheet"], record["header_row"], record["header_row"], columns, cancel)
        headers = self._cells(rows[0])
        if any(not header_matches(headers[h["column"]], h, record["header_row"]) for h in record["expected_headers"]):
            raise ValueError("云表表头或合并结构已变化，本次停止后续更新。")
        rows = self._read_rows(binding, record["sheet"], record["row_start"], record["row_end"], columns, cancel)
        found = {}
        for row in rows:
            cells = self._cells(row)
            try:
                key = _key([cells[c]["value"] for c in record["key_columns"]])
            except ValueError:
                continue
            found.setdefault(key, []).append(row)
        return found

    def _refresh_table_child(self, binding, record, child, cancel=None):
        from .cloud_tools import _key, _same
        current = self._table_read_index(binding, record, cancel)
        baseline = record["baseline"]
        original = {}
        for row in baseline["rows"]:
            cells = self._cells(row)
            try:
                key = _key([cells[c]["value"] for c in record["key_columns"]])
            except ValueError:
                continue
            original.setdefault(key, []).append(row)
        owned = {}
        touched = set()
        required = set()
        for member in self._table_children(record):
            for item in member["items"]:
                if item["status"] in ("planned", "unchanged", "verified"):
                    required.add(_key(item["key_values"]))
                if item["status"] == "verified":
                    key = _key(item["key_values"])
                    owned[(key, member["target_column"])] = "" if item["new_value"] is None else item["new_value"]
                    touched.add(key)
        implicit = {h["column"] for h in record["expected_headers"]} - set(baseline["columns"].values())
        for key in required:
            before_rows, now_rows = original.get(key, []), current.get(key, [])
            if len(before_rows) != 1 or len(now_rows) != 1:
                raise ValueError("匹配记录已移动出范围、缺失或出现重复，本次停止后续更新。")
            before, now = self._cells(before_rows[0]), self._cells(now_rows[0])
            for column, old in before.items():
                value = now[column]
                own_constant_formula = ((key, column) in owned and not old["has_formula"] and not value["has_formula"]
                    and _same(old["formula"], old["value"]) and _same(value["formula"], value["value"]))
                if not _date_system_matches(old, value, record["document_identity"]["provider"]):
                    raise ValueError("日期的计算基准发生变化或无法核对，本次停止后续更新。")
                if any(not _same(value.get(field), old.get(field)) for field in
                       ("has_formula", "merged", "number_format", "merge_range")) or (
                        not own_constant_formula and not _same(value["formula"], old["formula"])):
                    raise ValueError("相关单元格的公式、格式或合并结构发生变化，本次停止后续更新。")
                if (key, column) in owned:
                    matches = target_value_matches(value["value"], owned[(key, column)], old["number_format"])
                elif old["has_formula"] and column in implicit and key in touched:
                    # Only an implicit merge-header anchor is a display-only
                    # dependency. Explicit formula columns may be row filters.
                    matches = True
                else:
                    matches = _same(value["value"], old["value"])
                if not matches:
                    raise ValueError("相关记录已被其他操作修改，或筛选条件已变化；本次停止后续更新。")
        for change in child["changes"]:
            request = change["request"]
            row = current[_key(request["key_values"])][0]
            cells = self._cells(row)
            request["row"] = row["row"]
            request["guards"] = [dict(column=c, value=cells[c]["value"], formula=cells[c]["formula"]) for c in sorted(cells)]
        self._save(child)

    def _table_final_check(self, binding, record, children, cancel=None):
        from .cloud_tools import _now, _key, _same
        found = self._table_read_index(binding, record, cancel)
        originals = {}
        for row in record["baseline"]["rows"]:
            cells = self._cells(row)
            try:
                key = _key([cells[c]["value"] for c in record["key_columns"]])
            except ValueError:
                continue
            originals.setdefault(key, []).append(cells)
        current = dict(checked_at=_now(), checked=0, matched=0, changed=0, missing=0, duplicate=0, failed=False)
        for child in children:
            check = dict(checked_at=_now(), checked=0, matched=0, changed=0, missing=0, duplicate=0, failed=False)
            for item in child["items"]:
                if item["status"] not in ("verified", "unchanged"):
                    continue
                check["checked"] += 1
                key = _key(item["key_values"])
                matches = found.get(key, [])
                if len(matches) != 1:
                    check["missing" if not matches else "duplicate"] += 1
                    continue
                baseline = originals.get(key, [])
                if len(baseline) != 1:
                    raise ValueError("原始匹配快照不完整，不能确认完整任务结果。")
                old = baseline[0][child["target_column"]]
                target = self._cells(matches[0])[child["target_column"]]
                expected = "" if item["new_value"] is None else item["new_value"]
                value_matches = (target_value_matches(target["value"], expected, old["number_format"])
                    if item["status"] == "verified" else _same(target["value"], expected))
                if (value_matches and not target["has_formula"] and not target["merged"]
                        and target["number_format"] == old["number_format"]
                        and _date_system_matches(old, target, record["document_identity"]["provider"])):
                    check["matched"] += 1
                else:
                    check["changed"] += 1
            child["current_check"] = check
            for key in ("checked", "matched", "changed", "missing", "duplicate"):
                current[key] += check[key]
        record["current_check"] = current
        return current

    def _apply_table(self, plan_id, *, cancel=None, on_status=None):
        from .cloud_tools import _DOCUMENT_LOCK, _record_identity
        record = self.plans.get(plan_id)
        if record is None or record.get("kind") != "table_update" or plan_id not in self._session_plan_ids:
            raise ValueError("完整修改计划不存在，请先重新读取并生成；历史任务只能核对。")
        if record["status"] != "prepared":
            self._record_result(record)
            return dict(ok=self._table_result_ok(record), **self._public(record), note="已有执行记录，不重复写入。")
        requirement = self._mapping_requirement(record["baseline"], record["mappings"], record.get("mapping_confirmation_id"))
        if requirement:
            return requirement
        if not self.allow_write:
            return dict(ok=False, status="preview_only", plan_id=plan_id, error="当前只允许读取和预览，请明确要求执行更新。")
        if self._table_write_stopped:
            return dict(ok=False, status="stopped_for_turn", plan_id=plan_id, retryable=False,
                error="本轮完整更新已经停止，不能通过新计划继续写入。请先说明处理结果，等待用户下一条要求；仍可只读核对。")
        if self._pending_clarification or record["needs_clarification"]:
            return dict(ok=False, status="needs_user_input", plan_id=plan_id, error="还有列对应或重复匹配问题尚未明确，请先完成集中确认。")
        binding = self._binding(record["document_url"])
        if binding.workbook_name != record["document_name"] or binding_identity(binding) != _record_identity(record):
            raise ValueError("文档绑定已变化，请重新读取并生成完整计划。")
        with _DOCUMENT_LOCK:
            if self._unresolved(binding, plan_id):
                raise ValueError("这份文档有尚未核对的写入任务，请先核对，避免重复修改。")
            children = self._table_children(record)
            record["status"] = "running"
            record["last_action"] = "apply"
            self._save(record)
            self._record_result(record)
            self._active_table_task = plan_id
            try:
                for index, child in enumerate(children):
                    if cancel is not None and cancel.is_set():
                        record["status"] = "cancelled"
                        break
                    if on_status:
                        on_status(f"正在执行完整更新：第 {index + 1}/{len(children)} 列「{record['mappings'][index]['target_column']}」…")
                    self._refresh_table_child(binding, record, child, cancel)
                    self._apply(child["task_id"], cancel=cancel, on_status=on_status)
                    self._summarize_table(record, children)
                    self._save(record)
                    self._record_result(record)
                    if (child["status"] in ("uncertain", "cancelled", "running") or child.get("code")
                            or any(i["status"] not in ("verified", "unchanged", "blank_skipped") for i in child["items"])):
                        record["status"] = "uncertain" if child["unknown"] else "partial"
                        if child.get("error"):
                            record["error"] = child["error"]
                        break
            except (WpsError, ValueError, OSError) as exc:
                record["status"] = "uncertain" if any(c.get("unknown") or c["status"] == "running" for c in children) else "partial"
                record["error"] = str(exc) if not isinstance(exc, OSError) else "本机任务记录保存失败，已停止后续操作。"
                if isinstance(exc, WpsError):
                    self._record_failure("wps_apply_table_update", {"plan_id": plan_id}, exc, on_status)
                    record.update(code=exc.code, diagnostic=exc.diagnostic, retryable=False)
            finally:
                self._active_table_task = None
            for child in children:
                if child["status"] == "prepared":
                    child["status"] = "cancelled" if record["status"] == "cancelled" else "partial"
                    for item in child["items"]:
                        if item["status"] == "planned":
                            item["status"] = "not_submitted"
                    self._summarize(child)
                    self._save(child)
            self._summarize_table(record, children)
            if record["status"] == "running":
                record["status"] = "completed" if all(c["status"] == "completed" for c in record["columns"]) else "partial"
            try:
                self._table_final_check(binding, record, children)
                record["final_check"] = deepcopy(record["current_check"])
            except (WpsError, ValueError, OSError) as exc:
                record["current_check"] = dict(checked=0, matched=0, changed=0, missing=0, duplicate=0, failed=True)
                record["final_check"] = deepcopy(record["current_check"])
                record["error"] = "完整任务最终核对未完成，请核对处理记录；没有重复写入。"
                if isinstance(exc, WpsError):
                    self._record_failure("wps_apply_table_update", {"plan_id": plan_id}, exc, on_status)
            if record["status"] == "completed" and not self._table_result_ok(record):
                record["status"] = "partial"
            if not self._table_result_ok(record):
                record["retryable"] = False
                self._table_write_stopped = True
            self._save(record)
            self._record_result(record)
            self._last_completed = True
            self.invalidate_mapping("本次更新已结束，下次更新需要重新选择。")
            return dict(ok=self._table_result_ok(record), **self._public(record))

    @staticmethod
    def _table_result_ok(record):
        check = record.get("current_check", {})
        return (record.get("status") == "completed" and check.get("failed") is False
                and not any(check.get(k) for k in ("changed", "missing", "duplicate")))

    def _check_table_task(self, record, *, cancel=None, on_status=None):
        from .cloud_tools import _DOCUMENT_LOCK, _record_identity
        with _DOCUMENT_LOCK:
            record["last_action"] = "check"
            children = self._table_children(record, disk=True)
            if record["status"] == "prepared":
                self._summarize_table(record, children)
                self._record_result(record)
                return dict(ok=False, **self._public(record), note="这是历史预览，尚未执行；本次未写入，也未恢复写入权限。")
            for child in children:
                if child["status"] != "prepared":
                    self._check_task(child["task_id"], cancel=cancel, on_status=on_status)
            children = self._table_children(record, disk=True)
            identity = _record_identity(record)
            candidates = [b for b in self.bindings.values() if b.workbook_name == record["document_name"]
                and all(binding_identity(b)[k] == identity[k] for k in ("provider", "file_id"))]
            if not candidates or len({b.account_id for b in candidates}) != 1:
                raise ValueError("任务的文档身份与当前绑定不一致，请先核对连接。")
            binding = next((b for b in candidates if b.share_url == record["document_url"]), candidates[0])
            for child in children:
                if child["status"] == "prepared":
                    child["status"] = "partial"
                    for item in child["items"]:
                        if item["status"] == "planned":
                            item["status"] = "not_submitted"
                    self._summarize(child)
                    self._save(child)
            self._summarize_table(record, children)
            record["status"] = ("uncertain" if record["unknown"] else
                "completed" if all(c["status"] == "completed" for c in record["columns"]) else "partial")
            try:
                check = self._table_final_check(binding, record, children, cancel)
                if check["changed"] or check["missing"] or check["duplicate"]:
                    record["summary"] += " 历史完成记录已保留；当前云端内容与已完成结果有差异。"
                else:
                    record["summary"] += f" 当前只读核对一致 {check['matched']} 格。"
            except (WpsError, ValueError, OSError):
                record["current_check"] = dict(checked=0, matched=0, changed=0, missing=0, duplicate=0, failed=True)
                record["summary"] += " 本次未能完成当前内容核对，未重试任何写入。"
            if self._table_result_ok(record):
                for key in ("error", "code", "diagnostic", "retryable"):
                    record.pop(key, None)
            self._save(record)
            if record["task_id"] in self.plans and self.plans[record["task_id"]]["status"] != "prepared":
                self.plans[record["task_id"]] = record
            self._record_result(record)
            return dict(ok=self._table_result_ok(record), **self._public(record), note="本次只读核对全部目标列，没有重试或续写。")
