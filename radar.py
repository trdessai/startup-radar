#!/usr/bin/env python3
"""
Indian Startup Radar — free edition.

Watches Indian startup RSS feeds + Google News, scores stories with rules (no LLM,
no API bill), and posts the good ones to Slack. Runs on GitHub Actions.

    python radar.py --dry-run     # print, post nothing
    python radar.py               # fetch, score, post to Slack

Everything here is free: RSS is free, Google News RSS is free, Slack incoming
webhooks are free, and GitHub Actions is free and unlimited on public repos.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher
from pathlib import Path
from urllib.parse import parse_qsl, quote_plus, urlencode, urlsplit, urlunsplit

import feedparser
import requests
import yaml

ROOT = Path(__file__).resolve().parent
STATE_FILE = ROOT / "state" / "seen.json"
DATA_FILE = ROOT / "public" / "data.json"
DASHBOARD_DAYS = 7          # how much history the dashboard keeps
DASHBOARD_MAX = 300         # hard cap so the JSON stays small enough to fetch fast
UA = "Mozilla/5.0 (compatible; StartupRadar/1.0)"
TIMEOUT = 20


def now() -> datetime:
    return datetime.now(timezone.utc)


# ===========================================================================
# 1. Item
# ===========================================================================

@dataclass
class Item:
    title: str
    url: str
    source: str
    published: datetime
    summary: str = ""
    weight: int = 0            # per-feed trust bump from config
    india_native: bool = False

    # filled in by scoring
    score: int = 0
    category: str = "other"
    company: str = ""
    amount: str = ""
    stage: str = ""
    founders: list[str] = field(default_factory=list)
    handles: dict[str, str] = field(default_factory=dict)
    reasons: list[str] = field(default_factory=list)

    @property
    def age_min(self) -> float:
        return (now() - self.published).total_seconds() / 60

    def blob(self) -> str:
        return f"{self.title}. {self.summary}"


# ===========================================================================
# 2. Dedupe
# ===========================================================================

_TRACKING = re.compile(r"^(utm_|fbclid|gclid|mc_[ce]id|igshid|ref$|ref_src|cmpid|_ga|source$)", re.I)
_PUNCT = re.compile(r"[^a-z0-9 ]+")
_STOP = {"the", "a", "an", "of", "in", "on", "for", "to", "and", "with", "at", "by",
         "is", "as", "its", "it", "that", "this", "from", "after", "amid", "says"}

# Amounts and round stages are the only thing separating "Zepto raises $60M Series D"
# from "Zepto raises $340M Series F". Default tokenising destroys both — the single
# letter is dropped as too short, and 60 vs 340 is a rounding error against everything
# else the headlines share. Glue them into one token each so they survive.
_PRESERVE = [
    (re.compile(r"\bseries\s+([a-j])\b", re.I), r"series\1"),
    (re.compile(r"(\d[\d,.]*)\s*(crore|cr|lakh|million|billion|mn|bn|m|k)\b", re.I), r"\1\2"),
]
_SALIENT = re.compile(r"^(?:\d[\d.]*(?:crore|cr|lakh|million|billion|mn|bn|m|k)|series[a-j])$", re.I)

# Words in a third of all Indian startup headlines. Two stories sharing only these
# share nothing: "Startup launches AI platform" vs "Startup launches AI tool".
BEAT_VOCAB = {
    "startup", "startups", "company", "firm", "launch", "launches", "launched",
    "launching", "raises", "raised", "raising", "funding", "round", "new", "india",
    "indian", "app", "platform", "product", "service", "tool", "ai", "tech",
    "million", "crore", "backed", "announces", "announced", "unveils", "expands",
    "based", "business", "market", "users", "customers", "first",
}


def canonical_url(url: str) -> str:
    try:
        p = urlsplit(url.strip())
    except ValueError:
        return url.strip()
    q = [(k, v) for k, v in parse_qsl(p.query) if not _TRACKING.match(k)]
    host = p.netloc.lower().removeprefix("www.").removeprefix("m.").removeprefix("amp.")
    return urlunsplit(("https", host, p.path.rstrip("/") or "/", urlencode(q), ""))


def norm_title(title: str) -> str:
    text = title.lower()
    for pat, rep in _PRESERVE:
        text = pat.sub(rep, text)
    words = _PUNCT.sub(" ", text).split()
    return " ".join(w for w in words if w not in _STOP and len(w) > 1)


def same_story(a: str, b: str, ratio_threshold: float = 0.86,
               overlap_threshold: float = 0.70) -> bool:
    """Do two normalised headlines describe the same story?

    Thresholds lean toward treating a borderline pair as TWO stories. A duplicate
    alert wastes ten seconds; a wrongly merged story is one you never see.
    """
    if not a or not b:
        return False
    ta, tb = set(a.split()), set(b.split())
    if len(ta) < 3 or len(tb) < 3:
        return a == b

    # Different amounts or round stages = same company, different news.
    sa = {t for t in ta if _SALIENT.match(t)}
    sb = {t for t in tb if _SALIENT.match(t)}
    if sa and sb and not (sa & sb):
        return False

    shared = ta & tb
    if len(shared - BEAT_VOCAB) < 2:      # overlap must be real content, not boilerplate
        return False
    if len(shared) / min(len(ta), len(tb)) >= overlap_threshold:
        return True
    return SequenceMatcher(None, a, b).ratio() >= ratio_threshold


class Seen:
    """Dedupe state as a small JSON file, committed back to the repo each run.

    A plain file beats actions/cache here: cache entries can be evicted, and every
    eviction means re-alerting everything. Committing also counts as repository
    activity, which is what stops GitHub disabling the schedule after 60 days.
    """

    def __init__(self, path: Path = STATE_FILE, keep_days: int = 5):
        self.path = path
        self.keep_days = keep_days
        self.records: list[dict] = []
        if path.exists():
            try:
                self.records = json.loads(path.read_text()).get("items", [])
            except (json.JSONDecodeError, OSError):
                print("! state file unreadable, starting fresh")
        cutoff = (now() - timedelta(days=keep_days)).isoformat()
        self.records = [r for r in self.records if r.get("d", "") > cutoff]
        self._urls = {r["u"] for r in self.records}

    def is_dup(self, item: Item) -> bool:
        if canonical_url(item.url) in self._urls:
            return True
        n = norm_title(item.title)
        return any(same_story(n, r["t"]) for r in self.records)

    def add(self, item: Item) -> None:
        u = canonical_url(item.url)
        self.records.append({"u": u, "t": norm_title(item.title), "d": now().isoformat()})
        self._urls.add(u)

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(
            {"updated": now().isoformat(), "items": self.records}, indent=1))


# ===========================================================================
# 3. Fetch
# ===========================================================================

def _utc(st) -> datetime:
    return datetime(*st[:6], tzinfo=timezone.utc) if st else now()


def _clean(s: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", s or "")).strip()


def fetch_feed(feed: dict, cutoff: datetime) -> list[Item]:
    try:
        r = requests.get(feed["url"], headers={"User-Agent": UA}, timeout=TIMEOUT)
        r.raise_for_status()
        parsed = feedparser.parse(r.content)
    except Exception as e:
        print(f"  ! {feed['name']}: {type(e).__name__}")
        return []

    out = []
    for e in parsed.entries[:50]:
        pub = _utc(e.get("published_parsed") or e.get("updated_parsed"))
        if pub < cutoff:
            continue
        title, link = _clean(e.get("title", "")), e.get("link", "")
        if title and link:
            out.append(Item(title=title, url=link, source=feed["name"], published=pub,
                            summary=_clean(e.get("summary", ""))[:1200],
                            weight=feed.get("weight", 0),
                            india_native=feed.get("india_native", False)))
    return out


GNEWS = "https://news.google.com/rss/search?q={q}&hl=en-IN&gl=IN&ceid=IN:en"


def fetch_google_news(query: str, cutoff: datetime) -> list[Item]:
    try:
        r = requests.get(GNEWS.format(q=quote_plus(query)),
                         headers={"User-Agent": UA}, timeout=TIMEOUT)
        parsed = feedparser.parse(r.content)
    except Exception as e:
        print(f"  ! gnews '{query[:28]}': {type(e).__name__}")
        return []

    out = []
    for e in parsed.entries[:20]:
        pub = _utc(e.get("published_parsed"))
        if pub < cutoff:
            continue
        publisher = (e.get("source") or {}).get("title", "Google News")
        title = re.sub(rf"\s+-\s+{re.escape(publisher)}$", "", _clean(e.get("title", "")))
        if title and e.get("link"):
            out.append(Item(title=title, url=e["link"], source=publisher, published=pub,
                            summary=_clean(e.get("summary", ""))[:600]))
    return out


def fetch_all(cfg: dict) -> list[Item]:
    cutoff = now() - timedelta(minutes=cfg["lookback_minutes"])
    items: list[Item] = []
    with ThreadPoolExecutor(max_workers=10) as pool:
        jobs = [pool.submit(fetch_feed, f, cutoff) for f in cfg["feeds"]]
        jobs += [pool.submit(fetch_google_news, q, cutoff)
                 for q in cfg.get("google_news_queries", [])]
        for job in as_completed(jobs):
            try:
                items += job.result()
            except Exception as e:
                print(f"  ! source crashed: {e}")
    return items


# ===========================================================================
# 4. Extraction  (regex, no model)
# ===========================================================================

CATEGORY_PATTERNS = [
    ("launch", 25, re.compile(
        r"\b(launch(?:es|ed|ing)?|unveil(?:s|ed)?|debut(?:s|ed)?|roll(?:s|ed) out"
        r"|goes live|went live|out of stealth|introduc(?:es|ed)|now live|go(?:es)? public with)\b", re.I)),
    ("funding", 22, re.compile(
        r"\b(rais(?:es|ed|ing)|secur(?:es|ed)|bag(?:s|ged)|clos(?:es|ed) .{0,25}round"
        r"|funding|led by|mops up|pockets)\b", re.I)),
    ("acquisition", 18, re.compile(
        r"\b(acquir(?:es|ed|ing)|acquisition|buys?|bought|merge[sr]?|takeover)\b", re.I)),
    ("product", 15, re.compile(
        r"\b(releases?|released|adds?|added|announc(?:es|ed)|ships?|shipped|beta)\b", re.I)),
    ("expansion", 10, re.compile(
        r"\b(expand(?:s|ed)?|expansion|enters?|entered|foray|opens? .{0,20}(?:office|store|city))\b", re.I)),
]

MONEY_RE = re.compile(
    r"(rs\.?|inr|₹|\$|usd)?\s?(\d[\d,]*(?:\.\d+)?)\s*(crore|cr|lakh|billion|bn|million|mn|m|k)\b", re.I)
STAGE_RE = re.compile(r"\b(pre-?seed|seed|series\s+[a-j]|angel|bridge)\b", re.I)

# Junk that should never reach the channel.
NOISE_RE = re.compile(
    r"\b(top \d+|best \d+|\d+ (?:best|top|ways|things|reasons|startups)|listicle|round-?up"
    r"|weekly (?:wrap|digest|recap)|explain(?:ed|er)|opinion|editorial|analysis"
    r"|here'?s (?:how|why|what)|what is|why (?:india|the|this)|how to|guide to"
    r"|year in review|looking back|deep dive|interview|podcast|webinar|sponsored)\b", re.I)
MARKET_RE = re.compile(
    r"\b(sensex|nifty|share price|stock (?:price|market)|market cap|q[1-4] results"
    r"|quarterly (?:results|earnings)|gold rate|bullion|mutual fund|nav\b)\b", re.I)
PEOPLE_RE = re.compile(r"\b(appoints?|appointed|hires?|hired|joins? as|steps? down|resigns?|quits?)\b", re.I)

INDIA_RE = re.compile(
    r"\b(india|indian|bharat|bengaluru|bangalore|mumbai|delhi|gurugram|gurgaon|noida"
    r"|hyderabad|chennai|pune|kolkata|ahmedabad|jaipur|kochi|indore|chandigarh"
    r"|crore|lakh|rupee|inr|sebi|rbi|dpiit|upi)\b|₹", re.I)

LEAD_STRIP = re.compile(r"^(exclusive|breaking|update|watch|video|just in)\s*[:\-–]\s*", re.I)
DESCRIPTORS = {"indian", "india's", "homegrown", "startup", "startups", "fintech",
               "saas", "d2c", "edtech", "healthtech", "agritech", "insurtech",
               "deeptech", "spacetech", "cleantech", "ai", "new", "this", "quick",
               "commerce", "logistics", "b2b", "b2c", "stealth", "stealth-mode",
               "unicorn", "soonicorn", "app", "platform", "firm", "company"}

# Role keywords must be case-insensitive ("Founded by" starts a sentence), but the
# NAME group must stay case-sensitive or it matches any two lowercase words. Inline
# (?i:...) scopes the flag to just the keyword.
NAME = r"[A-Z][a-zA-Z'\u2019]+(?:\s+[A-Z][a-zA-Z'\u2019]+){1,2}"
FOUNDER_RES = [
    re.compile(rf"(?i:(?:co-?)?found(?:ed|er)|started|set up)\s+(?i:by)\s+({NAME})"),
    re.compile(rf"\b(?i:co-?founder|founder|ceo|chief executive)[,\s:]+"
               rf"(?:(?i:and)\s+\w+\s+)?({NAME})"),
    re.compile(rf"({NAME}),?\s+(?i:(?:the\s+)?(?:co-?)?founder|ceo)\b"),
]
AND_NAME = re.compile(rf"(?i:and)\s+({NAME})")
HANDLE_RE = re.compile(r"@([A-Za-z0-9_]{2,15})\b")

# Words that look like names but aren't people.
NOT_A_NAME = {
    "new delhi", "united states", "south india", "north india", "tamil nadu",
    "uttar pradesh", "madhya pradesh", "andhra pradesh", "west bengal", "series a",
    "series b", "private limited", "pvt ltd", "read more", "press release",
    "the company", "last year", "this year", "chief executive", "managing director",
}


def to_usd(amount: str, unit: str, currency: str | None) -> float:
    try:
        n = float(amount.replace(",", ""))
    except ValueError:
        return 0.0
    unit, currency = unit.lower(), (currency or "").lower()
    # crore/lakh are always rupees, symbol or not
    inr = unit in ("crore", "cr", "lakh") or currency in ("rs", "rs.", "inr", "₹")
    mult = {"crore": 1e7, "cr": 1e7, "lakh": 1e5, "billion": 1e9, "bn": 1e9,
            "million": 1e6, "mn": 1e6, "m": 1e6, "k": 1e3}.get(unit, 1)
    value = n * mult
    return value / 83 if inr else value      # rough INR->USD, only used for banding


def biggest_amount(text: str) -> tuple[float, str]:
    best, label = 0.0, ""
    for cur, amt, unit in MONEY_RE.findall(text):
        usd = to_usd(amt, unit, cur)
        if usd > best:
            best, label = usd, f"{cur}{amt} {unit}".strip()
    return best, label


def extract_company(title: str) -> str:
    t = LEAD_STRIP.sub("", title).strip()
    cut = len(t)
    for _, _, pat in CATEGORY_PATTERNS:
        m = pat.search(t)
        if m:
            cut = min(cut, m.start())
    words = t[:cut].split()
    while words and (words[0].lower().strip(",'") in DESCRIPTORS
                     or words[0].lower().endswith("-based")):
        words.pop(0)
    out = []
    for w in words[:5]:
        c = w.strip(",'\"\u2018\u2019")
        if c and (c[0].isupper() or c.isupper()):
            out.append(c)
        else:
            break
    return " ".join(out)[:60]


def extract_founders(text: str, company: str) -> list[str]:
    found: list[str] = []
    for pat in FOUNDER_RES:
        for m in pat.finditer(text):
            name = m.group(1).strip()
            # "founded by Asha Nair and Ravi Kumar" -> catch the second one too
            tail = text[m.end():m.end() + 40]
            partner = AND_NAME.match(tail.strip()) or AND_NAME.search(tail[:25])
            for candidate in (name, partner.group(1).strip() if partner else None):
                if not candidate:
                    continue
                low = candidate.lower()
                if (low in NOT_A_NAME or low == company.lower()
                        or candidate in found or len(candidate.split()) > 3):
                    continue
                found.append(candidate)
    return found[:3]


def find_handle(text: str, name: str) -> str | None:
    """An @handle sitting within ~90 characters of the founder's name."""
    first = name.split()[0]
    for m in HANDLE_RE.finditer(text):
        window = text[max(0, m.start() - 90): m.end() + 90]
        if first in window:
            return m.group(1)
    return None


