"""删除功能与客户字段自检。

覆盖：
  1. 业务记录：无权限被拒、有权限可删任意状态、回单目录一并清理
  2. 客户档案：有业务引用时拒绝、无引用时删除成功
  3. 部门：有人员或业务时拒绝、无引用时删除成功
  4. 人员：有业务时拒绝、无业务时删除成功、不能删自己
  5. 客户档案页字段：主体公司取代简称、业务数列已移除
  6. 旧导入模板的「简称」列仍能识别

运行：python test_delete.py
使用独立的 data_test3 目录，不影响正式数据。
"""
import io
import os
import sys

BASE = os.path.dirname(os.path.abspath(__file__))
os.environ["DATA_DIR"] = os.path.join(BASE, "data_test3")

for _f in ("app.db", "app.db-wal", "app.db-shm", "secret.key"):
    _p = os.path.join(os.environ["DATA_DIR"], _f)
    if os.path.exists(_p):
        os.remove(_p)

from openpyxl import Workbook  # noqa: E402

import app as app_module  # noqa: E402
from db import get_db  # noqa: E402

PASS, FAIL = [], []


def check(name, cond, extra=""):
    (PASS if cond else FAIL).append(name)
    print(("  ✅ " if cond else "  ❌ ") + name + (f"  {extra}" if extra else ""))


def login_as(client, username, password="Del@2026"):
    """登录：已改过密的直接用新密码，首次登录的走初始密码并完成改密。"""
    client.get("/logout")
    r = client.post("/login", data={"username": username, "password": password},
                    follow_redirects=True)
    if "用户名或密码不正确" in r.get_data(as_text=True):
        client.post("/login", data={"username": username, "password": "Init@2026"},
                    follow_redirects=True)
        r = client.post("/change-password", data={
            "old_password": "Init@2026", "new_password": password,
            "confirm_password": password,
        }, follow_redirects=True)
    return r


def build_xlsx(header, rows):
    wb = Workbook()
    ws = wb.active
    ws.append(header)
    for r in rows:
        ws.append(r)
    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return buf


