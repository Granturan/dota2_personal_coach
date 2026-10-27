"""Writes raw API responses to the S3 Bronze layer.

Layout: bronze/<source>/<dataset>/rank=<rank>/match_date=<YYYY-MM-DD>/hour=<HH>/<file>.json.gz
Every object is an envelope: request metadata plus the untouched API payload.
Keys are deterministic, so re-running the same day overwrites instead of duplicating.
"""

import gzip
import json

LAYERS = ("bronze", "silver", "gold")

# avg_rank_tier = two digits: medal (6 Ancient, 7 Divine, 8 Immortal) + star (1-5).
# The match average never exceeds 75 in practice, so Divine 5 and Immortal matches can't be told apart.
RANKS = {
    "ancient": (60, 69),
    "divine": (70, 74),
    "divine5_immortal": (75, 89),
}


def _partition(dataset, rank, match_date, hour):
    return f"bronze/opendota/{dataset}/rank={rank}/match_date={match_date}/hour={hour:02d}"


def summaries_key(rank, match_date, hour):
    return f"{_partition('ranked_match_summaries', rank, match_date, hour)}/summaries.json.gz"


def details_prefix(rank, match_date, hour):
    return _partition("ranked_match_details", rank, match_date, hour) + "/"


def details_key(rank, match_date, hour, match_id):
    return f"{details_prefix(rank, match_date, hour)}match_id={match_id}.json.gz"


def make_envelope(response, source="opendota", **extra):
    return {
        "source": source,
        "endpoint": response.path,
        "params": response.params,
        "fetched_at": response.fetched_at,
        "http_status": response.status,
        "rate_limit_remaining_day": response.remaining_day,
        **extra,
        "payload": response.data,
    }


class BronzeWriter:
    def __init__(self, s3_client, bucket):
        self.s3 = s3_client
        self.bucket = bucket

    def put_json(self, key, envelope):
        body = gzip.compress(json.dumps(envelope, ensure_ascii=False).encode("utf-8"))
        self.s3.put_object(Bucket=self.bucket, Key=key, Body=body,
                           ContentType="application/json", ContentEncoding="gzip")
        return len(body)

    def get_json(self, key):
        obj = self.s3.get_object(Bucket=self.bucket, Key=key)
        return json.loads(gzip.decompress(obj["Body"].read()))

    def list_keys(self, prefix):
        keys = []
        for page in self.s3.get_paginator("list_objects_v2").paginate(Bucket=self.bucket, Prefix=prefix):
            keys += [o["Key"] for o in page.get("Contents", [])]
        return keys

    def ensure_layer_prefixes(self):
        """Zero-byte 'folder' markers so empty layers show up in the S3 console (S3 has no real folders)."""
        for layer in LAYERS:
            self.s3.put_object(Bucket=self.bucket, Key=f"{layer}/", Body=b"")
