# galtools

galgame 解包资源的处理工具箱。一个 GUI 窗口作为统一入口，工具本身仍可单独在命令行使用。

```
python run_gui.py
```

左侧按分类列出工具，右侧是根据工具声明自动生成的参数表单，底部是所有工具共用的进度条与日志。

## 安装

需要 Python 3.10+（实测 3.10.8）。GUI 依赖 PySide6，`vndb 声优出演表` 写 Excel 依赖 openpyxl：

```
pip install -r requirements.txt
```

`galtools/core/` 只用标准库，工具也不在 import 期碰第三方库（openpyxl 一律在函数内 import）。因此没装 PySide6 时 GUI 起不来但命令行照常可用，没装 openpyxl 时工具列表照常列出——两点都有测试钉住，`requirements.txt` 里也逐行注明了哪个依赖是给谁的。

## 现有工具

**Majiro 脚本文本提取**（`galtools/tools/mjo_text.py`）把 arc_conv 解包出的 `.mjo` 批量提取为 txt，另在输出目录的父目录写一份合并全文。不递归子目录。

```
python -m galtools.tools.mjo_text <.mjo 目录> [输出目录]
```

输出目录留空时为 `<.mjo 目录>/script_text`。目录里一个 `.mjo` 都没有时什么都不做——合并全文往往是上一次的成果，`'w'` 打开就会把它清成空文件，而命令行没有预览那道闸。

字节码起点按头部算出的 `header_len` 而不是硬编码的 `0x28`：入口函数表项数为 1 时两者相等（剧情脚本几乎都是），不为 1 的那些 UI 脚本旧起点会从入口表中间起步。但实测两部作品共 306 个脚本、其中 60 个项数不为 1，两种起点的提取结果逐条相同——入口表里的字节极少凑成文本 opcode，凑成了也过不了校验，逐字节步进会在遇到真 opcode 之前重新对齐。所以这不是一个曾经产出乱码的 bug，改它只是为了不依赖这种巧合；对账脚本是 `tests/manual/mjo_offset_diff.py`。

**语音时长筛选**（`galtools/tools/audio_filter/`）按时长区间把语音复制到源目录下的新文件夹，并生成复制清单。不解码音频，直接读 WAV 的 RIFF 头与 Ogg 的 granule position。

```
python -m galtools.tools.audio_filter.cli
```

命令行版是原来的五步交互向导，提示词与打印格式逐字保留。GUI 里那个「报命中数 → 确认 → 回退重设阈值」的循环由实时预览取代：改阈值立刻看到命中几个、多少 MB、磁盘够不够。解析不出时长的文件（含被截断到几十字节、在头部切片上抛 `struct.error` 的那种）只算它自己失败，不会掀掉整次扫描。

**vndb 声优出演表**（`galtools/tools/vndb_voiced/`）抓 VNDB 上某个声优配过的全部角色，导出一个工作簿：一页概览（各人的作品数、角色数与主次分布）+ 每人一页明细，标题与角色名都带指向 vndb.org 的超链接。填两人以上时额外算出共同出演的作品，旧仓库里这是另一个需要先跑两遍再手动喂两个 xlsx 的脚本。

三人以上时两两及以上的每个组合各出一页——三个人就是「三人都在」加三种两人搭配共四页，没有交集的组合不建表。页名是组合内各人的罗马音，但 Excel 只给工作表名 31 个字符，三个人的完整罗马音塞不下（`Ono Ryouko+Mizuhashi Kaori+Okajima Tae` 是 39 个），所以整组一起降级：完整罗马音 → 姓 + 名首字母 → 姓 → sid，取第一个塞得下的。完整名字与「哪张表」的对应关系在新增的「组合」索引页里，那一页还带跳转链接。文件名把每个人的罗马音都写进去（`共同出演_Ono_Ryouko_Mizuhashi_Kaori_Okajima_Tae_3人.xlsx`），太长才退回前两人 + 等N人。

人数上限 8：N 个人有 2^N−N−1 个组合，8 人最多 247 张工作表，再往上只会产出没人翻得动的文件。同一个人填两次（`s367, Ono Ryouko` 是两个字串、一个人）按 sid 合并，不会抓两遍也不会出「他和他自己」那张表。

