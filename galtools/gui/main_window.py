# -*- coding: utf-8 -*-
"""主窗口：左侧按分类列出工具，右侧是自动生成的参数页，底部是共用的进度与日志。"""
import html
import os
import time

from PySide6.QtCore import QSettings, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QDesktopServices, QFont, QGuiApplication
from PySide6.QtWidgets import (
    QAbstractItemView, QFrame, QHBoxLayout, QLabel, QMainWindow, QMessageBox,
    QPlainTextEdit, QProgressBar, QPushButton, QSplitter, QStackedWidget,
    QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget,
)

from ..core.registry import discover
from .form import ToolForm
from .worker import Bridge, JobRunner

PREVIEW_DEBOUNCE_MS = 300
MAX_LOGGED_FAILURES = 20

LEVEL_COLORS = {'warn': '#b7791f', 'error': '#c0392b', 'ok': '#2d7d46'}


class ToolPage(QWidget):
    previewRequested = Signal()
    runRequested = Signal()
    cancelRequested = Signal()

    def __init__(self, spec, settings):
        super().__init__()
        self.spec = spec
        self.session = {}
        self.busy = False
        self.preview_ok = None
        self.has_rescan = any(f.rescan for f in spec.fields)
        # 有慢字段的工具开局即为过期：几千个文件的扫描不该在打开界面时自动发生。
        self.stale = self.has_rescan and spec.preview is not None

        layout = QVBoxLayout(self)
        title = QLabel(spec.name)
        font = title.font()
        font.setPointSize(font.pointSize() + 3)
        font.setBold(True)
        title.setFont(font)
        desc = QLabel(spec.description)
        desc.setWordWrap(True)
        desc.setStyleSheet('color: #666;')
        layout.addWidget(title)
        layout.addWidget(desc)

        line = QFrame()
        line.setFrameShape(QFrame.HLine)
        line.setFrameShadow(QFrame.Sunken)
        layout.addWidget(line)

        self.form = ToolForm(spec, settings)
        layout.addWidget(self.form)

        buttons = QHBoxLayout()
        self.scan_btn = QPushButton('扫描')
        self.scan_btn.setVisible(self.has_rescan and spec.preview is not None)
        buttons.addWidget(self.scan_btn)
        buttons.addStretch(1)
        layout.addLayout(buttons)

        self.preview_box = QPlainTextEdit()
        self.preview_box.setReadOnly(True)
        self.preview_box.setMaximumHeight(120)
        self.preview_box.setVisible(spec.preview is not None)
        layout.addWidget(self.preview_box)

        self.start_btn = QPushButton('开始')
        self.start_btn.setMinimumHeight(32)
        layout.addWidget(self.start_btn)
        layout.addStretch(1)

        self._debounce = QTimer(self)
        self._debounce.setSingleShot(True)
        self._debounce.setInterval(PREVIEW_DEBOUNCE_MS)
        self._debounce.timeout.connect(self.previewRequested.emit)

        self.form.changed.connect(self._on_changed)
        self.scan_btn.clicked.connect(self._on_scan)
        self.start_btn.clicked.connect(self._on_start)

        if self.stale:
            self.preview_box.setPlainText('尚未扫描，点「扫描」统计。')
        self.refresh_start_enabled()

    # ---------- 交互 ----------
    def _on_changed(self, rescan):
        if rescan:
            self.mark_stale()
        elif self.spec.preview is not None and not self.stale:
            self._debounce.start()
        self.refresh_start_enabled()

    def _on_scan(self):
        self.stale = False
        self._debounce.stop()
        self.previewRequested.emit()

    def _on_start(self):
        if self.busy:
            self.cancelRequested.emit()
        else:
            self.runRequested.emit()

    def mark_stale(self, reason='参数已改，点「扫描」重新统计。'):
        if not self.has_rescan or self.spec.preview is None:
            return
        self.stale = True
        self.preview_ok = None
        self._debounce.stop()
        self.preview_box.setPlainText(reason)

    # ---------- 状态 ----------
    def validation_errors(self):
        errors = {}
        for key in self.form.missing_required_keys():
            errors[key] = '必填'
        for key in self.form.bad_number_keys():
            errors[key] = '不是有效数字'
        if not errors and self.spec.validate is not None:
            for key, msg in self.spec.validate(self.form.values()):
                errors[key] = msg
        return errors

    def refresh_start_enabled(self):
        if self.busy:
            self.start_btn.setEnabled(True)
            return
        errors = self.validation_errors()
        self.form.set_errors(errors)
        ok = not errors
        if self.spec.preview is not None:
            ok = ok and not self.stale and self.preview_ok is True
        self.start_btn.setEnabled(ok)

    def show_preview(self, result):
        text = result.summary
        if result.warnings:
            text += '\n' + '\n'.join('[!] ' + w for w in result.warnings)
        self.preview_box.setPlainText(text)
        self.preview_ok = bool(result.ok)
        self.refresh_start_enabled()

    def show_preview_error(self, msg):
        self.preview_box.setPlainText('预览失败：' + msg)
        self.preview_ok = False
        self.refresh_start_enabled()

    def set_busy(self, busy):
        self.busy = busy
        self.form.set_editable(not busy)
        self.scan_btn.setEnabled(not busy)
        self.start_btn.setText('取消' if busy else '开始')
        self.refresh_start_enabled()


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle('galtools')
        self.settings = QSettings('galtools', 'galtools')
        self.bridge = Bridge()
        self.runner = JobRunner(self.bridge)
        self.pages = {}
        self._preview_page = None
        self._active_page = None
        self._outputs = []
        self._run_started = 0.0

        self.tree = QTreeWidget()
        self.tree.setHeaderHidden(True)
        self.tree.setMinimumWidth(180)
        self.tree.setSelectionMode(QAbstractItemView.SingleSelection)
        self.stack = QStackedWidget()

        top = QSplitter(Qt.Horizontal)
        left = QWidget()
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(0, 0, 0, 0)
        left_layout.addWidget(QLabel('工具'))
        left_layout.addWidget(self.tree)
        top.addWidget(left)
        top.addWidget(self.stack)
        top.setStretchFactor(1, 1)

        self.progress = QProgressBar()
        self.progress.setTextVisible(True)
        self.status = QLabel('就绪')
        self.status.setStyleSheet('color: #666;')
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setFont(QFont('Consolas', 9))
        self.log.setMinimumHeight(140)

        self.open_btn = QPushButton('打开输出目录')
        self.open_btn.setEnabled(False)
        self.copy_btn = QPushButton('复制日志')
        self.clear_btn = QPushButton('清空日志')
        log_buttons = QHBoxLayout()
        log_buttons.addWidget(self.open_btn)
        log_buttons.addWidget(self.copy_btn)
        log_buttons.addWidget(self.clear_btn)
        log_buttons.addStretch(1)

        bottom = QWidget()
        bottom_layout = QVBoxLayout(bottom)
        bottom_layout.setContentsMargins(0, 0, 0, 0)
        bottom_layout.addWidget(self.progress)
        bottom_layout.addWidget(self.status)
        bottom_layout.addWidget(self.log)
        bottom_layout.addLayout(log_buttons)

        split = QSplitter(Qt.Vertical)
        split.addWidget(top)
        split.addWidget(bottom)
        split.setStretchFactor(0, 3)
        split.setStretchFactor(1, 2)
        self.setCentralWidget(split)

        self.open_btn.clicked.connect(self._open_outputs)
        self.copy_btn.clicked.connect(self._copy_log)
        self.clear_btn.clicked.connect(self.log.clear)
        self.tree.currentItemChanged.connect(self._on_tool_selected)

        self.bridge.log.connect(self._append_log)
        self.bridge.progress.connect(self._on_progress)
        self.bridge.preview_ready.connect(self._on_preview_ready)
        self.bridge.preview_failed.connect(self._on_preview_failed)
        self.bridge.run_finished.connect(self._on_run_finished)
        self.bridge.run_cancelled.connect(self._on_run_cancelled)
        self.bridge.run_failed.connect(self._on_run_failed)

        self._load_tools()
        self._restore_geometry()

    # ---------- 装载 ----------
    def _load_tools(self):
        tools, errors = discover()
        categories = {}
        for spec in tools:
            page = ToolPage(spec, self.settings)
            page.previewRequested.connect(
                lambda p=page: self._request_preview(p))
            page.runRequested.connect(lambda p=page: self._start_run(p))
            page.cancelRequested.connect(self._cancel_run)
            index = self.stack.addWidget(page)
            self.pages[spec.id] = page

            parent = categories.get(spec.category)
            if parent is None:
                parent = QTreeWidgetItem(self.tree, [spec.category])
                parent.setFlags(Qt.ItemIsEnabled)
                parent.setExpanded(True)
                categories[spec.category] = parent
            item = QTreeWidgetItem(parent, [spec.name])
            item.setData(0, Qt.UserRole, index)
            item.setToolTip(0, spec.description)

        for err in errors:
            self._append_log('工具 %s 加载失败：\n%s' % (err.name, err.traceback), 'error')
            parent = categories.get('加载失败')
            if parent is None:
                parent = QTreeWidgetItem(self.tree, ['加载失败'])
                parent.setFlags(Qt.ItemIsEnabled)
                parent.setExpanded(True)
                categories['加载失败'] = parent
            item = QTreeWidgetItem(parent, [err.name])
            item.setDisabled(True)
            item.setToolTip(0, err.traceback)

        if not tools:
            self._append_log('galtools/tools/ 下没有可用工具。', 'warn')
            return
        self.tree.expandAll()
        first = self.tree.topLevelItem(0).child(0)
        self.tree.setCurrentItem(first)

    def _restore_geometry(self):
        geo = self.settings.value('window/geometry')
        if geo is not None:
            self.restoreGeometry(geo)
        else:
            self.resize(1000, 720)

    def closeEvent(self, event):
        # 先停活再存设置：留着的线程会在窗口析构后 emit 到已删除的 Bridge。
        self.runner.stop()
        self.settings.setValue('window/geometry', self.saveGeometry())
        super().closeEvent(event)

    # ---------- 工具切换 ----------
    def _on_tool_selected(self, current, _previous):
        if current is None:
            return
        index = current.data(0, Qt.UserRole)
        if index is None:
            return
        self.stack.setCurrentIndex(index)
        page = self.stack.widget(index)
        self._active_page = page
        # 廉价预览（如仅列目录）立刻跑；慢的等用户点扫描。
        if page.spec.preview is not None and not page.stale:
            self._request_preview(page)

    # ---------- 预览与执行 ----------
    def _request_preview(self, page):
        if self.runner.busy and page.busy:
            return
        if page.validation_errors():
            return
        self._preview_page = page
        self.runner.request_preview(page.spec, page.form.values(), page.session)

    def _on_preview_ready(self, gen, result):
        if not self.runner.is_current_preview(gen) or self._preview_page is None:
            return
        self._preview_page.show_preview(result)
        self.progress.setValue(0)
        self.progress.setMaximum(100)
        self.status.setText('就绪')

    def _on_preview_failed(self, gen, msg):
        if not self.runner.is_current_preview(gen) or self._preview_page is None:
            return
        self._preview_page.show_preview_error(msg)

    def _start_run(self, page):
        errors = page.validation_errors()
        if errors:
            page.form.set_errors(errors)
            return
        params = page.form.values()
        page.form.remember_paths()
        page.set_busy(True)
        self.tree.setEnabled(False)
        self._outputs = []
        self.open_btn.setEnabled(False)
        self.progress.setValue(0)
        self._run_started = time.monotonic()
        self._active_page = page
        self._append_log('开始：%s' % page.spec.name, 'ok')
        self.runner.start_run(page.spec, params, page.session)

    def _cancel_run(self):
        self._append_log('正在取消…', 'warn')
        self.runner.cancel()

    def _finish_run(self):
        page = self._active_page
        if page is not None:
            # 跑完一轮后盘上已经变了（音频工具的输出目录就建在源目录内），预览
            # 里的数字不再可信。重新标记为过期，免得「开始」还亮着、一点就悄悄
            # 触发那次本该由用户明确发起的慢扫描。先标记再解冻，让 set_busy
            # 末尾的 refresh_start_enabled 一次把按钮状态算对。
            page.mark_stale('已跑过一轮，点「扫描」重新统计。')
            page.set_busy(False)
        self.tree.setEnabled(True)

    def _on_run_finished(self, result):
        self._finish_run()
        elapsed = time.monotonic() - self._run_started
        self.progress.setMaximum(100)
        self.progress.setValue(100)
        self.status.setText('完成，耗时 %.1f 秒' % elapsed)
        for warning in result.warnings:
            self._append_log(warning, 'warn')
        if result.failures:
            self._append_log('失败 %d 项：' % len(result.failures), 'warn')
            for name, reason in result.failures[:MAX_LOGGED_FAILURES]:
                self._append_log('  %s: %s' % (name, reason), 'warn')
            if len(result.failures) > MAX_LOGGED_FAILURES:
                self._append_log('  …其余 %d 项见输出目录内的清单'
                                 % (len(result.failures) - MAX_LOGGED_FAILURES), 'warn')
        self._append_log(result.summary, 'ok')
        self._outputs = list(result.output_paths)
        self.open_btn.setEnabled(bool(self._outputs))

    def _on_run_cancelled(self, partial):
        self._finish_run()
        self.status.setText('已取消')
        self._append_log('已取消。已经写出的文件保留在输出目录中。', 'warn')
        if partial is not None and partial.summary:
            self._append_log(partial.summary, 'warn')

    def _on_run_failed(self, tb):
        self._finish_run()
        self.status.setText('出错')
        self._append_log(tb, 'error')

    # ---------- 底部面板 ----------
    def _on_progress(self, done, total, note):
        if total <= 0:
            self.progress.setMaximum(0)
        else:
            self.progress.setMaximum(total)
            self.progress.setValue(done)
        self.status.setText(note or '%d/%d' % (done, total))

    def _append_log(self, msg, level='info'):
        # 消息里的首尾空行是给终端排版用的（工具的 log 文本 CLI 与 GUI 共用），
        # 在带时间戳的日志控件里只会留下空行，去掉。
        stamp = time.strftime('%H:%M:%S')
        color = LEVEL_COLORS.get(level)
        body = html.escape(msg.strip('\n')).replace('\n', '<br>')
        if color:
            body = '<span style="color:%s">%s</span>' % (color, body)
        # appendHtml 不会自己把视图拉到底，日志一超出可见高度就永远停在开头。
        # 只在原本已经贴底时才跟随，用户上翻查看时不抢走滚动位置。
        bar = self.log.verticalScrollBar()
        follow = bar.value() == bar.maximum()
        self.log.appendHtml('<span style="color:#999">%s</span>  %s' % (stamp, body))
        if follow:
            bar.setValue(bar.maximum())

    def _copy_log(self):
        QGuiApplication.clipboard().setText(self.log.toPlainText())
        self.status.setText('日志已复制到剪贴板')

    def _open_outputs(self):
        for path in self._outputs:
            target = path if os.path.isdir(path) else os.path.dirname(path)
            if os.path.isdir(target):
                QDesktopServices.openUrl(QUrl.fromLocalFile(target))
                return
        QMessageBox.information(self, 'galtools', '输出目录不存在。')
