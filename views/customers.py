"""客户档案：列表、新增、编辑、停用、Excel 导入。"""
import io
import os

from flask import (Blueprint, abort, flash, g, redirect, render_template,
                   request, send_file, url_for)

from auth import login_required, perm_required
from db import get_db
from utils import now_text, to_cents

bp = Blueprint("customers", __name__, url_prefix="/customers")

PAGE_SIZE = 30

# 导入模板列定义：(表头, 字段名, 是否必填)
COLUMNS = [
    ("客户名称", "name", True),
    ("主体公司", "short_name", False),
    ("联系人", "contact", False),
    ("电话", "phone", False),
    ("纳税人识别号", "tax_no", False),
    ("开户行", "bank_name", False),
    ("银行账号", "bank_account", False),
    ("发票额度", "invoice_quota", False),
    ("地址", "address", False),
    ("备注", "remark", False),
]

# 旧模板用的是「简称」，导入时按别名兼容，避免历史表格被拒
HEADER_ALIASES = {
    "主体公司": ("简称",),
    "发票额度": ("开票额度", "额度"),
}

# 文本字段（发票额度是金额，单独按分存储，不在这份列表里）
FIELDS = [c[1] for c in COLUMNS if c[1] != "invoice_quota"]


def _norm(value):
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    return str(value).strip()


def _quota_cents():
    """发票额度按「元」录入，存为分。留空或非法一律按 0。"""
    value = to_cents(request.form.get("invoice_quota"))
    return value if value and value > 0 else 0


@bp.route("/")
@perm_required("customer.view")
def index():
    db = get_db()
    page = max(1, int(request.args.get("page", 1) or 1))
    kw = (request.args.get("kw") or "").strip()
    only_active = request.args.get("inactive") != "1"

    clauses, params = ["1=1"], []
    if kw:
        clauses.append("(c.name LIKE ? OR c.short_name LIKE ? OR c.contact LIKE ? OR c.phone LIKE ?)")
        params.extend([f"%{kw}%"] * 4)
    if only_active:
        clauses.append("c.is_active = 1")
    where = " AND ".join(clauses)

    total = db.execute(f"SELECT COUNT(*) c FROM customers c WHERE {where}", params).fetchone()["c"]
    pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
    page = min(page, pages)

    rows = db.execute(
        f"""SELECT c.*, d.name AS dept_name,
                   (SELECT COALESCE(SUM(r.biz_amount_cents), 0) FROM records r
                     WHERE r.customer_id = c.id AND r.is_invoiced = 1) AS invoiced_cents
             FROM customers c LEFT JOIN departments d ON d.id = c.dept_id
            WHERE {where}
            ORDER BY c.is_active DESC, c.name COLLATE NOCASE
            LIMIT ? OFFSET ?""",
        params + [PAGE_SIZE, (page - 1) * PAGE_SIZE],
    ).fetchall()

    depts = db.execute(
        "SELECT id, name FROM departments WHERE is_active = 1 ORDER BY sort_order, id"
    ).fetchall()

    return render_template(
        "customers.html", rows=rows, total=total, page=page, pages=pages,
        kw=kw, only_active=only_active, depts=depts,
    )


@bp.route("/new", methods=("POST",))
@perm_required("customer.manage")
def new():
    db = get_db()
    name = (request.form.get("name") or "").strip()
    if not name:
        flash("客户名称必填", "error")
        return redirect(url_for("customers.index"))
    if db.execute("SELECT 1 FROM customers WHERE name = ?", (name,)).fetchone():
        flash(f"客户「{name}」已存在", "error")
        return redirect(url_for("customers.index"))

    values = {f: (request.form.get(f) or "").strip() for f in FIELDS}
    values["name"] = name
    dept_id = request.form.get("dept_id") or None

    cols = FIELDS + ["invoice_quota_cents"]
    db.execute(
        f"""INSERT INTO customers ({', '.join(cols)}, dept_id, created_by, created_at, updated_at)
            VALUES ({', '.join('?' * len(cols))}, ?, ?, ?, ?)""",
        [values[f] for f in FIELDS] + [
            _quota_cents(),
            int(dept_id) if dept_id else None, g.user["name"], now_text(), now_text(),
        ],
    )
    db.commit()
    flash(f"已新增客户「{name}」", "ok")
    return redirect(url_for("customers.index"))


