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
    MERGED_NAME, extract_mjo, is_valid_str, main, resolve_paths, run,
)

V1 = b'MajiroObjV1.000\x00'
X1 = b'MajiroObjX1.000\x00'

# 多数测试会在字节码尾部补未知 opcode，验证逐字节重同步不会影响正常指令。
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


def test_string_id_operand_is_not_scanned_as_an_opcode(tmp_path):
    fake_text = struct.pack('<HH', 0x83A, 0x0840) + struct.pack('<H', 3) + b'AB\0'
    assert lines_of(tmp_path, fake_text) == []


def test_complete_show_text_may_end_at_the_declared_boundary(tmp_path):
    path = tmp_path / 'a.mjo'
    path.write_bytes(build_mjo(show_text('末尾')))
    assert extract_mjo(str(path)) == ['末尾']


def test_show_text_outside_the_declared_boundary_is_dropped(tmp_path):
    """声明范围里只剩 opcode，物理文件后的操作数不属于字节码。"""
    kept = show_text('残る') + dialog_close()
    dropped = show_text('消える')
    assert lines_of(tmp_path, kept + dropped,
                    declared_len=len(kept) + 2) == ['残る']
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


def test_a_drive_root_is_not_the_drives_working_directory():
    """`E:` 指的是该盘的当前工作目录，三处都得补回分隔符：源目录会去列错地方，
    输出目录让 txt 落成 `E:xxx.txt` 这种驱动器相对路径，而算父目录时剥掉尾分隔
    符又会把补好的 `E:\\` 打回 `E:`。"""
    src, out, merged = resolve_paths({'src_dir': 'E:', 'out_dir': 'E:'})
    root = 'E:' + os.sep
    assert (src, out) == (root, root)
    assert merged == os.path.join(root, MERGED_NAME)


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


def test_run_leaves_the_merged_text_alone_when_all_inputs_fail(tmp_path):
    src = tmp_path / 'src'
    src.mkdir()
    (src / 'bad.mjo').write_bytes(b'garbage')
    (src / 'truncated.mjo').write_bytes(V1)
    merged = tmp_path / MERGED_NAME
    merged.write_text('上一次的成果', encoding='utf-8')
    out = tmp_path / 'out'

    result = run({'src_dir': str(src), 'out_dir': str(out)}, RunContext())

    assert merged.read_text(encoding='utf-8') == '上一次的成果'
    assert result.output_paths == []
    assert [name for name, _ in result.failures] == ['bad.mjo', 'truncated.mjo']
    assert '成功 0 个' in result.summary


def test_run_says_so_instead_of_raising_when_the_dir_is_missing(tmp_path):
    """命令行直接调 run，没有 preview 那道闸。目录名敲错该得到一句话而不是
    traceback，也绝不能碰输出目录。"""
    missing = tmp_path / 'nope'
    result = run({'src_dir': str(missing), 'out_dir': str(tmp_path / 'out')},
                 RunContext())
    assert '目录不存在' in result.summary
    assert result.output_paths == []
    assert [n for n, _ in result.failures] == [str(missing)]
    assert not (tmp_path / 'out').exists()


def test_run_says_so_instead_of_raising_when_the_dir_is_unreadable(tmp_path,
                                                                  monkeypatch):
    """isdir 为真但 listdir 抛 OSError：权限不足、盘掉线都会走到这里。"""
    src = tmp_path / 'src'
    src.mkdir()

    def boom(_):
        raise PermissionError(13, '拒绝访问')

    monkeypatch.setattr('galtools.tools.mjo_text.os.listdir', boom)
    result = run({'src_dir': str(src), 'out_dir': str(tmp_path / 'out')},
                 RunContext())
    assert '无法读取目录' in result.summary
    assert result.output_paths == []
    assert not (tmp_path / 'out').exists()


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


