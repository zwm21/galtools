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

`galtools/core/` 只用标准库，工具也不在 import 期碰第三方库（openpyxl 一律在函数内 import）。因此没装 PySide6 时 GUI 起不来但命令行照常可用，没装 openpyxl 时工具列表照常列出——两点都有测试钉住。

## 现有工具

**Majiro 脚本文本提取**（`galtools/tools/mjo_text.py`）把 arc_conv 解包出的 `.mjo` 批量提取为 txt，另在输出目录的父目录写一份合并全文。不递归子目录。

```
python -m galtools.tools.mjo_text <.mjo 目录> [输出目录]
```

输出目录留空时为 `<.mjo 目录>/script_text`。

**语音时长筛选**（`galtools/tools/audio_filter/`）按时长区间把语音复制到源目录下的新文件夹，并生成复制清单。不解码音频，直接读 WAV 的 RIFF 头与 Ogg 的 granule position。

```
python -m galtools.tools.audio_filter.cli
```

命令行版是原来的五步交互向导，提示词与打印格式逐字保留。GUI 里那个「报命中数 → 确认 → 回退重设阈值」的循环由实时预览取代：改阈值立刻看到命中几个、多少 MB、磁盘够不够。

**vndb 声优出演表**（`galtools/tools/vndb_voiced/`）抓 VNDB 上某个声优配过的全部角色，导出一个工作簿：一页概览（各人的作品数、角色数与主次分布）+ 每人一页明细，标题与角色名都带指向 vndb.org 的超链接。填两人以上时额外算出共同出演的作品，旧仓库里这是另一个需要先跑两遍再手动喂两个 xlsx 的脚本。

```
python -m galtools.tools.vndb_voiced.cli [目标...] [-o 输出目录或 .xlsx]
```

目标可以是 id（`s367`）、声优页网址或名字，多个用逗号分隔；不带参数则进入交互模式。名字有歧义时不会替你猜，而是列出候选各自的主名并要求改填 id——按搜索结果的第一条自动取会静默选错人（实测搜 `Ono Ryouko` 会同时命中另一个人的某个别名行）。

走官方 kana JSON API（只用标准库 `urllib`），带限流、退避重试与三阶段字段裁剪：角色名来自便宜的 `/character` 查询，在 `/vn` 上展开 `va.character.name` 会让同样 100 部作品从 2 秒变成 30 秒。

GUI 里查询前先摆一张「将要抓取」的表，ID 一列双击即可在浏览器里核对是不是这个人；名字有歧义时这张表换成全部候选，比预览框里只放得下的前几行好选。跑完把共同出演（只有一个人时是他的全部出演记录）铺成表格，核对几部作品不必再开 Excel。

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

`validate(params) -> [(字段 key, 错误消息)]` 校验不通过时「开始」按钮禁用，消息标红显示在对应字段下方；key 填空串表示整表级错误，显示在表单末尾。跨字段规则（如「上限必须大于阈值」）只能写在这里，`Field` 上没有 min/max 常量。GUI 与 CLI 共用同一份，规则不会两处漂移。

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

覆盖三个工具的纯逻辑与 registry 的容错，不含 GUI 自动化测试。`pytest.ini` 里的 `pythonpath = .` 让 `galtools` 无需安装即可导入。vndb 那套测试零联网：`api` 模块只留 `_open` / `_sleep` 两个注入点，测试全打这两处（假睡眠顺带推进一个假时钟，否则限流的滑动窗口永远滚不过去）。

GUI 没有自动化测试，手动验证脚本放在 `tests/manual/`：`testpaths = tests` 只收 `test_*.py`，这几个不会被 pytest 捡走，要跑就 `python tests/manual/gui_smoke.py`。`gui_smoke.py` 离屏起真窗口走完「选工具 → 查询 → 预览 → 开始 → 改参数 → 重开窗」一整轮，vndb 那层换成测试里的假接口；`gui_widgets.py` 造一个假工具钉住整表级错误与表格的排序、链接、复制；`vndb_live.py` 是唯一真联网的一个，拿真实 s367 与实测基准对账——接口哪天改了字段，只有它能发现。

前两个脚本都把 `main_window.QSettings` 换成临时 ini。PySide6 里 `QSettings(org, app)` 不看 `setDefaultFormat()`，照样写注册表，不换会把验证用的路径写进用户的真实配置。
