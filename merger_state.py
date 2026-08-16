#!/usr/bin/env python3
"""Merge this run's generated files into whatever is now on disk.

The workflow calls this when another radar run pushed first. Both state/seen.json
and public/data.json are generated and purely additive, so `git rebase` conflicts on
them every single time — two runs each rewrote the same JSON from scratch, and git
has no idea the contents are mergeable by key.

Rebasing is the wrong tool here. Resetting to the remote and merging by key is both
deterministic and lossless: we keep every record from both sides.

    python merge_state.py <saved-seen.json> <saved-data.json>
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

SEEN = Path("state/seen.json")
DATA = Path("public/data.json")
SEEN_KEEP_DAYS = 5      # must match Seen(keep_days=...) in radar.py
DATA_KEEP_DAYS = 7      # must match DASHBOARD_DAYS in radar.py
DATA_MAX = 300          # must match DASHBOARD_MAX in radar.py


def load(path: Path) -> dict:
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def merge(remote: dict, mine: dict, key: str, date_field: str,
          keep_days: int, cap: int | None) -> list[dict]:
    """Union both sides by key. Ours wins ties, since ours is the later run."""
    rows: dict[str, dict] = {}
    for row in remote.get("items", []):
        if isinstance(row, dict) and key in row:
            rows[row[key]] = row
    for row in mine.get("items", []):
        if isinstance(row, dict) and key in row:
            rows[row[key]] = row

    cutoff = (datetime.now(timezone.utc) - timedelta(days=keep_days)).isoformat()
    out = [r for r in rows.values() if r.get(date_field, "") > cutoff]
    out.sort(key=lambda r: r.get(date_field, ""), reverse=True)
    return out[:cap] if cap else out


def main() -> int:
    if len(sys.argv) != 3:
        print(__doc__)
        return 2
    saved_seen, saved_data = Path(sys.argv[1]), Path(sys.argv[2])

    mine = load(saved_seen)
    merged = merge(load(SEEN), mine, "u", "d", SEEN_KEEP_DAYS, None)
    SEEN.parent.mkdir(parents=True, exist_ok=True)
    SEEN.write_text(json.dumps(
        {"updated": datetime.now(timezone.utc).isoformat(), "items": merged}, indent=1))
    print(f"merged state/seen.json -> {len(merged)} records")

    mine = load(saved_data)
    remote = load(DATA)
    items = merge(remote, mine, "id", "published", DATA_KEEP_DAYS, DATA_MAX)
    DATA.parent.mkdir(parents=True, exist_ok=True)
    DATA.write_text(json.dumps({
        # Our run is the later one, so our metadata is the more current.
        "generated_at": mine.get("generated_at") or remote.get("generated_at"),
        "min_score": mine.get("min_score", remote.get("min_score", 55)),
        "last_run": mine.get("last_run", {}),
        "items": items,
    }, indent=1))
    print(f"merged public/data.json -> {len(items)} items")
    return 0


if __name__ == "__main__":
    sys.exit(main())
