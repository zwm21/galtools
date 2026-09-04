# -*- coding: utf-8 -*-
"""命令行版：python -m galtools.tools.vndb_voiced.cli [目标...] [-o 输出]

不带参数时进入交互模式，两句提示词逐字沿用旧脚本。目标可以给多个（逗号分隔
或多个参数），行为与 GUI 一致：两人会多出一页共同出演，三人以上按两两及以上的
组合各出一页。

setup_console 与 msvcrt 只出现在这条路径上，import 时不执行任何副作用。
"""
import argparse
import sys

from ...core.context import ConsoleContext
from . import api, fetch, run, validate

BASE = 'https://vndb.org/'
DEFAULT_TARGET = 's367'


def setup_console():
    """强制 UTF-8 输出，防止日文人名在 GBK 控制台崩溃。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding='utf-8', errors='replace')
        except Exception:
            pass


def check_targets(raw):
    """返回 (目标列表, 错误消息)。

    规则不自己写一份，直接调 ToolSpec.validate：人数上限这类规则以前只装在 GUI
    那一侧，命令行喂 20 个 id 会一路跑到抓完再去枚举一百万个组合。out_dir 不传，
    validate 里那条目录检查就自动跳过——命令行的输出目录由 save 现建。
    """
    targets = fetch.parse_targets(raw)
    if not targets:
        return [], '没解析出任何目标。'
    for key, message in validate({'staff': raw}):
        if key == 'staff':
            return [], message
    return targets, ''


def ask_targets():
    """交互式询问目标：默认 s367 [y/N]，否则自行输入，可用逗号给多个。"""
    ans = input('是否运行默认抓取 %s%s ? [y/N] '
                % (BASE, DEFAULT_TARGET)).strip().lower()
    if ans == 'y':
        return DEFAULT_TARGET
    while True:
        raw = input('请输入需要抓取的网页后缀（如 s124）或完整 URL: ').strip()
        targets, error = check_targets(raw)
        if targets:
            return raw
        print('  [!] %s' % error)


def pause_any_key():
    """结束前等按键，避免双击运行时窗口一闪而过。非交互环境自动跳过。"""
    print('\n按任意键退出...')
    if not sys.stdin.isatty():
        return
    try:
        import msvcrt  # Windows：按任意键即退出
        msvcrt.getch()
    except ImportError:
        try:
            input()  # 非 Windows 回退：按回车退出
        except EOFError:
            pass


def main():
    setup_console()
    ap = argparse.ArgumentParser(
        description='抓 VNDB 上某个声优配过的全部角色，导出 Excel')
    ap.add_argument('targets', nargs='*',
                    help='staff id（s367）、声优页网址或名字；缺省时进入交互模式')
    ap.add_argument('-o', '--output', default='.',
                    help='输出目录或 .xlsx 路径，默认当前目录')
    ap.add_argument('--refresh', action='store_true',
                    help='忽略缓存重新抓取（命令行每次都是新进程，一般用不到）')
    args = ap.parse_args()

    if args.targets:
        raw = ','.join(args.targets)
        targets, error = check_targets(raw)
        if not targets:
            ap.error(error)          # 打印用法并以 2 退出
    else:
        print('=' * 46)
        print('      vndb 声优出演表导出工具')
        print('=' * 46)
        raw = ask_targets()

    ctx = ConsoleContext()
    print(run({'staff': raw, 'out_dir': args.output,
               'refresh': args.refresh}, ctx).summary)


if __name__ == '__main__':
    # 双击运行（无参数）时才在结束前暂停，带参数的调用要能进脚本管道。
    interactive = len(sys.argv) == 1
    try:
        main()
    except KeyboardInterrupt:
        print('\n\n[!] 用户中断（Ctrl+C），已退出。没有写出文件。')
        if interactive:
            pause_any_key()
        sys.exit(130)
    except api.ApiError as e:
        print('\n[!] vndb 接口出错：%s' % e, file=sys.stderr)
        if interactive:
            pause_any_key()
        sys.exit(1)
    except EOFError:
        print('\n[!] 输入流结束，程序退出。')
        sys.exit(1)
    if interactive:
        pause_any_key()
