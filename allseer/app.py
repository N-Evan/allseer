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
    else:
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
        where.append("(i.title LIKE ? OR i.llm_summary LIKE ? OR i.llm_why LIKE ? "
                     "OR i.domain LIKE ? OR i.llm_tags LIKE ?)")
        args += ["%" + q + "%"] * 5
    sql = (
        "SELECT i.*, r.day FROM items i JOIN runs r ON r.id=i.run_id WHERE "
        + " AND ".join(where)
        + " ORDER BY CASE i.bucket WHEN 'trending' THEN 0 WHEN 'niche' THEN 1 ELSE 2 END,"
          " i.rank, MAX(i.trending_score, i.niche_score) DESC LIMIT ?"
    )
    rows = con.execute(sql, args + [max(1, min(limit, 500))]).fetchall()
    con.close()
    return {"items": [row_to_item(r) for r in rows]}


@app.get("/api/item/{item_id}")
def item(item_id: int):
    con = db.connect()
    r = con.execute(
        "SELECT i.*, r.day FROM items i JOIN runs r ON r.id=i.run_id WHERE i.id=?",
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
                "INSERT INTO topics(name,keywords,exclusions,enabled) VALUES(?,?,?,?)",
                (name, body.get("keywords", ""), body.get("exclusions", ""),
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
            "exclusions=COALESCE(?,exclusions), enabled=COALESCE(?,enabled) WHERE id=?",
            (body.get("name"), body.get("keywords"), body.get("exclusions"),
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
