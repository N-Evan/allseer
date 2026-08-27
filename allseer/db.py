"""SQLite storage. One flat items table on purpose: every run writes rows, nothing joins."""
import json
import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent.parent / "allseer.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS topics (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL UNIQUE,
  keywords TEXT DEFAULT '',
  exclusions TEXT DEFAULT '',
  feeds TEXT DEFAULT '',
  providers TEXT DEFAULT '',
  enabled INTEGER DEFAULT 1,
  created_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  started_at TEXT DEFAULT (datetime('now')),
  finished_at TEXT,
  day TEXT,
  status TEXT DEFAULT 'running',
  stats TEXT DEFAULT '{}',
  error TEXT
);

CREATE TABLE IF NOT EXISTS items (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id INTEGER NOT NULL,
  topic_id INTEGER,
  topic_name TEXT,
  url TEXT,
  canon_url TEXT,
  domain TEXT,
  title TEXT,
  author TEXT,
  published_at TEXT,
  source_type TEXT,
  snippet TEXT,
  content TEXT,
  content_chars INTEGER DEFAULT 0,
  providers TEXT DEFAULT '[]',
  queries TEXT DEFAULT '[]',
  cluster_id INTEGER,
  cluster_domains INTEGER DEFAULT 1,
  discussion INTEGER DEFAULT 0,
  llm_relevance REAL, llm_novelty REAL, llm_depth REAL, llm_importance REAL,
  llm_offtopic INTEGER DEFAULT 0,
  llm_summary TEXT, llm_why TEXT, llm_facts TEXT, llm_tags TEXT,
  llm_model TEXT,
  trending_score REAL DEFAULT 0,
  niche_score REAL DEFAULT 0,
  breakdown TEXT DEFAULT '{}',
  bucket TEXT DEFAULT '',
  rank INTEGER,
  deep_analysis TEXT,
  created_at TEXT DEFAULT (datetime('now')),
  UNIQUE(run_id, canon_url)
);

CREATE INDEX IF NOT EXISTS idx_items_run ON items(run_id);
CREATE INDEX IF NOT EXISTS idx_items_bucket ON items(bucket, trending_score, niche_score);

CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT);

