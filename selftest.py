#!/usr/bin/env python3
"""Offline tests. No network, no keys, no API calls.

    python selftest.py

Most of this is score calibration. With no model in the loop, the rules ARE the
product, so these fixtures are what stop a config tweak silently breaking the feed.
"""
import sys
from datetime import timedelta
from types import ModuleType

# feedparser is only needed for live fetching; stub it so tests run anywhere.
try:
    import feedparser  # noqa: F401
except ImportError:
    stub = ModuleType("feedparser")
    stub.parse = lambda *a, **k: type("R", (), {"entries": []})()
    sys.modules["feedparser"] = stub

import yaml

from radar import (Item, biggest_amount, build_blocks, canonical_url, extract_company,
                   extract_founders, find_handle, norm_title, now, same_story, score_item)

CFG = yaml.safe_load(open("config.yaml"))
PASS = FAIL = 0


def check(label, got, want):
    global PASS, FAIL
    ok = got == want
    PASS, FAIL = PASS + ok, FAIL + (not ok)
    print(f"  [{'ok' if ok else 'XX'}] {label}")
    if not ok:
        print(f"        got:  {got!r}\n        want: {want!r}")


def mk(title, summary="", source="Entrackr", native=True, weight=8, mins=20):
    return Item(title=title, url=f"https://x.test/{abs(hash(title))}", source=source,
                published=now() - timedelta(minutes=mins), summary=summary,
                weight=weight, india_native=native)


print("\nURL + title normalisation")
check("strips tracking params",
      canonical_url("https://www.inc42.com/buzz/x/?utm_source=tw&id=7"),
      "https://inc42.com/buzz/x?id=7")
check("keeps amounts and round letters as tokens",
      norm_title("Zepto raises $340M Series F"), "zepto raises 340m seriesf")

print("\nDedupe — merging a real story is worse than a repeat alert")
PAIRS = [
    ("Zenpay raises $4M seed led by Blume",
     "Zenpay raises $4M in seed round led by Blume Ventures", True),
    ("Zepto raises $340M Series F", "Zepto closes $340M in Series F round", True),
    ("Meesho files for IPO", "Meesho files draft papers for IPO", True),
    ("Nykaa launches menswear line", "Nykaa launches menswear vertical in India", True),
    ("Zepto raises $60M Series D", "Zepto raises $340M Series F", False),
    ("Groww raises Rs 500 crore", "Groww raises Rs 1,200 crore", False),
    ("Flipkart launches grocery in Pune", "Flipkart launches insurance in Delhi", False),
    ("Cred launches Cred Pay", "Cred launches Cred Mint", False),
    ("Startup launches AI platform for India", "Startup launches AI tool for Indian market", False),
    ("Zomato acquires Blinkit", "Swiggy acquires Dineout", False),
    # found in an end-to-end run: two outlets, one story, missed at the old threshold
    ("Bengaluru-based Zenpay launches UPI-linked credit line",
     "Zenpay launches UPI credit line in three cities", True),
]
for a, b, want in PAIRS:
    check(f"{'merge   ' if want else 'separate'}: {a[:30]} | {b[:30]}",
          same_story(norm_title(a), norm_title(b)), want)

print("\nAmount parsing")
for text, want_band in [("raised $4M seed", 4e6), ("raised Rs 500 crore", 5e9 / 83),
                        ("bagged ₹12 crore", 1.2e8 / 83), ("secured $2.5 million", 2.5e6),
                        ("raised 40 lakh", 4e6 / 83)]:
    got, _ = biggest_amount(text)
    check(f"{text:<26} -> ${got:,.0f}", round(got) == round(want_band), True)
check("picks the largest of several", round(biggest_amount("$2M now, $10M total")[0]), 10000000)

print("\nCompany extraction")
for title, want in [
    ("Zenpay raises $4M seed led by Blume", "Zenpay"),
    ("Bengaluru-based Zenpay launches UPI credit", "Zenpay"),
    ("Indian fintech Jar raises Series B", "Jar"),
    ("Exclusive: Rapido acquires logistics firm", "Rapido"),
    ("Ola Electric unveils new scooter", "Ola Electric"),
    # found in an end-to-end run: leading descriptors masked the real name
    ("Stealth startup Kaya emerges out of stealth with Rs 16 crore", "Kaya"),
    ("Quick commerce firm Zepto launches in Nagpur", "Zepto"),
]:
    check(f"{title[:42]:<44} -> {want}", extract_company(title), want)

