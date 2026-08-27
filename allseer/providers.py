"""Search providers. All free, none need an API key.

Each provider is an async callable (client, query, cfg) -> list[result dict].
Add one and list its name in the `providers` setting; nothing else needs to change.
"""
import asyncio
import re
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone

from urllib.parse import urlsplit

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
# Editorial gamedev/AI outlets: a feed item from these is a real article, not "other".
FEEDY = re.compile(
    r"(80\.lv|gamedeveloper\.com|gamesindustry\.biz|gamefromscratch|indiedb|"
    r"godotengine\.org|unrealengine\.com|unity\.com|itch\.io|huggingface\.co|gdcvault)", re.I
)


# Social walled gardens and reference pages. They are never the research artefact - they
# are a link to it - and every one of them either blocks the fetch or has no extractable
# text. A single "Indie Games & Devlogs" run pulled 12 facebook, 8 linkedin, 5 instagram,
# 4 x.com and 4 wikipedia results out of SearXNG, all of which reached dedupe and the fetch
# budget before dying. Drop them where results are built, not where they are fetched.
JUNK = re.compile(
    r"^(www\.)?("
    r"facebook\.com|m\.facebook\.com|instagram\.com|threads\.net|"
    r"linkedin\.com|[a-z]{2}\.linkedin\.com|"
    r"x\.com|twitter\.com|t\.co|nitter\.[a-z.]+|"
    r"tiktok\.com|pinterest\.[a-z.]+|quora\.com|"
    r"[a-z]{2}\.wikipedia\.org|wikipedia\.org|wikimedia\.org|"
    r"podcasts\.apple\.com|open\.spotify\.com|soundcloud\.com|"
    r"fastercapital\.com|slideshare\.net|scribd\.com|coursehero\.com"
    r")$", re.I,
)


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
    if BLOGGY.search(d) or FEEDY.search(d):
        return "blog"
    if provider == "rss":
        return "blog"  # it publishes a feed, so it is a publication
    return "other"


def _iso(dt):
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


def _norm_date(value):
    """Store every date as ISO-8601 UTC. Feeds hand back RFC-2822 ("Thu, 27 Aug 2026 ..."),
    which parse_date understands but SQL ordering and the UI do not."""
    if not value:
        return None
    from .rank import parse_date  # local: rank imports this module
    d = parse_date(value)
    return _iso(d) if d else str(value)


def _result(url, title, provider, query, snippet="", published=None, author=None, discussion=0):
    if not url or not title:
        return None
    cu = canon_url(url)
    if JUNK.match(domain_of(cu) or ""):
        return None
    # A bare site root is a publication, not a story. SearXNG answers "indie devlog" with
    # site homepages, which arrive undated and won four niche slots in one run
    # ("The Llama Concept", "Emanschi Games"). Every real artefact has a path.
    if cu and urlsplit(cu).path in ("", "/") and not urlsplit(cu).query:
        return None
    published = _norm_date(published)
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


