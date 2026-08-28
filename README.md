# allseer

Fully local research/trend monitor. For each topic you configure it finds **today's top 3
stories** plus **3-5 niche finds** worth investigating, summarises them with a local LLM,
and stores everything in SQLite for browsing. The **Write** tab turns any of them into a
LinkedIn draft in your own voice.

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

```powershell
python run.py --reload   # restart the server on every code edit
python run.py --once     # one headless run, for Task Scheduler
python run.py --port 9000
```

That's it. SearXNG is optional (see below) - the keyless providers work without it.

## What it searches

| Provider | Source | Key needed |
|---|---|---|
| `hn` | Hacker News (Algolia API) | no |
| `rss` | Every feed URL in the `rss_feeds` setting - 80.lv, Game Developer, GamesIndustry, Godot, itch.io, HuggingFace, and subreddit `top.rss` feeds | no |
| `github` | GitHub repo search, **`created:>`** so only genuinely new repos match | no (unauthenticated, ~10 searches/min) |
| `arxiv` | arXiv Atom API | no |
| `searxng` | Any SearXNG instance = news sites, blogs, everything else | no, but needs an instance |
| `reddit` | Reddit search Atom feed - **off by default**, unauthenticated Reddit 429s after ~1 request; use its subreddit feeds via `rss` instead | no |

Edit the `providers` setting to enable/disable.

### rss - the highest-signal source

Feeds are complete, dated and never rate-limited, which makes them better than any search
API for gamedev/devlog material. Add a URL to `rss_feeds` (space or comma separated) and
it is live on the next run; nothing else changes. Each feed is fetched **once per run** and
cached, so extra queries over it are nearly free.

A feed cannot be searched, so entries are filtered locally: any distinctive word (>3 chars)
of the query must appear in the title or summary. That is deliberately generous - the LLM
judge is the real filter.

Each feed answers at most `FEED_CAP` (10) entries per query, best-matching first. Without
that cap, `"solo developer game jam insights"` matched 80 entries on one gamedev feed,
because every article there contains both "game" and "developer".

LinkedIn has no public feed and blocks unauthenticated fetches, so it cannot be a provider.

### Per-topic feeds and providers

`rss_feeds` and `providers` are global settings, which does not work once two topics want
different sources. A topic row may override either one:

| Column | Empty means | Set means |
|---|---|---|
| `topics.feeds` | use the global `rss_feeds` | use only these feeds, **and** use the topic's keywords verbatim as the queries instead of generating LLM angles |
| `topics.providers` | use the global `providers` | use only these providers for this topic |

The jobs topic uses both. Job boards must not answer gamedev queries, gamedev feeds must
not answer role queries, and arXiv/GitHub/HN have nothing to say about a job hunt - left
enabled they returned "solar eruption analyses" and Show HN posts for `platform engineer`,
because the shortlist gives every enabled provider an equal share.

Pinned queries matter for the same reason: a listing is titled *"Senior Unity Developer
(Remote)"* and only matches a query that literally says `unity developer`. An LLM angle
like *"remote gameplay hiring trends"* matches nothing in a job feed.

### Optional: SearXNG

Point the `searxng_url` setting at any instance whose JSON API is open. Locally:

```powershell
docker run -d --name searxng -p 8080:8080 -e SEARXNG_SETTINGS_PATH=/etc/searxng docker.io/searxng/searxng
# then set searxng_url = http://localhost:8080
```

**The JSON API is off by default** - a stock instance answers `?format=json` with
`403 Forbidden`, which allseer logs as `expected JSON, got text/html`. Add this to the
instance's `settings.yml` and restart it:

```yaml
search:
  formats:
    - html
    - json
```

SearXNG results almost never carry a publish date, so they arrive undated. They survive the
freshness gate on that basis (see `drop_undated`) until the page fetch reveals a real date.

If it is unreachable the run logs the failure and continues with the other providers.

## Two ways to run it

- **Run Research Now** - every enabled topic, the daily dossier.
- **Research this** (box at the top of Today) - a one-off subject typed right now, e.g.
  *"RISC-V laptops"*, with an optional exclusion. It is researched immediately through the
  same pipeline and stored in history, but never saved as a topic. Your exact wording is
  used as the first search query, then the LLM generates angles around it.
