"""通用工具：金额（分为单位）、日期、状态标签、CSV/Excel 辅助。"""
import re
from datetime import date, datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

# ---------- 状态 ----------

STATUS_META = {
    # 语义：登记/导入后的初始态，等待确认归属部门并报批
    "draft":            {"label": "报批中", "color": "#0369a1", "bg": "#e0f2fe"},
    # 状态码沿用 pending_payment（避免 SQLite 改 CHECK 要重建表），语义已是「待收款」
    "pending_payment":  {"label": "待收款", "color": "#7c3aed", "bg": "#ede9fe"},
    "completed":        {"label": "已收齐", "color": "#15803d", "bg": "#dcfce7"},
    "rejected":         {"label": "已驳回", "color": "#b91c1c", "bg": "#fee2e2"},
    # 历史遗留状态，仅用于兜底展示（初始化时已迁移为 pending_payment）
    "in_progress":      {"label": "进行中", "color": "#2563eb", "bg": "#dbeafe"},
    "pending_approval": {"label": "待收款", "color": "#7c3aed", "bg": "#ede9fe"},
}

STATUS_ORDER = ["draft", "pending_payment", "completed", "rejected"]

# 列表中可选的筛选状态（与 STATUS_ORDER 一致）
STATUS_FILTERS = STATUS_ORDER

PREPAID_MODES = ["预存", "后付", "月结"]

# 费用明细：不再选「费用类型」，改为每一项单独填列（金额，分）
# (表单字段名, 中文名, 数据库列名)
# 注：第一项原叫「服务费」，现统一叫「渠道提成」（对外付给渠道的提成）；
#     金额核算里的「渠道提成」列已改名「留存利润」，两者不是同一件事。
FEE_TYPES = [
    ("fee_service",   "渠道提成",   "fee_service_cents"),
    ("fee_overnight", "资金过夜费", "fee_overnight_cents"),
    ("fee_finance",   "财务费用",   "fee_finance_cents"),
    ("fee_other",     "其他费用",   "fee_other_cents"),
]

# 旧「费用类型」文本里的历史叫法，统一到新名称
FEE_ALIASES = {"通道费": "资金过夜费", "服务费": "渠道提成"}

ROLE_LABELS = {
    "staff": "业务员",
    "manager": "主管",
    "finance": "财务",
    "admin": "管理员",
}


def status_label(code):
    return STATUS_META.get(code, {}).get("label", code or "")


# ---------- 流程阶段（展示口径） ----------
# 数据库 status 只有四态：draft / pending_payment / completed / rejected。
# 但「已收齐」之后还有开票环节：收齐待开票、已开票、无需开票，三者的 status 都是
# completed，只看 status 会全部显示成「已收齐」，看不出流程走到哪一步。
# 所以展示层再派生一层更细的「阶段」，页面标签、待办、导出一律用它。
STAGE_META = {
    "draft":           {"label": "报批中",         "color": "#0369a1", "bg": "#e0f2fe", "bd": "#bae6fd",
                        "hint": "已登记，等待提交进入收款"},
    "pending_payment": {"label": "待收款",         "color": "#6d28d9", "bg": "#ede9fe", "bd": "#ddd6fe",
                        "hint": "已提交，等待资管确认客户款项到账"},
    "collecting":      {"label": "收款中",         "color": "#b45309", "bg": "#fef3c7", "bd": "#fde68a",
                        "hint": "已收到部分款项，还未收齐"},
    "invoice_pending": {"label": "待开票",         "color": "#c2410c", "bg": "#ffedd5", "bd": "#fed7aa",
                        "hint": "款项已收齐，等待财务开票"},
    "invoiced":        {"label": "已开票",         "color": "#15803d", "bg": "#dcfce7", "bd": "#bbf7d0",
                        "hint": "款项收齐、发票已开出，流程结束"},
    "paid_no_invoice": {"label": "已收齐·无需开票", "color": "#0f766e", "bg": "#ccfbf1", "bd": "#99f6e4",
                        "hint": "款项收齐，登记时选了不需要开票，流程结束"},
    "rejected":        {"label": "已驳回",         "color": "#b91c1c", "bg": "#fee2e2", "bd": "#fecaca",
                        "hint": "被退回，修改后可重新提交"},
}

STAGE_ORDER = ["draft", "pending_payment", "collecting", "invoice_pending",
               "invoiced", "paid_no_invoice", "rejected"]

