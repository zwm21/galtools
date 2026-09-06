# -*- coding: utf-8 -*-
"""声优库：离线翻本地库，并把翻到的那一份导出成 xlsx。

库是「vndb 声优出演表」写出来的一堆 json（一人一个，格式见 vndb_voiced/store.py）。
本工具从不联网，也从不改库里的文件——唯一的写操作是导出。

两个筛选框决定屏幕上摆什么：

    都空          名册：库里每人一行，列与工作簿的「概览」页同一组
    只填关键词    跨库搜索：全库逐条记录过一遍关键词，按人聚合
    只填人        一个人时摆他的明细，两人以上时离线算共同出演
    两个都填      先按人筛，再按关键词筛

没有 rescan 字段，因此改任何一格都会自动重跑预览。读库本身很便宜（真库实测
54 人 / 6.6 MB / 1.7 万条记录，读 + 解析 95 毫秒，且预览跑在后台线程里），
比起缓存住再操心「预览之后库又被抓取工具改过了」，每次重读更简单也更准。

不提供命令行：这个工具的产出就是屏幕上那张表，而导出用的是抓取工具已有的
`--db` 那条路。
"""
import os
from dataclasses import dataclass, field

from ..core.spec import DIR, TEXT, Field, PreviewResult, RunResult, ToolSpec
from .vndb_voiced import (
    MAX_LISTED_COMBOS, fetch, store, tables, too_many_people, xlsx,
)
from .vndb_voiced.model import StaffCredits

ROSTER, SEARCH, PEOPLE = 'roster', 'search', 'people'

# 搜索看哪些字段。日文与罗马字两版都看，所以一个词无论用哪种写法都找得到。
# 刻意不含 role 与 released：'main' 会命中半个库，日期用不着模糊匹配。
SEARCH_FIELDS = ('title', 'title_ja', 'cast', 'cast_ja', 'alias', 'alias_ja',
                 'note')

# 名字对不上时最多列几个候选，其余交给名册。
MAX_LISTED_CANDIDATES = 4
# 搜索结果摘要里最多逐个列几个人，其余交给下面那张表。
MAX_LISTED_PEOPLE = 12
# 关键词进文件名时截多长：整条导出路径过 260 会被一堆 Windows API 拒绝。
MAX_KEYWORD_STEM = 40


# ---------------- 查库 ----------------
def _names(person):
    """一个人用于比较的两种写法（罗马字、日文），都已归一化。"""
    staff = person.staff
    return (fetch.normalize(staff.name), fetch.normalize(staff.original))


def find_people(people, who):
    """把「只看这些人」里的每个词对到库里的人，返回 (选中的 Person, 问题)。

    三档匹配，只在前一档一无所获时才降级：sid → 名字精确相等 → 名字含这个词。
    含这个词的那一档命中多个是常态（真实库里 `ryouko` 同时命中小野 涼子与
    田中 涼子），此时不猜：猜错了导出的就是另一个人的表，而这种错在表格里
    看不出来。
    """
    by_sid = {fetch.normalize(p.staff.sid): p for p in people}
    out, seen, problems = [], set(), []
    for token in fetch.parse_targets(who):
        want = fetch.normalize(token)
        if want in by_sid:
            matches = [by_sid[want]]
        else:
            # `want in _names(p)` 是与两种写法之一精确相等；下一档才是子串。
            matches = ([p for p in people if want in _names(p)]
                       or [p for p in people
                           if any(want in n for n in _names(p))])
        if not matches:
            problems.append((token, '库里没有这个人'))
        elif len(matches) > 1:
            shown = '、'.join(p.staff.label()
                             for p in matches[:MAX_LISTED_CANDIDATES])
            if len(matches) > MAX_LISTED_CANDIDATES:
                shown += ' 等 %d 个' % len(matches)
            problems.append((token, '对上了 %d 个人（%s），改填 id 或写全'
                                    % (len(matches), shown)))
        elif matches[0].staff.sid not in seen:
            seen.add(matches[0].staff.sid)
            out.append(matches[0])
    return out, problems


def matches_keyword(credit, want):
    """一条记录是否含这个词。want 要先 fetch.normalize 过。"""
    return any(want in fetch.normalize(getattr(credit, key))
               for key in SEARCH_FIELDS)


def filter_credits(items, keyword):
    """按关键词过滤每个人的记录，返回新的 StaffCredits。

    不改传进来的那些：调用方手里的 people 还要用来摆名册、数库里到底有多少人。
    """
    want = fetch.normalize(keyword)
    if not want:
        return list(items)
    return [StaffCredits(staff=item.staff,
                         credits=[c for c in item.credits
                                  if matches_keyword(c, want)])
            for item in items]


def _mode(selected, problems, keyword):
    """三种视图取哪种。

    填了人却一个都没对上时退回名册，而不是拿关键词去搜全库：用户要的人不在库里，
    此刻该让他看见库里到底有谁。这种情况 ok=False，本来也导不出去。
    """
    if selected:
        return PEOPLE
    if problems or not keyword.strip():
        return ROSTER
    return SEARCH


