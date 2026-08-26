# 巴哈勇者福利社 → Discord 自動推播

用 GitHub Actions 定時抓 [勇者福利社現有商品](https://fuli.gamer.com.tw/shop.php?page=1&history=0)，
和上次的快照比對，把**新上架**與**內容異動**的商品推到 Discord 頻道。

## 運作方式

1. `baha_fuli_notify.py` 逐頁抓 `shop.php?page=N&history=0`（跟著「下一頁」翻到底），
   解析每張 `a.items-card`：名稱、圖片、類型、價格、數量、活動時間、人氣、`sn`。
2. 以商品 `sn` 為唯一鍵，比對 `data/seen.json`：
   - `sn` 沒看過 → **新上架**
   - `sn` 看過但 `title/type/price/quantity/period` 變了 → **異動**（人氣一直跳，不列入比對）
3. 用 Discord embed 推播（單則最多 10 個 embed，超過自動分批）。
4. workflow 把更新後的 `data/seen.json` commit 回 repo，作為下次的比對基準。

**首次執行只建立基準快照**，不會把整頁商品當成新品洗版，只會送一則「監控已啟動」訊息。

## 安裝

### 1. 建立 repo 並推上去

```bash
git init
git add .
git commit -m "init: 福利社商品推播"
git branch -M main
git remote add origin https://github.com/<你的帳號>/BahaItemAutoNotify.git
git push -u origin main
```

### 2. 設定 Discord

擇一即可，**兩種都設的話優先用 Bot**。

**方式 A：Webhook（最簡單，推薦）**

Discord 頻道 → 編輯頻道 → 整合 → Webhook → 新增 Webhook → 複製 Webhook 網址。

**方式 B：Bot Token**

1. [Discord Developer Portal](https://discord.com/developers/applications) → New Application → Bot → 複製 Token。
2. OAuth2 URL Generator 勾 `bot` + `Send Messages`、`Embed Links`，用產生的網址把 bot 邀進伺服器。
3. Discord 開「開發者模式」後右鍵頻道 → 複製頻道 ID。

### 3. 設定 GitHub Secrets

Repo → Settings → Secrets and variables → Actions → New repository secret：

| Secret | 說明 |
| --- | --- |
| `DISCORD_WEBHOOK_URL` | 方式 A 用 |
| `DISCORD_BOT_TOKEN` | 方式 B 用 |
| `DISCORD_CHANNEL_ID` | 方式 B 用 |

### 4. 開啟 Actions 寫入權限

Repo → Settings → Actions → General → Workflow permissions → 選 **Read and write permissions** → Save。
（沒開的話 workflow 無法把快照 commit 回 repo，等於每次都重新建立基準。）

### 5. 手動跑一次

Actions → 「福利社商品推播」→ Run workflow。第一次會建立基準，之後每 15 分鐘自動檢查。

## 本機測試

```bash
pip install -r requirements.txt
DRY_RUN=true DISCORD_WEBHOOK_URL=dummy python baha_fuli_notify.py   # 只印不送
DISCORD_WEBHOOK_URL="https://discord.com/api/webhooks/..." python baha_fuli_notify.py
```

## 環境變數

| 變數 | 預設 | 說明 |
| --- | --- | --- |
| `DISCORD_WEBHOOK_URL` | — | Webhook 網址 |
| `DISCORD_BOT_TOKEN` / `DISCORD_CHANNEL_ID` | — | Bot 模式，優先於 Webhook |
| `NOTIFY_CHANGES` | `true` | 是否推播「異動」；只想收新品就設 `false` |
| `DRY_RUN` | `false` | 只印出結果不送 Discord |
| `MAX_PAGES` | `20` | 最多翻幾頁，防呆用 |

## 手動執行選項

Actions → Run workflow 時可勾：

- **dry_run**：只印結果、不推 Discord、不 commit 快照。
- **reset_state**：刪掉 `data/seen.json`，這次執行重新建立基準。

## 注意事項

- **排程會延遲**：GitHub 的 `schedule` 在尖峰時常延後數分鐘到十幾分鐘，屬正常現象；想更即時要改用常駐主機。
- **repo 閒置 60 天排程會被停用**：本專案每次有更新就會 commit 快照，正常運作下不會觸發；真的被停用時 GitHub 會寄信，去 Actions 頁面按 enable 即可。
- **只監控「現有商品」**（`history=0`）。想改監控歷史商品，把 `LIST_URL` 的 `history` 改成 `1`。
- **網站改版時**：script 解析不到任何商品會直接 exit 1 並保留舊快照，不會把空狀態寫進去，也不會洗版。修 `parse_card()` 的 CSS selector 即可。
