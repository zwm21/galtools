# -*- coding: utf-8 -*-
"""vndb 声优出演表：抓 VNDB 上某个声优配过的全部角色，导出 Excel。

填两人以上时额外算出共同出演的作品（旧仓库里这是另一个脚本 compare_voiced_xlsx，
需要先跑两次抓取再手动喂两个 xlsx 给它；这里合成一步）。三人以上时两两及以上的
每个组合各出一页，页名是各人的罗马音，另有一张「组合」索引页。

旧版抓 HTML，本版走官方 kana JSON API：更准（角色主次、别名、日文原名都是结构化
字段，不靠 DOM 猜）也更快。三阶段抓取见 fetch.py。

preview 只做解析 + count（两三次便宜请求），真正的翻页留给 run。GUI 每改一个
非 rescan 字段就会自动重跑 preview，所以解析结果与 count 都缓存在 ctx.session 上。
"""
import os

from ...core.context import Cancelled
from ...core.spec import (
    BOOL, DIR, TEXT, Field, PreviewResult, RunResult, Table, ToolSpec,
)
from . import api, fetch, xlsx
from .model import url_for

# 预计耗时用的经验系数：实测 288 部候选作品 11.8 秒（其中 /vn 翻页占 8.3 秒）。
SECONDS_PER_VN = 0.045
SECONDS_PER_STAFF = 2.0
REFRESH_FLAG = 'refresh_done'

# 人数上限：N 人的两两及以上组合有 2^N-N-1 个，每个非空组合一张工作表。8 人最多
# 247 张，已经是 Excel 里翻不动的量；再往上只会产出没人看的文件。
MAX_TARGETS = 8
# 摘要里最多逐个列出多少个有交集的组合，其余交给工作簿里的「组合」页。
MAX_LISTED_COMBOS = 12


def _apply_refresh(params, ctx):
    """勾了「重新抓取」时丢缓存，但每次勾选只生效一次。

    preview 与 run 共用一个 session：若 run 再清一次，preview 刚抓回来的东西
    会被立刻丢掉、白抓一遍。
    """
    if not params.get('refresh'):
        ctx.session.pop(REFRESH_FLAG, None)
        return
    if not ctx.session.get(REFRESH_FLAG):
        fetch.clear_cache(ctx)
        ctx.session[REFRESH_FLAG] = True


def eta_text(total_vns, people):
    seconds = total_vns * SECONDS_PER_VN + SECONDS_PER_STAFF * people
    if seconds < 90:
        return '约 %d 秒' % max(5, int(round(seconds / 5.0)) * 5)
    return '约 %d 分钟' % max(2, int(round(seconds / 60.0)))


# ---------------- 结果表格 ----------------
def _people_table(found):
    """预览表：将要抓的人。ID 一列可双击验人——认错人是这里最容易犯的错。"""
    if not found:
        return None
    rows = [(staff.original or staff.name, staff.name,
             (staff.sid, url_for(staff.sid)), chars, vns)
            for staff, chars, vns in found]
    return Table(columns=('声优', '罗马字', 'ID', '角色数', '作品数 ≤'),
                 rows=rows, title='将要抓取')


def _candidate_table(problems):
    """名字有歧义时列出全部候选，双击 ID 就能看是谁。没有候选时返回 None。"""
    rows = [(res.target, cand.original or cand.name, cand.name,
             (cand.sid, url_for(cand.sid)))
            for res in problems for cand in res.candidates]
    if not rows:
        return None
    return Table(columns=('填的是', '候选', '罗马字', 'ID'), rows=rows,
                 title='改填其中一个 ID')


def _common_table(items, common, title=None):
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


def _credits_table(item):
    """只有一个人时没有交集可算，摆他的全部出演记录。"""
    rows = []
    for c in item.credits:
        vn_url, char_url = url_for(c.vid), url_for(c.cid)
        rows.append((c.released, (c.title_orig, vn_url),
                     (c.cast_orig, char_url),
                     c.alias_orig, c.role))
    return Table(columns=('发售日', 'Title', '角色', 'As', 'Role'), rows=rows,
                 title='%s：%d 条出演记录' % (item.staff.label(),
                                             len(item.credits)))


def combo_names(items, combo):
    """组合里各人的罗马音，摘要与索引页共用一种写法。"""
    return '、'.join(items[i].staff.name or items[i].staff.sid
                     for i in combo.members)


