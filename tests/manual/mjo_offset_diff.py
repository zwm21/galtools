# -*- coding: utf-8 -*-
"""对账：字节码起点用 header_len 与用硬编码 0x28，提取结果差在哪。

mjo_text 的模块注释里那句「306 个文件、60 个 count != 1、0 处差异」就是这个脚本
量出来的。它需要真实语料，所以不能进 pytest（tests 里的合成脚本只有几十字节，
证明不了「旧起点在整批真文件上也没扫出乱码」这种统计结论）。

    python tests/manual/mjo_offset_diff.py <.mjo 目录> [更多目录...]

旧实现不复制一份代码，而是把模块源码里那一行换掉后在内存里 exec——手抄一份迟早
与本体漂移，而这个脚本的全部意义就是两者只差那一行。
"""
import collections
import os
import struct
import sys
import types

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)

from galtools.tools import mjo_text as new          # noqa: E402

SOURCE = os.path.join(ROOT, 'galtools', 'tools', 'mjo_text.py')
LINE_NEW, LINE_OLD = '    i = header_len\n', '    i = 0x28\n'


def load_old():
    """把 `i = header_len` 换回 `i = 0x28`，得到旧起点的那一版。"""
    with open(SOURCE, encoding='utf-8') as fp:
        src = fp.read()
    if LINE_NEW not in src:
        raise SystemExit('mjo_text.py 里找不到 %r，脚本该跟着改了' % LINE_NEW)
    old = types.ModuleType('galtools.tools._mjo_text_old')
    old.__dict__.update(__package__='galtools.tools',
                        __name__='galtools.tools._mjo_text_old')
    exec(compile(src.replace(LINE_NEW, LINE_OLD), SOURCE, 'exec'), old.__dict__)
    return old


def entry_count(path):
    with open(path, 'rb') as fp:
        return struct.unpack_from('<I', fp.read(0x1c), 0x18)[0]


def extract(module, path):
    try:
        return module.extract_mjo(path)
    except Exception as e:                     # 报错本身也是一种「结果」
        return ('ERROR', type(e).__name__, str(e))


def main(dirs):
    old = load_old()
    total = multi = diff = 0
    counts = collections.Counter()
    for src in dirs:
        names = new.list_mjo(src)
        print('%s：%d 个 .mjo' % (src, len(names)))
        for name in names:
            path = os.path.join(src, name)
            count = entry_count(path)
            counts[count] += 1
            total += 1
            multi += count != 1
            a, b = extract(new, path), extract(old, path)
            if a != b:
                diff += 1
                print('  差异 %s（count=%d）' % (name, count))
                print('    header_len 起点：%r' % (a[:2] if a else a))
                print('    0x28 起点      ：%r' % (b[:2] if b else b))
    print('合计 %d 个，count != 1 的 %d 个，提取结果有差异的 %d 个'
          % (total, multi, diff))
    print('count 分布：%s' % dict(sorted(counts.items())))
    if 0 in counts:
        print('注意：有 count == 0 的文件，旧起点会漏掉 8 字节真字节码')


if __name__ == '__main__':
    if len(sys.argv) < 2:
        raise SystemExit('用法：python tests/manual/mjo_offset_diff.py '
                         '<.mjo 目录> [更多目录...]')
    main(sys.argv[1:])
