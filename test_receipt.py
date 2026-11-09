"""收款流程自检：提交 → 待收款 → 分批收款（留空/填0/收满）→ 费用登记 → 删除重算。

独立临时库运行，不动真实数据。
"""
import os
import sys
import tempfile

BASE = os.getcwd()
TMP = tempfile.mkdtemp(prefix="hailu_receipt_")
os.environ["DATA_DIR"] = TMP
sys.path.insert(0, BASE)

import app as app_module  # noqa: E402
from db import get_db  # noqa: E402
from utils import receipt_progress  # noqa: E402

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


def rec(rid):
    with application.app_context():
        return get_db().execute("SELECT * FROM records WHERE id = ?", (rid,)).fetchone()


def receipts(rid):
    with application.app_context():
        return get_db().execute(
            "SELECT * FROM record_receipts WHERE record_id = ? ORDER BY id", (rid,)
        ).fetchall()


def body(path):
    return client.get(path).get_data(as_text=True)


# ---------- 登录 ----------
print("=== 0. 登录 ===")
client.post("/login", data={"username": "admin", "password": "Hailu@2026"})
r = client.post("/change-password", data={
    "old_password": "Hailu@2026", "new_password": "Admin@2026",
    "confirm_password": "Admin@2026",
}, follow_redirects=True)
check("改密后进入系统", r.status_code == 200)

with application.app_context():
    dept_id = get_db().execute("SELECT id FROM departments ORDER BY id").fetchone()["id"]

FORM = {
    "dept_id": str(dept_id),
    "customer_name": "收款测试客户",
    "biz_type": "代采",
    "prepaid_mode": "后付",
    "biz_year": "2026",
    "biz_month_no": "11",
    "biz_amount": "100000",
    "fee_rate": "1.2",          # 贸易费 = 100000 × 1.2% = 1200 元
    "channel_commission": "0",  # 留存利润（导入贸易计划时取 K 列合计）
    "invoice_needed": "1",      # 登记时要选「要 / 不要」
    "prepaid_amount": "0",
    "fee_service": "",
    "fee_overnight": "",
    "fee_finance": "",
    "fee_other": "",
    "remark": "收款流程测试",
}

# ---------- 1. 新建并提交 ----------
print("\n=== 1. 新建登记 → 提交进入待收款 ===")
client.post("/records/new", data=dict(FORM, action="submit"), follow_redirects=True)
with application.app_context():
    rid = get_db().execute("SELECT MAX(id) m FROM records").fetchone()["m"]
row = rec(rid)
check("提交后状态 = pending_payment（待收款）", row["status"] == "pending_payment", row["status"])
check("已收金额初始为 0", row["received_amount_cents"] == 0, str(row["received_amount_cents"]))
check("贸易费 = 100000×1.2% = 1200 元", row["payable_amount_cents"] == 120000,
      str(row["payable_amount_cents"]))
check("待收 = 贸易费 120000 + 留存利润 0 = 120000 分", row["unpaid_amount_cents"] == 120000,
      str(row["unpaid_amount_cents"]))
check("收款结清标记为 0", row["receipt_closed"] == 0)

detail = body(f"/records/{rid}")
check("详情页状态显示「待收款」", "待收款" in detail)
check("详情页有「收款登记」卡片", "收款登记" in detail)
check("详情页说明「全留空 = 还有款项待收」",
      "各项全留空" in detail and "还有款项待收" in detail)
check("详情页有「费用登记」区块", "费用登记" in detail)
check("旧的「确认付款」按钮已移除", "确认付款" not in detail)
check("不再显示「未付款金额」字样", "未付款金额" not in detail)

# ---------- 2. 部分收款 ----------
print("\n=== 2. 部分收款，仍挂待收款 ===")
client.post(f"/records/{rid}/receipts", data={
    "received_amount": "300", "received_date": "2026-11-06",
    "receipt_no": "HD-001", "note": "首款",
}, follow_redirects=True)
row = rec(rid)
check("部分收款后状态仍是待收款", row["status"] == "pending_payment", row["status"])
check("累计已收 30000 分", row["received_amount_cents"] == 30000, str(row["received_amount_cents"]))
check("待收 = 120000 − 30000 = 90000 分", row["unpaid_amount_cents"] == 90000,
      str(row["unpaid_amount_cents"]))
check("流水写入 1 条", len(receipts(rid)) == 1)
check("回单号带到主表", row["receipt_no"] == "HD-001", str(row["receipt_no"]))
check("最近收款日期写入", row["paid_date"] == "2026-11-06", str(row["paid_date"]))

