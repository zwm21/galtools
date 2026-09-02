# -*- coding: utf-8 -*-
"""目标解析 + 三阶段抓取 + N 人交集。

抓取分三阶段，字段集是性能的关键：在 /vn 上展开 `va.character.name` 会让同样
100 部作品从 2 秒变成 30 秒，所以角色名一律来自便宜的 /character 查询。

    阶段 0  /staff      别名表（aid -> 名字）与主名
    阶段 1  /character  该声优配过的角色：名字 + 每部作品里的 role
    阶段 2  /vn         权威的 va 关系，只要必需字段

阶段 2 的嵌套 filter 会过度匹配：它返回「包含该声优配过的角色」的作品的**全部**
va 记录，因此必须按 `va[].staff.id == sid` 在客户端再过滤一遍（实测 288 候选
作品 / 307 pair vs 真实 268 / 284，缺失 0）。

缓存仿 audio_filter 的 ensure_scan：挂在 ctx.session 上、只在抓完后一次性写入，
被取消的抓取到不了那一步，不会留下半份缓存。GUI 每改一个非 rescan 字段就会重跑
预览，没有这层缓存就会连着打 API。
"""
import re

from . import api
from .model import (
    Candidate, Common, Credit, Resolution, Staff, StaffCredits,
    released_sort_key, url_for,
)

STAFF_FIELDS = 'id,aid,ismain,name,original'
CHAR_FIELDS = 'id,name,original,vns.id,vns.role'
VN_FIELDS = ('id,title,alttitle,released,'
             'va.staff.id,va.staff.aid,va.character.id,va.note')

SEARCH_LIMIT = 20

# 目标之间的分隔符。刻意不含空格：罗马字人名自带空格（Ono Ryouko），
# 拿空格当分隔符会把一个人切成两个不存在的目标。
SPLIT_RE = re.compile(r'[,，、;；\n\r\t]+')
ID_RE = re.compile(r'^s?(\d+)$', re.IGNORECASE)
URL_RE = re.compile(r'(?:https?://)?(?:www\.)?vndb\.org/([a-z]+\d+)', re.IGNORECASE)
OTHER_ID_RE = re.compile(r'^([vcrpgiu])\d+$', re.IGNORECASE)

OTHER_KINDS = {'v': '作品', 'c': '角色', 'r': '发行', 'p': '开发商',
               'g': '标签', 'i': '道具', 'u': '用户'}


def normalize(text):
    """比较与去重用的归一化：去掉全部空白后 casefold。

    vndb 上「水橋 かおり」与「水橋かおり」是同一个人，罗马字大小写也不该区分。
    """
    return ''.join((text or '').split()).casefold()


def looks_like_id(token):
    return bool(ID_RE.match(token) or URL_RE.search(token))


def parse_targets(raw):
    """把输入框里的一串目标切成列表，按归一化结果去重、保持原顺序。

    只在逗号/顿号/分号/换行处切。一段里全是 id 时才额外按空格切，这样
    `s367 s131` 能work，而 `Ono Ryouko` 不会被切成两半。
    """
    out, seen = [], set()
    for piece in SPLIT_RE.split(raw or ''):
        tokens = piece.split()
        if not tokens:
            continue
        if len(tokens) > 1 and all(looks_like_id(t) for t in tokens):
            candidates = tokens
        else:
            candidates = [' '.join(tokens)]
        for target in candidates:
            key = normalize(target)
            if key and key not in seen:
                seen.add(key)
                out.append(target)
    return out


