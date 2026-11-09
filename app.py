"""业务登记协同系统 · 应用入口

运行（本机）：
    python app.py            → http://127.0.0.1:5000
    flask --app app init-db  → 首次建库

部署（NAS / 服务器）：
    docker compose up -d     → 监听 $PORT（默认 5000），由 Cloudflare 隧道转发
"""
import os
import secrets

from flask import Flask, g, redirect, url_for, session

import auth as auth_module
import db as db_module
import utils

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.environ.get("DATA_DIR") or os.path.join(BASE_DIR, "data")


def _load_secret_key():
    """密钥持久化到数据目录，避免每次重启导致所有人掉登录。"""
    os.makedirs(DATA_DIR, exist_ok=True)
    path = os.path.join(DATA_DIR, "secret.key")
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            value = f.read().strip()
        if value:
            return value
    value = secrets.token_hex(32)
    with open(path, "w", encoding="utf-8") as f:
        f.write(value)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return value


def create_app():
    app = Flask(__name__, instance_relative_config=False)
    app.config.update(
        SECRET_KEY=os.environ.get("SECRET_KEY") or _load_secret_key(),
        DATABASE=os.path.join(DATA_DIR, "app.db"),
        RECEIPT_DIR=os.path.join(DATA_DIR, "receipts"),
        MAX_CONTENT_LENGTH=16 * 1024 * 1024,      # 上传上限 16MB
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=os.environ.get("COOKIE_SECURE", "0") == "1",
        PERMANENT_SESSION_LIFETIME=60 * 60 * 24 * 14,  # 记住登录 14 天
        JSON_AS_ASCII=False,
    )

    os.makedirs(DATA_DIR, exist_ok=True)
    os.makedirs(app.config["RECEIPT_DIR"], exist_ok=True)

    # 数据库
    from db import get_db, init_app as init_db_app, init_db
    init_db_app(app)

    # 首次启动自动建表（幂等）
    with app.app_context():
        init_db()

    # 蓝图
    from views.dashboard import bp as dashboard_bp
    from views.records import bp as records_bp
    from views.todos import bp as todos_bp
    from views.admin import bp as admin_bp
    from views.excel_io import bp as excel_bp
    from views.customers import bp as customers_bp

    app.register_blueprint(auth_module.bp)
    app.register_blueprint(dashboard_bp)
    app.register_blueprint(records_bp)
    app.register_blueprint(todos_bp)
    app.register_blueprint(admin_bp)
    app.register_blueprint(excel_bp)
    app.register_blueprint(customers_bp)

    # 蓝图的根路径映射到登录/看板
    @app.route("/")
    def home():
        if g.user is None:
            return redirect(url_for("auth.login"))
        return redirect(url_for("dashboard.index"))

    # 每个请求先加载当前用户
    app.before_request(auth_module.load_logged_in_user)

    # 模板全局变量与过滤器
    def _todo_badge():
        """导航红点数字：每个请求只算一次。"""
        if "badge_cache" not in g:
            if g.get("user"):
                from views.todos import todo_counts
                g.badge_cache = todo_counts(g.user)
            else:
                g.badge_cache = {"total": 0, "receipt": 0, "receipt_doing": 0,
                                 "receipt_label": "收款进行中", "collect": 0,
                                 "invoice": 0, "revise": 0, "mine": 0}
        return g.badge_cache

    @app.context_processor
    def inject_globals():
        perm_labels = {}
        if g.get("user"):
            for row in db_module.get_db().execute(
                "SELECT code, label FROM permission_catalog"
            ):
                perm_labels[row["code"]] = row["label"]
        return {
            "current_user": g.get("user"),
            "can": auth_module.can,
            "can_delete_record": auth_module.can_delete_record,
            "PERM_LABELS": perm_labels,
            "todo_badge": _todo_badge(),
            "STATUS_META": utils.STATUS_META,
            "STATUS_ORDER": utils.STATUS_ORDER,
            "ROLE_LABELS": utils.ROLE_LABELS,
            "PREPAID_MODES": utils.PREPAID_MODES,
            "FEE_TYPES": utils.FEE_TYPES,
            "fee_total": utils.fee_total_cents,
            "fee_items": utils.fee_items,
            "status_label": utils.status_label,
            "invoice_status": utils.invoice_status,
            "money": utils.money,
            "to_yuan_text": utils.to_yuan_text,
            "today_str": utils.today(),
            "this_month": utils.month_value(utils.today()),
        }

    @app.template_filter("money")
    def _money_filter(cents):
        return utils.money(cents)

    @app.template_filter("yuan")
    def _yuan_filter(cents):
        return utils.to_yuan_text(cents)

    @app.template_filter("shortdt")
    def _shortdt(value):
        return utils.short_datetime(value)

    @app.template_filter("monthlabel")
    def _monthlabel(value):
        return utils.month_label(value)

    @app.template_filter("monthval")
    def _monthval(value):
        return utils.month_value(value)

    @app.template_filter("waited")
    def _waited(value):
        return utils.waited_text(value)

    @app.template_filter("stale")
    def _stale(value):
        return utils.is_stale(value, 24)

    return app


app = create_app()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    debug = os.environ.get("FLASK_DEBUG", "1") == "1"
    app.run(host="0.0.0.0", port=port, debug=debug)
