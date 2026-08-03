#!/usr/bin/env python3
"""
X analytics collector — Phase 1.1

What it does (once per run):
  1. Resolves a target day (default: today − 3 days, Kyiv time).
  2. Opens an X search for @HANDLE posts from that exact day (replies excluded).
  3. For every post: extracts URL, Post ID, timestamp, text and views.
  4. Upserts a row in the Post Outputs Notion DB (by Post ID — no duplicates).
  5. Freezes the metric (views) once; never overwrites a frozen metric.
  6. Matches the post to a Content Pulse draft by PAGE-BODY text (fuzzy).
  7. Logs the whole run into the Collection Runs DB.

All config comes from environment variables / GitHub Secrets.
"""

import os
import re
import sys
import json
import difflib
from datetime import datetime, timedelta
from urllib.parse import quote
from zoneinfo import ZoneInfo

import requests
from playwright.sync_api import sync_playwright

# ----------------------------- Config ---------------------------------------
KYIV = ZoneInfo("Europe/Kyiv")
NOTION_API = "https://api.notion.com/v1"
NOTION_VERSION = "2022-06-28"

TOKEN = (os.environ.get("NOTION_TOKEN") or "").strip()
POST_OUTPUTS_DB = (os.environ.get("DATABASE_ID") or "").strip()
CONTENT_PULSE_DB = (os.environ.get("CONTENT_PULSE_DATABASE_ID") or "").strip()
RUNS_DB = (os.environ.get("COLLECTION_RUNS_DATABASE_ID") or "").strip()

X_HANDLE = (os.environ.get("X_HANDLE") or "Mylovanov").strip().lstrip("@")
COOKIES_RAW = os.environ.get("X_COOKIES_JSON") or "[]"

LOOKBACK_DAYS = int(os.environ.get("LOOKBACK_DAYS", "3"))
MATCH_THRESHOLD = float(os.environ.get("MATCH_THRESHOLD", "0.6"))
MAX_SCROLLS = int(os.environ.get("MAX_SCROLLS", "80"))
TARGET_DATE_OVERRIDE = (os.environ.get("TARGET_DATE") or "").strip()

# Content Pulse statuses we DO NOT match against.
EXCLUDE_STATUSES = {
    "Old idea", "Idea", "Morning Idea", "Done",
    "Scheduled", "Sent back", "In progress", "Тимофій апрувить",
}


def clean_id(v: str) -> str:
    """Keep only hex chars — tolerates stray newlines/spaces in secrets."""
    return re.sub(r"[^0-9a-fA-F]", "", v or "")


def headers() -> dict:
    return {
        "Authorization": f"Bearer {TOKEN}",
        "Notion-Version": NOTION_VERSION,
        "Content-Type": "application/json",
    }


def resolve_target_date():
    if TARGET_DATE_OVERRIDE:
        return datetime.strptime(TARGET_DATE_OVERRIDE, "%Y-%m-%d").date()
    return datetime.now(KYIV).date() - timedelta(days=LOOKBACK_DAYS)


# ----------------------------- Scraping -------------------------------------
def build_search_url(day) -> str:
    since = day.isoformat()
    until = (day + timedelta(days=1)).isoformat()
    q = f"from:{X_HANDLE} since:{since} until:{until} -filter:replies"
    return "https://x.com/search?q=" + quote(q) + "&src=typed_query&f=live"


def _num_from(s):
    s = (s or "").replace(",", "").strip()
    m = re.search(r"([\d.]+)\s*([KMkm]?)", s)
    if not m:
        return None
    val = float(m.group(1))
    suf = m.group(2).upper()
    if suf == "K":
        val *= 1_000
    elif suf == "M":
        val *= 1_000_000
    return int(val)


def _parse_views(article):
    el = article.query_selector("a[href$='/analytics']")
    if el:
        label = el.get_attribute("aria-label") or el.inner_text() or ""
        n = _num_from(re.sub(r"views?", "", label, flags=re.I))
        if n is not None:
            return n
    grp = article.query_selector("div[role='group']")
    if grp:
        label = grp.get_attribute("aria-label") or ""
        m = re.search(r"([\d.,KMkm]+)\s+views", label)
        if m:
            return _num_from(m.group(1))
    return None


def _extract_article(article):
    try:
        pid, url = None, None
        for el in article.query_selector_all("a[href*='/status/']"):
            href = el.get_attribute("href") or ""
            m = re.search(r"/status/(\d+)", href)
            if m:
                pid = m.group(1)
                url = href.split("?")[0]
                if url.startswith("/"):
                    url = "https://x.com" + url
                break
        if not pid:
            return None
        t = article.query_selector("time")
        ts = t.get_attribute("datetime") if t else None
        text_el = article.query_selector("div[data-testid='tweetText']")
        text = (text_el.inner_text() if text_el else "").strip()
        social = article.query_selector("div[data-testid='socialContext']")
        is_repost = bool(social and "repost" in ((social.inner_text() or "").lower()))
        return {
            "post_id": pid,
            "url": url,
            "published_at": ts,
            "text": text,
            "views": _parse_views(article),
            "is_repost": is_repost,
        }
    except Exception:
        return None


