# -*- coding: utf-8 -*-
"""mjo 提取的纯逻辑测试。

用手工拼的最小 MajiroObjV1 当输入。入口表项数默认取 1，此时 header_len 恰好是
0x28；count 取别的值时字节码起点跟着后移，`test_entry_table_is_not_scanned`
钉的就是这一点。
"""
import os
import struct

import pytest

from galtools.core.context import Cancelled, RunContext
from galtools.tools.mjo_text import (
    MERGED_NAME, extract_mjo, is_valid_str, resolve_paths, run,
)

V1 = b'MajiroObjV1.000\x00'
X1 = b'MajiroObjX1.000\x00'

# is_valid_str 要求字符串结尾的 \0 后面还有字节，主循环 `i + 2 < script_len`
# 也会漏掉贴在末尾的 opcode。给每份字节码留一点尾部填充，让最后一条指令
# 落在正常范围内。填充是 0 字节，被当作未知 opcode 逐字节跳过。
PAD = b'\x00' * 4


def build_mjo(bytecode, count=1, signature=V1, declared_len=None):
    head = bytearray(signature.ljust(0x18, b'\x00'))
    head += struct.pack('<I', count)
    head += struct.pack('<II', 0, 0) * count
    head += struct.pack('<I', len(bytecode) if declared_len is None
                        else declared_len)
    assert count != 1 or len(head) == 0x28
    return bytes(head) + bytecode


def show_text(s):
    raw = s.encode('cp932')
    return struct.pack('<HH', 0x840, len(raw) + 1) + raw + b'\x00'


def adv(sub):
    return struct.pack('<HHH', 0x842, 0x002, sub)


def brk_line():
    return adv(0x06E)


def click_wait():
    return adv(0x070)


def dialog_close():
    return adv(0x077)


def lines_of(tmp_path, bytecode, **kw):
    path = tmp_path / 'a.mjo'
    path.write_bytes(build_mjo(bytecode + PAD, **kw))
    return extract_mjo(str(path))


# ---------------- 头部 ----------------
def test_rejects_encrypted_x1(tmp_path):
    path = tmp_path / 'a.mjo'
    path.write_bytes(build_mjo(show_text('あ') + PAD, signature=X1))
    with pytest.raises(ValueError, match='加密'):
        extract_mjo(str(path))


def test_rejects_unknown_header(tmp_path):
    path = tmp_path / 'a.mjo'
    path.write_bytes(b'NotAMajiroFile\x00\x00' + b'\x00' * 32)
    with pytest.raises(ValueError, match='未知脚本头'):
        extract_mjo(str(path))


# ---------------- 取文本 ----------------
def test_name_and_line_paired(tmp_path):
    """不以「开头的短句 + 紧随其后以「开头的句子 = 说话人 + 台词。"""
    lines = lines_of(tmp_path,
                     show_text('樹') + show_text('「やあ」') + dialog_close())
    assert lines == ['樹：', '「やあ」']


def test_dialog_close_between_name_and_line(tmp_path):
    """名字与台词之间隔着对话框清空时，配对结果一样。"""
    lines = lines_of(tmp_path, show_text('樹') + dialog_close()
                     + show_text('「やあ」') + dialog_close())
    assert lines == ['樹：', '「やあ」']


def test_break_line_becomes_newline(tmp_path):
    lines = lines_of(tmp_path, show_text('一行目') + brk_line()
                     + show_text('二行目') + dialog_close())
    assert lines == ['一行目\n二行目']


def test_multiline_never_taken_as_name(tmp_path):
    """含换行的句子不参与人名配对，即便下一句以「开头。"""
    lines = lines_of(tmp_path, show_text('樹') + brk_line()
                     + show_text('の声') + dialog_close()
                     + show_text('「やあ」') + dialog_close())
    assert lines == ['樹\nの声', '「やあ」']


def test_long_line_never_taken_as_name(tmp_path):
    """人名判定卡在 20 字：21 字的句子按普通台词处理。"""
    long_name = 'あ' * 21
    lines = lines_of(tmp_path, show_text(long_name)
                     + show_text('「やあ」') + dialog_close())
    assert lines == [long_name, '「やあ」']
    short = 'あ' * 20
    lines = lines_of(tmp_path, show_text(short)
                     + show_text('「やあ」') + dialog_close())
    assert lines == [short + '：', '「やあ」']


