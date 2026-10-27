"""Daily ranked-match ingestion: one day of matches, three rank buckets, evenly spread over 24 hours.

Stage 1 (summaries): for every rank x hour, page through the Explorer until the hour is complete.
                     Cheap: 1 call returns up to 1,000 match summaries.
Stage 2 (details):   the remaining call budget is split evenly over rank x hour slots; each slot gets
                     a random (reproducible) sample of its matches fetched via /matches/{id}.

Usage:
    python -m ingest.daily_ranked                       # yesterday (UTC), budget 1,000 calls
    python -m ingest.daily_ranked --date 2026-10-26 --stage details
"""

import argparse
import logging
import random
from collections import Counter
from datetime import date, datetime, timedelta, timezone

import boto3

from ingest.bronze import RANKS, BronzeWriter, details_key, details_prefix, make_envelope, summaries_key
from ingest.opendota_client import ApiError, OpenDotaClient, QuotaExhausted

log = logging.getLogger("daily_ranked")

PAGE_SIZE = 1000
HOURS = range(24)
COLUMNS = ("match_id, match_seq_num, radiant_win, start_time, duration, lobby_type, game_mode, "
           "avg_rank_tier, num_rank_tier, cluster, radiant_team, dire_team")


def hour_window(match_date, hour):
    start = int(datetime(match_date.year, match_date.month, match_date.day, hour, tzinfo=timezone.utc).timestamp())
    return start, start + 3600


def build_sql(rank, start_ts, end_ts, before_match_id=None):
    lo, hi = RANKS[rank]
    cursor = f"AND match_id < {int(before_match_id)} " if before_match_id else ""
    return (f"SELECT {COLUMNS} FROM public_matches "
            f"WHERE lobby_type = 7 AND game_mode = 22 AND duration > 0 "
            f"AND avg_rank_tier BETWEEN {lo} AND {hi} "
            f"AND start_time >= {int(start_ts)} AND start_time < {int(end_ts)} {cursor}"
            f"ORDER BY match_id DESC LIMIT {PAGE_SIZE}")


# ---------------------------------------------------------------- stage 1

def collect_hour_summaries(client, writer, rank, match_date, hour, max_pages):
    """Page backwards through one hour; writes one object per rank x hour. Returns the rows."""
    start_ts, end_ts = hour_window(match_date, hour)
    pages, rows, cursor = [], [], None
    complete = False
    for _ in range(max_pages):
        resp = client.explorer(build_sql(rank, start_ts, end_ts, cursor))
        page_rows = resp.data.get("rows") or []
        pages.append(make_envelope(resp))
        rows += page_rows
        if len(page_rows) < PAGE_SIZE:
            complete = True
            break
        cursor = min(r["match_id"] for r in page_rows)

    envelope = {"source": "opendota", "dataset": "ranked_match_summaries", "rank": rank,
                "match_date": match_date.isoformat(), "hour": hour, "row_count": len(rows),
                "complete": complete, "pages": pages}
    writer.put_json(summaries_key(rank, match_date.isoformat(), hour), envelope)
    if not complete:
        log.warning("%s %02d:00 truncated at %d rows (max_pages=%d)", rank, hour, len(rows), max_pages)
    return rows


def read_hour_summaries(writer, rank, match_date, hour):
    envelope = writer.get_json(summaries_key(rank, match_date.isoformat(), hour))
    return [row for page in envelope["pages"] for row in page["payload"]["rows"]]


# ---------------------------------------------------------------- stage 2

def sample_match_ids(rows, k, seed):
    """Reproducible random sample: re-running the same day picks the same matches."""
    ids = sorted({r["match_id"] for r in rows})
    return random.Random(seed).sample(ids, min(k, len(ids)))


