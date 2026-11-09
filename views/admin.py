"""后台：人员与权限管理、部门维护。"""
import json

from flask import (Blueprint, abort, flash, g, jsonify, redirect,
                   render_template, request, url_for)
from werkzeug.security import generate_password_hash

import db as db_module
import notifier
from auth import has_perm, login_required, perm_required
from db import get_db
from utils import (CUSTOMER_LABEL_SQL, CUSTOMER_SEARCH_SQL, ROLE_LABELS,
                   now_text, status_label)

bp = Blueprint("admin", __name__, url_prefix="/admin")

DEFAULT_PASSWORD = "Hailu@2026"


def _perm_catalog():
    return get_db().execute(
        "SELECT * FROM permission_catalog ORDER BY sort_order, code"
    ).fetchall()


def _role_defaults(role):
    """返回该角色默认权限码列表。"""
    rows = _perm_catalog()
    codes = []
    for row in rows:
        defaults = json.loads(row["role_default"] or "[]")
        if role in defaults:
            codes.append(row["code"])
    return codes


# ---------- 人员 ----------

@bp.route("/users")
@perm_required("admin.users")
def users():
    db = get_db()
    rows = db.execute(
        """SELECT u.*, d.name AS dept_name
             FROM users u LEFT JOIN departments d ON d.id = u.dept_id
            ORDER BY u.is_active DESC, u.id"""
    ).fetchall()
    depts = db.execute(
        "SELECT id, name FROM departments ORDER BY sort_order, id"
    ).fetchall()
    parsed = []
    for r in rows:
        item = dict(r)
        item["perm_list"] = json.loads(r["permissions"] or "[]")
        parsed.append(item)

    groups = {}
    for row in _perm_catalog():
        groups.setdefault(row["group_name"], []).append(row)

    return render_template(
        "admin_users.html", users=parsed, depts=depts,
        perm_groups=list(groups.items()),
        role_defaults={role: _role_defaults(role) for role in ROLE_LABELS},
    )


@bp.route("/users/new", methods=("POST",))
@perm_required("admin.users")
def user_new():
    db = get_db()
    username = (request.form.get("username") or "").strip()
    name = (request.form.get("name") or "").strip()
    phone = (request.form.get("phone") or "").strip()
    wecom_userid = (request.form.get("wecom_userid") or "").strip()
    role = request.form.get("role") or "staff"
    dept_id = request.form.get("dept_id") or None
    password = request.form.get("password") or DEFAULT_PASSWORD
    perms = request.form.getlist("permissions")

    if not username or not name:
        flash("用户名和姓名必填", "error")
        return redirect(url_for("admin.users"))
    if role not in ROLE_LABELS:
        role = "staff"
    if len(password) < 8:
        flash("初始密码至少 8 位", "error")
        return redirect(url_for("admin.users"))
    if db.execute("SELECT 1 FROM users WHERE username = ?", (username,)).fetchone():
        flash(f"用户名 {username} 已存在", "error")
        return redirect(url_for("admin.users"))

    # 管理员角色默认拥有全部权限，但仍把勾选结果存下来便于审计
    if role == "admin" and not perms:
        perms = [row["code"] for row in _perm_catalog()]

    db.execute(
        """INSERT INTO users (username, password_hash, name, phone, dept_id, role,
                              permissions, wecom_userid, is_active, must_change_password)
           VALUES (?,?,?,?,?,?,?,?,1,1)""",
        (username, generate_password_hash(password), name, phone,
         int(dept_id) if dept_id else None, role,
         json.dumps(perms, ensure_ascii=False), wecom_userid or None),
    )
    db.commit()
    flash(f"已添加人员 {name}，初始密码 {password}，首次登录会要求改密", "ok")
    return redirect(url_for("admin.users"))


@bp.route("/users/<int:user_id>/update", methods=("POST",))
@perm_required("admin.users")
def user_update(user_id):
    db = get_db()
    target = db.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    if target is None:
        abort(404)

    name = (request.form.get("name") or "").strip()
    phone = (request.form.get("phone") or "").strip()
    wecom_userid = (request.form.get("wecom_userid") or "").strip()
    role = request.form.get("role") or target["role"]
    dept_id = request.form.get("dept_id") or None
    perms = request.form.getlist("permissions")

    if not name:
        flash("姓名必填", "error")
        return redirect(url_for("admin.users"))
    if role not in ROLE_LABELS:
        role = target["role"]

    # 不许把自己降权或停用，避免整站失去管理员
    if target["id"] == g.user["id"]:
        if role != "admin":
            flash("不能修改自己的管理员角色", "error")
            return redirect(url_for("admin.users"))
        perms = [row["code"] for row in _perm_catalog()]

    db.execute(
        """UPDATE users SET name=?, phone=?, dept_id=?, role=?, permissions=?,
                  wecom_userid=? WHERE id=?""",
        (name, phone, int(dept_id) if dept_id else None, role,
         json.dumps(perms, ensure_ascii=False), wecom_userid or None, user_id),
    )
    db.commit()
    flash(f"已更新 {name} 的权限", "ok")
    return redirect(url_for("admin.users"))


