"""数据库连接、初始化与种子数据。"""
import json
import os
import re
import sqlite3

import click
from flask import current_app, g
from flask.cli import with_appcontext
from werkzeug.security import generate_password_hash

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
SCHEMA_PATH = os.path.join(BASE_DIR, "schema.sql")

# 权限码字典：(code, label, group, 默认拥有该权限的角色列表)
PERMISSIONS = [
    ("record.create",          "登记业务",           "业务", ["staff", "manager"]),
    ("record.create_any_dept", "跨部门登记",         "业务", ["finance"]),
    ("record.view_dept",       "查看本部门全部业务", "查看", ["manager"]),
    ("record.view_all",        "查看全部部门业务",   "查看", ["finance"]),
    ("record.pay",             "收款登记 / 费用登记 / 录回单", "收款", ["finance"]),
    ("record.import",          "Excel 导入业务",     "数据", ["staff", "manager"]),
    ("record.export",          "导出 Excel",         "数据", ["manager", "finance"]),
    ("customer.view",          "查看客户档案",       "客户", ["staff", "manager", "finance"]),
    ("customer.manage",        "维护 / 导入客户",    "客户", ["staff", "manager", "finance"]),
    ("record.delete",          "删除业务（含任意状态）", "管理", ["manager", "finance"]),
    ("admin.users",            "人员与权限管理",     "管理", []),
    ("admin.master",           "部门 / 字典维护",    "管理", []),
]

# 已废弃的权限码：初始化时从目录与人员权限里清掉
RETIRED_PERMISSIONS = ["record.approve", "record.approve_own"]


def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(
            current_app.config["DATABASE"],
            detect_types=sqlite3.PARSE_DECLTYPES,
        )
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys = ON")
        g.db.execute("PRAGMA journal_mode = WAL")
        g.db.execute("PRAGMA busy_timeout = 5000")
    return g.db


def close_db(e=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def _columns(db, table):
    return {row["name"] for row in db.execute(f"PRAGMA table_info({table})")}


def _records_dept_nullable(db):
    """把 records.dept_id 的 NOT NULL 放开（SQLite 改约束只能重建表）。

    背景：导入贸易计划时，若客户档案里这家主体公司还没挂归属部门，业务先留空
    （待确认），确认后才能提交进入收款 —— 所以 dept_id 必须允许 NULL。
    重建表会保留全部列、自增 id 与数据，索引重建在函数末尾补回。
    """
    info = db.execute("PRAGMA table_info(records)").fetchall()
    col = next((r for r in info if r["name"] == "dept_id"), None)
    if col is None or not col["notnull"]:
        return                                  # 新库/已迁移过，直接跳过

    row = db.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'records'"
    ).fetchone()
    ddl = (row["sql"] if row else "") or ""
    new_ddl, replaced = re.subn(r"(dept_id\s+INTEGER)\s+NOT NULL", r"\1", ddl, count=1)
    if not replaced:
        return
    new_ddl = re.sub(r"^CREATE TABLE\s+(IF NOT EXISTS\s+)?[\"\'`]?records[\"\'`]?",
                     "CREATE TABLE records__new", new_ddl, count=1, flags=re.IGNORECASE)

    cols = ", ".join(f'"{r["name"]}"' for r in info)
    db.commit()                                 # PRAGMA 在事务里无效，先落盘
    db.execute("PRAGMA foreign_keys = OFF")
    db.execute("PRAGMA legacy_alter_table = ON")
    try:
        db.execute(new_ddl)
        db.execute(f"INSERT INTO records__new ({cols}) SELECT {cols} FROM records")
        db.execute("DROP TABLE records")
        db.execute("ALTER TABLE records__new RENAME TO records")
        try:
            db.execute(
                "UPDATE sqlite_sequence SET name = 'records' WHERE name = 'records__new'"
            )
        except sqlite3.OperationalError:
            pass                                # 没有自增序列记录时忽略
        # DROP TABLE 会把索引一并删掉，全部补回
        for idx in (
            "CREATE INDEX IF NOT EXISTS idx_records_status   ON records(status, dept_id)",
            "CREATE INDEX IF NOT EXISTS idx_records_owner    ON records(owner_id, updated_at DESC)",
            "CREATE INDEX IF NOT EXISTS idx_records_bizdate  ON records(biz_date DESC)",
            "CREATE INDEX IF NOT EXISTS idx_records_customer ON records(customer_name)",
            "CREATE INDEX IF NOT EXISTS idx_records_receipt  ON records(receipt_no)",
            "CREATE INDEX IF NOT EXISTS idx_records_cust_id  ON records(customer_id)",
        ):
            db.execute(idx)
        db.commit()
    finally:
        db.execute("PRAGMA legacy_alter_table = OFF")
        db.execute("PRAGMA foreign_keys = ON")


