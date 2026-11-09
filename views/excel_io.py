"""Excel 导入与导出：与原始《业务登记模板》26 列严格对齐。"""
import io
import os
import re
from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal

from flask import (Blueprint, flash, g, redirect, render_template, request,
                   send_file, url_for)
from openpyxl import Workbook, load_workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

from auth import has_perm, login_required, perm_required, visible_scope
from db import get_db
from utils import (CUSTOMER_LABEL_SQL, CUSTOMER_SEARCH_SQL, FEE_TYPES, calc_amounts,
                   clean_company_name, customer_label, money, month_bounds, month_label,
                   month_value, now_text, parse_date, parse_month, to_cents, to_rate,
                   today)

bp = Blueprint("excel", __name__, url_prefix="/excel")

# 导出模板列序（在原始 26 列基础上：客户名称后插「上游公司」，
# 「费用类型」拆成四项费用明细，均可被导入端按表头名反向识别）
# 第 9 列对外统一叫「主体公司」（导入端同时认「客户名称」等旧写法）
TEMPLATE_HEADERS = [
    "序号", "归属部门", "类型", "预存与否", "月份", "报送计划时间", "计划出具日期",
    "是否开票", "主体公司", "上游公司",
    "服务费", "资金过夜费", "财务费用", "其他费用",
    "业务金额", "费率", "渠道提成", "公司实收",
    "辅助【核算费用，有公式】", "应收金额", "预存金额", "已收金额", "待收金额",
    "最近收款日期", "收款账户", "收款银行", "付款账户", "付款银行", "备注", "回单编号",
]

COL_WIDTHS = [7, 14, 10, 9, 12, 13, 13, 9, 24, 20,
              11, 12, 11, 11,
              14, 8, 12, 12, 20,
              12, 12, 12, 12, 12, 20, 18, 20, 18, 26, 16]

# 字段 ← 可接受的表头写法（按顺序优先匹配）。
# 业务登记模板用前者，贸易计划模板用后者，同一份文件两种叫法都能导。
FIELD_ALIASES = {
    "dept_name":          ("归属部门", "部门", "归属公司", "公司"),
    "biz_type":           ("类型", "业务类型"),
    "prepaid_mode":       ("预存与否", "预存模式"),
    "biz_date":           ("月份", "时间", "业务月份", "业务日期"),
    "report_plan_date":   ("报送计划时间",),
    "issue_plan_date":    ("计划出具日期",),
    "is_invoiced":        ("是否开票",),
    "customer_name":      ("客户名称", "主体公司", "客户", "客户名", "客户全称"),
    "upstream_company":   ("上游公司", "上游", "供应商"),
    "category":           ("大类", "品类"),
    "variety":            ("品种", "品名"),
    "fee_service_cents":  ("服务费",),
    "fee_overnight_cents": ("资金过夜费", "通道费"),
    "fee_finance_cents":  ("财务费用",),
    "fee_other_cents":    ("其他费用",),
    "fee_type":           ("费用类型",),
    "biz_amount":         ("业务金额", "金额", "业务额", "交易金额"),
    "fee_rate":           ("费率",),
    "channel_commission": ("渠道提成", "提成"),
    "payable_amount":     ("应收金额", "应付金额"),
    "prepaid_amount":     ("预存金额",),
    "received_amount":    ("已收金额", "付款金额", "已收款金额"),
    "__computed_income":  ("公司实收",),
    "__computed_aux":     ("辅助",),
    "__computed_unpaid":  ("待收金额", "未付款金额", "未收金额"),
    "paid_date":          ("最近收款日期", "打款日期", "收款日期", "付款日期"),
    "receive_account":    ("收款账户",),
    "receive_bank":       ("收款银行",),
    "pay_account":        ("付款账户",),
    "pay_bank":           ("付款银行",),
    "remark":             ("备注",),
    "receipt_no":         ("回单编号",),
}

# 只有这些字段是「有则更好」，缺了也能导
REQUIRED_FIELDS = ("customer_name", "biz_amount")

