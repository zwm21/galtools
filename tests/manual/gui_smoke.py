# -*- coding: utf-8 -*-
"""GUI 冒烟：离屏起窗口 → 选 vndb 工具 → 扫描 → 开始 → 校验产出与输入历史，
再拿刚存进去的库走一遍「声优库」，并确认只预览也能记住目录。

不联网：把 tests 里的迷你 vndb 直接装到 api 模块的两个注入点上。
QSettings 也改指到临时目录，别把冒烟用的路径写进用户的真实配置。
"""
import os
import sys
import tempfile
import time

os.environ['QT_QPA_PLATFORM'] = 'offscreen'
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, 'tests'))

from PySide6.QtCore import QSettings, Qt
from PySide6.QtWidgets import QApplication

import test_vndb_voiced as T
from galtools.tools.vndb_voiced import api

fake = T.FakeApi(T.vndb())
api._open, api._sleep, api.time = fake.open, fake.sleep, fake.clock

work = tempfile.mkdtemp(prefix='galtools_smoke_')

from galtools.gui import main_window as mw
from galtools.gui.main_window import MainWindow


class TempSettings(QSettings):
    """PySide6 里 QSettings(org, app) 不看 defaultFormat()，照样写注册表。
    只能把 main_window 用的那个名字换掉，才不会污染用户的真实配置。"""

    def __init__(self, *_args):
        super().__init__(os.path.join(work, 'smoke.ini'), QSettings.IniFormat)


mw.QSettings = TempSettings

app = QApplication([])


def pump(seconds=8.0, until=None):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        app.processEvents()
        if until is not None and until():
            return True
        time.sleep(0.01)
    return until is None


def select(win, tool_id):
    """照用户的路径走：点树上的那一项，让 stack 自己跟着切。"""
    tree = win.tree
    for i in range(tree.topLevelItemCount()):
        top = tree.topLevelItem(i)
        for j in range(top.childCount()):
            item = top.child(j)
            if win.stack.widget(item.data(0, Qt.UserRole)) is win.pages[tool_id]:
                tree.setCurrentItem(item)
                return
    raise AssertionError('树里没有 %s' % tool_id)


win = MainWindow()
win.show()

labels = []
for i in range(win.tree.topLevelItemCount()):
    top = win.tree.topLevelItem(i)
    labels.append('%s: %s' % (top.text(0),
                              ', '.join(top.child(j).text(0)
                                        for j in range(top.childCount()))))
print('树 =', ' | '.join(labels))

select(win, 'vndb_voiced')
page = win.pages['vndb_voiced']
print('当前页 =', win.stack.currentWidget().spec.id)
print('开局：stale=%s 开始按钮=%s 扫描按钮=%r 预览框=%r'
      % (page.stale, page.start_btn.isEnabled(), page.scan_btn.text(),
         page.preview_box.toPlainText()))
print('缺必填 =', page.form.missing_required_keys())

db = os.path.join(work, 'db')
out = os.path.join(work, 'out')
os.makedirs(db)
os.makedirs(out)

page.form._editors['staff'].setCurrentText('s1, s2')
page.form._editors['db_dir'].setCurrentText(db)
# 默认存库、不导出。这一轮把两条都打开：产出的 xlsx 与库文件都要验。
page.form._editors['export'].setChecked(True)
page.form._editors['out_dir'].setCurrentText(out)
print('改完参数：stale=%s 开始按钮=%s' % (page.stale, page.start_btn.isEnabled()))
print('两个开关都关掉时的整表级错误 =',
      dict(page.spec.validate(dict(page.form.values(),
                                   save_db=False, export=False))).get(''))

page.scan_btn.click()
pump(until=lambda: page.preview_ok is not None)
print('预览 ok=%s 开始按钮=%s' % (page.preview_ok, page.start_btn.isEnabled()))
print('预览框 ↓\n' + page.preview_box.toPlainText())


