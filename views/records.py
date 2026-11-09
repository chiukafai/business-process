"""业务登记：列表、登记/编辑、详情、状态流转、回单。"""
import os

from flask import (Blueprint, abort, current_app, flash, g, redirect,
                   render_template, request, url_for)

from auth import (can_delete_record, can_receive_record, can_register_fee,
                  can_view_record, has_perm, login_required, perm_required,
                  visible_scope)
from db import get_db
from notifier import notify_next
from workflow import can_invoice as can_register_invoice
from utils import (CUSTOMER_LABEL_SQL, CUSTOMER_SEARCH_SQL, FEE_TYPES,
                   calc_amounts, clean_company_name, customer_label,
                   fee_total_cents, month_bounds, month_value, needs_invoice,
                   now_text, parse_date, parse_month, receipt_progress,
                   status_label, to_cents, to_rate, to_yuan_text, today,
                   trade_fee_cents)

bp = Blueprint("records", __name__, url_prefix="/records")

PAGE_SIZE = 20


# ---------- 辅助 ----------

def _log(db, record_id, action, from_status, to_status, note=None):
    db.execute(
        """INSERT INTO record_logs (record_id, action, from_status, to_status,
                                    actor_id, actor_name, note, created_at)
           VALUES (?,?,?,?,?,?,?,?)""",
        (record_id, action, from_status, to_status,
         g.user["id"], g.user["name"], note, now_text()),
    )


def _scope_clause(user):
    """列表可见范围 SQL 片段（与详情页 can_view_record 保持同一口径）。"""
    scope, value = visible_scope(user)
    if scope == "all":
        return "1=1", []
    if scope == "dept":
        return "(r.dept_id = ? OR r.owner_id = ?)", [value, user["id"]]
    return "r.owner_id = ?", [user["id"]]


def get_record_or_404(record_id):
    rec = get_db().execute(
        f"""SELECT r.*, d.name AS dept_name, {CUSTOMER_LABEL_SQL}
             FROM records r LEFT JOIN departments d ON d.id = r.dept_id
             LEFT JOIN customers cu ON cu.id = r.customer_id
            WHERE r.id = ?""",
        (record_id,),
    ).fetchone()
    if rec is None:
        abort(404)
    if not can_view_record(g.user, rec):
        abort(403)
    return rec


def _form_values(rec):
    """把数据库行转成表单回填字典（金额由分转元）。"""
    d = dict(rec)
    # 表单里的「主体公司」按档案口径回填，避免历史快照存的是旧名字
    if "customer_label" in rec.keys() and rec["customer_label"]:
        d["customer_name"] = rec["customer_label"]
    yuan_fields = ["biz_amount_cents", "channel_commission_cents",
                   "payable_amount_cents", "prepaid_amount_cents",
                   "received_amount_cents"]
    yuan_fields += [f[2] for f in FEE_TYPES]
    for key in yuan_fields:
        d[key.replace("_cents", "")] = to_yuan_text(d.get(key))
    return d


def _dept_options(user):
    db = get_db()
    if has_perm(user, "record.create_any_dept"):
        return db.execute(
            "SELECT id, name FROM departments WHERE is_active = 1 ORDER BY sort_order, id"
        ).fetchall()
    if user["dept_id"]:
        return db.execute(
            "SELECT id, name FROM departments WHERE id = ?", (user["dept_id"],)
        ).fetchall()
    return []


def _read_month(form):
    """把表单里的年+月统一成当月首日 'YYYY-MM-01'。

    表单是「年 + 月」两个下拉，也兼容直接传 biz_month（2026-11）或 biz_date。
    """
    year = (form.get("biz_year") or "").strip()
    month = (form.get("biz_month_no") or "").strip()
    if year and month:
        try:
            y, m = int(year), int(month)
        except (TypeError, ValueError):
            return None
        if 1900 <= y <= 2200 and 1 <= m <= 12:
            return f"{y:04d}-{m:02d}-01"
        return None
    raw = (form.get("biz_month") or "").strip()
    if raw:
        return parse_month(raw)
    return parse_date(form.get("biz_date"))