# ===========================================================================
# 5. Scoring
# ===========================================================================

def score_item(item: Item, cfg: dict) -> Item:
    title, blob = item.title, item.blob()
    s = 30
    why = []

    # --- geography. A bare mention of "India" in the body is weak evidence: a story
    # about a US launch that ends "no plans for India yet" would otherwise sail
    # through. Native feeds skip this entirely; for everyone else, the signal
    # counts fully only when it's in the headline.
    if not item.india_native:
        if INDIA_RE.search(title):
            pass
        elif INDIA_RE.search(blob):
            s -= 10
            why.append("India only in body")
        else:
            item.score = 0
            item.reasons = ["no India signal"]
            return item

    # --- category: verb in the headline counts for more than one buried in the body
    for name, bonus, pat in CATEGORY_PATTERNS:
        if pat.search(title):
            item.category, s, _ = name, s + bonus + 8, why.append(f"{name} in headline")
            break
        if pat.search(blob):
            item.category, s, _ = name, s + bonus, why.append(name)
            break

    if item.category == "other":
        s -= 12
        why.append("no clear event")

    # --- money
    usd, label = biggest_amount(blob)
    if usd:
        item.amount = label
        s += 18 if usd >= 20e6 else 14 if usd >= 5e6 else 10 if usd >= 1e6 else 6
        why.append(label)

    # --- stage: early rounds are the ones nobody has covered yet
    stage = STAGE_RE.search(blob)
    if stage:
        item.stage = stage.group(1).title()
        low = item.stage.lower()
        s += 12 if "seed" in low else 10 if low == "series a" else 4
        why.append(item.stage)

    if re.search(r"\bout of stealth\b", blob, re.I):
        s += 12
        why.append("stealth exit")

    # --- entities
    item.company = extract_company(title)
    if not item.company:
        s -= 12
        why.append("no company named")

    item.founders = extract_founders(blob, item.company)
    if item.founders:
        s += 5
        for f in item.founders:
            h = find_handle(blob, f)
            if h:
                item.handles[f] = h

    s += item.weight
    if item.age_min < 45:
        s += 5

    # --- penalties
    if NOISE_RE.search(title):
        s -= 45
        why.append("listicle/opinion")
    if MARKET_RE.search(blob):
        s -= 35
        why.append("markets story")
    if PEOPLE_RE.search(title):
        s -= 12
        why.append("people move")
    if title.rstrip().endswith("?"):
        s -= 12
        why.append("question headline")

    item.score = max(0, min(100, s))
    item.reasons = why
    return item


