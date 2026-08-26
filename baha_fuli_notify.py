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


# --------------------------------------------------------------------------- #
# 抓取與解析
# --------------------------------------------------------------------------- #
def build_scraper_session():
    """建立抓取用的 session。

    fuli.gamer.com.tw 掛在 Cloudflare 後面，會用 TLS 指紋 + IP 信譽判斷是不是
    瀏覽器。用普通 requests 從 GitHub Actions（資料中心 IP）打會吃 403，
    所以優先用 curl_cffi 模擬 Chrome 的 TLS/HTTP2 指紋。
    """
    backend = os.getenv("SCRAPER_BACKEND", "auto").lower()

    if backend in ("auto", "curl_cffi"):
        try:
            from curl_cffi import requests as curl_requests

            session = curl_requests.Session(
                impersonate=os.getenv("IMPERSONATE", "chrome")
            )
            print("抓取後端：curl_cffi（模擬 Chrome）")
            return session
        except ImportError:
            if backend == "curl_cffi":
                raise SystemExit("指定了 curl_cffi 後端但套件沒安裝")
            print("::warning::curl_cffi 未安裝，退回 requests（雲端 IP 可能被 403）")

    print("抓取後端：requests")
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


def fetch(url: str, session, retries: int = 3) -> str:
    last_err = None
    headers = dict(HEADERS, Referer=BASE_URL)

    for attempt in range(retries):
        try:
            resp = session.get(url, headers=headers, timeout=REQUEST_TIMEOUT)
            if resp.status_code >= 400:
                # 印一段 body 方便判斷是 Cloudflare 擋還是站方改版
                snippet = clean(resp.text)[:300] if resp.text else "(空)"
                print(f"::warning::{url} 回應 {resp.status_code}：{snippet}")
                raise RuntimeError(f"{resp.status_code} for {url}")
            resp.encoding = "utf-8"
            return resp.text
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


def scrape_all(session) -> dict[str, dict]:
    items: dict[str, dict] = {}
    page = 1
    while page <= MAX_PAGES:
        html = fetch(LIST_URL.format(page=page), session)
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

    scraper = build_scraper_session()
    warm_up(scraper)
    current = scrape_all(scraper)
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
        # 首次執行只建立基準，不把整頁商品當成「新上架」洗版
        save_state(current)
        if not dry_run:
            send(
                session,
                f"✅ 勇者福利社監控已啟動，建立基準 {len(current)} 件商品，"
                "之後只推播新增與異動。",
                [],
            )
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
    sys.exit(main())
