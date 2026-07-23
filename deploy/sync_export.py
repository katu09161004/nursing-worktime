#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""同期の送り出し側。**コピー元サーバ上で**実行し、打刻と集計エントリをJSONで標準出力へ出す。

    cd <アプリの配置ディレクトリ>
    ./.venv/bin/python deploy/sync_export.py [--from 2026-07-01] [--to 2026-07-31] > dump.json

接続情報は自分の config_local.py から読む（このスクリプトは認証情報を一切受け取らない）。
標準出力はJSONのみ。進捗やエラーは標準エラーへ出すので、そのまま ssh でパイプできる:

    ssh dl380 'cd /opt/nursing-worktime && ./.venv/bin/python deploy/sync_export.py' \
      | ssh vps 'cd /opt/nwt-mobile && ./.venv/bin/python deploy/sync_import.py'
"""
import argparse
import json
import os
import socket
import sys
from contextlib import closing
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from main import get_conn  # noqa: E402  (config_local.py の設定を丸ごと再利用する)

PUNCH_COLS = ["staff_id", "ward", "shift", "category", "subcategory", "ts", "note", "overtime"]
ENTRY_COLS = ["staff_id", "ward", "shift", "work_date", "category", "subcategory",
              "minutes", "overtime", "source", "batch_id", "created_at"]


def main() -> int:
    ap = argparse.ArgumentParser(description="打刻・集計エントリをJSONで書き出す")
    ap.add_argument("--from", dest="frm", help="この日付以降のみ (YYYY-MM-DD)")
    ap.add_argument("--to", dest="to", help="この日付以前のみ (YYYY-MM-DD)")
    a = ap.parse_args()

    pw, pp = ["1=1"], []
    ew, ep = ["1=1"], []
    if a.frm:
        pw.append("ts >= ?"); pp.append(a.frm)
        ew.append("work_date >= ?"); ep.append(a.frm)
    if a.to:
        pw.append("ts < ?"); pp.append(a.to + "T23:59:59")
        ew.append("work_date <= ?"); ep.append(a.to)

    with closing(get_conn()) as conn:
        punches = [{k: r[k] for k in PUNCH_COLS} for r in
                   conn.execute(f"SELECT * FROM punches WHERE {' AND '.join(pw)} ORDER BY ts, id", pp).fetchall()]
        entries = [{k: r[k] for k in ENTRY_COLS} for r in
                   conn.execute(f"SELECT * FROM entries WHERE {' AND '.join(ew)} ORDER BY id", ep).fetchall()]

    for p in punches:
        p["overtime"] = bool(p["overtime"])
    for e in entries:
        e["overtime"] = bool(e["overtime"])

    payload = {
        "schema": 1,
        "source_host": socket.gethostname(),
        "exported_at": datetime.now().isoformat(timespec="seconds"),
        "range": {"from": a.frm, "to": a.to},
        "punches": punches,
        "entries": entries,
    }
    json.dump(payload, sys.stdout, ensure_ascii=False)
    sys.stdout.write("\n")
    print(f"[export] {payload['source_host']}: punches={len(punches)} entries={len(entries)}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
