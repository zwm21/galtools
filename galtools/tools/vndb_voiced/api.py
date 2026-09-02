# -*- coding: utf-8 -*-
"""VNDB kana API 的标准库客户端。

旧版依赖 requests/bs4/lxml 是为了抓 HTML；改走官方 JSON API 后这三个依赖全部
不再需要，本工具因此仍然满足「galtools/tools 下只用标准库」。

模块里有且只有两个注入点，测试全打这两处，整套测试零联网：
    _open(req, timeout)   发请求
    _sleep(seconds)       睡觉（限流与退避）

限流按官方的 200 请求 / 5 分钟做滑动窗口。睡眠切成小片、片间 check_cancel，
否则一次长睡会把用户的取消吞掉；取消的粒度是「当前这一个请求」（实测单次
0.6–3 秒），与 GUI worker 的 5 秒 join 预算相容。
"""
import json
import time
import urllib.error
import urllib.request
from collections import deque

API_BASE = 'https://api.vndb.org/kana/'
USER_AGENT = 'galtools/1.0 (vndb_voiced)'
PAGE_SIZE = 100          # API 允许的单页上限
TIMEOUT = 60.0
CHUNK = 65536

RATE_WINDOW = 300.0      # 官方限额窗口：5 分钟
RATE_QUOTA = 190         # 官方 200，留 10 次余量给同一时段别的调用
MIN_INTERVAL = 0.25      # 未逼近限额时的最小请求间隔
SLEEP_SLICE = 0.2        # 睡眠切片长度，片间查一次取消
MAX_ATTEMPTS = 4
BACKOFF_BASE = 2.0
MAX_RETRY_AFTER = 60.0   # 服务端让等更久也不等：宁可报错让用户自己决定


class ApiError(Exception):
    """已翻译成人话的失败。调用方可以直接把 str(e) 显示给用户。"""


class _Retry(Exception):
    """内部信号：这次失败值得重试。delay 为 None 表示按指数退避。"""

    def __init__(self, message, delay=None):
        super().__init__(message)
        self.message = message
        self.delay = delay


def _open(req, timeout):
    return urllib.request.urlopen(req, timeout=timeout)


def _sleep(seconds):
    time.sleep(seconds)


def _reason(err):
    return getattr(err, 'reason', None) or err


def _retry_after(err):
    raw = err.headers.get('Retry-After') if err.headers else None
    try:
        delay = float((raw or '').strip())
    except ValueError:
        return None
    if delay < 0 or delay > MAX_RETRY_AFTER:
        return None
    return delay


def _read(resp, ctx):
    """分块读响应体，块间查一次取消。"""
    chunks = []
    while True:
        if ctx is not None:
            ctx.check_cancel()
        chunk = resp.read(CHUNK)
        if not chunk:
            break
        chunks.append(chunk)
    return b''.join(chunks)


class Client:
    """一次抓取用一个实例：限流窗口与请求计数都挂在实例上。"""

    def __init__(self, ctx=None, quota=RATE_QUOTA):
        self._ctx = ctx
        self._quota = quota
        self._stamps = deque()
        self.requests = 0

    # ---------- 取消与限流 ----------
    def _check_cancel(self):
        if self._ctx is not None:
            self._ctx.check_cancel()

    def _wait(self, seconds):
        deadline = time.monotonic() + seconds
        while True:
            self._check_cancel()
            left = deadline - time.monotonic()
            if left <= 0:
                return
            _sleep(min(SLEEP_SLICE, left))

    def _throttle(self):
        while True:
            now = time.monotonic()
            while self._stamps and now - self._stamps[0] >= RATE_WINDOW:
                self._stamps.popleft()
            if len(self._stamps) < self._quota:
                break
            # 睡到最老的那条滚出窗口为止。
            self._wait(RATE_WINDOW - (now - self._stamps[0]) + 0.05)
        if self._stamps:
            gap = MIN_INTERVAL - (time.monotonic() - self._stamps[-1])
            if gap > 0:
                self._wait(gap)

    # ---------- 请求 ----------
    def post(self, path, body):
        for attempt in range(1, MAX_ATTEMPTS + 1):
            self._check_cancel()
            self._throttle()
            self._stamps.append(time.monotonic())
            self.requests += 1
            try:
                return self._once(path, body)
            except _Retry as retry:
                if attempt == MAX_ATTEMPTS:
                    raise ApiError('%s，重试 %d 次仍失败'
                                   % (retry.message, MAX_ATTEMPTS))
                delay = retry.delay
                if delay is None:
                    delay = BACKOFF_BASE ** (attempt - 1)
                self._wait(delay)
        raise AssertionError('unreachable')

    def _once(self, path, body):
        data = json.dumps(body).encode('utf-8')
        req = urllib.request.Request(
            API_BASE + path, data=data,
            headers={'Content-Type': 'application/json',
                     'User-Agent': USER_AGENT})
        try:
            resp = _open(req, TIMEOUT)
        except urllib.error.HTTPError as e:
            raise self._translate(e)
        except urllib.error.URLError as e:
            raise _Retry('连不上 api.vndb.org（%s）' % _reason(e))
        except OSError as e:      # socket 超时、连接被重置等
            raise _Retry('与 api.vndb.org 的连接中断（%s）' % e)
        try:
            raw = _read(resp, self._ctx)
        finally:
            resp.close()
        try:
            return json.loads(raw.decode('utf-8'))
        except (UnicodeDecodeError, ValueError):
            raise ApiError('API 返回的不是合法 JSON（%d 字节）' % len(raw))

    def _translate(self, err):
        """把 HTTPError 翻成 ApiError（不重试）或 _Retry（重试）。"""
        try:
            body = err.read().decode('utf-8', 'replace').strip()[:200]
        except Exception:
            body = ''
        if err.code == 429:
            return _Retry('请求过于频繁（HTTP 429）', _retry_after(err))
        if err.code >= 500:
            return _Retry('VNDB 服务端错误（HTTP %d）' % err.code)
        if err.code == 400:
            return ApiError('API 拒绝了请求（HTTP 400）：%s' % (body or '无说明'))
        return ApiError('API 返回 HTTP %d：%s' % (err.code, body or '无说明'))

    # ---------- 组合用法 ----------
    def count(self, path, filters):
        """只要总数不要数据。实测 0.75–1.6 秒，便宜到可以放进预览。"""
        res = self.post(path, {'filters': filters, 'fields': 'id',
                               'results': 1, 'count': True})
        return int(res.get('count') or 0)

    def paged(self, path, body, on_count=None):
        """逐条产出全部结果。

        on_count 不为 None 时，第一页顺带要一次 count 并回调真实总数——这样
        进度条能是确定进度，且不额外花一次请求。
        """
        page = 1
        while True:
            payload = dict(body, page=page, results=PAGE_SIZE)
            if on_count is not None and page == 1:
                payload['count'] = True
            res = self.post(path, payload)
            if on_count is not None and page == 1:
                on_count(int(res.get('count') or 0))
            for item in res.get('results') or []:
                yield item
            if not res.get('more'):
                return
            page += 1
