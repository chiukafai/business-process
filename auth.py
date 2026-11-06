"""登录、会话与权限判断。"""
import functools
import json

from flask import (Blueprint, flash, g, redirect, render_template, request,
                   session, url_for)
from werkzeug.security import check_password_hash, generate_password_hash

from db import get_db

bp = Blueprint("auth", __name__)


# ---------- 会话 ----------

def load_logged_in_user():
    user_id = session.get("user_id")
    if user_id is None:
        g.user = None
        g.perms = set()
        return
    g.user = get_db().execute(
        """SELECT u.*, d.name AS dept_name
             FROM users u LEFT JOIN departments d ON d.id = u.dept_id
            WHERE u.id = ?""",
        (user_id,),
    ).fetchone()
    if g.user is None or not g.user["is_active"]:
        session.clear()
        g.user = None
        g.perms = set()
        return
    g.perms = set(json.loads(g.user["permissions"] or "[]"))


# ---------- 权限 ----------

# 权限继承：拥有左边的权限，视同也拥有右边的权限
IMPLIED_PERMS = {
    "record.create": ("record.create_any_dept",),
    "record.view_dept": ("record.view_all",),
}


def has_perm(user, code):
    if not user or not user["is_active"]:
        return False
    if user["role"] == "admin":
        return True
    perms = g.get("perms", set())
    if code in perms:
        return True
    return any(parent in perms for parent in IMPLIED_PERMS.get(code, ()))


def can(code):
    """模板里用：{% if can('record.approve') %}"""
    return has_perm(g.get("user"), code)


def visible_scope(user):
    """返回 ('all', None) 或 ('dept', dept_id) 或 ('own', user_id)。"""
    if not user:
        return ("own", None)
    if has_perm(user, "record.view_all"):
        return ("all", None)
    if has_perm(user, "record.view_dept"):
        return ("dept", user["dept_id"])
    return ("own", user["id"])


def can_view_record(user, rec):
    if not user:
        return False
    if rec["owner_id"] == user["id"]:
        return True
    scope, value = visible_scope(user)
    if scope == "all":
        return True
    if scope == "dept":
        return rec["dept_id"] == value
    return False


def can_delete_record(user, rec):
    """能否删除某条业务：有删除权限可删任意记录；本人只能删自己的草稿。"""
    if not user:
        return False
    if has_perm(user, "record.delete"):
        return True
    return rec["owner_id"] == user["id"] and rec["status"] == "draft"


def can_receive_record(user, rec):
    """能否登记收款：业务员本人（收款责任人）或有收款权限的人（财务/主管）。"""
    if not user or rec["status"] != "pending_payment":
        return False
    return rec["owner_id"] == user["id"] or has_perm(user, "record.pay")


def can_register_fee(user, rec):
    """费用登记：收到款项之后才能登记，且要有收款/财务权限。"""
    if not user or not has_perm(user, "record.pay"):
        return False
    if rec["status"] == "completed":
        return True
    try:
        received = int(rec["received_amount_cents"] or 0)
        prepaid = int(rec["prepaid_amount_cents"] or 0)
    except (KeyError, IndexError):
        return False
    return received > 0 or prepaid > 0


def login_required(view):
    @functools.wraps(view)
    def wrapped(**kwargs):
        if g.user is None:
            return redirect(url_for("auth.login", next=request.full_path))
        if g.user["must_change_password"] and request.endpoint != "auth.change_password":
            return redirect(url_for("auth.change_password"))
        return view(**kwargs)
    return wrapped


def perm_required(code):
    def decorator(view):
        @functools.wraps(view)
        def wrapped(**kwargs):
            if g.user is None:
                return redirect(url_for("auth.login", next=request.full_path))
            if g.user["must_change_password"]:
                return redirect(url_for("auth.change_password"))
            if not has_perm(g.user, code):
                flash("没有权限执行该操作", "error")
                return redirect(url_for("dashboard.index"))
            return view(**kwargs)
        return wrapped
    return decorator


# ---------- 路由 ----------

@bp.route("/login", methods=("GET", "POST"))
def login():
    if g.user is not None:
        return redirect(url_for("dashboard.index"))

    if request.method == "POST":
        username = (request.form.get("username") or "").strip()
        password = request.form.get("password") or ""
        db = get_db()
        user = db.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()

        if user is None or not check_password_hash(user["password_hash"], password):
            flash("用户名或密码不正确", "error")
        elif not user["is_active"]:
            flash("账号已停用，请联系管理员", "error")
        else:
            session.clear()
            session["user_id"] = user["id"]
            session.permanent = True
            db.execute(
                "UPDATE users SET last_login_at = datetime('now','localtime') WHERE id = ?",
                (user["id"],),
            )
            db.commit()
            nxt = request.args.get("next") or url_for("dashboard.index")
            if not nxt.startswith("/"):
                nxt = url_for("dashboard.index")
            return redirect(nxt)

    return render_template("login.html")


@bp.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("auth.login"))


@bp.route("/change-password", methods=("GET", "POST"))
def change_password():
    if g.user is None:
        return redirect(url_for("auth.login"))

    if request.method == "POST":
        old = request.form.get("old_password") or ""
        new = request.form.get("new_password") or ""
        confirm = request.form.get("confirm_password") or ""

        if not check_password_hash(g.user["password_hash"], old):
            flash("当前密码不正确", "error")
        elif len(new) < 8:
            flash("新密码至少 8 位", "error")
        elif new != confirm:
            flash("两次输入的新密码不一致", "error")
        elif new == old:
            flash("新密码不能与当前密码相同", "error")
        else:
            db = get_db()
            db.execute(
                "UPDATE users SET password_hash = ?, must_change_password = 0 WHERE id = ?",
                (generate_password_hash(new), g.user["id"]),
            )
            db.commit()
            flash("密码已更新", "ok")
            return redirect(url_for("dashboard.index"))

    return render_template("change_password.html")