-- Your verdict on a link, keyed by canon_url so it survives the run that found it.
-- One vote per link: voting again overwrites.
CREATE TABLE IF NOT EXISTS feedback (
  canon_url TEXT PRIMARY KEY,
  domain TEXT,
  topic_id INTEGER,
  title TEXT,
  vote INTEGER NOT NULL,
  created_at TEXT DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_feedback_domain ON feedback(domain, vote);

-- Extracted page text, reused across runs. max_fetch is 40 and only 8 items get promoted,
-- so most of every fetch budget is spent re-downloading pages a previous run already read.
CREATE TABLE IF NOT EXISTS page_cache (
  canon_url TEXT PRIMARY KEY,
  content TEXT,
  published_at TEXT,
  author TEXT,
  fetched_at TEXT DEFAULT (datetime('now'))
);

-- Full-text index over every run, so history is searchable. External-content table: the
-- rows live in items, this only holds the index. Rebuilt from items when empty.
CREATE VIRTUAL TABLE IF NOT EXISTS items_fts USING fts5(
  title, snippet, llm_summary, llm_tags, domain,
  content='items', content_rowid='id', tokenize='porter unicode61'
);
-- Only INSERT and DELETE: insert_items writes every indexed column at once, and the one
-- later UPDATE (set_deep_analysis) touches a column that is not indexed.
CREATE TRIGGER IF NOT EXISTS items_fts_ai AFTER INSERT ON items BEGIN
  INSERT INTO items_fts(rowid, title, snippet, llm_summary, llm_tags, domain)
  VALUES (new.id, new.title, new.snippet, new.llm_summary, new.llm_tags, new.domain);
END;
CREATE TRIGGER IF NOT EXISTS items_fts_ad AFTER DELETE ON items BEGIN
  INSERT INTO items_fts(items_fts, rowid, title, snippet, llm_summary, llm_tags, domain)
  VALUES ('delete', old.id, old.title, old.snippet, old.llm_summary, old.llm_tags, old.domain);
END;
"""

# Feeds beat search APIs for gamedev/devlog material: they are complete, dated and free.
DEFAULT_FEEDS = [
    # game dev / art / industry
    "https://80.lv/feed",
    "https://www.gamedeveloper.com/rss.xml",
    "https://www.gamesindustry.biz/feed",
    "https://godotengine.org/rss.xml",
    "https://itch.io/blog.rss",
    # AI / engineering
    "https://huggingface.co/blog/feed.xml",
    "https://simonwillison.net/atom/everything/",
    # reddit, via feeds rather than the rate-limited search API
    "https://www.reddit.com/r/gamedev/top.rss?t=day",
    "https://www.reddit.com/r/godot/top.rss?t=day",
    "https://www.reddit.com/r/LocalLLaMA/top.rss?t=day",
    "https://www.reddit.com/r/IndieDev/top.rss?t=day",
    "https://www.reddit.com/r/leveldesign/top.rss?t=week",
    "https://www.reddit.com/r/gamedesign/top.rss?t=week",
]

# Job boards, kept off the global rss_feeds list and pinned to the jobs topic instead:
# a "gameplay programming" query must never match a job posting, and a role query must
# never match a devlog. Verified live 2026-08-28 (parse + fresh dated entries):
# weworkremotely 89, himalayas 100, jobicy 200, remotive 20, hnrss/jobs 20.
# remoteok.com, workingnomads and gamesindustry.biz/jobs all serve malformed XML - do not
# re-add them without checking parse_feed() handles the body.
JOB_FEEDS = [
    "https://weworkremotely.com/remote-jobs.rss",
    "https://weworkremotely.com/categories/remote-programming-jobs.rss",
    "https://himalayas.app/jobs/rss",
    "https://remotive.com/remote-jobs/feed",
    "https://jobicy.com/?feed=job_feed",
    "https://hnrss.org/jobs",
    "https://hnrss.org/whoishiring/jobs",
    # the hunting-strategy half of the topic: craft, not listings
    "https://newsletter.pragmaticengineer.com/feed",
    "https://www.reddit.com/r/cscareerquestions/top.rss?t=week",
    "https://www.reddit.com/r/gamedevjobs/top.rss?t=week",
    "https://www.reddit.com/r/experienceddevs/top.rss?t=week",
]

DEFAULT_SETTINGS = {
    "ollama_url": "http://localhost:11434",
    "ollama_model": "qwen2.5:14b",
    "analysis_model": "",       # model for judging + analyst notes; empty = ollama_model
    "searxng_url": "",  # e.g. http://localhost:8080 - optional, other providers work without it
    "queries_per_topic": "6",
    "max_fetch": "40",
    "max_llm": "20",
    "top_trending": "3",
    "top_niche": "5",
    "days_back": "3",
    "drop_undated": "0",        # 1 = an item with no publish date is discarded, not kept
    "suppress_seen_days": "21", # skip anything already ranked in a run this recent; 0 = off
    "github_min_stars": "5",    # a repo this new needs some traction to be worth a slot; 0 = off
    "page_cache_days": "14",    # reuse extracted page text this recent instead of refetching; 0 = off
    "dislike_drop": "3",        # net downvotes that ban a domain outright; 0 = never ban
    "digest_dir": "digests",    # markdown digest written after every run; blank = off
    "providers": "hn,reddit,github,arxiv,searxng,rss",
    "rss_feeds": " ".join(DEFAULT_FEEDS),
    "user_agent": "allseer/0.1 (personal research agent)",
}

# Exclusions shared by every topic: the noise that follows any tech query around.
_NOISE = ("crypto, nft, token price, stock price, funding round, acquisition, layoffs, "
          "hiring, job posting, salary, bootcamp, course, tutorial roundup, coupon, "
          "giveaway, discount, sale, tier list, top 10 list")

# Game *coverage* is not game *craft*. A GTA 6 teaser took a niche slot in a level-design
# run before this existed; every game topic needs it, none of the AI/engineering ones do.
_GAME_NOISE = (", trailer, teaser, cinematic reveal, release date, delayed to, "
               "launch announcement, netflix adaptation, review score, metacritic, "
               "sales figures, player count, esports, tournament, celebrity voice cast, "
               "leak, datamine, fan theory")

SEED_TOPICS = [
    ("Local AI & Inference",
     "local llm, on-device inference, ollama, llama.cpp, gguf, quantization, vllm, sglang, "
     "mlx, exllama, lm studio, kv cache, speculative decoding, fine-tuning lora, "
     "open weights model release, small language model",
     _NOISE + ", api pricing, benchmark leaderboard drama"),

    ("Agentic Systems & Harnesses",
     "agentic, agent harness, coding agent, mcp, model context protocol, tool use, "
     "subagent, orchestration, context engineering, agent memory, agent eval, "
     "claude code, cursor, aider, codex cli, autonomous refactoring, computer use",
     _NOISE + ", agi hype, chatbot wrapper, prompt pack"),

    ("Software Engineering",
     "software architecture, refactoring, debugging techniques, performance profiling, "
     "systems programming, type system, build system, testing strategy, observability, "
     "concurrency, api design, postmortem, incident writeup, codebase migration",
     _NOISE + ", leetcode, interview prep, certification, framework comparison listicle"),

    ("Game Engines & Tech",
     "godot, unreal engine, unity, bevy, custom engine, ecs, renderer, shader, "
     "engine architecture, editor tooling, asset pipeline, physics engine, "
     "gpu optimization, nanite, lumen, engine source",
     _NOISE + ", gacha, casino, esports roster, console sales figures, review score" + _GAME_NOISE),

    ("Gameplay Programming",
     "gameplay systems, character controller, state machine, behavior tree, navmesh, "
     "netcode, rollback, animation blending, hit detection, procedural generation, "
     "game feel, juice, tuning, replay system, save system",
     _NOISE + ", cheat, hack, mod menu, aimbot, patch notes balance" + _GAME_NOISE),

    ("Level & Systems Design",
     "level design, blockout, greybox, encounter design, pacing, spatial storytelling, "
     "metroidvania layout, open world structure, systems design, economy design, "
     "difficulty curve, playtesting, map layout analysis",
     _NOISE + ", speedrun route, walkthrough, cheat, collectibles guide" + _GAME_NOISE),

    ("Game Writing & Narrative",
     "narrative design, game writing, branching dialogue, dialogue system, ink script, "
     "yarn spinner, twine, quest design, worldbuilding, environmental storytelling, "
     "character writing, player agency narrative, emergent narrative",
     _NOISE + ", fanfiction, movie adaptation, tv series, book review, celebrity" + _GAME_NOISE),

    ("Indie Games & Devlogs",
     "devlog, indie dev, solo developer, postmortem, game jam, itch.io release, "
     "steam page, early access lessons, marketing for indies, scope management, "
     "shipped my game, revenue breakdown, prototype",
     _NOISE + ", key giveaway, bundle deal, wishlist begging, asset flip" + _GAME_NOISE),
]


# Everything _NOISE screens out is the actual subject here (hiring, salary, job posting),
# so the jobs topic gets its own list: the scams and the roles that are not the target.
_JOB_NOISE = ("crypto, nft, web3 airdrop, unpaid, revenue share only, rev-share, "
              "equity only, volunteer, internship unpaid, mlm, commission only, "
              "sales representative, customer support, virtual assistant, data entry, "
              "recruiter spam, bootcamp, certification, course, leetcode grind, "
              "sponsorship required, click here to apply now, "
              # SEO pages that rank for every role term but are neither a job nor advice
              "freelance, for hire, how to become, career guide, salary guide, "
              "best jobs, top companies, academy, masterclass, roadmap 2026")

JOB_TOPIC = (
    "Remote Jobs: Software & Game Dev",
    # These are the queries verbatim - a feed-pinned topic does not generate angles.
    # Listing titles read "Senior Unity Developer (Remote)", so the terms have to be the
    # role names themselves, two words each, matching how postings are actually written.
    "remote software engineer, backend engineer, full stack developer, "
    "python developer, typescript developer, platform engineer, "
    "gameplay programmer, game developer, unity developer, unreal developer, "
    "engine programmer, tools programmer, technical designer, "
    "developer hiring, engineering interview, developer portfolio, "
    "salary negotiation, remote work culture, career progression engineer",
    _JOB_NOISE,
    " ".join(JOB_FEEDS),
    # arxiv, github and hn have nothing to say about a job hunt, but the shortlist gives
    # every enabled provider an equal share - so they returned "solar eruption analyses"
    # and Show HN posts for "platform engineer". Boards and the web only.
    "rss,searxng",
)


def connect():
    con = sqlite3.connect(DB_PATH, timeout=30)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA foreign_keys=ON")
    return con


def init():
    con = connect()
    with con:
        con.executescript(SCHEMA)
        for k, v in DEFAULT_SETTINGS.items():
            con.execute("INSERT OR IGNORE INTO settings(key,value) VALUES(?,?)", (k, v))
        # A run can only be live inside a running process; anything still marked running
        # at startup died with the last one.
        con.execute(
            "UPDATE runs SET status='interrupted', finished_at=datetime('now') "
            "WHERE status='running'"
        )
        # Older databases predate the feeds column.
        have = {r[1] for r in con.execute("PRAGMA table_info(topics)")}
        if "feeds" not in have:
            con.execute("ALTER TABLE topics ADD COLUMN feeds TEXT DEFAULT ''")
        if "providers" not in have:
            con.execute("ALTER TABLE topics ADD COLUMN providers TEXT DEFAULT ''")
        if not con.execute("SELECT 1 FROM topics LIMIT 1").fetchone():
            con.executemany(
                "INSERT OR IGNORE INTO topics(name,keywords,exclusions) VALUES(?,?,?)", SEED_TOPICS
            )
        # Seeded separately so it also lands in a database that already has the eight
        # research topics; INSERT OR IGNORE on the UNIQUE name makes it idempotent.
        con.execute(
            "INSERT OR IGNORE INTO topics(name,keywords,exclusions,feeds,providers)"
            " VALUES(?,?,?,?,?)",
            JOB_TOPIC,
        )
        # A database that predates the index has rows in items and an empty index.
        # Count the shadow table, NOT items_fts: an external-content table reads its
        # column values straight from items, so "SELECT ... FROM items_fts" reports
        # every row even when nothing is indexed and no MATCH can find anything.
        n_items = con.execute("SELECT count(*) FROM items").fetchone()[0]
        n_indexed = con.execute("SELECT count(*) FROM items_fts_docsize").fetchone()[0]
        if n_items and n_indexed != n_items:
            con.execute("INSERT INTO items_fts(items_fts) VALUES('rebuild')")
    con.close()


def get_settings(con=None):
    own = con is None
    con = con or connect()
    rows = con.execute("SELECT key,value FROM settings").fetchall()
    if own:
        con.close()
    s = dict(DEFAULT_SETTINGS)
    s.update({r["key"]: r["value"] for r in rows})
    return s


def save_settings(d):
    con = connect()
    with con:
        for k, v in d.items():
            if k in DEFAULT_SETTINGS:
                con.execute(
                    "INSERT INTO settings(key,value) VALUES(?,?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (k, str(v)),
                )
    con.close()


def setting_int(s, key, default):
    try:
        return int(str(s.get(key, default)).strip())
    except (TypeError, ValueError):
        return default


def topics(enabled_only=True):
    con = connect()
    q = "SELECT * FROM topics" + (" WHERE enabled=1" if enabled_only else "") + " ORDER BY id"
    rows = [dict(r) for r in con.execute(q)]
    con.close()
    return rows


def start_run(day):
    con = connect()
    with con:
        cur = con.execute("INSERT INTO runs(day) VALUES(?)", (day,))
    rid = cur.lastrowid
    con.close()
    return rid


def finish_run(run_id, status, stats=None, error=None):
    con = connect()
    with con:
        con.execute(
            "UPDATE runs SET finished_at=datetime('now'), status=?, stats=?, error=? WHERE id=?",
            (status, json.dumps(stats or {}), error, run_id),
        )
    con.close()


def insert_items(run_id, items):
    """items: list of dicts. Returns list of (db_id, item)."""
    con = connect()
    out = []
    with con:
        for it in items:
            cur = con.execute(
                """INSERT OR IGNORE INTO items
                (run_id, topic_id, topic_name, url, canon_url, domain, title, author,
                 published_at, source_type, snippet, content, content_chars, providers,
                 queries, cluster_id, cluster_domains, discussion,
                 llm_relevance, llm_novelty, llm_depth, llm_importance, llm_offtopic,
                 llm_summary, llm_why, llm_facts, llm_tags, llm_model,
                 trending_score, niche_score, breakdown, bucket, rank, deep_analysis)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    run_id, it.get("topic_id"), it.get("topic_name"), it.get("url"),
                    it.get("canon_url"), it.get("domain"), it.get("title"), it.get("author"),
                    it.get("published_at"), it.get("source_type"), it.get("snippet"),
                    it.get("content"), it.get("content_chars", 0),
                    json.dumps(sorted(it.get("providers", []))),
                    json.dumps(sorted(it.get("queries", []))[:6]),
                    it.get("cluster_id"), it.get("cluster_domains", 1), it.get("discussion", 0),
                    it.get("llm_relevance"), it.get("llm_novelty"), it.get("llm_depth"),
                    it.get("llm_importance"), int(bool(it.get("llm_offtopic"))),
                    it.get("llm_summary"), it.get("llm_why"),
                    json.dumps(it.get("llm_facts") or []), json.dumps(it.get("llm_tags") or []),
                    it.get("llm_model"),
                    it.get("trending_score", 0), it.get("niche_score", 0),
                    json.dumps(it.get("breakdown") or {}), it.get("bucket", ""),
                    it.get("rank"), it.get("deep_analysis"),
                ),
            )
            # rowcount, not lastrowid: after an ignored duplicate, lastrowid still holds
            # the previous row's id and would map this item to the wrong record.
            if cur.rowcount:
                out.append((cur.lastrowid, it))
    con.close()
    return out


