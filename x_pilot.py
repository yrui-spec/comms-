"""
X metrics collector — fixed date-range version.

Goal:
- Open @<handle>'s X profile as a logged-in browser using X_COOKIES_JSON.
- Scroll until it reaches posts older than START_DATE.
- Save ONLY posts published from START_DATE through END_DATE, inclusive.
- Write them to Notion.

Default date range for this run:
- 2026-07-27 through 2026-08-01

GitHub Secrets expected:
- NOTION_TOKEN
- DATABASE_ID
- X_COOKIES_JSON

GitHub env optional:
- X_HANDLE, START_DATE, END_DATE
"""

import json
import os
import re
import urllib.error
import urllib.request
from datetime import datetime, date, time, timezone
from typing import Dict, List, Optional

from playwright.sync_api import sync_playwright


HANDLE = os.environ.get("X_HANDLE", "Mylovanov")
NOTION_TOKEN = os.environ["NOTION_TOKEN"]
DATABASE_ID = os.environ["DATABASE_ID"]
X_COOKIES_JSON = os.environ.get("X_COOKIES_JSON", "")

START_DATE_TEXT = os.environ.get("START_DATE", "2026-07-27")
END_DATE_TEXT = os.environ.get("END_DATE", "2026-08-01")

START_DATE = date.fromisoformat(START_DATE_TEXT)
END_DATE = date.fromisoformat(END_DATE_TEXT)

START_DT = datetime.combine(START_DATE, time.min, tzinfo=timezone.utc)
END_DT = datetime.combine(END_DATE, time.max, tzinfo=timezone.utc)

MAX_SCROLLS = int(os.environ.get("MAX_SCROLLS", "180"))
SCROLL_PAUSE_MS = int(os.environ.get("SCROLL_PAUSE_MS", "1800"))
MAX_ROWS = int(os.environ.get("MAX_ROWS", "500"))


PROPERTY_CANDIDATES = {
    "title": ["Post", "Name", "title"],
    "channel": ["Channel"],
    "post_url": ["Post URL", "URL", "Url"],
    "published_at": ["Published at", "Date"],
    "metric": ["Metric", "Views", "views"],
    "run_status": ["Run status", "Status"],
}


def parse_number(text: str) -> Optional[int]:
    if not text:
        return None

    cleaned = text.strip().replace(",", "").replace(" ", "")
    match = re.match(r"^(\d+(?:\.\d+)?)([KMB]?)$", cleaned, re.IGNORECASE)
    if not match:
        return None

    number = float(match.group(1))
    suffix = match.group(2).upper()
    multiplier = {
        "": 1,
        "K": 1_000,
        "M": 1_000_000,
        "B": 1_000_000_000,
    }[suffix]
    return int(number * multiplier)


def extract_views_from_text(text: str) -> Optional[int]:
    if not text:
        return None

    candidates = re.findall(r"\b\d+(?:[.,]\d+)?[KMB]?\b", text, flags=re.IGNORECASE)
    values = []
    for candidate in candidates:
        value = parse_number(candidate.replace(",", "."))
        if value is not None:
            values.append(value)

    if not values:
        return None

    # Heuristic for profile cards: views are usually the largest compact number.
    return max(values)


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


def in_target_range(published_dt: datetime) -> bool:
    return START_DT <= published_dt <= END_DT


def is_older_than_target(published_dt: datetime) -> bool:
    return published_dt < START_DT


