#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
線上題庫 MVP — FastAPI 後端（連 PostgreSQL / schema_mvp.sql）
特色：
  - 免登入「裝置識別」：前端每個瀏覽器帶 X-Client-Token，後端對應唯一 user，
    每人資料（作答/錯題/統計）各自獨立；新裝置自動給試用授權。
  - 授權碼：管理端核發碼（含使用天數），使用者兌換後延長到期日；一碼綁一人；
    使用者身分固定，換新碼會延續舊資料。
  - 作答選項：打亂題目/打亂選項/未作答優先 皆真正生效。
連線：DATABASE_URL 或 PG* 環境變數。管理端需環境變數 ADMIN_KEY。
資料表遷移與題庫匯入由 deploy/start.py 負責。
"""
import os, json, secrets, time, threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
import random as _random
import psycopg2, psycopg2.extras
from fastapi import FastAPI, HTTPException, Header
from fastapi.responses import FileResponse
from pydantic import BaseModel

PLATFORM_TENANT = '00000000-0000-0000-0000-000000000001'
BANK_ID         = '00000000-0000-0000-0000-0000000000b1'
DEMO_USER       = '00000000-0000-0000-0000-0000000000de'
TRIAL_DAYS      = int(os.environ.get('TRIAL_DAYS', '7'))
ADMIN_KEY       = os.environ.get('ADMIN_KEY')
WEB_DIR = Path(__file__).resolve().parent.parent / 'web'

import battle
app = FastAPI(title='題庫 MVP')
app.include_router(battle.router)

def db():
    conn = psycopg2.connect(os.environ['DATABASE_URL']) if os.environ.get('DATABASE_URL') else psycopg2.connect()
    conn.cursor_factory = psycopg2.extras.RealDictCursor
    return conn

def _published_version(cur):
    cur.execute("""SELECT id FROM bank_versions WHERE bank_id=%s AND status='published'
                   ORDER BY effective_date DESC NULLS LAST LIMIT 1""", (BANK_ID,))
    r = cur.fetchone()
    if not r:
        raise HTTPException(500, '找不到已發布的題庫版本')
    return r['id']

def _resolve_user(cur, token):
    """匿名裝置身分：token 對應唯一 user；新裝置建立 user 並給試用授權。"""
    if not token:
        return DEMO_USER
    cur.execute("SELECT id FROM users WHERE device_token=%s", (token,))
    r = cur.fetchone()
    if r:
        return r['id']
    cur.execute("INSERT INTO users(device_token,status) VALUES(%s,'active') RETURNING id", (token,))
    uid = cur.fetchone()['id']
    cur.execute("INSERT INTO memberships(user_id,tenant_id,role) VALUES(%s,%s,'consumer') ON CONFLICT DO NOTHING",
                (uid, PLATFORM_TENANT))
    cur.execute("""INSERT INTO entitlements(tenant_id,subject_type,subject_id,bank_id,version_scope,end_at,
                     can_view_explanation,can_mock_exam,can_download_report,can_use_ai,status)
                   VALUES(%s,'user',%s,%s,'latest', now()+(%s||' days')::interval, true,true,false,true,'active')""",
                (PLATFORM_TENANT, uid, BANK_ID, TRIAL_DAYS))
    return uid

@contextmanager
def ctx(token):
    conn = db(); cur = conn.cursor()
    try:
        uid = _resolve_user(cur, token)
        conn.commit()
        yield conn, cur, uid
    finally:
        conn.close()

def _active_expiry(cur, uid):
    cur.execute("""SELECT max(end_at) AS exp FROM entitlements
                   WHERE subject_type='user' AND subject_id=%s AND bank_id=%s AND status='active'""", (uid, BANK_ID))
    return cur.fetchone()['exp']

def _is_active(exp):
    return exp is not None and exp > datetime.now(timezone.utc)

# ---------------- startup: 確保 demo 使用者存在（表結構由 start.py 建立）----------------
@app.on_event('startup')
def _seed():
    try:
        conn = db(); cur = conn.cursor()
        cur.execute("INSERT INTO users(id,email,email_verified,status) VALUES(%s,'demo@example.com',true,'active') ON CONFLICT DO NOTHING", (DEMO_USER,))
        cur.execute("INSERT INTO memberships(user_id,tenant_id,role) VALUES(%s,%s,'consumer') ON CONFLICT DO NOTHING", (DEMO_USER, PLATFORM_TENANT))
        conn.commit(); conn.close()
    except Exception:
        pass

# ---------------- pages ----------------
@app.get('/')
def index():
    return FileResponse(WEB_DIR / 'index.html')

@app.get('/admin')
def admin_page():
    return FileResponse(WEB_DIR / 'admin.html')

@app.get('/api/health')
def health():
    conn = db(); cur = conn.cursor()
    try:
        cur.execute('SELECT 1 AS ok'); return {'ok': cur.fetchone()['ok'] == 1}
    finally:
        conn.close()

# ---------------- 章節 ----------------
@app.get('/api/chapters')
def chapters(x_client_token: str | None = Header(None)):
    with ctx(x_client_token) as (conn, cur, uid):
        ver = _published_version(cur)
        cur.execute("""
          SELECT u.name AS unit, u.sort_order AS us, c.id AS chapter_id, c.name AS chapter,
                 c.sort_order AS cs, count(q.*) AS n
          FROM units u JOIN chapters c ON c.unit_id=u.id
          LEFT JOIN questions q ON q.chapter_id=c.id AND q.status='published'
          WHERE u.bank_version_id=%s
          GROUP BY u.name,u.sort_order,c.id,c.name,c.sort_order
          ORDER BY u.sort_order, c.sort_order""", (ver,))
        units = {}
        for r in cur.fetchall():
            units.setdefault(r['unit'], []).append(
                {'chapter_id': r['chapter_id'], 'chapter': r['chapter'], 'count': r['n']})
        return {'units': [{'unit': k, 'chapters': v} for k, v in units.items()]}

# ---------------- 建立作答場次 ----------------
class NewSession(BaseModel):
    chapter_ids: list[str] | None = None
    count: int = 20
    mode: str = 'practice'
    shuffle_questions: bool = True
    shuffle_options: bool = False
    unanswered_first: bool = False

@app.post('/api/sessions')
def new_session(body: NewSession, x_client_token: str | None = Header(None)):
    with ctx(x_client_token) as (conn, cur, uid):
        ver = _published_version(cur)
        if not _is_active(_active_expiry(cur, uid)):
            raise HTTPException(403, '授權已到期，請至「我的」輸入授權碼')
        params = []
        join = ""; group_extra = ""; order_pre = ""
        if body.unanswered_first:
            join = (" LEFT JOIN (SELECT DISTINCT ei.question_id FROM exam_items ei "
                    "JOIN exam_sessions es ON es.id=ei.session_id "
                    "WHERE es.user_id=%s AND ei.selected IS NOT NULL) ans ON ans.question_id=q.id")
            params.append(uid); group_extra = ", ans.question_id"
            order_pre = "(ans.question_id IS NOT NULL) ASC, "
        params.append(ver)
        where = "q.bank_version_id=%s AND q.status='published'"
        if body.chapter_ids:
            where += " AND q.chapter_id = ANY(%s::uuid[])"; params.append(body.chapter_ids)
        base_order = "random()" if body.shuffle_questions else "q.original_no"
        limit = max(1, min(body.count, 100)); params.append(limit)
        sql = f"""
          SELECT q.id, q.original_no, q.stem, c.name AS chapter,
                 coalesce(jsonb_agg(jsonb_build_object('label',o.label,'content',o.content)
                          ORDER BY o.sort_order) FILTER (WHERE o.id IS NOT NULL),'[]') AS options
          FROM questions q JOIN chapters c ON c.id=q.chapter_id
          LEFT JOIN question_options o ON o.question_id=q.id{join}
          WHERE {where}
          GROUP BY q.id, q.original_no, q.stem, c.name{group_extra}
          ORDER BY {order_pre}{base_order}
          LIMIT %s"""
        cur.execute(sql, params)
        qs = cur.fetchall()
        if not qs:
            raise HTTPException(404, '選定範圍沒有題目')
        cur.execute("""INSERT INTO exam_sessions(user_id,tenant_id,mode,bank_version_id,chapter_scope,question_count,status)
                       VALUES(%s,%s,%s,%s,%s,%s,'in_progress') RETURNING id""",
                    (uid, PLATFORM_TENANT, body.mode, ver,
                     json.dumps(body.chapter_ids) if body.chapter_ids else None, len(qs)))
        sid = cur.fetchone()['id']
        out = []
        for i, q in enumerate(qs):
            cur.execute("INSERT INTO exam_items(session_id,question_id,order_no) VALUES(%s,%s,%s) RETURNING id",
                        (sid, q['id'], i))
            item_id = cur.fetchone()['id']
            opts = list(q['options'])
            if body.shuffle_options:
                _random.shuffle(opts)
            out.append({'item_id': item_id, 'no': q['original_no'], 'chapter': q['chapter'],
                        'stem': q['stem'], 'options': opts})
        conn.commit()
        return {'session_id': sid, 'mode': body.mode, 'questions': out}

class Answer(BaseModel):
    item_id: str
    selected: str

@app.post('/api/sessions/{sid}/answer')
def answer(sid: str, body: Answer, x_client_token: str | None = Header(None)):
    with ctx(x_client_token) as (conn, cur, uid):
        cur.execute("SELECT mode FROM exam_sessions WHERE id=%s AND user_id=%s", (sid, uid))
        s = cur.fetchone()
        if not s:
            raise HTTPException(404, '找不到作答場次')
        cur.execute("""SELECT o.label, o.is_correct, e.explanation
                       FROM exam_items it JOIN question_options o ON o.question_id=it.question_id
                       LEFT JOIN question_explanations e ON e.question_id=it.question_id
                       WHERE it.id=%s AND it.session_id=%s""", (body.item_id, sid))
        optrows = cur.fetchall()
        if not optrows:
            raise HTTPException(404, '找不到題目')
        correct_label = next((r['label'] for r in optrows if r['is_correct']), None)
        is_correct = (body.selected == correct_label)
        cur.execute("UPDATE exam_items SET selected=%s, is_correct=%s WHERE id=%s",
                    (json.dumps(body.selected), is_correct, body.item_id))
        conn.commit()
        resp = {'recorded': True}
        if s['mode'] in ('practice', 'wrong_review', 'read'):
            resp.update({'is_correct': is_correct, 'correct_label': correct_label,
                         'explanation': optrows[0]['explanation']})
        return resp

@app.post('/api/sessions/{sid}/submit')
def submit(sid: str, x_client_token: str | None = Header(None)):
    with ctx(x_client_token) as (conn, cur, uid):
        cur.execute("SELECT id FROM exam_sessions WHERE id=%s AND user_id=%s", (sid, uid))
        if not cur.fetchone():
            raise HTTPException(404, '找不到作答場次')
        cur.execute("""
          SELECT it.selected, it.is_correct, q.original_no, q.question_key, c.name AS chapter, q.stem,
                 (SELECT label FROM question_options WHERE question_id=q.id AND is_correct LIMIT 1) AS correct_label
          FROM exam_items it JOIN questions q ON q.id=it.question_id JOIN chapters c ON c.id=q.chapter_id
          WHERE it.session_id=%s ORDER BY it.order_no""", (sid,))
        items = cur.fetchall()
        total = len(items)
        correct = sum(1 for r in items if r['is_correct'])
        answered = sum(1 for r in items if r['selected'] is not None)
        wrong = answered - correct
        unanswered = total - answered
        score = round(correct / total * 100) if total else 0
        per = {}
        for r in items:
            d = per.setdefault(r['chapter'], [0, 0]); d[1] += 1
            if r['is_correct']:
                d[0] += 1
        per_chapter = [{'chapter': k, 'accuracy': round(v[0]/v[1]*100)} for k, v in per.items()]
        wrong_list = [{'no': r['original_no'], 'chapter': r['chapter'], 'stem': r['stem'],
                       'correct_label': r['correct_label'], 'your': r['selected']}
                      for r in items if r['selected'] is not None and not r['is_correct']]
        cur.execute("""UPDATE exam_sessions SET status='submitted', submitted_at=now(),
                       score=%s, correct_count=%s, wrong_count=%s, unanswered_count=%s WHERE id=%s""",
                    (score, correct, wrong, unanswered, sid))
        for r in items:
            if r['selected'] is not None and not r['is_correct']:
                cur.execute("""INSERT INTO wrong_questions(user_id,question_key,bank_id,wrong_count,last_selected,resolved)
                               VALUES(%s,%s,%s,1,%s,false)
                               ON CONFLICT (user_id,question_key) DO UPDATE
                                 SET wrong_count=wrong_questions.wrong_count+1,
                                     last_wrong_at=now(), last_selected=EXCLUDED.last_selected, resolved=false""",
                            (uid, r['question_key'], BANK_ID, json.dumps(r['selected'])))
        conn.commit()
        return {'total': total, 'correct': correct, 'wrong': wrong, 'unanswered': unanswered,
                'score': score, 'pass': score >= 60, 'per_chapter': per_chapter, 'wrong_list': wrong_list}

@app.get('/api/wrong')
def wrong_book(x_client_token: str | None = Header(None)):
    with ctx(x_client_token) as (conn, cur, uid):
        cur.execute("""SELECT DISTINCT ON (w.question_key) w.question_key, w.wrong_count, w.last_wrong_at,
                              q.stem, c.name AS chapter
                       FROM wrong_questions w
                       JOIN questions q ON q.question_key=w.question_key
                       JOIN chapters c ON c.id=q.chapter_id
                       WHERE w.user_id=%s AND w.resolved=false
                       ORDER BY w.question_key, q.created_at DESC""", (uid,))
        rows = cur.fetchall()
        rows.sort(key=lambda r: (r['wrong_count'], r['last_wrong_at']), reverse=True)
        return {'items': rows[:100]}

@app.get('/api/stats')
def stats(x_client_token: str | None = Header(None)):
    with ctx(x_client_token) as (conn, cur, uid):
        ver = _published_version(cur)
        cur.execute("SELECT count(*) AS total FROM questions WHERE bank_version_id=%s AND status='published'", (ver,))
        total = cur.fetchone()['total']
        cur.execute("""SELECT count(DISTINCT it.question_id) FILTER (WHERE it.selected IS NOT NULL) AS answered,
                              count(*) FILTER (WHERE it.is_correct) AS correct,
                              count(*) FILTER (WHERE it.selected IS NOT NULL) AS answered_items
                       FROM exam_items it JOIN exam_sessions s ON s.id = it.session_id
                       WHERE s.user_id = %s""", (uid,))
        r = cur.fetchone()
        answered = r['answered'] or 0; correct = r['correct'] or 0; ai = r['answered_items'] or 0
        cur.execute("SELECT count(*) AS wrong FROM wrong_questions WHERE user_id=%s AND resolved=false", (uid,))
        wrong = cur.fetchone()['wrong']
        cur.execute("SELECT count(*) AS favs FROM favorites WHERE user_id=%s", (uid,))
        favs = cur.fetchone()['favs']
        acc = round(correct / ai * 100) if ai else 0
        return {'total': total, 'answered': answered, 'correct': correct,
                'wrong': wrong, 'favorites': favs, 'accuracy': acc}

# ---------------- 方案 / 授權碼兌換 ----------------
@app.get('/api/plan')
def plan(x_client_token: str | None = Header(None)):
    with ctx(x_client_token) as (conn, cur, uid):
        exp = _active_expiry(cur, uid)
        active = _is_active(exp)
        days_left = None
        if active:
            days_left = max(0, (exp - datetime.now(timezone.utc)).days)
        return {'active': active, 'expires_at': exp.isoformat() if exp else None, 'days_left': days_left}

class Redeem(BaseModel):
    code: str

@app.post('/api/redeem')
def redeem(body: Redeem, x_client_token: str | None = Header(None)):
    if not x_client_token:
        raise HTTPException(400, '缺少裝置識別')
    with ctx(x_client_token) as (conn, cur, uid):
        code = (body.code or '').strip().upper()
        if not code:
            raise HTTPException(400, '請輸入授權碼')
        cur.execute("SELECT * FROM license_codes WHERE code=%s", (code,))
        lc = cur.fetchone()
        if not lc or lc['status'] != 'active':
            raise HTTPException(404, '授權碼無效或已停用')
        if lc['bound_user_id'] and lc['bound_user_id'] == uid:
            raise HTTPException(409, '你已經使用過這組授權碼')
        if lc['bound_user_id'] and lc['bound_user_id'] != uid:
            raise HTTPException(409, '此授權碼已被其他人綁定')
        cur.execute("UPDATE license_codes SET bound_user_id=%s, redeemed_at=now() WHERE code=%s", (uid, code))
        cur.execute("""SELECT id FROM entitlements WHERE subject_type='user' AND subject_id=%s AND bank_id=%s
                       AND status='active' ORDER BY end_at DESC NULLS LAST LIMIT 1""", (uid, BANK_ID))
        ent = cur.fetchone()
        if ent:
            # 從「現有到期日與現在之較大者」往後加天數 → 延續舊資料、接續時間
            cur.execute("""UPDATE entitlements
                           SET end_at = GREATEST(COALESCE(end_at, now()), now()) + (%s||' days')::interval,
                               can_download_report=true
                           WHERE id=%s""", (lc['duration_days'], ent['id']))
        else:
            cur.execute("""INSERT INTO entitlements(tenant_id,subject_type,subject_id,bank_id,version_scope,end_at,
                             can_view_explanation,can_mock_exam,can_download_report,can_use_ai,status)
                           VALUES(%s,'user',%s,%s,'latest', now()+(%s||' days')::interval, true,true,true,true,'active')""",
                        (PLATFORM_TENANT, uid, BANK_ID, lc['duration_days']))
        conn.commit()
        exp = _active_expiry(cur, uid)
        return {'ok': True, 'expires_at': exp.isoformat() if exp else None,
                'days_left': max(0, (exp - datetime.now(timezone.utc)).days) if exp else 0}

# ---------------- 管理端：核發授權碼 ----------------
def _check_admin(key):
    if not ADMIN_KEY:
        raise HTTPException(503, '尚未設定 ADMIN_KEY 環境變數')
    if key != ADMIN_KEY:
        raise HTTPException(401, '管理金鑰錯誤')

def _gen_code():
    alphabet = 'ABCDEFGHJKLMNPQRSTUVWXYZ23456789'  # 去掉易混淆字元
    g = lambda n: ''.join(secrets.choice(alphabet) for _ in range(n))
    return f"PQC-{g(4)}-{g(4)}"

class NewCodes(BaseModel):
    count: int = 1
    duration_days: int = 30
    note: str | None = None

@app.post('/api/admin/codes')
def admin_new_codes(body: NewCodes, x_admin_key: str | None = Header(None)):
    _check_admin(x_admin_key)
    n = max(1, min(body.count, 500))
    conn = db(); cur = conn.cursor()
    try:
        made = []
        for _ in range(n):
            for _try in range(5):
                code = _gen_code()
                cur.execute("SELECT 1 FROM license_codes WHERE code=%s", (code,))
                if not cur.fetchone():
                    break
            cur.execute("""INSERT INTO license_codes(code,duration_days,status,note)
                           VALUES(%s,%s,'active',%s)""", (code, body.duration_days, body.note))
            made.append(code)
        conn.commit()
        return {'codes': made, 'duration_days': body.duration_days}
    finally:
        conn.close()

@app.get('/api/admin/codes')
def admin_list_codes(x_admin_key: str | None = Header(None)):
    _check_admin(x_admin_key)
    conn = db(); cur = conn.cursor()
    try:
        cur.execute("""SELECT code, duration_days, status, bound_user_id IS NOT NULL AS redeemed,
                              redeemed_at, note, created_at
                       FROM license_codes ORDER BY created_at DESC LIMIT 500""")
        return {'codes': cur.fetchall()}
    finally:
        conn.close()

# ---------------- 在線人數（記憶體；單一實例；僅管理者可見）----------------
# 前端每個開著的瀏覽器定時回報一次「我在線」；後端只記錄，不對一般使用者回傳人數。
# 人數＝最近 PRESENCE_WINDOW 秒內有回報的不重複裝置數，僅管理端（帶 ADMIN_KEY）可查。
PRESENCE_WINDOW = float(os.environ.get('PRESENCE_WINDOW_SECS', '75'))
_presence = {}
_presence_lock = threading.Lock()

def _presence_touch(token):
    now = time.monotonic()
    with _presence_lock:
        if token:
            _presence[token] = now
        cutoff = now - PRESENCE_WINDOW
        for k in [k for k, v in _presence.items() if v < cutoff]:
            _presence.pop(k, None)
        return len(_presence)

@app.post('/api/presence')
def presence_ping(x_client_token: str | None = Header(None)):
    """所有使用者定時呼叫；只記錄在線、不回傳人數（人數僅管理者可查）。"""
    _presence_touch(x_client_token)
    return {'ok': True}

@app.get('/api/admin/online')
def admin_online(x_admin_key: str | None = Header(None)):
    """管理者查詢目前在線人數（需 ADMIN_KEY）。"""
    _check_admin(x_admin_key)
    return {'online': _presence_touch(None), 'window_secs': int(PRESENCE_WINDOW)}
