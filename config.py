"""Settings from config.toml.

Code and personal data are kept apart: the code is this folder, the data (config.toml, .env, prompts/,
custom.py, cache/, logs/) is the instance folder, INBOX_HOME. Without INBOX_HOME the instance is this folder.
Without a config.toml the example is used, so tests run without one."""
import importlib.util
import os
import tomllib
from pathlib import Path

CODE = Path(__file__).resolve().parent
ROOT = Path(os.environ.get("INBOX_HOME") or CODE).expanduser().resolve()  # the instance folder

# Notion property names, keyed by role. An existing INBOX keeps its own names via [notion.properties].
DEFAULT_PROPERTIES = {
    "title": "Title", "category": "Category", "platform": "Platform", "url": "URL", "saved": "Saved",
    "status": "Status", "topic": "Topic", "tags": "Tags", "message_id": "Telegram message ID",
    "chat_id": "Telegram chat ID", "error": "Error", "book": "Book", "author": "Author",
    "reading": "Reading time (min)", "score": "Idea score", "covered": "Already covered",
    "similar": "Similar content", "draft_written": "Draft written",
}
DEFAULT_STATUSES = ["To review", "Good idea", "Discarded", "Used"]  # to review, good idea, discarded, used
PROMPTS = ("classify", "idea", "sources", "draft", "visual", "agent")


def load(path=None):
    """(Re)reads the settings. Tests call it with another file."""
    g = globals()
    path = Path(path) if path else (ROOT / "config.toml" if (ROOT / "config.toml").exists()
                                    else CODE / "config.example.toml")
    with open(path, "rb") as f:
        c = tomllib.load(f)
    cats = c["categories"]
    g.update(
        FILE=path,
        BRAND=c["brand"]["description"],
        LANGUAGE=c["brand"].get("language", "English"),
        UI_LANGUAGE=c.get("ui", {}).get("language", "en"),
        UI_STRINGS=c.get("ui", {}).get("strings", {}),
        CATEGORY_LIST=cats["list"],                    # name -> description, in file order
        CATEGORIES=list(cats["list"]),                 # the classifier picks among these
        FORCED=list(cats.get("forced", {})),           # only with a hashtag or a topic, never guessed
        CATEGORY_LIST_FORCED=cats.get("forced", {}),
        ALIASES={k.lower(): v for k, v in cats.get("aliases", {}).items()},  # short hashtags
        FALLBACK=cats.get("fallback", list(cats["list"])[-1]),
        BOOK=cats.get("book"),
        SOCIAL_FALLBACK=cats.get("social_fallback"),  # for a social video when classification fails
        TOPICS=c.get("topics", {}).get("names", []),
        PROPERTIES={**DEFAULT_PROPERTIES, **c.get("notion", {}).get("properties", {})},
        STATUSES=c.get("notion", {}).get("statuses", DEFAULT_STATUSES),
        SIMILAR_TYPE=c.get("notion", {}).get("similar_type", "url"),  # url, or relation to an archive page
        ARCHIVE=c.get("archive", {}),
        DRAFTS=c.get("drafts", {}),
        VIDEO=c.get("video", {}),
        SOURCES=c.get("sources", {}),
        DIGEST=c.get("digest", {}),
        UPDATES=c.get("updates", {}),
        READING=c.get("reading", {}),
        AGENT=c.get("agent", {}),
        TELEGRAM=c.get("telegram", {}),
        BOOKS=c.get("books", {}),
        COSTS=c.get("costs", {}),
        CREATOR=c.get("creator", {}),
    )
    g["CATEGORY_GUIDE"] = "\n".join(f"- {name}: {desc}" for name, desc in CATEGORY_LIST.items())
    # configs written before creator mode existed have categories.idea and no [creator]: they keep it on
    g["CREATOR_ON"] = CREATOR.get("enabled", "creator" not in c and "idea" in cats)
    g["IDEA"] = CREATOR.get("idea_category") or cats.get("idea")
    all_cats = CATEGORIES + FORCED
    assert FALLBACK in CATEGORIES, "categories.fallback must be in categories.list"
    assert BOOK is None or BOOK in CATEGORIES, "categories.book must be in categories.list"
    assert SOCIAL_FALLBACK is None or SOCIAL_FALLBACK in CATEGORIES, "categories.social_fallback must be in categories.list"
    assert not CREATOR_ON or IDEA in CATEGORIES, "with creator mode on, creator.idea_category must be in categories.list"
    assert all(v in all_cats for v in ALIASES.values()), "every categories.aliases value must be a category"
    assert len(STATUSES) == 4, "notion.statuses needs 4 names: to review, good idea, discarded, used"
    _hooks.clear()


def P(role):
    """The Notion name of a property, by role (title, category, status...)."""
    return PROPERTIES[role]


def prompt(name, default):
    """The prompt template: prompts/<name>.md in the instance folder if it exists, otherwise the built-in one."""
    f = ROOT / "prompts" / f"{name}.md"
    return f.read_text() if f.exists() else default


_hooks = {}


def hook(name):
    """A function from custom.py in the instance folder, or None. custom.py is loaded by its path, never from
    sys.path, so files in the instance folder cannot shadow the bot's own modules."""
    if "module" not in _hooks:
        f = ROOT / "custom.py"
        mod = None
        if f.exists():
            spec = importlib.util.spec_from_file_location("inbox_custom", f)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
        _hooks["module"] = mod
    return getattr(_hooks["module"], name, None) if _hooks["module"] else None


load()