def scrape_posts(day):
    os.makedirs("debug", exist_ok=True)
    cookies = json.loads(COOKIES_RAW)
    norm = []
    for c in cookies:
        nc = {
            "name": c.get("name"),
            "value": c.get("value"),
            "domain": c.get("domain", ".x.com"),
            "path": c.get("path", "/"),
        }
        exp = c.get("expires") or c.get("expirationDate")
        if isinstance(exp, (int, float)):
            nc["expires"] = int(exp)
        norm.append(nc)

    results = {}
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=["--no-sandbox"])
        ctx = browser.new_context(
            locale="en-US",
            viewport={"width": 1280, "height": 2000},
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0 Safari/537.36"
            ),
        )
        ctx.add_cookies(norm)
        page = ctx.new_page()
        page.goto(build_search_url(day), timeout=60_000)
        page.wait_for_timeout(5_000)
        page.screenshot(path="debug/search_top.png")

        last, stable = -1, 0
        for _ in range(MAX_SCROLLS):
            for a in page.query_selector_all("article"):
                data = _extract_article(a)
                if data:
                    results[data["post_id"]] = data
            if len(results) == last:
                stable += 1
                if stable >= 4:
                    break
            else:
                stable = 0
            last = len(results)
            page.mouse.wheel(0, 4_000)
            page.wait_for_timeout(1_500)

        page.screenshot(path="debug/search_bottom.png")
        browser.close()
    return list(results.values())


# ----------------------------- Notion I/O -----------------------------------
def find_existing(post_id):
    r = requests.post(
        f"{NOTION_API}/databases/{clean_id(POST_OUTPUTS_DB)}/query",
        headers=headers(),
        json={"filter": {"property": "Post ID",
                          "rich_text": {"equals": post_id}}, "page_size": 1},
    )
    r.raise_for_status()
    res = r.json().get("results", [])
    return res[0] if res else None


def build_props(post, match):
    title = post["text"][:120] if post["text"] else f"X post {post['post_id']}"
    props = {
        "Post": {"title": [{"text": {"content": title}}]},
        "Post ID": {"rich_text": [{"text": {"content": post["post_id"]}}]},
        "Post URL": {"url": post["url"]},
        "Channel": {"select": {"name": "X"}},
        "Collected at": {"date": {"start": datetime.now(KYIV).isoformat()}},
        "Run status": {"select": {"name":
            "Views captured" if post.get("views") is not None else "No views"}},
    }
    if post.get("published_at"):
        props["Published at"] = {"date": {"start": post["published_at"]}}
    if post.get("views") is not None:
        props["Metric"] = {"number": post["views"]}
        props["Metric frozen"] = {"checkbox": True}
    if match:
        props["Content Pulse Item"] = {"relation": [{"id": match["id"]}]}
        props["Match status"] = {"select": {"name": "✅ Matched"}}
    else:
        props["Match status"] = {"select": {"name": "⚠️ Needs review"}}
    return props


def create_row(props, unmatched):
    payload = {"parent": {"database_id": clean_id(POST_OUTPUTS_DB)}, "properties": props}
    if unmatched:
        payload["icon"] = {"type": "emoji", "emoji": "🔴"}
    r = requests.post(f"{NOTION_API}/pages", headers=headers(), json=payload)
    r.raise_for_status()


def update_row(page, props, unmatched):
    # Respect a frozen metric: never overwrite it.
    frozen = page["properties"].get("Metric frozen", {}).get("checkbox", False)
    if frozen:
        props.pop("Metric", None)
        props.pop("Metric frozen", None)
    payload = {"properties": props}
    if unmatched:
        payload["icon"] = {"type": "emoji", "emoji": "🔴"}
    r = requests.patch(f"{NOTION_API}/pages/{page['id']}", headers=headers(), json=payload)
    r.raise_for_status()
    return frozen


def content_pulse_candidates(day):
    start = (day - timedelta(days=3)).isoformat()
    end = (day + timedelta(days=3)).isoformat()
    flt = {"and": [
        {"property": "Channel", "multi_select": {"contains": "X"}},
        {"property": "Date", "date": {"on_or_after": start}},
        {"property": "Date", "date": {"on_or_before": end}},
    ]}
    out, cursor = [], None
    while True:
        body = {"filter": flt, "page_size": 100}
        if cursor:
            body["start_cursor"] = cursor
        r = requests.post(
            f"{NOTION_API}/databases/{clean_id(CONTENT_PULSE_DB)}/query",
            headers=headers(), json=body)
        r.raise_for_status()
        j = r.json()
        for pg in j.get("results", []):
            st = (pg["properties"].get("Status", {}) or {}).get("status") or {}
            if st.get("name") in EXCLUDE_STATUSES:
                continue
            out.append(pg)
        if j.get("has_more"):
            cursor = j.get("next_cursor")
        else:
            break
    return out