# 汇总 / 小计行的文字特征，这类行不导入
SUMMARY_WORDS = ("合计", "汇总", "小计", "总计", "总和")

# 未映射列里不值得提示的（自动编号、Excel 自动列名、约定不导入的列）
IGNORED_HEADERS = {"序号", "编号", "列1", "列2", "列3", "列4", "下游公司"}

TRUE_WORDS = {"是", "有", "真", "y", "yes", "true", "1", "√", "已开"}


def _norm_header(value):
    s = str(value if value is not None else "")
    s = s.replace("\u3000", "").strip()
    s = re.sub(r"\s+", "", s)
    s = re.sub(r"[【\[（(].*?[】\]）)]", "", s)
    return s


def _to_bool(value):
    return str(value if value is not None else "").strip().lower() in TRUE_WORDS


def _map_columns(header_cells):
    """把表头行映射成 {字段: 列序号}。同一字段有多种写法时按别名顺序优先。"""
    normalized = [_norm_header(c) for c in header_cells]
    col_map = {}
    for field, aliases in FIELD_ALIASES.items():
        for alias in aliases:
            if alias in normalized:
                col_map[field] = normalized.index(alias)
                break
    return col_map, normalized


def _formula_rows(values_ws):
    """找出含 SUBTOTAL / SUM 公式的行号（1 起），这些是汇总行，不导入。"""
    rows = set()
    for idx, row in enumerate(values_ws.iter_rows(), start=1):
        for cell in row:
            value = cell.value
            if isinstance(value, str) and value.startswith("="):
                upper = value.upper()
                if "SUBTOTAL" in upper or "SUM(" in upper:
                    rows.add(idx)
                    break
    return rows


def _looks_like_summary(*cells):
    """单元格内容是「合计 / 汇总 / 小计」等字样时，判定为汇总行。

    只做整格精确匹配：客户名里带「汇总」两个字（如「汇总结算客户」）不应被误杀。
    """
    for cell in cells:
        values = cell if isinstance(cell, (list, tuple)) else (cell,)
        for value in values:
            text = re.sub(r"\s+", "", str(value if value is not None else ""))
            if text in SUMMARY_WORDS:
                return True
    return False


def _uniq(values):
    """按原顺序去重，去掉空值。"""
    seen, out = set(), []
    for v in values:
        v = (v or "").strip() if isinstance(v, str) else v
        if not v or v in seen:
            continue
        seen.add(v)
        out.append(v)
    return out


def _first(values):
    """取第一个非空值。"""
    for v in values:
        if v:
            return v
    return ""


def _dept_name(dept_by_name, dept_id):
    for name, value in dept_by_name.items():
        if value == dept_id:
            return name
    return ""


# ---------- 导入 ----------

