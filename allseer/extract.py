"""Fetch a page and pull out readable text. Every failure is survivable: an item keeps
its search snippet and just scores lower on depth."""
import asyncio
import json

from . import db

try:
    import trafilatura
except ImportError:  # snippet-only mode still works
    trafilatura = None

SKIP_HOSTS = ("youtube.com", "youtu.be", "twitter.com", "x.com", "linkedin.com", "facebook.com")
MAX_CHARS = 12000


def _extract_sync(html, url):
    if not trafilatura:
        return "", None, None
    try:
        raw = trafilatura.extract(
            html, url=url, include_comments=False, include_tables=False,
            favor_precision=True, with_metadata=True, output_format="json",
        )
        if not raw:
            return "", None, None
        d = json.loads(raw)
        return (d.get("text") or "")[:MAX_CHARS], d.get("date"), d.get("author")
    except Exception:
        return "", None, None


async def fetch_one(client, item, sem):
    """Adds content / content_chars, and fills published_at + author when the page knows better."""
    url = item.get("url") or ""
    item.setdefault("content", "")
    item.setdefault("content_chars", 0)
    if any(h in (item.get("domain") or "") for h in SKIP_HOSTS):
        item["fetch_note"] = "skipped (JS-only host)"
        return item
    async with sem:
        try:
            r = await client.get(url, follow_redirects=True)
            if r.status_code >= 400:
                item["fetch_note"] = "HTTP " + str(r.status_code)
                return item
            ctype = r.headers.get("content-type", "")
            if "html" not in ctype and "xml" not in ctype and "text" not in ctype:
                item["fetch_note"] = "non-HTML (" + ctype[:40] + ")"
                return item
            text, date, author = await asyncio.to_thread(_extract_sync, r.text, url)
        except Exception as e:
            item["fetch_note"] = type(e).__name__
            return item
    if text:
        item["content"] = text
        item["content_chars"] = len(text)
        if date and not item.get("published_at"):
            item["published_at"] = date
        if author and not item.get("author"):
            item["author"] = author
    else:
        item["fetch_note"] = "no extractable text"
    return item


def _apply_cached(item, row):
    text = row.get("content") or ""
    item["content"] = text
    item["content_chars"] = len(text)
    if row.get("published_at") and not item.get("published_at"):
        item["published_at"] = row["published_at"]
    if row.get("author") and not item.get("author"):
        item["author"] = row["author"]
    item["fetch_note"] = "cached"
    return item


async def fetch_many(client, items, on_event=None, concurrency=8, cache_days=0):
    """Downloads only what is not already in page_cache.

    max_fetch is 40 a topic and at most 8 items get promoted, so nearly every fetch
    budget was being spent re-downloading pages an earlier run had already read. Text
    at a URL does not change on the timescale that matters here.
    """
    hits = db.cached_pages([it.get("canon_url") for it in items], cache_days)
    todo = []
    for it in items:
        row = hits.get(it.get("canon_url"))
        if row is None:
            todo.append(it)
        else:
            _apply_cached(it, row)
    if hits and on_event:
        on_event("reused " + str(len(items) - len(todo)) + "/" + str(len(items))
                 + " pages from the cache")

    sem = asyncio.Semaphore(concurrency)
    done = 0
    out = []
    for coro in asyncio.as_completed([fetch_one(client, it, sem) for it in todo]):
        out.append(await coro)
        done += 1
        if on_event and done % 5 == 0:
            on_event("fetched " + str(done) + "/" + str(len(todo)))
    if cache_days and out:
        db.cache_pages(out)
    return items
