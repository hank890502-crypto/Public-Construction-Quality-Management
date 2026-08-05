# -*- coding: utf-8 -*-
"""
即時對戰引擎（WebSocket）。與 main.py 同層，於 main.py 以 `import battle` 掛載：
    app.include_router(battle.router)

規則：兩位玩家做同一批題；每題一回合、限時 30 秒；兩人都作答即立刻結束該回合；
答對 +1 分；總題數可自訂。配對支援「房間碼」與「快速配對」。
比分相同 → 比總作答時間，較快者勝；仍相同為平手。中途離線由另一方獲勝。

對戰狀態存在記憶體（單一實例；免費方案 1 個 instance 沒問題）。不需資料表。
可用環境變數調整：BATTLE_ROUND_SECS（預設30）、BATTLE_REVEAL_SECS（預設2.5）。
"""
import os, asyncio, secrets, random
import psycopg2, psycopg2.extras
from fastapi import APIRouter, WebSocket, WebSocketDisconnect

BANK_ID = '00000000-0000-0000-0000-0000000000b1'
ROUND_SECS  = float(os.environ.get('BATTLE_ROUND_SECS', '30'))
REVEAL_SECS = float(os.environ.get('BATTLE_REVEAL_SECS', '2.5'))
MINQ, MAXQ, DEFQ = 3, 50, 10
# 快速配對找不到真人時，等待逾時後派一位電腦對手加入（對外偽裝成一般玩家）
BOT_WAIT_SECS = float(os.environ.get('BATTLE_BOT_WAIT_SECS', '15'))
BOTS_ENABLED  = os.environ.get('BATTLE_BOTS', '1') not in ('0', 'false', 'False', '')

router = APIRouter()

# ---------------- 題目來源（同步；於執行緒池呼叫避免卡住事件迴圈）----------------
def _db():
    conn = psycopg2.connect(os.environ['DATABASE_URL']) if os.environ.get('DATABASE_URL') else psycopg2.connect()
    conn.cursor_factory = psycopg2.extras.RealDictCursor
    return conn

def fetch_questions(n, chapters=None):
    conn = _db(); cur = conn.cursor()
    try:
        cur.execute("""SELECT id FROM bank_versions WHERE bank_id=%s AND status='published'
                       ORDER BY effective_date DESC NULLS LAST LIMIT 1""", (BANK_ID,))
        r = cur.fetchone()
        if not r:
            return []
        ver = r['id']
        params = [ver]
        where = "q.bank_version_id=%s AND q.status='published'"
        if chapters:
            where += " AND q.chapter_id = ANY(%s::uuid[])"
            params.append(list(chapters))
        params.append(n)
        cur.execute(f"""
          SELECT q.original_no AS no, q.stem, c.name AS chapter,
                 coalesce(jsonb_agg(jsonb_build_object('label',o.label,'content',o.content)
                          ORDER BY o.sort_order) FILTER (WHERE o.id IS NOT NULL),'[]') AS options,
                 (SELECT label FROM question_options WHERE question_id=q.id AND is_correct LIMIT 1) AS correct
          FROM questions q JOIN chapters c ON c.id=q.chapter_id
          LEFT JOIN question_options o ON o.question_id=q.id
          WHERE {where}
          GROUP BY q.id, q.original_no, q.stem, c.name
          ORDER BY random() LIMIT %s""", params)
        return cur.fetchall()
    finally:
        conn.close()

async def _questions(n, chapters=None):
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, fetch_questions, n, chapters)

# ---------------- 連線封裝 ----------------
class Conn:
    def __init__(self, ws, token, name):
        self.ws = ws
        self.token = token or ''
        self.name = (name or '玩家')[:20]
        self.inbox = asyncio.Queue()
        self.alive = True
        self.score = 0
        self.total_ms = 0
        self.qcount = DEFQ
        self.qchapters = None
        self.done = asyncio.get_running_loop().create_future()
    async def send(self, obj):
        if not self.alive:
            return
        try:
            await self.ws.send_json(obj)
        except Exception:
            self.alive = False
    def finish(self):
        if not self.done.done():
            self.done.set_result(True)

async def _reader(c):
    """持續把訊息讀進 inbox；斷線時標記並喚醒可能卡住的 handler。"""
    try:
        while True:
            c.inbox.put_nowait(await c.ws.receive_json())
    except Exception:
        c.alive = False
        c.inbox.put_nowait({'t': '__gone__'})
        c.finish()

# ---------------- 配對狀態（記憶體）----------------
_lock  = asyncio.Lock()
_quick = []   # 等待快速配對的 Conn
_rooms = {}   # code -> 等待中的房主 Conn

def _new_code():
    return 'PK-' + ''.join(secrets.choice('ABCDEFGHJKLMNPQRSTUVWXYZ23456789') for _ in range(4))