print("\nFounder extraction")
check("founded by X and Y",
      extract_founders("The company was founded by Asha Nair and Ravi Kumar in 2021.", "Zenpay"),
      ["Asha Nair", "Ravi Kumar"])
check("role then name",
      extract_founders("Co-founder Priya Sharma said the launch went well.", "Zenpay"),
      ["Priya Sharma"])
check("name then role",
      extract_founders("Rahul Mehta, founder of the company, confirmed it.", "Zenpay"),
      ["Rahul Mehta"])
check("ignores place names",
      extract_founders("Launched in New Delhi last week.", "Zenpay"), [])
check("finds nearby handle",
      find_handle("Founder Asha Nair (@ashanair) announced the launch.", "Asha Nair"),
      "ashanair")
check("ignores a distant handle",
      find_handle("Asha Nair said this." + " padding" * 30 + " ping @someoneelse", "Asha Nair"),
      None)

print("\nScore calibration — should ALERT (>= min_score)")
SHOULD_ALERT = [
    mk("Bengaluru-based Zenpay launches UPI-linked credit line",
       "Founded by Asha Nair, the fintech went live today across three cities."),
    mk("Jar raises $4.5M seed round led by Blume Ventures",
       "The Bengaluru startup was founded by Nishchay Ag."),
    mk("Stealth startup Kaya comes out of stealth with $2M pre-seed",
       "Kaya, founded by Ravi Kumar, is out of stealth."),
    mk("Rapido acquires logistics startup Porter's delivery arm",
       "The Bengaluru company confirmed the acquisition."),
    mk("Indian D2C brand Sunday launches mattress line in Pune", "Founded by Arjun Rao."),
]
print("\nScore calibration — should NOT alert (< min_score)")
SHOULD_MUTE = [
    mk("Top 10 Indian startups to watch in 2026", "A roundup of promising names."),
    mk("Why India's startup ecosystem needs better regulation", "Opinion piece."),
    mk("Sensex closes higher as IT stocks rally", "Nifty gained 200 points.", weight=0),
    mk("Explained: How UPI changed Indian payments", "A deep dive."),
    mk("Is India's quick commerce boom sustainable?", "Analysis of the sector."),
    mk("Stripe launches new checkout in Europe", "Available to European merchants.",
       source="TechCrunch", native=False, weight=0),
    mk("Zomato appoints new chief financial officer", "The company announced the hire."),
]

for group, want_alert in ((SHOULD_ALERT, True), (SHOULD_MUTE, False)):
    for it in group:
        s = score_item(it, CFG)
        got = s.score >= CFG["min_score"]
        check(f"{s.score:>3} {'ALERT ' if got else 'muted '} {it.title[:48]}", got, want_alert)

print("\nRanking")
ranked = sorted((score_item(i, CFG) for i in SHOULD_ALERT + SHOULD_MUTE),
                key=lambda i: i.score, reverse=True)
check("a launch outranks every listicle",
      all(ranked[0].score > m.score for m in (score_item(x, CFG) for x in SHOULD_MUTE)), True)

print("\nSlack rendering")
it = score_item(mk("Jar raises $4.5M seed led by Blume",
                   "Founded by Asha Nair (@ashanair), the Bengaluru startup..."), CFG)
blocks = build_blocks(it)
check("block sequence", [b["type"] for b in blocks],
      ["section", "section", "actions", "context", "divider"])
check("links the source", it.url in blocks[0]["text"]["text"], True)
check("shows the handle found in the article",
      any("x.com/ashanair" in b.get("text", {}).get("text", "") for b in blocks), True)

no_handle = score_item(mk("Zenpay launches credit line", "Founded by Ravi Kumar."), CFG)
check("offers a search link when no handle is printed",
      any("find on X" in b.get("text", {}).get("text", "") for b in build_blocks(no_handle)), True)
check("escapes angle brackets",
      "&lt;b&gt;" in build_blocks(mk("<b>Acme</b> launches app"))[0]["text"]["text"], True)

print(f"\n{PASS} passed, {FAIL} failed\n")
sys.exit(1 if FAIL else 0)
