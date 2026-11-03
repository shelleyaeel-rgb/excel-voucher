"""Column confirmation and source material review for the local Excel workflow."""
from __future__ import annotations

import re
from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QAbstractItemView, QCheckBox, QComboBox, QDialog, QDialogButtonBox,
    QFormLayout, QHBoxLayout, QHeaderView, QLabel, QLineEdit, QMessageBox, QPushButton,
    QScrollArea, QSizePolicy, QSpinBox, QTabWidget, QTableWidget,
    QTableWidgetItem, QTextEdit, QVBoxLayout, QWidget,
)

from .imports import (
    KIND_LABELS, SCHEMAS, MappingSpec, header_signature, suggest_header,
    suggest_mapping,
)


def _text(value):
    return "" if value is None else str(value)


def _label(text, name="", wrap=True):
    widget = QLabel(text)
    widget.setWordWrap(wrap)
    if name:
        widget.setObjectName(name)
    return widget


def _column_name(index):
    result = ""
    index += 1
    while index:
        index, letter = divmod(index - 1, 26)
        result = chr(65 + letter) + result
    return result


def _period_from_source(path, sheet, rows):
    text = " ".join([Path(path).stem, sheet] + [
        _text(cell) for row in rows[:12] for cell in row
    ])
    match = re.search(r"(?<!\d)(20\d{2})\s*[年./-]\s*(1[0-2]|0?[1-9])(?:月|\b)", text)
    if not match:
        match = re.search(r"(?<!\d)(20\d{2})(0[1-9]|1[0-2])(?!\d)", text)
    return f"{match[1]}-{int(match[2]):02d}" if match else ""