@bp.route("/import", methods=("GET", "POST"))
@perm_required("record.import")
def import_page():
    db = get_db()
    depts = db.execute(
        "SELECT id, name FROM departments WHERE is_active = 1 ORDER BY sort_order, id"
    ).fetchall()

    if request.method == "GET":
        return render_template("import.html", result=None, depts=depts,
                               default_dept=g.user["dept_id"] or (depts[0]["id"] if depts else None))

    file = request.files.get("file")
    if not file or not file.filename:
        flash("请选择要导入的 Excel 文件", "error")
        return redirect(url_for("excel.import_page"))
    # 中文文件名用 secure_filename 会被剥空，这里只取扩展名判断，文件本身不落盘
    ext = os.path.splitext(file.filename)[1].lower()
    if ext not in (".xlsx", ".xlsm"):
        flash("只支持 .xlsx 格式（如为 .xls 请先用 Excel 另存为 .xlsx）", "error")
        return redirect(url_for("excel.import_page"))

    raw = file.read()
    try:
        wb = load_workbook(io.BytesIO(raw), data_only=True)
        # 再读一次保留公式，用来识别汇总行（SUBTOTAL 行取到的只是结果值，看不出是合计）
        wb_formula = load_workbook(io.BytesIO(raw), data_only=False)
    except Exception as exc:  # noqa: BLE001
        flash(f"文件无法读取：{exc}", "error")
        return redirect(url_for("excel.import_page"))

    ws = wb.active
    rows = list(ws.iter_rows(values_only=True))
    if not rows:
        flash("表格是空的", "error")
        return redirect(url_for("excel.import_page"))

    # 表头定位：从上往下找一个能同时认出「客户名称」和「业务金额」的行。
    # 两者都接受别名，所以业务登记模板和贸易计划模板都能识别。
    header_row_idx = None
    col_map = {}
    for idx, row in enumerate(rows[:15]):
        candidate, _ = _map_columns(row)
        if all(f in candidate for f in REQUIRED_FIELDS):
            header_row_idx = idx
            col_map = candidate
            break
    if header_row_idx is None:
        flash("未识别到表头：文件里需要有「主体公司」或「客户名称」列，"
              "以及「业务金额」或「金额」列，表头须在第 15 行以内", "error")
        return redirect(url_for("excel.import_page"))

    # 表头里没被用到的列，导入后在结果页列出来，避免以为漏了数据
    mapped_idx = set(col_map.values())
    ignored_cols = []
    for i, cell in enumerate(rows[header_row_idx]):
        label = str(cell or "").strip()
        if not label or i in mapped_idx or _norm_header(cell) in IGNORED_HEADERS:
            continue
        ignored_cols.append(label)

    # 汇总行（SUBTOTAL/SUM）直接跳过，不当作业务数据
    formula_rows = _formula_rows(wb_formula.active)

    _dept_rows = db.execute("SELECT id, name FROM departments").fetchall()
    dept_by_name = {r["name"]: r["id"] for r in _dept_rows}
    dept_by_id = {r["id"]: r["name"] for r in _dept_rows}

    # 客户档案：主体公司 → 归属部门。贸易计划里没有部门列，
    # 这时按档案里这个主体公司挂的归属部门走，档案没挂才退回页面选的默认部门。
    cust_by_entity, cust_by_name = {}, {}
    for c in db.execute("SELECT id, name, short_name, dept_id FROM customers"):
        entity = (c["short_name"] or "").strip()
        if entity:
            cust_by_entity[entity] = c          # 主体公司优先
        name_key = (c["name"] or "").strip()
        if name_key:
            cust_by_name.setdefault(name_key, c)   # 客户名称兜底

    def _master_customer(name):
        return cust_by_entity.get(name) or cust_by_name.get(name)

    # 表格里没有「归属部门」列时（贸易计划模板就是这种），用页面上选的默认部门
    try:
        default_dept_id = int(request.form.get("default_dept") or 0) or None
    except (TypeError, ValueError):
        default_dept_id = None
    if (default_dept_id and not has_perm(g.user, "record.create_any_dept")
            and default_dept_id != g.user["dept_id"]):
        default_dept_id = g.user["dept_id"]      # 无跨部门权限者只能导到自己部门
    if not default_dept_id:
        default_dept_id = g.user["dept_id"] or (depts[0]["id"] if depts else None)

    bad_rows, warned, skipped_rows = [], [], []
    renamed = {}          # 原名 → 清理后的名称，供结果页说明
    dept_from_master = {}  # 主体公司 → 档案里的部门名（按档案归属的）
    dept_fallback = {}     # 主体公司 → 行号（档案没挂部门，用了默认部门）
    # 同一「主体公司 + 归属部门 + 月份」的明细行先归堆，最后合成一条业务
    buckets, bucket_order = {}, []

    for offset, row in enumerate(rows[header_row_idx + 1:], start=header_row_idx + 2):
        if row is None or all(c in (None, "") for c in row):
            continue
        if offset in formula_rows:
            skipped_rows.append({"row": offset, "reason": "含合计/汇总公式，不作为业务导入"})
            continue

        def cell(key):
            idx = col_map.get(key)
            return row[idx] if idx is not None and idx < len(row) else None

        raw_customer = cell("customer_name")
        if isinstance(raw_customer, (int, float)):
            skipped_rows.append({"row": offset, "reason": "主体公司是数字，判定为表尾备注行"})
            continue

        raw_name_text = str(raw_customer or "").strip()
        customer_name = clean_company_name(raw_customer)
        if raw_name_text and raw_name_text != customer_name:
            renamed[raw_name_text] = customer_name
        if (_looks_like_summary(row[:3], raw_customer)
                or customer_name in ("", "合计", "小计", "总计")):
            skipped_rows.append({"row": offset, "reason": "合计行或主体公司为空"})
            continue
        if len(customer_name) > 120:
            customer_name = customer_name[:120]

        biz_amount = to_cents(cell("biz_amount"))
        if not biz_amount or biz_amount <= 0:
            bad_rows.append({"row": offset, "customer": customer_name,
                             "reason": "业务金额为空或不大于 0"})
            continue

        fee_rate = to_rate(cell("fee_rate"))
        if fee_rate is None:
            bad_rows.append({"row": offset, "customer": customer_name, "reason": "费率格式不正确"})
            continue

        commission = to_cents(cell("channel_commission")) or 0
        raw_payable = cell("payable_amount")
        payable = to_cents(raw_payable) or 0
        prepaid = to_cents(cell("prepaid_amount")) or 0
        received = to_cents(cell("received_amount")) or 0

        # 部门优先级：表格里写了 → 按表格；没写 → 按客户档案里该主体公司的归属部门；
        # 档案也没挂 → 才用页面选的默认部门（即导入人的部门），并在结果页说明。
        dept_name = str(cell("dept_name") or "").strip()
        master = _master_customer(customer_name)
        if dept_name:
            if has_perm(g.user, "record.create_any_dept"):
                dept_id = dept_by_name.get(dept_name)
                if not dept_id:
                    bad_rows.append({"row": offset, "customer": customer_name,
                                     "reason": f"部门「{dept_name}」不存在"})
                    continue
            else:
                dept_id = g.user["dept_id"]
        else:
            master_dept_id = master["dept_id"] if master else None
            if master_dept_id:
                dept_id = master_dept_id
                dept_from_master[customer_name] = dept_by_id.get(master_dept_id, "—")
            else:
                dept_id = default_dept_id
                dept_fallback.setdefault(customer_name, offset)
        if not dept_id:
            bad_rows.append({"row": offset, "customer": customer_name,
                             "reason": "没有可用的归属部门：请在页面上选一个默认部门，"
                                       "或让管理员给你的账号分配部门"})
            continue

        prepaid_mode = str(cell("prepaid_mode") or "").strip() or "后付"
        if prepaid_mode not in ("预存", "后付", "月结"):
            prepaid_mode = "后付"

        # 时间按月份登记：接受「2026-11」「2026年11月」「2026-11-05」，统一存当月 1 日
        biz_date = parse_month(cell("biz_date")) or parse_month(today()) or today()

        fees = {}
        for _field, _label, col in FEE_TYPES:
            fees[col] = to_cents(cell(col)) or 0

        raw_upstream = str(cell("upstream_company") or "").strip()
        upstream = clean_company_name(raw_upstream)
        if raw_upstream and raw_upstream != upstream:
            renamed[raw_upstream] = upstream

        item = {
            "dept_id": dept_id,
            "biz_type": str(cell("biz_type") or "").strip(),
            "prepaid_mode": prepaid_mode,
            "biz_date": biz_date,
            "report_plan_date": parse_date(cell("report_plan_date")),
            "issue_plan_date": parse_date(cell("issue_plan_date")),
            "is_invoiced": 1 if _to_bool(cell("is_invoiced")) else 0,
            "customer_name": customer_name,
            "upstream_company": upstream,
            "category": str(cell("category") or "").strip(),
            "variety": str(cell("variety") or "").strip(),
            "fees": fees,
            "biz_amount_cents": biz_amount,
            "fee_rate": fee_rate,
            "channel_commission_cents": commission,
            "payable_cents": payable,
            "payable_given": raw_payable not in (None, ""),
            "prepaid_cents": prepaid,
            "received_cents": received,
            "paid_date": parse_date(cell("paid_date")),
            "receive_account": str(cell("receive_account") or "").strip(),
            "receive_bank": str(cell("receive_bank") or "").strip(),
            "pay_account": str(cell("pay_account") or "").strip(),
            "pay_bank": str(cell("pay_bank") or "").strip(),
            "remark": str(cell("remark") or "").strip(),
            "receipt_no": str(cell("receipt_no") or "").strip(),
            "excel_income": to_cents(cell("__computed_income")),
        }

        key = (customer_name, dept_id, biz_date)
        if key not in buckets:
            buckets[key] = {"row": offset, "items": []}
            bucket_order.append(key)
        buckets[key]["items"].append(item)

    # ---------- 汇总：一个主体公司 = 一条业务 ----------
    ok_rows, groups_preview = [], []
    for key in bucket_order:
        bucket = buckets[key]
        items = bucket["items"]
        customer_name, dept_id, biz_date = key

        total_amount = sum(i["biz_amount_cents"] for i in items)
        commission = sum(i["channel_commission_cents"] for i in items)
        prepaid = sum(i["prepaid_cents"] for i in items)
        received = sum(i["received_cents"] for i in items)
        given_payable = [i["payable_cents"] for i in items if i["payable_given"]]
        payable = sum(given_payable) if given_payable else 0
        fees = {col: sum(i["fees"][col] for i in items) for _f, _l, col in FEE_TYPES}

        # 费率：一致就沿用；不一致按金额加权平均，避免汇总后金额口径漂移
        rates = sorted({round(float(i["fee_rate"]), 6) for i in items})
        fee_rate = rates[0]
        if len(rates) > 1:
            weighted = (
                sum(Decimal(str(i["fee_rate"])) * Decimal(i["biz_amount_cents"]) for i in items)
                / Decimal(total_amount)
            ).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)
            fee_rate = float(weighted)
            warned.append({
                "row": bucket["row"], "customer": customer_name,
                "reason": "明细费率不一致（" + "、".join(str(r) for r in rates)
                          + f"%），已按金额加权平均 {fee_rate}% 计算",
            })

        amounts = calc_amounts(total_amount, fee_rate, commission, payable, prepaid, received)
        # 应收金额全空时默认取辅助核算费用
        if not given_payable:
            payable = amounts["aux_fee_cents"]
            amounts = calc_amounts(total_amount, fee_rate, commission, payable, prepaid, received)

        # 口径核对：Excel 里的「公司实收」与系统算的是否一致
        excel_incomes = [i["excel_income"] for i in items if i["excel_income"] not in (None, 0)]
        if len(excel_incomes) == len(items):
            excel_income = sum(excel_incomes)
            if abs(excel_income - amounts["company_income_cents"]) > 1:
                warned.append({
                    "row": bucket["row"], "customer": customer_name,
                    "reason": f"Excel 公司实收 {money(excel_income)}，"
                              f"系统按口径算 {money(amounts['company_income_cents'])}",
                })

        upstream = "、".join(_uniq([i["upstream_company"] for i in items
                                    if i["upstream_company"]]))[:200]

        # 备注：合并多行时留一行可追溯的来源摘要
        remark_parts = _uniq([i["remark"] for i in items if i["remark"]])
        remark = "；".join(remark_parts)
        if len(items) > 1:
            detail = "、".join(
                (("/".join(x for x in (i["category"], i["variety"]) if x)) or "明细")
                + " " + money(i["biz_amount_cents"])
                for i in items
            )
            remark = f"【导入汇总 {len(items)} 行】{detail}" + ("；" + remark if remark else "")
        remark = remark[:300]

        report_dates = [i["report_plan_date"] for i in items if i["report_plan_date"]]
        issue_dates = [i["issue_plan_date"] for i in items if i["issue_plan_date"]]
        paid_dates = [i["paid_date"] for i in items if i["paid_date"]]

        ok_rows.append({
            "dept_id": dept_id,
            "biz_type": _first([i["biz_type"] for i in items]),
            "prepaid_mode": _first([i["prepaid_mode"] for i in items]) or "后付",
            "biz_date": biz_date,
            "report_plan_date": min(report_dates) if report_dates else None,
            "issue_plan_date": min(issue_dates) if issue_dates else None,
            "is_invoiced": 1 if any(i["is_invoiced"] for i in items) else 0,
            "customer_name": customer_name,
            "upstream_company": upstream,
            "biz_amount_cents": total_amount,
            "fee_rate": fee_rate,
            "channel_commission_cents": commission,
            "payable_amount_cents": payable,
            "prepaid_amount_cents": prepaid,
            "received_amount_cents": received,
            "paid_date": min(paid_dates) if paid_dates else None,
            "receive_account": _first([i["receive_account"] for i in items]),
            "receive_bank": _first([i["receive_bank"] for i in items]),
            "pay_account": _first([i["pay_account"] for i in items]),
            "pay_bank": _first([i["pay_bank"] for i in items]),
            "remark": remark,
            "receipt_no": _first([i["receipt_no"] for i in items]),
            **fees,
            **amounts,
        })
        groups_preview.append({
            "customer": customer_name,
            "dept": _dept_name(dept_by_name, dept_id),
            "month": month_label(biz_date),
            "rows": len(items),
            "amount": total_amount,
            "upstream": upstream,
        })

    # 主体公司 → 客户档案（导入时自动建档，避免客户模块卡住批量导入）
    # 匹配顺序：客户档案「主体公司」优先，再退到「客户名称」，
    # 因为业务模块对外一律用主体公司，档案的主体公司没填时才用全称。
    if ok_rows:
        for item in ok_rows:
            name = item["customer_name"]
            row = db.execute(
                "SELECT id, name, short_name FROM customers "
                "WHERE TRIM(COALESCE(short_name,'')) = ? OR name = ? "
                "ORDER BY CASE WHEN TRIM(COALESCE(short_name,'')) = ? THEN 0 ELSE 1 END",
                (name, name, name),
            ).fetchone()
            if row:
                item["customer_id"] = row["id"]
                item["customer_name"] = customer_label(row, name)
            else:
                # 新客户：客户名称与主体公司同时写入，并挂上这次导入的部门，
                # 下次再导同一个主体公司就能自动带出归属部门
                cur = db.execute(
                    "INSERT INTO customers (name, short_name, dept_id, created_by, created_at, updated_at) "
                    "VALUES (?,?,?,?,?,?)",
                    (name, name, default_dept_id, g.user["name"], now_text(), now_text()),
                )
                item["customer_id"] = cur.lastrowid

    # 写入
    inserted = 0
    if ok_rows:
        cols = list(ok_rows[0].keys())
        sql = (f"INSERT INTO records ({', '.join(cols)}, owner_id, owner_name, status, created_at, updated_at) "
               f"VALUES ({', '.join('?' * len(cols))}, ?, ?, 'draft', ?, ?)")
        for item in ok_rows:
            cur = db.execute(sql, list(item.values()) + [g.user["id"], g.user["name"], now_text(), now_text()])
            db.execute(
                """INSERT INTO record_logs (record_id, action, from_status, to_status,
                                            actor_id, actor_name, note, created_at)
                   VALUES (?, 'import', NULL, 'draft', ?, ?, ?, ?)""",
                (cur.lastrowid, g.user["id"], g.user["name"], "Excel 导入", now_text()),
            )
            inserted += 1
        db.commit()

    notes = []
    merged = sum(g["rows"] for g in groups_preview)
    if groups_preview:
        notes.append(
            f"按主体公司汇总：{merged} 行明细合并为 {len(groups_preview)} 条业务"
            "（同一主体公司 + 归属部门 + 月份算一条）"
        )
    if "fee_rate" not in col_map:
        notes.append("文件里没有「费率」列，公司实收按 0 计算，导入后可在详情页逐条补录")
    if "dept_name" not in col_map:
        if dept_from_master:
            pairs = "、".join(f"{k}→{v}" for k, v in list(dept_from_master.items())[:8])
            more = f" 等 {len(dept_from_master)} 家" if len(dept_from_master) > 8 else ""
            notes.append(f"归属部门按客户档案里该主体公司的归属自动带出：{pairs}{more}")
        if dept_fallback:
            names = "、".join(list(dept_fallback.keys())[:8])
            more = f" 等 {len(dept_fallback)} 家" if len(dept_fallback) > 8 else ""
            notes.append(f"以下主体公司在客户档案里没有归属部门，已按你选的默认部门归入："
                         f"{names}{more}。建议先去「客户档案」补上部门，下次导入就自动带出")
    if "biz_date" not in col_map:
        notes.append("文件里没有「月份」或「时间」列，记录落为当前月份")
    if "upstream_company" not in col_map:
        notes.append("文件里没有「上游公司」列，上游公司留空，可导入后补录")
    if "fee_type" in col_map:
        notes.append("原「费用类型」列已不再使用（改为四项金额分列填列），该列文字未导入")
    if renamed:
        pairs = "、".join(f"{a}→{b}" for a, b in list(renamed.items())[:8])
        more = f" 等 {len(renamed)} 个" if len(renamed) > 8 else ""
        notes.append(f"已清理名称末尾的括号备注与多余符号：{pairs}{more}")
    if any(_norm_header(c) == "下游公司" for c in rows[header_row_idx]):
        notes.append("「下游公司」列按约定不导入")
    if ignored_cols:
        notes.append("以下列未导入：" + "、".join(ignored_cols))

    result = {
        "inserted": inserted,
        "bad": bad_rows,
        "warned": warned,
        "skipped_rows": skipped_rows,
        "ignored_cols": ignored_cols,
        "groups": groups_preview,
        "notes": notes,
        "header_row": header_row_idx + 1,
        "warned_rule": "Excel 里「公司实收」与系统计算结果不一致",
    }
    return render_template("import.html", result=result, depts=depts,
                           default_dept=default_dept_id)