def _read_form(form, db=None, rec=None):
    """读取表单并校验。返回 (data, errors)。

    金额口径：贸易费 = 业务金额 × 费率（公式锁定，不由表单填）；
    留存利润来自贸易计划的 K 列合计，允许手工修正；
    收款金额不在登记表单里改，统一走「收款登记」，所以这里只读已收原值。
    """
    errors = []

    # 名称清理：清掉末尾的括号备注（如「粤丰果业（无票）」→「粤丰果业」）
    customer_name = clean_company_name(form.get("customer_name"))
    customer_id = None
    raw_cid = (form.get("customer_id") or "").strip()
    if raw_cid:
        try:
            customer_id = int(raw_cid)
        except ValueError:
            customer_id = None

    if db is not None:
        if customer_id:
            row = db.execute(
                "SELECT id, name, short_name FROM customers WHERE id = ?", (customer_id,)
            ).fetchone()
            if row:
                # 业务口径存「主体公司」：档案主体公司优先，没填回退客户名称
                customer_name = customer_label(row, customer_name)
            else:
                errors.append("所选客户不存在，请重新选择")
                customer_id = None
        elif customer_name:
            # 输入的名字可能是主体公司，也可能是客户名称，两种都认
            row = db.execute(
                "SELECT id, name, short_name FROM customers "
                "WHERE TRIM(COALESCE(short_name,'')) = ? OR name = ? "
                "ORDER BY CASE WHEN TRIM(COALESCE(short_name,'')) = ? THEN 0 ELSE 1 END",
                (customer_name, customer_name, customer_name),
            ).fetchone()
            if row:
                customer_id = row["id"]
                customer_name = customer_label(row, customer_name)
            elif has_perm(g.user, "record.create"):
                # 首次出现的名字自动建档，避免业务员被客户模块卡住
                cur = db.execute(
                    "INSERT INTO customers (name, created_by, created_at, updated_at) "
                    "VALUES (?,?,?,?)",
                    (customer_name, g.user["name"], now_text(), now_text()),
                )
                customer_id = cur.lastrowid

    if not customer_name:
        errors.append("主体公司必填")

    biz_amount_cents = to_cents(form.get("biz_amount"))
    if biz_amount_cents is None:
        errors.append("业务金额格式不正确")
        biz_amount_cents = 0
    elif biz_amount_cents <= 0:
        errors.append("业务金额必须大于 0")

    fee_rate = to_rate(form.get("fee_rate"))
    if fee_rate is None or fee_rate < 0 or fee_rate > 100:
        errors.append("费率需在 0 ~ 100 之间（填 1.5 表示 1.5%）")
        fee_rate = 0.0

    # 留存利润：导入贸易计划时取 K 列（利润1）合计，也可手工修正
    retention_cents = to_cents(form.get("channel_commission"))
    if retention_cents is None:
        errors.append("留存利润格式不正确")
        retention_cents = 0

    # 贸易费：默认 = 业务金额 × 费率，留空即按公式；填了就以填的为准（四舍五入差异可手改）
    raw_trade_fee = (form.get("payable_amount") or "").strip()
    if raw_trade_fee == "":
        override_trade_fee = None
    else:
        override_trade_fee = to_cents(raw_trade_fee)
        if override_trade_fee is None:
            errors.append("贸易费格式不正确")
            override_trade_fee = None
        elif override_trade_fee < 0:
            errors.append("贸易费不能为负数")
            override_trade_fee = None

    prepaid_amount_cents = to_cents(form.get("prepaid_amount"))
    if prepaid_amount_cents is None:
        errors.append("预存金额格式不正确")
        prepaid_amount_cents = 0

    received_amount_cents = int(rec["received_amount_cents"] or 0) if rec else 0

    # 是否开票：登记时必须明确选「要 / 不要」
    raw_invoice = (form.get("invoice_needed") or "").strip()
    prev_needed = rec["invoice_needed"] if rec else None
    if raw_invoice in ("0", "1"):
        invoice_needed = int(raw_invoice)
    else:
        invoice_needed = prev_needed
        errors.append("请选择是否需要开票（要 / 不要）")
    # 选「不要」时把已开票标记清掉，避免留下无意义的已开票状态
    if invoice_needed == 0:
        is_invoiced = 0
    else:
        is_invoiced = int(rec["is_invoiced"] or 0) if rec else 0

    # 费用明细：四项分列填列（不再选费用类型）
    fee_values = {}
    for field, label, col in FEE_TYPES:
        cents = to_cents(form.get(field))
        if cents is None:
            errors.append(f"{label}格式不正确")
            cents = 0
        fee_values[col] = cents

    biz_date = _read_month(form) or today()

    prepaid_mode = form.get("prepaid_mode") or "后付"
    if prepaid_mode not in ("预存", "后付", "月结"):
        prepaid_mode = "后付"

    try:
        dept_id = int(form.get("dept_id") or 0)
    except ValueError:
        dept_id = 0
    if not dept_id:
        errors.append("归属部门必填")

    amounts = calc_amounts(
        biz_amount_cents, fee_rate, retention_cents,
        prepaid_amount_cents, received_amount_cents,
        override_trade_fee=override_trade_fee,
    )

    data = {
        "dept_id": dept_id,
        "biz_type": (form.get("biz_type") or "").strip(),
        "prepaid_mode": prepaid_mode,
        "biz_date": biz_date,
        "report_plan_date": parse_date(form.get("report_plan_date")),
        "issue_plan_date": parse_date(form.get("issue_plan_date")),
        # 「是否需要开票」登记时由业务员选；「是否已开票」收齐后由财务登记
        "invoice_needed": invoice_needed,
        "is_invoiced": is_invoiced,
        "customer_id": customer_id,
        "customer_name": customer_name,
        "upstream_company": clean_company_name(form.get("upstream_company")),
        "biz_amount_cents": biz_amount_cents,
        "fee_rate": fee_rate,
        "channel_commission_cents": retention_cents,
        "prepaid_amount_cents": prepaid_amount_cents,
        "paid_date": rec["paid_date"] if rec else None,
        "receive_account": (form.get("receive_account") or "").strip(),
        "receive_bank": (form.get("receive_bank") or "").strip(),
        "pay_account": (form.get("pay_account") or "").strip(),
        "pay_bank": (form.get("pay_bank") or "").strip(),
        "remark": (form.get("remark") or "").strip(),
        "receipt_no": rec["receipt_no"] if rec else "",
    }
    data.update(fee_values)
    data.update(amounts)
    return data, errors


def _guard_dept(user, dept_id):
    """无跨部门权限者只能选本部门。"""
    if has_perm(user, "record.create_any_dept"):
        return True
    return user["dept_id"] == dept_id


def _customer_options():
    """登记表单的客户下拉：按「主体公司」排序，前端展示主体公司，全称作备注。"""
    return get_db().execute(
        "SELECT id, name, short_name, "
        "       COALESCE(NULLIF(TRIM(short_name), ''), name) AS entity_name "
        "  FROM customers WHERE is_active = 1 "
        " ORDER BY entity_name COLLATE NOCASE"
    ).fetchall()


# ---------- 列表 ----------

