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
         "渠道提成": 100, "业务金额": 500000, "费率": 1.5, "留存利润": 1000,
         "收款账户": "6222021234567890", "收款银行": "工商银行",
         "备注": "首单", "回单编号": "HD001"},
        {"序号": 2, "归属部门": "集团总部", "类型": "过账", "预存与否": "预存",
         "月份": "2026/11/02", "是否开票": "否", "主体公司": "成都蜀鲜果业",
         "上游公司": "沃田农艺园（无票）", "资金过夜费": 200,
         "业务金额": 800000, "费率": 2, "留存利润": 0,
         "收款账户": "6228480123456789", "收款银行": "农业银行"},
        {"序号": 3, "归属部门": "集团总部", "类型": "代采", "预存与否": "月结",
         "月份": "2026/11/03", "是否开票": "否", "主体公司": "深圳鲜达供应链",
         "渠道提成": 300, "业务金额": 1200000, "费率": "1.2%", "留存利润": 5000,
         "备注": "含税"},
        {"序号": 4, "归属部门": "集团总部", "类型": "自营", "预存与否": "后付",
         "月份": "2026/11/04", "是否开票": "否", "主体公司": "广州顺鑫农业",
         "财务费用": 20, "业务金额": 300000, "费率": 1, "留存利润": 0},
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
                    data={"file": (build_xlsx(), "业务登记_测试.xlsx"), "invoice_batch": "1"},
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
    check("贸易费 = 500000×1.5% = 7500 元",
          rec["payable_amount_cents"] == 750000, f"实际 {rec['payable_amount_cents']}")
    check("留存利润取 K 列合计 1000 元",
          rec["channel_commission_cents"] == 100000, f"实际 {rec['channel_commission_cents']}")
    check("待收 = 贸易费 7500 + 留存利润 1000 = 8500 元",
          rec["unpaid_amount_cents"] == 850000, f"实际 {rec['unpaid_amount_cents']}")
    check("未收款时公司实收 = 0（= 累计已收）",
          rec["company_income_cents"] == 0, f"实际 {rec['company_income_cents']}")
    check("费率写成「1.2%」也能识别 → 贸易费 1200000×1.2% = 14400 元",
          r2["payable_amount_cents"] == 1440000, f"实际 {r2['payable_amount_cents']}")
    check("第二档留存利润 5000 元", r2["channel_commission_cents"] == 500000,
          f"实际 {r2['channel_commission_cents']}")

    print("\n=== 3. 布尔与日期解析 ===")
    check("「是」识别为需要开票（登记口径）", rec["invoice_needed"] == 1)
    check("「否」识别为不需要开票", r2["invoice_needed"] == 0, str(r2["invoice_needed"]))
    check("日期 2026/11/01 解析为 2026-11-01", rec["biz_date"] == "2026-11-01",
          rec["biz_date"])
    check("「预存」识别为预存模式", r3["prepaid_mode"] == "预存", r3["prepaid_mode"])
    check("「月结」识别为月结模式", r2["prepaid_mode"] == "月结", r2["prepaid_mode"])

    print("\n=== 3.1 费用分项与上游公司 ===")
    check("费用明细「渠道提成」100 元写入", rec["fee_service_cents"] == 10000,
          str(rec["fee_service_cents"]))
    check("费用明细「渠道提成」300 元写入（第二档）", r2["fee_service_cents"] == 30000,
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
         "上游公司": "甲供应商（无票）", "渠道提成": 10},
        {"主体公司": "汇总结算客户（无票）", "月份": "2026-12", "业务金额": 300000, "费率": 2,
         "上游公司": "乙供应商", "渠道提成": 20},
    ]
    for r in rows2:
        ws2.append([r.get(h) for h in TEMPLATE_HEADERS])
    buf2 = io.BytesIO()
    wb2.save(buf2)
    buf2.seek(0)
    resp2 = client.post("/excel/import", data={"file": (buf2, "汇总测试.xlsx"), "invoice_batch": "1"},
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
        check("贸易费 = 400000×1.75% = 7,000 元", m["payable_amount_cents"] == 700000,
              str(m["payable_amount_cents"]))
        check("费用明细「渠道提成」合计 30 元", m["fee_service_cents"] == 3000,
              str(m["fee_service_cents"]))
        check("上游公司去重合并", m["upstream_company"] == "甲供应商、乙供应商",
              str(m["upstream_company"]))
    check("页面提示费率不一致按加权平均", "加权平均" in body2)

    print("\n=== 3.3 贸易计划 K 列（利润1）合计 → 留存利润 ===")
    wb3 = Workbook()
    ws3 = wb3.active
    ws3.append(["大类", "品种", "上游公司", "数量", "单价", "金额", "主体公司",
                "数量2", "单价2", "金额2", "利润1", "下游公司"])
    for row3 in (
        ["茄果蔬菜", "杭椒", "长沙易汐", 100, 6.22, 622, "贸易计划客户", 100, 6.25, 625, 3, "涌之泉"],
        ["豆菜", "豆角", "长沙易汐", 200, 5.87, 1174, "贸易计划客户", 200, 5.9, 1180, 6, "涌之泉"],
        ["汇总", "", None, 300, None, 1796, "", 300, None, 1805, 9, ""],
    ):
        ws3.append(row3)
    buf3 = io.BytesIO()
    wb3.save(buf3)
    buf3.seek(0)
    resp3 = client.post("/excel/import", data={"file": (buf3, "贸易计划_测试.xlsx"), "invoice_batch": "1"},
                        content_type="multipart/form-data", follow_redirects=True)
    body3 = resp3.get_data(as_text=True)
    with application.app_context():
        plan = get_db().execute(
            "SELECT * FROM records WHERE customer_name = '贸易计划客户'").fetchone()
    check("贸易计划能导入（2 行明细合并 1 条）", plan is not None)
    if plan:
        check("业务金额取「金额」列合计 1,796 元", plan["biz_amount_cents"] == 179600,
              str(plan["biz_amount_cents"]))
        check("留存利润取 K 列（利润1）合计 9 元",
              plan["channel_commission_cents"] == 900, str(plan["channel_commission_cents"]))
        check("没有费率列时贸易费按 0 计", plan["payable_amount_cents"] == 0,
              str(plan["payable_amount_cents"]))
        check("待收 = 贸易费 0 + 留存利润 9 = 900 分",
              plan["unpaid_amount_cents"] == 900, str(plan["unpaid_amount_cents"]))
    check("结果页说明留存利润取自 K 列", "K 列" in body3)

    print("\n=== 4. 导出列序 ===")
    r = client.get("/excel/export")
    check("导出返回 xlsx", r.status_code == 200 and len(r.data) > 4000, f"{len(r.data)} 字节")
    wb = load_workbook(io.BytesIO(r.data))
    ws = wb.active
    headers = [c.value for c in ws[1]]
    check("导出 列序与模板表头完全一致（含上游公司 + 四项费用）",
          headers == TEMPLATE_HEADERS,
          f"实际 {len(headers)} 列")
    check("导出数据行数 = 6", ws.max_row == 7, f"实际 {ws.max_row - 1} 行")

    print("\n=== 5. 空白模板下载 ===")
    r = client.get("/excel/template")
    wb = load_workbook(io.BytesIO(r.data))
    check("空白模板表头一致", [c.value for c in wb.active[1]] == TEMPLATE_HEADERS)

    print("\n=== 6. 表里给了贸易费就以表里的为准 ===")
    wb2 = Workbook()
    ws2 = wb2.active
    ws2.append(TEMPLATE_HEADERS)
    ws2.append([{"归属部门": "集团总部", "类型": "代采", "预存与否": "后付",
                 "月份": "2026/12/01", "是否开票": "否", "主体公司": "手改贸易费客户",
                 "业务金额": 500000, "费率": 1.5, "留存利润": 1000,
                 "贸易费": 7500.26}.get(h) for h in TEMPLATE_HEADERS])
    buf2 = io.BytesIO()
    wb2.save(buf2)
    buf2.seek(0)
    r6 = client.post("/excel/import", data={"file": (buf2, "贸易费_测试.xlsx"), "invoice_batch": "1"},
                     content_type="multipart/form-data", follow_redirects=True)
    body6 = r6.get_data(as_text=True)
    with application.app_context():
        got = get_db().execute(
            "SELECT * FROM records WHERE customer_name = '手改贸易费客户'").fetchone()
    check("贸易费按表内 7500.26 取值（公式是 7500）",
          got is not None and got["payable_amount_cents"] == 750026,
          str(got["payable_amount_cents"]) if got else "未导入")
    check("待收 = 7500.26 + 留存利润 1000 = 8500.26",
          got is not None and got["unpaid_amount_cents"] == 850026,
          str(got["unpaid_amount_cents"]) if got else "-")
    check("差异超过 1 分时提示「已按 Excel 取值」", "已按 Excel 取值" in body6)

    # 表里没有贸易费列的值（留空）仍按公式
    wb3 = Workbook()
    ws3 = wb3.active
    ws3.append(TEMPLATE_HEADERS)
    ws3.append([{"归属部门": "集团总部", "类型": "代采", "预存与否": "后付",
                 "月份": "2026/12/01", "是否开票": "否", "主体公司": "公式贸易费客户",
                 "业务金额": 500000, "费率": 1.5, "留存利润": 1000}.get(h)
                for h in TEMPLATE_HEADERS])
    buf3 = io.BytesIO()
    wb3.save(buf3)
    buf3.seek(0)
    client.post("/excel/import", data={"file": (buf3, "贸易费_空列.xlsx"), "invoice_batch": "1"},
                content_type="multipart/form-data", follow_redirects=True)
    with application.app_context():
        got2 = get_db().execute(
            "SELECT * FROM records WHERE customer_name = '公式贸易费客户'").fetchone()
    check("贸易费留空仍按公式 7500 元",
          got2 is not None and got2["payable_amount_cents"] == 750000,
          str(got2["payable_amount_cents"]) if got2 else "未导入")


    print("\n=== 7. 导入前必须确认「是否开票」 ===")
    # 7.1 没选口径直接导入 → 拦下，一条都不写
    with application.app_context():
        before = get_db().execute("SELECT COUNT(*) c FROM records").fetchone()["c"]
    r7 = client.post("/excel/import", data={"file": (build_xlsx(), "未选开票.xlsx")},
                     content_type="multipart/form-data", follow_redirects=True)
    body7 = r7.get_data(as_text=True)
    with application.app_context():
        after = get_db().execute("SELECT COUNT(*) c FROM records").fetchone()["c"]
    check("未确认开票口径时导入被拦下", "请先确认本批数据是否需要开票" in body7)
    check("被拦时一条都没写入", after == before, f"{before} → {after}")

    # 7.2 贸易计划模板没有开票列 → 用本批确认的「不开票」
    wb7 = Workbook()
    ws7 = wb7.active
    ws7.append(["大类", "品种", "上游公司", "数量", "单价", "金额", "主体公司",
                "数量2", "单价2", "金额2", "利润1", "下游公司"])
    ws7.append(["叶菜", "菜心", "长沙易汐", 10, 5, 50, "不开票客户", 10, 5.2, 52, 2, ""])
    buf7 = io.BytesIO()
    wb7.save(buf7)
    buf7.seek(0)
    r7b = client.post("/excel/import",
                      data={"file": (buf7, "贸易计划_不开票.xlsx"), "invoice_batch": "0"},
                      content_type="multipart/form-data", follow_redirects=True)
    body7b = r7b.get_data(as_text=True)
    with application.app_context():
        n7 = get_db().execute(
            "SELECT * FROM records WHERE customer_name = '不开票客户'").fetchone()
    check("选「不开票」导入后 invoice_needed = 0",
          n7 is not None and n7["invoice_needed"] == 0,
          str(n7["invoice_needed"]) if n7 else "未导入")
    check("结果页回显本批开票口径", "本批开票口径" in body7b and "不开票" in body7b)
    check("结果页说明文件里没有开票列", "文件里没有「是否开票」列" in body7b)
    check("汇总表列出「开票」列", ">开票</th>" in body7b)

    # 7.3 表内写了开票口径 → 以表内为准，与本批选择冲突时告警
    wb7c = Workbook()
    ws7c = wb7c.active
    ws7c.append(TEMPLATE_HEADERS)
    ws7c.append([{"归属部门": "集团总部", "类型": "代采", "预存与否": "后付",
                  "月份": "2026/12/01", "是否开票": "是", "主体公司": "表内优先客户",
                  "业务金额": 100000, "费率": 1, "留存利润": 0}.get(h)
                 for h in TEMPLATE_HEADERS])
    buf7c = io.BytesIO()
    wb7c.save(buf7c)
    buf7c.seek(0)
    r7c = client.post("/excel/import",
                      data={"file": (buf7c, "表内开票优先.xlsx"), "invoice_batch": "0"},
                      content_type="multipart/form-data", follow_redirects=True)
    body7c = r7c.get_data(as_text=True)
    with application.app_context():
        n7c = get_db().execute(
            "SELECT * FROM records WHERE customer_name = '表内优先客户'").fetchone()
    check("表内「是」覆盖本批的「不开票」→ invoice_needed = 1",
          n7c is not None and n7c["invoice_needed"] == 1,
          str(n7c["invoice_needed"]) if n7c else "未导入")
    check("冲突行在结果页告警", "与本次选的本批口径" in body7c)

    # 7.4 开票口径为空时不能提交；补确认后可提交
    print("\n=== 7.4 开票口径为空 → 不能提交 ===")
    with application.app_context():
        db = get_db()
        target = db.execute(
            "SELECT id FROM records WHERE customer_name = '广州华农贸易' ORDER BY id DESC LIMIT 1"
        ).fetchone()
        db.execute("UPDATE records SET invoice_needed = NULL WHERE id = ?", (target["id"],))
        db.commit()
    rid7 = target["id"]
    d7 = client.get(f"/records/{rid7}").get_data(as_text=True)
    check("详情页出现「待确认：是否需要开票」卡片", "待确认：是否需要开票" in d7)
    check("开票口径没确认时提交按钮不出现", "提交并进入收款" not in d7)
    r7d = client.post(f"/records/{rid7}/submit", follow_redirects=True)
    check("直接提交被拦下", "请先确认这笔业务是否需要开票" in r7d.get_data(as_text=True))
    r7e = client.post(f"/records/{rid7}/invoice-needed",
                      data={"invoice_needed": "0"}, follow_redirects=True)
    check("补确认后提示已确认", "已确认开票口径" in r7e.get_data(as_text=True))
    with application.app_context():
        got7 = get_db().execute("SELECT * FROM records WHERE id = ?", (rid7,)).fetchone()
    check("invoice_needed 落库为 0", got7["invoice_needed"] == 0, str(got7["invoice_needed"]))
    d7b = client.get(f"/records/{rid7}").get_data(as_text=True)
    check("确认后提交按钮恢复", "提交并进入收款" in d7b)
    r7f = client.post(f"/records/{rid7}/submit", follow_redirects=True)
    check("确认后可提交进入待收款",
          "已提交，进入待收款" in r7f.get_data(as_text=True))


    print("\n" + "=" * 46)
    print(f"通过 {len(PASS)} 项，失败 {len(FAIL)} 项")
    if FAIL:
        for f in FAIL:
            print("  - " + f)
        sys.exit(1)
    print("全部通过 ✅")

if __name__ == "__main__":
    main()
