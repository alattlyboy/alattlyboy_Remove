# -*- coding: utf-8 -*-
"""
Tkinter 数据库管理工具（类 Navicat 完整版）
支持 MySQL、SQLite 与 Redis。


功能清单：
  - 连接管理、对象树（连接名 -> 数据库 -> 表/视图）
  - 右键菜单：刷新表 / 设计表 / 新建表 / 新建数据库 / 删除 / 清空 / 设置字符集 / 导出 / 导入 SQL
  - 数据表格展示与 GUI 增删改（新增行 / 编辑行 / 删除行 / 保存）
  - 设计表：字段（类型/长度/主键/自增/可空/默认值/注释）、索引、外键
  - 结构页：显示 CREATE TABLE 与字段列表（含主键/可空/默认/注释）
  - SQL 命令执行增删改查，执行过的 SQL 自动保存历史（脚本同目录 sql_history.json，
    支持查看历史 / 双击回填 / 清空历史，重复命令去重）
  - 字符集设置：MySQL 数据库/表字符集（ALTER DATABASE ... CHARACTER SET /
    ALTER TABLE ... CONVERT TO CHARACTER SET），支持 utf8mb4 / utf8 / gbk / latin1 等；
    SQLite 不支持并给出提示
  - 数据库导出：可导出当前数据库或当前表，范围可选“数据+结构 / 仅结构 / 仅数据”，
    生成包含 CREATE TABLE 与 INSERT 语句的 SQL 文件（MySQL 与 SQLite 均支持）
  - 导出整个数据库：菜单栏「数据库」菜单（连接菜单旁）与工具栏「导出数据库」按钮，
    一键导出完整库（MySQL 追加 CREATE DATABASE IF NOT EXISTS 与 USE 语句、视图结构，
    含所有表结构与数据，范围可选），未选中数据库时弹窗选择
  - SQL 文件导入：选择 .sql 文件执行其中建表与数据语句（支持多语句批量执行）
  - 导入 CSV（导入后给出结果提示）、导出 CSV / SQL / JSON


"""

import tkinter as tk
from tkinter import ttk, messagebox, filedialog, simpledialog
import sqlite3
import threading
import queue
import csv
import json
import os

try:
    import pymysql
    HAS_MYSQL = True
except ImportError:
    HAS_MYSQL = False

try:
    import redis
    HAS_REDIS = True
except ImportError:
    HAS_REDIS = False

MYSQL_TYPES = ['INT', 'BIGINT', 'SMALLINT', 'TINYINT', 'VARCHAR', 'CHAR', 'TEXT',
               'LONGTEXT', 'DECIMAL', 'FLOAT', 'DOUBLE', 'DATETIME', 'DATE',
               'TIMESTAMP', 'TIME', 'BOOLEAN', 'JSON', 'BLOB']
SQLITE_TYPES = ['INTEGER', 'TEXT', 'REAL', 'BLOB', 'NUMERIC', 'VARCHAR', 'DATETIME']

# 常见字符集
CHARSETS = ['utf8mb4', 'utf8', 'utf8mb3', 'gbk', 'gb2312', 'latin1',
            'big5', 'ascii', 'utf16', 'utf32']

# SQL 历史记录文件（脚本同目录）
SQL_HISTORY_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'sql_history.json')
SQL_HISTORY_MAX = 200

# Redis 命令历史记录文件（脚本同目录，独立于 SQL 历史）
REDIS_HISTORY_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)),          'redis_cmd_history.json')
REDIS_HISTORY_MAX = 200


def split_sql_statements(script):
    """按分号分割 SQL 脚本，忽略字符串（单引号/双引号/反引号）与注释内的分号。

    支持 -- 、# 行注释与 /* */ 块注释。
    """
    statements = []
    current = []
    i = 0
    n = len(script)
    in_single = False
    in_double = False
    in_backtick = False
    in_line_comment = False
    in_block_comment = False
    while i < n:
        ch = script[i]
        nxt = script[i + 1] if i + 1 < n else ''
        if in_line_comment:
            current.append(ch)
            if ch == '\n':
                in_line_comment = False
            i += 1
            continue
        if in_block_comment:
            current.append(ch)
            if ch == '*' and nxt == '/':
                current.append(nxt)
                in_block_comment = False
                i += 2
                continue
            i += 1
            continue
        if not in_single and not in_double and not in_backtick:
            if ch == '-' and nxt == '-':
                in_line_comment = True
                current.append(ch)
                i += 1
                continue
            if ch == '#':
                in_line_comment = True
                current.append(ch)
                i += 1
                continue
            if ch == '/' and nxt == '*':
                in_block_comment = True
                current.append(ch)
                current.append(nxt)
                i += 2
                continue
        if in_single:
            if ch == "'":
                if nxt == "'":  # 转义单引号 ''
                    current.append(ch)
                    current.append(nxt)
                    i += 2
                    continue
                in_single = False
            current.append(ch)
            i += 1
            continue
        if in_double:
            if ch == '"':
                if nxt == '"':
                    current.append(ch)
                    current.append(nxt)
                    i += 2
                    continue
                in_double = False
            current.append(ch)
            i += 1
            continue
        if in_backtick:
            if ch == '`':
                in_backtick = False
            current.append(ch)
            i += 1
            continue
        if ch == "'":
            in_single = True
            current.append(ch)
            i += 1
            continue
        if ch == '"':
            in_double = True
            current.append(ch)
            i += 1
            continue
        if ch == '`':
            in_backtick = True
            current.append(ch)
            i += 1
            continue
        if ch == ';':
            stmt = ''.join(current).strip()
            if stmt:
                statements.append(stmt)
            current = []
            i += 1
            continue
        current.append(ch)
        i += 1
    stmt = ''.join(current).strip()
    if stmt:
        statements.append(stmt)
    return statements


# ---------------------------------------------------------------------------
# 数据库连接封装
# ---------------------------------------------------------------------------
class DBConnection:
    def __init__(self, db_type, params, name):
        self.db_type = db_type
        self.params = params
        self.name = name
        self.conn = None

    @property
    def ph(self):
        """占位符：MySQL 用 %s，SQLite 用 ?"""
        return '%s' if self.db_type == 'mysql' else '?'

    @staticmethod
    def q(ident):
        return f'`{ident}`'

    def tbl(self, table, db=None):
        if self.db_type == 'mysql' and db:
            return f'`{db}`.`{table}`'
        return f'`{table}`'

    # ---- 连接 ----
    def connect(self):
        if self.db_type == 'mysql':
            if not HAS_MYSQL:
                raise RuntimeError("未安装 pymysql，请先执行: pip install pymysql")
            self.conn = pymysql.connect(
                host=self.params.get('host', '127.0.0.1'),
                port=int(self.params.get('port', 3306)),
                user=self.params.get('user', 'root'),
                password=self.params.get('password', ''),
                database=self.params.get('database', ''),
                charset='utf8mb4',
                cursorclass=pymysql.cursors.DictCursor,
                autocommit=True)
        else:
            self.conn = sqlite3.connect(self.params.get('path', '') or ':memory:')
            self.conn.row_factory = sqlite3.Row

    def close(self):
        if self.conn:
            try:
                self.conn.close()
            except Exception:
                pass
            self.conn = None

    def is_open(self):
        """连接是否已打开。"""
        return self.conn is not None

    # ---- 底层执行 ----
    def _query(self, sql, params=None):
        cur = self.conn.cursor()
        cur.execute(sql, params or ())
        rows = cur.fetchall()
        cur.close()
        return rows

    def _execute(self, sql, params=None):
        cur = self.conn.cursor()
        cur.execute(sql, params or ())
        affected = cur.rowcount
        cur.close()
        if self.db_type == 'sqlite':
            self.conn.commit()
        return affected

    def execute_script(self, sql):
        """执行多条语句（分号分隔）。"""
        for stmt in split_sql_statements(sql):
            if stmt:
                self._execute(stmt)

    def execute_query(self, sql):
        """执行单条查询/非查询语句，返回 (columns, rows, affected)。"""
        cur = self.conn.cursor()
        cur.execute(sql)
        if sql.strip().lower().startswith(('select', 'show', 'describe', 'desc', 'pragma', 'explain')):
            cols = [d[0] for d in cur.description] if cur.description else []
            rows = cur.fetchall()
            cur.close()
            return cols, rows, 0
        affected = cur.rowcount
        cur.close()
        if self.db_type == 'sqlite':
            self.conn.commit()
        return [], [], affected

    # ---- 元数据 ----
    def get_databases(self):
        if self.db_type == 'mysql':
            return [list(r.values())[0] for r in self._query("SHOW DATABASES")]
        return ['main']

    def get_tables(self, database=None):
        if self.db_type == 'mysql':
            rows = self._query(
                "SELECT TABLE_NAME FROM information_schema.TABLES "
                "WHERE TABLE_SCHEMA=%s AND TABLE_TYPE='BASE TABLE' ORDER BY TABLE_NAME",
                (database,))
            return [r['TABLE_NAME'] for r in rows]
        rows = self._query("SELECT name FROM sqlite_master WHERE type='table' "
                           "AND name NOT LIKE 'sqlite_%' ORDER BY name")
        return [r['name'] for r in rows]

    def get_views(self, database=None):
        if self.db_type == 'mysql':
            rows = self._query(
                "SELECT TABLE_NAME FROM information_schema.TABLES "
                "WHERE TABLE_SCHEMA=%s AND TABLE_TYPE='VIEW' ORDER BY TABLE_NAME",
                (database,))
            return [r['TABLE_NAME'] for r in rows]
        return [r['name'] for r in self._query(
            "SELECT name FROM sqlite_master WHERE type='view' ORDER BY name")]

    def get_columns(self, table, database=None):
        """返回 dict 列表：[{name,type,pk,nullable,default,auto,comment}]"""
        if self.db_type == 'mysql':
            rows = self._query(
                "SELECT COLUMN_NAME, COLUMN_TYPE, DATA_TYPE, COLUMN_KEY, "
                "IS_NULLABLE, COLUMN_DEFAULT, EXTRA, COLUMN_COMMENT "
                "FROM information_schema.COLUMNS "
                "WHERE TABLE_SCHEMA=%s AND TABLE_NAME=%s ORDER BY ORDINAL_POSITION",
                (database, table))
            return [{
                'name': r['COLUMN_NAME'], 'type': r['COLUMN_TYPE'],
                'pk': r['COLUMN_KEY'] == 'PRI',
                'nullable': r['IS_NULLABLE'] == 'YES',
                'default': r['COLUMN_DEFAULT'],
                'auto': 'auto_increment' in (r['EXTRA'] or '').lower(),
                'comment': r['COLUMN_COMMENT'],
            } for r in rows]
        rows = self._query(f"PRAGMA table_info('{table}')")
        return [{
            'name': r['name'], 'type': r['type'], 'pk': r['pk'] > 0,
            'nullable': not r['notnull'], 'default': r['dflt_value'],
            'auto': False, 'comment': '',
        } for r in rows]

    def get_indexes(self, table, database=None):
        if self.db_type == 'mysql':
            rows = self._query(
                "SELECT INDEX_NAME, COLUMN_NAME, NON_UNIQUE "
                "FROM information_schema.STATISTICS "
                "WHERE TABLE_SCHEMA=%s AND TABLE_NAME=%s "
                "AND INDEX_NAME != 'PRIMARY' ORDER BY INDEX_NAME, SEQ_IN_INDEX",
                (database, table))
            idx = {}
            for r in rows:
                idx.setdefault(r['INDEX_NAME'], []).append(r['COLUMN_NAME'])
            return [(name, ','.join(cols)) for name, cols in idx.items()]
        rows = self._query(f"PRAGMA index_list('{table}')")
        result = []
        for r in rows:
            cols = [c['name'] for c in self._query(f"PRAGMA index_info('{r['name']}')")]
            result.append((r['name'], ','.join(cols)))
        return result

    def get_foreign_keys(self, table, database=None):
        if self.db_type == 'mysql':
            rows = self._query(
                "SELECT CONSTRAINT_NAME, COLUMN_NAME, REFERENCED_TABLE_NAME, "
                "REFERENCED_COLUMN_NAME FROM information_schema.KEY_COLUMN_USAGE "
                "WHERE TABLE_SCHEMA=%s AND TABLE_NAME=%s AND REFERENCED_TABLE_NAME IS NOT NULL",
                (database, table))
            return [(r['CONSTRAINT_NAME'], r['COLUMN_NAME'],
                     r['REFERENCED_TABLE_NAME'], r['REFERENCED_COLUMN_NAME']) for r in rows]
        rows = self._query(f"PRAGMA foreign_key_list('{table}')")
        return [(f"fk_{r['id']}", r['from'], r['table'], r['to']) for r in rows]

    def get_primary_key(self, table, database=None):
        return [c['name'] for c in self.get_columns(table, database) if c['pk']]

    def get_create_sql(self, table, database=None):
        if self.db_type == 'mysql':
            rows = self._query(f"SHOW CREATE TABLE `{database}`.`{table}`")
            return rows[0].get('Create Table', '') if rows else ''
        rows = self._query("SELECT sql FROM sqlite_master WHERE name=? AND type='table'", (table,))
        return rows[0]['sql'] if rows else ''

    def get_row_count(self, table, database=None):
        """返回表中总行数。"""
        rows = self._query(f"SELECT COUNT(*) AS c FROM {self.tbl(table, database)}")
        return rows[0]['c'] if rows else 0

    # ---- 数据库/表 DDL ----
    def create_database(self, name):
        if self.db_type == 'mysql':
            self._execute(f"CREATE DATABASE `{name}` DEFAULT CHARACTER SET utf8mb4")
        else:
            raise RuntimeError("SQLite 不支持创建数据库")

    def drop_database(self, name):
        if self.db_type == 'mysql':
            self._execute(f"DROP DATABASE `{name}`")
        else:
            raise RuntimeError("SQLite 不支持删除数据库")

    def drop_table(self, table, database=None):
        self._execute(f"DROP TABLE {self.tbl(table, database)}")

    def truncate_table(self, table, database=None):
        if self.db_type == 'mysql':
            self._execute(f"TRUNCATE TABLE {self.tbl(table, database)}")
        else:
            self._execute(f"DELETE FROM {self.tbl(table, database)}")

    # ---- 字符集设置 ----
    def set_database_charset(self, database, charset):
        """设置数据库字符集（仅 MySQL）。"""
        if self.db_type != 'mysql':
            raise RuntimeError("SQLite 不支持字符集设置，仅 MySQL 可设置")
        self._execute(f"ALTER DATABASE `{database}` CHARACTER SET {charset}")

    def set_table_charset(self, table, database, charset):
        """设置表字符集（仅 MySQL）。"""
        if self.db_type != 'mysql':
            raise RuntimeError("SQLite 不支持字符集设置，仅 MySQL 可设置")
        self._execute(f"ALTER TABLE {self.tbl(table, database)} "
                      f"CONVERT TO CHARACTER SET {charset}")

    # ---- 数据增删改 ----
    def fetch_page(self, table, columns, db=None, limit=200, offset=0):
        cols_sql = ', '.join(self.q(c) for c in columns)
        sql = (f"SELECT {cols_sql} FROM {self.tbl(table, db)} "
               f"LIMIT {int(limit)} OFFSET {int(offset)}")
        return self._query(sql)

    def insert_row(self, table, data, db=None):
        cols = list(data.keys())
        phs = ', '.join([self.ph] * len(cols))
        sql = (f"INSERT INTO {self.tbl(table, db)} "
               f"({', '.join(self.q(c) for c in cols)}) VALUES ({phs})")
        self._execute(sql, list(data.values()))

    def update_row(self, table, data, old_row, db=None):
        cols = list(data.keys())
        set_sql = ', '.join(f"{self.q(c)}={self.ph}" for c in cols)
        where_cols = self.get_primary_key(table, db) or list(old_row.keys())
        where_sql = ' AND '.join(f"{self.q(c)}={self.ph}" for c in where_cols)
        sql = f"UPDATE {self.tbl(table, db)} SET {set_sql} WHERE {where_sql}"
        self._execute(sql, list(data.values()) + [old_row[c] for c in where_cols])

    def delete_row(self, table, old_row, db=None):
        where_cols = self.get_primary_key(table, db) or list(old_row.keys())
        where_sql = ' AND '.join(f"{self.q(c)}={self.ph}" for c in where_cols)
        sql = f"DELETE FROM {self.tbl(table, db)} WHERE {where_sql}"
        self._execute(sql, [old_row[c] for c in where_cols])

    # ---- 导出 ----
    def get_create_database_sql(self, database):
        """获取 MySQL 建库语句（含字符集），SQLite 返回空串。

        SHOW CREATE DATABASE 语句形如
        CREATE DATABASE `db` /*!40100 DEFAULT CHARACTER SET utf8mb4 */，
        在此基础上前插 IF NOT EXISTS。
        """
        if self.db_type != 'mysql':
            return ''
        try:
            rows = self._query(f"SHOW CREATE DATABASE `{database}`")
        except Exception:
            return ''
        if not rows:
            return ''
        row = list(rows[0].values())
        sql = str(row[0]) if row else ''
        if sql.upper().startswith('CREATE DATABASE'):
            sql = sql.replace('CREATE DATABASE', 'CREATE DATABASE IF NOT EXISTS', 1)
        return sql

    def get_create_view_sql(self, view, database=None):
        """获取视图定义 SQL；MySQL 取 SHOW CREATE VIEW 的 Create View 列。"""
        if self.db_type == 'mysql':
            rows = self._query(f"SHOW CREATE VIEW {self.tbl(view, database)}")
            if rows:
                row = list(rows[0].values())
                return str(row[1]) if len(row) > 1 else ''
            return ''
        rows = self._query("SELECT sql FROM sqlite_master "
                           "WHERE name=? AND type='view'", (view,))
        return rows[0]['sql'] if rows else ''

    def dump_database(self, database=None, include_structure=True, include_data=True,
                      tables=None):
        """导出数据库（或指定表集合）为 SQL 文本。

        include_structure: 是否包含 CREATE TABLE / CREATE VIEW 等结构语句
        include_data: 是否包含 INSERT 数据语句
        tables: 限定表集合；None 表示全部表
        MySQL 模式下头部追加 CREATE DATABASE IF NOT EXISTS（含字符集）与 USE 语句。
        """
        if tables is None:
            tables = self.get_tables(database)
        views = self.get_views(database) if include_structure else []
        lines = [
            "-- ============================================",
            "-- 数据库导出（Tkinter 数据库管理工具）",
            f"-- 连接: {self.name}",
            f"-- 数据库: {database or 'main'}",
            f"-- 表数量: {len(tables)}",
            f"-- 导出范围: {'数据+结构' if include_structure and include_data else ('仅结构' if include_structure else '仅数据')}",
            "-- ============================================",
            "",
        ]
        if self.db_type == 'mysql' and database:
            create_db = self.get_create_database_sql(database)
            if create_db:
                lines.append(create_db.rstrip().rstrip(';') + ';')
                lines.append(f"USE `{database}`;")
                lines.append("")
        for t in tables:
            if include_structure:
                create_sql = self.get_create_sql(t, database)
                if create_sql:
                    lines.append(f"-- 表结构: {t}")
                    lines.append(create_sql.rstrip().rstrip(';') + ';')
                    lines.append("")
            if include_data:
                lines.append(f"-- 表数据: {t}")
                lines.extend(self._dump_table_data(t, database))
                lines.append("")
        for v in views:
            create_view = self.get_create_view_sql(v, database)
            if create_view:
                lines.append(f"-- 视图结构: {v}")
                lines.append(create_view.rstrip().rstrip(';') + ';')
                lines.append("")
        return '\n'.join(lines)

    def _dump_table_data(self, table, database=None, batch=1000):
        """生成表的全部 INSERT 语句（分页读取，避免一次拉取过大）。"""
        cols = [c['name'] for c in self.get_columns(table, database)]
        if not cols:
            return []
        inserts = []
        offset = 0
        while True:
            rows = self.fetch_page(table, cols, database, batch, offset)
            if not rows:
                break
            for r in rows:
                vals = [r[c] if isinstance(r, dict) else r[i] for i, c in enumerate(cols)]
                vals_str = ', '.join(
                    'NULL' if v is None else "'" + str(v).replace("'", "''") + "'"
                    for v in vals)
                inserts.append(
                    "INSERT INTO %s (%s) VALUES (%s);" % (
                        self.tbl(table, database),
                        ', '.join(self.q(c) for c in cols),
                        vals_str))
            offset += len(rows)
            if len(rows) < batch:
                break
        return inserts

    # ---- 导入 ----
    def execute_sql_file(self, path):
        """执行 SQL 文件中的全部语句（支持建表与数据语句、多语句批量执行）。

        返回成功执行的语句条数。
        """
        with open(path, 'r', encoding='utf-8-sig') as f:
            script = f.read()
        statements = split_sql_statements(script)
        executed = 0
        for stmt in statements:
            self._execute(stmt)
            executed += 1
        return executed


