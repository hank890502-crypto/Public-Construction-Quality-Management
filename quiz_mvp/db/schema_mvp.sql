-- =====================================================================
-- 線上題庫與模擬考平台 — PostgreSQL 建表 SQL（MVP + 核心模型，含 RLS 範例）
-- 對應《題庫平台_資料模型與開發路線圖》第六～八章、第十一章
-- PostgreSQL 14+ ；以 psql 執行： psql -d yourdb -f schema_mvp.sql
--
-- 【MVP（第一階段）必需】群組 A/B 核心、C 全、entitlements 最小版、F 作答
-- 【第二階段以後】products/orders/coupons/b2b_deals、user_devices、
--   question_reports、announcements/notifications、classes/assignments 等
--   一併建立，欄位先預留，未來不需重做（本檔已全部含入）。
-- =====================================================================

-- gen_random_uuid() 於 PostgreSQL 13+ 已內建於核心，無需擴充；
-- 若使用 PG 12 以下，改為： CREATE EXTENSION IF NOT EXISTS pgcrypto;

-- ---------- 通用：updated_at 自動更新 ----------
CREATE OR REPLACE FUNCTION set_updated_at() RETURNS trigger AS $$
BEGIN NEW.updated_at = now(); RETURN NEW; END;
$$ LANGUAGE plpgsql;

-- =====================================================================
-- 列舉型別
-- =====================================================================
CREATE TYPE tenant_type      AS ENUM ('platform','org','whitelabel');
CREATE TYPE tenant_status    AS ENUM ('active','suspended','expired');
CREATE TYPE member_role      AS ENUM ('platform_admin','org_admin','teacher','org_student','consumer');
CREATE TYPE member_status    AS ENUM ('active','invited','removed');
CREATE TYPE user_status      AS ENUM ('active','disabled');
CREATE TYPE consent_doc      AS ENUM ('terms','privacy','refund','membership');
CREATE TYPE bank_status      AS ENUM ('active','archived');
CREATE TYPE version_status   AS ENUM ('draft','published','archived');
CREATE TYPE question_type    AS ENUM ('single','multiple','true_false','fill','group','image');
CREATE TYPE difficulty_level AS ENUM ('easy','medium','hard');
CREATE TYPE question_status  AS ENUM ('draft','pending','published','disputed','revised','retired');
CREATE TYPE product_type     AS ENUM ('single_bank','bundle','sub_month','sub_quarter','sub_year','org_license','addon','extend');
CREATE TYPE order_status     AS ENUM ('pending','paid','failed','refunded');
CREATE TYPE ent_subject      AS ENUM ('user','org');
CREATE TYPE ent_status       AS ENUM ('active','expired','revoked');
CREATE TYPE exam_mode        AS ENUM ('practice','full','mock','wrong_review','read');
CREATE TYPE exam_status      AS ENUM ('in_progress','submitted','expired');
CREATE TYPE report_reason    AS ENUM ('wrong_answer','unclear','wrong_option','law_updated','duplicate','image','explanation');
CREATE TYPE report_status    AS ENUM ('open','assigned','resolved','rejected');
CREATE TYPE notify_channel   AS ENUM ('inapp','email','sms','line');
CREATE TYPE coupon_type      AS ENUM ('percent','amount','free_trial');

-- =====================================================================
-- 群組 A：租戶與品牌
-- =====================================================================
CREATE TABLE tenants (
  id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  type        tenant_type   NOT NULL,
  name        text          NOT NULL,
  status      tenant_status NOT NULL DEFAULT 'active',
  created_at  timestamptz   NOT NULL DEFAULT now()
);

CREATE TABLE tenant_branding (
  tenant_id          uuid PRIMARY KEY REFERENCES tenants(id) ON DELETE CASCADE,
  brand_name         text,
  logo_url           text,
  primary_color      text,
  domain             text UNIQUE,
  login_image_url    text,
  support_info       text,
  terms_url          text,
  privacy_url        text,
  show_platform_brand boolean NOT NULL DEFAULT true
);

