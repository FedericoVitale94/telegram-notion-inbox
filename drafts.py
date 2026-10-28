"""Script draft in your voice (creator mode): voice file + best archive scripts as examples + the idea's material.

custom.py can replace three steps: draft_examples() -> [{"title", "script", "note"}], draft_postprocess(text)
-> text, and draft_checks(text, lo, hi) -> list of warnings (it replaces the word-count check)."""
import json
import time

import config
import ideas
import llm

CACHE_DAYS = 7

PROMPT = """You write drafts for {brand}. Write in {language}.

## Style rules
{voice}

## Example scripts (the best performing ones, for voice and rhythm)
{examples}

## Material for the idea
Title: {title}
Topic: {topic}
{material}

Write the draft following the rules above. Use only facts from the material.
Length: aim for {target} words, never more than {max_words} (drafts tend to come out too long: cut).
"draft": the script text only. "to_verify": facts in the draft to check before publishing (names, dates, \
numbers, quotes); if the material is not enough, say so here."""

SCHEMA = {
    "type": "object",
    "properties": {"draft": {"type": "string"}, "to_verify": {"type": "array", "items": {"type": "string"}}},
    "required": ["draft", "to_verify"],
    "additionalProperties": False,
}


def _cache():
    return config.ROOT / "cache" / "voice_examples.json"


def draft(item, c):
    """Returns {"draft", "to_verify", "checks"}."""
    d = config.DRAFTS
    lo, hi = d.get("min_words", 160), d.get("max_words", 220)
    voice_file = config.ROOT / d.get("voice_file", "prompts/voice.md")
    if not voice_file.exists():
        raise RuntimeError(f"{voice_file.name} missing: copy prompts/voice.example.md and edit it")
    out = llm.complete_json(config.prompt("draft", PROMPT).format(
        brand=config.BRAND, language=config.LANGUAGE, voice=voice_file.read_text(),
        examples=_examples_text() or "(none)", title=c["title"], topic=c["topic"],
        material=item.get("context", "")[:20000], target=f"{lo + (hi - lo) // 2}-{hi - (hi - lo) // 4}",
        max_words=hi), SCHEMA, task="draft", effort="medium")
    text = str(out.get("draft") or "").strip()
    post = config.hook("draft_postprocess")
    if post:
        text = post(text)
    check = config.hook("draft_checks") or checks
    return {"draft": text, "to_verify": [str(x) for x in out.get("to_verify") or []], "checks": check(text, lo, hi)}


def checks(text, lo, hi):
    """Automatic checks; empty list when everything is fine."""
    n = len(text.split())
    return [] if lo <= n <= hi else [f"{n} words (target {lo}-{hi})"]


def examples():
    """Example scripts: custom.py's draft_examples() if there is one, otherwise archive scripts, best score
    first, one per script. Weekly cache."""
    cache = _cache()
    try:
        if time.time() - cache.stat().st_mtime < CACHE_DAYS * 86400:
            return json.loads(cache.read_text())
    except (OSError, ValueError):
        pass
    custom = config.hook("draft_examples")
    chosen = custom() if custom else _from_archive()
    cache.parent.mkdir(exist_ok=True)
    tmp = cache.with_suffix(".tmp")
    tmp.write_text(json.dumps(chosen, ensure_ascii=False))
    tmp.replace(cache)
    return chosen


def _from_archive():
    with_script = [r for r in ideas.archive() if len(r["script"].split()) >= 50]
    with_script.sort(key=lambda r: (r["score"] is None, -(r["score"] or 0)))
    chosen, seen = [], set()
    for r in with_script:
        key = r["script"][:120]  # the same script is often published on several platforms
        if key not in seen:
            seen.add(key)
            chosen.append({"title": r["title"], "script": r["script"]})
        if len(chosen) == config.DRAFTS.get("examples", 8):
            break
    return chosen


def _examples_text():
    return "\n\n".join(f"### {e['title']}" + (f" ({e['note']})" if e.get("note") else "") + f"\n{e['script']}"
                       for e in examples())
