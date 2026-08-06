# -*- coding: utf-8 -*-
"""
GitHub Pages プレビュー（docs/）を、実アプリのソースから再生成する。

  python3 tools/build_docs.py

生成物:
  docs/demo.html            index.html + デモ用 fetch モック（バックエンド無しで動く）
  docs/demo-dashboard.html  dashboard.html + サンプル集計（main.py の集計ロジックで生成）
  docs/manual.html          manual.html のパスを Pages 用に置換

区分マスタ（CATEGORIES）と集計JSONは main.py から取るので、
区分やAPIを変えたら本スクリプトを実行し直せばプレビューが追従する。
"""

import ast
import json
import os
import shutil
import sys
import tempfile
from datetime import datetime, timedelta

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DOCS = os.path.join(ROOT, "docs")

BANNER = ('<div style="max-width:960px;margin:0 auto 10px;padding:9px 12px;background:#fff7ed;'
          'border:1px solid #fed7aa;border-radius:10px;color:#9a3412;font-size:12px;font-weight:700;'
          'text-align:center">デモ版：サンプル動作です。データは端末内のみで、サーバには送信されません。 '
          '<a href="{link}" style="color:#0f766e">{link_text}</a></div>')


# ---------------------------------------------------------------------------
# main.py から設定を読む（import せずに AST で定数を取り出す）
# ---------------------------------------------------------------------------
def load_consts():
    src = open(os.path.join(ROOT, "main.py"), encoding="utf-8").read()
    tree = ast.parse(src)
    out = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            t = node.targets[0]
            if isinstance(t, ast.Name) and t.id in ("CATEGORIES", "WARDS", "SHIFTS"):
                try:
                    out.setdefault(t.id, ast.literal_eval(node.value))
                except ValueError:
                    pass
    return out


def demo_config(c):
    return {
        "categories": c["CATEGORIES"],
        "wards": c["WARDS"],
        "shifts": c["SHIFTS"],
        "staff": [],
        "voice_ai": False,          # デモではAI解釈なし（端末内のあいまい一致だけで動く）
        "stt": {"mode": "webspeech", "url": "", "field": "audio"},
    }


# ---------------------------------------------------------------------------
# サンプル集計を、実物の集計ロジック（main.py）で作る
# ---------------------------------------------------------------------------
SAMPLE_DAY = [
    # (中分類, 小分類, 分)  ある日勤帯の1人分。AI効果測定対象（記録・サマリー・カンファ）を厚めに。
    ("direct1", "obs",     35), ("indirect", "info",    15), ("indirect", "handover", 25),
    ("direct2", "med_inj", 30), ("direct1", "hygiene",  50), ("indirect", "record",   30),
    ("direct2", "proc",    30), ("direct1", "excretion",25), ("contact", "nursecall", 10),
    ("direct1", "meal",    35), ("personal", "break",   45), ("indirect", "order",    12),
    ("direct2", "exam",    25), ("indirect", "summary", 30), ("direct3", "guidance",  15),
    ("supplies", "drug",   12), ("indirect", "conf",    25), ("env", "housekeeping",  10),
    ("indirect", "record", 25), ("clerical", "general", 10), ("admin_edu", "training",20),
    ("direct3", "transport", 8), ("contact", "phone",    6), ("indirect", "careplan", 15),
    ("direct1", "safety",  20), ("direct2", "patrol",   12), ("direct1", "comm",      15),
]