@bp.route("/users/<int:user_id>/toggle", methods=("POST",))
@perm_required("admin.users")
def user_toggle(user_id):
    db = get_db()
    target = db.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    if target is None:
        abort(404)
    if target["id"] == g.user["id"]:
        flash("不能停用自己的账号", "error")
        return redirect(url_for("admin.users"))

    new_state = 0 if target["is_active"] else 1
    db.execute("UPDATE users SET is_active = ? WHERE id = ?", (new_state, user_id))
    db.commit()
    flash(("已启用 " if new_state else "已停用 ") + target["name"], "ok")
    return redirect(url_for("admin.users"))


@bp.route("/users/<int:user_id>/reset", methods=("POST",))
@perm_required("admin.users")
def user_reset(user_id):
    db = get_db()
    target = db.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    if target is None:
        abort(404)
    password = request.form.get("password") or DEFAULT_PASSWORD
    if len(password) < 8:
        flash("密码至少 8 位", "error")
        return redirect(url_for("admin.users"))
    db.execute(
        "UPDATE users SET password_hash = ?, must_change_password = 1 WHERE id = ?",
        (generate_password_hash(password), user_id),
    )
    db.commit()
    flash(f"已重置 {target['name']} 的密码为 {password}", "ok")
    return redirect(url_for("admin.users"))


@bp.route("/users/<int:user_id>/delete", methods=("POST",))
@perm_required("admin.users")
def user_delete(user_id):
    db = get_db()
    target = db.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    if target is None:
        abort(404)
    if target["id"] == g.user["id"]:
        flash("不能删除自己的账号", "error")
        return redirect(url_for("admin.users"))

    used = db.execute(
        "SELECT COUNT(*) c FROM records WHERE owner_id = ?", (user_id,)
    ).fetchone()["c"]
    if used:
        flash(f"{target['name']} 名下还有 {used} 笔业务记录，不能删除；"
              f"如要收回权限请改用「停用」", "error")
        return redirect(url_for("admin.users"))

    db.execute("DELETE FROM users WHERE id = ?", (user_id,))
    db.commit()
    flash(f"已删除人员 {target['name']}", "ok")
    return redirect(url_for("admin.users"))


# ---------- 部门 ----------

@bp.route("/depts")
@perm_required("admin.master")
def depts():
    db = get_db()
    rows = db.execute(
        """SELECT d.*, (SELECT COUNT(*) FROM records r WHERE r.dept_id = d.id) AS record_count,
                  (SELECT COUNT(*) FROM users u WHERE u.dept_id = d.id) AS user_count
             FROM departments d ORDER BY d.sort_order, d.id"""
    ).fetchall()
    return render_template("admin_depts.html", depts=rows)


@bp.route("/depts/new", methods=("POST",))
@perm_required("admin.master")
def dept_new():
    name = (request.form.get("name") or "").strip()
    short_name = (request.form.get("short_name") or "").strip()
    try:
        sort_order = int(request.form.get("sort_order") or 100)
    except ValueError:
        sort_order = 100
    if not name:
        flash("部门名称必填", "error")
        return redirect(url_for("admin.depts"))
    db = get_db()
    try:
        db.execute(
            "INSERT INTO departments (name, short_name, sort_order) VALUES (?,?,?)",
            (name, short_name, sort_order),
        )
        db.commit()
        flash(f"已添加 {name}", "ok")
    except Exception:
        flash(f"部门 {name} 已存在", "error")
    return redirect(url_for("admin.depts"))


@bp.route("/depts/<int:dept_id>/update", methods=("POST",))
@perm_required("admin.master")
def dept_update(dept_id):
    name = (request.form.get("name") or "").strip()
    short_name = (request.form.get("short_name") or "").strip()
    try:
        sort_order = int(request.form.get("sort_order") or 100)
    except ValueError:
        sort_order = 100
    if not name:
        flash("部门名称必填", "error")
        return redirect(url_for("admin.depts"))
    db = get_db()
    try:
        db.execute(
            "UPDATE departments SET name=?, short_name=?, sort_order=? WHERE id=?",
            (name, short_name, sort_order, dept_id),
        )
        db.commit()
        flash("已保存", "ok")
    except Exception:
        flash("保存失败：名称可能重复", "error")
    return redirect(url_for("admin.depts"))


