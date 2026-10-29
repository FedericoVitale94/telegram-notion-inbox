"""Processing of one Telegram message -> INBOX page."""
import html
import logging
import re
from datetime import datetime, timezone
from urllib.parse import urlsplit

import books
import config
import costs
import destinations
import drafts
import i18n
import ideas
import llm
import media
import net
import notion
import telegram

log = logging.getLogger(__name__)

SOCIAL = {"Instagram", "TikTok", "YouTube"}
AUDIO = ("voice", "video_note", "audio")  # transcribed
MEDIA = ("photo", "video", "animation", "document", "voice", "video_note", "audio", "sticker")
FILE_TYPES = {"photo": "image/jpeg", "voice": "audio/ogg", "video_note": "video/mp4", "animation": "video/mp4",
              "video": "video/mp4", "audio": "audio/mpeg", "sticker": "image/webp", "document": "application/octet-stream"}
MAX_FILE = 20 * 1024 * 1024  # Telegram's download limit for bots and Notion's single upload limit
URL_RE = re.compile(r"https?://[^\s<>\"]+")

PROMPT_SOURCES = """Search the web for reliable sources on the main facts of this idea for {brand}.
Title: {title}
Facts to check:
{facts}

For each fact (at most 6): whether sources confirm it, contradict it or nothing was found; source name; link.
Prefer primary sources, encyclopedias, newspapers and archives; say so when you only find blogs or social media.
Answer in {language} with a bullet list, no introduction."""

PROMPT_CLASSIFY = """You assist {brand}. Classify the saved item below.

Categories:
{guide}
If there is a video, decide mainly from what it says and shows (transcript, what the video shows): its \
caption comes second, since reposts often carry a caption about something else. A very short transcript may be \
noise on music.
Tags: zero or more, only if relevant.
Title: at most 8 words, about the content. Topic: one line. Summary: 2-4 sentences, only about what is in \
the text, no invented facts. Write title, topic and summary in {language}.
Book: if the item is about one specific book (recommendation, review, quote, store or publisher page), \
book_title is its title as published and book_author its author as stated in the item; leave book_author \
empty if it is not stated, unless the book is well known and you are certain. Both empty otherwise.

Platform: {platform}
URL: {urls}
Text:
{text}"""


def _schema():
    return {
        "type": "object",
        "properties": {
            "category": {"type": "string", "enum": config.CATEGORIES},
            "title": {"type": "string"},
            "topic": {"type": "string"},
            "tags": {"type": "array", "items": {"type": "string", "enum": config.TOPICS or ["-"]}},
            "summary": {"type": "string"},
            "book_title": {"type": "string"},
            "book_author": {"type": "string"},
        },
        "required": ["category", "title", "topic", "tags", "summary", "book_title", "book_author"],
        "additionalProperties": False,
    }


def extract(msg):
    text = msg.get("text") or msg.get("caption") or ""
    urls = [u.rstrip(").,;:!?»'") for u in URL_RE.findall(text)]
    for e in msg.get("entities", []) + msg.get("caption_entities", []):
        if e["type"] == "text_link":
            urls.append(e["url"])
    kind_key = next((k for k in MEDIA if k in msg), None)
    kind = (i18n.t("kind_album", n=msg["album"]) if msg.get("album") else i18n.t(f"kind_{kind_key}") if kind_key
            else i18n.t("kind_link") if urls else i18n.t("kind_text"))
    origin = msg.get("forward_origin")
    if origin:
        kind += i18n.t("kind_forwarded")
    return {
        "text": text,
        "urls": list(dict.fromkeys(urls)),
        "kind": kind,
        "origin": _origin_name(origin) if origin else "",
        "date": datetime.fromtimestamp(msg["date"], timezone.utc).isoformat(),
        "message_id": msg["message_id"],
    }