@bp.route("/")
@login_required
def index():
    db = get_db()
    page = max(1, int(request.args.get("page", 1) or 1))
    f_status = request.args.getlist("status")
    f_dept = request.args.get("dept", "")
    f_from = month_value(request.args.get("from", ""))
    f_to = month_value(request.args.get("to", ""))
    kw = (request.args.get("kw") or "").strip()

    where, params = _scope_clause(g.user)
    clauses = [where]

    if f_status:
        clauses.append(f"r.status IN ({','.join('?' * len(f_status))})")
        params.extend(f_status)
    if f_dept:
        clauses.append("r.dept_id = ?")
        params.append(int(f_dept))
    m_from, _ = month_bounds(f_from)
    if m_from:
        clauses.append("r.biz_date >= ?")
        params.append(m_from)
    _, m_to = month_bounds(f_to)
    if m_to:
        clauses.append("r.biz_date <= ?")
        params.append(m_to)
    if kw:
        # 主体公司按「档案主体公司 → 档案客户名称 → 历史快照」三档匹配
        clauses.append(
            f"({CUSTOMER_SEARCH_SQL} OR r.upstream_company LIKE ? OR r.receipt_no LIKE ? "
            "OR r.remark LIKE ? OR r.biz_type LIKE ?)"
        )
        params.extend([f"%{kw}%"] * 5)

    where_sql = " AND ".join(clauses)
    from_sql = ("FROM records r "
                "LEFT JOIN departments d ON d.id = r.dept_id "
                "LEFT JOIN customers cu ON cu.id = r.customer_id")

    total = db.execute(f"SELECT COUNT(*) c {from_sql} WHERE {where_sql}", params).fetchone()["c"]
    pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
    page = min(page, pages)

    rows = db.execute(
        f"""SELECT r.*, d.name AS dept_name, {CUSTOMER_LABEL_SQL}
             {from_sql}
             WHERE {where_sql}
             ORDER BY r.updated_at DESC, r.id DESC
             LIMIT ? OFFSET ?""",
        params + [PAGE_SIZE, (page - 1) * PAGE_SIZE],
    ).fetchall()

    # 汇总（当前筛选结果）
    summary = db.execute(
        f"""SELECT COUNT(*) c,
                   COALESCE(SUM(r.biz_amount_cents),0)      biz,
                   COALESCE(SUM(r.company_income_cents),0)  income,
                   COALESCE(SUM(r.unpaid_amount_cents),0)   unpaid
              {from_sql} WHERE {where_sql}""",
        params,
    ).fetchone()

    depts = db.execute(
        "SELECT id, name FROM departments WHERE is_active = 1 ORDER BY sort_order, id"
    ).fetchall()

    return render_template(
        "records.html",
        rows=rows, total=total, page=page, pages=pages, summary=summary,
        depts=depts, f_status=f_status, f_dept=f_dept,
        f_from=f_from, f_to=f_to, kw=kw,
    )


# ---------- 新建 / 编辑 ----------

@bp.route("/new", methods=("GET", "POST"))
@perm_required("record.create")
def new():
    depts = _dept_options(g.user)
    if not depts:
        flash("你还没有归属部门，请联系管理员分配", "error")
        return redirect(url_for("dashboard.index"))

    if request.method == "POST":
        data, errors = _read_form(request.form, get_db())
        if not _guard_dept(g.user, data["dept_id"]):
            errors.append("没有跨部门登记的权限")
        if errors:
            for e in errors:
                flash(e, "error")
            return render_template("record_form.html", rec=None, form=request.form,
                                   depts=depts, customers=_customer_options(), is_new=True)

        db = get_db()
        cols = ", ".join(data.keys())
        marks = ", ".join("?" * len(data))
        cur = db.execute(
            f"INSERT INTO records ({cols}, owner_id, owner_name, status, created_at, updated_at) "
            f"VALUES ({marks}, ?, ?, 'draft', ?, ?)",
            list(data.values()) + [g.user["id"], g.user["name"], now_text(), now_text()],
        )
        record_id = cur.lastrowid
        _log(db, record_id, "create", None, "draft", "新建登记")

        if request.form.get("action") == "submit":
            db.execute(
                "UPDATE records SET status='pending_payment', submitted_at=?, updated_at=? WHERE id=?",
                (now_text(), now_text(), record_id),
            )
            _log(db, record_id, "submit", "draft", "pending_payment",
                 "提交，进入待收款，提醒资管确认收款")
            notify_next(db, record_id, "submit")

        db.commit()
        if request.form.get("action") == "submit":
            flash("已提交，进入待收款：等待资管确认客户款项", "ok")
        else:
            flash("登记已保存", "ok")
        return redirect(url_for("records.detail", record_id=record_id))

    return render_template("record_form.html", rec=None, form={}, depts=depts,
                           customers=_customer_options(), is_new=True)


@bp.route("/<int:record_id>/edit", methods=("GET", "POST"))
@login_required
def edit(record_id):
    rec = get_record_or_404(record_id)
    if rec["owner_id"] != g.user["id"]:
        abort(403)
    if rec["status"] not in ("draft", "rejected"):
        flash("只有在报批中或被驳回的业务可以编辑", "error")
        return redirect(url_for("records.detail", record_id=record_id))

    depts = _dept_options(g.user)

    if request.method == "POST":
        data, errors = _read_form(request.form, get_db(), rec=rec)
        if not _guard_dept(g.user, data["dept_id"]):
            errors.append("没有跨部门登记的权限")
        # 归属部门一经确认就锁定；原本留空的（待确认）允许这次补上
        if rec["dept_id"] and data["dept_id"] != rec["dept_id"]:
            errors.append("不允许修改归属部门")
        if errors:
            for e in errors:
                flash(e, "error")
            return render_template("record_form.html", rec=rec, form=request.form,
                                   depts=depts, customers=_customer_options(), is_new=False)

        # 预存金额属于财务口径，没有收款权限的人不能改
        if not has_perm(g.user, "record.pay"):
            data["prepaid_amount_cents"] = rec["prepaid_amount_cents"]

        sets = ", ".join(f"{k} = ?" for k in data.keys())
        db = get_db()
        db.execute(
            f"UPDATE records SET {sets}, updated_at = ? WHERE id = ?",
            list(data.values()) + [now_text(), record_id],
        )
        _log(db, record_id, "update", rec["status"], rec["status"], "修改登记内容")

        if request.form.get("action") == "submit":
            db.execute(
                "UPDATE records SET status='pending_payment', submitted_at=?, reject_reason=NULL, updated_at=? WHERE id=?",
                (now_text(), now_text(), record_id),
            )
            _log(db, record_id, "submit", rec["status"], "pending_payment",
                 "重新提交，进入待收款，提醒资管确认收款")
            notify_next(db, record_id, "submit")

        db.commit()
        flash("已提交，进入待收款" if request.form.get("action") == "submit" else "已保存", "ok")
        return redirect(url_for("records.detail", record_id=record_id))

    return render_template("record_form.html", rec=rec, form=_form_values(rec),
                           depts=depts, customers=_customer_options(), is_new=False)


