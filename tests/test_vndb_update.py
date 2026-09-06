# -*- coding: utf-8 -*-
"""更新全库的离线测试。

纯逻辑部分（update.py）不需要任何网络与假传输；集成部分复用
test_vndb_voiced 里的迷你 vndb 与 FakeApi，写法与 test_seiyuu_db 一致。
"""
import json
import os

import pytest

from galtools.core.context import RunContext
from galtools.tools import vndb_voiced as tool
from galtools.tools.vndb_voiced import api, cli, fetch, store, update
from galtools.tools.vndb_voiced.model import Credit, Staff, StaffCredits

import test_vndb_voiced as online


def make_credit(vid='v1', cid='c1', **kw):
    base = dict(vid=vid, title='Game', title_ja='ゲーム', released='2000-01-01',
                cid=cid, cast='Chara', cast_ja='キャラ', alias='A. One',
                alias_ja='', note='', role='main')
    base.update(kw)
    return Credit(**base)


def make_person(sid='s1', name='Alpha One', original='アルファ', credits=None,
                path=''):
    staff = Staff(sid=sid, name=name, original=original)
    item = StaffCredits(staff=staff, credits=list(credits or []))
    return store.Person(item=item, fetched_at='2026-01-01T00:00:00', path=path)


def make_fresh(sid='s1', name='Alpha One', original='アルファ', credits=None):
    staff = Staff(sid=sid, name=name, original=original)
    return StaffCredits(staff=staff, credits=list(credits or []))


# ---------------- update.py 纯逻辑 ----------------
def test_same_item_ignores_fetched_at_and_tie_order():
    credits = [make_credit(cid='c1'), make_credit(cid='c2')]
    old = make_person(credits=credits)
    fresh = make_fresh(credits=list(reversed(credits)))
    # 排序键（发售日/标题/角色）并列时两次抓取的先后可能反过来，同一份数据
    # 不该因此误判成有变化。
    assert update.same_item(old.item, fresh)


def test_same_item_notices_name_and_original_changes():
    old = make_person(credits=[make_credit()])
    assert not update.same_item(old.item, make_fresh(name='New Name'))
    assert not update.same_item(old.item, make_fresh(original='新名字'))


def test_same_item_notices_any_credit_field_change():
    old = make_person(credits=[make_credit()])
    for key in ('title', 'title_ja', 'released', 'cast', 'cast_ja', 'alias',
                'alias_ja', 'note', 'role'):
        fresh = make_fresh(credits=[make_credit(**{key: '变了'})])
        assert not update.same_item(old.item, fresh), key


def test_diff_credits_separates_added_removed_and_edits():
    old = [make_credit(cid='c1'), make_credit(cid='c2')]
    # c2 的 role 修订：身份键没变，不算增删；c3 新增；c1 移除。
    new = [make_credit(cid='c2', role='side'), make_credit(cid='c3')]
    added, removed = update.diff_credits(old, new)
    assert [c.cid for c in added] == ['c3']
    assert [c.cid for c in removed] == ['c1']


def test_build_entries_covers_all_four_statuses_in_roster_order():
    people = [make_person(sid='s1', credits=[make_credit()]),
              make_person(sid='s2', credits=[make_credit()]),
              make_person(sid='s3', credits=[make_credit()]),
              make_person(sid='s4', credits=[make_credit()])]
    fresh = {'s1': make_fresh(sid='s1', credits=[make_credit()]),
             's2': make_fresh(sid='s2', credits=[make_credit(role='side')]),
             's3': make_fresh(sid='s3', credits=[])}
    errors = {'s4': 'vndb 接口出错'}
    entries = update.build_entries(people, fresh, errors)
    assert [e.status for e in entries] == [update.SAME, update.CHANGED,
                                           update.EMPTY, update.FAILED]
    assert entries[1].added == [] and len(entries[1].removed) == 0
    assert entries[3].error == 'vndb 接口出错'


def test_build_entries_reports_missing_fetch_result():
    people = [make_person(sid='s1')]
    entries = update.build_entries(people, {}, {})
    assert entries[0].status == update.FAILED
    assert '没有抓取结果' in entries[0].error


def test_person_key_falls_back_to_the_file_name():
    person = make_person(sid='', path=os.path.join('lib', 's42.json'))
    assert update.person_key(person) == 's42'
    person = make_person(sid='s7', path=os.path.join('lib', 's42.json'))
    assert update.person_key(person) == 's7'


