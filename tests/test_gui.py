# -*- coding: utf-8 -*-
"""GUI 的自动化回归测试。

没装 PySide6 就整个文件跳过：命令行用法不该因为缺 Qt 就连测试都跑不了
（tests/test_registry.py 反过来钉住工具在 import 期不许把 Qt 拖进来）。

离屏跑，QSettings 一律指到 tmp_path——PySide6 里 QSettings(org, app) 不看
setDefaultFormat()，照样写注册表，不换会把测试用的路径写进用户的真实配置。

与 tests/manual/ 下那三个脚本分工不同：那边走完整一轮并打印给人眼看，这里只钉
「坏了也没人喊」的几个分支（活计换代、越期静音、取消、预览失败的收尾）。
"""
import os
import threading
import time

import pytest

pytest.importorskip('PySide6')
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from PySide6.QtCore import QEventLoop, QSettings, Qt               # noqa: E402
from PySide6.QtWidgets import QApplication                     # noqa: E402

from galtools.core.context import Cancelled                    # noqa: E402
from galtools.core.spec import (                                # noqa: E402
    TEXT, Field, PreviewResult, RunResult, Table, ToolSpec,
)
from galtools.gui import main_window as mw                     # noqa: E402
from galtools.gui.form import normalize_path                   # noqa: E402
from galtools.gui.worker import Bridge, JobRunner              # noqa: E402


@pytest.fixture(scope='session')
def qt_app():
    """整个会话一个 QApplication。Qt 不允许建第二个，也不该析构它。"""
    return QApplication.instance() or QApplication([])


def window(monkeypatch, tmp_path):
    ini = str(tmp_path / 'settings.ini')
    monkeypatch.setattr(mw, 'QSettings',
                        lambda *_args: QSettings(ini, QSettings.IniFormat))
    return mw.MainWindow()


def wait_until(qt_app, predicate, timeout=5):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        qt_app.processEvents(QEventLoop.AllEvents, 20)
        if predicate():
            return True
        time.sleep(0.005)
    return predicate()


def spec_with(run):
    return ToolSpec(id='fake', name='假工具', category='测试',
                    description='只为驱动 JobRunner',
                    fields=(Field(key='x', kind=TEXT, label='x'),), run=run)


def preview_spec(preview):
    return ToolSpec(id='preview', name='假预览', category='测试',
                    description='只为驱动 JobRunner',
                    fields=(Field(key='x', kind=TEXT, label='x'),),
                    run=lambda _params, _ctx: RunResult(), preview=preview)


def test_superseded_queued_events_are_ignored(qt_app):
    bridge = Bridge()
    runner = JobRunner(bridge)
    release = threading.Event()
    started = threading.Event()
    seen = []
    bridge.log.connect(
        lambda token, msg, _level: seen.append((token, msg))
        if runner.is_current(token) else None)

    def old_preview(_params, ctx):
        ctx.log('旧日志')
        started.set()
        release.wait(5)
        return PreviewResult(summary='旧结果')

    old_token = runner.request_preview(preview_spec(old_preview), {}, {})
    assert started.wait(5)
    qt_app.processEvents()
    assert seen == [(old_token, '旧日志')]
    new_token = runner.request_preview(
        preview_spec(lambda _params, _ctx: PreviewResult(summary='新结果')), {}, {})
    assert new_token != old_token
    release.set()
    assert wait_until(qt_app, lambda: not runner.busy)
    assert not runner.is_current(old_token)
    assert seen == [(old_token, '旧日志')]


def test_jobs_are_serialized_and_pending_run_can_be_cancelled(qt_app):
    bridge = Bridge()
    runner = JobRunner(bridge)
    preview_started = threading.Event()
    preview_release = threading.Event()
    run_started = threading.Event()
    cancelled = []
    logs = []
    bridge.run_cancelled.connect(
        lambda token, partial: cancelled.append((token, partial)), Qt.DirectConnection)
    bridge.log.connect(
        lambda token, msg, _level: logs.append((token, msg))
        if runner.is_current(token) else None)

    def slow_preview(_params, ctx):
        preview_started.set()
        preview_release.wait(5)
        ctx.log('取消后的旧日志')
        return PreviewResult()

    def run(_params, _ctx):
        run_started.set()
        return RunResult()

    preview_token = runner.request_preview(preview_spec(slow_preview), {}, {})
    assert preview_started.wait(5)
    run_token = runner.start_run(spec_with(run), {}, {})
    assert runner.kind == 'run'
    assert runner.state == 'cancelling'
    runner.cancel()
    assert not runner.is_current(preview_token)
    preview_release.set()
    assert wait_until(qt_app, lambda: not runner.busy)
    assert not run_started.is_set()
    assert logs == []
    assert [token for token, _partial in cancelled] == [run_token]