# ---------------------------------------------------------------------------
# Redis 辅助函数
# ---------------------------------------------------------------------------
def _redis_tokenize(cmd):
    """将 Redis 命令字符串拆分为参数列表（支持引号包裹）。"""
    import shlex
    try:
        return shlex.split(cmd)
    except Exception:
        return cmd.split()


def _format_redis_result(result, indent=0):
    """将 Redis 命令返回结果格式化为可读文本（bytes 自动解码）。"""
    pad = '  ' * indent
    if result is None:
        return 'OK\n'
    if isinstance(result, bool):
        return f'{result}\n'
    if isinstance(result, (int, float)):
        return f'{result}\n'
    if isinstance(result, (bytes, str)):
        s = result.decode('utf-8', errors='replace') if isinstance(result, bytes) else str(result)
        return f'{s}\n'
    if isinstance(result, dict):
        lines = []
        for k, v in result.items():
            kk = k.decode('utf-8', errors='replace') if isinstance(k, bytes) else str(k)
            lines.append(f'{pad}{kk}: {_format_redis_result(v, indent + 1)}'.rstrip('\n'))
        return '\n'.join(lines) + '\n'
    if isinstance(result, (list, tuple)):
        lines = []
        for v in result:
            lines.append(f'{pad}{_format_redis_result(v, indent + 1)}'.rstrip('\n'))
        return '\n'.join(lines) + '\n'
    return f'{result}\n'


# ---------------------------------------------------------------------------
# Redis 连接封装
# ---------------------------------------------------------------------------
class RedisConnection:
    """Redis 远程连接封装（host / port / password / db 序号）。"""

    MAX_KEYS = 10000    # 树/搜索最多返回的 key 数
    PAGE_SIZE = 200     # 数据页默认分页大小

    def __init__(self, params, name):
        self.db_type = 'redis'
        self.params = params
        self.name = name
        self.conn = None

    # ---- 连接 ----
    def connect(self):
        if not HAS_REDIS:
            raise RuntimeError("未安装 redis 库，请先执行: pip install redis")
        self.conn = redis.Redis(
            host=self.params.get('host', '127.0.0.1'),
            port=int(self.params.get('port', 6379)),
            password=self.params.get('password', '') or None,
            db=int(self.params.get('db', 0) or 0),
            decode_responses=True)
        self.conn.ping()  # 触发真实连接校验

    def close(self):
        if self.conn:
            try:
                self.conn.close()
            except Exception:
                pass
            self.conn = None

    def is_open(self):
        return self.conn is not None

    def select_db(self, db):
        self.conn.execute_command('SELECT', int(db))

    # ---- 逻辑库 ----
    def get_databases(self):
        return ['db%d' % i for i in range(16)]

    # ---- key 列表 ----
    def scan_keys(self, db, pattern='*', max_keys=None):
        """SCAN 收集匹配 pattern 的 key，返回 (keys, total)。
        keys 最多 max_keys（默认 MAX_KEYS），total 为 SCAN 全量计数。"""
        pattern = pattern or '*'
        max_keys = max_keys or self.MAX_KEYS
        self.select_db(db)
        keys = []
        total = 0
        cursor = 0
        while True:
            cursor, batch = self.conn.scan(cursor=cursor, match=pattern, count=500)
            total += len(batch)
            keys.extend(batch)
            if not cursor or len(keys) >= max_keys:
                break
        return keys[:max_keys], total

    # ---- key 类型 ----
    def get_key_type(self, db, key):
        self.select_db(db)
        t = self.conn.type(key)
        return t.decode() if isinstance(t, bytes) else str(t)

    # ---- 数据读取（分页） ----
    def get_string(self, db, key):
        self.select_db(db)
        return self.conn.get(key)

    def get_list_page(self, db, key, offset, size):
        self.select_db(db)
        start = offset
        end = offset + size - 1
        items = self.conn.lrange(key, start, end)
        total = self.conn.llen(key)
        return items, total

    def get_hash_page(self, db, key, cursor, size):
        self.select_db(db)
        ncursor, data = self.conn.hscan(key, cursor=cursor, count=size)
        return int(ncursor), data, self.conn.hlen(key)

    def get_set_page(self, db, key, cursor, size):
        self.select_db(db)
        ncursor, data = self.conn.sscan(key, cursor=cursor, count=size)
        return int(ncursor), data, self.conn.scard(key)

    def get_zset_page(self, db, key, cursor, size):
        self.select_db(db)
        ncursor, data = self.conn.zscan(key, cursor=cursor, count=size)
        return int(ncursor), data, self.conn.zcard(key)

    # ---- 数据写入 ----
    def set_string(self, db, key, value):
        self.select_db(db)
        self.conn.set(key, value)

    def add_list_item(self, db, key, value):
        self.select_db(db)
        self.conn.rpush(key, value)

    def del_list_items(self, db, key, value):
        self.select_db(db)
        self.conn.lrem(key, 0, value)

    def add_hash_field(self, db, key, field, value):
        self.select_db(db)
        self.conn.hset(key, field, value)

    def del_hash_field(self, db, key, field):
        self.select_db(db)
        self.conn.hdel(key, field)

    def add_set_member(self, db, key, member):
        self.select_db(db)
        self.conn.sadd(key, member)

    def del_set_member(self, db, key, member):
        self.select_db(db)
        self.conn.srem(key, member)

    def add_zset_member(self, db, key, member, score):
        self.select_db(db)
        self.conn.zadd(key, {member: float(score)})

    def del_zset_member(self, db, key, member):
        self.select_db(db)
        self.conn.zrem(key, member)

    # ---- TTL / 删除 ----
    def get_ttl(self, db, key):
        self.select_db(db)
        return self.conn.ttl(key)  # -1 永不过期，-2 key 不存在

    def set_ttl(self, db, key, seconds):
        self.select_db(db)
        if seconds is None or seconds < 0:
            self.conn.persist(key)
        else:
            self.conn.expire(key, int(seconds))

    def delete_key(self, db, key):
        self.select_db(db)
        self.conn.delete(key)

    # ---- 结构元信息 ----
    def get_key_info(self, db, key):
        """获取 key 的结构元信息：type / encoding / ttl / 元素个数 / 内存占用。

        各命令兼容性失败（如旧版 Redis 不支持 MEMORY USAGE / OBJECT ENCODING）
        时对应字段返回 None，不中断整体读取。
        """
        self.select_db(db)
        info = {'key': key, 'type': None, 'encoding': None,
                'ttl': None, 'size': None, 'memory': None}
        try:
            t = self.conn.type(key)
            info['type'] = t.decode() if isinstance(t, bytes) else str(t)
        except Exception:
            pass
        try:
            enc = self.conn.execute_command('OBJECT', 'ENCODING', key)
            info['encoding'] = enc.decode() if isinstance(enc, bytes) else str(enc)
        except Exception:
            pass
        try:
            info['ttl'] = self.conn.ttl(key)
        except Exception:
            pass
        ktype = info['type']
        try:
            if ktype == 'string':
                info['size'] = self.conn.strlen(key)
            elif ktype == 'list':
                info['size'] = self.conn.llen(key)
            elif ktype == 'hash':
                info['size'] = self.conn.hlen(key)
            elif ktype == 'set':
                info['size'] = self.conn.scard(key)
            elif ktype == 'zset':
                info['size'] = self.conn.zcard(key)
        except Exception:
            pass
        try:
            mem = self.conn.execute_command('MEMORY', 'USAGE', key)
            info['memory'] = int(mem) if mem is not None else None
        except Exception:
            pass
        return info

    # ---- 任意命令 ----
    def execute_command(self, db, tokens):
        self.select_db(db)
        return self.conn.execute_command(*tokens)


# ---------------------------------------------------------------------------
# 连接对话框
# ---------------------------------------------------------------------------
class ConnectDialog(tk.Toplevel):
    def __init__(self, master, db_type, on_ok):
        super().__init__(master)
        self.db_type = db_type
        self.on_ok = on_ok
        self.title(f"新建 {db_type.upper()} 连接")
        self.geometry("420x340")
        self.resizable(False, False)
        self._build()
        self.transient(master)
        self.grab_set()

    def _build(self):
        frame = ttk.Frame(self, padding=16)
        frame.pack(fill='both', expand=True)
        self.vars = {}
        row = 0
        fields = ([('连接名', 'name', '本地 MySQL'), ('主机', 'host', '127.0.0.1'),
                   ('端口', 'port', '3306'), ('用户名', 'user', 'root'),
                   ('密码', 'password', ''), ('数据库', 'database', '')]
                  if self.db_type == 'mysql' else
                  ([('连接名', 'name', '本地 Redis'), ('主机', 'host', '127.0.0.1'),
                    ('端口', 'port', '6379'), ('密码', 'password', ''),
                    ('数据库序号', 'db', '0')]
                   if self.db_type == 'redis' else
                   [('连接名', 'name', '本地 SQLite'), ('文件路径', 'path', '')]))
        for label, key, default in fields:
            ttk.Label(frame, text=label).grid(row=row, column=0, sticky='w', pady=5)
            entry = ttk.Entry(frame, width=32, show='*' if key == 'password' else '')
            entry.insert(0, default)
            entry.grid(row=row, column=1, sticky='we', pady=5, padx=(8, 0))
            self.vars[key] = entry
            if key == 'path':
                ttk.Button(frame, text="浏览",
                           command=lambda: self._browse(entry)).grid(row=row, column=2, padx=(4, 0))
            row += 1
        frame.columnconfigure(1, weight=1)
        btns = ttk.Frame(frame)
        btns.grid(row=row + 1, column=0, columnspan=3, pady=16, sticky='e')
        ttk.Button(btns, text="确定", command=self._ok).pack(side='left', padx=4)
        ttk.Button(btns, text="取消", command=self.destroy).pack(side='left', padx=4)

    def _browse(self, entry):
        path = filedialog.askopenfilename(
            filetypes=[("SQLite 文件", "*.db *.sqlite *.sqlite3 *.db3"), ("所有文件", "*.*")])
        if path:
            entry.delete(0, 'end')
            entry.insert(0, path)

    def _ok(self):
        params = {k: v.get().strip() for k, v in self.vars.items()}
        name = params.pop('name') or '未命名连接'
        if self.db_type == 'sqlite' and not params.get('path'):
            messagebox.showwarning("提示", "请填写 SQLite 文件路径")
            return
        if self.db_type == 'redis':
            try:
                dbn = int(params.get('db', '0') or '0')
            except ValueError:
                messagebox.showwarning("提示", "数据库序号必须是 0-15 的整数")
                return
            if dbn < 0 or dbn > 15:
                messagebox.showwarning("提示", "数据库序号必须是 0-15 的整数")
                return
            params['db'] = dbn
        self.destroy()
        self.on_ok({'name': name, 'params': params})


