"""OpenAI-compatible LLM client + the four prompts the pipeline needs.

Backend-agnostic: llama.cpp's llama-server and Ollama both speak this API.
"""
import json
import re

import httpx


class LLMError(RuntimeError):
    pass


class LLM:
    """OpenAI-compatible chat client.

    Talks to llama.cpp's llama-server and to Ollama unchanged: both serve
    /v1/chat/completions. Switching backend is a URL change in settings.
    """

    def __init__(self, base_url, model, timeout=300.0):
        self.base = (base_url or "http://127.0.0.1:8081").rstrip("/")
        self.model = model
        self.timeout = timeout

    async def chat(self, system, user, as_json=True, num_predict=700, temperature=0.2):
        payload = {
            "model": self.model,
            "stream": False,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "max_tokens": num_predict,
            "temperature": temperature,
            # ponytail: thinking models (qwen3.x, deepseek-r1) spend the whole token
            # budget reasoning and return content:"" -> "empty LLM response". Belt and
            # braces with llama-server's --reasoning-budget 0; harmless on models that
            # have no thinking mode. If a reasoning model is ever worth the wait, drop
            # this per-call and raise num_predict to ~4000.
            "chat_template_kwargs": {"enable_thinking": False},
        }
        if as_json:
            payload["response_format"] = {"type": "json_object"}
        async with httpx.AsyncClient(timeout=self.timeout) as c:
            try:
                r = await c.post(self.base + "/v1/chat/completions", json=payload)
            except httpx.HTTPError as e:
                raise LLMError("cannot reach LLM server at " + self.base + ": " + str(e)) from e
            if r.status_code >= 400:
                raise LLMError("LLM HTTP " + str(r.status_code) + ": " + r.text[:300])
            choices = r.json().get("choices") or []
            if not choices:
                raise LLMError("LLM returned no choices")
            return (choices[0].get("message") or {}).get("content") or ""

    async def json_chat(self, system, user, num_predict=700, temperature=0.2):
        txt = await self.chat(system, user, as_json=True, num_predict=num_predict,
                              temperature=temperature)
        return parse_json(txt)

    async def health(self):
        async with httpx.AsyncClient(timeout=10.0) as c:
            r = await c.get(self.base + "/v1/models")
            r.raise_for_status()
            names = [m.get("id", "") for m in r.json().get("data", [])]
        # llama-server loads exactly one model and reports it under the file or repo it
        # came from, which never matches a configured name. One model served = that one.
        present = len(names) == 1 or any(
            n == self.model or n.split(":")[0] == self.model.split(":")[0] for n in names)
        return {"ok": True, "models": names, "model_present": present}


def parse_json(txt):
    """Local models sometimes wrap JSON in prose or fences. Take the first object/array."""
    if not txt:
        raise LLMError("empty LLM response")
    txt = txt.strip()
    txt = re.sub(r"^```(?:json)?|```$", "", txt, flags=re.M).strip()
    try:
        return json.loads(txt)
    except json.JSONDecodeError:
        pass
    m = re.search(r"[\[{].*[\]}]", txt, re.S)
    if m:
        try:
            return json.loads(m.group(0))
        except json.JSONDecodeError as e:
            raise LLMError("unparseable JSON: " + txt[:200]) from e
    raise LLMError("no JSON in response: " + txt[:200])


def _num(v, lo=0.0, hi=10.0, default=5.0):
    try:
        n = float(v)
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, n))


# --- prompts ---------------------------------------------------------------

QUERY_SYS = """You write web search queries for a research analyst.
Return ONLY JSON: {"queries": ["...", "..."]}
Rules:
- Each query is 2-8 words, plain keywords, no boolean operators, no quotes, no dates.
- Cover DIFFERENT angles: breaking developments, technical deep dives, open-source
  releases, research papers, practitioner discussion, contrarian or critical takes,
  and small/obscure projects.
- Do not repeat the topic name verbatim in every query; vary the vocabulary."""


