# -*- coding: utf-8 -*-
"""
看護業務量調査ツール (nursing work-time logger)
------------------------------------------------
目的: AI導入前後の効果測定ベースライン + 多職種協働加算エビデンス

記録方式: 打刻式（状態遷移モデル）
  看護師は「今やっている業務」の区分ボタンを1タップするだけ。
  次のタップまでの時間が、その業務の所要時間になる。
  最後は「記録終了」ボタンで締める（最終区間を確定させる）。

PHIは一切扱わない。記録するのは業務区分と時刻のみ。
スタッフは匿名ID（例: N-01）で識別する。

依存: fastapi, uvicorn （SQLiteは標準ライブラリ）
起動: uvicorn main:app --host 0.0.0.0 --port 8301
"""

import csv
import io
import os
import sqlite3
from contextlib import closing
from datetime import datetime, timedelta

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import (HTMLResponse, StreamingResponse, JSONResponse,
                               FileResponse, RedirectResponse, PlainTextResponse)
from pydantic import BaseModel

# ---------------------------------------------------------------------------
# 設定
# ---------------------------------------------------------------------------
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "worktime.db")

# --- DB バックエンド（PostgreSQL 優先 / 無ければ SQLite にフォールバック）---
# 施設では config_local.py に DB を定義：
#   DB = {"backend":"postgres","host":"127.0.0.1","port":5432,
#         "dbname":"worktime","user":"nwt","password":"..."}
try:
    from config_local import DB as _DBCFG      # type: ignore
except Exception:
    _DBCFG = None
if _DBCFG is None and os.environ.get("NWT_DB_BACKEND") == "postgres":
    _DBCFG = {
        "backend": "postgres",
        "host": os.environ.get("NWT_DB_HOST", "127.0.0.1"),
        "port": int(os.environ.get("NWT_DB_PORT", "5432")),
        "dbname": os.environ.get("NWT_DB_NAME", "worktime"),
        "user": os.environ.get("NWT_DB_USER", "nwt"),
        "password": os.environ.get("NWT_DB_PASSWORD", ""),
    }
BACKEND = (_DBCFG or {}).get("backend", "sqlite")

# --- 認証（インターネット公開時に必須。未設定なら認証なし＝院内LAN運用の後方互換）---
# config_local.py に:
#   AUTH = {"password": "共有パスワード", "api_key": "Excel用のキー", "secret": "任意の長い文字列"}
# または環境変数 NWT_PASSWORD / NWT_API_KEY / NWT_SECRET
try:
    from config_local import AUTH as _AUTHCFG      # type: ignore
except Exception:
    _AUTHCFG = None
_AUTHCFG = _AUTHCFG or {}
AUTH_PASSWORD = _AUTHCFG.get("password") or os.environ.get("NWT_PASSWORD", "")
AUTH_API_KEY = _AUTHCFG.get("api_key") or os.environ.get("NWT_API_KEY", "")
AUTH_SECRET = (_AUTHCFG.get("secret") or os.environ.get("NWT_SECRET", "")
               or (AUTH_PASSWORD + "|nwt-session-secret"))
AUTH_ENABLED = bool(AUTH_PASSWORD)
COOKIE_NAME = "nwt_session"
COOKIE_DAYS = 30            # モバイルで毎回ログインさせないため長め

# --- 公開パス接頭辞（既存サイトのサブパスに相乗りする場合に使う）---
# 例: nginx が location /nwt/ で接頭辞を剥がして 127.0.0.1:8300 に渡す構成なら BASE_PATH="/nwt"。
# アプリ自身は常に接頭辞なしのパス（/api/... 等）で動き、
# 外向きに出す URL（リダイレクト・manifest・HTML内のリンク）にだけ接頭辞を付ける。
# 未設定（既定）なら "" ＝ルート直下＝従来どおりの動作。
try:
    from config_local import BASE_PATH as _BASEPATH   # type: ignore
except Exception:
    _BASEPATH = None
BASE_PATH = (_BASEPATH if _BASEPATH is not None
             else os.environ.get("NWT_BASE_PATH", "")).rstrip("/")
if BASE_PATH and not BASE_PATH.startswith("/"):
    BASE_PATH = "/" + BASE_PATH
BASE_URL = BASE_PATH + "/"   # HTML の相対リンク用。ルート運用なら "/"

# 打刻間隔がこの分数を超えたら「打ち忘れ疑い」として集計から除外できるよう flag を立てる
MAX_INTERVAL_MIN = 240

# 病棟・勤務帯・スタッフ（施設固有値）
# 実際の病棟名・スタッフIDは config_local.py（gitignore対象）に置くと、
# 公開リポジトリに施設固有情報を出さずに運用できる。無ければ以下の汎用値を使う。
try:
    from config_local import WARDS, SHIFTS, STAFF  # noqa: F401  施設固有・非公開
