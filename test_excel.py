"""Excel 导入导出自检：按模板表头造数据，验证导入校验、费用分项与导出列序。"""
import io
import os
import sys

os.environ["DATA_DIR"] = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data_test")

for _f in ("app.db", "app.db-wal", "app.db-shm", "secret.key"):
    _p = os.path.join(os.environ["DATA_DIR"], _f)
    if os.path.exists(_p):
        os.remove(_p)

from openpyxl import Workbook, load_workbook  # noqa: E402

import app as app_module  # noqa: E402
from db import get_db  # noqa: E402
from views.excel_io import TEMPLATE_HEADERS  # noqa: E402

PASS, FAIL = [], []


def check(name, cond, extra=""):
    (PASS if cond else FAIL).append(name)
    print(("  ✅ " if cond else "  ❌ ") + name + (f"  {extra}" if extra else ""))


def build_xlsx():
    wb = Workbook()
    ws = wb.active
    ws.append(TEMPLATE_HEADERS)
    # 4 行有效 + 1 行缺客户名 + 1 行金额为 0
    rows = [
        {"序号": 1, "归属部门": "集团总部", "类型": "代采", "预存与否": "后付",
         "月份": "2026/11/01", "报送计划时间": "2026/11/05", "计划出具日期": "2026/11/10",
         "是否开票": "是", "主体公司": "广州华农贸易", "上游公司": "沃田农艺园",
         "服务费": 100, "业务金额": 500000, "费率": 1.5, "渠道提成": 1000,
         "收款账户": "6222021234567890", "收款银行": "工商银行",
         "备注": "首单", "回单编号": "HD001"},
        {"序号": 2, "归属部门": "集团总部", "类型": "过账", "预存与否": "预存",
         "月份": "2026/11/02", "是否开票": "否", "主体公司": "成都蜀鲜果业",
         "上游公司": "沃田农艺园（无票）", "资金过夜费": 200,
         "业务金额": 800000, "费率": 2, "渠道提成": 0,
         "收款账户": "6228480123456789", "收款银行": "农业银行"},
        {"序号": 3, "归属部门": "集团总部", "类型": "代采", "预存与否": "月结",
         "月份": "2026/11/03", "是否开票": "否", "主体公司": "深圳鲜达供应链",
         "服务费": 300, "业务金额": 1200000, "费率": "1.2%", "渠道提成": 5000,
         "备注": "含税"},
        {"序号": 4, "归属部门": "集团总部", "类型": "自营", "预存与否": "后付",
         "月份": "2026/11/04", "是否开票": "否", "主体公司": "广州顺鑫农业",
         "财务费用": 20, "业务金额": 300000, "费率": 1, "渠道提成": 0},
        {"序号": 5, "归属部门": "集团总部", "类型": "代采", "预存与否": "后付",
         "月份": "2026/11/05", "主体公司": "", "业务金额": 100000, "费率": 1,
         "备注": "缺客户名"},
        {"序号": 6, "归属部门": "集团总部", "类型": "代采", "预存与否": "后付",
         "月份": "2026/11/05", "主体公司": "零金额客户", "业务金额": 0, "费率": 1},
    ]
    for r in rows:
        ws.append([r.get(h) for h in TEMPLATE_HEADERS])
    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return buf