class _SheetMapping(QWidget):
    """One selected sheet, with source column indexes kept in combo item data."""

    changed = Signal()

    def __init__(self, workbook, kind, name, rows, profiles, parent=None):
        super().__init__(parent)
        self.workbook = workbook
        self.kind = kind
        self.sheet_name = name
        self.rows = rows
        self._profiles = profiles
        self.columns = {}
        self._rebuilding = False
        self._matched_profile = None
        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 8, 10, 8)
        layout.setSpacing(6)
        top = QHBoxLayout()
        self.include = QCheckBox("导入这个工作表")
        top.addWidget(self.include)
        top.addStretch()
        top.addWidget(QLabel("表头在第"))
        self.header_row = QSpinBox()
        self.header_row.setRange(1, max(1, len(rows)))
        self.header_row.setSuffix(" 行")
        self.header_row.setValue(suggest_header(rows, kind) + 1 if rows else 1)
        top.addWidget(self.header_row)
        layout.addLayout(top)
        self.profile_hint = _label("", "muted")
        layout.addWidget(self.profile_hint)
        self.preview = QTableWidget()
        self.preview.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.preview.setMinimumHeight(85)
        self.preview.setMaximumHeight(105)
        self.preview.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        self.preview.horizontalHeader().setDefaultSectionSize(132)
        layout.addWidget(self.preview)

        self.mapping_table = QTableWidget(len(SCHEMAS[kind]), 2)
        self.mapping_table.setHorizontalHeaderLabels(["软件中的含义", "对应原 Excel 的哪一列"])
        self.mapping_table.verticalHeader().hide()
        self.mapping_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.mapping_table.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self.mapping_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        self.mapping_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.mapping_table.setMinimumHeight(130)
        for index, field in enumerate(SCHEMAS[kind]):
            label = field.label + (" *" if field.required else "")
            self.mapping_table.setItem(index, 0, QTableWidgetItem(label))
            combo = QComboBox()
            combo.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
            combo.setMinimumContentsLength(15)
            combo.currentIndexChanged.connect(self.changed)
            self.columns[field.key] = combo
            self.mapping_table.setCellWidget(index, 1, combo)
            self.mapping_table.setRowHeight(index, 42)
        layout.addWidget(self.mapping_table, 1)

        defaults_layout = QFormLayout()
        self.period = None
        self.direction = None
        self.amount_sign = None
        self.subtype = None
        if kind == "payroll":
            self.period = QLineEdit(_period_from_source(workbook.path, name, rows))
            self.period.setPlaceholderText("例如 2026-10；有年月列时优先读取该列")
            self.period.textChanged.connect(self.changed)
            defaults_layout.addRow("没有年月列时，所属年月", self.period)
        if kind in ("invoice", "bank"):
            self.direction = QComboBox()
            if kind == "invoice":
                for label, value in (("暂不确定", ""), ("收到的发票", "收到"), ("开出的发票", "开出")):
                    self.direction.addItem(label, value)
                caption = "没有方向列时，发票属于"
            else:
                for label, value in (("从收入／支出列或收支方向列读取", ""), ("本表全部是收入", "收入"), ("本表全部是支出", "支出")):
                    self.direction.addItem(label, value)
                caption = "单一金额列的收支方向"
            self.direction.currentIndexChanged.connect(self.changed)
            defaults_layout.addRow(caption, self.direction)
        if kind == "bank":
            self.amount_sign = QComboBox()
            for label, value in (
                ("遇到负金额先列为待核对", ""),
                ("按收支方向取金额绝对值", "absolute"),
                ("保留正负，负数作为冲回", "signed"),
            ):
                self.amount_sign.addItem(label, value)
            self.amount_sign.setToolTip(
                "仅用于单一金额列。例如支出写成 -100，若它表示实际付款 100 元，应选择取绝对值；"
                "若负数表示原付款的退款或冲回，选择保留正负。没有确认时会列为待核对。"
            )
            self.amount_sign.currentIndexChanged.connect(self.changed)
            defaults_layout.addRow("单一金额列遇到负数时", self.amount_sign)
        if kind == "logistics":
            self.subtype = QComboBox()
            for label in ("暂不确定", "商品收发货", "运输与运费"):
                self.subtype.addItem(label, "" if label == "暂不确定" else label)
            self.subtype.currentIndexChanged.connect(self.changed)
            defaults_layout.addRow("没有类别列时，物流资料属于", self.subtype)
        layout.addLayout(defaults_layout)
        memory = QHBoxLayout()
        self.remember = QCheckBox("记住这个表格格式")
        self.remember.setChecked(True)
        self.profile_name = QLineEdit(f"{KIND_LABELS[kind]} · {name}")
        self.profile_name.setPlaceholderText("例如：某银行流水格式")
        memory.addWidget(self.remember)
        memory.addWidget(self.profile_name, 1)
        layout.addLayout(memory)
        # Keep defaults such as payroll month visible before the long mapping table.
        layout.removeItem(defaults_layout)
        layout.insertLayout(2, defaults_layout)
        layout.removeItem(memory)
        layout.insertLayout(3, memory)
        self.remember.toggled.connect(self.profile_name.setEnabled)
        self.include.toggled.connect(self._enabled_changed)
        self.header_row.valueChanged.connect(self._rebuild)
        self.changed.connect(self._sync_amount_sign)
        self._rebuild()
        # A generic date/amount column can also belong to another kind of business.
        self.include.setChecked(self._auto_selected())
        self._enabled_changed()

    def _auto_selected(self):
        mapping = self.mapping()
        if self._matched_profile and mapping:
            return True
        if self.kind == "bank":
            other_invoice = suggest_mapping(self.headers(), "invoice")
            other_logistics = suggest_mapping(self.headers(), "logistics")
            if "invoice_no" in other_invoice or "tracking_no" in other_logistics:
                return False
            return "date" in mapping and bool({"income", "expense", "amount"} & mapping.keys())
        if self.kind == "payroll":
            return {"name", "gross", "net"} <= mapping.keys()
        if self.kind == "logistics":
            return bool({"tracking_no", "business_no"} & mapping.keys()) or {"item", "quantity"} <= mapping.keys()
        return "invoice_no" in mapping or (
            bool({"buyer", "seller"} & mapping.keys()) and bool({"tax", "total"} & mapping.keys())
        )

    def headers(self):
        index = self.header_row.value() - 1
        return self.rows[index] if 0 <= index < len(self.rows) else []

    def _enabled_changed(self, *_):
        enabled = self.include.isChecked()
        for widget in (self.header_row, self.mapping_table, self.period,
                       self.direction, self.amount_sign, self.subtype, self.remember, self.profile_name):
            if widget is not None:
                widget.setEnabled(enabled)
        self.profile_name.setEnabled(enabled and self.remember.isChecked())
        self.changed.emit()

    def _sync_amount_sign(self):
        if self.amount_sign is not None:
            mapping = self.mapping()
            self.amount_sign.setEnabled(
                self.include.isChecked() and "amount" in mapping
                and not ({"income", "expense"} & mapping.keys())
            )

    def _rebuild(self, *_):
        self._rebuilding = True
        headers = self.headers()
        signature = header_signature(headers)
        matches = [p for p in self._profiles
                   if p.get("kind") == self.kind and p.get("signature") == signature]
        self._matched_profile = matches[-1] if matches else None
        mapping = (self._matched_profile.get("mapping", {}) if self._matched_profile
                   else suggest_mapping(headers, self.kind))
        if self._matched_profile:
            self.profile_name.setText(self._matched_profile.get("name", "已保存格式"))
            self.profile_hint.setText(f"已套用“{self.profile_name.text()}”，请确认列对应仍然正确。")
            defaults = self._matched_profile.get("defaults", {})
            for key, combo in (("direction", self.direction), ("amount_sign", self.amount_sign),
                               ("subtype", self.subtype)):
                if combo is not None:
                    selected = combo.findData(defaults.get(key, ""))
                    combo.setCurrentIndex(max(0, selected))
        else:
            self.profile_hint.setText("已自动建议列对应；没有把握的列会留空，请手动选择。")
        for field, combo in self.columns.items():
            combo.blockSignals(True)
            combo.clear()
            combo.addItem("不读取这一项", None)
            for index, value in enumerate(headers):
                caption = _text(value).strip() or "（没有列名）"
                combo.addItem(f"{_column_name(index)} 列 · {caption}", index)
            proposed = mapping.get(field)
            if isinstance(proposed, int) and not isinstance(proposed, bool):
                selected = combo.findData(proposed)
                combo.setCurrentIndex(max(0, selected))
            combo.blockSignals(False)
        preview_rows = self.rows[self.header_row.value():self.header_row.value() + 5]
        # Preview only; mapping offers every source column, including columns beyond 40.
        width = min(40, max([len(headers)] + [len(row) for row in preview_rows]))
        self.preview.setColumnCount(width)
        self.preview.setRowCount(len(preview_rows))
        self.preview.setHorizontalHeaderLabels([
            f"{_column_name(i)} · {_text(headers[i]) if i < len(headers) else ''}"
            for i in range(width)
        ])
        self.preview.setVerticalHeaderLabels([
            str(self.header_row.value() + index + 1) for index in range(len(preview_rows))
        ])
        for row_index, row in enumerate(preview_rows):
            for col in range(width):
                value = _text(row[col]) if col < len(row) else ""
                item = QTableWidgetItem(value)
                item.setToolTip(value)
                self.preview.setItem(row_index, col, item)
        self._rebuilding = False
        self.changed.emit()

    def mapping(self):
        return {key: combo.currentData() for key, combo in self.columns.items()
                if combo.currentData() is not None}

    def defaults(self):
        values = {}
        if self.period is not None and self.period.text().strip():
            values["period"] = self.period.text().strip()
        for key, combo in (("direction", self.direction), ("subtype", self.subtype)):
            if combo is not None and combo.currentData():
                values[key] = combo.currentData()
        if self.amount_sign is not None and self.amount_sign.currentData():
            mapping = self.mapping()
            if "amount" in mapping and not ({"income", "expense"} & mapping.keys()):
                values["amount_sign"] = self.amount_sign.currentData()
        return values

    def validation_error(self):
        if not self.include.isChecked():
            return ""
        mapping = self.mapping()
        if not self.rows or not mapping:
            return "至少选择一项要读取的列。"
        if len(set(mapping.values())) != len(mapping):
            return "同一列被指定了两种含义，请保留其中一种，避免重复计算。"
        if self.kind == "bank":
            if "date" not in mapping:
                return "请选择交易日期对应的列。"
            if "amount" in mapping and {"income", "expense"} & mapping.keys():
                return "收入／支出分列与单一金额列只能选择一种方式，请将不用的金额项设为“不读取这一项”。"
            if not ({"income", "expense"} & mapping.keys()):
                if "amount" not in mapping:
                    return "请选择收入、支出列，或金额列。"
                if "direction" not in mapping and not self.defaults().get("direction"):
                    return "只有金额列时，还需要收支方向列，或确认本表全部是收入／支出。"
        if self.kind == "payroll":
            missing = {"name": "姓名", "gross": "应发工资", "net": "实发工资"}
            required = [label for key, label in missing.items() if key not in mapping]
            if required:
                return f"请指定{'、'.join(required)}对应的列。"
            period = self.defaults().get("period", "")
            if "period" not in mapping and not re.fullmatch(r"20\d{2}-(0[1-9]|1[0-2])", period):
                return "请指定所属年月列，或填写年月，例如 2026-10。"
            if period and not re.fullmatch(r"20\d{2}-(0[1-9]|1[0-2])", period):
                return "所属年月请填写为 2026-10 这样的格式。"
        return ""

    def spec(self):
        return MappingSpec(self.kind, self.sheet_name, self.header_row.value() - 1,
                           self.mapping(), self.defaults())

    def profile(self):
        # A source month's value must never leak into the next month's payroll.
        defaults = {key: value for key, value in self.defaults().items() if key != "period"}
        return {"kind": self.kind, "signature": header_signature(self.headers()),
                "name": self.profile_name.text().strip() or f"{KIND_LABELS[self.kind]} · {self.sheet_name}",
                "mapping": self.mapping(), "defaults": defaults}


