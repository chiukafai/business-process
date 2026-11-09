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
FEE_TYPES = [
    ("fee_service",   "服务费",     "fee_service_cents"),
    ("fee_overnight", "资金过夜费", "fee_overnight_cents"),
    ("fee_finance",   "财务费用",   "fee_finance_cents"),
    ("fee_other",     "其他费用",   "fee_other_cents"),
]

# 旧「费用类型」文本里的历史叫法，统一到新名称
FEE_ALIASES = {"通道费": "资金过夜费"}

ROLE_LABELS = {
    "staff": "业务员",
    "manager": "主管",
    "finance": "财务",
    "admin": "管理员",
}


def status_label(code):
    return STATUS_META.get(code, {}).get("label", code or "")


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


def calc_amounts(biz_amount_cents, fee_rate, channel_commission_cents,
                 payable_amount_cents, prepaid_amount_cents, received_amount_cents):
    """统一金额口径。所有入参与返回均为整数分。

    公司实收 = 业务金额 × 费率/100 − 渠道提成
    辅助核算费用 = 业务金额 − 公司实收        ← 待确认口径，改这里一处即可
    待收金额 = 应收 − 预存 − 已收（允许负数，表示多收/预收）
    """
    amt = Decimal(int(biz_amount_cents or 0))
    rate = Decimal(str(fee_rate or 0))
    comm = Decimal(int(channel_commission_cents or 0))

    income = (amt * rate / Decimal(100) - comm).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    aux = (amt - income).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    unpaid = (Decimal(int(payable_amount_cents or 0))
              - Decimal(int(prepaid_amount_cents or 0))
              - Decimal(int(received_amount_cents or 0))).quantize(Decimal("1"), rounding=ROUND_HALF_UP)

    return {
        "company_income_cents": int(income),
        "aux_fee_cents": int(aux),
        "unpaid_amount_cents": int(unpaid),
    }


def _row_get(row, key, default=0):
    try:
        value = row[key]
    except (KeyError, IndexError, TypeError):
        return default
    return default if value is None else value


def receipt_progress(row):
    """收款进度（分）。应收含预存抵扣，待收可为负数（多收）。

    返回 dict：receivable / prepaid / received / outstanding / percent / closed
    """
    receivable = int(_row_get(row, "payable_amount_cents"))
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
        "receivable": receivable,
        "prepaid": prepaid,
        "received": received,
        "outstanding": outstanding,
        "percent": percent,
        "closed": closed,
    }


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
    """开票两级状态：先确认「是否需要开票」，需要才跟踪「是否已开票」。

    返回 {"label": 文案, "tone": ok|warn|muted}，模板直接渲染。
    """
    keys = rec.keys() if hasattr(rec, "keys") else rec
    needed = rec["invoice_needed"] if "invoice_needed" in keys else None
    if needed is None:
        return {"label": "待确认是否需要开票", "tone": "muted"}
    if not needed:
        return {"label": "不需要开票", "tone": "muted"}
    if rec["is_invoiced"]:
        return {"label": "已开票", "tone": "ok"}
    return {"label": "需要开票 · 待开票", "tone": "warn"}


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