def page_body_text(page_id):
    parts, cursor = [], None
    while True:
        url = f"{NOTION_API}/blocks/{clean_id(page_id)}/children?page_size=100"
        if cursor:
            url += f"&start_cursor={cursor}"
        r = requests.get(url, headers=headers())
        if r.status_code != 200:
            break
        j = r.json()
        for b in j.get("results", []):
            payload = b.get(b.get("type"), {})
            if isinstance(payload, dict):
                for rt in payload.get("rich_text", []):
                    parts.append(rt.get("plain_text", ""))
        if j.get("has_more"):
            cursor = j.get("next_cursor")
        else:
            break
    return "\n".join(parts)


def _normalize(s):
    s = re.sub(r"https?://\S+", "", s or "")
    s = re.sub(r"\s+", " ", s).lower().strip()
    return s


def similarity(tweet, body):
    tw, bd = _normalize(tweet), _normalize(body)
    if not tw or not bd:
        return 0.0
    ratio = difflib.SequenceMatcher(None, tw, bd).ratio()
    if tw in bd:
        return 1.0
    m = difflib.SequenceMatcher(None, tw, bd).find_longest_match(0, len(tw), 0, len(bd))
    contain = m.size / max(len(tw), 1)
    return max(ratio, contain)


def log_run(day, stats, status, notes):
    if not RUNS_DB:
        return
    props = {
        "Run": {"title": [{"text": {"content": f"X · {day.isoformat()}"}}]},
        "Run at": {"date": {"start": datetime.now(KYIV).isoformat()}},
        "Target date": {"date": {"start": day.isoformat()}},
        "Status": {"select": {"name": status}},
        "Posts found": {"number": stats["found"]},
        "Rows created": {"number": stats["created"]},
        "Rows updated": {"number": stats["updated"]},
        "Metrics frozen": {"number": stats["frozen"]},
        "Matched": {"number": stats["matched"]},
        "Needs review": {"number": stats["review"]},
    }
    if notes:
        props["Errors / notes"] = {"rich_text": [{"text": {"content": notes[:1900]}}]}
    try:
        requests.post(f"{NOTION_API}/pages", headers=headers(),
                      json={"parent": {"database_id": clean_id(RUNS_DB)},
                            "properties": props})
    except Exception as e:
        print("Could not log run:", e)


# ----------------------------- Main -----------------------------------------
def main():
    if not TOKEN or not POST_OUTPUTS_DB:
        print("Missing NOTION_TOKEN or DATABASE_ID")
        sys.exit(1)

    day = resolve_target_date()
    print(f"Target date (Kyiv): {day}")
    stats = {"found": 0, "created": 0, "updated": 0,
             "frozen": 0, "matched": 0, "review": 0}
    notes = []
    status = "✅ OK"

    try:
        posts = scrape_posts(day)
    except Exception as e:
        log_run(day, stats, "❌ Failed", f"Scrape error: {e}")
        print("Scrape failed:", e)
        sys.exit(1)

    stats["found"] = len(posts)
    print(f"Found {len(posts)} posts")
    if not posts:
        log_run(day, stats, "❌ Failed",
                "0 posts found — cookies may be expired or the day is empty.")
        sys.exit(1)

    # Preload Content Pulse candidate bodies once.
    cand_bodies = []
    try:
        if CONTENT_PULSE_DB:
            for pg in content_pulse_candidates(day):
                cand_bodies.append((pg, page_body_text(pg["id"])))
    except Exception as e:
        notes.append(f"Content Pulse query error: {e}")
        status = "⚠️ Warning"

    for post in posts:
        try:
            match, best = None, 0.0
            if cand_bodies and post["text"]:
                for pg, body in cand_bodies:
                    sc = similarity(post["text"], body)
                    if sc > best:
                        best, match = sc, pg
                if best < MATCH_THRESHOLD:
                    match = None
            unmatched = match is None
            props = build_props(post, match)
            existing = find_existing(post["post_id"])
            if existing:
                was_frozen = update_row(existing, props, unmatched)
                stats["updated"] += 1
                if post.get("views") is not None and not was_frozen:
                    stats["frozen"] += 1
            else:
                create_row(props, unmatched)
                stats["created"] += 1
                if post.get("views") is not None:
                    stats["frozen"] += 1
            if match:
                stats["matched"] += 1
            else:
                stats["review"] += 1
        except requests.HTTPError as e:
            notes.append(f"{post['post_id']}: {e.response.text[:200]}")
            status = "⚠️ Warning"
        except Exception as e:
            notes.append(f"{post['post_id']}: {e}")
            status = "⚠️ Warning"

    log_run(day, stats, status, " | ".join(notes))
    print("Done:", stats)


if __name__ == "__main__":
    main()
