"""Spending on Anthropic: tokens and web searches of every call, priced per model.

Prices are USD per million tokens (platform.claude.com/docs/en/about-claude/pricing, October 2026) and can be
changed in [costs.prices]. Euros use the European Central Bank's daily reference rate, read at most once a day;
without it amounts are shown in dollars and the euro limit is compared using [costs] usd_per_eur, if set."""
import json
import logging
import re
import time
from datetime import date, timedelta

import config
import net

log = logging.getLogger(__name__)

PRICES = {  # USD per million tokens; cache_write is the 5-minute cache
    "claude-sonnet-5-5": {"input": 2.0, "output": 10.0, "cache_read": 0.10, "cache_write": 2.50},
    "claude-opus-5-5": {"input": 4.0, "output": 20.0, "cache_read": 0.20, "cache_write": 5.0},
    "claude-haiku-5-5": {"input": 0.10, "output": 0.50, "cache_read": 0.01, "cache_write": 0.125},
}
WEB_SEARCH_USD = 0.01  # 10 USD per 1,000 searches
ECB = "https://www.ecb.europa.eu/stats/eurofxref/eurofxref-daily.xml"
KEEP_DAYS = 400


class LimitReached(Exception):
    """The monthly spending limit is reached: no paid calls until the 1st."""


def _file():
    return config.ROOT / "cache" / "costs.json"


def _read():
    try:
        return json.loads(_file().read_text())
    except (OSError, ValueError):
        return {}


def _write(data):
    f = _file()
    f.parent.mkdir(exist_ok=True)
    tmp = f.with_suffix(".tmp")
    tmp.write_text(json.dumps(data))
    tmp.replace(f)


def _price(model):
    prices = {**PRICES, **config.COSTS.get("prices", {})}
    for name, p in prices.items():
        if model.startswith(name):
            return p
    log.warning("no price for model %s: its cost is not counted", model)
    return None


def usd(model, usage):
    """Cost in USD of one API response's usage (input, output, cache, web searches)."""
    p = _price(model)
    if not p or usage is None:
        return 0.0
    g = lambda k: getattr(usage, k, 0) or 0  # noqa: E731
    total = (g("input_tokens") * p["input"] + g("output_tokens") * p["output"]
             + g("cache_read_input_tokens") * p["cache_read"]
             + g("cache_creation_input_tokens") * p["cache_write"]) / 1_000_000
    tools = getattr(usage, "server_tool_use", None)
    return total + (getattr(tools, "web_search_requests", 0) or 0) * WEB_SEARCH_USD


def record(model, usage):
    """Adds one API response to today's spending. Never raises: accounting must not break the bot."""
    try:
        cost = usd(model, usage)
        if not cost:
            return
        data = _read()
        days = data.setdefault("days", {})
        today = date.today().isoformat()
        days[today] = round(days.get(today, 0) + cost, 6)
        oldest = (date.today() - timedelta(days=KEEP_DAYS)).isoformat()
        data["days"] = {d: v for d, v in days.items() if d >= oldest}
        _write(data)
    except Exception:
        log.exception("spending not recorded")


def spent_usd(start, end=None):
    """USD spent from date `start` to `end` (both included, end = today)."""
    end = (end or date.today()).isoformat()
    return sum(v for d, v in _read().get("days", {}).items() if start.isoformat() <= d <= end)


def usd_per_eur():
    """USD for one euro: the ECB rate of the day (cached a day), else [costs] usd_per_eur, else None."""
    data = _read()
    rate = data.get("rate", {})
    if rate.get("value") and time.time() - rate.get("at", 0) < 86400:
        return rate["value"]
    try:
        xml = net.request("GET", ECB, retries=2, timeout=(5, 15)).text
        value = float(re.search(r"currency=['\"]USD['\"]\s+rate=['\"]([\d.]+)", xml).group(1))
        data["rate"] = {"value": value, "at": time.time()}
        _write(data)
        return value
    except Exception as e:
        log.warning("ECB exchange rate unavailable: %s", str(e)[:120])
        return rate.get("value") or config.COSTS.get("usd_per_eur")


def fmt(amount_usd):
    rate = usd_per_eur()
    return f"€{amount_usd / rate:.2f}" if rate else f"${amount_usd:.2f}"


def month_start():
    return date.today().replace(day=1)


def limit_eur():
    """Monthly limit in euros; 0 means no limit."""
    return float(config.COSTS.get("monthly_limit_eur", 0) or 0)


def month_eur():
    rate = usd_per_eur()
    return spent_usd(month_start()) / rate if rate else None


def over_limit():
    """True when a monthly limit is set and this month's spending reached it."""
    limit = limit_eur()
    if not limit:
        return False
    spent = month_eur()
    return spent is not None and spent >= limit


def check():
    """Raises LimitReached when the monthly limit is reached. Called before every paid call."""
    if over_limit():
        raise LimitReached("monthly spending limit reached")


def pending_alert():
    """The alert to send at 80% and at 100% of the limit, once each per month; None otherwise."""
    import i18n
    limit = limit_eur()
    spent = month_eur() if limit else None
    if not limit or spent is None:
        return None
    level = "100" if spent >= limit else "80" if spent >= 0.8 * limit else None
    if not level:
        return None
    data = _read()
    month = month_start().isoformat()[:7]
    sent = data.setdefault("alerts", {}).setdefault(month, [])
    if level in sent:
        return None
    sent.append(level)
    data["alerts"] = {m: v for m, v in data["alerts"].items() if m == month}
    _write(data)
    key = "cost_limit_reached" if level == "100" else "cost_limit_near"
    return i18n.t(key, spent=f"€{spent:.2f}", limit=f"€{limit:.2f}")


def digest_line():
    """Spending of the last 7 days and of this month, for the weekly digest."""
    import i18n
    week = spent_usd(date.today() - timedelta(days=6))
    line = i18n.t("digest_costs", week=fmt(week), month=fmt(spent_usd(month_start())))
    if limit_eur():
        line += i18n.t("digest_costs_limit", limit=f"€{limit_eur():.2f}")
    return line
