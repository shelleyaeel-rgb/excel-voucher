"""The quiet rose palette used by the portable desktop application."""

BACKGROUND = "#FFF1F6"
SURFACE = "#FFFCFD"
INK = "#392F38"
MUTED = "#796571"
ROSE = "#B83E73"
PALE_ROSE = "#F9DCE9"
BORDER = "#ECCDDD"


STYLESHEET = """
QWidget {
    font-family: "Microsoft YaHei UI", "Microsoft YaHei", "微软雅黑";
    font-size: 13px; color: #392F38;
}
QMainWindow, QWidget#workspace { background: #FFF1F6; }
QFrame#sidebar { background: #F9DCE9; border-right: 1px solid #EBC5D8; }
QLabel { background: transparent; }
QLabel#brand { font-size: 25px; font-weight: 700; color: #94395E; }
QLabel#brandSub { font-size: 12px; color: #8E6D80; }
QLabel#eyebrow { font-size: 12px; color: #9F6983; }
QLabel#hero { font-size: 24px; font-weight: 700; color: #4C3442; }
QLabel#subtitle, QLabel#muted { color: #796571; }
QLabel#small { font-size: 11px; color: #8B7B86; }
QLabel#sectionTitle { font-size: 16px; font-weight: 700; }
QLabel#statValue { font-size: 22px; font-weight: 700; color: #96385E; }
QLabel#statLabel { color: #796571; font-size: 12px; }
QFrame#card { background: #FFFCFD; border: 1px solid #ECCDDD; border-radius: 14px; }
QFrame#dropArea { background: #FFF7FB; border: 1px dashed #D5A9BE; border-radius: 10px; }
QFrame#dropArea[dragging="true"] { background: #F9DCE9; border: 2px solid #B83E73; }
QFrame#emptyPreview { background: #FFF8FB; border: 1px dashed #DAB5C8; border-radius: 14px; }
QLabel#step { color: #886071; padding: 8px 12px; background: #FAE5EE; border-radius: 8px; }
QLabel#step[active="true"] { color: #8E3358; background: #F3CDDF; font-weight: 700; }
QPushButton {
    background: #FFF9FC; border: 1px solid #E6BED1; border-radius: 8px;
    padding: 8px 15px; min-height: 18px; color: #70455B;
}
QPushButton:hover { background: #FBE2ED; border-color: #CA8AAA; }
QPushButton:pressed { background: #F4CDDF; }
QPushButton:disabled { color: #B5A6AF; border-color: #EADFE5; background: #F6F0F3; }
QPushButton#primary { color: white; background: #B83E73; border: 1px solid #B83E73; font-weight: 600; }
QPushButton#primary:hover { background: #A83265; border-color: #A83265; }
QPushButton#primary:pressed { background: #922953; }
QPushButton#primary:disabled { background: #D9B7C6; border-color: #D9B7C6; color: #FFF8FA; }
QPushButton#nav { text-align: left; background: transparent; border: none; color: #846477; padding: 13px 17px; font-size: 14px; }
QPushButton#nav:hover { background: #F2CADE; }
QPushButton#nav[selected="true"] { background: #FFF5FA; color: #96385E; font-weight: 700; }
QPushButton#remove { border: none; padding: 4px 8px; color: #A26A83; background: transparent; font-size: 12px; }
QPushButton#remove:hover { background: #F9E7EF; color: #913350; }
QPushButton#link { border: none; background: transparent; color: #A13963; padding: 4px 5px; }
QLineEdit, QSpinBox, QDoubleSpinBox, QComboBox {
    background: #FFFAFC; border: 1px solid #E6CAD8; border-radius: 7px;
    padding: 7px 9px; min-height: 20px; selection-background-color: #EDBBD1;
}
QLineEdit:focus, QSpinBox:focus, QDoubleSpinBox:focus, QComboBox:focus { border: 1px solid #BA6A8C; }
QLineEdit:disabled, QComboBox:disabled, QDoubleSpinBox:disabled { background: #F6F1F4; color: #AD9AA6; }
QComboBox::drop-down { border: none; width: 24px; }
QComboBox QAbstractItemView { background: #FFFCFD; border: 1px solid #E6CAD8; selection-background-color: #F5CDDF; selection-color: #632E49; padding: 4px; }
QCheckBox { spacing: 9px; padding: 4px 0; }
QCheckBox::indicator { width: 16px; height: 16px; }
QScrollArea { border: none; background: transparent; }
QScrollArea > QWidget > QWidget { background: transparent; }
QListWidget { border: none; background: transparent; outline: none; }
QListWidget::item { border-bottom: 1px solid #F3DEE8; }
QListWidget::item:selected { background: #F8DAE8; }
QTableWidget, QTreeWidget { background: #FFFCFD; border: 1px solid #ECCDDD; border-radius: 8px; gridline-color: #F2DFE9; outline: none; selection-background-color: #F5CDDF; selection-color: #4B3341; }
QTableWidget::item { padding: 5px; }
QHeaderView::section { background: #F9E0EB; color: #70465D; border: none; border-bottom: 1px solid #ECCDDD; padding: 9px 7px; font-weight: 600; }
QTableCornerButton::section { background: #F9E0EB; border: none; }
QTreeWidget::item { padding: 5px 2px; }
QSplitter::handle:vertical { background: #EBC4D7; height: 8px; margin: 3px 32px; border-radius: 3px; }
QSplitter::handle:vertical:hover { background: #D999B8; }
QScrollBar:vertical { background: transparent; width: 9px; margin: 2px; }
QScrollBar::handle:vertical { background: #DFAFC5; border-radius: 3px; min-height: 28px; }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical { background: transparent; }
QScrollBar:horizontal { background: transparent; height: 9px; margin: 2px; }
QScrollBar::handle:horizontal { background: #DFAFC5; border-radius: 3px; min-width: 28px; }
QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal { width: 0; }
QScrollBar::add-page:horizontal, QScrollBar::sub-page:horizontal { background: transparent; }
QLabel#notice { padding: 10px 13px; background: #F8DDE9; color: #75445C; border-radius: 8px; }
QLabel#notice[tone="error"] { background: #FFF0E7; color: #99502F; }
QLabel#notice[tone="success"] { background: #EDF5F0; color: #3F7054; }
QProgressBar { background: #F2D3E2; border: none; border-radius: 3px; max-height: 5px; }
QProgressBar::chunk { background: #BD5386; border-radius: 3px; }
QStatusBar { background: #FFF1F6; color: #806574; border-top: 1px solid #ECD1DF; font-size: 11px; }
QToolTip { background: #FFFDFE; color: #563C4C; border: 1px solid #DEC4D1; padding: 5px; }
"""