def merge_split(updates):
    """Telegram splits texts over 4096 characters into consecutive messages, and albums into one message per
    photo: both are joined back. Returns [(message or None, update_id of the last piece)].
    A joined message keeps its pieces in "parts", for their attachments and for deleting them all."""
    # shortcut: a text split across two getUpdates batches (over 100 queued updates) stays split
    groups = []
    for u in updates:
        m = u.get("message")
        prev = groups[-1][-1].get("message") if groups else None
        if m and prev and (_continues(prev, m) or _same_album(prev, m)):
            groups[-1].append(u)
        else:
            groups.append([u])
    out = []
    for g in groups:
        msgs = [u["message"] for u in g if u.get("message")]
        msg = msgs[0] if msgs else None
        if len(msgs) > 1:  # text pieces or album: one item with all the text
            msg = {**msg, "text": "\n".join(filter(None, (m.get("text") or m.get("caption") for m in msgs))),
                   "entities": [e for m in msgs for e in m.get("entities", []) + m.get("caption_entities", [])],
                   "album": len(msgs) if msg.get("media_group_id") else 0, "parts": msgs}
            msg.pop("caption", None)
        out.append((msg, g[-1]["update_id"]))
    return out


def message_ids(msg):
    """Ids of every Telegram message an item is made of."""
    return [m["message_id"] for m in msg.get("parts") or [msg]]


def _continues(prev, m):
    """Only the pieces of a split: consecutive id, same moment, previous piece nearly full, plain text,
    not forwarded. Two separate messages sent together do not meet all the conditions."""
    return (m["message_id"] == prev["message_id"] + 1 and m["chat"]["id"] == prev["chat"]["id"]
            and 0 <= m["date"] - prev["date"] <= 2 and len(prev.get("text", "")) >= 3500 and "text" in m
            and not prev.get("forward_origin") and not m.get("forward_origin"))


def _same_album(prev, m):
    return bool(m.get("media_group_id")) and m.get("media_group_id") == prev.get("media_group_id")


def _origin_name(o):
    chat = o.get("chat") or o.get("sender_chat") or {}
    user = o.get("sender_user") or {}
    return o.get("sender_user_name") or chat.get("title") or " ".join(
        filter(None, [user.get("first_name"), user.get("last_name")]))


def platform(url):
    if not url:
        return i18n.t("platform_text")
    host = re.sub(r"^(www|m|vm|vt)\.", "", urlsplit(url).netloc.lower())
    if host.endswith("instagram.com"):
        return "Instagram"
    if host.endswith("tiktok.com"):
        return "TikTok"
    if host.endswith("youtube.com") or host == "youtu.be":
        return "YouTube"
    return "Web"


def web_preview(url):
    """Title and description of the page (like Telegram's link preview). Empty string if unavailable."""
    return web_page(url)["preview"]


def web_page(url):
    """{"preview": title and description, or "", "words": words of the page's main text, 0 if unavailable}."""
    try:
        r = net.request("GET", url, retries=2, stream=True, timeout=(5, 15),
                        headers={"User-Agent": "Mozilla/5.0 (Macintosh) TelegramBot (like TwitterBot)"})
        page = r.raw.read(500_000, decode_content=True).decode(r.encoding or "utf-8", "replace")
    except Exception as e:
        log.warning("no preview for %s: %s", urlsplit(url).netloc, str(e)[:120])
        return {"preview": "", "words": 0}
    found = {}
    for tag in re.findall(r"<meta\b[^>]*>", page, re.I):
        key = re.search(r"(?:property|name)\s*=\s*[\"']([^\"']+)", tag, re.I)
        val = re.search(r"content\s*=\s*(?:\"([^\"]*)\"|'([^']*)')", tag, re.I)
        if key and val:
            found.setdefault(key.group(1).lower(), val.group(1) or val.group(2) or "")
    title = found.get("og:title") or (re.search(r"<title[^>]*>(.*?)</title>", page, re.I | re.S) or [None, ""])[1]
    desc = found.get("og:description") or found.get("description", "")
    lines = [f"{k}: {html.unescape(' '.join(v.split()))[:500]}"
             for k, v in ((i18n.t("preview_page"), r.url), (i18n.t("preview_title"), title),
                          (i18n.t("preview_description"), desc)) if v]
    return {"preview": "\n".join(lines) if title or desc else "", "words": article_words(page)}


