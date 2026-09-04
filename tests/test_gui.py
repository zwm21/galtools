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

from PySide6.QtCore import QSettings, Qt                       # noqa: E402
from PySide6.QtWidgets import QApplication                     # noqa: E402

from galtools.core.context import Cancelled                    # noqa: E402
from galtools.core.spec import TEXT, Field, RunResult, ToolSpec  # noqa: E402
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


def spec_with(run):
    return ToolSpec(id='fake', name='假工具', category='测试',
                    description='只为驱动 JobRunner',
                    fields=(Field(key='x', kind=TEXT, label='x'),), run=run)


def test_an_abandoned_job_stops_being_heard(qt_app):
    """新活开跑前先取消旧活，但只 join 一秒——真卡在 socket 上的旧线程能活过这次
    等待。它后来发的日志、进度、结果必须一律丢弃，否则上一轮的东西会插进下一轮。"""
    bridge = Bridge()
    logs, steps = [], []
    bridge.log.connect(lambda msg, level: logs.append(msg), Qt.DirectConnection)
    bridge.progress.connect(lambda *a: steps.append(a), Qt.DirectConnection)
    runner = JobRunner(bridge)

    job = runner._job
    ctx = runner._context({}, job)
    ctx.log('还在的时候说的话')
    ctx.progress(1, 2, '还在的时候的进度')
    assert logs == ['还在的时候说的话'] and len(steps) == 1

    runner._stop_current()                  # 换代：这份活从此说什么都不算
    ctx.log('过期之后说的话')
    ctx.progress(2, 2, '过期之后的进度')
    runner._emit(job, bridge.log, '过期之后发的信号', 'info')
    assert logs == ['还在的时候说的话'] and len(steps) == 1


def test_cancelling_a_run_reports_the_partial_result(qt_app):
    """取消要能把「已经做了多少」带回来：Cancelled 上挂的 partial 是工具唯一的
    汇报渠道，run_cancelled 丢了它，用户就只看到一句「已取消」。"""
    bridge = Bridge()
    got = []
    bridge.run_cancelled.connect(got.append, Qt.DirectConnection)
    runner = JobRunner(bridge)
    running = threading.Event()

    def slow(params, ctx):
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
    runner._thread.join(5)
    assert not runner.busy
    assert [r.summary for r in got] == ['已经做了一半']


def test_a_failed_preview_puts_the_progress_bar_back(qt_app, monkeypatch,
                                                     tmp_path):
    """预览一开头的 progress(0, 0, …) 把进度条切成了无限滚动。失败时不收回来的话
    它会一直滚、状态栏也一直停在「正在查…」，看着像还在跑。"""
    win = window(monkeypatch, tmp_path)
    try:
        page = win.pages['vndb_voiced']
        win._preview_page = page
        win.progress.setMaximum(0)          # 预览把它切成了无限滚动
        win.status.setText('正在查 vndb…')
        win._on_preview_failed(win.runner._preview_gen, 'boom')
        assert win.progress.maximum() == 100
        assert win.status.text() == '预览失败'
        assert page.preview_ok is False
        assert not page.start_btn.isEnabled()
    finally:
        win.close()


def test_a_drive_root_keeps_its_separator():
    """`E:` 指的是 E 盘的当前工作目录而不是根目录，isdir 却照样为真：选了盘根做
    输出目录，文件会静默落到进程的 cwd 里。"""
    assert normalize_path('  "D:\\voice\\"  ') == 'D:\\voice'
    assert normalize_path('E:\\') == 'E:' + os.sep
    assert normalize_path('E:/') == 'E:' + os.sep
    assert normalize_path('e:') == 'e:' + os.sep
    assert normalize_path('') == ''
