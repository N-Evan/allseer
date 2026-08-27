"""Search providers. All free, none need an API key.

Each provider is an async callable (client, query, cfg) -> list[result dict].
Add one and list its name in the `providers` setting; nothing else needs to change.
"""
import asyncio
import re
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone

from .dedupe import canon_url, domain_of

NEWSY = re.compile(
    r"(reuters|apnews|bloomberg|ft\.com|wsj|nytimes|theguardian|bbc\.|cnbc|axios|"
    r"techcrunch|theverge|arstechnica|wired|venturebeat|zdnet|engadget|theinformation)",
    re.I,
)
PAPERS = re.compile(
    r"(arxiv\.org|openreview|acm\.org|ieee|nature\.com|science\.org|biorxiv|ssrn)", re.I
)
BLOGGY = re.compile(r"(blog|substack|medium|dev\.to|hashnode|ghost\.io|\.io$|\.dev$)", re.I)


def classify_source(url: str, provider: str) -> str:
    d = domain_of(url)
    if provider == "reddit" or "reddit.com" in d:
        return "reddit"
    if provider == "hn" or "news.ycombinator" in d:
        return "hn"
    if provider == "github" or d == "github.com":
        return "github"
    if PAPERS.search(d):
        return "paper"
    if NEWSY.search(d):
        return "news"
    if BLOGGY.search(d):
        return "blog"
    return "other"


def _iso(dt):
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


def _result(url, title, provider, query, snippet="", published=None, author=None, discussion=0):
    if not url or not title:
        return None
    cu = canon_url(url)
    return {
        "url": url,
        "canon_url": cu,
        "domain": domain_of(cu),
        "title": " ".join(str(title).split())[:400],
        "snippet": " ".join(str(snippet or "").split())[:1200],
        "published_at": published,
        "author": author,
        "discussion": int(discussion or 0),
        "source_type": classify_source(cu, provider),
        "providers": [provider],
        "queries": [query],
    }


async def _json(client, url, **kw):
    r = await client.get(url, **kw)
    r.raise_for_status()
    ct = r.headers.get("content-type", "")
    if "json" not in ct:
        # A 200 with an HTML body means blocked / rate-limited / interstitial, not data.
        raise ValueError("expected JSON, got " + (ct or "?") + " from " + str(r.url)[:120])
    return r.json()


class Throttle:
    """Serialise a provider's calls with a minimum gap. Reddit answers a burst with 429,
    arXiv with an empty body, so both need one request at a time."""

    def __init__(self, gap):
        self.gap = gap
        self._lock = asyncio.Lock()
        self._last = 0.0

    async def __aenter__(self):
        await self._lock.acquire()
        loop = asyncio.get_running_loop()
        wait = self.gap - (loop.time() - self._last)
        if wait > 0:
            await asyncio.sleep(wait)
        return self

    async def __aexit__(self, *exc):
        self._last = asyncio.get_running_loop().time()
        self._lock.release()
        return False


REDDIT_THROTTLE = Throttle(6.0)
ARXIV_THROTTLE = Throttle(3.0)


# --- providers -------------------------------------------------------------

async def hn(client, query, cfg):
    since = int((datetime.now(timezone.utc) - timedelta(days=cfg["days_back"])).timestamp())
    params = {"query": query, "tags": "story", "hitsPerPage": 20,
              "numericFilters": "created_at_i>" + str(since)}
    data = await _json(client, "https://hn.algolia.com/api/v1/search", params=params)
    if not data.get("hits"):
        # Narrow queries often have nothing inside the freshness window. Recency is a
        # ranking signal, not a filter, so widen rather than return nothing.
        params.pop("numericFilters")
        data = await _json(client, "https://hn.algolia.com/api/v1/search", params=params)
    out = []
    for h in data.get("hits", []):
        url = h.get("url") or ("https://news.ycombinator.com/item?id=" + str(h.get("objectID")))
        out.append(
            _result(
                url, h.get("title") or h.get("story_title"), "hn", query,
                snippet=h.get("story_text") or "",
                published=h.get("created_at"),
                author=h.get("author"),
                discussion=(h.get("points") or 0) + 2 * (h.get("num_comments") or 0),
            )
        )
    return out


# Reddit blocks unauthenticated /search.json (403 on www, HTML interstitial on old),
# but still serves the Atom feed to a browser UA.
# ponytail: RSS carries no score/comment counts, so reddit items get discussion=0 and lean
# on the other signals. Add OAuth (script app, free) if that signal starts to matter.
REDDIT_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
             "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")
_RD_LINK = re.compile(r'href="(https?://[^"]+)"[^>]*>\s*\[link\]', re.I)
_RD_SUB = re.compile(r"/r/([A-Za-z0-9_]+)/")


