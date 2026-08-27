"""One runnable check for the logic that isn't a one-liner: URL identity, story
clustering, the two scoring formulas, selection, and tolerant JSON parsing.

    python tests/test_core.py
"""
import collections
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from allseer import db, pipeline, providers, rank
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


def test_stop_is_safe_when_nothing_is_running():
    from allseer import pipeline
    assert pipeline.stop() is False          # no task -> no crash, just False
    assert pipeline.STATUS["running"] is False


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


def test_fresh_enough_gates_by_days_back():
    from datetime import datetime, timedelta, timezone
    now = datetime.now(timezone.utc)
    old = {"published_at": (now - timedelta(days=400)).isoformat()}
    new_ = {"published_at": (now - timedelta(hours=6)).isoformat()}
    edge = {"published_at": (now - timedelta(days=5)).isoformat()}
    undated = {"published_at": None}
    assert rank.fresh_enough(new_, 3)
    assert not rank.fresh_enough(old, 3), "a 2014 item must never survive days_back=3"
    assert not rank.fresh_enough(edge, 3)
    assert rank.fresh_enough(edge, 7)
    assert rank.fresh_enough(undated, 3), "undated is kept by default"
    assert not rank.fresh_enough(undated, 3, drop_unknown=True)
    assert rank.fresh_enough(old, 0), "days_back=0 disables the gate"


def test_parse_feed_handles_rss_and_atom():
    from allseer.providers import parse_feed, feed_matches
    rss = """<?xml version="1.0"?><rss version="2.0"><channel>
      <item><title>Godot 4.8 renderer work</title>
        <link>https://80.lv/articles/godot-48</link>
        <description>&lt;p&gt;Vulkan backend notes&lt;/p&gt;</description>
        <pubDate>Thu, 27 Aug 2026 10:14:00 +0000</pubDate></item>
    </channel></rss>"""
    atom = """<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom">
      <entry><title>My level design devlog</title>
        <link rel="alternate" href="https://example.dev/devlog-3"/>
        <id>t3_abc</id><summary>blockout pass</summary>
        <updated>2026-08-27T10:00:00Z</updated></entry>
    </feed>"""
    a = parse_feed(rss)
    assert len(a) == 1 and a[0]["url"].endswith("/godot-48"), a
    assert a[0]["published"].startswith("Thu, 27 Aug") and "Vulkan" in a[0]["snippet"]
    b = parse_feed(atom)
    assert len(b) == 1 and b[0]["url"] == "https://example.dev/devlog-3", b
    assert rank.parse_date(a[0]["published"]) is not None

    assert feed_matches("godot renderer", a[0]["title"], a[0]["snippet"])
    assert not feed_matches("kubernetes operator", a[0]["title"], a[0]["snippet"])
    assert feed_matches("the a of", a[0]["title"], "")  # no distinctive words -> keep


def test_github_query_uses_created_not_pushed():
    import inspect
    from allseer import providers
    src = inspect.getsource(providers.github)
    assert "readme created:>" in src and "readme pushed:>" not in src


def test_feed_items_are_classified_as_publications():
    from allseer.providers import classify_source
    assert classify_source("https://80.lv/articles/x", "rss") == "blog"
    assert classify_source("https://www.gamedeveloper.com/x", "rss") == "blog"
    assert classify_source("https://reddit.com/r/godot/x", "rss") == "reddit"
    assert classify_source("https://arxiv.org/abs/1", "rss") == "paper"



def test_junk_domains_never_become_results():
    """Social walled gardens ate 30+ slots of a real run before this gate existed."""
    for url in ("https://www.facebook.com/groups/gamedev/posts/1",
                "https://linkedin.com/in/someone", "https://x.com/dev/status/1",
                "https://en.wikipedia.org/wiki/Video_game", "https://www.pinterest.com/pin/1"):
        assert providers._result(url, "a title", "searxng", "q") is None, url
    for url in ("https://www.reddit.com/r/gamedev/comments/1",
                "https://itch.io/blog/1", "https://github.com/a/b",
                "https://www.gamesindustry.biz/x"):
        assert providers._result(url, "a title", "searxng", "q") is not None, url


def test_site_roots_are_not_stories():
    """SearXNG answers "indie devlog" with homepages; four won niche slots in one run."""
    for url in ("https://thellamaconcept.com", "https://emanschigames.com/",
                "https://magnate-games.itch.io"):
        assert providers._result(url, "a title", "searxng", "q") is None, url
    for url in ("https://godotengine.org/article/dev-snapshot-4-8/",
                "https://example.com/?p=123"):
        assert providers._result(url, "a title", "searxng", "q") is not None, url