# ---------------------------------------------------------------------------
# 行数据编辑对话框（增/改）
# ---------------------------------------------------------------------------
class RowEditDialog(tk.Toplevel):
    def __init__(self, master, columns, values, title):
        super().__init__(master)
        self.columns = columns          # dict 列表
        self.values = values or {}      # 现有值 dict
        self.result = None
        self.title(title)
        self.geometry("480x420")
        self._build()
        self.transient(master)
        self.grab_set()

    def _build(self):
        canvas = tk.Canvas(self)
        scroll = ttk.Scrollbar(self, orient='vertical', command=canvas.yview)
        frame = ttk.Frame(canvas)
        frame.bind('<Configure>', lambda e: canvas.configure(scrollregion=canvas.bbox('all')))
        canvas.create_window((0, 0), window=frame, anchor='nw')
        canvas.configure(yscrollcommand=scroll.set)
        canvas.pack(side='left', fill='both', expand=True)
        scroll.pack(side='right', fill='y')

        self.entries = {}
        for i, col in enumerate(self.columns):
            name = col['name']
            ttk.Label(frame, text=f"{name} {'*' if col['pk'] else ''}").grid(
                row=i, column=0, sticky='w', padx=8, pady=3)
            entry = ttk.Entry(frame, width=36)
            if name in self.values and self.values[name] is not None:
                entry.insert(0, str(self.values[name]))
            entry.grid(row=i, column=1, sticky='we', padx=8, pady=3)
            self.entries[name] = entry
        frame.columnconfigure(1, weight=1)

        btns = ttk.Frame(self)
        btns.pack(pady=10)
        ttk.Button(btns, text="确定", command=self._ok).pack(side='left', padx=6)
        ttk.Button(btns, text="取消", command=self.destroy).pack(side='left', padx=6)

    def _ok(self):
        self.result = {name: e.get() for name, e in self.entries.items()}
        self.destroy()


# ---------------------------------------------------------------------------
# 设计表 / 新建表对话框
# ---------------------------------------------------------------------------
class DesignTableDialog(tk.Toplevel):
    """字段 + 索引 + 外键 设计。mode: 'create' 或 'alter'"""

    def __init__(self, master, conn, table=None, database=None, on_done=None):
        super().__init__(master)
        self.conn = conn
        self.table = table
        self.database = database
        self.mode = 'alter' if table else 'create'
        self.on_done = on_done
        self.types = MYSQL_TYPES if conn.db_type == 'mysql' else SQLITE_TYPES
        self.title(f"{'设计表' if table else '新建表'}：{table or ''}")
        self.geometry("860x560")
        self._build()
        self.transient(master)
        self.grab_set()

    def _build(self):
        nb = ttk.Notebook(self)
        nb.pack(fill='both', expand=True, padx=8, pady=8)
        self._build_field_tab(nb)
        self._build_index_tab(nb)
        self._build_fk_tab(nb)

        bottom = ttk.Frame(self)
        bottom.pack(fill='x', padx=8, pady=6)
        if self.mode == 'create':
            ttk.Label(bottom, text="表名:").pack(side='left')
            self.table_name = ttk.Entry(bottom, width=24)
            self.table_name.pack(side='left', padx=6)
        ttk.Button(bottom, text="保存", command=self._save).pack(side='right')
        ttk.Button(bottom, text="取消", command=self.destroy).pack(side='right', padx=6)

    def _build_field_tab(self, nb):
        tab = ttk.Frame(nb)
        nb.add(tab, text="字段")
        toolbar = ttk.Frame(tab)
        toolbar.pack(fill='x', padx=4, pady=4)
        ttk.Button(toolbar, text="添加字段", command=self._add_field).pack(side='left')
        ttk.Button(toolbar, text="删除字段", command=self._del_field).pack(side='left', padx=6)

        cols = ('name', 'type', 'length', 'pk', 'nullable', 'auto', 'default', 'comment')
        heads = ('字段名', '类型', '长度', '主键', '可空', '自增', '默认值', '注释')
        self.field_tree = ttk.Treeview(tab, columns=cols, show='headings', selectmode='browse')
        for c, h in zip(cols, heads):
            self.field_tree.heading(c, text=h)
            self.field_tree.column(c, width=80, anchor='w')
        self.field_tree.column('comment', width=150)
        scroll = ttk.Scrollbar(tab, orient='vertical', command=self.field_tree.yview)
        self.field_tree.configure(yscrollcommand=scroll.set)
        scroll.pack(side='right', fill='y')
        self.field_tree.pack(fill='both', expand=True, padx=4, pady=2)
        self.field_tree.bind('<Double-1>', lambda e: self._edit_field())

        if self.mode == 'alter':
            for c in self.conn.get_columns(self.table, self.database):
                self.field_tree.insert('', 'end', values=(
                    c['name'], c['type'], self._len_of(c['type']), '是' if c['pk'] else '',
                    '是' if c['nullable'] else '', '是' if c['auto'] else '',
                    c['default'] or '', c['comment'] or ''))

    def _build_index_tab(self, nb):
        tab = ttk.Frame(nb)
        nb.add(tab, text="索引")
        toolbar = ttk.Frame(tab)
        toolbar.pack(fill='x', padx=4, pady=4)
        ttk.Button(toolbar, text="添加索引", command=self._add_index).pack(side='left')
        ttk.Button(toolbar, text="删除索引", command=self._del_index).pack(side='left', padx=6)
        self.index_tree = ttk.Treeview(tab, columns=('name', 'cols'), show='headings')
        self.index_tree.heading('name', text='索引名')
        self.index_tree.heading('cols', text='列')
        self.index_tree.column('name', width=180)
        self.index_tree.column('cols', width=300)
        scroll = ttk.Scrollbar(tab, orient='vertical', command=self.index_tree.yview)
        self.index_tree.configure(yscrollcommand=scroll.set)
        scroll.pack(side='right', fill='y')
        self.index_tree.pack(fill='both', expand=True, padx=4, pady=2)
        if self.mode == 'alter':
            for name, cols in self.conn.get_indexes(self.table, self.database):
                self.index_tree.insert('', 'end', values=(name, cols))

    def _build_fk_tab(self, nb):
        tab = ttk.Frame(nb)
        nb.add(tab, text="外键")
        toolbar = ttk.Frame(tab)
        toolbar.pack(fill='x', padx=4, pady=4)
        ttk.Button(toolbar, text="添加外键", command=self._add_fk).pack(side='left')
        ttk.Button(toolbar, text="删除外键", command=self._del_fk).pack(side='left', padx=6)
        self.fk_tree = ttk.Treeview(tab, columns=('name', 'col', 'rtable', 'rcol'), show='headings')
        for c, h in zip(('name', 'col', 'rtable', 'rcol'), ('外键名', '列', '引用表', '引用列')):
            self.fk_tree.heading(c, text=h)
            self.fk_tree.column(c, width=140)
        scroll = ttk.Scrollbar(tab, orient='vertical', command=self.fk_tree.yview)
        self.fk_tree.configure(yscrollcommand=scroll.set)
        scroll.pack(side='right', fill='y')
        self.fk_tree.pack(fill='both', expand=True, padx=4, pady=2)
        if self.mode == 'alter':
            for name, col, rtable, rcol in self.conn.get_foreign_keys(self.table, self.database):
                self.fk_tree.insert('', 'end', values=(name, col, rtable, rcol))

    @staticmethod
    def _len_of(ctype):
        if '(' in ctype:
            return ctype[ctype.index('(') + 1:ctype.index(')')]
        return ''

    # ---- 字段编辑 ----
    def _add_field(self):
        self.field_tree.insert('', 'end', values=('', 'VARCHAR', '255', '', '是', '', '', ''))
        self._edit_field()

    def _del_field(self):
        item = self.field_tree.focus()
        if item:
            self.field_tree.delete(item)

    def _edit_field(self):
        item = self.field_tree.focus()
        if not item:
            return
        dlg = FieldDefDialog(self, self.types, self.field_tree.item(item, 'values'))
        self.wait_window(dlg)
        if dlg.result:
            self.field_tree.item(item, values=dlg.result)

    def _add_index(self):
        name = simpledialog.askstring("索引名", "请输入索引名:")
        if not name:
            return
        cols = simpledialog.askstring("索引列", "请输入列名（多列用逗号分隔）:")
        if cols:
            self.index_tree.insert('', 'end', values=(name, cols))

    def _del_index(self):
        item = self.index_tree.focus()
        if item:
            self.index_tree.delete(item)

    def _add_fk(self):
        col = simpledialog.askstring("外键列", "本表列名:")
        rtable = simpledialog.askstring("引用表", "引用表名:")
        rcol = simpledialog.askstring("引用列", "引用列名:")
        if col and rtable and rcol:
            self.fk_tree.insert('', 'end', values=(f"fk_{col}", col, rtable, rcol))

    def _del_fk(self):
        item = self.fk_tree.focus()
        if item:
            self.fk_tree.delete(item)

    # ---- 保存 ----
    def _save(self):
        try:
            if self.mode == 'create':
                self._save_create()
            else:
                self._save_alter()
            if self.on_done:
                self.on_done()
            self.destroy()
        except Exception as e:
            messagebox.showerror("保存失败", str(e))

    def _collect_fields(self):
        fields = []
        for item in self.field_tree.get_children():
            vals = self.field_tree.item(item, 'values')
            name, ctype, length = vals[0].strip(), vals[1].strip(), vals[2].strip()
            if not name:
                continue
            if length and '(' not in ctype:
                ctype = f"{ctype}({length})"
            fields.append({
                'name': name, 'type': ctype, 'pk': vals[3] == '是',
                'nullable': vals[4] == '是', 'auto': vals[5] == '是',
                'default': vals[6].strip() or None, 'comment': vals[7].strip(),
            })
        return fields

    def _column_def(self, f):
        parts = [self.conn.q(f['name']), f['type']]
        if not f['nullable']:
            parts.append('NOT NULL')
        if f['auto']:
            parts.append('AUTO_INCREMENT')
        if f['default'] not in (None, ''):
            parts.append(f"DEFAULT '{f['default']}'")
        if f['comment'] and self.conn.db_type == 'mysql':
            parts.append(f"COMMENT '{f['comment']}'")
        return ' '.join(parts)

    def _save_create(self):
        table = (self.table_name.get().strip() if hasattr(self, 'table_name') else '') \
            or (self.table or '')
        if not table:
            raise RuntimeError("请填写表名")
        fields = self._collect_fields()
        if not fields:
            raise RuntimeError("至少需要一个字段")
        pk_cols = [f['name'] for f in fields if f['pk']]
        lines = [self._column_def(f) for f in fields]
        if pk_cols:
            lines.append(f"PRIMARY KEY ({', '.join(self.conn.q(c) for c in pk_cols)})")
        for item in self.fk_tree.get_children():
            name, col, rtable, rcol = self.fk_tree.item(item, 'values')
            lines.append(f"CONSTRAINT `{name}` FOREIGN KEY (`{col}`) "
                         f"REFERENCES `{rtable}` (`{rcol}`)")
        sql = f"CREATE TABLE {self.conn.tbl(table, self.database)} (\n  " + \
              ',\n  '.join(lines) + "\n)"
        self.conn._execute(sql)
        for item in self.index_tree.get_children():
            name, cols = self.index_tree.item(item, 'values')
            self.conn._execute(f"CREATE INDEX `{name}` ON "
                               f"{self.conn.tbl(table, self.database)} ({cols})")

    def _save_alter(self):
        table = self.table
        new_fields = self._collect_fields()
        old_fields = self.conn.get_columns(table, self.database)
        old_names = {f['name'] for f in old_fields}
        new_names = {f['name'] for f in new_fields}

        if self.conn.db_type == 'sqlite':
            # SQLite 通过重建表实现结构变更
            self._rebuild_sqlite(table, new_fields)
        else:
            for f in old_fields:
                if f['name'] not in new_names:
                    self.conn._execute(
                        f"ALTER TABLE {self.conn.tbl(table, self.database)} "
                        f"DROP COLUMN {self.conn.q(f['name'])}")
            for f in new_fields:
                if f['name'] not in old_names:
                    self.conn._execute(
                        f"ALTER TABLE {self.conn.tbl(table, self.database)} "
                        f"ADD COLUMN {self._column_def(f)}")
                else:
                    self.conn._execute(
                        f"ALTER TABLE {self.conn.tbl(table, self.database)} "
                        f"MODIFY COLUMN {self._column_def(f)}")
        # 索引同步（MySQL 简单重建）
        existing = {n for n, _ in self.conn.get_indexes(table, self.database)}
        for item in self.index_tree.get_children():
            name, cols = self.index_tree.item(item, 'values')
            if name not in existing:
                self.conn._execute(f"CREATE INDEX `{name}` ON "
                                   f"{self.conn.tbl(table, self.database)} ({cols})")

    def _rebuild_sqlite(self, table, new_fields):
        pk_cols = [f['name'] for f in new_fields if f['pk']]
        lines = [self._column_def(f) for f in new_fields]
        if pk_cols:
            lines.append(f"PRIMARY KEY ({', '.join(pk_cols)})")
        tmp = f"{table}__backup"
        self.conn._execute(f"ALTER TABLE `{table}` RENAME TO `{tmp}`")
        try:
            create_sql = f"CREATE TABLE `{table}` (\n  " + ',\n  '.join(lines) + "\n)"
            self.conn._execute(create_sql)
            new_names = [f['name'] for f in new_fields]
            old_cols = self.conn.get_columns(tmp)
            common = [c['name'] for c in old_cols if c['name'] in new_names]
            cols_sql = ', '.join(self.conn.q(c) for c in common)
            self.conn._execute(f"INSERT INTO `{table}` ({cols_sql}) "
                               f"SELECT {cols_sql} FROM `{tmp}`")
            self.conn._execute(f"DROP TABLE `{tmp}`")
        except Exception:
            self.conn._execute(f"ALTER TABLE `{tmp}` RENAME TO `{table}`")
            raise


