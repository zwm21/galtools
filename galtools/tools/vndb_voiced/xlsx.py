# -*- coding: utf-8 -*-
"""写 Excel：一个工作簿 = 概览 + 共同出演 + 每人一页。

openpyxl 一律在函数内 import。tests/test_registry.py 断言 discover() 的 errors
为空，模块级 import 第三方库会让没装 openpyxl 的环境连工具列表都列不出来。

表格用 openpyxl 的原生 Table（自带筛选下拉与隔行底色）而不是手动画格式：几百行
的表在 Excel 里能直接按列排序，可读性差别很大。空表不能加 Table——Excel 要求
ref 至少含一行数据，只有表头的 ref 会让文件被判定为损坏。
"""
import itertools
import os
import re
from .model import ROLES, url_for

# 每人一页的列。前 8 列沿用旧脚本的表头与列宽（Title1/Cast1/As1 是日文原名），
# 末尾新增 Role——角色主次是判断「这人在这部里是主役还是路人」的关键信息。
HEADERS = ('Title', 'Released', 'Cast', 'As', 'Note',
           'Title1', 'Cast1', 'As1', 'Role')
WIDTHS = (46, 11, 30, 20, 10, 46, 30, 20, 10)

OVERVIEW_HEADERS = (('声优', '罗马字', 'ID', '作品数', '角色数')
                    + ROLES + ('未标注',))
OVERVIEW_WIDTHS = (26, 22, 8, 8, 8, 8, 9, 8, 9, 9)

COMMON_HEADERS = ('Released', 'Title', 'Title1')
COMMON_WIDTHS = (11, 46, 46)
CAST_WIDTH = 30

TABLE_STYLE = 'TableStyleMedium2'
LINK_COLOR = '0563C1'

# Excel 不接受的工作表名字符，以及 xml 里非法的控制字符。
BAD_SHEET_CHARS = re.compile(r'[\[\]:*?/\\]')
CONTROL_CHARS = re.compile(r'[\x00-\x08\x0b\x0c\x0e-\x1f]')
NON_WORD = re.compile(r'[^\w]+', re.UNICODE)

SHEET_LIMIT = 31         # Excel 的工作表名长度上限
OVERVIEW_SHEET = '概览'
COMMON_SHEET = '共同出演'


# ---------------- 命名 ----------------
def slug(text):
    return NON_WORD.sub('_', text or '').strip('_') or 'unknown'


def workbook_name(staffs):
    """单人沿用旧脚本的 vndb_<Name>_voiced.xlsx，多人叫 共同出演_A_B.xlsx。"""
    names = [slug(s.name or s.sid) for s in staffs]
    if not names:
        return 'vndb_voiced.xlsx'
    if len(names) == 1:
        return 'vndb_%s_voiced.xlsx' % names[0]
    if len(names) == 2:
        return '共同出演_%s_%s.xlsx' % (names[0], names[1])
    return '共同出演_%s_%s等%d人.xlsx' % (names[0], names[1], len(names))


def unique_path(path):
    """已存在时追加序号，与 audio_filter 的 unique_dest 同款：x.xlsx -> x_1.xlsx。

    刻意不覆盖：抓一次要十几秒到几分钟，静默覆盖上一次的结果代价太大。
    """
    if not os.path.exists(path):
        return path
    stem, ext = os.path.splitext(path)
    i = 1
    while True:
        cand = '%s_%d%s' % (stem, i, ext)
        if not os.path.exists(cand):
            return cand
        i += 1


def sheet_title(used, base):
    """截到 31 字符、去掉非法字符、按需追加序号。used 存已用过的小写名。"""
    name = BAD_SHEET_CHARS.sub('_', (base or '').strip())[:SHEET_LIMIT].strip()
    name = name or 'Sheet'
    if name.casefold() not in used:
        used.add(name.casefold())
        return name
    i = 2
    while True:
        suffix = '_%d' % i
        cand = name[:SHEET_LIMIT - len(suffix)] + suffix
        if cand.casefold() not in used:
            used.add(cand.casefold())
            return cand
        i += 1


# ---------------- 单元格 ----------------
def clean(value):
    """去掉 xml 不接受的控制字符（note 里偶有），非字符串原样返回。"""
    if isinstance(value, str):
        return CONTROL_CHARS.sub('', value)
    return value


def _put(ws, row, col, value, url=None, font=None):
    """写一格。以 '=' 开头的字符串强制按文本存，否则 Excel 会当成公式。"""
    cell = ws.cell(row=row, column=col)
    value = clean(value)
    cell.value = value
    if isinstance(value, str) and value.startswith('='):
        cell.data_type = 's'
    if url and value:
        cell.hyperlink = url
        if font is not None:
            cell.font = font
    return cell


def _row(ws, row, values):
    for col, value in enumerate(values, 1):
        _put(ws, row, col, value)


