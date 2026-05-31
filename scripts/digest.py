#!/usr/bin/env python3
"""
News Digest hourly checker — RSS-based, free, runs on GitHub Actions.

Reads Firestore config (slots + categories) -> fetches RSS feeds for due regions ->
builds digest -> sends to Telegram -> writes to Firestore rolling history.

Env vars required:
  TELEGRAM_BOT_TOKEN

No auth needed for Firestore reads/writes (newsDigest collection allows anon writes
to feed_* per security rules).
"""

import os
import re
import sys
import json
import time
from datetime import datetime, timezone, timedelta
from urllib.parse import urlparse

import requests
import feedparser
from zoneinfo import ZoneInfo

# ============================================================
#  CONFIG
# ============================================================
FIRESTORE_PROJECT = "wing-news-digest-v2"
TELEGRAM_CHAT_ID = "-1003808016427"
TIMEZONE = "Asia/Kuala_Lumpur"
TZ = ZoneInfo(TIMEZONE)
STORIES_PER_CATEGORY = 2
MAX_ROLLING_ITEMS = 6
TELEGRAM_CHUNK_CHARS = 3800
SAME_DAY_TOLERANCE_HOURS = 30  # allow yesterday's late-evening news too

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
if not TELEGRAM_TOKEN:
    print("ERROR: TELEGRAM_BOT_TOKEN env var not set", file=sys.stderr)
    sys.exit(1)

# ============================================================
#  RSS FEEDS BY REGION + CATEGORY
# ============================================================
FEEDS = {
    "malaysia": {
        "general": [
            ("https://www.thestar.com.my/rss/News/Nation", "The Star"),
            ("https://www.nst.com.my/news/nation.rss", "NST"),
            ("https://www.freemalaysiatoday.com/category/nation/feed/", "FMT"),
        ],
        "political": [
            ("https://www.freemalaysiatoday.com/category/nation/feed/", "FMT"),
            ("https://www.thestar.com.my/rss/News/Nation", "The Star"),
        ],
        "world": [
            ("https://www.thestar.com.my/rss/News/World", "The Star"),
            ("https://www.nst.com.my/world.rss", "NST"),
        ],
        "business": [
            ("https://www.thestar.com.my/rss/Business/Business-News", "The Star"),
            ("https://www.nst.com.my/business.rss", "NST"),
            ("https://www.freemalaysiatoday.com/category/business/feed/", "FMT"),
        ],
        "markets": [
            ("https://www.thestar.com.my/rss/Business/Business-News", "The Star"),
            ("https://www.nst.com.my/business.rss", "NST"),
        ],
        "tech": [
            ("https://www.thestar.com.my/rss/Tech", "The Star"),
            ("https://www.nst.com.my/lifestyle/groove.rss", "NST"),
        ],
        "entertainment": [
            ("https://www.thestar.com.my/rss/Lifestyle/Entertainment", "The Star"),
            ("https://www.freemalaysiatoday.com/category/leisure/feed/", "FMT"),
        ],
        "sports": [
            ("https://www.thestar.com.my/rss/Sport", "The Star"),
        ],
        "health": [
            ("https://www.thestar.com.my/rss/Lifestyle/Health", "The Star"),
        ],
        "travel": [
            ("https://www.thestar.com.my/rss/Lifestyle/Travel", "The Star"),
        ],
    },
    "global": {
        "general": [
            ("http://feeds.bbci.co.uk/news/world/rss.xml", "BBC"),
            ("https://www.aljazeera.com/xml/rss/all.xml", "Al Jazeera"),
        ],
        "world": [
            ("http://feeds.bbci.co.uk/news/world/rss.xml", "BBC"),
            ("https://www.aljazeera.com/xml/rss/all.xml", "Al Jazeera"),
        ],
        "political": [
            ("http://feeds.bbci.co.uk/news/politics/rss.xml", "BBC"),
        ],
        "business": [
            ("https://www.cnbc.com/id/10001147/device/rss/rss.html", "CNBC"),
            ("http://feeds.bbci.co.uk/news/business/rss.xml", "BBC"),
        ],
        "markets": [
            ("https://www.cnbc.com/id/15839069/device/rss/rss.html", "CNBC"),
        ],
        "tech": [
            ("http://feeds.bbci.co.uk/news/technology/rss.xml", "BBC"),
            ("https://www.theverge.com/rss/index.xml", "The Verge"),
            ("https://techcrunch.com/feed/", "TechCrunch"),
        ],
        "entertainment": [
            ("http://feeds.bbci.co.uk/news/entertainment_and_arts/rss.xml", "BBC"),
            ("https://variety.com/feed/", "Variety"),
        ],
        "sports": [
            ("https://www.espn.com/espn/rss/news", "ESPN"),
            ("http://feeds.bbci.co.uk/sport/rss.xml", "BBC Sport"),
        ],
        "crypto": [
            ("https://www.coindesk.com/arc/outboundfeeds/rss/", "CoinDesk"),
        ],
        "science": [
            ("http://feeds.bbci.co.uk/news/science_and_environment/rss.xml", "BBC"),
        ],
        "health": [
            ("http://feeds.bbci.co.uk/news/health/rss.xml", "BBC"),
        ],
        "environment": [
            ("http://feeds.bbci.co.uk/news/science_and_environment/rss.xml", "BBC"),
        ],
    },
}

