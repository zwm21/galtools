# -*- coding: utf-8 -*-
"""命令行版：python -m galtools.tools.vndb_voiced.cli [目标...] [-o 输出]

不带参数时进入交互模式，两句提示词逐字沿用旧脚本。目标可以给多个（逗号分隔
或多个参数），行为与 GUI 一致：两人会多出一页共同出演，三人以上按两两及以上的
组合各出一页。

不带 --db 时只导出 xlsx，与旧脚本一模一样；--db 目录 追加存库，再配 --no-export
就只存库不写表格。GUI 的默认值正相反（默认存库、默认不导出），但两边的规则都由
ToolSpec.validate 一份说了算。--update-all 是另一条路：不给目标，把 --db 库里
已有的每个人重抓一遍，与 GUI 的「更新全库」开关同一份逻辑。

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
    那一侧，命令行喂 20 个 id 会一路跑到抓完再去枚举一百万个组合。这里只看 staff
    那几条，别的键交给 check_options。
    """
    targets = fetch.parse_targets(raw)
    if not targets:
        return [], '没解析出任何目标。'
    for key, message in validate({'staff': raw, 'export': True}):
        if key == 'staff':
            return [], message
    return targets, ''


def check_options(params):
    """把整份参数喂给 validate，返回第一条错误消息（没有就是空串）。

    跳过 staff（check_targets 的活，交互模式下先问的就是它）与 out_dir：GUI 要求
    导出目录已经存在，而命令行的 -o 一向是现建的。库目录不同，它必须已经存在——
    打错一个字就悄悄开一个新库，等到发现时数据已经分散在两处了。
    """
    for key, message in validate(params):
        if key not in ('staff', 'out_dir'):
            return message
    return ''


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
    # GUI 那边默认存库、不导出，命令行反过来：不带 --db 的调用与旧脚本一模一样，
    # 已经写进批处理的那些不会因为多了个库而改行为。规则仍然只有 validate 一份。
    ap.add_argument('--db', metavar='目录',
                    help='同时把抓到的人存进这个本地声优库（目录要已存在）')
    ap.add_argument('--no-export', action='store_true',
                    help='只存库，不写 xlsx；要配 --db 用')
    ap.add_argument('--refresh', action='store_true',
                    help='忽略缓存重新抓取（命令行每次都是新进程，一般用不到）')
    ap.add_argument('--update-all', action='store_true',
                    help='忽略目标，把 --db 库里已有的每个人都在 vndb 上重抓一遍，'
                         '有变化才覆盖（要配 --db 用，且不写 xlsx）')
    args = ap.parse_args()

    if args.update_all:
        if args.targets:
            ap.error('--update-all 与目标二选一，别一起给')
        if not args.db:
            ap.error('--update-all 要配 --db 用')
        raw = ''
    elif args.targets:
        raw = ','.join(args.targets)
        targets, error = check_targets(raw)
        if not targets:
            ap.error(error)          # 打印用法并以 2 退出
    else:
        print('=' * 46)
        print('      vndb 声优出演表导出工具')
        print('=' * 46)
        raw = ask_targets()

    params = {'staff': raw, 'out_dir': args.output,
              'export': False if args.update_all else not args.no_export,
              'save_db': bool(args.db), 'db_dir': args.db or '',
              'refresh': args.refresh, 'update_all': args.update_all}
    error = check_options(params)
    if error:
        ap.error(error)
    ctx = ConsoleContext()
    result = run(params, ctx)
    print(result.summary)
    # 一个文件都没写出来时以非零退出。ApiError 那条路径本来就这样，而「目标全都
    # 定位不到人」「输出目录不可用」同样是什么都没产出，exit 0 会让调用它的脚本
    # 把这些当成成功。
    return 0 if result.output_paths else 1


if __name__ == '__main__':
    # 双击运行（无参数）时才在结束前暂停，带参数的调用要能进脚本管道。
    interactive = len(sys.argv) == 1
    try:
        code = main()
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
    sys.exit(code)
