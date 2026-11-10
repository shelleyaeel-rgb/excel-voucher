"""Native, local-first voucher workflow. Financial logic lives in service/engine."""
from __future__ import annotations

import copy
import os
import re
import sys
from datetime import datetime
from decimal import Decimal
from pathlib import Path

from PySide6.QtCore import QObject, QRunnable, QSize, Qt, QThreadPool, QTimer, QUrl, Signal
from PySide6.QtGui import QColor, QDesktopServices, QFont, QIcon, QPixmap
from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QCheckBox, QComboBox, QCompleter, QDoubleSpinBox,
    QFileDialog, QFormLayout, QFrame, QGridLayout, QHBoxLayout, QHeaderView,
    QLabel, QLineEdit, QListWidget, QListWidgetItem, QMainWindow, QProgressBar,
    QDialog, QInputDialog, QMessageBox,
    QPushButton, QScrollArea, QSizePolicy, QSpinBox, QSplitter, QStackedWidget,
    QTableWidget, QTableWidgetItem, QTreeWidget, QTreeWidgetItem, QVBoxLayout,
    QWidget,
)

from . import service
from .accounts import resolve_account_input
from .config import load_config, resource_path, save_config
from .models import Issue, Options
from .imports import ImportResult, inspect_workbook, parse_workbook
from .import_ui import MappingDialog, RecordsPage
from .material_store import MaterialStore, export_materials
from .theme import STYLESHEET
from .update_ui import UpdateController


EXCEL_FILTER = "Excel 表格 (*.xls *.xlsx)"


def _label(text: str, name: str = "", wrap: bool = False) -> QLabel:
    label = QLabel(text)
    if name:
        label.setObjectName(name)
    label.setWordWrap(wrap)
    return label


def _button(text: str, callback=None, primary: bool = False) -> QPushButton:
    button = QPushButton(text)
    button.setCursor(Qt.CursorShape.PointingHandCursor)
    if primary:
        button.setObjectName("primary")
    if callback:
        button.clicked.connect(callback)
    return button


def _restyle(widget: QWidget) -> None:
    widget.style().unpolish(widget)
    widget.style().polish(widget)


def _money(value) -> str:
    return f"{value:,.2f}"


class WorkerSignals(QObject):
    result = Signal(object)
    error = Signal(str)
    finished = Signal()


class Worker(QRunnable):
    def __init__(self, function):
        super().__init__()
        self.function = function
        self.signals = WorkerSignals()

    def run(self):
        try:
            self.signals.result.emit(self.function())
        except Exception as exc:
            self.signals.error.emit(str(exc) or type(exc).__name__)
        finally:
            self.signals.finished.emit()


class DropFrame(QFrame):
    filesDropped = Signal(list)
    rejected = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("dropArea")
        self.setAcceptDrops(True)

    def dragEnterEvent(self, event):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
            self.setProperty("dragging", True)
            _restyle(self)

    def dragLeaveEvent(self, event):
        self.setProperty("dragging", False)
        _restyle(self)
        event.accept()

    def dropEvent(self, event):
        self.setProperty("dragging", False)
        _restyle(self)
        files = [url.toLocalFile() for url in event.mimeData().urls() if url.isLocalFile()]
        valid = [p for p in files if Path(p).suffix.lower() in (".xls", ".xlsx") and Path(p).is_file()]
        if valid:
            self.filesDropped.emit(valid)
        if len(valid) != len(files) or not files:
            self.rejected.emit("请选择 .xls 或 .xlsx 格式的 Excel 文件。")
        event.acceptProposedAction()


class FilePanel(QFrame):
    changed = Signal()
    rejected = Signal(str)

    def __init__(self, title: str, description: str, parent=None):
        super().__init__(parent)
        self.setObjectName("card")
        self.paths: list[str] = []
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 18, 20, 18)
        layout.setSpacing(10)
        title_row = QHBoxLayout()
        title_row.addWidget(_label(title, "sectionTitle"))
        title_row.addStretch()
        title_row.addWidget(_button("选择文件", self.choose))
        layout.addLayout(title_row)
        layout.addWidget(_label(description, "muted", True))
        self.drop = DropFrame()
        self.drop.filesDropped.connect(self.add_paths)
        self.drop.rejected.connect(self.rejected)
        drop_layout = QVBoxLayout(self.drop)
        drop_layout.setContentsMargins(12, 10, 12, 10)
        self.placeholder = _label("把 Excel 拖到这里\n支持多个 .xls / .xlsx 文件", "muted", True)
        self.placeholder.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.placeholder.setMinimumHeight(100)
        drop_layout.addWidget(self.placeholder)
        self.list = QListWidget()
        self.list.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self.list.setMinimumHeight(115)
        self.list.setMaximumHeight(175)
        self.list.setAcceptDrops(False)
        drop_layout.addWidget(self.list)
        self.list.hide()
        layout.addWidget(self.drop)
        self.count = _label("尚未选择文件", "small")
        layout.addWidget(self.count)

    def choose(self):
        paths, _ = QFileDialog.getOpenFileNames(self, "选择 Excel 文件", "", EXCEL_FILTER)
        self.add_paths(paths)

    def add_paths(self, paths):
        added = False
        existing = {str(Path(p).resolve()).casefold() for p in self.paths}
        rejected = []
        for value in paths:
            path = Path(value)
            if path.suffix.lower() not in (".xls", ".xlsx") or not path.is_file():
                rejected.append(value)
                continue
            normalized = str(path.resolve())
            if normalized.casefold() not in existing:
                self.paths.append(normalized)
                existing.add(normalized.casefold())
                added = True
        if added:
            self.refresh()
            self.changed.emit()
        if rejected:
            self.rejected.emit("部分文件无法读取；请选择现有的 .xls 或 .xlsx 文件。")

    def remove(self, path):
        self.paths.remove(path)
        self.refresh()
        self.changed.emit()

    def refresh(self):
        self.list.clear()
        for path in self.paths:
            item = QListWidgetItem()
            item.setSizeHint(QSize(100, 66))
            row = QWidget()
            row_layout = QHBoxLayout(row)
            row_layout.setContentsMargins(2, 7, 0, 7)
            texts = QVBoxLayout()
            texts.setSpacing(3)
            name = _label(Path(path).name)
            name.setToolTip(path)
            name.setWordWrap(True)
            name.setMinimumWidth(0)
            texts.addWidget(name)
            location = _label(str(Path(path).parent), "small")
            location.setToolTip(path)
            location.setMinimumWidth(0)
            location.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
            texts.addWidget(location)
            row_layout.addLayout(texts, 1)
            remove = _button("移除", lambda checked=False, p=path: self.remove(p))
            remove.setObjectName("remove")
            row_layout.addWidget(remove)
            self.list.addItem(item)
            self.list.setItemWidget(item, row)
        self.placeholder.setVisible(not self.paths)
        self.list.setVisible(bool(self.paths))
        self.count.setText(f"已选择 {len(self.paths)} 个文件" if self.paths else "尚未选择文件")