-- =====================================================================
-- 群組 B：使用者與權限（身分為全域；角色掛在 memberships）
-- =====================================================================
CREATE TABLE users (
  id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  email          text,            -- 唯一性以 lower(email) 索引處理（不分大小寫）
  phone          text,
  password_hash  text,
  google_sub     text UNIQUE,
  email_verified boolean NOT NULL DEFAULT false,
  phone_verified boolean NOT NULL DEFAULT false,
  status         user_status NOT NULL DEFAULT 'active',
  created_at     timestamptz NOT NULL DEFAULT now(),
  deleted_at     timestamptz,
  CONSTRAINT users_need_identifier CHECK (email IS NOT NULL OR phone IS NOT NULL OR google_sub IS NOT NULL)
);
CREATE UNIQUE INDEX users_email_uq ON users (lower(email)) WHERE email IS NOT NULL AND deleted_at IS NULL;
CREATE UNIQUE INDEX users_phone_uq ON users (phone) WHERE phone IS NOT NULL AND deleted_at IS NULL;

CREATE TABLE memberships (
  id         uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id    uuid NOT NULL REFERENCES users(id)   ON DELETE CASCADE,
  tenant_id  uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
  role       member_role   NOT NULL,
  status     member_status NOT NULL DEFAULT 'active',
  created_at timestamptz   NOT NULL DEFAULT now(),
  UNIQUE (user_id, tenant_id, role)
);
CREATE INDEX memberships_tenant_idx ON memberships (tenant_id);
CREATE INDEX memberships_user_idx   ON memberships (user_id);

CREATE TABLE user_devices (
  id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id     uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  device_info text,
  ip          inet,
  last_seen_at timestamptz NOT NULL DEFAULT now(),
  created_at  timestamptz  NOT NULL DEFAULT now()
);
CREATE INDEX user_devices_user_idx ON user_devices (user_id);

