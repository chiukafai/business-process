"""生成演示数据（独立目录 data_demo），用于界面预览与验收，不碰正式数据。"""
import json
import os

DATA = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data_demo")
os.environ["DATA_DIR"] = DATA

for _f in ("app.db", "app.db-wal", "app.db-shm", "secret.key"):
    _p = os.path.join(DATA, _f)
    if os.path.exists(_p):
        os.remove(_p)

import app as app_module  # noqa: E402
from db import get_db  # noqa: E402
from utils import calc_amounts, now_text, trade_fee_cents  # noqa: E402
from werkzeug.security import generate_password_hash  # noqa: E402

application = app_module.create_app()

DEPTS = [("集团总部", "总部", 10), ("广州公司", "广州", 20),
         ("成都公司", "成都", 30), ("上海公司", "上海", 40)]

USERS = [
    # username, name, dept, role, perms
    ("gz_mgr", "李国强", "广州公司", "manager",
     ["record.create", "record.view_dept", "record.export", "customer.view"]),
    ("gz_staff", "张小明", "广州公司", "staff", ["record.create", "record.import"]),
    ("gz_staff2", "周文静", "广州公司", "staff", ["record.create"]),
    ("cd_staff", "王海涛", "成都公司", "staff", ["record.create", "record.import"]),
    ("sh_staff", "刘思远", "上海公司", "staff", ["record.create"]),
    ("fin", "陈美玲", None, "finance",
     ["record.create_any_dept", "record.view_all", "record.pay",
      "record.export", "customer.view", "customer.manage"]),
]

RECORDS = [
    # 客户, 部门, 类型, 预存, 日期, 费率, 金额, 提成, 预存额, 付款额, 状态, 登记人, 备注
    ("广州华农贸易有限公司", "广州公司", "代采", "后付", "2026-11-01", 1.5, 1200000, 2000, 0, 0, "pending_payment", "gz_staff", "首批蔬菜代采"),
    ("成都蜀鲜果业", "成都公司", "过账", "预存", "2026-11-02", 2.0, 800000, 0, 0, 0, "pending_payment", "cd_staff", ""),
    ("深圳鲜达供应链", "广州公司", "代采", "月结", "2026-11-03", 1.2, 2400000, 8000, 0, 0, "pending_payment", "gz_staff", "月结客户，需开票"),
    ("上海沪果批发", "上海公司", "自营", "后付", "2026-10-28", 1.0, 650000, 0, 0, 650000, "completed", "sh_staff", ""),
    ("广州顺鑫农业", "广州公司", "代采", "后付", "2026-10-25", 1.8, 1800000, 5000, 0, 1795000, "completed", "gz_staff2", "已开票"),
    ("成都川野农产", "成都公司", "代采", "后付", "2026-10-20", 1.5, 950000, 3000, 0, 947000, "completed", "cd_staff", ""),
    ("北京京果联", "集团总部", "过账", "月结", "2026-11-04", 2.2, 3200000, 15000, 0, 0, "rejected", "gz_staff", "费率需与合同核对"),
    ("上海申鲜实业", "上海公司", "代采", "后付", "2026-11-05", 1.6, 1500000, 4000, 0, 0, "draft", "sh_staff", ""),
    ("广州南沙冷链", "广州公司", "自营", "预存", "2026-11-05", 1.1, 780000, 0, 780000, 0, "draft", "gz_staff2", "预存待核销"),
    ("成都天府果品", "成都公司", "代采", "后付", "2026-11-03", 1.4, 1100000, 2200, 0, 0, "pending_payment", "cd_staff", ""),
    ("深圳前海生鲜", "广州公司", "代采", "后付", "2026-10-30", 1.3, 2200000, 6000, 0, 0, "pending_payment", "gz_staff", ""),
    ("上海崇明农场", "上海公司", "自营", "后付", "2026-10-18", 0.9, 420000, 0, 0, 420000, "completed", "sh_staff", ""),
]

