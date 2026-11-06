"""看板首页。"""
from datetime import date

from flask import Blueprint, g, render_template

from auth import has_perm, login_required, visible_scope
from db import get_db
from utils import STATUS_META, STATUS_ORDER
from views.todos import build_todos, todo_counts

bp = Blueprint("dashboard", __name__)


def _scope_clause(user):
    scope, value = visible_scope(user)
    if scope == "all":
        return "1=1", []
    if scope == "dept":
        return "r.dept_id = ?", [value]
    return "r.owner_id = ?", [user["id"]]


@bp.route("/dashboard")
@login_required
def index():
    db = get_db()
    clause, params = _scope_clause(g.user)
    month = date.today().strftime("%Y-%m")

    stats = db.execute(
        f"""SELECT COUNT(*) c,
                   COALESCE(SUM(r.biz_amount_cents),0)     biz,
                   COALESCE(SUM(r.company_income_cents),0) income,
                   COALESCE(SUM(r.unpaid_amount_cents),0)  unpaid
              FROM records r WHERE {clause}""",
        params,
    ).fetchone()

    month_stats = db.execute(
        f"""SELECT COUNT(*) c,
                   COALESCE(SUM(r.biz_amount_cents),0)     biz,
                   COALESCE(SUM(r.company_income_cents),0) income
              FROM records r
             WHERE {clause} AND substr(r.biz_date,1,7) = ?""",
        params + [month],
    ).fetchone()

    dist_rows = db.execute(
        f"SELECT r.status, COUNT(*) c FROM records r WHERE {clause} GROUP BY r.status",
        params,
    ).fetchall()
    dist = {s: 0 for s in STATUS_ORDER}
    for row in dist_rows:
        # 历史遗留状态并入待收款
        key = row["status"]
        if key in ("in_progress", "pending_approval"):
            key = "pending_payment"
        dist[key] = dist.get(key, 0) + row["c"]
    dist_total = sum(dist.values()) or 1

    counts = todo_counts(g.user)

    return render_template(
        "dashboard.html",
        stats=stats, month_stats=month_stats, month=month,
        dist=dist, dist_total=dist_total, counts=counts,
    )
