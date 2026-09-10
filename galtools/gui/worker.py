# -*- coding: utf-8 -*-
"""在后台串行执行工具，并用任务 token 隔离过期的 Qt 队列事件。

同一时刻只允许一个线程访问工具 session 或写盘。新请求只会取消当前任务并成为
唯一 pending；当前线程真正退出后才启动它。UI 线程从不 join，网络阻塞时窗口仍可
响应。所有信号都携带 token，接收端必须再次核对，不能只依赖 emit 前过滤。
"""
import threading
import time
import traceback
from dataclasses import dataclass

from PySide6.QtCore import QObject, Signal

from ..core.context import Cancelled, RunContext

PROGRESS_INTERVAL = 0.1


def emit(signal, *args):
    """发信号，但容忍窗口关闭后 Bridge 已析构。"""
    try:
        signal.emit(*args)
    except RuntimeError:
        pass


class Bridge(QObject):
    log = Signal(int, str, str)               # token, 消息, 级别
    progress = Signal(int, int, int, str)     # token, 已完成, 总数, 说明
    preview_ready = Signal(int, object)       # token, PreviewResult
    preview_failed = Signal(int, str)         # token, 错误消息
    run_finished = Signal(int, object)        # token, RunResult
    run_cancelled = Signal(int, object)       # token, partial
    run_failed = Signal(int, str)             # token, 栈信息
    job_exited = Signal(int)                  # token；工作线程已 join
    idle = Signal()                           # active/pending 都已清空


class GuiContext(RunContext):
    def __init__(self, bridge, token, cancel_event, session):
        super().__init__(session)
        self._bridge = bridge
        self._token = token
        self._cancel = cancel_event
        self._last_emit = 0.0

    def log(self, msg, level='info'):
        emit(self._bridge.log, self._token, msg, level)

    def progress(self, done, total, note=''):
        now = time.monotonic()
        if done < total and now - self._last_emit < PROGRESS_INTERVAL:
            return
        self._last_emit = now
        emit(self._bridge.progress, self._token, done, total, note)

    def check_cancel(self):
        if self._cancel.is_set():
            raise Cancelled()


@dataclass
class _Request:
    token: int
    kind: str
    spec: object
    params: dict
    session: dict


@dataclass
class _Job:
    request: _Request
    cancel: threading.Event
    thread: object = None

    @property
    def token(self):
        return self.request.token

    @property
    def kind(self):
        return self.request.kind


class JobRunner:
    def __init__(self, bridge):
        self.bridge = bridge
        self._active = None
        self._pending = None
        self._next_token = 0
        self._current_token = 0
        self._state = 'idle'
        self._closing = False
        bridge.job_exited.connect(self._on_job_exited)

    @property
    def busy(self):
        return self._active is not None or self._pending is not None

    @property
    def state(self):
        # 生产代码只看 busy/kind；这个更细的生命周期字段
        # （idle/preview/run/cancelling/closing）是留给 test_gui 的可观测面，
        # 用来断言取消与关闭途中的中间态，busy 和 kind 表达不了。
        return self._state

    @property
    def kind(self):
        if self._pending is not None:
            return self._pending.kind
        return self._active.kind if self._active is not None else ''

    def is_current(self, token):
        return token != 0 and token == self._current_token

    def cancel(self):
        pending = self._pending
        if pending is not None and pending.kind == 'run':
            self._pending = None
            emit(self.bridge.run_cancelled, pending.token, None)
        if self._active is not None:
            self._active.cancel.set()
            self._state = 'cancelling'
        elif self._pending is None:
            self._current_token = 0
            self._state = 'idle'

    def stop(self):
        self._closing = True
        self._state = 'closing'
        self._pending = None
        self._current_token = 0
        if self._active is not None:
            self._active.cancel.set()
        else:
            emit(self.bridge.idle)

    def request_preview(self, spec, params, session):
        return self._submit('preview', spec, params, session)

    def start_run(self, spec, params, session):
        return self._submit('run', spec, params, session)

    def _new_request(self, kind, spec, params, session):
        self._next_token += 1
        return _Request(self._next_token, kind, spec, params, session)

    def _submit(self, kind, spec, params, session):
        if self._closing:
            return 0
        if kind == 'preview' and (
                (self._active is not None and self._active.kind == 'run')
                or (self._pending is not None and self._pending.kind == 'run')):
            return 0
        request = self._new_request(kind, spec, params, session)
        if self._active is not None:
            self._pending = request
            self._current_token = request.token
            self._active.cancel.set()
            self._state = 'cancelling'
            return request.token
        self._current_token = request.token
        self._start(request)
        return request.token

    def _start(self, request):
        job = _Job(request, threading.Event())
        self._active = job
        self._state = request.kind
        ctx = GuiContext(self.bridge, request.token, job.cancel, request.session)

        def work():
            if request.kind == 'preview':
                self._run_preview(request, ctx)
            else:
                self._run_tool(request, ctx)

        def reap():
            job.thread.join()
            emit(self.bridge.job_exited, request.token)

        job.thread = threading.Thread(target=work)
        job.thread.start()
        threading.Thread(target=reap).start()

    def _run_preview(self, request, ctx):
        try:
            result = request.spec.preview(request.params, ctx)
        except Cancelled:
            return
        except Exception as e:
            emit(self.bridge.preview_failed, request.token,
                 '%s: %s' % (type(e).__name__, e))
            return
        emit(self.bridge.preview_ready, request.token, result)

    def _run_tool(self, request, ctx):
        try:
            result = request.spec.run(request.params, ctx)
        except Cancelled as stop:
            emit(self.bridge.run_cancelled, request.token,
                 getattr(stop, 'partial', None))
            return
        except Exception:
            emit(self.bridge.run_failed, request.token, traceback.format_exc())
            return
        emit(self.bridge.run_finished, request.token, result)

    def _on_job_exited(self, token):
        if self._active is None or self._active.token != token:
            return
        self._active = None
        if self._closing:
            self._state = 'closing'
            emit(self.bridge.idle)
            return
        if self._pending is None:
            self._current_token = 0
            self._state = 'idle'
            emit(self.bridge.idle)
            return
        request, self._pending = self._pending, None
        self._start(request)
