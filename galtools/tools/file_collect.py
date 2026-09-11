# -*- coding: utf-8 -*-
"""按特征文件的后缀和文件名片段归集同类文件。"""
import os
import shutil
import tempfile

from ..core.context import Cancelled
from ..core.spec import BOOL, DIR, FILE, TEXT, Field, PreviewResult, RunResult, ToolSpec

FAILURE_MANIFEST = '复制失败.txt'


def feature_extension(path):
    """特征文件的最后一段后缀；保留原文供界面展示。"""
    return os.path.splitext(path)[1]


def name_matches(path, needle, case_sensitive):
    """固定字符串只匹配不含后缀的文件名，不解释通配符。"""
    stem = os.path.splitext(os.path.basename(path))[0]
    if not case_sensitive:
        stem, needle = stem.casefold(), needle.casefold()
    return needle in stem


def _canonical(path):
    return os.path.normcase(os.path.realpath(os.path.abspath(path)))


def _is_within(path, parent):
    try:
        return os.path.commonpath((_canonical(path), _canonical(parent))) == _canonical(parent)
    except ValueError:
        return False


def validate(params):
    errors = []
    feature = params.get('feature') or ''
    needle = params.get('name_contains') or ''
    src = params.get('src') or ''
    dst = params.get('dst') or ''
    if feature and not os.path.isfile(feature):
        errors.append(('feature', '文件不存在或不可访问'))
    ext = feature_extension(feature)
    if feature and os.path.isfile(feature) and not ext:
        errors.append(('feature', '特征文件必须有后缀'))
    if needle and feature and os.path.isfile(feature):
        if not name_matches(feature, needle, bool(params.get('case_sensitive'))):
            errors.append(('name_contains', '特征文件名不包含这段固定字符串'))
    if src and not os.path.isdir(src):
        errors.append(('src', '目录不存在或不可访问'))
    if dst and not os.path.isdir(dst):
        errors.append(('dst', '目录不存在或不可访问'))
    if src and dst and os.path.isdir(src) and os.path.isdir(dst):
        if _canonical(src) == _canonical(dst):
            errors.append(('dst', '目标目录不能与源目录相同'))
        elif _is_within(dst, src):
            errors.append(('dst', '目标目录不能位于源目录内部'))
    return errors


def scan_files(src, recursive, extension, ctx=None):
    """返回 (成功文件[(路径, 大小)], 扫描失败[(路径, 原因)])。"""
    ext_key = extension.casefold()
    paths = []
    failures = []
    if recursive:
        def failed_walk(error):
            failures.append((error.filename or src, str(error)))

        for root, dirs, files in os.walk(src, onerror=failed_walk):
            if ctx is not None:
                ctx.check_cancel()
            dirs.sort(key=str.casefold)
            files.sort(key=str.casefold)
            paths.extend(os.path.join(root, name) for name in files
                         if feature_extension(name).casefold() == ext_key)
    else:
        try:
            with os.scandir(src) as entries:
                for entry in entries:
                    if ctx is not None:
                        ctx.check_cancel()
                    try:
                        if (entry.is_file()
                                and feature_extension(entry.name).casefold() == ext_key):
                            paths.append(entry.path)
                    except OSError as error:
                        failures.append((entry.path, str(error)))
        except OSError as error:
            failures.append((error.filename or src, str(error)))
    paths.sort(key=lambda path: os.path.relpath(path, src).casefold())
    found = []
    total = len(paths)
    for index, path in enumerate(paths, 1):
        if ctx is not None:
            ctx.check_cancel()
        try:
            size = os.path.getsize(path)
        except OSError as error:
            failures.append((path, str(error)))
            continue
        found.append((path, size))
        if ctx is not None:
            ctx.progress(index, total, '扫描文件 %d/%d' % (index, total))
    if ctx is not None:
        ctx.check_cancel()
    return found, failures


def ensure_scan(params, ctx):
    src = os.path.abspath(params.get('src') or '')
    recursive = bool(params.get('recursive'))
    extension = feature_extension(params.get('feature') or '')
    key = (src, recursive, extension.casefold())
    cached = ctx.session.get('scan')
    if cached is not None and cached[0] == key:
        return cached[1]
    data = scan_files(src, recursive, extension, ctx)
    ctx.session['scan'] = (key, data)
    return data


def select_hits(files, needle, case_sensitive):
    return [(path, size) for path, size in files
            if name_matches(path, needle, case_sensitive)]


def unique_destination(path):
    """目标已存在就追加序号。单发用途：整批规划走 plan_destinations。"""
    candidate = path
    stem, ext = os.path.splitext(path)
    index = 1
    while os.path.exists(candidate):
        candidate = '%s_%d%s' % (stem, index, ext)
        index += 1
    return candidate