def test_update_table_lists_counts_and_status():
    people = [make_person(sid='s1', credits=[make_credit()]),
              make_person(sid='s2', credits=[make_credit()])]
    fresh = {'s1': make_fresh(sid='s1',
                              credits=[make_credit(), make_credit(cid='c2')]),
             's2': make_fresh(sid='s2', credits=[make_credit()])}
    entries = update.build_entries(people, fresh, {})
    table = update.update_table(entries)
    assert table.columns == ('声优', '罗马字', 'ID', '现有', '最新',
                             '新增', '移除', '状态')
    assert table.rows[0][3:6] == (1, 2, 1)       # 现有 1 → 最新 2，新增 1
    assert table.rows[0][7] == '有变化'
    assert table.rows[1][7] == '无变化'
    assert table.rows[0][2] == ('s1', 'https://vndb.org/s1')
    assert '有变化 1' in table.title and '无变化 1' in table.title


# ---------------- 更新全库（tool 集成，迷你 vndb） ----------------
def update_params(db_dir, **kw):
    """GUI 表单总会把每个字段都填上，更新全库模式的完整参数字典。"""
    params = {'staff': '', 'update_all': True, 'save_db': True,
              'db_dir': str(db_dir), 'export': False, 'out_dir': '',
              'refresh': False}
    params.update(kw)
    return params


def seed_library(monkeypatch, tmp_path, *sids):
    """用迷你 vndb 真抓一遍写库，fetched_at 钉在 2020 年，返回 {sid: 原始字节}。

    之后的更新重抓若写出同样内容，唯一区别就是 fetched_at——钉住它，
    「无变化不重写」才能用逐字节比较来验。
    """
    online.FakeApi(online.vndb()).install(monkeypatch)
    for sid in sids:
        staff = fetch.load_staff(sid, api.Client())
        credits = fetch.fetch_credits(staff, api.Client())
        store.write_person(str(tmp_path), StaffCredits(staff=staff,
                                                       credits=credits),
                           fetched_at='2020-01-01T00:00:00')
    return {sid: (tmp_path / f'{sid}.json').read_bytes() for sid in sids}


def test_update_all_preview_compares_with_the_library(monkeypatch, tmp_path):
    seed_library(monkeypatch, tmp_path, 's1', 's2')
    # 一个坏文件只进 warnings，不拖垮整批
    (tmp_path / 's3.json').write_text('{oops', encoding='utf-8')
    got = tool.preview(update_params(tmp_path), RunContext())
    assert got.ok
    assert '库内 2 人' in got.summary and '无变化 2' in got.summary
    assert any('s3.json' in w for w in got.warnings)
    assert got.table.title == '有变化 0 / 无变化 2 / 0 条保护 0 / 失败 0'
    assert [row[2][0] for row in got.table.rows] == ['s1', 's2']


def test_update_all_skips_files_without_changes(monkeypatch, tmp_path):
    before = seed_library(monkeypatch, tmp_path, 's1', 's2')
    result = tool.run(update_params(tmp_path), RunContext())
    assert result.failures == []
    assert '更新 0 人' in result.summary
    assert result.output_paths == [str(tmp_path)]      # 全部已是最新算成功
    for sid, data in before.items():
        assert (tmp_path / f'{sid}.json').read_bytes() == data


def test_update_all_rewrites_a_person_with_new_credits(monkeypatch, tmp_path):
    seed_library(monkeypatch, tmp_path, 's1')
    monkeypatch.setitem(online.CHAR_ROWS, 's1', online.CHAR_ROWS['s1'] + [
        {'id': 'c9', 'name': 'Chara Nine', 'original': '',
         'vns': [{'id': 'v3', 'role': 'main'}]}])
    monkeypatch.setitem(online.VN_ROWS, 'v3', {
        'id': 'v3', 'title': 'Game Three', 'alttitle': '',
        'released': '2026-01-01',
        'va': [{'staff': {'id': 's1', 'aid': 'a1'},
                'character': {'id': 'c9'}, 'note': ''}]})
    ctx = RunContext()
    got = tool.preview(update_params(tmp_path), ctx)
    assert '有变化 1' in got.summary
    assert 'Game Three' in got.summary          # 新增作品直接点名
    result = tool.run(update_params(tmp_path), ctx)
    assert '更新 1 人' in result.summary
    new = (tmp_path / 's1.json').read_text(encoding='utf-8')
    assert 'Game Three' in new and '2020-01-01' not in new
    assert len(store.read_person(str(tmp_path), 's1').credits) == 3


def test_update_all_run_reuses_the_preview_fetch(monkeypatch, tmp_path):
    seed_library(monkeypatch, tmp_path, 's1', 's2')
    fake = online.FakeApi(online.vndb()).install(monkeypatch)
    ctx = RunContext()
    tool.preview(update_params(tmp_path), ctx)
    calls = len(fake.calls)
    assert calls > 0
    tool.run(update_params(tmp_path), ctx)
    assert len(fake.calls) == calls             # run 零请求，全走缓存


