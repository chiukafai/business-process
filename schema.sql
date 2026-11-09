-- 业务登记协同系统 · 数据库结构
-- 金额字段统一以「分」为单位存整数(cents)，避免浮点误差。展示与导出时除以 100。
-- 日期字段统一存 'YYYY-MM-DD' 文本，便于 SQLite 比较与排序。

PRAGMA foreign_keys = ON;

-- 公司（全国各地的经营主体）。表名沿用 departments 以免历史数据迁移，
-- 语义上等同于「公司」，业务登记表的 dept_id 即归属公司。
CREATE TABLE IF NOT EXISTS departments (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  name        TEXT    NOT NULL UNIQUE,
  short_name  TEXT,
  sort_order  INTEGER NOT NULL DEFAULT 100,
  is_active   INTEGER NOT NULL DEFAULT 1,
  created_at  TEXT    NOT NULL DEFAULT (datetime('now','localtime'))
);

-- 人员与权限
-- permissions 存 JSON 数组文本，例如 ["record.create","record.pay"]
-- role 只是标签，真实判定看 permissions（admin 短路放行）
CREATE TABLE IF NOT EXISTS users (
  id                   INTEGER PRIMARY KEY AUTOINCREMENT,
  username             TEXT    NOT NULL UNIQUE,
  password_hash        TEXT    NOT NULL,
  name                 TEXT    NOT NULL,
  phone                TEXT,
  dept_id              INTEGER REFERENCES departments(id),
  role                 TEXT    NOT NULL DEFAULT 'staff'
                       CHECK (role IN ('staff','manager','finance','admin')),
  permissions          TEXT    NOT NULL DEFAULT '[]',
  wecom_userid         TEXT,
  is_active            INTEGER NOT NULL DEFAULT 1,
  must_change_password INTEGER NOT NULL DEFAULT 1,
  created_at           TEXT    NOT NULL DEFAULT (datetime('now','localtime')),
  last_login_at        TEXT
);

-- 权限码字典（后台勾选框的数据源）
CREATE TABLE IF NOT EXISTS permission_catalog (
  code        TEXT PRIMARY KEY,
  label       TEXT NOT NULL,
  group_name  TEXT NOT NULL,
  role_default TEXT NOT NULL DEFAULT '[]',
  sort_order  INTEGER NOT NULL DEFAULT 100
);

-- 客户档案
CREATE TABLE IF NOT EXISTS customers (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  name          TEXT    NOT NULL UNIQUE,   -- 客户全称
  short_name    TEXT,                      -- 简称，便于搜索
  contact       TEXT,                      -- 联系人
  phone         TEXT,
  tax_no        TEXT,                      -- 纳税人识别号（开票用）
  bank_name     TEXT,                      -- 开户行
  bank_account  TEXT,                      -- 银行账号
  address       TEXT,
  dept_id       INTEGER REFERENCES departments(id),  -- 归属部门
  invoice_quota_cents INTEGER NOT NULL DEFAULT 0,    -- 发票额度（分）
  remark        TEXT,
  is_active     INTEGER NOT NULL DEFAULT 1,
  created_by    TEXT,
  created_at    TEXT NOT NULL DEFAULT (datetime('now','localtime')),
  updated_at    TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);

