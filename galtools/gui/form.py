# -*- coding: utf-8 -*-
"""依 ToolSpec.fields 自动生成参数表单。

本模块不认识任何具体工具。要支持新的字段种类，往 _BUILDERS 加一个构造
函数即可，不必改动别处。
"""
import os

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QFileDialog, QFormLayout, QHBoxLayout, QLabel,
    QLineEdit, QPushButton, QVBoxLayout, QWidget,
)

from ..core.spec import BOOL, DIR, NUMBER, TEXT

HISTORY_LIMIT = 5


def normalize_path(raw):
    """沿用命令行版 ask_directory 的清洗：Windows「复制为路径」总是带引号。"""
    raw = raw.strip().strip('"').strip("'").rstrip('\\/').strip()
    return raw


def parse_number(raw):
    """空串返回 None（表示不限制）；容忍 6s / 6秒 这类习惯写法。"""
    raw = raw.strip()
    while raw and (raw[-1] in ('s', 'S') or raw.endswith('秒')):
        raw = raw[:-1].rstrip('秒').strip()
    if not raw:
        return None
    return float(raw)


class HistoryEdit(QComboBox):
    """可编辑下拉框：既能敲/粘，也能从最近用过的里挑。"""

    def __init__(self, placeholder=''):
        super().__init__()
        self.setEditable(True)
        self.setInsertPolicy(QComboBox.NoInsert)
        self.lineEdit().setPlaceholderText(placeholder)


class PathEdit(HistoryEdit):
    """再加拖放：拖文件夹进来即填路径，拖文件则取其所在目录。"""

    def __init__(self, placeholder=''):
        super().__init__(placeholder)
        self.setAcceptDrops(True)
        self.lineEdit().setAcceptDrops(False)

    def dragEnterEvent(self, event):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()

    def dragMoveEvent(self, event):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()

    def dropEvent(self, event):
        urls = event.mimeData().urls()
        if not urls:
            return
        path = urls[0].toLocalFile()
        # 拖的是文件就取其所在目录，省得用户先退一级。
        if path and not os.path.isdir(path):
            path = os.path.dirname(path)
        self.setCurrentText(path)
        event.acceptProposedAction()


def _build_dir(spec_field):
    edit = PathEdit(spec_field.placeholder)
    browse = QPushButton('浏览…')
    row = QWidget()
    layout = QHBoxLayout(row)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.addWidget(edit, 1)
    layout.addWidget(browse)

    def pick():
        start = normalize_path(edit.currentText()) or ''
        chosen = QFileDialog.getExistingDirectory(row, spec_field.label, start)
        if chosen:
            edit.setCurrentText(os.path.normpath(chosen))

    browse.clicked.connect(pick)
    return row, edit, edit.currentTextChanged, lambda: normalize_path(edit.currentText())


def _build_bool(spec_field):
    box = QCheckBox()
    box.setChecked(bool(spec_field.default))
    return box, box, box.toggled, box.isChecked


def _build_number(spec_field):
    edit = QLineEdit()
    if spec_field.default is not None:
        edit.setText(_fmt_default(spec_field.default))
    edit.setPlaceholderText(spec_field.placeholder)
    edit.setMaximumWidth(140)
    return edit, edit, edit.textChanged, lambda: parse_number(edit.text())


def _build_text(spec_field):
    # history=True 的字段换成可编辑下拉框：声优名、id 这类值往往要反复查同几个。
    if spec_field.history:
        edit = HistoryEdit(spec_field.placeholder)
        if spec_field.default is not None:
            edit.setCurrentText(str(spec_field.default))
        return (edit, edit, edit.currentTextChanged,
                lambda: edit.currentText().strip())
    edit = QLineEdit()
    if spec_field.default is not None:
        edit.setText(str(spec_field.default))
    edit.setPlaceholderText(spec_field.placeholder)
    return edit, edit, edit.textChanged, lambda: edit.text().strip()


