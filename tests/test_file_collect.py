# -*- coding: utf-8 -*-
import os

import pytest

from galtools.core.context import Cancelled, RunContext
from galtools.tools import file_collect as tool


def params(feature, src, dst, **overrides):
    values = {
        'feature': str(feature),
        'name_contains': 'voice',
        'case_sensitive': False,
        'src': str(src),
        'recursive': True,
        'dst': str(dst),
    }
    values.update(overrides)
    return values


def make_dirs(tmp_path):
    src = tmp_path / 'src'
    dst = tmp_path / 'dst'
    src.mkdir()
    dst.mkdir()
    return src, dst


def test_validate_requires_real_feature_with_extension_and_matching_name(tmp_path):
    src, dst = make_dirs(tmp_path)
    missing = tmp_path / 'voice.ogg'
    errors = tool.validate(params(missing, src, dst))
    assert ('feature', '文件不存在或不可访问') in errors

    no_ext = tmp_path / 'voice'
    no_ext.write_bytes(b'x')
    assert ('feature', '特征文件必须有后缀') in tool.validate(
        params(no_ext, src, dst))

    feature = tmp_path / 'sample.ogg'
    feature.write_bytes(b'x')
    assert ('name_contains', '特征文件名不包含这段固定字符串') in tool.validate(
        params(feature, src, dst))


def test_name_match_is_literal_on_stem_and_optional_case_sensitive(tmp_path):
    path = tmp_path / 'VOICE_[01].ogg'
    assert tool.name_matches(str(path), '[01]', True)
    assert tool.name_matches(str(path), 'voice', False)
    assert not tool.name_matches(str(path), 'voice', True)
    assert not tool.name_matches(str(path), '.ogg', False)


def test_validate_rejects_empty_required_fields(tmp_path):
    """必填字段空着必须是错误，而不是一路往下跑到默认值上。

    GUI 在调 validate 之前就拦掉了这些，所以这条守的是绕开 GUI 的调用方：
    src='' 会被 abspath 变成当前工作目录，name_contains='' 匹配所有同后缀文件。
    """
    src, dst = make_dirs(tmp_path)
    feature = tmp_path / 'voice.ogg'
    feature.write_bytes(b'x')
    assert tool.validate(params(feature, src, dst)) == []

    for key, label in (('feature', '特征文件'), ('name_contains', '文件名包含'),
                       ('src', '源目录'), ('dst', '目标目录')):
        errors = tool.validate({**params(feature, src, dst), key: ''})
        assert (key, '必填：%s' % label) in errors

    # 两个可选开关取 False 不是「空着」，不能被当成没填
    assert tool.validate(params(feature, src, dst, case_sensitive=False,
                                recursive=False)) == []


def test_run_refuses_to_start_on_empty_required_fields(tmp_path):
    """run 自己也要拦。参数无效那句话丢掉了字段名，所以消息里得自带。"""
    src, dst = make_dirs(tmp_path)
    feature = tmp_path / 'voice.ogg'
    feature.write_bytes(b'x')
    (src / 'voice_a.ogg').write_bytes(b'a')
    result = tool.run(params(feature, src, dst, name_contains=''), RunContext())
    assert result.summary == '参数无效：必填：文件名包含'
    assert list(dst.iterdir()) == []


def test_validate_rejects_same_and_nested_target(tmp_path):
    src = tmp_path / 'src'
    src.mkdir()
    feature = tmp_path / 'voice.ogg'
    feature.write_bytes(b'x')
    assert ('dst', '目标目录不能与源目录相同') in tool.validate(
        params(feature, src, src))
    nested = src / 'collected'
    nested.mkdir()
    assert ('dst', '目标目录不能位于源目录内部') in tool.validate(
        params(feature, src, nested))