def test_pending_run_starts_only_after_old_preview_exits(qt_app):
    bridge = Bridge()
    runner = JobRunner(bridge)
    preview_started = threading.Event()
    preview_release = threading.Event()
    run_started = threading.Event()

    def slow_preview(_params, _ctx):
        preview_started.set()
        preview_release.wait(5)
        return PreviewResult()

    runner.request_preview(preview_spec(slow_preview), {}, {})
    assert preview_started.wait(5)
    run_token = runner.start_run(
        spec_with(lambda _params, _ctx: run_started.set() or RunResult()), {}, {})
    assert runner.is_current(run_token)
    assert not run_started.is_set()
    assert runner.request_preview(preview_spec(lambda *_: PreviewResult()), {}, {}) == 0
    assert not run_started.is_set()
    preview_release.set()
    assert wait_until(qt_app, run_started.is_set)
    assert wait_until(qt_app, lambda: not runner.busy)
    assert runner.state == 'idle'


def test_stop_drops_pending_and_waits_for_active_exit(qt_app):
    bridge = Bridge()
    runner = JobRunner(bridge)
    preview_started = threading.Event()
    preview_release = threading.Event()
    run_started = threading.Event()
    idle = []
    bridge.idle.connect(lambda: idle.append(True))

    def slow_preview(_params, _ctx):
        preview_started.set()
        preview_release.wait(5)
        return PreviewResult()

    runner.request_preview(preview_spec(slow_preview), {}, {})
    assert preview_started.wait(5)
    runner.start_run(
        spec_with(lambda _params, _ctx: run_started.set() or RunResult()), {}, {})
    runner.stop()
    assert runner.state == 'closing'
    assert runner.request_preview(preview_spec(lambda *_: PreviewResult()), {}, {}) == 0
    qt_app.processEvents()
    assert idle == []
    preview_release.set()
    assert wait_until(qt_app, lambda: idle == [True])
    assert not run_started.is_set()
    assert not runner.busy


def test_cancelling_a_run_reports_the_partial_result(qt_app):
    bridge = Bridge()
    got = []
    bridge.run_cancelled.connect(
        lambda _token, partial: got.append(partial), Qt.DirectConnection)
    runner = JobRunner(bridge)
    running = threading.Event()

    def slow(_params, ctx):
        running.set()
        try:
            while True:
                ctx.check_cancel()
                time.sleep(0.005)
        except Cancelled as stop:
            stop.partial = RunResult(summary='已经做了一半')
            raise

    runner.start_run(spec_with(slow), {}, {})
    assert running.wait(5)
    runner.cancel()
    assert wait_until(qt_app, lambda: not runner.busy)
    assert [result.summary for result in got] == ['已经做了一半']


def test_a_failed_preview_puts_the_progress_bar_back(qt_app, monkeypatch,
                                                     tmp_path):
    """预览一开头的 progress(0, 0, …) 把进度条切成了无限滚动。失败时不收回来的话
    它会一直滚、状态栏也一直停在「正在查…」，看着像还在跑。"""
    win = window(monkeypatch, tmp_path)
    try:
        page = win.pages['vndb_voiced']
        token = 17
        monkeypatch.setattr(win.runner, 'is_current', lambda got: got == token)
        win._preview_requests[token] = (page, page.form.values())
        win.progress.setMaximum(0)          # 预览把它切成了无限滚动
        win.status.setText('正在查 vndb…')
        win._on_preview_failed(token, 'boom')
        assert win.progress.maximum() == 100
        assert win.status.text() == '预览失败'
        assert page.preview_ok is False
        assert not page.start_btn.isEnabled()
    finally:
        win.close()