def test_trailing_click_wait_dropped_at_dialog_close(tmp_path):
    lines = lines_of(tmp_path,
                     show_text('待って') + click_wait() + dialog_close())
    assert lines == ['待って']


def test_inner_click_wait_stripped_from_output(tmp_path):
    lines = lines_of(tmp_path, show_text('あ') + click_wait()
                     + show_text('い') + dialog_close())
    assert lines == ['あい']


def test_all_leading_brackets_stripped_when_unclosed(tmp_path):
    """s.lstrip('「') 剥掉所有前导括号而不是一个，且只在句子不以」结尾时发生。"""
    assert lines_of(tmp_path,
                    show_text('「「あ') + dialog_close()) == ['あ']
    assert lines_of(tmp_path,
                    show_text('「「あ」') + dialog_close()) == ['「「あ」']


def test_invalid_string_length_skipped(tmp_path):
    """长度字段不合法的 ShowText 不产出文本，扫描继续。"""
    bogus = struct.pack('<HH', 0x840, 0)
    assert lines_of(tmp_path, bogus + show_text('あ') + dialog_close()) == ['あ']


def test_string_id_and_parse_str_skipped(tmp_path):
    noise = struct.pack('<HH', 0x83A, 0x1234) + struct.pack('<H', 0x841)
    assert lines_of(tmp_path,
                    noise + show_text('あ') + dialog_close()) == ['あ']


def test_last_opcode_at_boundary_is_dropped(tmp_path):
    """既有的差一行为：正好贴在 script_len 末尾的 opcode 不会被处理。

    声明长度只到最后一条 ShowText 的 opcode 之后，于是 i + 2 == script_len，
    循环直接结束，那句话丢掉。刻意保留，此处只做钉桩。
    """
    kept = show_text('残る') + dialog_close()
    dropped = show_text('消える')
    assert lines_of(tmp_path, kept + dropped,
                    declared_len=len(kept) + 2) == ['残る']
    # 同样的字节码，只把声明长度放宽，那句话就回来了——证明上面丢的是边界
    # 而不是别的原因。
    assert lines_of(tmp_path, kept + dropped) == ['残る', '消える']


def test_adv_event_at_the_very_end_does_not_kill_the_file(tmp_path):
    """末尾几字节上的 0x842 曾让 unpack_from 越界，把整份文本一起废掉。"""
    kept = show_text('残る') + dialog_close()
    path = tmp_path / 'a.mjo'
    # 尾巴上只留 AdvEvent + 类型码，第三个 u16 缺席
    path.write_bytes(build_mjo(kept + struct.pack('<HH', 0x842, 0x002)))
    assert extract_mjo(str(path)) == ['残る']


def test_entry_table_is_not_scanned(tmp_path):
    """count != 1 时字节码从 header_len 起步，入口函数表不再被当字节码扫。

    入口表里塞一段合法的 ShowText，位置正好是旧代码硬编码的起点 0x28：按
    header_len 起步看不见它，按 0x28 起步会把它当台词扫出来。
    """
    count = 3                                  # header_len = 0x18+24+4+4 = 0x38
    bait = show_text('あい')                    # 9 字节，塞得进表尾的 12 字节
    table = bytearray(b'\x00' * 24)
    table[0x28 - 0x1C:0x28 - 0x1C + len(bait)] = bait
    bytecode = show_text('本物のセリフ') + dialog_close() + PAD
    data = (bytes(V1.ljust(0x18, b'\x00')) + struct.pack('<I', count)
            + bytes(table) + struct.pack('<I', len(bytecode)) + bytecode)
    path = tmp_path / 'a.mjo'
    path.write_bytes(data)

    # 先证明这段饵真的会被旧起点扫到，否则这条测试没有牙
    assert struct.unpack_from('<H', data, 0x28)[0] == 0x840
    assert is_valid_str(data, 0x2A)
    assert extract_mjo(str(path)) == ['本物のセリフ']


