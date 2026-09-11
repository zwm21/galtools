# -*- coding: utf-8 -*-
"""对账：plan_destinations 换成线性实现前后，规划出的目标名是否逐条相同。

旧实现逐个候选名做 `_path_key`（realpath + normcase）与 `os.path.exists`，且每
个基名都从 1 号重新探测，于是 k 个重名要探 k(k+1)/2 次。新实现开头列一次目标
目录，之后只比 normcase 后的文件名，并按基名记住上次落到第几号。

两者是否等价不该靠读代码断言，所以这里用随机用例逼出各种撞名形态（重名、大小写
变体、无后缀、名字本身就长得像已编号的、目标目录预存冲突），逐条比目标名，顺带
把耗时差量出来。它建真实文件、跑到几千个文件的规模，所以不进 pytest——pytest 里
那几条是形状与系统调用次数的守卫，这里证的是「整批改写忠实」。

    python tests/manual/file_collect_plan_diff.py [轮数] [--big]

下面那份 REFERENCE 是改动前的实现逐字复制。mjo_offset_diff 刻意不手抄旧代码，
因为那边新旧只差一行、手抄会漂移；这里不同：旧实现已被整个删掉，不存在会跟着变
的活体，参照物按定义就是冻结的。
"""
import os
import random
import shutil
import string
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)

from galtools.tools import file_collect as new          # noqa: E402


# ---------------- 改动前的实现（冻结的参照物，勿动） ----------------
def _ref_canonical(path):
    return os.path.normcase(os.path.realpath(os.path.abspath(path)))


def _ref_path_key(path):
    return _ref_canonical(path).casefold() if os.name == 'nt' else _ref_canonical(path)


def _ref_unique_destination(path, reserved=None):
    reserved = reserved or set()
    candidate = path
    stem, ext = os.path.splitext(path)
    index = 1
    while _ref_path_key(candidate) in reserved or os.path.exists(candidate):
        candidate = '%s_%d%s' % (stem, index, ext)
        index += 1
    return candidate


def REFERENCE(hits, dst):
    reserved = set()
    planned = []
    for source, size in hits:
        target = _ref_unique_destination(
            os.path.join(dst, os.path.basename(source)), reserved)
        reserved.add(_ref_path_key(target))
        planned.append((source, target, size))
    return planned
# ---------------- 参照物到此为止 ----------------


# 基名池刻意包含：同名、仅大小写不同、无后缀、双后缀、本身就带 _N 的。
NAME_POOL = [
    'voice.ogg', 'VOICE.OGG', 'voice_1.ogg', 'voice_2.ogg', 'Voice.Ogg',
    'bg.png', 'bg.PNG', 'readme', 'archive.tar.gz', 'se_01.wav', 'se_01_1.wav',
    '声优.ogg', 'ｖｏｉｃｅ.ogg',
]


def build_case(rng, root, big):
    """造一轮：随机建目标目录里的预存文件，随机产出一批命中。"""
    dst = os.path.join(root, 'dst')
    os.mkdir(dst)
    for name in rng.sample(NAME_POOL, rng.randint(0, 5)):
        with open(os.path.join(dst, name), 'wb') as fp:
            fp.write(b'old')
    count = rng.randint(200, 400) if big else rng.randint(1, 40)
    hits = []
    for i in range(count):
        name = rng.choice(NAME_POOL)
        # 源路径不必真实存在：规划只取 basename。
        hits.append((os.path.join(root, 'src', 'd%03d' % (i % 37), name), i))
    return hits, dst


def main():
    args = [a for a in sys.argv[1:] if not a.startswith('--')]
    big = '--big' in sys.argv
    rounds = int(args[0]) if args else 200
    rng = random.Random(20260911)
    mismatches = 0
    ref_time = new_time = 0.0
    total_hits = 0

    for round_no in range(1, rounds + 1):
        root = tempfile.mkdtemp(prefix='fcplan_')
        try:
            hits, dst = build_case(rng, root, big)
            total_hits += len(hits)

            start = time.perf_counter()
            expected = REFERENCE(hits, dst)
            ref_time += time.perf_counter() - start

            start = time.perf_counter()
            actual = new.plan_destinations(hits, dst)
            new_time += time.perf_counter() - start

            if actual != expected:
                mismatches += 1
                print('第 %d 轮不一致，目标目录预存：%s'
                      % (round_no, sorted(os.listdir(dst))))
                for (_s, want, _z), (_s2, got, _z2) in zip(expected, actual):
                    if want != got:
                        print('    期望 %-24s 实得 %s'
                              % (os.path.basename(want), os.path.basename(got)))
                if len(expected) != len(actual):
                    print('    条数不同：%d vs %d' % (len(expected), len(actual)))
        finally:
            shutil.rmtree(root, ignore_errors=True)

    print('%d 轮 / 共 %d 条命中' % (rounds, total_hits))
    print('旧实现 %8.3fs' % ref_time)
    print('新实现 %8.3fs   快 %.0f 倍' % (new_time, ref_time / max(new_time, 1e-9)))
    print('不一致 %d 轮' % mismatches)
    return 1 if mismatches else 0


if __name__ == '__main__':
    raise SystemExit(main())
