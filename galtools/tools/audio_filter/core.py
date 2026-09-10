# -*- coding: utf-8 -*-
"""语音时长筛选的纯逻辑：时长解析、目录扫描、筛选、复制、清单。

只用标准库，preview / run / CLI 向导三方共用同一份实现。时长解析算法原样
沿用旧脚本（已与 mutagen 1.48.1 对 2717 个文件全量交叉验证，零偏差）。

时长解析原理（无需解码音频，毫秒级/文件）：
    Ogg Vorbis : 首页 ID 头取采样率（\\x01vorbis 魔数后 +12 字节），
                 末页 EOS 的 granule position ÷ 采样率。
    Ogg Opus   : granule 恒按 48kHz 计，另减去 preskip。
    WAV        : 遍历 RIFF chunk，data 大小 ÷ 字节速率（见 PCM_FORMATS）。

刻意保留的既有行为：
- 命中按时长降序复制，unique_dest 的编号与清单顺序都依赖这个顺序。
- 超过 MAX_SANE_DURATION 时返回 None，文件归入「解析失败」而非「被排除」，
  所以失败数必须在界面上亮出来，否则文件看起来凭空消失。
- 磁盘余量查的是源盘（只因输出目录嵌在源目录内才正确），且零余量。
- OUT_DIR_PATTERN 只匹配中文命名的输出目录，罗马字命名的历史输出目录递归
  时仍会被重复扫进来。不改，靠预览里的文件总数让用户当场看出数字不对。
"""
import math
import os
import re
import shutil
import statistics
import struct
import tempfile

from ...core.context import Cancelled
from ...core.spec import RunResult

DEFAULT_THRESHOLD = 6.0
SUPPORTED_EXTS = {'.ogg', '.wav'}
TAIL_WINDOW = 65536          # 从文件尾部反读的窗口大小（字节）
MAX_SANE_DURATION = 120.0    # 超过该值视为解析异常（游戏语音不可能超过 2 分钟）

# 认得的 fmt 块格式标签。0x0003(IEEE float) 与 0xFFFE(EXTENSIBLE) 的
# wBitsPerSample 与 PCM 同义，data 大小 ÷ 字节速率照样成立。
PCM_FORMATS = frozenset((0x0001, 0x0003, 0xFFFE))

# 本工具输出目录的命名模式，递归扫描时只剔除完整匹配的自产目录。
_NUM_PATTERN = r'(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?'
OUT_DIR_PATTERN = re.compile(
    r'^.+_大于%s秒(?:且不超%s秒)?(?:_\d+)?$' % (_NUM_PATTERN, _NUM_PATTERN))

MANIFEST_NAME = '复制清单.txt'
SCAN_FAIL_NAME = '解析失败清单.txt'
COPY_FAIL_NAME = '复制失败清单.txt'


def fmt_num(x):
    """智能格式化有限数字：6.0 -> '6'，6.5 -> '6.5'。"""
    if not math.isfinite(x):
        raise ValueError('数字必须有限')
    if x == int(x):
        return str(int(x))
    return '%g' % x


# ---------------- 时长解析 ----------------
def parse_ogg_duration(path):
    """返回 Ogg 文件时长（秒），无法解析返回 None。"""
    with open(path, 'rb') as fp:
        head = fp.read(4096)
        v = head.find(b'\x01vorbis')
        o = head.find(b'OpusHead')
        preskip = 0
        if o >= 0:
            rate = 48000  # Opus 的 granule position 恒定按 48kHz 计
            preskip = struct.unpack('<H', head[o + 10:o + 12])[0]
        elif v >= 0:
            # Vorbis ID 头：\x01vorbis + version(4B) + channels(1B) + rate(4B)
            rate = struct.unpack('<I', head[v + 12:v + 16])[0]
            if rate == 0:
                return None
        else:
            return None

        fp.seek(0, os.SEEK_END)
        fsize = fp.tell()
        fp.seek(max(0, fsize - TAIL_WINDOW))
        tail = fp.read()

        # 从后往前找最后一个合法 OggS 页（优先带 EOS 标志的页）
        pos = tail.rfind(b'OggS')
        while pos >= 0:
            if pos + 27 <= len(tail) and tail[pos + 4] == 0:
                granule = struct.unpack('<q', tail[pos + 6:pos + 14])[0]
                if tail[pos + 5] & 0x04:  # EOS（流结束）页
                    d = (granule - preskip) / rate
                    return d if 0 <= d <= MAX_SANE_DURATION else None
            pos = tail.rfind(b'OggS', 0, pos)
        return None


