"""Idea analysis (creator mode): duplicates (archive and INBOX) and a 1-5 score.

The archive is the database in [archive], or whatever custom.py's archive() returns (e.g. published items joined
with their scripts from a second database). Each entry: title, text, script, score, url, date, and optionally
id (shown to the model and in replies), views, relation_id (a page to link in the "similar" relation property),
similar_note (a line added to the analysis when this entry is the duplicate)."""
import json
import math
import re
import time
import unicodedata

import config
import i18n
import llm
import notion

CACHE_HOURS = 24  # published content changes slowly: re-read from Notion at most once a day
STOP = set("""about after also been before being from have into just more most only other over same some such
than that their them then there these they this those very what when where which while will with would your
video story alla alle allo anche ancora come con contro dall dalla dalle dallo degli della delle dello dentro
dopo dove essere fino hanno loro molto nella nelle nello negli perché però per poi quando quella quelle quello
questa queste questo sono stata stato sulla sulle sullo sugli tutti tutto una uno storia""".split())

PROMPT = """You assist {brand}.

New saved idea:
Title: {title}
Topic: {topic}
Summary: {summary}
Available material:
{material}

Items already published, or already saved in the inbox, most similar by wording (they may be unrelated):
{candidates}

1. duplicate: the id of an item above about the SAME specific subject (same story, case, place, object or \
person). Sharing a broad theme is not enough. Empty string if none.
2. score from 1 to 5 as an idea for {brand}.
3. reason: 2-3 sentences in {language} covering fit with the brand, whether verifiable sources exist \
(yes/no, which kind) and overlap risk with published items. No invented facts."""


def _cache():
    return config.ROOT / "cache" / "archive.json"


def analyze(item, c):
    """Returns {"score", "reason", "similar": archive or INBOX entry, or None}."""
    candidates = neighbours(f"{c['title']} {c['topic']} {c['summary']}", archive() + inbox())
    ids = {}
    for i, r in enumerate(candidates):
        ids[r.get("id") or f"c{i + 1}"] = r
    lines = []
    for k, r in ids.items():
        views = i18n.t("candidate_views", views=r["views"]) if r.get("views") is not None else ""
        lines.append(i18n.t("candidate", id=k, where=i18n.t("candidate_inbox" if r.get("inbox") else "candidate_published"),
                            date=r["date"] or i18n.t("candidate_no_date"), views=views, title=r["title"],
                            text=r["text"][:300]))
    schema = {
        "type": "object",
        "properties": {
            "duplicate": {"type": "string", "enum": [""] + list(ids)},
            "score": {"type": "integer", "enum": [1, 2, 3, 4, 5]},
            "reason": {"type": "string"},
        },
        "required": ["duplicate", "score", "reason"],
        "additionalProperties": False,
    }
    out = llm.complete_json(config.prompt("idea", PROMPT).format(
        brand=config.BRAND, language=config.LANGUAGE, title=c["title"], topic=c["topic"], summary=c["summary"],
        material=item.get("context", "")[:6000] or i18n.t("summary_only"),
        candidates="\n".join(lines) or i18n.t("no_candidates")), schema)
    score = out.get("score")
    return {"score": score if score in (1, 2, 3, 4, 5) else None,
            "reason": str(out.get("reason") or "").strip(), "similar": ids.get(out.get("duplicate"))}


def analysis_text(a):
    t = i18n.t("analysis", score=a["score"], reason=a["reason"])
    s = a["similar"]
    if s:
        title = f"{s['id']} · {s['title']}" if s.get("id") else s["title"]
        t += "\n\n" + i18n.t("analysis_covered", title=title, date=s["date"], url=s["url"])
        if s.get("similar_note"):
            t += "\n" + s["similar_note"]
    return t


_inbox = None


def inbox():
    """Ideas already in INBOX: read once per run, then kept current with remember()."""
    global _inbox
    if _inbox is None:
        _inbox = []
        for r in notion.inbox_ideas(config.IDEA):
            p = r["properties"]
            remember(notion.text(p.get(config.P("title"))), notion.text(p.get(config.P("topic"))),
                     ((p.get(config.P("saved")) or {}).get("date") or {}).get("start", ""), r["url"], r["id"])
    return _inbox


def remember(title, topic, date, page_url, page_id):
    """Adds an idea to the INBOX list, including the ones created during this run."""
    if _inbox is not None:
        _inbox.append({"id": f"inbox-{page_id.replace('-', '')[-8:]}", "title": title, "text": topic, "script": "",
                       "url": page_url, "page_id": page_id, "date": date[:10], "score": None, "inbox": True})


def words(s):
    s = unicodedata.normalize("NFKD", s.lower())
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    return {w for w in re.findall(r"[a-z0-9]{4,}", s) if w not in STOP}


def neighbours(query, entries, n=6):
    """Text search: shared words weighted by rarity (idf). The top n go to the LLM for confirmation."""
    docs = [words(f"{r['title']} {r['text']} {r['script']}") for r in entries]
    df = {}
    for d in docs:
        for w in d:
            df[w] = df.get(w, 0) + 1
    q = words(query)
    points = [(sum(math.log(len(docs) / df[w]) for w in q & d), i) for i, d in enumerate(docs)]
    return [entries[i] for s, i in sorted(points, key=lambda x: (-x[0], x[1]))[:n] if s > 0]


def archive():
    """Published items: custom.py's archive() if there is one, otherwise the [archive] database.
    Local cache for 24 hours."""
    custom = config.hook("archive")
    if not custom and not config.ARCHIVE.get("database_id"):
        return []
    cache = _cache()
    try:
        if time.time() - cache.stat().st_mtime < CACHE_HOURS * 3600:
            return json.loads(cache.read_text())
    except (OSError, ValueError):
        pass
    entries = custom() if custom else _archive_database()
    cache.parent.mkdir(exist_ok=True)
    tmp = cache.with_suffix(".tmp")
    tmp.write_text(json.dumps(entries, ensure_ascii=False))
    tmp.replace(cache)
    return entries


def _archive_database():
    a = config.ARCHIVE
    entries = []
    for r in notion.query(a["database_id"].replace("-", "")):
        p = r["properties"]
        get = lambda name: _value(p[name]) if name and name in p else ""  # noqa: E731
        entries.append({
            "title": get(a.get("title_property")),
            "text": " ".join(str(get(n)) for n in a.get("text_properties", []))[:1000],
            "script": str(get(a.get("script_property")))[:3000],
            "score": get(a.get("score_property")) or None,
            "url": get(a.get("url_property")) or r["url"],
            "date": str(get(a.get("date_property")))[:10],
        })
    return entries


def _value(prop):
    t = prop["type"]
    if t in ("title", "rich_text"):
        return notion.text(prop)
    if t in ("select", "status"):
        return (prop[t] or {}).get("name", "")
    if t == "multi_select":
        return ", ".join(o["name"] for o in prop[t])
    if t == "date":
        return (prop[t] or {}).get("start", "")
    if t in ("url", "number", "email", "phone_number", "checkbox"):
        return prop[t]
    if t == "formula":
        f = prop[t]
        return f.get(f["type"])
    return ""