class MappingDialog(QDialog):
    """Ask the user to confirm each included sheet before parsing any records."""

    def __init__(self, workbook, kind, profiles=None, parent=None):
        super().__init__(parent)
        self.setWindowTitle(f"确认列对应 · {KIND_LABELS[kind]}")
        self.resize(940, 690)
        self.setMinimumSize(740, 520)
        self.specs = []
        self.profiles = []
        self.editors = []
        self.workbook = workbook
        self.kind = kind
        layout = QVBoxLayout(self)
        layout.setSpacing(10)
        layout.addWidget(_label(Path(workbook.path).name, "sectionTitle"))
        layout.addWidget(_label("请选择要导入的工作表。自动识别只提供建议，确认后才会读取为业务资料。", "muted"))
        self.tabs = QTabWidget()
        self.tabs.setUsesScrollButtons(True)
        self.tabs.setDocumentMode(True)
        layout.addWidget(self.tabs, 1)
        for name, rows in workbook.sheets:
            editor = _SheetMapping(workbook, kind, name, rows, profiles or [], self)
            scroll = QScrollArea()
            scroll.setWidgetResizable(True)
            scroll.setWidget(editor)
            self.tabs.addTab(scroll, name)
            self.editors.append(editor)
            editor.changed.connect(self._validate)
        self.validation = _label("", "notice")
        layout.addWidget(self.validation)
        self.buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        self.confirm_button = self.buttons.button(QDialogButtonBox.StandardButton.Ok)
        self.confirm_button.setText("确认对应并导入")
        self.confirm_button.setObjectName("primary")
        self.buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("取消本次导入")
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        layout.addWidget(self.buttons)
        self._validate()

    def _validate(self, *_):
        selected = [e for e in self.editors if e.include.isChecked()]
        errors = [f"{e.sheet_name}：{e.validation_error()}" for e in selected if e.validation_error()]
        if not selected:
            errors = ["请至少勾选一个工作表，并确认列对应。"]
        self.confirm_button.setEnabled(not errors)
        self.validation.setText(errors[0] if errors else f"将导入 {len(selected)} 个工作表。缺少的业务信息会在资料核对中提示。")
        self.validation.setProperty("tone", "error" if errors else "success")
        self.validation.style().unpolish(self.validation)
        self.validation.style().polish(self.validation)
        return not errors

    def accept(self):
        if not self._validate():
            return
        selected = [e for e in self.editors if e.include.isChecked()]
        self.specs = [e.spec() for e in selected]
        self.profiles = [e.profile() for e in selected if e.remember.isChecked()]
        super().accept()


