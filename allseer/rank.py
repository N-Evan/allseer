"""Scoring. Deterministic signals in Python, judgement calls from the LLM, combined here.

Two rankings, deliberately different shapes:
  TRENDING = recency + cross-source coverage + relevance + source quality + discussion
  NICHE    = novelty + depth + relevance + importance + LOW coverage
Swap the weights or add a strategy function; nothing else depends on the formulas.
"""
import math
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

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


# GitHub stars are a lifetime total, not today's conversation - they need a higher bar
# than HN points or reddit score before they mean the same thing.
DISCUSSION_CAP = {"github": 8000, "reddit": 1500, "hn": 1000, "paper": 200}


def discussion_score(item):
    cap = DISCUSSION_CAP.get(item.get("source_type"), 1500)
    return min(1.0, math.log1p(max(0, item.get("discussion", 0))) / math.log1p(cap))


def cross_source_score(item):
    return min(1.0, (item.get("cluster_domains", 1) - 1) / 3.0)


def prefilter_score(item, topic):
    """Cheap pre-LLM triage: who gets the limited inference budget."""
    title = (item.get("title") or "").lower()
    kws = [k.strip().lower() for k in (topic.get("keywords") or "").split(",") if k.strip()]
    kw_hit = sum(1 for k in kws if k and k in title)
    has_text = 1.0 if item.get("content_chars", 0) > 500 else 0.0
    return (
        0.30 * recency_score(item)
        + 0.20 * cross_source_score(item)
        + 0.20 * discussion_score(item)
        + 0.15 * SOURCE_QUALITY.get(item.get("source_type"), 0.5)
        + 0.10 * min(1.0, kw_hit / 2.0)
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
