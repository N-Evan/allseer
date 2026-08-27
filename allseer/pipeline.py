"""The research run: queries -> search -> fetch -> dedupe -> judge -> rank -> store.

One run handles every selected topic in sequence. Any single step failing degrades the
run (fewer/weaker results) instead of killing it.
"""
import asyncio
import json
from datetime import datetime, timezone

import httpx

from . import db, extract, llm as llm_mod, providers, rank
from .dedupe import cluster, pick_representatives

STATUS = {
    "running": False,
    "run_id": None,
    "phase": "idle",
    "topic": None,
    "started_at": None,
    "finished_at": None,
    "log": [],
    "stats": {},
    "error": None,
}


def log(msg):
    stamp = datetime.now().strftime("%H:%M:%S")
    STATUS["log"].append(stamp + "  " + str(msg))
    del STATUS["log"][:-250]
    print("[allseer]", msg, flush=True)


def phase(name):
    STATUS["phase"] = name
    log("== " + name)


def _cfg(settings, topic=None):
    # A topic may pin its own feed list. Job boards must not answer gamedev queries and
    # gamedev feeds must not answer job queries, and rss_feeds is a single global setting.
    feeds = (topic or {}).get("feeds") or settings.get("rss_feeds", "")
    return {
        "days_back": db.setting_int(settings, "days_back", 3),
        "searxng_url": settings.get("searxng_url", ""),
        "rss_feeds": feeds,
        "github_min_stars": db.setting_int(settings, "github_min_stars", 5),
    }