def collect_slot_details(client, writer, rank, match_date, hour, rows, per_slot, stats):
    day = match_date.isoformat()
    existing = {int(k.rsplit("match_id=", 1)[1].split(".")[0])
                for k in writer.list_keys(details_prefix(rank, day, hour))}
    for match_id in sample_match_ids(rows, per_slot, seed=f"{day}/{rank}/{hour}"):
        if match_id in existing:
            stats["skipped_existing"] += 1
            continue
        try:
            resp = client.get(f"/matches/{match_id}")
        except ApiError as exc:
            log.warning("match %s failed: %s", match_id, exc)
            stats["failed"] += 1
            continue
        parsed = bool((resp.data.get("od_data") or {}).get("has_parsed"))
        writer.put_json(details_key(rank, day, hour, match_id),
                        make_envelope(resp, dataset="ranked_match_details", rank=rank, match_date=day,
                                      hour=hour, match_id=match_id, parsed=parsed))
        stats["fetched"] += 1
        stats["parsed"] += parsed
        if stats["fetched"] % 50 == 0:
            log.info("details: %d fetched (%d parsed), API calls %d, remaining today %s",
                     stats["fetched"], stats["parsed"], client.calls, client.remaining_day)


# ---------------------------------------------------------------- orchestration

def run(client, writer, match_date, ranks, stage, max_pages_per_hour, max_calls):
    summaries, report = {}, {}
    if stage in ("both", "summaries"):
        for rank in ranks:
            for hour in HOURS:
                summaries[(rank, hour)] = collect_hour_summaries(client, writer, rank, match_date, hour, max_pages_per_hour)
            report[rank] = sum(len(summaries[(rank, h)]) for h in HOURS)
            log.info("summaries %s: %d matches, API calls so far %d", rank, report[rank], client.calls)
    if stage == "summaries":
        return report, Counter()

    if not summaries:
        summaries = {(r, h): read_hour_summaries(writer, r, match_date, h) for r in ranks for h in HOURS}
    slots = len(ranks) * len(HOURS)
    per_slot = max(0, (max_calls - client.calls) // slots)
    log.info("details: %d calls left in budget -> %d matches per rank-hour slot", max_calls - client.calls, per_slot)
    stats = Counter()
    for hour in HOURS:  # hour-major order keeps coverage even if we stop early
        for rank in ranks:
            collect_slot_details(client, writer, rank, match_date, hour, summaries[(rank, hour)], per_slot, stats)
    return report, stats


def main(argv=None):
    yesterday = (datetime.now(timezone.utc) - timedelta(days=1)).date()
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--date", type=date.fromisoformat, default=yesterday, help="match date (UTC), default yesterday")
    parser.add_argument("--stage", choices=["both", "summaries", "details"], default="both")
    parser.add_argument("--ranks", nargs="+", default=list(RANKS), choices=list(RANKS))
    parser.add_argument("--max-calls", type=int, default=1000, help="hard budget for this run")
    parser.add_argument("--max-pages-per-hour", type=int, default=5)
    parser.add_argument("--bucket", default="dota-coach-lake-299324001563")
    parser.add_argument("--profile", default="dota-coach")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    writer = BronzeWriter(boto3.Session(profile_name=args.profile).client("s3"), args.bucket)
    writer.ensure_layer_prefixes()
    client = OpenDotaClient(max_calls=args.max_calls)
    log.info("ingesting %s, ranks %s, stage %s, budget %d calls", args.date, args.ranks, args.stage, args.max_calls)

    try:
        report, stats = run(client, writer, args.date, args.ranks, args.stage, args.max_pages_per_hour, args.max_calls)
    except QuotaExhausted as exc:
        log.warning("stopped early: %s", exc)
        return

    print(f"\n=== {args.date} ===")
    for rank, n in report.items():
        print(f"summaries  {rank:<18}{n:>7,} matches")
    if stats:
        print(f"details    fetched {stats['fetched']}, parsed {stats['parsed']}, "
              f"already had {stats['skipped_existing']}, failed {stats['failed']}")
    print(f"API calls used: {client.calls} (remaining today per OpenDota: {client.remaining_day})")


if __name__ == "__main__":
    main()
