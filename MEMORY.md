# MEMORY.md - allseer

Project state and facts that already cost time. Read this before touching the code; do not
re-derive what is here.

## What it is

Local research/trend monitor. Per topic: today's top 3 trending stories + 3-5 niche finds,
summarised and scored by a local LLM, stored in SQLite, browsed in a single-page dashboard.
Python + FastAPI + Ollama + SQLite. No Docker, no keys, no auth, no build step.

Run: `python run.py` -> http://127.0.0.1:8077 -> "Run Research Now".
Headless: `python run.py --once`. Tests: `python tests/test_core.py`.

## Settled decisions (do not relitigate)

- **No Docker.** The user does not use Docker. SearXNG is optional and off by default; the
  keyless providers (HN, Reddit, GitHub, arXiv) carry the system alone.
- **One flat `items` table** holds search result + extracted content + LLM output + scores.
  Plus `topics`, `runs`, `settings`. No ORM, no migrations, no joins beyond `items x runs`.
- **Deterministic signals in Python, judgement in the LLM.** Recency, cross-source count,
  discussion, source quality are computed; relevance/novelty/depth/importance come from the
  model. Formulas live in `allseer/rank.py` and nothing else depends on them.
- **Facts vs interpretation is a product requirement**, enforced in the prompts and shown
  separately in the UI: `llm_summary` + `llm_facts` are source-bound; `llm_why` and
  `deep_analysis` are labelled AI interpretation.
- **Frontend is one static file** (`static/index.html`, vanilla JS). No framework, no build.
- Vendored dependency list is deliberately 4 packages: fastapi, uvicorn, httpx, trafilatura.

## Run control (added 2026-08-27)

- `POST /api/stop` -> `pipeline.stop()` cancels the asyncio task, which interrupts whatever
  it awaits. `run_research` catches `asyncio.CancelledError` **separately from `Exception`**
  (it is a BaseException) to mark the run `cancelled` rather than `failed`, then re-raises.
  Items stored for already-finished topics are kept on purpose.
- `POST /api/run` takes either `{"topic_ids": [...]}` or `{"query": "..."}`. A `query` runs
  an **ad-hoc** subject: a topic dict with `id=None`, so its items land with `topic_id NULL`
  and `topic_name` = the query. Never stored in `topics`. The user's exact phrase is forced
  in as the first search query before the LLM's generated angles.
- Ad-hoc runs are identified in History by the `subjects` column in `/api/state`, which is a
  `GROUP_CONCAT` of `items.topic_name` per run - deliberately no schema change.

## Provider facts learned the hard way (2026-08-27)

- **Reddit**: `www.reddit.com/search.json` -> 403 for any UA. `old.reddit.com/search.json`
  -> HTTP 200 with an *HTML interstitial* (so `.json()` throws, not a status error).
  **Only `https://www.reddit.com/search.rss` + a browser User-Agent works.** It is Atom XML
  with no score/comment counts, so reddit items get `discussion=0`. Reddit rate-limits
  brutally: `QUERY_CAP["reddit"] = 2` per run and a 6s `Throttle`. Still 429s occasionally -
  that is expected and non-fatal.
- **arXiv**: answers a burst with an **empty 200 body**, not an error. Serialised through a
  3s `Throttle` + one retry. A bare space in `search_query` means OR and returns unrelated
  papers - terms must be AND-ed (`all:x AND all:y`).
- **GitHub**: `sort=updated` returns junk. Relevance order (no `sort` param) plus
  `in:name,description,readme` is what makes results on-topic. Unauth search is ~10/min, so
  `QUERY_CAP["github"] = 5`. Use `created_at` as the date, not `pushed_at` - a 2019 repo
  pushed today is not a new find. Stars are a lifetime total, so `DISCUSSION_CAP["github"]`
  is 8000 vs 1000-1500 elsewhere.
- **HN Algolia** works well, but a narrow query often has nothing inside the freshness
  window, so the provider retries without `numericFilters` when the filtered call is empty.
- `_json()` rejects a 200 whose content-type is not JSON - that is how blocked/interstitial
  responses show up.

## Windows facts

- **The console is cp1252 and GitHub/Reddit titles contain emoji.** A bare `print()` of a
  log line killed an entire run with `UnicodeEncodeError`. Fixed once, centrally, in
  `allseer/__init__.py` (`sys.stdout.reconfigure(errors="replace")`), which every entry
  point imports. There is a regression test. Do not add `print()` handling elsewhere.
- Kill a stuck server with `Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
  Where-Object { $_.CommandLine -like '*run.py*' } | Stop-Process -Force`.
- A run left `status='running'` by a killed process is marked `interrupted` on next startup.

## Performance reality on this machine (CPU inference)

- `qwen2.5:14b` is the installed default: ~15-25s per judge call, ~40s per analyst note.
  `max_llm` is the setting that governs run length; 10 items + 8 notes is roughly 10 min.
- Defaults are deliberately small (`max_fetch=40`, `max_llm=20`). Raise them only on a GPU.
- The prompt body sent to `judge()` is capped at 4000 chars for the same reason.

## Known ceilings (deliberate, marked `ponytail:` in code)

- Clustering is an O(n^2) title comparison - fine at a few hundred items per run.
- Reddit items have no discussion signal (RSS limitation). OAuth would restore it.
- Both throttles are global, not per-host.

## Where things are

`run.py` launcher | `allseer/db.py` schema+settings | `providers.py` search |
`extract.py` fetch+text | `dedupe.py` URL identity+clustering | `llm.py` Ollama+3 prompts |
`rank.py` formulas+selection | `pipeline.py` the run | `app.py` API | `static/index.html` UI