def table_rows(page):
    t = page.table
    return [[t.item(r, c).text() for c in range(t.columnCount())]
            for r in range(t.rowCount())]


def headers(page):
    t = page.table
    return [t.horizontalHeaderItem(c).text() for c in range(t.columnCount())]


print('预览表格 可见=%s 标题=%r 表头=%r'
      % (page.table.isVisible(), page.table_title.text(), headers(page)))
for row in table_rows(page):
    print('   ', row)
print('ID 格链接 =', page.table.item(0, 2).data(Qt.UserRole))

done = []
win.bridge.run_finished.connect(lambda _token, result: done.append(result))
page.start_btn.click()
pump(until=lambda: done)
if not done:
    print('!! run 没有回来')
    sys.exit(1)
app.processEvents()
result = done[0]
print('产出 =', result.output_paths)
print('存在 =', all(os.path.exists(p) for p in result.output_paths))
print('库里 =', sorted(os.listdir(db)))
print('失败 =', result.failures, '警告 =', result.warnings)
print('跑完：stale=%s 开始按钮=%s 状态=%r'
      % (page.stale, page.start_btn.isEnabled(), win.status.text()))
print('日志尾 ↓\n' + '\n'.join(win.log.toPlainText().splitlines()[-5:]))
print('请求数 =', len(fake.calls))
print('结果表格 可见=%s 标题=%r 表头=%r'
      % (page.table.isVisible(), page.table_title.text(), headers(page)))
for row in table_rows(page):
    print('   ', row)
print('Title 格链接 =', page.table.item(0, 1).data(Qt.UserRole))

# 改参数 → 表里说的是上一次的事，该收起来。
page.form._editors['staff'].setCurrentText('s1')
print('改参数后：表格可见=%s 预览框=%r'
      % (page.table.isVisible(), page.preview_box.toPlainText()))
win.close()

# 再开一次：staff 与 out_dir 都该记住，且是可下拉的。
again = MainWindow()
select(again, 'vndb_voiced')
page2 = again.pages['vndb_voiced']
staff_edit = page2.form._editors['staff']
print('二次开窗 staff=%r 候选=%r 可编辑=%s'
      % (staff_edit.currentText(),
         [staff_edit.itemText(i) for i in range(staff_edit.count())],
         staff_edit.isEditable()))
print('二次开窗 out_dir=%r' % page2.form._editors['out_dir'].currentText())
print('二次开窗 db_dir=%r' % page2.form._editors['db_dir'].currentText())

# 刚才那一轮存进去的库，换「声优库」离屏翻一遍：只看不导，也该把库目录记住。
select(again, 'seiyuu_db')
viewer = again.pages['seiyuu_db']
viewer.form._editors['db_dir'].setCurrentText(db)
viewer.previewRequested.emit()
pump(until=lambda: viewer.preview_ok is not None)
print('声优库 预览 ok=%s（没填导出目录就该是 False）' % viewer.preview_ok)
print('声优库 预览框 ↓\n' + viewer.preview_box.toPlainText())
print('声优库 表格 可见=%s 标题=%r 表头=%r'
      % (viewer.table.isVisible(), viewer.table_title.text(), headers(viewer)))
for row in table_rows(viewer):
    print('   ', row)

# 另外三个工具不给表格：切过去不该炸，也不该留着别人的表。
for tool_id in ('mjo_text', 'audio_filter', 'file_collect'):
    select(again, tool_id)
    pump(1.0)
    other = again.pages[tool_id]
    print('%s：扫描按钮=%r 表格可见=%s 预览框=%r'
          % (tool_id, other.scan_btn.text(), other.table.isVisible(),
             other.preview_box.toPlainText()[:40]))
again.close()

# 第三次开窗：声优库那边只预览、没点「开始」，库目录照样该记住
# （remember_dirs 走的是预览成功那条路）。
third = MainWindow()
print('三次开窗 声优库 db_dir=%r'
      % third.pages['seiyuu_db'].form._editors['db_dir'].currentText())
third.close()