@bp.route("/depts/<int:dept_id>/toggle", methods=("POST",))
@perm_required("admin.master")
def dept_toggle(dept_id):
    db = get_db()
    row = db.execute("SELECT * FROM departments WHERE id = ?", (dept_id,)).fetchone()
    if row is None:
        abort(404)
    db.execute("UPDATE departments SET is_active = ? WHERE id = ?",
               (0 if row["is_active"] else 1, dept_id))
    db.commit()
    flash(("已停用 " if row["is_active"] else "已启用 ") + row["name"], "ok")
    return redirect(url_for("admin.depts"))


@bp.route("/depts/<int:dept_id>/delete", methods=("POST",))
@perm_required("admin.master")
def dept_delete(dept_id):
    db = get_db()
    row = db.execute("SELECT * FROM departments WHERE id = ?", (dept_id,)).fetchone()
    if row is None:
        abort(404)

    rec_n = db.execute(
        "SELECT COUNT(*) c FROM records WHERE dept_id = ?", (dept_id,)
    ).fetchone()["c"]
    user_n = db.execute(
        "SELECT COUNT(*) c FROM users WHERE dept_id = ?", (dept_id,)
    ).fetchone()["c"]
    if rec_n or user_n:
        parts = []
        if rec_n:
            parts.append(f"{rec_n} 笔业务")
        if user_n:
            parts.append(f"{user_n} 名人员")
        flash(f"「{row['name']}」下还有 {'、'.join(parts)}，不能删除；请改用「停用」", "error")
        return redirect(url_for("admin.depts"))

    cust_n = db.execute(
        "SELECT COUNT(*) c FROM customers WHERE dept_id = ?", (dept_id,)
    ).fetchone()["c"]
    if cust_n:
        # 客户档案的归属部门是可选项，删除部门前先置空，保留客户本身
        db.execute("UPDATE customers SET dept_id = NULL WHERE dept_id = ?", (dept_id,))

    db.execute("DELETE FROM departments WHERE id = ?", (dept_id,))
    db.commit()
    flash(
        f"已删除部门「{row['name']}」"
        + (f"，{cust_n} 家客户的归属部门已清空" if cust_n else ""),
        "ok",
    )
    return redirect(url_for("admin.depts"))


# ---------- 接口：切换角色时取默认权限 ----------

@bp.route("/api/role-defaults/<role>")
@perm_required("admin.users")
def role_defaults_api(role):
    if role not in ROLE_LABELS:
        abort(404)
    return jsonify({"role": role, "permissions": _role_defaults(role)})


# ---------- 操作日志（仅管理员） ----------

def _require_admin():
    if g.user is None or g.user["role"] != "admin":
        abort(403)


LOG_ACTION_LABELS = {
    "create": "新建", "submit": "提交", "update": "修改",
    "withdraw": "撤回", "reject": "退回", "pay": "付款",
    "complete": "完成", "reopen": "撤销完成", "import": "导入",
    "receipt": "回单上传", "approve": "审核通过（旧流程）",
}


def _log_filters(args):
    clauses, params = ["1=1"], []
    if args.get("action"):
        clauses.append("l.action = ?")
        params.append(args.get("action"))
    if args.get("actor"):
        clauses.append("l.actor_name LIKE ?")
        params.append(f"%{args.get('actor')}%")
    if args.get("from"):
        clauses.append("l.created_at >= ?")
        params.append(args.get("from") + " 00:00:00")
    if args.get("to"):
        clauses.append("l.created_at <= ?")
        params.append(args.get("to") + " 23:59:59")
    kw = (args.get("kw") or "").strip()
    if kw:
        clauses.append(f"({CUSTOMER_SEARCH_SQL} OR l.note LIKE ? OR CAST(r.id AS TEXT) = ?)")
        params.extend([f"%{kw}%", f"%{kw}%", kw])
    return " AND ".join(clauses), params