with application.app_context():
    db = get_db()
    for name, short, order in DEPTS:
        db.execute(
            """INSERT INTO departments (name, short_name, sort_order) VALUES (?,?,?)
               ON CONFLICT(name) DO UPDATE SET short_name=excluded.short_name""",
            (name, short, order),
        )
    dept_id = {r["name"]: r["id"] for r in db.execute("SELECT id, name FROM departments")}
    user_id = {}

    db.execute("UPDATE users SET password_hash=?, must_change_password=0 WHERE username='admin'",
               (generate_password_hash("Admin@2026"),))
    user_id["admin"] = db.execute("SELECT id FROM users WHERE username='admin'").fetchone()["id"]

    for username, name, dept, role, perms in USERS:
        exists = db.execute("SELECT id FROM users WHERE username=?", (username,)).fetchone()
        if exists:
            user_id[username] = exists["id"]
            continue
        cur = db.execute(
            """INSERT INTO users (username, password_hash, name, dept_id, role, permissions,
                                  is_active, must_change_password)
               VALUES (?,?,?,?,?,?,1,0)""",
            (username, generate_password_hash("Demo@2026"), name,
             dept_id.get(dept), role, json.dumps(perms, ensure_ascii=False)),
        )
        user_id[username] = cur.lastrowid

    if db.execute("SELECT COUNT(*) c FROM records").fetchone()["c"] == 0:
        for (cust, dept, btype, prepaid_mode, bdate, rate, amount, comm,
             prepaid_amt, paid_amt, status, owner, remark) in RECORDS:
            amt_c = int(amount * 100)
            comm_c = int(comm * 100)
            prepaid_c = int(prepaid_amt * 100)
            paid_c = int(paid_amt * 100)
            # 金额口径：贸易费 = 业务金额 × 费率；应收合计 = 贸易费 + 留存利润
            payable = trade_fee_cents(amt_c, rate)
            receivable = payable + comm_c
            # 已完成的单按「应收合计 − 预存」全额收款，待收归零
            if status == "completed":
                paid_c = max(0, receivable - prepaid_c)
            amounts = calc_amounts(amt_c, rate, comm_c, prepaid_c, paid_c)
            oid = user_id[owner]
            oname = db.execute("SELECT name FROM users WHERE id=?", (oid,)).fetchone()["name"]
            cur = db.execute(
                """INSERT INTO records (status, owner_id, owner_name, dept_id, biz_type,
                       prepaid_mode, biz_date, is_invoiced, customer_name, fee_service_cents,
                       biz_amount_cents, fee_rate, channel_commission_cents,
                       company_income_cents, aux_fee_cents, payable_amount_cents,
                       prepaid_amount_cents, received_amount_cents, receipt_closed,
                       unpaid_amount_cents, paid_date, receipt_no, remark,
                       created_at, updated_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (status, oid, oname, dept_id[dept], btype, prepaid_mode, bdate,
                 1 if status == "completed" else 0, cust, int(amt_c * 0.001),
                 amt_c, rate, comm_c,                  amounts["company_income_cents"],
                 0, payable, prepaid_c, paid_c,
                 1 if status == "completed" else 0,
                 amounts["unpaid_amount_cents"],
                 bdate if status == "completed" else None,
                 None,
                 remark, now_text(), now_text()),
            )
            rid = cur.lastrowid
            if status == "completed":
                receipt_no = f"HD{bdate.replace('-', '')}{rid:03d}"
                db.execute("UPDATE records SET receipt_no=?, receipt_closed_at=? WHERE id=?",
                           (receipt_no, now_text(), rid))
                # 演示数据：一笔收齐的收款流水
                db.execute(
                    """INSERT INTO record_receipts (record_id, amount_cents, received_date,
                           receipt_no, note, is_close, actor_id, actor_name, created_at)
                       VALUES (?,?,?,?,?,1,?,?,?)""",
                    (rid, paid_c, bdate, receipt_no, "演示数据·一次收齐", oid, oname, now_text()),
                )

            steps = [("create", None, "draft", "新建登记")]
            if status in ("pending_payment", "completed", "rejected"):
                steps.append(("submit", "draft", "pending_payment", "提交，进入待收款"))
            if status == "completed":
                steps.append(("receipt", "pending_payment", "completed", "收齐客户款项，业务结清"))
            if status == "rejected":
                steps.append(("reject", "pending_payment", "rejected", remark))
            for action, f, t, note in steps:
                db.execute(
                    """INSERT INTO record_logs (record_id, action, from_status, to_status,
                                                actor_id, actor_name, note, created_at)
                       VALUES (?,?,?,?,?,?,?,?)""",
                    (rid, action, f, t, oid, oname, note, now_text()),
                )
    db.commit()

    total = db.execute("SELECT COUNT(*) c FROM records").fetchone()["c"]
    print(f"演示数据就绪：{total} 条业务，{len(USERS)+1} 个账号")
    print("管理员 admin / Admin@2026")
    print("财务 fin / Demo@2026   广州主管 gz_mgr / Demo@2026   广州业务员 gz_staff / Demo@2026")