# ---------- 详情 ----------

@bp.route("/<int:record_id>")
@login_required
def detail(record_id):
    rec = get_record_or_404(record_id)
    db = get_db()
    logs = db.execute(
        "SELECT * FROM record_logs WHERE record_id = ? ORDER BY id ASC", (record_id,)
    ).fetchall()

    me = g.user
    is_owner = rec["owner_id"] == me["id"]
    is_admin = me["role"] == "admin"
    can_pay = has_perm(me, "record.pay")

    receipts = db.execute(
        "SELECT * FROM record_receipts WHERE record_id = ? ORDER BY id ASC", (record_id,)
    ).fetchall()

    actions = {
        "can_edit": is_owner and rec["status"] in ("draft", "rejected"),
        # 有删除权限的人可删任意状态；其他人只能删自己的报批中单据
        "can_delete": has_perm(me, "record.delete")
                      or (is_owner and rec["status"] == "draft"),
        # 归属部门没确认不能提交（否则收款的部门口径就是错的）
        "can_submit": is_owner and rec["status"] in ("draft", "rejected")
                      and bool(rec["dept_id"]),
        "dept_pending": not rec["dept_id"],
        "can_withdraw": is_owner and rec["status"] == "pending_payment",
        # 收款：业务员本人 + 有收款权限的人（财务）
        "can_receive": can_receive_record(me, rec),
        # 费用登记：收到款项之后
        "can_fee": can_register_fee(me, rec),
        "can_reject": rec["status"] == "pending_payment" and can_pay,
        # 开票：收齐（贸易费 + 留存利润）之后才轮到财务（record.invoice）
        "can_invoice": can_register_invoice(me),
        "invoice_ready": bool(rec["receipt_closed"]) or rec["status"] == "completed",
        "invoice_pending": (bool(rec["receipt_closed"]) or rec["status"] == "completed")
                           and needs_invoice(rec) and not int(rec["is_invoiced"] or 0),
        # 未收齐就结清 = 特殊情况，需要主管特批
        "can_close_short": has_perm(me, "record.close_short"),
        "can_reopen": rec["status"] == "completed" and is_admin,
        # 流转时间轴：仅管理员与财务可见
        "show_logs": is_admin or can_pay,
    }
    # 回单识别建议值（只做建议，不自动入库）
    import ocr
    suggestions = ocr.parse_fields(rec["receipt_ocr_text"] or "") if rec["receipt_ocr_text"] else {}

    # 贸易费是否被手工修正过：给出公式值，方便核对（四舍五入差异用）
    fee_formula = trade_fee_cents(rec["biz_amount_cents"], rec["fee_rate"])
    fee_gap = int(rec["payable_amount_cents"] or 0) - fee_formula

    return render_template(
        "record_detail.html", rec=rec, logs=logs, actions=actions,
        receipts=receipts, progress=receipt_progress(rec),
        suggestions=suggestions, ocr_status=ocr.status(),
        dept_options=_dept_options(me),
        fee_formula=fee_formula, fee_gap=fee_gap,
    )


# ---------- 流转 ----------

@bp.route("/<int:record_id>/dept", methods=("POST",))
@login_required
def set_dept(record_id):
    """确认归属部门。

    导入时若客户档案里这家主体公司没挂部门，单据的归属部门留空（待确认），
    必须在这里选好部门才能提交进入收款。确认后顺手回填客户档案，
    下次导入同一主体公司就自动带出了。
    """
    rec = get_record_or_404(record_id)
    is_owner = rec["owner_id"] == g.user["id"]
    any_dept = has_perm(g.user, "record.create_any_dept")
    if not (is_owner or any_dept):
        abort(403)
    back = redirect(url_for("records.detail", record_id=record_id))
    if rec["dept_id"]:
        flash("归属部门已确认，如需变更请联系管理员", "error")
        return back

    try:
        dept_id = int(request.form.get("dept_id") or 0)
    except (TypeError, ValueError):
        dept_id = 0
    db = get_db()
    row = db.execute(
        "SELECT id, name FROM departments WHERE id = ? AND is_active = 1", (dept_id,)
    ).fetchone() if dept_id else None
    if not row:
        flash("请选择归属部门", "error")
        return back
    if not any_dept and dept_id != g.user["dept_id"]:
        flash("没有跨部门权限，只能确认到你所属的部门", "error")
        return back

    db.execute("UPDATE records SET dept_id = ?, updated_at = ? WHERE id = ?",
               (dept_id, now_text(), record_id))
    _log(db, record_id, "dept", rec["status"], rec["status"],
         f"确认归属部门：{row['name']}")

    # 回填客户档案（仅当档案里这家还没挂部门时）
    synced = False
    if rec["customer_id"]:
        cu = db.execute("SELECT id, dept_id FROM customers WHERE id = ?",
                        (rec["customer_id"],)).fetchone()
        if cu and not cu["dept_id"]:
            db.execute("UPDATE customers SET dept_id = ?, updated_at = ? WHERE id = ?",
                       (dept_id, now_text(), cu["id"]))
            synced = True
    db.commit()

    msg = f"归属部门已确认为「{row['name']}」，可以提交进入收款了"
    if synced:
        msg += "；已同步写入客户档案，下次导入自动带出"
    flash(msg, "ok")
    return back