except ImportError:
    WARDS = ["病棟A", "病棟B", "病棟C", "その他"]
    SHIFTS = ["日勤", "夜勤", "早出", "遅出"]
    # スタッフ匿名ID候補（自由入力も可）。氏名は入れない運用を推奨。
    STAFF = [f"N-{i:02d}" for i in range(1, 21)]

# ---------------------------------------------------------------------------
# 業務区分定義（★ここが調査設計の心臓部。施設に合わせて編集する★）
#   施設の「看護業務量集計用紙」に準拠（大分類A〜D → 中分類 → 小分類）。
#   ai_tool を付けた小分類が、AI効果測定の対象（ベースライン→導入後で比較）。
# ---------------------------------------------------------------------------
CATEGORIES = [
    # ---- A 患者中心の看護活動 ----
    {
        "key": "direct1", "label": "直接看護Ⅰ", "group": "A 患者中心の看護活動", "color": "#0f766e",
        "subs": [
            {"key": "hygiene",   "label": "清潔"},
            {"key": "excretion", "label": "排泄"},
            {"key": "meal",      "label": "食事"},
            {"key": "safety",    "label": "安全・安楽"},
            {"key": "obs",       "label": "測定・観察"},
            {"key": "comm",      "label": "コミュニケーション"},
            {"key": "roomenv",   "label": "病室環境整備"},
        ],
    },
    {
        "key": "direct2", "label": "直接看護Ⅱ", "group": "A 患者中心の看護活動", "color": "#0d9488",
        "subs": [
            {"key": "proc",        "label": "看護処置", "note": "看護師独自又は医師の指示により看護師が実施"},
            {"key": "proc_assist", "label": "処置の介助", "note": "医師と共に実施"},
            {"key": "med_oral",    "label": "与薬（内服・外用・他）"},
            {"key": "med_inj",     "label": "与薬（注射）"},
            {"key": "round",       "label": "回診の介助"},
            {"key": "exam",        "label": "検査"},
            {"key": "patrol",      "label": "病室巡視"},
            {"key": "me_prep",     "label": "MEの使用準備"},
        ],
    },
    {
        "key": "direct3", "label": "直接看護Ⅲ", "group": "A 患者中心の看護活動", "color": "#14b8a6",
        "subs": [
            {"key": "guidance",  "label": "患者指導"},
            {"key": "transport", "label": "患者搬送"},
            {"key": "errand",    "label": "患者の用事"},
            {"key": "transfer",  "label": "転室・転棟"},
        ],
    },
    {
        "key": "indirect", "label": "間接看護", "group": "A 患者中心の看護活動", "color": "#b45309",
        "subs": [
            {"key": "record",     "label": "看護記録", "ai_tool": "medai SOAP Voice"},
            {"key": "careplan",   "label": "看護計画"},
            {"key": "summary",    "label": "サマリー", "ai_tool": "SummyAI"},
            {"key": "fee_record", "label": "診療報酬に係る記録",
             "note": "入退院診療計画書・褥瘡・SGA等、但し看護必要度は含まない"},
            {"key": "handover",   "label": "引継ぎ"},
            {"key": "conf",       "label": "カンファレンス", "ai_tool": "議事録自動生成"},
            {"key": "order",      "label": "指示受け・報告"},
            {"key": "info",       "label": "情報収集"},
        ],
    },
    # ---- B 看護単位中心の活動 ----
    {
        "key": "supplies", "label": "物品管理", "group": "B 看護単位中心の活動", "color": "#2563eb",
        "subs": [
            {"key": "drug",       "label": "薬品"},
            {"key": "consumable", "label": "消耗品"},
            {"key": "sterile",    "label": "滅菌器材"},
        ],
    },
    {
        "key": "env", "label": "環境整備", "group": "B 看護単位中心の活動", "color": "#3b82f6",
        "subs": [
            {"key": "housekeeping", "label": "ハウスキーピング"},
            {"key": "equipment",    "label": "器材の整備"},
            {"key": "wipecart",     "label": "清拭車の準備"},
        ],
    },
    {
        "key": "clerical", "label": "事務", "group": "B 看護単位中心の活動", "color": "#6366f1",
        "subs": [
            {"key": "general",         "label": "事務一般"},
            {"key": "transport_other", "label": "搬送（患者以外）"},
        ],
    },
    {
        "key": "contact", "label": "連絡", "group": "B 看護単位中心の活動", "color": "#7c3aed",
        "subs": [
            {"key": "nursecall", "label": "ナースコール"},
            {"key": "phone",     "label": "電話連絡"},
            {"key": "waiting",   "label": "待ち時間"},
        ],
    },
    # ---- C 職員中心の活動 ----
    {
        "key": "admin_edu", "label": "管理・教育", "group": "C 職員中心の活動", "color": "#475569",
        "subs": [
            {"key": "admin",    "label": "管理業務"},
            {"key": "training", "label": "研修"},
            {"key": "student",  "label": "学生指導"},
        ],
    },
    # ---- D その他の活動 ----
    {
        "key": "personal", "label": "私用", "group": "D その他の活動", "color": "#64748b",
        "subs": [
            {"key": "break",  "label": "食事・休憩"},
            {"key": "toilet", "label": "私用（トイレ）"},
        ],
    },
]

