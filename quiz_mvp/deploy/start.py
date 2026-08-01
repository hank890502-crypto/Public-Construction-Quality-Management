#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
容器啟動腳本：首次啟動自動建表＋灌題庫，之後啟動 API。
可安全重複執行（restart 不會重建或重灌）。
需要環境變數 DATABASE_URL；PORT 由平台注入（預設 8000）。
"""
import os, sys, time, json, subprocess
from pathlib import Path
import psycopg2

ROOT = Path(__file__).resolve().parent.parent
DB = os.environ.get('DATABASE_URL')
if not DB:
    sys.exit('缺少 DATABASE_URL 環境變數')

def connect():
    last = None
    for i in range(40):
        try:
            return psycopg2.connect(DB)
        except Exception as e:
            last = e; print(f'[start] 等待資料庫… ({i+1})', flush=True); time.sleep(2)
    raise SystemExit(f'[start] 無法連線資料庫：{last}')

def bootstrap():
    conn = connect(); conn.autocommit = True; cur = conn.cursor()
    cur.execute("SELECT to_regclass('public.questions')")
    if cur.fetchone()[0] is None:
        print('[start] 首次啟動：建立資料表…', flush=True)
        cur.execute((ROOT / 'db' / 'schema_mvp.sql').read_text(encoding='utf-8'))
    cur.execute("SELECT count(*) FROM questions")
    if cur.fetchone()[0] == 0:
        print('[start] 匯入題庫…', flush=True)
        xlsx = str(ROOT / 'data' / '公共工程品管(機電)題庫_結構化_115年1月起適用.xlsx')
        subprocess.check_call([sys.executable, str(ROOT / 'scripts' / 'import_questions.py'), xlsx])
    else:
        print('[start] 題庫已存在，略過匯入。', flush=True)
    migrate(cur)
    sync_explanations(cur)
    cur.close(); conn.close()

def migrate(cur):
    """冪等遷移：新增裝置身分欄位與授權碼表（不動既有資料）。"""
    cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS device_token text")
    cur.execute("CREATE UNIQUE INDEX IF NOT EXISTS users_device_token_uq ON users(device_token) WHERE device_token IS NOT NULL")
    cur.execute("ALTER TABLE users DROP CONSTRAINT IF EXISTS users_need_identifier")
    cur.execute("""CREATE TABLE IF NOT EXISTS license_codes(
        code text PRIMARY KEY,
        duration_days int NOT NULL,
        status text NOT NULL DEFAULT 'active',
        bound_user_id uuid REFERENCES users(id),
        created_at timestamptz NOT NULL DEFAULT now(),
        redeemed_at timestamptz,
        note text)""")
    print('[start] 遷移完成', flush=True)

def sync_explanations(cur):
    """把 data/explanations.json 冪等同步進 question_explanations（AI 草稿、未審核）。"""
    p = ROOT / 'data' / 'explanations.json'
    if not p.exists():
        return
    try:
        data = json.loads(p.read_text(encoding='utf-8'))
    except Exception as e:
        print('[start] 解析檔讀取失敗：', e, flush=True); return
    cur.execute("""SELECT id FROM bank_versions WHERE bank_id='00000000-0000-0000-0000-0000000000b1'
                   AND status='published' ORDER BY effective_date DESC NULLS LAST LIMIT 1""")
    row = cur.fetchone()
    if not row:
        return
    ver = row[0]; n = 0
    for no, e in data.items():
        cur.execute("SELECT id FROM questions WHERE bank_version_id=%s AND original_no=%s", (ver, int(no)))
        q = cur.fetchone()
        if not q:
            continue
        cur.execute("""INSERT INTO question_explanations(question_id,explanation,legal_basis,ai_generated,reviewed)
                       VALUES(%s,%s,%s,true,false)
                       ON CONFLICT (question_id) DO UPDATE
                         SET explanation=EXCLUDED.explanation, legal_basis=EXCLUDED.legal_basis""",
                    (q[0], e.get('explanation'), e.get('legal_basis')))
        n += 1
    print(f'[start] 已同步 {n} 筆解析', flush=True)

def main():
    bootstrap()
    port = os.environ.get('PORT', '8000')
    os.chdir(ROOT / 'backend')
    print(f'[start] 啟動 API 於 0.0.0.0:{port}', flush=True)
    os.execvp('uvicorn', ['uvicorn', 'main:app', '--host', '0.0.0.0', '--port', str(port)])

if __name__ == '__main__':
    main()