def collect_posts() -> List[Dict]:
    posts_by_id: Dict[str, Dict] = {}
    oldest_seen: Optional[datetime] = None
    consecutive_no_new = 0

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
        print(f"target date range: {START_DATE_TEXT} through {END_DATE_TEXT}")
        print(f"max scrolls: {MAX_SCROLLS}")

        page.goto(url, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(8000)
        page.screenshot(path="x_debug_home.png", full_page=True)
        print("saved screenshot: x_debug_home.png")

        for scroll_index in range(MAX_SCROLLS):
            before_count = len(posts_by_id)
            articles = page.query_selector_all("article")
            print(f"scroll {scroll_index}: article nodes={len(articles)}")

            for article in articles:
                try:
                    text = article.inner_text()
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
                    views = extract_views_from_text(text)

                    posts_by_id[tweet_id] = {
                        "tweet_id": tweet_id,
                        "url": f"https://x.com/{HANDLE}/status/{tweet_id}",
                        "published_at": published_at,
                        "published_dt": published_dt,
                        "views": views,
                        "text": text[:240].replace("\n", " "),
                    }

                    if published_dt and (oldest_seen is None or published_dt < oldest_seen):
                        oldest_seen = published_dt

                    print(
                        f"found post: {tweet_id} "
                        f"published_at={published_at} views={views}"
                    )

                except Exception as error:
                    print(f"skip article: {error}")

            after_count = len(posts_by_id)
            if after_count == before_count:
                consecutive_no_new += 1
            else:
                consecutive_no_new = 0

            target_count = sum(
                1
                for post in posts_by_id.values()
                if post["published_dt"] and in_target_range(post["published_dt"])
            )
            oldest_text = oldest_seen.isoformat() if oldest_seen else "none"
            print(
                f"progress: raw={after_count}, target_range_found={target_count}, "
                f"oldest_seen={oldest_text}"
            )

            # Stop once we have definitely scrolled past the requested date range.
            if oldest_seen and is_older_than_target(oldest_seen):
                print("stop: reached posts older than START_DATE")
                break

            # Safety stop if X stops loading new posts.
            if consecutive_no_new >= 8:
                print("stop: no new posts after repeated scrolls")
                break

            page.mouse.wheel(0, 3000)
            page.wait_for_timeout(SCROLL_PAUSE_MS)

        page.screenshot(path="x_debug_after_scroll.png", full_page=True)
        print("saved screenshot: x_debug_after_scroll.png")
        browser.close()

    posts = list(posts_by_id.values())
    posts.sort(
        key=lambda item: item["published_dt"] or datetime.min.replace(tzinfo=timezone.utc),
        reverse=True,
    )
    return posts


def filter_posts(posts: List[Dict]) -> List[Dict]:
    kept = []
    for post in posts:
        published_dt = post.get("published_dt")
        if published_dt and in_target_range(published_dt):
            kept.append(post)

    kept.sort(key=lambda item: item["published_dt"], reverse=True)
    return kept[:MAX_ROWS]


def notion_request(path: str, method: str = "GET", payload: Optional[Dict] = None) -> Dict:
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    request = urllib.request.Request(
        f"https://api.notion.com{path}",
        data=data,
        method=method,
        headers={
            "Authorization": f"Bearer {NOTION_TOKEN}",
            "Notion-Version": "2022-06-28",
            "Content-Type": "application/json",
        },
    )

    with urllib.request.urlopen(request, timeout=30) as response:
        body = response.read().decode("utf-8")
        return json.loads(body) if body else {}


def load_database_properties() -> Dict[str, Dict]:
    database = notion_request(f"/v1/databases/{DATABASE_ID}")
    return database.get("properties", {})


def first_existing_property(properties: Dict[str, Dict], names: List[str]) -> Optional[str]:
    for name in names:
        if name in properties:
            return name
    return None


def build_notion_properties(post: Dict, db_properties: Dict[str, Dict]) -> Dict:
    result = {}

    title_prop = first_existing_property(db_properties, PROPERTY_CANDIDATES["title"])
    if not title_prop:
        # Fallback: find the actual title property even if it has a custom name.
        for name, definition in db_properties.items():
            if definition.get("type") == "title":
                title_prop = name
                break

    if not title_prop:
        raise RuntimeError("No title property found in Notion database")

    result[title_prop] = {
        "title": [
            {
                "text": {
                    "content": f"@{HANDLE} · {post['tweet_id']}",
                }
            }
        ]
    }

    channel_prop = first_existing_property(db_properties, PROPERTY_CANDIDATES["channel"])
    if channel_prop:
        channel_type = db_properties[channel_prop].get("type")
        if channel_type == "select":
            result[channel_prop] = {"select": {"name": "X"}}
        elif channel_type == "multi_select":
            result[channel_prop] = {"multi_select": [{"name": "X"}]}

    url_prop = first_existing_property(db_properties, PROPERTY_CANDIDATES["post_url"])
    if url_prop and db_properties[url_prop].get("type") == "url":
        result[url_prop] = {"url": post["url"]}

    date_prop = first_existing_property(db_properties, PROPERTY_CANDIDATES["published_at"])
    if date_prop and db_properties[date_prop].get("type") == "date":
        result[date_prop] = {"date": {"start": post["published_at"]}}

    metric_prop = first_existing_property(db_properties, PROPERTY_CANDIDATES["metric"])
    if metric_prop and db_properties[metric_prop].get("type") == "number" and post["views"] is not None:
        result[metric_prop] = {"number": post["views"]}

    status_prop = first_existing_property(db_properties, PROPERTY_CANDIDATES["run_status"])
    if status_prop:
        status_type = db_properties[status_prop].get("type")
        status_name = "Views captured" if post["views"] is not None else "No views"
        if status_type == "select":
            result[status_prop] = {"select": {"name": status_name}}
        elif status_type == "status":
            # Only write this if the status option exists. Otherwise skip it.
            options = []
            groups = db_properties[status_prop].get("status", {}).get("groups", [])
            for group in groups:
                options.extend(group.get("option_ids", []))
            # Notion API does not expose status names cleanly in all cases here,
            # so avoid forcing a non-existing status into Content Pulse.
            pass

    return result


def notion_create(post: Dict, db_properties: Dict[str, Dict]) -> bool:
    properties = build_notion_properties(post, db_properties)
    payload = {
        "parent": {"database_id": DATABASE_ID},
        "properties": properties,
    }

    try:
        notion_request("/v1/pages", method="POST", payload=payload)
        return True
    except urllib.error.HTTPError as error:
        print(f"notion error {error.code}: {error.read().decode(errors='replace')}")
        return False
    except Exception as error:
        print(f"notion error: {error}")
        return False


def main() -> None:
    posts = collect_posts()
    print(f"raw posts: {len(posts)}")

    kept = filter_posts(posts)
    print(f"posts in requested date range ({START_DATE_TEXT}..{END_DATE_TEXT}): {len(kept)}")

    if kept:
        print("first qualifying posts:")
        for post in kept[:10]:
            print(f"qualified: {post['tweet_id']} {post['published_at']} views={post['views']}")

    db_properties = load_database_properties()
    print(f"loaded Notion database properties: {', '.join(db_properties.keys())}")

    saved = 0
    no_views = 0
    for post in kept:
        if notion_create(post, db_properties):
            saved += 1
            if post["views"] is None:
                no_views += 1

    print(f"saved={saved}, no_views={no_views}")

    if saved == 0 and len(kept) == 0:
        print("verdict: no posts found in requested date range")
    elif saved == 0:
        print("verdict: posts found, but none saved to Notion")
    elif no_views == saved:
        print("verdict: posts saved, but views were not captured")
    else:
        print("verdict: OK — requested date range saved with views")


if __name__ == "__main__":
    main()