# ---------- 3. 金额留空：还有款项待收 ----------
print("\n=== 3. 本次金额留空 = 仍有款项待收 ===")
client.post(f"/records/{rid}/receipts", data={
    "received_amount": "", "received_date": "2026-11-06", "note": "客户说下周付",
}, follow_redirects=True)
row = rec(rid)
check("留空不改变已收金额", row["received_amount_cents"] == 30000, str(row["received_amount_cents"]))
check("留空不改变状态", row["status"] == "pending_payment", row["status"])
check("留空也留一条流水", len(receipts(rid)) == 2)
check("流水金额为 0 且非结清", receipts(rid)[-1]["amount_cents"] == 0
      and receipts(rid)[-1]["is_close"] == 0)

detail = body(f"/records/{rid}")
check("流水区显示「未收款」标记", "未收款" in detail)
check("提示仍有款项待收", "仍有款项待收" in detail)

# ---------- 4. 填 0 = 收款完成 ----------
print("\n=== 4. 本次填 0 = 收款完成 ===")
client.post(f"/records/{rid}/receipts", data={
    "received_amount": "0", "received_date": "2026-11-06", "note": "客户确认不再付",
}, follow_redirects=True)
row = rec(rid)
check("填 0 后状态 = completed", row["status"] == "completed", row["status"])
check("收款结清标记置 1", row["receipt_closed"] == 1)
check("结清时间已写", bool(row["receipt_closed_at"]))
check("结清不改已收金额", row["received_amount_cents"] == 30000)
check("流水最后一条标记为结清", receipts(rid)[-1]["is_close"] == 1)
detail = body(f"/records/{rid}")
check("详情页状态显示「已收齐」", "已收齐" in detail)

# ---------- 5. 费用登记 ----------
print("\n=== 5. 收到款项后登记费用 ===")
client.post(f"/records/{rid}/fee", data={
    "fee_service": "12.5", "fee_overnight": "3", "fee_finance": "2", "fee_other": "0.5",
}, follow_redirects=True)
row = rec(rid)
check("服务费 1250 分", row["fee_service_cents"] == 1250, str(row["fee_service_cents"]))
check("资金过夜费 300 分", row["fee_overnight_cents"] == 300, str(row["fee_overnight_cents"]))
check("财务费用 200 分", row["fee_finance_cents"] == 200, str(row["fee_finance_cents"]))
check("其他费用 50 分", row["fee_other_cents"] == 50, str(row["fee_other_cents"]))
check("费用登记时间与登记人已写",
      bool(row["fee_registered_at"]) and row["fee_registered_by"] == "系统管理员",
      f"{row['fee_registered_at']} / {row['fee_registered_by']}")
check("详情页费用合计 18.00 元", "18.00" in body(f"/records/{rid}"))

# ---------- 6. 删除收款记录后重算 ----------
print("\n=== 6. 删掉结清那条，状态回到待收款 ===")
close_item = [x for x in receipts(rid) if x["is_close"]][0]
client.post(f"/records/{rid}/receipts/{close_item['id']}/delete", follow_redirects=True)
row = rec(rid)
check("删结清记录后状态回到待收款", row["status"] == "pending_payment", row["status"])
check("结清标记回 0", row["receipt_closed"] == 0)
check("已收金额仍为 30000 分", row["received_amount_cents"] == 30000)
# 应收 = 贸易费 120000 + 留存利润 0 + 资金过夜费 300（第 5 段登记的 3 元）
check("待收回到 90300 分（含资金过夜费 300）", row["unpaid_amount_cents"] == 90300,
      str(row["unpaid_amount_cents"]))

# ---------- 7. 收满自动结清 ----------
print("\n=== 7. 分批收满自动转已收齐 ===")
FORM2 = dict(FORM, customer_name="收满测试客户", fee_rate="1")  # 贸易费 1000 元
client.post("/records/new", data=dict(FORM2, action="submit"), follow_redirects=True)
with application.app_context():
    rid2 = get_db().execute("SELECT MAX(id) m FROM records").fetchone()["m"]

client.post(f"/records/{rid2}/receipts", data={"received_amount": "400"},
            follow_redirects=True)
row = rec(rid2)
check("先收 400，仍待收款", row["status"] == "pending_payment"
      and row["unpaid_amount_cents"] == 60000, f"{row['status']} {row['unpaid_amount_cents']}")

client.post(f"/records/{rid2}/receipts", data={"received_amount": "600"},
            follow_redirects=True)
row = rec(rid2)
check("收满 1000 后自动结清", row["status"] == "completed", row["status"])
check("待收归零", row["unpaid_amount_cents"] == 0, str(row["unpaid_amount_cents"]))
check("结清标记置 1", row["receipt_closed"] == 1)
check("两条流水", len(receipts(rid2)) == 2)