-- 业务登记主表
CREATE TABLE IF NOT EXISTS records (
  id                   INTEGER PRIMARY KEY AUTOINCREMENT,

  -- 流程字段：草稿 → 待收款 → 已收齐；可退回，业务员可撤回。
  -- 注：pending_payment 这个状态码沿用历史命名，当前语义是「待收款」，
  --     不改码是为了避开 SQLite 无法修改 CHECK 约束的坑（改码要重建整表）。
  status               TEXT    NOT NULL DEFAULT 'draft'
                       CHECK (status IN ('draft','pending_payment','completed','rejected')),
  owner_id             INTEGER NOT NULL REFERENCES users(id),
  owner_name           TEXT,
  -- 归属部门：导入时若客户档案里该主体公司没挂部门，先留空（待确认），
  -- 确认后才能提交进入收款 —— 所以这里不能用 NOT NULL
  dept_id              INTEGER REFERENCES departments(id),

  -- 模板 26 列（第 1 列「序号」= id）
  biz_type             TEXT,                       -- 2 类型
  prepaid_mode         TEXT NOT NULL DEFAULT '后付' -- 3 预存与否
                       CHECK (prepaid_mode IN ('预存','后付','月结')),
  biz_date             TEXT,                       -- 4 时间
  report_plan_date     TEXT,                       -- 5 报送计划时间
  issue_plan_date      TEXT,                       -- 6 计划出具日期
  is_invoiced          INTEGER NOT NULL DEFAULT 0, -- 7 是否已开票（收齐后由财务确认）
  invoice_needed       INTEGER,                    -- 是否需要开票：登记时选 0=不要 / 1=要；NULL=历史的待确认
  customer_id          INTEGER REFERENCES customers(id), -- 客户档案
  customer_name        TEXT    NOT NULL,           -- 8 客户名称（快照，导出用）
  upstream_company     TEXT,                       -- 上游公司（供应商，C 列）
  fee_type             TEXT,                       -- 9 费用类型（旧字段，已改为下方四项分列）
  fee_service_cents    INTEGER NOT NULL DEFAULT 0, -- 费用明细：渠道提成（原「服务费」，付给渠道）
  fee_overnight_cents  INTEGER NOT NULL DEFAULT 0, -- 费用明细：资金过夜费（原「通道费」）
  fee_finance_cents    INTEGER NOT NULL DEFAULT 0, -- 费用明细：财务费用
  fee_other_cents      INTEGER NOT NULL DEFAULT 0, -- 费用明细：其他费用
  biz_amount_cents     INTEGER NOT NULL DEFAULT 0, -- 10 业务金额
  fee_rate             REAL    NOT NULL DEFAULT 0, -- 11 费率（填 1.5 表示 1.5%）
  channel_commission_cents INTEGER NOT NULL DEFAULT 0, -- 12 留存利润（贸易计划 K 列合计，原「渠道提成」）
  company_income_cents INTEGER NOT NULL DEFAULT 0, -- 13 公司实收（= 累计已收，实际到账）
  aux_fee_cents        INTEGER NOT NULL DEFAULT 0, -- 14 辅助核算费用（已停用，仅保留历史值）
  payable_amount_cents INTEGER NOT NULL DEFAULT 0, -- 15 贸易费（= 业务金额 × 费率，公式锁定）
  prepaid_amount_cents INTEGER NOT NULL DEFAULT 0, -- 16 预存金额
  paid_amount_cents    INTEGER NOT NULL DEFAULT 0, -- 17 付款金额（历史列，现由 received_amount_cents 取代）
  unpaid_amount_cents  INTEGER NOT NULL DEFAULT 0, -- 18 待收金额（自动算：贸易费 + 留存利润 − 预存 − 已收）
  paid_date            TEXT,                       -- 19 打款日期
  receive_account      TEXT,                       -- 20 收款账户
  receive_bank         TEXT,                       -- 21 收款银行
  pay_account          TEXT,                       -- 22 付款账户
  pay_bank             TEXT,                       -- 23 付款银行
  remark               TEXT,                       -- 24 备注
  receipt_no           TEXT,                       -- 25 回单编号

  -- 回单附件与识别
  receipt_file         TEXT,                       -- 回单附件相对路径
  receipt_ocr_text     TEXT,                       -- OCR 原始识别文本
  receipt_ocr_at       TEXT,                       -- 识别时间

  -- 收款（业务登记后向客户收取款项，可分批）
  received_amount_cents INTEGER NOT NULL DEFAULT 0, -- 累计已收（分）
  receipt_closed       INTEGER NOT NULL DEFAULT 0,  -- 收款已结清（登记时填 0 或收满自动置 1）
  receipt_closed_at    TEXT,                        -- 收款结清时间
  fee_registered_at    TEXT,                        -- 费用登记时间
  fee_registered_by    TEXT,                        -- 费用登记人

  -- 流转审计
  submitted_at         TEXT,
  paid_confirmed_by    TEXT,
  paid_confirmed_at    TEXT,
  reject_reason        TEXT,

  created_at           TEXT NOT NULL DEFAULT (datetime('now','localtime')),
  updated_at           TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);

-- 收款流水：一笔业务可分多次收客户款，每次登记一条。
-- amount_cents = 0 且 is_close = 1 表示「本次不再收款，收款已结清」（表单填 0 的场景）。
CREATE TABLE IF NOT EXISTS record_receipts (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  record_id     INTEGER NOT NULL REFERENCES records(id) ON DELETE CASCADE,
  amount_cents  INTEGER NOT NULL DEFAULT 0,
  received_date TEXT,
  receipt_no    TEXT,
  note          TEXT,
  is_close      INTEGER NOT NULL DEFAULT 0,
  actor_id      INTEGER,
  actor_name    TEXT,
  created_at    TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);

-- 流转日志（时间轴 / 操作日志）
CREATE TABLE IF NOT EXISTS record_logs (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  record_id   INTEGER NOT NULL REFERENCES records(id) ON DELETE CASCADE,
  action      TEXT    NOT NULL,
  from_status TEXT,
  to_status   TEXT,
  actor_id    INTEGER,
  actor_name  TEXT,
  note        TEXT,
  created_at  TEXT    NOT NULL DEFAULT (datetime('now','localtime'))
);

-- 系统设置（企业微信凭据等键值对）
CREATE TABLE IF NOT EXISTS app_settings (
  key         TEXT PRIMARY KEY,
  value       TEXT,
  updated_at  TEXT NOT NULL DEFAULT (datetime('now','localtime')),
  updated_by  TEXT
);

CREATE INDEX IF NOT EXISTS idx_records_status    ON records(status, dept_id);
CREATE INDEX IF NOT EXISTS idx_records_owner     ON records(owner_id, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_records_bizdate   ON records(biz_date DESC);
CREATE INDEX IF NOT EXISTS idx_records_customer  ON records(customer_name);
CREATE INDEX IF NOT EXISTS idx_records_receipt   ON records(receipt_no);
CREATE INDEX IF NOT EXISTS idx_logs_record       ON record_logs(record_id, id);
CREATE INDEX IF NOT EXISTS idx_logs_created      ON record_logs(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_customers_active  ON customers(is_active, name);
