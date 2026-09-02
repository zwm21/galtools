# -*- coding: utf-8 -*-
"""插件发现机制。加新工具 = 往 galtools/tools/ 放一个模块并暴露 TOOL，
GUI 不改一行——这里钉住的就是这条承诺，包括坏工具不能拖垮整个程序。
"""
import os
import subprocess
import sys

from galtools.core.registry import discover

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

GOOD = '''# -*- coding: utf-8 -*-
from galtools.core.spec import DIR, Field, RunResult, ToolSpec

TOOL = ToolSpec(id=%(id)r, name=%(name)r, category=%(cat)r, description='',
                fields=(Field(key='src', kind=DIR, label='目录'),),
                run=lambda params, ctx: RunResult())
'''


def make_pkg(root, name):
    pkg = root / name
    pkg.mkdir()
    (pkg / '__init__.py').write_text('', encoding='utf-8')
    return pkg


def test_discovers_both_module_and_package_shapes(tmp_path, monkeypatch):
    pkg = make_pkg(tmp_path, 'shapes_tools')
    (pkg / 'flat.py').write_text(
        GOOD % {'id': 'flat', 'name': '单文件', 'cat': '甲'}, encoding='utf-8')
    sub = make_pkg(pkg, 'nested')
    (sub / '__init__.py').write_text(
        GOOD % {'id': 'nested', 'name': '子包', 'cat': '甲'}, encoding='utf-8')

    monkeypatch.syspath_prepend(str(tmp_path))
    tools, errors = discover('shapes_tools')
    assert [t.id for t in tools] == ['flat', 'nested']
    assert errors == []


def test_broken_tool_is_reported_not_raised(tmp_path, monkeypatch):
    pkg = make_pkg(tmp_path, 'broken_tools')
    (pkg / 'ok.py').write_text(
        GOOD % {'id': 'ok', 'name': '正常', 'cat': '甲'}, encoding='utf-8')
    (pkg / 'boom.py').write_text('raise RuntimeError("我坏了")', encoding='utf-8')

    monkeypatch.syspath_prepend(str(tmp_path))
    tools, errors = discover('broken_tools')
    assert [t.id for t in tools] == ['ok']
    assert [e.name for e in errors] == ['boom']
    assert '我坏了' in errors[0].traceback


def test_ignores_modules_without_tool_and_underscored(tmp_path, monkeypatch):
    pkg = make_pkg(tmp_path, 'quiet_tools')
    (pkg / 'helper.py').write_text('# 没有 TOOL，是个工具共用的辅助模块\n',
                                   encoding='utf-8')
    (pkg / '_wip.py').write_text('raise RuntimeError("下划线开头不该被 import")',
                                 encoding='utf-8')

    monkeypatch.syspath_prepend(str(tmp_path))
    assert discover('quiet_tools') == ([], [])


def test_sorted_by_category_then_name(tmp_path, monkeypatch):
    """按 (分类, 名称) 排序，比的是 Unicode 码位而不是拼音——加新分类时
    界面上的先后可能不合直觉。'文本' < '音频'，'丁' < '乙'。"""
    pkg = make_pkg(tmp_path, 'sorted_tools')
    for mod, cat, name in [('c', '音频', '甲'),
                           ('a', '文本', '乙'),
                           ('b', '文本', '丁')]:
        (pkg / (mod + '.py')).write_text(
            GOOD % {'id': mod, 'name': name, 'cat': cat}, encoding='utf-8')

    monkeypatch.syspath_prepend(str(tmp_path))
    tools, _ = discover('sorted_tools')
    assert [(t.category, t.name) for t in tools] == [
        ('文本', '丁'), ('文本', '乙'), ('音频', '甲')]


def test_real_tools_load_cleanly():
    tools, errors = discover()
    assert errors == []
    assert {t.id for t in tools} == {'mjo_text', 'audio_filter', 'vndb_voiced'}
    for tool in tools:
        assert tool.name and tool.category and tool.description
        assert tool.fields and callable(tool.run)
        assert len({f.key for f in tool.fields}) == len(tool.fields)


def test_loading_tools_does_not_pull_in_qt():
    """core 与 tools 只用标准库。工具的命令行用法必须在没装 PySide6 的环境里
    也能跑，所以谁都不许在 import 期把 Qt 拖进来。"""
    code = ('import sys; from galtools.core.registry import discover; '
            "discover(); "
            "print(any(m == 'PySide6' or m.startswith('PySide6.') "
            'for m in sys.modules))')
    done = subprocess.run([sys.executable, '-c', code], cwd=ROOT,
                          capture_output=True, text=True)
    assert done.returncode == 0, done.stderr
    assert done.stdout.strip() == 'False'
