"""客户档案导入自检：新增 / 同名更新 / 文件内重复 / 空名称跳过。

运行：python test_customers.py
使用独立的 data_test2 目录，不影响正式数据。
"""
import io
import os
import sys

os.environ["DATA_DIR"] = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data_test2")

for _f in ("app.db", "app.db-wal", "app.db-shm", "secret.key"):
    _p = os.path.join(os.environ["DATA_DIR"], _f)
    if os.path.exists(_p):
        os.remove(_p)

from openpyxl import Workbook, load_workbook  # noqa: E402

import app as app_module  # noqa: E402
from db import get_db  # noqa: E402

PASS, FAIL = [], []


def check(name, cond, extra=""):
    (PASS if cond else FAIL).append(name)
    print(("  ✅ " if cond else "  ❌ ") + name + (f"  {extra}" if extra else ""))


def build_xlsx(rows):
    wb = Workbook()
    ws = wb.active
    ws.append(["客户名称", "简称", "联系人", "电话", "纳税人识别号",
               "开户行", "银行账号", "地址", "备注"])
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

    print("\n=== 1. 模板下载 ===")
    r = client.get("/customers/import/template")
    check("模板可下载且为 xlsx", r.status_code == 200 and
          "spreadsheet" in r.headers.get("Content-Type", ""))
    wb = load_workbook(io.BytesIO(r.data))
    header = [c.value for c in wb.active[1]]
    check("模板表头包含客户名称", header[0] == "客户名称", str(header[:3]))

    print("\n=== 2. 首次导入（新增 3 家）===")
    f = build_xlsx([
        ["广州华农贸易有限公司", "华农", "张经理", "13800000001", "91440101X", "中国银行", "6222001", "广州", "月结"],
        ["成都蜀鲜果业有限公司", "蜀鲜", "王经理", "13800000002", "", "", "", "", ""],
        ["深圳鲜达供应链有限公司", "鲜达", "李经理", "13800000003", "", "", "", "", "重点客户"],
    ])
    r = client.post("/customers/import", data={"file": (f, "customers.xlsx")},
                    content_type="multipart/form-data", follow_redirects=True)
    body = r.get_data(as_text=True)
    check("导入后提示新增 3 家", "新增 3" in body, body[body.find("导入完成"):][:60] if "导入完成" in body else body[:120])
    with application.app_context():
        db = get_db()
        n = db.execute("SELECT COUNT(*) c FROM customers").fetchone()["c"]
    check("客户表里有 3 条", n == 3, f"实际 {n}")

    print("\n=== 3. 再次导入：同名更新 + 文件内重复 + 空名称 ===")
    f = build_xlsx([
        ["广州华农贸易有限公司", "华农新简称", "", "13800009999", "", "", "", "", ""],
        ["广州华农贸易有限公司", "重复", "", "", "", "", "", "", ""],
        ["", "无名称", "", "", "", "", "", "", ""],
        ["新增第三家客户", "第三", "", "", "", "", "", "", ""],
    ])
    r = client.post("/customers/import", data={"file": (f, "customers2.xlsx")},
                    content_type="multipart/form-data", follow_redirects=True)
    body = r.get_data(as_text=True)
    check("同名客户走更新而非新增", "更新 1" in body)
    check("文件内重复行被跳过", "文件内重复" in body)
    check("空名称行被跳过", "客户名称为空" in body)
    check("新增客户正常写入", "新增 1" in body)

    with application.app_context():
        db = get_db()
        row = db.execute(
            "SELECT * FROM customers WHERE name = '广州华农贸易有限公司'"
        ).fetchone()
        total = db.execute("SELECT COUNT(*) c FROM customers").fetchone()["c"]
    check("主体公司字段被更新", row["short_name"] == "华农新简称", row["short_name"] or "")
    check("电话被更新", row["phone"] == "13800009999", row["phone"] or "")
    check("原有非空字段未被空值覆盖（地址仍在）", row["address"] == "广州", row["address"] or "")
    check("客户总数 = 4", total == 4, f"实际 {total}")

    print("\n=== 4. 页面与权限 ===")
    r = client.get("/customers/")
    check("客户列表显示导入结果", "广州华农贸易有限公司" in r.get_data(as_text=True))
    r = client.post(f"/customers/{row['id']}/toggle", follow_redirects=True)
    check("可停用客户", "已停用" in r.get_data(as_text=True))

    print("\n=== 5. 登记页只带出启用客户 ===")
    r = client.get("/records/new")
    body = r.get_data(as_text=True)
    check("停用客户不出现在下拉里", "广州华农贸易有限公司" not in body)
    check("启用客户出现在下拉里", "成都蜀鲜果业有限公司" in body)


if __name__ == "__main__":
    main()
    print("\n" + "=" * 46)
    print(f"通过 {len(PASS)} 项，失败 {len(FAIL)} 项")
    if FAIL:
        for f in FAIL:
            print("  - " + f)
        sys.exit(1)
    print("全部通过 ✅")
