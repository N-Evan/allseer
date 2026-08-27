"""URL identity + same-story clustering. Union-find over canonical URL and title similarity."""
import re
from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode

TRACKING = re.compile(r"^(utm_|fbclid|gclid|mc_cid|mc_eid|ref_?$|ref_src|igshid|si$|spm)", re.I)

STOP = set(
    """a an the and or of for to in on at from with without by as is are was were be been
    this that these those it its into via new how why what when who will can could would
    you your we our their his her they i vs about over under more most just now today
    news article post report says said than then but not no yes""".split()
)


def canon_url(url: str) -> str:
    """Stable identity for a link: drop tracking params, fragment, trailing slash, www, AMP."""
    if not url:
        return ""
    url = url.strip()
    if "://" not in url:
        url = "https://" + url
    try:
        p = urlsplit(url)
    except ValueError:
        return url.lower()
    host = (p.hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    if host.endswith(".m.wikipedia.org"):
        host = host.replace(".m.wikipedia.org", ".wikipedia.org")
    path = re.sub(r"/+$", "", p.path) or "/"
    path = re.sub(r"/amp$", "", path, flags=re.I)
    q = [(k, v) for k, v in parse_qsl(p.query, keep_blank_values=False) if not TRACKING.match(k)]
    q.sort()
    scheme = "https" if p.scheme in ("http", "https", "") else p.scheme
    return urlunsplit((scheme, host, path, urlencode(q), ""))


def domain_of(url: str) -> str:
    try:
        h = (urlsplit(canon_url(url)).hostname or "").lower()
    except ValueError:
        return ""
    return h


def title_tokens(title: str) -> set:
    t = re.sub(r"[^a-z0-9\s]+", " ", (title or "").lower())
    return {w for w in t.split() if len(w) > 2 and w not in STOP}


def jaccard(a: set, b: set) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def cluster(items, threshold=0.55):
    """Group items covering the same underlying story.

    Same canonical URL -> always merged. Otherwise merged when title token sets overlap
    strongly. Sets item['cluster_id'] and item['cluster_domains'] (distinct domains in the
    cluster = independent-source count).
    """
    parent = list(range(len(items)))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    def union(i, j):
        ri, rj = find(i), find(j)
        if ri != rj:
            parent[max(ri, rj)] = min(ri, rj)

    by_url = {}
    toks = [title_tokens(it.get("title", "")) for it in items]
    for i, it in enumerate(items):
        cu = it.get("canon_url") or canon_url(it.get("url", ""))
        if cu in by_url:
            union(i, by_url[cu])
        else:
            by_url[cu] = i

    # ponytail: O(n^2) title compare. Fine for a few hundred items per run; bucket by
    # shared rare token if a run ever gets into the thousands.
    for i in range(len(items)):
        for j in range(i + 1, len(items)):
            if find(i) == find(j):
                continue
            if jaccard(toks[i], toks[j]) >= threshold:
                union(i, j)

    groups = {}
    for i in range(len(items)):
        groups.setdefault(find(i), []).append(i)

    for root, idxs in groups.items():
        domains = {items[i].get("domain") or domain_of(items[i].get("url", "")) for i in idxs}
        domains.discard("")
        for i in idxs:
            items[i]["cluster_id"] = root
            items[i]["cluster_domains"] = max(1, len(domains))
    return items


def pick_representatives(items):
    """One item per cluster: prefer real content, then discussion, then longest title."""
    best = {}
    for it in items:
        cid = it.get("cluster_id")
        cur = best.get(cid)
        key = (it.get("content_chars", 0) > 400, it.get("discussion", 0), len(it.get("title") or ""))
        if cur is None or key > cur[0]:
            best[cid] = (key, it)
    reps = []
    for cid, (_, it) in best.items():
        members = [x for x in items if x.get("cluster_id") == cid]
        it["providers"] = sorted({p for m in members for p in m.get("providers", [])})
        it["queries"] = sorted({q for m in members for q in m.get("queries", [])})
        it["discussion"] = max(m.get("discussion", 0) for m in members)
        it["also_seen"] = sorted({m.get("domain", "") for m in members} - {it.get("domain", "")})
        reps.append(it)
    return reps