# ===========================================================================
# 6. Slack
# ===========================================================================

EMOJI = {"launch": "\U0001f680", "funding": "\U0001f4b0", "product": "\U0001f4e6",
         "acquisition": "\U0001f91d", "expansion": "\U0001f30d", "other": "\U0001f4f0"}


def esc(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def rel_time(mins: float) -> str:
    return f"{int(mins)}m ago" if mins < 60 else f"{int(mins // 60)}h ago"


def build_blocks(item: Item) -> list[dict]:
    head = f"{EMOJI.get(item.category, EMOJI['other'])} *{item.category.upper()}*"
    for bit in (item.stage, item.amount):
        if bit:
            head += f"  ·  {esc(bit)}"
    head += f"  ·  _{item.score}_"

    body = f"{head}\n*<{item.url}|{esc(item.company or item.title)}>*"
    if item.company:
        body += f"\n{esc(item.title)}"

    blocks = [{"type": "section", "text": {"type": "mrkdwn", "text": body}}]

    # Founder names come from the article text. Handles are only shown when the
    # article itself printed one — everything else is a one-click search, because
    # guessing a handle from a name is how you end up pitching the wrong person.
    if item.founders:
        lines = []
        for f in item.founders:
            if f in item.handles:
                lines.append(f"{esc(f)} — <https://x.com/{item.handles[f]}|@{item.handles[f]}>")
            else:
                q = quote_plus(f'{f} {item.company} founder')
                lines.append(
                    f"{esc(f)} — <https://x.com/search?q={q}&f=user|find on X> · "
                    f"<https://www.google.com/search?q={q}+site:linkedin.com/in|LinkedIn>")
        blocks.append({"type": "section",
                       "text": {"type": "mrkdwn", "text": "*Founders:* " + "  |  ".join(lines)}})

    buttons = [{"type": "button", "text": {"type": "plain_text", "text": "Read source"},
                "url": item.url}]
    if item.company:
        buttons.append({
            "type": "button", "text": {"type": "plain_text", "text": "Background"},
            "url": "https://www.google.com/search?q=" + quote_plus(f"{item.company} startup India")})
    blocks.append({"type": "actions", "elements": buttons})

    ctx = f"{esc(item.source)}  ·  {rel_time(item.age_min)}"
    if item.reasons:
        ctx += "  ·  " + esc(", ".join(item.reasons[:4]))
    blocks.append({"type": "context", "elements": [{"type": "mrkdwn", "text": ctx}]})
    blocks.append({"type": "divider"})
    return blocks


def post_to_slack(items: list[Item], webhook: str) -> int:
    sent = 0
    for item in items:
        try:
            r = requests.post(webhook, timeout=20, json={
                "text": f"{item.category.upper()}: {item.company or item.title}",
                "blocks": build_blocks(item)})
            r.raise_for_status()
            sent += 1
            time.sleep(1.1)          # Slack allows about one message per second
        except Exception as e:
            print(f"  ! slack post failed: {e}")
    return sent


def print_console(items: list[Item]) -> None:
    if not items:
        print("\n(nothing cleared the threshold)\n")
        return
    for i in items:
        print("\n" + "-" * 72)
        print(f"[{i.score:>3}] {i.category.upper():<12} {i.company or '(no company)'}")
        print(f"      {i.title}")
        if i.amount or i.stage:
            print(f"      money: {i.amount or '-'}   stage: {i.stage or '-'}")
        for f in i.founders:
            h = f"@{i.handles[f]}" if f in i.handles else "(no handle in article)"
            print(f"      founder: {f}  {h}")
        print(f"      {i.source} · {rel_time(i.age_min)} · {', '.join(i.reasons[:5])}")
        print(f"      {i.url}")
    print("\n" + "-" * 72 + f"\n{len(items)} alerts\n")


# ===========================================================================
# 6b. Dashboard feed
# ===========================================================================

def item_id(item: Item) -> str:
    import hashlib
    return hashlib.sha1(canonical_url(item.url).encode()).hexdigest()[:12]


def item_to_dict(item: Item, alerted: bool) -> dict:
    return {
        "id": item_id(item),
        "title": item.title,
        "url": item.url,
        "source": item.source,
        "published": item.published.isoformat(),
        "score": item.score,
        "category": item.category,
        "company": item.company,
        "amount": item.amount,
        "stage": item.stage,
        "founders": [{"name": f, "handle": item.handles.get(f)} for f in item.founders],
        "reasons": item.reasons,
        "alerted": alerted,
    }


def write_dashboard(keep: list[Item], alerted: list[Item], cfg: dict, stats: dict) -> int:
    """Merge this run's results into public/data.json.

    Keyed by a hash of the canonical URL, so re-running (or a dry run) updates
    existing entries instead of duplicating them.
    """
    existing: dict[str, dict] = {}
    if DATA_FILE.exists():
        try:
            for row in json.loads(DATA_FILE.read_text()).get("items", []):
                existing[row["id"]] = row
        except (json.JSONDecodeError, OSError, KeyError):
            print("! data.json unreadable, rebuilding")

    alerted_ids = {item_id(i) for i in alerted}
    for item in keep:
        existing[item_id(item)] = item_to_dict(item, item_id(item) in alerted_ids)

    cutoff = (now() - timedelta(days=DASHBOARD_DAYS)).isoformat()
    rows = [r for r in existing.values() if r.get("published", "") > cutoff]
    rows.sort(key=lambda r: r["published"], reverse=True)
    rows = rows[:DASHBOARD_MAX]

    DATA_FILE.parent.mkdir(parents=True, exist_ok=True)
    DATA_FILE.write_text(json.dumps({
        "generated_at": now().isoformat(),
        "min_score": cfg["min_score"],
        "last_run": stats,
        "items": rows,
    }, indent=1))
    return len(rows)


# ===========================================================================
# 7. Main
# ===========================================================================

def run(cfg: dict, dry_run: bool, show_all: bool) -> int:
    started = time.time()
    items = fetch_all(cfg)
    print(f"fetched {len(items)}")

    seen = Seen(keep_days=cfg.get("state_keep_days", 5))
    fresh, batch = [], []
    for item in sorted(items, key=lambda i: i.published, reverse=True):
        if seen.is_dup(item):
            continue
        n = norm_title(item.title)
        if any(same_story(n, other) for other in batch):   # two feeds, one story
            continue
        batch.append(n)
        fresh.append(item)
    print(f"fresh {len(fresh)}")

    scored = sorted((score_item(i, cfg) for i in fresh), key=lambda i: i.score, reverse=True)
    threshold = 0 if show_all else cfg["min_score"]

    # Everything above the bar counts as an alert. max_alerts_per_run only caps how
    # many get *posted to Slack* — a channel can be flooded, a dashboard can't. If
    # the cap trimmed the dashboard too, the 11th story on a busy day would show up
    # mislabelled as a near-miss.
    qualified = [i for i in scored if i.score >= threshold]
    picked = qualified[:cfg["max_alerts_per_run"]]
    print(f"above {threshold}: {len(qualified)}")

    # The dashboard also keeps near-misses. Seeing what scored 45 against a
    # threshold of 55 is how you work out where the threshold should actually be.
    near = max(0, cfg["min_score"] - cfg.get("dashboard_margin", 20))
    keep = [i for i in scored if i.score >= near]
    stats = {"fetched": len(items), "fresh": len(fresh), "alerted": len(qualified)}
    kept = write_dashboard(keep, qualified, cfg, stats)

    if dry_run:
        print_console(picked)
        print(f"wrote {DATA_FILE.relative_to(ROOT)} ({kept} items, incl. near-misses)")
        return 0

    # Slack is optional. With no webhook the dashboard is the only output, which is
    # a perfectly good way to run this.
    webhook = os.environ.get("SLACK_WEBHOOK_URL", "")
    sent = post_to_slack(picked, webhook) if webhook else 0
    if not webhook:
        print("no SLACK_WEBHOOK_URL set — dashboard only")

    # Record everything fetched, not just what was posted — otherwise a story that
    # scored 40 gets re-evaluated every 30 minutes forever.
    for item in fresh:
        seen.add(item)
    seen.save()
    print(f"slack {sent} · dashboard {kept} · state {len(seen.records)} records "
          f"· {time.time() - started:.1f}s")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description="Indian startup radar (free edition)")
    p.add_argument("--dry-run", action="store_true", help="print instead of posting")
    p.add_argument("--show-all", action="store_true", help="ignore min_score, show everything")
    p.add_argument("--lookback", type=int, help="minutes of history (overrides config)")
    p.add_argument("--min-score", type=int, help="threshold 0-100 (overrides config)")
    p.add_argument("--config", default=str(ROOT / "config.yaml"))
    a = p.parse_args()

    cfg = yaml.safe_load(Path(a.config).read_text())
    if a.lookback:
        cfg["lookback_minutes"] = a.lookback
    if a.min_score is not None:
        cfg["min_score"] = a.min_score

    return run(cfg, a.dry_run, a.show_all)


if __name__ == "__main__":
    sys.exit(main())