# ---------- 导出 ----------

def _filtered_rows():
    db = get_db()
    scope, value = visible_scope(g.user)
    if scope == "all":
        clause, params = "1=1", []
    elif scope == "dept":
        clause, params = "(r.dept_id = ? OR r.owner_id = ?)", [value, g.user["id"]]
    else:
        clause, params = "r.owner_id = ?", [g.user["id"]]

    sql = f"""SELECT r.*, d.name AS dept_name, {CUSTOMER_LABEL_SQL}
                FROM records r
                LEFT JOIN departments d ON d.id = r.dept_id
                LEFT JOIN customers cu ON cu.id = r.customer_id
               WHERE {clause}"""
    status = request.args.getlist("status")
    if status:
        sql += f" AND r.status IN ({','.join('?' * len(status))})"
        params.extend(status)
    dept = request.args.get("dept")
    if dept:
        sql += " AND r.dept_id = ?"
        params.append(int(dept))
    m_from, _ = month_bounds(request.args.get("from", ""))
    _, m_to = month_bounds(request.args.get("to", ""))
    if m_from:
        sql += " AND r.biz_date >= ?"
        params.append(m_from)
    if m_to:
        sql += " AND r.biz_date <= ?"
        params.append(m_to)
    kw = (request.args.get("kw") or "").strip()
    if kw:
        sql += (f" AND ({CUSTOMER_SEARCH_SQL} OR r.upstream_company LIKE ?"
                " OR r.receipt_no LIKE ? OR r.remark LIKE ?)")
        params.extend([f"%{kw}%"] * 4)
    sql += " ORDER BY r.id"
    return db.execute(sql, params).fetchall()


