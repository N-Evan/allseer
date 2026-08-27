# allseer

Fully local research/trend monitor. For each topic you configure it finds **today's top 3
stories** plus **3-5 niche finds** worth investigating, summarises them with a local LLM,
and stores everything in SQLite for browsing.

No paid APIs, no cloud, no accounts, no Docker required.

## Setup (Windows, no Docker)

```powershell
# 1. Ollama - install from https://ollama.com, then pull a model
ollama pull qwen2.5:14b        # or llama3.1:8b / gemma3:12b - anything you have

# 2. Python deps
python -m pip install -r requirements.txt

# 3. Run
python run.py
```

Open http://127.0.0.1:8077, press **Run Research Now**.

That's it. SearXNG is optional (see below) - the keyless providers work without it.

## What it searches

| Provider | Source | Key needed |
|---|---|---|
| `hn` | Hacker News (Algolia API) | no |
| `reddit` | Reddit search (old.reddit JSON) | no |
| `github` | GitHub repo search | no (unauthenticated, ~10 searches/min) |
| `arxiv` | arXiv Atom API | no |
| `searxng` | Any SearXNG instance = news sites, blogs, everything else | no, but needs an instance |

Edit the `providers` setting to enable/disable. Without SearXNG you still get HN, Reddit,
GitHub and arXiv; with it you also get news sites and specialist blogs.

### Optional: SearXNG

Point the `searxng_url` setting at any instance whose JSON API is open. Locally:

```powershell
docker run -d --name searxng -p 8080:8080 -e SEARXNG_SETTINGS_PATH=/etc/searxng docker.io/searxng/searxng
# then set searxng_url = http://localhost:8080  (its settings.yml needs "json" in search.formats)
```

If it is unreachable the run logs the failure and continues with the other providers.

## How the ranking works

Deterministic signals are computed in Python; only judgement calls come from the LLM.

**TRENDING** = 0.25 recency + 0.25 cross-source coverage + 0.25 relevance
+ 0.10 source quality + 0.15 discussion
**NICHE** = 0.25 novelty + 0.25 depth + 0.15 relevance + 0.15 importance + 0.20 *low* coverage

- Recency has a 24h half-life; an unknown date is treated as ~3 days old, never as fresh.
- Cross-source coverage counts **distinct domains** in the same story cluster, so five
  reprints of one press release count once.
- Niche penalises mainstream aggregators and thin pages, so it surfaces the small repo or
  the one-person blog post rather than the same headline again.
- An item never appears in both lists, and one domain gets at most one slot per list.
- Weights live in `allseer/rank.py`; change them there.

Only the shortlisted items get a second, expensive LLM pass (the analyst note).

## Facts vs interpretation

Every card separates them on purpose:

- **Factual summary** and **Stated in the source** - constrained to the fetched text.
- **AI interpretation** ("why this matters") and **Analyst note** - the model's opinion,
  labelled as such in the UI, with a self-reported confidence level.

## Settings (Settings tab)

| Setting | Meaning |
|---|---|
| `ollama_url`, `ollama_model` | local LLM |
| `searxng_url` | optional, blank = off |
| `queries_per_topic` | search angles generated per topic (6 is a good default) |
| `max_fetch` | pages downloaded per topic |
| `max_llm` | items scored by the LLM per topic - **this is what run time depends on** |
| `top_trending`, `top_niche` | slots per list |
| `days_back` | freshness window for the searches |
| `providers` | comma separated provider names |

A run with `max_llm=35` on an 8B model takes roughly 5-15 minutes. Start smaller.

## Files

```
run.py                  launcher (python run.py, or --once for a headless run)
allseer/db.py           SQLite schema + settings
allseer/providers.py    search providers (add one here)
allseer/extract.py      page fetch + text extraction
allseer/dedupe.py       URL identity + same-story clustering
allseer/llm.py          Ollama client + the 3 prompts
allseer/rank.py         scoring formulas + list selection
allseer/pipeline.py     the run, start to finish
allseer/app.py          FastAPI API + dashboard host
static/index.html       the whole dashboard (no build step)
tests/test_core.py      python tests/test_core.py
allseer.db              created on first run
```

## Scheduling (optional)

`python run.py --once` runs a full research pass and exits. Point Windows Task Scheduler at
it for a daily 7am dossier:

```powershell
schtasks /create /tn allseer /tr "python C:\path\to\allseer\run.py --once" /sc daily /st 07:00
```

## Failure behaviour

Nothing in a run is fatal. Provider errors, 403s, paywalls, JS-only pages, unparseable
dates, a stopped Ollama, and bad JSON from a small model are all logged to the progress
panel and the run continues with what it has. If the LLM is down entirely you still get
searched, deduplicated, unscored discoveries under "Everything discovered".