CATEGORY_HEADERS = {
    "general": "📰 General / Top Stories",
    "political": "🏛 Political",
    "world": "🌍 World & Geopolitics",
    "business": "💰 Business & Economy",
    "markets": "📈 Markets & Investing",
    "crypto": "🪙 Crypto & Web3",
    "tech": "🤖 Tech & AI",
    "science": "🔬 Science & Space",
    "health": "🏥 Health & Medicine",
    "environment": "🌱 Environment & Climate",
    "property": "🏠 Property & Real Estate",
    "auto": "🚗 Automotive",
    "education": "🎓 Education",
    "entertainment": "🎬 Entertainment / Lifestyle",
    "sports": "⚽ Sports",
    "travel": "✈️ Travel",
}


# ============================================================
#  FIRESTORE
# ============================================================
def firestore_url(doc: str) -> str:
    return f"https://firestore.googleapis.com/v1/projects/{FIRESTORE_PROJECT}/databases/(default)/documents/newsDigest/{doc}"


def parse_firestore_value(v: dict):
    """Recursively unpack Firestore typed JSON into plain Python."""
    if "stringValue" in v:
        return v["stringValue"]
    if "booleanValue" in v:
        return v["booleanValue"]
    if "integerValue" in v:
        return int(v["integerValue"])
    if "doubleValue" in v:
        return float(v["doubleValue"])
    if "timestampValue" in v:
        return v["timestampValue"]
    if "nullValue" in v:
        return None
    if "arrayValue" in v:
        return [parse_firestore_value(x) for x in v["arrayValue"].get("values", [])]
    if "mapValue" in v:
        return {k: parse_firestore_value(val) for k, val in v["mapValue"].get("fields", {}).items()}
    return None


def get_config() -> dict:
    r = requests.get(firestore_url("config"), timeout=10)
    if r.status_code != 200:
        print(f"Config fetch failed: HTTP {r.status_code}", file=sys.stderr)
        sys.exit(0)
    doc = r.json()
    fields = doc.get("fields", {})
    return {
        "timezone": parse_firestore_value(fields.get("timezone", {"stringValue": TIMEZONE})),
        "regions": parse_firestore_value(fields.get("regions", {"mapValue": {"fields": {}}})) or {},
    }


def get_feed_items(region_key: str) -> list:
    r = requests.get(firestore_url(f"feed_{region_key}"), timeout=10)
    if r.status_code == 404:
        return []
    if r.status_code != 200:
        return []
    doc = r.json()
    items_field = doc.get("fields", {}).get("items", {})
    return items_field.get("arrayValue", {}).get("values", [])


def write_feed_items(region_key: str, items: list):
    body = {"fields": {"items": {"arrayValue": {"values": items}}}}
    r = requests.patch(firestore_url(f"feed_{region_key}"), json=body, timeout=15)
    if r.status_code not in (200, 201):
        print(f"Firestore PATCH failed: HTTP {r.status_code} — {r.text[:200]}", file=sys.stderr)


def already_done_this_hour(region_key: str, hour: int) -> bool:
    """Dedup: skip if newest item in feed is from today + same local hour.
    Needed because GitHub Actions multi-cron fires up to 3x per hour."""
    items = get_feed_items(region_key)
    if not items:
        return False
    newest = items[0]
    newest_ts = newest.get("mapValue", {}).get("fields", {}).get("updatedAt", {}).get("timestampValue", "")
    if not newest_ts:
        return False
    try:
        ts_utc = datetime.fromisoformat(newest_ts.replace("Z", "+00:00"))
        ts_local = ts_utc.astimezone(TZ)
        now_local = datetime.now(TZ)
        return ts_local.date() == now_local.date() and ts_local.hour == hour
    except Exception:
        return False


