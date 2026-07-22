#!/usr/bin/env bash
# =====================================================================
# さくらVPS モバイル入力環境セットアップ
#   DL380で動いているモバイル記録環境を、VPS上に HTTPS + ログイン認証付きで構築する。
#
#   使い方（VPS上で）:  sudo bash setup-mobile.sh
#
#   前提: Ubuntu 22.04/24.04、sudo可、ドメインがVPSのグローバルIPを指している
#         （HTTPSはPWA=ホーム画面追加に必須なのでドメインは必ず用意する）
# =====================================================================
set -euo pipefail

APP_DIR=/opt/nwt-mobile
REPO=https://github.com/katu09161004/nursing-worktime.git
DB_NAME=worktime
DB_USER=nwt
PORT=8300
SERVICE=nursing-worktime

if [ "$(id -u)" -ne 0 ]; then echo "sudo で実行してください"; exit 1; fi

echo "======================================================"
echo " 看護業務量調査ツール モバイル入力環境（さくらVPS）"
echo "======================================================"
read -r -p "公開するドメイン名 (例 nwt.example.com): " DOMAIN
read -r -p "サービス実行ユーザー名 (既存の一般ユーザー): " RUNUSER
read -r -s -p "スタッフ共有ログインパスワード: " APPPASS; echo
read -r -s -p "PostgreSQL(${DB_USER}) のパスワード: " DBPASS; echo
read -r -p "Let's Encrypt 用のメールアドレス: " LEMAIL

id "$RUNUSER" >/dev/null 2>&1 || { echo "ユーザー $RUNUSER が存在しません"; exit 1; }

# Excel用APIキーとセッション署名鍵を自動生成
APIKEY="$(head -c 24 /dev/urandom | base64 | tr -d '/+=' | cut -c1-24)"
SECRET="$(head -c 32 /dev/urandom | base64 | tr -d '/+=' | cut -c1-32)"

echo "--- [1/8] パッケージ導入 ---"
apt-get update -qq
apt-get install -y python3-venv python3-pip postgresql git nginx \
                   certbot python3-certbot-nginx ufw >/dev/null

echo "--- [2/8] PostgreSQL 準備 ---"
systemctl enable --now postgresql
sudo -u postgres psql -tc "SELECT 1 FROM pg_roles WHERE rolname='${DB_USER}'" | grep -q 1 || \
  sudo -u postgres psql -c "CREATE USER ${DB_USER} WITH PASSWORD '${DBPASS}';"
sudo -u postgres psql -tc "SELECT 1 FROM pg_database WHERE datname='${DB_NAME}'" | grep -q 1 || \
  sudo -u postgres createdb "${DB_NAME}" -O "${DB_USER}"

echo "--- [3/8] アプリ配置 ---"
if [ -d "$APP_DIR/.git" ]; then git -C "$APP_DIR" pull --ff-only; else git clone "$REPO" "$APP_DIR"; fi
chown -R "$RUNUSER":"$RUNUSER" "$APP_DIR"
sudo -u "$RUNUSER" python3 -m venv "$APP_DIR/.venv"
sudo -u "$RUNUSER" "$APP_DIR/.venv/bin/pip" install -q -r "$APP_DIR/requirements.txt"

echo "--- [4/8] 設定ファイル生成 ---"
if [ -f "$APP_DIR/config_local.py" ]; then
  echo "  既存の config_local.py を保持（病棟名などは手で編集してください）"
else
  cat > "$APP_DIR/config_local.py" <<EOF
# 施設固有・非公開。★このファイルは絶対にコミットしないこと（.gitignore済み）

# 病棟・勤務帯・スタッフID（実値に書き換える。公開したくない場合は汎用値のままでもよい）
WARDS  = ["病棟A", "病棟B", "その他"]
SHIFTS = ["日勤", "夜勤", "早出", "遅出"]
STAFF  = [f"N-{i:02d}" for i in range(1, 21)]