def test_update_all_never_replaces_a_person_with_zero_credits(monkeypatch,
                                                              tmp_path):
    before = seed_library(monkeypatch, tmp_path, 's1')
    monkeypatch.setitem(online.CHAR_ROWS, 's1', [])
    result = tool.run(update_params(tmp_path), RunContext())
    assert (tmp_path / 's1.json').read_bytes() == before['s1']
    assert any('0 条' in reason for _, reason in result.failures)
    assert result.output_paths == []            # 什么也没写成


def test_update_all_a_failed_person_keeps_his_old_file(monkeypatch, tmp_path):
    before = seed_library(monkeypatch, tmp_path, 's1', 's2')
    online.FakeApi(online.vndb(fail_vn_for='s2')).install(monkeypatch)
    result = tool.run(update_params(tmp_path), RunContext())
    assert any('s2' in label for label, _ in result.failures)
    assert (tmp_path / 's2.json').read_bytes() == before['s2']
    assert (tmp_path / 's1.json').read_bytes() == before['s1']


def test_update_all_a_person_gone_from_vndb_keeps_his_file(monkeypatch,
                                                          tmp_path):
    seed_library(monkeypatch, tmp_path, 's1')
    ghost = StaffCredits(staff=Staff(sid='s9', name='Ghost Nine'),
                         credits=[make_credit()])
    store.write_person(str(tmp_path), ghost)
    result = tool.run(update_params(tmp_path), RunContext())
    assert any('s9' in label for label, _ in result.failures)
    assert store.read_person(str(tmp_path), 's9').staff.name == 'Ghost Nine'


def test_update_all_validate_ignores_staff_and_forbids_export(tmp_path):
    assert tool.validate(update_params('')) == [
        ('db_dir', '更新全库要填库目录')]
    errors = dict(tool.validate(update_params('Z:\\nope\\nope')))
    assert '目录不存在' in errors['db_dir']
    errors = dict(tool.validate(update_params(tmp_path, export=True)))
    assert '不导出 Excel' in errors['export']
    # staff 填了乱码、超过 8 个人都不拦：更新全库时这个字段整个被忽略
    raw = ', '.join('s%d' % i for i in range(10)) + ', !!!'
    assert tool.validate(update_params(tmp_path, staff=raw)) == []


def test_manual_run_skips_an_unchanged_person(monkeypatch, tmp_path):
    before = seed_library(monkeypatch, tmp_path, 's1')
    result = tool.run(online.args('s1', save_db=True, db_dir=str(tmp_path),
                                  export=False), RunContext())
    assert (tmp_path / 's1.json').read_bytes() == before['s1']
    assert '1 人无变化' in result.summary
    assert result.output_paths == [str(tmp_path)]


def test_manual_run_never_replaces_a_person_with_zero_credits(monkeypatch,
                                                              tmp_path):
    before = seed_library(monkeypatch, tmp_path, 's1')
    monkeypatch.setitem(online.CHAR_ROWS, 's1', [])
    result = tool.run(online.args('s1', save_db=True, db_dir=str(tmp_path),
                                  export=False), RunContext())
    assert (tmp_path / 's1.json').read_bytes() == before['s1']
    assert any('0 条' in reason for _, reason in result.failures)


# ---------------- CLI --update-all ----------------
def test_cli_update_all_requires_db_and_refuses_targets(monkeypatch, tmp_path):
    for argv in (['cli', '--update-all'],
                 ['cli', '--update-all', 's1', '--db', str(tmp_path)]):
        monkeypatch.setattr(cli.sys, 'argv', argv)
        with pytest.raises(SystemExit) as caught:
            cli.main()
        assert caught.value.code == 2


def test_cli_update_all_runs_the_update_pipeline(monkeypatch, tmp_path):
    before = seed_library(monkeypatch, tmp_path, 's1', 's2')
    online.FakeApi(online.vndb()).install(monkeypatch)
    monkeypatch.setattr(cli.sys, 'argv',
                        ['cli', '--update-all', '--db', str(tmp_path)])
    assert cli.main() == 0                        # 全部已是最新算成功
    for sid, data in before.items():
        assert (tmp_path / f'{sid}.json').read_bytes() == data
    # 库目录必须已存在，这条与 GUI 是同一份 validate
    monkeypatch.setattr(cli.sys, 'argv',
                        ['cli', '--update-all', '--db', str(tmp_path / 'nope')])
    with pytest.raises(SystemExit) as caught:
        cli.main()
    assert caught.value.code == 2
