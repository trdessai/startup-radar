#!/usr/bin/env python3
"""Check every feed in config.yaml is alive. Run after editing the feed list.

Feed URLs rot — outlets migrate CMSs and quietly drop /feed/ endpoints. This tells
you which ones are dead before you spend a week wondering why coverage looks thin.

    python check_feeds.py
"""
import sys
from concurrent.futures import ThreadPoolExecutor

import feedparser
import requests
import yaml

UA = "Mozilla/5.0 (compatible; StartupRadar/1.0)"


def check(feed):
    try:
        r = requests.get(feed["url"], headers={"User-Agent": UA}, timeout=20)
        if r.status_code != 200:
            return feed["name"], "FAIL", f"HTTP {r.status_code}"
        entries = feedparser.parse(r.content).entries
        if not entries:
            return feed["name"], "EMPTY", "parsed but no entries — wrong URL?"
        return feed["name"], "OK", f"{len(entries):>3} entries · {entries[0].get('title','')[:52]}"
    except Exception as e:
        return feed["name"], "FAIL", f"{type(e).__name__}: {str(e)[:50]}"


feeds = yaml.safe_load(open("config.yaml"))["feeds"]
with ThreadPoolExecutor(max_workers=10) as pool:
    results = list(pool.map(check, feeds))

bad = 0
for name, status, detail in results:
    print(f"[{ {'OK':'  ok  ','EMPTY':' empty','FAIL':' FAIL '}[status] }] {name:<24} {detail}")
    bad += status != "OK"

print(f"\n{len(results)-bad}/{len(results)} healthy")
sys.exit(1 if bad else 0)