```
python -m galtools.tools.vndb_voiced.cli [目标...] [-o 输出目录或 .xlsx]
```

目标可以是 id（`s367`）、声优页网址或名字，多个用逗号分隔；不带参数则进入交互模式。名字有歧义时不会替你猜，而是列出候选各自的主名并要求改填 id——按搜索结果的第一条自动取会静默选错人（实测搜 `Ono Ryouko` 会同时命中另一个人的某个别名行）。

走官方 kana JSON API（只用标准库 `urllib`），带限流、退避重试与三阶段字段裁剪：角色名来自便宜的 `/character` 查询，在 `/vn` 上展开 `va.character.name` 会让同样 100 部作品从 2 秒变成 30 秒。组合也不是每个各算一遍交集（那是 2^N 次全量遍历）：一次遍历得到每部作品的出演者集合，再只枚举这个集合自己的子集，而一部作品通常只被两三个人共享。

失败的路径也照「别让用户白等」来安排。输出目录在抓取之前就先建一次：`-o Z:\nope\deeper` 以前要等打完 5 个请求才炸在 `os.makedirs` 上，几分钟的抓取白费，现在是零请求返回一句话。抓完才写不出去（盘满、整条路径过 260、目标正被 Excel 占着）报成一句话而不是一段栈，抓回来的东西还在会话缓存里，换个目录再点一次「开始」不必重抓。命令行在一个文件都没写出来时以非零退出，`ap.error` 那类用法错误照旧是 2。某个人抓失败时整份结果不进缓存——否则再点一次「开始」是零请求、同一份残缺结果，除非用户想到去勾「重新抓取」。撞上官方限额（这里按 190 请求 / 5 分钟留了余量）要等 5 秒以上时，日志里会说一声等多久；限流窗口挂在 `ctx.session` 上，也就是跨线程共享的，记账整段在一把模块级锁里做。

GUI 里查询前先摆一张「将要抓取」的表，ID 一列双击即可在浏览器里核对是不是这个人；名字有歧义时这张表换成全部候选，比预览框里只放得下的前几行好选。跑完把共同出演（三人以上是全员那一档，只有一个人时是他的全部出演记录）铺成表格，核对几部作品不必再开 Excel；各组合各自多少部则列在摘要里。

## 加一个新工具

往 `galtools/tools/` 放一个模块并暴露 `TOOL`，GUI 下次启动就会列出它。不需要改 GUI 任何一行。单个 `.py` 和子包（`__init__.py` 暴露 `TOOL`）两种形态都认——单文件是快车道，工具长大了再拆包。以 `_` 开头的模块会被跳过。

```python
import os

from galtools.core.spec import DIR, Field, RunResult, ToolSpec


def run(params, ctx):
    names = os.listdir(params['src'])
    for i, name in enumerate(names, 1):
        ctx.check_cancel()
        ctx.progress(i, len(names))
        ...
    return RunResult(summary='处理了 %d 个文件' % len(names),
                     output_paths=[params['src']])


TOOL = ToolSpec(
    id='my_tool', name='我的工具', category='文本',
    description='一句话说明，会显示在标题下方和树的悬浮提示里。',
    fields=(Field(key='src', kind=DIR, label='源目录'),),
    run=run,
)
```

`Field.kind` 只有 `dir` / `bool` / `number` / `text` 四种，由 `gui/form.py` 的分派表决定用哪个控件。要加新种类就往那张表加一个构造函数。`dir` 字段自带浏览按钮、拖放，以及按「工具 + 字段」分别持久化的最近路径下拉——在多个游戏目录间来回切换时省下反复粘贴长路径。`text` 字段默认是单行输入框，写 `history=True` 就换成同款下拉（vndb 那个声优框用它记住查过的人）。

`ctx` 是 `RunContext`（`galtools/core/context.py`），提供 `log(msg, level)`、`progress(done, total, note)`、`check_cancel()` 和 `session` 字典。GUI 传的实现把这些转成 Qt 信号，命令行传 `ConsoleContext`。

三个可选钩子：

`preview(params, ctx) -> PreviewResult` 在参数变动后自动调用，把摘要显示在预览框里。`ok=False` 时禁止启动——磁盘不够、命中 0 个都靠它挡下。工具没什么可预览的就不实现，预览框自动隐藏。