def test_topic_gate_keeps_the_topic_and_drops_the_homonym():
    topic = {"name": "Indie Games & Devlogs",
             "keywords": "devlog, indie dev, solo developer, game jam, scope management",
             "exclusions": "trailer, sale, course"}
    good = {"title": "How I managed scope on my solo indie game",
            "snippet": "devlog postmortem", "domain": "reddit.com", "url": ""}
    homonym = {"title": "Malaria transmission scope", "snippet": "",
               "domain": "ajtmh.org", "url": ""}
    assert rank.on_topic(good, topic)
    assert not rank.on_topic(homonym, topic)
    assert rank.topic_match(good, topic) > rank.topic_match(homonym, topic)


def test_exclusions_are_word_bounded_but_catch_plurals():
    topic = {"name": "T", "keywords": "godot", "exclusions": "trailer, sale, hack, course"}
    def t(title):
        return rank.excluded({"title": title}, topic)
    assert t("Steam Autumn Sale starts") == "sale"
    assert t("17+ Best Developer Courses 2026") == "course"   # plural
    assert t("New GTA 6 trailer") == "trailer"
    assert t("Hacking the Godot renderer") is None            # not a bare "hack"
    assert t("Godot 4.8 dev snapshot") is None


def test_diversify_balances_providers_before_domains():
    """20 of 20 judged items came from github.com because ranking was score-only."""
    items = ([{"domain": "github.com", "providers": ["github"], "s": 1.0}] * 30
             + [{"domain": "d%d.com" % i, "providers": ["searxng"], "s": 0.9}
                for i in range(30)]
             + [{"domain": "reddit.com", "providers": ["rss"], "s": 0.5},
                {"domain": "itch.io", "providers": ["rss"], "s": 0.4}])
    got = rank.diversify(items, lambda i: i["s"], 9)
    by_prov = collections.Counter(i["providers"][0] for i in got)
    assert len(got) == 9
    assert set(by_prov) == {"github", "searxng", "rss"}, by_prov
    # no provider may run away with the budget
    assert max(by_prov.values()) <= 4, by_prov


def test_diversify_does_not_lose_or_duplicate_items():
    items = [{"domain": "a.com", "providers": ["rss"], "s": 0.5},
             {"domain": "a.com", "providers": ["rss"], "s": 0.4},
             {"domain": "b.com", "providers": ["hn"], "s": 0.3}]
    got = rank.diversify(items, lambda i: i["s"], 99)
    assert len(got) == 3 and len({id(i) for i in got}) == 3


def test_feed_cap_stops_one_feed_flooding_a_query():
    """"solo developer game jam insights" matched 80 entries on one gamedev feed."""
    assert providers.feed_match_count("solo developer game jam", "Solo developer diary",
                                      "a game jam postmortem") >= 2
    assert providers.feed_match_count("solo developer game jam", "Unrelated news", "") == 0
    assert providers.FEED_CAP > 0


def test_job_topic_is_seeded_with_its_own_feeds_and_providers():
    name, keywords, exclusions, feeds, provs = db.JOB_TOPIC
    assert feeds.split(), "job topic must pin its own feeds"
    assert provs == "rss,searxng", "arxiv/github/hn have nothing to say about a job hunt"
    # _NOISE bans the whole subject of this topic; it must not be inherited.
    terms = {t.strip() for t in exclusions.split(",")}
    for banned in ("hiring", "job posting", "salary", "layoffs"):
        assert banned not in terms, banned
    assert "salary guide" in terms   # the SEO listicle, not the subject
    posting = {"title": "Senior Unity Developer (Remote)", "snippet": "", "domain": "", "url": ""}
    topic = {"name": name, "keywords": keywords, "exclusions": exclusions}
    assert rank.on_topic(posting, topic)
    assert rank.excluded(posting, topic) is None
    # the keywords are used verbatim as queries, so they must read like posting titles
    assert all(len(k.split()) >= 2 for k in keywords.split(",")), keywords


def test_topic_feeds_and_providers_override_the_global_settings():
    settings = {"days_back": "3", "searxng_url": "", "rss_feeds": "https://global/feed",
                "github_min_stars": "5"}
    assert pipeline._cfg(settings)["rss_feeds"] == "https://global/feed"
    assert pipeline._cfg(settings, {"feeds": "https://jobs/feed"})["rss_feeds"] \
        == "https://jobs/feed"
    assert pipeline._cfg(settings, {"feeds": ""})["rss_feeds"] == "https://global/feed"


if __name__ == "__main__":
    for name, fn in sorted(list(globals().items())):
        if name.startswith("test_"):
            fn()
            print("ok", name)
    print("all core checks passed")
