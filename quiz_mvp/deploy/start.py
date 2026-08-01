#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
容器啟動腳本：首次啟動自動建表＋灌題庫，之後啟動 API。
可安全重複執行（restart 不會重建或重灌）。
需要環境變數 DATABASE_URL；PORT 由平台注入（預設 8000）。
"""
import os, sys, time, subprocess
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
    cur.close(); conn.close()

def main():
    bootstrap()
    port = os.environ.get('PORT', '8000')
    os.chdir(ROOT / 'backend')
    print(f'[start] 啟動 API 於 0.0.0.0:{port}', flush=True)
    os.execvp('uvicorn', ['uvicorn', 'main:app', '--host', '0.0.0.0', '--port', str(port)])

if __name__ == '__main__':
    main()
