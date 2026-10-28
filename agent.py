"""Talk to your inbox: a message starting with "?" or "/ask" goes to Claude, which answers using tools on
INBOX (and, in creator mode, on your archive). Every other message is saved as usual.

Writes go only to INBOX, through the guarded functions in notion.py. Status changes on more than 3 pages
need an explicit confirmation; drafts are capped per question; questions are capped per day ([agent] in
config.toml)."""
import json
import logging
import math
import os
import re
import time
from datetime import date

from anthropic import beta_tool

import config
import costs
import i18n
import ideas
import llm
import notion
import pipeline

log = logging.getLogger(__name__)

HISTORY = config.ROOT / "cache" / "agent_history.json"
USAGE = config.ROOT / "cache" / "agent_usage.json"
SAVE_LINK = re.compile(r"^\s*(?:save|add|store|keep|salva|aggiungi|metti|archivia)\b.*https?://", re.I | re.S)
MAX_PAIRS = 6            # exchanges remembered per chat
EXPIRY = 2 * 3600        # after 2 hours the conversation starts over
MAX_DRAFTS = 2           # drafts per question (Opus is expensive)
MAX_UNCONFIRMED = 3      # bulk status changes allowed without confirmation
MAX_PAGE = 12000         # characters returned by read_page
MAX_RESULTS = 15

PROMPT = """You assist {brand}. They write to you on Telegram.

INBOX is the Notion database where the bot saves what they forward. Categories:
{guide}
Statuses: {statuses}.{creator}

Rules:
- Answer in {language}, short and plain, as plain text without markdown: Telegram does not render it.
- Use the tools for every fact about INBOX{archive_rule}. Never invent titles, numbers, dates or links: \
if a tool did not return it, say so.
- When you mention INBOX pages, include their links.
- For outside facts use web search and name the source; say so when sources are weak or disagree.
- Before changing the status of more than 3 pages, list them and ask for confirmation; call again with \
confirmed=true only after an explicit yes in their next message.
- Long texts (outlines, lists, notes) go into the right page with add_note; on Telegram reply with a short \
summary and the link.
- To save a new link they must send it to the bot without "?": you do not save new items.
- If the request is unclear or data is missing, ask one question."""

_drafts_written = 0


# ---------- routing and history

def _prefix():
    command = re.escape(config.AGENT.get("command", "ask"))
    return re.compile(rf"^\s*(?:\?|/{command}(?:@\w+)?(?=\s|$))", re.I)


def is_question(msg):
    """True if the message is for the agent: text starting with "?" or the command (/ask by default)."""
    return config.AGENT.get("enabled", True) and bool(_prefix().match(msg.get("text") or ""))


def _key(chat_id, thread_id=None):
    return f"{chat_id}:{thread_id}" if thread_id is not None else str(chat_id)


def _read_json(path):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return {}


def _write_json(path, data):
    path.parent.mkdir(exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False))
    tmp.replace(path)


def history(chat_id, thread_id=None):
    """Recent exchanges of this chat that have not expired, as API messages."""
    limit = time.time() - EXPIRY
    out = []
    for e in _read_json(HISTORY).get(_key(chat_id, thread_id), []):
        if e["t"] >= limit:
            out += [{"role": "user", "content": e["q"]}, {"role": "assistant", "content": e["a"]}]
    return out


def remember_exchange(chat_id, question, answer, thread_id=None):
    data = _read_json(HISTORY)
    limit = time.time() - EXPIRY
    k = _key(chat_id, thread_id)
    kept = [e for e in data.get(k, []) if e["t"] >= limit]
    data[k] = (kept + [{"t": time.time(), "q": question, "a": answer}])[-MAX_PAIRS:]
    _write_json(HISTORY, data)


def _used_today():
    u = _read_json(USAGE)
    return u.get("n", 0) if u.get("day") == date.today().isoformat() else 0


def _count_question():
    _write_json(USAGE, {"day": date.today().isoformat(), "n": _used_today() + 1})


# ---------- tools

def _select(prop):
    return ((prop or {}).get("select") or {}).get("name", "")


def _row(r):
    p, P = r["properties"], config.P
    row = {"page_id": r["id"], "title": notion.text(p.get(P("title"))),
           "category": _select(p.get(P("category"))), "status": _select(p.get(P("status"))),
           "saved": ((p.get(P("saved")) or {}).get("date") or {}).get("start", "")[:10],
           "topic": notion.text(p.get(P("topic"))), "url": r.get("url", "")}
    for role, key in (("score", "score"), ("reading", "reading_min")):
        if (p.get(P(role)) or {}).get("number") is not None:
            row[key] = p[P(role)]["number"]
    for role in ("book", "author"):
        if notion.text(p.get(P(role))):
            row[role] = notion.text(p[P(role)])
    return row


def _by_relevance(query, rows):
    """Sorted by shared words weighted by rarity (like ideas.neighbours); rows with no shared word are dropped."""
    docs = [ideas.words(f"{r['title']} {r['topic']} {r.get('book', '')} {r.get('author', '')}") for r in rows]
    df = {}
    for d in docs:
        for w in d:
            df[w] = df.get(w, 0) + 1
    q = ideas.words(query)
    points = [(sum(math.log((len(docs) + 1) / df[w]) for w in q & d), i) for i, d in enumerate(docs)]
    return [rows[i] for s, i in sorted(points, key=lambda x: (-x[0], x[1])) if s > 0]


@beta_tool
def search_inbox(text: str = "", category: str = "", status: str = "", min_score: int = 0) -> str:
    """Searches INBOX pages. Returns up to 15 pages (page_id, title, category, status, saved date, topic,
    author, reading time, idea score if any, link): the most relevant to the text or, with no text, the newest.

    Args:
        text: words to look for in title, topic and author; empty to list by date.
        category: one of the INBOX categories; empty for all.
        status: one of the statuses; empty for all.
        min_score: only ideas scored at least this (1-5, creator mode); 0 for no filter.
    """
    if status and status not in config.STATUSES:
        return json.dumps({"error": f"invalid status: {status}. Valid: {', '.join(config.STATUSES)}"})
    rows = [_row(r) for r in notion.mirror()]  # local copy, synced with what changed in Notion
    rows = [r for r in rows if (not category or r["category"] == category) and (not status or r["status"] == status)
            and (not min_score or (r.get("score") or 0) >= min_score)]
    rows.sort(key=lambda r: r["saved"], reverse=True)
    if text.strip():
        rows = _by_relevance(text, rows)
    return json.dumps({"found": len(rows), "pages": rows[:MAX_RESULTS]}, ensure_ascii=False)


@beta_tool
def read_page(page_id: str) -> str:
    """Reads an INBOX page: its properties and its text split by section (transcript, summary, analysis,
    draft, sources...). Text beyond 12,000 characters is cut.

    Args:
        page_id: the page id, as returned by search_inbox.
    """
    head = json.dumps(_row(notion.page(page_id)), ensure_ascii=False)
    body = "\n\n".join(f"## {k}\n{v}" if k else v for k, v in notion.page_sections(page_id).items())
    out = f"{head}\n\n{body}"
    return out[:MAX_PAGE] + ("\n[text cut]" if len(out) > MAX_PAGE else "")


@beta_tool
def set_status(page_ids: list[str], status: str, confirmed: bool = False) -> str:
    """Sets the status of one or more INBOX pages. More than 3 pages need confirmed=true, to be used only
    after the user explicitly confirmed the list.

    Args:
        page_ids: the page ids.
        status: one of the statuses.
        confirmed: true only after the user's explicit confirmation.
    """
    if status not in config.STATUSES:
        return f"invalid status: {status}. Valid: {', '.join(config.STATUSES)}"
    if len(page_ids) > MAX_UNCONFIRMED and not confirmed:
        return (f"{len(page_ids)} pages: confirmation needed. List the pages for the user, ask them to confirm, "
                "then call again with confirmed=true.")
    done, errors = 0, []
    for pid in page_ids:
        try:
            notion.update_inbox_page(pid, {config.P("status"): {"select": {"name": status}}})
            done += 1
        except Exception as e:  # a non-INBOX page or a network error does not stop the others
            errors.append(f"{pid}: {str(e)[:120]}")
    return f"status '{status}' set on {done} pages" + (f"; errors: {'; '.join(errors)}" if errors else "")


@beta_tool
def add_note(page_id: str, title: str, text: str) -> str:
    """Adds a section at the end of an INBOX page (an outline, a reading note, a list of sources).
    Existing text is not changed.

    Args:
        page_id: the INBOX page id.
        title: the section title.
        text: the section content, plain text.
    """
    notion.append_to_inbox_page(page_id, notion.section(title, text))
    return f"section '{title}' added"


@beta_tool
def search_published(text: str) -> str:
    """Searches what the user already published (the archive). Returns the 6 closest items with
    id, title, date, type, score, views and link (when the archive has them).

    Args:
        text: the subject to look for.
    """
    found = ideas.neighbours(text, ideas.archive())
    keys = ("id", "title", "date", "type", "score", "views", "url")
    return json.dumps([{k: r[k] for k in keys if r.get(k) is not None} for r in found], ensure_ascii=False)


@beta_tool
def write_draft(page_id: str) -> str:
    """Writes a script draft in the user's voice for an idea in INBOX and adds it to the page.
    Costs money: at most 2 drafts per question.

    Args:
        page_id: the INBOX page id of the idea.
    """
    global _drafts_written
    if _drafts_written >= MAX_DRAFTS:
        return f"limit of {MAX_DRAFTS} drafts per question reached: the others in a later question"
    _drafts_written += 1
    return pipeline.draft_for_page(notion.page(page_id))


def _has_archive():
    return bool(config.ARCHIVE.get("database_id") or config.hook("archive"))


def tools():
    """The tools on offer: the creator ones only in creator mode (and search_published only with an archive)."""
    out = [search_inbox, read_page, set_status, add_note]
    if config.CREATOR_ON:
        if _has_archive():
            out.append(search_published)
        if config.DRAFTS.get("enabled", True):
            out.append(write_draft)
    searches = config.AGENT.get("web_searches", 3)
    if searches:
        out.append({"type": "web_search_20260209", "name": "web_search", "max_uses": searches})
    return out


def system_prompt():
    creator = ""
    if config.CREATOR_ON:
        creator = (f"\nCreator mode is on: items in the category {config.IDEA} are ideas for what the user publishes, "
                   "scored 1-5, and can get a script draft (write_draft).")
    archive_rule = " and on what they published" if config.CREATOR_ON and _has_archive() else ""
    guide = config.CATEGORY_GUIDE + "".join(f"\n- {c}: {d}" for c, d in config.CATEGORY_LIST_FORCED.items())
    return config.prompt("agent", PROMPT).format(
        brand=config.BRAND, language=config.LANGUAGE, guide=guide, statuses=", ".join(config.STATUSES),
        creator=creator, archive_rule=archive_rule)


# ---------- loop

def _runner(messages):
    """Iterator over the model's messages: the tool runner calls the tools and continues until the answer."""
    return llm.client().beta.messages.tool_runner(
        model=llm.model("agent"),
        max_tokens=16000,
        max_iterations=config.AGENT.get("max_steps", 12),
        system=[{"type": "text", "text": system_prompt(), "cache_control": {"type": "ephemeral"}}],
        tools=tools(),
        output_config={"effort": "medium"},
        cache_control={"type": "ephemeral"},  # each step reads the conversation already sent from the cache
        messages=messages,
    )


def answer(msg):
    """The agent's answer to a question from Telegram (plain text)."""
    global _drafts_written
    chat, thread = msg["chat"]["id"], msg.get("message_thread_id")
    question = _prefix().sub("", msg.get("text") or "", count=1).strip()
    if not question:
        return i18n.t("ask_empty")
    if SAVE_LINK.match(question) or re.fullmatch(r"https?://\S+", question):
        return i18n.t("ask_save_link")
    if os.environ.get("LLM_PROVIDER", "anthropic") != "anthropic":
        return i18n.t("ask_needs_anthropic")
    limit = config.AGENT.get("daily_limit", 30)
    if _used_today() >= limit:
        return i18n.t("ask_daily_limit", limit=limit)
    if costs.over_limit():
        return i18n.t("ask_cost_limit")
    _count_question()
    _drafts_written = 0
    last, usage = None, {"input": 0, "output": 0, "cache": 0, "steps": 0}
    for m in _runner(history(chat, thread) + [{"role": "user", "content": question}]):
        last = m
        u = getattr(m, "usage", None)
        costs.record(getattr(m, "model", None) or llm.model("agent"), u)
        if u:
            usage["input"] += u.input_tokens or 0
            usage["output"] += u.output_tokens or 0
            usage["cache"] += getattr(u, "cache_read_input_tokens", 0) or 0
        usage["steps"] += 1
    log.info("agent: %d steps, tokens in %d (cached %d), out %d", usage["steps"], usage["input"], usage["cache"],
             usage["output"])
    if last is None:
        return i18n.t("ask_no_answer")
    if last.stop_reason == "refusal":
        return i18n.t("ask_refused")
    reply = "".join(b.text for b in last.content if b.type == "text").strip()
    if last.stop_reason != "end_turn":
        reply = (reply + "\n\n" if reply else "") + i18n.t("ask_not_finished")
    remember_exchange(chat, question, reply, thread)
    return reply