def plan_destinations(hits, dst):
    """整批规划目标名：彼此之间、以及与 dst 已有文件都不撞名。

    全部目标共用 dst 这一个父目录，判重因此只需比文件名，不必对每个候选做一次
    realpath——那在 Windows 上是一次开文件的系统调用。dst 里原有什么开头列一次
    就够：列完之后才出现的文件由 copy_hits 撞 FileExistsError 时重新编号兜住。

    每个基名还要记住上次落到第几号，否则 k 个重名得探 k(k+1)/2 次。这不是理论
    上的坏情况——解包目录里同一个 voice.ogg 散在上百个子目录下是常态，5000 个
    文件 / 50 个基名实测是 40 秒与 0.03 秒的差别，而 preview 每次都要算一遍。
    """
    try:
        taken = {os.path.normcase(name) for name in os.listdir(dst)}
    except OSError:
        # 列不动的目标目录同样让每个 mkstemp 失败，那些会按单文件记入 failures，
        # 与这里抛出去掀掉整轮相比是想要的那种。
        taken = set()
    next_index = {}
    planned = []
    for source, size in hits:
        base = os.path.basename(source)
        stem, ext = os.path.splitext(base)
        key = os.path.normcase(base)
        # 从上次用过的号起跳是安全的：中间那些号要么被同基名的前一个命中占了，
        # 要么当时就因为已被占用而跳过，两种情况都已经在 taken 里。
        index = next_index.get(key, 0)
        name = base
        while os.path.normcase(name) in taken:
            index += 1
            name = '%s_%d%s' % (stem, index, ext)
        next_index[key] = index
        taken.add(os.path.normcase(name))
        planned.append((source, os.path.join(dst, name), size))
    return planned


def _copy_contents(source, target, ctx):
    """分块复制内容，让大文件复制也能响应取消。"""
    with open(source, 'rb') as reader, open(target, 'wb') as writer:
        while True:
            ctx.check_cancel()
            chunk = reader.read(1024 * 1024)
            if not chunk:
                break
            writer.write(chunk)
    shutil.copystat(source, target)


def atomic_copy(source, target, ctx):
    """先复制到目标目录内的唯一临时文件，再原子且不覆盖地发布。"""
    fd, temporary = tempfile.mkstemp(prefix='.%s.' % os.path.basename(target),
                                     suffix='.tmp', dir=os.path.dirname(target))
    os.close(fd)
    try:
        _copy_contents(source, temporary, ctx)
        ctx.check_cancel()
        os.rename(temporary, target)
    except BaseException:
        try:
            os.remove(temporary)
        except OSError:
            pass
        raise


def copy_hits(planned, ctx):
    copied = []
    failures = []
    total = len(planned)
    try:
        for index, (source, planned_target, size) in enumerate(planned, 1):
            ctx.check_cancel()
            target = planned_target
            while True:
                try:
                    atomic_copy(source, target, ctx)
                    copied.append((source, target, size))
                    break
                except FileExistsError:
                    target = unique_destination(planned_target)
                except OSError as error:
                    failures.append((source, str(error)))
                    break
            ctx.progress(index, total, '复制文件 %d/%d，失败 %d'
                         % (index, total, len(failures)))
        ctx.check_cancel()
    except Cancelled as stop:
        stop.partial = RunResult(
            summary='已复制 %d / %d 个文件' % (len(copied), total),
            output_paths=[os.path.dirname(planned[0][1])] if copied else [],
            failures=failures)
        stop.copied_count = len(copied)
        raise
    return copied, failures


def write_failure_manifest(dst, failures, ctx):
    if not failures:
        return ''
    path = unique_destination(os.path.join(dst, FAILURE_MANIFEST))
    fd, temporary = tempfile.mkstemp(prefix='.%s.' % os.path.basename(path),
                                     suffix='.tmp', dir=dst)
    os.close(fd)
    try:
        with open(temporary, 'w', encoding='utf-8') as stream:
            stream.write('\n'.join('%s\t%s' % item for item in failures) + '\n')
        ctx.check_cancel()
        os.rename(temporary, path)
        return path
    except BaseException:
        try:
            os.remove(temporary)
        except OSError:
            pass
        raise


def _target_free_space(dst):
    return shutil.disk_usage(dst).free


