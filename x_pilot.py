"""
X metrics collector — Playwright edition.
Loads @<handle>'s X profile, extracts posts aged 1.5–5 days,
and writes rows to the Notion Post Outputs database.

Env:
  NOTION_TOKEN  — Notion internal integration secret
  DATABASE_ID   — 32-hex Post Outputs database id
  X_HANDLE      — X handle without @ (default: Mylovanov)

No X login required — view counts are shown publicly on profile timeline.
"""

import json
import os
import re
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

from playwright.sync_api import sync_playwright

HANDLE = os.environ.get("X_HANDLE", "Mylovanov")
NOTION_TOKEN = os.environ["NOTION_TOKEN"]
DATABASE_ID = os.environ["DATABASE_ID"]

NOW = datetime.now(timezone.utc)
MIN_AGE = timedelta(hours=36)   # skip too-fresh posts
MAX_AGE = timedelta(days=5)     # skip stale posts
MAX_ROWS = 20


def parse_number(text):
    """'1,234' | '1.2K' | '3M' -> int."""
    if not text:
        return None
    text = text.strip().replace(",", "").replace(" ", "")
    m = re.match(r"^([\d.]+)([KMB]?)$", text, re.IGNORECASE)
    if not m:
        return None
    num = float(m.group(1))
    suf = m.group(2).upper()
    mult = {"": 1, "K": 1_000, "M": 1_000_000, "B": 1_000_000_000}[suf]
    return int(num * mult)


def extract_views_from_aria(aria):
    if not aria:
        return None
    m = re.search(r"([\d.,KMB]+)\s*[Vv]iew", aria)
    return parse_number(m.group(1)) if m else None


def collect_posts():
    """Load profile timeline, return list of {url, published_at, views}."""
    posts = []
    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True, args=["--no-sandbox", "--disable-blink-features=AutomationControlled"]
        )
        ctx = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1280, "height": 900},
            locale="en-US",
        )
        page = ctx.new_page()
        url = f"https://x.com/{HANDLE}"
        print(f"opening {url}")
        page.goto(url, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(3000)
        for _ in range(5):
            page.mouse.wheel(0, 2500)
            page.wait_for_timeout(1500)
        articles = page.query_selector_all("article[data-testid='tweet']")
        print(f"found {len(articles)} article nodes")
        seen = set()
        for a in articles:
            try:
                link_el = a.query_selector("a[href*='/status/']")
                if not link_el:
                    continue
                href = link_el.get_attribute("href") or ""
                m = re.search(r"/status/(\d+)", href)
                if not m:
                    continue
                tweet_id = m.group(1)
                if tweet_id in seen:
                    continue
                seen.add(tweet_id)
                post_url = f"https://x.com/{HANDLE}/status/{tweet_id}"
                time_el = a.query_selector("time")
                published_at = time_el.get_attribute("datetime") if time_el else None
                views = None
                group = a.query_selector("[role='group']")
                if group:
                    views = extract_views_from_aria(group.get_attribute("aria-label"))
                if views is None:
                    analytics = a.query_selector("a[href$='/analytics']")
                    if analytics:
                        views = extract_views_from_aria(analytics.get_attribute("aria-label"))
                posts.append(
                    {"url": post_url, "published_at": published_at, "views": views}
                )
            except Exception as e:
                print(f"skip one article: {e}")
        browser.close()
    return posts


def filter_posts(posts):
    kept = []
    for p in posts:
        if not p["published_at"]:
            continue
        try:
            dt = datetime.fromisoformat(p["published_at"].replace("Z", "+00:00"))
        except Exception:
            continue
        age = NOW - dt
        if age < MIN_AGE or age > MAX_AGE:
            continue
        kept.append(p)
    kept.sort(key=lambda x: x["published_at"], reverse=True)
    return kept[:MAX_ROWS]


def notion_create(post):
    tweet_id = post["url"].rsplit("/", 1)[-1]
    props = {
        "Post": {"title": [{"text": {"content": f"@{HANDLE} · {tweet_id}"}}]},
        "Channel": {"select": {"name": "X"}},
        "Post URL": {"url": post["url"]},
        "Published at": {"date": {"start": post["published_at"]}},
        "Run status": {
            "select": {
                "name": "Views captured" if post["views"] is not None else "No views"
            }
        },
    }
    if post["views"] is not None:
        props["Metric"] = {"number": post["views"]}
    payload = json.dumps(
        {"parent": {"database_id": DATABASE_ID}, "properties": props}
    ).encode()
    req = urllib.request.Request(
        "https://api.notion.com/v1/pages",
        data=payload,
        method="POST",
        headers={
            "Authorization": f"Bearer {NOTION_TOKEN}",
            "Notion-Version": "2022-06-28",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as res:
            res.read()
        return True
    except urllib.error.HTTPError as e:
        print(f"notion error {e.code}: {e.read().decode(errors='replace')}")
        return False


def main():
    posts = collect_posts()
    print(f"raw posts: {len(posts)}")
    kept = filter_posts(posts)
    print(f"after age filter (1.5-5 days): {len(kept)}")
    saved = 0
    no_views = 0
    for p in kept:
        if notion_create(p):
            saved += 1
            if p["views"] is None:
                no_views += 1
    print(f"saved={saved}, no_views={no_views}")
    if saved == 0:
        print("verdict: no qualifying posts in the 1.5-5 day window")
    elif no_views == saved:
        print("verdict: views gated — Playwright saw posts but no view counts")
    else:
        print("verdict: OK — views captured")


if __name__ == "__main__":
    main()
