import os

import net


def _call(method, **params):
    url = f"https://api.telegram.org/bot{os.environ['TELEGRAM_BOT_TOKEN']}/{method}"
    data = net.request("POST", url, json=params).json()
    if not data.get("ok"):
        raise net.HttpError(None, f"telegram {method}: {data.get('description')}")
    return data["result"]


MAX_TEXT = 4096  # Telegram's limit per message, in UTF-16 units


def get_updates(offset, timeout=10):
    # passing offset confirms every earlier update to Telegram
    # long polling: with timeout=0 Telegram sometimes answers empty while messages are queued
    return _call("getUpdates", offset=offset, timeout=timeout, allowed_updates=["message"])


def _units(s):
    return sum(2 if ord(c) > 0xFFFF else 1 for c in s)


def pieces(text, n=MAX_TEXT):
    """Splits a long text into messages of at most n UTF-16 units, cutting at a line end when possible."""
    out = []
    while _units(text) > n:
        cut, size = 0, 0
        for i, c in enumerate(text):  # last index that fits
            size += 2 if ord(c) > 0xFFFF else 1
            if size > n:
                break
            cut = i + 1
        line = text.rfind("\n", 0, cut)
        if line > cut // 2:  # a line end in the second half: cut there
            cut = line + 1
        out.append(text[:cut].rstrip("\n"))
        text = text[cut:]
    return out + ([text] if text or not out else [])


def reply(chat_id, text, reply_to=None, thread_id=None):
    """One message (or several, if long); only the first replies to the original message."""
    for i, piece in enumerate(pieces(text)):
        params = {"chat_id": chat_id, "text": piece, "link_preview_options": {"is_disabled": True}}
        if thread_id is not None:
            params["message_thread_id"] = thread_id
        if reply_to and i == 0:
            params["reply_parameters"] = {"message_id": reply_to, "allow_sending_without_reply": True}
        _call("sendMessage", **params)


def react(chat_id, message_id, emoji="👌"):
    """A reaction on your message: in a group the reply goes to another topic, so you see it was saved."""
    _call("setMessageReaction", chat_id=chat_id, message_id=message_id,
          reaction=[{"type": "emoji", "emoji": emoji}])


def delete(chat_id, message_ids):
    """Deletes messages (up to 100 per call). Telegram allows it within 48 hours of sending; in a group the bot
    must be an admin with the right to delete messages. Raises net.HttpError when Telegram refuses."""
    for i in range(0, len(message_ids), 100):
        _call("deleteMessages", chat_id=chat_id, message_ids=message_ids[i:i + 100])


def download(file_id):
    """Content of a file sent to the bot (max 20MB, a Bot API limit)."""
    path = _call("getFile", file_id=file_id)["file_path"]
    return net.request("GET", f"https://api.telegram.org/file/bot{os.environ['TELEGRAM_BOT_TOKEN']}/{path}").content
