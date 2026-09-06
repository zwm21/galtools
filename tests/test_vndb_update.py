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