def parse_wav_duration(path):
    """返回 WAV 文件时长（秒），无法解析返回 None。"""
    try:
        with open(path, 'rb') as fp:
            fp.seek(0, os.SEEK_END)
            file_size = fp.tell()
            fp.seek(0)
            hdr = fp.read(12)
            if len(hdr) < 12 or hdr[:4] != b'RIFF' or hdr[8:12] != b'WAVE':
                return None
            rate = channels = bits = format_tag = None
            data_size = None
            while fp.tell() < file_size:
                if file_size - fp.tell() < 8:
                    return None
                ck = fp.read(8)
                cid = ck[:4]
                size = struct.unpack('<I', ck[4:])[0]
                rest = file_size - fp.tell()
                if size > rest:
                    return None      # 声明长度超出物理文件，整份都不可信
                # 奇数长度的块后面该有一个填充字节，但末块的那个常被省略。
                # 只在文件真的到头时容忍，别把它和上面那种截断混为一谈。
                skip = min(size + (size & 1), rest)
                if cid == b'fmt ':
                    d = fp.read(size)
                    if len(d) < 16:
                        return None
                    format_tag, channels = struct.unpack('<HH', d[:4])
                    rate = struct.unpack('<I', d[4:8])[0]
                    bits = struct.unpack('<H', d[14:16])[0]
                    fp.seek(skip - size, os.SEEK_CUR)
                else:
                    if cid == b'data':
                        data_size = size
                    fp.seek(skip, os.SEEK_CUR)
            if format_tag not in PCM_FORMATS or not (
                    rate and channels and bits and data_size is not None):
                return None
            byte_rate = rate * channels * bits // 8
            if byte_rate == 0:
                return None
            d = data_size / byte_rate
            return d if 0 < d <= MAX_SANE_DURATION else None
    except (OSError, struct.error):
        return None


def get_duration(path):
    """按扩展名分发，返回时长（秒）或 None。

    struct.error 与 OSError 一样要接住：ogg 那条路径在文件被截断到几十字节时会
    在 head[o+10:o+12] 这类切片上抛 struct.error，而调用方 scan_audio_files 没有
    try——一个坏文件就会掀掉整次扫描，连同已经解析好的几千个文件的结果。wav 那条
    路径本来就接了（parse_wav_duration 的 except），两边现在对称。
    """
    ext = os.path.splitext(path)[1].lower()
    try:
        if ext == '.ogg':
            return parse_ogg_duration(path)
        if ext == '.wav':
            return parse_wav_duration(path)
    except (OSError, struct.error):
        return None
    return None


# ---------------- 扫描 ----------------
def scan_audio_files(src, recursive, ctx=None):
    """返回 (解析成功列表[(路径, 时长, 大小)], 解析失败列表[路径], 音频文件总数)。

    ctx 为 None 时不报进度也不响应取消，与命令行版行为一致。

    非递归那一支顺手带上 scandir 已经缓存的大小，省掉每个文件一次 stat；
    os.walk 不产出 DirEntry，递归那一支只能填 None 回落到 os.path.getsize。
    """
    entries = []
    if recursive:
        for root, dirs, files in os.walk(src):
            dirs[:] = [d for d in dirs if not OUT_DIR_PATTERN.fullmatch(d)]
            for name in files:
                entries.append((os.path.join(root, name), None))
    else:
        with os.scandir(src) as it:
            for e in it:
                if e.is_file():
                    try:
                        size = e.stat().st_size
                    except OSError:
                        size = None
                    entries.append((e.path, size))

    targets = [item for item in entries
               if os.path.splitext(item[0])[1].lower() in SUPPORTED_EXTS]
    audio, failed = [], []
    total = len(targets)
    for idx, (p, known_size) in enumerate(targets, 1):
        if ctx is not None:
            ctx.check_cancel()
        d = get_duration(p)
        if d is None:
            failed.append(p)
        else:
            size = known_size
            if size is None:
                try:
                    size = os.path.getsize(p)
                except OSError:
                    failed.append(p)
                    continue
            audio.append((p, d, size))
        if ctx is not None:
            ctx.progress(idx, total, '解析时长 %d/%d' % (idx, total))
    if ctx is not None:
        ctx.check_cancel()
    return audio, failed, len(audio) + len(failed)


def ensure_scan(params, ctx, report_progress=True):
    """唯一的缓存感知扫描入口，preview / run / CLI 三方共用。

    缓存挂在 ctx.session（由调用方持有的 per-tool 字典）上，不做 mtime 校验：
    与其对几千个文件 stat，不如让用户重新点扫描。只在扫完后一次性写入，被
    取消的扫描到不了这一步，因此不会留下半份缓存。
    """
    src = os.path.abspath(params.get('src') or '')
    recursive = bool(params.get('recursive'))
    key = (src, recursive)
    cached = ctx.session.get('scan')
    if cached is not None and cached[0] == key:
        return cached[1]
    data = scan_audio_files(src, recursive, ctx if report_progress else None)
    ctx.session['scan'] = (key, data)
    return data


def duration_stats(audio):
    """audio 非空时的时长概览。max/min 遇并列取先出现者，依赖扫描顺序。"""
    durs = [d for _, d, _ in audio]
    return {
        'longest': max(audio, key=lambda x: x[1]),
        'shortest': min(audio, key=lambda x: x[1]),
        'mean': statistics.mean(durs),
        'median': statistics.median(durs),
    }