# 阶段 → SQL 条件。列表筛选、导出、仪表盘统计共用同一份，必须与 stage_of() 一一对应，
# 否则会出现「筛选出来的条数和标签对不上」。
STAGE_SQL = {
    "draft": "r.status = 'draft'",
    "pending_payment": ("r.status IN ('pending_payment','pending_approval') "
                        "AND COALESCE(r.received_amount_cents,0) "
                        "+ COALESCE(r.prepaid_amount_cents,0) = 0"),
    "collecting": ("r.status IN ('pending_payment','pending_approval') "
                   "AND COALESCE(r.received_amount_cents,0) "
                   "+ COALESCE(r.prepaid_amount_cents,0) > 0"),
    "invoice_pending": ("r.status = 'completed' AND COALESCE(r.invoice_needed,1) = 1 "
                        "AND COALESCE(r.is_invoiced,0) = 0"),
    "invoiced": "r.status = 'completed' AND COALESCE(r.is_invoiced,0) = 1",
    "paid_no_invoice": "r.status = 'completed' AND r.invoice_needed = 0",
    "rejected": "r.status = 'rejected'",
}

STAGE_FILTERS = STAGE_ORDER


def stage_of(rec):
    """把 status + 开票字段合成为一个展示阶段，返回 STAGE_META 副本 + code。"""
    status = _row_get(rec, "status", "") or ""
    if status in ("in_progress", "pending_approval"):
        status = "pending_payment"

    if status == "rejected":
        code = "rejected"
    elif status == "draft":
        code = "draft"
    elif status == "completed":
        needed = _row_get(rec, "invoice_needed", None)
        if needed is not None and not int(needed):
            # 登记时就选了「不要」，开票环节跳过
            code = "paid_no_invoice"
        elif int(_row_get(rec, "is_invoiced", 0) or 0):
            code = "invoiced"
        else:
            code = "invoice_pending"
    else:
        covered = (int(_row_get(rec, "received_amount_cents", 0) or 0)
                   + int(_row_get(rec, "prepaid_amount_cents", 0) or 0))
        code = "collecting" if covered > 0 else "pending_payment"

    meta = dict(STAGE_META[code])
    meta["code"] = code
    meta["status"] = status
    return meta


def stage_steps(rec):
    """详情页流程进度条：报批 → 收款 → 收齐 → 开票。

    返回 [{"key","label","note","state"}]，state ∈ done / current / todo / skip / reject。
    """
    code = stage_of(rec)["code"]
    steps = [
        {"key": "apply",   "label": "报批", "note": "登记 / 导入"},
        {"key": "collect", "label": "收款", "note": "资管确认到账"},
        {"key": "close",   "label": "收齐", "note": "应收三项收满"},
        {"key": "invoice", "label": "开票", "note": "财务确认开票"},
    ]
    idx = {"draft": 0, "rejected": 0, "pending_payment": 1, "collecting": 1,
           "invoice_pending": 3, "invoiced": 4, "paid_no_invoice": 4}.get(code, 0)
    for i, st in enumerate(steps):
        if i < idx:
            st["state"] = "done"
        elif i == idx:
            st["state"] = "current"
        else:
            st["state"] = "todo"

    if code == "rejected":
        steps[0]["state"] = "reject"
        steps[0]["note"] = "被退回，待修改"
    elif code == "collecting":
        steps[1]["note"] = "已收部分，继续追收"
    elif code == "paid_no_invoice":
        steps[3]["state"] = "skip"
        steps[3]["note"] = "无需开票"
    elif code == "invoice_pending":
        steps[2]["state"] = "done"
        steps[3]["state"] = "current"
    elif code == "invoiced":
        steps[3]["note"] = "已开出"
    return steps


# ---------- 金额 ----------

