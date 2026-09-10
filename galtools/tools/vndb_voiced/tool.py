# -*- coding: utf-8 -*-
"""工具层编排：validate / preview / run 与 TOOL 声明。包级说明见 __init__.py。"""
import os

from ...core.context import Cancelled
from ...core.spec import (
    BOOL, DIR, TEXT, Field, PreviewResult, RunResult, Table, ToolSpec,
)
from . import api, fetch, store, tables, update, xlsx
from .model import url_for

# 预计耗时用的经验系数：实测 288 部候选作品 11.8 秒（其中 /vn 翻页占 8.3 秒）。
SECONDS_PER_VN = 0.045
SECONDS_PER_STAFF = 2.0
REFRESH_FLAG = 'refresh_done'
# 更新全库的 staff 列表缓存键，与 fetch.CACHE_KEYS 并列挂在 ctx.session 上：
# preview 确认过谁还在 vndb 上，run 不该把几十次 /staff 请求再打一遍。
UPDATE_STAFFS_KEY = 'update_staffs'

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
        ctx.session.pop(UPDATE_STAFFS_KEY, None)
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


def _validate_update(params):
    """更新全库只看库目录：staff 被忽略，Excel 不导出。"""
    errors = []
    db_dir = params.get('db_dir')
    if db_dir and not os.path.isdir(db_dir):
        errors.append(('db_dir', '目录不存在或不可访问'))
    elif not db_dir:
        errors.append(('db_dir', '更新全库要填库目录'))
    if params.get('export'):
        errors.append(('export', '更新全库时不导出 Excel，请关掉'))
    return errors


def validate(params):
    """每敲一个键都会跑，只做纯本地判断，绝不联网。

    两个目录都是「条件必填」，所以 Field 那边写的是 required=False：写成
    required=True 的话 ToolPage.validation_errors 会在跑到这里之前就报「必填」，
    连预览都发不出去——而开关关掉的那一个本来就不用填。
    """
    if params.get('update_all'):
        return _validate_update(params)
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
    if not raw:
        errors.append(('staff', '要填一个声优目标'))
    elif not targets:
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


# ---------------- 更新全库 ----------------
def _update_batch(params, ctx):
    """读库 → 逐人抓最新 → 与本地比对，返回 (entries, broken, fatal)。

    preview 与 run 共用这一条管线：抓取走 ensure_credits 的会话缓存，run 命中
    缓存时零请求。fatal 非空表示库目录本身读不了，entries 为 None。
    """
    ctx.log('更新全库：正在读取本地库…')
    try:
        people, broken = store.read_all(params.get('db_dir'))
    except store.BadFile as e:
        ctx.log('本地库读取失败：%s' % e, 'warn')
        return None, [], str(e)
    file_count = len(people) + len(broken)
    if not people:
        ctx.log('本地库读取完成：人物文件 %d 个，有效 0 人，坏文件 %d 个；'
                '没有可更新的人。' % (file_count, len(broken)))
        return [], broken, ''
    ctx.log('本地库读取完成：人物文件 %d 个，有效 %d 人，坏文件 %d 个；'
            '接下来确认 %d 人的 VNDB 主页。'
            % (file_count, len(people), len(broken), len(people)))

    client = api.Client(ctx)
    key = tuple(update.person_key(person) for person in people)
    cached = ctx.session.get(UPDATE_STAFFS_KEY)
    if cached is not None and cached[0] == key:
        staffs, errors = cached[1], {}
        ctx.log('VNDB 主页确认：复用本次查询结果，共 %d 人；不重复请求。'
                % len(staffs))
    else:
        staffs, errors = [], {}
        total = len(people)
        ctx.log('开始确认 VNDB 主页：共 %d 人。' % total)
        for current, person in enumerate(people, 1):
            # list_files 只认 s<id>.json，person_key 在这里总能拿到合法 sid：
            # 文件内容里的 sid 坏了也能从文件名救回来，写完顺手把它治愈。
            sid = key[current - 1]
            ctx.progress(current, total, '正在确认 VNDB 主页 %d/%d：%s'
                         % (current, total, sid))
            try:
                staff = fetch.load_staff(sid, client)
            except api.ApiError as e:
                errors[sid] = str(e)
                continue
            if staff is None:
                errors[sid] = 'vndb 上已经没有 %s 这个人' % sid
                continue
            staffs.append(staff)
        ctx.log('VNDB 主页确认完成：成功 %d 人，失败 %d 人；'
                '接下来处理 %d 人的出演记录。'
                % (len(staffs), len(errors), len(staffs)))
        if not errors:
            # 与 ensure_credits 同一口径：抖一次网造成的残缺名单不缓存，
            # 否则用户再点一次「查询」拿回的仍是同一份残缺。
            ctx.check_cancel()
            ctx.session[UPDATE_STAFFS_KEY] = (key, staffs)
    credit_key = tuple(staff.sid for staff in staffs)
    credit_cache = ctx.session.get('credits')
    credits_cached = credit_cache is not None and credit_cache[0] == credit_key
    if not staffs:
        ctx.log('没有通过 VNDB 主页确认的人，跳过出演记录处理；'
                '接下来与本地库比对。')
    elif credits_cached:
        ctx.log('出演记录：复用本次查询结果，共 %d 人；不重复抓取；'
                '接下来与本地库比对。' % len(credit_cache[1][0]))
    else:
        ctx.log('开始处理出演记录：共 %d 人。' % len(staffs))
    items, hard = fetch.ensure_credits(staffs, ctx, client)
    if staffs and not credits_cached:
        ctx.log('出演记录处理完成：成功 %d 人，失败 %d 人；'
                '接下来与本地库比对。' % (len(items), len(hard)))
    by_label = {staff.label(): staff.sid for staff in staffs}
    for lbl, reason in hard:
        errors[by_label.get(lbl, lbl)] = reason
    fresh = {item.staff.sid: item for item in items}
    entries = update.build_entries(people, fresh, errors)
    ctx.log('本地比对完成：%s。' % update.summary_line(entries))
    return entries, broken, ''


