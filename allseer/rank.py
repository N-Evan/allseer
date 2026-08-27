"""Scoring. Deterministic signals in Python, judgement calls from the LLM, combined here.

Two rankings, deliberately different shapes:
  TRENDING = recency + cross-source coverage + relevance + source quality + discussion
  NICHE    = novelty + depth + relevance + importance + LOW coverage
Swap the weights or add a strategy function; nothing else depends on the formulas.
"""
import math
import re
from collections import OrderedDict
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from functools import lru_cache

from .providers import NEWSY

SOURCE_QUALITY = {
    "paper": 0.95, "news": 0.85, "github": 0.80, "blog": 0.70,
    "hn": 0.70, "reddit": 0.55, "other": 0.50,
}
# Big aggregators: fine for trending, disqualifying-ish for "niche find".
MAINSTREAM_PENALTY = 0.45

TRENDING_WEIGHTS = {
    "recency": 0.25, "cross_source": 0.25, "relevance": 0.25,
    "source_quality": 0.10, "discussion": 0.15,
}
NICHE_WEIGHTS = {
    "novelty": 0.25, "depth": 0.25, "relevance": 0.15,
    "importance": 0.15, "low_coverage": 0.20,
}


def parse_date(value):
    if not value:
        return None
    s = str(value).strip()
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00").replace(" ", "T", 1))
    except ValueError:
        pass
    try:
        return parsedate_to_datetime(s)
    except (TypeError, ValueError):
        pass
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%d %b %Y", "%b %d, %Y"):
        try:
            return datetime.strptime(s[:20], fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def age_hours(item, now=None):
    now = now or datetime.now(timezone.utc)
    d = parse_date(item.get("published_at"))
    if d is None:
        return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    return max(0.0, (now - d).total_seconds() / 3600.0)


def recency_score(item, now=None):
    """Half-life 24h. Unknown date is treated as ~3 days old, not as fresh."""
    h = age_hours(item, now)
    if h is None:
        h = 72.0
    return 0.5 ** (h / 24.0)


def fresh_enough(item, days_back, drop_unknown=False):
    """Hard freshness gate. days_back was only ever a per-provider hint, so items with an
    old known date (GitHub created_at, an undated SearXNG hit, an HN fallback result) still
    reached the ranker - where NICHE_WEIGHTS has no recency term at all and happily crowned
    a 2014 repo. Enforce it once, centrally, for every provider.

    Unknown date is kept by default (a lot of good pages publish none) and scored as ~72h
    old; set drop_unknown to require a date.
    """
    if days_back <= 0:
        return True
    h = age_hours(item)
    if h is None:
        return not drop_unknown
    return h <= days_back * 24 + 12  # slack for timezone-sloppy feeds


# GitHub stars are a lifetime total, not today's conversation - they need a higher bar
# than HN points or reddit score before they mean the same thing.
DISCUSSION_CAP = {"github": 8000, "reddit": 1500, "hn": 1000, "paper": 200}


def discussion_score(item):
    cap = DISCUSSION_CAP.get(item.get("source_type"), 1500)
    return min(1.0, math.log1p(max(0, item.get("discussion", 0))) / math.log1p(cap))


def cross_source_score(item):
    return min(1.0, (item.get("cluster_domains", 1) - 1) / 3.0)


# --- topic relevance ------------------------------------------------------
# Nothing used to check a result against its topic before the LLM saw it, so a run for
# "Indie Games & Devlogs" paid to fetch and judge scope.riege.com (freight logistics),
# forums.scopeusers.com and ajtmh.org (tropical medicine) - all matched on the word
# "scope" alone. The judge caught them (19 of 20 came back off_topic) but only after the
# inference budget was already spent on them.

_WORD = re.compile(r"[a-z0-9+#]+")
TOPIC_STOP = {"the", "and", "for", "with", "from", "your", "our", "new", "how", "why",
              "what", "its", "into", "out", "own", "any", "all", "use", "using", "based"}


@lru_cache(maxsize=128)
def _terms(name, keywords):
    """(phrases, words) for a topic. A phrase is a multi-word keyword - specific enough to
    be worth two single-word hits. Cached: this is called once per result per topic."""
    phrases, words = set(), set()
    for k in [name] + str(keywords or "").split(","):
        toks = [w for w in _WORD.findall(k.lower())
                if len(w) > 2 and w not in TOPIC_STOP]
        if len(toks) >= 2:
            phrases.add(" ".join(toks))
        words.update(toks)
    return frozenset(phrases), frozenset(words)


# One shared generic word ("game", "developer") is not evidence of anything: the AI feeds
# leaked "llm-anthropic 0.27" into an indie-devlog run on a single-word match. Two distinct
# words, or one multi-word keyword, is the bar.
ON_TOPIC_MIN = 2


def on_topic(item, topic):
    return topic_match(item, topic) >= ON_TOPIC_MIN


def topic_match(item, topic):
    """How much topic vocabulary the result carries. 0 means nothing matched at all."""
    text = " ".join(str(item.get(k) or "") for k in
                    ("title", "snippet", "domain", "url")).lower()
    phrases, words = _terms(topic.get("name") or "", topic.get("keywords") or "")
    toks = set(_WORD.findall(text))
    return 2 * sum(1 for p in phrases if p in text) + len(words & toks)


def excluded(item, topic):
    """The topic's own exclusion list, applied to the title before anything is spent on
    the item. Word-boundary matched so "hack" does not kill "Hacking the Godot renderer"."""
    title = (item.get("title") or "").lower()
    for raw in str(topic.get("exclusions") or "").split(","):
        term = " ".join(raw.lower().split())
        if len(term) < 3:
            continue
        # "courses" must trip the "course" exclusion; SEO titles are almost always plural.
        if re.search(r"\b" + re.escape(term) + r"s?\b", title):
            return term
    return None


def diversify(items, key, n):
    """Pick n items, round-robin across providers and then across domains inside each.

    Straight score ordering handed the whole budget to one source: GitHub is the only
    provider reporting a discussion number (stars), so discussion_score was in practice an
    "is this GitHub?" term and 20 of 20 judged items in one run came from github.com.
    Round-robin on domain alone then handed it to SearXNG instead, which returns ~100
    one-off domains per run against RSS's ~10 - so it won 17 of 20 slots with undated
    evergreen pages while the dated feed articles got three. Balance providers first.

    This only decides who gets looked at; select() still ranks on the real scores."""
    ranked = sorted(items, key=key, reverse=True)
    groups = OrderedDict()
    for it in ranked:
        prov = (it.get("providers") or ["?"])[0]
        groups.setdefault(prov, OrderedDict()).setdefault(it.get("domain") or "?", []).append(it)
    out = []
    while len(out) < n:
        before = len(out)
        for by_domain in groups.values():
            for dom, queue in by_domain.items():
                if queue:
                    out.append(queue.pop(0))
                    by_domain.move_to_end(dom)  # next turn goes to a different domain
                    break
            if len(out) >= n:
                break
        if len(out) == before:
            break  # everything is exhausted
    return out


def prefilter_score(item, topic):
    """Cheap pre-LLM triage: who gets the limited inference budget."""
    has_text = 1.0 if item.get("content_chars", 0) > 500 else 0.0
    return (
        0.30 * min(1.0, topic_match(item, topic) / 5.0)
        + 0.25 * recency_score(item)
        + 0.15 * cross_source_score(item)
        + 0.15 * SOURCE_QUALITY.get(item.get("source_type"), 0.5)
        # Was 0.20. Only GitHub populates this, so a high weight is a GitHub subsidy.
        + 0.10 * discussion_score(item)
        + 0.05 * has_text
    )


def score(item, now=None):
    """Fills trending_score, niche_score and the breakdown shown in the UI."""
    rel = (item.get("llm_relevance") or 0) / 10.0
    nov = (item.get("llm_novelty") or 0) / 10.0
    dep = (item.get("llm_depth") or 0) / 10.0
    imp = (item.get("llm_importance") or 0) / 10.0
    rec = recency_score(item, now)
    cross = cross_source_score(item)
    disc = discussion_score(item)
    qual = SOURCE_QUALITY.get(item.get("source_type"), 0.5)
    mainstream = bool(NEWSY.search(item.get("domain") or ""))
    low_cov = (1.0 - cross) * (MAINSTREAM_PENALTY if mainstream else 1.0)

    t = TRENDING_WEIGHTS
    trending = (
        t["recency"] * rec + t["cross_source"] * cross + t["relevance"] * rel
        + t["source_quality"] * qual + t["discussion"] * disc
    )
    n = NICHE_WEIGHTS
    niche = (
        n["novelty"] * nov + n["depth"] * dep + n["relevance"] * rel
        + n["importance"] * imp + n["low_coverage"] * low_cov
    )
    # Thin content can't demonstrate depth; don't let a snippet win a niche slot.
    if item.get("content_chars", 0) < 400:
        niche *= 0.85
    if item.get("llm_offtopic"):
        trending = niche = 0.0

    item["trending_score"] = round(trending, 4)
    item["niche_score"] = round(niche, 4)
    item["breakdown"] = {
        "recency": round(rec, 3), "cross_source": round(cross, 3),
        "discussion": round(disc, 3), "source_quality": qual,
        "low_coverage": round(low_cov, 3), "relevance": round(rel, 3),
        "novelty": round(nov, 3), "depth": round(dep, 3), "importance": round(imp, 3),
        "age_hours": None if age_hours(item, now) is None else round(age_hours(item, now), 1),
    }
    return item


def select(items, n_trending, n_niche):
    """Pick the two rankings. An item never appears in both, and one domain does not
    get to own a whole list."""
    live = [i for i in items if not i.get("llm_offtopic")]

    def take(pool, key, n, max_per_domain=1):
        chosen, used = [], {}
        for it in sorted(pool, key=lambda x: x.get(key, 0), reverse=True):
            d = it.get("domain") or "?"
            if used.get(d, 0) >= max_per_domain:
                continue
            chosen.append(it)
            used[d] = used.get(d, 0) + 1
            if len(chosen) >= n:
                break
        # Relax the cap in steps rather than return a short list: better two items from
        # one domain than one domain owning the whole list.
        for cap in (max_per_domain + 1, 10 ** 6):
            if len(chosen) >= n:
                break
            for it in sorted(pool, key=lambda x: x.get(key, 0), reverse=True):
                d = it.get("domain") or "?"
                if any(it is c for c in chosen) or used.get(d, 0) >= cap:
                    continue
                chosen.append(it)
                used[d] = used.get(d, 0) + 1
                if len(chosen) >= n:
                    break
        return chosen

    trending = take(live, "trending_score", n_trending)
    rest = [i for i in live if not any(i is t for t in trending)]  # identity, not dict equality
    niche = take(rest, "niche_score", n_niche)

    for it in items:
        it["bucket"] = ""
        it["rank"] = None
    for rank, it in enumerate(trending, 1):
        it["bucket"] = "trending"
        it["rank"] = rank
    for rank, it in enumerate(niche, 1):
        it["bucket"] = "niche"
        it["rank"] = rank
    return trending, niche
