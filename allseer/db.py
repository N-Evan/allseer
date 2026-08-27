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
"""

DEFAULT_SETTINGS = {
    "ollama_url": "http://localhost:11434",
    "ollama_model": "qwen2.5:14b",
    "searxng_url": "",  # e.g. http://localhost:8080 - optional, other providers work without it
    "queries_per_topic": "6",
    "max_fetch": "40",
    "max_llm": "20",
    "top_trending": "3",
    "top_niche": "5",
    "days_back": "3",
    "providers": "hn,reddit,github,arxiv,searxng",
    "user_agent": "allseer/0.1 (personal research agent)",
}

SEED_TOPICS = [
    ("Local LLMs & inference", "ollama, quantization, gguf, vllm, llama.cpp", "crypto, nft"),
    ("AI agents & tooling", "agent framework, mcp, tool use, retrieval", "stock price, funding round"),
]


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
        if not con.execute("SELECT 1 FROM topics LIMIT 1").fetchone():
            con.executemany(
                "INSERT OR IGNORE INTO topics(name,keywords,exclusions) VALUES(?,?,?)", SEED_TOPICS
            )
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


def set_deep_analysis(item_id, text):
    con = connect()
    with con:
        con.execute("UPDATE items SET deep_analysis=? WHERE id=?", (text, item_id))
    con.close()
