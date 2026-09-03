# -*- coding: utf-8 -*-
"""纯数据结构与展示用的小工具函数。

不 import 网络、不 import openpyxl：抓取层与写盘层都依赖它，两边都要能在
没有对方的情况下被测试。
"""
from dataclasses import dataclass, field

SITE = 'https://vndb.org/'

# vndb 的 role 取值，按重要性排列，用于产出里的 Role 列。
ROLES = ('main', 'primary', 'side', 'appears')


def url_for(vndb_id):
    """'v17' -> 'https://vndb.org/v17'。空 id 返回 None（写盘时表示不加超链接）。"""
    return SITE + vndb_id if vndb_id else None


def label(name, original, sid=''):
    """人名的统一展示写法：`小野 涼子 (Ono Ryouko) s367`。

    日文名缺失（原名本就是拉丁字母）时退化成 `Name sid`，不留空括号。
    """
    text = '%s (%s)' % (original, name) if original and original != name else name
    return ('%s %s' % (text, sid)).strip()


@dataclass(frozen=True)
class Candidate:
    """名字搜索命中的一个人。

    name/original 存的是这个 id 的**主名**，不是搜索命中的那行别名——命中行的
    名字可能与用户敲的字串毫无关系（实测搜 'Ono Ryouko' 会命中 s252 的某个
    别名行），拿命中行去消歧会让人选错人。
    """
    sid: str
    name: str
    original: str = ''

    def label(self):
        return label(self.name, self.original, self.sid)


@dataclass(frozen=True)
class Staff:
    """一个确定的声优：主名 + 全部别名（aid -> (罗马字, 日文)）。

    别名表是 As 列的唯一来源：vndb 的 va 记录挂的是 aid 而不是 sid。
    """
    sid: str
    name: str
    original: str = ''
    aliases: dict = field(default_factory=dict)

    def label(self):
        return label(self.name, self.original, self.sid)

    def alias(self, aid):
        return self.aliases.get(aid, ('', ''))


@dataclass
class Credit:
    """一条「某人在某作品里配了某角色」的记录，即产出里的一行。"""
    vid: str
    title: str = ''
    title_ja: str = ''
    released: str = ''
    cid: str = ''
    cast: str = ''
    cast_ja: str = ''
    alias: str = ''
    alias_ja: str = ''
    note: str = ''
    role: str = ''

    # 「原名」三列（产出里的 Title1 / Cast1 / As1）的取值。原名本就是拉丁字母时
    # 接口不给 alttitle / original，直接落格会得到一列空白，所以退回罗马字。
    @property
    def title_orig(self):
        return self.title_ja or self.title

    @property
    def cast_orig(self):
        return self.cast_ja or self.cast

    @property
    def alias_orig(self):
        return self.alias_ja or self.alias


@dataclass
class StaffCredits:
    staff: Staff
    credits: list = field(default_factory=list)

    @property
    def vids(self):
        return {c.vid for c in self.credits}


@dataclass
class Resolution:
    """一个用户输入的目标的解析结果：要么确定到一个人，要么给出原因与候选。"""
    target: str
    staff: Staff = None
    error: str = ''
    candidates: list = field(default_factory=list)

    @property
    def ok(self):
        return self.staff is not None


@dataclass
class Common:
    """共同出演页的一行。casts 与声优列表同序，每项是 [(角色名, 链接)]。"""
    vid: str
    title: str = ''
    title_ja: str = ''
    released: str = ''
    casts: list = field(default_factory=list)

    @property
    def title_orig(self):
        return self.title_ja or self.title


@dataclass
class Combo:
    """一个组合的共同出演结果。

    members 是 items 的下标元组（升序），entries 是这些人共同出演的作品。三人
    以上时两两组合也各算一份，所以一部三人都在的作品会同时出现在三人那份与三个
    两两那份里——「这两人都出演」的自然定义如此。
    """
    members: tuple
    entries: list = field(default_factory=list)


def released_sort_key(released):
    """产出的排序键。

    旧版 compare 脚本只认完整 ISO 日期，`1995` 这种年份粒度的值一律折成
    `'9999'` 排到最后——那是既有 bug 而非既有约定（API 确实会返回年份粒度的
    值，老作品因此被排到了 TBA 后面）。这里补齐成 `1995-00-00` 参与排序，
    真正未定档（released 为 null）的才排最后。
    """
    text = (released or '').strip()
    parts = text.split('-')
    if not parts[0].isdigit():
        return '9999-99-99'
    out = [parts[0].rjust(4, '0')]
    for i in (1, 2):
        piece = parts[i] if len(parts) > i and parts[i].isdigit() else '00'
        out.append(piece.rjust(2, '0'))
    return '-'.join(out)
