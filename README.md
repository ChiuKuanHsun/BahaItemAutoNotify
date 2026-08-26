# 巴哈勇者福利社 → Discord 自動推播

定時抓 [勇者福利社現有商品](https://fuli.gamer.com.tw/shop.php?page=1&history=0)，和上次的快照比對，
把**新上架**與**內容異動**的商品推到 Discord 頻道。

目前部署在 **Android / Termux**。原本走 GitHub Actions，被 Cloudflare 擋掉了，
原因見〈[為什麼不用 GitHub Actions](#為什麼不用-github-actions)〉。

## 運作方式

1. `baha_fuli_notify.py` 逐頁抓 `shop.php?page=N&history=0`（跟著「下一頁」翻到底），
   解析每張 `a.items-card`：名稱、圖片、類型、價格、數量、活動時間、人氣、`sn`。
2. 以商品 `sn` 為唯一鍵，比對 `data/seen.json`：
   - `sn` 沒看過 → **新上架**
   - `sn` 看過但 `title/type/price/quantity/period` 變了 → **異動**
     （人氣一直跳，不列入比對，否則每次都會全部觸發）
3. 用 Discord embed 推播，單則最多 10 個 embed，超過自動分批，遇 429 依 `retry_after` 重試。
4. 更新 `data/seen.json` 作為下次比對基準。

**首次執行只建立基準快照**，不會把整頁商品當成新品洗版，只送一則「監控已啟動」訊息。

## 設定 Discord

擇一即可，**兩種都設的話優先用 Bot**。

**方式 A：Webhook（最簡單，推薦）**

Discord 頻道 → 編輯頻道 → 整合 → Webhook → 新增 Webhook → 複製網址。

**方式 B：Bot Token**

1. [Discord Developer Portal](https://discord.com/developers/applications) → New Application → Bot → 複製 Token。
2. OAuth2 URL Generator 勾 `bot` + `Send Messages`、`Embed Links`，用產生的網址把 bot 邀進伺服器。
3. Discord 開「開發者模式」後右鍵頻道 → 複製頻道 ID。

## 部署：Android（Termux）

行動網路是電信商的住宅級 IP，Cloudflare 不會出挑戰頁，所以只需要
`requests` + `beautifulsoup4` 兩個純 Python 套件，不用編譯任何東西。

### 1. 裝 Termux

**要從 [F-Droid](https://f-droid.org/packages/com.termux/) 裝**，Google Play 上那版已經廢棄，套件庫是壞的。

### 2. 拉專案並執行安裝腳本

```bash
pkg install -y git
git clone https://github.com/<你的帳號>/BahaItemAutoNotify.git
cd BahaItemAutoNotify
bash deploy/termux-setup.sh
```

腳本會裝好套件、從範本建立 `.env`、啟用 cron 並加好排程（預設每 15 分鐘）。
想改間隔就 `INTERVAL_MINUTES=5 bash deploy/termux-setup.sh`；
想半夜不跑就加 `ACTIVE_HOURS="8-23"`（見〈[耗電](#耗電)〉）。

### 3. 填 Discord Webhook

```bash
nano .env      # 填 DISCORD_WEBHOOK_URL
```

### 4. 手動跑一次確認

```bash
bash deploy/run.sh && tail -20 logs/notify.log
```

看到「首次執行：已建立基準快照」而且 Discord 收到啟動訊息就成功了。

### 5. 這兩步不做的話排程會被系統殺掉

- **關電池優化**：Android 設定 → 應用程式 → Termux → 電池 → 選「不受限制」
- **開機自動啟動**：F-Droid 裝 [Termux:Boot](https://f-droid.org/packages/com.termux.boot/)，然後

  ```bash
  mkdir -p ~/.termux/boot
  printf '#!/data/data/com.termux/files/usr/bin/sh\nsv-enable crond\n' \
    > ~/.termux/boot/start-cron.sh
  chmod +x ~/.termux/boot/start-cron.sh
  ```

`deploy/run.sh` 只在**執行的那幾秒**抓 wake lock，跑完立刻釋放，不會常駐持有。
開機腳本刻意不抓 wake lock —— 常駐持有會阻止 CPU 深度睡眠，那才是真正的耗電來源。

### 日常操作

```bash
tail -f logs/notify.log     # 看即時 log
crontab -l                  # 確認排程還在
crontab -e                  # 改間隔
sv status crond             # 確認 cron 服務活著
```

## 耗電

會增加，但正常設定下幅度很小。拆開來看：

**執行本身很輕。** 每次就是啟動一次 Python、抓兩頁 HTML（約 50 KB）、比對、大多數
情況下不送任何東西，數秒內結束。以 15 分鐘一次算，一天 96 次，累積 CPU 時間大概
只有幾分鐘，比一個聊天 app 在背景同步還少。

**真正的變數是有沒有阻止手機深度睡眠。** Android 的 Doze 模式會在螢幕關閉後大幅
壓低喚醒頻率，若持續持有 wake lock 把它擋掉，待機耗電可能翻倍 —— 這跟你跑什麼程式
無關，純粹是不讓 CPU 睡。本專案只在執行期間持有幾秒，所以不會有這個問題。

反過來說，這代表 **cron 不保證準時**：手機深睡時排程可能被延後到系統的喚醒窗口，
實際間隔會比設定值長一些。這是省電與即時性的取捨，想更準時就得付出待機電量。

### 三個省電旋鈕

```bash
# 1. 拉長間隔（影響最直接）
crontab -e        # 把 */15 改成 */30

# 2. 限制時段，半夜不跑（少掉約 1/3 執行次數）
ACTIVE_HOURS="8-23" bash deploy/termux-setup.sh

# 3. 完全不抓 wake lock，讓系統自由排程（最省電，但延遲最大）
echo 'WAKE_LOCK=false' >> .env
```

### 實測方法

跑一兩天後看 Android 設定 → 電池 → 用量，找 Termux 那一欄。如果佔比明顯偏高，
八成是別的東西持有 wake lock，用 `termux-wake-unlock` 手動釋放看看。

## 之後搬到伺服器

script 沒有平台相依，搬遷只要三步：

```bash
git clone <repo> && cd BahaItemAutoNotify
pip install -r requirements.txt
cp deploy/.env.example .env && nano .env
```

排程用系統 cron（把 `deploy/run.sh` 的 shebang 換成 `#!/usr/bin/env bash`）：

```
*/15 * * * * /path/to/BahaItemAutoNotify/deploy/run.sh
```

⚠️ 如果新伺服器是**雲端主機**（AWS、GCP、Azure、DigitalOcean…），大機率會遇到和
GitHub Actions 一樣的 Cloudflare 挑戰頁。要找住宅／機房白名單 IP，或家裡的常開裝置。

`data/seen.json` 不進版控，所以新機器第一次跑會重新建立基準（送一則啟動訊息，
不會把現有商品當新品洗版）。想沿用舊狀態就把舊的 `seen.json` 複製過去。

## 為什麼不用 GitHub Actions

`fuli.gamer.com.tw` 掛在 Cloudflare 後面。從住宅／行動 IP 連完全不會被攔，
但從 GitHub Actions 的資料中心 IP 連會被擋，而且是三段式升級：

| 嘗試 | 結果 |
| --- | --- |
| `requests` + 瀏覽器 UA | 403，TLS 指紋一看就不是瀏覽器 |
| `curl_cffi` 模擬 Chrome 指紋 | 過了指紋檢查，但吃到 `Enable JavaScript and cookies to continue`，需要執行 JS |
| Playwright headless Chromium | 真的執行了 JS，但被升級成 **Interactive Challenge**（要人為點擊） |

自動化過不了需要人為互動的挑戰，所以這條路是死的。`.github/workflows/notify.yml`
的排程已註解掉（留著只會每 15 分鐘失敗寄信一次），檔案保留是為了將來站方若放寬規則
可以直接恢復 —— 把 `on:` 底下那兩行 `schedule` 取消註解即可。

## 抓取後端

`SCRAPER_BACKEND=auto` 會依序嘗試，被 challenge 擋下就換下一個
（**不重試同一個後端**，重試沒有意義）：

| 後端 | 需要的套件 | 說明 |
| --- | --- | --- |
| `curl_cffi` | `curl_cffi` | 模擬 Chrome TLS/HTTP2 指紋，輕量 |
| `playwright` | `playwright` + Chromium | 開真的瀏覽器執行 JS challenge，慢 |
| `requests` | 無（核心相依） | 最陽春，住宅／行動 IP 用這個就夠 |

手機上直接指定 `SCRAPER_BACKEND=requests` 最省事（`.env` 範本已設好），
沒裝的後端會 `ImportError` 然後自動跳過，不會中斷。

## 環境變數

| 變數 | 預設 | 說明 |
| --- | --- | --- |
| `DISCORD_WEBHOOK_URL` | — | Webhook 網址 |
| `DISCORD_BOT_TOKEN` / `DISCORD_CHANNEL_ID` | — | Bot 模式，優先於 Webhook |
| `NOTIFY_CHANGES` | `true` | 是否推播「異動」；只想收新品就設 `false` |
| `DRY_RUN` | `false` | 只印出結果不送 Discord |
| `MAX_PAGES` | `20` | 最多翻幾頁，防呆用 |
| `SCRAPER_BACKEND` | `auto` | `auto`／`curl_cffi`／`playwright`／`requests` |
| `IMPERSONATE` | `chrome` | curl_cffi 模擬的瀏覽器指紋 |
| `CHALLENGE_TIMEOUT_MS` | `45000` | 等 Cloudflare challenge 解完的上限 |
| `HEADLESS` | `true` | 設 `false` 可在本機看瀏覽器實際跑什麼 |
| `WAKE_LOCK` | `true` | 執行期間是否抓 wake lock；設 `false` 最省電但延遲較大 |

## 疑難排解

**沒收到任何推播** — 先看 `logs/notify.log`，正常沒更新時會印「沒有更新，結束」。

**排程沒在跑** — `sv status crond` 確認服務活著；十之八九是電池優化沒關。

**抓到 0 件／解析錯亂** — 站方改版了。script 解析不到任何商品會 exit 1 並保留舊快照，
不會把空狀態寫進去，也不會洗版。修 `parse_card()` 的 CSS selector 即可。

**只想監控歷史商品** — 把 `LIST_URL` 的 `history` 改成 `1`。

## 本機測試（Windows / 一般環境）

```bash
pip install -r requirements.txt
DRY_RUN=true DISCORD_WEBHOOK_URL=dummy python baha_fuli_notify.py   # 只印不送
SCRAPER_BACKEND=requests python baha_fuli_notify.py                # 指定後端
```

要測 Playwright 後端才需要 `pip install -r requirements-cloud.txt` 和
`python -m playwright install chromium`；加 `HEADLESS=false` 可以看瀏覽器實際動作。