@bp.route("/logs")
@login_required
def logs():
    _require_admin()
    db = get_db()
    page = max(1, int(request.args.get("page", 1) or 1))
    size = 50
    where, params = _log_filters(request.args)

    total = db.execute(
        f"""SELECT COUNT(*) c FROM record_logs l
              LEFT JOIN records r ON r.id = l.record_id
              LEFT JOIN customers cu ON cu.id = r.customer_id
             WHERE {where}""",
        params,
    ).fetchone()["c"]
    pages = max(1, (total + size - 1) // size)
    page = min(page, pages)

    rows = db.execute(
        f"""SELECT l.*, {CUSTOMER_LABEL_SQL}, r.biz_amount_cents
              FROM record_logs l
              LEFT JOIN records r ON r.id = l.record_id
              LEFT JOIN customers cu ON cu.id = r.customer_id
             WHERE {where}
             ORDER BY l.id DESC LIMIT ? OFFSET ?""",
        params + [size, (page - 1) * size],
    ).fetchall()

    return render_template(
        "admin_logs.html", rows=rows, total=total, page=page, pages=pages,
        action_labels=LOG_ACTION_LABELS,
    )


@bp.route("/logs/export")
@login_required
def logs_export():
    _require_admin()
    import io
    from flask import send_file
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill

    db = get_db()
    where, params = _log_filters(request.args)
    rows = db.execute(
        f"""SELECT l.id, l.created_at, r.id AS rid, {CUSTOMER_LABEL_SQL},
                   r.biz_amount_cents, l.action, l.from_status, l.to_status,
                   l.actor_name, l.note
              FROM record_logs l
              LEFT JOIN records r ON r.id = l.record_id
              LEFT JOIN customers cu ON cu.id = r.customer_id
             WHERE {where}
             ORDER BY l.id DESC""",
        params,
    ).fetchall()

    wb = Workbook()
    ws = wb.active
    ws.title = "操作日志"
    headers = ["日志ID", "时间", "业务单号", "主体公司", "业务金额", "动作",
               "原状态", "新状态", "操作人", "说明"]
    ws.append(headers)
    for c in ws[1]:
        c.font = Font(bold=True, color="FFFFFF")
        c.fill = PatternFill("solid", fgColor="0F766E")
        c.alignment = Alignment(horizontal="center", vertical="center")

    for r in rows:
        ws.append([
            r["id"], r["created_at"], r["rid"], r["customer_label"],
            (r["biz_amount_cents"] or 0) / 100,
            LOG_ACTION_LABELS.get(r["action"], r["action"]),
            status_label(r["from_status"]), status_label(r["to_status"]),
            r["actor_name"], r["note"],
        ])

    widths = [8, 20, 10, 26, 14, 10, 10, 10, 12, 42]
    for i, w in enumerate(widths, start=1):
        ws.column_dimensions[ws.cell(row=1, column=i).column_letter].width = w
    ws.freeze_panes = "A2"

    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    stamp = now_text().replace("-", "").replace(":", "").replace(" ", "-")
    return send_file(
        buf, as_attachment=True,
        download_name=f"操作日志_{stamp}.xlsx",
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )


# ---------- 系统设置（企业微信） ----------

@bp.route("/settings", methods=("GET", "POST"))
@login_required
def settings():
    _require_admin()
    db = get_db()
    if request.method == "POST":
        for key in notifier.SETTING_KEYS:
            if key == "wecom_enabled":
                set_value = "1" if request.form.get(key) in ("1", "on", "true") else "0"
            else:
                set_value = (request.form.get(key) or "").strip()
            notifier.set_setting(db, key, set_value, g.user["name"])
        # 凭据变了就清掉缓存 token
        notifier.set_setting(db, "wecom_token", "", g.user["name"])
        notifier.set_setting(db, "wecom_token_expire", "0", g.user["name"])
        db.commit()
        flash("设置已保存", "ok")
        return redirect(url_for("admin.settings"))

    cfg = notifier.config(db)
    wx_users = db.execute(
        """SELECT name, username, wecom_userid FROM users
            WHERE is_active = 1 ORDER BY id"""
    ).fetchall()
    return render_template("admin_settings.html", cfg=cfg, wx_users=wx_users)


@bp.route("/settings/test", methods=("POST",))
@login_required
def settings_test():
    _require_admin()
    db = get_db()
    target = (request.form.get("test_userid") or "").strip()
    if not target:
        flash("请先填写接收测试消息的企业微信 userid", "error")
        return redirect(url_for("admin.settings"))
    ok, err = notifier.send_text(db, [target], "【测试】业务登记系统通知已接通。")
    db.commit()
    flash(f"测试消息已发送给 {target}" if ok else f"发送失败：{err}", "ok" if ok else "error")
    return redirect(url_for("admin.settings"))
