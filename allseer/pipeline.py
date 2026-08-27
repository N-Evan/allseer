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


def _cfg(settings):
    return {
        "days_back": db.setting_int(settings, "days_back", 3),
        "searxng_url": settings.get("searxng_url", ""),
    }


async def run_research(topic_ids=None):
    if STATUS["running"]:
        raise RuntimeError("a research run is already in progress")

    settings = db.get_settings()
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

    cfg = _cfg(settings)
    n_queries = db.setting_int(settings, "queries_per_topic", 6)
    max_fetch = db.setting_int(settings, "max_fetch", 60)
    max_llm = db.setting_int(settings, "max_llm", 35)
    n_trend = db.setting_int(settings, "top_trending", 3)
    n_niche = db.setting_int(settings, "top_niche", 5)
    provider_names = [p.strip() for p in settings.get("providers", "").split(",") if p.strip()]
    llm = llm_mod.Ollama(settings["ollama_url"], settings["ollama_model"])

    stats = {
        "topics": len(all_topics), "queries": 0, "raw_results": 0, "unique_stories": 0,
        "fetched": 0, "judged": 0, "trending": 0, "niche": 0,
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

                phase("queries for: " + topic["name"])
                try:
                    queries = await llm_mod.gen_queries(llm, topic, n_queries)
                    if not queries:
                        raise llm_mod.LLMError("no usable queries")
                except Exception as e:
                    log("query generation failed (" + str(e)[:120] + ") - using templates")
                    queries = llm_mod.fallback_queries(topic, n_queries)
                stats["queries"] += len(queries)
                for q in queries:
                    log("  q: " + q)

                phase("searching: " + topic["name"])
                results, errs = await providers.search_all(
                    client, queries, cfg, provider_names, on_event=log
                )
                stats["provider_errors"].extend(errs[:10])
                stats["raw_results"] += len(results)
                if not results:
                    log("no results for this topic - check providers/network")
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
                reps.sort(key=lambda i: rank.prefilter_score(i, topic), reverse=True)
                to_fetch = reps[:max_fetch]
                await extract.fetch_many(client, to_fetch, on_event=log)
                got = sum(1 for i in to_fetch if i.get("content_chars", 0) > 400)
                stats["fetched"] += got
                log("extracted readable text from " + str(got) + "/" + str(len(to_fetch)))

                phase("scoring with LLM: " + topic["name"])
                reps.sort(key=lambda i: rank.prefilter_score(i, topic), reverse=True)
                shortlist = reps[:max_llm]
                for n, it in enumerate(shortlist, 1):
                    log("judging " + str(n) + "/" + str(len(shortlist)) + ": "
                        + (it.get("title") or "")[:70])
                    try:
                        it.update(await llm_mod.judge(llm, it, topic))
                        stats["judged"] += 1
                    except Exception as e:
                        msg = "judge failed: " + type(e).__name__ + ": " + str(e)[:120]
                        stats["llm_errors"].append(msg)
                        log(msg)
                    rank.score(it)
                for it in reps[max_llm:]:
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
                        db.set_deep_analysis(iid, await llm_mod.deep_analyze(llm, it, topic))
                        log("analysed: " + (it.get("title") or "")[:60])
                    except Exception as e:
                        log("deep analysis failed: " + type(e).__name__ + ": " + str(e)[:120])

        STATUS["stats"] = stats
        db.finish_run(run_id, "done", stats)
        phase("done")
        log("run " + str(run_id) + " complete: " + json.dumps(
            {k: v for k, v in stats.items() if not k.endswith("errors")}))
        return stats
    except Exception as e:
        STATUS["error"] = type(e).__name__ + ": " + str(e)
        db.finish_run(run_id, "failed", stats, STATUS["error"])
        phase("failed")
        log("RUN FAILED: " + STATUS["error"])
        raise
    finally:
        STATUS["running"] = False
        STATUS["finished_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")


def start_background(topic_ids=None):
    if STATUS["running"]:
        return False
    asyncio.create_task(_guarded(topic_ids))
    return True


async def _guarded(topic_ids):
    try:
        await run_research(topic_ids)
    except Exception:
        pass  # already recorded in STATUS and the runs table
