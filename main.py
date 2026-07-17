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
起動: uvicorn main:app --host 0.0.0.0 --port 8300
"""

import csv
import io
import os
import sqlite3
from contextlib import closing
from datetime import datetime, timedelta

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import HTMLResponse, StreamingResponse, JSONResponse, FileResponse
from pydantic import BaseModel

# ---------------------------------------------------------------------------
# 設定
# ---------------------------------------------------------------------------
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "worktime.db")

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
#   ai_tool を付けた小区分が、AI効果測定の対象（ベースライン→導入後で比較）。
# ---------------------------------------------------------------------------
CATEGORIES = [
    {
        "key": "direct", "label": "直接看護", "color": "#0f766e",
        "subs": [
            {"key": "obs",     "label": "観察・バイタル測定"},
            {"key": "care",    "label": "処置・与薬"},
            {"key": "adl",     "label": "清潔・排泄ケア"},
            {"key": "move",    "label": "移乗・移動介助"},
            {"key": "explain", "label": "患者対応・説明"},
        ],
    },
    {
        "key": "indirect", "label": "間接看護", "color": "#2563eb",
        "subs": [
            {"key": "order",   "label": "指示受け・確認"},
            {"key": "prep",    "label": "物品準備・片付け"},
            {"key": "env",     "label": "環境整備"},
            {"key": "coord",   "label": "連絡・調整"},
        ],
    },
    {
        # ★AI効果測定の主対象グループ★
        "key": "record", "label": "記録", "color": "#b45309",
        "subs": [
            {"key": "soap",    "label": "看護記録・SOAP入力", "ai_tool": "medai SOAP Voice"},
            {"key": "summary", "label": "退院時サマリー作成", "ai_tool": "SummyAI"},
            {"key": "chart",   "label": "温度板・観察記録入力"},
            {"key": "doc",     "label": "その他書類・記録"},
        ],
    },
    {
        "key": "share", "label": "情報共有", "color": "#7c3aed",
        "subs": [
            {"key": "handover", "label": "申し送り"},
            {"key": "conf",     "label": "カンファレンス・会議", "ai_tool": "議事録自動生成"},
            {"key": "phone",    "label": "電話・連絡対応"},
        ],
    },
    {
        "key": "misc", "label": "移動・教育・その他", "color": "#475569",
        "subs": [
            {"key": "walk",    "label": "移動（病棟内・院内）"},
            {"key": "edu",     "label": "指導・教育・委員会"},
            {"key": "other",   "label": "その他業務"},
        ],
    },
    {
        "key": "off", "label": "休憩・待機", "color": "#64748b",
        "subs": [
            {"key": "break",   "label": "休憩"},
            {"key": "standby", "label": "待機"},
        ],
    },
]

# 区分キー → ラベル / AIツール の逆引き辞書を構築
SUB_LABEL = {}
SUB_AI = {}
CAT_LABEL = {}
for c in CATEGORIES:
    CAT_LABEL[c["key"]] = c["label"]
    for s in c["subs"]:
        SUB_LABEL[(c["key"], s["key"])] = s["label"]
        if s.get("ai_tool"):
            SUB_AI[(c["key"], s["key"])] = s["ai_tool"]

# ---------------------------------------------------------------------------
# DB
# ---------------------------------------------------------------------------
def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with closing(get_conn()) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS punches (
                id        INTEGER PRIMARY KEY AUTOINCREMENT,
                staff_id  TEXT NOT NULL,
                ward      TEXT NOT NULL,
                shift     TEXT NOT NULL,
                category  TEXT NOT NULL,   -- 大区分キー。'END' は記録終了マーカー
                subcategory TEXT,          -- 小区分キー
                ts        TEXT NOT NULL,   -- ISO8601 (ローカル時刻)
                note      TEXT
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_staff_ts ON punches(staff_id, ts)")
        conn.commit()


init_db()

# ---------------------------------------------------------------------------
# FastAPI
# ---------------------------------------------------------------------------
app = FastAPI(title="看護業務量調査ツール")


class Punch(BaseModel):
    staff_id: str
    ward: str
    shift: str
    category: str
    subcategory: str | None = None
    note: str | None = None
    ts: str | None = None  # クライアント側のオフライン打刻用（省略時はサーバ時刻）


def parse_ts(value: str | None) -> datetime:
    if not value:
        return datetime.now()
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return datetime.now()


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
            "INSERT INTO punches (staff_id, ward, shift, category, subcategory, ts, note) "
            "VALUES (?,?,?,?,?,?,?)",
            (p.staff_id, p.ward, p.shift, p.category, p.subcategory, ts.isoformat(timespec="seconds"), p.note),
        )
        conn.commit()
        pid = cur.lastrowid
    return {"ok": True, "id": pid, "ts": ts.isoformat(timespec="seconds")}


@app.post("/api/end")
def api_end(p: Punch):
    """記録終了マーカー。最終区間を確定させる。"""
    ts = parse_ts(p.ts)
    with closing(get_conn()) as conn:
        conn.execute(
            "INSERT INTO punches (staff_id, ward, shift, category, subcategory, ts, note) "
            "VALUES (?,?,?,?,?,?,?)",
            (p.staff_id, p.ward, p.shift, "END", None, ts.isoformat(timespec="seconds"), "記録終了"),
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
                "cat_label": CAT_LABEL.get(cur["category"], cur["category"]),
                "sub_label": SUB_LABEL.get((cur["category"], cur["subcategory"]), ""),
                "ai_tool": SUB_AI.get((cur["category"], cur["subcategory"]), ""),
                "start": cur["ts"],
                "end": nxt["ts"],
                "minutes": round(minutes, 1),
                "suspect": flag,
            })
    return intervals


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
    if not include_suspect:
        intervals = [x for x in intervals if not x["suspect"]]

    total = sum(x["minutes"] for x in intervals) or 1.0

    # スタッフ×シフト（=1人1回の勤務）の数で「1シフトあたり」を出す
    sessions = set((x["staff_id"], x["start"][:10], x["shift"]) for x in intervals)
    n_sessions = max(len(sessions), 1)

    # 小区分別集計
    sub_agg: dict = {}
    for x in intervals:
        k = (x["category"], x["subcategory"])
        a = sub_agg.setdefault(k, {
            "category": x["category"], "cat_label": x["cat_label"],
            "subcategory": x["subcategory"], "sub_label": x["sub_label"],
            "ai_tool": x["ai_tool"], "minutes": 0.0, "count": 0,
        })
        a["minutes"] += x["minutes"]
        a["count"] += 1

    sub_list = []
    for a in sub_agg.values():
        a["minutes"] = round(a["minutes"], 1)
        a["pct"] = round(a["minutes"] / total * 100, 1)
        a["min_per_session"] = round(a["minutes"] / n_sessions, 1)
        sub_list.append(a)
    sub_list.sort(key=lambda z: -z["minutes"])

    # 大区分別集計
    cat_agg: dict = {}
    for x in intervals:
        a = cat_agg.setdefault(x["category"], {
            "category": x["category"], "cat_label": x["cat_label"], "minutes": 0.0,
        })
        a["minutes"] += x["minutes"]
    cat_list = []
    for a in cat_agg.values():
        a["minutes"] = round(a["minutes"], 1)
        a["pct"] = round(a["minutes"] / total * 100, 1)
        cat_list.append(a)
    cat_list.sort(key=lambda z: -z["minutes"])

    # AI効果測定ベースライン（ai_tool付きの小区分のみ抽出）
    ai_baseline = [a for a in sub_list if a["ai_tool"]]

    return {
        "total_minutes": round(total, 1),
        "n_sessions": n_sessions,
        "n_intervals": len(intervals),
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
):
    where, params = _build_filter(frm, to, ward, shift)
    intervals = _load_intervals(where, params)
    if not include_suspect:
        intervals = [x for x in intervals if not x["suspect"]]

    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["staff_id", "ward", "shift", "大区分", "小区分",
                "AIツール", "開始", "終了", "所要分", "打ち忘れ疑い"])
    for x in intervals:
        w.writerow([x["staff_id"], x["ward"], x["shift"], x["cat_label"], x["sub_label"],
                    x["ai_tool"], x["start"], x["end"], x["minutes"],
                    "○" if x["suspect"] else ""])
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
        "start_url": "/",
        "scope": "/",
        "display": "standalone",
        "orientation": "portrait",
        "background_color": "#eef2f4",
        "theme_color": "#0f766e",
        "icons": [
            {"src": "/icons/icon-192.png", "sizes": "192x192", "type": "image/png"},
            {"src": "/icons/icon-512.png", "sizes": "512x512", "type": "image/png"},
            {"src": "/icons/icon-maskable.png", "sizes": "512x512", "type": "image/png", "purpose": "maskable"},
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
    with open(os.path.join(BASE_DIR, name), encoding="utf-8") as f:
        return f.read()


@app.get("/", response_class=HTMLResponse)
def index():
    return _read("index.html")


@app.get("/dashboard", response_class=HTMLResponse)
def dashboard():
    return _read("dashboard.html")
