# MEMORY.md - allseer

Project state and facts that already cost time. Read this before touching the code; do not
re-derive what is here.

## What it is

Local research/trend monitor. Per topic: today's top 3 trending stories + 3-5 niche finds,
summarised and scored by a local LLM, stored in SQLite, browsed in a single-page dashboard.
Python + FastAPI + local LLM (OpenAI-compatible API: llama.cpp or Ollama) + SQLite. No Docker, no keys, no auth, no build step.

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
  **The query filters on `created:>`, not `pushed:>`.** `pushed:>` was the single cause of
  2014-2023 repos winning slots in a `days_back=3` run: it selected on push activity while
  reporting `created_at` as the date. Measured on 822 rows: **every single old ranked item
  came from GitHub.** Do not put `pushed:>` back.
- **Reddit is not usable unauthenticated.** It serves exactly one request then 429s every
  following one for minutes - both `search.rss` and `/r/<sub>/top.rss`. The `reddit`
  provider still exists but is OFF in the default provider list; subreddit `top.rss` feeds
  go through `rss` instead, behind a 12s throttle. OAuth (free script app) is the real fix.
- **`rss` provider**: one generic provider over the `rss_feeds` setting covers 80.lv,
  gamedeveloper.com, gamesindustry.biz, godotengine.org, itch.io, huggingface, and
  subreddit feeds. Feeds are the best gamedev/devlog source - complete, dated, unmetered.
  Each URL is fetched once per run (`_FEED_CACHE`, 900s TTL), so extra queries are free.
  RSS and Atom disagree on every tag name; `_feed_text()` tries both, and Atom `<link>` has
  no text, only `href`. Local filtering: any query word >3 chars must appear in title or
  summary. LinkedIn has no feed and blocks fetches - it cannot be a provider.
- **HN Algolia** works well, but a narrow query often has nothing inside the freshness
  window, so the provider retries without `numericFilters` when the filtered call is empty.
- `_json()` rejects a 200 whose content-type is not JSON - that is how blocked/interstitial
  responses show up.
- **SearXNG ships with the JSON API disabled.** A stock instance answers `?format=json`
  with `403 Forbidden` (an HTML body), which surfaces as `expected JSON, got text/html`.
  Fix is in the *instance*, not allseer: add `search: {formats: [html, json]}` to its
  `settings.yml` and restart. This machine's instance is at `http://127.0.0.1:1991`, Docker
  container `searxng-core`, config bind-mounted from `<your searxng config dir>`.
  SearXNG results essentially never carry `publishedDate`, so they arrive undated.

## Freshness is enforced centrally, not per provider

`days_back` used to be only a hint each provider interpreted its own way, and nothing
downstream re-checked. `NICHE_WEIGHTS` has **no recency term at all**, so a stale item with
good novelty/depth won a niche slot outright. `rank.fresh_enough(item, days_back,
drop_unknown)` is now the one gate, applied twice per topic in `pipeline.py`:

1. right after `search_all` - kills known-stale results before they cost a fetch or a judge call
2. right after `extract.fetch_many` - trafilatura fills `published_at` for pages the search
   API left undated, so some items only reveal they are ancient after the fetch

Undated items are KEPT by default (many good pages publish no date) and scored as ~72h old.
`drop_undated=1` makes the gate strict; it costs most SearXNG hits.

`suppress_seen_days` (default 21) skips any `canon_url` already promoted to a bucket in a
recent run - `db.seen_canon_urls()`. Without it the same evergreen repo was re-promoted
every run, which is what made the output look stale even when the sources had moved on.
Note `UNIQUE(run_id, canon_url)` only ever deduped *within* a run.

## The server serves the code it started with (2026-08-28)

`uvicorn.run()` in `run.py` has **no `--reload`**. A dashboard left running across an edit
keeps serving the modules it imported at startup. This cost a whole debugging session: the
"Indie Games & Devlogs" run that produced a 2016 repo as its top result was made by a
process started at 19:53, hours before the fixes it was supposed to contain. On that old
code `rss` was not in `REGISTRY`, so `REGISTRY.get("rss")` returned `None` and the provider
was **skipped without raising**, and the GitHub query still said `pushed:>`.

