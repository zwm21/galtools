# -*- coding: utf-8 -*-
"""工具描述协议。

GUI 只认识这里的 dataclass，不认识任何具体工具：表单由 ToolSpec.fields
自动生成。加新工具 = 往 galtools/tools/ 放一个模块并暴露 TOOL。

本模块及 galtools.core 下其余模块只用标准库，不得 import Qt——工具模块
依赖它们，而工具必须在没有 GUI 的情况下也能跑。
"""
from dataclasses import dataclass, field
from typing import Any, Callable

DIR = 'dir'
BOOL = 'bool'
NUMBER = 'number'
TEXT = 'text'


@dataclass(frozen=True)
class Field:
    """一个参数。kind 决定 GUI 用哪种控件，见 gui/form.py 的分派表。"""
    key: str
    kind: str
    label: str
    default: Any = None
    help: str = ''
    placeholder: str = ''
    required: bool = True
    # 改动此字段意味着预览赖以计算的数据集失效。GUI 据此决定是自动重算
    # 预览，还是仅标记过期、等用户点「扫描」——扫描可能很慢。
    rescan: bool = False


@dataclass
class PreviewResult:
    """参数变动后给出的即时反馈。ok=False 时 GUI 禁止启动。"""
    summary: str = ''
    warnings: list[str] = field(default_factory=list)
    ok: bool = True


@dataclass
class RunResult:
    summary: str = ''
    output_paths: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    # 逐项失败，与整体性的 warnings 分开：工具需要把它们落盘成清单。
    failures: list[tuple[str, str]] = field(default_factory=list)


@dataclass(frozen=True)
class ToolSpec:
    id: str
    name: str
    category: str
    description: str
    fields: tuple[Field, ...]
    run: Callable[[dict, Any], RunResult]
    preview: Callable[[dict, Any], PreviewResult] | None = None
    # 跨字段校验，返回 (字段 key, 错误消息) 列表；key 为空串表示整表级错误。
    # 数值范围也走这里而不是 Field 上的常量上下界：「上限必须大于阈值」「必须
    # 为正数」这类规则常量表达不了，两套机制并存只会让规则散落两处。GUI 与
    # CLI 共用同一份以免漂移。
    validate: Callable[[dict], list[tuple[str, str]]] | None = None
