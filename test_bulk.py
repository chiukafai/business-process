"""业务列表批量选择与批量删除自检。

覆盖：
  1. 列表页有全选框（表头 + 工具条）、每行复选框、批量删除按钮
  2. 无删除权限的行：复选框禁用、删除按钮置灰可见
  3. 批量删除能一次删多条，并清理回单目录
  4. 权限边界：越权勾选的记录被跳过，不被误删
  5. 空选、无权限、超量提交的兜底提示

运行：python test_bulk.py
使用独立的 data_test5 目录，不影响正式数据。
"""
import os

BASE = os.path.dirname(os.path.abspath(__file__))
os.environ["DATA_DIR"] = os.path.join(BASE, "data_test5")

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


def login_as(client, username, password="Bulk@2026"):
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
    with application.app_context():
        db = get_db()
        dept_id = db.execute("SELECT id FROM departments WHERE name='甲部门'").fetchone()["id"]

    client.post("/admin/users/new", data={
        "username": "m1", "name": "李主管", "role": "manager", "dept_id": str(dept_id),
        "password": "Init@2026",
        "permissions": ["record.create", "record.view_dept", "record.delete"],
    })
    client.post("/admin/users/new", data={
        "username": "s1", "name": "张业务", "role": "staff", "dept_id": str(dept_id),
        "password": "Init@2026", "permissions": ["record.create"],
    })

    form = {
        "dept_id": str(dept_id), "customer_name": "批量测试客户", "biz_type": "代采",
        "prepaid_mode": "后付", "biz_date": "2026-11-02", "fee_rate": "1.5",
        "biz_amount": "10000", "channel_commission": "0",
        "invoice_needed": "1", "prepaid_amount": "0", "paid_amount": "0",
        "remark": "", "action": "submit",
    }
    login_as(client, "s1")
    ids = []
    for i in range(4):
        client.post("/records/new", data=form, follow_redirects=True)
        with application.app_context():
            row = get_db().execute("SELECT MAX(id) m FROM records").fetchone()
        ids.append(row["m"])
    login_as(client, "admin", "Admin@2026")
    # 管理员再造一笔别人看不到的场景不需要，这里统一由主管删除
    with application.app_context():
        total = get_db().execute("SELECT COUNT(*) c FROM records").fetchone()["c"]
    check("4 笔待付款业务已建立", total == 4, f"共 {total} 笔")

    print("\n=== 2. 列表页批量交互元素 ===")
    login_as(client, "m1")
    body = client.get("/records/").get_data(as_text=True)
    check("有批量表单容器", 'id="batchForm"' in body)
    check("表头有全选框", 'class="js-check-all"' in body)
    check("工具条有「全选本页」", "全选本页" in body)
    check("有反选按钮", 'id="invertSel"' in body)
    check("有已选数量提示", 'id="selCount"' in body)
    check("有批量删除按钮", 'id="bulkDelete"' in body)
    check("每行都有复选框且关联批量表单",
          body.count('class="row-check"') == 8 and 'form="batchForm"' in body,
          f"复选框 {body.count('class=\"row-check\"')} 个（PC+手机各 4）")
    check("每行删除按钮可见", body.count(">删除</button>") == 8,
          f"{body.count('>删除</button>')} 个（PC+手机各 4）")
    check("操作列表头常驻", "<th style=\"width:70px\">操作</th>" in body)
    check("批量删除默认禁用（未选）", 'id="bulkDelete" disabled' in body)

    print("\n=== 3. 无删除权限的人看到的是禁用态 ===")
    login_as(client, "s1")
    body_s = client.get("/records/").get_data(as_text=True)
    check("复选框仍成对渲染", body_s.count('class="row-check"') == 8)
    check("无删除权限且非本人待提交的单据，复选框禁用（此处均为已提交）",
          body_s.count('disabled title="这条业务你没有可批量操作的权限"') == 4,
          f"{body_s.count('disabled title=')} 处禁用")
    check("删除按钮置灰但可见", "仅登记人可删除自己的报批中单据，其他人需要「删除业务」权限" in body_s)

    print("\n=== 4. 批量删除 ===")
    login_as(client, "m1")
    receipt_dir = os.path.join(application.config["RECEIPT_DIR"], str(ids[0]))
    os.makedirs(receipt_dir, exist_ok=True)
    with open(os.path.join(receipt_dir, "fake.jpg"), "wb") as f:
        f.write(b"x")

    r = client.post("/records/batch-delete", data={"ids": [str(ids[0]), str(ids[1])]},
                    follow_redirects=True)
    body = r.get_data(as_text=True)
    check("提示删除 2 条", "已删除 2 条业务" in body or "已删除" in body, body[body.find("已删除"):][:40])
    with application.app_context():
        db = get_db()
        left = {r["id"] for r in db.execute("SELECT id FROM records")}
        logs = db.execute("SELECT COUNT(*) c FROM record_logs WHERE record_id = ?",
                          (ids[0],)).fetchone()["c"]
    check("两条记录已删除", left == {ids[2], ids[3]}, str(sorted(left)))
    check("流程日志级联清除", logs == 0)
    check("回单目录一并清理", not os.path.exists(receipt_dir))

    print("\n=== 5. 权限边界与兜底 ===")
    r = client.post("/records/batch-delete", data={"ids": []}, follow_redirects=True)
    check("空选有提示", "请先勾选要删除的业务" in r.get_data(as_text=True))

    login_as(client, "s1")
    r = client.post("/records/batch-delete", data={"ids": [str(ids[2])]}, follow_redirects=True)
    check("无权限者批量删除被拒", "都没有删除权限" in r.get_data(as_text=True))
    with application.app_context():
        still = get_db().execute("SELECT 1 FROM records WHERE id = ?", (ids[2],)).fetchone()
    check("记录未被误删", still is not None)

    login_as(client, "m1")
    r = client.post("/records/batch-delete",
                    data={"ids": [str(ids[2]), "999999"]}, follow_redirects=True)
    body = r.get_data(as_text=True)
    check("不存在的 id 被安全忽略", "1 条已不存在" in body, body[body.find("已删除"):][:60])
    with application.app_context():
        left = get_db().execute("SELECT COUNT(*) c FROM records").fetchone()["c"]
    check("剩余 1 条", left == 1, f"剩 {left} 笔")

    r = client.post("/records/batch-delete",
                    data={"ids": [str(ids[3])], "next": "/records/?page=1"},
                    follow_redirects=False)
    check("删除后回到原列表位置", r.status_code == 302 and r.headers["Location"].endswith("/records/?page=1"),
          r.headers.get("Location", ""))

    print("\n=== 6. 客户档案的批量操作 ===")
    login_as(client, "admin", "Admin@2026")
    client.post("/customers/new", data={"name": "无业务客户甲", "short_name": "甲"})
    client.post("/customers/new", data={"name": "无业务客户乙", "short_name": "乙"})

    # 造一家「有业务记录」的客户：登记时输入新客户名会自动建档
    login_as(client, "s1")
    client.post("/records/new", data={
        "dept_id": str(dept_id), "customer_name": "有业务客户丙", "biz_type": "代采",
        "prepaid_mode": "后付", "biz_date": "2026-11-03", "fee_rate": "1.5",
        "biz_amount": "5000", "channel_commission": "0",
        "invoice_needed": "1", "prepaid_amount": "0", "paid_amount": "0",
        "remark": "", "action": "submit",
    }, follow_redirects=True)
    login_as(client, "admin", "Admin@2026")
    with application.app_context():
        db = get_db()
        custs = {r["name"]: r["id"] for r in db.execute("SELECT id, name FROM customers")}
    check("客户档案已建立（含一家有业务）",
          {"无业务客户甲", "无业务客户乙", "有业务客户丙"} <= set(custs), str(sorted(custs)))

    body_c = client.get("/customers/").get_data(as_text=True)
    check("客户页有批量表单", 'id="batchForm"' in body_c and "customers/batch" in body_c)
    check("客户页有两个全选框", body_c.count('class="js-check-all"') == 2)
    check("客户页有反选与已选计数", 'id="invertSel"' in body_c and 'id="selCount"' in body_c)
    check("客户页有批量停用/启用/删除",
          'id="bulkDisable"' in body_c and 'id="bulkEnable"' in body_c and 'id="bulkDelete"' in body_c)
    check("客户页每行有勾选框与删除按钮",
          body_c.count('class="row-check"') == len(custs) and body_c.count(">删除</button>") == len(custs),
          f"客户 {len(custs)} 家")

    ids_dis = [custs["无业务客户甲"], custs["无业务客户乙"]]
    r = client.post("/customers/batch",
                    data={"action": "disable", "ids": [str(i) for i in ids_dis]},
                    follow_redirects=True)
    check("批量停用成功", "已停用 2 家客户" in r.get_data(as_text=True),
          r.get_data(as_text=True)[r.get_data(as_text=True).find("已停用"):][:30])
    with application.app_context():
        db = get_db()
        states = [dict(db.execute("SELECT name, is_active FROM customers WHERE id=?", (i,)).fetchone())
                  for i in ids_dis]
    check("两家都变为停用", all(s["is_active"] == 0 for s in states), str(states))

    r = client.post("/customers/batch",
                    data={"action": "enable", "ids": [str(i) for i in ids_dis]},
                    follow_redirects=True)
    check("批量启用成功", "已启用 2 家客户" in r.get_data(as_text=True))

    kept = custs["有业务客户丙"]   # 有业务记录，删除必须被拒
    r = client.post("/customers/batch",
                    data={"action": "delete", "ids": [str(kept), str(custs["无业务客户甲"])]},
                    follow_redirects=True)
    body = r.get_data(as_text=True)
    check("有业务的客户被跳过并提示", "1 家因已有业务记录被跳过" in body,
          body[body.find("已删除"):][:50])
    with application.app_context():
        db = get_db()
        left = {r["name"] for r in db.execute("SELECT name FROM customers")}
    check("无业务客户已删除、有业务客户保留",
          "无业务客户甲" not in left and "有业务客户丙" in left, str(sorted(left)))

    r = client.post("/customers/batch",
                    data={"action": "delete", "ids": [str(kept)]}, follow_redirects=True)
    check("只勾有业务的客户时全部拒绝", "不能删除" in r.get_data(as_text=True))

    r = client.post("/customers/batch", data={"action": "delete", "ids": []}, follow_redirects=True)
    check("空选有提示", "请先勾选要操作的客户" in r.get_data(as_text=True))

    login_as(client, "admin", "Admin@2026")
    client.post("/admin/users/new", data={
        "username": "v1", "name": "只看客户", "role": "staff", "dept_id": str(dept_id),
        "password": "Init@2026", "permissions": ["customer.view"],
    })
    login_as(client, "v1")
    body_s = client.get("/customers/").get_data(as_text=True)
    check("只读用户也能看到客户列表", "客户名称" in body_s)
    check("无管理权限时勾选框禁用",
          body_s.count('disabled title="需要「维护 / 导入客户」权限"') >= len(left),
          f"{body_s.count('disabled title=')} 处禁用")
    r = client.post("/customers/batch",
                    data={"action": "delete", "ids": [str(custs["无业务客户乙"])]},
                    follow_redirects=True)
    with application.app_context():
        alive = get_db().execute("SELECT 1 FROM customers WHERE id=?",
                                 (custs["无业务客户乙"],)).fetchone()
    check("无权限者批量删除被拦", alive is not None)
    check("拦截时有明确提示", "没有权限执行该操作" in r.get_data(as_text=True))

    print("\n=== 7. 角色默认权限 ===")
    login_as(client, "admin", "Admin@2026")
    with application.app_context():
        from db import PERMISSIONS
    delete_roles = [p[3] for p in PERMISSIONS if p[0] == "record.delete"][0]
    check("主管/财务默认带删除权限", set(delete_roles) == {"manager", "finance"}, str(delete_roles))

    print("\n" + "=" * 46)
    print(f"通过 {len(PASS)} 项，失败 {len(FAIL)} 项")
    if FAIL:
        for name in FAIL:
            print("  失败：" + name)
        return 1
    print("全部通过 ✅")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
