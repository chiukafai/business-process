"""用「贸易计划」模板格式验证导入改造（自造脱敏样例，不依赖真实业务文件）：

1. 业务金额按「主体公司」汇总，不再每一行生成一条业务
2. 上游公司（C 列）带入业务登记，下游公司（L 列）不导入
3. 费用类型改为四项单独填列，「通道费」改名「资金过夜费」
4. 名称末尾括号备注先清理再入库

样例文件每次运行时由本脚本生成到 data_tpl/ 下，公司名与金额均为虚构，
列结构与《贸易计划》模板完全一致，因此可以直接在任意机器上跑。
"""
import io
import os
import sys

BASE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(BASE, "data_tpl")
os.makedirs(DATA, exist_ok=True)
XLSX = os.path.join(DATA, "示例贸易计划.xlsx")

for name in ("app.db", "app.db-wal", "app.db-shm", "secret.key"):
    path = os.path.join(DATA, name)
    if os.path.exists(path):
        os.remove(path)


def make_sample_xlsx(path):
    """按贸易计划模板的列结构造脱敏样例：3 行明细 + 1 汇总行 + 表尾零散计算块。"""
    from openpyxl import Workbook

    wb = Workbook()
    ws = wb.active
    ws.append(["大类", "品种", "上游公司", "数量", "单价", "金额", "主体公司",
               "数量2", "单价2", "金额2", "利润", "下游公司", "列1", "列2"])
    rows = [
        ["豆菜", "豆角", "沃田农艺园（无票）", 34610, 6.01, 100000.5, "粤丰果业",
         34500, 6.06, 100500, 499.50, "批发商（广州）-示例商行", 21, "广州"],
        ["茄果蔬菜", "青线椒", "沃田农艺园", 11930, 14.01, 200000.25, "粤丰果业",
         11850, 14.12, 200300, 299.75, "批发商（广州）-示例商行", 22, "深圳"],
        ["青菜", "上海青", "沃田农艺园", 97370, 2.21, 300000.25, "粤丰果业",
         97300, 2.25, 300500, 499.75, "批发商（广州）-示例商行", 22, "广州"],
        # 汇总行：应被跳过，不当成业务
        ["汇总", None, 3, 143910, None, 600001, None, 143650, None, 601300,
         1299.0, None, None, None],
        [None] * 14,
        # 表尾零散计算块：客户名位置是数字，应被跳过
        [None, None, None, None, None, "贸易", 1948, 0.0033, None, None, None, None, None, None],
        [None, None, None, None, None, "利润", 1299.0, 1948.09923, None, None, None, None, None, None],
        [None, None, None, None, None, None, 3247.0, None, None, None, None, None, None, None],
    ]
    for r in rows:
        ws.append(r)
    wb.save(path)
    return path


make_sample_xlsx(XLSX)

os.environ["DATA_DIR"] = DATA
sys.path.insert(0, BASE)

import app as app_module  # noqa: E402
from db import get_db  # noqa: E402
from utils import (FEE_TYPES, clean_company_name, fee_total_cents, month_bounds,  # noqa: E402
                  month_label, month_value, parse_month)

passed = failed = 0


def check(label, ok, extra=""):
    global passed, failed
    if ok:
        passed += 1
        print(f"  [OK] {label}")
    else:
        failed += 1
        print(f"  [FAIL] {label}  {extra}")


print("=== 0. 名称清理（去掉末尾括号备注） ===")
check("粤丰果业（无票）",
      clean_company_name("粤丰果业（无票）") == "粤丰果业",
      clean_company_name("粤丰果业（无票）"))
check("沃田农艺园（无票）",
      clean_company_name("沃田农艺园（无票）") == "沃田农艺园",
      clean_company_name("沃田农艺园（无票）"))
check("半角括号 (无票)",
      clean_company_name("沃田农艺园(无票)") == "沃田农艺园",
      clean_company_name("沃田农艺园(无票)"))
check("【无票】方括号",
      clean_company_name("沃田农艺园【无票】") == "沃田农艺园",
      clean_company_name("沃田农艺园【无票】"))
check("括号 + 多余符号 + 空格",
      clean_company_name(" 粤丰果业 （ 无票 ） - ") == "粤丰果业",
      clean_company_name(" 粤丰果业 （ 无票 ） - "))
check("多重括号备注连续清理",
      clean_company_name("粤丰果业（无票）（广州）") == "粤丰果业",
      clean_company_name("粤丰果业（无票）（广州）"))