class MainWindow(QMainWindow):
    def __init__(self, material_store=None):
        super().__init__()
        self.setWindowTitle("给小雨的软件 · 账务、物流与发票")
        self.resize(1180, 800)
        self.setMinimumSize(1000, 700)
        self.setAcceptDrops(True)
        self.setStyleSheet(STYLESHEET)
        icon = Path(resource_path("assets/app.ico"))
        if icon.exists():
            self.setWindowIcon(QIcon(str(icon)))
        self.bundle = None
        self.result = None
        self._template_path = ""
        self._last_output = ""
        self._revision = 0
        self._busy = False
        self._updating = True
        self._workers = []
        self._row_vouchers = {}
        self._saved_period = ""
        self._config = load_config() or {}
        self.material_store = material_store if material_store is not None else MaterialStore()
        self._company = self._config.get("company") or "默认账套"
        self.material_records = []
        self.material_issues = []
        self._save_timer = QTimer(self)
        self._save_timer.setSingleShot(True)
        self._save_timer.setInterval(450)
        self._save_timer.timeout.connect(self._save_settings)
        self._build()
        self._restore()
        self._updating = False
        self._refresh_materials()
        self._sync_buttons()
        self.statusBar().showMessage("表格在本机读取 · 原始文件保持不变")

    def _build(self):
        central = QWidget()
        central.setObjectName("workspace")
        self.setCentralWidget(central)
        outer = QHBoxLayout(central)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        sidebar = QFrame()
        sidebar.setObjectName("sidebar")
        sidebar.setFixedWidth(196)
        side = QVBoxLayout(sidebar)
        side.setContentsMargins(22, 30, 18, 22)
        side.setSpacing(10)
        logo = Path(resource_path("assets/logo.png"))
        if logo.exists():
            image = QPixmap(str(logo))
            if not image.isNull():
                logo_label = QLabel()
                logo_label.setPixmap(image.scaled(92, 92, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation))
                side.addWidget(logo_label)
        side.addWidget(_label("小雨", "brand"))
        side.addWidget(_label("给小雨的软件", "brandSub"))
        side.addSpacing(28)
        self.nav_buttons = [None] * 5
        for index, text in ((0, "导入表格"), (3, "资料核对"), (1, "凭证预览"), (2, "生成规则"), (4, "财务 AI 助手")):
            button = _button(text, lambda checked=False, i=index: self._navigate(i))
            button.setObjectName("nav")
            side.addWidget(button)
            self.nav_buttons[index] = button
        side.addStretch()
        self.update_controller = UpdateController(self)
        side.addWidget(self.update_controller.version_label)
        side.addWidget(self.update_controller.check_button)
        outer.addWidget(sidebar)
        main = QVBoxLayout()
        main.setContentsMargins(27, 24, 27, 16)
        main.setSpacing(13)
        self.company_bar = QWidget()
        company_row = QHBoxLayout(self.company_bar)
        company_row.setContentsMargins(0, 0, 0, 0)
        company_row.addWidget(_label("当前公司 / 账套"))
        self.company_combo = QComboBox()
        self.company_combo.setMinimumWidth(200)
        self.company_combo.addItems(sorted(set(self.material_store.companies()) | {self._company}))
        self.company_combo.setCurrentText(self._company)
        self.company_combo.currentTextChanged.connect(self._switch_company)
        company_row.addWidget(self.company_combo, 1)
        self.add_company_button = _button("新增公司", self._add_company)
        company_row.addWidget(self.add_company_button)
        main.addWidget(self.company_bar)
        self.eyebrow = _label("XIAOYU  /  账务 · 物流 · 发票", "eyebrow")
        self.subtitle = _label("选择 Excel，确认每列含义，核对资料后导出清单或生成账务凭证。", "subtitle", True)
        main.addWidget(self.eyebrow)
        main.addWidget(self.subtitle)
        main.addWidget(self.update_controller.panel)
        self.steps_bar = QWidget()
        steps = QHBoxLayout(self.steps_bar)
        steps.setContentsMargins(0, 0, 0, 0)
        steps.setSpacing(9)
        self.steps = []
        for text in ("01  导入表格", "02  核对资料", "03  导出文件"):
            label = _label(text, "step")
            steps.addWidget(label)
            self.steps.append(label)
        steps.addStretch()
        main.addWidget(self.steps_bar)
        self.progress = QProgressBar()
        self.progress.setRange(0, 0)
        self.progress.setTextVisible(False)
        self.progress.hide()
        main.addWidget(self.progress)
        self.pages = QStackedWidget()
        self.pages.addWidget(self._build_import())
        self.pages.addWidget(self._build_preview())
        self.pages.addWidget(self._build_rules())
        self.records_page = RecordsPage()
        self.records_page.exportRequested.connect(self._export_materials)
        self.records_page.generateRequested.connect(self._prepare_accounting)
        self.records_page.removeRequested.connect(self._remove_materials)
        self.pages.addWidget(self.records_page)
        # Load AI credentials only when entering this independent feature.
        self.ai_page = None
        self.pages.addWidget(QWidget())
        main.addWidget(self.pages, 1)
        self.notice = _label("四类资料均可单独导入；首次确认列对应后，可以记住这种格式。", "notice", True)
        main.addWidget(self.notice)
        outer.addLayout(main, 1)
        self._navigate(0)

    def _scroll_page(self):
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        content = QWidget()
        layout = QVBoxLayout(content)
        layout.setContentsMargins(0, 3, 5, 3)
        layout.setSpacing(15)
        scroll.setWidget(content)
        return scroll, layout

    def _card(self, title, description=""):
        card = QFrame()
        card.setObjectName("card")
        layout = QVBoxLayout(card)
        layout.setContentsMargins(20, 18, 20, 18)
        layout.setSpacing(12)
        layout.addWidget(_label(title, "sectionTitle"))
        if description:
            layout.addWidget(_label(description, "muted", True))
        return card, layout

    def _build_import(self):
        page, layout = self._scroll_page()
        pair = QGridLayout()
        pair.setSpacing(14)
        self.bank_panel = FilePanel("银行流水", "识别日期、收支金额等列，确认后用于生成凭证。")
        self.payroll_panel = FilePanel("工资表", "识别工资和扣款列，每次确认工资所属年月。")
        self.logistics_panel = FilePanel("物流 Excel", "商品收发货、快递运输和运费记录。")
        self.invoice_panel = FilePanel("发票 Excel", "收到或开出的发票，保留一张票的多行明细。")
        self.file_panels = {"bank": self.bank_panel, "payroll": self.payroll_panel,
                            "logistics": self.logistics_panel, "invoice": self.invoice_panel}
        for index, panel in enumerate(self.file_panels.values()):
            panel.placeholder.setMinimumHeight(55)
            panel.list.setFixedHeight(90)
            panel.layout().setContentsMargins(18, 14, 18, 14)
            panel.changed.connect(self._files_changed)
            panel.rejected.connect(lambda text: self._notify(text, "error"))
            pair.addWidget(panel, index // 2, index % 2)
        layout.addLayout(pair)
        card, content = self._card("柠檬云导入模板", "已内置当前账套模板；更换账套时，可选择新的凭证导入模板。")
        self.template_drop = DropFrame()
        self.template_drop.filesDropped.connect(self._template_dropped)
        self.template_drop.rejected.connect(lambda text: self._notify(text, "error"))
        row = QHBoxLayout(self.template_drop)
        row.setContentsMargins(13, 12, 13, 12)
        text_column = QVBoxLayout()
        self.template_name = _label("使用内置模板")
        self.template_name.setWordWrap(True)
        self.template_location = _label("35 列标准格式 · 含当前账套科目", "small", True)
        text_column.addWidget(self.template_name)
        text_column.addWidget(self.template_location)
        row.addLayout(text_column, 1)
        row.addWidget(_button("更换模板", self._choose_template))
        reset = _button("恢复内置", self._reset_template)
        reset.setObjectName("link")
        row.addWidget(reset)
        content.addWidget(self.template_drop)
        layout.addWidget(card)
        action = QHBoxLayout()
        action.addWidget(_label("先确认列对应，再保存资料；原始 Excel 保持不变。", "muted", True), 1)
        self.load_button = _button("识别列名，确认导入", self._load_inputs, True)
        action.addWidget(self.load_button)
        layout.insertLayout(0, action)
        layout.addStretch()
        return page

    def _build_preview(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 3, 0, 0)
        layout.setSpacing(12)
        controls = QHBoxLayout()
        controls.addWidget(_label("账务月份"))
        self.period_combo = QComboBox()
        self.period_combo.setEditable(True)
        self.period_combo.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        self.period_combo.addItem("全部月份", "")
        self.period_combo.setToolTip("选择月份，或输入 YYYY-MM。只有工资表时，请明确所属年月。")
        self.period_combo.setMinimumWidth(135)
        self.period_combo.currentTextChanged.connect(self._settings_changed)
        controls.addWidget(self.period_combo)
        controls.addStretch()
        self.select_all = QCheckBox("全选凭证")
        self.select_all.setChecked(True)
        self.select_all.toggled.connect(self._select_all_vouchers)
        controls.addWidget(self.select_all)
        self.generate_button = _button("重新生成", self._generate)
        controls.addWidget(self.generate_button)
        layout.addLayout(controls)
        stats = QHBoxLayout()
        stats.setSpacing(10)
        self.stat_labels = {}
        for key, caption in (("count", "已选凭证"), ("debit", "借方合计（元）"), ("credit", "贷方合计（元）"), ("issues", "待核对")):
            card = QFrame()
            card.setObjectName("card")
            box = QVBoxLayout(card)
            box.setContentsMargins(15, 12, 15, 12)
            box.setSpacing(4)
            box.addWidget(_label(caption, "statLabel"))
            value = _label("—", "statValue")
            self.stat_labels[key] = value
            box.addWidget(value)
            stats.addWidget(card, 1)
        layout.addLayout(stats)
        self.balance_label = _label("每张凭证整体选择，分录不会拆开导出。", "muted", True)
        layout.addWidget(self.balance_label)
        self.preview_stack = QStackedWidget()
        empty = QFrame()
        empty.setObjectName("emptyPreview")
        empty_layout = QVBoxLayout(empty)
        empty_layout.addStretch()
        empty_layout.addWidget(_label("凭证准备好后，会出现在这里", "sectionTitle"), 0, Qt.AlignmentFlag.AlignCenter)
        self.empty_hint = _label("先导入表格，再核对金额与科目。", "muted", True)
        self.empty_hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        empty_layout.addWidget(self.empty_hint)
        back = _button("去导入表格", lambda: self._navigate(0))
        empty_layout.addWidget(back, 0, Qt.AlignmentFlag.AlignCenter)
        empty_layout.addStretch()
        self.preview_stack.addWidget(empty)
        splitter = QSplitter(Qt.Orientation.Vertical)
        self.preview_splitter = splitter
        splitter.setChildrenCollapsible(False)
        splitter.setHandleWidth(8)
        self.preview = QTableWidget(0, 6)
        self.preview.setHorizontalHeaderLabels(["导出", "日期 / 凭证", "摘要", "科目", "借方（元）", "贷方（元）"])
        self.preview.verticalHeader().hide()
        self.preview.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.preview.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.preview.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.preview.setWordWrap(True)
        self.preview.setShowGrid(False)
        header = self.preview.horizontalHeader()
        for col, width in ((0, 48), (1, 118), (2, 150), (4, 112), (5, 112)):
            header.setSectionResizeMode(col, QHeaderView.ResizeMode.Interactive)
            self.preview.setColumnWidth(col, width)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        self.preview.itemChanged.connect(self._voucher_checked)
        splitter.addWidget(self.preview)
        self.issue_panel = QWidget()
        warnings_layout = QVBoxLayout(self.issue_panel)
        warnings_layout.setContentsMargins(0, 4, 0, 0)
        warnings_layout.setSpacing(6)
        warning_row = QHBoxLayout()
        self.issue_title = _label("待核对内容", "sectionTitle")
        warning_row.addWidget(self.issue_title)
        warning_row.addStretch()
        warning_row.addWidget(_label("提示不阻止导出", "small"))
        warnings_layout.addLayout(warning_row)
        self.issue_table = QTreeWidget()
        self.issue_table.setColumnCount(3)
        self.issue_table.setHeaderLabels(["类型", "需要核对的内容", "来源"])
        self.issue_table.setRootIsDecorated(False)
        self.issue_table.setWordWrap(True)
        self.issue_table.setMinimumHeight(150)
        self.issue_table.setColumnWidth(0, 70)
        self.issue_table.setColumnWidth(2, 200)
        self.issue_table.header().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.issue_table.header().setStretchLastSection(False)
        warnings_layout.addWidget(self.issue_table)
        splitter.addWidget(self.issue_panel)
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 2)
        splitter.setSizes([300, 210])
        self.preview_stack.addWidget(splitter)
        layout.addWidget(self.preview_stack, 1)
        footer = QHBoxLayout()
        self.export_format = QComboBox()
        self.export_format.addItem("Excel 97–2003（.xls）", "xls")
        self.export_format.addItem("Excel 工作簿（.xlsx）", "xlsx")
        self.export_format.currentIndexChanged.connect(lambda: self._save_timer.start())
        footer.addWidget(self.export_format)
        self.open_output_button = _button("打开导出文件夹", self._open_output)
        self.open_output_button.setObjectName("link")
        self.open_output_button.hide()
        footer.addWidget(self.open_output_button)
        footer.addStretch()
        self.export_button = _button("导出凭证", self._export, True)
        footer.addWidget(self.export_button)
        layout.addLayout(footer)
        return page

    def _build_rules(self):
        page, layout = self._scroll_page()
        card, content = self._card("常用设置", "设置会保存在本机。修改规则后，请重新生成预览。")
        grid = QGridLayout()
        grid.setHorizontalSpacing(14)
        grid.setVerticalSpacing(10)
        self.start_number = QSpinBox()
        self.start_number.setRange(1, 999999)
        self.preparer = QLineEdit("001")
        self.bank_account = QComboBox()
        self.bank_account.setEditable(True)
        self.bank_account.addItem("1002001  银行存款-示例银行账户一", "1002001")
        self._setup_account_combo(self.bank_account)
        grid.addWidget(_label("起始凭证号"), 0, 0)
        grid.addWidget(self.start_number, 0, 1)
        grid.addWidget(_label("制单人"), 0, 2)
        grid.addWidget(self.preparer, 0, 3)
        grid.addWidget(_label("银行科目"), 1, 0)
        grid.addWidget(self.bank_account, 1, 1, 1, 3)
        grid.setColumnStretch(1, 1)
        grid.setColumnStretch(3, 1)
        content.addLayout(grid)
        self.bank_account_hint = _label("可按科目编码或完整名称搜索选择。", "small", True)
        content.addWidget(self.bank_account_hint)
        layout.addWidget(card)

        card, content = self._card("平台回款", "平台收入已在其他地方确认时，选择“仅记录回款”，避免重复记收入。")
        form = QFormLayout()
        form.setSpacing(11)
        self.platform_mode = QComboBox()
        self.platform_mode.addItem("仅记录回款（默认）", "settlement_only")
        self.platform_mode.addItem("同一张凭证确认收入并结算", "income_and_settlement")
        self.fee_mode = QComboBox()
        self.fee_mode.addItem("无平台费", "none")
        self.fee_mode.addItem("按每笔结算的费率", "rate")
        self.fee_mode.addItem("每笔固定金额", "amount")
        self.fee_value = QDoubleSpinBox()
        self.fee_value.setRange(0, 999999999)
        self.fee_value.setDecimals(2)
        fee_row = QHBoxLayout()
        fee_row.addWidget(self.fee_mode, 2)
        fee_row.addWidget(self.fee_value, 1)
        self.tax_rate = QDoubleSpinBox()
        self.tax_rate.setDecimals(3)
        self.tax_rate.setRange(0, 100)
        self.tax_rate.setSuffix(" %")
        self.tax_rate.setValue(1)
        form.addRow("记账方式", self.platform_mode)
        form.addRow("平台手续费", fee_row)
        form.addRow("收入税率", self.tax_rate)
        content.addLayout(form)
        content.addWidget(_label("费率或固定金额会用于每一笔平台收款。银行转账手续费取流水中的实际扣款。", "small", True))
        content.addWidget(_label("收入按含税总额拆分；仅记录回款时，不再次确认收入和税费。", "small", True))
        self.fee_mode.currentIndexChanged.connect(self._fee_mode_changed)
        layout.addWidget(card)

        card, content = self._card("工资与社保", "金额取本次所选月份的工资与已确认社保，银行付款日期按流水保留。")
        self.payroll_accrual = QCheckBox("同时生成工资计提凭证")
        self.social_accrual = QCheckBox("同时生成单位社保计提凭证")
        content.addWidget(self.payroll_accrual)
        content.addWidget(_label("默认不计提。已在柠檬云计提的月份，请保持不选。", "small", True))
        content.addWidget(_label("计提入账月份 · 可选"))
        self.accrual_period = QLineEdit()
        self.accrual_period.setPlaceholderText("留空使用所选月份，例如填 2026-07 可记入上月")
        content.addWidget(self.accrual_period)
        content.addWidget(_label("计提月份可填上月；金额仍取本次所选月份的资料。已计提过的不勾选，避免重复。", "small", True))
        content.addWidget(_label("已确认的社保缴费日期"))
        self.social_dates = QLineEdit()
        self.social_dates.setPlaceholderText("例如：2026-08-03, 2026-09-10")
        content.addWidget(self.social_dates)
        content.addWidget(_label("仅填写已核实的社保扣款批次；多个日期用逗号隔开。未确认的公共缴费会提示核对。", "small", True))
        content.addWidget(self.social_accrual)
        layout.addWidget(card)

        card, content = self._card("对方户名与科目", "为已确认的业务指定科目，下次遇到完全相同的户名时自动使用。")
        content.addWidget(_label("同一户名有多种业务时，请先核对用途，避免把所有付款都归到同一个科目。", "small", True))
        self.mapping_table = QTableWidget(0, 3)
        self.mapping_table.setHorizontalHeaderLabels(["对方户名", "对应科目", ""])
        self.mapping_table.verticalHeader().hide()
        self.mapping_table.setMinimumHeight(165)
        self.mapping_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.mapping_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.mapping_table.setColumnWidth(2, 62)
        self.mapping_table.itemChanged.connect(self._settings_changed)
        content.addWidget(self.mapping_table)
        add = _button("添加对应关系", lambda: self._add_mapping())
        content.addWidget(add, 0, Qt.AlignmentFlag.AlignLeft)
        layout.addWidget(card)
        layout.addStretch()
        for control, signal in ((self.start_number, "valueChanged"), (self.preparer, "textChanged"),
                                (self.platform_mode, "currentIndexChanged"),
                                (self.fee_value, "valueChanged"), (self.tax_rate, "valueChanged"),
                                (self.payroll_accrual, "toggled"), (self.social_accrual, "toggled"),
                                (self.social_dates, "textChanged"), (self.accrual_period, "textChanged")):
            getattr(control, signal).connect(self._settings_changed)
        return page

    def _navigate(self, index):
        if index == 4 and self.ai_page is None:
            from .ai_ui import FinanceAIPage
            placeholder = self.pages.widget(4)
            self.pages.removeWidget(placeholder)
            placeholder.deleteLater()
            self.ai_page = FinanceAIPage()
            self.pages.addWidget(self.ai_page)
        self.pages.setCurrentIndex(index)
        self.eyebrow.setVisible(index not in (1, 4))
        self.subtitle.setVisible(index not in (1, 4))
        self.company_bar.setVisible(index != 4)
        self.steps_bar.setVisible(index != 4)
        self.notice.setVisible(index != 4)
        self.progress.setVisible(self._busy and index != 4)
        self.statusBar().showMessage('财务 AI 助手 · 文件由用户上传，交由 OpenAI 云端处理'
                                    if index == 4 else '表格在本机读取 · 原始文件保持不变')
        for i, button in enumerate(self.nav_buttons):
            button.setProperty("selected", i == index)
            _restyle(button)
        self._update_steps()

    def _update_steps(self):
        if not hasattr(self, "steps"):
            return
        current = 2 if self._last_output else (1 if self.result or self.material_records else 0)
        for i, label in enumerate(self.steps):
            label.setProperty("active", i == current)
            _restyle(label)

    def _notify(self, text, tone=""):
        self.notice.setText(str(text))
        self.notice.setProperty("tone", tone)
        _restyle(self.notice)

    def _sync_buttons(self):
        if not hasattr(self, "load_button"):
            return
        has_files = any(panel.paths for panel in self.file_panels.values())
        self.load_button.setEnabled(has_files and not self._busy)
        self.generate_button.setEnabled(self.bundle is not None and not self._busy)
        self.export_button.setEnabled(self.result is not None and bool(self.result.selected) and not self._busy)
        self.select_all.setEnabled(self.result is not None and not self._busy)
        self.preview.setEnabled(not self._busy)
        self.period_combo.setEnabled(not self._busy)
        self.export_format.setEnabled(not self._busy)
        self.pages.widget(0).setEnabled(not self._busy)
        self.pages.widget(2).setEnabled(not self._busy)
        self.company_combo.setEnabled(not self._busy)
        self.add_company_button.setEnabled(not self._busy)
        self.records_page.set_busy(self._busy)
        self.progress.setVisible(self._busy and self.pages.currentIndex() != 4)
        self._update_steps()

    def _invalidate(self, files=False):
        if self._updating:
            return
        self._revision += 1
        self.result = None
        self._last_output = ""
        self.open_output_button.hide()
        if files:
            self.bundle = None
        self.preview.clearContents()
        self.preview.setRowCount(0)
        self.issue_table.clear()
        self.preview_stack.setCurrentIndex(0)
        self._row_vouchers.clear()
        self.empty_hint.setText("表格或规则已更新，请重新生成预览。")
        self.balance_label.setText("每张凭证整体选择，分录不会拆开导出。")
        for label in self.stat_labels.values():
            label.setText("—")
        self._sync_buttons()
        self._save_timer.start()

    def _files_changed(self):
        self._invalidate(files=True)
        if not self._updating:
            self._notify("文件已更新，点击“识别列名，确认导入”继续。")

    def _settings_changed(self, *_):
        self._invalidate()
        if not self._updating:
            self._notify("规则已更新。重新生成后，即可核对和导出。")

    def _fee_mode_changed(self, *_):
        mode = self.fee_mode.currentData()
        self.fee_value.blockSignals(True)
        self.fee_value.setEnabled(mode != "none")
        self.fee_value.setDecimals(3 if mode == "rate" else 2)
        self.fee_value.setMaximum(99.999 if mode == "rate" else 999999999)
        self.fee_value.setSuffix(" %" if mode == "rate" else " 元 / 笔")
        self.fee_value.blockSignals(False)
        self._settings_changed()

    def _choose_template(self):
        path, _ = QFileDialog.getOpenFileName(self, "选择柠檬云凭证导入模板", "", EXCEL_FILTER)
        if path:
            self._template_dropped([path])

    def _template_dropped(self, paths):
        if not paths:
            return
        self._template_path = paths[0]
        self._refresh_template_label()
        self._files_changed()
        if len(paths) > 1:
            self._notify("一次使用一个模板，已选择第一个文件。")

    def _reset_template(self):
        self._template_path = ""
        self._refresh_template_label()
        self._files_changed()

    def _refresh_template_label(self):
        if self._template_path:
            self.template_name.setText(Path(self._template_path).name)
            self.template_location.setText(str(Path(self._template_path).parent))
            self.template_name.setToolTip(self._template_path)
        else:
            self.template_name.setText("使用内置模板")
            self.template_location.setText("35 列标准格式 · 含当前账套科目")
            self.template_name.setToolTip(str(service.default_template_path()))

    def _setup_account_combo(self, combo):
        combo.setEditable(True)
        combo.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        completer = QCompleter(combo.model(), combo)
        completer.setCompletionColumn(0)
        completer.setCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        completer.setFilterMode(Qt.MatchFlag.MatchContains)
        completer.setCompletionMode(QCompleter.CompletionMode.PopupCompletion)
        combo.setCompleter(completer)
        combo.currentTextChanged.connect(lambda _text, field=combo: self._account_changed(field))
        combo.lineEdit().editingFinished.connect(lambda field=combo: self._commit_account(field))
        combo.activated.connect(lambda _index, field=combo: self._commit_account(field))
        completer.activated[str].connect(lambda text, field=combo: self._complete_account(field, text))

    def _complete_account(self, combo, text):
        combo.setEditText(text)
        self._commit_account(combo)

    def _account_changed(self, combo):
        self._settings_changed()
        if not self._updating:
            self._resolve_account_combo(combo)

    def _account_feedback(self, combo, message="", unresolved=False):
        detail = message or "可按科目编码或完整名称搜索选择。"
        if unresolved:
            detail += " 可先生成待核对凭证，仍可导出。"
        combo.setToolTip(detail)
        combo.lineEdit().setToolTip(detail)
        combo.setProperty("accountError", unresolved)
        combo.setStyleSheet("QComboBox { border: 1px solid #B76D43; }" if unresolved else "")
        if combo is self.bank_account and hasattr(self, "bank_account_hint"):
            self.bank_account_hint.setText(detail)
            self.bank_account_hint.setStyleSheet("color: #99502F;" if unresolved else "")

    def _resolve_account_combo(self, combo, canonicalize=False):
        """Return a true account code, or keep unresolved text for engine review.

        Editable QComboBox.currentData() may still point at an old selection.
        It is only usable before template loading when the visible text has
        not changed from that exact item. Loaded templates are authoritative.
        """
        text = combo.currentText().strip()
        if self.bundle is None:
            self._account_feedback(combo, "读取模板后验证科目；当前输入会保留。")
            index = combo.currentIndex()
            if index >= 0 and text == combo.itemText(index).strip() and combo.itemData(index):
                return str(combo.itemData(index)), None
            return text, None
        resolution = resolve_account_input(text, self.bundle.template.accounts)
        if resolution.account is None:
            self._account_feedback(combo, resolution.message or "请从候选项中选择完整科目。", unresolved=True)
            return text, resolution
        account = resolution.account
        self._account_feedback(combo)
        if canonicalize:
            previous = combo.blockSignals(True)
            index = combo.findData(account.code)
            if index >= 0:
                combo.setCurrentIndex(index)
                # setCurrentIndex may be a no-op while editable text was changed.
                combo.setEditText(combo.itemText(index))
            else:
                combo.setCurrentIndex(-1)
                combo.setEditText(f"{account.code}  {account.name}")
            combo.blockSignals(previous)
        return account.code, resolution

    def _commit_account(self, combo):
        _value, resolution = self._resolve_account_combo(combo, canonicalize=True)
        if self._updating:
            return
        self._save_timer.start()
        if resolution is not None and resolution.account is None:
            self._notify(f"科目需要核对：{resolution.message} 仍可生成并导出待核对凭证。", "error")

    def _code(self, combo):
        # Saving may run while the user is still typing. Normalize the saved
        # value, but leave display normalization to commit/load/generation.
        return self._resolve_account_combo(combo)[0]

    def _account_input_issues(self):
        if self.bundle is None:
            return []
        controls = [(self.bank_account, "生成规则 / 银行科目")]
        for row in range(self.mapping_table.rowCount()):
            item = self.mapping_table.item(row, 0)
            name = item.text().strip() if item else ""
            if name:
                controls.append((self.mapping_table.cellWidget(row, 1), f"生成规则 / 对方户名：{name}"))
        issues = []
        for combo, source in controls:
            _value, resolution = self._resolve_account_combo(combo, canonicalize=True)
            if resolution is not None and resolution.account is None:
                issues.append(Issue("warning", resolution.message or "尚未选择完整科目。", source))
        return issues

    def _account_choices(self):
        return list(self.bundle.template.accounts.values()) if self.bundle else []

    def _populate_account_combo(self, combo, value, bank_only=False):
        previous = combo.blockSignals(True)
        combo.clear()
        for account in self._account_choices():
            if not bank_only or account.code.startswith("1002"):
                combo.addItem(f"{account.code}  {account.name}", account.code)
        combo.setCurrentIndex(-1)
        combo.setEditText(str(value or ""))
        self._resolve_account_combo(combo, canonicalize=True)
        combo.blockSignals(previous)

    def _add_mapping(self, name="", code=""):
        row = self.mapping_table.rowCount()
        old_updating = self._updating
        self._updating = True
        self.mapping_table.insertRow(row)
        item = QTableWidgetItem(name)
        item.setToolTip("填写银行流水中的完整对方户名")
        self.mapping_table.setItem(row, 0, item)
        combo = QComboBox()
        self._setup_account_combo(combo)
        combo.setMinimumWidth(100)
        combo.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        combo.lineEdit().setPlaceholderText("搜索科目名称或编码，选择完整项")
        self._populate_account_combo(combo, code)
        self.mapping_table.setCellWidget(row, 1, combo)
        remove = _button("移除")
        remove.setObjectName("remove")
        remove.clicked.connect(lambda checked=False, b=remove: self._remove_mapping(b))
        self.mapping_table.setCellWidget(row, 2, remove)
        self.mapping_table.setRowHeight(row, 46)
        self._updating = old_updating
        self._settings_changed()

    def _remove_mapping(self, button):
        for row in range(self.mapping_table.rowCount()):
            if self.mapping_table.cellWidget(row, 2) is button:
                self.mapping_table.removeRow(row)
                self._settings_changed()
                return

    def _mapping_values(self):
        result = {}
        for row in range(self.mapping_table.rowCount()):
            item = self.mapping_table.item(row, 0)
            name = item.text().strip() if item else ""
            code = self._code(self.mapping_table.cellWidget(row, 1))
            if name:
                result[name] = code
        return result

    def _current_period(self):
        text = self.period_combo.currentText().strip()
        return "" if text == "全部月份" else text

    def _read_options(self):
        period = self._current_period()
        if period:
            try:
                if not re.fullmatch(r"\d{4}-\d{2}", period):
                    raise ValueError()
                datetime.strptime(period, "%Y-%m")
            except ValueError as exc:
                raise ValueError("账务月份请写成 2026-08，或选择“全部月份”。") from exc
        accrual_period = self.accrual_period.text().strip()
        if accrual_period:
            try:
                if not re.fullmatch(r"\d{4}-\d{2}", accrual_period):
                    raise ValueError()
                datetime.strptime(accrual_period, "%Y-%m")
            except ValueError as exc:
                raise ValueError("计提入账月份请写成 2026-07，留空则使用本次所选月份。") from exc
        dates = []
        for date in re.split(r"[,，;；\s]+", self.social_dates.text().strip()):
            if not date:
                continue
            if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date):
                raise ValueError("社保日期请写成 2026-08-03，多笔用逗号隔开。")
            try:
                datetime.strptime(date, "%Y-%m-%d")
            except ValueError as exc:
                raise ValueError(f"社保日期“{date}”不存在，请检查。") from exc
            if date not in dates:
                dates.append(date)
        return Options(
            period=period,
            bank_account=self._code(self.bank_account),
            start_number=self.start_number.value(),
            preparer=self.preparer.text().strip(),
            platform_mode=self.platform_mode.currentData(),
            platform_fee_mode=self.fee_mode.currentData(),
            platform_fee_value=Decimal(str(self.fee_value.value())),
            tax_rate=Decimal(str(self.tax_rate.value())),
            payroll_accrual=self.payroll_accrual.isChecked(),
            mappings=self._mapping_values(),
            social_payment_dates=dates,
            social_accrual=self.social_accrual.isChecked(),
            accrual_period=accrual_period,
        )

    def _run(self, function, callback, message):
        if self._busy:
            return
        revision = self._revision
        self._busy = True
        self._sync_buttons()
        self._notify(message)
        worker = Worker(function)
        self._workers.append(worker)

        def completed(value):
            self._busy = False
            self._sync_buttons()
            if revision != self._revision:
                self._notify("表格或规则在处理期间发生了变化，请按新设置重新生成。")
                return
            try:
                callback(value)
            except Exception as exc:
                self._notify(f"处理结果时遇到问题：{exc}", "error")

        def failed(message):
            self._busy = False
            self._sync_buttons()
            self._notify(f"未能完成：{message}", "error")

        worker.signals.result.connect(completed)
        worker.signals.error.connect(failed)
        worker.signals.finished.connect(lambda: self._workers.remove(worker) if worker in self._workers else None)
        QThreadPool.globalInstance().start(worker)

    def _load_inputs(self):
        if self._busy:
            return
        sources = [(kind, path) for kind, panel in self.file_panels.items() for path in panel.paths]
        self._invalidate(files=True)
        def inspect():
            workbooks, issues, cache = [], [], {}
            for kind, path in sources:
                try:
                    if path not in cache:
                        cache[path] = inspect_workbook(path)
                    workbooks.append((kind, cache[path]))
                except Exception as exc:
                    issues.append(Issue("error", str(exc), str(path)))
            return workbooks, issues
        self._run(inspect, self._confirm_imports, "正在读取 Excel，识别各工作表的列名…")

    def _confirm_imports(self, inspected):
        workbooks, read_issues = inspected
        company = self._company
        confirmed, profiles = [], []
        for kind, workbook in workbooks:
            known = self.material_store.load_profiles(company, kind)
            known.extend(profile for profile in profiles if profile["kind"] == kind)
            dialog = MappingDialog(workbook, kind, known, self)
            if dialog.exec() == QDialog.DialogCode.Accepted:
                confirmed.append((workbook, dialog.specs))
                profiles.extend(dialog.profiles)
            dialog.deleteLater()
        if not confirmed:
            if read_issues:
                self._notify("；".join(issue.message for issue in read_issues), "error")
            else:
                self._notify("本次未确认导入，已保存的资料保持不变。")
            return
        def save():
            records, issues = {}, list(read_issues)
            for workbook, specs in confirmed:
                result = parse_workbook(workbook, specs)
                issues.extend(result.issues)
                for record in result.records:
                    if record.id in records:
                        issues.append(Issue("info", "本次完全相同的来源行已合并，未重复保存。", record.source))
                    records[record.id] = record
            result = ImportResult(list(records.values()), issues)
            sources = [(workbook.path, spec.sheet) for workbook, specs in confirmed for spec in specs]
            scopes = [(workbook.digest, spec.kind, spec.sheet) for workbook, specs in confirmed for spec in specs]
            statistics = self.material_store.save_import(company, result, replace_sources=sources, replace_scopes=scopes)
            self.material_store.save_profiles(company, profiles)
            return statistics
        self._run(save, self._materials_saved, "正在检查数据并保存到当前公司的本地资料库…")

    def _materials_saved(self, statistics):
        self._invalidate(files=True)
        self._refresh_materials()
        self._navigate(3)
        updated = statistics.get('updated', 0)
        repeated = statistics['existing'] - updated
        removed = f"本次更正后移除了 {statistics['removed']} 条旧识别记录。" if statistics.get('removed') else ''
        self._notify(f"资料已保存：新增 {statistics['added']} 条，更新 {updated} 条，重复 {repeated} 条。{removed}"
                     "请核对提示；银行和工资可继续生成凭证。", "success")

    def _refresh_materials(self):
        self.material_records = self.material_store.load_records(self._company)
        self.material_issues = self.material_store.get_issues(self._company)
        self.records_page.set_records(self.material_records, self.material_issues)

    def _remove_materials(self, ids):
        if self._busy or not ids:
            return
        self.material_store.delete_records(self._company, ids)
        self._invalidate(files=True)
        self._refresh_materials()
        self._notify(f"已从当前公司资料库移除 {len(ids)} 条资料，原始 Excel 保持不变。")

    def _prepare_accounting(self):
        if self._busy:
            return
        records = copy.deepcopy(self.material_records)
        issues = copy.deepcopy(self.material_issues)
        template = self._template_path or None
        self._invalidate(files=True)
        self._run(lambda: service.bundle_from_records(records, template, issues), self._loaded,
                  "正在从当前公司已保存的银行和工资资料准备凭证，可在预览页选择月份…")

    def _export_materials(self):
        if self._busy or not self.material_records:
            return
        safe_company = re.sub(r'[<>:"/\\|?*]', '_', self._company)
        directory = self._config.get("output_directory") or str(Path.home() / "Desktop")
        path, _ = QFileDialog.getSaveFileName(self, "导出当前公司全部资料",
                         str(Path(directory) / f"{safe_company}_资料清单.xlsx"), "Excel 工作簿 (*.xlsx)")
        if not path:
            return
        if Path(path).suffix.lower() != ".xlsx":
            path += ".xlsx"
        sources = {str(Path(record.source_path).resolve()).casefold() for record in self.material_records}
        if str(Path(path).resolve()).casefold() in sources:
            self._notify("请另选文件名，资料清单不能覆盖原始 Excel。", "error")
            return
        records, issues = copy.deepcopy(self.material_records), copy.deepcopy(self.material_issues)
        self._config["output_directory"] = str(Path(path).parent)
        self._save_settings()
        self._run(lambda: export_materials(records, issues, path),
                  lambda info: self._notify(f"已导出当前公司全部 {len(records)} 条资料及核对清单：{path}", "success"),
                  "正在导出四类资料和待核对清单…")

    def _add_company(self):
        name, ok = QInputDialog.getText(self, "新增公司 / 账套", "填写公司或账套名称，例如：小雨商贸")
        name = name.strip()
        if not ok or not name:
            return
        if len(name) > 80:
            self._notify("公司名称请控制在 80 个字以内。", "error")
            return
        if self.company_combo.findText(name) < 0:
            self.company_combo.addItem(name)
        self.company_combo.setCurrentText(name)

    def _switch_company(self, company):
        if self._updating or self._busy or not company or company == self._company:
            return
        self._save_settings()
        self._save_timer.stop()
        self._company = company
        scoped = self._config.get("company_settings", {}).get(company, {})
        for key in ("options", "social_dates_text", "bank_paths", "payroll_paths", "logistics_paths",
                    "invoice_paths", "template_path"):
            self._config.pop(key, None)
        self._config.update(copy.deepcopy(scoped))
        self._updating = True
        self.mapping_table.setRowCount(0)
        self._restore()
        self._updating = False
        self._invalidate(files=True)
        self._refresh_materials()
        self._save_settings()
        self._navigate(3)
        self._notify(f"已切换到“{company}”。资料、列对应格式及生成规则分别保存。")

    def _loaded(self, bundle):
        selected_period = self._current_period() or self._saved_period
        bank_input = self.bank_account.currentText()
        mapping_inputs = [self.mapping_table.cellWidget(row, 1).currentText() for row in range(self.mapping_table.rowCount())]
        self.bundle = bundle
        self._updating = True
        self.period_combo.clear()
        self.period_combo.addItem("全部月份", "")
        for month in bundle.months:
            self.period_combo.addItem(month, month)
        selected_index = self.period_combo.findData(selected_period)
        if selected_index > 0:
            self.period_combo.setCurrentIndex(selected_index)
        elif selected_period and not bundle.months:
            self.period_combo.setEditText(selected_period)
        elif len(bundle.months) == 1:
            self.period_combo.setCurrentIndex(1)
        self._populate_account_combo(self.bank_account, bank_input, bank_only=True)
        for row, value in enumerate(mapping_inputs):
            self._populate_account_combo(self.mapping_table.cellWidget(row, 1), value)
        self._updating = False
        self._sync_buttons()
        self._save_settings()
        self._generate()

    def _generate(self):
        if self._busy:
            return
        if self.bundle is None:
            self._navigate(0)
            self._notify("请先读取表格，再生成凭证。")
            return
        try:
            options = self._read_options()
        except ValueError as exc:
            self._navigate(2)
            self._notify(str(exc), "error")
            return
        bundle = self.bundle
        account_issues = self._account_input_issues()
        self._invalidate()
        self._save_settings()
        self._run(lambda: service.generate_bundle(bundle, options), lambda result: self._generated(result, account_issues), "正在匹配科目、汇总金额并生成凭证…")

    def _generated(self, result, account_issues=None):
        if account_issues:
            result.issues.extend(account_issues)
        self.result = result
        self._render_result()
        self._navigate(1)
        issues = len(self._issues())
        if issues:
            self._notify(f"已生成 {len(result.vouchers)} 张凭证，有 {issues} 项待核对。缺少科目可能被柠檬云拒收，仍可导出并查看核对清单。")
        else:
            self._notify(f"已生成 {len(result.vouchers)} 张凭证。核对金额后即可导出。", "success")
        self._sync_buttons()

    def _issues(self):
        if self.result is None:
            return []
        found = []
        seen = set()
        candidates = list(self.result.issues)
        for voucher in self.result.vouchers:
            candidates.extend(voucher.issues)
        for issue in candidates:
            key = (issue.severity, issue.message, issue.source)
            if key not in seen:
                seen.add(key)
                found.append(issue)
        return found

    def _render_result(self):
        self.preview.blockSignals(True)
        self.preview.setRowCount(sum(len(v.entries) for v in self.result.vouchers))
        self._row_vouchers.clear()
        row = 0
        for voucher_index, voucher in enumerate(self.result.vouchers):
            shade = QColor("#FFFCFD" if voucher_index % 2 == 0 else "#FFF0F7")
            for entry_index, entry in enumerate(voucher.entries):
                texts = ["", f"{voucher.date}\n记-{voucher.number}" if entry_index == 0 else "",
                         entry.summary or voucher.summary,
                         f"{entry.account_code}  {entry.account_name}".strip() or "待匹配科目",
                         _money(entry.debit) if entry.debit else "",
                         _money(entry.credit) if entry.credit else ""]
                for column, text in enumerate(texts):
                    item = QTableWidgetItem(text)
                    item.setBackground(shade)
                    item.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
                    if column in (4, 5):
                        item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
                    if column in (1, 2, 3):
                        item.setToolTip("\n".join(voucher.sources))
                    if column == 3 and not entry.account_code:
                        item.setForeground(QColor("#A25A32"))
                    self.preview.setItem(row, column, item)
                if entry_index == 0:
                    check = self.preview.item(row, 0)
                    check.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsUserCheckable | Qt.ItemFlag.ItemIsSelectable)
                    check.setCheckState(Qt.CheckState.Checked if voucher.included else Qt.CheckState.Unchecked)
                    self._row_vouchers[row] = voucher
                self.preview.setRowHeight(row, 55 if entry_index == 0 else 40)
                row += 1
        self.preview.blockSignals(False)
        self.preview_stack.setCurrentIndex(1 if self.result.vouchers or self._issues() else 0)
        if not self.result.vouchers:
            self.empty_hint.setText("当前月份没有可生成的凭证，请检查文件、月份或生成规则。")
        self.issue_table.clear()
        severity_names = {"error": "需补充", "warning": "待核对", "warn": "待核对", "info": "提示"}
        for issue in self._issues():
            item = QTreeWidgetItem([severity_names.get(issue.severity, "待核对"), issue.message, issue.source])
            item.setToolTip(1, issue.message)
            item.setToolTip(2, issue.source)
            if issue.severity in ("error", "warning", "warn"):
                item.setForeground(0, QColor("#9B5935"))
            self.issue_table.addTopLevelItem(item)
        count = len(self._issues())
        self.issue_title.setText(f"待核对内容 · {count} 项")
        self.issue_panel.setVisible(bool(count))
        self._update_statistics()

    def _update_statistics(self):
        if self.result is None:
            return
        selected = self.result.selected
        self.stat_labels["count"].setText(str(len(selected)))
        self.stat_labels["debit"].setText(_money(self.result.debit))
        self.stat_labels["credit"].setText(_money(self.result.credit))
        self.stat_labels["issues"].setText(str(len(self._issues())))
        balanced = all(v.debit == v.credit for v in selected)
        if not selected:
            self.balance_label.setText("尚未选择凭证。勾选后即可导出。")
        elif balanced:
            self.balance_label.setText(f"已选 {len(selected)} / {len(self.result.vouchers)} 张 · 每张凭证借贷平衡")
        else:
            self.balance_label.setText("部分凭证借贷不平，请核对下方提示；仍可导出核对。")
        self.select_all.blockSignals(True)
        self.select_all.setChecked(bool(selected) and len(selected) == len(self.result.vouchers))
        self.select_all.blockSignals(False)
        self._sync_buttons()

    def _voucher_checked(self, item):
        if item.column() != 0 or item.row() not in self._row_vouchers:
            return
        self._row_vouchers[item.row()].included = item.checkState() == Qt.CheckState.Checked
        self._last_output = ""
        self.open_output_button.hide()
        self._update_statistics()

    def _select_all_vouchers(self, checked):
        if self.result is None:
            return
        self.preview.blockSignals(True)
        for row, voucher in self._row_vouchers.items():
            voucher.included = checked
            self.preview.item(row, 0).setCheckState(Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked)
        self.preview.blockSignals(False)
        self._last_output = ""
        self.open_output_button.hide()
        self._update_statistics()

    def _export(self):
        if self._busy or self.result is None or not self.result.selected:
            return
        extension = self.export_format.currentData()
        period = self._current_period() or "全部月份"
        directory = str(Path(self._last_output).parent) if self._last_output else self._config.get("last_export_dir", str(Path.home() / "Desktop"))
        initial = str(Path(directory) / f"小雨_凭证_{period}.{extension}")
        path, _ = QFileDialog.getSaveFileName(self, "导出柠檬云凭证", initial, f"Excel 文件 (*.{extension})")
        if not path:
            return
        if Path(path).suffix.lower() != f".{extension}":
            path = str(Path(path).with_suffix(f".{extension}"))
        original_paths = [*self.bank_panel.paths, *self.payroll_panel.paths,
                          self._template_path, self.bundle.template.path]
        destination = os.path.normcase(str(Path(path).resolve()))
        if destination in {os.path.normcase(str(Path(p).resolve())) for p in original_paths if p}:
            self._notify("请选择新文件名，避免覆盖原始表格。", "error")
            return
        result = copy.deepcopy(self.result)
        template = self.bundle.template
        preparer = self.preparer.text().strip()
        self._run(lambda: service.export_result(result, template, path, preparer=preparer), self._exported, "正在保存凭证和核对清单…")

    def _exported(self, info):
        self._last_output = str(info["output_path"])
        self._config["last_export_dir"] = str(Path(self._last_output).parent)
        self.open_output_button.show()
        self._update_steps()
        self._save_settings()
        count = info.get("voucher_count", len(self.result.selected) if self.result else 0)
        lines = info.get("line_count", "")
        details = f"，共 {lines} 条分录" if lines != "" else ""
        suffix = "；核对清单已一并保存。" if info.get("issues_path") else "。"
        self._notify(f"已导出 {count} 张凭证{details}{suffix} 文件：{self._last_output}", "success")

    def _open_output(self):
        if self._last_output:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(Path(self._last_output).parent)))

    def _restore(self):
        settings = self._config.get("options", {})
        if not isinstance(settings, dict):
            settings = {}

        def number(key, default, minimum, maximum):
            try:
                value = Decimal(str(settings.get(key, default)))
                if not value.is_finite():
                    return default
                return float(max(Decimal(str(minimum)), min(value, Decimal(str(maximum)))))
            except Exception:
                return default

        self._saved_period = str(settings.get("period", ""))
        self.period_combo.clear()
        self.period_combo.addItem("全部月份", "")
        if self._saved_period:
            self.period_combo.setEditText(self._saved_period)
        self.start_number.setValue(int(number("start_number", 1, 1, 999999)))
        self.preparer.setText(str(settings.get("preparer", "001")))
        bank_code = str(settings.get("bank_account", "1002001"))
        bank_index = self.bank_account.findData(bank_code)
        if bank_index >= 0:
            self.bank_account.setCurrentIndex(bank_index)
        else:
            self.bank_account.setEditText(bank_code)
        for combo, key, default in ((self.platform_mode, "platform_mode", "settlement_only"), (self.fee_mode, "platform_fee_mode", "none")):
            index = combo.findData(settings.get(key, default))
            combo.setCurrentIndex(max(0, index))
        self._fee_mode_changed()
        self.fee_value.setValue(number("platform_fee_value", 0, 0, self.fee_value.maximum()))
        self.tax_rate.setValue(number("tax_rate", 1, 0, 100))
        self.payroll_accrual.setChecked(bool(settings.get("payroll_accrual", False)))
        self.social_accrual.setChecked(bool(settings.get("social_accrual", False)))
        self.accrual_period.setText(str(settings.get("accrual_period", "")))
        saved_dates = settings.get("social_payment_dates", [])
        if not isinstance(saved_dates, list):
            saved_dates = []
        self.social_dates.setText(str(self._config.get("social_dates_text", ", ".join(str(v) for v in saved_dates))))
        mappings = settings.get("mappings", {})
        if not isinstance(mappings, dict):
            mappings = {}
        for name, code in mappings.items():
            self._add_mapping(str(name), str(code))
        for kind, panel in self.file_panels.items():
            key = f"{kind}_paths"
            paths = self._config.get(key, [])
            panel.paths = [str(p) for p in paths if isinstance(p, str) and Path(p).is_file()] if isinstance(paths, list) else []
            panel.refresh()
        template = str(self._config.get("template_path", ""))
        self._template_path = template if template and Path(template).is_file() else ""
        self._refresh_template_label()
        extension_index = self.export_format.findData(self._config.get("export_format", "xls"))
        self.export_format.setCurrentIndex(max(0, extension_index))

    def _save_settings(self):
        if self._updating:
            return False
        options = {
            "period": self._current_period(),
            "bank_account": self._code(self.bank_account),
            "start_number": self.start_number.value(), "preparer": self.preparer.text().strip(),
            "platform_mode": self.platform_mode.currentData(),
            "platform_fee_mode": self.fee_mode.currentData(),
            "platform_fee_value": str(self.fee_value.value()), "tax_rate": str(self.tax_rate.value()),
            "payroll_accrual": self.payroll_accrual.isChecked(),
            "social_accrual": self.social_accrual.isChecked(), "mappings": self._mapping_values(),
            "accrual_period": self.accrual_period.text().strip(),
        }
        self._config.update({"options": options, "bank_paths": self.bank_panel.paths,
                             "payroll_paths": self.payroll_panel.paths, "template_path": self._template_path,
                             "social_dates_text": self.social_dates.text(),
                             "export_format": self.export_format.currentData()})
        self._config.update({f"{kind}_paths": list(panel.paths) for kind, panel in self.file_panels.items()})
        self._config["company"] = self._company
        self._config.setdefault("company_settings", {})[self._company] = {
            key: copy.deepcopy(self._config.get(key)) for key in
            ("options", "social_dates_text", "bank_paths", "payroll_paths", "logistics_paths", "invoice_paths", "template_path")
        }
        try:
            save_config(self._config)
        except Exception as exc:
            self.statusBar().showMessage(f"本次设置暂未保存：{exc}")
            return False
        return True

    def dragEnterEvent(self, event):
        if self.pages.currentIndex() == 4:
            if self.ai_page is not None and not self.ai_page.is_busy and event.mimeData().hasUrls():
                event.acceptProposedAction()
            return
        if event.mimeData().hasUrls() and not self._busy:
            event.acceptProposedAction()

    def dropEvent(self, event):
        if self.pages.currentIndex() == 4:
            if self.ai_page is not None and not self.ai_page.is_busy:
                self.ai_page.add_paths([url.toLocalFile() for url in event.mimeData().urls() if url.isLocalFile()])
                event.acceptProposedAction()
            else:
                event.ignore()
            return
        if self._busy:
            event.ignore()
            return
        paths = [url.toLocalFile() for url in event.mimeData().urls() if url.isLocalFile()]
        choices = ["银行流水", "工资表", "物流 Excel", "发票 Excel"]
        selection, ok = QInputDialog.getItem(self, "选择资料类型", "这些文件属于哪类资料？也可以直接拖到对应区域。", choices, 0, False)
        if ok:
            list(self.file_panels.values())[choices.index(selection)].add_paths(paths)
        self._navigate(0)
        event.acceptProposedAction()

    def _restart_for_update(self):
        self._restart_after_update = True
        try:
            self.close()
        finally:
            self._restart_after_update = False

    def closeEvent(self, event):
        if self._busy:
            self._notify("正在处理文件，请等当前操作结束后再关闭。")
            event.ignore()
            return
        if (self.update_controller.has_ready_update and self.ai_page is not None
                and self.ai_page.is_busy):
            message = "AI 助手正在处理，请等当前操作结束后再更新。"
            self._notify(message)
            status = getattr(self.ai_page, "_status", None)
            if callable(status):
                status(message)
            event.ignore()
            return
        self._save_timer.stop()
        saved = self._save_settings()
        if self.update_controller.has_ready_update and not saved:
            self._notify("设置暂未保存，本次先不更新。请检查保存位置后重试。", "error")
            event.ignore()
            return
        if not self.update_controller.prepare_close(
                restart=getattr(self, "_restart_after_update", False)):
            event.ignore()
            return
        self.update_controller.shutdown()
        if self.ai_page is not None:
            self.ai_page.shutdown()
        event.accept()