class FieldDefDialog(tk.Toplevel):
    def __init__(self, master, types, values):
        super().__init__(master)
        self.types = types
        self.result = None
        self.title("字段定义")
        self.geometry("360x300")
        self._build(values)
        self.transient(master)
        self.grab_set()

    def _build(self, v):
        f = ttk.Frame(self, padding=12)
        f.pack(fill='both', expand=True)
        self.name = ttk.Entry(f)
        self.type = ttk.Combobox(f, values=self.types)
        self.length = ttk.Entry(f)
        self.pk = tk.BooleanVar(value=v[3] == '是')
        self.nullable = tk.BooleanVar(value=v[4] == '是')
        self.auto = tk.BooleanVar(value=v[5] == '是')
        self.default = ttk.Entry(f)
        self.comment = ttk.Entry(f)

        self.name.insert(0, v[0]); self.type.set(v[1] or self.types[0])
        self.length.insert(0, v[2]); self.default.insert(0, v[6]); self.comment.insert(0, v[7])

        rows = [('字段名', self.name), ('类型', self.type), ('长度', self.length),
                ('默认值', self.default), ('注释', self.comment)]
        for i, (label, w) in enumerate(rows):
            ttk.Label(f, text=label).grid(row=i, column=0, sticky='w', pady=3)
            w.grid(row=i, column=1, sticky='we', pady=3, padx=(8, 0))
        ttk.Checkbutton(f, text='主键', variable=self.pk).grid(row=5, column=0, sticky='w')
        ttk.Checkbutton(f, text='可空', variable=self.nullable).grid(row=5, column=1, sticky='w')
        ttk.Checkbutton(f, text='自增', variable=self.auto).grid(row=6, column=0, sticky='w')
        f.columnconfigure(1, weight=1)

        btns = ttk.Frame(f)
        btns.grid(row=7, column=0, columnspan=2, pady=10, sticky='e')
        ttk.Button(btns, text="确定", command=self._ok).pack(side='left', padx=4)
        ttk.Button(btns, text="取消", command=self.destroy).pack(side='left', padx=4)

    def _ok(self):
        self.result = (self.name.get().strip(), self.type.get().strip(),
                       self.length.get().strip(), '是' if self.pk.get() else '',
                       '是' if self.nullable.get() else '', '是' if self.auto.get() else '',
                       self.default.get().strip(), self.comment.get().strip())
        self.destroy()


# ---------------------------------------------------------------------------
# 字符集选择对话框
# ---------------------------------------------------------------------------
class CharsetDialog(tk.Toplevel):
    def __init__(self, master, title, default='utf8mb4'):
        super().__init__(master)
        self.result = None
        self.title(title)
        self.geometry("340x150")
        self.resizable(False, False)
        self._build(default)
        self.transient(master)
        self.grab_set()

    def _build(self, default):
        frame = ttk.Frame(self, padding=16)
        frame.pack(fill='both', expand=True)
        ttk.Label(frame, text="选择字符集：").pack(anchor='w')
        self.combo = ttk.Combobox(frame, values=CHARSETS)
        self.combo.set(default)
        self.combo.pack(fill='x', pady=8)
        btns = ttk.Frame(frame)
        btns.pack(pady=8, anchor='e')
        ttk.Button(btns, text="确定", command=self._ok).pack(side='left', padx=4)
        ttk.Button(btns, text="取消", command=self.destroy).pack(side='left', padx=4)

    def _ok(self):
        cs = self.combo.get().strip()
        if not cs:
            messagebox.showwarning("提示", "请选择或输入字符集")
            return
        self.result = cs
        self.destroy()


# ---------------------------------------------------------------------------
# 数据库选择对话框（导出整个数据库等未选中时使用）
# ---------------------------------------------------------------------------
class DatabaseSelectDialog(tk.Toplevel):
    def __init__(self, master, databases):
        super().__init__(master)
        self.result = None
        self.databases = databases
        self.title("选择数据库")
        self.geometry("320x140")
        self.resizable(False, False)
        self._build()
        self.transient(master)
        self.grab_set()

    def _build(self):
        frame = ttk.Frame(self, padding=16)
        frame.pack(fill='both', expand=True)
        ttk.Label(frame, text="请选择数据库：").pack(anchor='w')
        self.combo = ttk.Combobox(frame, values=self.databases, state='readonly')
        if self.databases:
            self.combo.set(self.databases[0])
        self.combo.pack(fill='x', pady=8)
        btns = ttk.Frame(frame)
        btns.pack(pady=8, anchor='e')
        ttk.Button(btns, text="确定", command=self._ok).pack(side='left', padx=4)
        ttk.Button(btns, text="取消", command=self.destroy).pack(side='left', padx=4)

    def _ok(self):
        if not self.combo.get():
            messagebox.showwarning("提示", "请选择数据库")
            return
        self.result = self.combo.get()
        self.destroy()

    def show(self):
        """模态等待，返回选中的库名（取消返回 None）。"""
        self.wait_window(self)
        return self.result


# ---------------------------------------------------------------------------
# 导出对话框
# ---------------------------------------------------------------------------
class ExportDialog(tk.Toplevel):
    def __init__(self, master, scope_desc, on_export):
        super().__init__(master)
        self.on_export = on_export
        self.title("导出 SQL")
        self.geometry("380x220")
        self.resizable(False, False)
        self._build(scope_desc)
        self.transient(master)
        self.grab_set()

    def _build(self, scope_desc):
        frame = ttk.Frame(self, padding=16)
        frame.pack(fill='both', expand=True)
        ttk.Label(frame, text=f"导出对象：{scope_desc}", padding=(0, 0, 0, 8)).pack(anchor='w')
        ttk.Label(frame, text="导出范围：").pack(anchor='w')
        self.mode = tk.StringVar(value='both')
        for text, val in [("数据和结构", 'both'), ("仅结构", 'structure'), ("仅数据", 'data')]:
            ttk.Radiobutton(frame, text=text, value=val, variable=self.mode).pack(
                anchor='w', padx=8, pady=2)
        btns = ttk.Frame(frame)
        btns.pack(pady=10, anchor='e')
        ttk.Button(btns, text="选择文件并导出", command=self._ok).pack(side='left', padx=4)
        ttk.Button(btns, text="取消", command=self.destroy).pack(side='left', padx=4)

    def _ok(self):
        path = filedialog.asksaveasfilename(
            defaultextension=".sql", filetypes=[("SQL 文件", "*.sql")],
            initialfile="export.sql")
        if not path:
            return
        self.on_export(self.mode.get(), path)
        self.destroy()


# ---------------------------------------------------------------------------
# Redis Key 数据查看/编辑对话框
# ---------------------------------------------------------------------------
class RedisDataPane(ttk.Frame):
    """Redis key 数据视图（嵌入右侧「数据」页签，替代原弹窗 RedisKeyDialog）。

    STRING 支持 JSON 格式化/原始切换，LIST/HASH/SET/ZSET 表格化展示并支持分页，
    增删改均在页内完成；「关闭」或删除 key 后恢复 SQL 数据视图。
    """

    def __init__(self, master, app, conn, dbnum, key, ktype=None):
        super().__init__(master)
        self.pack(fill='both', expand=True)
        self.app = app
        self.conn = conn
        self.dbnum = dbnum
        self.key = key
        self.ktype = ktype or 'string'
        self._page_offset = 0
        self._cursor = 0
        self._seen = 0
        self._total = 0
        self._page_size = conn.PAGE_SIZE
        self._load_q = None
        self._table = None
        self._string_text = None
        self._string_raw = ''
        self._string_pretty = ''
        self._raw_mode = tk.BooleanVar(value=False)
        self._build()
        self._load()

    # ---- UI ----
    def _build(self):
        header = ttk.Frame(self, padding=6)
        header.pack(fill='x')
        ttk.Label(header, text=f"Redis Key: {self.key}", font=('Segoe UI', 10, 'bold')).pack(side='left')
        self.type_label = ttk.Label(header, text=f"类型：{self.ktype}")
        self.type_label.pack(side='left', padx=12)
        self.ttl_label = ttk.Label(header, text="TTL：-")
        self.ttl_label.pack(side='left', padx=8)
        self.status_label = ttk.Label(header, text="")
        self.status_label.pack(side='left', padx=12)
        btns = ttk.Frame(header)
        btns.pack(side='right')
        ttk.Button(btns, text="关闭", command=self._close_pane).pack(side='left')
        ttk.Button(btns, text="刷新", command=self._load).pack(side='left', padx=4)
        ttk.Button(btns, text="设置 TTL", command=self._set_ttl).pack(side='left', padx=4)
        ttk.Button(btns, text="删除 Key", command=self._del_key).pack(side='left')
        # 结构信息条（第二行）：编码 / 元素个数 / 内存占用
        info_bar = ttk.Frame(self, padding=(6, 0, 6, 2))
        info_bar.pack(fill='x')
        self.enc_label = ttk.Label(info_bar, text="编码：-", foreground="#444")
        self.enc_label.pack(side='left')
        self.size_label = ttk.Label(info_bar, text="元素个数：-", foreground="#444")
        self.size_label.pack(side='left', padx=10)
        self.mem_label = ttk.Label(info_bar, text="内存占用：-", foreground="#444")
        self.mem_label.pack(side='left', padx=10)

        self.body = ttk.Frame(self)
        self.body.pack(fill='both', expand=True, padx=6, pady=4)

        footer = ttk.Frame(self, padding=6)
        footer.pack(fill='x')
        self.page_label = ttk.Label(footer, text="")
        self.page_label.pack(side='left')
        self.prev_btn = ttk.Button(footer, text="首页", command=self._turn_home, state='disabled')
        self.prev_btn.pack(side='right', padx=4)
        self.next_btn = ttk.Button(footer, text="下一页", command=self._turn_next, state='disabled')
        self.next_btn.pack(side='right')

    def _close_pane(self):
        """关闭 Redis key 视图，恢复 SQL 数据视图。"""
        if self.app:
            self.app._close_redis_pane()

    # ---- 数据加载（后台线程） ----
    def _load(self):
        self.status_label.config(text="加载中...")
        q = queue.Queue()

        def worker():
            try:
                ktype = self.conn.get_key_type(self.dbnum, self.key)
                ttl = self.conn.get_ttl(self.dbnum, self.key)
                info = self.conn.get_key_info(self.dbnum, self.key)
                page = self._fetch_page(ktype)
                q.put(('ok', ktype, ttl, info, page))
            except Exception as e:
                q.put(('err', str(e)))

        threading.Thread(target=worker, daemon=True).start()
        self._load_q = q
        self.after(100, self._poll_load)

    def _fetch_page(self, ktype):
        if ktype == 'string':
            return ('string', self.conn.get_string(self.dbnum, self.key), 1)
        if ktype == 'list':
            items, total = self.conn.get_list_page(self.dbnum, self.key, self._page_offset, self._page_size)
            return ('list', items, total)
        if ktype == 'hash':
            ncursor, data, total = self.conn.get_hash_page(self.dbnum, self.key, self._cursor, self._page_size)
            self._cursor = ncursor
            self._seen += len(data)
            return ('hash', data, total)
        if ktype == 'set':
            ncursor, data, total = self.conn.get_set_page(self.dbnum, self.key, self._cursor, self._page_size)
            self._cursor = ncursor
            self._seen += len(data)
            return ('set', data, total)
        if ktype == 'zset':
            ncursor, data, total = self.conn.get_zset_page(self.dbnum, self.key, self._cursor, self._page_size)
            self._cursor = ncursor
            self._seen += len(data)
            return ('zset', data, total)
        return ('string', self.conn.get_string(self.dbnum, self.key), 1)

    def _poll_load(self):
        try:
            result = self._load_q.get_nowait()
        except queue.Empty:
            self.after(100, self._poll_load)
            return
        status, *rest = result
        if status == 'err':
            self.status_label.config(text=f"读取失败：{rest[0]}")
            return
        ktype, ttl, info, page = rest
        self.ktype = ktype
        self.type_label.config(text=f"类型：{ktype}")
        ttl_text = f"TTL：{ttl} 秒" + ("（永不过期）" if ttl == -1 else "")
        self.ttl_label.config(text=ttl_text)
        self._update_info_labels(info)
        self._render(ktype, page)

    def _update_info_labels(self, info):
        """在结构信息条中展示 encoding / 元素个数 / 内存占用。"""
        if not info:
            return
        enc = info.get('encoding')
        self.enc_label.config(text=f"编码：{enc}" if enc else "编码：-")
        size = info.get('size')
        if size is not None:
            self.size_label.config(text=f"元素个数：{size}")
        else:
            self.size_label.config(text="元素个数：-")
        mem = info.get('memory')
        if mem is not None:
            self.mem_label.config(text=f"内存占用：{self._fmt_bytes(mem)}")
        else:
            self.mem_label.config(text="内存占用：-")

    @staticmethod
    def _fmt_bytes(n):
        """内存字节数人性化显示。"""
        if n < 1024:
            return f"{n} B"
        if n < 1024 * 1024:
            return f"{n / 1024:.1f} KB"
        return f"{n / (1024 * 1024):.1f} MB"

    def _render(self, ktype, page):
        for w in self.body.winfo_children():
            w.destroy()
        if ktype == 'list':
            self._render_list(page)
        elif ktype == 'hash':
            self._render_hash(page)
        elif ktype == 'set':
            self._render_set(page)
        elif ktype == 'zset':
            self._render_zset(page)
        else:
            self._render_string(page)

    # ---- 通用 ----
    def _build_table(self, columns):
        frame = ttk.Frame(self.body)
        frame.pack(fill='both', expand=True)
        tree = ttk.Treeview(frame, columns=columns, show='headings', height=16)
        for c in columns:
            tree.heading(c, text=c)
            tree.column(c, width=200, anchor='w')
        vs = ttk.Scrollbar(frame, orient='vertical', command=tree.yview)
        tree.configure(yscrollcommand=vs.set)
        vs.pack(side='right', fill='y')
        tree.pack(fill='both', expand=True)
        return tree

    def _update_pager(self, total):
        self._total = total
        if self.ktype == 'list':
            self.page_label.config(
                text=f"当前条目 {self._page_offset + 1}-{min(self._page_offset + self._page_size, total)} / 共 {total}")
            self.next_btn.config(state='normal' if self._page_offset + self._page_size < total else 'disabled')
        else:
            self.page_label.config(text=f"已浏览 {self._seen} / 共 {total}")
            self.next_btn.config(state='normal' if self._seen < total else 'disabled')

    def _turn_home(self):
        self._page_offset = 0
        self._cursor = 0
        self._seen = 0
        self._load()

    def _turn_next(self):
        if self.ktype == 'list':
            self._page_offset += self._page_size
        self._load()

    # ---- STRING ----
    def _render_string(self, page):
        kind, value, _ = page
        frame = ttk.Frame(self.body)
        frame.pack(fill='both', expand=True)
        toolbar = ttk.Frame(frame)
        toolbar.pack(fill='x', pady=4)
        ttk.Button(toolbar, text="保存", command=self._save_string).pack(side='left')
        self._raw_mode.set(False)
        self._fmt_btn = ttk.Checkbutton(
            toolbar, text="原始", variable=self._raw_mode,
            command=self._toggle_string_mode, state='disabled')
        self._fmt_btn.pack(side='left', padx=4)
        ttk.Label(toolbar, text="字符串值（保存即覆盖）：").pack(side='left', padx=10)
        text = tk.Text(frame, wrap='none', font=('Consolas', 11))
        vs = ttk.Scrollbar(frame, orient='vertical', command=text.yview)
        text.configure(yscrollcommand=vs.set)
        vs.pack(side='right', fill='y')
        text.pack(fill='both', expand=True)
        self._string_raw = value if value is not None else ''
        self._string_pretty = self._pretty_json(self._string_raw)
        if self._string_pretty != self._string_raw:
            self._fmt_btn.config(state='normal')
            text.insert('1.0', self._string_pretty)
        else:
            text.insert('1.0', self._string_raw)
        self._string_text = text
        self.status_label.config(text="已加载")
        self.page_label.config(text="")

    def _pretty_json(self, raw):
        """尝试将字符串解析为 JSON 并缩进格式化；无法解析时原样返回。"""
        try:
            obj = json.loads(raw)
            return json.dumps(obj, ensure_ascii=False, indent=2)
        except Exception:
            return raw

    def _toggle_string_mode(self):
        """原始 / 格式化 切换（仅当字符串可解析为 JSON 时有效）。"""
        if self._string_text is None:
            return
        self._string_text.delete('1.0', 'end')
        if self._raw_mode.get():
            self._string_text.insert('1.0', self._string_raw)
        else:
            self._string_text.insert('1.0', self._string_pretty)

    def _save_string(self):
        try:
            value = self._string_text.get('1.0', 'end').rstrip('\n')
            self.conn.set_string(self.dbnum, self.key, value)
            self.status_label.config(text="已保存")
        except Exception as e:
            messagebox.showerror("保存失败", str(e))

    # ---- LIST ----
    def _render_list(self, page):
        kind, items, total = page
        toolbar = ttk.Frame(self.body)
        toolbar.pack(fill='x', pady=4)
        ttk.Button(toolbar, text="添加元素", command=self._add_list_item).pack(side='left')
        ttk.Button(toolbar, text="删除选中", command=self._del_list_item).pack(side='left', padx=4)
        ttk.Label(toolbar, text="元素列表（rpush 追加 / 按值删除）").pack(side='left', padx=10)
        tree = self._build_table(['序号', '元素'])
        for i, it in enumerate(items):
            tree.insert('', 'end', values=(self._page_offset + i + 1, it))
        self._table = tree
        self._update_pager(total)

    def _add_list_item(self):
        s = simpledialog.askstring("添加元素", "输入元素值：")
        if s is None:
            return
        try:
            self.conn.add_list_item(self.dbnum, self.key, s)
            self._load()
        except Exception as e:
            messagebox.showerror("失败", str(e))

    def _del_list_item(self):
        sel = self._table.selection()
        if not sel:
            messagebox.showinfo("提示", "请先选中要删除的元素")
            return
        value = self._table.item(sel[0], 'values')[1]
        if messagebox.askyesno("确认", f"删除元素（按值删除所有匹配项）：{value}"):
            try:
                self.conn.del_list_items(self.dbnum, self.key, value)
                self._load()
            except Exception as e:
                messagebox.showerror("失败", str(e))

    # ---- HASH ----
    def _render_hash(self, page):
        kind, data, total = page
        toolbar = ttk.Frame(self.body)
        toolbar.pack(fill='x', pady=4)
        ttk.Button(toolbar, text="新增字段", command=self._add_hash_field).pack(side='left')
        ttk.Button(toolbar, text="编辑选中", command=self._edit_hash_field).pack(side='left', padx=4)
        ttk.Button(toolbar, text="删除选中", command=self._del_hash_field).pack(side='left', padx=4)
        tree = self._build_table(['field', 'value'])
        for f, val in data.items():
            tree.insert('', 'end', values=(f, val))
        self._table = tree
        self._update_pager(total)

    def _add_hash_field(self):
        f = simpledialog.askstring("新增字段", "field：")
        if f is None:
            return
        v = simpledialog.askstring("新增字段", f"field `{f}` 的 value：")
        if v is None:
            return
        try:
            self.conn.add_hash_field(self.dbnum, self.key, f, v)
            self._load()
        except Exception as e:
            messagebox.showerror("失败", str(e))

    def _edit_hash_field(self):
        sel = self._table.selection()
        if not sel:
            messagebox.showinfo("提示", "请先选中字段")
            return
        f, old_v = self._table.item(sel[0], 'values')
        v = simpledialog.askstring("编辑字段", f"field `{f}` 的新 value：", initialvalue=old_v)
        if v is None:
            return
        try:
            self.conn.add_hash_field(self.dbnum, self.key, f, v)
            self._load()
        except Exception as e:
            messagebox.showerror("失败", str(e))

    def _del_hash_field(self):
        sel = self._table.selection()
        if not sel:
            messagebox.showinfo("提示", "请先选中字段")
            return
        f = self._table.item(sel[0], 'values')[0]
        if messagebox.askyesno("确认", f"删除字段 `{f}`？"):
            try:
                self.conn.del_hash_field(self.dbnum, self.key, f)
                self._load()
            except Exception as e:
                messagebox.showerror("失败", str(e))

    # ---- SET ----
    def _render_set(self, page):
        kind, data, total = page
        toolbar = ttk.Frame(self.body)
        toolbar.pack(fill='x', pady=4)
        ttk.Button(toolbar, text="添加成员", command=self._add_set_member).pack(side='left')
        ttk.Button(toolbar, text="删除选中", command=self._del_set_member).pack(side='left', padx=4)
        ttk.Label(toolbar, text="成员列表").pack(side='left', padx=10)
        tree = self._build_table(['成员'])
        for m in data:
            tree.insert('', 'end', values=(m,))
        self._table = tree
        self._update_pager(total)

    def _add_set_member(self):
        s = simpledialog.askstring("添加成员", "输入成员值：")
        if s is None:
            return
        try:
            self.conn.add_set_member(self.dbnum, self.key, s)
            self._load()
        except Exception as e:
            messagebox.showerror("失败", str(e))

    def _del_set_member(self):
        sel = self._table.selection()
        if not sel:
            messagebox.showinfo("提示", "请先选中成员")
            return
        m = self._table.item(sel[0], 'values')[0]
        if messagebox.askyesno("确认", f"删除成员 `{m}`？"):
            try:
                self.conn.del_set_member(self.dbnum, self.key, m)
                self._load()
            except Exception as e:
                messagebox.showerror("失败", str(e))

    # ---- ZSET ----
    def _render_zset(self, page):
        kind, data, total = page
        toolbar = ttk.Frame(self.body)
        toolbar.pack(fill='x', pady=4)
        ttk.Button(toolbar, text="添加成员", command=self._add_zset_member).pack(side='left')
        ttk.Button(toolbar, text="删除选中", command=self._del_zset_member).pack(side='left', padx=4)
        tree = self._build_table(['member', 'score'])
        for m, sc in data.items():
            tree.insert('', 'end', values=(m, sc))
        self._table = tree
        self._update_pager(total)

    def _add_zset_member(self):
        m = simpledialog.askstring("添加成员", "member：")
        if m is None:
            return
        sc = simpledialog.askstring("添加成员", f"member `{m}` 的 score：")
        if sc is None:
            return
        try:
            self.conn.add_zset_member(self.dbnum, self.key, m, sc)
            self._load()
        except Exception as e:
            messagebox.showerror("失败", str(e))

    def _del_zset_member(self):
        sel = self._table.selection()
        if not sel:
            messagebox.showinfo("提示", "请先选中成员")
            return
        m = self._table.item(sel[0], 'values')[0]
        if messagebox.askyesno("确认", f"删除成员 `{m}`？"):
            try:
                self.conn.del_zset_member(self.dbnum, self.key, m)
                self._load()
            except Exception as e:
                messagebox.showerror("失败", str(e))

    # ---- TTL / 删除 ----
    def _set_ttl(self):
        s = simpledialog.askstring("设置 TTL", "过期时间（秒），-1 表示永不过期：", initialvalue='-1')
        if s is None:
            return
        try:
            secs = int(s)
        except ValueError:
            messagebox.showwarning("提示", "请输入整数")
            return
        try:
            self.conn.set_ttl(self.dbnum, self.key, secs)
            self._load()
        except Exception as e:
            messagebox.showerror("设置失败", str(e))

    def _del_key(self):
        if messagebox.askyesno("确认", f"确定删除 key `{self.key}`？"):
            try:
                self.conn.delete_key(self.dbnum, self.key)
                self._close_pane()
                if self.app:
                    self.app._refresh_tree()
            except Exception as e:
                messagebox.showerror("删除失败", str(e))