**Diagnose it from the stats blob, not the code.** A run's `stats` must contain
`dropped_stale`, `dropped_seen`, `dropped_offtopic`, `dropped_excluded`. Missing keys mean
the process predates the code on disk. Kill and restart before believing any run.

## Retrieval quality: what actually went wrong (2026-08-28)

The failing run, measured: 190 items, of which **20 of 20 judged were github.com**, and
searxng contributed 140 results led by facebook 12, linkedin 8, reddit 8, x 4, wikipedia 4.
Three separate causes, all of which survive a restart:

- **`discussion_score` was an "is this GitHub?" term.** GitHub is the only provider that
  reports a number there (stars); searxng, rss, arxiv and reddit-RSS all send `discussion=0`.
  At weight 0.20 in `prefilter_score` a 48k-star repo scored 0.96 while everything else
  scored 0. Weight is now 0.10 and topic match carries 0.30.
- **Nothing checked relevance before the LLM.** `exclusions` were only ever used *inside*
  the judge prompt, i.e. after the fetch and the inference were already paid for. A run for
  indie devlogs fetched scope.riege.com (freight), forums.scopeusers.com and ajtmh.org
  (tropical medicine), all matched on the word "scope". `rank.excluded()` and
  `rank.on_topic()` now gate before the fetch.
- **No source quota.** `select()` caps per domain, but by then the shortlist was already
  all GitHub. `rank.diversify()` now round-robins **providers first, then domains** when
  choosing who gets fetched and judged. Domain-only round-robin was tried first and simply
  moved the monopoly to SearXNG, which returns ~100 one-off domains per run vs RSS's ~10.

`providers.JUNK` drops the social walled gardens where results are built, not where they
are fetched - `extract.SKIP_HOSTS` only skipped the download, so they still ate dedupe and
fetch slots. Do not merge the two lists: SKIP_HOSTS is about "cannot extract text",
JUNK is about "is never the artefact".

A bare site root is dropped in `_result()` too: SearXNG returns homepages for topical
queries, they arrive undated, and four of them (`thellamaconcept.com`,
`emanschigames.com`, `magnate-games.itch.io`) took niche slots in the 05:08 run.
Every real artefact has a path; a query string counts as one.

`rank.excluded()` matches the title with `\b<term>s?\b`. The optional plural is load-bearing
(SEO titles say "Courses", "Jobs"); the word boundary is too ("Hacking the Godot renderer"
must survive the `hack` exclusion).

## Per-topic overrides (2026-08-28)

`topics` gained two columns, both empty by default, both migrated in `db.init()`:

- **`feeds`** - overrides `rss_feeds` for this topic. Setting it also switches query
  generation off: the topic's `keywords` become the queries verbatim. A job listing is
  titled "Senior Unity Developer (Remote)" and only matches a query that literally says
  `unity developer`; an LLM angle like "remote gameplay hiring trends" matches nothing.
- **`providers`** - overrides the global `providers` for this topic.

## Topic 9: Remote Jobs: Software & Game Dev (2026-08-28)

Remote/worldwide, general software + game dev, listings **and** hunting strategy in one
topic. `providers = rss,searxng` - arXiv, GitHub and HN returned "solar eruption analyses"
and Show HN posts for `platform engineer` when left enabled, because diversify gives every
enabled provider an equal share.

Feeds verified live 2026-08-28 (parse + fresh dated entries): weworkremotely 89,
himalayas 100, jobicy 200, remotive 20, hnrss/jobs 20, plus Pragmatic Engineer and
r/cscareerquestions, r/gamedevjobs, r/experienceddevs for the strategy half.
**Dead - do not re-add without checking `parse_feed()` first:** remoteok.com,
workingnomads.com, gamesindustry.biz/jobs and stackoverflow.com/jobs all serve malformed
XML; rss.app/hitmarker is not XML at all.

The topic uses `_JOB_NOISE`, **not** `_NOISE`. `_NOISE` excludes "hiring, job posting,
salary" - the entire subject of this topic. `_JOB_NOISE` instead screens the scams
(rev-share, unpaid, commission only), the wrong roles (virtual assistant, data entry) and
the SEO listicles that rank for every role term ("courses", "academy", "how to become",
"salary guide").

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
- `rss` feed match is a bare substring test, not stemming or embeddings. The LLM judge is
  the real filter; tighten only if noise actually reaches the buckets.
