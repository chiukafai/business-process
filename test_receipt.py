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
    "fee_rate": "1",
    "channel_commission": "0",
    "payable_amount": "1200",   # 应收 1200 元
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
check("待收 = 应收 120000 分", row["unpaid_amount_cents"] == 120000, str(row["unpaid_amount_cents"]))
check("收款结清标记为 0", row["receipt_closed"] == 0)

detail = body(f"/records/{rid}")
check("详情页状态显示「待收款」", "待收款" in detail)
check("详情页有「收款登记」卡片", "收款登记" in detail)
check("详情页说明「留空 = 未收完」", "留空 = 未收完" in detail or "留空表示还有款项待收" in detail)
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
check("待收回到 90000 分", row["unpaid_amount_cents"] == 90000, str(row["unpaid_amount_cents"]))

# ---------- 7. 收满自动结清 ----------
print("\n=== 7. 分批收满自动转已收齐 ===")
FORM2 = dict(FORM, customer_name="收满测试客户", payable_amount="1000")
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
check("导出用「应收金额」", "应收金额" in TEMPLATE_HEADERS)
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

# ---------- 14. 开票：收款后由财务做两级确认 ----------
print("\n=== 14. 开票：收款到账后由财务确认「需不需要 / 是否已开」 ===")
detail_ok = client.get(f"/records/{rid}").get_data(as_text=True)
check("收款到账后详情页出现开票卡片", "是否需要开票" in detail_ok)
check("开票卡片默认待确认", "待确认是否需要开票" in detail_ok)

r = client.post(f"/records/{rid}/invoice", data={"invoice_needed": "0"},
                follow_redirects=True)
row = rec(rid)
check("选「不需要开票」写入 0", row["invoice_needed"] == 0, str(row["invoice_needed"]))
check("不需要开票时清掉已开票标记", row["is_invoiced"] == 0, str(row["is_invoiced"]))
check("页面显示「不需要开票」", "不需要开票" in r.get_data(as_text=True))

client.post(f"/records/{rid}/invoice", data={"invoice_needed": "1"}, follow_redirects=True)
row = rec(rid)
check("选「需要开票」但未开 → 待开票",
      row["invoice_needed"] == 1 and row["is_invoiced"] == 0,
      f"{row['invoice_needed']}/{row['is_invoiced']}")
check("页面显示「需要开票 · 待开票」",
      "需要开票 · 待开票" in client.get(f"/records/{rid}").get_data(as_text=True))

client.post(f"/records/{rid}/invoice", data={"invoice_needed": "1", "is_invoiced": "1"},
            follow_redirects=True)
row = rec(rid)
check("需要开票 + 已开票 → is_invoiced=1", row["is_invoiced"] == 1, str(row["is_invoiced"]))
check("页面显示「已开票」", "已开票" in client.get(f"/records/{rid}").get_data(as_text=True))

# 不需要开票时，即便传了已开票也不记
client.post(f"/records/{rid}/invoice", data={"invoice_needed": "0", "is_invoiced": "1"},
            follow_redirects=True)
row = rec(rid)
check("选「不需要开票」时不保留已开票",
      row["invoice_needed"] == 0 and row["is_invoiced"] == 0,
      f"{row['invoice_needed']}/{row['is_invoiced']}")

# 没收到款之前不给登记
client.post("/records/new", data=dict(FORM, customer_name="没收款的单", action="save"),
            follow_redirects=True)
with application.app_context():
    rid_nopay = get_db().execute("SELECT MAX(id) m FROM records").fetchone()["m"]
r = client.post(f"/records/{rid_nopay}/invoice", data={"invoice_needed": "1"},
                follow_redirects=True)
row = rec(rid_nopay)
check("未收到款不能登记开票",
      row["invoice_needed"] is None and "收款到账后才能登记开票" in r.get_data(as_text=True))
html_nopay = client.get(f"/records/{rid_nopay}").get_data(as_text=True)
check("未收款时详情页不出现开票卡片", 'id="invoiceCard"' not in html_nopay)
check("未收款时登记信息提示收款后再确认", "收款到账后由财务确认" in html_nopay)

# 权限：资管有收款权限但没有开票权限
r = client3.post(f"/records/{rid}/invoice", data={"invoice_needed": "1"})
check("无开票权限者被拦（资管也不行）", r.status_code == 302, str(r.status_code))
check("被拦后状态没被改动", rec(rid)["invoice_needed"] == 0, str(rec(rid)["invoice_needed"]))

# 登记表单里不再有「是否开票」勾选
r = client.get("/records/new")
check("登记表单不再有开票勾选框",
      'name="is_invoiced"' not in r.get_data(as_text=True))

print("\n" + "=" * 50)
print(f"通过 {passed} 项，失败 {failed} 项")
sys.exit(0 if failed == 0 else 1)