def main():
    application = app_module.create_app()
    application.config["TESTING"] = True
    client = application.test_client()

    # 登录并改密
    client.post("/login", data={"username": "admin", "password": "Hailu@2026"})
    client.post("/change-password", data={
        "old_password": "Hailu@2026", "new_password": "Admin@2026",
        "confirm_password": "Admin@2026"})

    print("\n=== 1. 导入校验 ===")
    r = client.post("/excel/import",
                    data={"file": (build_xlsx(), "业务登记_测试.xlsx")},
                    content_type="multipart/form-data",
                    follow_redirects=True)
    body = r.get_data(as_text=True)
    check("导入请求成功", r.status_code == 200)
    with application.app_context():
        db = get_db()
        count = db.execute("SELECT COUNT(*) c FROM records").fetchone()["c"]
    check("4 行有效数据入库", count == 4, f"实际 {count} 行")
    check("缺主体公司的行被拒", "主体公司为空" in body)
    check("金额为 0 的行被拒", "业务金额为空或不大于 0" in body)
    check("导入后为报批中状态", "报批中" in body)

    print("\n=== 2. 导入后的金额口径 ===")
    with application.app_context():
        db = get_db()
        rec = db.execute(
            "SELECT * FROM records WHERE customer_name = '广州华农贸易'"
        ).fetchone()
        r2 = db.execute(
            "SELECT * FROM records WHERE customer_name = '深圳鲜达供应链'"
        ).fetchone()
        r3 = db.execute(
            "SELECT * FROM records WHERE customer_name = '成都蜀鲜果业'"
        ).fetchone()
    check("费率 1.5% + 提成 1000 → 公司实收 6500 元",
          rec["company_income_cents"] == 650000, f"实际 {rec['company_income_cents']}")
    check("应付留空 → 自动取辅助核算费用 493500 元",
          rec["payable_amount_cents"] == 49350000, f"实际 {rec['payable_amount_cents']}")
    check("费率写成「1.2%」也能识别 → 实收 14400−5000=9400 元",
          r2["company_income_cents"] == 940000, f"实际 {r2['company_income_cents']}")

    print("\n=== 3. 布尔与日期解析 ===")
    check("「是」识别为已开票", rec["is_invoiced"] == 1)
    check("日期 2026/11/01 解析为 2026-11-01", rec["biz_date"] == "2026-11-01",
          rec["biz_date"])
    check("「预存」识别为预存模式", r3["prepaid_mode"] == "预存", r3["prepaid_mode"])
    check("「月结」识别为月结模式", r2["prepaid_mode"] == "月结", r2["prepaid_mode"])

    print("\n=== 3.1 费用分项与上游公司 ===")
    check("服务费 100 元写入", rec["fee_service_cents"] == 10000, str(rec["fee_service_cents"]))
    check("服务费 300 元写入（第二档）", r2["fee_service_cents"] == 30000,
          str(r2["fee_service_cents"]))
    check("资金过夜费 200 元写入", r3["fee_overnight_cents"] == 20000,
          str(r3["fee_overnight_cents"]))
    with application.app_context():
        db = get_db()
        r4 = db.execute(
            "SELECT * FROM records WHERE customer_name = '广州顺鑫农业'").fetchone()
    check("第四行财务费用 20 元写入", r4 is not None and r4["fee_finance_cents"] == 2000,
          str(r4["fee_finance_cents"] if r4 else None))
    check("上游公司写入", rec["upstream_company"] == "沃田农艺园", str(rec["upstream_company"]))
    check("另一行上游公司括号备注被清理",
          r3["upstream_company"] == "沃田农艺园", str(r3["upstream_company"]))

    print("\n=== 3.2 同一主体公司多行合并（加权费率 + 上游去重） ===")
    wb2 = Workbook()
    ws2 = wb2.active
    ws2.append(TEMPLATE_HEADERS)
    rows2 = [
        {"主体公司": "汇总结算客户", "月份": "2026-12", "业务金额": 100000, "费率": 1,
         "上游公司": "甲供应商（无票）", "服务费": 10},
        {"主体公司": "汇总结算客户（无票）", "月份": "2026-12", "业务金额": 300000, "费率": 2,
         "上游公司": "乙供应商", "服务费": 20},
    ]
    for r in rows2:
        ws2.append([r.get(h) for h in TEMPLATE_HEADERS])
    buf2 = io.BytesIO()
    wb2.save(buf2)
    buf2.seek(0)
    resp2 = client.post("/excel/import", data={"file": (buf2, "汇总测试.xlsx")},
                        content_type="multipart/form-data", follow_redirects=True)
    body2 = resp2.get_data(as_text=True)
    with application.app_context():
        merged = get_db().execute(
            "SELECT * FROM records WHERE customer_name = '汇总结算客户'").fetchall()
    check("两行合并为一条业务", len(merged) == 1, f"实际 {len(merged)} 条")
    if merged:
        m = merged[0]
        check("金额合计 400,000 元", m["biz_amount_cents"] == 40000000, str(m["biz_amount_cents"]))
        check("费率按金额加权平均 1.75%", abs(m["fee_rate"] - 1.75) < 1e-6, str(m["fee_rate"]))
        check("公司实收 7,000 元", m["company_income_cents"] == 700000,
              str(m["company_income_cents"]))
        check("服务费合计 30 元", m["fee_service_cents"] == 3000, str(m["fee_service_cents"]))
        check("上游公司去重合并", m["upstream_company"] == "甲供应商、乙供应商",
              str(m["upstream_company"]))
    check("页面提示费率不一致按加权平均", "加权平均" in body2)

    print("\n=== 4. 导出列序 ===")
    r = client.get("/excel/export")
    check("导出返回 xlsx", r.status_code == 200 and len(r.data) > 4000, f"{len(r.data)} 字节")
    wb = load_workbook(io.BytesIO(r.data))
    ws = wb.active
    headers = [c.value for c in ws[1]]
    check("导出 列序与模板表头完全一致（含上游公司 + 四项费用）",
          headers == TEMPLATE_HEADERS,
          f"实际 {len(headers)} 列")
    check("导出数据行数 = 5", ws.max_row == 6, f"实际 {ws.max_row - 1} 行")

    print("\n=== 5. 空白模板下载 ===")
    r = client.get("/excel/template")
    wb = load_workbook(io.BytesIO(r.data))
    check("空白模板表头一致", [c.value for c in wb.active[1]] == TEMPLATE_HEADERS)


if __name__ == "__main__":
    main()
    print("\n" + "=" * 46)
    print(f"通过 {len(PASS)} 项，失败 {len(FAIL)} 项")
    if FAIL:
        for f in FAIL:
            print("  - " + f)
        sys.exit(1)
    print("全部通过 ✅")