# ---------------- 筛选 ----------------
def select_hits(audio, threshold, max_limit):
    """严格大于阈值、小于等于上限。降序排列，复制顺序与清单顺序都由此决定。"""
    hits = [(p, d, s) for p, d, s in audio
            if d > threshold and (max_limit is None or d <= max_limit)]
    hits.sort(key=lambda x: -x[1])
    return hits


def describe_condition(threshold, max_limit):
    if max_limit is None:
        return '时长 > %s 秒' % fmt_num(threshold)
    return '%s 秒 < 时长 <= %s 秒' % (fmt_num(threshold), fmt_num(max_limit))


def out_dir_name(src, threshold, max_limit):
    base = os.path.basename(os.path.normpath(src))
    name = '%s_大于%s秒' % (base, fmt_num(threshold))
    if max_limit is not None:
        name += '且不超%s秒' % fmt_num(max_limit)
    return name


def resolve_out_dir(src, threshold, max_limit):
    """输出目录建在源目录内，已存在则追加序号。不创建目录。"""
    name = out_dir_name(src, threshold, max_limit)
    out_dir = os.path.join(src, name)
    n = 2
    while os.path.exists(out_dir):
        out_dir = os.path.join(src, '%s_%d' % (name, n))
        n += 1
    return out_dir


# ---------------- 复制 ----------------
def unique_dest(path):
    """目标路径已存在时自动追加序号：name.ogg -> name_1.ogg"""
    if not os.path.exists(path):
        return path
    stem, ext = os.path.splitext(path)
    i = 1
    while True:
        cand = '%s_%d%s' % (stem, i, ext)
        if not os.path.exists(cand):
            return cand
        i += 1


def copy_hits(hits, out_dir, ctx):
    """返回 (成功数, 成功字节, 失败列表, 清单条目)。"""
    copied, copied_bytes, copy_failed, manifest = 0, 0, [], []
    total = len(hits)
    try:
        for idx, (p, d, size) in enumerate(hits, 1):
            ctx.check_cancel()
            dest = unique_dest(os.path.join(out_dir, os.path.basename(p)))
            try:
                shutil.copy2(p, dest)
                copied += 1
                copied_bytes += size
                manifest.append((os.path.basename(dest), d))
            except OSError as e:
                copy_failed.append((p, str(e)))
            ctx.progress(idx, total, '复制进度: %d/%d (%.1f%%)  失败: %d'
                         % (idx, total, idx / total * 100, len(copy_failed)))
        ctx.check_cancel()
    except Cancelled as c:
        c.partial = RunResult(
            summary='已复制 %d / %d 个，保留在 %s' % (copied, total, out_dir),
            output_paths=[out_dir], failures=copy_failed)
        raise
    return copied, copied_bytes, copy_failed, manifest


def write_text(path, text, ctx):
    """原子写一份清单，返回 (成功路径, 错误消息)。"""
    directory = os.path.dirname(path) or '.'
    try:
        fd, temporary = tempfile.mkstemp(prefix='.%s.' % os.path.basename(path),
                                         suffix='.tmp', dir=directory)
    except OSError as e:
        ctx.log('清单写入失败: %s (%s)' % (path, e), 'warn')
        return '', str(e)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as fp:
            fp.write(text)
        ctx.check_cancel()
        os.replace(temporary, path)
        return path, ''
    except Cancelled:
        try:
            os.remove(temporary)
        except OSError:
            pass
        raise
    except OSError as e:
        try:
            os.remove(temporary)
        except OSError:
            pass
        ctx.log('清单写入失败: %s (%s)' % (path, e), 'warn')
        return '', str(e)


def write_manifests(out_dir, src, cond_desc, hits, copied, copied_bytes,
                    manifest, scan_failed, copy_failed, ctx):
    """原子写各份清单，返回 (成功路径列表, 失败列表)。"""
    lines = [
        '复制清单',
        '源目录    : %s' % src,
        '筛选条件  : %s' % cond_desc,
        '命中文件  : %d 个' % len(hits),
        '复制成功  : %d 个' % copied,
        '复制大小  : %.1f MB' % (copied_bytes / 1024 / 1024),
        '-' * 60,
        '文件名\t时长(秒)',
    ]
    lines += ['%s\t%.2f' % (name, d) for name, d in manifest]
    specs = [(MANIFEST_NAME, '\n'.join(lines) + '\n')]
    if scan_failed:
        specs.append((SCAN_FAIL_NAME, '\n'.join(scan_failed) + '\n'))
    if copy_failed:
        specs.append((COPY_FAIL_NAME,
                      '\n'.join('%s\t%s' % item for item in copy_failed) + '\n'))

    paths, failures = [], []
    for name, text in specs:
        ctx.check_cancel()
        path = os.path.join(out_dir, name)
        written, error = write_text(path, text, ctx)
        if written:
            paths.append(written)
        else:
            failures.append((name, error))
    return paths, failures