async def reddit(client, query, cfg):
    t = "day" if cfg["days_back"] <= 1 else ("week" if cfg["days_back"] <= 7 else "month")
    params = {"q": query, "sort": "top", "t": t, "limit": 25}
    for attempt in (0, 1):
        async with REDDIT_THROTTLE:
            r = await client.get(
                "https://www.reddit.com/search.rss", params=params,
                headers={"User-Agent": REDDIT_UA},
            )
        if r.status_code != 429:
            break
        await asyncio.sleep(3)
    r.raise_for_status()
    ns = {"a": "http://www.w3.org/2005/Atom"}
    root = ET.fromstring(r.text.encode("utf-8", "ignore"))
    out = []
    for e in root.findall("a:entry", ns):
        perma = ""
        for ln in e.findall("a:link", ns):
            perma = ln.get("href") or perma
        body = e.findtext("a:content", "", ns) or ""
        m = _RD_LINK.search(body)
        # Link posts point at the source article; self-posts stay on reddit.
        url = m.group(1) if m and "reddit.com" not in m.group(1) else perma
        sub = _RD_SUB.search(perma or "")
        text = re.sub(r"<[^>]+>", " ", body)
        out.append(
            _result(
                url, e.findtext("a:title", "", ns), "reddit", query,
                snippet=text,
                published=e.findtext("a:updated", None, ns),
                author=("r/" + sub.group(1)) if sub else "reddit",
            )
        )
    return out


async def github(client, query, cfg):
    since = (datetime.now(timezone.utc) - timedelta(days=max(cfg["days_back"], 14))).date()
    data = await _json(
        client,
        "https://api.github.com/search/repositories",
        # Relevance order beats "recently pushed" here, and the in: qualifier stops GitHub
        # from OR-ing the words together and returning unrelated repos.
        params={"q": query + " in:name,description,readme pushed:>" + str(since),
                "per_page": 12},
        headers={"Accept": "application/vnd.github+json"},
    )
    out = []
    for r in data.get("items", []):
        desc = r.get("description") or "repository"
        out.append(
            _result(
                r.get("html_url"), str(r.get("full_name")) + " - " + desc, "github", query,
                snippet=desc + " | topics: " + ", ".join(r.get("topics") or []),
                # created_at, not pushed_at: a 2019 repo pushed today is not a new find.
                published=r.get("created_at"),
                author=(r.get("owner") or {}).get("login"),
                discussion=r.get("stargazers_count") or 0,
            )
        )
    return out




async def arxiv(client, query, cfg):
    # A bare space means OR here, which returns unrelated papers; AND the terms.
    terms = [w for w in re.split(r"\W+", query) if len(w) > 1]
    sq = " AND ".join("all:" + w for w in terms) or ("all:" + query)
    params = {"search_query": sq, "sortBy": "submittedDate", "sortOrder": "descending",
              "max_results": 12}
    text = ""
    for attempt in (0, 1):
        async with ARXIV_THROTTLE:
            r = await client.get("http://export.arxiv.org/api/query", params=params)
        r.raise_for_status()
        text = r.text.strip()
        if text:
            break
    if not text:
        raise ValueError("arxiv returned an empty body (rate limited)")
    ns = {"a": "http://www.w3.org/2005/Atom"}
    root = ET.fromstring(text.encode("utf-8", "ignore"))
    out = []
    for e in root.findall("a:entry", ns):
        authors = [a.findtext("a:name", "", ns) for a in e.findall("a:author", ns)]
        out.append(
            _result(
                e.findtext("a:id", "", ns), e.findtext("a:title", "", ns), "arxiv", query,
                snippet=e.findtext("a:summary", "", ns),
                published=e.findtext("a:published", None, ns),
                author=", ".join(authors[:3]),
            )
        )
    return out


async def searxng(client, query, cfg):
    base = (cfg.get("searxng_url") or "").rstrip("/")
    if not base:
        return []
    tr = "day" if cfg["days_back"] <= 1 else ("week" if cfg["days_back"] <= 7 else "month")
    data = await _json(
        client,
        base + "/search",
        params={"q": query, "format": "json", "time_range": tr, "safesearch": 0},
    )
    out = []
    for r in data.get("results", [])[:25]:
        out.append(
            _result(
                r.get("url"), r.get("title"), "searxng", query,
                snippet=r.get("content") or "",
                published=r.get("publishedDate"),
            )
        )
    return out


REGISTRY = {"hn": hn, "reddit": reddit, "github": github, "arxiv": arxiv, "searxng": searxng}

# Unauthenticated quotas are the binding constraint: reddit tolerates only a couple of
# requests per run, GitHub search allows ~10/min. Spend the query budget where it is free.
QUERY_CAP = {"reddit": 2, "github": 5}


async def search_all(client, queries, cfg, names, on_event=None):
    """Run every enabled provider over every query. Failures are logged, never fatal."""
    tasks = []
    for name in names:
        fn = REGISTRY.get(name)
        if not fn:
            continue
        for q in queries[:QUERY_CAP.get(name, len(queries))]:
            tasks.append((name, q, fn))

    errors = []
    sem = asyncio.Semaphore(6)

    async def run(name, q, fn):
        async with sem:
            try:
                got = [x for x in await fn(client, q, cfg) if x]
                if on_event:
                    on_event(name + ": " + str(len(got)) + ' hits for "' + q[:60] + '"')
                return got
            except Exception as e:  # provider down, rate-limited, blocked JSON, bad XML...
                msg = (name + ' failed for "' + q[:40] + '": ' + type(e).__name__ + ": " + str(e))[:300]
                errors.append(msg)
                if on_event:
                    on_event(msg)
                return []

    results = []
    for chunk in await asyncio.gather(*(run(n, q, f) for n, q, f in tasks)):
        results.extend(chunk)
    return results, errors
