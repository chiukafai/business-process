"""通知与待办可见性自检。

两件事：
1. **谁该收到通知**：提交 → 资管；收款确认 → 登记的业务员；收齐 → 业务员（+ 需要开票时财务）。
   财务、其他部门的人在「上一步没轮到他」时不该收到。
2. **谁不该看到别人的待收款**：「待收款跟进」只给主管看本部门进度，
   财务（record.pay / view_all）不该被别人的待收业务天天占着待办。

站内通知不依赖企业微信配置，所以这里把企业微信关着跑，
验证「一条外部推送都没配」时，用户依然能在系统里收到提醒。
"""
import os
import sys
import tempfile

BASE = os.getcwd()
TMP = tempfile.mkdtemp(prefix="hailu_notify_")
os.environ["DATA_DIR"] = TMP
sys.path.insert(0, BASE)

from werkzeug.security import generate_password_hash  # noqa: E402

import app as app_module  # noqa: E402
from db import get_db  # noqa: E402

application = app_module.create_app()
application.config["TESTING"] = True
client = application.test_client()

passed = failed = 0


def check(name, cond, extra=""):
    global passed, failed
    if cond:
        passed += 1
        print(f"  OK   {name}")
    else:
        failed += 1
        print(f"  FAIL {name}" + (f"  →  {extra}" if extra else ""))


def q(sql, args=()):
    with application.app_context():
        return get_db().execute(sql, args).fetchall()


def one(sql, args=()):
    with application.app_context():
        return get_db().execute(sql, args).fetchone()


def notes(username):
    """某人的站内通知（新的在前）。"""
    rows = q(
        """SELECT n.*, u.username FROM notifications n
             JOIN users u ON u.id = n.user_id
            WHERE u.username = ? ORDER BY n.id DESC""",
        (username,),
    )
    return rows


def login(username, password):
    c = application.test_client()
    r = c.post("/login", data={"username": username, "password": password},
               follow_redirects=True)
    return c, r


# ---------- 0. 建库 + 账号 ----------
print("=== 0. 准备账号：资管 / 业务员 / 主管 / 财务 ===")
client.post("/login", data={"username": "admin", "password": "Hailu@2026"})
client.post("/change-password", data={
    "old_password": "Hailu@2026", "new_password": "Admin@2026",
    "confirm_password": "Admin@2026"}, follow_redirects=True)

with application.app_context():
    db = get_db()
    dept_ids = {}
    for name in ("资管", "市场部", "财务部", "集群事业部"):
        row = db.execute("SELECT id FROM departments WHERE name = ?", (name,)).fetchone()
        dept_ids[name] = row["id"] if row else db.execute(
            "INSERT INTO departments (name, short_name, sort_order) VALUES (?,?,50)",
            (name, name)).lastrowid
    users = [
        ("zg",   "资管员", "资管",       "manager", ["record.pay", "record.view_all"], "wx_zg"),
        ("sales", "业务员", "市场部",     "staff",   ["record.create"], "wx_sales"),
        ("boss", "主管",   "市场部",     "manager", ["record.create", "record.view_dept",
                                                    "record.close_short"], "wx_boss"),
        ("fin",  "财务",   "财务部",     "finance", ["record.pay", "record.view_all",
                                                    "record.invoice",
                                                    "record.create_any_dept"], "wx_fin"),
    ]
    for uname, cname, dept, role, perms, _wx in users:
        db.execute(
            """INSERT INTO users (username, password_hash, name, dept_id, role, permissions,
                                  is_active, must_change_password, wecom_userid)
               VALUES (?,?,?,?,?,?,1,0,?)""",
            (uname, generate_password_hash("Test@2026"), cname, dept_ids[dept], role,
             __import__("json").dumps(perms), _wx),
        )
    db.commit()
check("四个账号建好", len(q("SELECT id FROM users WHERE username IN ('zg','sales','boss','fin')")) == 4)