async def run_research(topic_ids=None, ad_hoc=None):
    """ad_hoc: {"name": "<what to research>", "keywords": ..., "exclusions": ...} - a
    one-off subject that is never stored as a topic. Its items get topic_id NULL."""
    if STATUS["running"]:
        raise RuntimeError("a research run is already in progress")

    settings = db.get_settings()
    if ad_hoc:
        query = " ".join(str(ad_hoc.get("name") or "").split())
        if not query:
            raise RuntimeError("nothing to research - type a subject or keywords")
        all_topics = [{"id": None, "name": query,
                       "keywords": ad_hoc.get("keywords") or "",
                       "exclusions": ad_hoc.get("exclusions") or "",
                       "ad_hoc": True}]
    else:
        all_topics = db.topics(enabled_only=True)
        if topic_ids:
            wanted = set(int(t) for t in topic_ids)
            all_topics = [t for t in db.topics(enabled_only=False) if t["id"] in wanted]
        if not all_topics:
            raise RuntimeError("no enabled topics - add one in Settings")

    day = datetime.now(timezone.utc).date().isoformat()
    run_id = db.start_run(day)
    STATUS.update(
        running=True, run_id=run_id, phase="starting", topic=None, error=None,
        started_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        finished_at=None, log=[], stats={},
    )

    n_queries = db.setting_int(settings, "queries_per_topic", 6)
    max_fetch = db.setting_int(settings, "max_fetch", 60)
    max_llm = db.setting_int(settings, "max_llm", 35)
    n_trend = db.setting_int(settings, "top_trending", 3)
    n_niche = db.setting_int(settings, "top_niche", 5)
    provider_names = [p.strip() for p in settings.get("providers", "").split(",") if p.strip()]
    drop_undated = db.setting_int(settings, "drop_undated", 0) == 1
    seen_days = db.setting_int(settings, "suppress_seen_days", 21)
    seen = db.seen_canon_urls(seen_days)
    if seen:
        log("suppressing " + str(len(seen)) + " links already ranked in the last "
            + str(seen_days) + " days")
    llm = llm_mod.Ollama(settings["ollama_url"], settings["ollama_model"])
    # Query generation is cheap and forgiving; judging and the analyst note are where a
    # better model actually shows. Empty analysis_model means "same model for both".
    analyst = llm_mod.Ollama(settings["ollama_url"],
                             settings.get("analysis_model") or settings["ollama_model"])
    if analyst.model != llm.model:
        log("analysis model: " + analyst.model + " (queries: " + llm.model + ")")

    stats = {
        "topics": len(all_topics), "queries": 0, "raw_results": 0, "unique_stories": 0,
        "fetched": 0, "judged": 0, "trending": 0, "niche": 0,
        "dropped_stale": 0, "dropped_seen": 0,
        "dropped_offtopic": 0, "dropped_excluded": 0,
        "provider_errors": [], "llm_errors": [],
    }
    headers = {"User-Agent": settings.get("user_agent") or "allseer/0.1", "Accept-Language": "en"}

    try:
        async with httpx.AsyncClient(timeout=20.0, headers=headers, follow_redirects=True) as client:
            try:
                h = await llm.health()
                log("ollama ok, model present: " + str(h["model_present"]))
                if not h["model_present"]:
                    log("WARNING: model '" + llm.model + "' not pulled. Available: "
                        + ", ".join(h["models"][:8]))
            except Exception as e:
                log("WARNING: ollama unreachable (" + type(e).__name__ + "). "
                    "Falling back to template queries; items will not be scored.")

            for topic in all_topics:
                STATUS["topic"] = topic["name"]
                cfg = _cfg(settings, topic)

                phase("queries for: " + topic["name"])
                if topic.get("feeds"):
                    # A feed-pinned topic wants the same terms searched every day, not a
                    # fresh set of LLM angles. Job listings are titled "Senior Unity
                    # Developer (Remote)" and only match a query that literally says
                    # "unity developer"; an angle like "remote gameplay hiring trends"
                    # matches nothing in a feed. The keywords ARE the queries.
                    queries = [k.strip() for k in (topic.get("keywords") or "").split(",")
                               if k.strip()][:n_queries * 2]
                    if not queries:
                        queries = [topic["name"]]
                else:
                    try:
                        queries = await llm_mod.gen_queries(llm, topic, n_queries)
                        if not queries:
                            raise llm_mod.LLMError("no usable queries")
                    except Exception as e:
                        log("query generation failed (" + str(e)[:120] + ") - using templates")
                        queries = llm_mod.fallback_queries(topic, n_queries)
                if topic.get("ad_hoc"):
                    # Search what was actually typed, then the generated angles around it.
                    exact = topic["name"]
                    queries = [exact] + [q for q in queries if q.lower() != exact.lower()]
                    queries = queries[:n_queries]
                stats["queries"] += len(queries)
                for q in queries:
                    log("  q: " + q)

                phase("searching: " + topic["name"])
                names = [p.strip() for p in (topic.get("providers") or "").split(",")
                         if p.strip()] or provider_names
                results, errs = await providers.search_all(
                    client, queries, cfg, names, on_event=log
                )
                stats["provider_errors"].extend(errs[:10])
                stats["raw_results"] += len(results)

                # days_back used to be a hint each provider honoured differently (GitHub
                # not at all), so 2014 repos reached a 3-day run. Enforce it here, once.
                n_before = len(results)
                results = [r for r in results
                           if rank.fresh_enough(r, cfg["days_back"], drop_undated)]
                dropped = n_before - len(results)
                stats["dropped_stale"] += dropped
                if dropped:
                    log("dropped " + str(dropped) + "/" + str(n_before)
                        + " results older than " + str(cfg["days_back"]) + " days")

                if seen:
                    n_before = len(results)
                    results = [r for r in results if r.get("canon_url") not in seen]
                    stats["dropped_seen"] += n_before - len(results)
                    if n_before - len(results):
                        log("dropped " + str(n_before - len(results))
                            + " already shown in a recent run")

                # Relevance was only ever checked by the LLM, i.e. after the fetch and
                # the inference had already been paid for. Two cheap lexical gates first.
                n_before = len(results)
                results = [r for r in results if not rank.excluded(r, topic)]
                stats["dropped_excluded"] += n_before - len(results)
                if n_before - len(results):
                    log("dropped " + str(n_before - len(results))
                        + " matching this topic's exclusions")

                n_before = len(results)
                results = [r for r in results if rank.on_topic(r, topic)]
                stats["dropped_offtopic"] += n_before - len(results)
                if n_before - len(results):
                    log("dropped " + str(n_before - len(results))
                        + " carrying no real topic vocabulary")

                if not results:
                    log("no fresh results for this topic - widen days_back, "
                        "lower suppress_seen_days, or check providers/network")
                    continue

                phase("deduplicating: " + topic["name"])
                for r in results:
                    r["topic_id"] = topic["id"]
                    r["topic_name"] = topic["name"]
                cluster(results)
                reps = pick_representatives(results)
                stats["unique_stories"] += len(reps)
                log(str(len(results)) + " results -> " + str(len(reps)) + " distinct stories")

                phase("fetching content: " + topic["name"])
                to_fetch = rank.diversify(
                    reps, lambda i: rank.prefilter_score(i, topic), max_fetch)
                await extract.fetch_many(client, to_fetch, on_event=log)
                got = sum(1 for i in to_fetch if i.get("content_chars", 0) > 400)
                stats["fetched"] += got
                log("extracted readable text from " + str(got) + "/" + str(len(to_fetch)))

                # trafilatura fills published_at for pages the search API left undated, so
                # some items only reveal they are ancient after the fetch. Re-check.
                n_before = len(reps)
                reps = [r for r in reps if rank.fresh_enough(r, cfg["days_back"], drop_undated)]
                if n_before - len(reps):
                    stats["dropped_stale"] += n_before - len(reps)
                    log("dropped " + str(n_before - len(reps))
                        + " more after the page revealed its real date")
                if not reps:
                    continue

                phase("scoring with LLM: " + topic["name"])
                # Judge only what we actually tried to fetch: the post-fetch freshness
                # re-gate reshuffles reps, and an unfetched item would be judged on its
                # search snippet alone.
                tried = {id(i) for i in to_fetch}
                pool = [r for r in reps if id(r) in tried] or reps
                shortlist = rank.diversify(
                    pool, lambda i: rank.prefilter_score(i, topic), max_llm)
                log("judging " + str(len(shortlist)) + " items from "
                    + str(len({i.get("domain") for i in shortlist})) + " domains")
                for n, it in enumerate(shortlist, 1):
                    log("judging " + str(n) + "/" + str(len(shortlist)) + ": "
                        + (it.get("title") or "")[:70])
                    try:
                        it.update(await llm_mod.judge(analyst, it, topic))
                        stats["judged"] += 1
                    except Exception as e:
                        msg = "judge failed: " + type(e).__name__ + ": " + str(e)[:120]
                        stats["llm_errors"].append(msg)
                        log(msg)
                    rank.score(it)
                judged = {id(i) for i in shortlist}
                for it in reps:
                    if id(it) not in judged:
                        rank.score(it)

                phase("ranking: " + topic["name"])
                trending, niche = rank.select(shortlist, n_trend, n_niche)
                stats["trending"] += len(trending)
                stats["niche"] += len(niche)
                for it in trending:
                    log("TRENDING " + str(it["rank"]) + ": " + (it.get("title") or "")[:80])
                for it in niche:
                    log("NICHE " + str(it["rank"]) + ": " + (it.get("title") or "")[:80])

                inserted = db.insert_items(run_id, reps)
                id_by_url = {it.get("canon_url"): iid for iid, it in inserted}

                phase("deep analysis: " + topic["name"])
                for it in trending + niche:
                    iid = id_by_url.get(it.get("canon_url"))
                    if not iid:
                        continue
                    try:
                        db.set_deep_analysis(iid, await llm_mod.deep_analyze(analyst, it, topic))
                        log("analysed: " + (it.get("title") or "")[:60])
                    except Exception as e:
                        log("deep analysis failed: " + type(e).__name__ + ": " + str(e)[:120])

        STATUS["stats"] = stats
        db.finish_run(run_id, "done", stats)
        phase("done")
        log("run " + str(run_id) + " complete: " + json.dumps(
            {k: v for k, v in stats.items() if not k.endswith("errors")}))
        return stats
    except asyncio.CancelledError:
        STATUS["stats"] = stats
        db.finish_run(run_id, "cancelled", stats, "stopped by user")
        phase("cancelled")
        log("run " + str(run_id) + " stopped by user. Anything already stored stays.")
        raise
    except Exception as e:
        STATUS["error"] = type(e).__name__ + ": " + str(e)
        db.finish_run(run_id, "failed", stats, STATUS["error"])
        phase("failed")
        log("RUN FAILED: " + STATUS["error"])
        raise
    finally:
        STATUS["running"] = False
        STATUS["finished_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")


_TASK = None


def start_background(topic_ids=None, ad_hoc=None):
    global _TASK
    if STATUS["running"]:
        return False
    _TASK = asyncio.create_task(_guarded(topic_ids, ad_hoc))
    return True


def stop():
    """Cancel the in-flight run. Cancelling the task interrupts whatever it is awaiting
    (a search, a page fetch, an Ollama call), so this takes effect immediately."""
    if _TASK is None or _TASK.done():
        return False
    _TASK.cancel()
    log("stop requested")
    return True


async def _guarded(topic_ids, ad_hoc=None):
    try:
        await run_research(topic_ids, ad_hoc)
    except (Exception, asyncio.CancelledError):
        pass  # already recorded in STATUS and the runs table