def seen_canon_urls(days):
    """Canonical URLs already promoted to a bucket inside the last `days`. Re-showing them
    every run is what made the feed look stale even when the sources had moved on."""
    if not days or days <= 0:
        return set()
    con = connect()
    rows = con.execute(
        "SELECT DISTINCT canon_url FROM items WHERE bucket!='' AND canon_url IS NOT NULL"
        " AND created_at >= datetime('now', ?)", ("-" + str(int(days)) + " days",)).fetchall()
    con.close()
    return {r["canon_url"] for r in rows}


def set_deep_analysis(item_id, text):
    con = connect()
    with con:
        con.execute("UPDATE items SET deep_analysis=? WHERE id=?", (text, item_id))
    con.close()


# --- feedback -------------------------------------------------------------
# Relevance used to be tuned only by hand-editing a topic's keywords and exclusions.
# A vote is the same signal with no config edit: it biases who gets the fetch and
# inference budget, bans a domain that keeps losing, and gives the judge examples.

def vote(item_id, value):
    """value: 1 (useful), -1 (junk), 0 (clear). Keyed by canon_url, so the verdict
    applies to the link forever, not just to the run that happened to surface it."""
    value = max(-1, min(1, int(value)))
    con = connect()
    r = con.execute(
        "SELECT canon_url, domain, topic_id, title FROM items WHERE id=?", (item_id,)
    ).fetchone()
    if not r or not r["canon_url"]:
        con.close()
        return None
    with con:
        if value == 0:
            con.execute("DELETE FROM feedback WHERE canon_url=?", (r["canon_url"],))
        else:
            con.execute(
                "INSERT INTO feedback(canon_url,domain,topic_id,title,vote) VALUES(?,?,?,?,?)"
                " ON CONFLICT(canon_url) DO UPDATE SET vote=excluded.vote,"
                " created_at=datetime('now')",
                (r["canon_url"], r["domain"], r["topic_id"], r["title"], value),
            )
    con.close()
    return {"canon_url": r["canon_url"], "vote": value}


