# -*- coding: utf-8 -*-
"""GUI 控件级检查：整表级错误、结果表格的排序/链接/复制。

现有三个工具都没有整表级规则，只能造一个假 spec 来钉这两条路径。
"""
import os
import sys
import tempfile

os.environ['QT_QPA_PLATFORM'] = 'offscreen'
sys.path.insert(0, os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))))

from PySide6.QtCore import QSettings, Qt
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import QApplication

from galtools.core.spec import NUMBER, TEXT, Field, RunResult, Table, ToolSpec
from galtools.gui.main_window import ToolPage

app = QApplication([])
work = tempfile.mkdtemp(prefix='galtools_formerr_')
settings = QSettings(os.path.join(work, 'x.ini'), QSettings.IniFormat)


def validate(params):
    if (params.get('high') or 0) <= (params.get('low') or 0):
        return [('', '上限必须大于下限')]
    return []


spec = ToolSpec(
    id='fake', name='假工具', category='测试', description='只为验证整表级错误',
    fields=(Field(key='low', kind=NUMBER, label='下限', default=5),
            Field(key='high', kind=NUMBER, label='上限', default=1)),
    run=lambda params, ctx: RunResult(),
    validate=validate,
)

page = ToolPage(spec, settings)
page.show()
app.processEvents()
label = page.form._form_error
print('冲突时：可见=%s 文本=%r 开始按钮=%s'
      % (label.isVisible(), label.text(), page.start_btn.isEnabled()))

page.form._editors['high'].setText('9')
print('改好后：可见=%s 文本=%r 开始按钮=%s'
      % (label.isVisible(), label.text(), page.start_btn.isEnabled()))

# 字段级错误不该串到整表级标签上。
page.form._editors['high'].setText('abc')
print('数字非法：整表标签可见=%s low 旁=%r high 旁=%r 开始按钮=%s'
      % (label.isVisible(), page.form._errors['low'].text(),
         page.form._errors['high'].text(), page.start_btn.isEnabled()))


def cells(col=0):
    return [page.table.item(r, col).text() for r in range(page.table.rowCount())]


t = page.table
print('\n--- 结果表格 ---')
print('开局可见=%s 弹簧 stretch=%d'
      % (t.isVisible(), page._layout.stretch(page._stretch_index)))

page.show_table(Table(columns=('年', '作品'),
                      rows=[('2020', ('乙', 'https://vndb.org/v2')),
                            ('1995', ('甲', 'https://vndb.org/v1'))],
                      title='顺序检查'))
print('可见=%s 标题=%r 弹簧 stretch=%d 表头=%r'
      % (t.isVisible(), page.table_title.text(),
         page._layout.stretch(page._stretch_index),
         [t.horizontalHeaderItem(i).text() for i in range(t.columnCount())]))
print('工具给的顺序保留 =', cells())
print('链接格 文本=%r 链接=%r' % (t.item(1, 1).text(), t.item(1, 1).data(Qt.UserRole)))
t.sortItems(0, Qt.AscendingOrder)
print('点表头升序后 =', cells())

t.selectAll()
page._copy_table()
print('复制出来的 TSV =', repr(QGuiApplication.clipboard().text()))

page.show_table(Table(columns=('数',), rows=[(9,), (10,)]))
t.sortItems(0, Qt.AscendingOrder)
print('数字列升序 =', cells())

page.show_table(Table(columns=('空',), rows=[]))
print('空表：可见=%s 行数=%d 弹簧 stretch=%d'
      % (t.isVisible(), t.rowCount(), page._layout.stretch(page._stretch_index)))
