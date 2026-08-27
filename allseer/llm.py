"""Ollama client + the three prompts the pipeline needs.

Swap in another local backend by reimplementing chat() with the same signature.
"""
import json
import re

import httpx


class LLMError(RuntimeError):
    pass


class Ollama:
    def __init__(self, base_url, model, timeout=300.0):
        self.base = (base_url or "http://localhost:11434").rstrip("/")
        self.model = model
        self.timeout = timeout

    async def chat(self, system, user, as_json=True, num_predict=700):
        payload = {
            "model": self.model,
            "stream": False,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "options": {"temperature": 0.2, "num_predict": num_predict},
        }
        if as_json:
            payload["format"] = "json"
        async with httpx.AsyncClient(timeout=self.timeout) as c:
            try:
                r = await c.post(self.base + "/api/chat", json=payload)
            except httpx.HTTPError as e:
                raise LLMError("cannot reach Ollama at " + self.base + ": " + str(e)) from e
            if r.status_code == 404:
                raise LLMError("model '" + self.model + "' not found. Run: ollama pull " + self.model)
            if r.status_code >= 400:
                raise LLMError("Ollama HTTP " + str(r.status_code) + ": " + r.text[:300])
            return (r.json().get("message") or {}).get("content", "")

    async def json_chat(self, system, user, num_predict=700):
        txt = await self.chat(system, user, as_json=True, num_predict=num_predict)
        return parse_json(txt)

    async def health(self):
        async with httpx.AsyncClient(timeout=10.0) as c:
            r = await c.get(self.base + "/api/tags")
            r.raise_for_status()
            names = [m.get("name", "") for m in r.json().get("models", [])]
        return {"ok": True, "models": names, "model_present": any(
            n == self.model or n.split(":")[0] == self.model.split(":")[0] for n in names)}


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