@bp.route("/<int:record_id>/submit", methods=("POST",))
@login_required
def submit(record_id):
    rec = get_record_or_404(record_id)
    if rec["owner_id"] != g.user["id"]:
        abort(403)
    if rec["status"] not in ("draft", "rejected"):
        flash("当前状态不能提交", "error")
        return redirect(url_for("records.detail", record_id=record_id))
    if not rec["dept_id"]:
        flash("请先确认归属部门，再提交进入收款", "error")
        return redirect(url_for("records.detail", record_id=record_id))

    db = get_db()
    db.execute(
        "UPDATE records SET status='pending_payment', submitted_at=?, reject_reason=NULL, updated_at=? WHERE id=?",
        (now_text(), now_text(), record_id),
    )
    _log(db, record_id, "submit", rec["status"], "pending_payment",
         "提交，进入待收款，提醒资管确认收款")
    db.commit()
    notify_next(db, record_id, "submit")
    flash("已提交，进入待收款：等待资管确认客户款项", "ok")
    return redirect(url_for("records.detail", record_id=record_id))


@bp.route("/<int:record_id>/withdraw", methods=("POST",))
@login_required
def withdraw(record_id):
    """业务员在收款前撤回，回到报批中可继续修改。"""
    rec = get_record_or_404(record_id)
    if rec["owner_id"] != g.user["id"]:
        abort(403)
    if rec["status"] != "pending_payment":
        flash("只有待收款的单据可以撤回", "error")
        return redirect(url_for("records.detail", record_id=record_id))
    if int(rec["received_amount_cents"] or 0) > 0:
        flash("已经登记过收款，不能撤回；如金额有误请先删除收款记录", "error")
        return redirect(url_for("records.detail", record_id=record_id))

    db = get_db()
    db.execute(
        "UPDATE records SET status='draft', updated_at=? WHERE id=?",
        (now_text(), record_id),
    )
    _log(db, record_id, "withdraw", "pending_payment", "draft", "业务员撤回")
    db.commit()
    flash("已撤回，可继续修改后重新提交", "ok")
    return redirect(url_for("records.detail", record_id=record_id))


@bp.route("/<int:record_id>/reject", methods=("POST",))
@perm_required("record.pay")
def reject(record_id):
    """财务/主管发现金额或资料有误，退回业务员修改。"""
    rec = get_record_or_404(record_id)
    if rec["status"] != "pending_payment":
        flash("只有待收款的单据可以退回", "error")
        return redirect(url_for("records.detail", record_id=record_id))
    if int(rec["received_amount_cents"] or 0) > 0:
        flash("已经登记过收款，不能退回；请先删除收款记录", "error")
        return redirect(url_for("records.detail", record_id=record_id))

    reason = (request.form.get("reason") or "").strip()
    if not reason:
        flash("退回必须填写原因", "error")
        return redirect(url_for("records.detail", record_id=record_id))

    db = get_db()
    db.execute(
        "UPDATE records SET status='rejected', reject_reason=?, updated_at=? WHERE id=?",
        (reason, now_text(), record_id),
    )
    _log(db, record_id, "reject", "pending_payment", "rejected", reason)
    db.commit()
    notify_next(db, record_id, "reject", reason)
    flash("已退回给提交人", "ok")
    return redirect(url_for("records.detail", record_id=record_id))


# ---------- 收款 ----------

def _recalc_receipt_state(db, record_id):
    """按收款流水重算累计已收与状态。返回 (已收分, 是否结清, 待收分, 原状态)。

    应收合计 = 贸易费 + 留存利润；两项都收满（待收 ≤ 0）才自动结清。
    公司实收 = 累计已收（实际到账），随收款一起更新。
    """
    rec = db.execute("SELECT * FROM records WHERE id = ?", (record_id,)).fetchone()
    agg = db.execute(
        """SELECT COALESCE(SUM(amount_cents), 0) AS total,
                  COALESCE(MAX(is_close), 0)    AS closed
             FROM record_receipts WHERE record_id = ?""",
        (record_id,),
    ).fetchone()
    received = int(agg["total"] or 0)
    closed = bool(int(agg["closed"] or 0))

    amounts = calc_amounts(
        rec["biz_amount_cents"], rec["fee_rate"], rec["channel_commission_cents"],
        rec["prepaid_amount_cents"], received,
        # 贸易费可能被手工修正过，重算待收时不能拿公式值覆盖它
        override_trade_fee=rec["payable_amount_cents"],
    )
    outstanding = amounts["unpaid_amount_cents"]
    # 收满了也自动结清，避免金额已齐却还挂在「待收款」
    receivable = amounts["payable_amount_cents"] + int(rec["channel_commission_cents"] or 0)
    if not closed and outstanding <= 0 and receivable > 0:
        closed = True

    status = "completed" if closed else "pending_payment"
    db.execute(
        """UPDATE records SET received_amount_cents = ?, unpaid_amount_cents = ?,
                  payable_amount_cents = ?, company_income_cents = ?,
                  receipt_closed = ?, receipt_closed_at = ?, status = ?, updated_at = ?
            WHERE id = ?""",
        (received, outstanding, amounts["payable_amount_cents"],
         amounts["company_income_cents"], 1 if closed else 0,
         now_text() if closed else None, status, now_text(), record_id),
    )
    return received, closed, outstanding, rec["status"]


# ---------- 开票登记（收款后，财务） ----------