def preview(params, ctx):
    files, scan_failures = ensure_scan(params, ctx)
    hits = select_hits(files, params['name_contains'], bool(params.get('case_sensitive')))
    extension = feature_extension(params['feature'])
    total_size = sum(size for _path, size in hits)
    lines = [
        '格式 %s：候选 %d 个' % (extension, len(files)),
        '文件名包含 %r：命中 %d 个，约 %.1f MB'
        % (params['name_contains'], len(hits), total_size / 1024 / 1024),
        '目标目录：%s' % os.path.abspath(params['dst']),
    ]
    warnings = []
    if scan_failures:
        warnings.append('%d 个文件或目录无法读取，已跳过' % len(scan_failures))
    ok = bool(hits)
    if not hits:
        lines.append('没有满足两个条件的文件。')
    else:
        free = _target_free_space(params['dst'])
        if free < total_size:
            warnings.append('目标盘剩余 %.1f MB，不够放 %.1f MB'
                            % (free / 1024 / 1024, total_size / 1024 / 1024))
            ok = False
        conflicts = sum(
            os.path.basename(source) != os.path.basename(target)
            for source, target, _size in plan_destinations(hits, params['dst']))
        if conflicts:
            lines.append('%d 个名称会自动追加序号。' % conflicts)
    return PreviewResult(summary='\n'.join(lines), warnings=warnings, ok=ok)


def _partial_result(dst, copied, total, scan_failures, copy_failures):
    return RunResult(
        summary='已复制 %d / %d 个文件；扫描跳过 %d 个；复制失败 %d 个'
                % (len(copied), total, len(scan_failures), len(copy_failures)),
        output_paths=[dst] if copied else [],
        warnings=(['扫描时有 %d 个文件或目录无法读取' % len(scan_failures)]
                  if scan_failures else []),
        failures=scan_failures + copy_failures)


def run(params, ctx):
    errors = validate(params)
    if errors:
        return RunResult(summary='参数无效：' + '；'.join(msg for _key, msg in errors))
    # 预览后的目录内容可能已经变化，执行时必须重新扫描。
    ctx.session.pop('scan', None)
    files, scan_failures = ensure_scan(params, ctx)
    hits = select_hits(files, params['name_contains'], bool(params.get('case_sensitive')))
    if not hits:
        return RunResult(
            summary='没有满足两个条件的文件，未写入任何内容。扫描跳过 %d 个。'
                    % len(scan_failures),
            warnings=(['扫描时有 %d 个文件或目录无法读取' % len(scan_failures)]
                      if scan_failures else []),
            failures=scan_failures)
    total_size = sum(size for _path, size in hits)
    if _target_free_space(params['dst']) < total_size:
        return RunResult(summary='目标盘剩余空间不足，未开始复制。')
    planned = plan_destinations(hits, params['dst'])
    try:
        copied, copy_failures = copy_hits(planned, ctx)
    except Cancelled as stop:
        partial = stop.partial
        copied_count = getattr(stop, 'copied_count', 0)
        stop.partial = RunResult(
            summary='已复制 %d / %d 个文件；扫描跳过 %d 个'
                    % (copied_count, len(hits), len(scan_failures)),
            output_paths=partial.output_paths if partial is not None else [],
            warnings=(['扫描时有 %d 个文件或目录无法读取'
                       % len(scan_failures)] if scan_failures else []),
            failures=scan_failures + (partial.failures if partial is not None else []))
        raise
    failures = scan_failures + copy_failures
    manifest = ''
    if failures:
        try:
            manifest = write_failure_manifest(params['dst'], failures, ctx)
        except Cancelled as stop:
            stop.partial = _partial_result(
                params['dst'], copied, len(hits), scan_failures, copy_failures)
            raise
        except OSError as error:
            failures.append((FAILURE_MANIFEST, str(error)))
    outputs = [params['dst']] if copied else []
    if manifest:
        outputs.append(manifest)
    return RunResult(
        summary='复制成功 %d / %d 个；扫描跳过 %d 个；复制失败 %d 个。\n目标目录：%s'
                % (len(copied), len(hits), len(scan_failures),
                   len(copy_failures), params['dst']),
        output_paths=outputs,
        warnings=(['扫描时有 %d 个文件或目录无法读取' % len(scan_failures)]
                  if scan_failures else []),
        failures=failures)


TOOL = ToolSpec(
    id='file_collect',
    name='特征文件归集',
    category='资源整理',
    description='按特征文件的后缀和文件名中的固定字符串，把同类文件复制到指定目录。',
    fields=(
        Field(key='feature', kind=FILE, label='特征文件', rescan=True,
              help='取其最后一段后缀作为格式条件。', placeholder='选择或拖入一个文件'),
        Field(key='name_contains', kind=TEXT, label='文件名包含',
              help='字面量包含匹配，不支持通配符；特征文件自身也必须匹配。'),
        Field(key='case_sensitive', kind=BOOL, label='区分大小写', default=False,
              required=False, help='只影响固定字符串，不影响后缀。'),
        Field(key='src', kind=DIR, label='源目录', rescan=True,
              placeholder='选择解包后的杂乱目录'),
        Field(key='recursive', kind=BOOL, label='包含子目录', default=True,
              required=False, rescan=True),
        Field(key='dst', kind=DIR, label='目标目录',
              help='必须已存在，不能与源目录相同或位于源目录内部。'),
    ),
    run=run,
    preview=preview,
    validate=validate,
)