def article_words(page):
    """Words of a page's main text: the <article> elements if any, otherwise every paragraph.
    Scripts, menus, headers, footers and sidebars are left out. Behind a paywall only the free part counts."""
    page = re.sub(r"(?is)<(script|style|noscript|nav|header|footer|aside|form)\b.*?</\1\s*>", " ", page)
    parts = re.findall(r"(?is)<article\b.*?</article\s*>", page) or re.findall(r"(?is)<p\b[^>]*>.*?</p\s*>", page)
    return len(re.findall(r"\w+", html.unescape(re.sub(r"<[^>]+>", " ", " ".join(parts)))))


def reading_minutes(words):
    """Estimated reading time in minutes, or None for pages too short to be articles."""
    r = config.READING
    if words < r.get("min_words", 300):
        return None
    return max(1, round(words / r.get("words_per_minute", 220)))


def _video_context(v):
    if not v:
        return ""
    return (i18n.t("video_ctx", title=v["title"], author=v["author"], duration=v["duration"],
                   caption=v["caption"] or i18n.t("none"), transcript=v["transcript"] or i18n.t("no_speech_short"))
            + (i18n.t("video_ctx_visual", visual=v["visual"]) if v.get("visual") else ""))


def forced_category(text):
    """Category you picked with a hashtag in the message (#recipe, #guide, or an alias), or None."""
    for tag in re.findall(r"#(\w+)", text):
        low = tag.lower()
        for cat in config.CATEGORIES + config.FORCED:
            if cat.lower().replace("_", "") == low.replace("_", ""):
                return cat
        if low in config.ALIASES:
            return config.ALIASES[low]
    return None


def first_line(text):
    """First non-empty line without hashtags, at most 80 characters ("" if there is no text)."""
    for line in (text or "").splitlines():
        line = re.sub(r"\s+", " ", re.sub(r"#\w+", "", line)).strip()
        if line:
            return line[:80]
    return ""


def _draft_blocks(d):
    return [*notion.section(i18n.t("sec_draft"), d["draft"]),
            *notion.section(i18n.t("sec_to_verify"), "\n".join(f"• {x}" for x in d["to_verify"])),
            *notion.section(i18n.t("sec_checks"), "\n".join(f"⚠️ {x}" for x in d["checks"]) or i18n.t("checks_ok"))]


def drafts_on_request(limit=5):
    """Drafts for the pages you marked "good idea" in Notion that have none. Returns Telegram messages."""
    out = []
    for r in notion.pages_to_draft()[:limit]:  # each draft costs money: the rest waits for the next run
        text, written = _draft(r)
        if written is not None:  # None: a draft was already there, nothing to say
            out.append(text)
    return out


def draft_for_page(r):
    """Writes the draft for an INBOX page (a query row or a page read) and returns the Telegram message.
    A model error becomes a message with ⚠️; a Notion error propagates."""
    return _draft(r)[0]


def _draft(r):
    p = r["properties"]
    title = notion.text(p.get(config.P("title")))
    sections = notion.page_sections(r["id"])
    if i18n.t("sec_draft") in sections:  # a draft is already there (also pages created before the checkbox)
        notion.update_inbox_page(r["id"], {config.P("draft_written"): {"checkbox": True}})
        return i18n.t("draft_already_there", title=title, url=r["url"]), None
    item = {"context": "\n\n".join(sections[k] for k in (i18n.t(s) for s in (
        "sec_original_text", "sec_voice_transcript", "sec_caption", "sec_transcript", "sec_visual", "sec_preview",
        "sec_summary")) if sections.get(k))[:30000]}
    try:
        d = drafts.draft(item, {"title": title, "topic": notion.text(p.get(config.P("topic")))})
    except Exception as e:
        log.exception("draft failed for %s", r["url"])
        return i18n.t("draft_failed", title=title, error=str(e)[:200]), False
    notion.append_to_inbox_page(r["id"], _draft_blocks(d))
    notion.update_inbox_page(r["id"], {config.P("draft_written"): {"checkbox": True}})
    return i18n.t("draft_written", title=title, url=r["url"]), True