def run_app():
    app = QApplication.instance() or QApplication(sys.argv)
    app.setApplicationName("给小雨的软件")
    app.setOrganizationName("XiaoYuVoucher")
    app.setFont(QFont("Microsoft YaHei UI", 10))
    lock = None
    if getattr(sys, 'frozen', False):
        from .update_installer import AppLock, startup_recovery_command
        app_dir = Path(sys.executable).parent
        lock = AppLock(app_dir)
        if not lock.acquire():
            QMessageBox.information(None, '软件已经打开', '软件已在运行或正在更新，请稍后再打开。')
            return 0
        try:
            recovery = startup_recovery_command(app_dir)
            if recovery:
                import subprocess
                environment = {key: value for key, value in os.environ.items() if not key.startswith('_PYI_')}
                environment['PYINSTALLER_RESET_ENVIRONMENT'] = '1'
                subprocess.Popen(recovery, cwd=str(Path(recovery[0]).parent), env=environment,
                                 creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
                lock.release()
                return 0
        except Exception:
            lock.release()
            raise
    try:
        window = MainWindow()
        window.show()
        # One check after the interface is ready. There is no periodic poll.
        QTimer.singleShot(0, window.update_controller.startup_check)
        return app.exec()
    finally:
        if lock is not None:
            lock.release()
