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
for c in CATEGORIES:
    CAT_LABEL[c["key"]] = c["label"]
    CAT_GROUP[c["key"]] = c.get("group", "")
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
                category  TEXT NOT NULL,   -- 中分類キー。'END' は記録終了マーカー
                subcategory TEXT,          -- 小分類キー
                ts        TEXT NOT NULL,   -- ISO8601 (ローカル時刻)
                note      TEXT,
                overtime  INTEGER NOT NULL DEFAULT 0  -- 0=勤務時間内 / 1=勤務時間外
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_staff_ts ON punches(staff_id, ts)")
        # overtime 列を後から足したため、既存DBには ALTER で追加する
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(punches)")}
        if "overtime" not in cols:
            conn.execute("ALTER TABLE punches ADD COLUMN overtime INTEGER NOT NULL DEFAULT 0")
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
            "VALUES (?,?,?,?,?,?,?,?)",
            (p.staff_id, p.ward, p.shift, p.category, p.subcategory,
             ts.isoformat(timespec="seconds"), p.note, int(p.overtime)),
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
            "INSERT INTO punches (staff_id, ward, shift, category, subcategory, ts, note, overtime) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (p.staff_id, p.ward, p.shift, "END", None,
             ts.isoformat(timespec="seconds"), "記録終了", int(p.overtime)),
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
                "group": CAT_GROUP.get(cur["category"], ""),
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
):
    where, params = _build_filter(frm, to, ward, shift)
    intervals = _load_intervals(where, params)
    if not include_suspect:
        intervals = [x for x in intervals if not x["suspect"]]

    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["staff_id", "ward", "shift", "大分類", "中分類", "小分類",
                "AIツール", "開始", "終了", "所要分", "勤務時間内", "勤務時間外",
                "時間区分", "打ち忘れ疑い"])
    for x in intervals:
        w.writerow([x["staff_id"], x["ward"], x["shift"], x["group"], x["cat_label"], x["sub_label"],
                    x["ai_tool"], x["start"], x["end"], x["minutes"],
                    "" if x["overtime"] else x["minutes"],
                    x["minutes"] if x["overtime"] else "",
                    "勤務時間外" if x["overtime"] else "勤務時間内",
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
