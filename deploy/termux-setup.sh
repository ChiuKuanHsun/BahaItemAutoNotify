#!/data/data/com.termux/files/usr/bin/bash
# Termux 一鍵安裝：裝套件、建 .env、設定 cron。
# 用法：bash deploy/termux-setup.sh
set -e

PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$PROJECT_DIR"

echo "==> 安裝系統套件"
# -o 那串避免遇到設定檔衝突時停下來等人回答（腳本是非互動的）
pkg update -y -o Dpkg::Options::=--force-confold
pkg install -y python git cronie termux-services
# 新版 Termux 把 pip 拆成獨立套件，舊版隨 python 附帶，所以失敗也不當錯誤
pkg install -y python-pip 2>/dev/null || true

echo "==> 安裝 Python 套件（只要 requests + beautifulsoup4）"
# 注意：Termux 禁止 pip install --upgrade pip，會破壞它的 python-pip 套件
pip install -r requirements.txt

echo "==> 準備設定檔"
if [ ! -f .env ]; then
  cp deploy/.env.example .env
  echo "已建立 .env，等一下要填 DISCORD_WEBHOOK_URL"
else
  echo ".env 已存在，跳過"
fi

chmod +x deploy/run.sh

echo "==> 啟用 cron 服務"
# shellcheck disable=SC1091
[ -f "$PREFIX/etc/profile.d/start-services.sh" ] && . "$PREFIX/etc/profile.d/start-services.sh"
sv-enable crond 2>/dev/null || echo "crond 稍後會由 termux-services 啟動"

INTERVAL="${INTERVAL_MINUTES:-15}"
# 限制執行時段可以直接省電，例如 ACTIVE_HOURS="8-23" 半夜就不跑
HOURS="${ACTIVE_HOURS:-*}"
ENTRY="*/$INTERVAL $HOURS * * * $PROJECT_DIR/deploy/run.sh"

echo "==> 設定排程（每 $INTERVAL 分鐘，時段 $HOURS）"
if crontab -l 2>/dev/null | grep -qF "$PROJECT_DIR/deploy/run.sh"; then
  echo "排程已存在，跳過"
else
  (crontab -l 2>/dev/null; echo "$ENTRY") | crontab -
  echo "已加入：$ENTRY"
fi

cat <<'TIPS'

==================== 接下來要手動做的 ====================
1. 編輯 .env 填入 Discord Webhook：
     nano .env

2. 先手動跑一次，確認抓得到、推得出去：
     bash deploy/run.sh && tail -20 logs/notify.log

3. 關掉 Termux 的電池優化（很重要，否則排程會被系統殺掉）：
     Android 設定 → 應用程式 → Termux → 電池 → 不受限制

4. 想要開機自動啟動，去 F-Droid 裝 Termux:Boot，
   然後建立開機腳本：
     mkdir -p ~/.termux/boot
     printf '#!/data/data/com.termux/files/usr/bin/sh\nsv-enable crond\n' \
       > ~/.termux/boot/start-cron.sh
     chmod +x ~/.termux/boot/start-cron.sh
=========================================================
TIPS