async def gen_queries(llm, topic, n):
    user = (
        "Topic: " + topic["name"] + "\n"
        "Must-include keywords: " + (topic.get("keywords") or "(none)") + "\n"
        "Avoid: " + (topic.get("exclusions") or "(none)") + "\n"
        "Write " + str(n) + " diverse search queries."
    )
    data = await llm.json_chat(QUERY_SYS, user, num_predict=400)
    qs = data.get("queries") if isinstance(data, dict) else data
    out = []
    for q in qs or []:
        q = " ".join(str(q).split())[:120]
        if q and q.lower() not in [x.lower() for x in out]:
            out.append(q)
    return out[:n]


def fallback_queries(topic, n):
    """Used when the LLM is unreachable or returns junk - the run still produces results."""
    name = topic["name"]
    kws = [k.strip() for k in (topic.get("keywords") or "").split(",") if k.strip()]
    base = [
        name,
        name + " news",
        name + " release",
        name + " research paper",
        name + " open source project",
        name + " discussion",
    ]
    for k in kws:
        base.append(k)
        base.append(k + " " + name)
    seen, out = set(), []
    for q in base:
        if q.lower() not in seen:
            seen.add(q.lower())
            out.append(q)
    return out[:n]


JUDGE_SYS = """You are a research analyst screening a discovery for a topic dossier.
Return ONLY this JSON object:
{
 "off_topic": true|false,
 "relevance": 0-10,
 "novelty": 0-10,
 "depth": 0-10,
 "importance": 0-10,
 "summary": "2-3 sentence factual summary, only what the text states",
 "why_matters": "1-2 sentences of YOUR analysis of the significance",
 "facts": ["concrete fact stated in the source", "..."],
 "tags": ["short-tag", "..."]
}
Scoring guide:
- relevance: how directly this serves the topic.
- novelty: is this genuinely new/unfamiliar (10) or the same story everyone reprints (0)?
- depth: technical or practical substance a reader can act on. Press releases and
  headline aggregation score low; benchmarks, code, methods, post-mortems score high.
- importance: potential to matter in 6-12 months, even if obscure today.
Keep "summary" and "facts" strictly to what the text supports - no speculation there.
Put all interpretation in "why_matters". Prefer [] to inventing facts."""


def _examples_block(examples):
    """Your own past verdicts, as few-shot calibration.

    The scoring guide is generic; these are the only part of the prompt that knows
    what *you* consider a good find. Titles only - a full example item each would
    blow a local model's context window for no extra signal.
    """
    liked, disliked = examples or ([], [])
    if not liked and not disliked:
        return ""
    out = "\nYOUR PAST VERDICTS ON THIS TOPIC - calibrate against them:\n"
    if liked:
        out += "Rated USEFUL:\n" + "".join("  + " + str(t)[:110] + "\n" for t in liked)
    if disliked:
        out += ("Rated JUNK (score these low and mark off_topic when they recur):\n"
                + "".join("  - " + str(t)[:110] + "\n" for t in disliked))
    return out


async def judge(llm, item, topic, examples=None):
    body = item.get("content") or item.get("snippet") or ""
    user = (
        "TOPIC: " + topic["name"] + "\n"
        "TOPIC KEYWORDS: " + (topic.get("keywords") or "-") + "\n"
        "EXCLUDE IF ABOUT: " + (topic.get("exclusions") or "-") + "\n" + _examples_block(examples) + "\n"
        "SOURCE TYPE: " + str(item.get("source_type")) + "\n"
        "DOMAIN: " + str(item.get("domain")) + "\n"
        "TITLE: " + str(item.get("title")) + "\n"
        "PUBLISHED: " + str(item.get("published_at") or "unknown") + "\n"
        "INDEPENDENT SOURCES COVERING THIS: " + str(item.get("cluster_domains", 1)) + "\n"
        "TEXT (may be truncated):\n" + body[:4000]
    )
    d = await llm.json_chat(JUDGE_SYS, user, num_predict=600)
    if not isinstance(d, dict):
        raise LLMError("judge returned non-object")
    facts = d.get("facts")
    tags = d.get("tags")
    return {
        "llm_offtopic": bool(d.get("off_topic")),
        "llm_relevance": _num(d.get("relevance")),
        "llm_novelty": _num(d.get("novelty")),
        "llm_depth": _num(d.get("depth")),
        "llm_importance": _num(d.get("importance")),
        "llm_summary": str(d.get("summary") or "")[:1500],
        "llm_why": str(d.get("why_matters") or "")[:1000],
        "llm_facts": [str(f)[:300] for f in (facts if isinstance(facts, list) else [])][:8],
        "llm_tags": [str(t)[:40] for t in (tags if isinstance(tags, list) else [])][:8],
        "llm_model": llm.model,
    }