# ---------- 8. 收款进度工具 ----------
print("\n=== 8. 收款进度口径 ===")
p = receipt_progress(rec(rid2))
check("应收 100000 分", p["receivable"] == 100000, str(p["receivable"]))
check("已收 100000 分", p["received"] == 100000, str(p["received"]))
check("待收 0", p["outstanding"] == 0, str(p["outstanding"]))
check("进度 100%", p["percent"] == 100, str(p["percent"]))
check("已结清", p["closed"] is True)

p2 = receipt_progress(rec(rid))
check("部分收款进度 25%（预存0 + 已收30000 / 应收120000）", p2["percent"] == 25,
      str(p2["percent"]))

# ---------- 9. 待办与列表 ----------
print("\n=== 9. 待办与列表口径 ===")
todos = body("/todos/")
# 待收款这一环归资管，管理员（不是资管）只看到进度，不被当成待办提醒
check("管理员待办不出现「待我确认收款」", "待我确认收款" not in todos)
check("管理员待办显示「收款进行中」进度", "收款进行中" in todos)
check("待办不再有「待我付款」", "待我付款" not in todos)
check("进度型待办给「查看」而不是「去收款」", "查看" in todos and "去收款</a>" not in todos)

lst = body("/records/")
check("列表表头为「待收」", "<th class=\"num\">待收</th>" in lst)
check("列表汇总为「待收合计」", "待收合计" in lst)
check("列表不再出现「未付款合计」", "未付款合计" not in lst)

# ---------- 10. 导出表头 ----------
print("\n=== 10. 导出列名随口径调整 ===")
from views.excel_io import TEMPLATE_HEADERS  # noqa: E402
check("导出用「贸易费」", "贸易费" in TEMPLATE_HEADERS)
check("导出用「留存利润」", "留存利润" in TEMPLATE_HEADERS)
check("导出不再有「辅助核算费用」", "辅助核算费用" not in "".join(TEMPLATE_HEADERS))
check("导出用「已收金额」", "已收金额" in TEMPLATE_HEADERS)
check("导出用「待收金额」", "待收金额" in TEMPLATE_HEADERS)
check("导出不再有「未付款金额」", "未付款金额" not in TEMPLATE_HEADERS)
check("导出不再有「应付金额」", "应付金额" not in TEMPLATE_HEADERS)

# ---------- 11. 权限：非登记人且无收款权限不能登记 ----------
print("\n=== 11. 收款权限 ===")
with application.app_context():
    db = get_db()
    db.execute(
        """INSERT INTO users (username, password_hash, name, role, permissions,
                              is_active, must_change_password, dept_id)
           VALUES (?,?,?,?,?,1,0,?)""",
        ("staff1", "x", "业务员甲", "staff",
         '["record.create","record.import","customer.view"]', dept_id),
    )
    db.commit()

client2 = application.test_client()
with application.app_context():
    from werkzeug.security import generate_password_hash
    get_db().execute("UPDATE users SET password_hash = ? WHERE username = 'staff1'",
                     (generate_password_hash("Staff@2026"),))
    get_db().commit()
client2.post("/login", data={"username": "staff1", "password": "Staff@2026"})

# 别人（staff 非本人、无 record.pay）看不到 admin 的这条记录 → 403
r = client2.get(f"/records/{rid}")
check("无权限者打不开他人业务", r.status_code == 403, str(r.status_code))

# 自己的单子可以收款
client2.post("/records/new", data=dict(FORM, customer_name="业务员自己的单",
                                       action="submit"), follow_redirects=True)
with application.app_context():
    rid3 = get_db().execute("SELECT MAX(id) m FROM records").fetchone()["m"]
r = client2.post(f"/records/{rid3}/receipts", data={"received_amount": "100"},
                 follow_redirects=True)
row = rec(rid3)
check("业务员可给自己的单子登记收款", row["received_amount_cents"] == 10000,
      str(row["received_amount_cents"]))
check("业务员登记后仍待收款", row["status"] == "pending_payment")
r = client2.post(f"/records/{rid3}/fee", data={"fee_service": "1"}, follow_redirects=True)
row = rec(rid3)
check("业务员无费用登记权限（费用未写入）", row["fee_service_cents"] == 0,
      str(row["fee_service_cents"]))
todos2 = client2.get("/todos/").get_data(as_text=True)
# 业务员在「待收款」阶段不被告知，只在款项到账后从「收款进行中」看到进度
check("业务员待办不出现「待我确认收款」", "待我确认收款" not in todos2)
check("业务员待办显示「收款进行中」", "收款进行中" in todos2)
check("业务员待办没有「待收款跟进」tab", "待收款跟进" not in todos2)