CREATE TABLE consents (
  id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id     uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  doc_type    consent_doc NOT NULL,
  doc_version text NOT NULL,
  accepted_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX consents_user_idx ON consents (user_id);

CREATE TABLE audit_logs (
  id            bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  tenant_id     uuid REFERENCES tenants(id) ON DELETE SET NULL,
  actor_user_id uuid REFERENCES users(id)   ON DELETE SET NULL,
  action        text NOT NULL,
  target        text,
  ip            inet,
  created_at    timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX audit_logs_tenant_idx ON audit_logs (tenant_id, created_at DESC);

-- =====================================================================
-- 群組 C：題庫內容（系統核心）
-- =====================================================================
CREATE TABLE question_banks (
  id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  owner_tenant_id uuid NOT NULL REFERENCES tenants(id) ON DELETE RESTRICT,
  name            text NOT NULL,
  exam_category   text,
  description     text,
  is_shared       boolean NOT NULL DEFAULT false,  -- 平台共用 vs 私有
  status          bank_status NOT NULL DEFAULT 'active',
  created_at      timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX question_banks_owner_idx ON question_banks (owner_tenant_id);

CREATE TABLE bank_versions (
  id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  bank_id        uuid NOT NULL REFERENCES question_banks(id) ON DELETE CASCADE,
  version_label  text NOT NULL,                 -- 例：115年1月起適用
  effective_date date,
  status         version_status NOT NULL DEFAULT 'draft',
  notes          text,
  created_at     timestamptz NOT NULL DEFAULT now(),
  UNIQUE (bank_id, version_label)
);

CREATE TABLE units (
  id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  bank_version_id uuid NOT NULL REFERENCES bank_versions(id) ON DELETE CASCADE,
  name            text NOT NULL,
  sort_order      int  NOT NULL DEFAULT 0
);
CREATE INDEX units_version_idx ON units (bank_version_id);

CREATE TABLE chapters (
  id         uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  unit_id    uuid NOT NULL REFERENCES units(id) ON DELETE CASCADE,
  name       text NOT NULL,
  sort_order int  NOT NULL DEFAULT 0
);
CREATE INDEX chapters_unit_idx ON chapters (unit_id);

CREATE TABLE questions (
  id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  bank_version_id uuid NOT NULL REFERENCES bank_versions(id) ON DELETE CASCADE,
  chapter_id      uuid NOT NULL REFERENCES chapters(id)      ON DELETE RESTRICT,
  question_key    text NOT NULL,   -- 跨版本穩定識別（bank + 原始題號），錯題/學習追蹤用
  original_no     int,
  type            question_type   NOT NULL DEFAULT 'single',
  stem            text NOT NULL,
  difficulty      difficulty_level,
  source          text,
  image_url       text,
  status          question_status NOT NULL DEFAULT 'draft',
  created_by      uuid REFERENCES users(id) ON DELETE SET NULL,
  reviewed_by     uuid REFERENCES users(id) ON DELETE SET NULL,
  created_at      timestamptz NOT NULL DEFAULT now(),
  updated_at      timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX questions_version_idx  ON questions (bank_version_id);
CREATE INDEX questions_chapter_idx  ON questions (chapter_id);
CREATE INDEX questions_key_idx      ON questions (question_key);
CREATE INDEX questions_status_idx   ON questions (status);
CREATE TRIGGER questions_touch BEFORE UPDATE ON questions
  FOR EACH ROW EXECUTE FUNCTION set_updated_at();

CREATE TABLE question_options (
  id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  question_id uuid NOT NULL REFERENCES questions(id) ON DELETE CASCADE,
  label       char(1) NOT NULL,          -- A/B/C/D/E
  content     text NOT NULL,
  is_correct  boolean NOT NULL DEFAULT false,
  sort_order  int NOT NULL DEFAULT 0,
  UNIQUE (question_id, label)
);
CREATE INDEX question_options_q_idx ON question_options (question_id);

CREATE TABLE question_explanations (
  question_id  uuid PRIMARY KEY REFERENCES questions(id) ON DELETE CASCADE,
  explanation  text,
  legal_basis  text,
  ai_generated boolean NOT NULL DEFAULT false,  -- AI 產生須標示
  reviewed     boolean NOT NULL DEFAULT false,  -- 人工審核後才可發布
  reviewer_id  uuid REFERENCES users(id) ON DELETE SET NULL,
  updated_at   timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE tags (
  id        uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id uuid REFERENCES tenants(id) ON DELETE CASCADE,  -- null＝平台共用標籤
  name      text NOT NULL,
  UNIQUE (tenant_id, name)
);

CREATE TABLE question_tags (
  question_id uuid NOT NULL REFERENCES questions(id) ON DELETE CASCADE,
  tag_id      uuid NOT NULL REFERENCES tags(id)      ON DELETE CASCADE,
  PRIMARY KEY (question_id, tag_id)
);

CREATE TABLE question_revisions (
  id          bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  question_id uuid NOT NULL REFERENCES questions(id) ON DELETE CASCADE,
  snapshot    jsonb NOT NULL,
  change_type text NOT NULL,   -- add/edit/answer_change/legal_update/retire
  changed_by  uuid REFERENCES users(id) ON DELETE SET NULL,
  changed_at  timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX question_revisions_q_idx ON question_revisions (question_id, changed_at DESC);

CREATE TABLE question_reports (
  id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  question_id      uuid NOT NULL REFERENCES questions(id) ON DELETE CASCADE,
  reporter_user_id uuid REFERENCES users(id) ON DELETE SET NULL,
  reason_type      report_reason NOT NULL,
  detail           text,
  status           report_status NOT NULL DEFAULT 'open',
  assignee_id      uuid REFERENCES users(id) ON DELETE SET NULL,
  resolution       text,
  created_at       timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX question_reports_status_idx ON question_reports (status);

-- =====================================================================
-- 群組 D：商品、授權與訂單
-- =====================================================================
CREATE TABLE products (
  id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  owner_tenant_id uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
  type            product_type NOT NULL,
  name            text NOT NULL,
  price           numeric(12,2) NOT NULL DEFAULT 0,
  currency        char(3) NOT NULL DEFAULT 'TWD',
  config          jsonb NOT NULL DEFAULT '{}',
  status          text NOT NULL DEFAULT 'active',
  created_at      timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX products_owner_idx ON products (owner_tenant_id);

CREATE TABLE product_banks (
  product_id uuid NOT NULL REFERENCES products(id)       ON DELETE CASCADE,
  bank_id    uuid NOT NULL REFERENCES question_banks(id) ON DELETE CASCADE,
  PRIMARY KEY (product_id, bank_id)
);

CREATE TABLE orders (
  id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id      uuid NOT NULL REFERENCES tenants(id) ON DELETE RESTRICT,
  buyer_user_id  uuid REFERENCES users(id) ON DELETE SET NULL,
  product_id     uuid REFERENCES products(id) ON DELETE SET NULL,
  amount         numeric(12,2) NOT NULL DEFAULT 0,
  status         order_status NOT NULL DEFAULT 'pending',
  payment_method text,
  invoice_no     text,
  created_at     timestamptz NOT NULL DEFAULT now(),
  paid_at        timestamptz
);
CREATE INDEX orders_tenant_idx ON orders (tenant_id, created_at DESC);
CREATE INDEX orders_buyer_idx  ON orders (buyer_user_id);

-- 授權：整個系統的存取核心（見第八章 has_bank_access）
CREATE TABLE entitlements (
  id                   uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id            uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
  subject_type         ent_subject NOT NULL,          -- user / org
  subject_id           uuid NOT NULL,                  -- 對應 user 或 tenant
  bank_id              uuid NOT NULL REFERENCES question_banks(id) ON DELETE CASCADE,
  version_scope        text NOT NULL DEFAULT 'latest', -- 'latest' 或 bank_versions.id
  chapter_scope        jsonb,                          -- null＝全部；或 chapter id 陣列
  start_at             timestamptz NOT NULL DEFAULT now(),
  end_at               timestamptz,
  max_attempts         int,                            -- null＝不限
  can_view_explanation boolean NOT NULL DEFAULT true,
  can_mock_exam        boolean NOT NULL DEFAULT true,
  can_download_report  boolean NOT NULL DEFAULT false,
  can_use_ai           boolean NOT NULL DEFAULT false,
  source_order_id      uuid REFERENCES orders(id) ON DELETE SET NULL,
  status               ent_status NOT NULL DEFAULT 'active',
  created_at           timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX entitlements_lookup_idx ON entitlements (subject_type, subject_id, bank_id, status);
CREATE INDEX entitlements_tenant_idx ON entitlements (tenant_id);

CREATE TABLE coupons (
  id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id   uuid REFERENCES tenants(id) ON DELETE CASCADE,
  code        text NOT NULL,
  type        coupon_type NOT NULL,
  value       numeric(12,2) NOT NULL DEFAULT 0,
  constraints jsonb NOT NULL DEFAULT '{}',
  valid_from  timestamptz,
  valid_to    timestamptz,
  usage_limit int,
  UNIQUE (tenant_id, code)
);

CREATE TABLE referrals (
  id                uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  referrer_user_id  uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  referee_user_id   uuid REFERENCES users(id) ON DELETE SET NULL,
  reward            text,
  status            text NOT NULL DEFAULT 'pending',
  created_at        timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE b2b_deals (
  id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id      uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
  contact        text,
  quote          numeric(12,2),
  contract_start date,
  contract_end   date,
  seats          int,
  paid_amount    numeric(12,2) DEFAULT 0,
  expire_date    date,
  renewal_status text,
  sales_owner    text,
  created_at     timestamptz NOT NULL DEFAULT now()
);

-- =====================================================================
-- 群組 E：機構與班級
-- =====================================================================
CREATE TABLE classes (
  id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id       uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
  name            text NOT NULL,
  teacher_user_id uuid REFERENCES users(id) ON DELETE SET NULL,
  allowed_banks   jsonb NOT NULL DEFAULT '[]',
  created_at      timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX classes_tenant_idx ON classes (tenant_id);

CREATE TABLE class_members (
  class_id uuid NOT NULL REFERENCES classes(id) ON DELETE CASCADE,
  user_id  uuid NOT NULL REFERENCES users(id)   ON DELETE CASCADE,
  status   text NOT NULL DEFAULT 'active',
  PRIMARY KEY (class_id, user_id)
);

CREATE TABLE invitations (
  id         uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id  uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
  class_id   uuid REFERENCES classes(id) ON DELETE CASCADE,
  code       text NOT NULL,
  link_token text UNIQUE,
  max_uses   int,
  used       int NOT NULL DEFAULT 0,
  expire_at  timestamptz,
  UNIQUE (tenant_id, code)
);

CREATE TABLE assignments (
  id                 uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  class_id           uuid NOT NULL REFERENCES classes(id) ON DELETE CASCADE,
  bank_version_id    uuid NOT NULL REFERENCES bank_versions(id) ON DELETE RESTRICT,
  chapter_scope      jsonb,
  question_count     int NOT NULL DEFAULT 20,
  time_limit         int,                 -- 秒；null＝不限
  start_at           timestamptz,
  due_at             timestamptz,
  pass_score         int NOT NULL DEFAULT 60,
  max_attempts       int,
  reveal_answer      boolean NOT NULL DEFAULT false,
  reveal_explanation boolean NOT NULL DEFAULT false,
  created_by         uuid REFERENCES users(id) ON DELETE SET NULL,
  created_at         timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX assignments_class_idx ON assignments (class_id);

CREATE TABLE assignment_targets (
  assignment_id uuid NOT NULL REFERENCES assignments(id) ON DELETE CASCADE,
  user_id       uuid NOT NULL REFERENCES users(id)       ON DELETE CASCADE,
  PRIMARY KEY (assignment_id, user_id)
);

-- =====================================================================
-- 群組 F：作答與學習
-- =====================================================================
CREATE TABLE exam_sessions (
  id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id          uuid NOT NULL REFERENCES users(id)   ON DELETE CASCADE,
  tenant_id        uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
  mode             exam_mode   NOT NULL,
  bank_version_id  uuid NOT NULL REFERENCES bank_versions(id) ON DELETE RESTRICT,
  chapter_scope    jsonb,
  question_count   int,
  time_limit       int,
  assignment_id    uuid REFERENCES assignments(id) ON DELETE SET NULL,
  status           exam_status NOT NULL DEFAULT 'in_progress',  -- in_progress＝暫存續作
  started_at       timestamptz NOT NULL DEFAULT now(),
  submitted_at     timestamptz,
  score            int,
  correct_count    int,
  wrong_count      int,
  unanswered_count int,
  duration_sec     int
);
CREATE INDEX exam_sessions_user_idx   ON exam_sessions (user_id, started_at DESC);
CREATE INDEX exam_sessions_tenant_idx ON exam_sessions (tenant_id);
CREATE INDEX exam_sessions_open_idx   ON exam_sessions (user_id) WHERE status = 'in_progress';

CREATE TABLE exam_items (
  id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  session_id      uuid NOT NULL REFERENCES exam_sessions(id) ON DELETE CASCADE,
  question_id     uuid NOT NULL REFERENCES questions(id)     ON DELETE RESTRICT,
  order_no        int NOT NULL,
  selected        jsonb,               -- 單選字串或複選陣列；null＝未作答（暫存）
  is_correct      boolean,
  marked_uncertain boolean NOT NULL DEFAULT false,
  time_spent_sec  int NOT NULL DEFAULT 0,
  UNIQUE (session_id, order_no)
);
CREATE INDEX exam_items_session_idx ON exam_items (session_id);

CREATE TABLE wrong_questions (
  id                 uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id            uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  question_key       text NOT NULL,        -- 跨版本追蹤
  bank_id            uuid NOT NULL REFERENCES question_banks(id) ON DELETE CASCADE,
  wrong_count        int NOT NULL DEFAULT 1,
  last_wrong_at      timestamptz NOT NULL DEFAULT now(),
  last_selected      jsonb,
  resolved           boolean NOT NULL DEFAULT false,
  consecutive_correct int NOT NULL DEFAULT 0,
  UNIQUE (user_id, question_key)
);
CREATE INDEX wrong_questions_user_idx ON wrong_questions (user_id) WHERE resolved = false;

CREATE TABLE favorites (
  id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id      uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  question_key text NOT NULL,
  note         text,
  importance   int NOT NULL DEFAULT 0,
  confusing    boolean NOT NULL DEFAULT false,
  created_at   timestamptz NOT NULL DEFAULT now(),
  UNIQUE (user_id, question_key)
);

CREATE TABLE learning_progress (
  user_id         uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  bank_id         uuid NOT NULL REFERENCES question_banks(id) ON DELETE CASCADE,
  chapter_key     text NOT NULL,
  answered        int NOT NULL DEFAULT 0,
  mastered        int NOT NULL DEFAULT 0,
  wrong           int NOT NULL DEFAULT 0,
  completion_rate numeric(5,2) NOT NULL DEFAULT 0,
  accuracy        numeric(5,2) NOT NULL DEFAULT 0,
  updated_at      timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (user_id, bank_id, chapter_key)
);

CREATE TABLE review_schedule (
  user_id       uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  question_key  text NOT NULL,
  next_review_at timestamptz NOT NULL DEFAULT now(),
  ease          numeric(4,2) NOT NULL DEFAULT 2.5,
  interval_days int NOT NULL DEFAULT 1,
  PRIMARY KEY (user_id, question_key)
);
CREATE INDEX review_schedule_due_idx ON review_schedule (user_id, next_review_at);

-- =====================================================================
-- 群組 G：通知與公告
-- =====================================================================
CREATE TABLE announcements (
  id         uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id  uuid REFERENCES tenants(id) ON DELETE CASCADE,  -- null＝全平台
  title      text NOT NULL,
  body       text,
  type       text,
  publish_at timestamptz,
  expire_at  timestamptz
);

CREATE TABLE notifications (
  id       uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id  uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  channel  notify_channel NOT NULL,
  template text,
  payload  jsonb NOT NULL DEFAULT '{}',
  status   text NOT NULL DEFAULT 'queued',
  sent_at  timestamptz
);
CREATE INDEX notifications_user_idx ON notifications (user_id, sent_at DESC);

-- =====================================================================
-- 存取核心：授權判斷函式（對應第八章）
-- 應用層在查詢前設定： SET LOCAL app.user_id / app.tenant_id / app.is_platform
-- =====================================================================
CREATE OR REPLACE FUNCTION has_bank_access(
  p_user uuid, p_tenant uuid, p_bank uuid,
  p_version uuid DEFAULT NULL, p_chapter uuid DEFAULT NULL, p_feature text DEFAULT NULL
) RETURNS boolean AS $$
DECLARE r record;
BEGIN
  FOR r IN
    SELECT * FROM entitlements e
    WHERE e.bank_id = p_bank
      AND e.status = 'active'
      AND (e.start_at <= now())
      AND (e.end_at IS NULL OR e.end_at >= now())
      AND (
            (e.subject_type = 'user' AND e.subject_id = p_user)
         OR (e.subject_type = 'org'  AND e.subject_id = p_tenant
             AND EXISTS (SELECT 1 FROM memberships m
                         WHERE m.user_id = p_user AND m.tenant_id = p_tenant
                           AND m.status = 'active'))
          )
  LOOP
    -- 版本範圍
    IF p_version IS NOT NULL AND r.version_scope <> 'latest'
       AND r.version_scope <> p_version::text THEN CONTINUE; END IF;
    -- 章節範圍（null＝全部）
    IF p_chapter IS NOT NULL AND r.chapter_scope IS NOT NULL
       AND NOT (r.chapter_scope ? p_chapter::text) THEN CONTINUE; END IF;
    -- 功能旗標
    IF p_feature = 'explanation' AND NOT r.can_view_explanation THEN CONTINUE; END IF;
    IF p_feature = 'mock'        AND NOT r.can_mock_exam        THEN CONTINUE; END IF;
    IF p_feature = 'report'      AND NOT r.can_download_report  THEN CONTINUE; END IF;
    IF p_feature = 'ai'          AND NOT r.can_use_ai           THEN CONTINUE; END IF;
    RETURN true;  -- 命中一筆即可（max_attempts 由應用層計次判斷）
  END LOOP;
  RETURN false;
END;
$$ LANGUAGE plpgsql STABLE;

-- =====================================================================
-- Row-Level Security 範例（多租戶／個人資料隔離）
-- 應用層每次連線： SET LOCAL app.tenant_id='...'; SET LOCAL app.user_id='...';
--                 平台後台： SET LOCAL app.is_platform='on';
-- 註：題庫內容（question_banks/questions…）為可跨租戶共用之資產，
--     其存取以 has_bank_access() 於應用層控管，不在此做行級過濾。
-- =====================================================================
CREATE OR REPLACE FUNCTION current_tenant() RETURNS uuid AS $$
  SELECT nullif(current_setting('app.tenant_id', true),'')::uuid;
$$ LANGUAGE sql STABLE;
CREATE OR REPLACE FUNCTION current_user_id() RETURNS uuid AS $$
  SELECT nullif(current_setting('app.user_id', true),'')::uuid;
$$ LANGUAGE sql STABLE;
CREATE OR REPLACE FUNCTION is_platform() RETURNS boolean AS $$
  SELECT coalesce(current_setting('app.is_platform', true),'off') = 'on';
$$ LANGUAGE sql STABLE;

-- 租戶隔離：以 tenant_id 過濾
DO $$
DECLARE t text;
BEGIN
  FOREACH t IN ARRAY ARRAY[
    'tenant_branding','memberships','orders','entitlements','coupons',
    'b2b_deals','classes','invitations','exam_sessions'
  ] LOOP
    EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY;', t);
    EXECUTE format($f$
      CREATE POLICY tenant_isolation ON %I
        USING (is_platform() OR tenant_id = current_tenant())
        WITH CHECK (is_platform() OR tenant_id = current_tenant());
    $f$, t);
  END LOOP;
END $$;

-- products 以 owner_tenant_id 隔離
ALTER TABLE products ENABLE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON products
  USING (is_platform() OR owner_tenant_id = current_tenant())
  WITH CHECK (is_platform() OR owner_tenant_id = current_tenant());

-- 個人資料隔離：以 user_id 過濾（本人或平台）
DO $$
DECLARE t text;
BEGIN
  FOREACH t IN ARRAY ARRAY[
    'wrong_questions','favorites','learning_progress','review_schedule',
    'notifications','user_devices','consents'
  ] LOOP
    EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY;', t);
    EXECUTE format($f$
      CREATE POLICY owner_isolation ON %I
        USING (is_platform() OR user_id = current_user_id())
        WITH CHECK (is_platform() OR user_id = current_user_id());
    $f$, t);
  END LOOP;
END $$;

-- =====================================================================
-- 最小種子資料：平台租戶 + 本題庫 + 版本 + 18 章節骨架
-- （2,538 題本身由結構化 Excel 匯入，見結尾說明）
-- =====================================================================
INSERT INTO tenants (id, type, name)
VALUES ('00000000-0000-0000-0000-000000000001','platform','平台（自有品牌）');

INSERT INTO question_banks (id, owner_tenant_id, name, exam_category, is_shared, status)
VALUES ('00000000-0000-0000-0000-0000000000b1',
        '00000000-0000-0000-0000-000000000001',
        '公共工程品質管理（機電）','公共工程品管', true, 'active');

INSERT INTO bank_versions (id, bank_id, version_label, effective_date, status)
VALUES ('00000000-0000-0000-0000-0000000000c1',
        '00000000-0000-0000-0000-0000000000b1',
        '115年1月起適用','2026-01-01','published');

-- 提示：以結構化 Excel 匯入題目時，
--   question_key 建議＝ bank_id 短碼 + 原始題號（如 'jd-0001'），跨版本穩定；
--   units/chapters 依「課程名稱（原始）」建立，注意「單元二」有兩組章節序，
--   請用章節完整名稱，勿只用序號（見設計文件第十五章）。
