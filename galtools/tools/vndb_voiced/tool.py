# -*- coding: utf-8 -*-
"""工具层编排：validate / preview / run 与 TOOL 声明。包级说明见 __init__.py。"""
import os

from ...core.context import Cancelled
from ...core.spec import (
    BOOL, DIR, TEXT, Field, PreviewResult, RunResult, Table, ToolSpec,
)
from . import api, fetch, store, tables, xlsx
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


def too_many_people(count):
    """人数超上限时的那条消息，没超就是空串。

    「声优库」的离线导出受同一条约束：贵的不是抓取而是写出 2^N 张工作表，所以
    两个工具共用这一份规则，免得一边放宽了另一边还拦着。
    """
    if count <= MAX_TARGETS:
        return ''
    return ('最多 %d 个人：%d 个人有 %d 个两两及以上的组合，工作簿会大到没法看'
            % (MAX_TARGETS, count, 2 ** count - count - 1))


def validate(params):
    """每敲一个键都会跑，只做纯本地判断，绝不联网。

    两个目录都是「条件必填」，所以 Field 那边写的是 required=False：写成
    required=True 的话 ToolPage.validation_errors 会在跑到这里之前就报「必填」，
    连预览都发不出去——而开关关掉的那一个本来就不用填。
    """
    errors = []
    save_db, export = bool(params.get('save_db')), bool(params.get('export'))
    if not save_db and not export:
        # 整表级错误（key 为空串）：两个开关都关掉时抓完什么都不留，不该让用户
        # 等几分钟才发现。
        errors.append(('', '「存入本地库」和「导出 Excel」至少要有一个，'
                           '否则抓完什么都不留。'))
    for key, on, label in (('db_dir', save_db, '存入本地库'),
                           ('out_dir', export, '导出 Excel')):
        value = params.get(key)
        if value and not os.path.isdir(value):
            errors.append((key, '目录不存在或不可访问'))
        elif on and not value:
            errors.append((key, '勾了「%s」就要填这里' % label))
    raw = (params.get('staff') or '').strip()
    targets = fetch.parse_targets(raw)
    if raw and not targets:
        errors.append(('staff', '没解析出任何目标'))
    if len(targets) > MAX_TARGETS:
        errors.append(('staff', too_many_people(len(targets))))
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
    tail = '共 %d 人，预计 %s' % (len(done), eta_text(total_vns, len(done)))
    if params.get('save_db'):
        tail += '；存入 %s' % params.get('db_dir')
    if params.get('export'):
        tail += '；导出 %s' % xlsx.workbook_name([r.staff for r in done])
    lines.append(tail)
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
    """先把导出目录备好再抓，一无所获时把自己建的那个目录收回去。

    不导出时整段跳过：只存库的那一次没有 xlsx 要落地，而库目录 validate 已经
    要求它本来就存在。
    """
    if not params.get('export'):
        return _run(params, ctx)
    out_dir = params.get('out_dir')
    target = xlsx.target_dir(out_dir)
    # 抓之前先把目录建出来。GUI 侧 validate 已经拦过一道，命令行的 -o 没人拦：
    # 实测 -o Z:\nope\deeper 是在打完 5 个请求之后才炸的，几分钟的抓取白费。
    already = os.path.isdir(target)
    try:
        os.makedirs(target, exist_ok=True)
    except OSError as e:
        return RunResult(summary='\n导出目录不可用，没有抓取：%s' % e,
                         failures=[('导出目录', str(e))])
    try:
        return _run(params, ctx)
    finally:
        # 五个出口都从这里过，包括 Cancelled（它是 BaseException）。目录是我们刚
        # 建的才收回去——不然「定位不到人」「取消」这些一无所获的结局会在盘上留个
        # 空壳。写出了工作簿的那次不必另加判断：rmdir 本身就拒绝删非空目录。
        # 只收叶子那一级：深路径 -o a\b\c 建了三层时 a\b 会留下，而 removedirs
        # 会往上爬进用户本来就有的空目录，不能用。
        if not already:
            try:
                os.rmdir(target)
            except OSError:
                pass


def _save_to_db(db_dir, ctx, items):
    """把抓到的人写进本地库，返回 (写成功的 sid, 失败项)。

    一个人写失败只报他自己：库是一人一个文件，没有「写了一半」的中间状态。
    """
    saved, failures = [], []
    for item in items:
        try:
            store.write_person(db_dir, item)
        except (store.BadFile, OSError) as e:
            ctx.log('%s 没写进本地库：%s' % (item.staff.label(), e), 'warn')
            failures.append((item.staff.label(), str(e)))
        else:
            saved.append(item.staff.sid)
    return saved, failures