# ---------- 12. 批量提交 ----------
print("\n=== 12. 批量提交报批中单据进待收款 ===")
draft_ids = []
for nm in ("批量客户甲", "批量客户乙"):
    client.post("/records/new", data=dict(FORM, customer_name=nm, action="save"),
                follow_redirects=True)
    with application.app_context():
        draft_ids.append(
            get_db().execute("SELECT MAX(id) m FROM records").fetchone()["m"])

with application.app_context():
    st = [get_db().execute("SELECT status FROM records WHERE id = ?", (i,)).fetchone()["status"]
          for i in draft_ids]
check("暂存不进收款", st == ["draft", "draft"], str(st))

r = client.post("/records/batch-submit", data={"ids": [str(i) for i in draft_ids]},
                follow_redirects=True)
with application.app_context():
    st2 = [get_db().execute("SELECT status FROM records WHERE id = ?", (i,)).fetchone()["status"]
           for i in draft_ids]
check("批量提交后进入待收款", st2 == ["pending_payment", "pending_payment"], str(st2))
check("提示已提交条数", "已提交 2 条" in r.get_data(as_text=True))

# 业务员不能批量提交别人的单子
r = client2.post("/records/batch-submit", data={"ids": [str(draft_ids[0])]},
                 follow_redirects=True)
with application.app_context():
    st3 = get_db().execute("SELECT status FROM records WHERE id = ?",
                           (draft_ids[0],)).fetchone()["status"]
check("业务员批量提交他人单据被跳过", st3 == "pending_payment" and "都不是你的" in r.get_data(as_text=True))

lst = body("/records/")
check("列表有「批量提交收款」按钮", "批量提交收款" in lst)

# ---------- 13. 待收款归资管，业务员不被打扰 ----------
print("\n=== 13. 待收款归资管（业务员不提醒，到账后回头通知） ===")
from werkzeug.security import generate_password_hash  # noqa: E402
from unittest.mock import patch  # noqa: E402
import notifier  # noqa: E402
from workflow import is_receipt_staff, receipt_dept_ids  # noqa: E402

with application.app_context():
    db = get_db()
    zg_dept = db.execute("SELECT id FROM departments WHERE name = '资管'").fetchone()
    if zg_dept:
        zg_dept = zg_dept["id"]
    else:
        zg_dept = db.execute(
            "INSERT INTO departments (name, short_name, sort_order) VALUES ('资管','资管',30)"
        ).lastrowid
    for uname, uname_cn, perms, wx in (
        ("zg1", "资管员甲",
         '["record.pay","record.view_all","record.create"]', "wx_zg"),
        ("staff1", "业务员甲", None, "wx_staff"),
    ):
        if uname == "zg1":
            db.execute(
                """INSERT INTO users (username, password_hash, name, role, permissions,
                                      is_active, must_change_password, dept_id, wecom_userid)
                   VALUES (?,?,?,?,?,1,0,?,?)""",
                (uname, generate_password_hash("Zg@2026"), uname_cn, "finance", perms,
                 zg_dept, wx),
            )
        else:
            db.execute("UPDATE users SET wecom_userid = ? WHERE username = ?", (wx, uname))
    db.commit()

client3 = application.test_client()
client3.post("/login", data={"username": "zg1", "password": "Zg@2026"})

with application.app_context():
    db = get_db()
    zg_user = db.execute("SELECT * FROM users WHERE username = 'zg1'").fetchone()
    staff_user = db.execute("SELECT * FROM users WHERE username = 'staff1'").fetchone()
    check("收款确认部门取到「资管」", zg_dept in receipt_dept_ids(db), str(receipt_dept_ids(db)))
    check("资管人员算「确认收款」的人", is_receipt_staff(db, zg_user))
    check("业务员不算「确认收款」的人", not is_receipt_staff(db, staff_user))

todos3 = client3.get("/todos/").get_data(as_text=True)
check("资管待办有「待我确认收款」", "待我确认收款" in todos3)
check("资管能看到别人部门登记的待收款（不受部门限制）", "测试客户A" in todos3 or "收款测试客户" in todos3)

todos2b = client2.get("/todos/").get_data(as_text=True)
check("业务员待办没有「待我确认收款」", "待我确认收款" not in todos2b)

# 通知对象：提交 → 资管；每一轮收款确认 → 登记该业务的业务员
sent = []
with application.app_context():
    db = get_db()
    fake_cfg = {"enabled": True, "corp_id": "x", "agent_id": "1", "secret": "s"}
    with patch.object(notifier, "config", return_value=fake_cfg), \
         patch.object(notifier, "send_text",
                      side_effect=lambda db_, ids, content: sent.append((list(ids), content))):
        notifier.notify_next(db, rid3, "submit")
        notifier.notify_next(db, rid3, "receipt")
