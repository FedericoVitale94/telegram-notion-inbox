"""Telegram -> Notion inbox bot.
Usage: python main.py --once    reads new messages, processes them and exits (GitHub Actions, manual runs)
       python main.py --listen  keeps listening while the computer is on: items saved and questions answered
                                within seconds
The instance folder (config.toml, .env, prompts/, custom.py, cache/, logs/) is INBOX_HOME, or this folder."""
import argparse
import fcntl
import json
import logging
import os
import socket
import subprocess
import sys
import time
from datetime import date
from logging.handlers import RotatingFileHandler
from pathlib import Path

from dotenv import load_dotenv

HOME = Path(os.environ.get("INBOX_HOME") or Path(__file__).resolve().parent).expanduser().resolve()
load_dotenv(HOME / ".env")

import agent  # noqa: E402
import config  # noqa: E402
import costs  # noqa: E402
import destinations  # noqa: E402
import i18n  # noqa: E402
import notion  # noqa: E402
import pipeline  # noqa: E402
import telegram  # noqa: E402

STATE = config.ROOT / "cache" / "state.json"
log = logging.getLogger("bot")


def setup_logging():
    (config.ROOT / "logs").mkdir(exist_ok=True)
    handlers = [RotatingFileHandler(config.ROOT / "logs" / "bot.log", maxBytes=1_000_000, backupCount=5),
                logging.StreamHandler()]
    logging.basicConfig(level=logging.INFO, handlers=handlers,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")


def load_offset():
    try:
        return json.loads(STATE.read_text())["offset"]
    except (OSError, ValueError, KeyError):
        return 0


def save_offset(offset):
    tmp = STATE.with_suffix(".tmp")
    tmp.write_text(json.dumps({"offset": offset}))
    tmp.replace(STATE)


def run(timeout=10):
    allowed = {int(x) for x in os.environ["TELEGRAM_ALLOWED_USER_IDS"].split(",") if x.strip()}
    offset = load_offset()
    while updates := telegram.get_updates(offset, timeout):
        for msg, update_id in pipeline.merge_split(updates):
            if msg and msg.get("from", {}).get("id") in allowed:
                handle(msg)
            elif msg:
                log.warning("ignored message from unauthorized user %s", msg.get("from", {}).get("id"))
            # the offset moves on only after the item is saved in Notion (handle raises if Notion fails)
            offset = update_id + 1
            save_offset(offset)
        _cost_alert()


def handle(msg):
    mid, chat = msg["message_id"], msg["chat"]
    confirmation = destinations.topic_command(msg)
    if confirmation:
        telegram.reply(chat["id"], confirmation, mid, msg.get("message_thread_id"))
        return
    if agent.is_question(msg):  # a question for the agent: answered where it was asked, no INBOX page
        _answer_question(msg)
        return
    if destinations.ignore(msg):
        return
    existing = notion.find_by_message_id(mid, chat["id"], chat["type"] == "private")
    if existing:
        log.info("message %s already in INBOX: %s", mid, existing)
        return
    result = pipeline.process(msg)
    log.info("message %s saved: %s", mid, result["reply"].replace("\n", " | "))
    try:
        dest_chat, thread, reply_to = destinations.where("saved", msg)
        telegram.reply(dest_chat, result["reply"], reply_to, thread)
        if reply_to is None and not _deleting(result):  # reply in another topic: a reaction on the original
            telegram.react(chat["id"], mid)
    except Exception:
        log.exception("Telegram reply failed for %s (page already saved)", mid)
    if _deleting(result):
        _delete(msg)


def _deleting(result):
    return result["deletable"] and config.TELEGRAM.get("delete_saved", False)


def _delete(msg):
    """Deletes your messages that are now safely in Notion. In a group the bot needs the right to delete
    messages: without it you get one alert a day and the messages stay."""
    chat = msg["chat"]
    try:
        telegram.delete(chat["id"], pipeline.message_ids(msg))
    except Exception as e:
        log.warning("messages %s not deleted: %s", pipeline.message_ids(msg), str(e)[:200])
        mark = config.ROOT / "cache" / f"delete_alert_{chat['id']}"
        if chat["type"] != "private" and due(mark, 24):
            mark.parent.mkdir(exist_ok=True)
            mark.touch()
            _alert(i18n.t("delete_not_allowed"))


def _answer_question(msg):
    """An agent error becomes a Telegram reply: it does not block the queue and the question is not asked
    again (each question costs money)."""
    chat, mid, thread = msg["chat"]["id"], msg["message_id"], msg.get("message_thread_id")
    try:
        text = agent.answer(msg)
    except Exception as e:
        log.exception("agent failed on message %s", mid)
        text = i18n.t("ask_failed", error=str(e)[:300])
    try:
        telegram.reply(chat, text, mid, thread)
    except Exception:
        log.exception("agent answer not sent for %s", mid)


def due(mark, hours):
    """True if the mark file is missing or older than `hours`: for periodic jobs."""
    try:
        return time.time() - mark.stat().st_mtime >= hours * 3600
    except OSError:
        return True


def main():
    parser = argparse.ArgumentParser()
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--once", action="store_true", help="reads new messages, processes them and exits")
    mode.add_argument("--listen", action="store_true", help="keeps listening until the process is stopped")
    args = parser.parse_args()
    setup_logging()
    if not (config.ROOT / "config.toml").exists():
        log.error("config.toml missing in %s: copy config.example.toml and edit it", config.ROOT)
        return 1
    STATE.parent.mkdir(exist_ok=True)
    lock = open(STATE.parent / "run.lock", "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        log.info("another run is in progress, exiting")
        return 0
    if args.listen:
        return listen()
    log.info("start")
    _wait_for_network()
    _update_ytdlp()
    try:
        added = notion.ensure_properties(config.CREATOR_ON)
        if added:
            log.info("properties added to INBOX: %s", ", ".join(added))
        run()
        if config.CREATOR_ON:
            for text in pipeline.drafts_on_request():
                _alert(text, "saved")
        _weekly_digest()
    except Exception as e:
        log.exception("run interrupted, unsaved messages will be picked up next time")
        _alert(i18n.t("run_interrupted", error=e)[:500])
        return 1
    log.info("done")
    return 0


def listen():
    """Endless loop: 50-second long polling, periodic jobs when due. An error (no network, computer just woken)
    does not stop the loop: it retries after 30 seconds, with a Telegram alert only if the problem lasts
    10 minutes. After a yt-dlp upgrade the process exits and launchd restarts it with the new version."""
    log.info("start, listening")
    _wait_for_network()
    failing_since, alerted = None, False
    cache = STATE.parent
    while True:
        try:
            if due(cache / "ytdlp_checked", 24):
                (cache / "ytdlp_checked").touch()
                if _update_ytdlp():
                    log.info("yt-dlp upgraded: restarting to load it")
                    return 0
            if due(cache / "properties_checked", 24):
                added = notion.ensure_properties(config.CREATOR_ON)
                if added:
                    log.info("properties added to INBOX: %s", ", ".join(added))
                (cache / "properties_checked").touch()
            if config.CREATOR_ON and due(cache / "drafts_checked", 1):
                for text in pipeline.drafts_on_request():
                    _alert(text, "saved")
                (cache / "drafts_checked").touch()
            _weekly_digest()
            run(timeout=50)
            if failing_since is not None:
                log.info("recovered")
            failing_since, alerted = None, False
        except Exception as e:
            log.exception("error in the listening loop, retrying in 30 seconds")
            failing_since = failing_since or time.monotonic()
            if not alerted and time.monotonic() - failing_since > 600:
                _alert(i18n.t("stuck", error=e)[:500])
                alerted = True
            time.sleep(30)


def _wait_for_network(seconds=180):
    """Right after login Wi-Fi may not be up yet: wait up to 3 minutes instead of failing at once."""
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        try:
            socket.create_connection(("api.telegram.org", 443), timeout=5).close()
            return
        except OSError:
            time.sleep(10)
    log.warning("no network after %ds, trying anyway", seconds)


def _update_ytdlp():
    """Instagram and TikTok change often and yt-dlp stops working: it is upgraded once a week.
    Returns True if it was upgraded."""
    mark = STATE.parent / "ytdlp_updated"
    if not config.UPDATES.get("yt_dlp_weekly", True) or (mark.exists() and time.time() - mark.stat().st_mtime < 7 * 86400):
        return False
    r = subprocess.run([sys.executable, "-m", "pip", "install", "-q", "-U", "yt-dlp"],
                       capture_output=True, text=True, timeout=300)
    if r.returncode:
        log.warning("yt-dlp upgrade failed: %s", r.stderr[-300:])
        return False
    log.info("yt-dlp upgraded")
    mark.touch()
    return True


def _cost_alert():
    text = costs.pending_alert()
    if text:
        _alert(text)


def _weekly_digest():
    """On the chosen weekday (or the first run after a missed one): what is still to review (in creator mode
    the best ideas), what to read, and the spending."""
    d = config.DIGEST
    if not d.get("enabled", True):
        return
    mark = STATE.parent / "digest.json"
    try:
        last = date.fromisoformat(json.loads(mark.read_text())["last"])
    except (OSError, ValueError, KeyError):
        last = date.min
    today = date.today()
    if (today - last).days < 7 and not (today.weekday() == d.get("weekday", 6) and last != today):
        return
    text = digest_text(d)
    if text:
        _alert(text, "saved")
    mark.write_text(json.dumps({"last": today.isoformat()}))


def digest_text(d):
    P = config.P
    pages = [r for r in notion.mirror()
             if ((r["properties"].get(P("status")) or {}).get("select") or {}).get("name") == config.STATUSES[0]]
    pages.sort(key=lambda r: ((r["properties"].get(P("saved")) or {}).get("date") or {}).get("start", ""), reverse=True)
    category = lambda r: ((r["properties"].get(P("category")) or {}).get("select") or {}).get("name") or "-"  # noqa: E731
    title = lambda r: notion.text(r["properties"].get(P("title")))  # noqa: E731
    parts = []
    if config.CREATOR_ON:
        ideas = [r for r in pages if category(r) == config.IDEA
                 and ((r["properties"].get(P("score")) or {}).get("number") or 0) >= d.get("min_score", 4)]
        ideas.sort(key=lambda r: -r["properties"][P("score")]["number"])
        if ideas:
            parts.append(i18n.t("digest_ideas", n=len(ideas)) + "\n\n" + "\n\n".join(
                f"{int(r['properties'][P('score')]['number'])}/5 · {title(r)}\n{r['url']}" for r in ideas[:10]))
    elif pages:
        counts = {}
        for r in pages:
            counts[category(r)] = counts.get(category(r), 0) + 1
        summary = " · ".join(f"{cat} {n}" for cat, n in sorted(counts.items(), key=lambda x: -x[1]))
        parts.append(i18n.t("digest_review", n=len(pages)) + f"\n{summary}\n\n" + i18n.t("digest_latest") + "\n\n"
                     + "\n\n".join(f"{title(r)}\n{r['url']}" for r in pages[:10]))
    reading = _reading_block(pages, category, title)
    if reading:
        parts.append(reading)
    if parts or costs.spent_usd(costs.month_start()):
        parts.append(costs.digest_line())
    return "\n\n".join(parts) if parts else ""


def _reading_block(pages, category, title):
    """Articles still to read, short ones first, and how many books are waiting."""
    P, r = config.P, config.READING
    short_max = r.get("digest_short_minutes", 10)
    timed = [(p["properties"].get(P("reading")) or {}).get("number") for p in pages]
    articles = [(m, p) for m, p in zip(timed, pages) if m]
    short = sorted([a for a in articles if a[0] <= short_max], key=lambda a: a[0])[:r.get("digest_short", 3)]
    long = sorted([a for a in articles if a[0] > short_max], key=lambda a: a[0])[:r.get("digest_long", 2)]
    book_count = sum(1 for p in pages if config.BOOK and category(p) == config.BOOK)
    if not short and not long and not book_count:
        return ""
    lines = [i18n.t("digest_reading")]
    if short:
        lines += ["", i18n.t("digest_short_reads", minutes=short_max)] + [f"{m} min · {title(p)}\n{p['url']}"
                                                                         for m, p in short]
    if long:
        lines += ["", i18n.t("digest_long_reads")] + [f"{m} min · {title(p)}\n{p['url']}" for m, p in long]
    if book_count:
        lines += ["", i18n.t("digest_books", n=book_count)]
    return "\n".join(lines)


def _alert(text, kind="alerts"):
    """Message to you on Telegram: errors in the alerts topic, drafts and digest in the saved topic
    (or in the private chat if no group is configured)."""
    try:
        chat, thread, _ = destinations.where(kind)
        telegram.reply(chat, text, thread_id=thread)
    except Exception:
        log.exception("could not send the Telegram alert")


if __name__ == "__main__":
    sys.exit(main())
