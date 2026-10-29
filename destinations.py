"""Where the bot writes: the private chat (as always) or a private group with Topics.

In the group, "/here saved" in a topic makes it the one for the bot's replies, "/here alerts" the one for errors,
and "/here <category>" (or one of its hashtag aliases) makes everything written in that topic go to that category,
and the replies for items of that category go there instead of the saved topic.
Every other topic collects items as usual. The command and the two words can be changed in [telegram]."""
import json
import os
import re

import config
import i18n

CONTENT = ("text", "caption", "photo", "video", "document", "voice", "audio", "video_note", "animation", "sticker")


def _file():
    return config.ROOT / "cache" / "topics.json"


def _words():
    t = config.TELEGRAM
    return t.get("here_command", "here"), t.get("saved_word", "saved"), t.get("alerts_word", "alerts")


def _config():
    try:
        return json.loads(_file().read_text())
    except (OSError, ValueError):
        return {}


def _save(cfg):
    f = _file()
    f.parent.mkdir(exist_ok=True)
    tmp = f.with_suffix(".tmp")
    tmp.write_text(json.dumps(cfg))
    tmp.replace(f)


def _category(word):
    w = word.lower().replace("_", "")
    for cat in config.CATEGORIES + config.FORCED:
        if cat.lower().replace("_", "") == w:
            return cat
    return config.ALIASES.get(word.lower())


def topic_command(msg):
    """Handles "/here saved|alerts|<category>" in a group. Returns the confirmation, or None if it is not the command."""
    here, saved, alerts = _words()
    m = re.match(rf"^/{re.escape(here)}(?:@\w+)?\s+(\S+)\s*$", msg.get("text") or "", re.I)
    if not m or msg["chat"]["type"] == "private":
        return None
    if msg.get("message_thread_id") is None:
        return i18n.t("topic_needed")
    what = m.group(1)
    cfg = _config()
    if cfg.get("chat_id") != msg["chat"]["id"]:
        cfg = {"chat_id": msg["chat"]["id"]}  # a new group: start over
    thread = msg["message_thread_id"]
    cats = {k: v for k, v in cfg.get("categories", {}).items() if k != str(thread)}
    if what.lower() in (saved.lower(), alerts.lower()):
        role = "saved" if what.lower() == saved.lower() else "alerts"
        cfg[role] = thread
        cfg["categories"] = cats
        _save(cfg)
        return i18n.t("topic_set", what=what.lower())
    category = _category(what)
    if not category:
        return i18n.t("topic_unknown", what=what, saved=saved, alerts=alerts)
    cfg["categories"] = {**cats, str(thread): category}
    for role in ("saved", "alerts"):  # a topic has one role only
        if cfg.get(role) == thread:
            cfg.pop(role)
    _save(cfg)
    return i18n.t("topic_category", category=category)


def ignore(msg):
    """Service messages (topic created, members, pins) and messages in the bot's own topics."""
    if not any(k in msg for k in CONTENT):
        return True
    cfg = _config()
    thread = msg.get("message_thread_id")
    return msg["chat"]["id"] == cfg.get("chat_id") and thread is not None and thread in (
        cfg.get("saved"), cfg.get("alerts"))


def topic_category(msg):
    """The category of the topic the message was written in, if the topic has one."""
    cfg = _config()
    thread = msg.get("message_thread_id")
    if thread is None or msg["chat"]["id"] != cfg.get("chat_id"):
        return None
    return cfg.get("categories", {}).get(str(thread))


def where(kind, msg=None, category=None):
    """(chat_id, thread_id, reply_to) for a "saved" or "alerts" message.
    Private chat: a reply to the message, as always. Configured group: the category's topic if it has one,
    otherwise the chosen topic.
    Without an original message (alerts, drafts, digest): the topic if there is one, otherwise the private chat."""
    cfg = _config()
    if msg and msg["chat"]["type"] == "private":
        return msg["chat"]["id"], None, msg["message_id"]
    topics = {c: int(t) for t, c in cfg.get("categories", {}).items()}
    if category in topics and msg and msg["chat"]["id"] == cfg.get("chat_id"):
        return cfg["chat_id"], topics[category], None
    if cfg.get(kind) is not None and (msg is None or msg["chat"]["id"] == cfg["chat_id"]):
        return cfg["chat_id"], cfg[kind], None
    if msg:  # a group without configured topics: reply where the message arrived
        return msg["chat"]["id"], msg.get("message_thread_id"), msg["message_id"]
    return int(os.environ["TELEGRAM_ALLOWED_USER_IDS"].split(",")[0].strip()), None, None
