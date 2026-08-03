import os
import re
import json
import time
import requests
from datetime import datetime, timezone, timedelta
from playwright.sync_api import sync_playwright

NOTION_TOKEN = os.getenv("NOTION_TOKEN", "").strip()
DATABASE_ID = os.getenv("DATABASE_ID", "").strip().replace("-", "")
X_HANDLE = os.getenv("X_HANDLE", "Mylovanov").strip().lstrip("@")
X_COOKIES_JSON = os.getenv("X_COOKIES_JSON", "").strip()

# Test safety: set SAVE_LIMIT=10 in workflow while testing. Empty/0 = no limit.
SAVE_LIMIT = int(os.getenv("SAVE_LIMIT", "10").strip() or "0")
MAX_SCROLLS = int(os.getenv("MAX_SCROLLS", "220").strip() or "220")

# Collect posts older than 36h, but not older than 7 days.
NOW = datetime.now(timezone.utc)
TARGET_END = NOW - timedelta(hours=36)
TARGET_START = NOW - timedelta(days=7)

NOTION_VERSION = "2022-06-28"


def clean_id(value: str) -> str:
    value = (value or "").strip()
    # If user pasted Notion URL, extract 32-char UUID-ish tail.
    m = re.findall(r"[0-9a-fA-F]{32}", value.replace("-", ""))
    if m:
        return m[-1]
    return value.replace("-", "")

DATABASE_ID = clean_id(DATABASE_ID)


def parse_views(text: str):
    if not text:
        return None
    text = text.replace("\u202f", " ").replace("\xa0", " ")
    patterns = [
        r"([0-9]+(?:[.,][0-9]+)?\s*[KMBКМБ]?)\s+(?:Views|views|перегляд|перегляди|переглядів)",
        r"(?:Views|views|перегляди|переглядів)\s*[:·]?\s*([0-9]+(?:[.,][0-9]+)?\s*[KMBКМБ]?)",
    ]
    for p in patterns:
        m = re.search(p, text)
        if m:
            return normalize_number(m.group(1))
    return None


def normalize_number(s: str):
    s = s.strip().replace(" ", "").replace(",", ".")
    mult = 1
    if s[-1:].upper() in ["K", "К"]:
        mult = 1_000
        s = s[:-1]
    elif s[-1:].upper() in ["M", "М"]:
        mult = 1_000_000
        s = s[:-1]
    elif s[-1:].upper() in ["B", "Б"]:
        mult = 1_000_000_000
        s = s[:-1]
    try:
        return int(float(s) * mult)
    except Exception:
        return None


def post_url(post_id: str):
    return f"https://x.com/{X_HANDLE}/status/{post_id}"


def notion_headers():
    return {
        "Authorization": f"Bearer {NOTION_TOKEN}",
        "Notion-Version": NOTION_VERSION,
        "Content-Type": "application/json",
    }


def load_database_props():
    url = f"https://api.notion.com/v1/databases/{DATABASE_ID}"
    r = requests.get(url, headers=notion_headers(), timeout=30)
    if r.status_code != 200:
        print(f"notion database load error {r.status_code}: {r.text}")
        return {}
    props = r.json().get("properties", {})
    print("loaded Notion database properties:", ", ".join(props.keys()))
    return props


def prop_payload(props, name, value):
    if name not in props or value is None:
        return None
    typ = props[name]["type"]
    if typ == "title":
        return {"title": [{"text": {"content": str(value)[:2000]}}]}
    if typ == "rich_text":
        return {"rich_text": [{"text": {"content": str(value)[:2000]}}]}
    if typ == "number":
        return {"number": int(value)}
    if typ == "url":
        return {"url": str(value)}
    if typ == "date":
        return {"date": {"start": str(value)}}
    if typ == "select":
        return {"select": {"name": str(value)}}
    if typ == "status":
        return {"status": {"name": str(value)}}
    return None


def save_to_notion(item, props):
    properties = {}
    mapping = {
        "Post": f"X post {item['id']}",
        "Channel": "X",
        "Published at": item["published_at"],
        "Metric type": "Views",
        "Metric": item["views"],
        "Post URL": item["url"],
        "Run status": "Views captured",
    }
    for k, v in mapping.items():
        payload = prop_payload(props, k, v)
        if payload:
            properties[k] = payload

    body = {"parent": {"database_id": DATABASE_ID}, "properties": properties}
    r = requests.post("https://api.notion.com/v1/pages", headers=notion_headers(), json=body, timeout=30)
    if r.status_code not in (200, 201):
        print(f"notion error {r.status_code}: {r.text}")
        return False
    print(f"saved to Notion: {item['id']} views={item['views']}")
    return True