@bp.route("/<int:customer_id>/update", methods=("POST",))
@perm_required("customer.manage")
def update(customer_id):
    db = get_db()
    row = db.execute("SELECT * FROM customers WHERE id = ?", (customer_id,)).fetchone()
    if row is None:
        abort(404)

    name = (request.form.get("name") or "").strip()
    if not name:
        flash("客户名称必填", "error")
        return redirect(url_for("customers.index"))
    dup = db.execute(
        "SELECT id FROM customers WHERE name = ? AND id <> ?", (name, customer_id)
    ).fetchone()
    if dup:
        flash(f"客户名称「{name}」已被其他档案占用", "error")
        return redirect(url_for("customers.index"))

    values = {f: (request.form.get(f) or "").strip() for f in FIELDS}
    values["name"] = name
    dept_id = request.form.get("dept_id") or None

    sets = ", ".join(f"{f} = ?" for f in FIELDS)
    db.execute(
        f"UPDATE customers SET {sets}, invoice_quota_cents = ?, dept_id = ?, updated_at = ? "
        f"WHERE id = ?",
        [values[f] for f in FIELDS] + [
            _quota_cents(),
            int(dept_id) if dept_id else None, now_text(), customer_id,
        ],
    )
    # 客户改名时，历史业务里的名称快照同步更新
    if name != row["name"]:
        db.execute(
            "UPDATE records SET customer_name = ? WHERE customer_id = ?",
            (name, customer_id),
        )
    db.commit()
    flash(f"已保存客户「{name}」", "ok")
    return redirect(url_for("customers.index"))


@bp.route("/<int:customer_id>/toggle", methods=("POST",))
@perm_required("customer.manage")
def toggle(customer_id):
    db = get_db()
    row = db.execute("SELECT * FROM customers WHERE id = ?", (customer_id,)).fetchone()
    if row is None:
        abort(404)
    db.execute(
        "UPDATE customers SET is_active = ?, updated_at = ? WHERE id = ?",
        (0 if row["is_active"] else 1, now_text(), customer_id),
    )
    db.commit()
    flash(("已停用 " if row["is_active"] else "已启用 ") + row["name"], "ok")
    return redirect(url_for("customers.index"))


@bp.route("/<int:customer_id>/delete", methods=("POST",))
@perm_required("customer.manage")
def delete(customer_id):
    db = get_db()
    row = db.execute("SELECT * FROM customers WHERE id = ?", (customer_id,)).fetchone()
    if row is None:
        abort(404)

    used = db.execute(
        "SELECT COUNT(*) c FROM records WHERE customer_id = ?", (customer_id,)
    ).fetchone()["c"]
    if used:
        flash(f"客户「{row['name']}」还有 {used} 笔业务记录，不能删除；"
              f"不再合作请改用「停用」，历史数据会保留", "error")
        return redirect(url_for("customers.index"))

    db.execute("DELETE FROM customers WHERE id = ?", (customer_id,))
    db.commit()
    flash(f"已删除客户「{row['name']}」", "ok")
    return redirect(url_for("customers.index"))


# ---------- 批量操作 ----------

def _parse_ids():
    ids = []
    for raw in request.form.getlist("ids"):
        try:
            value = int(raw)
        except (TypeError, ValueError):
            continue
        if value not in ids:
            ids.append(value)
    return ids


def _back_to_list():
    nxt = request.form.get("next") or ""
    if nxt.startswith("/customers"):
        return redirect(nxt)
    return redirect(url_for("customers.index"))