def unique_people(resolutions):
    """按 sid 去重，返回 (保留的解析结果, 被合并掉的个数)。

    `s367, Ono Ryouko` 是两个不同的字串、指向同一个人，parse_targets 那层按字串
    去重看不出来。不合并的话这个人会被抓两遍、出两页一模一样的明细，还多一张
    「他和他自己」的共同出演页。
    """
    out, seen = [], set()
    for res in resolutions:
        if res.staff.sid in seen:
            continue
        seen.add(res.staff.sid)
        out.append(res)
    return out, len(resolutions) - len(out)


def validate(params):
    """每敲一个键都会跑，只做纯本地判断，绝不联网。"""
    errors = []
    out_dir = params.get('out_dir')
    if out_dir and not os.path.isdir(out_dir):
        errors.append(('out_dir', '目录不存在或不可访问'))
    raw = (params.get('staff') or '').strip()
    targets = fetch.parse_targets(raw)
    if raw and not targets:
        errors.append(('staff', '没解析出任何目标'))
    if len(targets) > MAX_TARGETS:
        errors.append(('staff', '最多 %d 个人：%d 个人有 %d 个两两及以上的组合，'
                                '工作簿会大到没法看'
                                % (MAX_TARGETS, len(targets),
                                   2 ** len(targets) - len(targets) - 1)))
    for target in targets:
        kind, value = fetch.classify(target)
        if kind == 'bad':
            # 每个字段只能显示一条错误，多个坏目标就先报第一个。
            errors.append(('staff', value))
            break
    return errors


def preview(params, ctx):
    _apply_refresh(params, ctx)
    targets = fetch.parse_targets(params.get('staff'))
    if not targets:
        return PreviewResult(
            summary='填一个声优：id（s367）、网址或名字，多个用逗号分隔。', ok=False)

    client = api.Client(ctx)
    ctx.progress(0, 0, '正在查 vndb…')
    try:
        resolutions = fetch.ensure_resolved(params, ctx, client)
        done, merged = unique_people([r for r in resolutions if r.ok])
        found = [(r.staff,) + tuple(fetch.ensure_counts(r.staff, ctx, client))
                 for r in done]
    except api.ApiError as e:
        # ApiError 已经是人话，不能让它穿到 worker 那层变成一段栈。
        return PreviewResult(summary='vndb 接口出错：%s' % e, ok=False)

    problems = [r for r in resolutions if not r.ok]
    total_vns = sum(vns for _, _, vns in found)
    lines = ['%s：角色 %d，作品 ≤%d' % (staff.label(), chars, vns)
             for staff, chars, vns in found]
    for res in problems:
        lines.append(fetch.describe_problem(res))

    # 有歧义时表格让位给候选：那才是此刻要做的决定，而 describe_problem 在
    # 预览框里只放得下前几个。
    table = _candidate_table(problems) or _people_table(found)
    warnings = []
    if merged:
        warnings.append('有 %d 个目标指向同一个人，已合并。' % merged)
    if not done:
        return PreviewResult(summary='\n'.join(lines), ok=False, table=table,
                             warnings=['没有一个目标能定位到具体的人。'])
    if problems:
        warnings.append('有 %d 个目标没定位到人，先改对再开始。' % len(problems))
    lines.append('共 %d 人，预计 %s；输出 %s'
                 % (len(done), eta_text(total_vns, len(done)),
                    xlsx.workbook_name([r.staff for r in done])))
    if len(done) == 1:
        warnings.append('只有一个人，不会有共同出演页。')
    elif len(done) > 2:
        n = len(done)
        lines.append('共同出演表：%d 个组合（两两及以上），没有交集的不建表'
                     % (2 ** n - n - 1))
        if n >= 5:
            warnings.append('%d 个人最多要写 %d 张共同出演表。'
                            % (n, 2 ** n - n - 1))
    return PreviewResult(summary='\n'.join(lines), warnings=warnings,
                         ok=not problems, table=table)