def classify(item, plat):
    out = llm.complete_json(config.prompt("classify", PROMPT_CLASSIFY).format(
        brand=config.BRAND, language=config.LANGUAGE, guide=config.CATEGORY_GUIDE, platform=plat,
        urls=" ".join(item["urls"]) or i18n.t("no_urls"), text=item["context"] or i18n.t("no_text")), _schema())
    return {
        "category": out.get("category") if out.get("category") in config.CATEGORIES else config.FALLBACK,
        "title": str(out.get("title") or "").strip()[:120],
        "topic": str(out.get("topic") or "").strip()[:300],
        "tags": [t for t in out.get("tags") or [] if t in config.TOPICS],
        "summary": str(out.get("summary") or "").strip(),
        "book_title": str(out.get("book_title") or "").strip()[:200],
        "book_author": str(out.get("book_author") or "").strip()[:200],
    }


def _plain(category, title):
    return {"category": category, "title": title, "topic": "", "tags": [], "summary": "",
            "book_title": "", "book_author": ""}


def attachments(msg):
    """Files of an item (every part of an album): [(kind, file, message_id)]. Photos: the largest size."""
    out = []
    for part in msg.get("parts") or [msg]:
        for k in MEDIA:
            if k in part:
                f = part[k][-1] if k == "photo" else part[k]
                out.append((k, f, part["message_id"]))
    return out


def save_attachments(msg, page_id):
    """Uploads the item's files to the INBOX page. Returns the list of problems (empty: all saved)."""
    if not config.TELEGRAM.get("upload_files", True):
        return []
    files = attachments(msg)
    if not files:
        return []
    blocks, problems = [], []
    for kind, f, mid in files:
        ctype = f.get("mime_type") or FILE_TYPES[kind]
        name = f.get("file_name") or f"{kind}_{mid}.{ctype.split('/')[-1].replace('mpeg', 'mp3')}"
        if (f.get("file_size") or 0) > MAX_FILE:
            problems.append(i18n.t("attachment_missing", name=name, reason="> 20 MB"))
            continue
        try:
            uid = notion.upload_file(name, telegram.download(f["file_id"]), ctype)
            blocks.append(notion.file_block(uid, image=kind in ("photo", "sticker") and ctype.startswith("image/")))
        except Exception as e:
            log.warning("attachment %s not saved: %s", name, str(e)[:200])
            problems.append(i18n.t("attachment_missing", name=name, reason=str(e)[:80]))
    if blocks:
        try:
            notion.append_files(page_id, [notion.heading(i18n.t("sec_attachments")), *blocks])
        except Exception as e:
            log.warning("attachments not attached to %s: %s", page_id, str(e)[:200])
            problems.append(i18n.t("attachment_missing", name=i18n.t("sec_attachments"), reason=str(e)[:80]))
    return problems


def _page_id(url):
    return re.search(r"[0-9a-f]{32}$", url.split("?")[0]).group()