# ============================================================
#  RSS FETCH + FILTER
# ============================================================
def fetch_stories(region_key: str, enabled_categories: list[str]) -> dict:
    now = datetime.now(TZ)
    cutoff = now - timedelta(hours=SAME_DAY_TOLERANCE_HOURS)
    region_feeds = FEEDS.get(region_key, {})
    out = {}
    seen_titles = set()  # global dedupe across categories

    for cat in enabled_categories:
        if cat not in region_feeds:
            continue
        cat_stories = []
        for url, source in region_feeds[cat]:
            try:
                parsed = feedparser.parse(url, request_headers={"User-Agent": "Mozilla/5.0 NewsDigestBot/1.0"})
            except Exception as e:
                print(f"  RSS fetch failed for {url}: {e}", file=sys.stderr)
                continue
            for entry in parsed.entries[:15]:
                title = (entry.get("title") or "").strip()
                if not title:
                    continue
                norm = re.sub(r"\W+", " ", title.lower()).strip()
                if norm in seen_titles or len(norm) < 10:
                    continue
                # Try to parse published date
                pub_dt = None
                for k in ("published_parsed", "updated_parsed"):
                    pp = entry.get(k)
                    if pp:
                        try:
                            pub_dt = datetime(*pp[:6], tzinfo=timezone.utc).astimezone(TZ)
                            break
                        except Exception:
                            pass
                if pub_dt and pub_dt < cutoff:
                    continue  # too old
                seen_titles.add(norm)
                summary = re.sub(r"<[^>]+>", "", entry.get("summary", "") or "").strip()
                summary = re.sub(r"\s+", " ", summary)[:240]
                cat_stories.append({
                    "title": title,
                    "summary": summary,
                    "url": entry.get("link", ""),
                    "source": source,
                    "date": (pub_dt or now).strftime("%b %-d, %Y"),
                })
                if len(cat_stories) >= STORIES_PER_CATEGORY:
                    break
            if len(cat_stories) >= STORIES_PER_CATEGORY:
                break
        if cat_stories:
            out[cat] = cat_stories
    return out


# ============================================================
#  DIGEST BUILD
# ============================================================
# Traffic-light impact classifier. Picks 🟢 (positive), 🔴 (negative) or
# 🟡 (neutral/mixed) from the headline + summary. Never returns ⚪ white.
_POS_WORDS = (
    "surge", "soar", "jump", "rally", "record", "gain", "gains", "rise", "rises",
    "boost", "win", "wins", "won", "beat", "beats", "growth", "grows", "profit",
    "approve", "approved", "deal", "agree", "agreement", "recovery", "rebound",
    "breakthrough", "launch", "expand", "upgrade", "success", "milestone",
    "invest", "investment", "partnership", "award", "celebrate", "high", "top",
)
_NEG_WORDS = (
    "dies", "die", "death", "killed", "kill", "fire", "crash", "attack", "strike",
    "war", "conflict", "drop", "fall", "falls", "plunge", "slump", "loss", "losses",
    "cut", "cuts", "ban", "warning", "warn", "fear", "fears", "crisis", "shortage",
    "decline", "slows", "weak", "outflow", "fraud", "scandal", "protest", "clash",
    "arrest", "injured", "victim", "collapse", "recession", "layoff", "default",
    "tension", "tensions", "threat", "sanction", "delay", "probe", "lawsuit", "row",
)

def classify_impact(title: str, summary: str) -> str:
    text = f"{title} {summary}".lower()
    pos = sum(1 for w in _POS_WORDS if w in text)
    neg = sum(1 for w in _NEG_WORDS if w in text)
    if neg > pos:
        return "🔴"
    if pos > neg:
        return "🟢"
    return "🟡"  # neutral / mixed — never ⚪


def build_digest(region_key: str, hour: int, stories_by_cat: dict) -> tuple[str, str]:
    label = "WORLD" if region_key == "global" else "MALAYSIA"
    h12 = hour % 12 or 12
    ampm = "AM" if hour < 12 else "PM"
    today_str = datetime.now(TZ).strftime("%A, %B %-d, %Y")
    header_label = f"{label} — {h12}:00 {ampm}"
    lines = [
        f"📰 {label} NEWS DIGEST — {h12}:00 {ampm}",
        f"📅 {today_str}",
        "",
    ]
    has_any = False
    for cat, header in CATEGORY_HEADERS.items():
        if cat not in stories_by_cat:
            continue
        stories = stories_by_cat[cat]
        if not stories:
            continue
        has_any = True
        lines.append(header)
        for i, s in enumerate(stories, 1):
            lines.append(f"{i}. {s['title']}")
            if s["summary"]:
                lines.append(s["summary"])
            lines.append(f"{classify_impact(s['title'], s['summary'])} Impact | 📰 {s['source']} | 📅 {s['date']}")
            lines.append(f"🔗 {s['url']}")
            lines.append("──────────")
        lines.append("")
    if not has_any:
        return header_label, f"{label} — no notable same-day news today."
    return header_label, "\n".join(lines).rstrip() + "\n"