DB = {
    "backend": "postgres",
    "host": "127.0.0.1",
    "port": 5432,
    "dbname": "${DB_NAME}",
    "user": "${DB_USER}",
    "password": "${DBPASS}",
}

# インターネット公開のため認証必須
AUTH = {
    "password": "${APPPASS}",   # スタッフがブラウザで入れる共有パスワード
    "api_key":  "${APIKEY}",    # Excelの「サーバ同期設定」N7 に入れるキー
    "secret":   "${SECRET}",    # セッション署名用（変更するとログインし直しになる）
}
EOF
  chown "$RUNUSER":"$RUNUSER" "$APP_DIR/config_local.py"
  chmod 600 "$APP_DIR/config_local.py"
fi

echo "--- [5/8] systemd 常駐化（127.0.0.1 で待受）---"
cat > "/etc/systemd/system/${SERVICE}.service" <<EOF
[Unit]
Description=Nursing Worktime Logger (mobile, VPS)
After=network.target postgresql.service
Wants=postgresql.service

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
systemctl enable --now "${SERVICE}"
sleep 3
curl -fsS "http://127.0.0.1:${PORT}/health" >/dev/null && echo "  アプリ起動OK" || \
  { echo "  起動失敗: journalctl -u ${SERVICE} -n 50"; exit 1; }

echo "--- [6/8] Nginx（リバースプロキシ）---"
cat > /etc/nginx/sites-available/nwt-mobile <<EOF
server {
    listen 80;
    server_name ${DOMAIN};

    # モバイルからの入力を想定し、body上限は控えめ
    client_max_body_size 1m;

    location / {
        proxy_pass         http://127.0.0.1:${PORT};
        proxy_http_version 1.1;
        proxy_set_header   Host              \$host;
        proxy_set_header   X-Real-IP         \$remote_addr;
        proxy_set_header   X-Forwarded-For   \$proxy_add_x_forwarded_for;
        proxy_set_header   X-Forwarded-Proto \$scheme;
        proxy_read_timeout 60s;
    }
}
EOF
ln -sf /etc/nginx/sites-available/nwt-mobile /etc/nginx/sites-enabled/nwt-mobile
rm -f /etc/nginx/sites-enabled/default
nginx -t && systemctl reload nginx

echo "--- [7/8] ファイアウォール（22/80/443 のみ）---"
ufw allow 22/tcp >/dev/null; ufw allow 80/tcp >/dev/null; ufw allow 443/tcp >/dev/null
ufw --force enable >/dev/null
echo "  ※ さくらVPSのコントロールパネル側パケットフィルタでも 22/80/443 を許可すること"

echo "--- [8/8] HTTPS化（PWA/ホーム画面追加に必須）---"
certbot --nginx -d "${DOMAIN}" --non-interactive --agree-tos -m "${LEMAIL}" --redirect

# バックアップ（日次 pg_dump、14世代）
install -d -m 750 /var/backups/nwt
cat > /etc/cron.daily/nwt-backup <<EOF
#!/bin/sh
sudo -u postgres pg_dump ${DB_NAME} | gzip > /var/backups/nwt/${DB_NAME}_\$(date +\%F).sql.gz
find /var/backups/nwt -name '*.sql.gz' -mtime +14 -delete
EOF
chmod +x /etc/cron.daily/nwt-backup

echo
echo "======================================================"
echo " 完了"
echo "   記録画面   : https://${DOMAIN}/"
echo "   集計       : https://${DOMAIN}/dashboard"
echo "   ログインPW : （設定したもの。スタッフに共有）"
echo
echo "   Excel用APIキー（入力シート N7 に貼る）:"
echo "     ${APIKEY}"
echo
echo " スマホでの使い方: 上記URLを開く → ログイン →"
echo "   ブラウザメニューから「ホーム画面に追加」でアプリのように使える"
echo "======================================================"
