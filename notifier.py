"""企业微信通知（自建应用）。

设计要点：
1. 所有对外请求都包在 try/except 里，通知失败绝不影响主流程落库。
2. access_token 缓存在 app_settings 表，提前 5 分钟过期，避免频繁取票触发限流。
3. 收件人取 users.wecom_userid；没填的人跳过，不报错。
"""
import json
import time
import urllib.parse
import urllib.request

API = "https://qyapi.weixin.qq.com/cgi-bin"

SETTING_KEYS = ("wecom_enabled", "wecom_corp_id", "wecom_agent_id", "wecom_secret")


# ---------- 设置读写 ----------

def get_setting(db, key, default=""):
    row = db.execute("SELECT value FROM app_settings WHERE key = ?", (key,)).fetchone()
    return row["value"] if row and row["value"] is not None else default


def set_setting(db, key, value, actor=""):
    db.execute(
        """INSERT INTO app_settings (key, value, updated_at, updated_by)
           VALUES (?,?,datetime('now','localtime'),?)
           ON CONFLICT(key) DO UPDATE SET
             value = excluded.value, updated_at = excluded.updated_at,
             updated_by = excluded.updated_by""",
        (key, value, actor),
    )


def config(db):
    return {
        "enabled": get_setting(db, "wecom_enabled", "0") == "1",
        "corp_id": get_setting(db, "wecom_corp_id", ""),
        "agent_id": get_setting(db, "wecom_agent_id", ""),
        "secret": get_setting(db, "wecom_secret", ""),
    }


# ---------- 底层调用 ----------

def _post_json(url, payload, timeout=8):
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json; charset=utf-8"}
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _get_json(url, timeout=8):
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def access_token(db, force=False):
    """取 access_token，缓存到 app_settings。失败返回 (None, 错误说明)。"""
    cfg = config(db)
    if not cfg["corp_id"] or not cfg["secret"]:
        return None, "未配置企业微信 corpId / secret"

    if not force:
        cached = get_setting(db, "wecom_token", "")
        expire_at = get_setting(db, "wecom_token_expire", "0")
        if cached and expire_at.isdigit() and int(expire_at) > int(time.time()) + 300:
            return cached, ""

    url = (f"{API}/gettoken?corpid={urllib.parse.quote(cfg['corp_id'])}"
           f"&corpsecret={urllib.parse.quote(cfg['secret'])}")
    try:
        res = _get_json(url)
    except Exception as exc:  # 网络问题
        return None, f"获取 token 失败：{exc}"

    if res.get("errcode") != 0:
        return None, f"获取 token 失败：{res.get('errcode')} {res.get('errmsg')}"

    token = res.get("access_token")
    expires_in = int(res.get("expires_in") or 7200)
    set_setting(db, "wecom_token", token)
    set_setting(db, "wecom_token_expire", str(int(time.time()) + expires_in))
    return token, ""


def send_text(db, userids, content):
    """给一组企业微信 userid 发文本消息。返回 (成功数, 错误说明)。"""
    cfg = config(db)
    if not cfg["enabled"]:
        return 0, "企业微信通知未启用"
    ids = [u for u in (userids or []) if u]
    if not ids:
        return 0, "收件人都没填企业微信 userid"

    token, err = access_token(db)
    if not token:
        return 0, err

    url = f"{API}/message/send?access_token={urllib.parse.quote(token)}"
    payload = {
        "touser": "|".join(ids),
        "msgtype": "text",
        "agentid": int(cfg["agent_id"]) if cfg["agent_id"].isdigit() else cfg["agent_id"],
        "text": {"content": content},
        "safe": 0,
    }
    try:
        res = _post_json(url, payload)
    except Exception as exc:
        return 0, f"发送失败：{exc}"

    errcode = res.get("errcode")
    if errcode == 42001 or errcode == 40014:
        # token 失效，强制刷新重试一次
        token, err = access_token(db, force=True)
        if not token:
            return 0, err
        url = f"{API}/message/send?access_token={urllib.parse.quote(token)}"
        try:
            res = _post_json(url, payload)
        except Exception as exc:
            return 0, f"发送失败：{exc}"
        errcode = res.get("errcode")

    if errcode != 0:
        return 0, f"发送失败：{errcode} {res.get('errmsg')}"
    return len(ids), ""


# ---------- 业务触发 ----------

EVENT_TITLE = {
    "submit": "业务待确认收款",
    "receipt": "收款已确认",
    "reject": "业务被退回",
    "paid": "业务已收齐",
}


def _receipt_targets(db):
    """待收款由「资管」确认，所以提交后提醒资管，而不是登记人自己。"""
    from workflow import receipt_staff
    return [r["wecom_userid"] for r in receipt_staff(db) if r["wecom_userid"]]


def _invoice_targets(db):
    """收齐后需要开票的，提醒有开票权限的财务人员。"""
    from workflow import invoice_staff
    return [r["wecom_userid"] for r in invoice_staff(db) if r["wecom_userid"]]


def notify_next(db, record_id, event, extra=""):
    """流程节点变化时通知下一步负责人。任何异常都只记录不抛出。"""
    from utils import needs_invoice

    try:
        cfg = config(db)
        if not cfg["enabled"]:
            return
        rec = db.execute(
            """SELECT r.*, d.name AS dept_name, u.wecom_userid AS owner_wx
                 FROM records r
                 LEFT JOIN departments d ON d.id = r.dept_id
                 LEFT JOIN users u ON u.id = r.owner_id
                WHERE r.id = ?""",
            (record_id,),
        ).fetchone()
        if rec is None:
            return

        fee = int(rec["payable_amount_cents"] or 0) / 100
        retention = int(rec["channel_commission_cents"] or 0) / 100

        if event == "submit":
            # 待收款 = 资管确认款项，业务员这一步不提醒
            targets = _receipt_targets(db)
            tip = (f"业务已提交，待确认收款。应收合计 {fee + retention:,.2f} 元"
                   f"（贸易费 {fee:,.2f} + 留存利润 {retention:,.2f}），"
                   f"请确认客户款项是否到账。")
        elif event == "receipt":
            # 资管确认了一轮收款 → 回头告知登记该业务的业务员
            targets = [rec["owner_wx"]] if rec["owner_wx"] else []
            tip = extra or (f"收款已确认 {rec['received_amount_cents'] / 100:,.2f} 元，"
                            f"尚待收 {rec['unpaid_amount_cents'] / 100:,.2f} 元。")
        elif event == "paid":
            # 收齐后：通知登记人；需要开票的，同时轮转到财务
            need_inv = needs_invoice(rec)
            targets = ([rec["owner_wx"]] if rec["owner_wx"] else []) \
                + (_invoice_targets(db) if need_inv else [])
            tip = (f"款项已收齐，累计已收 {rec['received_amount_cents'] / 100:,.2f} 元。"
                   + ("需要开票，请财务确认开票。" if need_inv else "本单不需要开票，流程结束。"))
        else:
            targets = [rec["owner_wx"]] if rec["owner_wx"] else []
            tip = f"业务被退回：{extra or '未填写原因'}。修改后可重新提交。"

        targets = list(dict.fromkeys(t for t in targets if t))
        if not targets:
            return
        content = "\n".join([
            f"【{EVENT_TITLE.get(event, '业务提醒')}】",
            f"客户：{rec['customer_name']}",
            f"归属部门：{rec['dept_name'] or '-'}",
            tip,
        ])
        send_text(db, targets, content)
    except Exception:
        # 通知是辅助功能，失败不影响业务数据
        pass
