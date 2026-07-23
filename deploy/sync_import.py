#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""同期の受け取り側。**正とするサーバ上で**実行し、sync_export.py のJSONを取り込む。

    ssh dl380 'cd /opt/nursing-worktime && ./.venv/bin/python deploy/sync_export.py' \
      | ssh vps 'cd /opt/nwt-mobile && ./.venv/bin/python deploy/sync_import.py --dry-run'

何度流しても二重計上しない（冪等）:
  - punches : (staff_id, ward, shift, category, subcategory, ts) が既にあれば入れない
  - entries : batch_id 単位で入れ替え（/api/entries/bulk と同じ扱い）

取り込んだ entries の source には "<送り元ホスト>:" を付ける。これがあるので、
「前回この同期で入れた分」と「取り込み先が自前で持っている分」を区別できる。

安全側の既定:
  - 取り込み先が自前で持っている batch_id とぶつかったら**上書きせずスキップ**（--force で上書き）
  - 同じ担当者・同じ日の打刻が両サーバにあると区間計算が混ざるため、警告を出す
"""
import argparse
import json
import sys
from contextlib import closing

import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from main import get_conn  # noqa: E402

PKEY = ("staff_id", "ward", "shift", "category", "subcategory", "ts")
ENTRY_COLS = ["staff_id", "ward", "shift", "work_date", "category", "subcategory",
              "minutes", "overtime", "source", "batch_id", "created_at"]


def _pkey(r) -> tuple:
    return tuple(r[k] for k in PKEY)


def main() -> int:
    ap = argparse.ArgumentParser(description="sync_export.py のJSONを取り込む")
    ap.add_argument("--dry-run", action="store_true", help="書き込まずに結果だけ表示")
    ap.add_argument("--force", action="store_true", help="batch_id 衝突時に取り込み先を上書きする")
    a = ap.parse_args()

    payload = json.load(sys.stdin)
    if payload.get("schema") != 1:
        print(f"[import] 未知のschema: {payload.get('schema')}", file=sys.stderr)
        return 2
    host = payload.get("source_host") or "unknown"
    tag = f"{host}:"
    punches = payload.get("punches", [])
    entries = payload.get("entries", [])
    print(f"[import] 送り元={host} punches={len(punches)} entries={len(entries)}"
          f"{' (dry-run)' if a.dry_run else ''}")

    ins_p = skip_p = ins_e = 0
    conflicts: list[str] = []
    overlaps: list[str] = []

    with closing(get_conn()) as conn:
        # ---- punches ----
        if punches:
            # 重複検出は「同じ日に相手側の打刻があるか」を見たいので、
            # 時刻ぴったりではなく日単位まで広げて既存行を取る（狭いと日中/夜勤帯のずれを見逃す）。
            ts_min = min(p["ts"] for p in punches)[:10] + "T00:00:00"
            ts_max = max(p["ts"] for p in punches)[:10] + "T23:59:59"
            existing = conn.execute(
                "SELECT staff_id, ward, shift, category, subcategory, ts FROM punches "
                "WHERE ts >= ? AND ts <= ?", [ts_min, ts_max]).fetchall()
            have = {_pkey(r) for r in existing}
            incoming = {_pkey(p) for p in punches}

            # 同じ担当者・同じ日に「取り込み先が自前で持っている打刻」があると、
            # 区間は時刻順に組まれるため両者が交互に並んで所要時間が壊れる。
            foreign = [r for r in existing if _pkey(r) not in incoming]
            if foreign:
                pairs = sorted({(r["staff_id"], (r["ts"] or "")[:10]) for r in foreign})
                mine = {(p["staff_id"], (p["ts"] or "")[:10]) for p in punches}
                overlaps = [f"{s} / {d}" for (s, d) in pairs if (s, d) in mine]

            for p in punches:
                if _pkey(p) in have:
                    skip_p += 1
                    continue
                if not a.dry_run:
                    conn.execute(
                        "INSERT INTO punches (staff_id, ward, shift, category, subcategory, ts, note, overtime) "
                        "VALUES (?,?,?,?,?,?,?,?)",
                        [p["staff_id"], p["ward"], p["shift"], p["category"], p["subcategory"],
                         p["ts"], p["note"], bool(p["overtime"])])
                have.add(_pkey(p))
                ins_p += 1

        # ---- entries (batch_id 単位) ----
        batches: dict = {}
        for e in entries:
            batches.setdefault(e["batch_id"], []).append(e)
        for batch, rows in batches.items():
            cur = conn.execute("SELECT source FROM entries WHERE batch_id = ?", [batch]).fetchall()
            # 取り込み先が自前で作った batch（=このタグが付いていない）を黙って消さない
            if cur and not all((r["source"] or "").startswith(tag) for r in cur):
                if not a.force:
                    conflicts.append(batch)
                    continue
                conflicts.append(batch + "  ← --force で上書き")
            if not a.dry_run:
                conn.execute("DELETE FROM entries WHERE batch_id = ?", [batch])
                for e in rows:
                    conn.execute(
                        "INSERT INTO entries (staff_id,ward,shift,work_date,category,subcategory,"
                        "minutes,overtime,source,batch_id,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                        [e["staff_id"], e["ward"], e["shift"], e["work_date"], e["category"],
                         e["subcategory"], e["minutes"], bool(e["overtime"]),
                         tag + (e["source"] or "excel"), e["batch_id"], e["created_at"]])
            ins_e += len(rows)

        if a.dry_run:
            conn.raw.rollback() if hasattr(conn.raw, "rollback") else None
        else:
            conn.commit()

    print(f"[import] punches: 追加={ins_p} 既存につきスキップ={skip_p}")
    print(f"[import] entries : 取り込み={ins_e} 行 / {len(batches)} batch"
          f"（衝突スキップ={len([c for c in conflicts if '← ' not in c])}）")
    for c in conflicts:
        print(f"  [衝突] batch_id={c}")
    for o in overlaps:
        print(f"  [警告] 同一担当者・同一日の打刻が両サーバに存在: {o}  → 区間計算が混ざります")
    if a.dry_run:
        print("[import] dry-run のため書き込みはしていません")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