- **Stop** - appears while a run is in progress and cancels it immediately, including the
  search, page fetch or Ollama call in flight. The run is marked `cancelled`; whatever was
  already stored stays browsable.

Only one run happens at a time - starting a second returns 409 rather than queueing.

## What gets thrown away before the LLM sees it

Inference is the scarce resource, so four cheap gates run first. Each one logs how much it
dropped, and the counts land in the run's `stats`.

| Gate | Where | Drops |
|---|---|---|
| `providers.JUNK` | as results are built | facebook, linkedin, x, instagram, pinterest, quora, wikipedia, tiktok, podcast and course-mill hosts - never the artefact, always a link to it, and every one blocks the fetch |
| bare site roots | as results are built | a URL with no path is a *publication*, not a story - SearXNG answers "indie devlog" with homepages, and four of them won niche slots in one run |
| `rank.fresh_enough` | after search, again after fetch | anything with a known date outside `days_back` |
| `rank.excluded` | after search | the topic's own `exclusions`, matched on the title with word boundaries and an optional plural, so `courses` trips `course` but `Hacking the Godot renderer` survives `hack` |
| `rank.on_topic` | after search | fewer than two distinct topic keywords - one shared generic word ("game", "developer") is not evidence |

Then `rank.diversify()` picks who gets fetched and judged: **round-robin across providers
first, then across domains inside each provider.** Score order alone gave one run's entire
LLM budget - 20 items out of 20 - to github.com, because GitHub is the only provider that
reports a discussion number (stars), which made `discussion_score` an "is this GitHub?"
term in practice. Round-robin on domain alone then handed it to SearXNG, which returns
~100 one-off domains per run against RSS's ~10.

Ranking still happens afterwards on the real scores; this only decides who gets looked at.

## Teaching it what you like

Every card has a thumb up and a thumb down. That vote is the only part of the system that
knows what *you* consider a good find, and it does three things on the next run:

| Vote | Effect |
|---|---|
| up | that domain scores higher in the pre-LLM triage, so it is likelier to get a fetch and an inference slot |
| down | the same, downward - and the title becomes a negative example in the judge's prompt |
| net `-3` on a domain | the domain is dropped outright, before the fetch, in every future run |

A vote is keyed by **canonical URL, not by run**, so it sticks to the link, and voting on
the same story found again next week overwrites rather than double-counts. Voting `0`
clears it.

The bias is `tanh(net / 3)`, added to the prefilter as a signed `+/-0.15` term against a
roughly 0-1 base. It saturates on purpose: three downvotes should already mean "stop
showing me this", but the thirtieth must not outweigh recency, relevance and coverage
combined.

Votes on a topic count double for that topic and single elsewhere - a domain can be right
for the job hunt and wrong for devlogs. The ban threshold is the `dislike_drop` setting;
`0` turns banning off and keeps only the soft bias.

The judge prompt gets your five most recent likes and dislikes as titles, labelled
`Rated USEFUL` / `Rated JUNK`. This is the one part of the prompt that is not generic
advice, and it is where a bigger `analysis_model` pays off - a 14B model calibrates
against examples noticeably better than an 8B one.

`GET /api/feedback` shows the current per-domain standing, so you can see what your
voting has actually taught it.

## Searching every run

The search box queries an SQLite **FTS5** index over `title`, `snippet`, `llm_summary`,
`llm_tags` and `domain`, across your whole history - not just the run on screen. Results
come back newest-run-first. Opening a specific run or day from History scopes the search
back to it.

The last word is a prefix, so `geometr` finds "geometry" while you type, and extra words
narrow rather than widen. Typed text is never valid FTS5 syntax on its own (`c++` and a
stray quote both raise), so `db.fts_query()` quotes every token into a literal phrase
before it reaches `MATCH`.

The index is external-content: the rows live in `items` and two triggers keep the index in
step. An older database is backfilled at startup - which is checked by comparing
`items_fts_docsize` to `items`, **not** by selecting from `items_fts`, because an
external-content table reads its column values from `items` and so reports every row even
when the index is empty and no `MATCH` can find a thing.

## The digest

Every finished run writes `digests/YYYY-MM-DD-run<id>.md`: the promoted items only, grouped
by topic and bucket, with the summary and the AI interpretation. A run that finishes into a
browser tab is a run you have to remember to open; the same content on disk is readable on
a phone, greppable from a shell, and outlives the database. Set `digest_dir` blank to turn
it off.