DEEP_SYS = """You are writing the analyst note for a shortlisted discovery.
Plain markdown, no JSON, under 250 words, using exactly these sections:

**What it is** - factual, from the source only.
**Why it matters** - your analysis; say plainly that this is interpretation.
**What to check next** - 2-3 concrete follow-ups (a repo to read, a claim to verify, a benchmark to reproduce).
**Confidence** - low/medium/high plus the reason (thin source text = low)."""


async def deep_analyze(llm, item, topic):
    user = (
        "TOPIC: " + topic["name"] + "\n"
        "TITLE: " + str(item.get("title")) + "\n"
        "URL: " + str(item.get("url")) + "\n"
        "BUCKET: " + str(item.get("bucket")) + "\n"
        "INDEPENDENT SOURCES: " + str(item.get("cluster_domains", 1)) + "\n"
        "TEXT (may be truncated or just a search snippet):\n"
        + (item.get("content") or item.get("snippet") or "")[:8000]
    )
    return (await llm.chat(DEEP_SYS, user, as_json=False, num_predict=700)).strip()


# --- LinkedIn post writer --------------------------------------------------

LENGTHS = {"short": 70, "medium": 150, "long": 260}   # target words, hook plus body

# Nine ways to say something about a discovery. Hardcoded on purpose: tuning one is a
# one-line edit here, and an angle CRUD screen would be more code than the angles.
ANGLES = {
    "signal": {
        "name": "Signal", "min_items": 1, "needs_take": False,
        "blurb": "One development and what it changes.",
        "how": "Report one concrete development and what it changes. Open with the change "
               "itself, never with a claim about how important it is. Close on the "
               "implication, stated flatly.",
    },
    "discovery": {
        "name": "Discovery", "min_items": 1, "needs_take": False,
        "blurb": "Something obscure that deserves attention. Pairs with the niche bucket.",
        "how": "Surface something small or obscure and say what it is in plain terms "
               "before saying why it is interesting. Be honest that it is early, unproven "
               "or narrow. Close by naming exactly who should care and why.",
    },
    "field-notes": {
        "name": "Field notes", "min_items": 1, "needs_take": True,
        "blurb": "A practitioner account. Requires your take - the take IS the experience.",
        "how": "Write a practitioner's account grounded entirely in YOUR TAKE. The take is "
               "the experience; the source is background and must stay attributed to the "
               "source. Include the friction, not only the result. Close on what you would "
               "do differently next time.",
    },
    "teardown": {
        "name": "Teardown", "min_items": 1, "needs_take": False,
        "blurb": "Explain the mechanism plainly to a competent non-specialist.",
        "how": "Explain how the thing actually works to a competent non-specialist. One "
               "analogy at most, and only if it earns its place. Close on the trade-off "
               "the mechanism buys and what it costs.",
    },
    "contrarian": {
        "name": "Contrarian", "min_items": 1, "needs_take": False,
        "blurb": "A common belief set against what the source states.",
        "how": "Name a belief that is widely held in this field, state it fairly and "
               "without caricature, then set it against what the source actually shows. "
               "Close by conceding the strongest point on the other side.",
    },
    "provoke": {
        "name": "Thought-provoker", "min_items": 1, "needs_take": False,
        "blurb": "The second-order consequence nobody is discussing.",
        "how": "State the first-order fact in one sentence, then spend the post on the "
               "second-order consequence people are not discussing yet. Mark it clearly "
               "as the author's reasoning. Close open-ended, as a statement, not a question.",
    },
    "ask": {
        "name": "Ask the room", "min_items": 1, "needs_take": False,
        "blurb": "A real question to the industry. The only angle that closes on a question.",
        "how": "Give the context from the source, state the author's current lean and why "
               "it is uncertain, then ask ONE specific answerable question. Not 'thoughts?' "
               "- a question only someone with real experience could answer. This is the "
               "only angle allowed to end on a question mark.",
    },
    "lesson": {
        "name": "Lesson", "min_items": 1, "needs_take": False,
        "blurb": "A transferable principle, with the source as its evidence.",
        "how": "Extract one transferable principle. The source is the evidence, the "
               "principle is the point, so do not let the retelling take over. Close on "
               "where the principle stops applying.",
    },
    "synthesis": {
        "name": "Synthesis", "min_items": 2, "needs_take": False,
        "blurb": "Two or more finds connected into one pattern. Needs 2+ items.",
        "how": "Connect every source given into one pattern. State the pattern first, then "
               "each source as a line of evidence for it - do not summarise them in turn "
               "as a list. Close on what the pattern predicts next.",
    },
}