def process(msg):
    """Creates the INBOX page. Returns {"reply": Telegram text, "deletable": True when everything in the
    messages is safely in Notion and analyzed, so they may be deleted from the chat}.
    An analysis error still produces a page with the Error field filled; a Notion error propagates."""
    item = extract(msg)
    forced = forced_category(item["text"]) or destinations.topic_category(msg)
    note_only = forced in config.FORCED  # a forced-only category: your own note, no analysis
    url = item["urls"][0] if item["urls"] else None
    plat = platform(url)
    existing = url and notion.find_by_url(url)
    if existing:
        return {"reply": i18n.t("already_in_inbox", url=existing), "deletable": not attachments(msg)}
    limited = costs.over_limit()
    page = web_page(url) if plat == "Web" else {"preview": "", "words": 0}
    item["preview"] = page["preview"]
    minutes = reading_minutes(page["words"])
    errors = []
    if plat in SOCIAL and not note_only:
        try:
            item["video"] = media.video(url)
        except Exception as e:  # e.g. a blocked download: classify with what there is
            log.warning("video not downloaded for message %s: %s", item["message_id"], str(e)[:200])
            item["preview"] = web_preview(url)  # e.g. an Instagram post with only photos: at least the caption
            if "No video formats" in str(e):  # a photo post or carousel: look at its images
                try:
                    item["video"] = media.photo_post(url)
                except Exception as e2:
                    log.warning("photo post not read for message %s: %s", item["message_id"], str(e2)[:200])
            if not item["preview"] and not item.get("video"):
                errors.append(f"{i18n.t('err_video')}: {e}")
    audio = next((msg[k] for k in AUDIO if k in msg), None)
    if audio:
        try:
            item["text"] = "\n".join(filter(None, [item["text"], media.transcribe(
                f"audio.{'mp4' if 'video_note' in msg else 'ogg'}", telegram.download(audio["file_id"]))]))
            item["transcribed"] = True
        except Exception as e:
            log.warning("transcription failed for message %s: %s", item["message_id"], str(e)[:200])
            errors.append(f"{i18n.t('err_transcription')}: {e}")
    item["context"] = "\n\n".join(filter(None, [item["text"], item["preview"],
                                                _video_context(item.get("video"))]))[:30000]
    if note_only:
        c = _plain(forced, first_line(item["text"]))
    elif not item["text"] and not url:  # nothing to read: no LLM call, you decide
        c = _plain(config.FALLBACK, i18n.t("without_text", kind=item["kind"].capitalize()))
    elif limited:  # spending limit reached: saved as it is, analysis skipped
        c = _plain(config.FALLBACK, first_line(item["text"]) or (url or "")[:80])
    else:
        try:
            c = classify(item, plat)
        except Exception as e:
            log.exception("classification failed for message %s", item["message_id"])
            errors.append(f"{i18n.t('err_classification')}: {e}")
            # a social video is most likely an idea: [categories] social_fallback, if set
            c = _plain(config.SOCIAL_FALLBACK if plat in SOCIAL and config.SOCIAL_FALLBACK else config.FALLBACK, "")
    if forced:
        c["category"] = forced
    book, author = c.get("book_title", ""), c.get("book_author", "")
    found = books.lookup(book, author) if book and config.BOOKS.get("open_library", True) else None
    if found and not author:
        author = found["author"]  # from Open Library, a checkable source
    title = (book if book and c["category"] == config.BOOK else c["title"]) or (
        item["text"] or url or i18n.t("untitled"))[:80]
    analysis = None
    # without the video's content a score would be made up, and an empty page would absorb unrelated links as
    # duplicates: no analysis (a social page preview is usually just the site name)
    if (config.CREATOR_ON and not limited and c["category"] == config.IDEA and c["title"]
            and (plat not in SOCIAL or item.get("video"))):
        try:
            analysis = ideas.analyze(item, c)
        except Exception as e:
            log.exception("idea analysis failed for message %s", item["message_id"])
            errors.append(f"{i18n.t('err_analysis')}: {e}")
    v = item.get("video") or {}

    P = config.P
    props = {
        P("title"): {"title": notion.rich(title)},
        P("category"): {"select": {"name": c["category"]}},
        P("platform"): {"select": {"name": plat}},
        P("url"): {"url": url},
        P("saved"): {"date": {"start": item["date"]}},
        P("status"): {"select": {"name": config.STATUSES[0]}},
        P("topic"): {"rich_text": notion.rich(c["topic"])},
        P("tags"): {"multi_select": [{"name": t} for t in c["tags"]]},
        P("message_id"): {"number": item["message_id"]},
        P("chat_id"): {"number": msg["chat"]["id"]},
    }
    if book:
        props[P("book")] = {"rich_text": notion.rich(book)}
        props[P("author")] = {"rich_text": notion.rich(author)}
    if minutes:
        props[P("reading")] = {"number": minutes}
    similar = analysis and analysis["similar"]
    if similar and similar.get("inbox"):  # same idea from another link: added to the existing page
        notion.append_to_inbox_page(similar["page_id"], [
            notion.heading(i18n.t("sec_another_link")),
            *notion.paragraphs(i18n.t("saved_on", link=url or i18n.t("platform_text"), date=item["date"][:10],
                                      title=title, summary=c["summary"])),
            *notion.section(i18n.t("sec_caption"), v.get("caption")),
            *notion.section(i18n.t("sec_transcript"), v.get("transcript")),
            *notion.section(i18n.t("sec_preview"), item["preview"]),
            *([] if url else notion.section(i18n.t("sec_original_text"), item["text"]))])
        problems = save_attachments(msg, similar["page_id"])
        reply = i18n.t("already_in_inbox_merged", title=similar["title"], url=similar["url"])
        return {"reply": "\n".join([reply, *problems]), "deletable": not problems}
    if analysis:
        props[P("score")] = {"number": analysis["score"]}
        props[P("covered")] = {"checkbox": bool(similar)}
        if similar and config.SIMILAR_TYPE == "url":
            props[P("similar")] = {"url": similar["url"]}
        elif similar and similar.get("relation_id"):
            props[P("similar")] = {"relation": [{"id": similar["relation_id"]}]}
    draft = None
    if (analysis and config.DRAFTS.get("enabled", True)
            and (analysis["score"] or 0) >= config.DRAFTS.get("min_score", 3)):
        try:
            draft = drafts.draft(item, c)
        except Exception as e:
            log.exception("draft failed for message %s", item["message_id"])
            errors.append(f"{i18n.t('err_draft')}: {e}")
    sources = ""
    s = config.SOURCES
    if analysis and s.get("enabled", True) and (analysis["score"] or 0) >= s.get("min_score", 4):
        try:
            sources = llm.search_web(config.prompt("sources", PROMPT_SOURCES).format(
                brand=config.BRAND, language=config.LANGUAGE, title=title,
                facts="\n".join(draft["to_verify"]) if draft else c["summary"]),
                s.get("max_searches", 3)).replace("**", "")  # Notion shows markdown as plain text
        except Exception as e:
            log.exception("source search failed for message %s", item["message_id"])
            errors.append(f"{i18n.t('err_sources')}: {e}")
    if draft:
        props[P("draft_written")] = {"checkbox": True}
    error = "\n".join(errors)[:1900]
    props[P("error")] = {"rich_text": notion.rich(error)}
    meta = i18n.t("meta_type", kind=item["kind"])
    if item["origin"]:
        meta += "\n" + i18n.t("meta_forwarded_from", origin=item["origin"])
    if len(item["urls"]) > 1:
        meta += "\n" + i18n.t("meta_other_links") + "\n" + "\n".join(item["urls"][1:])
    if book:
        meta += "\n" + i18n.t("meta_book", book=book) + (f" · {author}" if author else "")
    if minutes:
        meta += "\n" + i18n.t("meta_reading", minutes=minutes, words=page["words"])
    if v:
        meta += "\n" + i18n.t("meta_video", title=v["title"], author=v["author"])
        if v["duration"]:
            meta += f" · {v['duration'] // 60}:{v['duration'] % 60:02d}"
    blocks = [*notion.paragraphs(meta),
              *notion.section(i18n.t("sec_voice_transcript" if item.get("transcribed") else "sec_original_text"),
                              item["text"]),
              *notion.section(i18n.t("sec_caption"), v.get("caption")),
              *notion.section(i18n.t("sec_transcript"), v.get("transcript") or (v and not v.get("photo") and i18n.t("no_speech"))),
              *notion.section(i18n.t("sec_visual"), v.get("visual")),
              *notion.section(i18n.t("sec_preview"), item["preview"]),
              *notion.section(i18n.t("sec_summary"), c["summary"]),
              *notion.section(i18n.t("sec_book"), found and i18n.t(
                  "book_details", url=found["url"], year=found["year"] or "?", pages=found["pages"] or "?",
                  isbn=found["isbn"] or "?", publisher=found["publisher"] or "?")),
              *notion.section(i18n.t("sec_analysis"), analysis and ideas.analysis_text(analysis)),
              *(_draft_blocks(draft) if draft else []),
              *notion.section(i18n.t("sec_sources"), sources)]
    page_url = notion.create_inbox_page(props, blocks, cover=found and found["cover"] or None)
    if analysis:
        ideas.remember(title, c["topic"], item["date"], page_url, _page_id(page_url))
    problems = save_attachments(msg, _page_id(page_url))

    reply = f"{c['category']} · {title}"
    if book and author:
        reply += f" · {author}"
    if minutes:
        reply += f" · {minutes} min"
    if analysis:
        reply += f" · {analysis['score']}/5"
        if similar:
            reply += f" · {i18n.t('already_covered')}" + (f" ({similar['id']})" if similar.get("id") else "")
    reply += f"\n{page_url}"
    if error:
        reply += f"\n⚠️ {error[:200]}"
    reply = "\n".join([reply, *problems])
    skipped = limited and not note_only and bool(item["text"] or url)  # analysis that the limit prevented
    return {"reply": reply, "deletable": not error and not problems and not skipped, "category": c["category"]}