def test_scan_filters_extension_case_insensitively_and_recurses(tmp_path):
    src, _dst = make_dirs(tmp_path)
    (src / 'voice_a.OGG').write_bytes(b'a')
    (src / 'other.txt').write_bytes(b'x')
    sub = src / 'sub'
    sub.mkdir()
    (sub / 'voice_b.ogg').write_bytes(b'bb')

    top, top_failed = tool.scan_files(str(src), False, '.ogg')
    recursive, recursive_failed = tool.scan_files(str(src), True, '.OGG')
    assert top_failed == recursive_failed == []
    assert [os.path.basename(path) for path, _size in top] == ['voice_a.OGG']
    assert [os.path.relpath(path, src) for path, _size in recursive] == [
        'sub' + os.sep + 'voice_b.ogg', 'voice_a.OGG']


def test_scan_records_size_failures(tmp_path, monkeypatch):
    src, _dst = make_dirs(tmp_path)
    broken = src / 'voice_broken.ogg'
    broken.write_bytes(b'x')
    real = tool.os.path.getsize

    def fail_size(path):
        if path == str(broken):
            raise OSError('gone')
        return real(path)

    monkeypatch.setattr(tool.os.path, 'getsize', fail_size)
    files, failures = tool.scan_files(str(src), False, '.ogg')
    assert files == []
    assert failures == [(str(broken), 'gone')]


def test_ensure_scan_cache_depends_on_source_recursion_and_extension(tmp_path,
                                                                     monkeypatch):
    src, dst = make_dirs(tmp_path)
    feature = tmp_path / 'voice.ogg'
    feature.write_bytes(b'x')
    calls = []
    real = tool.scan_files

    def counting(*args, **kwargs):
        calls.append(args[:3])
        return real(*args, **kwargs)

    monkeypatch.setattr(tool, 'scan_files', counting)
    ctx = RunContext()
    values = params(feature, src, dst)
    first = tool.ensure_scan(values, ctx)
    assert tool.ensure_scan(values, ctx) is first
    assert len(calls) == 1
    tool.ensure_scan({**values, 'recursive': False}, ctx)
    assert len(calls) == 2
    other = tmp_path / 'voice.wav'
    other.write_bytes(b'x')
    tool.ensure_scan({**values, 'feature': str(other)}, ctx)
    assert len(calls) == 3


def test_preview_requires_hits_and_checks_target_disk(tmp_path, monkeypatch):
    src, dst = make_dirs(tmp_path)
    feature = tmp_path / 'voice.ogg'
    feature.write_bytes(b'x')
    (src / 'voice_a.ogg').write_bytes(b'1234')
    values = params(feature, src, dst)
    result = tool.preview(values, RunContext())
    assert result.ok and '命中 1 个' in result.summary
    (dst / 'voice_a.ogg').write_bytes(b'old')
    result = tool.preview(values, RunContext())
    assert '1 个名称会自动追加序号' in result.summary

    monkeypatch.setattr(tool, '_target_free_space', lambda _path: 0)
    result = tool.preview(values, RunContext())
    assert not result.ok and result.warnings

    result = tool.preview({**values, 'name_contains': 'missing'}, RunContext())
    assert not result.ok and '没有满足两个条件' in result.summary


def test_plan_destinations_is_stable_and_never_overwrites(tmp_path):
    dst = tmp_path / 'dst'
    dst.mkdir()
    (dst / 'voice.ogg').write_bytes(b'old')
    hits = [(str(tmp_path / 'a' / 'voice.ogg'), 1),
            (str(tmp_path / 'b' / 'voice.ogg'), 2)]
    planned = tool.plan_destinations(hits, str(dst))
    assert [os.path.basename(target) for _source, target, _size in planned] == [
        'voice_1.ogg', 'voice_2.ogg']


def test_plan_destinations_skips_numbers_claimed_by_other_hits(tmp_path):
    """同基名从上次的号起跳，但撞上别的命中已占的号时仍要继续让路。"""
    dst = tmp_path / 'dst'
    dst.mkdir()
    hits = [(str(tmp_path / 'a' / 'voice.ogg'), 1),
            (str(tmp_path / 'b' / 'voice_1.ogg'), 1),
            (str(tmp_path / 'c' / 'voice.ogg'), 1),
            (str(tmp_path / 'd' / 'voice.ogg'), 1)]
    planned = tool.plan_destinations(hits, str(dst))
    assert [os.path.basename(target) for _source, target, _size in planned] == [
        'voice.ogg', 'voice_1.ogg', 'voice_2.ogg', 'voice_3.ogg']


