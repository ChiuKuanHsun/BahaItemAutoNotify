#!/usr/bin/env python3
"""巴哈姆特勇者福利社 商品更新 → Discord 推播。

抓取 https://fuli.gamer.com.tw/shop.php （現有商品，含分頁），
與 data/seen.json 內的上次快照比對，把「新上架」與「內容異動」的商品
用 Discord embed 推播出去，最後把新的快照寫回 data/seen.json。
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
from bs4 import BeautifulSoup

BASE_URL = "https://fuli.gamer.com.tw/"
LIST_URL = BASE_URL + "shop.php?page={page}&history=0"
STATE_FILE = Path(__file__).resolve().parent / "data" / "seen.json"

MAX_PAGES = int(os.getenv("MAX_PAGES", "20"))
REQUEST_TIMEOUT = 30
# Cloudflare Managed Challenge 通常幾秒內解完，給寬一點的餘裕
CHALLENGE_TIMEOUT = int(os.getenv("CHALLENGE_TIMEOUT_MS", "45000"))
TPE = timezone(timedelta(hours=8))

HEADERS = {
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,"
        "image/avif,image/webp,*/*;q=0.8"
    ),
    "Accept-Language": "zh-TW,zh;q=0.9,en-US;q=0.8,en;q=0.7",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "same-origin",
    "Sec-Fetch-User": "?1",
}

# 會觸發「異動通知」的欄位（人氣一直在跳，不列入比對）
TRACKED_FIELDS = ["title", "type", "price", "quantity", "period"]
FIELD_LABELS = {
    "title": "名稱",
    "type": "類型",
    "price": "價格",
    "quantity": "商品數量",
    "period": "活動時間",
}
TYPE_COLORS = {
    "兌換": 0x2ECC71,
    "競標": 0xE67E22,
    "抽獎": 0x9B59B6,
    "折扣": 0x3498DB,
}
DEFAULT_COLOR = 0x5865F2


# Cloudflare Managed Challenge 頁的特徵字串（巴哈套了自己的樣板，但文案是 CF 的）
CHALLENGE_MARKERS = (
    "Enable JavaScript and cookies to continue",
    "challenge-platform",
    "cf-browser-verification",
    "Just a moment",
    "系統異常回報",
)


class BlockedError(RuntimeError):
    """被 Cloudflare 擋下來。重試同一個後端沒意義，要換一個後端。"""


def looks_like_challenge(html: str) -> bool:
    return bool(html) and any(marker in html for marker in CHALLENGE_MARKERS)


# --------------------------------------------------------------------------- #
# 抓取與解析
# --------------------------------------------------------------------------- #
def build_http_session(kind: str):
    """建立 HTTP 抓取用的 session（不執行 JS 的那類後端）。"""
    if kind == "curl_cffi":
        from curl_cffi import requests as curl_requests

        return curl_requests.Session(impersonate=os.getenv("IMPERSONATE", "chrome"))

    session = requests.Session()
    # curl_cffi 會自帶與 TLS 指紋一致的 UA；requests 得自己補，否則直接 403
    session.headers["User-Agent"] = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    )
    return session


def warm_up(session) -> None:
    """先進首頁拿 cookie，讓後續請求看起來像正常瀏覽流程。"""
    try:
        session.get(BASE_URL, headers=HEADERS, timeout=REQUEST_TIMEOUT)
        time.sleep(1)
    except Exception as err:  # 暖身失敗不致命，繼續抓正題
        print(f"::warning::首頁暖身失敗（不影響後續嘗試）：{err}")


def describe_error(html: str) -> str:
    """把錯誤頁轉成純文字，才看得出站方到底說了什麼（HTML 標籤會塞爆 log）。"""
    if not html:
        return "(空回應)"
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style"]):
        tag.decompose()
    title = clean(soup.title.get_text()) if soup.title else ""
    body = clean(soup.get_text(" "))
    return f"[{title}] {body}"[:800]


def fetch(url: str, session, retries: int = 3) -> str:
    last_err = None
    headers = dict(HEADERS, Referer=BASE_URL)

    for attempt in range(retries):
        try:
            resp = session.get(url, headers=headers, timeout=REQUEST_TIMEOUT)
            # encoding 必須在讀 .text 之前設定，curl_cffi 讀過就不給改了
            try:
                resp.encoding = "utf-8"
            except Exception:
                pass
            html = resp.text

            if resp.status_code >= 400:
                print(f"::warning::{url} 回應 {resp.status_code}：{describe_error(html)}")
                if looks_like_challenge(html):
                    raise BlockedError("Cloudflare challenge（此後端無法執行 JS）")
                raise RuntimeError(f"{resp.status_code} for {url}")
            if looks_like_challenge(html):
                raise BlockedError("Cloudflare challenge（回 200 但內容是挑戰頁）")
            return html
        except BlockedError:
            raise  # 重試同一後端沒用，直接讓上層換後端
        except Exception as err:  # 巴哈偶爾 5xx／連線抖動，重試即可
            last_err = err
            if attempt < retries - 1:
                time.sleep(3 * (attempt + 1))
    raise RuntimeError(f"抓取失敗 {url}: {last_err}")


def clean(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def parse_card(card) -> dict | None:
    href = card.get("href") or ""
    match = re.search(r"sn=(\d+)", href)
    if not match:
        return None

    item = {
        "sn": match.group(1),
        "url": href if href.startswith("http") else BASE_URL + href.lstrip("/"),
        "title": "",
        "image": "",
        "type": "",
        "price": "",
        "quantity": "",
        "period": "",
        "popularity": "",
    }

    title = card.select_one(".items-title")
    if title:
        item["title"] = clean(title.get_text())

    img = card.select_one(".card-left img")
    if img and img.get("src"):
        item["image"] = img["src"]

    tag = card.select_one(".type-tag")
    if tag:
        item["type"] = clean(tag.get_text())

    price = card.select_one(".price")
    if price:
        # <div class="price"><p class="digital">500</p>巴幣/個</div>，中間沒空白
        digital = price.select_one(".digital")
        if digital:
            unit = clean(price.get_text().replace(digital.get_text(), "", 1))
            item["price"] = clean(f"{clean(digital.get_text())} {unit}")
        else:
            item["price"] = clean(price.get_text())

    # 人氣 / 商品數量 / 活動時間 都是 <p>標籤<span>值</span></p>，靠標籤字取值
    label_map = {"人氣": "popularity", "商品數量": "quantity", "活動時間": "period"}
    for para in card.select(".items-instructions p"):
        span = para.find("span")
        if not span:
            continue
        value = clean(span.get_text())
        label = clean(para.get_text().replace(span.get_text(), ""))
        for keyword, key in label_map.items():
            if keyword in label:
                item[key] = value

    return item


# 抹掉 headless Chromium 最明顯的自動化痕跡。Cloudflare 的 JS 會讀這些欄位。
STEALTH_SCRIPT = """
Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
Object.defineProperty(navigator, 'platform', {get: () => 'Win32'});
Object.defineProperty(navigator, 'languages', {get: () => ['zh-TW', 'zh', 'en-US', 'en']});
Object.defineProperty(navigator, 'plugins', {
  get: () => [1, 2, 3, 4, 5].map(i => ({name: 'Plugin ' + i})),
});
Object.defineProperty(navigator, 'hardwareConcurrency', {get: () => 8});
Object.defineProperty(navigator, 'deviceMemory', {get: () => 8});
window.chrome = window.chrome || {runtime: {}, app: {}, csi: () => {}, loadTimes: () => {}};
const origQuery = window.navigator.permissions && window.navigator.permissions.query;
if (origQuery) {
  window.navigator.permissions.query = (params) =>
    params.name === 'notifications'
      ? Promise.resolve({state: Notification.permission})
      : origQuery(params);
}
"""


class PlaywrightFetcher:
    """跑真的 Chromium 去執行 Cloudflare 的 JS challenge。

    curl_cffi 只能偽裝 TLS 指紋，過不了需要執行 JavaScript 的 Managed
    Challenge；這個後端開真的瀏覽器，等 challenge 自己解完再取內容。
    """

    def __enter__(self) -> "PlaywrightFetcher":
        from playwright.sync_api import sync_playwright

        self._pw = sync_playwright().start()
        self._browser = self._pw.chromium.launch(
            headless=os.getenv("HEADLESS", "true").lower() == "true",
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-gpu",
                "--lang=zh-TW",
            ],
        )
        # UA 版本跟著實際 Chromium 走，寫死容易和指紋對不上
        version = self._browser.version.split()[-1]
        user_agent = (
            f"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            f"(KHTML, like Gecko) Chrome/{version} Safari/537.36"
        )
        print(f"Playwright 瀏覽器：Chromium {version}")

        self._context = self._browser.new_context(
            user_agent=user_agent,
            locale="zh-TW",
            timezone_id="Asia/Taipei",
            viewport={"width": 1920, "height": 1080},
            extra_http_headers={"Accept-Language": HEADERS["Accept-Language"]},
        )
        self._context.add_init_script(STEALTH_SCRIPT)
        self._page = self._context.new_page()
        return self

    def __exit__(self, *_exc) -> None:
        for closeable in (getattr(self, "_context", None), getattr(self, "_browser", None)):
            try:
                if closeable:
                    closeable.close()
            except Exception:
                pass
        try:
            self._pw.stop()
        except Exception:
            pass

    def get(self, url: str) -> str:
        self._page.goto(url, wait_until="domcontentloaded", timeout=60_000)
        try:
            # challenge 過關後會自動跳轉回真正的頁面，商品卡出現就代表成功
            self._page.wait_for_selector("a.items-card", timeout=CHALLENGE_TIMEOUT)
        except Exception:
            html = self._page.content()
            self._dump_debug()
            if looks_like_challenge(html):
                raise BlockedError(f"challenge 未通過：{describe_error(html)}")
            raise RuntimeError(f"等不到商品列表：{describe_error(html)}")
        return self._page.content()

    def _dump_debug(self) -> None:
        """卡關時留下截圖與 HTML，workflow 會當成 artifact 上傳。"""
        out = Path(__file__).resolve().parent / "debug"
        try:
            out.mkdir(exist_ok=True)
            self._page.screenshot(path=str(out / "challenge.png"), full_page=True)
            (out / "challenge.html").write_text(self._page.content(), encoding="utf-8")
            print(f"::notice::已存除錯檔（截圖 + HTML）到 {out}")
        except Exception as err:
            print(f"::warning::存除錯檔失敗：{err}")


def scrape_all(fetch_html) -> dict[str, dict]:
    """fetch_html 是 (url) -> html 的 callable，讓不同後端可以共用解析邏輯。"""
    items: dict[str, dict] = {}
    page = 1
    while page <= MAX_PAGES:
        html = fetch_html(LIST_URL.format(page=page))
        soup = BeautifulSoup(html, "html.parser")
        cards = soup.select("a.items-card")
        if not cards:
            break

        for card in cards:
            item = parse_card(card)
            if item:
                items[item["sn"]] = item

        # 有「下一頁」才繼續翻
        if not soup.select_one("#BH-pagebtn a.next[href]"):
            break
        page += 1
        time.sleep(1)

    return items


def scrape_with_fallback() -> dict[str, dict]:
    """依序嘗試各後端，被 Cloudflare 擋下就換下一個。"""
    plans = {
        # requests 墊底：住宅／行動 IP 不會被 challenge，也不需要裝重量級後端
        "auto": ["curl_cffi", "playwright", "requests"],
        "curl_cffi": ["curl_cffi"],
        "requests": ["requests"],
        "playwright": ["playwright"],
    }
    backend = os.getenv("SCRAPER_BACKEND", "auto").lower()
    plan = plans.get(backend)
    if plan is None:
        print(f"::warning::未知的 SCRAPER_BACKEND={backend}，改用 auto")
        plan = plans["auto"]

    last_err: Exception | None = None
    for kind in plan:
        print(f"--- 嘗試抓取後端：{kind} ---")
        try:
            if kind == "playwright":
                with PlaywrightFetcher() as fetcher:
                    return scrape_all(fetcher.get)
            session = build_http_session(kind)
            warm_up(session)
            return scrape_all(lambda url: fetch(url, session))
        except BlockedError as err:
            print(f"::warning::{kind} 被擋下：{err}")
            last_err = err
        except ImportError as err:
            print(f"::warning::{kind} 套件未安裝：{err}")
            last_err = err
        except Exception as err:
            print(f"::warning::{kind} 失敗：{err}")
            last_err = err

    raise RuntimeError(f"所有抓取後端都失敗，最後的錯誤：{last_err}")


# --------------------------------------------------------------------------- #
# 狀態
# --------------------------------------------------------------------------- #
def load_state() -> dict:
    if not STATE_FILE.exists():
        return {}
    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        return data.get("items", {})
    except (json.JSONDecodeError, OSError) as err:
        print(f"::warning::讀取 {STATE_FILE.name} 失敗（視為首次執行）：{err}")
        return {}


def save_state(items: dict[str, dict]) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "updated_at": datetime.now(TPE).isoformat(timespec="seconds"),
        "count": len(items),
        "items": items,
    }
    STATE_FILE.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def diff(old: dict[str, dict], new: dict[str, dict]):
    added = [new[sn] for sn in new if sn not in old]
    removed = [old[sn] for sn in old if sn not in new]
    changed = []
    for sn, item in new.items():
        if sn not in old:
            continue
        deltas = {
            field: (old[sn].get(field, ""), item.get(field, ""))
            for field in TRACKED_FIELDS
            if old[sn].get(field, "") != item.get(field, "")
        }
        if deltas:
            changed.append((item, deltas))

    added.sort(key=lambda i: int(i["sn"]), reverse=True)
    return added, changed, removed


# --------------------------------------------------------------------------- #
# Discord
# --------------------------------------------------------------------------- #
def build_embed(item: dict, *, kind: str, deltas: dict | None = None) -> dict:
    prefix = {"new": "🆕 新商品", "changed": "♻️ 商品異動"}[kind]
    embed = {
        "title": f"{prefix}｜{item['title']}"[:256],
        "url": item["url"],
        "color": TYPE_COLORS.get(item["type"], DEFAULT_COLOR),
        "fields": [],
        "footer": {"text": f"勇者福利社 · sn={item['sn']}"},
        "timestamp": datetime.now(timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z"),
    }
    if item.get("image"):
        embed["thumbnail"] = {"url": item["image"]}

    if kind == "new":
        for key, label in (
            ("type", "類型"),
            ("price", "價格"),
            ("quantity", "商品數量"),
            ("popularity", "人氣"),
        ):
            if item.get(key):
                embed["fields"].append(
                    {"name": label, "value": item[key][:1024], "inline": True}
                )
        if item.get("period"):
            embed["fields"].append(
                {"name": "活動時間", "value": item["period"][:1024], "inline": False}
            )
    else:
        for field, (before, after) in (deltas or {}).items():
            embed["fields"].append(
                {
                    "name": FIELD_LABELS.get(field, field),
                    "value": f"~~{before or '（空）'}~~ → **{after or '（空）'}**"[:1024],
                    "inline": False,
                }
            )

    return embed


def discord_target() -> tuple[str, dict]:
    """優先用 Bot Token + Channel ID，沒有的話退回 Webhook。"""
    token = os.getenv("DISCORD_BOT_TOKEN", "").strip()
    channel = os.getenv("DISCORD_CHANNEL_ID", "").strip()
    webhook = os.getenv("DISCORD_WEBHOOK_URL", "").strip()

    if token and channel:
        return (
            f"https://discord.com/api/v10/channels/{channel}/messages",
            {"Authorization": f"Bot {token}"},
        )
    if webhook:
        return webhook, {}
    raise SystemExit(
        "缺少 Discord 設定：請提供 DISCORD_BOT_TOKEN + DISCORD_CHANNEL_ID，"
        "或 DISCORD_WEBHOOK_URL"
    )


def send(session: requests.Session, content: str | None, embeds: list[dict]) -> None:
    url, headers = discord_target()
    payload: dict = {"embeds": embeds}
    if content:
        payload["content"] = content[:2000]
    if "discord.com/api/" not in url:  # webhook 才能自訂顯示名稱
        payload["username"] = "勇者福利社"

    for _ in range(5):
        resp = session.post(url, json=payload, headers=headers, timeout=REQUEST_TIMEOUT)
        if resp.status_code == 429:
            wait = float(resp.json().get("retry_after", 2))
            print(f"::notice::Discord 限流，{wait:.1f}s 後重試")
            time.sleep(wait + 0.5)
            continue
        if resp.status_code >= 400:
            raise RuntimeError(f"Discord 回應 {resp.status_code}: {resp.text[:500]}")
        return
    raise RuntimeError("Discord 連續限流，放棄送出")


def send_batched(session: requests.Session, header: str, embeds: list[dict]) -> None:
    """Discord 單則訊息最多 10 個 embed，超過就分批。"""
    for index in range(0, len(embeds), 10):
        chunk = embeds[index : index + 10]
        send(session, header if index == 0 else None, chunk)
        if index + 10 < len(embeds):
            time.sleep(1)


# --------------------------------------------------------------------------- #
def main() -> int:
    notify_changes = os.getenv("NOTIFY_CHANGES", "true").lower() == "true"
    dry_run = os.getenv("DRY_RUN", "false").lower() == "true"

    try:
        current = scrape_with_fallback()
    except Exception as err:
        print(f"::error::{err}")
        return 1

    session = requests.Session()  # Discord 用普通 requests 即可
    if not current:
        print("::error::沒有解析到任何商品，可能是網站改版或被擋；保留舊狀態不覆蓋")
        return 1
    print(f"抓到 {len(current)} 件商品")

    previous = load_state()
    first_run = not previous

    added, changed, removed = diff(previous, current)
    print(f"新增 {len(added)}／異動 {len(changed)}／下架 {len(removed)}")

    if first_run:
        # 首次執行只建立基準，不把整頁商品當成「新上架」洗版。
        # 先送再存：webhook 沒設好時 send 會拋錯，快照就不會寫下去，
        # 下次執行仍算首次，不必手動刪 seen.json。
        if not dry_run:
            send(
                session,
                f"✅ 勇者福利社監控已啟動，建立基準 {len(current)} 件商品，"
                "之後只推播新增與異動。",
                [],
            )
        save_state(current)
        print("首次執行：已建立基準快照")
        return 0

    embeds = [build_embed(item, kind="new") for item in added]
    if notify_changes:
        embeds += [build_embed(item, kind="changed", deltas=d) for item, d in changed]

    if not embeds:
        save_state(current)
        print("沒有更新，結束")
        return 0

    now = datetime.now(TPE).strftime("%Y-%m-%d %H:%M")
    parts = []
    if added:
        parts.append(f"新上架 {len(added)} 件")
    if notify_changes and changed:
        parts.append(f"異動 {len(changed)} 件")
    header = f"📢 **勇者福利社更新**（{now}）：{'、'.join(parts)}"

    if dry_run:
        print(header)
        print(json.dumps(embeds, ensure_ascii=False, indent=2))
    else:
        send_batched(session, header, embeds)

    save_state(current)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as err:
        # cron 的 log 只要一行看得懂的訊息；要完整 traceback 就設 DEBUG=true
        print(f"::error::{type(err).__name__}: {err}")
        if os.getenv("DEBUG", "").lower() == "true":
            raise
        sys.exit(1)