REDDIT_THROTTLE = Throttle(12.0)
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
    # created:, not pushed:. A 2014 repo pushed today is not a new find, and pushed:>
    # was the single reason 2014-2023 repos kept winning slots in a days_back=3 run.
    since = (datetime.now(timezone.utc) - timedelta(days=cfg["days_back"])).date()
    # created:> alone floods the results with day-old empty repos. A repo that picked up a
    # handful of stars in its first days has actual traction; this cut 15925 matches to 90
    # without displacing a single relevant one.
    stars = int(cfg.get("github_min_stars") or 0)
    bar = (" stars:>=" + str(stars)) if stars > 0 else ""
    data = await _json(
        client,
        "https://api.github.com/search/repositories",
        # Relevance order beats "recently pushed" here, and the in: qualifier stops GitHub
        # from OR-ing the words together and returning unrelated repos.
        params={"q": query + " in:name,description,readme created:>" + str(since) + bar,
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


# --- rss -------------------------------------------------------------------
# The highest-signal sources for gamedev/devlog content publish feeds, not APIs. One
# generic provider covers all of them: add a URL to the rss_feeds setting, nothing else.
FEED_UA = REDDIT_UA  # plain UAs get 403 from a few CDNs
_FEED_CACHE = {}     # url -> (monotonic_fetched_at, [entry dicts])
_FEED_LOCKS = {}
FEED_TTL = 900.0     # a feed does not change meaningfully inside a run
FEED_CAP = 10        # best-matching entries kept per feed per query


def _feed_text(el, *names):
    """RSS and Atom disagree on every tag name; try both namespaced and bare."""
    for n in names:
        for tag in (n, "{http://www.w3.org/2005/Atom}" + n):
            v = el.findtext(tag)
            if v and v.strip():
                return v.strip()
    return ""


def parse_feed(text):
    """RSS 2.0 <item> or Atom <entry> -> list of {url,title,snippet,published,author}."""
    root = ET.fromstring(text.encode("utf-8", "ignore") if isinstance(text, str) else text)
    nodes = root.iter("item")
    entries = list(nodes)
    if not entries:
        entries = list(root.iter("{http://www.w3.org/2005/Atom}entry"))
    out = []
    for e in entries:
        url = _feed_text(e, "link", "guid", "id")
        if not url.startswith("http"):
            url = ""
            for ln in e.iter("{http://www.w3.org/2005/Atom}link"):
                if (ln.get("rel") or "alternate") == "alternate" and ln.get("href"):
                    url = ln.get("href")
                    break
        body = _feed_text(e, "description", "summary", "content",
                          "{http://purl.org/rss/1.0/modules/content/}encoded")
        out.append({
            "url": url,
            "title": _feed_text(e, "title"),
            "snippet": re.sub(r"<[^>]+>", " ", body),
            "published": _feed_text(e, "pubDate", "published", "updated",
                                    "{http://purl.org/dc/elements/1.1/}date") or None,
            "author": _feed_text(e, "author", "creator",
                                 "{http://purl.org/dc/elements/1.1/}creator") or None,
        })
    return out


async def _get_feed(client, url):
    """Fetch+parse once per URL per run. reddit.com shares the reddit throttle."""
    loop = asyncio.get_running_loop()
    lock = _FEED_LOCKS.setdefault(url, asyncio.Lock())
    async with lock:
        hit = _FEED_CACHE.get(url)
        if hit and loop.time() - hit[0] < FEED_TTL:
            return hit[1]
        if "reddit.com" in url:
            async with REDDIT_THROTTLE:
                r = await client.get(url, headers={"User-Agent": FEED_UA})
        else:
            r = await client.get(url, headers={"User-Agent": FEED_UA})
        r.raise_for_status()
        entries = parse_feed(r.text)
        _FEED_CACHE[url] = (loop.time(), entries)
        return entries


def feed_match_count(query, title, snippet):
    """How many of the query's distinctive words the entry contains.

    A feed is not searchable, so filtering happens locally on the entry text. Any-one-word
    matching let "agentic system tool use" pull 103 gamedev articles that merely said
    "tool", so a query with three or more distinctive words needs two of them. Returns the
    count (0 = no match) so the caller can also rank by it. Deliberately loose past the
    threshold - the topic gate and the LLM judge are the real filters."""
    hay = (title + " " + snippet).lower()
    words = {w for w in re.split(r"\W+", query.lower()) if len(w) > 3 and w not in FEED_STOP}
    if not words:
        return 1
    need = 2 if len(words) >= 3 else 1
    hits = sum(1 for w in words if w in hay)
    return hits if hits >= need else 0


def feed_matches(query, title, snippet):
    return feed_match_count(query, title, snippet) > 0


FEED_STOP = {"what", "when", "with", "from", "this", "that", "your", "than", "then", "into",
             "best", "news", "latest", "recent", "using", "about", "over", "very", "more",
             "most", " how", "does", "will", "have", "been", "they", "them", "some", "make"}


async def rss(client, query, cfg):
    urls = [u.strip() for u in re.split(r"[,\s]+", cfg.get("rss_feeds") or "") if u.strip()]
    if not urls:
        return []
    got = await asyncio.gather(*(_get_feed(client, u) for u in urls), return_exceptions=True)
    out = []
    for u, entries in zip(urls, got):
        if isinstance(entries, BaseException):
            continue  # one dead feed must not take the provider down
        # "solo developer game jam insights" matched 80 entries in one run, because on a
        # gamedev feed every article contains "game" and "developer". Take the best-matching
        # FEED_CAP per feed per query instead of the whole feed.
        scored = []
        for e in entries:
            n = feed_match_count(query, e["title"], e["snippet"])
            if n:
                scored.append((n, e))
        scored.sort(key=lambda x: -x[0])
        for _, e in scored[:FEED_CAP]:
            out.append(_result(e["url"], e["title"], "rss", query,
                               snippet=e["snippet"], published=e["published"],
                               author=e["author"] or domain_of(u)))
    return out


REGISTRY = {"hn": hn, "reddit": reddit, "github": github, "arxiv": arxiv,
            "searxng": searxng, "rss": rss}

# Unauthenticated quotas are the binding constraint: reddit tolerates only a couple of
# requests per run, GitHub search allows ~10/min. Spend the query budget where it is free.
# rss re-uses one cached fetch per feed, so extra queries there are nearly free.
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