def test_plan_destinations_only_lists_the_target_once(tmp_path, monkeypatch):
    """规划整批只列一次目标目录，不对每个候选名做一次系统调用。

    退回「逐个候选 exists + realpath」的写法时重名一多就是平方级：5000 个文件 /
    50 个基名实测 40 秒对 0.03 秒，而这段规划每次预览都要重跑一遍。
    """
    dst = tmp_path / 'dst'
    dst.mkdir()
    (dst / 'voice.ogg').write_bytes(b'old')
    listed = []
    real_listdir = tool.os.listdir

    def counting_listdir(path):
        listed.append(path)
        return real_listdir(path)

    def forbidden(path, *_args, **_kwargs):
        raise AssertionError('不该逐个探测文件系统：%s' % path)

    monkeypatch.setattr(tool.os, 'listdir', counting_listdir)
    monkeypatch.setattr(tool.os.path, 'exists', forbidden)
    monkeypatch.setattr(tool.os.path, 'realpath', forbidden)

    hits = [(str(tmp_path / ('d%d' % i) / 'voice.ogg'), 1) for i in range(400)]
    planned = tool.plan_destinations(hits, str(dst))
    assert listed == [str(dst)]
    assert [os.path.basename(target) for _source, target, _size in planned] == [
        'voice_%d.ogg' % i for i in range(1, 401)]


def test_plan_destinations_survives_an_unlistable_target(tmp_path, monkeypatch):
    """列不动目标目录也照旧规划：失败按单文件记在 copy_hits，不掀掉整轮。"""
    dst = tmp_path / 'dst'
    dst.mkdir()

    def denied(_path):
        raise OSError('denied')

    monkeypatch.setattr(tool.os, 'listdir', denied)
    source = str(tmp_path / 'a' / 'voice.ogg')
    assert tool.plan_destinations([(source, 1)], str(dst)) == [
        (source, str(dst / 'voice.ogg'), 1)]


def test_atomic_copy_cleans_temporary_and_preserves_existing_target(tmp_path,
                                                                    monkeypatch):
    src = tmp_path / 'source.ogg'
    target = tmp_path / 'target.ogg'
    src.write_bytes(b'new')

    def fail_replace(_source, _target):
        raise OSError('locked')

    monkeypatch.setattr(tool.os, 'rename', fail_replace)
    with pytest.raises(OSError, match='locked'):
        tool.atomic_copy(str(src), str(target), RunContext())
    assert not target.exists()
    assert sorted(path.name for path in tmp_path.iterdir()) == ['source.ogg']


def test_atomic_copy_never_overwrites_existing_target(tmp_path):
    src = tmp_path / 'source.ogg'
    target = tmp_path / 'target.ogg'
    src.write_bytes(b'new')
    target.write_bytes(b'old')
    with pytest.raises(FileExistsError):
        tool.atomic_copy(str(src), str(target), RunContext())
    assert target.read_bytes() == b'old'
    assert sorted(path.name for path in tmp_path.iterdir()) == [
        'source.ogg', 'target.ogg']


def test_atomic_copy_refuses_target_created_during_copy(tmp_path, monkeypatch):
    src = tmp_path / 'source.ogg'
    target = tmp_path / 'target.ogg'
    src.write_bytes(b'new')
    real_rename = tool.os.rename

    def competing_rename(temporary, destination):
        target.write_bytes(b'other process')
        return real_rename(temporary, destination)

    monkeypatch.setattr(tool.os, 'rename', competing_rename)
    with pytest.raises(OSError):
        tool.atomic_copy(str(src), str(target), RunContext())
    assert target.read_bytes() == b'other process'
    assert sorted(path.name for path in tmp_path.iterdir()) == [
        'source.ogg', 'target.ogg']