check("括号在中间时不误删",
      clean_company_name("广州（集团）食品有限公司") == "广州（集团）食品有限公司",
      clean_company_name("广州（集团）食品有限公司"))
check("末尾括号是名称组成部分时保留",
      clean_company_name("果业（集团）") == "果业（集团）",
      clean_company_name("果业（集团）"))
check("干净名称原样返回",
      clean_company_name("粤丰果业") == "粤丰果业")
check("空值返回空串", clean_company_name(None) == "" and clean_company_name("  ") == "")

print("\n=== 1. 月份工具 ===")
check("parse_month('2026-11')", parse_month("2026-11") == "2026-11-01", parse_month("2026-11"))
check("parse_month('2026年11月')", parse_month("2026年11月") == "2026-11-01")
check("parse_month('2026-11-05') 归一为月首", parse_month("2026-11-05") == "2026-11-01")
check("parse_month 非法返回 None", parse_month("贸易") is None)
check("month_label 月首", month_label("2026-11-01") == "2026年11月", month_label("2026-11-01"))
check("month_label 保留历史日", month_label("2026-11-05") == "2026年11月5日", month_label("2026-11-05"))
check("month_value 回填", month_value("2026-11-01") == "2026-11")
check("month_bounds 月末", month_bounds("2026-02")[1] == "2026-02-28", str(month_bounds("2026-02")))
check("month_bounds 12 月", month_bounds("2026-12")[1] == "2026-12-31")

application = app_module.create_app()
application.config["TESTING"] = True
client = application.test_client()

print("\n=== 2. 登录并改密 ===")
r = client.post("/login", data={"username": "admin", "password": "Hailu@2026"}, follow_redirects=True)
if "用户名或密码不正确" in r.get_data(as_text=True):
    client.post("/login", data={"username": "admin", "password": "Admin@2026"}, follow_redirects=True)
else:
    client.post("/change-password", data={
        "old_password": "Hailu@2026", "new_password": "Admin@2026",
        "confirm_password": "Admin@2026",
    }, follow_redirects=True)
check("管理员登录", "退出" in client.get("/dashboard").get_data(as_text=True))

print("\n=== 3. 导入贸易计划模板（按主体公司汇总） ===")
with open(XLSX, "rb") as fh:
    payload = {"file": (fh, os.path.basename(XLSX))}
    resp = client.post("/excel/import", data=payload,
                       content_type="multipart/form-data", follow_redirects=True)
body = resp.get_data(as_text=True)
check("导入请求返回 200", resp.status_code == 200, resp.status_code)
check("提示缺少字段的情况已消失", "未识别到表头" not in body)
check("页面出现导入结果", "导入结果" in body)
check("页面出现「按主体公司汇总」区块", "按主体公司汇总" in body)

with application.app_context():
    db = get_db()
    rows = db.execute(
        "SELECT * FROM records ORDER BY id"
    ).fetchall()

print(f"\n  实际写入 {len(rows)} 条业务")
for r in rows:
    print(f"    #{r['id']} {r['customer_name']} | {r['biz_amount_cents'] / 100:.2f} "
          f"| 上游 {r['upstream_company']} | {r['biz_date']}")

check("3 行明细汇总成 1 条业务", len(rows) == 1, str(len(rows)))

if rows:
    rec = rows[0]
    check("G 列「主体公司」当作客户名称", rec["customer_name"] == "粤丰果业", rec["customer_name"])
    check("F 列「金额」三行合计 600,001.00",
          rec["biz_amount_cents"] == 60000100, str(rec["biz_amount_cents"]))
    check("C 列「上游公司」写入业务登记",
          "沃田农艺园" in (rec["upstream_company"] or ""), str(rec["upstream_company"]))
    check("上游公司括号备注已清理、去重后只剩一个",
          rec["upstream_company"] == "沃田农艺园", str(rec["upstream_company"]))
    check("主体公司括号备注已清理（客户名不含括号）",
          "（" not in rec["customer_name"] and "(" not in rec["customer_name"],
          rec["customer_name"])
    check("月份落为当月首日",
          bool(rec["biz_date"]) and rec["biz_date"].endswith("-01"), str(rec["biz_date"]))
    check("备注留了导入汇总摘要",
          "导入汇总" in (rec["remark"] or "") and "豆角" in (rec["remark"] or ""),
          str(rec["remark"]))
    check("费用四项默认为 0",
          all(rec[col] == 0 for _f, _l, col in FEE_TYPES),
          str([rec[col] for _f, _l, col in FEE_TYPES]))

    with application.app_context():
        db = get_db()
        cust = db.execute("SELECT id FROM customers WHERE name = '粤丰果业'").fetchone()
        check("客户档案自动建档", cust is not None)

