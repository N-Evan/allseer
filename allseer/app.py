"""FastAPI app: JSON API + the static dashboard."""
import json
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse

from . import db, llm as llm_mod, pipeline

STATIC = Path(__file__).resolve().parent.parent / "static"

app = FastAPI(title="allseer")
db.init()

JSON_COLS = ("providers", "queries", "llm_facts", "llm_tags", "breakdown")


def row_to_item(r, with_content=False):
    d = dict(r)
    for c in JSON_COLS:
        try:
            d[c] = json.loads(d.get(c) or "null")
        except (TypeError, json.JSONDecodeError):
            d[c] = None
    if not with_content:
        d.pop("content", None)
    return d


@app.get("/api/state")
def state():
    con = db.connect()
    runs = [dict(r) for r in con.execute(
        "SELECT r.id, r.day, r.started_at, r.finished_at, r.status, r.stats,"
        "  (SELECT GROUP_CONCAT(x.topic_name, ' | ') FROM"
        "     (SELECT DISTINCT topic_name FROM items WHERE run_id=r.id) x) AS subjects"
        " FROM runs r ORDER BY r.id DESC LIMIT 30")]
    latest = con.execute(
        "SELECT id, day FROM runs WHERE status='done' ORDER BY id DESC LIMIT 1").fetchone()
    days = [r["day"] for r in con.execute(
        "SELECT DISTINCT day FROM runs WHERE status='done' ORDER BY day DESC LIMIT 60")]
    con.close()
    return {
        "topics": db.topics(enabled_only=False),
        "settings": db.get_settings(),
        "runs": runs,
        "days": days,
        "latest_run": dict(latest) if latest else None,
        "status": pipeline.STATUS,
    }


@app.get("/api/status")
def status():
    return pipeline.STATUS


@app.post("/api/run")
async def run(body: dict | None = None):
    """Body: {"topic_ids": [...]} for configured topics, or {"query": "..."} for a one-off
    subject that is researched now and never stored as a topic."""
    if pipeline.STATUS["running"]:
        raise HTTPException(409, "a run is already in progress")
    body = body or {}
    ids = body.get("topic_ids")
    query = " ".join(str(body.get("query") or "").split())
    if query:
        ad_hoc = {"name": query, "keywords": body.get("keywords", ""),
                  "exclusions": body.get("exclusions", "")}
        pipeline.start_background(None, ad_hoc)
        return {"started": True, "ad_hoc": query}
    if not db.topics(enabled_only=True) and not ids:
        raise HTTPException(400, "no enabled topics")
    pipeline.start_background(ids)
    return {"started": True}


@app.post("/api/stop")
def stop():
    if not pipeline.stop():
        raise HTTPException(409, "no run is in progress")
    return {"stopping": True}


@app.get("/api/results")
def results(run_id: int | None = None, day: str | None = None, topic_id: int | None = None,
            bucket: str | None = None, q: str | None = None, limit: int = 120):
    """bucket: 'trending' | 'niche' | 'all' (all = everything discovered, including unranked)."""
    con = db.connect()
    where, args = ["1=1"], []
    if run_id:
        where.append("i.run_id=?")
        args.append(run_id)
    elif day:
        where.append("r.day=?")
        args.append(day)
    elif not q:
        # A search is over your whole history; only an unsearched view defaults to
        # "the latest run", which is what made past runs unreachable.
        latest = con.execute(
            "SELECT id FROM runs WHERE status='done' ORDER BY id DESC LIMIT 1").fetchone()
        if latest:
            where.append("i.run_id=?")
            args.append(latest["id"])
    if topic_id:
        where.append("i.topic_id=?")
        args.append(topic_id)
    if bucket in ("trending", "niche"):
        where.append("i.bucket=?")
        args.append(bucket)
    elif bucket != "all":
        where.append("i.bucket!=''")
    if q:
        # LIKE '%x%' cannot use an index and scanned every row of every run. FTS5 is a
        # real index over title/snippet/summary/tags/domain; db.fts_query() makes
        # arbitrary typed text a valid MATCH expression.
        match = db.fts_query(q)
        if match:
            where.append("i.id IN (SELECT rowid FROM items_fts WHERE items_fts MATCH ?)")
            args.append(match)
    sql = (
        "SELECT i.*, r.day, f.vote FROM items i JOIN runs r ON r.id=i.run_id"
        " LEFT JOIN feedback f ON f.canon_url=i.canon_url WHERE "
        + " AND ".join(where)
        + (" ORDER BY i.run_id DESC, i.rank" if (q and not run_id and not day) else
           " ORDER BY CASE i.bucket WHEN 'trending' THEN 0 WHEN 'niche' THEN 1 ELSE 2 END,"
           " i.rank, MAX(i.trending_score, i.niche_score) DESC")
        + " LIMIT ?"
    )
    rows = con.execute(sql, args + [max(1, min(limit, 500))]).fetchall()
    con.close()
    return {"items": [row_to_item(r) for r in rows]}


