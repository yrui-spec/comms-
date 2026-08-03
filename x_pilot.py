"""
X scroll diagnostic — screenshot version.

Purpose:
- Check whether GitHub/Playwright can scroll deeper in @Mylovanov's X profile.
- Save screenshots at the start, middle, and end.
- Print how many unique posts were found and which dates were reached.
- Does NOT write anything to Notion.

Required GitHub Secret:
- X_COOKIES_JSON

Optional env:
- X_HANDLE
- MAX_SCROLLS
"""

import json
import os
import re
from collections import Counter
from datetime import datetime, timezone
from typing import Dict, List, Optional

from playwright.sync_api import sync_playwright


HANDLE = os.environ.get("X_HANDLE", "Mylovanov")
X_COOKIES_JSON = os.environ.get("X_COOKIES_JSON", "")
MAX_SCROLLS = int(os.environ.get("MAX_SCROLLS", "80"))
SCROLL_PAUSE_MS = int(os.environ.get("SCROLL_PAUSE_MS", "2000"))


def normalize_cookies(raw: str) -> List[Dict]:
    if not raw:
        raise RuntimeError("X_COOKIES_JSON is missing")

    cookies = json.loads(raw)
    normalized = []

    for cookie in cookies:
        name = cookie.get("name")
        value = cookie.get("value")
        if not name or value is None:
            continue

        normalized.append(
            {
                "name": name,
                "value": value,
                "domain": cookie.get("domain") or ".x.com",
                "path": cookie.get("path") or "/",
                "httpOnly": bool(cookie.get("httpOnly", False)),
                "secure": True,
                "sameSite": cookie.get("sameSite", "Lax"),
            }
        )

    return normalized


def parse_published_at(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except Exception:
        return None


def collect_visible_posts(page, posts_by_id: Dict[str, Dict]) -> int:
    new_count = 0
    articles = page.query_selector_all("article")

    for article in articles:
        try:
            link_elements = article.query_selector_all("a[href*='/status/']")
            tweet_id = None

            for link in link_elements:
                href = link.get_attribute("href") or ""
                match = re.search(r"/status/(\d+)", href)
                if match:
                    tweet_id = match.group(1)
                    break

            if not tweet_id or tweet_id in posts_by_id:
                continue

            time_el = article.query_selector("time")
            published_at = time_el.get_attribute("datetime") if time_el else None
            published_dt = parse_published_at(published_at)

            posts_by_id[tweet_id] = {
                "tweet_id": tweet_id,
                "published_at": published_at,
                "published_dt": published_dt,
            }
            new_count += 1

        except Exception as error:
            print(f"skip article: {error}")

    return new_count


def summarize_dates(posts_by_id: Dict[str, Dict]) -> None:
    dated = [post for post in posts_by_id.values() if post.get("published_dt")]

    if not dated:
        print("date summary: no dated posts found")
        return

    newest = max(post["published_dt"] for post in dated)
    oldest = min(post["published_dt"] for post in dated)
    counts = Counter(post["published_dt"].date().isoformat() for post in dated)

    print(f"newest collected: {newest.isoformat()}")
    print(f"oldest collected: {oldest.isoformat()}")
    print("date counts:")
    for day, count in sorted(counts.items(), reverse=True):
        print(f"  {day}: {count}")


def main() -> None:
    posts_by_id: Dict[str, Dict] = {}

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-blink-features=AutomationControlled"],
        )
        context = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1280, "height": 1400},
            locale="en-US",
        )

        cookies = normalize_cookies(X_COOKIES_JSON)
        context.add_cookies(cookies)
        print(f"loaded cookies: {len(cookies)}")

        page = context.new_page()
        url = f"https://x.com/{HANDLE}"
        print(f"opening {url}")
        print(f"diagnostic max scrolls: {MAX_SCROLLS}")

        page.goto(url, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(8000)

        collect_visible_posts(page, posts_by_id)
        page.screenshot(path="x_debug_00_start.png", full_page=True)
        print("saved screenshot: x_debug_00_start.png")
        print(f"scroll -1: raw={len(posts_by_id)}")

        no_new_streak = 0

        for scroll_index in range(MAX_SCROLLS):
            before = len(posts_by_id)

            page.mouse.wheel(0, 3500)
            page.wait_for_timeout(SCROLL_PAUSE_MS)

            new_count = collect_visible_posts(page, posts_by_id)
            after = len(posts_by_id)

            if after == before:
                no_new_streak += 1
            else:
                no_new_streak = 0

            dated = [post for post in posts_by_id.values() if post.get("published_dt")]
            oldest = min((post["published_dt"] for post in dated), default=None)
            oldest_text = oldest.isoformat() if oldest else "none"

            print(
                f"scroll {scroll_index}: raw={after}, new={new_count}, "
                f"no_new_streak={no_new_streak}, oldest={oldest_text}"
            )

            if scroll_index == 20:
                page.screenshot(path="x_debug_20_scrolls.png", full_page=True)
                print("saved screenshot: x_debug_20_scrolls.png")

            if scroll_index == 50:
                page.screenshot(path="x_debug_50_scrolls.png", full_page=True)
                print("saved screenshot: x_debug_50_scrolls.png")

            if no_new_streak >= 12:
                print("stop: X stopped loading new posts")
                break

        page.screenshot(path="x_debug_final.png", full_page=True)
        print("saved screenshot: x_debug_final.png")
        browser.close()

    print(f"raw posts total: {len(posts_by_id)}")
    summarize_dates(posts_by_id)
    print("verdict: diagnostic complete — check screenshots artifact")


if __name__ == "__main__":
    main()
