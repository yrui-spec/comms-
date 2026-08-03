"""
X metrics collector — logged-in cookie version.

Uses X_COOKIES_JSON from GitHub Secrets to open X as a logged-in browser,
scroll @<handle>'s profile, collect visible post URLs / timestamps / views,
and write qualifying posts to the Notion Post Outputs database.
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
X_COOKIES_JSON = os.environ.get("X_COOKIES_JSON", "")

NOW = datetime.now(timezone.utc)
MIN_AGE = timedelta(hours=36)
MAX_AGE = timedelta(days=5)
MAX_ROWS = 20


def parse_number(text):
    if not text:
        return None
    text = text.strip().replace(",", "").replace(" ", "")
    match = re.match(r"^([\d.]+)([KMB]?)$", text, re.IGNORECASE)
    if not match:
        return None
    number = float(match.group(1))
    suffix = match.group(2).upper()
    multiplier = {"": 1, "K": 1_000, "M": 1_000_000, "B": 1_000_000_000}[suffix]
    return int(number * multiplier)


def extract_views_from_text(text):
    if not text:
        return None
    candidates = re.findall(r"\b\d+(?:[.,]\d+)?[KMB]?\b", text, flags=re.IGNORECASE)
    compact = []
    for candidate in candidates:
        value = parse_number(candidate)
        if value is not None:
            compact.append(value)
    return max(compact) if compact else None


def normalize_cookies(raw):
    if not raw:
        raise RuntimeError("X_COOKIES_JSON is missing")
    cookies = json.loads(raw)
    normalized = []
    for cookie in cookies:
        name = cookie.get("name")
        value = cookie.get("value")
        if not name or value is None:
            continue
        normalized.append({
            "name": name,
            "value": value,
            "domain": cookie.get("domain") or ".x.com",
            "path": cookie.get("path") or "/",
            "httpOnly": bool(cookie.get("httpOnly", False)),
            "secure": True,
            "sameSite": cookie.get("sameSite", "Lax"),
        })
    return normalized


def collect_posts():
    posts = []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=["--no-sandbox", "--disable-blink-features=AutomationControlled"])
        context = browser.new_context(
            user_agent="Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            viewport={"width": 1280, "height": 1400},
            locale="en-US",
        )
        cookies = normalize_cookies(X_COOKIES_JSON)
        context.add_cookies(cookies)
        print(f"loaded cookies: {len(cookies)}")
        page = context.new_page()
        url = f"https://x.com/{HANDLE}"
        print(f"opening {url}")
        page.goto(url, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(8000)
        page.screenshot(path="x_debug_home.png", full_page=True)
        print("saved screenshot: x_debug_home.png")
        seen = set()
        for scroll_index in range(12):
            articles = page.query_selector_all("article")
            print(f"scroll {scroll_index}: article nodes={len(articles)}")
            for article in articles:
                try:
                    text = article.inner_text(timeout=3000)
                    link_elements = article.query_selector_all("a[href*='/status/']")
                    tweet_id = None
                    for link in link_elements:
                        href = link.get_attribute("href") or ""
                        match = re.search(r"/status/(\d+)", href)
                        if match:
                            tweet_id = match.group(1)
                            break
                    if not tweet_id or tweet_id in seen:
                        continue
                    seen.add(tweet_id)
                    time_el = article.query_selector("time")
                    published_at = time_el.get_attribute("datetime") if time_el else None
                    views = extract_views_from_text(text)
                    posts.append({
                        "url": f"https://x.com/{HANDLE}/status/{tweet_id}",
                        "published_at": published_at,
                        "views": views,
                        "text": text[:120].replace("\n", " "),
                    })
                    print(f"found post: {tweet_id} published_at={published_at} views={views}")
                except Exception as error:
                    print(f"skip article: {error}")
            page.mouse.wheel(0, 2500)
            page.wait_for_timeout(2500)
        page.screenshot(path="x_debug_after_scroll.png", full_page=True)
        print("saved screenshot: x_debug_after_scroll.png")
        browser.close()
    return posts


def filter_posts(posts):
    kept = []
    for post in posts:
        if not post["published_at"]:
            continue
        try:
            published_dt = datetime.fromisoformat(post["published_at"].replace("Z", "+00:00"))
        except Exception:
            continue
        age = NOW - published_dt
        if MIN_AGE <= age <= MAX_AGE:
            kept.append(post)
    kept.sort(key=lambda item: item["published_at"], reverse=True)
    return kept[:MAX_ROWS]


def notion_create(post):
    tweet_id = post["url"].rsplit("/", 1)[-1]
    properties = {
        "Post": {"title": [{"text": {"content": f"@{HANDLE} · {tweet_id}"}}]},
        "Channel": {"select": {"name": "X"}},
        "Post URL": {"url": post["url"]},
        "Published at": {"date": {"start": post["published_at"]}},
        "Run status": {"select": {"name": "Views captured" if post["views"] is not None else "No views"}},
    }
    if post["views"] is not None:
        properties["Metric"] = {"number": post["views"]}
    payload = json.dumps({"parent": {"database_id": DATABASE_ID}, "properties": properties}).encode("utf-8")
    request = urllib.request.Request(
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
        with urllib.request.urlopen(request, timeout=30) as response:
            response.read()
        return True
    except urllib.error.HTTPError as error:
        print(f"notion error {error.code}: {error.read().decode(errors='replace')}")
        return False


def main():
    posts = collect_posts()
    print(f"raw posts: {len(posts)}")
    kept = filter_posts(posts)
    print(f"after age filter (1.5–5 days): {len(kept)}")
    saved = 0
    no_views = 0
    for post in kept:
        if notion_create(post):
            saved += 1
            if post["views"] is None:
                no_views += 1
    print(f"saved={saved}, no_views={no_views}")
    if saved == 0:
        print("verdict: no qualifying posts saved")
    elif no_views == saved:
        print("verdict: posts saved, but views were not captured")
    else:
        print("verdict: OK — posts saved and views captured")


if __name__ == "__main__":
    main()