def _preview_update(params, ctx):
    ctx.progress(0, 0, '正在读本地库…')
    try:
        entries, broken, fatal = _update_batch(params, ctx)
    except api.ApiError as e:
        # ApiError 已经是人话，不能让它穿到 worker 那层变成一段栈。
        return PreviewResult(summary='vndb 接口出错：%s' % e, ok=False)
    if fatal:
        return PreviewResult(summary='本地库读不出来：%s' % fatal, ok=False)
    if not entries:
        return PreviewResult(
            summary='库里还没有人：先在上面的「声优」里填人抓进库，再来更新。',
            ok=False)
    lines = ['库内 %d 人，%s。' % (len(entries), update.summary_line(entries))]
    for entry in entries:
        if entry.status != update.CHANGED:
            continue
        titles = []
        for credit in entry.added:
            if credit.title not in titles:
                titles.append(credit.title)
        note = ''
        if titles:
            note = '，新增：%s' % '、'.join(titles[:5])
            if len(titles) > 5:
                note += ' 等'
        lines.append('%s：%d → %d（+%d/-%d）%s' % (
            entry.person.staff.label(), len(entry.person.credits),
            len(entry.fresh.credits), len(entry.added), len(entry.removed),
            note))
    warnings = []
    if broken:
        warnings.append('%d 个库文件读不出来，已跳过：%s'
                        % (len(broken),
                           '、'.join(name for name, _ in broken[:3])))
    failed = update.counts(entries)[3]
    if failed:
        warnings.append('%d 个人抓取失败，点「开始」也不会更新他们。' % failed)
    return PreviewResult(summary='\n'.join(lines), warnings=warnings,
                         table=update.update_table(entries))


def _run_update(params, ctx):
    """命令行从不预览，所以 run 自己也要走完整管线（有缓存时零请求）。"""
    _apply_refresh(params, ctx)
    try:
        entries, broken, fatal = _update_batch(params, ctx)
    except api.ApiError as e:
        return RunResult(summary='\nvndb 接口出错，没有写出文件。',
                         failures=[('vndb 接口', str(e))])
    if fatal:
        return RunResult(summary='\n本地库读不出来，没有写出文件。',
                         failures=[('库目录', fatal)])
    if not entries:
        return RunResult(summary='\n库里还没有人，什么都没做。',
                         failures=[('库目录', '库里还没有人')])

    db_dir = params.get('db_dir')
    lines = ['\n========== 更新结果 ==========']
    failures, written = [], []
    changed = update.counts(entries)[0]
    if changed:
        ctx.log('开始写回本地库：待写 %d 人。' % changed)
    else:
        ctx.log('写回本地库：有变化 0 人，无需写入。')
    write_index = 0
    for entry in entries:
        label = entry.person.staff.label() or update.person_key(entry.person)
        if entry.status == update.FAILED:
            failures.append((label, entry.error))
        elif entry.status == update.EMPTY:
            failures.append((label, 'vndb 上抓到 0 条，保留旧文件'))
        elif entry.status == update.CHANGED:
            try:
                ctx.check_cancel()
                write_index += 1
                ctx.progress(write_index, changed,
                             '正在写回本地库 %d/%d：%s'
                             % (write_index, changed, label))
                store.write_person(db_dir, entry.fresh)
            except (store.BadFile, OSError) as e:
                ctx.log('%s 没写进本地库：%s' % (label, e), 'warn')
                failures.append((label, str(e)))
            else:
                written.append(entry)
                ctx.log('已写入本地库：%s' % label)
                lines.append('%s : %d → %d（+%d/-%d）' % (
                    label, len(entry.person.credits), len(entry.fresh.credits),
                    len(entry.added), len(entry.removed)))
    if changed:
        ctx.log('本地库写回完成：成功 %d 人，失败 %d 人。'
                % (len(written), changed - len(written)))
    lines.append('更新 %d 人 / %s' % (len(written), update.summary_line(entries)))
    warnings = []
    if broken:
        warnings.append('%d 个库文件读不出来，已跳过。' % len(broken))
    lines += ['=' * 30, '全部完成。']
    # 全都没写且有失败项时不给输出路径：命令行据此退出非零，脚本才能感知
    # 「这一趟白跑了」。全部无变化（已是最新）算成功，路径照给。
    outputs = [db_dir] if written or not failures else []
    return RunResult(summary='\n'.join(lines), output_paths=outputs,
                     warnings=warnings, failures=failures,
                     table=update.update_table(entries))


