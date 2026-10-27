"""Daily ingestion of the previous day's ranked matches into the S3 Bronze layer.

Each run processes the UTC day before its logical date, so a run at 2026-10-28 03:00
collects matches played on 2026-10-27, and backfilling a date range re-collects those days.
Both stages are idempotent: re-runs overwrite summaries and skip match details already in S3.
"""

import logging
import os
from datetime import datetime, timedelta

import boto3
from airflow.sdk import dag, get_current_context, task

from ingest.bronze import RANKS, BronzeWriter
from ingest.daily_ranked import run
from ingest.opendota_client import OpenDotaClient

log = logging.getLogger(__name__)

CALL_BUDGET = 1000         # per day; OpenDota's free tier allows 3,000
SUMMARY_CALL_BUDGET = 300  # stage 1 normally needs ~160
MAX_PAGES_PER_HOUR = 5


def _match_date():
    ctx = get_current_context()
    reference = ctx.get("logical_date") or ctx["dag_run"].run_after  # manual runs may have no logical date
    return (reference - timedelta(days=1)).date()


def _writer():
    # Credentials come from the AWS_PROFILE env var locally, and from an IAM role on AWS.
    return BronzeWriter(boto3.client("s3"), os.environ["DOTA_BUCKET"])


@dag(
    schedule="0 3 * * *",
    start_date=datetime(2026, 10, 27),
    catchup=False,
    max_active_runs=1,
    tags=["ingest", "bronze", "opendota"],
    default_args={"retries": 2, "retry_delay": timedelta(minutes=10)},
)
def daily_ranked_ingest():

    @task
    def summaries() -> int:
        """Stage 1: complete hourly match summaries per rank. Returns the API calls used."""
        match_date = _match_date()
        writer = _writer()
        writer.ensure_layer_prefixes()
        client = OpenDotaClient(max_calls=SUMMARY_CALL_BUDGET)
        report, _ = run(client, writer, match_date, list(RANKS), "summaries", MAX_PAGES_PER_HOUR, SUMMARY_CALL_BUDGET)
        log.info("summaries for %s: %s (%d API calls)", match_date, report, client.calls)
        return client.calls

    @task
    def details(summary_calls: int) -> dict:
        """Stage 2: spend the rest of the day's budget on an even per-hour sample of match details."""
        match_date = _match_date()
        budget = CALL_BUDGET - summary_calls
        client = OpenDotaClient(max_calls=budget)
        _, stats = run(client, _writer(), match_date, list(RANKS), "details", MAX_PAGES_PER_HOUR, budget)
        log.info("details for %s: %s (%d API calls)", match_date, dict(stats), client.calls)
        return dict(stats)

    details(summaries())


daily_ranked_ingest()
