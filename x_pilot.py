"""
X metrics collector — debug version.

This version opens @<handle>'s X profile, saves a screenshot of what GitHub Actions sees,
then tries to find post cards in the page.
"""

import os
import re
from datetime import datetime, timedelta, timezone
from playwright.sync_api import sync_playwright

HANDLE = os.environ.get("X_HANDLE", "Mylovanov")
NOW = datetime.now(timezone.utc)
MIN_AGE = timedelta(hours=36)
MAX_AGE = timedelta(days=5)

def main():
    url = f"https://x.com/{HANDLE}"

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
            viewport={"width": 1280, "height": 1200},
            locale="en-US",
        )

        page = context.new_page()

        print(f"opening {url}")
        page.goto(url, wait_until="domcontentloaded", timeout=60000)

        print("waiting for page to load...")
        page.wait_for_timeout(8000)

        page.screenshot(path="x_debug_home.png", full_page=True)
        print("saved screenshot: x_debug_home.png")

        title = page.title()
        print(f"page title: {title}")

        html = page.content()
        print(f"html length: {len(html)}")

        if "Log in" in html or "Sign in" in html:
            print("diagnosis: X is showing a login/sign-in screen")

        if "Something went wrong" in html:
            print("diagnosis: X is showing an error page")

        if "This browser is no longer supported" in html:
            print("diagnosis: X is blocking this browser environment")

        articles = page.query_selector_all("article[data-testid='tweet']")
        print(f"found article nodes before scroll: {len(articles)}")

        for i in range(5):
            page.mouse.wheel(0, 2500)
            page.wait_for_timeout(2000)
            articles = page.query_selector_all("article[data-testid='tweet']")
            print(f"after scroll {i + 1}: found article nodes: {len(articles)}")

        page.screenshot(path="x_debug_after_scroll.png", full_page=True)
        print("saved screenshot: x_debug_after_scroll.png")

        posts = []
        seen = set()

        for article in articles:
            link_el = article.query_selector("a[href*='/status/']")
            if not link_el:
                continue

            href = link_el.get_attribute("href") or ""
            match = re.search(r"/status/(\d+)", href)
            if not match:
                continue

            tweet_id = match.group(1)
            if tweet_id in seen:
                continue

            seen.add(tweet_id)

            time_el = article.query_selector("time")
            published_at = time_el.get_attribute("datetime") if time_el else None

            posts.append(
                {
                    "url": f"https://x.com/{HANDLE}/status/{tweet_id}",
                    "published_at": published_at,
                }
            )

        print(f"raw posts found: {len(posts)}")

        kept = []
        for post in posts:
            if not post["published_at"]:
                continue

            try:
                dt = datetime.fromisoformat(post["published_at"].replace("Z", "+00:00"))
            except Exception:
                continue

            age = NOW - dt
            if MIN_AGE <= age <= MAX_AGE:
                kept.append(post)

        print(f"after age filter (1.5–5 days): {len(kept)}")

        for post in kept[:20]:
            print(f"qualified post: {post['url']} published_at={post['published_at']}")

        if not posts:
            print("verdict: no posts visible to the browser")
        elif not kept:
            print("verdict: posts visible, but none in the 1.5–5 day window")
        else:
            print("verdict: posts visible and qualifying posts found")

        browser.close()

if __name__ == "__main__":
    main()
