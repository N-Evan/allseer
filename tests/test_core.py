"""One runnable check for the logic that isn't a one-liner: URL identity, story
clustering, the two scoring formulas, selection, and tolerant JSON parsing.

    python tests/test_core.py
"""
import asyncio
import collections
import contextlib
import shutil
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from allseer import db, extract, llm, pipeline, providers, rank
from allseer.dedupe import canon_url, cluster, jaccard, pick_representatives, title_tokens
from allseer.llm import _examples_block, parse_json, fallback_queries

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


# --- feedback, page cache, full-text search, digest, timings ---------------
# These need a database, so they get a throwaway one. db.DB_PATH is read inside
# connect(), so pointing it at a temp file redirects every call in the module.

@contextlib.contextmanager
def temp_db():
    d = tempfile.mkdtemp(prefix="allseer-test-")
    original = db.DB_PATH
    db.DB_PATH = Path(d) / "t.db"
    try:
        db.init()
        yield Path(d)
    finally:
        db.DB_PATH = original
        shutil.rmtree(d, ignore_errors=True)


def store(run_id, **kw):
    """One item in the database, with the fields these tests care about."""
    it = {"url": "https://x.com/p", "title": "t", "domain": "x.com", "topic_id": 1,
          "topic_name": "T", "snippet": "", "bucket": "", "rank": None}
    it.update(kw)
    it["canon_url"] = it.get("canon_url") or canon_url(it["url"])
    return db.insert_items(run_id, [it])[0][0]


def test_vote_bias_saturates_instead_of_taking_over():
    """One downvote is a nudge; ten must not outweigh every other signal combined."""
    item = {"domain": "spam.io"}
    assert rank.vote_bias(item, None) == 0.0
    assert rank.vote_bias(item, {}) == 0.0
    assert rank.vote_bias({"domain": "other.io"}, {"spam.io": -9}) == 0.0
    up1 = rank.vote_bias(item, {"spam.io": 1})
    up3 = rank.vote_bias(item, {"spam.io": 3})
    up30 = rank.vote_bias(item, {"spam.io": 30})
    assert 0 < up1 < up3 < up30 < 1.0, (up1, up3, up30)
    assert up30 - up3 < up3 - up1, "must saturate, not keep growing linearly"
    assert rank.vote_bias(item, {"spam.io": -3}) == -up3          # symmetric

    # and it has to actually move the triage order it feeds
    topic = {"name": "godot renderer", "keywords": "godot, renderer"}
    it = {"title": "godot renderer notes", "domain": "spam.io", "snippet": "", "url": ""}
    plain = rank.prefilter_score(it, topic)
    assert rank.prefilter_score(it, topic, {"spam.io": -30}) < plain
    assert rank.prefilter_score(it, topic, {"spam.io": 30}) > plain


def test_downvoting_a_domain_three_times_bans_it():
    net = {"junk.io": -3, "meh.io": -2, "good.io": 5}
    assert rank.banned({"domain": "junk.io"}, net, 3)
    assert not rank.banned({"domain": "meh.io"}, net, 3)
    assert not rank.banned({"domain": "good.io"}, net, 3)
    assert not rank.banned({"domain": "junk.io"}, net, 0), "0 means never ban"
    assert not rank.banned({"domain": "junk.io"}, {}, 3)


def test_a_vote_is_keyed_to_the_link_not_the_run():
    with temp_db():
        r1, r2 = db.start_run("2026-08-28"), db.start_run("2026-08-29")
        a = store(r1, url="https://blog.io/a", domain="blog.io", title="Renderer notes")
        b = store(r2, url="https://blog.io/a", domain="blog.io", title="Renderer notes")
        assert a != b, "the same link found again by a later run is a new row"

        db.vote(a, 1)
        assert db.votes_by_url()[canon_url("https://blog.io/a")] == 1
        db.vote(b, -1)
        assert db.domain_votes()["blog.io"] == -1, "one vote per link, not per row"
        db.vote(b, 0)
        assert db.domain_votes() == {}, "0 clears the vote"
        assert db.vote(10 ** 6, 1) is None, "unknown item is not a crash"


