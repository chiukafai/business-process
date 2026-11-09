"""流程角色判定：谁负责确认收款、谁能登记开票。

规则（单一来源，待办、通知、详情页都走这里）：

1. **收款确认 = 资管部门**
   待收款业务由资管人员确认款项是否到账，业务员在这一步不被提醒。
   收款确认部门可在「系统设置」里多选，默认取名字含「资管」的部门。
   资管确认每一轮收款后，系统再回头通知登记该业务的业务员。

2. **开票登记 = 有 record.invoice 权限的人（财务部）**
   「是否开票」不再在登记时填，改为收款到账后由财务登记。
"""
from auth import has_perm

SETTING_KEY = "receipt_confirm_depts"


def _setting(db, key, default=""):
    row = db.execute("SELECT value FROM app_settings WHERE key = ?", (key,)).fetchone()
    return row["value"] if row and row["value"] is not None else default


def receipt_dept_ids(db):
    """收款确认部门的 id 集合。

    优先取系统设置里选的部门；没配置（或配置全失效）时回退到
    名字含「资管」的部门，保证开箱即用。
    """
    raw = _setting(db, SETTING_KEY, "") or ""
    ids = []
    for part in raw.replace("，", ",").split(","):
        part = part.strip()
        if part.isdigit():
            ids.append(int(part))
    if ids:
        marks = ",".join("?" * len(ids))
        rows = db.execute(
            f"SELECT id FROM departments WHERE id IN ({marks})", ids
        ).fetchall()
        found = {r["id"] for r in rows}
        if found:
            return found
    rows = db.execute("SELECT id FROM departments WHERE name LIKE '%资管%'").fetchall()
    return {r["id"] for r in rows}


def is_receipt_staff(db, user):
    """当前用户是不是「确认收款」的人（资管 + 有收款权限）。

    管理员不参与日常提醒（待办里不出现），但仍可在业务详情里代操作。
    """
    if not user or user["role"] == "admin":
        return False
    if not has_perm(user, "record.pay"):
        return False
    return user["dept_id"] in receipt_dept_ids(db)


def receipt_staff(db):
    """收款确认部门里有收款权限的人员列表（通知用）。"""
    ids = receipt_dept_ids(db)
    if not ids:
        return []
    rows = db.execute(
        """SELECT id, name, dept_id, role, permissions, wecom_userid, is_active
             FROM users WHERE is_active = 1 ORDER BY id"""
    ).fetchall()
    return [
        r for r in rows
        if r["role"] != "admin" and r["dept_id"] in ids and has_perm(r, "record.pay")
    ]


def can_invoice(user):
    """能否登记「是否开票」（财务部）。"""
    return bool(user) and has_perm(user, "record.invoice")