def votes_by_url():
    con = connect()
    out = {r["canon_url"]: r["vote"] for r in con.execute("SELECT canon_url, vote FROM feedback")}
    con.close()
    return out


def domain_votes(topic_id=None):
    """Net vote per domain. Topic-specific votes count double for their own topic: a
    domain can be right for the job hunt and wrong for devlogs."""
    con = connect()
    rows = con.execute("SELECT domain, topic_id, SUM(vote) AS net FROM feedback"
                       " WHERE domain IS NOT NULL AND domain != ''"
                       " GROUP BY domain, topic_id").fetchall()
    con.close()
    net = {}
    for r in rows:
        w = 2 if (topic_id is not None and r["topic_id"] == topic_id) else 1
        net[r["domain"]] = net.get(r["domain"], 0) + w * (r["net"] or 0)
    return net


def feedback_examples(topic_id=None, n=5):
    """(liked, disliked) titles, most recent first - few-shot material for the judge."""
    con = connect()
    args, where = [], "1=1"
    if topic_id is not None:
        where = "(topic_id=? OR topic_id IS NULL)"
        args = [topic_id]

    def grab(sign):
        return [r["title"] for r in con.execute(
            "SELECT title FROM feedback WHERE " + where + " AND vote=? AND title IS NOT NULL"
            " ORDER BY created_at DESC LIMIT ?", args + [sign, n])]

    out = (grab(1), grab(-1))
    con.close()
    return out


