# galtools

galgame 解包资源的处理工具箱。一个 GUI 窗口作为统一入口，工具本身仍可单独在命令行使用。

```
python run_gui.py
```

左侧按分类列出工具，右侧是根据工具声明自动生成的参数表单，底部是所有工具共用的进度条与日志。

## 安装

需要 Python 3.10+（实测 3.10.8）。GUI 依赖 PySide6：

```
pip install -r requirements.txt
```

`galtools/core/` 与 `galtools/tools/` 只用标准库。没装 PySide6 时 GUI 起不来，但两个工具的命令行用法照常可用（有测试钉住这一点）。

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

`Field.kind` 只有 `dir` / `bool` / `number` / `text` 四种，由 `gui/form.py` 的分派表决定用哪个控件。要加新种类就往那张表加一个构造函数。`dir` 字段自带浏览按钮、拖放，以及按「工具 + 字段」分别持久化的最近路径下拉——在多个游戏目录间来回切换时省下反复粘贴长路径。

`ctx` 是 `RunContext`（`galtools/core/context.py`），提供 `log(msg, level)`、`progress(done, total, note)`、`check_cancel()` 和 `session` 字典。GUI 传的实现把这些转成 Qt 信号，命令行传 `ConsoleContext`。

三个可选钩子：

`preview(params, ctx) -> PreviewResult` 在参数变动后自动调用，把摘要显示在预览框里。`ok=False` 时禁止启动——磁盘不够、命中 0 个都靠它挡下。工具没什么可预览的就不实现，预览框自动隐藏。

`validate(params) -> [(字段 key, 错误消息)]` 校验不通过时「开始」按钮禁用，消息标红显示在对应字段下方。跨字段规则（如「上限必须大于阈值」）只能写在这里，`Field` 上没有 min/max 常量。GUI 与 CLI 共用同一份，规则不会两处漂移。

`Field.rescan=True` 声明「改这个字段会让预览赖以计算的数据集失效」。此时 GUI 不自动重算，只把预览标记为过期并露出「扫描」按钮——否则在路径框里敲一个字符就会去 walk 一遍整个盘。改非 rescan 字段则防抖 300ms 后自动重算。数据集缓存放 `ctx.session`，由 GUI 按工具持有。

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

覆盖两个工具的纯逻辑与 registry 的容错，不含 GUI 自动化测试。`pytest.ini` 里的 `pythonpath = .` 让 `galtools` 无需安装即可导入。

GUI 只做过手动验证。
