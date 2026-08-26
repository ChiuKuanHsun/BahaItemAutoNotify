#!/data/data/com.termux/files/usr/bin/bash
# 給 cron 呼叫的執行包裝：載入設定、抓住 wake lock、寫 log。
# 在一般 Linux 伺服器上也能用（shebang 換成 #!/usr/bin/env bash 即可）。

# cron 的環境很乾淨，PATH 要自己補，否則找不到 python
export PATH="${PREFIX:-/usr}/bin:$PATH"
# log 一律寫 UTF-8，不要跟著系統語系跑掉（Windows 上預設會變 cp950）
export PYTHONIOENCODING=utf-8

PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$PROJECT_DIR" || exit 1

# 設定放 .env，不進版控
if [ -f .env ]; then
  set -a
  . ./.env
  set +a
fi

mkdir -p logs
LOG="logs/notify.log"

# 抓 wake lock，避免抓到一半手機睡著把程序凍住（沒有此指令就跳過）
command -v termux-wake-lock >/dev/null 2>&1 && termux-wake-lock

echo "===== $(date '+%Y-%m-%d %H:%M:%S') =====" >>"$LOG"
python baha_fuli_notify.py >>"$LOG" 2>&1
status=$?
[ "$status" -ne 0 ] && echo "!!! 結束碼 $status" >>"$LOG"

command -v termux-wake-unlock >/dev/null 2>&1 && termux-wake-unlock

# log 只留最後 2000 行，免得長期跑爆手機空間
if [ "$(wc -l <"$LOG")" -gt 2000 ]; then
  tail -n 2000 "$LOG" >"$LOG.tmp" && mv "$LOG.tmp" "$LOG"
fi

exit "$status"
