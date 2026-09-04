# -*- coding: utf-8 -*-
"""Majiro 引擎 .mjo 脚本文本批量提取工具
依据 MajiroStringEditor (marcussacana) 的 OBJ.cs 解析逻辑移植

mjo 结构（MajiroObjV1.000，未加密）：
  0x00  char[16]  "MajiroObjV1.000\\0"（若为 X1.000 则字节码被 XOR 加密，本工具暂不支持）
  0x18  uint32    入口函数表项数 count
  0x1C  count × {uint32 hash, uint32 offset}
  +4    uint32    字节码长度 ScriptLen（相对 HeaderLen）
  0x28  字节码开始

字节码 opcode（u16 小端）：
  0x840 ShowText : u16 len + len 字节 SJIS 文本（含结尾 \\0）
  0x842 AdvEvent : 后随 u16(0x002) + 子码：0x06E 换行 / 0x070 等待点击 / 0x077 对话框清空
  0x83A StringId : u16 ID
  0x841 ParseStr
  其他 opcode 按单字节步进扫描（与原工具一致）

输出：每个 mjo 一个 .txt（UTF-8），外加一个合并全文本。

已知怪癖，均为既有行为，刻意保留：
  * 主循环 `i + 2 < script_len` 会漏掉最后一个 opcode。
  * 合并全文写在输出目录的**父**目录；输出目录为相对路径时父目录为空串，
    退化成当前工作目录。

已修掉的一处：字节码起点原先硬编码 0x28，只在 count == 1 时才等于真正的
header_len。实测 144 个文件中 22 个 count != 1（全是 buttonmenu/cgmode/config
之类 UI 脚本），它们的入口函数表会被当作字节码扫描，扫出乱码或直接报解析失败。
现在按 header_len 起步：count == 1 的剧情脚本产出逐字节不变，那 22 个 UI 脚本
与旧脚本的产出会不同——那本来就是旧脚本的错。
"""
import os
import struct
import sys

from ..core.context import RunContext
from ..core.spec import DIR, Field, PreviewResult, RunResult, ToolSpec

ShowText = 0x840
AdvEvent = 0x842
StringId = 0x83A
ParseStr = 0x841
AdvEvtType = 0x002
AdvBrkLine = 0x06E
AdvClkWait = 0x070
AdvDialCls = 0x077

MERGED_NAME = 'scenario_text_全文.txt'
# extract_mjo 实际可能抛出的异常。原先是 except Exception，会把 Cancelled
# 之外的一切都吞成「单文件解析失败」——收窄以免取消信号被误当解析错误。
PARSE_ERRORS = (ValueError, OSError, struct.error)


def is_valid_str(script, index):
    """校验 index 处是否存在合法的 u16 前缀长度字符串"""
    if index + 2 >= len(script):
        return False
    ln = struct.unpack_from('<H', script, index)[0]
    if ln < 2 or index + ln + 2 >= len(script):
        return False
    for i in range(ln - 1):
        if script[index + 2 + i] == 0:
            return False
    if script[index + ln + 1] != 0:
        return False
    return True


def extract_mjo(path):
    with open(path, 'rb') as f:
        script = f.read()
    header = script[:15]
    if header == b'MajiroObjX1.000':
        raise ValueError('加密脚本(X1)，本工具暂不支持（需要 XOR 密钥）')
    if header != b'MajiroObjV1.000':
        raise ValueError(f'未知脚本头: {header!r}')

    count = struct.unpack_from('<I', script, 0x18)[0]
    header_len = 0x18 + count * 8 + 4 + 4
    script_len = struct.unpack_from('<I', script, header_len - 4)[0] + header_len
    script_len = min(script_len, len(script))

    strings = []          # 每个元素 = 一条对话/一个名字
    cur = ''
    has_text = False

    def finish():
        nonlocal cur, has_text
        if has_text and cur:
            strings.append(cur)
        cur = ''
        has_text = False

    i = header_len
    while i + 2 < script_len:
        cmd = struct.unpack_from('<H', script, i)[0]
        if cmd == ShowText:
            i += 2
            if not is_valid_str(script, i):
                continue
            ln = struct.unpack_from('<H', script, i)[0]
            try:
                s = script[i + 2:i + 2 + ln - 1].decode('cp932')
            except UnicodeDecodeError:
                s = script[i + 2:i + 2 + ln - 1].decode('cp932', errors='replace')
            # 名字判定：以「开头 → 上一条(名字)结束，新对话开始
            if s.startswith('「') and has_text and cur:
                finish()
            if not s.endswith('」'):
                s = s.lstrip('「')
            cur += s
            has_text = True
            i += ln + 2
        elif cmd == AdvEvent:
            # 末尾几个字节上恰好是 0x842 时，下面两次 unpack_from 会越过缓冲区，
            # 把整个文件废成「解析失败」——已经提取出的几千条文本一起丢掉。主循环
            # 本来就刻意漏掉最后一个 opcode，这里同样当这截残字节不存在。
            if i + 6 > script_len:
                break
            i += 2
            if struct.unpack_from('<H', script, i)[0] == AdvEvtType:
                i += 2
                sub = struct.unpack_from('<H', script, i)[0]
                i += 2
                if sub == AdvBrkLine:
                    cur += '\n'
                elif sub == AdvClkWait:
                    cur += '[wait]'
                elif sub == AdvDialCls:
                    if cur.endswith('[wait]'):
                        cur = cur[:-6]
                    finish()
            else:
                pass
        elif cmd == StringId or cmd == ParseStr:
            i += 2
        else:
            i += 1
    finish()

    # 名字与台词配对：若本条不以「结尾/开头且下一条以「开头，则本条为说话人名
    # 原脚本逻辑里名字独立成条；这里合并为「名前」+ 换行 + 台词
    lines = []
    j = 0
    while j < len(strings):
        s = strings[j]
        nxt = strings[j + 1] if j + 1 < len(strings) else None
        if (nxt is not None and nxt.startswith('「')
                and not s.startswith('「') and '\n' not in s and len(s) <= 20):
            lines.append(f'{s}：')
            j += 1
            continue
        # 阅读友好化：去掉控制标记
        s = s.replace('[wait]', '').replace('[clear]', '')
        s = s.strip('\n')
        if s:
            lines.append(s)
        j += 1
    return lines