# Phrases that make a post read as engagement bait to the people it is aimed at.
# Checked in the prompt and again in lint_post, because a local model will use one anyway.
BANNED = [
    "game changer", "game-changer", "let that sink in", "in today's fast-paced world",
    "i'm humbled", "i am humbled", "thrilled to announce", "excited to announce",
    "this is huge", "mind-blowing", "mind blowing", "revolutionary", "unlock the power",
    "here's the thing", "the future of", "is here to stay", "needle-moving",
    "deep dive into the world of", "buckle up", "read that again",
    # observed coming out of qwen2.5:14b on the first real run
    "check it out", "did you know", "intrigued?", "let's dive in", "stay tuned",
]

POST_SYS = """You write LinkedIn posts for a working practitioner. The reader is a peer,
not a follower: they can tell instantly when a post is padded, hyped or machine-written.

Return ONLY this JSON object:
{
 "hooks": ["opening line A", "opening line B", "opening line C"],
 "body": "everything after the opening line",
 "hashtags": ["Tag", "..."],
 "first_comment": "the link plus one line on what is worth reading in it"
}

TRUTH
- Every factual claim must come from the SOURCE material below. Never invent a number,
  quote, benchmark, version, date, price, name or company.
- Never claim personal experience, personal use, attendance or authorship unless YOUR TAKE
  says so. If YOUR TAKE is "(none given)", write as someone who read the source - not as
  someone who used the thing.
- Interpretation is welcome but must read as the author's opinion, never as reported fact.
- If the source is too thin to support the requested angle, say so plainly in the body
  instead of inventing substance to fill the length.

FORM
- The three hooks are three genuinely different openings, not one sentence reworded.
- Each hook stands alone, is at most 200 characters, and works before LinkedIn truncates
  the post at "see more".
- "body" does not repeat the hook. It begins with the sentence that follows it.
- One idea per post. Paragraphs of one or two sentences, separated by a blank line.
- Name the source publication or author in the text: the link goes in the first comment,
  not in the post.
- No emoji anywhere, including as bullets.
- Do not end on a question unless the angle explicitly calls for one.
- Never use these phrases: """ + "; ".join(BANNED)


def _persona_block(settings):
    """Only the fields you actually filled in. Sending blanks teaches the model that an
    empty author profile is normal, and it writes to that."""
    fields = [("Role", "persona_role"), ("Speaks credibly on", "persona_expertise"),
              ("Writing for", "persona_audience"), ("Voice", "persona_voice"),
              ("Never says", "persona_avoid")]
    lines = [label + ": " + str(settings.get(key) or "").strip()
             for label, key in fields if str(settings.get(key) or "").strip()]
    out = ""
    if lines:
        # Not "AUTHOR": each SOURCE block already has an AUTHOR field meaning the person
        # who wrote the article, and the model must not confuse them for each other.
        out += "WHO IS WRITING THIS POST\n" + "\n".join(lines) + "\n"
    sample = str(settings.get("persona_sample") or "").strip()
    if sample:
        out += ("STYLE SAMPLE - the author's own writing. Match its rhythm and register.\n"
                "Do NOT reuse its content or subject:\n" + sample[:800] + "\n")
    return out