def test_votes_count_double_for_their_own_topic():
    with temp_db():
        r = db.start_run("2026-08-28")
        db.vote(store(r, url="https://a.io/1", domain="a.io", topic_id=1), -1)
        db.vote(store(r, url="https://a.io/2", domain="a.io", topic_id=2), -1)
        assert db.domain_votes()["a.io"] == -2       # no topic given: plain sum
        assert db.domain_votes(1)["a.io"] == -3      # own topic weighs double
        assert db.domain_votes(2)["a.io"] == -3


def test_the_judge_is_shown_your_recent_verdicts():
    with temp_db():
        r = db.start_run("2026-08-28")
        db.vote(store(r, url="https://a.io/1", title="Godot renderer deep dive"), 1)
        db.vote(store(r, url="https://b.io/2", title="Top 10 engines 2026"), -1)
        liked, disliked = db.feedback_examples(1)
        assert liked == ["Godot renderer deep dive"], liked
        assert disliked == ["Top 10 engines 2026"], disliked

    assert _examples_block(None) == ""
    assert _examples_block(([], [])) == "", "no votes yet = no prompt bloat"
    block = _examples_block((["good one"], ["bad one"]))
    assert "good one" in block and "bad one" in block
    assert "USEFUL" in block and "JUNK" in block


def test_search_reaches_every_run_not_just_the_latest():
    with temp_db():
        old_run, new_run = db.start_run("2026-08-01"), db.start_run("2026-08-28")
        store(old_run, url="https://a.io/1", domain="a.io", bucket="trending", rank=1,
              title="Nanite virtualized geometry teardown")
        store(new_run, url="https://b.io/2", domain="b.io", bucket="trending", rank=1,
              title="Bevy ECS scheduling rewrite")
        con = db.connect()

        def hits(text):
            return [r[0] for r in con.execute(
                "SELECT title FROM items WHERE id IN"
                " (SELECT rowid FROM items_fts WHERE items_fts MATCH ?)",
                (db.fts_query(text),))]

        assert hits("nanite") == ["Nanite virtualized geometry teardown"]
        assert hits("bevy ecs") == ["Bevy ECS scheduling rewrite"]
        assert hits("geometr") == ["Nanite virtualized geometry teardown"], "prefix search"
        assert hits("nanite bevy") == [], "extra words narrow, they do not widen"
        assert hits("a.io") == ["Nanite virtualized geometry teardown"], "domain is indexed"
        # typed text is not FTS5 syntax; none of this may raise
        for hostile in ["c++", "!!!", "NEAR(", "a AND", '" OR 1=1 --', "-"]:
            con.execute("SELECT rowid FROM items_fts WHERE items_fts MATCH ?",
                        (db.fts_query(hostile) or '"zz"',)).fetchall()
        con.close()
    assert db.fts_query("   ") == "", "a blank search must not become match-all"


def test_the_index_backfills_for_a_database_that_predates_it():
    with temp_db():
        r = db.start_run("2026-08-28")
        store(r, url="https://a.io/1", title="Nanite geometry teardown")
        con = db.connect()
        with con:
            con.execute("DROP TABLE items_fts")     # a database from before the index
        con.close()
        db.init()
        con = db.connect()
        n = con.execute("SELECT count(*) FROM items_fts WHERE items_fts MATCH ?",
                        (db.fts_query("nanite"),)).fetchone()[0]
        con.close()
        assert n == 1, "existing rows must be indexed on startup, not only new ones"