@bp.route("/<int:record_id>/invoice", methods=("POST",))
@perm_required("record.invoice")
def invoice_set(record_id):
    """开票登记（收齐后由财务确认）。

    登记时业务员已选「要 / 不要」：选「不要」的自动跳过这一步，不出现在财务待办里；
    选「要」的收齐后轮转到财务，财务只需勾「已经开票」。
    这里仍保留「不需要开票」选项，便于财务按实际情况纠正。
    """
    rec = get_record_or_404(record_id)
    back = redirect(url_for("records.detail", record_id=record_id))
    if not (rec["receipt_closed"] or rec["status"] == "completed"):
        flash("款项收齐后才能登记开票", "error")
        return back

    raw = (request.form.get("invoice_needed") or "").strip()
    if raw not in ("0", "1"):
        flash("请先选择是否需要开票", "error")
        return back
    needed = int(raw)
    invoiced = 0
    if needed and request.form.get("is_invoiced") in ("1", "on", "true"):
        invoiced = 1

    db = get_db()
    db.execute(
        "UPDATE records SET invoice_needed = ?, is_invoiced = ?, updated_at = ? WHERE id = ?",
        (needed, invoiced, now_text(), record_id),
    )
    note = ("不需要开票" if not needed
            else ("需要开票，已开票" if invoiced else "需要开票，待开票"))
    _log(db, record_id, "invoice", rec["status"], rec["status"], f"开票登记：{note}")
    db.commit()
    flash(f"已登记：{note}", "ok")
    return back


@bp.route("/<int:record_id>/receipts", methods=("POST",))
@login_required
def receipt_add(record_id):
    """登记一笔收款。

    金额留空 = 本次还没收到，业务继续挂在待收款；
    金额填 0 = 收款完毕（剩余不再追收），业务转「已收齐」——属特殊情况，
                需要「收款不足特批结清」权限（主管）；
    金额 > 0 = 累加已收，贸易费 + 留存利润都收满后自动转「已收齐」。
    """
    rec = get_record_or_404(record_id)
    if not can_receive_record(g.user, rec):
        flash("当前不能为这笔业务登记收款", "error")
        return redirect(url_for("records.detail", record_id=record_id))

    raw = (request.form.get("received_amount") or "").strip()
    if raw == "":
        amount_cents = 0
        is_close = False
        blank = True
    else:
        blank = False
        amount_cents = to_cents(raw)
        if amount_cents is None or amount_cents < 0:
            flash("本次收款金额格式不正确", "error")
            return redirect(url_for("records.detail", record_id=record_id))
        is_close = amount_cents == 0
        if is_close and not has_perm(g.user, "record.close_short"):
            flash("未收齐就结清属于特殊情况，需要主管特批；请按实填写本次收款金额", "error")
            return redirect(url_for("records.detail", record_id=record_id))

    received_date = parse_date(request.form.get("received_date")) or today()
    receipt_no = (request.form.get("receipt_no") or "").strip()
    note = (request.form.get("note") or "").strip()

    db = get_db()
    before = rec["status"]
    db.execute(
        """INSERT INTO record_receipts (record_id, amount_cents, received_date,
                                        receipt_no, note, is_close, actor_id, actor_name, created_at)
           VALUES (?,?,?,?,?,?,?,?,?)""",
        (record_id, amount_cents, received_date, receipt_no, note,
         1 if is_close else 0, g.user["id"], g.user["name"], now_text()),
    )
    received, closed, outstanding, _ = _recalc_receipt_state(db, record_id)

    if blank:
        detail_note = "本次未填金额：仍有款项待收，业务继续挂在待收款"
    elif is_close:
        detail_note = (f"主管特批结清（填 0，剩余不再追收），"
                       f"待收 {to_yuan_text(outstanding)} 元")
    else:
        detail_note = (f"收款 {to_yuan_text(amount_cents)} 元，回单号 {receipt_no or '—'}"
                       f"，累计已收 {to_yuan_text(received)} 元，待收 {to_yuan_text(outstanding)} 元")
    db.execute(
        "UPDATE records SET paid_date = ?, receipt_no = COALESCE(NULLIF(?, ''), receipt_no) WHERE id = ?",
        (received_date, receipt_no, record_id),
    )
    _log(db, record_id, "receipt", before, "completed" if closed else before, detail_note)
    db.commit()

    if blank:
        flash("已记录：仍有款项待收，业务继续挂在待收款", "ok")
    elif is_close:
        flash("收款已按主管特批结清" + (f"，待收 {to_yuan_text(outstanding)} 元不再追收"
                                        if outstanding > 0 else ""), "ok")
    elif closed:
        flash("本次收款已登记，累计收满，业务转为已收齐", "ok")
    else:
        flash(f"本次收款已登记，还差 {to_yuan_text(outstanding)} 元", "ok")

    # 每一轮收款确认完之后，回头通知登记这笔业务的业务员；
    # 未填金额的那一轮（还没收到钱）不通知，避免噪音。
    if not blank:
        if closed:
            notify_next(db, record_id, "paid")
        else:
            notify_next(
                db, record_id, "receipt",
                f"资管已确认收款 {to_yuan_text(amount_cents)} 元，"
                f"累计已收 {to_yuan_text(received)} 元，尚待收 {to_yuan_text(outstanding)} 元。",
            )
    return redirect(url_for("records.detail", record_id=record_id))


@bp.route("/<int:record_id>/receipts/<int:receipt_id>/delete", methods=("POST",))
@perm_required("record.pay")
def receipt_remove(record_id, receipt_id):
    """删掉一笔登记错的收款，累计与状态自动重算。"""
    rec = get_record_or_404(record_id)
    row = get_db().execute(
        "SELECT * FROM record_receipts WHERE id = ? AND record_id = ?",
        (receipt_id, record_id),
    ).fetchone()
    if row is None:
        flash("收款记录不存在", "error")
        return redirect(url_for("records.detail", record_id=record_id))

    db = get_db()
    db.execute("DELETE FROM record_receipts WHERE id = ?", (receipt_id,))
    received, closed, outstanding, before = _recalc_receipt_state(db, record_id)
    _log(db, record_id, "receipt",
         before, "completed" if closed else "pending_payment",
         f"删除收款记录（原 {to_yuan_text(row['amount_cents'])} 元），"
         f"累计已收 {to_yuan_text(received)} 元，待收 {to_yuan_text(outstanding)} 元")
    db.commit()
    flash("已删除该笔收款，金额与状态已重算", "ok")
    return redirect(url_for("records.detail", record_id=record_id))