def test_starting_a_run_takes_over_the_progress_bar(qt_app, monkeypatch,
                                                    tmp_path):
    """预览开头那句 progress(0, 0, …) 把进度条切成无限滚动，而运行取消/出错时只
    改状态栏文字（要留住已完成的进度）。所以接管必须发生在起跑的那一刻，否则
    「预览过一次 → 开始 → 取消」之后进度条会一直滚，看着像还在跑。"""
    win = window(monkeypatch, tmp_path)
    try:
        page = win.pages['mjo_text']
        page.form._editors['src_dir'].setCurrentText(str(tmp_path))
        assert page.validation_errors() == {}          # 证明起跑不会被校验拦下
        monkeypatch.setattr(win.runner, 'start_run',
                            lambda *a, **k: 23)       # 不真起线程
        monkeypatch.setattr(win.runner, 'is_current', lambda token: token == 23)

        win.progress.setMaximum(0)                  # 预览留下的无限滚动
        assert win.progress.maximum() == 0

        win._start_run(page)
        assert win.progress.maximum() == 100

        win._on_run_cancelled(23, None)
        assert win.progress.maximum() == 100
        assert win.status.text() == '已取消'
    finally:
        win.close()


def test_a_successful_preview_remembers_the_directory(qt_app, monkeypatch,
                                                      tmp_path):
    """只看不导的用法（翻库、看统计）从不点「开始」，_start_run 里的
    remember_paths 因此永远不跑，路径每次开界面都得重新粘一遍。"""
    db = tmp_path / 'db'
    db.mkdir()
    win = window(monkeypatch, tmp_path)
    try:
        page = win.pages['seiyuu_db']
        page.form._editors['db_dir'].setCurrentText(str(db))
        page.form._editors['who'].setCurrentText('s1')
        assert page.validation_errors() == {}     # 证明预览不会被校验拦下
        token = 29
        monkeypatch.setattr(win.runner, 'request_preview',
                            lambda *a, **k: token)     # 不真起线程
        monkeypatch.setattr(win.runner, 'is_current', lambda got: got == token)
        win._active_page = page
        win._request_preview(page)
        win._on_preview_ready(token, PreviewResult(summary='只看不导', ok=False))
    finally:
        win.close()

    again = window(monkeypatch, tmp_path)
    try:
        values = again.pages['seiyuu_db'].form.values()
        assert values['db_dir'] == str(db)
        # 只记目录。声优名这类 TEXT 历史仍只在真跑过一轮之后才记。
        assert values['who'] == ''
    finally:
        again.close()


def test_remembering_a_directory_does_not_disturb_typing(qt_app, monkeypatch,
                                                         tmp_path):
    """预览是敲字过程中自动触发的。remember_paths 那套「清空下拉项再重填」会把
    光标从用户正在输入的那一格里踢出去，所以这条路径只许写设置。"""
    win = window(monkeypatch, tmp_path)
    try:
        form = win.pages['seiyuu_db'].form
        editor = form._editors['db_dir']
        half_typed = str(tmp_path) + os.sep + '还没打完'
        editor.setCurrentText(half_typed)
        changes = []
        editor.currentTextChanged.connect(changes.append)

        form.remember_dirs({'db_dir': str(tmp_path)})

        assert changes == []
        assert editor.currentText() == half_typed
        assert editor.count() == 0
        assert form._settings.value(form._history_key('db_dir')) == [str(tmp_path)]
    finally:
        win.close()


def test_a_directory_that_does_not_exist_is_not_remembered(qt_app, monkeypatch,
                                                           tmp_path):
    """历史是给下拉框用的。记一条打不开的路径，下次开界面它就排在最前面。"""
    win = window(monkeypatch, tmp_path)
    try:
        form = win.pages['seiyuu_db'].form
        form.remember_dirs({'db_dir': str(tmp_path / '没有这个目录'),
                            'out_dir': ''})
        assert form._settings.value(form._history_key('db_dir')) is None
        assert form._settings.value(form._history_key('out_dir')) is None
    finally:
        win.close()


def test_update_all_does_not_require_staff_in_the_gui(qt_app, monkeypatch, tmp_path):
    win = window(monkeypatch, tmp_path)
    try:
        page = win.pages['vndb_voiced']
        page.form._editors['update_all'].setChecked(True)
        page.form._editors['db_dir'].setCurrentText(str(tmp_path))
        assert page.form.missing_required_keys() == []
        assert page.validation_errors() == {}
    finally:
        win.close()