def _migrate(db):
    """把旧库升级到当前结构，可重复执行。"""
    # 1. 补齐新增列
    add_cols = {
        "users": [("wecom_userid", "TEXT")],
        "customers": [("invoice_quota_cents", "INTEGER NOT NULL DEFAULT 0")],
        "records": [
            ("customer_id", "INTEGER"),
            ("receipt_file", "TEXT"),
            ("receipt_ocr_text", "TEXT"),
            ("receipt_ocr_at", "TEXT"),
            # 上游公司（来源：贸易计划模板 C 列）
            ("upstream_company", "TEXT"),
            # 费用明细：每项单独填列，替代原来的单一「费用类型」下拉
            ("fee_service_cents", "INTEGER NOT NULL DEFAULT 0"),
            ("fee_overnight_cents", "INTEGER NOT NULL DEFAULT 0"),
            ("fee_finance_cents", "INTEGER NOT NULL DEFAULT 0"),
            ("fee_other_cents", "INTEGER NOT NULL DEFAULT 0"),
            # 收款：可分批收，累计已收与结清标记
            ("received_amount_cents", "INTEGER NOT NULL DEFAULT 0"),
            ("receipt_closed", "INTEGER NOT NULL DEFAULT 0"),
            ("receipt_closed_at", "TEXT"),
            ("fee_registered_at", "TEXT"),
            ("fee_registered_by", "TEXT"),
        ],
    }
    for table, cols in add_cols.items():
        have = _columns(db, table)
        for name, ddl in cols:
            if name not in have:
                db.execute(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")

    # 老库把 records.dept_id 的 NOT NULL 放开（归属部门允许留空 = 待确认）
    _records_dept_nullable(db)

    # 老库补完列之后再建依赖该列的索引
    db.execute("CREATE INDEX IF NOT EXISTS idx_records_cust_id ON records(customer_id)")

    # 1.1 收款流水表（老库缺表时补建）
    db.execute(
        """CREATE TABLE IF NOT EXISTS record_receipts (
             id            INTEGER PRIMARY KEY AUTOINCREMENT,
             record_id     INTEGER NOT NULL REFERENCES records(id) ON DELETE CASCADE,
             amount_cents  INTEGER NOT NULL DEFAULT 0,
             received_date TEXT,
             receipt_no    TEXT,
             note          TEXT,
             is_close      INTEGER NOT NULL DEFAULT 0,
             actor_id      INTEGER,
             actor_name    TEXT,
             created_at    TEXT NOT NULL DEFAULT (datetime('now','localtime'))
           )"""
    )
    db.execute(
        "CREATE INDEX IF NOT EXISTS idx_receipts_record ON record_receipts(record_id, id)"
    )

    # 1.2 历史「付款金额」搬到「已收金额」（一次性，避免覆盖后续手工登记）
    done_receipt = db.execute(
        "SELECT value FROM app_settings WHERE key = 'receipt_backfill_v1'"
    ).fetchone()
    if not done_receipt:
        db.execute(
            """UPDATE records SET received_amount_cents = paid_amount_cents
                WHERE received_amount_cents = 0 AND paid_amount_cents > 0"""
        )
        db.execute(
            """UPDATE records SET receipt_closed = 1
                WHERE status = 'completed' AND receipt_closed = 0"""
        )
        # 历史已完成但没有收款流水的，补一条结清流水，详情页时间线才说得通
        db.execute(
            """INSERT INTO record_receipts (record_id, amount_cents, received_date,
                                            receipt_no, note, is_close, actor_name, created_at)
               SELECT r.id, r.received_amount_cents, r.paid_date, r.receipt_no,
                      '历史数据迁移：按已完成状态补记收款结清', 1, '系统迁移', datetime('now','localtime')
                 FROM records r
                WHERE r.status = 'completed'
                  AND NOT EXISTS (SELECT 1 FROM record_receipts x WHERE x.record_id = r.id)"""
        )
        db.execute(
            "INSERT INTO app_settings (key, value) VALUES ('receipt_backfill_v1', '1') "
            "ON CONFLICT(key) DO UPDATE SET value = '1'"
        )

    # 2. 状态机简化：取消审核环节，旧的进行中/待审核并入待收款
    db.execute(
        "UPDATE records SET status = 'pending_payment' "
        "WHERE status IN ('in_progress','pending_approval')"
    )

    # 3. 费用类型口径变更：「通道费」统一改称「资金过夜费」（历史文本数据一并改名）
    db.execute(
        "UPDATE records SET fee_type = REPLACE(fee_type, '通道费', '资金过夜费') "
        "WHERE fee_type LIKE '%通道费%'"
    )

    # 4. 清掉已废弃权限：目录 + 每个人权限 JSON
    marks = ",".join("?" * len(RETIRED_PERMISSIONS))
    db.execute(f"DELETE FROM permission_catalog WHERE code IN ({marks})", RETIRED_PERMISSIONS)
    for row in db.execute("SELECT id, permissions FROM users").fetchall():
        try:
            codes = json.loads(row["permissions"] or "[]")
        except ValueError:
            codes = []
        kept = [c for c in codes if c not in RETIRED_PERMISSIONS]
        if len(kept) != len(codes):
            db.execute(
                "UPDATE users SET permissions = ? WHERE id = ?",
                (json.dumps(kept, ensure_ascii=False), row["id"]),
            )

    # 4. 一次性回填新增权限（客户模块），避免老账号看不到新菜单
    NEW_PERMS = ("customer.view", "customer.manage")
    done = db.execute(
        "SELECT value FROM app_settings WHERE key = 'perm_backfill_v2'"
    ).fetchone()
    if not done:
        role_default = {}
        for code, label, group, roles in PERMISSIONS:
            role_default[code] = roles
        for row in db.execute("SELECT id, role, permissions FROM users").fetchall():
            try:
                codes = json.loads(row["permissions"] or "[]")
            except ValueError:
                codes = []
            added = [
                code for code in NEW_PERMS
                if code not in codes and row["role"] in role_default.get(code, [])
            ]
            if added:
                db.execute(
                    "UPDATE users SET permissions = ? WHERE id = ?",
                    (json.dumps(codes + added, ensure_ascii=False), row["id"]),
                )
        db.execute(
            "INSERT INTO app_settings (key, value) VALUES ('perm_backfill_v2', '1') "
            "ON CONFLICT(key) DO UPDATE SET value = '1'"
        )

    # 4.1 一次性回填删除权限：主管 / 财务默认拥有（新增人员按角色默认，老账号补一次）
    DELETE_ROLES = ("manager", "finance")
    done3 = db.execute(
        "SELECT value FROM app_settings WHERE key = 'perm_backfill_v3'"
    ).fetchone()
    if not done3:
        for row in db.execute("SELECT id, role, permissions FROM users").fetchall():
            if row["role"] not in DELETE_ROLES:
                continue
            try:
                codes = json.loads(row["permissions"] or "[]")
            except ValueError:
                codes = []
            if "record.delete" not in codes:
                db.execute(
                    "UPDATE users SET permissions = ? WHERE id = ?",
                    (json.dumps(codes + ["record.delete"], ensure_ascii=False), row["id"]),
                )
        db.execute(
            "INSERT INTO app_settings (key, value) VALUES ('perm_backfill_v3', '1') "
            "ON CONFLICT(key) DO UPDATE SET value = '1'"
        )

    # 5. 历史客户名回填成客户档案（按名称去重，不覆盖已存在的档案）
    db.execute(
        """INSERT INTO customers (name, created_by, created_at, updated_at)
           SELECT DISTINCT TRIM(customer_name), '系统迁移',
                  datetime('now','localtime'), datetime('now','localtime')
             FROM records
            WHERE customer_id IS NULL
              AND TRIM(COALESCE(customer_name,'')) <> ''
              AND TRIM(customer_name) NOT IN (SELECT name FROM customers)"""
    )
    db.execute(
        """UPDATE records
              SET customer_id = (SELECT c.id FROM customers c WHERE c.name = TRIM(records.customer_name))
            WHERE customer_id IS NULL
              AND TRIM(COALESCE(customer_name,'')) <> ''"""
    )


def init_db():
    """建表 + 写入种子数据，可重复执行。"""
    db = get_db()
    with open(SCHEMA_PATH, "r", encoding="utf-8") as f:
        db.executescript(f.read())

    _migrate(db)

    # 权限目录
    for idx, (code, label, group, default_roles) in enumerate(PERMISSIONS):
        db.execute(
            """INSERT INTO permission_catalog (code, label, group_name, role_default, sort_order)
               VALUES (?,?,?,?,?)
               ON CONFLICT(code) DO UPDATE SET
                 label=excluded.label, group_name=excluded.group_name,
                 role_default=excluded.role_default, sort_order=excluded.sort_order""",
            (code, label, group, json.dumps(default_roles, ensure_ascii=False), (idx + 1) * 10),
        )

    # 兜底部门（已有数据时不动）
    if db.execute("SELECT COUNT(*) c FROM departments").fetchone()["c"] == 0:
        db.execute(
            "INSERT INTO departments (name, short_name, sort_order) VALUES (?,?,?)",
            ("集团总部", "总部", 10),
        )

    # 兜底管理员：admin / Hailu@2026，首次登录强制改密
    if db.execute("SELECT COUNT(*) c FROM users").fetchone()["c"] == 0:
        db.execute(
            """INSERT INTO users (username, password_hash, name, role, permissions,
                                  is_active, must_change_password)
               VALUES (?,?,?,?,?,1,1)""",
            (
                "admin",
                generate_password_hash("Hailu@2026"),
                "系统管理员",
                "admin",
                json.dumps([p[0] for p in PERMISSIONS], ensure_ascii=False),
            ),
        )
    db.commit()


@click.command("init-db")
@with_appcontext
def init_db_command():
    """初始化数据库：建表并写入权限目录与默认管理员。"""
    init_db()
    click.echo("数据库初始化完成。默认账号 admin / Hailu@2026（首次登录需改密）")


@click.command("reset-password")
@click.argument("username")
@click.argument("new_password")
@with_appcontext
def reset_password_command(username, new_password):
    """忘记密码时重置：flask reset-password <用户名> <新密码>"""
    db = get_db()
    row = db.execute("SELECT id FROM users WHERE username = ?", (username,)).fetchone()
    if not row:
        click.echo(f"用户 {username} 不存在")
        return
    db.execute(
        "UPDATE users SET password_hash = ?, must_change_password = 1 WHERE id = ?",
        (generate_password_hash(new_password), row["id"]),
    )
    db.commit()
    click.echo(f"已重置 {username} 的密码，该用户下次登录需重新设置密码")


def init_app(app):
    app.teardown_appcontext(close_db)
    app.cli.add_command(init_db_command)
    app.cli.add_command(reset_password_command)
