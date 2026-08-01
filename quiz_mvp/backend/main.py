#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
線上題庫 MVP — FastAPI 後端（連 PostgreSQL / schema_mvp.sql）
端點：
  GET  /                      前端頁面
  GET  /api/health            健康檢查
  GET  /api/chapters          單元/章節與題數（已發布版本）
  POST /api/sessions          建立作答場次（授權檢查＋隨機出題）
  POST /api/sessions/{id}/answer   即時作答（記錄＋批改）
  POST /api/sessions/{id}/submit   交卷計分（更新錯題本）
  GET  /api/wrong             錯題本
連線：DATABASE_URL 或 PG* 環境變數。
"""
import os, json
import psycopg2, psycopg2.extras
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel
from pathlib import Path

PLATFORM_TENANT = '00000000-0000-0000-0000-000000000001'
BANK_ID         = '00000000-0000-0000-0000-0000000000b1'
DEMO_USER       = '00000000-0000-0000-0000-0000000000de'
WEB_DIR = Path(__file__).resolve().parent.parent / 'web'

app = FastAPI(title='題庫 MVP')

def db():
    conn = psycopg2.connect(os.environ['DATABASE_URL']) if os.environ.get('DATABASE_URL') else psycopg2.connect()
    conn.cursor_factory = psycopg2.extras.RealDictCursor
    return conn

def set_ctx(cur):
    # 設定多租戶／使用者上下文（RLS 用）
    cur.execute("SELECT set_config('app.user_id',%s,true), set_config('app.tenant_id',%s,true)",
                (DEMO_USER, PLATFORM_TENANT))

def published_version(cur):
    cur.execute("""SELECT id FROM bank_versions WHERE bank_id=%s AND status='published'
                   ORDER BY effective_date DESC NULLS LAST LIMIT 1""", (BANK_ID,))
    r = cur.fetchone()
    if not r: raise HTTPException(500, '找不到已發布的題庫版本')
    return r['id']

@app.on_event('startup')
def seed_demo():
    conn = db(); cur = conn.cursor()
    cur.execute("INSERT INTO users(id,email,email_verified,status) VALUES(%s,'demo@example.com',true,'active') ON CONFLICT DO NOTHING", (DEMO_USER,))
    cur.execute("INSERT INTO memberships(user_id,tenant_id,role) VALUES(%s,%s,'consumer') ON CONFLICT DO NOTHING", (DEMO_USER, PLATFORM_TENANT))
    cur.execute("""INSERT INTO entitlements(tenant_id,subject_type,subject_id,bank_id,version_scope,
                     can_view_explanation,can_mock_exam,can_download_report,can_use_ai)
                   SELECT %s,'user',%s,%s,'latest',true,true,true,true
                   WHERE NOT EXISTS (SELECT 1 FROM entitlements
                     WHERE subject_type='user' AND subject_id=%s AND bank_id=%s)""",
                (PLATFORM_TENANT, DEMO_USER, BANK_ID, DEMO_USER, BANK_ID))
    conn.commit(); cur.close(); conn.close()

@app.get('/')
def index():
    return FileResponse(WEB_DIR / 'index.html')

@app.get('/api/health')
def health():
    conn = db(); cur = conn.cursor(); cur.execute('SELECT 1 AS ok'); r = cur.fetchone(); conn.close()
    return {'ok': r['ok'] == 1}

@app.get('/api/chapters')
def chapters():
    conn = db(); cur = conn.cursor(); set_ctx(cur)
    ver = published_version(cur)
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
    conn.close()
    return {'units': [{'unit': k, 'chapters': v} for k, v in units.items()]}

class NewSession(BaseModel):
    chapter_ids: list[str] | None = None
    count: int = 20
    mode: str = 'practice'

@app.post('/api/sessions')
def new_session(body: NewSession):
    conn = db(); cur = conn.cursor(); set_ctx(cur)
    ver = published_version(cur)
    # 授權檢查
    cur.execute("SELECT has_bank_access(%s,%s,%s) AS ok", (DEMO_USER, PLATFORM_TENANT, BANK_ID))
    if not cur.fetchone()['ok']:
        raise HTTPException(403, '無此題庫的使用授權')
    # 隨機出題
    params = [ver]
    where = "q.bank_version_id=%s AND q.status='published'"
    if body.chapter_ids:
        where += " AND q.chapter_id = ANY(%s::uuid[])"; params.append(body.chapter_ids)
    cur.execute(f"""
      SELECT q.id, q.original_no, q.stem, c.name AS chapter,
             coalesce(jsonb_agg(jsonb_build_object('label',o.label,'content',o.content)
                      ORDER BY o.sort_order) FILTER (WHERE o.id IS NOT NULL),'[]') AS options
      FROM questions q JOIN chapters c ON c.id=q.chapter_id
      LEFT JOIN question_options o ON o.question_id=q.id
      WHERE {where}
      GROUP BY q.id, q.original_no, q.stem, c.name
      ORDER BY random() LIMIT %s""", params + [max(1, min(body.count, 100))])
    qs = cur.fetchall()
    if not qs: raise HTTPException(404, '選定範圍沒有題目')
    # 建 session + items
    cur.execute("""INSERT INTO exam_sessions(user_id,tenant_id,mode,bank_version_id,chapter_scope,question_count,status)
                   VALUES(%s,%s,%s,%s,%s,%s,'in_progress') RETURNING id""",
                (DEMO_USER, PLATFORM_TENANT, body.mode, ver,
                 json.dumps(body.chapter_ids) if body.chapter_ids else None, len(qs)))
    sid = cur.fetchone()['id']
    out = []
    for i, q in enumerate(qs):
        cur.execute("INSERT INTO exam_items(session_id,question_id,order_no) VALUES(%s,%s,%s) RETURNING id",
                    (sid, q['id'], i))
        item_id = cur.fetchone()['id']
        out.append({'item_id': item_id, 'no': q['original_no'], 'chapter': q['chapter'],
                    'stem': q['stem'], 'options': q['options']})
    conn.commit(); conn.close()
    return {'session_id': sid, 'mode': body.mode, 'questions': out}

class Answer(BaseModel):
    item_id: str
    selected: str

@app.post('/api/sessions/{sid}/answer')
def answer(sid: str, body: Answer):
    conn = db(); cur = conn.cursor(); set_ctx(cur)
    cur.execute("SELECT mode FROM exam_sessions WHERE id=%s AND user_id=%s", (sid, DEMO_USER))
    s = cur.fetchone()
    if not s: raise HTTPException(404, '找不到作答場次')
    cur.execute("""SELECT o.label, o.is_correct, e.explanation
                   FROM exam_items it JOIN question_options o ON o.question_id=it.question_id
                   LEFT JOIN question_explanations e ON e.question_id=it.question_id
                   WHERE it.id=%s""", (body.item_id,))
    optrows = cur.fetchall()
    if not optrows: raise HTTPException(404, '找不到題目')
    correct_label = next((r['label'] for r in optrows if r['is_correct']), None)
    is_correct = (body.selected == correct_label)
    cur.execute("UPDATE exam_items SET selected=%s, is_correct=%s WHERE id=%s",
                (json.dumps(body.selected), is_correct, body.item_id))
    conn.commit()
    reveal = s['mode'] in ('practice', 'wrong_review', 'read')
    resp = {'recorded': True}
    if reveal:
        resp.update({'is_correct': is_correct, 'correct_label': correct_label,
                     'explanation': optrows[0]['explanation']})
    conn.close()
    return resp

@app.post('/api/sessions/{sid}/submit')
def submit(sid: str):
    conn = db(); cur = conn.cursor(); set_ctx(cur)
    cur.execute("SELECT id,bank_version_id FROM exam_sessions WHERE id=%s AND user_id=%s", (sid, DEMO_USER))
    s = cur.fetchone()
    if not s: raise HTTPException(404, '找不到作答場次')
    cur.execute("""
      SELECT it.id, it.selected, it.is_correct, q.original_no, q.question_key, c.name AS chapter,
             (SELECT label FROM question_options WHERE question_id=q.id AND is_correct LIMIT 1) AS correct_label,
             q.stem
      FROM exam_items it JOIN questions q ON q.id=it.question_id JOIN chapters c ON c.id=q.chapter_id
      WHERE it.session_id=%s ORDER BY it.order_no""", (sid,))
    items = cur.fetchall()
    total = len(items)
    correct = sum(1 for r in items if r['is_correct'])
    answered = sum(1 for r in items if r['selected'] is not None)
    wrong = answered - correct
    unanswered = total - answered
    score = round(correct / total * 100) if total else 0
    # 各章正確率
    per = {}
    for r in items:
        d = per.setdefault(r['chapter'], [0, 0]); d[1] += 1
        if r['is_correct']: d[0] += 1
    per_chapter = [{'chapter': k, 'accuracy': round(v[0]/v[1]*100)} for k, v in per.items()]
    wrong_list = [{'no': r['original_no'], 'chapter': r['chapter'], 'stem': r['stem'],
                   'correct_label': r['correct_label'],
                   'your': r['selected']}
                  for r in items if r['selected'] is not None and not r['is_correct']]
    # 更新 session
    cur.execute("""UPDATE exam_sessions SET status='submitted', submitted_at=now(),
                   score=%s, correct_count=%s, wrong_count=%s, unanswered_count=%s WHERE id=%s""",
                (score, correct, wrong, unanswered, sid))
    # 更新錯題本（以 question_key 累計）
    for r in items:
        if r['selected'] is not None and not r['is_correct']:
            cur.execute("""INSERT INTO wrong_questions(user_id,question_key,bank_id,wrong_count,last_selected,resolved)
                           VALUES(%s,%s,%s,1,%s,false)
                           ON CONFLICT (user_id,question_key) DO UPDATE
                             SET wrong_count=wrong_questions.wrong_count+1,
                                 last_wrong_at=now(), last_selected=EXCLUDED.last_selected, resolved=false""",
                        (DEMO_USER, r['question_key'], BANK_ID, json.dumps(r['selected'])))
    conn.commit(); conn.close()
    return {'total': total, 'correct': correct, 'wrong': wrong, 'unanswered': unanswered,
            'score': score, 'pass': score >= 60, 'per_chapter': per_chapter, 'wrong_list': wrong_list}

@app.get('/api/wrong')
def wrong_book():
    conn = db(); cur = conn.cursor(); set_ctx(cur)
    cur.execute("""SELECT w.question_key, w.wrong_count, w.last_wrong_at,
                          q.stem, c.name AS chapter
                   FROM wrong_questions w
                   JOIN questions q ON q.question_key=w.question_key
                   JOIN chapters c ON c.id=q.chapter_id
                   WHERE w.user_id=%s AND w.resolved=false
                   ORDER BY w.wrong_count DESC, w.last_wrong_at DESC LIMIT 100""", (DEMO_USER,))
    rows = cur.fetchall(); conn.close()
    return {'items': rows}

@app.get('/api/stats')
def stats():
    conn = db(); cur = conn.cursor(); set_ctx(cur)
    ver = published_version(cur)
    cur.execute("SELECT count(*) AS total FROM questions WHERE bank_version_id=%s AND status='published'", (ver,))
    total = cur.fetchone()['total']
    cur.execute("""SELECT count(DISTINCT it.question_id) FILTER (WHERE it.selected IS NOT NULL) AS answered,
                          count(*) FILTER (WHERE it.is_correct) AS correct,
                          count(*) FILTER (WHERE it.selected IS NOT NULL) AS answered_items
                   FROM exam_items it JOIN exam_sessions s ON s.id = it.session_id
                   WHERE s.user_id = %s""", (DEMO_USER,))
    r = cur.fetchone()
    answered = r['answered'] or 0; correct = r['correct'] or 0; ai = r['answered_items'] or 0
    cur.execute("SELECT count(*) AS wrong FROM wrong_questions WHERE user_id=%s AND resolved=false", (DEMO_USER,))
    wrong = cur.fetchone()['wrong']
    cur.execute("SELECT count(*) AS favs FROM favorites WHERE user_id=%s", (DEMO_USER,))
    favs = cur.fetchone()['favs']
    conn.close()
    acc = round(correct / ai * 100) if ai else 0
    return {'total': total, 'answered': answered, 'correct': correct,
            'wrong': wrong, 'favorites': favs, 'accuracy': acc}