def test_atomic_text_write_keeps_the_previous_file_on_failure(tmp_path, monkeypatch):
    import galtools.tools.mjo_text as mod

    target = tmp_path / 'old.txt'
    target.write_text('旧内容', encoding='utf-8')

    def boom(_src, _dst):
        raise OSError('替换失败')

    monkeypatch.setattr(mod.os, 'replace', boom)
    with pytest.raises(OSError, match='替换失败'):
        mod.write_text_atomic(str(target), '新内容')
    assert target.read_text(encoding='utf-8') == '旧内容'
    assert os.listdir(str(tmp_path)) == ['old.txt']


def test_single_file_write_failure_does_not_abort_the_batch(tmp_path, monkeypatch):
    """一个文件写不出去只算它自己失败，其余 txt 与合并全文照常产出。"""
    import galtools.tools.mjo_text as mod

    src = tmp_path / 'src'
    src.mkdir()
    (src / 'a.mjo').write_bytes(build_mjo(show_text('あ')))
    (src / 'b.mjo').write_bytes(build_mjo(show_text('い')))
    real_write = mod.write_text_atomic

    def picky(path, text):
        if os.path.basename(path) == 'a.txt':
            raise OSError('文件名过长')
        return real_write(path, text)

    monkeypatch.setattr(mod, 'write_text_atomic', picky)
    result = run({'src_dir': str(src), 'out_dir': str(tmp_path / 'out')},
                 RunContext())
    assert result.failures == [('a.mjo', '写出失败: 文件名过长')]
    assert not (tmp_path / 'out' / 'a.txt').exists()
    assert (tmp_path / 'out' / 'b.txt').read_text(encoding='utf-8') == 'い'
    assert str(tmp_path / MERGED_NAME) in result.output_paths
    # 失败的那个既不该进合并全文，也不该计进条数
    assert 'あ' not in (tmp_path / MERGED_NAME).read_text(encoding='utf-8')
    assert '共提取 1 条文本' in result.summary


def test_merged_write_failure_still_publishes_single_files(tmp_path, monkeypatch):
    """合并全文写不出去时单文件仍算产出，旧的合并全文保持原样。"""
    import galtools.tools.mjo_text as mod

    src = tmp_path / 'src'
    src.mkdir()
    (src / 'a.mjo').write_bytes(build_mjo(show_text('あ')))
    merged = tmp_path / MERGED_NAME
    merged.write_text('旧全文', encoding='utf-8')
    real_write = mod.write_text_atomic

    def picky(path, text):
        if os.path.basename(path) == MERGED_NAME:
            raise OSError('盘满')
        return real_write(path, text)

    monkeypatch.setattr(mod, 'write_text_atomic', picky)
    result = run({'src_dir': str(src), 'out_dir': str(tmp_path / 'out')},
                 RunContext())
    assert (tmp_path / 'out' / 'a.txt').exists()
    assert merged.read_text(encoding='utf-8') == '旧全文'
    assert result.output_paths == [str(tmp_path / 'out')]
    assert result.failures == [(MERGED_NAME, '写出失败: 盘满')]
    assert result.warnings


def test_all_failed_leaves_no_empty_output_dir(tmp_path, monkeypatch):
    import galtools.tools.mjo_text as mod

    src = tmp_path / 'src'
    src.mkdir()
    (src / 'a.mjo').write_bytes(build_mjo(show_text('あ')))

    def boom(_path, _text):
        raise OSError('只读盘')

    monkeypatch.setattr(mod, 'write_text_atomic', boom)
    out = tmp_path / 'out'
    result = run({'src_dir': str(src), 'out_dir': str(out)}, RunContext())
    assert result.output_paths == []
    assert not out.exists()


def test_all_failed_keeps_a_pre_existing_output_dir(tmp_path, monkeypatch):
    """非空的输出目录留着：rmdir 删不掉它，里面是上一轮的成果。"""
    import galtools.tools.mjo_text as mod

    src = tmp_path / 'src'
    src.mkdir()
    (src / 'a.mjo').write_bytes(build_mjo(show_text('あ')))
    out = tmp_path / 'out'
    out.mkdir()
    (out / '上一轮.txt').write_text('旧', encoding='utf-8')

    def boom(_path, _text):
        raise OSError('只读盘')

    monkeypatch.setattr(mod, 'write_text_atomic', boom)
    run({'src_dir': str(src), 'out_dir': str(out)}, RunContext())
    assert (out / '上一轮.txt').read_text(encoding='utf-8') == '旧'