# --- page cache -----------------------------------------------------------

def cached_pages(canon_urls, days):
    """Extracted text from a previous run, still fresh enough to reuse."""
    if not days or days <= 0 or not canon_urls:
        return {}
    urls = [u for u in canon_urls if u]
    con = connect()
    out = {}
    # SQLite caps variables per statement; chunk rather than assume the batch is small.
    for i in range(0, len(urls), 400):
        chunk = urls[i:i + 400]
        q = ("SELECT canon_url, content, published_at, author FROM page_cache"
             " WHERE fetched_at >= datetime('now', ?) AND canon_url IN ("
             + ",".join("?" * len(chunk)) + ")")
        for r in con.execute(q, ["-" + str(int(days)) + " days"] + chunk):
            out[r["canon_url"]] = dict(r)
    con.close()
    return out


def cache_pages(items):
    """Store what the fetch actually produced. Empty extractions are stored too: a page
    that yields nothing today will yield nothing tomorrow, and skipping it is the point."""
    rows = [(it.get("canon_url"), it.get("content") or "", it.get("published_at"),
             it.get("author")) for it in items if it.get("canon_url")]
    if not rows:
        return 0
    con = connect()
    with con:
        con.executemany(
            "INSERT INTO page_cache(canon_url,content,published_at,author) VALUES(?,?,?,?)"
            " ON CONFLICT(canon_url) DO UPDATE SET content=excluded.content,"
            " published_at=excluded.published_at, author=excluded.author,"
            " fetched_at=datetime('now')", rows)
    con.close()
    return len(rows)


# --- full-text search -----------------------------------------------------

def fts_query(text):
    """Turn typed words into an FTS5 MATCH expression.

    Raw user input is not valid FTS5 syntax: "c++" and a stray quote both raise
    OperationalError. Quoting every token makes each one a literal phrase, and joining
    with AND keeps multi-word searches narrowing rather than widening.
    """
    toks = [t.replace('"', "") for t in str(text or "").split()]
    toks = [t for t in toks if t]
    if not toks:
        return ""
    # Trailing * on the last token = prefix search, so "godo" finds "godot" while typing.
    body = ['"' + t + '"' for t in toks[:-1]] + ['"' + toks[-1] + '"*']
    return " AND ".join(body)