def test_copy_renumbers_if_target_appears_after_planning(tmp_path):
    src = tmp_path / 'voice.ogg'
    dst = tmp_path / 'dst'
    dst.mkdir()
    src.write_bytes(b'new')
    planned = [(str(src), str(dst / 'voice.ogg'), 3)]
    (dst / 'voice.ogg').write_bytes(b'other process')
    copied, failures = tool.copy_hits(planned, RunContext())
    assert failures == []
    assert copied[0][1] == str(dst / 'voice_1.ogg')
    assert (dst / 'voice.ogg').read_bytes() == b'other process'
    assert (dst / 'voice_1.ogg').read_bytes() == b'new'


def test_atomic_copy_preserves_readonly_source_without_leaving_temp(tmp_path):
    src = tmp_path / 'readonly.ogg'
    target = tmp_path / 'target.ogg'
    src.write_bytes(b'data')
    src.chmod(0o444)
    try:
        tool.atomic_copy(str(src), str(target), RunContext())
        assert target.read_bytes() == b'data'
        assert sorted(path.name for path in tmp_path.iterdir()) == [
            'readonly.ogg', 'target.ogg']
    finally:
        src.chmod(0o666)
        if target.exists():
            target.chmod(0o666)


def test_failure_manifest_never_overwrites_existing_file(tmp_path):
    dst = tmp_path / 'dst'
    dst.mkdir()
    existing = dst / tool.FAILURE_MANIFEST
    existing.write_text('old', encoding='utf-8')
    path = tool.write_failure_manifest(
        str(dst), [('broken.ogg', 'denied')], RunContext())
    assert existing.read_text(encoding='utf-8') == 'old'
    assert os.path.basename(path) == '复制失败_1.txt'
    assert 'broken.ogg' in open(path, encoding='utf-8').read()


def test_large_copy_checks_cancel_between_chunks(tmp_path):
    src = tmp_path / 'large.ogg'
    target = tmp_path / 'target.ogg'
    src.write_bytes(b'x' * (2 * 1024 * 1024 + 1))

    class CancelMidCopy(RunContext):
        def __init__(self):
            super().__init__()
            self.calls = 0

        def check_cancel(self):
            self.calls += 1
            if self.calls == 3:
                raise Cancelled()

    with pytest.raises(Cancelled):
        tool.atomic_copy(str(src), str(target), CancelMidCopy())
    assert not target.exists()
    assert [path.name for path in tmp_path.iterdir()] == ['large.ogg']


def test_copy_continues_after_failure_and_reports_partial_cancel(tmp_path,
                                                                 monkeypatch):
    dst = tmp_path / 'dst'
    dst.mkdir()
    sources = []
    for index in range(3):
        source = tmp_path / ('voice_%d.ogg' % index)
        source.write_bytes(bytes([index]))
        sources.append(source)
    planned = [(str(source), str(dst / source.name), 1) for source in sources]
    real = tool.atomic_copy

    def one_failure(source, target, ctx):
        if source.endswith('1.ogg'):
            raise OSError('denied')
        return real(source, target, ctx)

    monkeypatch.setattr(tool, 'atomic_copy', one_failure)
    copied, failures = tool.copy_hits(planned, RunContext())
    assert len(copied) == 2
    assert len(failures) == 1 and failures[0][0].endswith('1.ogg')

    class CancelOnThird(RunContext):
        def __init__(self):
            super().__init__()
            self.calls = 0

        def check_cancel(self):
            self.calls += 1
            if self.calls == 5:
                raise Cancelled()

    monkeypatch.setattr(tool, 'atomic_copy', real)
    cancel_dst = tmp_path / 'cancelled'
    cancel_dst.mkdir()
    cancel_plan = [(str(source), str(cancel_dst / source.name), 1)
                   for source in sources]
    with pytest.raises(Cancelled) as caught:
        tool.copy_hits(cancel_plan, CancelOnThird())
    assert caught.value.partial.summary == '已复制 1 / 3 个文件'
    assert [path.name for path in cancel_dst.iterdir()] == ['voice_0.ogg']


