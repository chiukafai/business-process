"""银行回单识别（本地 tesseract，不联网、不外传）。

用法：
    text, err = recognize(path)
    fields = parse_fields(text)     # {'receipt_no': ..., 'amount': ..., 'date': ...}

设计取舍：识别结果只作为「建议值」回填到界面，由人确认后保存，
不直接写库 —— 银行回单版式差异大，静默写入会把错账带进财务数据。
"""
import os
import re
import shutil

IMG_EXTS = (".png", ".jpg", ".jpeg", ".bmp", ".webp", ".tif", ".tiff")
PDF_EXTS = (".pdf",)
ALLOWED_EXTS = IMG_EXTS + PDF_EXTS


def tesseract_cmd():
    """返回可用的 tesseract 可执行文件路径，找不到返回 None。"""
    env = os.environ.get("TESSERACT_CMD")
    if env and os.path.exists(env):
        return env
    found = shutil.which("tesseract")
    if found:
        return found
    for path in (
        r"C:\Program Files\Tesseract-OCR\tesseract.exe",
        r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe",
        "/usr/bin/tesseract",
        "/usr/local/bin/tesseract",
    ):
        if os.path.exists(path):
            return path
    return None


def status():
    """给设置页/详情页显示的可用性说明。"""
    cmd = tesseract_cmd()
    if not cmd:
        return {"ok": False, "reason": "未安装 tesseract，无法识别回单", "cmd": ""}
    try:
        import PIL  # noqa: F401
        import pytesseract  # noqa: F401
    except ImportError as exc:
        return {"ok": False, "reason": f"缺少 Python 依赖：{exc.name}", "cmd": cmd}
    return {"ok": True, "reason": "", "cmd": cmd}


def _preprocess(path):
    """灰度 + 放大到约 2200px 宽，提升小字识别率。"""
    from PIL import Image, ImageOps

    img = Image.open(path)
    if img.mode not in ("L", "RGB"):
        img = img.convert("RGB")
    img = ImageOps.exif_transpose(img)
    img = img.convert("L")
    w, h = img.size
    if w < 1600:
        scale = 1600 / float(w)
        img = img.resize((int(w * scale), int(h * scale)), Image.LANCZOS)
    return img


def recognize(path):
    """返回 (文本, 错误说明)。错误时文本为空字符串。"""
    import pytesseract

    cmd = tesseract_cmd()
    if cmd:
        pytesseract.pytesseract.tesseract_cmd = cmd

    ext = os.path.splitext(path)[1].lower()
    if ext in PDF_EXTS:
        return "", "PDF 暂不支持识别，请把回单页另存为图片（PNG/JPG）后上传"
    if ext not in IMG_EXTS:
        return "", "不支持的文件格式"

    try:
        img = _preprocess(path)
        # psm 6：按单个文本块识别，适合票据
        text = pytesseract.image_to_string(img, lang="chi_sim+eng", config="--psm 6")
    except Exception as exc:
        return "", f"识别失败：{exc}"
    return text, ""


# ---------- 字段抽取 ----------

RE_RECEIPT_NO = re.compile(
    r"(?:回单编号|回单号|流水号|交易流水号|凭证号|业务编号|参考号)[:：\s]*([A-Za-z0-9\-]{6,40})"
)
RE_AMOUNT = re.compile(
    r"(?:小写金额|金额|人民币|交易金额|付款金额|合计)[^0-9]{0,8}([0-9][0-9,]*\.\d{2})"
)
RE_AMOUNT_ALT = re.compile(r"[¥￥]\s*([0-9][0-9,]*\.\d{2})")
RE_DATE = re.compile(r"(20\d{2})\s*[年\-/.]\s*(\d{1,2})\s*[月\-/.]\s*(\d{1,2})")
RE_ACCOUNT = re.compile(r"\b(\d{12,25})\b")


def _clean_digits(text):
    """OCR 常见混淆：O→0，l/I→1，S→5（只在纯数字串里替换）。"""
    table = str.maketrans({"O": "0", "o": "0", "l": "1", "I": "1", "S": "5", "B": "8"})
    return text.translate(table)


def parse_fields(text):
    """从 OCR 文本里抽关键字段，抽不到就不返回该键。"""
    if not text:
        return {}
    flat = text.replace("\u3000", " ")
    out = {}

    m = RE_RECEIPT_NO.search(flat)
    if m:
        out["receipt_no"] = m.group(1).strip("-").strip()

    m = RE_AMOUNT.search(flat) or RE_AMOUNT_ALT.search(flat)
    if m:
        amount = m.group(1).replace(",", "")
        try:
            float(amount)
            out["amount"] = amount
        except ValueError:
            pass

    m = RE_DATE.search(flat)
    if m:
        y, mo, d = m.groups()
        try:
            out["date"] = f"{int(y):04d}-{int(mo):02d}-{int(d):02d}"
        except ValueError:
            pass

    accounts = RE_ACCOUNT.findall(flat)
    if accounts:
        # 取最长的数字串作为付款账号候选
        best = max(accounts, key=len)
        out["account"] = _clean_digits(best)

    return out
