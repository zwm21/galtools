# -*- coding: utf-8 -*-
"""更新全库：本地库与 vndb 最新抓取结果的逐人比对。

纯逻辑，不 import 网络：抓取与读库在 tool.py 里汇合，这里只负责「谁变了、
变了什么、表格怎么摆」，整个模块都能离线测试。
"""
from dataclasses import dataclass, field

from ...core.spec import Table
from . import store
from .model import url_for

# 一条出演记录的身份键：同一部作品里的同一个角色、同一个名义。role、note、
# 发售日变了算同一条记录的修订（不列进新增/移除），但整人状态仍是 changed。
KEY_FIELDS = ('vid', 'cid', 'alias')

CHANGED, SAME, EMPTY, FAILED = 'changed', 'same', 'empty', 'failed'

STATUS_LABEL = {
    CHANGED: '有变化',
    SAME: '无变化',
    EMPTY: '0 条，保留旧文件',
    FAILED: '失败',
}


@dataclass
class Entry:
    """一个人的更新判定。fresh 为 None 表示没抓到可用结果（failed）。"""
    person: store.Person
    fresh: object = None
    status: str = SAME
    added: list = field(default_factory=list)
    removed: list = field(default_factory=list)
    error: str = ''


def person_key(person):
    """更新用的身份键：库文件名是唯一来源。"""
    return store.sid_from_path(person.path) or person.staff.sid


def credit_key(credit):
    return tuple(getattr(credit, key) for key in KEY_FIELDS)


def same_item(old, new):
    """忽略 fetched_at 的全字段一致：主名变了、credits 任一字段变了都算变。

    credits 按字段元组排序后比较：抓取排序键（发售日、标题、角色）并列时的
    先后由翻页顺序决定，不该因此把同一份数据误判成「有变化」。
    """
    if (old.staff.name, old.staff.original) != (new.staff.name, new.staff.original):
        return False
    shape = lambda credits: sorted(
        tuple(getattr(c, key) for key in store.CREDIT_FIELDS) for c in credits)
    return shape(old.credits) == shape(new.credits)


def diff_credits(old, new):
    """(新增, 移除)：按身份键做集合差，各自保持在原列表里的顺序。"""
    old_keys = {credit_key(c) for c in old}
    new_keys = {credit_key(c) for c in new}
    added = [c for c in new if credit_key(c) not in old_keys]
    removed = [c for c in old if credit_key(c) not in new_keys]
    return added, removed


def build_entries(people, fresh, errors):
    """people（名册序）× 抓取结果 -> [Entry]。fresh/errors 都以 person_key 为键。

    判定顺序固定：报错 > 没抓到 > 0 条保护 > 无变化 > 有变化。一个人既在
    fresh 里又在 errors 里不该发生，真发生了按失败处理——失败是用户要看见的。
    """
    entries = []
    for person in people:
        key = person_key(person)
        if key in errors:
            entries.append(Entry(person=person, status=FAILED,
                                 error=errors[key]))
            continue
        item = fresh.get(key)
        if item is None:
            entries.append(Entry(person=person, status=FAILED,
                                 error='没有抓取结果'))
            continue
        if not item.credits:
            entries.append(Entry(person=person, fresh=item, status=EMPTY))
            continue
        if same_item(person.item, item):
            entries.append(Entry(person=person, fresh=item, status=SAME))
            continue
        added, removed = diff_credits(person.item.credits, item.credits)
        entries.append(Entry(person=person, fresh=item, status=CHANGED,
                             added=added, removed=removed))
    return entries


def counts(entries):
    """(有变化, 无变化, 0 条保护, 失败) 各多少人，摘要与表标题共用一份数法。"""
    tally = {CHANGED: 0, SAME: 0, EMPTY: 0, FAILED: 0}
    for entry in entries:
        tally[entry.status] += 1
    return tally[CHANGED], tally[SAME], tally[EMPTY], tally[FAILED]


def summary_line(entries):
    return '有变化 %d / 无变化 %d / 0 条保护 %d / 失败 %d' % counts(entries)


def update_table(entries):
    """更新对比表：一行一个人，名字摆旧数据里的——那是用户认得的样子。"""
    rows = []
    for entry in entries:
        staff = entry.person.staff
        key = person_key(entry.person)
        head = [staff.original or staff.name, staff.name, (key, url_for(key)),
                len(entry.person.credits)]
        if entry.status == FAILED:
            rows.append(tuple(head) + ('—', '—', '—',
                                       '%s：%s' % (STATUS_LABEL[FAILED],
                                                   entry.error)))
        else:
            rows.append(tuple(head) + (len(entry.fresh.credits),
                                       len(entry.added), len(entry.removed),
                                       STATUS_LABEL[entry.status]))
    return Table(columns=('声优', '罗马字', 'ID', '现有', '最新', '新增', '移除', '状态'),
                 rows=rows, title=summary_line(entries))
