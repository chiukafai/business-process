"""我的待办：按权限与状态实时计算，不落表。

流程：登记 → 报批中 → 提交进「待收款」→ **资管确认收款**（业务员这一步不被提醒）
      → 每一轮收款确认后回头通知业务员 → 收齐「已收齐」→ 财务登记开票与费用。

所以「待收款」是资管的活，业务员只在收款到账后才收到通知。
"""
from flask import Blueprint, g, render_template, request

from auth import has_perm, login_required, visible_scope
from db import get_db
from utils import CUSTOMER_LABEL_SQL
from workflow import is_receipt_staff

bp = Blueprint("todos", __name__, url_prefix="/todos")

TODO_META = {
    "receipt": {"label": "待我确认收款", "hint": "资管待确认的收款，确认后会自动通知业务员",
                "action_label": "去确认"},
    "collect": {"label": "待收款跟进", "hint": "本部门还没收齐的业务，可协助登记收款",
                "action_label": "去收款"},
    "revise":  {"label": "待我修改", "hint": "被退回，需要你修改后重新提交",
                "action_label": "去修改"},
    "mine":    {"label": "我的报批中", "hint": "还没提交、或待确认归属部门的业务",
                "action_label": "继续登记"},
}

# 业务员视角：这里只放「资管已经确认到账」的进度，不作为待办提醒（不计红点）
META_OWNER_RECEIPT = {
    "label": "收款进行中",
    "hint": "资管已确认到账的收款，只作进度查看，不需要你操作",
    "action_label": "查看",
}

TAB_ORDER = ("receipt", "collect", "revise", "mine")


def _dept_clause(user, alias="r"):
    scope, value = visible_scope(user)
    if scope == "all":
        return "1=1", []
    if scope == "dept":
        return f"{alias}.dept_id = ?", [value]
    return "1=0", []


def receipt_mode(user):
    """当前用户看到的「收款」tab 口径。

    - staff：资管人员，看全公司待确认收款（要他们去确认）
    - owner：业务员，只看自己那些「已经收到部分款」的进度
    - None ：两者都不是，不显示该 tab
    """
    if not user:
        return None
    if is_receipt_staff(db=get_db(), user=user):
        return "staff"
    if has_perm(user, "record.create"):
        return "owner"
    return None


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

    mode = receipt_mode(user)
    if mode == "staff":
        # 资管的活：所有待收款业务，不分部门（哪里有钱要确认都得看得见）
        result["receipt"] = db.execute(
            base.format(cond="r.status = 'pending_payment'")
        ).fetchall()
    elif mode == "owner":
        # 业务员：只显示资管已经确认到账的（还没到过账的一律不打扰）
        result["receipt"] = db.execute(
            base.format(
                cond="r.status = 'pending_payment' AND r.owner_id = ? "
                     "AND r.received_amount_cents > 0"
            ),
            [user["id"]],
        ).fetchall()

    # 待收款跟进：给主管看本部门进度。资管自己已经有一份「待我确认收款」全量清单，
    # 再给一份会重复，所以资管不显示这个 tab。
    if mode != "staff" and _can_collect(user):
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


def _can_collect(user):
    return (has_perm(user, "record.pay") or has_perm(user, "record.view_dept")
            or has_perm(user, "record.view_all"))


def visible_tabs(user):
    """当前用户该看到的 tab 顺序：没权限的分类不显示。"""
    mode = receipt_mode(user)
    tabs = []
    if mode:
        tabs.append("receipt")
    if mode != "staff" and _can_collect(user):
        tabs.append("collect")
    tabs += ["revise", "mine"]
    return tuple(tabs)


def tab_meta(user):
    """tab 文案：资管看「待我确认收款」，业务员看「收款进行中」。"""
    meta = dict(TODO_META)
    if receipt_mode(user) == "owner":
        meta["receipt"] = META_OWNER_RECEIPT
    return meta


def todo_counts(user):
    todos = build_todos(user)
    tabs = visible_tabs(user)
    mode = receipt_mode(user)
    receipt_todo = len(todos["receipt"]) if mode == "staff" else 0
    return {
        # receipt 只统计「等我确认」的量；业务员的到账进度单独放 receipt_doing
        "receipt": receipt_todo,
        "receipt_doing": len(todos["receipt"]) if mode == "owner" else 0,
        "receipt_label": "待我确认收款" if mode == "staff" else "收款进行中",
        "collect": len(todos["collect"]) if "collect" in tabs else 0,
        "revise": len(todos["revise"]),
        "mine": len(todos["mine"]),
        # 导航红点只算「等我动作」的：资管待确认 + 待我修改 + 需要我跟进的收款。
        # 业务员的「收款进行中」只是进度，不计红点（到账由企业微信另行通知）。
        "total": (
            receipt_todo
            + (len(todos["collect"]) if "collect" in tabs else 0)
            + len(todos["revise"])
        ),
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
        todos=todos, meta=tab_meta(g.user), tab_order=tabs, active_tab=tab,
        counts={k: len(todos[k]) for k in tabs},
    )
