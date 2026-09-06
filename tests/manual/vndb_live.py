# -*- coding: utf-8 -*-
"""真联网：对账 fetch.py 与探针基准（2026-09 实测 s367 -> 303 行 / 268 部，
s367∩s131 -> 27 部）。离线测试钉不住接口自己变了，这个脚本才能。
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))))

from galtools.core.context import ConsoleContext
from galtools.tools.vndb_voiced import api, fetch

ctx = ConsoleContext()
client = api.Client(ctx)

t0 = time.time()
print('--- main_names(or filter) ---')
print(fetch.main_names(['s367', 's131'], client))

print('--- resolve ---')
res = fetch.ensure_resolved({'staff': 's367, s131'}, ctx, client)
for r in res:
    print(r.target, '->', r.staff.label() if r.ok else r.error)
    if r.ok:
        print('   aliases:', len(r.staff.aliases), 'counts:',
              fetch.ensure_counts(r.staff, ctx, client))

print('--- credits ---')
items, failures = fetch.ensure_credits([r.staff for r in res], ctx, client)
print()
for it in items:
    print(it.staff.label(), 'rows=%d vns=%d' % (len(it.credits), len(it.vids)))
print('failures:', failures)

print('--- intersect ---')
common = fetch.intersect(items)
print('common vns:', len(common))
for c in common[:3]:
    print('  ', c.released, c.title_ja or c.title, c.casts)
print('--- sample credit ---')
print(items[0].credits[0])
print('requests=%d  %.1fs' % (client.requests, time.time() - t0))

# --- update_all：把刚抓的两人写进 tests/manual/live_db，再真更新一遍 ---
# 期望：第二遍全部「无变化」，一个文件都不重写。库目录留着不删，方便人眼检查。
from galtools.tools.vndb_voiced import store, tool

db = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'live_db')
os.makedirs(db, exist_ok=True)
for it in items:
    store.write_person(db, it)

params = {'staff': '', 'update_all': True, 'save_db': True, 'db_dir': db,
          'export': False, 'out_dir': '', 'refresh': False}
t1 = time.time()
result = tool.run(params, ConsoleContext())
print('--- update_all ---')
print(result.summary)
print('update_all 耗时 %.1fs' % (time.time() - t1))