# 区分キー → ラベル / AIツール の逆引き辞書を構築
SUB_LABEL = {}
SUB_AI = {}
CAT_LABEL = {}
CAT_GROUP = {}  # 中分類キー → 大分類（A〜D）。集計用紙の見出しに対応する。
# 区分定義から消えたキーの打刻は、ここにまとめて「未定義」として可視化する
UNDEFINED_GROUP = "※ 定義外（区分変更前の記録）"
for c in CATEGORIES:
    CAT_LABEL[c["key"]] = c["label"]
    CAT_GROUP[c["key"]] = c.get("group", "")
    for s in c["subs"]:
        SUB_LABEL[(c["key"], s["key"])] = s["label"]
        if s.get("ai_tool"):
            SUB_AI[(c["key"], s["key"])] = s["ai_tool"]

# ラベル→キー 逆引き（Excel等がラベルで送ってくる集計エントリをキーに正規化）
LABEL_TO_CAT = {c["label"]: c["key"] for c in CATEGORIES}
LABEL_TO_SUB = {(c["key"], s["label"]): s["key"] for c in CATEGORIES for s in c["subs"]}

def resolve_labels(mid_label, sub_label):
    ck = LABEL_TO_CAT.get((mid_label or "").strip())
    if ck is None:
        return None, None
    return ck, LABEL_TO_SUB.get((ck, (sub_label or "").strip()))

# ---------------------------------------------------------------------------
# DB
# ---------------------------------------------------------------------------
class _PGCur:
    """psycopg2カーソルを sqlite 風(fetchone/fetchall)に見せる薄いラッパ。"""
    def __init__(self, cur): self._cur = cur
    def fetchall(self): return self._cur.fetchall()
    def fetchone(self): return self._cur.fetchone()
    def __iter__(self): return iter(self._cur.fetchall())


class _Conn:
    """SQLite / PostgreSQL を同じ conn.execute(sql, params).fetch...() で扱う。"""
    def __init__(self):
        if BACKEND == "postgres":
            import psycopg2, psycopg2.extras
            self._extras = psycopg2.extras
            params = {k: _DBCFG[k] for k in ("host", "port", "dbname", "user", "password") if k in _DBCFG}
            self.raw = psycopg2.connect(**params)
        else:
            self.raw = sqlite3.connect(DB_PATH)
            self.raw.row_factory = sqlite3.Row

    def execute(self, sql, params=()):
        if BACKEND == "postgres":
            cur = self.raw.cursor(cursor_factory=self._extras.RealDictCursor)
            cur.execute(sql.replace("?", "%s"), tuple(params))
            return _PGCur(cur)
        return self.raw.execute(sql, params)

    def commit(self): self.raw.commit()
    def close(self): self.raw.close()


def get_conn():
    return _Conn()


def init_db():
    with closing(get_conn()) as conn:
        if BACKEND == "postgres":
            conn.execute("""CREATE TABLE IF NOT EXISTS punches (
                id SERIAL PRIMARY KEY, staff_id TEXT NOT NULL, ward TEXT NOT NULL, shift TEXT NOT NULL,
                category TEXT NOT NULL, subcategory TEXT, ts TEXT NOT NULL, note TEXT,
                overtime BOOLEAN NOT NULL DEFAULT FALSE)""")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_staff_ts ON punches(staff_id, ts)")
            conn.execute("""CREATE TABLE IF NOT EXISTS entries (
                id SERIAL PRIMARY KEY, staff_id TEXT NOT NULL, ward TEXT NOT NULL, shift TEXT NOT NULL,
                work_date TEXT NOT NULL, category TEXT NOT NULL, subcategory TEXT, minutes INTEGER NOT NULL,
                overtime BOOLEAN NOT NULL DEFAULT FALSE, source TEXT DEFAULT 'excel', batch_id TEXT, created_at TEXT)""")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_entries_date ON entries(work_date)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_entries_batch ON entries(batch_id)")
        else:
            conn.execute("""CREATE TABLE IF NOT EXISTS punches (
                id INTEGER PRIMARY KEY AUTOINCREMENT, staff_id TEXT NOT NULL, ward TEXT NOT NULL, shift TEXT NOT NULL,
                category TEXT NOT NULL, subcategory TEXT, ts TEXT NOT NULL, note TEXT,
                overtime INTEGER NOT NULL DEFAULT 0)""")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_staff_ts ON punches(staff_id, ts)")
            cols = {r["name"] for r in conn.execute("PRAGMA table_info(punches)").fetchall()}
            if "overtime" not in cols:
                conn.execute("ALTER TABLE punches ADD COLUMN overtime INTEGER NOT NULL DEFAULT 0")
            conn.execute("""CREATE TABLE IF NOT EXISTS entries (
                id INTEGER PRIMARY KEY AUTOINCREMENT, staff_id TEXT NOT NULL, ward TEXT NOT NULL, shift TEXT NOT NULL,
                work_date TEXT NOT NULL, category TEXT NOT NULL, subcategory TEXT, minutes INTEGER NOT NULL,
                overtime INTEGER NOT NULL DEFAULT 0, source TEXT DEFAULT 'excel', batch_id TEXT, created_at TEXT)""")
        conn.commit()