def classify(target):
    """返回 ('id', 'sNNN') / ('name', 名字) / ('bad', 原因)。

    纯本地判断，不联网——validate() 每敲一个键都会跑一次。
    """
    text = (target or '').strip()
    if not text:
        return 'bad', '目标为空'
    match = URL_RE.search(text)
    if match:
        ident = match.group(1).lower()
        if ID_RE.match(ident):
            return 'id', ident
        kind = OTHER_KINDS.get(ident[0])
        return 'bad', ('%s 是%s的页面，本工具要声优页（https://vndb.org/s367）'
                       % (ident, kind or '别的东西'))
    if '/' in text or text.lower().startswith('http'):
        return 'bad', '看不出 staff id，网址要形如 https://vndb.org/s367'
    match = ID_RE.match(text)
    if match:
        return 'id', 's' + match.group(1)
    match = OTHER_ID_RE.match(text)
    if match:
        kind = OTHER_KINDS.get(match.group(1).lower(), '别的东西')
        return 'bad', '%s 是%s的 id，本工具要声优的 staff id（如 s367）' % (text, kind)
    if len(text) < 2:
        return 'bad', '「%s」太短，一个字符会命中成百上千人' % text
    return 'name', text


# ---------------- 过滤器 ----------------
def char_filter(sid):
    return ['seiyuu', '=', ['id', '=', sid]]


def vn_filter(sid):
    return ['character', '=', ['seiyuu', '=', ['id', '=', sid]]]


# ---------------- 解析 ----------------
def load_staff(sid, client):
    """按 id 取一个人的主名与全部别名。查不到返回 None。"""
    aliases, main = {}, None
    for row in client.paged('staff', {'filters': ['id', '=', sid],
                                      'fields': STAFF_FIELDS}):
        entry = (row.get('name') or '', row.get('original') or '')
        aliases[row.get('aid')] = entry
        if row.get('ismain'):
            main = entry
    if not aliases:
        return None
    if main is None:                      # 理论上每个人都有一行 ismain
        main = next(iter(aliases.values()))
    return Staff(sid=sid, name=main[0], original=main[1], aliases=aliases)


def main_names(sids, client):
    """批量取每个 id 的主名，一次请求。"""
    if not sids:
        return {}
    if len(sids) == 1:
        filters = ['id', '=', sids[0]]
    else:
        filters = ['or'] + [['id', '=', s] for s in sids]
    out = {}
    for row in client.paged('staff', {'filters': filters,
                                      'fields': STAFF_FIELDS}):
        if row.get('ismain'):
            out[row.get('id')] = (row.get('name') or '', row.get('original') or '')
    return out


def resolve_name(target, client):
    """按名字搜人。

    搜索命中的是**别名行**，返回行里的名字可能与用户敲的字串毫无关系：实测
    `search=Ono Ryouko` 会同时命中 s252（其别名之一恰好被索引到，主名是
    Muryoukouji Kabutonosuke）和 s367（真正的小野涼子）。按第一条自动取会静默
    选错人，所以规则写死成：按 id 去重 → 归一化后与 name/original 精确相等的
    唯一候选才自动采用 → 否则列出全部候选（展示各 id 的主名）并拒绝启动。
    """
    res = client.post('staff', {'filters': ['search', '=', target],
                                'fields': STAFF_FIELDS, 'results': SEARCH_LIMIT})
    rows = res.get('results') or []
    if not rows:
        return Resolution(target=target, error='搜不到叫「%s」的人' % target)

    order, groups = [], {}
    for row in rows:
        sid = row.get('id')
        if sid not in groups:
            order.append(sid)
            groups[sid] = []
        groups[sid].append(row)

    want = normalize(target)
    exact = [sid for sid in order
             if any(normalize(r.get('name')) == want
                    or normalize(r.get('original')) == want
                    for r in groups[sid])]
    if len(exact) == 1:
        staff = load_staff(exact[0], client)
        if staff is not None:
            return Resolution(target=target, staff=staff)

    names = main_names(order, client)
    candidates = []
    for sid in order:
        name, original = names.get(sid) or (groups[sid][0].get('name') or '',
                                            groups[sid][0].get('original') or '')
        candidates.append(Candidate(sid=sid, name=name, original=original))
    if len(candidates) == 1:
        error = '「%s」只命中一个人，但名字不完全相同' % target
    else:
        error = '「%s」命中 %d 个人' % (target, len(candidates))
    return Resolution(target=target, error=error + '，请改填其中一个 id',
                      candidates=candidates)


