"""端到端自检脚本：验证权限隔离、两级流程（提交→付款）、客户模块与日志权限。

运行：python e2e_test.py
会重建测试数据库（data_test/app.db），不影响正式数据。
"""
import os
import sys

os.environ["DATA_DIR"] = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data_test")

# 必须在导入 app 之前清空测试库（app 模块加载时就会建库）
for _f in ("app.db", "app.db-wal", "app.db-shm", "secret.key"):
    _p = os.path.join(os.environ["DATA_DIR"], _f)
    if os.path.exists(_p):
        os.remove(_p)

import app as app_module  # noqa: E402
from db import get_db  # noqa: E402

PASS, FAIL = [], []


def check(name, cond, extra=""):
    (PASS if cond else FAIL).append(name)
    print(("  ✅ " if cond else "  ❌ ") + name + (f"  {extra}" if extra else ""))


def main():
    application = app_module.create_app()
    application.config["TESTING"] = True
    client = application.test_client()

    print("\n=== 1. 未登录访问拦截 ===")
    r = client.get("/dashboard", follow_redirects=False)
    check("未登录访问看板被重定向到登录页",
          r.status_code == 302 and "/login" in r.headers["Location"])

    print("\n=== 2. 管理员登录与强制改密 ===")
    r = client.post("/login", data={"username": "admin", "password": "Hailu@2026"},
                    follow_redirects=True)
    check("初始管理员可登录", r.status_code == 200)
    check("首次登录被要求改密", "首次登录" in r.get_data(as_text=True))
    r = client.post("/change-password", data={
        "old_password": "Hailu@2026", "new_password": "Admin@2026",
        "confirm_password": "Admin@2026",
    }, follow_redirects=True)
    check("修改密码成功进入看板", "业务看板" in r.get_data(as_text=True))

    print("\n=== 3. 建立公司与人员 ===")
    for name, short in (("广州公司", "广州"), ("成都公司", "成都")):
        client.post("/admin/depts/new",
                    data={"name": name, "short_name": short, "sort_order": "10"})
    with application.app_context():
        db = get_db()
        depts = {r["name"]: r["id"] for r in db.execute("SELECT id, name FROM departments")}
    check("两家公司已建立", "广州公司" in depts and "成都公司" in depts, str(list(depts)))

    users = [
        ("gz_staff", "张业务", "广州公司", "staff", ["record.create", "record.import"]),
        ("gz_mgr", "李主管", "广州公司", "manager",
         ["record.create", "record.view_dept", "record.export"]),
        ("cd_staff", "王业务", "成都公司", "staff", ["record.create"]),
        ("fin", "陈财务", None, "finance",
         ["record.create_any_dept", "record.view_all", "record.pay", "record.export"]),
    ]
    for username, name, dept, role, perms in users:
        client.post("/admin/users/new", data={
            "username": username, "name": name, "role": role,
            "dept_id": str(depts.get(dept, "")) if dept else "",
            "password": "Init@2026", "permissions": perms,
        })
    with application.app_context():
        db = get_db()
        count = db.execute("SELECT COUNT(*) c FROM users").fetchone()["c"]
        approve_rows = db.execute(
            "SELECT COUNT(*) c FROM permission_catalog WHERE code LIKE '%approve%'"
        ).fetchone()["c"]
    check("人员创建成功（含管理员共 5 人）", count == 5, f"实际 {count}")
    check("权限目录里已无审核权限", approve_rows == 0, f"残留 {approve_rows} 条")

    proceed(application, client, depts)


def login_as(client, username, password="Test@2026"):
    """登录；首次登录会被要求改密，自动完成改密。"""
    client.get("/logout")
    r = client.post("/login", data={"username": username, "password": password},
                    follow_redirects=True)
    if "用户名或密码不正确" in r.get_data(as_text=True):
        client.get("/logout")
        client.post("/login", data={"username": username, "password": "Init@2026"},
                    follow_redirects=True)
        r = client.post("/change-password", data={
            "old_password": "Init@2026", "new_password": password,
            "confirm_password": password,
        }, follow_redirects=True)
    return r