init_db()

# ---------------------------------------------------------------------------
# FastAPI
# ---------------------------------------------------------------------------
app = FastAPI(title="看護業務量調査ツール")


# ===================== 認証（Cookieセッション + APIキー） =====================
import hashlib
import hmac as _hmac
import time as _time
from html import escape as _esc
from urllib.parse import quote

_PUBLIC_PATHS = {"/health", "/login", "/manifest.json", "/favicon.ico", "/robots.txt"}


def _sign(value: str) -> str:
    return _hmac.new(AUTH_SECRET.encode(), value.encode(), hashlib.sha256).hexdigest()[:32]


def make_token(days: int = COOKIE_DAYS) -> str:
    exp = str(int(_time.time()) + days * 86400)
    return f"{exp}.{_sign(exp)}"


def token_valid(token: str) -> bool:
    try:
        exp, sig = (token or "").split(".", 1)
    except ValueError:
        return False
    if not _hmac.compare_digest(sig, _sign(exp)):
        return False
    return int(exp) > _time.time()


def _is_public(path: str) -> bool:
    return path in _PUBLIC_PATHS or path.startswith("/icons/")


def _safe_next(nxt: str) -> str:
    """ログイン後の遷移先。自サイト内の絶対パスだけを許可する。

    "//evil.example" や "/\\evil.example" はブラウザが外部URLとして解釈するため弾く
    （オープンリダイレクト対策）。
    """
    nxt = nxt or "/"
    if not nxt.startswith("/") or nxt.startswith("//") or nxt.startswith("/\\"):
        return "/"
    return nxt


def _ext(path: str) -> str:
    """アプリ内パス（接頭辞なし）を、ブラウザに返す外向きURLに変換する。"""
    return BASE_PATH + path if path.startswith("/") else path


@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    if not AUTH_ENABLED or _is_public(request.url.path):
        return await call_next(request)

    # Excel など機械からのアクセスは APIキー
    key = request.headers.get("X-API-Key", "")
    if AUTH_API_KEY and _hmac.compare_digest(key, AUTH_API_KEY):
        return await call_next(request)

    # ブラウザは Cookie セッション
    if token_valid(request.cookies.get(COOKIE_NAME, "")):
        return await call_next(request)

    if request.url.path.startswith("/api/"):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    nxt = quote(_safe_next(request.url.path), safe="/")
    return RedirectResponse(_ext(f"/login?next={nxt}"), status_code=302)


LOGIN_HTML = """<!DOCTYPE html><html lang="ja"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<title>ログイン ｜ 看護業務量調査</title>
<style>
 *{box-sizing:border-box}
 body{margin:0;min-height:100vh;display:flex;align-items:center;justify-content:center;
   background:#f6f8f9;font-family:-apple-system,"Hiragino Kaku Gothic ProN","Noto Sans JP",sans-serif;color:#0f172a;padding:20px}
 .card{background:#fff;border:1px solid #e2e8ee;border-radius:18px;padding:28px 24px;width:100%;max-width:380px;
   box-shadow:0 10px 30px rgba(15,23,42,.07)}
 h1{font-size:19px;margin:0 0 6px} p{margin:0 0 18px;color:#5b6b7a;font-size:13px;line-height:1.7}
 label{display:block;font-size:12px;font-weight:800;color:#475569;margin-bottom:6px}
 input{width:100%;padding:14px;font-size:16px;border:1px solid #cbd5e1;border-radius:12px;background:#fff}
 button{width:100%;margin-top:14px;padding:15px;font-size:16px;font-weight:800;color:#fff;background:#0f766e;
   border:0;border-radius:12px}
 .err{background:#fef2f2;border:1px solid #fecaca;color:#b91c1c;border-radius:10px;padding:10px 12px;
   font-size:13px;margin-bottom:14px;font-weight:700}
</style></head><body>
<form class="card" method="post" action="__ACTION__">
  <h1>看護業務量調査</h1>
  <p>共有パスワードを入力してください。<br>一度ログインすると、しばらく再入力は不要です。</p>
  __ERR__
  <input type="hidden" name="next" value="__NEXT__">
  <label for="pw">パスワード</label>
  <input id="pw" name="password" type="password" autocomplete="current-password" required autofocus>
  <button type="submit">ログイン</button>
</form></body></html>"""