def resolve_target(target, client):
    kind, value = classify(target)
    if kind == 'bad':
        return Resolution(target=target, error=value)
    if kind == 'name':
        return resolve_name(value, client)
    staff = load_staff(value, client)
    if staff is None:
        return Resolution(target=target, error='vndb 上没有 %s 这个人' % value)
    return Resolution(target=target, staff=staff)


def describe_problem(res, limit=6):
    """把一个解析失败连候选一起写成可读的几行。preview 与 CLI 共用。"""
    lines = ['%s：%s' % (res.target, res.error)]
    for cand in res.candidates[:limit]:
        lines.append('    ' + cand.label())
    if len(res.candidates) > limit:
        lines.append('    …等共 %d 个' % len(res.candidates))
    return '\n'.join(lines)


# ---------------- 抓取 ----------------
def fetch_credits(staff, client, on_progress=None):
    """三阶段抓一个人的全部出演记录，按 (发售日, 标题, 角色) 排序后返回。"""
    chars, roles = {}, {}
    for ch in client.paged('character', {'filters': char_filter(staff.sid),
                                         'fields': CHAR_FIELDS, 'sort': 'id'}):
        chars[ch.get('id')] = (ch.get('name') or '', ch.get('original') or '')
        for vn in ch.get('vns') or []:
            roles[(vn.get('id'), ch.get('id'))] = vn.get('role') or ''

    state = {'total': 0, 'done': 0}
    credits = []
    pages = client.paged('vn', {'filters': vn_filter(staff.sid),
                                'fields': VN_FIELDS, 'sort': 'id'},
                         on_count=lambda n: state.__setitem__('total', n))
    for vn in pages:
        state['done'] += 1
        if on_progress is not None:
            on_progress(state['done'], state['total'])
        vid = vn.get('id')
        for va in vn.get('va') or []:
            credited = va.get('staff') or {}
            if credited.get('id') != staff.sid:
                continue      # 同作品里别的声优，嵌套 filter 把他们一并带回了
            cid = (va.get('character') or {}).get('id') or ''
            cast, cast_ja = chars.get(cid, ('', ''))
            alias, alias_ja = staff.alias(credited.get('aid'))
            credits.append(Credit(
                vid=vid, title=vn.get('title') or '',
                title_ja=vn.get('alttitle') or '',
                released=vn.get('released') or '', cid=cid,
                cast=cast, cast_ja=cast_ja, alias=alias, alias_ja=alias_ja,
                note=va.get('note') or '', role=roles.get((vid, cid), '')))
    credits.sort(key=lambda c: (released_sort_key(c.released), c.title, c.cast))
    return credits


# ---------------- 缓存 ----------------
# 键名与 audio_filter 的 'scan' 并列，都挂在 ctx.session 上：
#     resolve  (归一化目标元组) -> [Resolution]
#     counts   sid -> (角色数, 候选作品数)
#     credits  (sid 元组) -> ([StaffCredits], failures)
CACHE_KEYS = ('resolve', 'counts', 'credits')


def clear_cache(ctx):
    """丢掉全部缓存。勾了「重新抓取」时调用。"""
    for key in CACHE_KEYS:
        ctx.session.pop(key, None)


def ensure_resolved(params, ctx, client=None):
    """把输入框里的目标逐个解析成人，按目标列表缓存。

    返回 [Resolution]，顺序与用户输入一致；中途抛 ApiError 时不写缓存。
    """
    targets = parse_targets(params.get('staff'))
    key = tuple(normalize(t) for t in targets)
    cached = ctx.session.get('resolve')
    if cached is not None and cached[0] == key:
        return cached[1]
    if client is None:
        client = api.Client(ctx)
    out = [resolve_target(t, client) for t in targets]
    ctx.session['resolve'] = (key, out)
    return out


