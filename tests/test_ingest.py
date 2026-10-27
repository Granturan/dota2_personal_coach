from collections import Counter
from datetime import date, datetime, timezone

import boto3
import pytest
from moto import mock_aws

from ingest.bronze import BronzeWriter, details_key, details_prefix, summaries_key
from ingest.daily_ranked import (build_sql, collect_hour_summaries, collect_slot_details, hour_window,
                                 read_hour_summaries, run, sample_match_ids)
from ingest.opendota_client import ApiError, OpenDotaClient, QuotaExhausted

BUCKET = "test-bucket"


class FakeResponse:
    def __init__(self, status=200, data=None, remaining=2000, remaining_minute=50):
        self.status_code = status
        self._data = data if data is not None else {}
        self.headers = {"x-rate-limit-remaining-day": str(remaining),
                        "x-rate-limit-remaining-minute": str(remaining_minute)}
        self.text = str(self._data)

    def json(self):
        return self._data


class FakeSession:
    """Returns queued responses in order and records each request."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []

    def get(self, url, params=None, timeout=None):
        self.requests.append((url, params))
        return self.responses.pop(0)


def make_client(responses, **kwargs):
    sleeps = []
    client = OpenDotaClient(session=FakeSession(responses), sleep=sleeps.append, clock=lambda: 0.0, **kwargs)
    return client, sleeps


# ---------------------------------------------------------------- client

def test_429_waits_full_minute_then_succeeds():
    client, sleeps = make_client([FakeResponse(429), FakeResponse(200, {"ok": 1})])
    assert client.get("/x").data == {"ok": 1}
    assert client.calls == 2
    assert 60.0 in sleeps  # the per-minute window must reset; a short backoff just burns calls


def test_server_error_gives_up_after_two_retries():
    client, _ = make_client([FakeResponse(500) for _ in range(5)])
    with pytest.raises(ApiError):
        client.get("/matches/1")
    assert client.calls == 3  # 1 try + 2 retries, not 6


def test_cools_down_before_hitting_minute_limit():
    client, sleeps = make_client([FakeResponse(remaining_minute=2), FakeResponse()])
    client.get("/a")
    client.get("/b")
    assert sleeps == [60.0]  # paused proactively instead of collecting a 429


def test_non_retryable_error_raises():
    client, _ = make_client([FakeResponse(404, {"error": "Not Found"})])
    with pytest.raises(ApiError):
        client.get("/matches/1")


def test_throttle_waits_between_calls():
    client, sleeps = make_client([FakeResponse(), FakeResponse()], min_interval=1.05)
    client.get("/a")
    client.get("/b")
    assert sleeps == [1.05]  # frozen clock -> full interval waited before the second call


def test_run_budget_stops_calls():
    client, _ = make_client([FakeResponse(), FakeResponse()], max_calls=1)
    client.get("/a")
    with pytest.raises(QuotaExhausted):
        client.get("/b")


def test_daily_reserve_stops_calls():
    client, _ = make_client([FakeResponse(remaining=40)], daily_reserve=50)
    client.get("/a")
    with pytest.raises(QuotaExhausted):
        client.get("/b")


def test_explorer_error_payload_raises():
    client, _ = make_client([FakeResponse(200, {"err": "Query read timeout"})])
    with pytest.raises(ApiError):
        client.explorer("SELECT 1")


# ---------------------------------------------------------------- bronze + daily job

DAY = date(2026, 10, 26)


@pytest.fixture
def writer():
    with mock_aws():
        s3 = boto3.client("s3", region_name="eu-north-1")
        s3.create_bucket(Bucket=BUCKET, CreateBucketConfiguration={"LocationConstraint": "eu-north-1"})
        yield BronzeWriter(s3, BUCKET)


def rows(ids):
    return [{"match_id": i, "start_time": 1_700_000_000} for i in ids]


def test_sql_filters_rank_hour_and_cursor():
    start, end = hour_window(DAY, 14)
    assert end - start == 3600
    assert datetime.fromtimestamp(start, timezone.utc) == datetime(2026, 10, 26, 14, tzinfo=timezone.utc)
    sql = build_sql("divine", start, end, before_match_id=123)
    assert "avg_rank_tier BETWEEN 70 AND 74" in sql
    assert f"start_time >= {start} AND start_time < {end}" in sql
    assert "match_id < 123" in sql and "lobby_type = 7" in sql and "duration > 0" in sql


def test_key_layout():
    assert summaries_key("ancient", "2026-10-26", 3) == (
        "bronze/opendota/ranked_match_summaries/rank=ancient/match_date=2026-10-26/hour=03/summaries.json.gz")
    assert details_key("divine5_immortal", "2026-10-26", 14, 42) == (
        "bronze/opendota/ranked_match_details/rank=divine5_immortal/match_date=2026-10-26/hour=14/match_id=42.json.gz")


def test_hour_summaries_page_until_complete(writer):
    client, _ = make_client([FakeResponse(200, {"rows": rows(range(2000, 1000, -1))}),  # full page
                             FakeResponse(200, {"rows": rows(range(1000, 990, -1))})])  # short page -> done
    got = collect_hour_summaries(client, writer, "ancient", DAY, 14, max_pages=5)

    assert len(got) == 1010 and client.calls == 2
    assert "match_id < 1001" in client.session.requests[1][1]["sql"]  # cursor = smallest id of page 1
    envelope = writer.get_json(summaries_key("ancient", "2026-10-26", 14))
    assert envelope["complete"] and envelope["row_count"] == 1010 and len(envelope["pages"]) == 2
    assert read_hour_summaries(writer, "ancient", DAY, 14) == got


def test_hour_summaries_flags_truncation(writer):
    client, _ = make_client([FakeResponse(200, {"rows": rows(range(2000, 1000, -1))})])
    collect_hour_summaries(client, writer, "divine", DAY, 0, max_pages=1)
    assert writer.get_json(summaries_key("divine", "2026-10-26", 0))["complete"] is False


def test_sample_is_reproducible_and_capped():
    r = rows(range(100))
    assert sample_match_ids(r, 10, "seed") == sample_match_ids(r, 10, "seed")
    assert len(sample_match_ids(rows(range(3)), 10, "seed")) == 3


def test_details_rerun_skips_existing(writer):
    hour_rows = rows(range(1, 21))
    match = {"match_id": 1, "od_data": {"has_parsed": False}}
    client, _ = make_client([FakeResponse(200, match) for _ in range(5)])
    stats = Counter()
    collect_slot_details(client, writer, "divine", DAY, 5, hour_rows, per_slot=5, stats=stats)
    assert stats["fetched"] == 5 and client.calls == 5

    client2, _ = make_client([])  # any API call would fail: the queue is empty
    stats2 = Counter()
    collect_slot_details(client2, writer, "divine", DAY, 5, hour_rows, per_slot=5, stats=stats2)
    assert stats2["skipped_existing"] == 5 and client2.calls == 0
    assert len(writer.list_keys(details_prefix("divine", "2026-10-26", 5))) == 5


def test_run_splits_remaining_budget_over_slots(writer):
    # Stage 1: every hour returns 3 matches in one call -> 24 calls for one rank.
    summaries = [FakeResponse(200, {"rows": rows(range(h * 10, h * 10 + 3))}) for h in range(24)]
    details = [FakeResponse(200, {"od_data": {}}) for _ in range(100)]
    client, _ = make_client(summaries + details)
    report, stats = run(client, writer, DAY, ["ancient"], "both", max_pages_per_hour=5, max_calls=24 + 48)

    assert report == {"ancient": 72}
    assert stats["fetched"] == 48  # (72 - 24) // 24 slots = 2 per hour
    assert client.calls == 72