check("提交后提醒资管（不是业务员自己）",
      sent and "wx_zg" in sent[0][0] and "wx_staff" not in sent[0][0], str(sent[:1]))
check("每一轮收款确认后通知业务员",
      len(sent) > 1 and "wx_staff" in sent[1][0], str(sent[1:2]))
check("收款通知带金额", len(sent) > 1 and "收款" in sent[1][1], str(sent[1:2]))

# ---------- 14. 开票：登记时选「要/不要」，收齐后转财务确认 ----------
print("\n=== 14. 开票：选「要」收齐后转财务，选「不要」自动跳过 ===")
detail_ok = client.get(f"/records/{rid2}").get_data(as_text=True)
check("收齐后详情页出现开票卡片", "是否需要开票" in detail_ok)
check("登记时选了要 → 待开票", "需要开票 · 待开票" in detail_ok)

r = client.post(f"/records/{rid2}/invoice", data={"invoice_needed": "0"},
                follow_redirects=True)
row = rec(rid2)
check("财务改选「不需要」写入 0", row["invoice_needed"] == 0, str(row["invoice_needed"]))
check("不需要开票时清掉已开票标记", row["is_invoiced"] == 0, str(row["is_invoiced"]))
check("页面提示开票环节已跳过", "已自动跳过" in r.get_data(as_text=True))

client.post(f"/records/{rid2}/invoice", data={"invoice_needed": "1"}, follow_redirects=True)
row = rec(rid2)
check("选「要」但未开 → 待开票",
      row["invoice_needed"] == 1 and row["is_invoiced"] == 0,
      f"{row['invoice_needed']}/{row['is_invoiced']}")
check("页面显示「需要开票 · 待开票」",
      "需要开票 · 待开票" in client.get(f"/records/{rid2}").get_data(as_text=True))

client.post(f"/records/{rid2}/invoice", data={"invoice_needed": "1", "is_invoiced": "1"},
            follow_redirects=True)
row = rec(rid2)
check("要开票 + 已开票 → is_invoiced=1", row["is_invoiced"] == 1, str(row["is_invoiced"]))
check("页面显示「已开票」", "已开票" in client.get(f"/records/{rid2}").get_data(as_text=True))

# 不需要开票时，即便传了已开票也不记
client.post(f"/records/{rid2}/invoice", data={"invoice_needed": "0", "is_invoiced": "1"},
            follow_redirects=True)
row = rec(rid2)
check("选「不需要」时不保留已开票",
      row["invoice_needed"] == 0 and row["is_invoiced"] == 0,
      f"{row['invoice_needed']}/{row['is_invoiced']}")

# 未收齐之前不给登记（收款到账不算，要收齐）
client.post("/records/new", data=dict(FORM, customer_name="没收齐的单", action="save"),
            follow_redirects=True)
with application.app_context():
    rid_nopay = get_db().execute("SELECT MAX(id) m FROM records").fetchone()["m"]
r = client.post(f"/records/{rid_nopay}/invoice", data={"invoice_needed": "1"},
                follow_redirects=True)
row = rec(rid_nopay)
check("未收齐不能登记开票",
      row["invoice_needed"] == 1 and "款项收齐后才能登记开票" in r.get_data(as_text=True),
      str(row["invoice_needed"]))
html_nopay = client.get(f"/records/{rid_nopay}").get_data(as_text=True)
check("未收齐时详情页不出现开票卡片", 'id="invoiceCard"' not in html_nopay)

# 财务待办：已收齐 + 要开票 + 未开票 → 出现在「待我确认开票」
with application.app_context():
    db = get_db()
    db.execute(
        """INSERT OR IGNORE INTO users (username, password_hash, name, role, permissions,
                                        is_active, must_change_password, dept_id)
           VALUES (?,?,?,?,?,1,0,?)""",
        ("cw1", generate_password_hash("Cw@2026"), "财务乙", "finance",
         '["record.invoice","record.view_all"]', dept_id),
    )
    db.execute("UPDATE users SET password_hash = ? WHERE username = 'cw1'",
               (generate_password_hash("Cw@2026"),))
    db.commit()
client4 = application.test_client()
client4.post("/login", data={"username": "cw1", "password": "Cw@2026"})

client.post(f"/records/{rid2}/invoice", data={"invoice_needed": "1"}, follow_redirects=True)
todo_inv = client4.get("/todos/?tab=invoice").get_data(as_text=True)
check("财务待办有「待我确认开票」", "待我确认开票" in todo_inv)
check("需要开票的已收齐单据出现在待开票清单", "收满测试客户" in todo_inv)
client.post(f"/records/{rid2}/invoice", data={"invoice_needed": "1", "is_invoiced": "1"},
            follow_redirects=True)
