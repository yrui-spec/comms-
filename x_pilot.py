import os, re, json, time, requests
from difflib import SequenceMatcher
from datetime import datetime, timezone, timedelta
from playwright.sync_api import sync_playwright

NOTION_TOKEN = os.getenv("NOTION_TOKEN", "").strip()
DATABASE_ID = os.getenv("DATABASE_ID", "").strip()
CONTENT_PULSE_DATABASE_ID = os.getenv("CONTENT_PULSE_DATABASE_ID", "").strip()
X_HANDLE = os.getenv("X_HANDLE", "Mylovanov").strip().lstrip("@")
X_COOKIES_JSON = os.getenv("X_COOKIES_JSON", "").strip()
SAVE_LIMIT = int(os.getenv("SAVE_LIMIT", "10").strip() or "0")
MAX_SCROLLS = int(os.getenv("MAX_SCROLLS", "260").strip() or "260")
START_DATE = os.getenv("START_DATE", "2026-07-27").strip()
END_DATE = os.getenv("END_DATE", "2026-08-01").strip()
NOTION_VERSION = "2022-06-28"


def clean_id(value):
    value = (value or "").strip()
    m = re.findall(r"[0-9a-fA-F]{32}", value.replace("-", ""))
    return m[-1] if m else value.replace("-", "")

DATABASE_ID = clean_id(DATABASE_ID)
CONTENT_PULSE_DATABASE_ID = clean_id(CONTENT_PULSE_DATABASE_ID)
START_DT = datetime.fromisoformat(START_DATE).replace(tzinfo=timezone.utc)
END_DT = datetime.fromisoformat(END_DATE).replace(tzinfo=timezone.utc) + timedelta(days=1)


def notion_headers():
    return {"Authorization": f"Bearer {NOTION_TOKEN}", "Notion-Version": NOTION_VERSION, "Content-Type": "application/json"}


def normalize_text(s):
    s = (s or "").lower()
    s = re.sub(r"https?://\S+", " ", s)
    s = re.sub(r"[^\w\sа-яіїєґ'-]", " ", s, flags=re.I)
    return re.sub(r"\s+", " ", s).strip()[:800]


def similarity(a, b):
    a, b = normalize_text(a), normalize_text(b)
    return SequenceMatcher(None, a, b).ratio() if a and b else 0


def normalize_number(s):
    s = s.strip().replace(" ", "").replace(",", ".")
    mult = 1
    if s[-1:].upper() in ["K", "К"]: mult, s = 1000, s[:-1]
    elif s[-1:].upper() in ["M", "М"]: mult, s = 1000000, s[:-1]
    elif s[-1:].upper() in ["B", "Б"]: mult, s = 1000000000, s[:-1]
    try: return int(float(s) * mult)
    except Exception: return None


def parse_views(text):
    if not text: return None
    text = text.replace("\u202f", " ").replace("\xa0", " ")
    pats = [
        r"([0-9]+(?:[.,][0-9]+)?\s*[KMBКМБ]?)\s+(?:Views|views|перегляд|перегляди|переглядів)",
        r"(?:Views|views|перегляди|переглядів)\s*[:·]?\s*([0-9]+(?:[.,][0-9]+)?\s*[KMBКМБ]?)",
    ]
    for p in pats:
        m = re.search(p, text)
        if m: return normalize_number(m.group(1))
    return None


def prop_payload(props, name, value):
    if name not in props or value is None: return None
    typ = props[name]["type"]
    if typ == "title": return {"title": [{"text": {"content": str(value)[:2000]}}]}
    if typ == "rich_text": return {"rich_text": [{"text": {"content": str(value)[:2000]}}]}
    if typ == "number": return {"number": int(value)}
    if typ == "url": return {"url": str(value)}
    if typ == "date": return {"date": {"start": str(value)}}
    if typ == "select": return {"select": {"name": str(value)}}
    if typ == "status": return {"status": {"name": str(value)}}
    return None


def load_database_props(db_id):
    r = requests.get(f"https://api.notion.com/v1/databases/{db_id}", headers=notion_headers(), timeout=30)
    if r.status_code != 200:
        print(f"notion database load error {r.status_code}: {r.text}"); return {}
    props = r.json().get("properties", {})
    print("loaded database properties:", ", ".join(props.keys()))
    return props


def rich_text_to_plain(prop):
    typ = prop.get("type") if prop else None
    if typ == "title": return "".join(x.get("plain_text", "") for x in prop.get("title", []))
    if typ == "rich_text": return "".join(x.get("plain_text", "") for x in prop.get("rich_text", []))
    return ""


def load_content_pulse_items():
    if not CONTENT_PULSE_DATABASE_ID:
        print("no CONTENT_PULSE_DATABASE_ID, skip matching"); return []
    r = requests.post(f"https://api.notion.com/v1/databases/{CONTENT_PULSE_DATABASE_ID}/query", headers=notion_headers(), json={"page_size": 100}, timeout=30)
    if r.status_code != 200:
        print(f"content pulse query error {r.status_code}: {r.text}"); return []
    items = []
    for page in r.json().get("results", []):
        chunks = [rich_text_to_plain(p) for p in page.get("properties", {}).values() if p.get("type") in ["title", "rich_text"]]
        text = "\n".join(chunks)
        if text.strip(): items.append({"id": page["id"], "text": text})
    print(f"loaded content pulse candidates: {len(items)}")
    return items


def find_content_match(post_text, candidates):
    best = (0, None)
    for c in candidates:
        score = similarity(post_text, c["text"])
        if score > best[0]: best = (score, c)
    if best[0] >= 0.55:
        print(f"content pulse match score={best[0]:.2f}"); return best[1]["id"]
    print(f"no content pulse match, best_score={best[0]:.2f}"); return None


