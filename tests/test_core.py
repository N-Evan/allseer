"""One runnable check for the logic that isn't a one-liner: URL identity, story
clustering, the two scoring formulas, selection, and tolerant JSON parsing.

    python tests/test_core.py
"""
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from allseer import rank
from allseer.dedupe import canon_url, cluster, jaccard, pick_representatives, title_tokens
from allseer.llm import parse_json, fallback_queries

NOW = datetime(2026, 8, 27, 12, 0, tzinfo=timezone.utc)


def iso(hours_ago):
    return (NOW - timedelta(hours=hours_ago)).isoformat()


def test_canon():
    a = canon_url("https://WWW.Example.com/post/?utm_source=x&id=7#frag")
    assert a == "https://example.com/post?id=7", a
    assert canon_url("http://example.com/p/") == canon_url("https://www.example.com/p")
    assert canon_url("example.com/a/amp") == "https://example.com/a"
    assert canon_url("") == ""


def test_cluster_and_reps():
    items = [
        {"url": "https://a.com/x", "canon_url": canon_url("https://a.com/x"), "domain": "a.com",
         "title": "OpenModel 3 released with 40% faster inference", "providers": ["hn"],
         "queries": ["q1"], "discussion": 100, "content_chars": 3000},
        {"url": "https://b.com/y", "canon_url": canon_url("https://b.com/y"), "domain": "b.com",
         "title": "OpenModel 3 released, inference 40% faster", "providers": ["reddit"],
         "queries": ["q2"], "discussion": 20, "content_chars": 0},
        {"url": "https://a.com/x?utm_source=t", "canon_url": canon_url("https://a.com/x?utm_source=t"),
         "domain": "a.com", "title": "OpenModel 3 released with 40% faster inference",
         "providers": ["searxng"], "queries": ["q3"], "discussion": 0, "content_chars": 0},
        {"url": "https://c.org/z", "canon_url": canon_url("https://c.org/z"), "domain": "c.org",
         "title": "A tiny Rust crate for sparse tensor slicing", "providers": ["github"],
         "queries": ["q4"], "discussion": 3, "content_chars": 900},
    ]
    cluster(items)
    assert items[0]["cluster_id"] == items[1]["cluster_id"] == items[2]["cluster_id"]
    assert items[3]["cluster_id"] != items[0]["cluster_id"]
    # two distinct domains cover the same story
    assert items[0]["cluster_domains"] == 2, items[0]["cluster_domains"]
    assert items[3]["cluster_domains"] == 1

    reps = pick_representatives(items)
    assert len(reps) == 2
    big = [r for r in reps if r["cluster_domains"] == 2][0]
    assert big["domain"] == "a.com"                      # richest member wins
    assert set(big["providers"]) == {"hn", "reddit", "searxng"}  # provenance merged
    assert set(big["queries"]) == {"q1", "q2", "q3"}
    assert big["discussion"] == 100
    assert big["also_seen"] == ["b.com"]

    assert jaccard(title_tokens("Fast sparse attention kernels"),
                   title_tokens("Sparse attention kernels, fast")) == 1.0


def test_scores_split_trending_from_niche():
    fresh_mainstream = {
        "title": "Big lab ships thing", "domain": "techcrunch.com", "source_type": "news",
        "published_at": iso(3), "cluster_domains": 4, "discussion": 900, "content_chars": 4000,
        "llm_relevance": 9, "llm_novelty": 3, "llm_depth": 3, "llm_importance": 6,
    }
    obscure_deep = {
        "title": "Kernel trick nobody noticed", "domain": "someones.blog", "source_type": "blog",
        "published_at": iso(30), "cluster_domains": 1, "discussion": 4, "content_chars": 9000,
        "llm_relevance": 8, "llm_novelty": 9, "llm_depth": 9, "llm_importance": 8,
    }
    rank.score(fresh_mainstream, NOW)
    rank.score(obscure_deep, NOW)
    assert fresh_mainstream["trending_score"] > obscure_deep["trending_score"]
    assert obscure_deep["niche_score"] > fresh_mainstream["niche_score"]
    assert fresh_mainstream["breakdown"]["age_hours"] == 3.0

    off = dict(obscure_deep, llm_offtopic=True)
    rank.score(off, NOW)
    assert off["trending_score"] == off["niche_score"] == 0

    # unknown publication date must not read as "brand new"
    undated = dict(fresh_mainstream, published_at=None)
    rank.score(undated, NOW)
    assert undated["breakdown"]["recency"] < fresh_mainstream["breakdown"]["recency"]


def test_select_no_overlap_and_domain_spread():
    def mk(i, dom, t, n):
        return {"title": "item " + str(i), "domain": dom, "source_type": "news",
                "published_at": iso(5), "cluster_domains": 2, "discussion": 10,
                "content_chars": 2000, "trending_score": t, "niche_score": n}
    items = [mk(1, "a.com", .9, .1), mk(2, "a.com", .85, .2), mk(3, "b.com", .8, .3),
             mk(4, "c.com", .4, .9), mk(5, "d.com", .3, .8)]
    trending, niche = rank.select(items, 2, 2)
    assert [i["domain"] for i in trending] == ["a.com", "b.com"]   # one slot per domain
    assert all(i not in trending for i in niche)                   # never in both lists
    assert [i["title"] for i in niche] == ["item 4", "item 5"]
    assert trending[0]["rank"] == 1 and trending[0]["bucket"] == "trending"
    assert niche[0]["bucket"] == "niche"
    # asking for more than the pool holds still returns the pool, not a crash
    t2, n2 = rank.select(items[:2], 3, 3)
    assert len(t2) == 2 and len(n2) == 0


def test_emoji_titles_do_not_kill_logging():
    """A cp1252 console must not be able to abort a run (regression: emoji in a repo name)."""
    from allseer import pipeline
    pipeline.STATUS["log"] = []
    pipeline.log("judging 1/10: shimmy - ⚡ Pure-Rust engine — GGUF 🚀")
    assert "judging 1/10" in pipeline.STATUS["log"][-1]


def test_parse_json_survives_local_model_noise():
    assert parse_json('{"a": 1}')["a"] == 1
    assert parse_json('```json\n{"a": 2}\n```')["a"] == 2
    assert parse_json('Sure! Here you go: {"a": 3} hope that helps')["a"] == 3
    for bad in ("", "no json at all", "{broken"):
        try:
            parse_json(bad)
            raise AssertionError("should have raised on " + repr(bad))
        except Exception as e:
            assert type(e).__name__ == "LLMError", e


def test_fallback_queries_are_unique_and_bounded():
    qs = fallback_queries({"name": "Local LLMs", "keywords": "ollama, gguf"}, 5)
    assert len(qs) == 5 and len(set(q.lower() for q in qs)) == 5


def test_date_parsing_formats():
    for s in ("2026-08-27T10:00:00Z", "2026-08-27 10:00:00", "2026-08-27",
              "Thu, 27 Aug 2026 10:00:00 GMT", "Aug 27, 2026"):
        assert rank.parse_date(s) is not None, s
    assert rank.parse_date("not a date") is None
    assert rank.parse_date(None) is None


if __name__ == "__main__":
    for name, fn in sorted(list(globals().items())):
        if name.startswith("test_"):
            fn()
            print("ok", name)
    print("all core checks passed")