@app.get("/login", response_class=HTMLResponse)
def login_page(next: str = "/", error: int = 0):
    err = '<div class="err">パスワードが違います</div>' if error else ""
    # next は利用者由来なので、属性値として必ずエスケープする
    safe_next = _esc(_safe_next(next), quote=True)
    return HTMLResponse(LOGIN_HTML
                        .replace("__ERR__", err)
                        .replace("__ACTION__", _ext("/login"))
                        .replace("__NEXT__", safe_next))


@app.post("/login")
async def login_submit(request: Request):
    form = await request.form()
    password = str(form.get("password", ""))
    nxt = _safe_next(str(form.get("next", "/")))
    if not AUTH_ENABLED or _hmac.compare_digest(password, AUTH_PASSWORD):
        resp = RedirectResponse(_ext(nxt), status_code=303)
        resp.set_cookie(
            COOKIE_NAME, make_token(), max_age=COOKIE_DAYS * 86400,
            httponly=True, samesite="lax", path=BASE_URL,
            secure=(os.environ.get("NWT_COOKIE_SECURE", "1") == "1"),
        )
        return resp
    return RedirectResponse(_ext(f"/login?error=1&next={quote(nxt, safe='/')}"), status_code=303)


@app.get("/logout")
def logout():
    resp = RedirectResponse(_ext("/login"), status_code=303)
    resp.delete_cookie(COOKIE_NAME, path=BASE_URL)
    return resp


@app.get("/robots.txt", response_class=PlainTextResponse)
def robots():
    return "User-agent: *\nDisallow: /\n"



class Punch(BaseModel):
    staff_id: str
    ward: str
    shift: str
    category: str
    subcategory: str | None = None
    note: str | None = None
    ts: str | None = None  # クライアント側のオフライン打刻用（省略時はサーバ時刻）
    overtime: bool = False  # True=勤務時間外。記録画面のトグルで切り替える


def parse_ts(value: str | None) -> datetime:
    """クライアントの打刻時刻を、DB保存形式（ローカル時刻・tz無し）に正規化する。

    ブラウザは toISOString() で UTC の "Z" 付きを送ってくる。Python 3.10 の
    fromisoformat は "Z" を解釈できないため、"+00:00" に直してから解析し、
    ローカル時刻に変換して tz を落とす。ここで取りこぼすと、オフライン退避
    された打刻が「再送した時刻」に化けて所要時間が壊れる。
    """
    if not value:
        return datetime.now()
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return datetime.now()
    if dt.tzinfo is not None:
        dt = dt.astimezone().replace(tzinfo=None)
    return dt


@app.get("/api/config")
def api_config():
    return {
        "categories": CATEGORIES,
        "wards": WARDS,
        "shifts": SHIFTS,
        "staff": STAFF,
    }


@app.post("/api/punch")
def api_punch(p: Punch):
    ts = parse_ts(p.ts)
    with closing(get_conn()) as conn:
        cur = conn.execute(
            "INSERT INTO punches (staff_id, ward, shift, category, subcategory, ts, note, overtime) "
            "VALUES (?,?,?,?,?,?,?,?) RETURNING id",
            (p.staff_id, p.ward, p.shift, p.category, p.subcategory,
             ts.isoformat(timespec="seconds"), p.note, bool(p.overtime)),
        )
        row = cur.fetchone()
        conn.commit()
        pid = row["id"] if row else None
    return {"ok": True, "id": pid, "ts": ts.isoformat(timespec="seconds")}


@app.post("/api/end")
def api_end(p: Punch):
    """記録終了マーカー。最終区間を確定させる。"""
    ts = parse_ts(p.ts)
    with closing(get_conn()) as conn:
        conn.execute(
            "INSERT INTO punches (staff_id, ward, shift, category, subcategory, ts, note, overtime) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (p.staff_id, p.ward, p.shift, "END", None,
             ts.isoformat(timespec="seconds"), "記録終了", bool(p.overtime)),
        )
        conn.commit()
    return {"ok": True, "ts": ts.isoformat(timespec="seconds")}


@app.get("/api/current")
def api_current(staff_id: str):
    """そのスタッフの最後の打刻（現在記録中の業務）を返す。"""
    with closing(get_conn()) as conn:
        row = conn.execute(
            "SELECT * FROM punches WHERE staff_id=? ORDER BY ts DESC, id DESC LIMIT 1",
            (staff_id,),
        ).fetchone()
    if not row:
        return {"active": False}
    if row["category"] == "END":
        return {"active": False, "last_end": row["ts"]}
    return {
        "active": True,
        "id": row["id"],
        "category": row["category"],
        "subcategory": row["subcategory"],
        "label": SUB_LABEL.get((row["category"], row["subcategory"]), row["category"]),
        "since": row["ts"],
    }


@app.post("/api/undo")
def api_undo(staff_id: str):
    """直近の打刻を取り消す（押し間違い訂正）。"""
    with closing(get_conn()) as conn:
        row = conn.execute(
            "SELECT id FROM punches WHERE staff_id=? ORDER BY ts DESC, id DESC LIMIT 1",
            (staff_id,),
        ).fetchone()
        if not row:
            raise HTTPException(404, "取り消せる打刻がありません")
        conn.execute("DELETE FROM punches WHERE id=?", (row["id"],))
        conn.commit()
    return {"ok": True, "deleted": row["id"]}


