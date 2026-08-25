# -*- coding: utf-8 -*-
"""语音时长筛选：按时长区间把语音复制到新目录并生成清单。

原命令行版是五步 input() 向导，其中第 3–5 步是「报命中数 → 确认 → 选否则
回退重设阈值」的循环。GUI 里这个循环由 preview 取代：改阈值立刻看到命中几
个、多少 MB、磁盘够不够，确认步骤自然消失。向导本身保留在 cli.py。
"""
import os
import shutil

from ...core.spec import BOOL, DIR, NUMBER, Field, PreviewResult, RunResult, ToolSpec
from . import core


def validate(params):
    errors = []
    src = params.get('src')
    if src and not os.path.isdir(src):
        errors.append(('src', '目录不存在或不可访问'))
    threshold = params.get('threshold')
    if threshold is not None and threshold <= 0:
        errors.append(('threshold', '必须为正数'))
    max_limit = params.get('max_limit')
    if max_limit is not None:
        if max_limit <= 0:
            errors.append(('max_limit', '必须为正数'))
        elif threshold is not None and max_limit <= threshold:
            errors.append(('max_limit', '必须大于筛选阈值'))
    return errors


def preview(params, ctx):
    audio, failed, total_audio = core.ensure_scan(params, ctx)
    threshold = params.get('threshold')
    max_limit = params.get('max_limit')
    cond = core.describe_condition(threshold, max_limit)

    lines = ['音频 %d / 解析成功 %d / 失败 %d'
             % (total_audio, len(audio), len(failed))]
    warnings = []
    if failed:
        warnings.append('%d 个文件解析失败，不计入命中（超过 %s 秒的也算失败）'
                        % (len(failed), core.fmt_num(core.MAX_SANE_DURATION)))
    if not audio:
        return PreviewResult(summary='\n'.join(lines + ['没有可解析的音频文件。']),
                             warnings=warnings, ok=False)

    stats = core.duration_stats(audio)
    lines.append('最长 %.2fs  最短 %.2fs  中位 %.2fs'
                 % (stats['longest'][1], stats['shortest'][1], stats['median']))

    hits = core.select_hits(audio, threshold, max_limit)
    total_size = sum(s for _, _, s in hits)
    lines.append('%s → 命中 %d / %d (%.1f%%)  约 %.1f MB'
                 % (cond, len(hits), len(audio), len(hits) / len(audio) * 100,
                    total_size / 1024 / 1024))

    src = os.path.abspath(params.get('src') or '')
    ok = bool(hits)
    if hits:
        lines.append('输出目录: %s' % core.out_dir_name(src, threshold, max_limit))
        free = shutil.disk_usage(src).free
        if free < total_size:
            warnings.append('源盘剩余 %.1f MB，不够放 %.1f MB，无法复制'
                            % (free / 1024 / 1024, total_size / 1024 / 1024))
            ok = False
    else:
        lines.append('没有满足条件的文件。')
    return PreviewResult(summary='\n'.join(lines), warnings=warnings, ok=ok)


def run(params, ctx):
    # 用户完全可能填好参数直接点开始、从没触发过预览，所以 run 自己也要扫。
    audio, scan_failed, _total = core.ensure_scan(params, ctx)
    src = os.path.abspath(params.get('src') or '')
    threshold = params.get('threshold')
    max_limit = params.get('max_limit')
    cond_desc = core.describe_condition(threshold, max_limit)
    hits = core.select_hits(audio, threshold, max_limit)
    if not hits:
        return RunResult(
            summary='\n没有满足条件（%s）的文件，程序结束。' % cond_desc)

    total_size = sum(s for _, _, s in hits)
    free = shutil.disk_usage(src).free
    ctx.log('\n'.join([
        '\n========== 筛选预览 ==========',
        '筛选条件       : %s' % cond_desc,
        '命中文件       : %d / %d (%.1f%%)'
        % (len(hits), len(audio), len(hits) / len(audio) * 100),
        '预计复制大小   : %.1f MB' % (total_size / 1024 / 1024),
        '源盘剩余空间   : %.1f MB' % (free / 1024 / 1024),
    ]))
    if free < total_size:
        return RunResult(
            summary='\n[!] 磁盘剩余空间不足，已中止复制。请清理空间后重试。')

    out_dir = core.resolve_out_dir(src, threshold, max_limit)
    os.makedirs(out_dir)
    ctx.log('\n输出目录       : %s' % out_dir)
    ctx.log('开始复制...\n')
    try:
        copied, copy_failed, manifest = core.copy_hits(hits, out_dir, ctx)
    finally:
        # 输出目录就建在源目录内，这次之后的扫描结果本就该不同。
        ctx.session.pop('scan', None)
    ctx.log('复制完成，正在生成清单...')

    manifest_path = core.write_manifests(
        out_dir, src, cond_desc, hits, copied, manifest, scan_failed,
        copy_failed, ctx)

    lines = ['\n========== 执行结果 ==========', '复制成功 : %d 个' % copied]
    if copy_failed:
        lines.append('复制失败 : %d 个（详见 %s）'
                     % (len(copy_failed), core.COPY_FAIL_NAME))
    lines += ['输出目录 : %s' % out_dir,
              '复制清单 : %s' % manifest_path,
              '=' * 30,
              '全部完成。']
    warnings = []
    if scan_failed:
        warnings.append('%d 个文件解析失败，已写入 %s'
                        % (len(scan_failed), core.SCAN_FAIL_NAME))
    return RunResult(summary='\n'.join(lines),
                     output_paths=[out_dir, manifest_path],
                     warnings=warnings,
                     failures=copy_failed)


TOOL = ToolSpec(
    id='audio_filter',
    name='语音时长筛选',
    category='音频',
    description='按时长区间把语音复制到源目录下的新文件夹，并生成复制清单。'
                '支持 %s。' % '、'.join(sorted(core.SUPPORTED_EXTS)),
    fields=(
        Field(key='src', kind=DIR, label='语音目录', rescan=True,
              help='解包出的语音目录。输出目录会建在它里面。',
              placeholder='可把文件夹拖到这里'),
        Field(key='recursive', kind=BOOL, label='包含子目录', default=False,
              rescan=True, required=False,
              help='默认只扫顶层。递归时会跳过本工具以往的输出目录。'),
        Field(key='threshold', kind=NUMBER, label='时长阈值(秒)',
              default=core.DEFAULT_THRESHOLD,
              help='时长【大于】该值的文件才会被复制。'),
        Field(key='max_limit', kind=NUMBER, label='时长上限(秒)', required=False,
              help='时长【不超过】该值的文件才会被复制。',
              placeholder='留空 = 不限制'),
    ),
    run=run,
    preview=preview,
    validate=validate,
)