## Writing a LinkedIn post about a find (Write tab)

Research surfaces the material; the **Write** tab turns a find into a post you would not be
embarrassed to publish.

Tick one item for a single-source post, or two or more for a **synthesis** that connects
them into one pattern. Pick an angle, a length and whether you want hashtags, add your own
take in one line, and generate. You get **three different hooks** to choose between, an
editable body, the hashtags, and the text for the **first comment** - the link goes there,
because LinkedIn throttles posts that send people off-platform.

Nine angles, hardcoded in `allseer/llm.py` so tuning one is a one-line edit:

| Angle | What it writes | Closes on |
|---|---|---|
| Signal | one development and what it changes | the implication |
| Discovery | something obscure, in plain terms, honestly early | who should care |
| Field notes | a practitioner account - **requires your take**, the take is the experience | what you'd do differently |
| Teardown | how the thing works, for a competent non-specialist | the trade-off it buys |
| Contrarian | a common belief set fairly against what the source shows | the strongest counterpoint |
| Thought-provoker | the second-order consequence nobody is discussing | open, as a statement |
| Ask the room | context, your lean, then one answerable question | the only angle that ends on `?` |
| Lesson | a transferable principle, source as evidence | where it stops applying |
| Synthesis | 2+ finds as one pattern (needs 2+ items) | what the pattern predicts |

### Why the output does not read as AI slop

Three things, in order of how much they matter:

1. **Your author profile.** The six `persona_*` settings - role, expertise, audience, voice,
   a paragraph of your own writing, and phrases you never use. `persona_sample` does the
   most work of any single setting in the app: it is the only thing in the prompt that
   knows how *you* sound. Leave them blank and every post is written from the article
   alone, which is exactly the generic case this exists to avoid. The Write tab says so
   until you fill them in. Empty fields are never sent.
2. **Fixed prompt rules.** Facts only from the source - no invented number, quote,
   benchmark, version or date. Never claims you used or attended anything unless your take
   says so. One idea, short paragraphs, no emoji, the source named in the text, no closing
   question except on `ask`, and a banned-phrase list (`game changer`, `let that sink in`,
   `thrilled to announce`, `check it out`, and friends).
3. **A lint pass over the result.** Every hook - not just the first - plus the body and
   hashtags are checked for banned phrases, emoji, an over-long hook, hashtag spam, a bait
   question, and wall-of-text formatting. Findings show as amber chips beside
   **Regenerate**. They never block: you read a bad post faster than a retry loop rewrites
   one.

Drafts are stored in the `posts` table and listed under the composer, so a good one from
last week is still there. Item titles are copied into the draft, so it stays readable even
if the topic that found it is deleted.

Post writing is one LLM call awaited directly, not a pipeline run - you can write a post
while research is running. It uses `analysis_model` if set. On `qwen2.5:14b` expect
20-60 seconds.

## What it does not download twice

`max_fetch` is 40 pages a topic and at most 8 items are ever promoted, so most of every
fetch budget was going on pages an earlier run had already read. Extracted text is now kept
in `page_cache` and reused for `page_cache_days` (14). Failed extractions are cached too -
a page that yields nothing today will yield nothing tomorrow, and skipping it is the point.
Set `page_cache_days` to `0` to always refetch.

This is not the same as `suppress_seen_days`, which hides links already *promoted*. The
cache helps the much larger set that was fetched and never shortlisted.

## Where the time went

A run takes 5-15 minutes and the log never said which stage owned it. Each run's `stats`
now carries `secs` - `search`, `fetch`, `judge`, `analyse` - shown as a badge on the run in
History, along with how many pages came from the cache. Check it before changing a setting:
a slow model and a slow network want opposite fixes.

## How the ranking works

Deterministic signals are computed in Python; only judgement calls come from the LLM.

