# -*- coding: utf-8 -*-
"""发现 galtools/tools/ 下的工具。

工具可以是单个 .py 模块，也可以是子包（__init__.py 暴露 TOOL）——单文件
是加新脚本的快车道，工具长大了再拆包，两种形态这里都认。

逐个包裹 import：某个工具写坏了只应该让它自己缺席并把栈留给界面显示，
不该拖垮整个程序。
"""
import importlib
import pkgutil
import traceback
from dataclasses import dataclass


@dataclass
class LoadError:
    name: str
    traceback: str


def discover(package='galtools.tools'):
    """返回 (tools, errors)，tools 按 (category, name) 排序。"""
    pkg = importlib.import_module(package)
    tools = []
    errors = []
    for info in sorted(pkgutil.iter_modules(pkg.__path__), key=lambda i: i.name):
        if info.name.startswith('_'):
            continue
        try:
            module = importlib.import_module(package + '.' + info.name)
        except Exception:
            errors.append(LoadError(info.name, traceback.format_exc()))
            continue
        spec = getattr(module, 'TOOL', None)
        if spec is not None:
            tools.append(spec)
    tools.sort(key=lambda t: (t.category, t.name))
    return tools, errors