@dataclass
class View:
    """屏幕上（以及导出的工作簿里）那一份东西。

    preview 与 run 各自 build 一次，看到的必须是同一份，所以这里只装读库和筛选
    的结果，不装任何与「谁在调用」有关的东西。
    """
    mode: str = ROSTER
    people: list = field(default_factory=list)     # 库里全部人，[store.Person]
    items: list = field(default_factory=list)      # 要摆/要导的 StaffCredits
    groups: list = field(default_factory=list)     # 共同出演组合
    problems: list = field(default_factory=list)   # [(填的是, 原因)]
    bad_files: list = field(default_factory=list)  # [(文件名, 原因)]
    keyword: str = ''


def build_view(params):
    """读库 + 应用两个筛选框。纯本地，读不了库时抛 store.BadFile。"""
    people, bad_files = store.read_all(params.get('db_dir') or '')
    who = params.get('who') or ''
    keyword = params.get('keyword') or ''
    selected, problems = find_people(people, who) if who.strip() else ([], [])
    mode = _mode(selected, problems, keyword)
    if mode == PEOPLE:
        items = filter_credits([p.item for p in selected], keyword)
    elif mode == SEARCH:
        # 一条都没命中的人不摆：搜索结果里几十行「0 条」没有信息量。选人模式下
        # 相反，用户点名要看的人即使 0 条也得在，否则他会以为是自己填错了。
        items = [i for i in filter_credits([p.item for p in people], keyword)
                 if i.credits]
    else:
        items = [p.item for p in people]
    groups = fetch.combos(items) if mode == PEOPLE and len(items) > 1 else []
    return View(mode=mode, people=people, items=items, groups=groups,
                problems=problems, bad_files=bad_files,
                keyword=keyword.strip())


# ---------------- 摆出来 ----------------
def workbook_name(view):
    """导出的文件名。

    选人导出沿用抓取工具的 workbook_name：同样几个人，离线导出与在线抓取该落到
    同一个文件名上。名册与搜索没有对应的在线产出，各自一种写法。
    """
    if view.keyword:
        return '声优库_搜索_%s.xlsx' % xlsx.slug(view.keyword)[:MAX_KEYWORD_STEM]
    if view.mode == PEOPLE:
        return xlsx.workbook_name([i.staff for i in view.items])
    return '声优库_%d人.xlsx' % len(view.items)


def view_table(view):
    """一个视图对应一张表。屏幕上只摆得下一张。"""
    if view.mode == ROSTER:
        return tables.roster_table(view.people)
    if view.mode == SEARCH:
        return tables.hits_table(view.items)
    if len(view.items) == 1:
        item = view.items[0]
        title = ('%s：命中 %d 条' % (item.staff.label(), len(item.credits))
                 if view.keyword else None)
        return tables.credits_table(item, title)
    return tables.result_table(view.items, view.groups)


def _combo_lines(view):
    """共同出演那几行摘要。两人时一行，三人以上逐个组合列，超了就收尾。"""
    if view.mode != PEOPLE or len(view.items) < 2:
        return []
    if len(view.items) == 2:
        return ['共同出演 : %d 部' % len(view.groups[0].entries)]
    shared = [c for c in view.groups if c.entries]
    lines = ['共同出演 : %d 个组合有交集（共 %d 个组合）'
             % (len(shared), len(view.groups))]
    for combo in shared[:MAX_LISTED_COMBOS]:
        lines.append('  %s : %d 部' % (tables.combo_names(view.items, combo),
                                       len(combo.entries)))
    if len(shared) > MAX_LISTED_COMBOS:
        lines.append('  …其余 %d 个组合也有交集' % (len(shared)
                                                 - MAX_LISTED_COMBOS))
    return lines


def describe(view):
    """视图的文字摘要，preview 与 run 共用。"""
    if view.mode == ROSTER:
        vids = {c.vid for p in view.people for c in p.credits}
        credits = sum(len(p.credits) for p in view.people)
        return ['库里 %d 人，%d 部作品，%d 条出演记录'
                % (len(view.people), len(vids), credits)]
    if view.mode == SEARCH:
        hits = sum(len(i.credits) for i in view.items)
        lines = ['「%s」命中 %d 条，%d 个人（库里共 %d 人）'
                 % (view.keyword, hits, len(view.items), len(view.people))]
        for item in view.items[:MAX_LISTED_PEOPLE]:
            lines.append('  %s : %d 条' % (item.staff.label(),
                                           len(item.credits)))
        if len(view.items) > MAX_LISTED_PEOPLE:
            lines.append('  …其余 %d 人见下表'
                         % (len(view.items) - MAX_LISTED_PEOPLE))
        return lines
    lines = []
    for item in view.items:
        lines.append('%s : %s%d 部作品 / %d 个角色'
                     % (item.staff.label(), '命中 ' if view.keyword else '',
                        len(item.vids), len(item.credits)))
    return lines + _combo_lines(view)


def _problem_lines(view):
    return ['%s：%s' % p for p in view.problems]


def _warnings(view):
    out = []
    if view.bad_files:
        out.append('%d 个文件读不出来，已跳过：%s'
                   % (len(view.bad_files),
                      '、'.join(name for name, _ in view.bad_files[:3])))
    if view.problems:
        out.append('%d 个名字没对上库里的人，先改对再导出。' % len(view.problems))
    return out


