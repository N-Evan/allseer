"""Fetch a page and pull out readable text. Every failure is survivable: an item keeps
its search snippet and just scores lower on depth."""
import asyncio
import json

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


async def fetch_many(client, items, on_event=None, concurrency=8):
    sem = asyncio.Semaphore(concurrency)
    done = 0
    out = []
    for coro in asyncio.as_completed([fetch_one(client, it, sem) for it in items]):
        out.append(await coro)
        done += 1
        if on_event and done % 5 == 0:
            on_event("fetched " + str(done) + "/" + str(len(items)))
    return out
