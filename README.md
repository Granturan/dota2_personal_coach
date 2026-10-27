# Dota Coach — Personal Edition

A data-driven personal coaching platform for Dota 2, and a hands-on project for learning
data engineering and cloud infrastructure (AWS, Spark, Docker, Kubernetes).

## Current state: Phase 0 (discovery)

We are exploring what the [OpenDota API](https://docs.opendota.com/) actually returns
before designing any pipeline or schema.

| Path | What it is |
|---|---|
| `scripts/explore_opendota.py` | Fetches sample responses from the main OpenDota endpoints into `data/samples/` |
| `data/samples/` | Raw API responses (matches, players, heroes, items, constants, OpenAPI spec) |
| `app/match_report.py` | Streamlit report that explains one match response to a non-player |
| `ingest/` | Daily ingestion of ranked matches into the S3 Bronze layer |
| `tests/` | Unit tests (fake HTTP, S3 mocked with moto) |

## Setup

```powershell
python -m venv venv
venv\Scripts\activate
pip install -r requirements-dev.txt
aws login --profile dota-coach --region eu-north-1   # S3 access for ingestion
```

## Run

```powershell
python scripts/explore_opendota.py      # refresh samples (~15 API calls)
streamlit run app/match_report.py       # open the match report at http://localhost:8501
python -m ingest.daily_ranked           # yesterday's ranked matches -> S3 (budget 1,000 calls)
python -m pytest -q                     # tests
```

## Airflow (local)

Docker Desktop required. Airflow 3.3.2 with LocalExecutor; the image adds the `ingest` package.

```powershell
cd airflow
copy .env.example .env                 # then fill in secrets (see comments in the file)
docker compose up -d --build           # UI: http://localhost:8080 (admin / password from .env)
docker compose down                    # stop; metadata DB survives in a Docker volume
```

DAG `daily_ranked_ingest` runs daily at 03:00 UTC and ingests the previous UTC day.
Locally it uses your `aws login` session (the `~/.aws` folder is mounted), so the session
must be valid when the run starts.

## S3 layout (`s3://dota-coach-lake-299324001563`)

```
bronze/opendota/ranked_match_summaries/rank=<r>/match_date=<YYYY-MM-DD>/hour=<HH>/summaries.json.gz
bronze/opendota/ranked_match_details/rank=<r>/match_date=<YYYY-MM-DD>/hour=<HH>/match_id=<id>.json.gz
silver/   gold/   (not used yet)
```

Rank buckets use the match's `avg_rank_tier`: `ancient` (60–69), `divine` (70–74),
`divine5_immortal` (75+). The match average never exceeds 75, so Divine 5 and Immortal
can't be separated at this level.

## Key findings so far

- Free tier: 60 requests/min, 3,000/day (from response headers).
- Only ~2–3% of random ranked matches are *parsed*. Parsed matches have ~148 fields per player
  (per-minute series, event logs, lanes, wards); unparsed ones have ~56 end-of-game fields.
- The SQL Explorer endpoint returns 1,000 filtered rows per call (e.g. ranked only) when
  queries stay index-friendly.
- Raw data lands in S3 (`eu-north-1`) as the Bronze layer; see the medallion design discussion.