def run(params, ctx):
    """命令行从不预览，所以 run 自己也要解析目标、自己也要抓。"""
    _apply_refresh(params, ctx)
    out_dir = params.get('out_dir')
    try:
        # 抓之前先把目录建出来。GUI 侧 validate 已经拦过一道，命令行的 -o 没人
        # 拦：实测 -o Z:\nope\deeper 是在打完 5 个请求之后才炸的，几分钟的抓取
        # 白费。
        os.makedirs(xlsx.target_dir(out_dir), exist_ok=True)
    except OSError as e:
        return RunResult(summary='\n输出目录不可用，没有抓取：%s' % e,
                         failures=[('输出目录', str(e))])
    try:
        resolutions = fetch.ensure_resolved(params, ctx)
    except api.ApiError as e:
        # ApiError 已经是人话；放它穿到 worker 那层会变成一段红色的 traceback。
        return RunResult(summary='\nvndb 接口出错，没有写出文件。',
                         failures=[('vndb 接口', str(e))])
    done, merged = unique_people([r for r in resolutions if r.ok])
    problems = [r for r in resolutions if not r.ok]
    for res in problems:
        ctx.log(fetch.describe_problem(res), 'warn')
    if merged:
        ctx.log('有 %d 个目标指向同一个人，已合并。' % merged, 'warn')
    unresolved = [(r.target, r.error) for r in problems]
    if not done:
        return RunResult(summary='\n没有一个目标能定位到具体的人，什么都没做。',
                         failures=unresolved)

    try:
        items, hard = fetch.ensure_credits([r.staff for r in done], ctx)
    except Cancelled as c:
        # 取消时缓存没写、文件没建，能报的只有解析阶段的问题。
        c.partial = RunResult(summary='\n已取消，没有写出文件。',
                             failures=unresolved)
        raise
    failures = unresolved + hard
    if not items:
        return RunResult(summary='\n每个人都抓取失败了，没有写出文件。',
                         failures=failures)

    groups = fetch.combos(items) if len(items) > 1 else []
    ctx.log('正在写 Excel…')
    try:
        path = xlsx.save(items, groups, out_dir)
    except OSError as e:
        # 先挡不掉的那几种：磁盘满、整条路径过了 260、目标文件正被 Excel 占着。
        # 抓回来的东西还在 ctx.session 里，换个目录再点一次「开始」不必重抓。
        return RunResult(summary='\n写 Excel 失败，没有输出文件：%s' % e,
                         failures=failures + [('写 Excel', str(e))])

    lines = ['\n========== 执行结果 ==========']
    for item in items:
        lines.append('%s : %d 部作品 / %d 个角色'
                     % (item.staff.label(), len(item.vids), len(item.credits)))
    if len(items) == 2:
        lines.append('共同出演 : %d 部' % len(groups[0].entries))
    elif len(items) > 2:
        shared = [c for c in groups if c.entries]
        lines.append('共同出演 : %d 个组合有交集（共 %d 个组合）'
                     % (len(shared), len(groups)))
        for combo in shared[:MAX_LISTED_COMBOS]:
            lines.append('  %s : %d 部'
                         % (combo_names(items, combo), len(combo.entries)))
        if len(shared) > MAX_LISTED_COMBOS:
            lines.append('  …其余 %d 个见工作簿里的「组合」页'
                         % (len(shared) - MAX_LISTED_COMBOS))
    lines += ['输出文件 : %s' % path, '=' * 30, '全部完成。']

    warnings = []
    if unresolved:
        warnings.append('%d 个目标没定位到人，已跳过。' % len(unresolved))
    if hard:
        warnings.append('%d 个人抓取失败，已跳过。' % len(hard))
    if merged:
        warnings.append('%d 个目标指向同一个人，已合并。' % merged)
    if groups:
        # 屏幕上只摆得下一张表。groups 按人数降序，优先摆全员那一档，但三人以上
        # 全员同时出演经常是空的——那就往下取第一个有交集的组合，并在标题里写清
        # 是哪一档，否则用户明明有几个组合有交集却看到一屏空白。
        shown = next((c for c in groups if c.entries), groups[0])
        title = None if len(shown.members) == len(items) else (
            '%s 共同出演 %d 部' % (combo_names(items, shown), len(shown.entries)))
        table = _common_table([items[i] for i in shown.members], shown.entries,
                              title)
    else:
        table = _credits_table(items[0])
    return RunResult(summary='\n'.join(lines), output_paths=[path],
                     warnings=warnings, failures=failures, table=table)


TOOL = ToolSpec(
    id='vndb_voiced',
    name='vndb 声优出演表',
    category='资料',
    description='抓 VNDB 上某个声优配过的全部角色，导出 Excel：一页概览 + 每人'
                '一页明细。填两人以上时额外算出共同出演的作品，三人以上按两两'
                '及以上的组合各出一页。',
    fields=(
        Field(key='staff', kind=TEXT, label='声优', rescan=True, history=True,
              help='id（s367）、声优页网址或名字，多个用逗号分隔，最多 8 个人。'
                   '名字有歧义时会列出候选，改填其中的 id 即可。',
              placeholder='s367, s131'),
        Field(key='out_dir', kind=DIR, label='输出目录',
              help='xlsx 写到这里。同名文件不覆盖，自动加序号。',
              placeholder='可把文件夹拖到这里'),
        Field(key='refresh', kind=BOOL, label='重新抓取', default=False,
              rescan=True, required=False,
              help='忽略本次会话已抓到的结果，重新打一遍 vndb 的 API。'),
    ),
    run=run,
    preview=preview,
    validate=validate,
    scan_label='查询',
)