todo_inv2 = client4.get("/todos/?tab=invoice").get_data(as_text=True)
check("已开票后从待办里消失", "收满测试客户" not in todo_inv2)

# 权限：资管有收款权限但没有开票权限
r = client3.post(f"/records/{rid2}/invoice", data={"invoice_needed": "0"})
check("无开票权限者被拦（资管也不行）", r.status_code == 302, str(r.status_code))
check("被拦后状态没被改动", rec(rid2)["invoice_needed"] == 1, str(rec(rid2)["invoice_needed"]))

# 登记表单：要 / 不要 单选（不再有「是否已开票」勾选）
form_html = client.get("/records/new").get_data(as_text=True)
check("登记表单有「要 / 不要」单选",
      'name="invoice_needed" value="1"' in form_html
      and 'name="invoice_needed" value="0"' in form_html)
check("登记表单仍不出现「是否已开票」勾选", 'name="is_invoiced"' not in form_html)

# 未选是否开票时不允许保存
r = client.post("/records/new", data={k: v for k, v in FORM.items()
                                      if k != "invoice_needed"} | {"customer_name": "没选开票的单"},
                follow_redirects=True)
check("没选「要/不要」时保存被拦", "请选择是否需要开票" in r.get_data(as_text=True))

print("\n=== 13. 贸易费可手工修正（四舍五入差异）===")
FEE_FORM = dict(FORM, customer_name="贸易费手改客户", biz_amount="100000", fee_rate="1.2",
                channel_commission="100", payable_amount="1200.26")
client.post("/records/new", data=FEE_FORM, follow_redirects=True)
with application.app_context():
    rid3 = get_db().execute("SELECT MAX(id) m FROM records").fetchone()["m"]
row = rec(rid3)
check("贸易费按手填值 1200.26（公式是 1200）", row["payable_amount_cents"] == 120026,
      str(row["payable_amount_cents"]))
check("应收合计 = 1200.26 + 100 = 1300.26",
      row["payable_amount_cents"] + row["channel_commission_cents"] == 130026,
      str(row["payable_amount_cents"] + row["channel_commission_cents"]))
check("待收跟着手填值算 1300.26", row["unpaid_amount_cents"] == 130026,
      str(row["unpaid_amount_cents"]))

detail3 = body(f"/records/{rid3}")
check("详情页标注「手工修正」", "手工修正" in detail3)
check("详情页给出公式值 1,200.00", "1,200.00" in detail3)

edit_html = body(f"/records/{rid3}/edit")
check("编辑页把 1200.26 回填到贸易费框", 'value="1200.26"' in edit_html)
fee_tag = edit_html.split('id="trade_fee"')[1].split(">")[0] if 'id="trade_fee"' in edit_html else ""
check("贸易费框已不是只读", 'id="trade_fee"' in edit_html and "readonly" not in fee_tag, fee_tag.strip())
check("贸易费框旁有「按公式」按钮", 'id="tradeFeeReset"' in edit_html)

# 留空 → 回落到公式值
client.post(f"/records/{rid3}/edit",
            data={k: v for k, v in FEE_FORM.items() if k != "payable_amount"},
            follow_redirects=True)
row = rec(rid3)
check("留空贸易费则按公式 = 1200 元", row["payable_amount_cents"] == 120000,
      str(row["payable_amount_cents"]))
check("留空后待收回到 1300 元", row["unpaid_amount_cents"] == 130000,
      str(row["unpaid_amount_cents"]))

# 再手改一次并提交，然后走收款：重算待收不能把手工值冲掉
client.post(f"/records/{rid3}/edit", data=dict(FEE_FORM, action="submit"), follow_redirects=True)
check("提交后进入待收款", rec(rid3)["status"] == "pending_payment", rec(rid3)["status"])
client.post(f"/records/{rid3}/receipts",
            data={"trade_fee": "1000", "received_date": "2026-11-07"}, follow_redirects=True)
row = rec(rid3)
check("收款重算后贸易费仍是手填的 1200.26", row["payable_amount_cents"] == 120026,
      str(row["payable_amount_cents"]))
check("待收 = 贸易费差 200.26 + 留存利润 100 = 300.26", row["unpaid_amount_cents"] == 30026,
      str(row["unpaid_amount_cents"]))
check("流水记下分项：贸易费 1000", receipts(rid3)[-1]["trade_fee_cents"] == 100000,
      str(receipts(rid3)[-1]["trade_fee_cents"]))