# ---------------- 入口 ----------------
def validate(params):
    """每敲一个键都会跑。只看路径与人数，读库是 preview 的活。

    「导出目录」空着不算错：这个工具的主要用法是在屏幕上翻库，为了看一眼就得先
    填一个用不上的目录太怪。何况 validate 报错会连预览一起拦掉，那就连翻都翻不了
    ——「没填导出目录」由 preview 用 ok=False 把「开始」置灰。
    """
    errors = []
    for key in ('db_dir', 'out_dir'):
        value = params.get(key)
        if value and not os.path.isdir(value):
            errors.append((key, '目录不存在或不可访问'))
    message = too_many_people(len(fetch.parse_targets(params.get('who'))))
    if message:
        errors.append(('who', message))
    return errors


def preview(params, ctx):
    try:
        view = build_view(params)
    except store.BadFile as e:
        return PreviewResult(summary=str(e), ok=False)
    if not view.people:
        return PreviewResult(
            summary='这个目录里没有 s<id>.json。库是「vndb 声优出演表」勾上'
                    '「存入本地库」写出来的。', ok=False,
            warnings=_warnings(view))
    # 没对上的名字排在最前：它是此刻要做的决定，而下面的表已经退回成名册了。
    lines = _problem_lines(view) + describe(view)
    out_dir = (params.get('out_dir') or '').strip()
    if view.problems:
        lines.append('先把上面的名字改对，再导出。')
    elif not view.items:
        lines.append('没有能导出的东西。')
    elif out_dir:
        lines.append('导出 : %s -> %s' % (workbook_name(view), out_dir))
    else:
        lines.append('只看不导：要导出就填「导出目录」。')
    return PreviewResult(summary='\n'.join(lines), warnings=_warnings(view),
                         ok=bool(out_dir and view.items and not view.problems),
                         table=view_table(view))


def run(params, ctx):
    """把当前视图导成 xlsx。这个工具只有这一种写操作。"""
    out_dir = (params.get('out_dir') or '').strip()
    if not out_dir:
        # 预览已经把「开始」置灰了。真走到这里说明调用方不是 GUI，宁可什么都不做
        # 也不能拿进程的当前目录当默认值。
        return RunResult(summary='\n没填导出目录，什么都没做。',
                         failures=[('导出目录', '没填')])
    try:
        view = build_view(params)
    except store.BadFile as e:
        return RunResult(summary='\n%s' % e, failures=[('库目录', str(e))])
    if view.problems:
        return RunResult(summary='\n有名字没对上库里的人，没有导出：\n'
                                 + '\n'.join(_problem_lines(view)),
                         failures=view.problems)
    if not view.items:
        return RunResult(summary='\n没有要导出的东西。',
                         warnings=_warnings(view), table=view_table(view))

    lines = ['\n========== 执行结果 =========='] + describe(view)
    path = xlsx.unique_path(os.path.join(xlsx.target_dir(out_dir),
                                         workbook_name(view)))
    ctx.log('正在写 Excel…')
    ctx.progress(0, 0, '正在写 Excel…')
    try:
        xlsx.build(view.items, view.groups, path)
    except OSError as e:
        lines.append('写 Excel 失败，没有导出文件：%s' % e)
        return RunResult(summary='\n'.join(lines), warnings=_warnings(view),
                         failures=view.bad_files + [('写 Excel', str(e))],
                         table=view_table(view))
    lines += ['输出文件 : %s' % path, '=' * 30, '全部完成。']
    return RunResult(summary='\n'.join(lines), output_paths=[path],
                     warnings=_warnings(view), failures=view.bad_files,
                     table=view_table(view))


TOOL = ToolSpec(
    id='seiyuu_db',
    name='声优库',
    category='资料',
    description='离线翻「vndb 声优出演表」存下来的本地库：看名册与单人明细、'
                '不联网算共同出演、按关键词跨全库搜作品名与角色名，'
                '并把看到的这一份导出成 Excel。',
    fields=(
        Field(key='db_dir', kind=DIR, label='库目录',
              help='「vndb 声优出演表」存进去的那个目录，一人一个 s<id>.json。'
                   '本工具只读它。'),
        Field(key='who', kind=TEXT, label='只看这些人', required=False,
              history=True,
              help='id（s367）、罗马字或日文名，多个用逗号分隔。两人以上时算'
                   '共同出演，最多 8 个人。空着就是全库。',
              placeholder='空着 = 全库名册'),
        Field(key='keyword', kind=TEXT, label='关键词', required=False,
              history=True,
              help='在作品名、角色名、As 与 note 里找，日文与罗马字两版都找，'
                   '忽略大小写与空格。',
              placeholder='空着 = 不筛'),
        Field(key='out_dir', kind=DIR, label='导出目录', required=False,
              help='点「开始」把屏幕上这一份写成 xlsx，同名不覆盖。'
                   '只看不导可以空着。'),
    ),
    run=run,
    preview=preview,
    validate=validate,
)