# ---------------------------------------------------------------------------
# SQL 历史记录窗口
# ---------------------------------------------------------------------------
class SQLHistoryWindow(tk.Toplevel):
    def __init__(self, master, history, on_select, on_clear, title="SQL 命令历史"):
        super().__init__(master)
        self.history = history
        self.on_select = on_select
        self.on_clear = on_clear
        self.title(title)
        self.geometry("620x420")
        self._build()
        self.transient(master)
        self.grab_set()

    def _build(self):
        frame = ttk.Frame(self, padding=8)
        frame.pack(fill='both', expand=True)
        ttk.Label(frame, text="双击历史命令可回填到查询输入框").pack(anchor='w')
        list_frame = ttk.Frame(frame)
        list_frame.pack(fill='both', expand=True, pady=4)
        self.listbox = tk.Listbox(list_frame, font=('Consolas', 10))
        sb = ttk.Scrollbar(list_frame, orient='vertical', command=self.listbox.yview)
        self.listbox.configure(yscrollcommand=sb.set)
        sb.pack(side='right', fill='y')
        self.listbox.pack(side='left', fill='both', expand=True)
        for s in self.history:
            self.listbox.insert('end', s)
        self.listbox.bind('<Double-1>', self._pick)
        btns = ttk.Frame(frame)
        btns.pack(fill='x', pady=4)
        ttk.Button(btns, text="回填选中", command=self._pick).pack(side='left')
        ttk.Button(btns, text="清空历史", command=self._clear).pack(side='left', padx=6)
        ttk.Button(btns, text="关闭", command=self.destroy).pack(side='right')

    def _pick(self, event=None):
        sel = self.listbox.curselection()
        if not sel:
            return
        sql = self.listbox.get(sel[0])
        if self.on_select:
            self.on_select(sql)
        self.destroy()

    def _clear(self):
        if messagebox.askyesno("确认", "确定清空全部 SQL 历史记录？"):
            if self.on_clear:
                self.on_clear()
            self.listbox.delete(0, 'end')


