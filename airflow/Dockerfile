# Our Airflow image: the official one plus the project's ingestion code.
# Build context is the repo root (see docker-compose.yaml), so `ingest/` can be copied in.
FROM apache/airflow:3.3.2

COPY airflow/requirements.txt /tmp/requirements.txt
# Pinning apache-airflow in the same command stops pip from silently up/downgrading Airflow itself.
RUN pip install --no-cache-dir "apache-airflow==3.3.2" -r /tmp/requirements.txt

COPY --chown=airflow:root ingest /opt/airflow/project/ingest
ENV PYTHONPATH=/opt/airflow/project