def _finish(ws, widths, rows, tid):
    """列宽 + 冻结表头 + 套原生表格。rows 为 0 时不加表格。"""
    from openpyxl.utils import get_column_letter
    from openpyxl.worksheet.table import Table, TableStyleInfo

    for col, width in enumerate(widths, 1):
        ws.column_dimensions[get_column_letter(col)].width = width
    ws.freeze_panes = 'A2'
    if not rows:
        return
    ref = 'A1:%s%d' % (get_column_letter(len(widths)), rows + 1)
    table = Table(displayName='T%d' % tid, ref=ref)
    table.tableStyleInfo = TableStyleInfo(name=TABLE_STYLE, showRowStripes=True)
    ws.add_table(table)


# ---------------- 各页 ----------------
def _overview(wb, items, used, tid, font):
    ws = wb.create_sheet(sheet_title(used, OVERVIEW_SHEET))
    _row(ws, 1, OVERVIEW_HEADERS)
    for row, item in enumerate(items, 2):
        staff = item.staff
        counts = dict.fromkeys(ROLES, 0)
        other = 0
        for credit in item.credits:
            if credit.role in counts:
                counts[credit.role] += 1
            else:
                other += 1
        _put(ws, row, 1, staff.original or staff.name)
        _put(ws, row, 2, staff.name)
        _put(ws, row, 3, staff.sid, url_for(staff.sid), font)
        _put(ws, row, 4, len(item.vids))
        _put(ws, row, 5, len(item.credits))
        for i, role in enumerate(ROLES):
            _put(ws, row, 6 + i, counts[role])
        _put(ws, row, 6 + len(ROLES), other)
    _finish(ws, OVERVIEW_WIDTHS, len(items), tid)


def _staff_sheet(wb, item, used, tid, font):
    staff = item.staff
    base = '%s %s' % (staff.original or staff.name, staff.sid)
    ws = wb.create_sheet(sheet_title(used, base))
    _row(ws, 1, HEADERS)
    for row, credit in enumerate(item.credits, 2):
        vn_url, char_url = url_for(credit.vid), url_for(credit.cid)
        _put(ws, row, 1, credit.title, vn_url, font)
        _put(ws, row, 2, credit.released)
        _put(ws, row, 3, credit.cast, char_url, font)
        _put(ws, row, 4, credit.alias)
        _put(ws, row, 5, credit.note)
        _put(ws, row, 6, credit.title_ja, vn_url, font)
        _put(ws, row, 7, credit.cast_ja, char_url, font)
        _put(ws, row, 8, credit.alias_ja)
        _put(ws, row, 9, credit.role)
    _finish(ws, WIDTHS, len(item.credits), tid)


def cast_columns(items):
    """共同出演页的人名列头。同名时补 sid——Table 不允许重复表头。"""
    names, seen = [], set()
    for item in items:
        name = item.staff.name or item.staff.sid
        if name in seen:
            name = '%s %s' % (name, item.staff.sid)
        seen.add(name)
        names.append(name)
    return names


def _common_sheet(wb, items, common, used, tid, font):
    ws = wb.create_sheet(sheet_title(used, COMMON_SHEET))
    _row(ws, 1, tuple(COMMON_HEADERS) + tuple(cast_columns(items)))
    for row, entry in enumerate(common, 2):
        vn_url = url_for(entry.vid)
        _put(ws, row, 1, entry.released)
        _put(ws, row, 2, entry.title, vn_url, font)
        _put(ws, row, 3, entry.title_ja, vn_url, font)
        for i, casts in enumerate(entry.casts):
            # 一个人在同一部里配多个角色时，链接指向谁都不对，索性不加。
            link = casts[0][1] if len(casts) == 1 else None
            _put(ws, row, 4 + i, ' / '.join(t for t, _ in casts), link, font)
    _finish(ws, tuple(COMMON_WIDTHS) + (CAST_WIDTH,) * len(items),
            len(common), tid)


# ---------------- 入口 ----------------
def build(items, common, path):
    """写出工作簿，返回 path。common 为空或只有一个人时不建共同出演页。"""
    from openpyxl import Workbook
    from openpyxl.styles import Font

    font = Font(color=LINK_COLOR, underline='single')
    wb = Workbook()
    wb.remove(wb.active)
    used, tid = set(), itertools.count(1)
    _overview(wb, items, used, next(tid), font)
    if len(items) > 1:
        _common_sheet(wb, items, common, used, next(tid), font)
    for item in items:
        _staff_sheet(wb, item, used, next(tid), font)
    wb.save(path)
    return path


def save(items, common, target):
    """target 可以是目录也可以是 .xlsx 路径。返回实际写入的路径。"""
    target = os.path.abspath(target or '.')
    if target.lower().endswith('.xlsx'):
        directory, name = os.path.dirname(target), os.path.basename(target)
    else:
        directory, name = target, workbook_name([i.staff for i in items])
    os.makedirs(directory or '.', exist_ok=True)
    return build(items, common, unique_path(os.path.join(directory, name)))
