#!/usr/bin/env bash
# =====================================================================
# 看護業務量調査ツール — 既存サイトのサブパス配下に相乗りする構成
#
#   使い方（VPS上で）:  sudo bash setup-vps-subpath.sh
#
# setup-mobile.sh との違い（＝既に他アプリが動いているサーバー向けの安全版）:
#   - ufw を有効化しない ...... 既存アプリの待受ポートを遮断してしまうため
#   - certbot を実行しない .... 既存ドメインの証明書をそのまま流用する
#   - PostgreSQL はクラスタを明示 ... 既定の5432が他プロセスに使われている場合に備える
#   - 既存の nginx 設定は上書きせず、location ブロックを1つ include するだけ
#     （変更前にバックアップし、nginx -t が通らなければ自動で戻す）
# =====================================================================
set -euo pipefail

APP_DIR=/opt/nwt-mobile
REPO=https://github.com/katu09161004/nursing-worktime.git
DB_NAME=worktime
DB_USER=nwt
PORT=8300
SERVICE=nwt-mobile
SNIPPET=/etc/nginx/snippets/nwt-mobile.conf

if [ "$(id -u)" -ne 0 ]; then echo "sudo で実行してください"; exit 1; fi

echo "======================================================"
echo " 看護業務量調査ツール（既存サイトのサブパス配下に設置）"
echo "======================================================"
read -r -p "既存サイトの nginx 設定ファイル名 (/etc/nginx/sites-available/ 配下): " VHOST
read -r -p "公開パス接頭辞 [/nwt]: " BASE_PATH; BASE_PATH="${BASE_PATH:-/nwt}"
read -r -p "サービス実行ユーザー名: " RUNUSER
read -r -p "PostgreSQL クラスタのバージョンとポート (例 '14 5433'): " PGVER PGPORT
echo
echo "病棟名を入力してください（カンマ区切り。例: 病棟A,病棟B,その他）"
echo "  ※ ここで入力した値は VPS 上の config_local.py にのみ書かれます（リポジトリには出ません）"
read -r -p "病棟名: " WARDS_CSV
read -r -s -p "スタッフ共有ログインパスワード: " APPPASS; echo
read -r -s -p "PostgreSQL(${DB_USER}) のパスワード: " DBPASS; echo

BASE_PATH="/$(echo "$BASE_PATH" | sed 's#^/*##; s#/*$##')"
VHOST_PATH="/etc/nginx/sites-available/${VHOST}"
[ -f "$VHOST_PATH" ] || { echo "nginx 設定が見つかりません: $VHOST_PATH"; exit 1; }
id "$RUNUSER" >/dev/null 2>&1 || { echo "ユーザー $RUNUSER が存在しません"; exit 1; }
[ -n "$APPPASS" ] || { echo "ログインパスワードは必須です（インターネット公開のため）"; exit 1; }

# Excel用APIキーとセッション署名鍵を自動生成
APIKEY="$(head -c 24 /dev/urandom | base64 | tr -d '/+=' | cut -c1-24)"
SECRET="$(head -c 32 /dev/urandom | base64 | tr -d '/+=' | cut -c1-32)"

echo "--- [1/6] パッケージ導入（python3-venv / git のみ）---"
apt-get update -qq
apt-get install -y python3-venv python3-pip git >/dev/null

echo "--- [2/6] PostgreSQL クラスタ ${PGVER} (port ${PGPORT}) ---"
# 既定の postgresql.service ではなく、対象クラスタだけを起動する。
# （他のクラスタや Docker の PostgreSQL とポートが衝突するのを避けるため）
systemctl enable --now "postgresql@${PGVER}-main"
for i in $(seq 1 20); do
  sudo -u postgres psql -p "$PGPORT" -Atqc "SELECT 1" >/dev/null 2>&1 && break
  sleep 1
done
sudo -u postgres psql -p "$PGPORT" -Atqc "SELECT 1" >/dev/null \
  || { echo "PostgreSQL に接続できません: pg_lsclusters と journalctl を確認"; exit 1; }