def _source_block(items):
    """Per-source text budget shrinks with the selection, so a five-item synthesis still
    fits a local model's context instead of silently losing the last sources."""
    budget = max(600, 6000 // max(1, len(items)))
    out = []
    for n, it in enumerate(items, 1):
        facts = it.get("llm_facts") or []
        out.append(
            "SOURCE " + str(n) + "\n"
            "TITLE: " + str(it.get("title") or "") + "\n"
            "PUBLICATION: " + str(it.get("domain") or "") + "\n"
            "AUTHOR: " + str(it.get("author") or "unknown") + "\n"
            "PUBLISHED: " + str(it.get("published_at") or "unknown") + "\n"
            "URL: " + str(it.get("url") or "") + "\n"
            "FACTS STATED IN THE SOURCE:\n"
            + ("".join("  - " + str(f)[:300] + "\n" for f in facts) or "  (none extracted)\n")
            + "SUMMARY: " + str(it.get("llm_summary") or it.get("snippet") or "") + "\n"
            "TEXT:\n" + (it.get("content") or it.get("snippet") or "")[:budget]
        )
    return "\n\n".join(out)


def build_post_user(items, angle, length="medium", hashtags_on=True, take="", settings=None):
    a = ANGLES[angle]
    words = LENGTHS.get(length, LENGTHS["medium"])
    tags = ("Pick 3-5 hashtags that a specialist would actually follow. No generic ones "
            "(innovation, technology, motivation, leadership)."
            if hashtags_on else "Return an empty hashtags list.")
    return (
        _persona_block(settings or {})
        + "\nANGLE: " + a["name"] + "\n" + a["how"] + "\n"
        "TARGET LENGTH: about " + str(words) + " words for hook plus body.\n"
        "HASHTAGS: " + tags + "\n"
        "YOUR TAKE (the author's own angle - make it the spine of the post): "
        + (str(take or "").strip() or "(none given)") + "\n\n"
        + _source_block(items)
    )


async def write_post(llm, items, angle, length="medium", hashtags_on=True, take="",
                     settings=None):
    user = build_post_user(items, angle, length, hashtags_on, take, settings)
    # Warmer than the judge: three hooks at temperature 0.2 come back as one hook
    # reworded twice, which defeats the point of offering a choice.
    d = await llm.json_chat(POST_SYS, user, num_predict=900, temperature=0.8)
    if not isinstance(d, dict):
        raise LLMError("post writer returned non-object")
    body = str(d.get("body") or "").strip()
    hooks = [" ".join(str(h).split()) for h in (d.get("hooks") or []) if str(h).strip()][:3]
    if not hooks:
        # A model that ignored the hooks field still wrote a post: its first line is the
        # hook. Splitting it off beats failing the request over a schema slip.
        first, _, rest = body.partition("\n")
        hooks, body = [first.strip()], rest.strip()
    tags = [str(t).lstrip("#").strip()[:40] for t in (d.get("hashtags") or []) if str(t).strip()]
    return {
        "hooks": hooks,
        "body": body,
        "hashtags": tags[:5] if hashtags_on else [],
        "first_comment": str(d.get("first_comment") or "").strip()[:600],
        "model": llm.model,
    }


# Pictographs, dingbats and symbols. Arrows (U+2190-21FF) are deliberately NOT in here:
# "->" rendered as an arrow is ordinary technical writing, not engagement bait.
EMOJI_RE = re.compile("[\U0001f000-\U0001faff⌀-➿⬀-⯿️☀-⛿]")


def lint_post(hook, body, hashtags, angle):
    """Advisory checks on a draft. Never blocks: the UI shows these beside Regenerate,
    because a human reads a bad post faster than a retry loop can rewrite one."""
    warn = []
    text = str(hook or "") + "\n" + str(body or "")
    low = text.lower()
    hits = sorted(p for p in BANNED if p in low)
    if hits:
        warn.append("cliche: " + ", ".join('"' + h + '"' for h in hits))
    if EMOJI_RE.search(text):
        warn.append("contains emoji - reads as engagement bait to this audience")
    if len(hook or "") > 220:
        warn.append("hook is " + str(len(hook)) + " chars - LinkedIn cuts around 200")
    if len(hashtags or []) > 5:
        warn.append(str(len(hashtags)) + " hashtags - 3 to 5 reads deliberate, more reads spam")
    lines = [ln for ln in str(body or "").strip().splitlines() if ln.strip()]
    if angle != "ask" and lines and lines[-1].rstrip().endswith("?"):
        warn.append("closes on a question - only the 'ask the room' angle should")
    if len(str(body or "").split()) > 120 and str(body or "").count("\n\n") < 2:
        warn.append("one wall of text - break it into one or two sentence paragraphs")
    return warn
