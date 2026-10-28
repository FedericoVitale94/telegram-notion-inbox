"""HTTP with retries and backoff on 429 / 5xx / network errors."""
import logging
import time
from urllib.parse import urlsplit

import requests

log = logging.getLogger(__name__)


class HttpError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status


def request(method, url, retries=5, **kw):
    kw.setdefault("timeout", (5, 60))  # 5s to connect: an unreachable address fails fast
    host = urlsplit(url).netloc  # never the full URL in messages: it can contain the Telegram token
    for attempt in range(retries):
        last = attempt == retries - 1
        try:
            r = requests.request(method, url, **kw)
        except requests.RequestException as e:
            if last:
                raise HttpError(None, f"{host}: {type(e).__name__}") from None
            wait = 2 ** attempt * 2
        else:
            if r.status_code < 400:
                return r
            if r.status_code != 429 and r.status_code < 500 or last:
                raise HttpError(r.status_code, f"{host} {r.status_code}: {r.text[:300]}")
            wait = float(r.headers.get("Retry-After") or 2 ** attempt * 2)
        log.warning("%s: attempt %d failed, retrying in %.0fs", host, attempt + 1, wait)
        time.sleep(min(wait, 120))
