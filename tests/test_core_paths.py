# -*- coding: utf-8 -*-
"""core.paths 的规则测试。

这一条规则被 GUI 表单、mjo_text 的路径推导、audio_filter 的向导、vndb 的输出
目录四处共用，所以在这里单独钉一次；四处各自的用法另有测试。
"""
import os

from galtools.core.paths import keep_drive_root


def test_a_bare_drive_letter_gets_its_separator_back():
    assert keep_drive_root('E:') == 'E:' + os.sep
    assert keep_drive_root('e:') == 'e:' + os.sep


def test_everything_else_is_returned_untouched():
    # UNC 共享根不用补：它的 dirname 返回自身，本来就与盘根一致。
    # 'EE:'/':'/'1:' 不是盘符写法，别顺手给它们加分隔符。
    for path in ['E:' + os.sep, r'X:\a\b', 'script_text', '', r'\\nas\gal',
                 'EE:', ':', '1:', 'E:x', 'E:.']:
        assert keep_drive_root(path) == path