# ============================================================
#  TELEGRAM
# ============================================================
def chunk_for_telegram(text: str) -> list[str]:
    if len(text) <= TELEGRAM_CHUNK_CHARS:
        return [text]
    # Split on story boundaries (──────────)
    parts = text.split("──────────\n")
    chunks = []
    current = ""
    for p in parts:
        piece = p + ("──────────\n" if p != parts[-1] else "")
        if len(current) + len(piece) > TELEGRAM_CHUNK_CHARS and current:
            chunks.append(current.rstrip())
            current = piece
        else:
            current += piece
    if current.strip():
        chunks.append(current.rstrip())
    # Add (n/N) markers
    if len(chunks) > 1:
        out = []
        for i, c in enumerate(chunks, 1):
            # Inject (i/N) into the first line if it starts with "📰"
            first_nl = c.find("\n")
            if first_nl > 0 and c.startswith("📰"):
                c = c[:first_nl] + f" ({i}/{len(chunks)})" + c[first_nl:]
            out.append(c)
        return out
    return chunks


def send_telegram(text: str):
    chunks = chunk_for_telegram(text)
    for i, chunk in enumerate(chunks):
        try:
            r = requests.post(
                f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
                data={
                    "chat_id": TELEGRAM_CHAT_ID,
                    "text": chunk,
                    "disable_web_page_preview": "true",
                },
                timeout=15,
            )
            if r.status_code != 200 or not r.json().get("ok"):
                print(f"Telegram send failed (chunk {i+1}/{len(chunks)}): {r.text[:200]}", file=sys.stderr)
                return False
        except Exception as e:
            print(f"Telegram send exception: {e}", file=sys.stderr)
            return False
        time.sleep(0.3)  # small delay between chunks
    return True


# ============================================================
#  MAIN
# ============================================================
def main():
    print(f"=== Run at {datetime.now(TZ).isoformat()} ===")
    cfg = get_config()
    now = datetime.now(TZ)
    hour = now.hour
    print(f"Current local hour: {hour}")

    regions = cfg.get("regions", {})
    fired_any = False

    for region_key in ["global", "malaysia"]:
        rconf = regions.get(region_key, {})
        slots = rconf.get("slots", []) or []
        cats_map = rconf.get("categories", {}) or {}
        enabled_cats = [c for c, on in cats_map.items() if on]
        is_due = any(s.get("enabled") and int(s.get("hour", -1)) == hour for s in slots)
        if not is_due:
            print(f"  {region_key}: not due at H={hour}, skip")
            continue
        if not enabled_cats:
            print(f"  {region_key}: due but no categories enabled, skip")
            continue

        if already_done_this_hour(region_key, hour):
            print(f"  {region_key}: DUE but already sent at H={hour} today (dedup), skip")
            continue

        print(f"  {region_key}: DUE — fetching {len(enabled_cats)} categories")
        stories = fetch_stories(region_key, enabled_cats)
        print(f"  {region_key}: got {sum(len(v) for v in stories.values())} stories across {len(stories)} categories")

        label, digest_text = build_digest(region_key, hour, stories)

        ok = send_telegram(digest_text)
        if not ok:
            print(f"  {region_key}: Telegram send FAILED, skipping Firestore write")
            continue
        print(f"  {region_key}: Telegram sent ({len(digest_text)} chars)")

        # Save to Firestore rolling history
        new_item = {
            "mapValue": {
                "fields": {
                    "label": {"stringValue": label},
                    "updatedAt": {"timestampValue": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")},
                    "text": {"stringValue": digest_text},
                }
            }
        }
        existing = get_feed_items(region_key)
        items = [new_item] + existing
        items = items[:MAX_ROLLING_ITEMS]
        write_feed_items(region_key, items)
        print(f"  {region_key}: Firestore feed updated ({len(items)} items)")
        fired_any = True

    if not fired_any:
        print(f"Nothing was due at H={hour} — exiting silently.")
    else:
        print("=== Done ===")


if __name__ == "__main__":
    main()
