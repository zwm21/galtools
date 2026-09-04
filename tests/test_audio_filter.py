# -*- coding: utf-8 -*-
"""语音筛选的纯逻辑测试。

时长解析用两种输入：wav 用标准库 wave 合成真文件（已知帧数 ÷ 采样率即精确
时长）；ogg 无法用标准库合成，改为手工拼出解析器实际读的那几个字段——这样
连 Opus 的 preskip 扣减这种容易写错的细节也能钉住。
"""
import os
import shutil
import struct
import wave
from collections import namedtuple

import pytest

from galtools.core.context import Cancelled, RunContext
from galtools.tools.audio_filter import core, preview, run, validate

Usage = namedtuple('usage', 'total used free')


# ---------------- 造样本 ----------------
def make_wav(path, seconds, rate=8000, channels=1, width=2):
    with wave.open(str(path), 'wb') as w:
        w.setnchannels(channels)
        w.setsampwidth(width)
        w.setframerate(rate)
        w.writeframes(b'\x00' * (round(rate * seconds) * channels * width))
    return str(path)


def wav_header(data_size, rate=8000, channels=1, bits=16):
    """只有头、没有真实采样数据的 wav。data 的声明长度可以随便撒谎——
    解析器算的是 data_size / byte_rate，从不读采样。"""
    fmt = struct.pack('<HHIIHH', 1, channels, rate,
                      rate * channels * bits // 8, channels * bits // 8, bits)
    body = (b'WAVE' + b'fmt ' + struct.pack('<I', len(fmt)) + fmt
            + b'data' + struct.pack('<I', data_size))
    return b'RIFF' + struct.pack('<I', len(body)) + body


def ogg_page(granule, eos=True):
    """一个 27 字节的 Ogg 页头：version 在 +4、header_type 在 +5、granule 在 +6。"""
    return (b'OggS' + bytes([0, 0x04 if eos else 0x00])
            + struct.pack('<q', granule) + b'\x00' * 13)


def ogg_bytes(kind, rate=44100, granule=0, preskip=0, eos=True):
    if kind == 'opus':
        idhdr = b'OpusHead' + b'\x01\x02' + struct.pack('<H', preskip)
    else:
        # \x01vorbis + version(4) + channels(1) + rate(4)
        idhdr = b'\x01vorbis' + b'\x00' * 4 + b'\x01' + struct.pack('<I', rate)
    first = b'OggS' + bytes([0, 0x02]) + b'\x00' * 21
    return first + idhdr + ogg_page(granule, eos)


def write(path, data):
    path.write_bytes(data)
    return str(path)


# ---------------- wav ----------------
def test_wav_duration_exact(tmp_path):
    path = make_wav(tmp_path / 'a.wav', 2.5)
    assert core.parse_wav_duration(path) == pytest.approx(2.5)


def test_wav_duration_respects_channels_and_width(tmp_path):
    path = make_wav(tmp_path / 'a.wav', 1.25, rate=16000, channels=2, width=1)
    assert core.parse_wav_duration(path) == pytest.approx(1.25)


def test_wav_skips_unknown_chunks(tmp_path):
    """LIST 之类的额外块要被跳过而不是让解析放弃。"""
    fmt = struct.pack('<HHIIHH', 1, 1, 8000, 16000, 2, 16)
    body = (b'WAVE' + b'LIST' + struct.pack('<I', 4) + b'INFO'
            + b'fmt ' + struct.pack('<I', len(fmt)) + fmt
            + b'data' + struct.pack('<I', 16000))
    path = write(tmp_path / 'a.wav', b'RIFF' + struct.pack('<I', len(body)) + body)
    assert core.parse_wav_duration(path) == pytest.approx(1.0)


def test_wav_rejects_non_riff(tmp_path):
    assert core.parse_wav_duration(write(tmp_path / 'a.wav', b'not a wav')) is None


def test_wav_rejects_missing_data_chunk(tmp_path):
    fmt = struct.pack('<HHIIHH', 1, 1, 8000, 16000, 2, 16)
    body = b'WAVE' + b'fmt ' + struct.pack('<I', len(fmt)) + fmt
    path = write(tmp_path / 'a.wav', b'RIFF' + struct.pack('<I', len(body)) + body)
    assert core.parse_wav_duration(path) is None


def test_wav_zero_length_is_failure_not_zero(tmp_path):
    """时长 0 走 `0 < d` 判定，归为解析失败。"""
    assert core.parse_wav_duration(write(tmp_path / 'a.wav', wav_header(0))) is None


def test_wav_beyond_sane_duration_is_failure(tmp_path):
    """超过 MAX_SANE_DURATION 返回 None → 计为解析失败，不是「被排除」。"""
    byte_rate = 8000 * 2
    ok = write(tmp_path / 'ok.wav', wav_header(int(byte_rate * 120)))
    bad = write(tmp_path / 'bad.wav', wav_header(int(byte_rate * 120.5)))
    assert core.parse_wav_duration(ok) == pytest.approx(120.0)
    assert core.parse_wav_duration(bad) is None


# ---------------- ogg ----------------
def test_ogg_vorbis_duration(tmp_path):
    path = write(tmp_path / 'a.ogg', ogg_bytes('vorbis', rate=44100,
                                               granule=44100 * 3))
    assert core.parse_ogg_duration(path) == pytest.approx(3.0)


def test_ogg_opus_subtracts_preskip(tmp_path):
    """Opus 的 granule 恒按 48kHz 计，且要扣掉 preskip。"""
    path = write(tmp_path / 'a.ogg', ogg_bytes('opus', granule=48000 * 10 + 312,
                                               preskip=312))
    assert core.parse_ogg_duration(path) == pytest.approx(10.0)


def test_ogg_walks_back_past_pages_without_eos(tmp_path):
    """从尾部往前找，非 EOS 页要跳过继续找，取 EOS 页的 granule。"""
    data = ogg_bytes('vorbis', granule=44100) + ogg_page(44100 * 99, eos=False)
    assert core.parse_ogg_duration(
        write(tmp_path / 'a.ogg', data)) == pytest.approx(1.0)


def test_ogg_without_eos_page_is_failure(tmp_path):
    data = ogg_bytes('vorbis', granule=44100, eos=False)
    assert core.parse_ogg_duration(write(tmp_path / 'a.ogg', data)) is None


def test_ogg_without_id_header_is_failure(tmp_path):
    assert core.parse_ogg_duration(
        write(tmp_path / 'a.ogg', ogg_page(44100))) is None


def test_ogg_zero_sample_rate_is_failure(tmp_path):
    path = write(tmp_path / 'a.ogg', ogg_bytes('vorbis', rate=0, granule=1))
    assert core.parse_ogg_duration(path) is None


def test_get_duration_dispatches_on_extension(tmp_path):
    wav = make_wav(tmp_path / 'a.wav', 1.0)
    assert core.get_duration(wav) == pytest.approx(1.0)
    # 内容是 wav，扩展名不认识 → 不解析
    mp3 = tmp_path / 'a.mp3'
    mp3.write_bytes(open(wav, 'rb').read())
    assert core.get_duration(str(mp3)) is None
    assert core.get_duration(str(tmp_path / '不存在.wav')) is None


# ---------------- 扫描 ----------------
def build_corpus(root):
    root.mkdir(exist_ok=True)
    make_wav(root / '短.wav', 1.0)
    make_wav(root / '长.wav', 8.0)
    (root / '说明.txt').write_text('不是音频', encoding='utf-8')
    sub = root / 'sub'
    sub.mkdir()
    make_wav(sub / '子.wav', 5.0)
    old_out = root / 'root_大于6秒'
    old_out.mkdir()
    make_wav(old_out / '上次的产物.wav', 9.0)
    return root


def test_scan_top_level_only(tmp_path):
    root = build_corpus(tmp_path / 'root')
    audio, failed, total = core.scan_audio_files(str(root), recursive=False)
    assert sorted(os.path.basename(p) for p, _, _ in audio) == ['短.wav', '长.wav']
    assert (failed, total) == ([], 2)


def test_scan_recursive_prunes_own_output_dirs(tmp_path):
    root = build_corpus(tmp_path / 'root')
    audio, _, _ = core.scan_audio_files(str(root), recursive=True)
    names = sorted(os.path.basename(p) for p, _, _ in audio)
    assert names == ['子.wav', '短.wav', '长.wav']    # 上次的产物被剔除


def test_out_dir_pattern_misses_romaji_names():
    """既有缺陷，钉住而不修：剔除规则只认中文命名，罗马字命名的历史输出目录
    递归时仍会被重复扫进来。靠预览里的文件总数让用户当场看出数字不对。"""
    assert core.OUT_DIR_PATTERN.match('chika_大于6秒')
    assert core.OUT_DIR_PATTERN.match('chika_大于6.5秒')
    assert not core.OUT_DIR_PATTERN.match('chika_dayu7miaoqiebuchao9miao - fuben')


def test_scan_counts_unparsable_as_failure(tmp_path):
    root = tmp_path / 'root'
    root.mkdir()
    make_wav(root / '好.wav', 1.0)
    (root / '坏.wav').write_bytes(b'not a wav at all')
    audio, failed, total = core.scan_audio_files(str(root), recursive=False)
    assert len(audio) == 1 and len(failed) == 1 and total == 2


def test_truncated_ogg_is_one_failure_not_a_dead_scan(tmp_path):
    """截断到几十字节的 ogg 会在头部切片上抛 struct.error。它必须只算这一个
    文件失败——原先它会一路穿到界面，把整次扫描连同已解析的结果一起废掉。"""
    root = tmp_path / 'root'
    root.mkdir()
    make_wav(root / '好.wav', 1.0)
    # 'OpusHead' 贴在文件末尾，preskip 字段只剩 1 个字节
    (root / '截断.ogg').write_bytes(b'OggS' + b'\x00' * 20 + b'OpusHead' + b'\x01')
    assert core.get_duration(str(root / '截断.ogg')) is None
    audio, failed, total = core.scan_audio_files(str(root), recursive=False)
    assert [os.path.basename(p) for p, _, _ in audio] == ['好.wav']
    assert [os.path.basename(p) for p in failed] == ['截断.ogg']
    assert total == 2


def test_scan_reports_progress_and_honours_cancel(tmp_path):
    root = tmp_path / 'root'
    root.mkdir()
    for i in range(3):
        make_wav(root / ('%d.wav' % i), 1.0)

    class Recorder(RunContext):
        def __init__(self):
            super().__init__()
            self.notes = []

        def progress(self, done, total, note=''):
            self.notes.append((done, total))

    ctx = Recorder()
    core.scan_audio_files(str(root), recursive=False, ctx=ctx)
    assert ctx.notes == [(1, 3), (2, 3), (3, 3)]

    class Canceller(RunContext):
        def check_cancel(self):
            raise Cancelled()

    with pytest.raises(Cancelled):
        core.scan_audio_files(str(root), recursive=False, ctx=Canceller())


# ---------------- 缓存 ----------------
def test_ensure_scan_caches_by_dir_and_recursion(tmp_path):
    root = build_corpus(tmp_path / 'root')
    calls = {'n': 0}
    real = core.scan_audio_files

    def counting(*a, **kw):
        calls['n'] += 1
        return real(*a, **kw)

    core.scan_audio_files = counting
    try:
        ctx = RunContext()
        params = {'src': str(root), 'recursive': False}
        first = core.ensure_scan(params, ctx)
        again = core.ensure_scan(params, ctx)
        assert calls['n'] == 1 and again is first
        # 换递归开关必须重扫，不能吃旧缓存
        core.ensure_scan({'src': str(root), 'recursive': True}, ctx)
        assert calls['n'] == 2
        # 缓存只留最近一次，切回来会再扫
        core.ensure_scan(params, ctx)
        assert calls['n'] == 3
    finally:
        core.scan_audio_files = real


def test_ensure_scan_can_stay_silent(tmp_path):
    """命令行版扫描时只打一句「请稍候」，不能多出逐文件进度。"""
    root = build_corpus(tmp_path / 'root')

    class Loud(RunContext):
        def progress(self, done, total, note=''):
            raise AssertionError('不该报进度')

    core.ensure_scan({'src': str(root)}, Loud(), report_progress=False)


# ---------------- 筛选 ----------------
def audio_set(*durations):
    return [('%s.ogg' % core.fmt_num(d), float(d), 100) for d in durations]


def test_select_hits_is_strictly_above_threshold():
    hits = core.select_hits(audio_set(5.9, 6, 6.1), 6, None)
    assert [d for _, d, _ in hits] == [6.1]


def test_select_hits_upper_bound_is_inclusive():
    hits = core.select_hits(audio_set(7, 8, 9, 9.1), 7, 9)
    assert [d for _, d, _ in hits] == [9, 8]


def test_select_hits_sorted_descending():
    """降序是复制顺序与清单顺序的依据，不能改。"""
    hits = core.select_hits(audio_set(1, 9, 3, 7), 0.5, None)
    assert [d for _, d, _ in hits] == [9, 7, 3, 1]


def test_select_hits_empty_when_range_excludes_all():
    assert core.select_hits(audio_set(1, 2), 10, None) == []


def test_duration_stats():
    stats = core.duration_stats(audio_set(1, 5, 9))
    assert stats['longest'][1] == 9
    assert stats['shortest'][1] == 1
    assert stats['median'] == 5


# ---------------- 命名 ----------------
def test_fmt_num_drops_pointless_decimals():
    assert core.fmt_num(6.0) == '6'
    assert core.fmt_num(6.5) == '6.5'
    assert core.fmt_num(0.1) == '0.1'


def test_out_dir_name():
    assert core.out_dir_name(r'X:\a\chika', 6, None) == 'chika_大于6秒'
    assert core.out_dir_name(r'X:\a\chika\\', 7, 9) == 'chika_大于7秒且不超9秒'


def test_resolve_out_dir_appends_number(tmp_path):
    root = tmp_path / 'root'
    root.mkdir()
    first = core.resolve_out_dir(str(root), 6, None)
    assert os.path.basename(first) == 'root_大于6秒'
    os.makedirs(first)
    second = core.resolve_out_dir(str(root), 6, None)
    assert os.path.basename(second) == 'root_大于6秒_2'


def test_unique_dest_appends_number(tmp_path):
    target = tmp_path / 'a.ogg'
    assert core.unique_dest(str(target)) == str(target)
    target.write_bytes(b'')
    assert core.unique_dest(str(target)) == str(tmp_path / 'a_1.ogg')


def test_describe_condition():
    assert core.describe_condition(6.0, None) == '时长 > 6 秒'
    assert core.describe_condition(7.0, 9.0) == '7 秒 < 时长 <= 9 秒'


# ---------------- 复制与取消 ----------------
def test_copy_hits_keeps_partial_work_on_cancel(tmp_path):
    src = tmp_path / 'src'
    src.mkdir()
    out = tmp_path / 'out'
    out.mkdir()
    hits = [(make_wav(src / ('%d.wav' % i), 1.0), 1.0, 100) for i in range(4)]

    class CancelOnThird(RunContext):
        def __init__(self):
            super().__init__()
            self.calls = 0

        def check_cancel(self):
            self.calls += 1
            if self.calls == 3:
                raise Cancelled()

    with pytest.raises(Cancelled) as caught:
        core.copy_hits(hits, str(out), CancelOnThird())

    assert len(os.listdir(out)) == 2            # 已复制的留下
    assert core.MANIFEST_NAME not in os.listdir(out)   # 清单不写
    assert caught.value.partial.output_paths == [str(out)]
    assert '已复制 2 / 4' in caught.value.partial.summary


def test_copy_hits_collects_failures(tmp_path):
    out = tmp_path / 'out'
    out.mkdir()
    good = make_wav(tmp_path / 'good.wav', 1.0)
    hits = [(good, 1.0, 100), (str(tmp_path / '不存在.wav'), 2.0, 100)]
    copied, failed, manifest = core.copy_hits(hits, str(out), RunContext())
    assert copied == 1
    assert [os.path.basename(p) for p, _ in failed] == ['不存在.wav']
    assert manifest == [('good.wav', 1.0)]


def test_write_manifests_layout(tmp_path):
    out = tmp_path / 'out'
    out.mkdir()
    hits = audio_set(9, 7)
    path = core.write_manifests(str(out), 'X:\\src', '时长 > 6 秒', hits, 2,
                                [('9.ogg', 9.0), ('7.ogg', 7.0)],
                                ['X:\\src\\坏.ogg'], [], RunContext())
    text = open(path, encoding='utf-8').read()
    lines = text.splitlines()
    assert lines[0] == '复制清单'
    assert lines[7] == '文件名\t时长(秒)'
    assert lines[8:] == ['9.ogg\t9.00', '7.ogg\t7.00']
    assert text.endswith('\n')
    assert (out / core.SCAN_FAIL_NAME).exists()
    assert not (out / core.COPY_FAIL_NAME).exists()


# ---------------- 校验 ----------------
def test_validate_accepts_sane_params(tmp_path):
    assert validate({'src': str(tmp_path), 'threshold': 6.0,
                     'max_limit': None}) == []
    assert validate({'src': str(tmp_path), 'threshold': 6.0,
                     'max_limit': 9.0}) == []


def test_validate_rejects_upper_bound_not_above_threshold(tmp_path):
    """跨字段规则，min/max 常量表达不了。缺了它，用户填「阈值 10 上限 5」
    只会看到命中 0 个，与真实的空结果无法区分。"""
    for limit in (10.0, 5.0):
        assert validate({'src': str(tmp_path), 'threshold': 10.0,
                         'max_limit': limit}) == [('max_limit', '必须大于筛选阈值')]


def test_validate_rejects_non_positive_numbers(tmp_path):
    assert validate({'src': str(tmp_path), 'threshold': 0.0}) == \
        [('threshold', '必须为正数')]
    assert validate({'src': str(tmp_path), 'threshold': 6.0,
                     'max_limit': -1.0}) == [('max_limit', '必须为正数')]


def test_validate_rejects_missing_dir(tmp_path):
    errors = validate({'src': str(tmp_path / '不存在'), 'threshold': 6.0})
    assert errors == [('src', '目录不存在或不可访问')]
    # 空目录名交给必填校验，validate 不重复报错
    assert validate({'src': '', 'threshold': 6.0}) == []


# ---------------- run ----------------
def test_run_clears_scan_cache(tmp_path):
    """输出目录建在源目录内，跑完之后旧的扫描结果必然过期。"""
    src = tmp_path / 'src'
    src.mkdir()
    make_wav(src / '长.wav', 8.0)
    ctx = RunContext()
    params = {'src': str(src), 'threshold': 6.0, 'max_limit': None}
    result = run(params, ctx)
    assert 'scan' not in ctx.session
    assert '复制成功 : 1 个' in result.summary
    out = os.path.join(str(src), 'src_大于6秒')
    assert sorted(os.listdir(out)) == sorted(['长.wav', core.MANIFEST_NAME])


def test_run_without_hits_does_not_create_output_dir(tmp_path):
    src = tmp_path / 'src'
    src.mkdir()
    make_wav(src / '短.wav', 1.0)
    result = run({'src': str(src), 'threshold': 6.0, 'max_limit': None},
                 RunContext())
    assert '没有满足条件' in result.summary
    assert result.output_paths == []
    assert os.listdir(src) == ['短.wav']


# ---------------- 磁盘余量 ----------------
def one_hit(tmp_path):
    src = tmp_path / 'src'
    src.mkdir()
    make_wav(src / '长.wav', 8.0)
    return {'src': str(src), 'threshold': 6.0, 'max_limit': None}


def test_disk_check_looks_at_the_source_drive(tmp_path, monkeypatch):
    """查的是源盘而不是目标盘。只因输出目录嵌在源目录内，这才恰好正确。"""
    params = one_hit(tmp_path)
    asked = []
    monkeypatch.setattr(shutil, 'disk_usage',
                        lambda p: asked.append(p) or Usage(0, 0, 1 << 40))
    run(params, RunContext())
    assert asked == [params['src']]


def test_preview_blocks_start_when_disk_is_short(tmp_path, monkeypatch):
    params = one_hit(tmp_path)
    monkeypatch.setattr(shutil, 'disk_usage', lambda p: Usage(0, 0, 1))
    result = preview(params, RunContext())
    assert result.ok is False
    assert any('无法复制' in w for w in result.warnings)


def test_run_aborts_before_creating_output_dir_when_disk_is_short(
        tmp_path, monkeypatch):
    params = one_hit(tmp_path)
    monkeypatch.setattr(shutil, 'disk_usage', lambda p: Usage(0, 0, 1))
    result = run(params, RunContext())
    assert '磁盘剩余空间不足' in result.summary
    assert os.listdir(params['src']) == ['长.wav']