def resolve_paths(params):
    """(源目录, 输出目录, 合并全文路径)。

    合并全文的落点沿用原脚本写法，包含输出目录为相对路径时退化到当前工作
    目录这一行为——它是既有产出的一部分，不能顺手改成 pathlib。
    """
    src_dir = params.get('src_dir') or '.'
    out_dir = params.get('out_dir') or os.path.join(src_dir, 'script_text')
    merged_path = os.path.join(
        os.path.dirname(out_dir.rstrip('/\\')) or '.', MERGED_NAME)
    return src_dir, out_dir, merged_path


def list_mjo(src_dir):
    return sorted(f for f in os.listdir(src_dir) if f.lower().endswith('.mjo'))


def preview(params, ctx):
    src_dir, out_dir, merged_path = resolve_paths(params)
    if not os.path.isdir(src_dir):
        return PreviewResult(summary='目录不存在：%s' % src_dir, ok=False)
    try:
        files = list_mjo(src_dir)
    except OSError as e:
        return PreviewResult(summary='无法读取目录：%s' % e, ok=False)

    summary = '\n'.join([
        '找到 %d 个 .mjo 文件' % len(files),
        '单文件输出目录: %s' % out_dir,
        '合并全文: %s' % merged_path,
    ])
    warnings = []
    if os.path.exists(merged_path):
        warnings.append('合并全文已存在，将被覆盖：%s' % merged_path)
    return PreviewResult(summary=summary, warnings=warnings, ok=bool(files))


def run(params, ctx):
    src_dir, out_dir, merged_path = resolve_paths(params)
    files = list_mjo(src_dir)
    if not files:
        # 一个文件都没有时绝不碰合并全文：它很可能是上一次的成果，而下面那个
        # 'w' 会把它清成空文件。GUI 靠 preview 的 ok=False 挡住这种情况，命令行
        # 直接调 run，没有那道闸。
        return RunResult(summary='%s 里没有 .mjo 文件，什么都没做。' % src_dir)
    os.makedirs(out_dir, exist_ok=True)

    total_lines = 0
    merged = []
    failed = []
    for idx, name in enumerate(files):
        ctx.check_cancel()
        ctx.progress(idx, len(files), '提取 %s' % name)
        path = os.path.join(src_dir, name)
        try:
            lines = extract_mjo(path)
        except PARSE_ERRORS as e:
            failed.append((name, str(e)))
            continue
        total_lines += len(lines)
        stem = os.path.splitext(name)[0]
        with open(os.path.join(out_dir, stem + '.txt'), 'w', encoding='utf-8') as o:
            o.write('\n'.join(lines))
        merged.append(f'{"=" * 60}\n【{stem}】\n{"=" * 60}\n' + '\n'.join(lines) + '\n')
    ctx.progress(len(files), len(files), '写出合并全文')

    with open(merged_path, 'w', encoding='utf-8') as o:
        o.write('\n'.join(merged))

    parts = ['处理 %d 个 mjo，成功 %d 个，共提取 %d 条文本'
             % (len(files), len(files) - len(failed), total_lines)]
    if failed:
        parts.append('失败列表:')
        parts.extend('  %s: %s' % (n, e) for n, e in failed)
    parts.append('单文件输出目录: %s' % out_dir)
    parts.append('合并全文: %s' % merged_path)
    return RunResult(
        summary='\n'.join(parts),
        output_paths=[out_dir, merged_path],
        failures=failed,
    )


TOOL = ToolSpec(
    id='mjo_text',
    name='Majiro 脚本文本提取',
    category='文本',
    description='把 arc_conv 解包出的 .mjo 批量提取为 txt，并另存一份合并全文。',
    fields=(
        Field(key='src_dir', kind=DIR, label='.mjo 目录',
              help='arc_conv 解包出的目录，通常名为 scenario.arc~。不递归子目录。'),
        Field(key='out_dir', kind=DIR, label='输出目录', required=False,
              placeholder='留空 = <.mjo 目录>/script_text',
              help='合并全文写在该目录的父目录下。'),
    ),
    run=run,
    preview=preview,
)


def main():
    src_dir = sys.argv[1] if len(sys.argv) > 1 else '.'
    out_dir = sys.argv[2] if len(sys.argv) > 2 else ''
    # 静默 ctx：原脚本的命令行输出只有末尾这段汇总，没有逐文件进度，
    # 走 ConsoleContext 会多出 \r 进度行，破坏既有 stdout 格式。
    result = run({'src_dir': src_dir, 'out_dir': out_dir}, RunContext())
    print(result.summary)


if __name__ == '__main__':
    main()