# ---------------------------------------------------------------------------
# 主窗口
# ---------------------------------------------------------------------------
class MainApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("数据库管理工具（MySQL / SQLite）")
        self.geometry("1280x780")
        self.connections = []
        self.current = None
        self.sql_history = self._load_sql_history()
        self.redis_history = self._load_redis_history()
        self._redis_loads = []
        self._build_menu()
        self._build_layout()
        self._update_type_ui()
        self._update_db_menu()
        self._status("就绪")

    def _build_menu(self):
        menubar = tk.Menu(self)
        conn_menu = tk.Menu(menubar, tearoff=0)
        conn_menu.add_command(label="新建 MySQL 连接", command=lambda: self._new_conn('mysql'))
        conn_menu.add_command(label="新建 SQLite 连接", command=lambda: self._new_conn('sqlite'))
        conn_menu.add_command(label="新建 Redis 连接", command=lambda: self._new_conn('redis'))
        conn_menu.add_separator()
        conn_menu.add_command(label="断开当前连接", command=self._disconnect_current)
        menubar.add_cascade(label="连接", menu=conn_menu)

        db_menu = tk.Menu(menubar, tearoff=0)
        db_menu.add_command(label="导出整个数据库", command=self._export_whole_db)
        db_menu.add_command(label="导入 SQL 文件", command=self._import_sql_file)
        db_menu.add_separator()
        db_menu.add_command(label="设置数据库字符集", command=self._set_database_charset_menu)
        db_menu.add_command(label="设置表字符集", command=self._set_table_charset_menu)
        menubar.add_cascade(label="数据库", menu=db_menu)
        self.db_menu = db_menu

        help_menu = tk.Menu(menubar, tearoff=0)
        help_menu.add_command(label="关于", command=self._about)
        menubar.add_cascade(label="帮助", menu=help_menu)
        self.config(menu=menubar)

    def _build_layout(self):
        toolbar = ttk.Frame(self, padding=(8, 6))
        toolbar.pack(side='top', fill='x')
        ttk.Button(toolbar, text="新建连接", command=self._new_conn_menu).pack(side='left')
        ttk.Button(toolbar, text="刷新", command=self._refresh_tree).pack(side='left', padx=6)
        self.export_db_btn = ttk.Button(toolbar, text="导出数据库", command=self._export_whole_db)
        self.export_db_btn.pack(side='left', padx=6)
        self.conn_label = ttk.Label(toolbar, text="当前连接：无", foreground="#555")
        self.conn_label.pack(side='left', padx=12)

        main_pane = ttk.PanedWindow(self, orient='horizontal')
        main_pane.pack(fill='both', expand=True)

        left = ttk.Frame(main_pane)
        main_pane.add(left, weight=1)

        self._redis_filter = ttk.Frame(left, padding=(6, 4, 6, 2))
        # 默认隐藏：仅在 Redis 连接成功后由 _update_redis_ui 显示
        ttk.Label(self._redis_filter, text="Key Pattern:").pack(side='left')
        self._redis_pattern_var = tk.StringVar(value='*')
        self._redis_pattern_entry = ttk.Entry(self._redis_filter, textvariable=self._redis_pattern_var, width=14)
        self._redis_pattern_entry.pack(side='left', padx=4)
        self._redis_pattern_entry.bind('<Return>', lambda e: self._refresh_tree())
        ttk.Button(self._redis_filter, text="搜索", command=self._refresh_tree).pack(side='left')
        ttk.Button(self._redis_filter, text="刷新", command=self._refresh_tree).pack(side='left', padx=4)

        self.tree = ttk.Treeview(left, show='tree')
        ts = ttk.Scrollbar(left, orient='vertical', command=self.tree.yview)
        self.tree.configure(yscrollcommand=ts.set)
        self.tree.pack(side='left', fill='both', expand=True)
        ts.pack(side='right', fill='y')
        self.tree.bind('<<TreeviewSelect>>', self._on_tree_select)
        self.tree.bind('<<TreeviewOpen>>', self._on_tree_open)
        self.tree.bind('<Double-1>', self._on_tree_double)
        self.tree.bind('<Button-3>', self._on_tree_right_click)

        right = ttk.Frame(main_pane)
        main_pane.add(right, weight=4)
        self.notebook = ttk.Notebook(right)
        self.notebook.pack(fill='both', expand=True)
        self._build_query_tab()
        self._build_data_tab()
        self._build_schema_tab()
        self._build_redis_tab()

        self.status_var = tk.StringVar()
        ttk.Label(self, textvariable=self.status_var, relief='sunken',
                  anchor='w', padding=(8, 2)).pack(side='bottom', fill='x')

    def _build_query_tab(self):
        self.query_tab = ttk.Frame(self.notebook)
        self.notebook.add(self.query_tab, text="查询")
        toolbar = ttk.Frame(self.query_tab, padding=4)
        toolbar.pack(fill='x')
        ttk.Button(toolbar, text="执行 (Ctrl+Enter)", command=self._run_sql).pack(side='left')
        ttk.Button(toolbar, text="清空", command=lambda: self.sql_text.delete('1.0', 'end')).pack(side='left', padx=6)
        ttk.Button(toolbar, text="历史", command=self._show_history).pack(side='left', padx=6)
        self.sql_text = tk.Text(self.query_tab, height=10, wrap='none', font=('Consolas', 11))
        ss = ttk.Scrollbar(self.query_tab, orient='vertical', command=self.sql_text.yview)
        self.sql_text.configure(yscrollcommand=ss.set)
        ss.pack(side='right', fill='y')
        self.sql_text.pack(fill='x', padx=4, pady=2)
        self.sql_text.bind('<Control-Return>', lambda e: self._run_sql())
        self.query_result = ttk.Treeview(self.query_tab, show='headings')
        rs = ttk.Scrollbar(self.query_tab, orient='vertical', command=self.query_result.yview)
        self.query_result.configure(yscrollcommand=rs.set)
        rs.pack(side='right', fill='y')
        self.query_result.pack(fill='both', expand=True, padx=4, pady=2)

    def _build_data_tab(self):
        self.data_tab = ttk.Frame(self.notebook)
        self.notebook.add(self.data_tab, text="数据")
        # SQL 数据视图容器（默认显示）
        self.data_tab_sql = ttk.Frame(self.data_tab)
        self.data_tab_sql.pack(fill='both', expand=True)
        toolbar = ttk.Frame(self.data_tab_sql, padding=4)
        toolbar.pack(fill='x')
        for text, cmd in [('刷新', self._refresh_data), ('新增行', self._add_row),
                          ('编辑行', self._edit_row), ('删除行', self._del_row),
                          ('保存', self._save_changes), ('导入', self._import_csv),
                          ('导出', self._export_menu)]:
            ttk.Button(toolbar, text=text, command=cmd).pack(side='left', padx=2)
        self.page_label = ttk.Label(toolbar, text="表：未选择")
        self.page_label.pack(side='left', padx=12)
        ttk.Button(toolbar, text="上一页", command=lambda: self._data_page(-1)).pack(side='right')
        ttk.Button(toolbar, text="下一页", command=lambda: self._data_page(1)).pack(side='right')

        self.data_tree = ttk.Treeview(self.data_tab_sql, show='headings')
        sy = ttk.Scrollbar(self.data_tab_sql, orient='vertical', command=self.data_tree.yview)
        sx = ttk.Scrollbar(self.data_tab_sql, orient='horizontal', command=self.data_tree.xview)
        self.data_tree.configure(yscrollcommand=sy.set, xscrollcommand=sx.set)
        sy.pack(side='right', fill='y')
        sx.pack(side='bottom', fill='x')
        self.data_tree.pack(fill='both', expand=True, padx=4, pady=2)

        # Redis key 数据视图容器（默认隐藏，选中 Redis key 时切换显示）
        self.data_tab_redis = ttk.Frame(self.data_tab)

        self._data_offset = 0
        self._data_table = None
        self._data_db = None
        self._data_columns = []
        self._pending = []   # ('insert'|'update'|'delete', payload)

    def _show_sql_data_view(self):
        """切换到 SQL 表数据视图（关闭 Redis key 视图）。"""
        if self.data_tab_redis.winfo_manager():
            self.data_tab_redis.pack_forget()
            for w in self.data_tab_redis.winfo_children():
                w.destroy()
        if not self.data_tab_sql.winfo_manager():
            self.data_tab_sql.pack(fill='both', expand=True)

    def _show_redis_data_view(self):
        """切换到 Redis key 数据视图（隐藏 SQL 表视图）。"""
        if self.data_tab_sql.winfo_manager():
            self.data_tab_sql.pack_forget()
        if not self.data_tab_redis.winfo_manager():
            self.data_tab_redis.pack(fill='both', expand=True)
        else:
            for w in self.data_tab_redis.winfo_children():
                w.destroy()

    def _close_redis_pane(self):
        """关闭 Redis key 视图。

        Redis 连接下：清空数据页签内的 Redis 容器内容并保持显示；
        SQL 连接/未连接：恢复 SQL 表数据视图。
        """
        if not hasattr(self, 'data_tab_redis'):
            return
        if self.current and self.current.db_type == 'redis':
            if not self.data_tab_redis.winfo_manager():
                self.data_tab_redis.pack(fill='both', expand=True)
            if self.data_tab_sql.winfo_manager():
                self.data_tab_sql.pack_forget()
            for w in self.data_tab_redis.winfo_children():
                w.destroy()
        else:
            self._show_sql_data_view()

    def _build_schema_tab(self):
        self.schema_tab = ttk.Frame(self.notebook)
        self.notebook.add(self.schema_tab, text="结构")
        self.schema_text = tk.Text(self.schema_tab, wrap='none', font=('Consolas', 11))
        ss = ttk.Scrollbar(self.schema_tab, orient='vertical', command=self.schema_text.yview)
        self.schema_text.configure(yscrollcommand=ss.set)
        ss.pack(side='right', fill='y')
        self.schema_text.pack(fill='both', expand=True, padx=4, pady=2)

    def _build_redis_tab(self):
        self.redis_tab = ttk.Frame(self.notebook)
        self._redis_tab_added = False  # 默认不加入 Notebook，由 _update_redis_ui 控制
        toolbar = ttk.Frame(self.redis_tab, padding=4)
        toolbar.pack(fill='x')
        ttk.Button(toolbar, text="执行 (Ctrl+Enter)", command=self._run_redis_cmd).pack(side='left')
        ttk.Button(toolbar, text="清空", command=lambda: self.redis_cmd_text.delete('1.0', 'end')).pack(side='left', padx=6)
        ttk.Button(toolbar, text="历史", command=self._show_redis_history).pack(side='left', padx=6)
        ttk.Label(toolbar, text="提示：需连接 Redis 后使用，如 GET user:1 / KEYS * / INFO").pack(side='left', padx=12)
        self.redis_cmd_text = tk.Text(self.redis_tab, height=6, wrap='none', font=('Consolas', 11))
        self.redis_cmd_text.pack(fill='x', padx=4, pady=2)
        self.redis_cmd_text.bind('<Control-Return>', lambda e: self._run_redis_cmd())
        self.redis_result = tk.Text(self.redis_tab, wrap='none', font=('Consolas', 11),
                                    state='disabled', bg='#fafafa')
        rs = ttk.Scrollbar(self.redis_tab, orient='vertical', command=self.redis_result.yview)
        self.redis_result.configure(yscrollcommand=rs.set)
        rs.pack(side='right', fill='y')
        self.redis_result.pack(fill='both', expand=True, padx=4, pady=2)

    def _update_redis_ui(self):
        """根据当前连接类型同步 Redis 专属 UI 显隐。

        仅当当前连接为 Redis 且已连接成功时显示：
        - Key Pattern 工具条（左侧树上方）
        - 「Redis 命令」页签（Notebook）
        未连接、连接 MySQL/SQLite、断开连接时均隐藏。
        """
        is_redis = bool(self.current) and self.current.db_type == 'redis'
        # Key Pattern 工具条
        if is_redis:
            if not self._redis_filter.winfo_manager():
                self._redis_filter.pack(side='top', fill='x')
        else:
            if self._redis_filter.winfo_manager():
                self._redis_filter.pack_forget()
        # Redis 命令页签
        if is_redis:
            if not self._redis_tab_added:
                self.notebook.add(self.redis_tab, text="Redis 命令")
                self._redis_tab_added = True
        else:
            if self._redis_tab_added:
                for t in self.notebook.tabs():
                    if self.notebook.tab(t, 'text') == 'Redis 命令':
                        self.notebook.forget(t)
                        break
                self._redis_tab_added = False

    def _update_type_ui(self):
        """按当前连接类型统一切换界面元素（初始化/连接成功/断开/切换连接时调用）。

        Redis 连接：只保留 Redis 元素（Key Pattern 工具条、Redis 命令页签、
        数据页签内的 Redis 数据视图），隐藏「查询」「结构」SQL 专属页签，
        禁用「导出数据库」工具栏按钮；
        MySQL/SQLite 连接：恢复 SQL 页签与导出按钮，隐藏 Redis 元素。
        """
        self._update_redis_ui()
        self._update_sql_tabs()

    def _update_sql_tabs(self):
        """同步 SQL 专属页签显隐、激活页签与工具栏导出按钮。"""
        is_redis = bool(self.current) and self.current.db_type == 'redis'
        # 「查询」「结构」为 SQL 专属页签：Redis 连接时隐藏，SQL 连接时恢复。
        # 「数据」页签为双容器（data_tab_sql / data_tab_redis），Redis 时展示 Redis 数据视图，不整体隐藏。
        for tab, name in [(self.query_tab, '查询'), (self.schema_tab, '结构')]:
            present = any(self.notebook.tab(t, 'text') == name for t in self.notebook.tabs())
            if is_redis and present:
                self.notebook.forget(tab)
            elif not is_redis and not present:
                self.notebook.add(tab, text=name)
        # 恢复页签顺序为 查询/数据/结构（仅 SQL 模式）
        if not is_redis:
            try:
                self.notebook.insert(0, self.query_tab)
            except tk.TclError:
                pass
        # 数据页签内容按连接类型切换
        if is_redis:
            if not self.data_tab_redis.winfo_manager():
                self.data_tab_redis.pack(fill='both', expand=True)
            if self.data_tab_sql.winfo_manager():
                self.data_tab_sql.pack_forget()
        else:
            if not self.data_tab_sql.winfo_manager():
                self.data_tab_sql.pack(fill='both', expand=True)
            if self.data_tab_redis.winfo_manager():
                self.data_tab_redis.pack_forget()
        # 激活页签：Redis 默认激活「Redis 命令」，SQL 默认激活「查询」
        try:
            if is_redis:
                self.notebook.select(self.redis_tab)
            else:
                self.notebook.select(self.query_tab)
        except tk.TclError:
            pass
        # 工具栏「导出数据库」仅 MySQL/SQLite 可用
        state = 'normal' if (self.current and self.current.db_type in ('mysql', 'sqlite')) else 'disabled'
        try:
            self.export_db_btn.config(state=state)
        except AttributeError:
            pass

    def _update_db_menu(self):
        """数据库菜单动态启停：仅 MySQL/SQLite 连接成功时可用。

        未连接或当前连接为 Redis 时整菜单禁用（保持可见但置灰）。
        """
        ok = bool(self.current) and self.current.db_type in ('mysql', 'sqlite')
        state = 'normal' if ok else 'disabled'
        try:
            for i in range(self.db_menu.index('end') + 1):
                try:
                    self.db_menu.entryconfigure(i, state=state)
                except tk.TclError:
                    pass
        except tk.TclError:
            pass

    # ---- 导出整个数据库 / 数据库菜单 ----
    def _selected_db_from_tree(self):
        """返回树中当前选中的数据库名；未选中返回 None。"""
        v = self._node_info()
        if v and v[0] == 'database':
            return v[1]
        return None

    def _choose_database(self):
        """弹窗让用户选择数据库，取消返回 None。"""
        if not self.current:
            return None
        try:
            dbs = self.current.get_databases()
        except Exception as e:
            messagebox.showerror("错误", f"读取数据库列表失败：\n{e}")
            return None
        if not dbs:
            messagebox.showwarning("提示", "当前连接下没有可选数据库")
            return None
        return DatabaseSelectDialog(self, dbs).show()

    def _export_whole_db(self):
        """导出整个数据库（工具栏按钮 / 数据库菜单共用入口）。

        优先导出树中选中的数据库；未选中时弹窗选择。
        """
        if not self.current:
            messagebox.showwarning("提示", "请先建立数据库连接")
            return
        if self.current.db_type == 'redis':
            messagebox.showinfo("提示", "Redis 连接不支持导出整个数据库，请在「Redis 命令」页签执行命令")
            return
        db = self._selected_db_from_tree()
        if not db:
            db = self._choose_database()
        if not db:
            return
        try:
            tables = self.current.get_tables(db)
        except Exception as e:
            messagebox.showerror("错误", f"读取表列表失败：\n{e}")
            return
        dlg = ExportDialog(self, f"整个数据库 {db}（{len(tables)} 张表）",
                           lambda mode, path: self._do_export(db, mode, path))
        self.wait_window(dlg)

    def _set_database_charset_menu(self):
        """数据库菜单：设置数据库字符集（未选中库时弹窗选择）。"""
        if not self.current or self.current.db_type != 'mysql':
            messagebox.showinfo("提示", "SQLite 不支持字符集设置，仅 MySQL 可设置")
            return
        db = self._selected_db_from_tree()
        if not db:
            db = self._choose_database()
        if not db:
            return
        self._set_database_charset(db)

    def _set_table_charset_menu(self):
        """数据库菜单：设置表字符集（未选中表时选择库并输入表名）。"""
        if not self.current or self.current.db_type != 'mysql':
            messagebox.showinfo("提示", "SQLite 不支持字符集设置，仅 MySQL 可设置")
            return
        v = self._node_info()
        if v and v[0] == 'table':
            db, table = v[1], v[2]
        else:
            db = self._selected_db_from_tree() or self._choose_database()
            if not db:
                return
            table = simpledialog.askstring("设置表字符集", "请输入要设置的表名：")
        if not table:
            return
        self._set_table_charset(db, table)

    # ---- 连接 ----
    def _new_conn_menu(self):
        menu = tk.Menu(self, tearoff=0)
        menu.add_command(label="MySQL", command=lambda: self._new_conn('mysql'))
        menu.add_command(label="SQLite", command=lambda: self._new_conn('sqlite'))
        menu.add_command(label="Redis", command=lambda: self._new_conn('redis'))
        try:
            menu.tk_popup(self.winfo_pointerx(), self.winfo_pointery())
        finally:
            menu.grab_release()

    def _new_conn(self, db_type):
        self._dialog_db_type = db_type
        ConnectDialog(self, db_type, self._on_conn_ok)

    def _on_conn_ok(self, cfg):
        if self._dialog_db_type == 'redis':
            conn = RedisConnection(cfg['params'], cfg['name'])
        else:
            conn = DBConnection(self._dialog_db_type, cfg['params'], cfg['name'])
        try:
            conn.connect()
        except Exception as e:
            messagebox.showerror("连接失败", f"无法连接数据库：\n{e}")
            return
        self.connections.append(conn)
        self.current = conn
        self.conn_label.config(text=f"当前连接：{cfg['name']} ({conn.db_type})")
        self._refresh_tree()
        self._update_type_ui()
        self._update_db_menu()
        self._status(f"已连接 {cfg['name']}")

    def _disconnect_current(self):
        if not self.current:
            return
        self.current.close()
        self.connections = [c for c in self.connections if c is not self.current]
        self.current = None
        self.conn_label.config(text="当前连接：无")
        self._close_redis_pane()
        if self.connections:
            # 仍有其他连接：重建树（均收起），保留多类型连接共存
            self._refresh_tree()
        else:
            self.tree.delete(*self.tree.get_children())
        self._update_type_ui()
        self._update_db_menu()
        self._status("已断开连接")

    # ---- 对象树 ----
    def _refresh_tree(self):
        """重建左侧对象树：渲染所有已建立连接，多类型连接共存。

        当前连接展开并填充子节点，其余连接收起到顶层（点击节点可切换）。
        """
        self.tree.delete(*self.tree.get_children())
        if not self.connections:
            return
        for idx, conn in enumerate(self.connections):
            text = conn.name
            if conn.db_type == 'redis':
                text = f"{conn.name} (Redis)"
            elif conn.db_type != 'mysql':
                text = f"{conn.name} ({conn.db_type})"
            is_cur = conn is self.current
            conn_node = self.tree.insert('', 'end', iid=f"conn_{idx}", text=text,
                                         values=('connection', idx), open=is_cur)
            if is_cur:
                self._populate_conn_children(conn_node, conn)

    def _populate_conn_children(self, conn_node, conn):
        """为指定连接节点填充其子节点（Redis：db/key；MySQL/SQLite：库/表/视图）。"""
        if conn.db_type == 'redis':
            pattern = self._redis_pattern_var.get() if hasattr(self, '_redis_pattern_var') else '*'
            for db in conn.get_databases():
                db_node = self.tree.insert(conn_node, 'end', text=f"{db} (…)",
                                           values=('redis_db', db, pattern))
                self.tree.insert(db_node, 'end', text="加载中...", values=('placeholder',))
            return
        for db in conn.get_databases():
            db_node = self.tree.insert(conn_node, 'end', text=db, values=('database', db))
            try:
                tables = conn.get_tables(db)
                views = conn.get_views(db)
            except Exception:
                tables, views = [], []
            if tables:
                t_node = self.tree.insert(db_node, 'end', text="表", values=('folder',))
                for t in tables:
                    self.tree.insert(t_node, 'end', text=t, values=('table', db, t))
            if views:
                v_node = self.tree.insert(db_node, 'end', text="视图", values=('folder',))
                for v in views:
                    self.tree.insert(v_node, 'end', text=v, values=('view', db, v))
            self.tree.item(db_node, open=True)

    def _switch_connection(self, idx):
        """切换当前连接到树中指定索引的连接，并按类型刷新界面。"""
        if idx < 0 or idx >= len(self.connections):
            return
        conn = self.connections[idx]
        if conn is self.current:
            return
        self.current = conn
        self.conn_label.config(text=f"当前连接：{conn.name} ({conn.db_type})")
        self._close_redis_pane()
        self._refresh_tree()
        self._update_type_ui()
        self._update_db_menu()
        self._status(f"已切换连接 {conn.name}")

    def _node_info(self):
        item = self.tree.focus()
        if not item:
            return None
        return self.tree.item(item, 'values')

    # ---- Redis 树 ----
    def _on_tree_open(self, event):
        item = self.tree.focus()
        if not item:
            return
        v = self.tree.item(item, 'values')
        if not v or v[0] != 'redis_db':
            return
        children = self.tree.get_children(item)
        if children and self.tree.item(children[0], 'values')[0] == 'placeholder':
            self.tree.delete(*children)
            self._load_redis_db_keys(item, v[1], v[2] if len(v) > 2 else '*')

    def _load_redis_db_keys(self, db_node, db_label, pattern='*'):
        if not self.current or self.current.db_type != 'redis':
            return
        dbnum = int(db_label[2:]) if db_label.startswith('db') else 0
        q = queue.Queue()

        def worker():
            try:
                keys, total = self.current.scan_keys(dbnum, pattern)
                q.put(('ok', keys, total))
            except Exception as e:
                q.put(('err', str(e)))

        threading.Thread(target=worker, daemon=True).start()
        self._redis_loads.append((db_node, q))
        self.after(100, lambda: self._poll_redis_db(db_node, db_label))

    def _poll_redis_db(self, db_node, db_label):
        q = None
        for item in list(self._redis_loads):
            if item[0] == db_node:
                q = item[1]
                break
        if q is None:
            return
        try:
            result = q.get_nowait()
        except queue.Empty:
            self.after(100, lambda: self._poll_redis_db(db_node, db_label))
            return
        self._redis_loads = [i for i in self._redis_loads if i[0] != db_node]
        status, *rest = result
        if status == 'err':
            self.tree.item(db_node, text=f"{db_label} (加载失败)")
            self._status(f"Redis 加载失败：{rest[0]}")
            return
        keys, total = rest
        self.tree.item(db_node, text=f"{db_label} ({total})")
        for k in keys:
            self._insert_redis_key_node(db_node, db_label, k)
        self.tree.item(db_node, open=True)

    def _insert_redis_key_node(self, db_node, db_label, key):
        try:
            ktype = self.current.get_key_type(int(db_label[2:]), key)
        except Exception:
            ktype = 'string'
        self.tree.insert(db_node, 'end', text=f"[{ktype}] {key}",
                         values=('redis_key', db_label, key, ktype))

    def _reload_redis_db(self, db_node, db_label):
        if not db_node:
            return
        pattern = self._redis_pattern_var.get() if hasattr(self, '_redis_pattern_var') else '*'
        self.tree.delete(*self.tree.get_children(db_node))
        self.tree.item(db_node, text=f"{db_label} (…)",
                       values=('redis_db', db_label, pattern))
        self._load_redis_db_keys(db_node, db_label, pattern)

    def _find_redis_db_node(self, db_label):
        for child in self.tree.get_children():
            v = self.tree.item(child, 'values')
            if v and v[0] == 'connection' and self.tree.item(child, 'text').endswith('(Redis)'):
                for c in self.tree.get_children(child):
                    if self.tree.item(c, 'values')[1] == db_label:
                        return c
        return None

    def _open_redis_key(self, db_label, key, ktype=None):
        if not self.current or self.current.db_type != 'redis':
            return
        dbnum = int(db_label[2:]) if db_label.startswith('db') else 0
        self._show_redis_data_view()
        self._redis_pane = RedisDataPane(self.data_tab_redis, self, self.current, dbnum, key, ktype)
        self.notebook.select(self.data_tab)

    def _show_key_ttl(self, db_label, key):
        try:
            ttl = self.current.get_ttl(int(db_label[2:]), key)
        except Exception as e:
            messagebox.showerror("错误", str(e))
            return
        msg = f"key：{key}\nTTL：{ttl} 秒" + ("（永不过期）" if ttl == -1 else "")
        messagebox.showinfo("查看 TTL", msg)

    def _set_key_ttl(self, db_label, key):
        dbnum = int(db_label[2:])
        try:
            cur = self.current.get_ttl(dbnum, key)
        except Exception:
            cur = -1
        s = simpledialog.askstring("设置 TTL", "过期时间（秒），-1 表示永不过期：",
                                   initialvalue=str(cur))
        if s is None:
            return
        try:
            secs = int(s)
        except ValueError:
            messagebox.showwarning("提示", "请输入整数")
            return
        try:
            self.current.set_ttl(dbnum, key, secs)
            self._status(f"已设置 {key} 的 TTL：{secs}")
        except Exception as e:
            messagebox.showerror("设置失败", str(e))

    def _del_redis_key(self, item, db_label, key):
        if messagebox.askyesno("确认", f"确定删除 key `{key}`？"):
            try:
                self.current.delete_key(int(db_label[2:]), key)
                self.tree.delete(item)
                db_node = self._find_redis_db_node(db_label)
                if db_node:
                    self._reload_redis_db(db_node, db_label)
                self._status(f"已删除 key {key}")
            except Exception as e:
                messagebox.showerror("删除失败", str(e))

    def _on_tree_select(self, event):
        v = self._node_info()
        if not v:
            return
        if v[0] == 'connection':
            # 多连接共存：点击连接节点切换当前连接
            self._switch_connection(int(v[1]))
        elif v[0] == 'table':
            self._open_table(v[1], v[2])
        elif v[0] == 'redis_key':
            # 单击 Redis key 与双击等效：在「数据」页签内渲染该 key 数据
            self._open_redis_key(v[1], v[2], v[3] if len(v) > 3 else None)

    def _on_tree_double(self, event):
        v = self._node_info()
        if v and v[0] == 'table':
            self._open_table(v[1], v[2])
        elif v and v[0] == 'redis_key':
            self._open_redis_key(v[1], v[2], v[3] if len(v) > 3 else None)

    def _on_tree_right_click(self, event):
        item = self.tree.identify_row(event.y)
        if not item:
            return
        self.tree.selection_set(item)
        self.tree.focus(item)
        v = self.tree.item(item, 'values')
        menu = tk.Menu(self, tearoff=0)
        if v[0] == 'redis_db':
            menu.add_command(label="刷新 Key 列表", command=lambda: self._reload_redis_db(item, v[1]))
        elif v[0] == 'redis_key':
            db_label = v[1]
            key = v[2]
            ktype = v[3] if len(v) > 3 else None
            menu.add_command(label="打开", command=lambda: self._open_redis_key(db_label, key, ktype))
            menu.add_command(label="查看 TTL", command=lambda: self._show_key_ttl(db_label, key))
            menu.add_command(label="设置 TTL", command=lambda: self._set_key_ttl(db_label, key))
            menu.add_separator()
            menu.add_command(label="删除 Key", command=lambda: self._del_redis_key(item, db_label, key))
            menu.add_command(label="刷新", command=lambda: self._reload_redis_db(self._find_redis_db_node(db_label), db_label))
        elif v[0] == 'connection':
            if self.current and self.current.db_type == 'redis':
                menu.add_command(label="刷新", command=self._refresh_tree)
            else:
                menu.add_command(label="新建数据库", command=self._new_database)
                menu.add_command(label="导入 SQL 文件", command=self._import_sql_file)
                menu.add_command(label="刷新", command=self._refresh_tree)
        elif v[0] == 'database':
            menu.add_command(label="新建表", command=lambda: self._new_table(v[1]))
            menu.add_command(label="导出整个数据库", command=self._export_whole_db)
            menu.add_command(label="导出数据库", command=lambda: self._export_database(v[1]))
            menu.add_command(label="导入 SQL 文件", command=self._import_sql_file)
            menu.add_command(label="设置数据库字符集", command=lambda: self._set_database_charset(v[1]))
            menu.add_command(label="设置表字符集", command=self._set_table_charset_menu)
            menu.add_command(label="刷新", command=self._refresh_tree)
            menu.add_command(label="删除数据库", command=lambda: self._drop_database(v[1]))
        elif v[0] == 'table':
            db, table = v[1], v[2]
            menu.add_command(label="刷新表", command=lambda: self._open_table(db, table))
            menu.add_command(label="设计表", command=lambda: self._design_table(db, table))
            menu.add_command(label="设置字符集", command=lambda: self._set_table_charset(db, table))
            menu.add_command(label="导出表", command=lambda: self._export_table(db, table))
            menu.add_command(label="清空表", command=lambda: self._truncate_table(db, table))
            menu.add_command(label="删除表", command=lambda: self._drop_table(db, table))
        elif v[0] == 'folder' and self.tree.parent(item):
            parent = self.tree.item(self.tree.parent(item), 'values')
            if parent and parent[0] == 'database':
                menu.add_command(label="新建表", command=lambda: self._new_table(parent[1]))
        if menu.index('end') is not None:
            try:
                menu.tk_popup(event.x_root, event.y_root)
            finally:
                menu.grab_release()

    def _open_table(self, db, table):
        if not self.current:
            return
        self._show_sql_data_view()
        self._data_db, self._data_table, self._data_offset = db, table, 0
        try:
            self._data_columns = [c['name'] for c in self.current.get_columns(table, db)]
        except Exception as e:
            messagebox.showerror("错误", f"打开表失败：\n{e}")
            return
        self._pending = []
        self._refresh_data()
        self.notebook.select(self.data_tab)
        # 结构页
        try:
            lines = [self.current.get_create_sql(table, db), '', "字段："]
            for c in self.current.get_columns(table, db):
                lines.append(f"{c['name']}\t{c['type']}\t主键={'是' if c['pk'] else ''}"
                             f"\t可空={'是' if c['nullable'] else ''}"
                             f"\t默认={c['default']}\t注释={c['comment']}")
            self.schema_text.delete('1.0', 'end')
            self.schema_text.insert('1.0', '\n'.join(str(x) for x in lines))
        except Exception:
            pass

    # ---- 数据展示 ----
    def _refresh_data(self):
        if not self.current or not self._data_table:
            return
        try:
            rows = self.current.fetch_page(self._data_table, self._data_columns,
                                           self._data_db, 200, self._data_offset)
        except Exception as e:
            self._status(f"读取数据失败：{e}")
            return
        self.data_tree.delete(*self.data_tree.get_children())
        self.data_tree['columns'] = self._data_columns
        for c in self._data_columns:
            self.data_tree.heading(c, text=c)
            self.data_tree.column(c, width=110, anchor='w')
        for r in rows:
            vals = [r[c] if isinstance(r, dict) else r[i] for i, c in enumerate(self._data_columns)]
            self.data_tree.insert('', 'end', values=[str(v) if v is not None else '' for v in vals])
        total_str = ''
        try:
            total = self.current.get_row_count(self._data_table, self._data_db)
            total_str = f"  共 {total} 行"
        except Exception:
            pass
        self.page_label.config(
            text=f"表：{self._data_table}  行：{self._data_offset + 1}-{self._data_offset + len(rows)}{total_str}"
                 + (f"  待提交：{len(self._pending)}" if self._pending else ""))

    def _data_page(self, step):
        self._data_offset = max(0, self._data_offset + step * 200)
        self._refresh_data()

    def _row_values(self, item):
        return dict(zip(self._data_columns, self.data_tree.item(item, 'values')))

    # ---- 增删改 ----
    def _add_row(self):
        if not self._data_table:
            return
        cols = self.current.get_columns(self._data_table, self._data_db)
        dlg = RowEditDialog(self, cols, {}, "新增行")
        self.wait_window(dlg)
        if dlg.result:
            self._pending.append(('insert', dlg.result))
            self.data_tree.insert('', 'end', values=[dlg.result.get(c, '') for c in self._data_columns])
            self._update_pending_label()

    def _edit_row(self):
        item = self.data_tree.focus()
        if not item:
            return
        old = self._row_values(item)
        cols = self.current.get_columns(self._data_table, self._data_db)
        dlg = RowEditDialog(self, cols, old, "编辑行")
        self.wait_window(dlg)
        if dlg.result:
            self._pending.append(('update', (dlg.result, old)))
            self.data_tree.item(item, values=[dlg.result.get(c, '') for c in self._data_columns])
            self._update_pending_label()

    def _del_row(self):
        item = self.data_tree.focus()
        if not item:
            return
        old = self._row_values(item)
        self._pending.append(('delete', old))
        self.data_tree.delete(item)
        self._update_pending_label()

    def _update_pending_label(self):
        self.page_label.config(
            text=f"表：{self._data_table}  待提交：{len(self._pending)}")

    def _save_changes(self):
        if not self._pending:
            messagebox.showinfo("提示", "没有待提交的修改")
            return
        try:
            for op, payload in self._pending:
                if op == 'insert':
                    self.current.insert_row(self._data_table, payload, self._data_db)
                elif op == 'update':
                    self.current.update_row(self._data_table, payload[0], payload[1], self._data_db)
                else:
                    self.current.delete_row(self._data_table, payload, self._data_db)
            self._pending = []
            self._refresh_data()
            self._status("修改已保存")
        except Exception as e:
            messagebox.showerror("保存失败", str(e))

    # ---- 导入导出 ----
    def _import_csv(self):
        if not self._data_table:
            return
        path = filedialog.askopenfilename(filetypes=[("CSV 文件", "*.csv")])
        if not path:
            return
        try:
            count = 0
            with open(path, 'r', encoding='utf-8-sig', newline='') as f:
                reader = csv.DictReader(f)
                for row in reader:
                    data = {k: v for k, v in row.items() if k in self._data_columns}
                    if data:
                        self.current.insert_row(self._data_table, data, self._data_db)
                        count += 1
            self._refresh_data()
            self._status(f"导入完成：{path}（{count} 行）")
            messagebox.showinfo("导入完成", f"已从 CSV 导入 {count} 行数据到表 {self._data_table}")
        except Exception as e:
            messagebox.showerror("导入失败", str(e))

    def _export_menu(self):
        if not self._data_table:
            return
        menu = tk.Menu(self, tearoff=0)
        menu.add_command(label="导出 CSV", command=lambda: self._export('csv'))
        menu.add_command(label="导出 SQL", command=lambda: self._export('sql'))
        menu.add_command(label="导出 JSON", command=lambda: self._export('json'))
        try:
            menu.tk_popup(self.winfo_pointerx(), self.winfo_pointery())
        finally:
            menu.grab_release()

    def _export(self, fmt):
        rows = self.current.fetch_page(self._data_table, self._data_columns,
                                       self._data_db, 100000, 0)
        default_name = f"{self._data_table}.{fmt}"
        path = filedialog.asksaveasfilename(defaultextension=f".{fmt}",
                                            initialfile=default_name)
        if not path:
            return
        try:
            if fmt == 'csv':
                with open(path, 'w', encoding='utf-8-sig', newline='') as f:
                    w = csv.DictWriter(f, fieldnames=self._data_columns)
                    w.writeheader()
                    for r in rows:
                        w.writerow({c: (r[c] if isinstance(r, dict) else r[i])
                                    for i, c in enumerate(self._data_columns)})
            elif fmt == 'json':
                data = [{c: (r[c] if isinstance(r, dict) else r[i])
                         for i, c in enumerate(self._data_columns)} for r in rows]
                with open(path, 'w', encoding='utf-8') as f:
                    json.dump(data, f, ensure_ascii=False, indent=2, default=str)
            else:
                with open(path, 'w', encoding='utf-8') as f:
                    for r in rows:
                        vals = [r[c] if isinstance(r, dict) else r[i]
                                for i, c in enumerate(self._data_columns)]
                        f.write(self._to_insert_sql(vals) + ';\n')
            self._status(f"导出完成：{path}")
        except Exception as e:
            messagebox.showerror("导出失败", str(e))

    def _to_insert_sql(self, vals):
        cols = ', '.join(self.current.q(c) for c in self._data_columns)
        vals_str = ', '.join(
            'NULL' if v is None else f"'{str(v).replace(chr(39), chr(39) * 2)}'" for v in vals)
        return f"INSERT INTO {self.current.tbl(self._data_table, self._data_db)} ({cols}) VALUES ({vals_str})"

    # ---- 右键 DDL ----
    def _new_database(self):
        name = simpledialog.askstring("新建数据库", "数据库名:")
        if name:
            try:
                self.current.create_database(name)
                self._refresh_tree()
            except Exception as e:
                messagebox.showerror("失败", str(e))

    def _drop_database(self, db):
        if messagebox.askyesno("确认", f"确定删除数据库 `{db}`？此操作不可恢复！"):
            try:
                self.current.drop_database(db)
                self._refresh_tree()
            except Exception as e:
                messagebox.showerror("失败", str(e))

    def _new_table(self, db):
        DesignTableDialog(self, self.current, database=db, on_done=self._refresh_tree)

    def _design_table(self, db, table):
        DesignTableDialog(self, self.current, table=table, database=db,
                          on_done=lambda: self._open_table(db, table))

    def _drop_table(self, db, table):
        if messagebox.askyesno("确认", f"确定删除表 `{table}`？此操作不可恢复！"):
            try:
                self.current.drop_table(table, db)
                self._refresh_tree()
            except Exception as e:
                messagebox.showerror("失败", str(e))

    def _truncate_table(self, db, table):
        if messagebox.askyesno("确认", f"确定清空表 `{table}` 的所有数据？"):
            try:
                self.current.truncate_table(table, db)
                self._open_table(db, table)
            except Exception as e:
                messagebox.showerror("失败", str(e))

    # ---- 字符集设置 ----
    def _set_database_charset(self, db):
        if not self.current or self.current.db_type != 'mysql':
            messagebox.showinfo("提示", "SQLite 不支持字符集设置，仅 MySQL 可设置")
            return
        dlg = CharsetDialog(self, f"设置数据库 {db} 字符集")
        self.wait_window(dlg)
        if not dlg.result:
            return
        try:
            self.current.set_database_charset(db, dlg.result)
            self._status(f"已设置数据库 {db} 字符集为 {dlg.result}")
            messagebox.showinfo("成功", f"数据库 {db} 字符集已设置为 {dlg.result}")
        except Exception as e:
            messagebox.showerror("设置失败", str(e))

    def _set_table_charset(self, db, table):
        if not self.current or self.current.db_type != 'mysql':
            messagebox.showinfo("提示", "SQLite 不支持字符集设置，仅 MySQL 可设置")
            return
        dlg = CharsetDialog(self, f"设置表 {table} 字符集")
        self.wait_window(dlg)
        if not dlg.result:
            return
        try:
            self.current.set_table_charset(table, db, dlg.result)
            self._status(f"已设置表 {table} 字符集为 {dlg.result}")
            messagebox.showinfo("成功", f"表 {table} 字符集已设置为 {dlg.result}")
        except Exception as e:
            messagebox.showerror("设置失败", str(e))

    # ---- 数据库导出 ----
    def _export_database(self, db):
        if not self.current:
            return
        try:
            tables = self.current.get_tables(db)
        except Exception as e:
            messagebox.showerror("错误", str(e))
            return
        dlg = ExportDialog(self, f"数据库 {db}（{len(tables)} 张表）",
                           lambda mode, path: self._do_export(db, mode, path))
        self.wait_window(dlg)

    def _export_table(self, db, table):
        if not self.current:
            return
        dlg = ExportDialog(self, f"表 {db}.{table}",
                           lambda mode, path: self._do_export(db, mode, path, tables=[table]))
        self.wait_window(dlg)

    def _do_export(self, db, mode, path, tables=None):
        try:
            include_structure = mode in ('both', 'structure')
            include_data = mode in ('both', 'data')
            sql_text = self.current.dump_database(db, include_structure, include_data, tables)
            with open(path, 'w', encoding='utf-8') as f:
                f.write(sql_text)
            self._status(f"导出完成：{path}")
            messagebox.showinfo("导出完成", f"已导出到：\n{path}")
        except Exception as e:
            messagebox.showerror("导出失败", str(e))

    # ---- SQL 文件导入 ----
    def _import_sql_file(self):
        if not self.current:
            messagebox.showwarning("提示", "请先建立数据库连接")
            return
        if self.current.db_type == 'redis':
            messagebox.showinfo("提示", "Redis 连接不支持导入 SQL 文件")
            return
        path = filedialog.askopenfilename(filetypes=[("SQL 文件", "*.sql"), ("所有文件", "*.*")])
        if not path:
            return
        if not messagebox.askyesno("确认", f"将执行 SQL 文件中的全部语句：\n{path}\n\n确定继续？"):
            return
        self._status("正在导入 SQL...")
        try:
            count = self.current.execute_sql_file(path)
            self._refresh_tree()
            self._status(f"导入完成，共执行 {count} 条语句")
            messagebox.showinfo("导入完成", f"成功执行 {count} 条 SQL 语句")
        except Exception as e:
            messagebox.showerror("导入失败", str(e))

    # ---- SQL 历史 ----
    def _load_sql_history(self):
        try:
            with open(SQL_HISTORY_FILE, 'r', encoding='utf-8') as f:
                data = json.load(f)
                return list(data) if isinstance(data, list) else []
        except Exception:
            return []

    def _save_sql_history(self, sql):
        sql = sql.strip()
        if not sql:
            return
        if sql in self.sql_history:
            self.sql_history.remove(sql)
        self.sql_history.insert(0, sql)
        self.sql_history = self.sql_history[:SQL_HISTORY_MAX]
        try:
            with open(SQL_HISTORY_FILE, 'w', encoding='utf-8') as f:
                json.dump(self.sql_history, f, ensure_ascii=False, indent=2)
        except Exception as e:
            self._status(f"保存 SQL 历史失败：{e}")

    def _show_history(self):
        if not self.sql_history:
            messagebox.showinfo("SQL 历史", "暂无历史记录")
            return
        SQLHistoryWindow(self, self.sql_history, self._fill_history_sql, self._clear_history)

    def _fill_history_sql(self, sql):
        self.sql_text.delete('1.0', 'end')
        self.sql_text.insert('1.0', sql)
        self.notebook.select(self.query_tab)
        self._status("已回填历史命令")

    def _clear_history(self):
        self.sql_history = []
        try:
            with open(SQL_HISTORY_FILE, 'w', encoding='utf-8') as f:
                json.dump([], f)
            self._status("SQL 历史已清空")
        except Exception as e:
            self._status(f"清空历史失败：{e}")

    # ---- SQL 执行 ----
    # ---- Redis 命令 ----
    def _run_redis_cmd(self):
        if not self.current:
            messagebox.showwarning("提示", "请先建立 Redis 连接")
            return
        if self.current.db_type != 'redis':
            messagebox.showinfo("提示", "当前不是 Redis 连接，请先新建/切换 Redis 连接")
            return
        cmd = self.redis_cmd_text.get('1.0', 'end').strip()
        if not cmd:
            return
        self._save_redis_history(cmd)
        self._status("Redis 命令执行中...")
        dbnum = int(self.current.params.get('db', 0) or 0)
        q = queue.Queue()

        def worker():
            try:
                tokens = _redis_tokenize(cmd)
                if not tokens:
                    q.put(('err', '无法解析命令'))
                    return
                result = self.current.execute_command(dbnum, tokens)
                q.put(('ok', result))
            except Exception as e:
                q.put(('err', str(e)))

        threading.Thread(target=worker, daemon=True).start()
        self.after(100, lambda: self._poll_redis_result(q))

    def _poll_redis_result(self, q):
        try:
            result = q.get_nowait()
        except queue.Empty:
            self.after(100, lambda: self._poll_redis_result(q))
            return
        status, *rest = result
        self.redis_result.configure(state='normal')
        self.redis_result.delete('1.0', 'end')
        if status == 'err':
            self.redis_result.insert('end', f"错误：{rest[0]}\n")
            self._status("Redis 命令执行失败")
        else:
            self.redis_result.insert('end', _format_redis_result(rest[0]))
            self._status("Redis 命令执行完成")
        self.redis_result.configure(state='disabled')

    # ---- Redis 命令历史 ----
    def _load_redis_history(self):
        try:
            with open(REDIS_HISTORY_FILE, 'r', encoding='utf-8') as f:
                data = json.load(f)
                return list(data) if isinstance(data, list) else []
        except Exception:
            return []

    def _save_redis_history(self, cmd):
        cmd = cmd.strip()
        if not cmd:
            return
        if cmd in self.redis_history:
            self.redis_history.remove(cmd)
        self.redis_history.insert(0, cmd)
        self.redis_history = self.redis_history[:REDIS_HISTORY_MAX]
        try:
            with open(REDIS_HISTORY_FILE, 'w', encoding='utf-8') as f:
                json.dump(self.redis_history, f, ensure_ascii=False, indent=2)
        except Exception as e:
            self._status(f"保存 Redis 历史失败：{e}")

    def _show_redis_history(self):
        if not self.redis_history:
            messagebox.showinfo("Redis 历史", "暂无历史记录")
            return
        SQLHistoryWindow(self, self.redis_history, self._fill_history_redis_cmd,
                         self._clear_redis_history, title="Redis 命令历史")

    def _fill_history_redis_cmd(self, cmd):
        self.redis_cmd_text.delete('1.0', 'end')
        self.redis_cmd_text.insert('1.0', cmd)
        self.notebook.select(self.redis_tab)
        self._status("已回填 Redis 历史命令")

    def _clear_redis_history(self):
        self.redis_history = []
        try:
            with open(REDIS_HISTORY_FILE, 'w', encoding='utf-8') as f:
                json.dump([], f)
            self._status("Redis 历史已清空")
        except Exception as e:
            self._status(f"清空历史失败：{e}")

    def _run_sql(self):
        if not self.current:
            messagebox.showwarning("提示", "请先建立数据库连接")
            return
        if self.current.db_type == 'redis':
            messagebox.showinfo("提示", "当前为 Redis 连接，请切换到「Redis 命令」页签执行命令")
            return
        sql = self.sql_text.get('1.0', 'end').strip()
        if not sql:
            return
        self._save_sql_history(sql)
        self._status("执行中...")
        q = queue.Queue()

        def worker():
            try:
                cols, rows, affected = self._do_query(sql)
                q.put(('ok', cols, rows, affected))
            except Exception as e:
                q.put(('err', str(e)))

        threading.Thread(target=worker, daemon=True).start()
        self.after(100, lambda: self._poll_result(q))

    def _do_query(self, sql):
        stmts = split_sql_statements(sql)
        if len(stmts) > 1:
            for stmt in stmts:
                self.current._execute(stmt)
            return [], [], 0
        if stmts:
            return self.current.execute_query(stmts[0])
        return [], [], 0

    def _poll_result(self, q):
        try:
            result = q.get_nowait()
        except queue.Empty:
            self.after(100, lambda: self._poll_result(q))
            return
        status, *rest = result
        if status == 'err':
            messagebox.showerror("SQL 错误", rest[0])
            return
        cols, rows, affected = rest
        if cols:
            self._fill_result_tree(cols, rows)
            self._status(f"查询完成，共 {len(rows)} 行")
        else:
            self._status(f"执行完成，影响 {affected} 行")

    def _fill_result_tree(self, cols, rows):
        self.query_result.delete(*self.query_result.get_children())
        self.query_result['columns'] = cols
        for c in cols:
            self.query_result.heading(c, text=c)
            self.query_result.column(c, width=120, anchor='w')
        for r in rows:
            if isinstance(r, dict):
                vals = [r[c] for c in cols]
            else:
                vals = list(r)
            self.query_result.insert('', 'end', values=[str(v) if v is not None else '' for v in vals])

    def _status(self, msg):
        self.status_var.set(msg)

    def _about(self):
        messagebox.showinfo(
            "关于",
            "alattlyboy_RemoteTools1.0\n"
                     "   （类 Navicat 完整版）\n"
                     "功能：\n"
                    "       redis连接\n"
                    "       sqlLite连接\n"
                    "       redis连接"
        )


if __name__ == '__main__':
    MainApp().mainloop()
