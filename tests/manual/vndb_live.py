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