def proceed(application, client, depts):
    dept_gz = depts["广州公司"]

    print("\n=== 4. 业务员登记并提交（无审核环节）===")
    login_as(client, "gz_staff")
    r = client.get("/records/new")
    check("业务员可打开登记表单", r.status_code == 200)
    check("登记表单有客户下拉数据源", "customerList" in r.get_data(as_text=True))

    r = client.post("/records/new", data={
        "dept_id": str(dept_gz), "customer_name": "测试客户A", "biz_type": "代采",
        "prepaid_mode": "后付", "biz_date": "2026-11-01", "fee_rate": "1.5",
        "biz_amount": "1000000", "channel_commission": "2000",
        "payable_amount": "", "prepaid_amount": "0",
        "remark": "端到端测试", "action": "submit",
    }, follow_redirects=True)
    body = r.get_data(as_text=True)
    check("提交后进入详情页", r.status_code == 200 and "测试客户A" in body)
    check("提交后状态显示为「待收款」", "待收款" in body)

    with application.app_context():
        db = get_db()
        rec = db.execute("SELECT * FROM records ORDER BY id DESC LIMIT 1").fetchone()
        cust = db.execute("SELECT * FROM customers WHERE name = '测试客户A'").fetchone()
    record_id = rec["id"]
    check("公司实收 = 1000000×1.5%−2000 = 13000 元",
          rec["company_income_cents"] == 1300000, f"实际 {rec['company_income_cents']}")
    check("辅助核算 = 1000000−13000 = 987000 元",
          rec["aux_fee_cents"] == 98700000, f"实际 {rec['aux_fee_cents']}")
    check("应收留空取辅助核算，待收 = 987000 元",
          rec["unpaid_amount_cents"] == 98700000, f"实际 {rec['unpaid_amount_cents']}")
    check("提交后没有已收金额", rec["received_amount_cents"] == 0,
          f"实际 {rec['received_amount_cents']}")
    check("新客户名自动建档", cust is not None and rec["customer_id"] == cust["id"])

    print("\n=== 5. 跨公司隔离 ===")
    login_as(client, "cd_staff")
    r = client.get(f"/records/{record_id}", follow_redirects=False)
    check("成都业务员看不到广州的业务（403）", r.status_code == 403, f"返回 {r.status_code}")
    r = client.get("/records/")
    check("成都业务员列表里没有该客户", "测试客户A" not in r.get_data(as_text=True))

    login_as(client, "gz_mgr")
    r = client.get("/todos/?tab=collect")
    check("主管在「待收款跟进」里能看到本部门未收齐的单",
          "测试客户A" in r.get_data(as_text=True))
    r = client.post(f"/records/{record_id}/receipts", data={"received_amount": "1"},
                    follow_redirects=False)
    with application.app_context():
        rec = get_db().execute("SELECT received_amount_cents FROM records WHERE id = ?",
                               (record_id,)).fetchone()
    check("无收款权限且非登记人，不能登记收款",
          rec["received_amount_cents"] == 0 and r.status_code == 302,
          f"返回 {r.status_code} 已收 {rec['received_amount_cents']}")

    print("\n=== 6. 财务登记收款（分批） ===")
    login_as(client, "fin")
    r = client.get("/todos/?tab=collect")
    check("财务「待收款跟进」里出现该单", "测试客户A" in r.get_data(as_text=True))
    r = client.post(f"/records/{record_id}/receipts", data={
        "received_amount": "985000", "received_date": "2026-11-05",
        "receipt_no": "HD20261105001", "note": "首款",
    }, follow_redirects=True)
    body = r.get_data(as_text=True)
    with application.app_context():
        db = get_db()
        rec = db.execute("SELECT * FROM records WHERE id = ?", (record_id,)).fetchone()
    check("收到部分款后仍是待收款", "待收款" in body and rec["status"] == "pending_payment",
          rec["status"])
    check("待收 = 987000 − 985000 = 2000 元",
          rec["unpaid_amount_cents"] == 200000, f"实际 {rec['unpaid_amount_cents']}")
    check("累计已收 985000 元", rec["received_amount_cents"] == 98500000,
          f"实际 {rec['received_amount_cents']}")
    check("回单编号已记录", rec["receipt_no"] == "HD20261105001")
    with application.app_context():
        cnt = get_db().execute(
            "SELECT COUNT(*) c FROM record_receipts WHERE record_id = ?",
            (record_id,)).fetchone()["c"]
    check("收款流水 1 条", cnt == 1, str(cnt))

    print("\n=== 6.1 尾款收齐自动结清 ===")
    r = client.post(f"/records/{record_id}/receipts", data={
        "received_amount": "2000", "received_date": "2026-11-06", "receipt_no": "HD20261106002",
    }, follow_redirects=True)
    body = r.get_data(as_text=True)
    with application.app_context():
        rec = get_db().execute("SELECT * FROM records WHERE id = ?", (record_id,)).fetchone()
    check("收满后状态为「已收齐」", "已收齐" in body and rec["status"] == "completed",
          rec["status"])
    check("待收归零", rec["unpaid_amount_cents"] == 0, f"实际 {rec['unpaid_amount_cents']}")
    check("收款结清标记置 1", rec["receipt_closed"] == 1)

    print("\n=== 7. 待收款状态下业务员不可编辑，可撤回 ===")
    login_as(client, "gz_staff")
    client.post("/records/new", data={
        "dept_id": str(dept_gz), "customer_name": "测试客户B", "biz_date": "2026-11-02",
        "fee_rate": "2", "biz_amount": "500000", "channel_commission": "0",
        "payable_amount": "0", "prepaid_amount": "0",
        "action": "submit",
    }, follow_redirects=True)
    with application.app_context():
        db = get_db()
        rid_b = db.execute("SELECT id FROM records ORDER BY id DESC LIMIT 1").fetchone()["id"]
    r = client.get(f"/records/{rid_b}/edit", follow_redirects=False)
    check("待收款状态点编辑被拒", r.status_code in (302, 403), f"返回 {r.status_code}")

    r = client.post(f"/records/{rid_b}/withdraw", follow_redirects=True)
    check("业务员可撤回待收款单据", "已撤回" in r.get_data(as_text=True))
    with application.app_context():
        db = get_db()
        rid_b_status = db.execute("SELECT status FROM records WHERE id = ?",
                                  (rid_b,)).fetchone()["status"]
    check("撤回后回到草稿", rid_b_status == "draft", rid_b_status)

    print("\n=== 8. 财务退回必须填原因 ===")
    r = client.post(f"/records/{rid_b}/submit", follow_redirects=True)
    login_as(client, "fin")
    r = client.post(f"/records/{rid_b}/reject", data={"reason": ""}, follow_redirects=True)
    check("退回不填原因被拒", "退回必须填写原因" in r.get_data(as_text=True))
    r = client.post(f"/records/{rid_b}/reject", data={"reason": "金额需复核"},
                    follow_redirects=True)
    check("填写原因后退回成功", "已退回" in r.get_data(as_text=True))

    login_as(client, "gz_staff")
    r = client.get("/todos/?tab=revise")
    check("被退回业务出现在「待我修改」", "测试客户B" in r.get_data(as_text=True))

    print("\n=== 9. 权限边界 ===")
    r = client.get("/admin/users", follow_redirects=False)
    check("业务员访问人员管理被拦截", r.status_code == 302, f"返回 {r.status_code}")
    r = client.get("/admin/logs", follow_redirects=False)
    check("业务员访问操作日志被拦截", r.status_code in (302, 403), f"返回 {r.status_code}")
    r = client.get("/excel/export", follow_redirects=False)
    check("业务员无导出权限被拦截", r.status_code == 302, f"返回 {r.status_code}")

    login_as(client, "fin")
    r = client.get("/admin/logs", follow_redirects=False)
    check("财务也看不了操作日志（仅管理员）", r.status_code == 403, f"返回 {r.status_code}")
    r = client.get("/excel/export")
    check("财务可导出 Excel", r.status_code == 200 and
          "spreadsheet" in r.headers.get("Content-Type", ""), r.headers.get("Content-Type", ""))
    r = client.get("/records/" + str(record_id))
    check("财务能看到流转时间轴", "流转记录" in r.get_data(as_text=True))

    r = client.get("/logout")
    client.post("/login", data={"username": "admin", "password": "Admin@2026"},
                follow_redirects=True)
    r = client.get("/admin/logs")
    check("管理员可打开操作日志", r.status_code == 200 and "全站操作留痕" in r.get_data(as_text=True))
    r = client.get("/admin/logs/export")
    check("管理员可导出操作日志",
          r.status_code == 200 and "spreadsheet" in r.headers.get("Content-Type", ""))
    r = client.get("/admin/settings")
    check("管理员可打开系统设置", r.status_code == 200 and "企业微信" in r.get_data(as_text=True))
    r = client.get("/admin/users")
    check("人员页有「全选」控件", "perm-all" in r.get_data(as_text=True))
    r = client.get("/dashboard")
    check("看板已无「最近动态」", "最近动态" not in r.get_data(as_text=True))

    print("\n=== 10. 客户模块 ===")
    r = client.get("/customers/")
    check("客户档案页可打开", r.status_code == 200 and "测试客户A" in r.get_data(as_text=True))
    r = client.get("/customers/import/template")
    check("客户导入模板可下载",
          r.status_code == 200 and "spreadsheet" in r.headers.get("Content-Type", ""))

    client.get("/logout")
    client.post("/login", data={"username": "gz_staff", "password": "Test@2026"},
                follow_redirects=True)
    r = client.get("/customers/", follow_redirects=False)
    check("无客户查看权限时客户档案被拦截", r.status_code == 302, f"返回 {r.status_code}")

    print("\n=== 11. 时间轴留痕 ===")
    with application.app_context():
        db = get_db()
        logs = db.execute(
            "SELECT action FROM record_logs WHERE record_id = ? ORDER BY id", (record_id,)
        ).fetchall()
    actions = [l["action"] for l in logs]
    check("完整留痕：create→submit→receipt（收款两笔）",
          actions[:3] == ["create", "submit", "receipt"]
          and actions.count("receipt") == 2, str(actions))


if __name__ == "__main__":
    main()
    print("\n" + "=" * 46)
    print(f"通过 {len(PASS)} 项，失败 {len(FAIL)} 项")
    if FAIL:
        print("失败项：")
        for f in FAIL:
            print("  - " + f)
        sys.exit(1)
    print("全部通过 ✅")