def _keeps_history(spec_field):
    """哪些字段要记「最近用过」：目录一律记，别的看 Field.history。"""
    return spec_field.kind == DIR or spec_field.history


def _fmt_default(value):
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


# kind -> 构造函数，返回 (放进布局的控件, 焦点控件, 变更信号, 取值函数)
_BUILDERS = {
    DIR: _build_dir,
    BOOL: _build_bool,
    NUMBER: _build_number,
    TEXT: _build_text,
}


class ToolForm(QWidget):
    # 参数有变；bool 表示改动的是否为 rescan 字段（预览数据集因此失效）。
    changed = Signal(bool)

    def __init__(self, spec, settings):
        super().__init__()
        self.spec = spec
        self._settings = settings
        self._getters = {}
        self._editors = {}
        self._containers = {}
        self._errors = {}

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        form = QFormLayout()
        form.setLabelAlignment(Qt.AlignRight | Qt.AlignVCenter)
        outer.addLayout(form)

        for f in spec.fields:
            builder = _BUILDERS.get(f.kind)
            if builder is None:
                continue
            widget, editor, signal, getter = builder(f)
            self._getters[f.key] = getter
            self._editors[f.key] = editor
            self._containers[f.key] = widget
            tip = f.help or f.placeholder
            if tip:
                widget.setToolTip(tip)
                editor.setToolTip(tip)

            error = QLabel()
            error.setStyleSheet('color: #c0392b;')
            error.setWordWrap(True)
            error.hide()
            self._errors[f.key] = error

            label = f.label if f.required else f.label + '（可选）'
            form.addRow(label, widget)
            form.addRow('', error)
            signal.connect(lambda *_, rescan=f.rescan: self.changed.emit(rescan))

        self._restore_history()

    # ---------- 取值与校验 ----------
    def values(self):
        out = {}
        for key, getter in self._getters.items():
            try:
                out[key] = getter()
            except ValueError:
                out[key] = None
        return out

    def bad_number_keys(self):
        """填了内容但解析不出数字的字段。"""
        bad = []
        for f in self.spec.fields:
            if f.kind != NUMBER:
                continue
            editor = self._editors.get(f.key)
            if editor is None or not editor.text().strip():
                continue
            try:
                self._getters[f.key]()
            except ValueError:
                bad.append(f.key)
        return bad

    def missing_required_keys(self):
        values = self.values()
        return [f.key for f in self.spec.fields
                if f.required and values.get(f.key) in (None, '')]

    def set_errors(self, mapping):
        for key, label in self._errors.items():
            msg = mapping.get(key)
            label.setText(msg or '')
            label.setVisible(bool(msg))

    def set_editable(self, enabled):
        for widget in self._containers.values():
            widget.setEnabled(enabled)

    # ---------- 输入历史 ----------
    def _history_key(self, field_key):
        return 'history/%s/%s' % (self.spec.id, field_key)

    def _restore_history(self):
        for f in self.spec.fields:
            if not _keeps_history(f):
                continue
            items = self._settings.value(self._history_key(f.key)) or []
            if isinstance(items, str):
                items = [items]
            if not items:
                continue      # 没历史就别动，免得把 Field.default 洗掉
            editor = self._editors[f.key]
            editor.addItems(items)
            editor.setCurrentText(items[0])

    def remember_paths(self):
        """把本次实际用过的值记进历史，供下次直接下拉选。"""
        for f in self.spec.fields:
            if not _keeps_history(f):
                continue
            value = self._getters[f.key]()
            if not value:
                continue
            items = self._settings.value(self._history_key(f.key)) or []
            if isinstance(items, str):
                items = [items]
            items = [value] + [i for i in items if i != value]
            del items[HISTORY_LIMIT:]
            self._settings.setValue(self._history_key(f.key), items)
            editor = self._editors[f.key]
            editor.blockSignals(True)
            editor.clear()
            editor.addItems(items)
            editor.setCurrentText(value)
            editor.blockSignals(False)
