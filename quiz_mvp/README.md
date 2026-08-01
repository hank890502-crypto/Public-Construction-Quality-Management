# 公共工程品管題庫 — MVP（前端 + FastAPI + PostgreSQL）

可實際運行的最小可用版本：從資料庫載入 2,538 題真實題目，支援選章節、隨機出題、
即時批改、交卷計分、錯題本，前端為手機優先介面。對應設計文件的資料模型與第一階段範圍。

## 專案結構

```
quiz_mvp/
├─ README.md
├─ db/
│  └─ schema_mvp.sql            建表 SQL（含 RLS 與授權函式）
├─ backend/
│  ├─ main.py                   FastAPI 後端
│  └─ requirements.txt
├─ web/
│  └─ index.html               手機優先前端（呼叫 /api）
├─ scripts/
│  ├─ import_questions.py       匯入題庫 Excel → 資料庫
│  └─ tag_difficulty.py         產生難度／標籤初稿（未審核）
└─ data/
   └─ 公共工程品管(機電)題庫_結構化_115年1月起適用.xlsx
```

## 需求

- PostgreSQL 14+（建議 16）
- Python 3.10+

## 安裝與啟動

```bash
# 1) 建立資料庫並套用 schema
createdb quizdb
psql -d quizdb -f db/schema_mvp.sql

# 2) 安裝 Python 套件
pip install -r backend/requirements.txt

# 3) 設定連線
export DATABASE_URL=postgresql://使用者:密碼@localhost:5432/quizdb
#   或用標準 PG 變數：export PGHOST=localhost PGPORT=5432 PGUSER=... PGDATABASE=quizdb

# 4)（可選）先產生難度與標籤初稿，再匯入
python scripts/tag_difficulty.py "data/公共工程品管(機電)題庫_結構化_115年1月起適用.xlsx"

# 5) 匯入 2,538 題
python scripts/import_questions.py "data/公共工程品管(機電)題庫_結構化_115年1月起適用.xlsx"

# 6) 啟動後端（同時提供前端頁）
cd backend
uvicorn main:app --reload --port 8000
# 瀏覽器開 http://localhost:8000/ ，手機可連同網段 IP
```

## API 端點

| 方法 | 路徑 | 說明 |
|---|---|---|
| GET | `/api/health` | 健康檢查 |
| GET | `/api/chapters` | 單元／章節與題數 |
| POST | `/api/sessions` | 建立作答場次（授權檢查＋隨機出題）body: `{count, mode, chapter_ids?}` |
| POST | `/api/sessions/{id}/answer` | 即時作答（記錄＋批改）body: `{item_id, selected}` |
| POST | `/api/sessions/{id}/submit` | 交卷計分（更新錯題本） |
| GET | `/api/wrong` | 錯題本 |

## 重要說明（MVP 邊界）

- **登入為示範用**：後端內建一位 demo 使用者與一筆整套題庫授權，方便直接試跑；
  正式版請接第二階段的註冊／登入與金流，並在每次請求設定
  `app.user_id` / `app.tenant_id`（RLS 已就緒）。
- **難度與標籤為自動初稿、未經人工審核**；解析與法規依據尚未建立。正式發布前需人工審核，
  AI／自動產生內容應標示「未審核」。
- **技術選型**：此 MVP 以 FastAPI + 原生前端便於直接運行與驗證；正式版前端可依設計文件
  改用 Next.js/React（PWA），後端亦可換 Django，資料模型不變。
- 題庫版本以 `bank_versions` 管理；錯題以 `question_key` 跨版本追蹤。

## 已驗證

於 PostgreSQL 16 實跑：schema 零錯誤、2,538 題匯入（3 單元／18 章節／10,152 選項、
每題恰一正解）、RLS 租戶隔離與 `has_bank_access` 授權判斷、所有 API 端點與前端作答流程。

## 部署上線

### 方式一：Render（推薦，最省事）

1. 把整個 `quiz_mvp` 專案放到一個 GitHub repo（可私有）。
2. 到 render.com 註冊 → New → Blueprint → 選這個 repo → Apply。
3. Render 讀 `render.yaml`，自動建立「PostgreSQL + Web 服務」；首次部署會由 `deploy/start.py`
   自動建表並匯入 2,538 題。
4. 完成後會給一個 `https://xxx.onrender.com` 網址，手機／朋友直接開即可。

注意：免費方案的 Web 服務閒置會休眠（下次連線需等幾秒喚醒），免費 PostgreSQL 有使用期限——
測試足夠，正式上線再升級付費方案。

### 方式二：自己的 VPS 或本機（Docker）

```bash
docker compose up --build      # 首次會自動建表＋灌題庫
# 開 http://<主機IP>:8000/
```

正式對外建議前面加一層自動 HTTPS 反向代理（如 Caddy），把網域指到本服務。

### 方式三：只想快速給朋友測（本機 + 通道）

在自己電腦 `docker compose up` 後，用 Cloudflare Tunnel 或 ngrok 產生臨時公開網址：

```bash
cloudflared tunnel --url http://localhost:8000
```

把產生的網址給朋友即可（電腦關掉就失效）。

### ⚠️ 上線前安全提醒

目前為 **demo 版、無真正登入**：內建一個 demo 帳號且擁有整套題庫授權，任何拿到網址的人
都能使用全部題目。給朋友測試沒問題，但**請勿公開散布網址**。正式收費前，務必先完成第二階段的
註冊／登入與授權綁定（資料庫的 RLS 與 `entitlements` 已就緒）。