def _run(params, ctx):
    """命令行从不预览，所以 run 自己也要解析目标、自己也要抓。"""
    _apply_refresh(params, ctx)
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
    warnings = []
    if unresolved:
        warnings.append('%d 个目标没定位到人，已跳过。' % len(unresolved))
    if hard:
        warnings.append('%d 个人抓取失败，已跳过。' % len(hard))
    if merged:
        warnings.append('%d 个目标指向同一个人，已合并。' % merged)

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
                         % (tables.combo_names(items, combo),
                            len(combo.entries)))
        if len(shared) > MAX_LISTED_COMBOS:
            lines.append('  …其余 %d 个见工作簿里的「组合」页'
                         % (len(shared) - MAX_LISTED_COMBOS))

    outputs = []
    # 先写库再写 xlsx：写 xlsx 会因为盘满、路径过 260、文件正被 Excel 占着而失败，
    # 而抓一次要十几秒到几分钟，那几分钟不该连库都没进。
    if params.get('save_db'):
        db_dir = params.get('db_dir')
        ctx.log('正在写本地库…')
        saved, refused = _save_to_db(db_dir, ctx, items)
        failures += refused
        if refused:
            warnings.append('%d 个人没写进本地库。' % len(refused))
        if saved:
            outputs.append(db_dir)
        lines.append('本地库 : %d/%d 人 -> %s' % (len(saved), len(items), db_dir))
    if params.get('export'):
        ctx.log('正在写 Excel…')
        try:
            path = xlsx.save(items, groups, params.get('out_dir'), ctx=ctx)
        except OSError as e:
            # 先挡不掉的那几种：磁盘满、整条路径过了 260、目标文件正被 Excel 占着。
            # 抓回来的东西还在 ctx.session 里，换个目录再点一次「开始」不必重抓。
            lines.append('写 Excel 失败，没有导出文件：%s' % e)
            return RunResult(summary='\n'.join(lines), output_paths=outputs,
                             warnings=warnings,
                             failures=failures + [('写 Excel', str(e))],
                             table=tables.result_table(items, groups))
        # _open_outputs 只打开第一个存在的路径，xlsx 要排在库目录前面。
        outputs.insert(0, path)
        lines.append('输出文件 : %s' % path)
    lines += ['=' * 30, '全部完成。']
    return RunResult(summary='\n'.join(lines), output_paths=outputs,
                     warnings=warnings, failures=failures,
                     table=tables.result_table(items, groups))


TOOL = ToolSpec(
    id='vndb_voiced',
    name='vndb 声优出演表',
    category='资料',
    description='抓 VNDB 上某个声优配过的全部角色，默认存进本地库（一人一个 json，'
                '「声优库」工具读的就是它），也可以直接导出 Excel：一页概览 + 每人'
                '一页明细。填两人以上时额外算出共同出演的作品，三人以上按两两'
                '及以上的组合各出一页。',
    fields=(
        Field(key='staff', kind=TEXT, label='声优', rescan=True, history=True,
              help='id（s367）、声优页网址或名字，多个用逗号分隔，最多 8 个人。'
                   '名字有歧义时会列出候选，改填其中的 id 即可。',
              placeholder='s367, s131'),
        Field(key='save_db', kind=BOOL, label='存入本地库', default=True,
              required=False,
              help='一人一个 json 写进库目录，同一个人再抓一次就整份换掉。'),
        Field(key='db_dir', kind=DIR, label='库目录', required=False,
              help='本地声优库的位置，与「声优库」工具填同一个目录。要先存在。',
              placeholder='勾了「存入本地库」就要填'),
        Field(key='export', kind=BOOL, label='导出 Excel', default=False,
              required=False,
              help='另外写一份 xlsx。默认关着：进了库的人用「声优库」随时能离线'
                   '导出，不必为了一份表格重抓。'),
        Field(key='out_dir', kind=DIR, label='导出目录', required=False,
              help='xlsx 写到这里。同名文件不覆盖，自动加序号。',
              placeholder='勾了「导出 Excel」就要填'),
        Field(key='refresh', kind=BOOL, label='重新抓取', default=False,
              rescan=True, required=False,
              help='忽略本次会话已抓到的结果，重新打一遍 vndb 的 API。'),
    ),
    run=run,
    preview=preview,
    validate=validate,
    scan_label='查询',
)
