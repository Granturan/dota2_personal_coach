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

## Setup

```powershell
python -m venv venv
venv\Scripts\activate
pip install -r requirements.txt
```

## Run

```powershell
python scripts/explore_opendota.py      # refresh samples (~15 API calls)
streamlit run app/match_report.py       # open the match report at http://localhost:8501
```

## Key findings so far

- Free tier: 60 requests/min, 3,000/day (from response headers).
- Only ~2–3% of random ranked matches are *parsed*. Parsed matches have ~148 fields per player
  (per-minute series, event logs, lanes, wards); unparsed ones have ~56 end-of-game fields.
- The SQL Explorer endpoint returns 1,000 filtered rows per call (e.g. ranked only) when
  queries stay index-friendly.
- Raw data lands in S3 (`eu-north-1`) as the Bronze layer; see the medallion design discussion.