@bp.route("/<int:record_id>/fee", methods=("POST",))
@perm_required("record.pay")
def fee_update(record_id):
    """费用登记：收到款项后，按四项分别填列。"""
    rec = get_record_or_404(record_id)
    if not can_register_fee(g.user, rec):
        flash("收到款项后才能登记费用", "error")
        return redirect(url_for("records.detail", record_id=record_id))

    values, errors = {}, []
    for field, label, col in FEE_TYPES:
        cents = to_cents(request.form.get(field))
        if cents is None or cents < 0:
            errors.append(f"{label}格式不正确")
            cents = 0
        values[col] = cents
    if errors:
        for e in errors:
            flash(e, "error")
        return redirect(url_for("records.detail", record_id=record_id))

    sets = ", ".join(f"{col} = ?" for col in values)
    db = get_db()
    db.execute(
        f"""UPDATE records SET {sets}, fee_registered_at = ?, fee_registered_by = ?,
                   updated_at = ? WHERE id = ?""",
        list(values.values()) + [now_text(), g.user["name"], now_text(), record_id],
    )
    _log(db, record_id, "fee", rec["status"], rec["status"],
         "费用登记：" + "，".join(f"{label} {to_yuan_text(values[col])} 元"
                                 for _f, label, col in FEE_TYPES))
    db.commit()
    flash("费用已登记", "ok")
    return redirect(url_for("records.detail", record_id=record_id))


@bp.route("/<int:record_id>/reopen", methods=("POST",))
@login_required
def reopen(record_id):
    if g.user["role"] != "admin":
        abort(403)
    rec = get_record_or_404(record_id)
    if rec["status"] != "completed":
        flash("只有已完成的业务可以撤销完成", "error")
        return redirect(url_for("records.detail", record_id=record_id))

    reason = (request.form.get("reason") or "").strip() or "管理员撤销完成"
    db = get_db()
    db.execute(
        "UPDATE records SET status='pending_payment', receipt_closed=0, "
        "receipt_closed_at=NULL, updated_at=? WHERE id=?",
        (now_text(), record_id),
    )
    _log(db, record_id, "reopen", "completed", "pending_payment", reason)
    db.commit()
    flash("已撤销完成状态，业务回到待收款，可继续登记收款或调整金额", "ok")
    return redirect(url_for("records.detail", record_id=record_id))


def _purge_receipt_folder(record_id):
    """删除业务时一并清掉回单附件目录，避免留下无主文件。"""
    import shutil

    folder = os.path.join(current_app.config["RECEIPT_DIR"], str(record_id))
    shutil.rmtree(folder, ignore_errors=True)


@bp.route("/<int:record_id>/delete", methods=("POST",))
@login_required
def delete(record_id):
    rec = get_record_or_404(record_id)
    # 有删除权限的人可删任意记录；其他人只能删自己的报批中单据
    if not can_delete_record(g.user, rec):
        flash("只有自己的报批中单据可以删除，其他情况请联系管理员", "error")
        return redirect(url_for("records.detail", record_id=record_id))

    label = f"#{rec['id']} {rec['customer_name']}"
    db = get_db()
    db.execute("DELETE FROM records WHERE id = ?", (record_id,))
    db.commit()
    # record_logs / record_receipts 由外键级联删除，回单目录手动清理
    _purge_receipt_folder(record_id)
    flash(f"已删除业务 {label}", "ok")
    return redirect(url_for("records.index"))


@bp.route("/batch-delete", methods=("POST",))
@login_required
def batch_delete():
    """批量删除：逐条校验权限，只删有权的，其余跳过并回报。"""
    ids = []
    for raw in request.form.getlist("ids"):
        try:
            value = int(raw)
        except (TypeError, ValueError):
            continue
        if value not in ids:
            ids.append(value)
    if not ids:
        flash("请先勾选要删除的业务", "error")
        return _back_to_list()
    if len(ids) > 500:
        flash("一次最多删除 500 条，请分批处理", "error")
        return _back_to_list()

    db = get_db()
    marks = ",".join("?" * len(ids))
    rows = db.execute(
        f"SELECT id, customer_name, owner_id, status FROM records WHERE id IN ({marks})",
        ids,
    ).fetchall()

    allowed = [r for r in rows if can_delete_record(g.user, r)]
    denied = len(rows) - len(allowed)
    if not allowed:
        flash("选中的业务你都没有删除权限，请联系管理员", "error")
        return _back_to_list()

    for rec in allowed:
        db.execute("DELETE FROM records WHERE id = ?", (rec["id"],))
    db.commit()
    for rec in allowed:
        _purge_receipt_folder(rec["id"])

    msg = f"已删除 {len(allowed)} 条业务"
    if denied:
        msg += f"，{denied} 条因无权限被跳过"
    if len(rows) < len(ids):
        msg += f"，{len(ids) - len(rows)} 条已不存在"
    flash(msg, "ok" if not denied else "error")
    return _back_to_list()