print("\n=== 4. 汇总行 / 下游公司 ===")
check("页面出现「已跳过的行」区块", "已跳过的行" in body)
check("页面出现导入说明", "导入说明" in body)
check("说明了汇总口径", "按主体公司汇总" in body)
check("说明「下游公司」不导入", "下游公司" in body)
check("下游公司未出现在未导入列提示里",
      "以下列未导入" not in body or "下游公司" not in body.split("以下列未导入")[1].split("<")[0]
      if "以下列未导入" in body else True)

print("\n=== 5. 客户档案的发票额度 ===")
client.post("/customers/new", data={
    "name": "测试客户甲", "short_name": "甲", "invoice_quota": "500000",
})
with application.app_context():
    db = get_db()
    c = db.execute("SELECT * FROM customers WHERE name = '测试客户甲'").fetchone()
check("新增客户带发票额度", c is not None and c["invoice_quota_cents"] == 50000000,
      str(dict(c) if c else None))

if c:
    client.post(f"/customers/{c['id']}/update", data={
        "name": "测试客户甲", "short_name": "甲", "invoice_quota": "800,000",
    })
    with application.app_context():
        c2 = get_db().execute("SELECT invoice_quota_cents FROM customers WHERE id=?",
                              (c["id"],)).fetchone()
    check("编辑支持千分位写法", c2["invoice_quota_cents"] == 80000000, str(c2["invoice_quota_cents"]))

cust_page = client.get("/customers/").get_data(as_text=True)
check("客户列表出现「发票额度」列", "发票额度" in cust_page)
check("列表显示剩余额度", "剩" in cust_page)

print("\n=== 6. 业务登记表单 ===")
form = client.get("/records/new").get_data(as_text=True)
check("月份用年+月下拉", 'name="biz_year"' in form and 'name="biz_month_no"' in form)
check("不再有日期输入框", 'name="biz_date"' not in form)
check("月份下拉含 12 个选项", form.count("月</option>") == 12, str(form.count("月</option>")))
check("表单有「上游公司」输入框", 'name="upstream_company"' in form)
check("费用类型不再用选择框", 'name="fee_type"' not in form)
check("费用四项各自一个输入框",
      all(f'name="{f}"' in form for f, _l, _c in FEE_TYPES),
      str([f for f, _l, _c in FEE_TYPES if f'name="{f}"' not in form]))
check("四项费用在「金额核算」区内", form.index("费用明细") > form.index("金额核算"))
check("「资金过夜费」已出现", "资金过夜费" in form)
check("「通道费」已改名不再出现", "通道费" not in form)
check("试算面板有费用合计", 'id="calcFee"' in form)

print("\n=== 7. 新建业务：月份 + 费用分项 ===")
with application.app_context():
    dept = get_db().execute("SELECT id FROM departments ORDER BY id LIMIT 1").fetchone()["id"]
resp = client.post("/records/new", data={
    "dept_id": str(dept), "customer_name": "费用测试客户", "biz_type": "代采",
    "prepaid_mode": "后付", "biz_year": "2025", "biz_month_no": "3",
    "fee_rate": "1.5", "biz_amount": "10000", "channel_commission": "0",
    "invoice_needed": "1", "prepaid_amount": "0", "paid_amount": "0",
    "upstream_company": "沃田农艺园",
    "fee_service": "100", "fee_overnight": "50.5",
    "fee_finance": "20", "fee_other": "1.25",
    "action": "save",
}, follow_redirects=True)
with application.app_context():
    rec = get_db().execute(
        "SELECT * FROM records WHERE customer_name='费用测试客户'").fetchone()
check("按年月保存为 2025-03-01", rec is not None and rec["biz_date"] == "2025-03-01",
      str(dict(rec) if rec else None))
if rec:
    check("上游公司保存成功", rec["upstream_company"] == "沃田农艺园", str(rec["upstream_company"]))
    check("渠道提成（费用明细）100 元", rec["fee_service_cents"] == 10000, str(rec["fee_service_cents"]))
    check("资金过夜费 50.5 元", rec["fee_overnight_cents"] == 5050, str(rec["fee_overnight_cents"]))
    check("财务费用 20 元", rec["fee_finance_cents"] == 2000, str(rec["fee_finance_cents"]))
    check("其他费用 1.25 元", rec["fee_other_cents"] == 125, str(rec["fee_other_cents"]))
    check("费用合计 171.75 元", fee_total_cents(rec) == 17175, str(fee_total_cents(rec)))

    detail = client.get(f"/records/{rec['id']}").get_data(as_text=True)
    check("详情页显示费用明细四项",
          all(lbl in detail for _f, lbl, _c in FEE_TYPES))
    check("详情页显示费用合计", "费用合计" in detail)
    check("详情页显示上游公司", "上游公司" in detail and "沃田农艺园" in detail)

    edit = client.get(f"/records/{rec['id']}/edit").get_data(as_text=True)
    check("编辑页回填资金过夜费 50.50", 'value="50.50"' in edit, "未见回填值")