# 企业微信保持关闭：验证站内通知不依赖它
with application.app_context():
    db = get_db()
    row = db.execute("SELECT value FROM app_settings WHERE key = 'wecom_enabled'").fetchone()
check("企业微信处于未启用状态（模拟真实环境）", row is None or row["value"] != "1")

sales_client, _ = login("sales", "Test@2026")
zg_client, _ = login("zg", "Test@2026")
boss_client, _ = login("boss", "Test@2026")
fin_client, _ = login("fin", "Test@2026")

# ---------- 1. 登记 + 提交 ----------
print("\n=== 1. 业务员登记并提交 → 只该资管收到 ===")
FORM = {
    "dept_id": str(dept_ids["市场部"]),
    "customer_name": "通知测试客户",
    "biz_type": "代采",
    "prepaid_mode": "后付",
    "biz_year": "2026",
    "biz_month_no": "11",
    "biz_amount": "100000",
    "fee_rate": "1.2",          # 贸易费 1200 元
    "channel_commission": "0",
    "invoice_needed": "1",      # 要开票
    "prepaid_amount": "0",
    "fee_service": "", "fee_overnight": "", "fee_finance": "", "fee_other": "",
    "remark": "通知测试",
}
sales_client.post("/records/new", data=dict(FORM, action="submit"), follow_redirects=True)
rid = one("SELECT MAX(id) m FROM records")["m"]
check("业务已进入待收款", one("SELECT status FROM records WHERE id=?", (rid,))["status"]
      == "pending_payment")

check("资管收到「业务待确认收款」通知", len(notes("zg")) == 1
      and notes("zg")[0]["event"] == "submit", str([n["event"] for n in notes("zg")]))
check("业务员自己没有这条通知（提交时不该提醒他自己）", notes("sales") == [])
check("财务没有收到任何收款相关通知", notes("fin") == [])
check("主管没有收到别人的提交通知", notes("boss") == [])

# ---------- 2. 待办可见性矩阵 ----------
print("\n=== 2. 待办：谁能看到「待收款跟进」 ===")
todo_zg = zg_client.get("/todos/").get_data(as_text=True)
todo_boss = boss_client.get("/todos/").get_data(as_text=True)
todo_fin = fin_client.get("/todos/").get_data(as_text=True)

check("资管有「待我确认收款」", "待我确认收款" in todo_zg)
check("资管没有「待收款跟进」（他已有全量待确认清单）", "待收款跟进" not in todo_zg)
check("主管有「待收款跟进」", "待收款跟进" in todo_boss)
check("主管能看到本部门这笔待收款", "通知测试客户" in todo_boss)
check("财务没有「待收款跟进」", "待收款跟进" not in todo_fin)
check("财务也没有「待我确认收款」（那是资管的活）", "待我确认收款" not in todo_fin)
check("财务仍有「待我确认开票」", "待我确认开票" in todo_fin)

# ---------- 3. 资管确认第一笔收款（未收齐） ----------
print("\n=== 3. 资管确认收款 → 通知业务员 ===")
with application.app_context():
    row = get_db().execute("SELECT * FROM records WHERE id=?", (rid,)).fetchone()
check("应收合计 = 贸易费 1200（留存利润 0）",
      row["payable_amount_cents"] + row["channel_commission_cents"] == 120000,
      str(row["payable_amount_cents"]))

zg_client.post(f"/records/{rid}/receipts", data={
    "trade_fee": "500", "retention": "", "overnight": "",
    "channel": "", "finance": "", "other": "",
    "received_date": "2026-11-09", "receipt_no": "HD-N-001", "note": "首款",
}, follow_redirects=True)

check("业务员收到「收款已确认」通知",
      any(n["event"] == "receipt" for n in notes("sales")),
      str([n["event"] for n in notes("sales")]))
r_sales = notes("sales")
check("通知里带收款金额", r_sales and "500.00" in (r_sales[0]["body"] or ""),
      r_sales[0]["body"] if r_sales else "-")