@app.get("/api/item/{item_id}")
def item(item_id: int):
    con = db.connect()
    r = con.execute(
        "SELECT i.*, r.day, f.vote FROM items i JOIN runs r ON r.id=i.run_id"
        " LEFT JOIN feedback f ON f.canon_url=i.canon_url WHERE i.id=?",
        (item_id,)).fetchone()
    con.close()
    if not r:
        raise HTTPException(404, "no such item")
    d = row_to_item(r, with_content=True)
    d["content"] = (d.get("content") or "")[:8000]
    return d


@app.post("/api/topics")
def add_topic(body: dict):
    name = (body.get("name") or "").strip()
    if not name:
        raise HTTPException(400, "name required")
    con = db.connect()
    try:
        with con:
            con.execute(
                "INSERT INTO topics(name,keywords,exclusions,feeds,providers,enabled)"
                " VALUES(?,?,?,?,?,?)",
                (name, body.get("keywords", ""), body.get("exclusions", ""),
                 body.get("feeds", ""), body.get("providers", ""),
                 1 if body.get("enabled", True) else 0),
            )
    except Exception as e:
        raise HTTPException(400, str(e))
    finally:
        con.close()
    return {"ok": True}


@app.put("/api/topics/{topic_id}")
def update_topic(topic_id: int, body: dict):
    con = db.connect()
    with con:
        con.execute(
            "UPDATE topics SET name=COALESCE(?,name), keywords=COALESCE(?,keywords), "
            "exclusions=COALESCE(?,exclusions), feeds=COALESCE(?,feeds), "
            "providers=COALESCE(?,providers), enabled=COALESCE(?,enabled) WHERE id=?",
            (body.get("name"), body.get("keywords"), body.get("exclusions"),
             body.get("feeds"), body.get("providers"),
             None if "enabled" not in body else (1 if body["enabled"] else 0), topic_id),
        )
    con.close()
    return {"ok": True}


@app.delete("/api/topics/{topic_id}")
def delete_topic(topic_id: int):
    con = db.connect()
    with con:
        con.execute("DELETE FROM topics WHERE id=?", (topic_id,))
    con.close()
    return {"ok": True}


@app.post("/api/feedback")
def feedback(body: dict):
    """{"item_id": 12, "vote": 1 | -1 | 0}. Keyed by the link, not the run: the next run
    biases its fetch and inference budget with it, bans domains you keep rejecting, and
    shows the judge your recent verdicts as examples."""
    try:
        item_id = int(body.get("item_id"))
        value = int(body.get("vote"))
    except (TypeError, ValueError):
        raise HTTPException(400, "item_id and vote (1, -1 or 0) required")
    out = db.vote(item_id, value)
    if out is None:
        raise HTTPException(404, "no such item")
    return out


@app.get("/api/feedback")
def feedback_summary(topic_id: int | None = None):
    net = db.domain_votes(topic_id)
    liked, disliked = db.feedback_examples(topic_id)
    return {
        "domains": sorted(({"domain": d, "net": n} for d, n in net.items() if n),
                          key=lambda x: x["net"]),
        "liked": liked, "disliked": disliked,
    }