print("\n=== 8. 列表按月筛选与显示 ===")
page = client.get("/records/?from=2025-01&to=2025-12").get_data(as_text=True)
check("筛选参数不报错", page.count("费用测试客户") >= 1)
check("列表显示年月而非日期", "2025年3月" in page)
page_all = client.get("/records/").get_data(as_text=True)
check("列表表头为「月份」", "<th>月份</th>" in page_all)
check("列表有「上游公司」列", "<th>上游公司</th>" in page_all)
check("筛选框改为月份选择", 'type="month"' in page_all)

print("\n=== 9. 导出 ===")
export = client.get("/excel/export")
check("导出返回 xlsx", export.status_code == 200
      and "spreadsheetml" in export.headers.get("Content-Type", ""))
from openpyxl import load_workbook  # noqa: E402

wb = load_workbook(io.BytesIO(export.data))
ws = wb.active
headers = [c.value for c in ws[1]]
check("导出表头第 5 列为「月份」", headers[4] == "月份", str(headers[:6]))
check("导出含「上游公司」列", "上游公司" in headers)
check("导出含四项费用列", all(h in headers for h in ("渠道提成", "资金过夜费", "财务费用", "其他费用")))
check("导出不再有「费用类型」列", "费用类型" not in headers)

col = headers.index("月份") + 1
vals = [ws.cell(row=i, column=col).value for i in range(2, ws.max_row + 1)]
check("导出月份写为 YYYY-MM", all(isinstance(v, str) and len(v) == 7 for v in vals), str(vals))

up_col = headers.index("上游公司") + 1
up_vals = [ws.cell(row=i, column=up_col).value for i in range(2, ws.max_row + 1)]
check("导出带出上游公司", any(v and "沃田" in str(v) for v in up_vals), str(up_vals))

print("\n=== 10. 导出文件能再导入（闭环） ===")
tmpl = client.get("/excel/template")
check("空白模板可下载", tmpl.status_code == 200)
tw = load_workbook(io.BytesIO(tmpl.data))
th = [c.value for c in tw.active[1]]
check("空白模板表头与导出一致", th == headers, str(th[:6]))

print("\n=== 11. 旧「通道费」文本自动改名 ===")
with application.app_context():
    db = get_db()
    db.execute("UPDATE records SET fee_type='通道费' WHERE customer_name='费用测试客户'")
    db.commit()
from db import _migrate  # noqa: E402
with application.app_context():
    db = get_db()
    _migrate(db)
    db.commit()
    fixed = db.execute(
        "SELECT fee_type FROM records WHERE customer_name='费用测试客户'").fetchone()["fee_type"]
check("通道费 → 资金过夜费", fixed == "资金过夜费", str(fixed))

print("\n=== 12. 归属部门按客户档案带出（贸易计划没有部门列） ===")
from openpyxl import Workbook  # noqa: E402

with application.app_context():
    db = get_db()
    row = db.execute("SELECT id FROM departments WHERE name = '市场部'").fetchone()
    if row:
        dept_b = row["id"]
    else:
        dept_b = db.execute(
            "INSERT INTO departments (name, short_name, sort_order) VALUES ('市场部','市场',20)"
        ).lastrowid
    dept_a = db.execute(
        "SELECT id FROM departments WHERE name = '集团总部'").fetchone()["id"]
    db.execute("DELETE FROM records WHERE customer_name IN ('部门测试主体','无部门测试主体')")
    db.execute("DELETE FROM customers WHERE short_name IN ('部门测试主体','无部门测试主体')")
    db.execute("INSERT INTO customers (name, short_name, dept_id) VALUES (?,?,?)",
               ("部门测试主体", "部门测试主体", dept_b))
    db.execute("INSERT INTO customers (name, short_name) VALUES (?,?)",
               ("无部门测试主体", "无部门测试主体"))
    db.commit()

