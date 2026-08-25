# -*- coding: utf-8 -*-
"""工具与调用方之间的唯一通道。

同一个 run()/preview() 既被 GUI 调用也被命令行调用，差别只在传入哪种
RunContext：GUI 传发信号的实现，终端传 ConsoleContext。
"""
import sys


class Cancelled(BaseException):
    """用户取消。

    继承 BaseException 而非 Exception 是刻意的：工具里的批量循环普遍写
    `except Exception` 来收集单项失败，若取消信号是 Exception 就会被吞成
    一条「解析失败」然后继续跑完整个目录。
    """


class RunContext:
    """默认实现什么都不做，适合测试。子类改写三个方法。

    session 是调用方持有的字典，用来跨多次 preview/run 缓存昂贵的中间
    结果（如目录扫描）。放在这里而不是工具的模块级全局，是为了让缓存的
    归属和生命周期明确，也免得在纯逻辑模块里加锁。
    """

    def __init__(self, session=None):
        self.session = {} if session is None else session

    def log(self, msg, level='info'):
        pass

    def progress(self, done, total, note=''):
        pass

    def check_cancel(self):
        pass


class ConsoleContext(RunContext):
    """终端实现：progress 用 \\r 覆写同一行，log 直接 print。"""

    def __init__(self, session=None, stream=None):
        super().__init__(session)
        self._stream = stream or sys.stdout
        self._cr_pending = False

    def log(self, msg, level='info'):
        # 有未收尾的 \r 进度行时，先空一行把它隔开再输出正文。
        if self._cr_pending:
            self._stream.write('\n\n')
            self._cr_pending = False
        prefix = {'warn': '  [!] ', 'error': '  [x] '}.get(level, '')
        self._stream.write(prefix + msg + '\n')
        self._stream.flush()

    def progress(self, done, total, note=''):
        if not note:
            note = '%d/%d' % (done, total)
        self._stream.write('\r' + note)
        self._stream.flush()
        self._cr_pending = True

    def check_cancel(self):
        # 终端里 Ctrl+C 自然抛 KeyboardInterrupt，无需轮询标志。
        pass
