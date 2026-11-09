"""我的通知：流程节点变化时给下一步负责人留的站内提醒。

为什么要有这一层：企业微信没配置、或某个同事没填 userid 时，
外部推送一条也发不出去，用户就感觉「系统没提醒我」。
站内通知不依赖网络与配置，登录后在侧边栏红点上就能看到。
"""
from flask import Blueprint, g, redirect, render_template, url_for

from auth import login_required
from db import get_db
from utils import CUSTOMER_LABEL_SQL

bp = Blueprint("notifications", __name__, url_prefix="/notifications")

EVENT_LABELS = {
    "submit": "待确认收款",
    "receipt": "收款已确认",
    "paid": "已收齐",
    "reject": "被退回",
    "invoice": "开票",
}


def unread_count(user):
    """未读条数（侧边栏红点）。每个请求只查一次。"""
    if not user:
        return 0
    row = get_db().execute(
        "SELECT COUNT(*) AS c FROM notifications WHERE user_id = ? AND read_at IS NULL",
        (user["id"],),
    ).fetchone()
    return int(row["c"] or 0) if row else 0


@bp.route("/")
@login_required
def index():
    db = get_db()
    rows = db.execute(
        f"""SELECT n.*, r.status, r.customer_name, {CUSTOMER_LABEL_SQL}
              FROM notifications n
              LEFT JOIN records r ON r.id = n.record_id
              LEFT JOIN customers cu ON cu.id = r.customer_id
             WHERE n.user_id = ?
             ORDER BY n.id DESC
             LIMIT 300""",
        (g.user["id"],),
    ).fetchall()
    unread = sum(1 for r in rows if not r["read_at"])
    return render_template("notifications.html", rows=rows, unread=unread,
                           event_labels=EVENT_LABELS)


@bp.route("/<int:nid>/open")
@login_required
def open_item(nid):
    """点开一条通知：标已读，然后跳到对应业务。"""
    db = get_db()
    row = db.execute(
        "SELECT * FROM notifications WHERE id = ? AND user_id = ?",
        (nid, g.user["id"]),
    ).fetchone()
    if row is None:
        return redirect(url_for("notifications.index"))
    if not row["read_at"]:
        db.execute(
            "UPDATE notifications SET read_at = datetime('now','localtime') WHERE id = ?",
            (nid,),
        )
        db.commit()
    if row["record_id"]:
        return redirect(url_for("records.detail", record_id=row["record_id"]))
    return redirect(url_for("notifications.index"))


@bp.route("/read-all", methods=("POST",))
@login_required
def read_all():
    db = get_db()
    db.execute(
        "UPDATE notifications SET read_at = datetime('now','localtime') "
        "WHERE user_id = ? AND read_at IS NULL",
        (g.user["id"],),
    )
    db.commit()
    return redirect(url_for("notifications.index"))