def test_a_cached_page_is_not_downloaded_twice():
    with temp_db():
        assert db.cache_pages([{"canon_url": "https://a.io/p", "content": "body text",
                                "published_at": "2026-08-27", "author": "Ada"}]) == 1
        got = db.cached_pages(["https://a.io/p", "https://b.io/q"], 14)
        assert set(got) == {"https://a.io/p"}
        assert got["https://a.io/p"]["content"] == "body text"
        assert db.cached_pages(["https://a.io/p"], 0) == {}, "0 days = cache off"

        con = db.connect()
        with con:
            con.execute("UPDATE page_cache SET fetched_at=datetime('now','-30 days')")
        con.close()
        assert db.cached_pages(["https://a.io/p"], 14) == {}, "stale entries expire"

        # and the fetcher has to actually skip what it already has
        calls = []

        class FakeResponse:
            status_code = 200
            headers = {"content-type": "text/html"}
            text = "<html></html>"

        class FakeClient:
            async def get(self, url, **kw):
                calls.append(url)
                return FakeResponse()

        db.cache_pages([{"canon_url": "https://a.io/p", "content": "cached body",
                         "published_at": None, "author": None}])
        items = [{"url": "https://a.io/p", "canon_url": "https://a.io/p", "domain": "a.io"},
                 {"url": "https://c.io/r", "canon_url": "https://c.io/r", "domain": "c.io"}]
        out = asyncio.run(extract.fetch_many(FakeClient(), items, cache_days=14))
        assert calls == ["https://c.io/r"], calls
        assert len(out) == 2, "every item comes back, cached or freshly fetched"
        assert items[0]["content"] == "cached body"
        assert items[0]["fetch_note"] == "cached"


def test_the_digest_lists_every_promoted_item_and_nothing_else():
    with temp_db() as root:
        r = db.start_run("2026-08-28")
        store(r, url="https://a.io/1", domain="a.io", title="Bevy ECS rewrite",
              bucket="trending", rank=1, llm_summary="They rewrote the scheduler.",
              llm_why="Signals a stable 1.0.")
        store(r, url="https://b.io/2", domain="b.io", title="A one-person shader blog",
              bucket="niche", rank=1)
        store(r, url="https://c.io/3", domain="c.io", title="Not shortlisted")

        assert pipeline.write_digest(r, "2026-08-28", "") is None, "blank dir = off"
        path = pipeline.write_digest(r, "2026-08-28", str(root / "digests"))
        text = path.read_text(encoding="utf-8")
        assert path.name == "2026-08-28-run" + str(r) + ".md"
        assert "Bevy ECS rewrite" in text and "https://a.io/1" in text
        assert "A one-person shader blog" in text
        assert "Not shortlisted" not in text, "only promoted items go in the digest"
        assert "Top stories" in text and "Niche finds" in text
        assert "They rewrote the scheduler." in text and "Signals a stable 1.0." in text
        assert pipeline.write_digest(db.start_run("2026-08-29"), "2026-08-29",
                                     str(root / "digests")) is None, "empty run, no file"


def test_stage_timings_accumulate_across_topics():
    stats = {}
    for _ in range(2):
        with pipeline.timed(stats, "judge"):
            pass
    with pipeline.timed(stats, "fetch"):
        pass
    assert set(stats["secs"]) == {"judge", "fetch"}
    assert all(v >= 0 for v in stats["secs"].values())
    # a stage that raises still records its time instead of losing the run's stats
    try:
        with pipeline.timed(stats, "search"):
            raise ValueError("boom")
    except ValueError:
        pass
    assert "search" in stats["secs"]


# --- LinkedIn post writer -------------------------------------------------

CLEAN_BODY = ("Godot 4.4 lands its rewritten navigation server, per the engine blog.\n\n"
              "Pathfinding now runs off the main thread, which is the part that mattered "
              "for anyone shipping large levels.\n\n"
              "The trade is a migration: the old NavigationServer calls are gone.")


def test_angles_and_lengths_are_well_formed():
    assert len(llm.ANGLES) >= 9
    for key, a in llm.ANGLES.items():
        assert key == key.lower() and " " not in key, key
        for field in ("name", "how", "blurb", "min_items", "needs_take"):
            assert a.get(field) not in (None, ""), (key, field)
        assert a["min_items"] >= 1
        assert isinstance(a["needs_take"], bool)
    assert llm.ANGLES["synthesis"]["min_items"] == 2, "synthesis is the multi-item angle"
    assert llm.ANGLES["field-notes"]["needs_take"] is True, "field notes IS the take"
    # the three the UI offers, and no others
    assert set(llm.LENGTHS) == {"short", "medium", "long"}
    assert llm.LENGTHS["short"] < llm.LENGTHS["medium"] < llm.LENGTHS["long"]