def ensure_counts(staff, ctx, client):
    """(角色数, 候选作品数)。两次 count 请求，便宜到可以放进预览。

    候选作品数是上界而非真实值：/vn 的嵌套 filter 会把「含有该声优配过的角色，
    但这一部里由别人配」的作品也算进来（实测 288 vs 真实 268）。
    """
    slot = ctx.session.setdefault('counts', {})
    if staff.sid not in slot:
        slot[staff.sid] = (client.count('character', char_filter(staff.sid)),
                           client.count('vn', vn_filter(staff.sid)))
    return slot[staff.sid]


def _reporter(ctx, staff, base, grand):
    """把单人的 done/total 拼成横跨所有人的全局进度。"""
    def report(done, total):
        ctx.progress(min(base + done, grand), grand,
                     '%s %d/%d' % (staff.name, done, total))
    return report


def ensure_credits(staffs, ctx, client=None):
    """抓多人的出演记录，返回 ([StaffCredits], failures)。

    failures 是 [(人名, 原因)]：某一个人抓失败不该连累别人，所以按人捕获
    ApiError；Cancelled 是 BaseException，会照常穿透并且不留下半份缓存。

    进度条要横跨所有人，因此先各要一次 count 算出总量（每人一次便宜请求，
    换一个确定进度；这些 count 本来预览就已经缓存过了）。count 也可能失败，
    所以它同样按人捕获，失败的人直接从抓取循环里跳过。
    """
    key = tuple(s.sid for s in staffs)
    cached = ctx.session.get('credits')
    if cached is not None and cached[0] == key:
        return cached[1]
    if client is None:
        client = api.Client(ctx)

    totals = {}
    failures = []
    for staff in staffs:
        try:
            totals[staff.sid] = ensure_counts(staff, ctx, client)[1]
        except api.ApiError as e:
            failures.append((staff.label(), str(e)))
    grand = sum(totals.values())

    items, base = [], 0
    for staff in staffs:
        if staff.sid not in totals:
            continue          # 连数都要不到，跳过，失败已记在上面
        ctx.log('抓取 %s' % staff.label())
        try:
            credits = fetch_credits(staff, client, _reporter(ctx, staff, base, grand))
        except api.ApiError as e:
            failures.append((staff.label(), str(e)))
        else:
            items.append(StaffCredits(staff=staff, credits=credits))
            ctx.log('  %d 部作品 / %d 个角色' % (len(set(c.vid for c in credits)),
                                            len(credits)))
        base += totals[staff.sid]
    ctx.session['credits'] = (key, (items, failures))
    return items, failures


# ---------------- 交集 ----------------
def intersect(items):
    """N 人共同出演：按 vid 求交集，每部作品带上各人在其中配的角色。

    casts 与 items 同序，每项是该人在这部作品里的 [(角色名, 链接)]——一个人在
    同一部里配多个角色是常态，所以是列表而不是单值。同名角色去重（保留先出现
    的链接），沿用旧 compare 脚本的行为。
    """
    if not items:
        return []
    maps = []
    for item in items:
        by_vid = {}
        for credit in item.credits:
            by_vid.setdefault(credit.vid, []).append(credit)
        maps.append(by_vid)

    shared = set(maps[0])
    for by_vid in maps[1:]:
        shared &= set(by_vid)

    out = []
    for vid in shared:
        first = maps[0][vid][0]
        casts = []
        for by_vid in maps:
            seen, one = set(), []
            for credit in by_vid[vid]:
                text = credit.cast_ja or credit.cast
                if text not in seen:
                    seen.add(text)
                    one.append((text, url_for(credit.cid)))
            casts.append(one)
        out.append(Common(vid=vid, title=first.title, title_ja=first.title_ja,
                          released=first.released, casts=casts))
    out.sort(key=lambda x: (released_sort_key(x.released), x.title, x.vid))
    return out