sudo -u postgres psql -p "$PGPORT" -tc \
  "SELECT 1 FROM pg_roles WHERE rolname='${DB_USER}'" | grep -q 1 \
  && sudo -u postgres psql -p "$PGPORT" -c \
       "ALTER USER ${DB_USER} WITH PASSWORD '${DBPASS}';" >/dev/null \
  || sudo -u postgres psql -p "$PGPORT" -c \
       "CREATE USER ${DB_USER} WITH PASSWORD '${DBPASS}';" >/dev/null
sudo -u postgres psql -p "$PGPORT" -tc \
  "SELECT 1 FROM pg_database WHERE datname='${DB_NAME}'" | grep -q 1 \
  || sudo -u postgres createdb -p "$PGPORT" "${DB_NAME}" -O "${DB_USER}"
echo "  ロール ${DB_USER} / DB ${DB_NAME} 準備OK"

echo "--- [3/6] アプリ配置 ---"
if [ -d "$APP_DIR/.git" ]; then git -C "$APP_DIR" pull --ff-only; else git clone "$REPO" "$APP_DIR"; fi
chown -R "$RUNUSER":"$RUNUSER" "$APP_DIR"
[ -d "$APP_DIR/.venv" ] || sudo -u "$RUNUSER" python3 -m venv "$APP_DIR/.venv"
sudo -u "$RUNUSER" "$APP_DIR/.venv/bin/pip" install -q --upgrade pip
sudo -u "$RUNUSER" "$APP_DIR/.venv/bin/pip" install -q -r "$APP_DIR/requirements.txt"

echo "--- [4/6] 設定ファイル生成 ---"
if [ -f "$APP_DIR/config_local.py" ]; then
  echo "  既存の config_local.py を保持（病棟名などは手で編集してください）"
else
  WARDS_PY="$(python3 - "$WARDS_CSV" <<'PY'
import sys
items = [w.strip() for w in sys.argv[1].split(",") if w.strip()] or ["病棟A", "病棟B", "その他"]
print(repr(items))
PY
)"
  cat > "$APP_DIR/config_local.py" <<EOF
# 施設固有・非公開。★このファイルは絶対にコミットしないこと（.gitignore済み）
WARDS  = ${WARDS_PY}
SHIFTS = ["日勤", "夜勤", "早出", "遅出"]
STAFF  = []          # 担当者は従業員番号の自由入力のため未使用

# 既存サイトのサブパス配下で動かす（nginx が接頭辞を剥がして渡す）
BASE_PATH = "${BASE_PATH}"

DB = {
    "backend": "postgres",
    "host": "127.0.0.1",
    "port": ${PGPORT},
    "dbname": "${DB_NAME}",
    "user": "${DB_USER}",
    "password": "${DBPASS}",
}

# インターネット公開のため認証必須
AUTH = {
    "password": "${APPPASS}",   # スタッフがブラウザで入れる共有パスワード
    "api_key":  "${APIKEY}",    # Excelの「サーバ同期設定」N7 に入れるキー
    "secret":   "${SECRET}",    # セッション署名用（変更すると全員ログインし直し）
}
EOF
  chown "$RUNUSER":"$RUNUSER" "$APP_DIR/config_local.py"
  chmod 600 "$APP_DIR/config_local.py"
fi

echo "--- [5/6] systemd 常駐化（127.0.0.1:${PORT} で待受）---"
cat > "/etc/systemd/system/${SERVICE}.service" <<EOF
[Unit]
Description=Nursing Worktime Logger (subpath on existing site)
After=network.target postgresql@${PGVER}-main.service
Wants=postgresql@${PGVER}-main.service

[Service]
Type=simple
User=${RUNUSER}
WorkingDirectory=${APP_DIR}
ExecStart=${APP_DIR}/.venv/bin/uvicorn main:app --host 127.0.0.1 --port ${PORT} --proxy-headers
Restart=always
RestartSec=5
Environment=PYTHONUNBUFFERED=1
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=full
ProtectHome=read-only

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable "${SERVICE}" >/dev/null
systemctl restart "${SERVICE}"
for i in $(seq 1 20); do
  curl -fsS "http://127.0.0.1:${PORT}/health" >/dev/null 2>&1 && break
  sleep 1
