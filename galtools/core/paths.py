# -*- coding: utf-8 -*-
"""路径清洗。只用标准库，GUI 与三个命令行入口共用同一份规则。

规则本来分散在四处各写一遍（GUI 的表单、mjo_text 的路径推导、audio_filter 的
向导、vndb 的输出目录），于是给其中一处补的例外另外三处并不知道。
"""
import os
import re

# 只剩一个盘符的写法。
DRIVE_ONLY = re.compile(r'^[A-Za-z]:$')


def keep_drive_root(path):
    """`E:` 补成 `E:\\`。

    `E:` 指的是 E 盘的**当前工作目录**而不是根目录，而 `os.path.isdir('E:')` 对
    两者都为真，所以没有哪一道校验拦得住它。落点还随进程 cwd 漂移：实测 cwd 在
    C: 上时 `abspath('E:')` 得到 `E:\\`，cwd 在 `E:\\AGENT\\galgame` 时得到的就是
    那个目录，`os.listdir('E:')` 列的也是它。

    UNC 共享根（`\\\\nas\\gal`）不需要处理：它的 dirname 返回自身，与盘根一致。
    """
    return path + os.sep if DRIVE_ONLY.match(path) else path