**PREFILTER** (who gets the fetch and inference budget) = 0.30 topic match + 0.25 recency
+ 0.15 cross-source + 0.15 source quality + 0.10 discussion + 0.05 has text

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
| `ollama_url`, `ollama_model` | local LLM; `ollama_model` writes the search queries |
| `analysis_model` | model used for judging and the analyst notes; blank = same as `ollama_model`. Query generation is cheap and forgiving, judging is where a bigger model shows |
| `searxng_url` | optional, blank = off |
| `queries_per_topic` | search angles generated per topic (6 is a good default) |
| `max_fetch` | pages downloaded per topic |
| `max_llm` | items scored by the LLM per topic - **this is what run time depends on** |
| `top_trending`, `top_niche` | slots per list |
| `days_back` | **hard** freshness window - anything with a known date older than this is discarded before ranking |
| `drop_undated` | `1` = also discard items with no publish date at all (strict; costs you most SearXNG hits) |
| `suppress_seen_days` | skip links already promoted to a bucket in a run this recent (`21`); `0` = off |
| `providers` | comma separated provider names |
| `rss_feeds` | feed URLs for the `rss` provider, space or comma separated |
| `page_cache_days` | reuse page text fetched this recently instead of downloading it again; `0` = always refetch |
| `dislike_drop` | net downvotes that ban a domain from every future run (`3`); `0` = soft bias only |
| `digest_dir` | folder for the per-run markdown digest (`digests`); blank = off |
| `persona_role`, `persona_expertise`, `persona_audience`, `persona_voice`, `persona_sample`, `persona_avoid` | who is writing the LinkedIn posts. `persona_sample` - a paragraph of your own writing - is the single highest-leverage field in this table |

A run with `max_llm=35` on an 8B model takes roughly 5-15 minutes. Start smaller.

### Why a run can be perfect on disk and broken in the browser

Start it with `python run.py --reload` while editing and this cannot happen.

Without that flag the dashboard keeps serving the modules it imported at startup,
silently, forever, and a run made that way looks like a retrieval failure when it is not:

- `rss` was in the `providers` setting but not in the old `REGISTRY`, so `REGISTRY.get()`
  returned `None` and the provider was **skipped with no error** - zero feed results.
- the old GitHub query still used `pushed:>`, so 2014-2016 repos came back.
- `fresh_enough()` did not exist yet, so nothing filtered them.

Tell them apart in one glance: the run's `stats` should contain `dropped_stale`,
`dropped_seen`, `dropped_offtopic` and `dropped_excluded`. If those keys are missing, the
server is older than the code on disk - restart it, or use `--reload`.

### Why old items used to show up

`days_back` was only ever a *hint* passed to each provider, and each honoured it
differently - GitHub not at all: it filtered on `pushed:>` while reporting `created_at` as
the publish date, so a repo created in 2014 and pushed yesterday sailed through. Nothing
downstream re-checked, and `NICHE_WEIGHTS` has no recency term, so those items won niche
slots outright. It is now enforced centrally in `rank.fresh_enough()`, twice per topic:
once after the search and again after the page fetch (which is when an undated item often
reveals its real date).

`suppress_seen_days` covers the other half of the complaint: without it, the same evergreen
repo was re-promoted every single run, so the output looked stale even when the sources had
moved on.

## Files

```
run.py                  launcher (--reload while editing, --once for a headless run)
allseer/db.py           SQLite schema, settings, votes, page cache, FTS helpers
allseer/providers.py    search providers (add one here)
allseer/extract.py      page fetch + text extraction
allseer/dedupe.py       URL identity + same-story clustering
allseer/llm.py          Ollama client + the 4 prompts, post angles, draft lint
allseer/rank.py         scoring formulas + list selection
allseer/pipeline.py     the run, start to finish
allseer/app.py          FastAPI API + dashboard host
static/index.html       the whole dashboard (no build step)
tests/test_core.py      python tests/test_core.py - no network, no LLM, ~1.5s
allseer.db              created on first run
digests/                one markdown file per run
```

Tables in `allseer.db`: `topics`, `runs`, `items`, `settings`, `feedback` (your votes,
keyed by canonical URL), `page_cache` (extracted text, reused across runs), `items_fts`
(the search index over `items`), and `posts` (your LinkedIn drafts).

## Scheduling (optional)

`python run.py --once` runs a full research pass over the enabled topics and exits. Point Windows Task Scheduler at
it for a daily 7am dossier:

```powershell
schtasks /create /tn allseer /tr "python C:\path\to\allseer\run.py --once" /sc daily /st 07:00
```

## Failure behaviour

Nothing in a run is fatal. Provider errors, 403s, paywalls, JS-only pages, unparseable
dates, a stopped Ollama, and bad JSON from a small model are all logged to the progress
panel and the run continues with what it has. If the LLM is down entirely you still get
searched, deduplicated, unscored discoveries under "Everything discovered".