def main():
    application = app_module.create_app()
    application.config["TESTING"] = True
    client = application.test_client()

    client.post("/login", data={"username": "admin", "password": "Hailu@2026"},
                follow_redirects=True)
    client.post("/change-password", data={
        "old_password": "Hailu@2026", "new_password": "Admin@2026",
        "confirm_password": "Admin@2026",
    }, follow_redirects=True)

    print("\n=== 1. 准备数据 ===")
    client.post("/admin/depts/new", data={"name": "甲部门", "short_name": "甲", "sort_order": "10"})
    client.post("/admin/depts/new", data={"name": "乙部门", "short_name": "乙", "sort_order": "20"})
    with application.app_context():
        db = get_db()
        depts = {r["name"]: r["id"] for r in db.execute("SELECT id, name FROM departments")}
    check("两个部门已建立", "甲部门" in depts and "乙部门" in depts, str(list(depts)))

    users = [
        ("s1", "张业务", "甲部门", "staff", ["record.create"]),
        ("m1", "李主管", "甲部门", "manager", ["record.create", "record.view_dept"]),
        ("s2", "王业务", "乙部门", "staff", ["record.create"]),
    ]
    for username, name, dept, role, perms in users:
        client.post("/admin/users/new", data={
            "username": username, "name": name, "role": role,
            "dept_id": str(depts[dept]), "password": "Init@2026", "permissions": perms,
        })
    with application.app_context():
        db = get_db()
        created = {r["username"]: r["id"] for r in db.execute("SELECT id, username FROM users")}
    check("三个测试人员已建立", all(k in created for k in ("s1", "m1", "s2")), str(list(created)))

    client.post("/customers/new", data={
        "name": "测试客户甲", "short_name": "甲客户", "dept_id": str(depts["甲部门"]),
    })
    with application.app_context():
        db = get_db()
        cust = db.execute("SELECT * FROM customers WHERE name = '测试客户甲'").fetchone()
        user_rows = {r["username"]: r["id"] for r in db.execute("SELECT id, username FROM users")}
    cust_id = cust["id"]

    # 业务员登记并提交一笔业务（走真实表单，确保金额与状态正确）
    login_as(client, "s1")
    client.post("/records/new", data={
        "dept_id": str(depts["甲部门"]), "customer_name": "测试客户甲", "biz_type": "代采",
        "prepaid_mode": "后付", "biz_date": "2026-11-01", "fee_rate": "1.5",
        "biz_amount": "100000", "channel_commission": "0",
        "payable_amount": "", "prepaid_amount": "0", "paid_amount": "0",
        "remark": "删除测试", "action": "submit",
    }, follow_redirects=True)
    with application.app_context():
        db = get_db()
        rec = db.execute("SELECT * FROM records ORDER BY id DESC LIMIT 1").fetchone()
    record_id = rec["id"]
    check("业务已提交（待付款）", rec["status"] == "pending_payment", rec["status"])

    # 造一个回单目录，验证删除业务时会一并清理
    receipt_dir = os.path.join(application.config["RECEIPT_DIR"], str(record_id))
    os.makedirs(receipt_dir, exist_ok=True)
    with open(os.path.join(receipt_dir, "fake.jpg"), "wb") as f:
        f.write(b"x")

    print("\n=== 2. 业务记录删除权限 ===")
    login_as(client, "m1")
    r = client.post(f"/records/{record_id}/delete", follow_redirects=True)
    body = r.get_data(as_text=True)
    check("无删除权限的人删不了别人已提交的业务", "只有自己的草稿可以删除" in body)
    with application.app_context():
        still = get_db().execute("SELECT 1 FROM records WHERE id = ?", (record_id,)).fetchone()
    check("记录仍在（未被误删）", still is not None)

    login_as(client, "s1")
    r = client.post(f"/records/{record_id}/delete", follow_redirects=True)
    check("本人也删不了已提交的业务", "只有自己的草稿可以删除" in r.get_data(as_text=True))

    print("\n=== 3. 引用保护 ===")
    client.get("/logout")
    client.post("/login", data={"username": "admin", "password": "Admin@2026"},
                follow_redirects=True)

    r = client.post(f"/customers/{cust_id}/delete", follow_redirects=True)
    check("客户有业务记录时拒绝删除", "不能删除" in r.get_data(as_text=True))
    with application.app_context():
        alive = get_db().execute("SELECT 1 FROM customers WHERE id = ?", (cust_id,)).fetchone()
    check("客户仍在", alive is not None)

    r = client.post(f"/admin/depts/{depts['甲部门']}/delete", follow_redirects=True)
    check("部门下有人员/业务时拒绝删除", "不能删除" in r.get_data(as_text=True))

    r = client.post(f"/admin/users/{user_rows['s1']}/delete", follow_redirects=True)
    check("人员名下有业务时拒绝删除", "不能删除" in r.get_data(as_text=True))

    r = client.post(f"/admin/users/{user_rows['admin']}/delete", follow_redirects=True)
    check("不能删除自己的账号", "不能删除自己的账号" in r.get_data(as_text=True))

    print("\n=== 4. 管理员可删任意状态的业务 ===")
    r = client.post(f"/records/{record_id}/delete", follow_redirects=True)
    body = r.get_data(as_text=True)
    check("管理员删除待付款业务成功", "已删除业务" in body)
    with application.app_context():
        db = get_db()
        gone = db.execute("SELECT 1 FROM records WHERE id = ?", (record_id,)).fetchone()
        logs = db.execute("SELECT COUNT(*) c FROM record_logs WHERE record_id = ?",
                          (record_id,)).fetchone()["c"]
    check("记录已从库中移除", gone is None)
    check("流程日志随记录级联清除", logs == 0, f"残留 {logs} 条")
    check("回单附件目录已清理", not os.path.exists(receipt_dir))

    print("\n=== 5. 引用解除后可正常删除 ===")
    r = client.post(f"/customers/{cust_id}/delete", follow_redirects=True)
    check("客户无业务引用后删除成功", "已删除客户" in r.get_data(as_text=True))

    r = client.post(f"/admin/users/{user_rows['s2']}/delete", follow_redirects=True)
    check("无业务的人员可删除", "已删除人员" in r.get_data(as_text=True))

    r = client.post(f"/admin/depts/{depts['甲部门']}/delete", follow_redirects=True)
    check("部门仍有人员时仍拒绝删除", "不能删除" in r.get_data(as_text=True))

    client.post(f"/admin/users/{user_rows['s1']}/delete")
    client.post(f"/admin/users/{user_rows['m1']}/delete")
    r = client.post(f"/admin/depts/{depts['甲部门']}/delete", follow_redirects=True)
    check("人员清空后部门可删除", "已删除部门" in r.get_data(as_text=True))

    client.post("/admin/depts/new", data={"name": "空部门", "short_name": "空", "sort_order": "99"})
    with application.app_context():
        db = get_db()
        empty_id = db.execute(
            "SELECT id FROM departments WHERE name = '空部门'"
        ).fetchone()["id"]
    r = client.post(f"/admin/depts/{empty_id}/delete", follow_redirects=True)
    check("新建空部门可直接删除", "已删除部门" in r.get_data(as_text=True))

    print("\n=== 6. 客户档案页字段 ===")
    client.post("/customers/new", data={"name": "字段检查客户", "short_name": "字段"})
    r = client.get("/customers/")
    body = r.get_data(as_text=True)
    check("表头已改为「主体公司」", "<th>主体公司</th>" in body)
    check("不再出现「简称」表头", "<th>简称</th>" not in body)
    check("「业务数」列已移除", "<th class=\"num\">业务数</th>" not in body)
    check("表头已改为「归属部门」", "归属部门</th>" in body)
    check("操作列有删除按钮", '/delete"' in body)

    print("\n=== 7. 导入模板与旧模板兼容 ===")
    r = client.get("/customers/import/template")
    from openpyxl import load_workbook
    header = [c.value for c in load_workbook(io.BytesIO(r.data)).active[1]]
    check("新模板列名为「主体公司」", header[1] == "主体公司", str(header[:3]))

    # 旧模板：表头仍是「简称」，应能正常识别
    old = build_xlsx(
        ["客户名称", "简称", "联系人", "电话", "纳税人识别号", "开户行", "银行账号", "地址", "备注"],
        [["旧模板客户", "旧简称", "赵经理", "13900000000", "", "", "", "", ""]],
    )
    r = client.post("/customers/import", data={"file": (old, "old.xlsx")},
                    content_type="multipart/form-data", follow_redirects=True)
    check("旧模板（简称列）可导入", "新增 1" in r.get_data(as_text=True))
    with application.app_context():
        db = get_db()
        c = db.execute("SELECT * FROM customers WHERE name = '旧模板客户'").fetchone()
    check("旧模板的简称值写进主体公司字段", c is not None and c["short_name"] == "旧简称",
          (c["short_name"] if c else "无记录") or "")


if __name__ == "__main__":
    main()
    print("\n" + "=" * 46)
    print(f"通过 {len(PASS)} 项，失败 {len(FAIL)} 项")
    if FAIL:
        for f in FAIL:
            print("  - " + f)
        sys.exit(1)
    print("全部通过 ✅")