# ---------------- 電腦對手（快速配對備援；對外偽裝成一般玩家，不顯示為機器人）----------------
_BOT_NAMES = ['阿哲','小明','建豪','家瑋','怡君','昱翔','宗翰','品妍','阿凱','大雄','Kevin','Ryan','Wei',
              '阿翔','阿宏','俊傑','雅婷','子涵','世傑','阿德','浩宇','宇軒','冠廷','思妤','柏翰','阿良']
def _bot_name():
    return random.choice(_BOT_NAMES)

class BotConn:
    """假裝成玩家的電腦對手：攔截 round 訊息，延遲一段擬真時間後把答案放進自己的 inbox。
    介面與 Conn 相容（send/finish/inbox/alive/score/total_ms/done），run_match 無須特別處理。"""
    def __init__(self, name, questions):
        self.ws = None
        self.token = ''
        self.name = name
        self.inbox = asyncio.Queue()
        self.alive = True
        self.score = 0
        self.total_ms = 0
        self.qcount = len(questions)
        self.qchapters = None
        self.done = asyncio.get_running_loop().create_future()
        self.is_bot = True
        self._q = questions
        self._acc = random.uniform(0.55, 0.82)     # 本場實力（答對機率），每場不同
        self._fast = random.uniform(0.7, 1.25)      # 速度係數
    async def send(self, obj):
        if not self.alive:
            return
        if isinstance(obj, dict) and obj.get('t') == 'round':
            asyncio.ensure_future(self._think(obj.get('i'), obj.get('secs', int(ROUND_SECS))))
    def finish(self):
        if not self.done.done():
            self.done.set_result(True)
    async def _think(self, i, secs):
        if random.random() < 0.04:          # 偶爾放空一題（逾時），像真人分心
            return
        delay = min(max(1.6, random.uniform(2.5, 11.0) * self._fast), max(1.6, secs - 1.5))
        try:
            await asyncio.sleep(delay)
        except asyncio.CancelledError:
            return
        if not self.alive:
            return
        q = self._q[i - 1] if isinstance(i, int) and 0 < i <= len(self._q) else None
        if not q:
            return
        correct = q.get('correct')
        opts = [o.get('label') for o in (q.get('options') or []) if o.get('label')]
        if correct and random.random() < self._acc:
            sel = correct
        else:
            wrong = [o for o in opts if o != correct]
            sel = random.choice(wrong) if wrong else correct
        try:
            self.inbox.put_nowait({'t': 'answer', 'i': i, 'selected': sel})
        except Exception:
            pass

async def _bot_fallback(c):
    """快速配對等待逾時仍無真人 → 取出等待者並派電腦對手開賽。"""
    try:
        await asyncio.sleep(BOT_WAIT_SECS + random.uniform(0, 6))
    except asyncio.CancelledError:
        return
    async with _lock:
        if c not in _quick or not c.alive or c.done.done():
            return
        _quick.remove(c)
    qs = await _questions(c.qcount, c.qchapters)
    if not qs:
        async with _lock:
            if c.alive and not c.done.done():
                _quick.append(c)
        return
    asyncio.ensure_future(run_match(c, BotConn(_bot_name(), qs), qs, c.qchapters))

# ---------------- 一場對戰 ----------------
async def run_match(a, b, questions, chapters=None):
    loop = asyncio.get_running_loop()
    total = len(questions)
    # 本場章節範圍：有指定章節 → 列出實際出到的章節；未指定 → 全部章節
    names = list(dict.fromkeys(q['chapter'] for q in questions if q.get('chapter')))
    scope = ('、'.join(names) if names else '指定章節') if chapters else '全部章節'
    await a.send({'t': 'start', 'total': total, 'you': a.name, 'opp': b.name, 'scope': scope})
    await b.send({'t': 'start', 'total': total, 'you': b.name, 'opp': a.name, 'scope': scope})
    try:
        for i, q in enumerate(questions, 1):
            if not (a.alive and b.alive):
                break
            qpay = {'no': q['no'], 'chapter': q['chapter'], 'stem': q['stem'], 'options': list(q['options'])}
            for who, opp in ((a, b), (b, a)):
                await who.send({'t': 'round', 'i': i, 'total': total, 'secs': int(ROUND_SECS),
                                'q': qpay, 'score': {'you': who.score, 'opp': opp.score}})
            picks = {}     # conn -> (label, ms)
            t0 = loop.time(); deadline = t0 + ROUND_SECS
            tasks = {asyncio.ensure_future(a.inbox.get()): a,
                     asyncio.ensure_future(b.inbox.get()): b}
            gone = False
            while len(picks) < 2:
                remaining = deadline - loop.time()
                if remaining <= 0:
                    break
                live = {t for t in tasks if t is not None}
                if not live:
                    break
                done, _pending = await asyncio.wait(live, timeout=remaining, return_when=asyncio.FIRST_COMPLETED)
                if not done:
                    break  # 時間到
                for t in done:
                    c = tasks.pop(t)
                    try:
                        msg = t.result()
                    except Exception:
                        msg = {'t': '__gone__'}
                    if msg.get('t') == '__gone__':
                        gone = True
                    elif msg.get('t') == 'answer' and c not in picks and msg.get('i') == i:
                        picks[c] = (msg.get('selected'), int((loop.time() - t0) * 1000))
                        await c.send({'t': 'locked'})
                    # 若此人尚未作答且仍在線，繼續聽下一則
                    if c not in picks and c.alive and not gone:
                        tasks[asyncio.ensure_future(c.inbox.get())] = c
                if gone:
                    break
            for t in list(tasks):      # 收拾未完成的等待
                if not t.done():
                    t.cancel()
            if gone:
                await _finish_gone(a, b)
                return
            # 結算本回合
            correct = q['correct']
            for c in (a, b):
                sel, ms = picks.get(c, (None, int(ROUND_SECS * 1000)))
                if sel == correct:
                    c.score += 1
                c.total_ms += ms
            for who, opp in ((a, b), (b, a)):
                sw = picks.get(who, (None,))[0]
                so = picks.get(opp, (None,))[0]
                await who.send({'t': 'result', 'i': i, 'correct': correct,
                                'you': {'picked': sw, 'correct': sw == correct, 'score': who.score},
                                'opp': {'picked': so, 'correct': so == correct, 'score': opp.score}})
            await asyncio.sleep(REVEAL_SECS)
        await _finish_scores(a, b)
    finally:
        a.finish(); b.finish()