- Reddit feeds sit behind a 12s throttle, so ~6 of them add ~70s to the first run that
  touches them (cached for the rest of the run).

## Topics (Aug 28 2026 - rewritten from the owner's stated interests)

Eight topics, seeded in `db.SEED_TOPICS` and live in the DB. Two shared exclusion blocks do
the heavy lifting:

- `_NOISE` - money/career/listicle noise that follows any tech query. On every topic.
- `_GAME_NOISE` - game *coverage* rather than game *craft*: trailers, release dates,
  review scores, sales, esports, leaks. On the five game topics only. It exists because a
  "Grand Theft Auto 6 teaser debuts on Netflix" item won a NICHE slot in a Level & Systems
  Design run. Coverage is not craft.

| Topic | Covers |
|---|---|
| Local AI & Inference | local llm, ollama, llama.cpp, gguf, quantization, vllm, mlx |
| Agentic Systems & Harnesses | agentic, harnesses, mcp, tool use, context engineering, coding agents |
| Software Engineering | architecture, refactoring, profiling, systems, testing, postmortems |
| Game Engines & Tech | godot, unreal, unity, bevy, ecs, renderer, shader, tooling |
| Gameplay Programming | controllers, behavior trees, navmesh, netcode, procgen, game feel |
| Level & Systems Design | blockout, encounter design, pacing, spatial storytelling, economy |
| Game Writing & Narrative | narrative design, branching dialogue, ink/yarn, quest design |
| Indie Games & Devlogs | devlogs, solo dev, postmortems, game jams, scope, marketing |
| Remote Jobs: Software & Game Dev | pinned job-board feeds + strategy feeds; own providers and exclusions |

Eight topics x 5 queries x 5 providers is a long run. Run a subset by `topic_ids`, or drop
`queries_per_topic`, when iterating.


## LinkedIn post writer - the Write tab (2026-08-28)

A fourth view, bolted on beside the pipeline rather than into it. Pick 1+ discovered items,
pick an angle, get 3 hooks + an editable body + first-comment text. Drafts live in `posts`.

Settled decisions:

- **Nine angles hardcoded in `llm.py`** (`ANGLES`), not a DB table. Tuning one is a
  one-line edit; an angle CRUD screen would be more code than the angles.
- **Persona lives in the `settings` key/value table** (`persona_*`), not its own table.
  `renderSettings()` already renders every settings key, so six new keys cost zero UI.
  It renders a `textarea` for `persona_*` and an `input` for everything else.
- **Generation is awaited inline in the endpoint**, never `pipeline.STATUS`. One LLM call,
  and keeping it off the pipeline means you can write a post during a research run.
- **`temperature=0.8` for the post writer** (the judge stays at 0.2). At 0.2 the three
  hooks came back as one hook reworded twice, which defeats offering a choice.
- **Lint warns, never blocks or retries.** A human reads a bad post faster than a retry
  loop rewrites one. Every hook is linted, not just the first - hooks 2 and 3 are one
  radio click from being published.
- The prompt says `WHO IS WRITING THIS POST`, not `AUTHOR`: every SOURCE block already has
  an `AUTHOR:` field meaning the person who wrote the article.
- Empty `persona_*` fields are omitted from the prompt entirely. Sending blanks teaches the
  model that an empty author profile is normal and it writes to that.
- `posts.titles` is denormalised so a draft stays readable after its source item is deleted.

`persona_sample` (a paragraph of the owner's own writing) is the highest-leverage field in
the app. With the persona empty the output is measurably generic - a first real run on
`qwen2.5:14b` produced hooks ending in *"Check it out!"* and *"Intrigued?"*, which is what
put those phrases in `BANNED`.

## Where things are

`run.py` launcher | `allseer/db.py` schema+settings | `providers.py` search |
`extract.py` fetch+text | `dedupe.py` URL identity+clustering |
`llm.py` LLM client+4 prompts+post angles+lint | `rank.py` formulas+selection |
`pipeline.py` the run | `app.py` API | `static/index.html` UI