def build_summary():
    """main.py を一時ディレクトリにコピーして import し、サンプル打刻から /api/summary を得る。"""
    tmp = tempfile.mkdtemp(prefix="nwt-docs-")
    shutil.copy(os.path.join(ROOT, "main.py"), tmp)
    sys.path.insert(0, tmp)
    sys.modules.pop("main", None)
    import main  # noqa: E402  （init_db が走り、tmp 配下に空DBができる）

    base = datetime(2026, 6, 1, 8, 30)          # 集計は日付で絞らないので固定日でよい
    for day, (staff, ward, shift) in enumerate(
            [("A101", main.WARDS[0], main.SHIFTS[0]),
             ("A102", main.WARDS[0], main.SHIFTS[0]),
             ("A103", main.WARDS[1], main.SHIFTS[0])]):
        t = base + timedelta(days=day)
        for i, (cat, sub, minutes) in enumerate(SAMPLE_DAY):
            # 人ごとに少しずつ時間を散らす（同じ数字が並ばないように）
            dur = max(4, minutes - (i % 3) * 2 - day * 2)
            main.api_punch(main.Punch(staff_id=staff, ward=ward, shift=shift,
                                      category=cat, subcategory=sub,
                                      ts=t.isoformat(timespec="seconds")))
            t += timedelta(minutes=dur)
        main.api_end(main.Punch(staff_id=staff, ward=ward, shift=shift,
                                category="END", ts=t.isoformat(timespec="seconds")))

    # Query(...) の既定値をそのまま渡さないよう、全引数を明示する
    summary = main.api_summary(frm=None, to=None, ward=None, shift=None, include_suspect=False)
    sys.path.remove(tmp)
    shutil.rmtree(tmp, ignore_errors=True)
    return json.loads(json.dumps(summary, default=str))


# ---------------------------------------------------------------------------
# 生成
# ---------------------------------------------------------------------------
def strip_pwa(html):
    for line in ('<link rel="manifest" href="__BASE__manifest.json">\n',
                 '<link rel="icon" href="__BASE__icons/icon-192.png">\n',
                 '<link rel="apple-touch-icon" href="__BASE__icons/icon-192.png">\n'):
        html = html.replace(line, "")
    return html


def build_demo(cfg):
    html = strip_pwa(open(os.path.join(ROOT, "index.html"), encoding="utf-8").read())
    html = html.replace('href="__BASE__dashboard"', 'href="./demo-dashboard.html"')
    html = html.replace('href="__BASE__manual"', 'href="./manual.html"')
    html = html.replace("__BASE__", "./")

    shim = """
<script>
/* ===== DEMO MODE: バックエンド無し・全てブラウザ内で動作 ===== */
const DEMO_CONFIG = %s;
(function(){
  const KEY="nwt.demo.punches";
  const load=()=>JSON.parse(localStorage.getItem(KEY)||"[]");
  const save=a=>localStorage.setItem(KEY,JSON.stringify(a));
  const label={};
  DEMO_CONFIG.categories.forEach(c=>c.subs.forEach(s=>label[c.key+"|"+s.key]=s.label));
  const orig=window.fetch.bind(window);
  window.fetch=async(url,opts={})=>{
    const u=typeof url==="string"?url:(url&&url.url)||"";
    const body=opts.body&&typeof opts.body==="string"?JSON.parse(opts.body):{};
    const J=o=>new Response(JSON.stringify(o),{status:200,headers:{"Content-Type":"application/json"}});
    const sp=()=>{try{return new URL(u,location.href).searchParams;}catch(e){return new URLSearchParams();}};
    if(u.includes("/api/config")) return J(DEMO_CONFIG);
    if(u.includes("/api/voice/interpret")) return J({enabled:false, match:null});
    if(u.includes("/api/punch")){const a=load();a.push({staff_id:body.staff_id,category:body.category,subcategory:body.subcategory,ts:body.ts});save(a);return J({ok:true});}
    if(u.includes("/api/end")){const a=load();a.push({staff_id:body.staff_id,category:"END",ts:body.ts});save(a);return J({ok:true});}
    if(u.includes("/api/undo")){const sid=sp().get("staff_id");const a=load();for(let i=a.length-1;i>=0;i--){if(a[i].staff_id===sid){a.splice(i,1);break;}}save(a);return J({ok:true});}
    if(u.includes("/api/current")){const sid=sp().get("staff_id");const a=load().filter(x=>x.staff_id===sid);const last=a[a.length-1];if(!last||last.category==="END")return J({active:false});return J({active:true,category:last.category,subcategory:last.subcategory,label:label[last.category+"|"+last.subcategory]||last.category,since:last.ts});}
    return orig(url,opts);
  };
})();
</script>
""" % json.dumps(cfg, ensure_ascii=False)

    anchor = '<script>\nconst LS = "nwt.session.v1";'
    assert anchor in html, "index.html のスクリプト開始位置が見つからない"
    html = html.replace(anchor, shim.strip() + "\n\n" + anchor, 1)
    html = html.replace('<div class="wrap">',
                        '<div class="wrap">\n  ' + BANNER.format(
                            link="./demo-dashboard.html", link_text="集計デモを見る →"), 1)
    open(os.path.join(DOCS, "demo.html"), "w", encoding="utf-8").write(html)