def _outcome(me, opp):
    if me.score > opp.score: return 'win', 'score'
    if me.score < opp.score: return 'lose', 'score'
    if me.total_ms < opp.total_ms: return 'win', 'speed'
    if me.total_ms > opp.total_ms: return 'lose', 'speed'
    return 'draw', 'draw'

async def _finish_scores(a, b):
    for me, opp in ((a, b), (b, a)):
        oc, reason = _outcome(me, opp)
        await me.send({'t': 'over', 'you': me.score, 'opp': opp.score, 'outcome': oc, 'reason': reason})

async def _finish_gone(a, b):
    for me, opp in ((a, b), (b, a)):
        if me.alive:
            await me.send({'t': 'over', 'you': me.score, 'opp': opp.score, 'outcome': 'win', 'reason': 'opp_left'})

# ---------------- WebSocket 進入點 ----------------
@router.websocket('/ws/battle')
async def battle_ws(ws: WebSocket):
    await ws.accept()
    try:
        hello = await ws.receive_json()
    except Exception:
        try: await ws.close()
        except Exception: pass
        return
    mode  = hello.get('mode', 'quick')
    try:
        count = max(MINQ, min(MAXQ, int(hello.get('count', DEFQ))))
    except Exception:
        count = DEFQ
    c = Conn(ws, hello.get('token'), hello.get('name'))
    c.qcount = count
    _ch = hello.get('chapter_ids')
    c.qchapters = [str(x) for x in _ch][:60] if isinstance(_ch, list) and _ch else None
    rtask = asyncio.ensure_future(_reader(c))
    waiting_code = None; waiting_quick = False
    try:
        if mode == 'join':
            code = (hello.get('code') or '').strip().upper()
            if code and not code.startswith('PK-'):
                code = 'PK-' + code.replace('PK', '').lstrip('-')
            async with _lock:
                host = _rooms.pop(code, None)
            if host is None or not host.alive or host.done.done():
                await c.send({'t': 'error', 'msg': '房間不存在或已關閉'})
                return
            qs = await _questions(host.qcount, host.qchapters)
            if not qs:
                await c.send({'t': 'error', 'msg': '題庫讀取失敗'}); host.finish(); return
            asyncio.ensure_future(run_match(host, c, qs, host.qchapters))
            await c.done

        elif mode == 'create':
            async with _lock:
                code = _new_code()
                while code in _rooms:
                    code = _new_code()
                _rooms[code] = c
            waiting_code = code
            await c.send({'t': 'waiting', 'mode': 'create', 'code': code})
            await c.done

        else:  # quick
            host = None
            async with _lock:
                while _quick:
                    cand = _quick.pop(0)
                    if cand.alive and not cand.done.done():
                        host = cand; break
                if host is None:
                    _quick.append(c); waiting_quick = True
            if host is not None:
                qs = await _questions(host.qcount, host.qchapters)
                if not qs:
                    await c.send({'t': 'error', 'msg': '題庫讀取失敗'}); host.finish(); return
                asyncio.ensure_future(run_match(host, c, qs, host.qchapters))
                await c.done
            else:
                await c.send({'t': 'waiting', 'mode': 'quick'})
                if BOTS_ENABLED:
                    asyncio.ensure_future(_bot_fallback(c))
                await c.done
    except WebSocketDisconnect:
        c.alive = False
    except Exception:
        c.alive = False
    finally:
        async with _lock:
            if waiting_code and _rooms.get(waiting_code) is c:
                _rooms.pop(waiting_code, None)
            if waiting_quick and c in _quick:
                _quick.remove(c)
        c.finish()
        rtask.cancel()
        try:
            await ws.close()
        except Exception:
            pass