def test_lint_post_flags_what_makes_a_post_look_generated():
    def w(hook="A real hook.", body=CLEAN_BODY, tags=("Godot",), angle="signal"):
        return " | ".join(llm.lint_post(hook, body, list(tags), angle))

    assert "cliche" in w(body="This is huge. " + CLEAN_BODY)
    assert "cliche" in w(hook="Game changer.")
    assert "emoji" in w(hook="We shipped it \U0001f680")
    assert "chars" in w(hook="x" * 300)
    assert "hashtags" in w(tags=("a", "b", "c", "d", "e", "f", "g"))
    assert "question" in w(body=CLEAN_BODY + "\n\nThoughts?")
    # the same closing question is correct for the one angle that asks for it
    assert "question" not in w(body=CLEAN_BODY + "\n\nThoughts?", angle="ask")
    assert "wall of text" in w(body=" ".join(["word"] * 200))
    # an arrow is technical writing, not engagement bait
    assert w(body=CLEAN_BODY.replace("The trade is", "old -> new. The trade is")) == ""


def test_lint_post_passes_a_clean_draft():
    assert llm.lint_post("Godot 4.4 moved pathfinding off the main thread.",
                         CLEAN_BODY, ["Godot", "GameDev", "Navigation"], "signal") == []


def test_post_prompt_omits_empty_persona_fields():
    item = {"title": "T", "domain": "d.io", "url": "https://d.io/a",
            "llm_facts": ["fact one"], "llm_summary": "s", "content": "x" * 20000}
    bare = llm.build_post_user([item], "signal", settings={})
    assert "WHO IS WRITING" not in bare and "STYLE SAMPLE" not in bare
    assert "(none given)" in bare, "an absent take must be stated, not implied"
    full = llm.build_post_user([item], "signal", take="I shipped this", settings={
        "persona_role": "Gameplay engineer", "persona_voice": "dry",
        "persona_sample": "I ship things.", "persona_audience": ""})
    assert "Role: Gameplay engineer" in full and "Voice: dry" in full
    assert "I ship things." in full and "I shipped this" in full
    assert "Writing for" not in full, "an empty persona field must not reach the prompt"


def test_post_prompt_budget_shrinks_with_more_sources():
    item = {"title": "T", "domain": "d.io", "url": "https://d.io/a",
            "llm_facts": [], "llm_summary": "s", "content": "x" * 20000}
    one = llm.build_post_user([item], "signal", settings={})
    five = llm.build_post_user([item] * 5, "synthesis", settings={})
    assert five.count("SOURCE ") == 5
    # four extra sources must not multiply the prompt: they share one text budget
    assert len(five) < len(one) + 2000, (len(one), len(five))


def test_parse_json_reads_a_fenced_post_response():
    d = parse_json('```json\n{"hooks": ["a", "b", "c"], "body": "text",\n'
                   ' "hashtags": ["Godot"], "first_comment": "link"}\n```')
    assert d["hooks"] == ["a", "b", "c"] and d["body"] == "text"


def test_posts_table_round_trip():
    with temp_db():
        pid = db.insert_post({"item_ids": [3, 4], "titles": ["A", "B"], "angle": "synthesis",
                              "length": "medium", "hashtags_on": True, "take": "my angle",
                              "hooks": ["h1", "h2"], "body": "body text",
                              "hashtags": ["Godot"], "first_comment": "link",
                              "warnings": ["cliche"], "model": "qwen2.5:14b"})
        p = db.get_post(pid)
        assert p["item_ids"] == [3, 4] and p["titles"] == ["A", "B"]
        assert p["hooks"] == ["h1", "h2"] and p["warnings"] == ["cliche"]
        assert p["edited"] == "", "a fresh draft has no edit yet"
        assert [x["id"] for x in db.list_posts()] == [pid]

        assert db.update_post(pid, "my edited version") is True
        assert db.get_post(pid)["edited"] == "my edited version"
        assert db.update_post(pid + 99, "x") is False

        assert db.delete_post(pid) is True
        assert db.get_post(pid) is None and db.list_posts() == []
        assert db.delete_post(pid) is False