def build_demo_dashboard(cfg, summary):
    html = open(os.path.join(ROOT, "dashboard.html"), encoding="utf-8").read()
    html = html.replace('<link rel="icon" href="__BASE__icons/icon-192.png">\n', "")
    html = html.replace('href="__BASE__"', 'href="./demo.html"')
    html = html.replace("__BASE__", "./")

    shim = """
<script>
/* ===== DEMO MODE: サンプル集計を埋め込み、fetch を差し替える ===== */
const DEMO_CONFIG = %s;
const DEMO_SUMMARY = %s;
(function(){
  const orig=window.fetch.bind(window);
  window.fetch=async(url,opts={})=>{
    const u=typeof url==="string"?url:(url&&url.url)||"";
    const J=o=>new Response(JSON.stringify(o),{status:200,headers:{"Content-Type":"application/json"}});
    if(u.includes("/api/config")) return J(DEMO_CONFIG);
    if(u.includes("/api/summary")) return J(DEMO_SUMMARY);
    return orig(url,opts);
  };
})();
</script>
""" % (json.dumps(cfg, ensure_ascii=False), json.dumps(summary, ensure_ascii=False))

    anchor = "<script>\nconst $=s=>document.querySelector(s);"
    assert anchor in html, "dashboard.html のスクリプト開始位置が見つからない"
    html = html.replace(anchor, shim.strip() + "\n\n" + anchor, 1)
    html = html.replace('<div class="wrap">',
                        '<div class="wrap">\n  ' + BANNER.format(
                            link="./demo.html", link_text="記録デモを触る →"), 1)
    html = html.replace("</body>",
                        '<script>const _c=document.querySelector("#csv");'
                        ' if(_c) _c.onclick=()=>alert("デモではCSV出力は無効です（実運用版では有効）。");</script>\n</body>')
    open(os.path.join(DOCS, "demo-dashboard.html"), "w", encoding="utf-8").write(html)


def build_manual():
    # スクリーンショットは manual/ が正。docs/manual-assets/ はその複製。
    dst = os.path.join(DOCS, "manual-assets")
    os.makedirs(dst, exist_ok=True)
    for name in sorted(os.listdir(os.path.join(ROOT, "manual"))):
        if name.endswith(".png"):
            shutil.copy(os.path.join(ROOT, "manual", name), os.path.join(dst, name))

    html = open(os.path.join(ROOT, "manual.html"), encoding="utf-8").read()
    html = html.replace('<link rel="icon" href="__BASE__icons/icon-192.png">\n', "")
    html = html.replace('href="__BASE__">← 記録画面へ戻る', 'href="./demo.html">← 記録画面へ戻る')
    html = html.replace('src="__BASE__manual-assets/', 'src="./manual-assets/')
    html = html.replace("__BASE__", "./")
    open(os.path.join(DOCS, "manual.html"), "w", encoding="utf-8").write(html)


def main_():
    c = load_consts()
    cfg = demo_config(c)
    summary = build_summary()
    build_demo(cfg)
    build_demo_dashboard(cfg, summary)
    build_manual()
    n_sub = sum(len(x["subs"]) for x in c["CATEGORIES"])
    print(f"docs/ を再生成しました（中分類 {len(c['CATEGORIES'])} / 小分類 {n_sub} / "
          f"サンプル集計 {summary['total_minutes']:.0f}分・{summary['n_sessions']}シフト）")


if __name__ == "__main__":
    main_()