class RecordsPage(QWidget):
    """Review imported source material independently from voucher generation."""

    exportRequested = Signal()
    generateRequested = Signal()
    removeRequested = Signal(list)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.records = []
        self.issues = []
        self._visible_records = []
        self._busy = False
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 3, 0, 3)
        layout.setSpacing(10)
        layout.addWidget(_label("资料核对", "sectionTitle"))
        layout.addWidget(_label("这里查看原始业务资料；导入或导出资料不代表已经记账。双击一行可查看原表内容。", "muted"))
        filters = QHBoxLayout()
        self.kind_filter = QComboBox()
        self.kind_filter.addItem("全部资料", "")
        for key, label in KIND_LABELS.items():
            self.kind_filter.addItem(label, key)
        self.period_filter = QComboBox()
        self.period_filter.addItem("全部月份", "")
        self.search = QLineEdit()
        self.search.setPlaceholderText("搜索单号、单位、商品或来源文件")
        self.review_only = QCheckBox("仅看待核对")
        filters.addWidget(self.kind_filter)
        filters.addWidget(self.period_filter)
        filters.addWidget(self.search, 1)
        filters.addWidget(self.review_only)
        layout.addLayout(filters)
        self.statistics = _label("尚未导入资料", "notice")
        layout.addWidget(self.statistics)
        self.table = QTableWidget()
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.table.setAlternatingRowColors(True)
        self.table.verticalHeader().hide()
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        self.table.horizontalHeader().setDefaultSectionSize(145)
        self.table.cellDoubleClicked.connect(self._show_detail)
        self.table.itemSelectionChanged.connect(self._selection_changed)
        layout.addWidget(self.table, 1)
        self.batch_issues = QTextEdit()
        self.batch_issues.setReadOnly(True)
        self.batch_issues.setMaximumHeight(80)
        self.batch_issues.hide()
        layout.addWidget(self.batch_issues)
        footer = QHBoxLayout()
        self.export_button = QPushButton("导出全部资料清单")
        self.export_button.clicked.connect(self.exportRequested)
        self.remove_button = QPushButton("移除所选资料")
        self.remove_button.clicked.connect(self._remove_selected)
        self.generate_button = QPushButton("用已保存的银行／工资生成凭证")
        self.generate_button.setObjectName("primary")
        self.generate_button.clicked.connect(self.generateRequested)
        footer.addWidget(self.export_button)
        footer.addWidget(self.remove_button)
        footer.addStretch()
        footer.addWidget(self.generate_button)
        layout.addLayout(footer)
        layout.addWidget(_label("筛选只影响查看。清单导出包含当前公司全部资料；生成凭证使用当前公司全部已保存银行、工资，再在凭证页选择月份。校验错误的记录不会参与生成。", "small"))
        self.kind_filter.currentIndexChanged.connect(self._render)
        self.period_filter.currentIndexChanged.connect(self._render)
        self.search.textChanged.connect(self._render)
        self.review_only.toggled.connect(self._render)
        self.set_records([])

    @staticmethod
    def _month(record):
        value = record.values.get("period") or record.values.get("date") or ""
        match = re.match(r"^(20\d{2})-(0[1-9]|1[0-2])", str(value))
        return match[0] if match else ""

    @staticmethod
    def _needs_review(record):
        return any(issue.severity in ("warning", "error") for issue in record.issues)

    def set_records(self, records, issues=None):
        self.records = list(records)
        self.issues = list(issues or [])
        previous = self.period_filter.currentData()
        self.period_filter.blockSignals(True)
        self.period_filter.clear()
        self.period_filter.addItem("全部月份", "")
        for month in sorted({self._month(r) for r in self.records if self._month(r)}):
            self.period_filter.addItem(month, month)
        if any(not self._month(record) for record in self.records):
            self.period_filter.addItem("日期／月份待确认", "unknown")
        selected = self.period_filter.findData(previous)
        self.period_filter.setCurrentIndex(max(0, selected))
        self.period_filter.blockSignals(False)
        # Avoid repeating row issues already shown in the record's prompt column.
        row_messages = {(i.severity, i.message, i.source) for r in self.records for i in r.issues}
        batch_messages = [i for i in self.issues if (i.severity, i.message, i.source) not in row_messages]
        self.batch_issues.setPlainText("\n".join(
            f"{issue.message}" + (f"（{issue.source}）" if issue.source else "")
            for issue in batch_messages
        ))
        self.batch_issues.setVisible(bool(batch_messages))
        self._render()
        self.set_busy(self._busy)

    def selected_records(self):
        """Return highlighted rows, not all filtered rows or all saved records."""
        rows = sorted({index.row() for index in self.table.selectionModel().selectedRows()})
        return [self._visible_records[row] for row in rows if row < len(self._visible_records)]

    def _selection_changed(self):
        if hasattr(self, "remove_button"):
            self.remove_button.setEnabled(not self._busy and bool(self.selected_records()))

    def _remove_selected(self):
        records = self.selected_records()
        if not records or self._busy:
            return
        reply = QMessageBox.question(
            self, "移除资料", f"要移除选中的 {len(records)} 条资料吗？\n\n仅从本软件资料库移除，原 Excel 不变。需要时可以重新导入。",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if reply == QMessageBox.StandardButton.Yes:
            self.removeRequested.emit([record.id for record in records])

    def set_busy(self, busy):
        self._busy = bool(busy)
        self.export_button.setEnabled(bool(self.records) and not busy)
        self.generate_button.setEnabled(not busy and any(
            record.kind in ("bank", "payroll") and record.valid for record in self.records
        ))
        for widget in (self.kind_filter, self.period_filter, self.search, self.review_only, self.table):
            widget.setEnabled(not busy)
        self._selection_changed()

    def _render(self, *_):
        kind = self.kind_filter.currentData()
        period = self.period_filter.currentData()
        needle = self.search.text().strip().casefold()
        visible = []
        for record in self.records:
            if kind and record.kind != kind:
                continue
            if period == "unknown" and self._month(record):
                continue
            if period and period != "unknown" and self._month(record) != period:
                continue
            if self.review_only.isChecked() and not self._needs_review(record):
                continue
            haystack = " ".join([record.source] + [_text(v) for v in record.values.values()]).casefold()
            if needle and needle not in haystack:
                continue
            visible.append(record)
        self._visible_records = visible
        valid = sum(record.valid for record in self.records)
        review = sum(self._needs_review(record) for record in self.records)
        self.statistics.setText(
            f"全部 {len(self.records)} 条 · 基础校验通过 {valid} 条 · 待核对 {review} 条 · 当前显示 {len(visible)} 条"
            if self.records else "尚未导入资料。请先选择 Excel，并确认各列含义。"
        )
        if kind:
            fields = [(field.key, field.label) for field in SCHEMAS[kind]]
            headers = ["核对状态"] + [label for _, label in fields] + ["来源", "提示"]
        else:
            fields = []
            headers = ["资料类型", "日期／月份", "单号／姓名", "单位／商品", "核对状态", "来源", "提示"]
        self.table.setUpdatesEnabled(False)
        self.table.clear()
        self.table.setColumnCount(len(headers))
        self.table.setRowCount(len(visible))
        self.table.setHorizontalHeaderLabels(headers)
        for row_index, record in enumerate(visible):
            data = record.values
            status = "有错误，待核对" if not record.valid else ("待核对" if self._needs_review(record) else "基础校验通过")
            issues = "；".join(issue.message for issue in record.issues)
            if kind:
                values = [status] + [_text(data.get(key)) for key, _ in fields] + [record.source, issues]
            else:
                identifier = next((data.get(key) for key in ("invoice_no", "tracking_no", "business_no", "name") if data.get(key)), "")
                summary = " / ".join(_text(data.get(key)) for key in ("counterparty", "buyer", "seller", "item") if data.get(key))
                values = [KIND_LABELS.get(record.kind, record.kind), data.get("period") or data.get("date") or "",
                          identifier, summary, status, record.source, issues]
            for col, value in enumerate(values):
                item = QTableWidgetItem(_text(value))
                item.setToolTip(_text(value))
                if not record.valid:
                    item.setBackground(QColor("#FFF0E7"))
                self.table.setItem(row_index, col, item)
        self.table.setColumnWidth(len(headers) - 2, 280)
        self.table.setColumnWidth(len(headers) - 1, 360)
        self.table.setUpdatesEnabled(True)

    def _show_detail(self, row, _column=0):
        if row < 0 or row >= len(self._visible_records):
            return
        record = self._visible_records[row]
        dialog = QDialog(self)
        dialog.setWindowTitle("资料来源与原始内容")
        dialog.resize(720, 520)
        layout = QVBoxLayout(dialog)
        layout.addWidget(_label(record.source, "sectionTitle"))
        text = QTextEdit()
        text.setReadOnly(True)
        labels = {f.key: f.label for f in SCHEMAS.get(record.kind, [])}
        details = [f"{labels.get(key, key)}：{_text(value)}" for key, value in record.values.items()]
        details += ["", "原表本行内容（按列顺序）："]
        details += [f"{_column_name(index)} 列：{_text(value)}" for index, value in enumerate(record.raw)]
        if record.issues:
            details += ["", "待核对提示："] + [issue.message for issue in record.issues]
        text.setPlainText("\n".join(details))
        layout.addWidget(text, 1)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.button(QDialogButtonBox.StandardButton.Close).setText("关闭")
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)
        dialog.exec()