@bp.route("/export")
@perm_required("record.export")
def export():
    rows = _filtered_rows()
    with_extra = request.args.get("extra") == "1"

    wb = Workbook()
    ws = wb.active
    ws.title = "业务登记"

    headers = list(TEMPLATE_HEADERS)
    if with_extra:
        headers += ["系统状态", "登记人", "登记时间", "最后更新时间"]

    ws.append(headers)

    head_font = Font(bold=True, color="FFFFFF", size=10)
    head_fill = PatternFill("solid", fgColor="1E40AF")
    thin = Side(style="thin", color="D1D5DB")
    border = Border(left=thin, right=thin, top=thin, bottom=thin)
    for col in range(1, len(headers) + 1):
        c = ws.cell(row=1, column=col)
        c.font = head_font
        c.fill = head_fill
        c.alignment = Alignment(horizontal="center", vertical="center")
        c.border = border
        ws.column_dimensions[get_column_letter(col)].width = (
            COL_WIDTHS[col - 1] if col <= len(COL_WIDTHS) else 14
        )

    from utils import status_label

    for r in rows:
        line = [
            r["id"], r["dept_name"] or "", r["biz_type"] or "", r["prepaid_mode"] or "",
            month_value(r["biz_date"]), r["report_plan_date"] or "",
            r["issue_plan_date"] or "",
            "是" if r["is_invoiced"] else "否", r["customer_label"] or r["customer_name"],
            r["upstream_company"] or "",
            r["fee_service_cents"] / 100, r["fee_overnight_cents"] / 100,
            r["fee_finance_cents"] / 100, r["fee_other_cents"] / 100,
            r["biz_amount_cents"] / 100, r["fee_rate"], r["channel_commission_cents"] / 100,
            r["company_income_cents"] / 100, r["aux_fee_cents"] / 100,
            r["payable_amount_cents"] / 100, r["prepaid_amount_cents"] / 100,
            r["received_amount_cents"] / 100, r["unpaid_amount_cents"] / 100,
            r["paid_date"] or "", r["receive_account"] or "", r["receive_bank"] or "",
            r["pay_account"] or "", r["pay_bank"] or "", r["remark"] or "",
            r["receipt_no"] or "",
        ]
        if with_extra:
            line += [status_label(r["status"]), r["owner_name"] or "",
                     r["created_at"] or "", r["updated_at"] or ""]
        ws.append(line)

    # 金额列两位小数 + 冻结首行 + 筛选
    money_cols = [11, 12, 13, 14, 15, 17, 18, 19, 20, 21, 22, 23]
    for row_idx in range(2, ws.max_row + 1):
        for col_idx in money_cols:
            c = ws.cell(row=row_idx, column=col_idx)
            if isinstance(c.value, (int, float)):
                c.number_format = "#,##0.00"
    ws.freeze_panes = "A2"
    ws.auto_filter.ref = f"A1:{get_column_letter(len(headers))}{ws.max_row}"

    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    filename = f"业务登记_{datetime.now().strftime('%Y%m%d_%H%M')}.xlsx"
    return send_file(
        buf,
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        as_attachment=True,
        download_name=filename,
    )


@bp.route("/template")
@login_required
def template():
    """下载空白模板（与原始 26 列一致），供分公司线下填写后导入。"""
    wb = Workbook()
    ws = wb.active
    ws.title = "Sheet1"
    ws.append(TEMPLATE_HEADERS)
    head_font = Font(bold=True, size=10)
    for col in range(1, len(TEMPLATE_HEADERS) + 1):
        ws.cell(row=1, column=col).font = head_font
        ws.column_dimensions[get_column_letter(col)].width = COL_WIDTHS[col - 1]
    ws.freeze_panes = "A2"

    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return send_file(
        buf,
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        as_attachment=True,
        download_name="业务登记模板.xlsx",
    )