def add_cookies(context):
    if not X_COOKIES_JSON:
        print("no cookies provided")
        return
    try:
        cookies = json.loads(X_COOKIES_JSON)
        if isinstance(cookies, dict):
            cookies = cookies.get("cookies", [])
        fixed = []
        for c in cookies:
            if "sameSite" in c and c["sameSite"] not in ["Strict", "Lax", "None"]:
                c.pop("sameSite", None)
            if "domain" not in c:
                c["domain"] = ".x.com"
            if "path" not in c:
                c["path"] = "/"
            fixed.append(c)
        context.add_cookies(fixed)
        print(f"loaded cookies: {len(fixed)}")
    except Exception as e:
        print("cookies error:", repr(e))


def extract_post(article):
    try:
        links = article.locator("a[href*='/status/']").evaluate_all("els => els.map(a => a.href)")
        ids = []
        for href in links:
            m = re.search(r"/status/(\d+)", href)
            if m:
                ids.append(m.group(1))
        if not ids:
            return None
        post_id = ids[0]

        times = article.locator("time").evaluate_all("els => els.map(t => t.getAttribute('datetime'))")
        if not times:
            return None
        published_at = times[0]
        dt = datetime.fromisoformat(published_at.replace("Z", "+00:00"))

        text = article.inner_text(timeout=3000)
        aria = article.evaluate("""el => Array.from(el.querySelectorAll('[aria-label]')).map(x => x.getAttribute('aria-label')).filter(Boolean).join(' | ')""")
        views = parse_views(text) or parse_views(aria)
        if views is None:
            return None
        if views > 20_000_000:  # guard against parser grabbing wrong huge numbers
            print(f"skip suspicious views: {post_id} views={views}")
            return None

        return {"id": post_id, "published_at": published_at, "dt": dt, "views": views, "url": post_url(post_id)}
    except Exception as e:
        print("extract error:", repr(e))
        return None


def main():
    if not NOTION_TOKEN:
        raise RuntimeError("Missing NOTION_TOKEN")
    if not DATABASE_ID or not re.fullmatch(r"[0-9a-fA-F]{32}", DATABASE_ID):
        raise RuntimeError(f"Bad DATABASE_ID after cleanup: {DATABASE_ID!r}")

    print(f"opening https://x.com/{X_HANDLE}")
    print(f"target date range: {TARGET_START.date()} through {TARGET_END.date()}")
    print(f"save limit: {SAVE_LIMIT}")

    seen = {}
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(viewport={"width": 1400, "height": 1800})
        add_cookies(context)
        page = context.new_page()
        page.goto(f"https://x.com/{X_HANDLE}", wait_until="domcontentloaded", timeout=60000)
        time.sleep(5)
        page.screenshot(path="x_debug_home.png", full_page=True)
        print("saved screenshot: x_debug_home.png")

        no_new = 0
        for scroll in range(MAX_SCROLLS):
            articles = page.locator("article")
            count = articles.count()
            new_this = 0
            for i in range(count):
                item = extract_post(articles.nth(i))
                if item and item["id"] not in seen:
                    seen[item["id"]] = item
                    new_this += 1
                    print(f"found post: {item['id']} published_at={item['published_at']} views={item['views']}")
            print(f"scroll {scroll}: article nodes={count}, raw={len(seen)}, new_this_scroll={new_this}")
            if new_this == 0:
                no_new += 1
            else:
                no_new = 0
            if no_new >= 10:
                print("stop: no new posts after repeated scrolls")
                break
            page.mouse.wheel(0, 3500)
            time.sleep(2)

        page.screenshot(path="x_debug_after_scroll.png", full_page=True)
        print("saved screenshot: x_debug_after_scroll.png")
        browser.close()

    all_posts = list(seen.values())
    qualified = [x for x in all_posts if TARGET_START <= x["dt"] <= TARGET_END]
    qualified.sort(key=lambda x: x["dt"], reverse=True)
    print(f"raw posts: {len(all_posts)}")
    print(f"posts in requested date range: {len(qualified)}")
    for q in qualified[:25]:
        print(f"qualified: {q['id']} {q['published_at']} views={q['views']}")

    if SAVE_LIMIT > 0:
        qualified = qualified[:SAVE_LIMIT]
        print(f"limited qualified for test: {len(qualified)}")

    props = load_database_props()
    saved = 0
    for item in qualified:
        if save_to_notion(item, props):
            saved += 1
    print(f"saved={saved}, attempted={len(qualified)}")
    if saved:
        print("verdict: saved to Notion")
    else:
        print("verdict: posts found, but none saved to Notion")


if __name__ == "__main__":
    main()