def test_all_failed_keeps_a_pre_existing_empty_output_dir(tmp_path, monkeypatch):
    """空的输出目录只要不是本轮建的就得留着——那是用户先建好的。

    光靠「rmdir 删不掉非空目录」兜不住这条：目录空着的时候它删得掉。
    """
    import galtools.tools.mjo_text as mod

    src = tmp_path / 'src'
    src.mkdir()
    (src / 'a.mjo').write_bytes(build_mjo(show_text('あ')))
    out = tmp_path / 'out'
    out.mkdir()

    def boom(_path, _text):
        raise OSError('只读盘')

    monkeypatch.setattr(mod, 'write_text_atomic', boom)
    result = run({'src_dir': str(src), 'out_dir': str(out)}, RunContext())
    assert out.is_dir()
    assert result.output_paths == []


def test_cancel_after_the_last_parse_does_not_publish_merged(tmp_path, monkeypatch):
    import galtools.tools.mjo_text as mod

    src = tmp_path / 'src'
    src.mkdir()
    (src / 'a.mjo').write_bytes(build_mjo(show_text('あ')))
    merged = tmp_path / MERGED_NAME
    merged.write_text('旧全文', encoding='utf-8')
    real_extract = mod.extract_mjo

    class StopAfterParse(RunContext):
        def __init__(self):
            super().__init__()
            self.parsed = False

        def check_cancel(self):
            if self.parsed:
                raise Cancelled()

    ctx = StopAfterParse()

    def parsed(path):
        result = real_extract(path)
        ctx.parsed = True
        return result

    monkeypatch.setattr(mod, 'extract_mjo', parsed)
    with pytest.raises(Cancelled) as caught:
        run({'src_dir': str(src), 'out_dir': str(tmp_path / 'out')}, ctx)
    assert merged.read_text(encoding='utf-8') == '旧全文'
    assert caught.value.partial.output_paths == []


def test_cancel_before_merged_publish_reports_written_files(tmp_path):
    src = tmp_path / 'src'
    src.mkdir()
    (src / 'a.mjo').write_bytes(build_mjo(show_text('あ')))
    merged = tmp_path / MERGED_NAME
    merged.write_text('旧全文', encoding='utf-8')

    class StopBeforeMerged(RunContext):
        def __init__(self):
            super().__init__()
            self.calls = 0

        def check_cancel(self):
            self.calls += 1
            if self.calls == 4:
                raise Cancelled()

    with pytest.raises(Cancelled) as caught:
        run({'src_dir': str(src), 'out_dir': str(tmp_path / 'out')},
            StopBeforeMerged())
    assert (tmp_path / 'out' / 'a.txt').exists()
    assert merged.read_text(encoding='utf-8') == '旧全文'
    assert caught.value.partial.output_paths == [str(tmp_path / 'out')]


# ---------------- 命令行 ----------------
def test_cli_exit_code_says_whether_anything_was_written(monkeypatch, tmp_path):
    """什么都没写出来就得非零退出，与 vndb_voiced/cli.py 同一套约定。以前不论
    目录不存在还是目录里没有 .mjo 都是 exit 0，脚本里的 && 不会断。"""
    import galtools.tools.mjo_text as mod

    src = tmp_path / 'src'
    src.mkdir()
    out = tmp_path / 'out'

    monkeypatch.setattr(mod.sys, 'argv',
                        ['mjo_text', str(tmp_path / 'nope'), str(out)])
    assert main() == 1                       # 目录不存在

    monkeypatch.setattr(mod.sys, 'argv', ['mjo_text', str(src), str(out)])
    assert main() == 1                       # 目录在，但没有 .mjo

    (src / 'bad.mjo').write_bytes(b'garbage')
    assert main() == 1                       # 有输入，但一个都没解析成功

    (src / 'a.mjo').write_bytes(build_mjo(
        show_text('あ') + dialog_close() + PAD))
    assert main() == 0                       # 写出来了