@bp.route("/batch", methods=("POST",))
@perm_required("customer.manage")
def batch():
    """批量停用 / 启用 / 删除。删除按客户逐个校验是否已有业务记录。"""
    action = (request.form.get("action") or "").strip()
    ids = _parse_ids()
    if action not in ("disable", "enable", "delete"):
        flash("未知的批量操作", "error")
        return _back_to_list()
    if not ids:
        flash("请先勾选要操作的客户", "error")
        return _back_to_list()
    if len(ids) > 500:
        flash("一次最多处理 500 家客户，请分批操作", "error")
        return _back_to_list()

    db = get_db()
    marks = ",".join("?" * len(ids))
    rows = db.execute(
        f"""SELECT c.id, c.name, c.is_active,
                   (SELECT COUNT(*) FROM records r WHERE r.customer_id = c.id) AS used
              FROM customers c WHERE c.id IN ({marks})""",
        ids,
    ).fetchall()

    if action in ("disable", "enable"):
        target = 0 if action == "disable" else 1
        changed = 0
        for row in rows:
            if row["is_active"] == target:
                continue
            db.execute(
                "UPDATE customers SET is_active = ?, updated_at = ? WHERE id = ?",
                (target, now_text(), row["id"]),
            )
            changed += 1
        db.commit()
        verb = "停用" if target == 0 else "启用"
        skipped = len(rows) - changed
        msg = f"已{verb} {changed} 家客户"
        if skipped:
            msg += f"，{skipped} 家本来就是{'停用' if target == 0 else '启用'}状态"
        if len(rows) < len(ids):
            msg += f"，{len(ids) - len(rows)} 家已不存在"
        flash(msg, "ok")
        return _back_to_list()

    # 删除：有业务记录的客户不删，改建议停用
    deleted, blocked = [], []
    for row in rows:
        if row["used"]:
            blocked.append(row)
            continue
        db.execute("DELETE FROM customers WHERE id = ?", (row["id"],))
        deleted.append(row)
    db.commit()

    if not deleted:
        sample = "、".join(r["name"] for r in blocked[:3])
        more = f" 等 {len(blocked)} 家" if len(blocked) > 3 else ""
        flash(f"选中的客户都已有业务记录（{sample}{more}），不能删除；"
              f"不再合作请改用「停用」", "error")
        return _back_to_list()

    msg = f"已删除 {len(deleted)} 家客户"
    if blocked:
        msg += f"，{len(blocked)} 家因已有业务记录被跳过（可改用停用）"
    if len(rows) < len(ids):
        msg += f"，{len(ids) - len(rows)} 家已不存在"
    flash(msg, "ok" if not blocked else "error")
    return _back_to_list()


# ---------- 导入 ----------