# ---------------- 路径推导 ----------------
def test_out_dir_defaults_under_source():
    src, out, merged = resolve_paths({'src_dir': r'X:\game\scenario.arc~'})
    assert out == os.path.join(src, 'script_text')
    assert merged == os.path.join(src, MERGED_NAME)


def test_merged_goes_to_parent_of_out_dir():
    _, _, merged = resolve_paths({'src_dir': r'X:\game\scenario.arc~',
                                  'out_dir': r'X:\game\out\txt'})
    assert merged == os.path.join(r'X:\game\out', MERGED_NAME)


def test_trailing_separator_ignored():
    _, _, merged = resolve_paths({'src_dir': 'X:\\', 'out_dir': 'X:\\a\\b\\'})
    assert merged == os.path.join('X:\\a', MERGED_NAME)


def test_relative_out_dir_puts_merged_in_cwd():
    """既有的坑：输出目录是相对路径时 dirname 为空串，合并全文落到当前工作
    目录而不是输出目录旁边。原样保留。"""
    _, _, merged = resolve_paths({'src_dir': '.', 'out_dir': 'script_text'})
    assert merged == os.path.join('.', MERGED_NAME)


# ---------------- run ----------------
def test_run_writes_txt_without_trailing_newline(tmp_path):
    src = tmp_path / 'src'
    src.mkdir()
    (src / 'a.mjo').write_bytes(build_mjo(
        show_text('樹') + show_text('「やあ」') + dialog_close() + PAD))
    out = tmp_path / 'out'
    result = run({'src_dir': str(src), 'out_dir': str(out)}, RunContext())

    assert result.failures == []
    assert (out / 'a.txt').read_text(encoding='utf-8') == '樹：\n「やあ」'
    merged = (tmp_path / MERGED_NAME).read_text(encoding='utf-8')
    assert merged.endswith('「やあ」\n')          # 合并条目有结尾换行
    assert '【a】' in merged


def test_run_leaves_the_merged_text_alone_when_there_is_no_input(tmp_path):
    """空目录不能把上一次的合并全文清成空文件——命令行没有预览那道闸。"""
    src = tmp_path / 'src'
    src.mkdir()
    merged = tmp_path / MERGED_NAME
    merged.write_text('上一次的成果', encoding='utf-8')
    result = run({'src_dir': str(src), 'out_dir': str(tmp_path / 'out')},
                 RunContext())
    assert merged.read_text(encoding='utf-8') == '上一次的成果'
    assert '没有 .mjo 文件' in result.summary
    assert result.output_paths == []
    assert not (tmp_path / 'out').exists()     # 连输出目录都不建


def test_run_collects_per_file_failures(tmp_path):
    src = tmp_path / 'src'
    src.mkdir()
    (src / 'bad.mjo').write_bytes(b'garbage')
    (src / 'truncated.mjo').write_bytes(V1)      # 头部合法但读不到项数
    (src / 'good.mjo').write_bytes(build_mjo(
        show_text('あ') + dialog_close() + PAD))
    result = run({'src_dir': str(src), 'out_dir': str(tmp_path / 'out')},
                 RunContext())

    assert [n for n, _ in result.failures] == ['bad.mjo', 'truncated.mjo']
    assert (tmp_path / 'out' / 'good.txt').exists()
    assert '成功 1 个' in result.summary


def test_cancel_is_not_swallowed_as_a_parse_failure(tmp_path):
    """取消必须穿透批量循环。Cancelled 不是 Exception，且 run 里的 except 已
    收窄到解析异常——两道保障都在这条测试的射程内。"""
    assert not issubclass(Cancelled, Exception)

    src = tmp_path / 'src'
    src.mkdir()
    for name in ('a.mjo', 'b.mjo'):
        (src / name).write_bytes(build_mjo(
            show_text('あ') + dialog_close() + PAD))

    class CancelOnSecond(RunContext):
        def __init__(self):
            super().__init__()
            self.calls = 0

        def check_cancel(self):
            self.calls += 1
            if self.calls == 2:
                raise Cancelled()

    ctx = CancelOnSecond()
    with pytest.raises(Cancelled):
        run({'src_dir': str(src), 'out_dir': str(tmp_path / 'out')}, ctx)
    assert not (tmp_path / MERGED_NAME).exists()