# 只补贸易费：贸易费收满，但留存利润一分没收，仍不能结清
client.post(f"/records/{rid3}/receipts",
            data={"trade_fee": "200.26", "received_date": "2026-11-08"}, follow_redirects=True)
row = rec(rid3)
check("贸易费收满、留存利润未收 → 仍挂待收款", row["status"] == "pending_payment", row["status"])
check("待收只剩留存利润 100 元", row["unpaid_amount_cents"] == 10000,
      str(row["unpaid_amount_cents"]))

client.post(f"/records/{rid3}/receipts",
            data={"retention": "100", "received_date": "2026-11-08"}, follow_redirects=True)
row = rec(rid3)
check("留存利润也收满 → 自动结清", row["receipt_closed"] == 1 and row["status"] == "completed",
      f"{row['receipt_closed']}/{row['status']}")

# 校验：负数 / 非数字
r = client.post("/records/new", data=dict(FORM, customer_name="贸易费负数",
                                          payable_amount="-1"), follow_redirects=True)
check("贸易费填负数被拦", "贸易费不能为负数" in r.get_data(as_text=True))
r = client.post("/records/new", data=dict(FORM, customer_name="贸易费乱码",
                                          payable_amount="abc"), follow_redirects=True)
check("贸易费填非数字被拦", "贸易费格式不正确" in r.get_data(as_text=True))

print("\n=== 14. 收款按费用分项填列 ===")
SPLIT_FORM = dict(FORM, customer_name="分项收款客户", biz_amount="100000", fee_rate="1",
                  channel_commission="200", fee_overnight="50", invoice_needed="0")
client.post("/records/new", data=dict(SPLIT_FORM, action="submit"), follow_redirects=True)
with application.app_context():
    rid4 = get_db().execute("SELECT MAX(id) m FROM records").fetchone()["m"]
row = rec(rid4)
check("应收合计 = 贸易费 1000 + 留存 200 + 过夜费 50 = 1250 元",
      row["payable_amount_cents"] + row["channel_commission_cents"]
      + row["fee_overnight_cents"] == 125000,
      str(row["payable_amount_cents"] + row["channel_commission_cents"]
          + row["fee_overnight_cents"]))
check("资金过夜费计入待收 1250 元", row["unpaid_amount_cents"] == 125000,
      str(row["unpaid_amount_cents"]))

detail4 = body(f"/records/{rid4}")
check("详情页有分项表「收款项目」", "收款项目" in detail4)
check("分项表列出资金过夜费", "资金过夜费" in detail4)
check("表单有三项应收输入框",
      'name="trade_fee"' in detail4 and 'name="retention"' in detail4
      and 'name="overnight"' in detail4)
check("表单有费用留痕三项",
      'name="channel"' in detail4 and 'name="finance"' in detail4
      and 'name="other"' in detail4)
check("有「按待收全额填入」按钮", 'id="recvFillAll"' in detail4)
check("本次合计只读框存在", 'id="fRecvTotal"' in detail4 and "readonly" in
      detail4.split('id="fRecvTotal"')[1].split(">")[0])

# 只收贸易费 → 不结清
client.post(f"/records/{rid4}/receipts",
            data={"trade_fee": "1000", "received_date": "2026-11-09"}, follow_redirects=True)
row = rec(rid4)
check("只收贸易费 1000 → 待收 250 元", row["unpaid_amount_cents"] == 25000,
      str(row["unpaid_amount_cents"]))
check("未收齐不结清", row["status"] == "pending_payment", row["status"])

# 一次补齐留存利润 + 过夜费，并登记渠道提成留痕
client.post(f"/records/{rid4}/receipts",
            data={"retention": "200", "overnight": "50", "channel": "10",
                  "received_date": "2026-11-09"}, follow_redirects=True)
row = rec(rid4)
last = receipts(rid4)[-1]
check("补齐后自动结清", row["status"] == "completed" and row["receipt_closed"] == 1,
      f"{row['status']}/{row['receipt_closed']}")
check("本次合计 = 200 + 50 + 10(留痕) = 260 元", last["amount_cents"] == 26000,
      str(last["amount_cents"]))
check("流水分项：留存利润 200", last["retention_cents"] == 20000, str(last["retention_cents"]))
check("流水分项：资金过夜费 50", last["overnight_cents"] == 5000, str(last["overnight_cents"]))
check("流水分项：渠道提成 10（留痕，不进应收）", last["channel_cents"] == 1000,
      str(last["channel_cents"]))
check("累计已收 1250 + 10 = 1260 元（留痕项也计入实收合计）",
      row["received_amount_cents"] == 126000, str(row["received_amount_cents"]))