def _load_intervals(where_sql: str, params: list):
    """打刻列から区間（duration付き）を組み立てる。"""
    with closing(get_conn()) as conn:
        rows = conn.execute(
            f"SELECT * FROM punches {where_sql} ORDER BY staff_id, ts, id", params
        ).fetchall()

    intervals = []
    by_staff: dict[str, list] = {}
    for r in rows:
        by_staff.setdefault(r["staff_id"], []).append(r)

    for staff, plist in by_staff.items():
        for i in range(len(plist)):
            cur = plist[i]
            if cur["category"] == "END":
                continue
            if i + 1 >= len(plist):
                # 締めがない最終打刻はスキップ（未確定区間）
                continue
            nxt = plist[i + 1]
            t0 = datetime.fromisoformat(cur["ts"])
            t1 = datetime.fromisoformat(nxt["ts"])
            minutes = (t1 - t0).total_seconds() / 60.0
            if minutes <= 0:
                continue
            flag = minutes > MAX_INTERVAL_MIN  # 打ち忘れ疑い
            intervals.append({
                "staff_id": staff,
                "ward": cur["ward"],
                "shift": cur["shift"],
                "category": cur["category"],
                "subcategory": cur["subcategory"],
                "overtime": bool(cur["overtime"]),
                # 区分定義を変えると、それ以前の打刻のキーが CATEGORIES から消える。
                # 黙ってラベル空欄で混ぜず、「未定義」と分かる形で出す。
                "group": CAT_GROUP.get(cur["category"], UNDEFINED_GROUP),
                "cat_label": CAT_LABEL.get(cur["category"], f"未定義({cur['category']})"),
                "sub_label": SUB_LABEL.get(
                    (cur["category"], cur["subcategory"]), f"未定義({cur['subcategory']})"
                ),
                "undefined": (cur["category"], cur["subcategory"]) not in SUB_LABEL,
                "ai_tool": SUB_AI.get((cur["category"], cur["subcategory"]), ""),
                "start": cur["ts"],
                "end": nxt["ts"],
                "minutes": round(minutes, 1),
                "suspect": flag,
                "source": "web",       # Web打刻由来（区間は実時刻から算出）
            })
    return intervals


def _load_entry_intervals(frm, to, ward, shift):
    """entries（Excel等の集計済み）を、集計に合流できる区間形に変換する。"""
    where = ["1=1"]; params = []
    if frm: where.append("work_date >= ?"); params.append(frm)
    if to:  where.append("work_date <= ?"); params.append(to)
    if ward: where.append("ward = ?"); params.append(ward)
    if shift: where.append("shift = ?"); params.append(shift)
    sql = "SELECT * FROM entries WHERE " + " AND ".join(where)
    with closing(get_conn()) as conn:
        rows = conn.execute(sql, params).fetchall()
    out = []
    for e in rows:
        d = (e["work_date"] or "")[:10] + "T00:00:00"
        out.append({
            "staff_id": e["staff_id"], "ward": e["ward"], "shift": e["shift"],
            "category": e["category"], "subcategory": e["subcategory"],
            "overtime": bool(e["overtime"]),
            "group": CAT_GROUP.get(e["category"], UNDEFINED_GROUP),
            "cat_label": CAT_LABEL.get(e["category"], f"未定義({e['category']})"),
            "sub_label": SUB_LABEL.get((e["category"], e["subcategory"]), f"未定義({e['subcategory']})"),
            "undefined": (e["category"], e["subcategory"]) not in SUB_LABEL,
            "ai_tool": SUB_AI.get((e["category"], e["subcategory"]), ""),
            "start": d, "end": d,
            "minutes": round(float(e["minutes"] or 0), 1),
            "suspect": False,
            # 投入元（既定は Excel）。CSVでWeb打刻と区別するために持ち回る。
            "source": (e["source"] if "source" in e.keys() else None) or "excel",
        })
    return out


def _build_filter(frm, to, ward, shift):
    where = ["1=1"]
    params: list = []
    if frm:
        where.append("ts >= ?"); params.append(frm)
    if to:
        # to は日付想定。翌日0時未満まで含める
        where.append("ts < ?"); params.append(to + "T23:59:59")
    if ward:
        where.append("ward = ?"); params.append(ward)
    if shift:
        where.append("shift = ?"); params.append(shift)
    return "WHERE " + " AND ".join(where), params


class BulkEntry(BaseModel):
    staff_id: str
    ward: str
    shift: str
    date: str
    batch_id: str | None = None
    rows: list[dict] = []


