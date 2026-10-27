"""Phase 0 exploration: fetch sample responses from the main OpenDota endpoints.

Saves raw JSON to data/samples/ so we can inspect real payloads before
designing any schema. Throttled to ~1 request/second to respect the free tier.
"""

import json
import time
from pathlib import Path

import requests

BASE_URL = "https://api.opendota.com/api"
OUT_DIR = Path(__file__).resolve().parent.parent / "data" / "samples"
RATE_LIMIT_HEADERS = ("x-rate-limit-remaining-minute", "x-rate-limit-remaining-day")


def fetch(path, name, params=None):
    resp = requests.get(f"{BASE_URL}{path}", params=params, timeout=30)
    limits = {h: resp.headers.get(h) for h in RATE_LIMIT_HEADERS}
    print(f"{resp.status_code} {path:<40} {len(resp.content):>9,} bytes  {limits}")
    resp.raise_for_status()
    data = resp.json()
    (OUT_DIR / f"{name}.json").write_text(json.dumps(data, indent=2), encoding="utf-8")
    time.sleep(1.1)
    return data


def main():
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    # Population-level feeds: matches from everyone, not a specific player.
    public = fetch("/publicMatches", "public_matches")
    parsed = fetch("/parsedMatches", "parsed_matches")
    fetch("/proMatches", "pro_matches")

    # One parsed and one (probably) unparsed match, to compare field coverage.
    parsed_match = fetch(f"/matches/{parsed[0]['match_id']}", "match_parsed")
    fetch(f"/matches/{public[0]['match_id']}", "match_unparsed")

    # A player with a public profile, taken from the parsed match.
    account_id = next(p["account_id"] for p in parsed_match["players"] if p.get("account_id"))
    fetch(f"/players/{account_id}", "player_profile")
    fetch(f"/players/{account_id}/matches", "player_matches", params={"limit": 20})

    # Reference data and population aggregates.
    fetch("/heroes", "heroes")
    fetch("/heroStats", "hero_stats")
    fetch("/constants/items", "constants_items")
    fetch("/constants/patch", "constants_patch")
    fetch("/constants/region", "constants_region")
    fetch("/benchmarks", "benchmarks_hero1", params={"hero_id": 1})
    fetch(
        "/explorer",
        "explorer_sample",
        params={"sql": "SELECT match_id, start_time, duration FROM public_matches ORDER BY match_id DESC LIMIT 5"},
    )


if __name__ == "__main__":
    main()
