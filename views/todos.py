"""我的待办：按权限与状态实时计算，不落表。

流程：登记 → 提交进「待收款」→ 业务员跟进收客户款 → 收齐后「已收齐」→ 财务登记费用。
所以待办的核心分类是「待我收款」，不再是原来的「待我付款」。
"""
from flask import Blueprint, g, render_template, request

from auth import has_perm, login_required, visible_scope
from db import get_db
from utils import CUSTOMER_LABEL_SQL

bp = Blueprint("todos", __name__, url_prefix="/todos")

TODO_META = {
    "receipt": {"label": "待我收款", "hint": "已提交、款项还没收齐，去跟进客户回款"},
    "collect": {"label": "待收款跟进", "hint": "本部门还没收齐的业务，可协助登记收款"},
    "revise":  {"label": "待我修改", "hint": "被退回，需要你修改后重新提交"},
    "mine":    {"label": "我的报批中", "hint": "还没提交、或待确认归属部门的业务"},
}

TAB_ORDER = ("receipt", "collect", "revise", "mine")


def _dept_clause(user, alias="r"):
    scope, value = visible_scope(user)
    if scope == "all":
        return "1=1", []
    if scope == "dept":
        return f"{alias}.dept_id = ?", [value]
    return "1=0", []


def build_todos(user):
    """返回 {kind: [记录行]}。没有权限的分类给空列表，模板按 tab_order 渲染。"""
    db = get_db()
    result = {"receipt": [], "collect": [], "revise": [], "mine": []}
    if not user:
        return result

    base = f"""SELECT r.*, d.name AS dept_name, u.name AS owner_display, {CUSTOMER_LABEL_SQL}
                FROM records r
                LEFT JOIN departments d ON d.id = r.dept_id
                LEFT JOIN users u ON u.id = r.owner_id
                LEFT JOIN customers cu ON cu.id = r.customer_id
               WHERE {{cond}}
               ORDER BY r.submitted_at ASC, r.updated_at ASC
               LIMIT 200"""

    # 待我收款：我登记的单子，钱还没收齐
    result["receipt"] = db.execute(
        base.format(cond="r.status = 'pending_payment' AND r.owner_id = ?"), [user["id"]]
    ).fetchall()

    # 待收款跟进：别人登记、我能看到的未收齐业务（财务 / 主管用）
    if has_perm(user, "record.pay") or has_perm(user, "record.view_dept"):
        clause, params = _dept_clause(user)
        result["collect"] = db.execute(
            base.format(cond=f"r.status = 'pending_payment' AND ({clause}) AND r.owner_id <> ?"),
            params + [user["id"]],
        ).fetchall()

    result["revise"] = db.execute(
        base.format(cond="r.status = 'rejected' AND r.owner_id = ?"), [user["id"]]
    ).fetchall()

    result["mine"] = db.execute(
        base.format(cond="r.status = 'draft' AND r.owner_id = ?"), [user["id"]]
    ).fetchall()

    return result


def visible_tabs(user):
    """当前用户该看到的 tab 顺序：没权限的分类不显示。"""
    tabs = ["receipt"]
    if has_perm(user, "record.pay") or has_perm(user, "record.view_dept"):
        tabs.append("collect")
    tabs += ["revise", "mine"]
    return tuple(tabs)


def todo_counts(user):
    todos = build_todos(user)
    tabs = visible_tabs(user)
    return {
        "receipt": len(todos["receipt"]),
        "collect": len(todos["collect"]) if "collect" in tabs else 0,
        "revise": len(todos["revise"]),
        "mine": len(todos["mine"]),
        # 导航红点只算「等我动作」的：待我收款 + 待我修改 + 需要我跟进的收款
        "total": sum(len(todos[k]) for k in tabs if k in ("receipt", "collect", "revise")),
    }


@bp.route("/")
@login_required
def index():
    todos = build_todos(g.user)
    tabs = visible_tabs(g.user)
    tab = request.args.get("tab")
    if tab not in tabs:
        tab = next((k for k in tabs if todos[k]), tabs[0])
    return render_template(
        "todos.html",
        todos=todos, meta=TODO_META, tab_order=tabs, active_tab=tab,
        counts={k: len(v) for k, v in todos.items()},
    )