@app.post("/api/entries/bulk")
def api_entries_bulk(b: BulkEntry):
    """Excel等からの集計済みエントリを一括投入。同じ batch_id は置き換え（再送で重複しない）。
    rows 例: [{"mid":"間接看護","sub":"看護記録","in":30,"out":0}, ...]"""
    batch = b.batch_id or f"{b.staff_id}|{b.ward}|{b.shift}|{b.date}"
    now = datetime.now().isoformat(timespec="seconds")
    inserted = 0
    with closing(get_conn()) as conn:
        conn.execute("DELETE FROM entries WHERE batch_id=?", (batch,))
        for r in b.rows:
            ck, sk = resolve_labels(r.get("mid", ""), r.get("sub", ""))
            if ck is None:
                continue
            for ot, mins in ((False, r.get("in", 0)), (True, r.get("out", 0))):
                try:
                    m = int(mins or 0)
                except (TypeError, ValueError):
                    m = 0
                if m <= 0:
                    continue
                conn.execute(
                    "INSERT INTO entries (staff_id,ward,shift,work_date,category,subcategory,minutes,overtime,source,batch_id,created_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (b.staff_id, b.ward, b.shift, b.date, ck, sk, m, bool(ot), "excel", batch, now),
                )
                inserted += 1
        conn.commit()
    return {"ok": True, "batch_id": batch, "inserted": inserted}


@app.get("/api/summary")
def api_summary(
    frm: str | None = Query(None, alias="from"),
    to: str | None = None,
    ward: str | None = None,
    shift: str | None = None,
    include_suspect: bool = False,
):
    where, params = _build_filter(frm, to, ward, shift)
    intervals = _load_intervals(where, params)
    intervals += _load_entry_intervals(frm, to, ward, shift)   # Excel等の集計も同じ土俵に乗せる
    if not include_suspect:
        intervals = [x for x in intervals if not x["suspect"]]

    total = sum(x["minutes"] for x in intervals)
    denom = total or 1.0  # 0件のとき総計を1.0分と偽らないよう、割り算用の分母だけ分ける

    # スタッフ×シフト（=1人1回の勤務）の数で「1シフトあたり」を出す
    sessions = set((x["staff_id"], x["start"][:10], x["shift"]) for x in intervals)
    n_sessions = max(len(sessions), 1)

    # 小分類別集計。集計用紙に合わせ、勤務時間内/外を分けて持つ。
    sub_agg: dict = {}
    for x in intervals:
        k = (x["category"], x["subcategory"])
        a = sub_agg.setdefault(k, {
            "category": x["category"], "cat_label": x["cat_label"], "group": x["group"],
            "subcategory": x["subcategory"], "sub_label": x["sub_label"],
            "ai_tool": x["ai_tool"], "minutes": 0.0, "count": 0,
            "in_minutes": 0.0, "ot_minutes": 0.0,
        })
        a["minutes"] += x["minutes"]
        a["ot_minutes" if x["overtime"] else "in_minutes"] += x["minutes"]
        a["count"] += 1

    sub_list = []
    for a in sub_agg.values():
        for k in ("minutes", "in_minutes", "ot_minutes"):
            a[k] = round(a[k], 1)
        a["pct"] = round(a["minutes"] / denom * 100, 1)
        a["min_per_session"] = round(a["minutes"] / n_sessions, 1)
        sub_list.append(a)
    sub_list.sort(key=lambda z: -z["minutes"])

    # 中分類別集計（＝集計用紙の「合計」行）。用紙の並び順を保つ。
    order = {c["key"]: i for i, c in enumerate(CATEGORIES)}
    cat_agg: dict = {}
    for x in intervals:
        a = cat_agg.setdefault(x["category"], {
            "category": x["category"], "cat_label": x["cat_label"], "group": x["group"],
            "minutes": 0.0, "in_minutes": 0.0, "ot_minutes": 0.0,
        })
        a["minutes"] += x["minutes"]
        a["ot_minutes" if x["overtime"] else "in_minutes"] += x["minutes"]
    cat_list = []
    for a in cat_agg.values():
        for k in ("minutes", "in_minutes", "ot_minutes"):
            a[k] = round(a[k], 1)
        a["pct"] = round(a["minutes"] / denom * 100, 1)
        cat_list.append(a)
    cat_list.sort(key=lambda z: order.get(z["category"], 999))

    # 大分類（A〜D）別集計
    group_agg: dict = {}
    for x in intervals:
        a = group_agg.setdefault(x["group"], {
            "group": x["group"], "minutes": 0.0, "in_minutes": 0.0, "ot_minutes": 0.0,
        })
        a["minutes"] += x["minutes"]
        a["ot_minutes" if x["overtime"] else "in_minutes"] += x["minutes"]
    group_list = []
    for a in group_agg.values():
        for k in ("minutes", "in_minutes", "ot_minutes"):
            a[k] = round(a[k], 1)
        a["pct"] = round(a["minutes"] / denom * 100, 1)
        group_list.append(a)
    group_list.sort(key=lambda z: z["group"])

    # 総計（集計用紙の最下段）
    total_in = round(sum(x["minutes"] for x in intervals if not x["overtime"]), 1)
    total_ot = round(sum(x["minutes"] for x in intervals if x["overtime"]), 1)

    # AI効果測定ベースライン（ai_tool付きの小分類のみ抽出）
    ai_baseline = [a for a in sub_list if a["ai_tool"]]

    return {
        "total_minutes": round(total, 1),
        "total_in_minutes": total_in,
        "total_ot_minutes": total_ot,
        "n_sessions": n_sessions,
        "n_intervals": len(intervals),
        "groups": group_list,
        "categories": cat_list,
        "subcategories": sub_list,
        "ai_baseline": ai_baseline,
    }


