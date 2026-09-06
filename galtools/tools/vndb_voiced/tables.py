# -*- coding: utf-8 -*-
"""铺在预览框下方的结果表格，抓取工具与「声优库」查看器共用。

同一份数据从网上抓回来还是从本地库里读出来，摆在屏幕上都该是同一张表；两边
各写一份的话，只有一边会跟着改。

不 import openpyxl（xlsx 那边一律在函数内 import），也不碰网络。
"""
from ...core.spec import Table
from . import xlsx
from .model import ROLES, role_counts, url_for


def combo_names(items, combo):
    """组合里各人的罗马音，摘要、索引页与表标题共用一种写法。"""
    return '、'.join(items[i].staff.name or items[i].staff.sid
                     for i in combo.members)


def common_table(items, common, title=None):
    """共同出演：一行一部作品，每人一列各自配的角色。"""
    rows = []
    for entry in common:
        vn_url = url_for(entry.vid)
        row = [entry.released, (entry.title, vn_url), (entry.title_orig, vn_url)]
        for casts in entry.casts:
            # 一个人在同一部里配多个角色时，链接指向谁都不对，索性不加。
            link = casts[0][1] if len(casts) == 1 else None
            row.append((' / '.join(t for t, _ in casts), link))
        rows.append(tuple(row))
    return Table(columns=('发售日', 'Title', '日文原名')
                         + tuple(xlsx.cast_columns(items)),
                 rows=rows, title=title or '共同出演 %d 部' % len(common))


def credits_table(item, title=None):
    """只有一个人时没有交集可算，摆他的全部出演记录。"""
    rows = []
    for c in item.credits:
        vn_url, char_url = url_for(c.vid), url_for(c.cid)
        rows.append((c.released, (c.title_orig, vn_url),
                     (c.cast_orig, char_url),
                     c.alias_orig, c.role))
    return Table(columns=('发售日', 'Title', '角色', 'As', 'Role'), rows=rows,
                 title=title or '%s：%d 条出演记录' % (item.staff.label(),
                                                     len(item.credits)))


def result_table(items, groups):
    """抓完/导完之后摆哪张表。屏幕上只摆得下一张。

    groups 按人数降序，优先摆全员那一档，但三人以上全员同时出演经常是空的——那就
    往下取第一个有交集的组合，并在标题里写清是哪一档，否则用户明明有几个组合有
    交集却看到一屏空白。
    """
    if not groups:
        return credits_table(items[0])
    shown = next((c for c in groups if c.entries), groups[0])
    title = None if len(shown.members) == len(items) else (
        '%s 共同出演 %d 部' % (combo_names(items, shown), len(shown.entries)))
    return common_table([items[i] for i in shown.members], shown.entries, title)


def roster_table(people, title=None):
    """名册：一行一个人，列与工作簿的「概览」页同序，末尾多一列抓取时间。

    people 是 store.Person，比 StaffCredits 多知道文件是什么时候抓的——名册的
    用处之一就是看谁的数据旧了该重抓。
    """
    rows = []
    for person in people:
        staff = person.staff
        rows.append((staff.original or staff.name, staff.name,
                     (staff.sid, url_for(staff.sid)),
                     len(person.item.vids), len(person.credits))
                    + tuple(role_counts(person.credits))
                    + (person.fetched_at,))
    return Table(columns=('声优', '罗马字', 'ID', '作品数', '角色数')
                         + ROLES + ('未标注', '抓取时间'),
                 rows=rows, title=title or '库里共 %d 人' % len(people))


def hits_table(items, title=None):
    """搜索命中：一行一条出演记录，第一列是这条记录属于谁。

    跨人搜索的结果摆不进 credits_table——那张表的每一行都默认属于同一个人。
    """
    rows = []
    for item in items:
        who = item.staff.original or item.staff.name or item.staff.sid
        for c in item.credits:
            vn_url, char_url = url_for(c.vid), url_for(c.cid)
            rows.append((who, c.released, (c.title_orig, vn_url),
                         (c.cast_orig, char_url), c.alias_orig, c.role))
    return Table(columns=('声优', '发售日', 'Title', '角色', 'As', 'Role'),
                 rows=rows,
                 title=title or '命中 %d 条（%d 人）' % (len(rows), len(items)))