check("财务这时仍没有通知（还没轮到开票）", notes("fin") == [])
check("主管没有收到这类通知（他不经手收款确认）", notes("boss") == [])

# ---------- 4. 收齐 → 业务员 + 需要开票的财务 ----------
print("\n=== 4. 收齐 → 通知业务员与财务 ===")
zg_client.post(f"/records/{rid}/receipts", data={
    "trade_fee": "700", "retention": "", "overnight": "",
    "channel": "", "finance": "", "other": "",
    "received_date": "2026-11-09", "receipt_no": "HD-N-002", "note": "尾款",
}, follow_redirects=True)
row = one("SELECT * FROM records WHERE id=?", (rid,))
check("款项已收齐", int(row["receipt_closed"]) == 1 and row["status"] == "completed", row["status"])
check("业务员收到「已收齐」通知",
      any(n["event"] == "paid" for n in notes("sales")),
      str([n["event"] for n in notes("sales")]))
check("财务收到「已收齐、需要开票」通知",
      any(n["event"] == "paid" for n in notes("fin")),
      str([n["event"] for n in notes("fin")]))
check("财务通知里说要开票", any("请财务确认开票" in (n["body"] or "") for n in notes("fin")))

todo_fin2 = fin_client.get("/todos/").get_data(as_text=True)
check("财务待办里出现这笔待开票业务", "通知测试客户" in todo_fin2)
check("财务待办仍没有「待收款跟进」", "待收款跟进" not in todo_fin2)

# ---------- 5. 站内通知页面 ----------
print("\n=== 5. 我的通知：未读数、已读、跳转 ===")
page = sales_client.get("/notifications/").get_data(as_text=True)
check("通知页能看到自己的通知", "通知测试客户" in page and "收款已确认" in page)
check("侧边栏有「我的通知」入口", "我的通知" in page)

unread = [n for n in notes("sales") if not n["read_at"]]
check("业务员有未读通知", len(unread) >= 1, str(len(unread)))

first = unread[0]
r = sales_client.get(f"/notifications/{first['id']}/open")
check("点开通知跳到业务详情", r.status_code == 302 and f"/records/{rid}" in r.headers["Location"],
      r.headers.get("Location", ""))
check("点开后标为已读",
      one("SELECT read_at FROM notifications WHERE id=?", (first["id"],))["read_at"] is not None)

sales_client.post("/notifications/read-all", follow_redirects=True)
check("全部标为已读后没有未读",
      all(n["read_at"] for n in notes("sales")), str([n["read_at"] for n in notes("sales")]))

# 别人不能看自己的通知
other = one("SELECT id FROM notifications WHERE user_id = "
            "(SELECT id FROM users WHERE username='zg') LIMIT 1")
if other:
    r = sales_client.get(f"/notifications/{other['id']}/open")
    check("不能点开别人的通知（只跳回列表）",
          "/notifications" in r.headers.get("Location", ""), r.headers.get("Location", ""))

# ---------- 6. 企业微信启用时仍然发外网消息 ----------
print("\n=== 6. 企业微信启用后，站内 + 外部两路都走 ===")
from unittest.mock import patch  # noqa: E402
import notifier  # noqa: E402

sent = []
with application.app_context():
    db = get_db()
    fake_cfg = {"enabled": True, "corp_id": "x", "agent_id": "1", "secret": "s"}
    with patch.object(notifier, "config", return_value=fake_cfg), \
         patch.object(notifier, "send_text",
                      side_effect=lambda db_, ids, content: sent.append((list(ids), content))):
        notifier.notify_next(db, rid, "receipt", "资管已确认收款 1.00 元")
check("企业微信启用后仍按 userid 发送", sent and sent[0][0] == ["wx_sales"], str(sent[:1]))
check("同一次调用也写了站内通知",
      len([n for n in notes("sales") if "1.00" in (n["body"] or "")]) >= 1)


print("\n" + "=" * 46)
print(f"通过 {passed} 项，失败 {failed} 项")
if failed:
    sys.exit(1)
print("全部通过 ✅")