@bp.route("/import", methods=("GET", "POST"))
@perm_required("customer.manage")
def import_page():
    if request.method == "GET":
        return render_template("customers_import.html", result=None)

    file = request.files.get("file")
    if not file or not file.filename:
        flash("请选择要导入的 Excel 文件", "error")
        return redirect(url_for("customers.import_page"))
    filename = os.path.basename(file.filename).lower()
    if not filename.endswith((".xlsx", ".xlsm")):
        flash("只支持 .xlsx 格式（.xls 请先另存为 .xlsx）", "error")
        return redirect(url_for("customers.import_page"))

    from openpyxl import load_workbook

    try:
        wb = load_workbook(file, data_only=True)
    except Exception as exc:
        flash(f"文件打不开：{exc}", "error")
        return redirect(url_for("customers.import_page"))

    ws = wb.active
    header = [_norm(c.value) for c in ws[1]]
    index_map = {}
    for col_name, field, required in COLUMNS:
        candidates = (col_name,) + HEADER_ALIASES.get(col_name, ())
        hit = next((header.index(c) for c in candidates if c in header), None)
        if hit is not None:
            index_map[field] = hit
        elif required:
            flash(f"缺少必需列「{col_name}」", "error")
            return redirect(url_for("customers.import_page"))

    db = get_db()
    created, updated, skipped, errors = 0, 0, 0, []
    seen = set()

    for row_no, row in enumerate(ws.iter_rows(min_row=2, values_only=True), start=2):
        if row is None or all(_norm(v) == "" for v in row):
            continue

        def cell(field):
            idx = index_map.get(field)
            if idx is None or idx >= len(row):
                return ""
            return _norm(row[idx])

        name = cell("name")
        if not name:
            skipped += 1
            errors.append(f"第 {row_no} 行：客户名称为空，已跳过")
            continue
        if name in seen:
            skipped += 1
            errors.append(f"第 {row_no} 行：文件内重复的客户「{name}」，已跳过")
            continue
        seen.add(name)
        if len(name) > 100:
            skipped += 1
            errors.append(f"第 {row_no} 行：客户名称超过 100 字，已跳过")
            continue

        payload = {f: cell(f) for f in FIELDS}
        payload["name"] = name
        raw_quota = cell("invoice_quota")
        # 空值代表这一行没提供额度，更新时保持原值不动
        quota = to_cents(raw_quota) if raw_quota else None

        exists = db.execute("SELECT id FROM customers WHERE name = ?", (name,)).fetchone()
        try:
            if exists:
                # 已存在：用非空值覆盖，空值保留原样
                sets, vals = [], []
                for f in FIELDS:
                    if f == "name" or payload[f] == "":
                        continue
                    sets.append(f"{f} = ?")
                    vals.append(payload[f])
                if quota is not None:
                    sets.append("invoice_quota_cents = ?")
                    vals.append(quota)
                if sets:
                    vals.append(now_text())
                    db.execute(
                        f"UPDATE customers SET {', '.join(sets)}, updated_at = ? WHERE id = ?",
                        vals + [exists["id"]],
                    )
                updated += 1
            else:
                cols = FIELDS + ["invoice_quota_cents"]
                db.execute(
                    f"""INSERT INTO customers ({', '.join(cols)}, created_by, created_at, updated_at)
                        VALUES ({', '.join('?' * len(cols))}, ?, ?, ?)""",
                    [payload[f] for f in FIELDS] + [
                        quota or 0, g.user["name"], now_text(), now_text(),
                    ],
                )
                created += 1
        except Exception as exc:
            skipped += 1
            errors.append(f"第 {row_no} 行：写入失败（{exc}）")

    db.commit()

    result = {
        "created": created, "updated": updated, "skipped": skipped,
        "errors": errors[:200], "error_total": len(errors),
        "filename": file.filename,
    }
    flash(f"导入完成：新增 {created}，更新 {updated}，跳过 {skipped}", "ok")
    return render_template("customers_import.html", result=result)


@bp.route("/import/template")
@perm_required("customer.manage")
def import_template():
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill

    wb = Workbook()
    ws = wb.active
    ws.title = "客户档案"
    ws.append([c[0] for c in COLUMNS])
    for c in ws[1]:
        c.font = Font(bold=True, color="FFFFFF")
        c.fill = PatternFill("solid", fgColor="0F766E")
        c.alignment = Alignment(horizontal="center", vertical="center")

    sample = ["广州华农贸易有限公司", "华农", "张经理", "13800000000",
              "91440101MA5XXXXX0A", "中国银行广州分行", "6222 0000 0000 0000",
              "500000", "广州市天河区XX路1号", "月结客户"]
    ws.append(sample)

    widths = [32, 14, 12, 16, 26, 26, 26, 14, 32, 24]
    for i, w in enumerate(widths, start=1):
        ws.column_dimensions[ws.cell(row=1, column=i).column_letter].width = w
    ws.freeze_panes = "A2"

    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return send_file(
        buf, as_attachment=True, download_name="客户档案导入模板.xlsx",
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )
