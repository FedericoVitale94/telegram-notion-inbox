"""Notion client. Writes ONLY to the INBOX database; every other database is read-only.
Property names come from config ([notion.properties]), so an existing INBOX keeps its own names."""
import json
import os
import time
from datetime import datetime, timedelta, timezone

import config
import net

BASE = "https://api.notion.com/v1"
VERSION = "2022-06-28"
UPLOAD_VERSION = "2026-03-11"  # file uploads, and the blocks that attach them
PLATFORMS = ["Instagram", "TikTok", "YouTube", "Web"]  # plus the "text" label of the UI language
MIRROR_FULL_HOURS = 24  # full re-read of the local copy: it also drops pages deleted or archived in Notion
_writable = set()  # the only pages blocks may be appended to


def P(role):
    return config.P(role)


def statuses():
    return config.STATUSES


def common_properties():
    """Properties added after the first version: ensure_properties creates them on an older INBOX."""
    return {P("book"): {"rich_text": {}}, P("author"): {"rich_text": {}}, P("reading"): {"number": {}},
            P("chat_id"): {"number": {}}}


def creator_properties():
    similar = {"url": {}} if config.SIMILAR_TYPE == "url" else None
    props = {P("score"): {"number": {}}, P("covered"): {"checkbox": {}}, P("draft_written"): {"checkbox": {}}}
    if similar:  # a relation needs the target database: create it by hand in Notion
        props[P("similar")] = similar
    return props


def _headers(version=VERSION):
    return {"Authorization": f"Bearer {os.environ['NOTION_TOKEN']}", "Notion-Version": version}


def _inbox():
    return os.environ["NOTION_INBOX_DB_ID"].replace("-", "")


def query(db_id, body=None):
    """Read (any database). Returns every row, paginating."""
    body, rows = dict(body or {}), []
    while True:
        r = net.request("POST", f"{BASE}/databases/{db_id}/query", headers=_headers(), json=body).json()
        rows += r["results"]
        if not r.get("has_more"):
            return rows
        body["start_cursor"] = r["next_cursor"]


def find_by_message_id(message_id, chat_id, private=True):
    """Page already created for this message. Message ids restart from 1 in every chat, so the chat counts too.
    Pages without a chat id predate groups, so they come from the private chat."""
    rows = query(_inbox(), {"filter": {"property": P("message_id"), "number": {"equals": message_id}}})
    for r in rows:
        chat = r["properties"].get(P("chat_id"), {}).get("number")
        if chat == chat_id or (chat is None and private):
            return r["url"]
    return None


def find_by_url(url):
    """INBOX page with the same link (share parameters ignored, e.g. ?igsh=)."""
    base = url.split("?")[0].rstrip("/")
    rows = query(_inbox(), {"filter": {"property": P("url"), "url": {"starts_with": base}}, "page_size": 1})
    return rows[0]["url"] if rows else None


def inbox_ideas(category):
    return query(_inbox(), {"filter": {"property": P("category"), "select": {"equals": category}}})


def create_inbox_page(properties, blocks, cover=None):
    body = {"parent": {"database_id": _inbox()}, "properties": properties, "children": blocks[:100]}
    if cover:
        body["cover"] = {"type": "external", "external": {"url": cover}}
    page = net.request("POST", f"{BASE}/pages", headers=_headers(), json=body).json()
    _writable.add(page["id"])
    for i in range(100, len(blocks), 100):  # API limit: 100 blocks per request
        _append(page["id"], blocks[i:i + 100])
    return page["url"]


def _check_inbox(page_id):
    """Makes an existing page writable only after checking it belongs to INBOX."""
    page = net.request("GET", f"{BASE}/pages/{page_id}", headers=_headers()).json()
    if (page.get("parent", {}).get("database_id") or "").replace("-", "") != _inbox():
        raise PermissionError(f"write refused on {page_id}: not an INBOX page")
    _writable.add(page["id"])
    return page["id"]


def append_to_inbox_page(page_id, blocks):
    """Appends blocks at the end of an existing INBOX page."""
    page_id = _check_inbox(page_id)
    for i in range(0, len(blocks), 100):
        _append(page_id, blocks[i:i + 100])


def update_inbox_page(page_id, properties):
    page_id = _check_inbox(page_id)
    net.request("PATCH", f"{BASE}/pages/{page_id}", headers=_headers(), json={"properties": properties})


def page(page_id):
    """A page with its properties. Read only."""
    return net.request("GET", f"{BASE}/pages/{page_id}", headers=_headers()).json()


def page_sections(page_id):
    """A page's text split by section heading: {"Summary": "...", ...}. Read only."""
    out, section, cursor = {}, "", None
    while True:
        r = net.request("GET", f"{BASE}/blocks/{page_id}/children", headers=_headers(),
                        params={"page_size": 100, **({"start_cursor": cursor} if cursor else {})}).json()
        for b in r["results"]:
            t = "".join(x["plain_text"] for x in b.get(b["type"], {}).get("rich_text", []))
            if b["type"] == "heading_2":
                section = t
            else:
                out[section] = (out.get(section, "") + "\n" + t).strip()
        if not r.get("has_more"):
            return out
        cursor = r["next_cursor"]


def pages_to_draft():
    """Pages you marked "good idea" that have no draft yet."""
    return query(_inbox(), {"filter": {"and": [
        {"property": P("status"), "select": {"equals": statuses()[1]}},
        {"property": P("draft_written"), "checkbox": {"equals": False}}]}})


def to_review():
    """Pages still "to review", most recently saved first."""
    return query(_inbox(), {"filter": {"property": P("status"), "select": {"equals": statuses()[0]}},
                            "sorts": [{"property": P("saved"), "direction": "descending"}]})


def best_ideas(category, min_score):
    return query(_inbox(), {"filter": {"and": [
        {"property": P("category"), "select": {"equals": category}},
        {"property": P("status"), "select": {"equals": statuses()[0]}},
        {"property": P("score"), "number": {"greater_than_or_equal_to": min_score}}]},
        "sorts": [{"property": P("score"), "direction": "descending"}]})


def ensure_properties(creator=False):
    """Adds to INBOX the properties it lacks (an INBOX created by an older version, or creator mode turned on later).
    Touches only the INBOX schema; existing properties are left alone."""
    wanted = {**common_properties(), **(creator_properties() if creator else {})}
    have = net.request("GET", f"{BASE}/databases/{_inbox()}", headers=_headers()).json()["properties"]
    missing = {k: v for k, v in wanted.items() if k not in have}
    if missing:
        net.request("PATCH", f"{BASE}/databases/{_inbox()}", headers=_headers(), json={"properties": missing})
    return sorted(missing)


# --- local copy of INBOX, for fast searches by the agent ---

def _mirror_file():
    return config.ROOT / "cache" / "inbox_mirror.json"


def mirror():
    """Every INBOX page (id, url, properties), from a local copy kept in sync: only the pages edited since the
    last sync are read from Notion, plus a full re-read once a day. Returns a list of pages."""
    f = _mirror_file()
    try:
        data = json.loads(f.read_text())
    except (OSError, ValueError):
        data = {}
    now = datetime.now(timezone.utc)
    full = not data or now - datetime.fromisoformat(data["full"]) > timedelta(hours=MIRROR_FULL_HOURS)
    if full:
        pages, data["full"] = {}, now.isoformat()
        rows = query(_inbox())
    else:
        pages = data["pages"]
        since = (datetime.fromisoformat(data["synced"]) - timedelta(minutes=2)).isoformat()  # minute precision
        rows = query(_inbox(), {"filter": {"timestamp": "last_edited_time", "last_edited_time": {"on_or_after": since}}})
    for r in rows:
        pages[r["id"]] = {"id": r["id"], "url": r["url"], "properties": r["properties"]}
    data.update(synced=now.isoformat(), pages=pages)
    f.parent.mkdir(exist_ok=True)
    tmp = f.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False))
    tmp.replace(f)
    return list(pages.values())


# --- files ---

def upload_file(name, data, content_type):
    """Uploads a file (max 20 MB) and returns its upload id, to attach within an hour with file_block()."""
    h = _headers(UPLOAD_VERSION)
    up = net.request("POST", f"{BASE}/file_uploads", headers=h,
                     json={"filename": name, "content_type": content_type}).json()
    net.request("POST", f"{BASE}/file_uploads/{up['id']}/send", headers=h, files={"file": (name, data, content_type)})
    return up["id"]


def file_block(upload_id, image=False):
    kind = "image" if image else "file"
    return {"type": kind, kind: {"type": "file_upload", "file_upload": {"id": upload_id}}}


def append_files(page_id, blocks):
    """Attaches uploaded files at the end of an INBOX page created or checked by the bot."""
    if page_id not in _writable:
        page_id = _check_inbox(page_id)
    for i in range(0, len(blocks), 100):
        _append(page_id, blocks[i:i + 100], UPLOAD_VERSION)


def _append(page_id, blocks, version=VERSION):
    if page_id not in _writable:
        raise PermissionError(f"write refused on {page_id}: not an INBOX page created or checked by the bot")
    net.request("PATCH", f"{BASE}/blocks/{page_id}/children", headers=_headers(version), json={"children": blocks})


def create_inbox_database(parent_page_id, categories, topics, creator=False):
    """Used once by setup_notion.py."""
    opts = lambda names: {"options": [{"name": n} for n in names]}  # noqa: E731
    import i18n
    db = net.request("POST", f"{BASE}/databases", headers=_headers(), json={
        "parent": {"type": "page_id", "page_id": parent_page_id},
        "title": [{"type": "text", "text": {"content": "INBOX"}}],
        "properties": {
            P("title"): {"title": {}},
            P("category"): {"select": opts(categories)},
            P("platform"): {"select": opts(PLATFORMS + [i18n.t("platform_text")])},
            P("url"): {"url": {}},
            P("saved"): {"date": {}},
            P("status"): {"select": opts(statuses())},
            P("topic"): {"rich_text": {}},
            P("tags"): {"multi_select": opts(topics)},
            **common_properties(),
            **(creator_properties() if creator else {}),
            P("message_id"): {"number": {}},
            P("error"): {"rich_text": {}},
        },
    }).json()
    return db["id"]


# --- property and block builders ---

def chunks(s, n=2000):
    """Splits into pieces of at most n UTF-16 units: that is how Notion counts (an emoji is 2)."""
    out, cur, size = [], [], 0
    for ch in s:
        w = 2 if ord(ch) > 0xFFFF else 1
        if size + w > n:
            out.append("".join(cur))
            cur, size = [], 0
        cur.append(ch)
        size += w
    if cur:
        out.append("".join(cur))
    return out or [""]


def text(prop):
    """Plain text of a title or rich_text property read from the API ("" if missing)."""
    return "".join(x["plain_text"] for x in prop.get(prop["type"]) or []) if prop else ""


def rich(s):
    return [{"type": "text", "text": {"content": c}} for c in chunks(s)][:100] if s else []


def heading(s):
    return {"type": "heading_2", "heading_2": {"rich_text": rich(s)}}


def paragraphs(s):
    return [{"type": "paragraph", "paragraph": {"rich_text": rich(c)}} for c in chunks(s)]


def section(title, body):
    return [heading(title), *paragraphs(body)] if body else []