def test_run_rescans_after_preview_cache(tmp_path):
    src, dst = make_dirs(tmp_path)
    feature = tmp_path / 'voice.ogg'
    feature.write_bytes(b'x')
    ctx = RunContext()
    values = params(feature, src, dst)
    tool.ensure_scan(values, ctx)
    (src / 'voice_added.ogg').write_bytes(b'new')
    result = tool.run(values, ctx)
    assert '复制成功 1 / 1' in result.summary
    assert (dst / 'voice_added.ogg').read_bytes() == b'new'


def test_run_copies_hits_numbers_conflicts_and_writes_failure_manifest(
        tmp_path, monkeypatch):
    src, dst = make_dirs(tmp_path)
    feature = tmp_path / 'voice.ogg'
    feature.write_bytes(b'x')
    (src / 'voice_a.ogg').write_bytes(b'a')
    (src / 'VOICE_b.OGG').write_bytes(b'bb')
    (src / 'other.ogg').write_bytes(b'no')
    (dst / 'voice_a.ogg').write_bytes(b'old')
    values = params(feature, src, dst)

    real = tool.atomic_copy

    def fail_second(source, target, ctx):
        if source.endswith('VOICE_b.OGG'):
            raise OSError('blocked')
        return real(source, target, ctx)

    monkeypatch.setattr(tool, 'atomic_copy', fail_second)
    result = tool.run(values, RunContext())
    assert (dst / 'voice_a.ogg').read_bytes() == b'old'
    assert (dst / 'voice_a_1.ogg').read_bytes() == b'a'
    assert len(result.failures) == 1
    assert (dst / tool.FAILURE_MANIFEST).is_file()
    assert result.output_paths[0] == str(dst)


def test_run_reports_scan_failures_even_without_hits(tmp_path, monkeypatch):
    src, dst = make_dirs(tmp_path)
    feature = tmp_path / 'voice.ogg'
    feature.write_bytes(b'x')
    monkeypatch.setattr(
        tool, 'ensure_scan',
        lambda _params, _ctx: ([], [('broken.ogg', 'gone')]))
    result = tool.run(params(feature, src, dst), RunContext())
    assert result.failures == [('broken.ogg', 'gone')]
    assert result.warnings == ['扫描时有 1 个文件或目录无法读取']
    assert '扫描跳过 1 个' in result.summary


def test_cancel_while_writing_manifest_keeps_complete_partial(tmp_path, monkeypatch):
    src, dst = make_dirs(tmp_path)
    feature = tmp_path / 'voice.ogg'
    feature.write_bytes(b'x')
    (src / 'voice_a.ogg').write_bytes(b'a')
    monkeypatch.setattr(
        tool, 'ensure_scan',
        lambda _params, _ctx: ([(str(src / 'voice_a.ogg'), 1)],
                              [('broken.ogg', 'gone')]))

    def cancel_manifest(_dst, _failures, _ctx):
        raise Cancelled()

    monkeypatch.setattr(tool, 'write_failure_manifest', cancel_manifest)
    with pytest.raises(Cancelled) as caught:
        tool.run(params(feature, src, dst), RunContext())
    partial = caught.value.partial
    assert '已复制 1 / 1' in partial.summary
    assert partial.failures == [('broken.ogg', 'gone')]
    assert partial.output_paths == [str(dst)]


def test_run_with_no_hits_or_space_does_not_write(tmp_path, monkeypatch):
    src, dst = make_dirs(tmp_path)
    feature = tmp_path / 'voice.ogg'
    feature.write_bytes(b'x')
    values = params(feature, src, dst)
    result = tool.run(values, RunContext())
    assert result.output_paths == []
    assert list(dst.iterdir()) == []

    (src / 'voice_a.ogg').write_bytes(b'content')
    monkeypatch.setattr(tool, '_target_free_space', lambda _path: 0)
    result = tool.run(values, RunContext())
    assert '空间不足' in result.summary
    assert list(dst.iterdir()) == []