@app.get("/api/export.csv")
def api_export(
    frm: str | None = Query(None, alias="from"),
    to: str | None = None,
    ward: str | None = None,
    shift: str | None = None,
    include_suspect: bool = True,
    include_excel: bool = True,
):
    where, params = _build_filter(frm, to, ward, shift)
    intervals = _load_intervals(where, params)
    if include_excel:
        # /api/summary と同じ土俵に揃える。これを入れないとExcel投入分がCSVから丸ごと落ちる。
        intervals += _load_entry_intervals(frm, to, ward, shift)
    if not include_suspect:
        intervals = [x for x in intervals if not x["suspect"]]

    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["staff_id", "ward", "shift", "大分類", "中分類", "小分類",
                "AIツール", "開始", "終了", "所要分", "勤務時間内", "勤務時間外",
                "時間区分", "打ち忘れ疑い", "データ元"])
    for x in intervals:
        src = x.get("source", "web")
        if src == "web":
            start, end = x["start"], x["end"]
        else:
            # 集計済み投入は実時刻を持たない。0時の打刻に見えないよう日付だけ出し、終了は空。
            start, end = x["start"][:10], ""
        w.writerow([x["staff_id"], x["ward"], x["shift"], x["group"], x["cat_label"], x["sub_label"],
                    x["ai_tool"], start, end, x["minutes"],
                    "" if x["overtime"] else x["minutes"],
                    x["minutes"] if x["overtime"] else "",
                    "勤務時間外" if x["overtime"] else "勤務時間内",
                    "○" if x["suspect"] else "",
                    "Web打刻" if src == "web" else "Excel"])
    buf.seek(0)
    fname = f"worktime_{datetime.now():%Y%m%d_%H%M}.csv"
    # Excel(日本語)向けにBOM付きUTF-8
    data = ("\ufeff" + buf.getvalue()).encode("utf-8")
    return StreamingResponse(
        io.BytesIO(data),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{fname}"'},
    )


@app.get("/health")
def health():
    """疎通確認用。AQUOSのブラウザでこのURLを開いて {\"ok\":true} が出れば到達成功。"""
    return {"ok": True, "app": "nursing-worktime", "time": datetime.now().isoformat(timespec="seconds")}


@app.get("/manifest.json")
def manifest():
    return JSONResponse({
        "name": "看護業務量調査",
        "short_name": "業務量調査",
        "start_url": BASE_URL,
        "scope": BASE_URL,
        "display": "standalone",
        "orientation": "portrait",
        "background_color": "#eef2f4",
        "theme_color": "#0f766e",
        "icons": [
            {"src": _ext("/icons/icon-192.png"), "sizes": "192x192", "type": "image/png"},
            {"src": _ext("/icons/icon-512.png"), "sizes": "512x512", "type": "image/png"},
            {"src": _ext("/icons/icon-maskable.png"), "sizes": "512x512",
             "type": "image/png", "purpose": "maskable"},
        ],
    })


@app.get("/icons/{name}")
def icon(name: str):
    path = os.path.join(BASE_DIR, "icons", os.path.basename(name))
    if not os.path.exists(path):
        raise HTTPException(404)
    return FileResponse(path, media_type="image/png")


# ---------------------------------------------------------------------------
# 画面
# ---------------------------------------------------------------------------
def _read(name: str) -> str:
    """HTML を読み、__BASE__ を公開URLの接頭辞に差し替えて返す。

    ルート運用なら "/"、サブパス相乗りなら "/nwt/" 等。
    HTML 側は href="__BASE__dashboard" / fetch("__BASE__api/config") のように書く。
    """
    with open(os.path.join(BASE_DIR, name), encoding="utf-8") as f:
        return f.read().replace("__BASE__", BASE_URL)


@app.get("/", response_class=HTMLResponse)
def index():
    return _read("index.html")


@app.get("/dashboard", response_class=HTMLResponse)
def dashboard():
    return _read("dashboard.html")


@app.get("/manual", response_class=HTMLResponse)
def manual():
    return _read("manual.html")


@app.get("/manual-assets/{name}")
def manual_asset(name: str):
    # マニュアルのスクリーンショット（manual/ ディレクトリ、汎用値のみ）
    path = os.path.join(BASE_DIR, "manual", os.path.basename(name))
    if not os.path.exists(path):
        raise HTTPException(404)
    return FileResponse(path, media_type="image/png")
