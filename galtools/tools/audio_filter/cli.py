# -*- coding: utf-8 -*-
"""原命令行五步向导：python -m galtools.tools.audio_filter.cli

提示词与打印格式逐字保留，逻辑全部走 core / run，不另写一份。第 3–5 步的
确认循环也保留：报命中数后选 n 就回到第 3 步重设阈值。

setup_console 与 msvcrt 只在这条路径上出现，import 时不执行任何副作用。
"""
import os
import sys

from ...core.context import ConsoleContext
from . import core
from . import run

SUPPORTED_EXTS = core.SUPPORTED_EXTS
DEFAULT_THRESHOLD = core.DEFAULT_THRESHOLD
fmt_num = core.fmt_num


def setup_console():
    """强制 UTF-8 输出，防止日文文件名在 GBK 控制台崩溃。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding='utf-8', errors='replace')
        except Exception:
            pass


# ---------------- 交互输入 ----------------
def ask_directory():
    while True:
        raw = input('请输入语音文件所在目录（可直接拖拽文件夹到此）: ').strip()
        raw = raw.strip('"').strip("'").rstrip('\\/').strip()
        if not raw:
            print('  [!] 目录不能为空，请重新输入。')
            continue
        if os.path.isdir(raw):
            return os.path.abspath(raw)
        print(f'  [!] 目录不存在或不可访问: {raw}\n      请重新输入。')


def ask_recursive():
    while True:
        raw = input('是否包含子目录一并扫描? (y/N，直接回车=仅扫顶层): ').strip().lower()
        if raw in ('', 'n', 'no'):
            return False
        if raw in ('y', 'yes'):
            return True
        print('  [!] 请输入 y 或 n（直接回车=否）。')


def ask_threshold():
    tip = fmt_num(DEFAULT_THRESHOLD)
    while True:
        raw = input(
            f'请输入筛选时长阈值（秒，时长【大于】该值的文件将被复制，'
            f'直接回车默认 {tip} 秒）: '
        ).strip()
        if raw == '':
            return DEFAULT_THRESHOLD
        while raw and (raw[-1] in ('s', 'S') or raw.endswith('秒')):
            raw = raw[:-1].rstrip('秒').strip()
        try:
            val = float(raw)
        except ValueError:
            print('  [!] 无法识别的数字，请重新输入（例如 6 或 6.5）。')
            continue
        if val <= 0:
            print('  [!] 阈值必须为正数，请重新输入。')
            continue
        return val


def ask_max_limit(threshold):
    """限制筛选的文件时长最大值。直接回车=不限制（按原逻辑只看阈值下限）。

    这里的规则与 ToolSpec.validate 等价，只是错误提示写法为终端量身定做。
    """
    while True:
        raw = input(
            '请输入筛选文件时长的最大值（秒，时长【不超过】该值的文件才会被复制，'
            '直接回车默认不限制）: '
        ).strip()
        if raw == '':
            return None
        while raw and (raw[-1] in ('s', 'S') or raw.endswith('秒')):
            raw = raw[:-1].rstrip('秒').strip()
        try:
            val = float(raw)
        except ValueError:
            print('  [!] 无法识别的数字，请重新输入（例如 15 或 15.5）。')
            continue
        if val <= 0:
            print('  [!] 最大值必须为正数，请重新输入。')
            continue
        if val <= threshold:
            print(f'  [!] 最大值（{fmt_num(val)} 秒）必须大于筛选阈值'
                  f'（{fmt_num(threshold)} 秒），请重新输入。')
            continue
        return val


def ask_proceed():
    """是否进行下一步操作（复制）。直接回车=是，输入 n 返回 False 让用户重设阈值。"""
    while True:
        raw = input('是否进行下一步操作（复制）? (Y/n，直接回车=是): ').strip().lower()
        if raw in ('', 'y', 'yes'):
            return True
        if raw in ('n', 'no'):
            return False
        print('  [!] 请输入 y 或 n（直接回车=是）。')


def pause_any_key():
    """程序结束前等待按键，避免双击运行时窗口一闪而过。非交互环境自动跳过。"""
    print('\n按任意键退出...')
    if not sys.stdin.isatty():
        return  # 管道/重定向等非交互场景，跳过等待
    try:
        import msvcrt  # Windows：无需回车、按任意键即退出
        msvcrt.getch()
    except ImportError:
        try:
            input()  # 非 Windows 回退：按回车退出
        except EOFError:
            pass


def report_stats(audio, failed, total_audio):
    print('\n========== 目录扫描结果 ==========')
    print(f'音频文件总数   : {total_audio}')
    print(f'成功解析       : {len(audio)}')
    print(f'解析失败       : {len(failed)}')
    if failed:
        for p in failed[:5]:
            print(f'    - {os.path.basename(p)}')
        if len(failed) > 5:
            print(f'    ... 等共 {len(failed)} 个')
    if not audio:
        return
    stats = core.duration_stats(audio)
    print(f"最长时长       : {stats['longest'][1]:.2f}s  "
          f"({os.path.basename(stats['longest'][0])})")
    print(f"最短时长       : {stats['shortest'][1]:.2f}s  "
          f"({os.path.basename(stats['shortest'][0])})")
    print(f"平均时长       : {stats['mean']:.2f}s")
    print(f"中位数时长     : {stats['median']:.2f}s")
    print('==================================\n')


def main():
    setup_console()
    print('=' * 46)
    print('      语音文件时长筛选工具（时长 > 阈值）')
    print(f"      支持格式: {', '.join(sorted(SUPPORTED_EXTS))}")
    print('=' * 46)

    src = ask_directory()
    recursive = ask_recursive()
    ctx = ConsoleContext()
    params = {'src': src, 'recursive': recursive}

    print('\n正在扫描并解析音频时长，请稍候...')
    # 不报扫描进度：原版这里只有一句「请稍候」，多出 \r 进度行会改掉 stdout。
    audio, failed, total_audio = core.ensure_scan(
        params, ctx, report_progress=False)
    report_stats(audio, failed, total_audio)

    if not audio:
        print('没有可解析的音频文件，程序结束。')
        return

    # 筛选确认循环：先报命中个数，用户确认后才继续；选否则重新指定阈值
    while True:
        threshold = ask_threshold()
        max_limit = ask_max_limit(threshold)
        hits = core.select_hits(audio, threshold, max_limit)
        cond_desc = core.describe_condition(threshold, max_limit)
        print(f'\n筛选条件: {cond_desc}')
        print(f'筛选到符合条件的文件个数: {len(hits)} / {len(audio)}'
              f' ({len(hits) / len(audio) * 100:.1f}%)')
        if ask_proceed():
            break
        print('好的，请重新指定筛选时长。\n')

    # 共用同一个 ctx，所以 run 里的 ensure_scan 命中缓存，不会重扫。
    params.update(threshold=threshold, max_limit=max_limit)
    print(run(params, ctx).summary)


if __name__ == '__main__':
    try:
        main()
        pause_any_key()
    except KeyboardInterrupt:
        print('\n\n[!] 用户中断（Ctrl+C），已退出。已复制的文件保留在输出目录中。')
        pause_any_key()
        sys.exit(130)
    except EOFError:
        print('\n[!] 输入流结束，程序退出。')
        sys.exit(1)