@bp.route("/batch-submit", methods=("POST",))
@login_required
def batch_submit():
    """批量提交：把勾选的报批中/已驳回单据一次性送进「待收款」。"""
    ids = []
    for raw in request.form.getlist("ids"):
        try:
            value = int(raw)
        except (TypeError, ValueError):
            continue
        if value not in ids:
            ids.append(value)
    if not ids:
        flash("请先勾选要提交的业务", "error")
        return _back_to_list()
    if len(ids) > 500:
        flash("一次最多提交 500 条，请分批处理", "error")
        return _back_to_list()

    db = get_db()
    marks = ",".join("?" * len(ids))
    rows = db.execute(
        f"SELECT id, owner_id, status, dept_id FROM records WHERE id IN ({marks})", ids
    ).fetchall()
    allowed = [r for r in rows if r["owner_id"] == g.user["id"]
               and r["status"] in ("draft", "rejected")]
    no_dept = [r for r in allowed if not r["dept_id"]]
    allowed = [r for r in allowed if r["dept_id"]]
    denied = len(rows) - len(allowed) - len(no_dept)
    if not allowed:
        if no_dept and not denied:
            flash(f"选中的 {len(no_dept)} 条还没确认归属部门，请先在业务详情里选好部门", "error")
        else:
            flash("选中的业务都不是你的待提交单据", "error")
        return _back_to_list()

    for rec in allowed:
        db.execute(
            "UPDATE records SET status='pending_payment', submitted_at=?, reject_reason=NULL, "
            "updated_at=? WHERE id=?",
            (now_text(), now_text(), rec["id"]),
        )
        _log(db, rec["id"], "submit", rec["status"], "pending_payment",
             "批量提交，进入待收款")
    db.commit()

    msg = f"已提交 {len(allowed)} 条，进入待收款"
    if no_dept:
        msg += f"，{len(no_dept)} 条因归属部门未确认被跳过"
    if denied:
        msg += f"，{denied} 条不符合条件被跳过（只能提交自己的报批中/被退回单据）"
    flash(msg, "ok" if not (denied or no_dept) else "error")
    return _back_to_list()


def _back_to_list():
    """回到删除前的列表位置（保留筛选与页码）。"""
    nxt = request.form.get("next") or request.args.get("next") or ""
    if nxt.startswith("/records"):
        return redirect(nxt)
    return redirect(url_for("records.index"))


# ---------- 回单附件与识别 ----------

@bp.route("/<int:record_id>/receipt", methods=("POST",))
@perm_required("record.pay")
def receipt_upload(record_id):
    from datetime import datetime

    from flask import current_app

    import ocr

    rec = get_record_or_404(record_id)
    file = request.files.get("receipt_file")
    if not file or not file.filename:
        flash("请选择回单文件", "error")
        return redirect(url_for("records.detail", record_id=record_id))

    ext = os.path.splitext(file.filename)[1].lower()
    if ext not in ocr.ALLOWED_EXTS:
        flash("只支持图片（JPG/PNG）或 PDF", "error")
        return redirect(url_for("records.detail", record_id=record_id))

    folder = os.path.join(current_app.config["RECEIPT_DIR"], str(record_id))
    os.makedirs(folder, exist_ok=True)
    stored = datetime.now().strftime("%Y%m%d%H%M%S") + ext
    full_path = os.path.join(folder, stored)
    file.save(full_path)
    rel_path = os.path.join(str(record_id), stored)

    text, err = ("", "")
    if ext in ocr.IMG_EXTS:
        text, err = ocr.recognize(full_path)

    db = get_db()
    db.execute(
        """UPDATE records SET receipt_file = ?, receipt_ocr_text = ?, receipt_ocr_at = ?,
                  updated_at = ? WHERE id = ?""",
        (rel_path, text, now_text(), now_text(), record_id),
    )
    _log(db, record_id, "receipt", rec["status"], rec["status"],
         "上传回单" + ("（已识别）" if text else "（未识别）"))
    db.commit()

    if err:
        flash(f"回单已上传，但识别失败：{err}", "error")
    elif text:
        flash("回单已上传并完成识别，请在页面下方核对建议值", "ok")
    else:
        flash("回单已上传", "ok")
    return redirect(url_for("records.detail", record_id=record_id))


@bp.route("/<int:record_id>/receipt/file")
@login_required
def receipt_download(record_id):
    from flask import send_from_directory

    rec = get_record_or_404(record_id)
    if not rec["receipt_file"]:
        abort(404)
    folder = os.path.join(current_app.config["RECEIPT_DIR"], os.path.dirname(rec["receipt_file"]))
    name = os.path.basename(rec["receipt_file"])
    return send_from_directory(folder, name, as_attachment=False)


@bp.route("/<int:record_id>/receipt/ocr", methods=("POST",))
@perm_required("record.pay")
def receipt_reocr(record_id):
    import ocr

    rec = get_record_or_404(record_id)
    if not rec["receipt_file"]:
        flash("请先上传回单", "error")
        return redirect(url_for("records.detail", record_id=record_id))

    full_path = os.path.join(current_app.config["RECEIPT_DIR"], rec["receipt_file"])
    if not os.path.exists(full_path):
        flash("回单文件丢失，请重新上传", "error")
        return redirect(url_for("records.detail", record_id=record_id))

    text, err = ocr.recognize(full_path)
    db = get_db()
    db.execute(
        "UPDATE records SET receipt_ocr_text = ?, receipt_ocr_at = ?, updated_at = ? WHERE id = ?",
        (text, now_text(), now_text(), record_id),
    )
    _log(db, record_id, "receipt", rec["status"], rec["status"], "重新识别回单")
    db.commit()
    flash(f"识别完成" if text else f"识别失败：{err or '未取到文字'}", "ok" if text else "error")
    return redirect(url_for("records.detail", record_id=record_id))


@bp.route("/<int:record_id>/receipt/delete", methods=("POST",))
@perm_required("record.pay")
def receipt_delete(record_id):
    rec = get_record_or_404(record_id)
    if not rec["receipt_file"]:
        flash("这笔业务还没有回单附件", "error")
        return redirect(url_for("records.detail", record_id=record_id))

    full_path = os.path.join(current_app.config["RECEIPT_DIR"], rec["receipt_file"])
    try:
        os.remove(full_path)
    except OSError:
        pass  # 文件已不在磁盘上，照常清理数据库字段

    db = get_db()
    db.execute(
        """UPDATE records SET receipt_file = NULL, receipt_ocr_text = NULL,
                  receipt_ocr_at = NULL, updated_at = ? WHERE id = ?""",
        (now_text(), record_id),
    )
    _log(db, record_id, "receipt", rec["status"], rec["status"], "删除回单附件")
    db.commit()
    flash("已删除回单附件", "ok")
    return redirect(url_for("records.detail", record_id=record_id))