done
curl -fsS "http://127.0.0.1:${PORT}/health" >/dev/null \
  || { echo "  起動失敗: journalctl -u ${SERVICE} -n 50"; exit 1; }
echo "  アプリ起動OK"

echo "--- [6/6] nginx（既存サイトに location を1つ追加）---"
install -d /etc/nginx/snippets
cat > "$SNIPPET" <<EOF
# 看護業務量調査ツール — ${BASE_PATH}/ 配下（setup-vps-subpath.sh が生成）
location = ${BASE_PATH} { return 301 ${BASE_PATH}/; }

location ${BASE_PATH}/ {
    proxy_pass         http://127.0.0.1:${PORT}/;
    proxy_http_version 1.1;
    proxy_set_header   Host              \$host;
    proxy_set_header   X-Real-IP         \$remote_addr;
    proxy_set_header   X-Forwarded-For   \$proxy_add_x_forwarded_for;
    proxy_set_header   X-Forwarded-Proto \$scheme;
    proxy_read_timeout 60s;
    client_max_body_size 1m;
}
EOF

BACKUP="${VHOST_PATH}.bak-nwt-$(date +%Y%m%d%H%M%S)"
cp -a "$VHOST_PATH" "$BACKUP"
echo "  既存設定をバックアップ: $BACKUP"

# HTTPS(443) の server ブロック内に include を1行だけ挿入する（既存 location は触らない）
python3 - "$VHOST_PATH" "$SNIPPET" <<'PY'
import sys
path, snippet = sys.argv[1], sys.argv[2]
line = f"    include {snippet};\n"
src = open(path, encoding="utf-8").read().splitlines(keepends=True)
if any(snippet in l for l in src):
    print("  include は既に入っています（変更なし）")
    raise SystemExit(0)
start = next((i for i, l in enumerate(src) if "listen 443 ssl" in l), None)
if start is None:
    raise SystemExit("  443 の server ブロックが見つかりません")
anchor = next((i for i in range(start, min(start + 40, len(src)))
               if "ssl_prefer_server_ciphers" in src[i] or "ssl_certificate_key" in src[i]), None)
if anchor is None:
    raise SystemExit("  挿入位置が特定できません。手動で include を追加してください")
src.insert(anchor + 1, line)
open(path, "w", encoding="utf-8").write("".join(src))
print(f"  {path} の {anchor + 2} 行目に include を追加")
PY

if nginx -t 2>&1 | tail -2 && systemctl reload nginx; then
  echo "  nginx 反映OK"
else
  echo "  !! nginx 設定エラー。バックアップから復元します"
  cp -a "$BACKUP" "$VHOST_PATH"
  nginx -t && systemctl reload nginx
  exit 1
fi

# バックアップ（日次 pg_dump、14世代）
install -d -m 750 /var/backups/nwt
cat > /etc/cron.daily/nwt-backup <<EOF
#!/bin/sh
sudo -u postgres pg_dump -p ${PGPORT} ${DB_NAME} | gzip > /var/backups/nwt/${DB_NAME}_\$(date +\%F).sql.gz
find /var/backups/nwt -name '*.sql.gz' -mtime +14 -delete
EOF
chmod +x /etc/cron.daily/nwt-backup

DOMAIN="$(grep -m1 -oP 'server_name\s+\K[^;]+' "$VHOST_PATH" | awk '{print $1}')"
echo
echo "======================================================"
echo " 完了"
echo "   記録画面   : https://${DOMAIN}${BASE_PATH}/"
echo "   集計       : https://${DOMAIN}${BASE_PATH}/dashboard"
echo "   使い方     : https://${DOMAIN}${BASE_PATH}/manual"
echo
echo "   Excel用APIキー（入力シート N7 に貼る）:"
echo "     ${APIKEY}"
echo
echo " ※ ufw は変更していません（既存アプリを遮断しないため）"
echo " ※ 証明書は既存の Let's Encrypt をそのまま使っています"
echo "======================================================"
