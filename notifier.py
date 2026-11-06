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
    "submit": "业务待收款",
    "reject": "业务被退回",
    "paid": "业务已收齐",
}


def _recipients_for_pay(db, record):
    """需要付款的人：有付款权限，且能看这笔业务（全部部门 / 同一部门）。"""
    rows = db.execute(
        """SELECT name, wecom_userid, permissions, dept_id FROM users
            WHERE is_active = 1 AND wecom_userid IS NOT NULL AND TRIM(wecom_userid) <> ''"""
    ).fetchall()
    out = []
    for row in rows:
        try:
            perms = set(json.loads(row["permissions"] or "[]"))
        except ValueError:
            perms = set()
        if "record.pay" not in perms:
            continue
        if "record.view_all" in perms or row["dept_id"] == record["dept_id"]:
            out.append(row["wecom_userid"])
    return out


def notify_next(db, record_id, event, extra=""):
    """流程节点变化时通知下一步负责人。任何异常都只记录不抛出。"""
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

        if event == "submit":
            # 提交后下一步是业务员去收客户的钱，所以提醒登记人本人
            targets = [rec["owner_wx"]] if rec["owner_wx"] else []
            tip = (f"业务已进入待收款，应收 {rec['payable_amount_cents'] / 100:,.2f} 元，"
                   f"请跟进收取客户款项。")
        else:
            targets = [rec["owner_wx"]] if rec["owner_wx"] else []
            if event == "reject":
                tip = f"业务被退回：{extra or '未填写原因'}。修改后可重新提交。"
            else:
                tip = (f"款项已收齐，累计已收 {rec['received_amount_cents'] / 100:,.2f} 元，"
                       f"请及时登记费用。")

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