# ---------- 9. 阶段标签：已收齐之后还要分清「待开票 / 已开票」 ----------
print("\n=== 9. 阶段标签（报批 → 待收款 → 收款中 → 待开票 → 已开票） ===")
import re

STAGE_WORDS = ("报批中", "待收款", "收款中", "待开票", "已开票", "已收齐·无需开票", "已驳回")
STAGE_RE = ">\\s*(%s)\\s*<" % "|".join(STAGE_WORDS)


def stage_tag(path):
    """从详情页头部 detail-sub 里取阶段标签文案（不是从整页里碰运气）。"""
    html = body(path)
    i = html.find("detail-sub")
    seg = html[i:i + 700] if i >= 0 else ""
    m = re.search(STAGE_RE, seg)
    return m.group(1) if m else ""


def new_record(customer, invoice_needed):
    client.post("/records/new", data=dict(FORM, customer_name=customer,
                                          invoice_needed=invoice_needed),
                follow_redirects=True)
    with application.app_context():
        return get_db().execute("SELECT MAX(id) m FROM records").fetchone()["m"]


rid9 = new_record("阶段标签客户", "1")
check("新登记显示「报批中」", stage_tag(f"/records/{rid9}") == "报批中", stage_tag(f"/records/{rid9}"))

client.post(f"/records/{rid9}/submit", follow_redirects=True)
check("提交后显示「待收款」", stage_tag(f"/records/{rid9}") == "待收款", stage_tag(f"/records/{rid9}"))

client.post(f"/records/{rid9}/receipts",
            data={"trade_fee": "600", "received_date": "2026-11-09"}, follow_redirects=True)
check("收到部分款显示「收款中」", stage_tag(f"/records/{rid9}") == "收款中", stage_tag(f"/records/{rid9}"))

client.post(f"/records/{rid9}/receipts",
            data={"trade_fee": "600", "received_date": "2026-11-09"}, follow_redirects=True)
check("收满要开票的单显示「待开票」而非「已收齐」",
      stage_tag(f"/records/{rid9}") == "待开票", stage_tag(f"/records/{rid9}"))
check("收齐但未开票，数据库 status 仍是 completed",
      rec(rid9)["status"] == "completed" and rec(rid9)["is_invoiced"] == 0,
      f"{rec(rid9)['status']}/{rec(rid9)['is_invoiced']}")

client.post(f"/records/{rid9}/invoice",
            data={"invoice_needed": "1", "is_invoiced": "1"}, follow_redirects=True)
check("开票后显示「已开票」", stage_tag(f"/records/{rid9}") == "已开票",
      stage_tag(f"/records/{rid9}"))
check("开票不改变 status（仍是 completed）", rec(rid9)["status"] == "completed")
check("详情页步骤条：开票这一步标为已完成",
      'class="stage-step done"' in body(f"/records/{rid9}"))

# 登记时选「不要」的，收齐后直接是「已收齐·无需开票」，不进开票待办
rid9b = new_record("阶段标签无需开票", "0")
client.post(f"/records/{rid9b}/submit", follow_redirects=True)
client.post(f"/records/{rid9b}/receipts",
            data={"trade_fee": "1200", "received_date": "2026-11-09"}, follow_redirects=True)
check("不要开票的单收满后显示「已收齐·无需开票」",
      stage_tag(f"/records/{rid9b}") == "已收齐·无需开票", stage_tag(f"/records/{rid9b}"))

# 列表页 / 阶段筛选 / 导出口径一致（只取行内标签，别把筛选 chip 的文字也算进来）
ROW_TAG_RE = r'<span class="tag" title="[^"]*"\s*style="[^"]*">([^<]+)</span>'


def row_tags(path):
    return re.findall(ROW_TAG_RE, body(path))


tags_inv = row_tags("/records/?stage=invoice_pending")
check("按「待开票」筛选后列表里只有待开票",
      bool(tags_inv) and all(t == "待开票" for t in tags_inv), str(set(tags_inv)))
check("「待开票」筛选不含已开票的单", "阶段标签客户" not in body("/records/?stage=invoice_pending"))
tags_done = row_tags("/records/?stage=invoiced")
check("按「已开票」筛选只出已开票",
      bool(tags_done) and all(t == "已开票" for t in tags_done), str(set(tags_done)))
tags_all = row_tags("/records/")
check("不筛选时列表出现多种阶段", len(set(tags_all)) >= 3, str(set(tags_all)))
check("按四态的老链接仍然可用（status=completed）",
      client.get("/records/?status=completed").status_code == 200)

print("\n" + "=" * 50)
print(f"通过 {passed} 项，失败 {failed} 项")
sys.exit(0 if failed == 0 else 1)