def save_to_notion(item, props, candidates):
    properties = {}
    mapping = {"Post": f"X post {item['id']}", "Channel": "X", "Published at": item["published_at"], "Metric type": "Views", "Metric": item["views"], "Post URL": item["url"], "Run status": "Views captured"}
    for k, v in mapping.items():
        payload = prop_payload(props, k, v)
        if payload: properties[k] = payload
    match_id = find_content_match(item.get("text", ""), candidates)
    if match_id and props.get("Content Pulse Item", {}).get("type") == "relation":
        properties["Content Pulse Item"] = {"relation": [{"id": match_id}]}
    r = requests.post("https://api.notion.com/v1/pages", headers=notion_headers(), json={"parent": {"database_id": DATABASE_ID}, "properties": properties}, timeout=30)
    if r.status_code not in (200, 201):
        print(f"notion error {r.status_code}: {r.text}"); return False
    print(f"saved to Notion: {item['id']} {item['published_at']} views={item['views']}"); return True


def add_cookies(context):
    if not X_COOKIES_JSON:
        print("no cookies provided"); return
    cookies = json.loads(X_COOKIES_JSON)
    if isinstance(cookies, dict): cookies = cookies.get("cookies", [])
    for c in cookies:
        c.setdefault("domain", ".x.com"); c.setdefault("path", "/")
        if c.get("sameSite") not in [None, "Strict", "Lax", "None"]: c.pop("sameSite", None)
    context.add_cookies(cookies)
    print(f"loaded cookies: {len(cookies)}")


def is_reply_or_thread_child(article, text):
    low = text.lower()
    markers = ["replying to", "у відповідь", "в ответ", "show this thread", "показати цю гілку", "показать эту ветку"]
    if any(m in low for m in markers): return True
    try:
        # Replies/thread children usually have socialContext/user-name text above the tweet body.
        social = article.locator("[data-testid='socialContext']").count()
        if social > 0: return True
    except Exception:
        pass
    return False


def extract_post(article):
    try:
        text = article.inner_text(timeout=3000)
        if is_reply_or_thread_child(article, text): return None

        # Use links that are inside THIS article only. Pick the last status link, usually canonical tweet link.
        links = article.locator("a[href*='/status/']").evaluate_all("els => els.map(a => a.href)")
        ids = []
        for href in links:
            m = re.search(r"/status/(\d+)", href)
            if m and f"/{X_HANDLE}/status/" in href:
                ids.append(m.group(1))
        if not ids: return None
        post_id = ids[-1]

        times = article.locator("time").evaluate_all("els => els.map(t => t.getAttribute('datetime'))")
        if not times: return None
        published_at = times[-1]
        dt = datetime.fromisoformat(published_at.replace("Z", "+00:00"))

        # Hard date gate inside extraction, so Aug 2 cannot pass.
        if not (START_DT <= dt < END_DT): return None

        aria = article.evaluate("""el => Array.from(el.querySelectorAll('[aria-label]')).map(x => x.getAttribute('aria-label')).filter(Boolean).join(' | ')""")
        views = parse_views(text) or parse_views(aria)
        if views is None or views > 20000000: return None
        return {"id": post_id, "published_at": published_at, "dt": dt, "views": views, "url": f"https://x.com/{X_HANDLE}/status/{post_id}", "text": text}
    except Exception as e:
        print("extract error:", repr(e)); return None


def main():
    if not re.fullmatch(r"[0-9a-fA-F]{32}", DATABASE_ID): raise RuntimeError(f"Bad DATABASE_ID: {DATABASE_ID!r}")
    print("CODE_VERSION: strict_dates_no_replies_v3")
    print(f"opening https://x.com/{X_HANDLE}")
    print(f"STRICT_DATE_RANGE: {START_DATE} 00:00 UTC through {END_DATE} 23:59 UTC")
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
        no_new = 0
        for scroll in range(MAX_SCROLLS):
            articles = page.locator("article")
            count, new_this = articles.count(), 0
            for i in range(count):
                item = extract_post(articles.nth(i))
                if item and item["id"] not in seen:
                    seen[item["id"]] = item; new_this += 1
                    print(f"qualified found: {item['id']} {item['published_at']} views={item['views']}")
            print(f"scroll {scroll}: article nodes={count}, qualified_raw={len(seen)}, new_this_scroll={new_this}")
            no_new = no_new + 1 if new_this == 0 else 0
            if no_new >= 25: break
            page.mouse.wheel(0, 3500); time.sleep(2)
        page.screenshot(path="x_debug_after_scroll.png", full_page=True)
        browser.close()
    qualified = sorted(seen.values(), key=lambda x: x["dt"], reverse=True)
    print(f"FINAL qualified posts in strict date range ({START_DATE}..{END_DATE}): {len(qualified)}")
    for q in qualified[:30]: print(f"QUALIFIED_CHECK: {q['id']} {q['published_at']} views={q['views']}")
    if SAVE_LIMIT > 0:
        qualified = qualified[:SAVE_LIMIT]
        print(f"limited qualified for test: {len(qualified)}")
    props = load_database_props(DATABASE_ID)
    candidates = load_content_pulse_items()
    saved = sum(1 for item in qualified if save_to_notion(item, props, candidates))
    print(f"saved={saved}, attempted={len(qualified)}")
    print("verdict: saved to Notion" if saved else "verdict: posts found, but none saved to Notion")

if __name__ == "__main__": main()