def preview(params, ctx):
    _apply_refresh(params, ctx)
    if params.get('update_all'):
        return _preview_update(params, ctx)
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
    要求它本来就存在。更新全库模式没有 xlsx 这回事，走自己的管线。
    """
    if params.get('update_all'):
        return _run_update(params, ctx)
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
    """把抓到的人写进本地库，返回 (写成功的 sid, 无变化跳过的 sid, 失败项)。

    写之前先跟旧文件比一遍：一字不差就不重写（连 fetched_at 也不动，文件时间
    才能如实反映数据新旧）；旧文件非空而新数据 0 条时保留旧文件——那多半是
    vndb 抽风，不该把攒了好几年的人员档案抹成空白。一个人写失败只报他自己：
    库是一人一个文件，没有「写了一半」的中间状态。
    """
    saved, skipped, failures = [], [], []
    for item in items:
        who = item.staff.label()
        if os.path.exists(store.path_for(db_dir, item.staff.sid)):
            try:
                old = store.read_person(db_dir, item.staff.sid)
            except store.BadFile as e:
                ctx.log('%s 的旧文件读不出来，没有覆盖：%s' % (who, e), 'warn')
                failures.append((who, str(e)))
                continue
            if not item.credits and old.credits:
                ctx.log('%s 在 vndb 上抓到 0 条，保留旧文件' % who, 'warn')
                failures.append((who, 'vndb 上抓到 0 条，保留旧文件'))
                continue
            if update.same_item(old.item, item):
                skipped.append(item.staff.sid)
                continue
        try:
            ctx.check_cancel()
            store.write_person(db_dir, item)
        except (store.BadFile, OSError) as e:
            ctx.log('%s 没写进本地库：%s' % (item.staff.label(), e), 'warn')
            failures.append((who, str(e)))
        else:
            saved.append(item.staff.sid)
    return saved, skipped, failures


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
        saved, skipped, refused = _save_to_db(db_dir, ctx, items)
        failures += refused
        if refused:
            warnings.append('%d 个人没写进本地库。' % len(refused))
        if saved or skipped:
            outputs.append(db_dir)
        line = '本地库 : %d/%d 人 -> %s' % (len(saved), len(items), db_dir)
        if skipped:
            line += '（%d 人无变化，未重写）' % len(skipped)
        lines.append(line)
    if params.get('export'):
        ctx.log('正在写 Excel…')
        try:
            path = xlsx.save(items, groups, params.get('out_dir'), ctx=ctx)
        except Cancelled as stop:
            stop.partial = RunResult(
                summary='\n'.join(lines + ['写 Excel 时取消，没有导出文件。']),
                output_paths=[path for path in outputs if os.path.exists(path)],
                warnings=warnings, failures=failures,
                table=tables.result_table(items, groups))
            raise
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
                '及以上的组合各出一页。勾上「更新全库」则把库目录里已有的每个人'
                '都重抓一遍，有变化才覆盖。',
    fields=(
        Field(key='staff', kind=TEXT, label='声优', rescan=True, history=True,
              required=False,
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
        Field(key='update_all', kind=BOOL, label='更新全库', default=False,
              rescan=True, required=False,
              help='把库目录里已有的每个人都在 vndb 上重抓一遍：有变化才整份'
                   '覆盖，没变化不动文件。勾选后忽略上面的「声优」输入，也不'
                   '导出 Excel；不受 8 人上限约束，人多时要几分钟，耐心等。'),
    ),
    run=run,
    preview=preview,
    validate=validate,
    scan_label='查询',
)