def to_cents(value):
    """把用户输入的金额（元，可为字符串）转成整数分。空值→0，非法→None。"""
    if value is None:
        return 0
    if isinstance(value, (int, float, Decimal)):
        raw = str(value)
    else:
        raw = str(value).strip()
    if raw == "":
        return 0
    # 去掉千分位、币种符号、单位
    for ch in [",", "，", "¥", "￥", "元", " ", "\u3000"]:
        raw = raw.replace(ch, "")
    if raw in ("", "-"):
        return 0
    try:
        d = Decimal(raw)
    except (InvalidOperation, ValueError):
        return None
    return int((d * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def to_yuan_text(cents):
    """整数分 → 两位小数字符串（不带千分位），供表单回填。"""
    if cents is None:
        return ""
    d = (Decimal(int(cents)) / 100).quantize(Decimal("0.01"))
    return f"{d:.2f}"


def money(cents):
    """整数分 → 带千分位的展示文本。"""
    if cents is None:
        return "-"
    d = (Decimal(int(cents)) / 100).quantize(Decimal("0.01"))
    return f"{d:,.2f}"


def to_rate(value):
    """费率：填 1.5 表示 1.5%。返回 float，非法返回 None。"""
    if value is None or str(value).strip() == "":
        return 0.0
    try:
        d = Decimal(str(value).strip().replace("%", ""))
    except (InvalidOperation, ValueError):
        return None
    return float(d)


def trade_fee_cents(biz_amount_cents, fee_rate):
    """贸易费 = 业务金额 × 费率（分）。四舍五入到分。"""
    amt = Decimal(int(biz_amount_cents or 0))
    rate = Decimal(str(fee_rate or 0))
    return int((amt * rate / Decimal(100)).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def calc_amounts(biz_amount_cents, fee_rate, retention_cents,
                 prepaid_amount_cents, received_amount_cents,
                 override_trade_fee=None, overnight_cents=0):
    """统一金额口径。所有入参与返回均为整数分。

    贸易费   = 业务金额 × 费率（四舍五入到分）
               override_trade_fee 非 None 时以手填值为准 —— 用于四舍五入差异、
               或与客户谈定的实际费用不一致时的人工修正
    留存利润 = 贸易计划 K 列（利润1）合计        ← 与贸易费彼此独立，互不扣减
    资金过夜费 = 费用明细里的「资金过夜费」       ← 计入应收（先按这三项判定收齐）
    应收合计 = 贸易费 + 留存利润 + 资金过夜费
    公司实收 = 累计已收（实际到账，由收款流水累计）
    待收     = 应收合计 − 预存 − 已收（允许负数，表示多收/预收）
    """
    formula = trade_fee_cents(biz_amount_cents, fee_rate)
    fee = formula if override_trade_fee is None else int(override_trade_fee)
    retention = int(retention_cents or 0)
    overnight = int(overnight_cents or 0)
    received = int(received_amount_cents or 0)
    receivable = fee + retention + overnight
    unpaid = (Decimal(receivable)
              - Decimal(int(prepaid_amount_cents or 0))
              - Decimal(received)).quantize(Decimal("1"), rounding=ROUND_HALF_UP)

    return {
        # 列名沿用 payable_amount_cents，语义已是「贸易费」
        "payable_amount_cents": int(fee),
        # 公司实收 = 实际到账，与累计已收保持一致
        "company_income_cents": received,
        "unpaid_amount_cents": int(unpaid),
    }


def _row_get(row, key, default=0):
    try:
        value = row[key]
    except (KeyError, IndexError, TypeError):
        return default
    return default if value is None else value


def receipt_progress(row):
    """收款进度（分）。应收合计 = 贸易费 + 留存利润 + 资金过夜费，含预存抵扣。

    返回 dict：trade_fee / retention / overnight / receivable / prepaid / received /
               outstanding / percent / closed
    """
    trade_fee = int(_row_get(row, "payable_amount_cents"))
    retention = int(_row_get(row, "channel_commission_cents"))
    overnight = int(_row_get(row, "fee_overnight_cents"))
    receivable = trade_fee + retention + overnight
    prepaid = int(_row_get(row, "prepaid_amount_cents"))
    received = int(_row_get(row, "received_amount_cents"))
    closed = bool(int(_row_get(row, "receipt_closed")))
    outstanding = receivable - prepaid - received

    covered = prepaid + received
    if receivable <= 0:
        percent = 100 if closed else 0
    else:
        percent = int(max(0, min(100, round(covered * 100 / receivable))))
    return {
        "trade_fee": trade_fee,
        "retention": retention,
        "overnight": overnight,
        "receivable": receivable,
        "prepaid": prepaid,
        "received": received,
        "outstanding": outstanding,
        "percent": percent,
        "closed": closed,
    }


# 收款分项：(表单字段, 标签, 流水列, 是否计入应收, records 上对应的应收列)
RECEIPT_ITEMS = [
    ("trade_fee", "贸易费",   "trade_fee_cents", True,  "payable_amount_cents"),
    ("retention", "留存利润", "retention_cents", True,  "channel_commission_cents"),
    ("overnight", "资金过夜费", "overnight_cents", True, "fee_overnight_cents"),
    ("channel",   "渠道提成", "channel_cents",   False, "fee_service_cents"),
    ("finance",   "财务费用", "finance_cents",   False, "fee_finance_cents"),
    ("other",     "其他费用", "other_cents",     False, "fee_other_cents"),
]


def _prepaid_alloc(prepaid_cents, needs):
    """预存按顺序抵扣各项应收（贸易费 → 留存利润 → 资金过夜费）。"""
    alloc, left = {}, int(prepaid_cents or 0)
    for key, need in needs:
        take = min(max(0, left), max(0, need))
        alloc[key] = take
        left -= take
    return alloc


def receipt_items(row, totals):
    """按分项算应收 / 已收 / 待收。

    totals: dict，流水列名 → 累计分（可由 SQL SUM 得到，缺省当 0）
    返回 list，每项：
      key / label / counts（是否计入应收判定）/ needed / received / prepaid / outstanding
    """
    needs = []
    for key, _label, _rc, counts, col in RECEIPT_ITEMS:
        if counts:
            needs.append((key, int(_row_get(row, col))))
    alloc = _prepaid_alloc(_row_get(row, "prepaid_amount_cents"), needs)

    items = []
    for key, label, rc, counts, col in RECEIPT_ITEMS:
        needed = int(_row_get(row, col))
        got = int(totals.get(rc, 0) or 0)
        offset = alloc.get(key, 0) if counts else 0
        items.append({
            "key": key,
            "label": label,
            "counts": counts,
            "needed": needed,
            "received": got,
            "prepaid": offset,
            "outstanding": (needed - got - offset) if counts else 0,
        })
    return items


def fee_total_cents(row):
    """费用明细四项合计（分）。row 可以是 sqlite Row / dict / 表单字典。"""
    total = 0
    for _key, _label, col in FEE_TYPES:
        try:
            total += int(row[col] or 0)
        except (KeyError, IndexError, TypeError, ValueError):
            continue
    return total


def fee_items(row):
    """[(中文名, 金额分)] ，供详情页/表单渲染。"""
    out = []
    for _key, label, col in FEE_TYPES:
        try:
            value = int(row[col] or 0)
        except (KeyError, IndexError, TypeError, ValueError):
            value = 0
        out.append((label, value))
    return out


# ---------- 名称清理 ----------

# 名称末尾的括号备注：粤丰果业（无票）→ 粤丰果业
# 括号内容若本身就是名称的一部分（含公司/集团/有限等字样），保留不动
_BRACKET_TAIL = re.compile(
    r"\s*[（(【\[｛{〔]\s*([^（）()【】\[\]｛｝{}〔〕]*?)\s*[）)】\]｝}〕]\s*$"
)
_TAIL_JUNK = re.compile(r"[\s、，,。;；:：·\-—_*#/|]+$")
_NAME_KEEP_WORDS = ("公司", "集团", "有限", "股份", "企业", "厂", "合作社")


def clean_company_name(value):
    """清掉名称末尾的括号备注与多余符号，得到原公司名称。

    「粤丰果业（无票）」→「粤丰果业」
    「沃田农艺园（无票）」→「沃田农艺园」
    「粤丰果业（集团）有限公司」保持不变（括号不在末尾）
    清理后不足 2 个字的，保留原值，避免误伤。
    """
    raw = str(value if value is not None else "").replace("\u3000", " ").strip()
    if not raw:
        return ""
    name = raw
    changed = True
    while changed:
        changed = False
        m = _BRACKET_TAIL.search(name)
        if m and not any(w in m.group(1) for w in _NAME_KEEP_WORDS):
            trimmed = name[:m.start()].strip()
            if len(trimmed) >= 2:
                name, changed = trimmed, True
                continue
        trimmed = _TAIL_JUNK.sub("", name)
        if trimmed != name and len(trimmed) >= 2:
            name, changed = trimmed, True
    return name


# ---------- 客户主体公司口径 ----------
# 业务模块对外一律叫「主体公司」，取值优先级：
#   客户档案的「主体公司」→ 客户档案的「客户名称」→ 业务里的历史快照
# SQL 片段：使用处必须 LEFT JOIN customers cu ON cu.id = r.customer_id
CUSTOMER_LABEL_SQL = (
    "COALESCE(NULLIF(TRIM(cu.short_name), ''), cu.name, r.customer_name) AS customer_label"
)

# 关键词搜索时也要走到主体公司，否则历史快照存的是全称就搜不到
CUSTOMER_SEARCH_SQL = (
    "COALESCE(NULLIF(TRIM(cu.short_name), ''), cu.name, r.customer_name) LIKE ?"
)


def customer_label(row, fallback=""):
    """Python 侧取主体公司：档案主体公司 → 档案客户名称 → 兜底值。"""
    if row is not None:
        try:
            short = (row["short_name"] or "").strip()
        except (KeyError, IndexError):
            short = ""
        if short:
            return short
        try:
            name = (row["name"] or "").strip()
        except (KeyError, IndexError):
            name = ""
        if name:
            return name
    return (fallback or "").strip()


# ---------- 开票 ----------

def invoice_status(rec):
    """开票两级状态：登记时选「要 / 不要」，收齐后由财务确认「已开票」。

    返回 {"label": 文案, "tone": ok|warn|muted}，模板直接渲染。
    """
    needed = _row_get(rec, "invoice_needed", None)
    if needed is None:
        return {"label": "待确认是否需要开票", "tone": "muted"}
    if not int(needed):
        return {"label": "不需要开票（已跳过开票环节）", "tone": "muted"}
    if int(_row_get(rec, "is_invoiced")):
        return {"label": "已开票", "tone": "ok"}
    return {"label": "需要开票 · 待开票", "tone": "warn"}


def needs_invoice(rec):
    """这笔业务是否还需要走开票环节（历史 NULL 视为需要，避免漏开票）。"""
    return int(_row_get(rec, "invoice_needed", 1) or 0) == 1


# ---------- 日期 ----------
def parse_date(value):
    """把各种输入统一成 'YYYY-MM-DD'，无法解析返回 None。"""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%d")
    if isinstance(value, date):
        return value.strftime("%Y-%m-%d")
    s = str(value).strip()
    if s == "":
        return None
    s = s.replace("/", "-").replace(".", "-")
    for fmt in ("%Y-%m-%d", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            return datetime.strptime(s, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return None


def today():
    return date.today().strftime("%Y-%m-%d")


# ---------- 月份 ----------

_MONTH_RE = re.compile(r"^(\d{4})\s*[-/年.]?\s*(\d{1,2})")


def parse_month(value):
    """把「2026-11」「2026/11」「2026年11月」「2026-11-05」统一成当月首日 'YYYY-MM-01'。

    无法解析返回 None。存储上仍沿用 biz_date 的 'YYYY-MM-DD' 格式，
    这样既有的日期比较、排序、统计逻辑都不用改。
    """
    if value is None:
        return None
    if isinstance(value, (datetime, date)):
        return value.strftime("%Y-%m-01")
    s = str(value).strip()
    if not s:
        return None
    m = _MONTH_RE.match(s)
    if not m:
        return None
    year, month = int(m.group(1)), int(m.group(2))
    if not (1 <= month <= 12) or not (1900 <= year <= 2200):
        return None
    return f"{year:04d}-{month:02d}-01"


def month_value(value):
    """'2026-11-05' / '2026-11-01' → '2026-11'，供 <input type="month"> 回填。"""
    if not value:
        return ""
    m = _MONTH_RE.match(str(value).strip())
    if not m:
        return ""
    month = int(m.group(2))
    if not (1 <= month <= 12):
        return ""
    return f"{int(m.group(1)):04d}-{month:02d}"


def month_label(value):
    """'2026-11-01' → '2026年11月'；日期不是 1 号（历史精确日期）时保留日。"""
    if not value:
        return ""
    s = str(value).strip()
    m = _MONTH_RE.match(s)
    if not m:
        return s
    year, month = int(m.group(1)), int(m.group(2))
    if not (1 <= month <= 12):
        return s
    if len(s) >= 10 and s[8:10].isdigit() and s[8:10] != "01":
        return f"{year}年{month}月{int(s[8:10])}日"
    return f"{year}年{month}月"


def month_bounds(value):
    """月份输入 → (当月首日, 当月末日)，用于列表按月筛选。"""
    first = parse_month(value)
    if not first:
        return None, None
    year, month = int(first[:4]), int(first[5:7])
    nxt = date(year + 1, 1, 1) if month == 12 else date(year, month + 1, 1)
    last = date.fromordinal(nxt.toordinal() - 1).strftime("%Y-%m-%d")
    return first, last


def now_text():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def short_datetime(value):
    """'2026-11-05 10:20:30' → '11-05 10:20'"""
    if not value:
        return ""
    s = str(value)
    return s[5:16] if len(s) >= 16 else s


def waited_text(value):
    """从某时间点到现在过了多久，用于待办提示。"""
    if not value:
        return ""
    try:
        dt = datetime.strptime(str(value)[:19], "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return ""
    hours = (datetime.now() - dt).total_seconds() / 3600
    if hours < 1:
        return f"{int(hours * 60)} 分钟"
    if hours < 24:
        return f"{int(hours)} 小时"
    return f"{int(hours / 24)} 天"


def is_stale(value, hours=24):
    if not value:
        return False
    try:
        dt = datetime.strptime(str(value)[:19], "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return False
    return (datetime.now() - dt).total_seconds() / 3600 > hours
