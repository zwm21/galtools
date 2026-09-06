# -*- coding: utf-8 -*-
"""本地声优库：一人一个 `s<id>.json`。

只用标准库，也不碰 openpyxl。写库的是「vndb 声优出演表」，读库的是并列的
「声优库」，两个包都要能在没有对方的情况下被测试。

这个模块留在 vndb_voiced 而不是查看器那个包里，因为被序列化的类型定义在
model.py；反过来会让本包去 import 一个 import 本包的包。
"""
import json
import os
import re
import time
from dataclasses import dataclass

from .model import Credit, Staff, StaffCredits

SCHEMA = 1

# 文件名即身份，且要拼进路径，所以 sid 只认 vndb 那一种形状。
SID_RE = re.compile(r'^s\d+$')
NAME_RE = re.compile(r'^s\d+\.json$', re.IGNORECASE)

# 逐条落盘的字段，与 model.Credit 的字段同名。刻意写成常量而不是从
# dataclasses.fields 现推：给 Credit 加字段就该顺手决定库格式要不要跟着变，
# test_credit_fields_cover_the_dataclass 会在忘了这件事时失败。
CREDIT_FIELDS = ('vid', 'title', 'title_ja', 'released', 'cid', 'cast',
                 'cast_ja', 'alias', 'alias_ja', 'note', 'role')


class BadFile(Exception):
    """这份文件用不了，消息已经是人话。"""


@dataclass
class Person:
    """库里的一条：StaffCredits 加上只有文件才知道的两样东西。"""
    item: StaffCredits
    fetched_at: str = ''
    path: str = ''

    @property
    def staff(self):
        return self.item.staff

    @property
    def credits(self):
        return self.item.credits


def path_for(db_dir, sid):
    return os.path.join(db_dir, '%s.json' % sid)


def _text(value):
    """文件是用户能手改的，非字符串一律退回空串——库的读者会对这些值
    casefold、strip，None 进去就是一段栈。"""
    return value if isinstance(value, str) else ''


def to_json(item, fetched_at=None):
    """StaffCredits -> 可 json.dump 的 dict。

    不存 staff.aliases：别名只在抓取期用来把 aid 翻成 As 列，翻好的文本已经落在
    每条 credit 的 alias/alias_ja 里，库的读者没有第二个用途。
    fetched_at 是本机时间、不带时区：它只用来在名册里显示「多久前抓的」。
    """
    staff = item.staff
    return {
        'schema': SCHEMA,
        'sid': staff.sid,
        'name': staff.name,
        'original': staff.original,
        'fetched_at': fetched_at or time.strftime('%Y-%m-%dT%H:%M:%S'),
        'credits': [{key: getattr(credit, key) for key in CREDIT_FIELDS}
                    for credit in item.credits],
    }


def from_json(data):
    """dict -> StaffCredits。认不出的键忽略，缺的键取 Credit 自己的默认值。"""
    staff = Staff(sid=_text(data.get('sid')), name=_text(data.get('name')),
                  original=_text(data.get('original')))
    credits = []
    raw_list = data.get('credits')
    for raw in raw_list if isinstance(raw_list, list) else ():
        if not isinstance(raw, dict):
            continue
        kw = {key: raw[key] for key in CREDIT_FIELDS
              if isinstance(raw.get(key), str)}
        kw.setdefault('vid', '')
        credits.append(Credit(**kw))
    return StaffCredits(staff=staff, credits=credits)


def write_person(db_dir, item, fetched_at=None):
    """整份覆盖写一个人，返回落盘路径。

    先写同目录的 .tmp 再 os.replace：抓一次要几分钟，写到一半断电不该把上一份
    也毁掉。临时文件必须同目录，os.replace 跨盘会失败。

    不合并旧文件里认不出的键：这个目录只有本工具写，合并是为假想的第二个写入者
    设计的；真有格式变动，schema 号足够让新旧双方认出对方。
    """
    sid = item.staff.sid
    if not SID_RE.match(sid or ''):
        raise BadFile('sid 不像 vndb 的人物 id：%r' % (sid,))
    path = path_for(db_dir, sid)
    tmp = path + '.tmp'
    try:
        with open(tmp, 'w', encoding='utf-8') as fp:
            json.dump(to_json(item, fetched_at), fp,
                      ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    except Exception:
        # 不在库里留半份 .tmp：list_files 不认它，只有人眼会被它困惑。
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise
    return path


def read_file(path):
    """读一个文件，返回 Person。读不了就抛 BadFile。"""
    try:
        with open(path, 'r', encoding='utf-8') as fp:
            data = json.load(fp)
    except OSError as e:
        raise BadFile('读不出来：%s' % e)
    except UnicodeDecodeError:
        raise BadFile('不是 UTF-8 文本')
    except ValueError as e:            # JSONDecodeError 是它的子类
        raise BadFile('不是有效的 JSON：%s' % e)
    if not isinstance(data, dict):
        raise BadFile('顶层不是一个对象')
    schema = data.get('schema')
    if isinstance(schema, int) and schema > SCHEMA:
        raise BadFile('这个库比工具新（schema %d，本工具只认到 %d），'
                      '先更新工具再读' % (schema, SCHEMA))
    return Person(item=from_json(data), fetched_at=_text(data.get('fetched_at')),
                  path=path)


def read_person(db_dir, sid):
    return read_file(path_for(db_dir, sid))


def list_files(db_dir):
    """库目录下全部 `s<id>.json`。目录读不了就抛 BadFile。

    只认这一种文件名：库目录里躺着别的东西（导出的 xlsx、写崩的 .tmp）是常态，
    它们不该被当成库的一部分。
    """
    try:
        names = sorted(os.listdir(db_dir))
    except OSError as e:
        raise BadFile('库目录读不出来：%s' % e)
    return [os.path.join(db_dir, name) for name in names if NAME_RE.match(name)]


def read_all(db_dir):
    """读整个库，返回 ([Person], [(文件名, 原因)])。

    一份坏文件只报它自己：库是攒起来的，其中一个人的文件被手改坏了不该让整个
    查看器什么都显示不出来。按罗马字排序，名册和导出因此有同一个稳定顺序。
    """
    people, failures = [], []
    for path in list_files(db_dir):
        try:
            people.append(read_file(path))
        except BadFile as e:
            failures.append((os.path.basename(path), str(e)))
    people.sort(key=lambda p: ((p.staff.name or '').casefold(), p.staff.sid))
    return people, failures
