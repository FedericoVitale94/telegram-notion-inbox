"""Book details from Open Library (openlibrary.org, free, no key): a page with year, pages, ISBN, publisher
and cover that can be checked, instead of relying only on the model."""
import logging
import re
import unicodedata

import net

log = logging.getLogger(__name__)

SEARCH = "https://openlibrary.org/search.json"
FIELDS = "key,title,author_name,first_publish_year,number_of_pages_median,isbn,publisher,cover_i"


def _words(s):
    s = unicodedata.normalize("NFKD", (s or "").lower())
    s = "".join(c for c in s if not unicodedata.combining(c))
    return set(re.findall(r"[a-z0-9]+", s))


def _matches(wanted, found):
    """The found title has most of the words of the wanted one (subtitles and articles aside)."""
    w, f = _words(wanted), _words(found)
    return bool(w) and len(w & f) >= max(1, round(0.6 * len(w)))


def lookup(title, author=""):
    """{"url", "title", "author", "year", "pages", "isbn", "publisher", "cover"} of the best match, or None."""
    params = {"title": title, "limit": 5, "fields": FIELDS}
    if author:
        params["author"] = author
    try:
        docs = net.request("GET", SEARCH, params=params, retries=2, timeout=(5, 15),
                           headers={"User-Agent": "telegram-notion-inbox (personal bot)"}).json().get("docs", [])
    except Exception as e:
        log.warning("Open Library unavailable: %s", str(e)[:120])
        return None
    doc = next((d for d in docs if _matches(title, d.get("title"))), None)
    if not doc:
        return None
    isbns = doc.get("isbn") or []
    isbn = next((i for i in isbns if len(i) == 13), isbns[0] if isbns else "")
    return {
        "url": f"https://openlibrary.org{doc['key']}",
        "title": doc.get("title", ""),
        "author": ", ".join((doc.get("author_name") or [])[:2]),
        "year": doc.get("first_publish_year") or "",
        "pages": doc.get("number_of_pages_median") or "",
        "isbn": isbn,
        "publisher": ((doc.get("publisher") or [""])[0]),
        "cover": f"https://covers.openlibrary.org/b/id/{doc['cover_i']}-M.jpg" if doc.get("cover_i") else "",
    }