wb = Workbook()
ws = wb.active
ws.append(["大类", "品种", "上游公司", "数量", "单价", "金额", "主体公司"])
ws.append(["豆菜", "豆角", "沃田农艺园", 100, 10, 1000, "部门测试主体"])
ws.append(["豆菜", "生菜", "沃田农艺园", 100, 10, 2000, "无部门测试主体"])
buf = io.BytesIO()
wb.save(buf)
buf.seek(0)
# 故意用「集团总部」当兜底部门、且导入人也是集团总部：
# 档案挂了市场部的主体公司必须跟着档案走；档案没挂部门的一律留空待确认，
# 绝不能按导入人的部门兜底。
resp = client.post("/excel/import",
                   data={"file": (buf, "部门归属测试.xlsx"), "default_dept": str(dept_a)},
                   content_type="multipart/form-data", follow_redirects=True)
body = resp.get_data(as_text=True)

with application.app_context():
    db = get_db()
    got_b = db.execute(
        "SELECT id, dept_id, status FROM records WHERE customer_name = '部门测试主体'").fetchone()
    got_a = db.execute(
        "SELECT id, dept_id, status FROM records WHERE customer_name = '无部门测试主体'").fetchone()
check("档案有归属部门 → 按档案归属（不是导入人的部门）",
      bool(got_b) and got_b["dept_id"] == dept_b,
      f"期望 {dept_b}，实际 {got_b['dept_id'] if got_b else None}")
check("档案没归属部门 → 归属部门留空（不按导入人兜底）",
      bool(got_a) and got_a["dept_id"] is None,
      f"期望 None，实际 {got_a['dept_id'] if got_a else None}")
check("结果页说明按客户档案带出部门", "按客户档案" in body)
check("结果页提示归属部门待确认", "归属部门待确认" in body)
check("导入落库状态为报批中", bool(got_a) and got_a["status"] == "draft",
      got_a["status"] if got_a else None)
check("导入结果页用「报批中」措辞", "报批中" in body)

# 部门没确认时不允许提交进入收款
rid_pending = got_a["id"]
r = client.post(f"/records/{rid_pending}/submit", follow_redirects=True)
body = r.get_data(as_text=True)
with application.app_context():
    db = get_db()
    st = db.execute("SELECT status, dept_id FROM records WHERE id = ?",
                    (rid_pending,)).fetchone()
check("部门未确认 → 提交被拦", st["status"] == "draft", st["status"])
check("提交被拦时给出提示", "请先确认归属部门" in body)

# 批量提交也要跳过无部门的
r = client.post("/records/batch-submit", data={"ids": str(rid_pending)},
                follow_redirects=True)
body = r.get_data(as_text=True)
with application.app_context():
    db = get_db()
    st = db.execute("SELECT status FROM records WHERE id = ?", (rid_pending,)).fetchone()
check("部门未确认 → 批量提交同样被跳过", st["status"] == "draft", st["status"])
check("批量提交说明跳过原因", "归属部门未确认" in body or "还没确认归属部门" in body)

# 详情页可见「归属部门待确认」卡片
r = client.get(f"/records/{rid_pending}")
check("详情页出现确认部门入口", "归属部门待确认" in r.get_data(as_text=True))

# 确认部门 → 单据可提交，且客户档案被回填
r = client.post(f"/records/{rid_pending}/dept", data={"dept_id": str(dept_a)},
                follow_redirects=True)
body = r.get_data(as_text=True)
with application.app_context():
    db = get_db()
    rec = db.execute("SELECT dept_id, status FROM records WHERE id = ?",
                     (rid_pending,)).fetchone()
    cu = db.execute("SELECT dept_id FROM customers WHERE short_name = '无部门测试主体'").fetchone()
check("确认部门后单据带上部门", rec["dept_id"] == dept_a, str(rec["dept_id"]))
check("确认部门时回填客户档案", cu["dept_id"] == dept_a, str(cu["dept_id"]))
check("确认成功有提示", "归属部门已确认" in body)

# 确认后可以正常提交
client.post(f"/records/{rid_pending}/submit", follow_redirects=True)
with application.app_context():
    db = get_db()
    st = db.execute("SELECT status FROM records WHERE id = ?", (rid_pending,)).fetchone()
check("部门确认后可提交进入收款", st["status"] == "pending_payment", st["status"])

with application.app_context():
    db = get_db()
    created = db.execute(
        "SELECT dept_id FROM customers WHERE short_name = '部门测试主体'").fetchone()
check("已有档案的部门不被导入覆盖", created["dept_id"] == dept_b, str(created["dept_id"]))

print(f"\n{'=' * 46}")
print(f"通过 {passed} 项，失败 {failed} 项")
if failed:
    sys.exit(1)
