# -*- coding: utf-8 -*-
"""把工具的执行搬到后台线程，结果经 Qt 信号回到界面线程。

用标准库 threading.Thread 而非 QThread：已实测普通线程里 emit 信号能正常
跨线程投递，这样就不必操心 QThread 的对象归属与生命周期。

预览与执行串行进行——新请求先取消并等待旧线程退出。工具侧只在扫描完成时
一次性写入 ctx.session，被取消的预览到不了那一步，所以两者不会争抢缓存。
"""
import threading
import time
import traceback

from PySide6.QtCore import QObject, Signal

from ..core.context import Cancelled, RunContext

# 进度信号合流间隔：逐文件 emit 上千次会把日志控件拖死。
PROGRESS_INTERVAL = 0.1


def emit(signal, *args):
    """发信号，但容忍 Bridge 已经没了。

    stop() 只等 5 秒，卡在一个网络请求里的工具可能超时；等它醒过来时窗口已关、
    Bridge 已析构，emit 会抛 RuntimeError: Signal source has been deleted，在
    后台线程里变成一段没人能处理的栈。这时候的结果本就没人要了，咽掉即可。
    """
    try:
        signal.emit(*args)
    except RuntimeError:
        pass


class Bridge(QObject):
    log = Signal(str, str)                 # 消息, 级别
    progress = Signal(int, int, str)       # 已完成, 总数, 说明
    preview_ready = Signal(int, object)    # 代次, PreviewResult
    preview_failed = Signal(int, str)      # 代次, 错误消息
    run_finished = Signal(object)          # RunResult
    run_cancelled = Signal(object)         # 取消时已完成的部分，可能为 None
    run_failed = Signal(str)               # 栈信息


class GuiContext(RunContext):
    def __init__(self, bridge, cancel_event, session):
        super().__init__(session)
        self._bridge = bridge
        self._cancel = cancel_event
        self._last_emit = 0.0

    def log(self, msg, level='info'):
        emit(self._bridge.log, msg, level)

    def progress(self, done, total, note=''):
        now = time.monotonic()
        # 最后一次必须送达，中间的按间隔丢弃。
        if done < total and now - self._last_emit < PROGRESS_INTERVAL:
            return
        self._last_emit = now
        emit(self._bridge.progress, done, total, note)

    def check_cancel(self):
        if self._cancel.is_set():
            raise Cancelled()


class JobRunner:
    def __init__(self, bridge):
        self.bridge = bridge
        self._thread = None
        self._cancel = threading.Event()
        self._preview_gen = 0

    @property
    def busy(self):
        return self._thread is not None and self._thread.is_alive()

    def cancel(self):
        self._cancel.set()

    def stop(self):
        """关窗时调用：先把在跑的活停掉，别让它在窗口析构后还往 Bridge 发信号
        （超时溜过去的那种由上面的 emit 兜住）。"""
        self._stop_current()

    def _stop_current(self, timeout=5.0):
        """取消在跑的活并等它退出。工具在循环里 check_cancel，退出很快。"""
        if self.busy:
            self._cancel.set()
            self._thread.join(timeout)
        self._cancel = threading.Event()
        self._thread = None

    def _spawn(self, target):
        self._thread = threading.Thread(target=target, daemon=True)
        self._thread.start()

    def request_preview(self, spec, params, session):
        self._stop_current()
        self._preview_gen += 1
        gen = self._preview_gen
        ctx = GuiContext(self.bridge, self._cancel, session)

        def job():
            try:
                result = spec.preview(params, ctx)
            except Cancelled:
                return
            except Exception as e:
                emit(self.bridge.preview_failed, gen,
                     '%s: %s' % (type(e).__name__, e))
                return
            emit(self.bridge.preview_ready, gen, result)

        self._spawn(job)
        return gen

    def is_current_preview(self, gen):
        return gen == self._preview_gen

    def start_run(self, spec, params, session):
        self._stop_current()
        ctx = GuiContext(self.bridge, self._cancel, session)

        def job():
            try:
                result = spec.run(params, ctx)
            except Cancelled as c:
                emit(self.bridge.run_cancelled, getattr(c, 'partial', None))
                return
            except Exception:
                emit(self.bridge.run_failed, traceback.format_exc())
                return
            emit(self.bridge.run_finished, result)

        self._spawn(job)
