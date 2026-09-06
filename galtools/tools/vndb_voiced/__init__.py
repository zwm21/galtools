# -*- coding: utf-8 -*-
"""vndb 声优出演表：抓 VNDB 上某个声优配过的全部角色，存进本地库并可导出 Excel。

填两人以上时额外算出共同出演的作品（旧仓库里这是另一个脚本 compare_voiced_xlsx，
需要先跑两次抓取再手动喂两个 xlsx 给它；这里合成一步）。三人以上时两两及以上的
每个组合各出一页，页名是各人的罗马音，另有一张「组合」索引页。

抓回来的东西有两个去处，各自一个开关：本地库（默认开，一人一个 json，见
store.py）与 xlsx（默认关）。默认这么配是因为抓一次要十几秒到几分钟，而库里的
数据用「声优库」工具随时能离线导出、离线算共同出演，不必为了看一眼再抓一遍。

旧版抓 HTML，本版走官方 kana JSON API：更准（角色主次、别名、日文原名都是结构化
字段，不靠 DOM 猜）也更快。三阶段抓取见 fetch.py。

preview 只做解析 + count（两三次便宜请求），真正的翻页留给 run。GUI 每改一个
非 rescan 字段就会自动重跑 preview，所以解析结果与 count 都缓存在 ctx.session 上。

工具层编排在 tool.py；这里只留对外的那几个名字，调用方（cli、seiyuu_db、GUI）
的导入路径不因拆分而变。
"""
from .tool import (
    MAX_LISTED_COMBOS, MAX_TARGETS, TOOL, _apply_refresh, eta_text, preview,
    run, too_many_people, unique_people, validate,
)

__all__ = [
    'MAX_LISTED_COMBOS', 'MAX_TARGETS', 'TOOL', '_apply_refresh', 'eta_text',
    'preview', 'run', 'too_many_people', 'unique_people', 'validate',
]