def test_hidden_page_cannot_request_a_preview(qt_app, monkeypatch, tmp_path):
    win = window(monkeypatch, tmp_path)
    try:
        hidden = win.pages['seiyuu_db']
        active = win.pages['mjo_text']
        hidden._debounce.start()
        assert hidden._debounce.isActive()
        win._active_page = hidden
        win._on_tool_selected(win.tree.topLevelItem(0).child(0), None)
        hidden.stop_debounce()
        assert not hidden._debounce.isActive()
        win._active_page = active
        calls = []
        monkeypatch.setattr(win.runner, 'request_preview',
                            lambda *args: calls.append(args))
        win._request_preview(hidden)
        assert calls == []
    finally:
        win.close()


def test_hidden_preview_signal_cannot_cancel_a_running_job(qt_app, monkeypatch,
                                                            tmp_path):
    win = window(monkeypatch, tmp_path)
    try:
        hidden = win.pages['seiyuu_db']
        active = win.pages['mjo_text']
        win._active_page = active
        monkeypatch.setattr(type(win.runner), 'busy', property(lambda _self: True))
        monkeypatch.setattr(type(win.runner), 'kind', property(lambda _self: 'run'))
        calls = []
        monkeypatch.setattr(win.runner, 'request_preview',
                            lambda *args: calls.append(args))
        hidden.previewRequested.emit()
        qt_app.processEvents()
        assert calls == []
    finally:
        win.close()


def test_cancelled_result_keeps_outputs_and_diagnostics(qt_app, monkeypatch,
                                                         tmp_path):
    win = window(monkeypatch, tmp_path)
    try:
        page = win.pages['mjo_text']
        page.set_busy(True)
        win._active_page = page
        win._run_token = 41
        monkeypatch.setattr(win.runner, 'is_current', lambda token: token == 41)
        output = tmp_path / 'partial'
        output.mkdir()
        partial = RunResult(summary='完成一半', output_paths=[str(output)],
                            warnings=['注意'], failures=[('x', '失败')],
                            table=Table(columns=('列',), rows=[('半份数据',)]))
        win._on_run_cancelled(41, partial)
        assert win.status.text() == '已取消'
        assert win.open_btn.isEnabled()
        assert page.table.rowCount() == 1
        assert page.table.item(0, 0).text() == '半份数据'
        text = win.log.toPlainText()
        assert '完成一半' in text and '注意' in text and '失败' in text
    finally:
        win.close()


def test_stale_main_window_events_are_all_ignored(qt_app, monkeypatch, tmp_path):
    win = window(monkeypatch, tmp_path)
    try:
        page = win.pages['mjo_text']
        win._active_page = page
        win._run_token = 91
        current = 92
        monkeypatch.setattr(win.runner, 'is_current', lambda token: token == current)
        win._preview_requests[91] = (page, {})
        before_log = win.log.toPlainText()
        before_status = win.status.text()
        before_progress = win.progress.value()

        win._on_log(91, '旧日志', 'warn')
        win._on_progress(91, 8, 10, '旧进度')
        win._on_preview_ready(91, PreviewResult(summary='旧预览'))
        win._on_run_finished(91, RunResult(summary='旧完成'))
        win._on_run_cancelled(91, RunResult(summary='旧取消'))
        win._on_run_failed(91, '旧错误')

        assert win.log.toPlainText() == before_log
        assert win.status.text() == before_status
        assert win.progress.value() == before_progress
        assert page.preview_box.toPlainText() != '旧预览'
        assert win._run_token == 91
    finally:
        win._run_token = 0
        win.close()


# ---------------- 路径 ----------------
def test_a_drive_root_keeps_its_separator():
    """`E:` 指的是 E 盘的当前工作目录而不是根目录，isdir 却照样为真：选了盘根做
    输出目录，文件会静默落到进程的 cwd 里。"""
    assert normalize_path('  "D:\\voice\\"  ') == 'D:\\voice'
    assert normalize_path('E:\\') == 'E:' + os.sep
    assert normalize_path('E:/') == 'E:' + os.sep
    assert normalize_path('e:') == 'e:' + os.sep
    assert normalize_path('') == ''
    assert normalize_path('E:\\voice\\\\') == 'E:\\voice'
    assert normalize_path('..\\out\\') == '..\\out'
    # UNC 共享根不受影响：剥掉尾分隔符后仍指向共享本身（它的 dirname 是自己）。
    assert normalize_path('\\\\server\\share\\') == '\\\\server\\share'
    # 只有分隔符的输入会被剥成空串，于是当作「没填」——必填校验会拦下来，
    # 不填必填字段本来也走不下去。既有行为，原样记着。
    assert normalize_path('\\\\') == ''
    assert normalize_path('/') == ''