def test_items_by_ids_keeps_order_and_decodes_facts():
    with temp_db():
        r = db.start_run("2026-08-28")
        ids = [i for i, _ in db.insert_items(r, [
            {"canon_url": "https://a.io/1", "title": "First", "llm_facts": ["f1", "f2"]},
            {"canon_url": "https://b.io/2", "title": "Second", "llm_facts": []},
        ])]
        got = db.items_by_ids([ids[1], ids[0]])
        assert [g["title"] for g in got] == ["Second", "First"], "order follows the request"
        assert got[1]["llm_facts"] == ["f1", "f2"], "facts arrive as a list, not JSON text"
        # a stale selection comes back short, which is how the API detects it
        assert len(db.items_by_ids([ids[0], 999999])) == 1


def test_post_api_rejects_impossible_requests():
    with temp_db():
        from fastapi.testclient import TestClient

        from allseer import app as app_mod
        c = TestClient(app_mod.app)

        r = db.start_run("2026-08-28")
        [(iid, _)] = db.insert_items(r, [{"canon_url": "https://a.io/1", "title": "One"}])

        def post(**kw):
            body = {"item_ids": [iid], "angle": "signal", "length": "medium"}
            body.update(kw)
            return c.post("/api/posts", json=body)

        assert post(angle="nonsense").status_code == 400
        assert post(angle="").status_code == 400
        assert post(length="epic").status_code == 400
        assert post(angle="synthesis").status_code == 400, "synthesis needs 2+ items"
        assert post(angle="field-notes").status_code == 400, "field notes needs a take"
        assert post(item_ids=[]).status_code == 400
        assert post(item_ids=[999999]).status_code == 400, "stale item id"
        assert post(item_ids=["not-an-int"]).status_code == 400

        # the angle list the UI builds itself from
        a = c.get("/api/angles").json()
        assert {x["key"] for x in a["angles"]} == set(llm.ANGLES)
        assert a["lengths"] == llm.LENGTHS
        assert c.get("/api/posts").json() == {"posts": []}
        assert c.put("/api/posts/1", json={"edited": "x"}).status_code == 404
        assert c.delete("/api/posts/1").status_code == 404


def test_every_hook_is_linted_not_only_the_first():
    """A cliche in hook 3 is one radio click from being published, so it has to be
    flagged too - and named by number, or you cannot tell which hook to skip."""
    hooks = ["A clean, specific opening line.", "Check it out!", "We shipped it \U0001f680"]
    warn = llm.lint_post(hooks[0], CLEAN_BODY, ["Godot"], "signal")
    for n, h in enumerate(hooks[1:], 2):
        warn += ["hook " + str(n) + ": " + w for w in llm.lint_post(h, "", [], "signal")]
    joined = " | ".join(warn)
    assert "hook 2: cliche" in joined, joined
    assert "hook 3: contains emoji" in joined, joined
    assert not any(w.startswith("hook 1") for w in warn), "hook 1 is reported unprefixed"


def test_post_api_lints_and_stores_the_draft():
    """The endpoint's own wiring: lint every hook, denormalise the titles, persist."""
    with temp_db():
        from fastapi.testclient import TestClient

        from allseer import app as app_mod
        c = TestClient(app_mod.app)
        r = db.start_run("2026-08-28")
        [(iid, _)] = db.insert_items(r, [{"canon_url": "https://a.io/1", "title": "One"}])

        async def fake_write_post(*a, **kw):
            return {"hooks": ["A clean, specific opening line.", "Check it out!"],
                    "body": CLEAN_BODY, "hashtags": ["Godot"],
                    "first_comment": "https://a.io/1 - worth reading", "model": "test"}

        original = app_mod.llm_mod.write_post
        app_mod.llm_mod.write_post = fake_write_post
        try:
            p = c.post("/api/posts", json={"item_ids": [iid], "angle": "signal",
                                           "length": "medium"}).json()
        finally:
            app_mod.llm_mod.write_post = original

        assert p["warnings"] == ['hook 2: cliche: "check it out"'], p["warnings"]
        assert p["titles"] == ["One"], "the title is copied in, so a deleted item is survivable"
        assert p["item_ids"] == [iid]
        assert db.get_post(p["id"])["hooks"] == p["hooks"], "the draft is stored, not just returned"


if __name__ == "__main__":
    for name, fn in sorted(list(globals().items())):
        if name.startswith("test_"):
            fn()
            print("ok", name)
    print("all core checks passed")