`PreviewResult` 与 `RunResult` 都可以再带一个 `Table(columns, rows, title)`，GUI 在预览框下方铺一张表：点表头排序、Ctrl+C 按 TSV 复制选中区域，单元格写成 `(文本, 链接)` 二元组就变成双击打开浏览器的蓝字。数字传 int/float 而不是字符串，否则 `10` 会排在 `9` 前面。不给表就不显示，纵向空间还给上面的表单。

`validate(params) -> [(字段 key, 错误消息)]` 校验不通过时「开始」按钮禁用，消息标红显示在对应字段下方；key 填空串表示整表级错误，显示在表单末尾。跨字段规则（如「上限必须大于阈值」）只能写在这里，`Field` 上没有 min/max 常量。命令行入口要自己调它——`vndb_voiced/cli.py` 的 `check_targets` 就是直接转发给 `validate`；`audio_filter` 那个交互向导目前是另写一份等价规则，两处漂移的风险仍在（vndb 的人数上限当初就是这么漏掉命令行的）。

`Field.rescan=True` 声明「改这个字段会让预览赖以计算的数据集失效」。此时 GUI 不自动重算，只把预览标记为过期并露出那个按钮——否则在路径框里敲一个字符就会去 walk 一遍整个盘。改非 rescan 字段则防抖 300ms 后自动重算。数据集缓存放 `ctx.session`，由 GUI 按工具持有。按钮上的字由 `ToolSpec.scan_label` 决定，默认「扫描」；vndb 那个叫「查询」，它打的是接口而不是磁盘。

取消靠 `ctx.check_cancel()`，它抛 `Cancelled`。**`Cancelled` 继承 `BaseException` 而不是 `Exception`**，这样批量循环里的 `except Exception` 不会把取消吞成「单个文件失败」然后继续跑完。要向 GUI 汇报已完成的部分，就在放行异常前给它挂一个 `RunResult`：

```python
except Cancelled as c:
    c.partial = RunResult(summary='已复制 %d / %d 个' % (copied, total),
                          output_paths=[out_dir])
    raise
```

某个工具 import 失败只会让它自己缺席：树里显示成「加载失败」、栈写进日志，其余工具照常可用。

## 测试

```
pytest
```

覆盖三个工具的纯逻辑、registry 的容错，以及 GUI 里几个「坏了也没人喊」的分支。`pytest.ini` 里的 `pythonpath = .` 让 `galtools` 无需安装即可导入。vndb 那套测试零联网：`api` 模块只留 `_open` / `_sleep` 两个注入点，测试全打这两处（假睡眠顺带推进一个假时钟，否则限流的滑动窗口永远滚不过去）。

`tests/test_gui.py` 顶上是 `pytest.importorskip('PySide6')`，没装 Qt 就整个文件跳过——命令行用法不该因为缺 Qt 连测试都跑不了。它离屏跑，钉四件事：活计换代后被放弃的线程发的日志、进度、结果一律丢弃；取消时挂在 `Cancelled` 上的 `partial` 要送到 `run_cancelled`；预览失败后进度条要从无限滚动收回来；路径被剥到只剩盘符时要把分隔符补回去（`E:` 指的是该盘的当前工作目录而非根目录，`isdir` 却照样为真，产出会静默落到别处）。这四处的变异在此之前都能全绿通过。

更完整的一轮留给 `tests/manual/` 里的手动脚本：`testpaths = tests` 只收 `test_*.py`，这几个不会被 pytest 捡走，要跑就 `python tests/manual/gui_smoke.py`。`gui_smoke.py` 离屏起真窗口走完「选工具 → 查询 → 预览 → 开始 → 改参数 → 重开窗」一整轮，vndb 那层换成测试里的假接口；`gui_widgets.py` 造一个假工具钉住整表级错误与表格的排序、链接、复制；`vndb_live.py` 是唯一真联网的一个，拿真实 s367 与实测基准对账——接口哪天改了字段，只有它能发现；`mjo_offset_diff.py` 要一批真 `.mjo` 才有意义，用来复核字节码起点那条结论。

前两个脚本都把 `main_window.QSettings` 换成临时 ini（`tests/test_gui.py` 同理）。PySide6 里 `QSettings(org, app)` 不看 `setDefaultFormat()`，照样写注册表，不换会把验证用的路径写进用户的真实配置。