@app.get("/api/angles")
def angles():
    """The post writer's fixed vocabulary. Served so the UI never restates it."""
    return {
        "angles": [
            {"key": k, "name": v["name"], "min_items": v["min_items"],
             "needs_take": v["needs_take"], "blurb": v["blurb"]}
            for k, v in llm_mod.ANGLES.items()
        ],
        "lengths": llm_mod.LENGTHS,
    }


@app.post("/api/posts")
async def create_post(body: dict):
    """{"item_ids": [...], "angle": "...", "length": "...", "hashtags_on": true,
    "take": "..."} -> writes a LinkedIn draft from those items and stores it.

    Awaited inline rather than run through pipeline.STATUS: it is one LLM call, and
    keeping it off the pipeline means a post can be written during a research run."""
    angle = str(body.get("angle") or "").strip()
    a = llm_mod.ANGLES.get(angle)
    if not a:
        raise HTTPException(400, "unknown angle: " + (angle or "(none given)"))
    length = str(body.get("length") or "medium")
    if length not in llm_mod.LENGTHS:
        raise HTTPException(400, "unknown length: " + length)
    take = str(body.get("take") or "").strip()
    if a["needs_take"] and not take:
        raise HTTPException(400, "the '" + a["name"] + "' angle needs your own take - "
                                 "the take is the post")
    try:
        ids = [int(i) for i in (body.get("item_ids") or [])]
    except (TypeError, ValueError):
        raise HTTPException(400, "item_ids must be integers")
    if len(ids) < a["min_items"]:
        raise HTTPException(400, "the '" + a["name"] + "' angle needs at least "
                            + str(a["min_items"]) + " selected item(s), got " + str(len(ids)))
    items = db.items_by_ids(ids)
    if len(items) != len(ids):
        raise HTTPException(400, "one of the selected items no longer exists")
    hashtags_on = bool(body.get("hashtags_on", True))
    s = db.get_settings()
    llm = llm_mod.Ollama(s["ollama_url"], s.get("analysis_model") or s["ollama_model"])
    out = await llm_mod.write_post(llm, items, angle, length, hashtags_on, take, s)
    warn = llm_mod.lint_post(out["hooks"][0] if out["hooks"] else "",
                             out["body"], out["hashtags"], angle)
    # Hooks 2 and 3 are one radio click away, so a cliche hiding in one of them has to
    # surface as well - numbered, so it is obvious which hook not to pick.
    for n, h in enumerate(out["hooks"][1:], 2):
        warn += ["hook " + str(n) + ": " + w for w in llm_mod.lint_post(h, "", [], angle)]
    out["warnings"] = warn
    pid = db.insert_post({**out, "item_ids": ids,
                          "titles": [i.get("title") for i in items],
                          "angle": angle, "length": length,
                          "hashtags_on": hashtags_on, "take": take})
    return db.get_post(pid)


@app.get("/api/posts")
def posts(limit: int = 50):
    return {"posts": db.list_posts(limit)}


@app.put("/api/posts/{post_id}")
def save_post(post_id: int, body: dict):
    if not db.update_post(post_id, str(body.get("edited") or "")):
        raise HTTPException(404, "no such post")
    return {"ok": True}


@app.delete("/api/posts/{post_id}")
def remove_post(post_id: int):
    if not db.delete_post(post_id):
        raise HTTPException(404, "no such post")
    return {"ok": True}


@app.post("/api/settings")
def settings(body: dict):
    db.save_settings(body or {})
    return {"ok": True, "settings": db.get_settings()}


@app.get("/api/health")
async def health():
    s = db.get_settings()
    out = {"ollama": {"ok": False}, "searxng": {"configured": bool(s.get("searxng_url"))}}
    try:
        out["ollama"] = await llm_mod.Ollama(s["ollama_url"], s["ollama_model"]).health()
    except Exception as e:
        out["ollama"] = {"ok": False, "error": type(e).__name__ + ": " + str(e)[:200]}
    return out


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")


@app.exception_handler(RuntimeError)
def runtime_error(_request, exc):
    return JSONResponse({"detail": str(exc)}, status_code=400)
