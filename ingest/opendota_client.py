"""Rate-limited, retrying HTTP client for the OpenDota API."""

import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone

import requests

log = logging.getLogger(__name__)

BASE_URL = "https://api.opendota.com/api"
SERVER_ERRORS = {500, 502, 503, 504}


class QuotaExhausted(RuntimeError):
    """Raised instead of making a call that would exceed our own call budget or eat the daily reserve."""


class ApiError(RuntimeError):
    def __init__(self, path, status, body):
        super().__init__(f"{path} -> HTTP {status}: {body[:200]}")
        self.status = status


@dataclass
class ApiResponse:
    path: str
    params: dict
    status: int
    data: object
    fetched_at: str
    remaining_day: int | None = None
    headers: dict = field(default_factory=dict)


class OpenDotaClient:
    """Free tier allows 60 requests/min and 3,000/day; we stay just under 1 request/second.

    Retry policy:
      429 (rate limited)  -> wait a full minute window, then retry (up to max_retries)
      5xx (server error)  -> short exponential backoff, at most max_server_retries (often permanent per match)
    max_calls is our own budget for one run, independent of OpenDota's limit.
    """

    def __init__(self, session=None, base_url=BASE_URL, min_interval=1.05, max_retries=3, max_server_retries=2,
                 backoff_base=2.0, rate_limit_cooldown=60.0, minute_reserve=2, daily_reserve=50, max_calls=None,
                 sleep=time.sleep, clock=time.monotonic):
        self.session = session or requests.Session()
        self.base_url = base_url
        self.min_interval = min_interval
        self.max_retries = max_retries
        self.max_server_retries = max_server_retries
        self.backoff_base = backoff_base
        self.rate_limit_cooldown = rate_limit_cooldown
        self.minute_reserve = minute_reserve
        self.daily_reserve = daily_reserve
        self.max_calls = max_calls
        self._sleep = sleep
        self._clock = clock
        self._last_call = None
        self.remaining_minute = None
        self.remaining_day = None
        self.calls = 0

    def _check_budget(self):
        if self.max_calls is not None and self.calls >= self.max_calls:
            raise QuotaExhausted(f"run budget of {self.max_calls} calls used up")
        if self.remaining_day is not None and self.remaining_day <= self.daily_reserve:
            raise QuotaExhausted(f"only {self.remaining_day} calls left today (reserve {self.daily_reserve})")

    def _throttle(self):
        if self.remaining_minute is not None and self.remaining_minute <= self.minute_reserve:
            log.info("minute quota nearly used (%d left), cooling down %.0fs", self.remaining_minute, self.rate_limit_cooldown)
            self._sleep(self.rate_limit_cooldown)
            self.remaining_minute = None
        elif self._last_call is not None:
            wait = self.min_interval - (self._clock() - self._last_call)
            if wait > 0:
                self._sleep(wait)
        self._last_call = self._clock()

    def _read_limits(self, headers):
        minute, day = headers.get("x-rate-limit-remaining-minute"), headers.get("x-rate-limit-remaining-day")
        if minute is not None:
            self.remaining_minute = int(minute)
        if day is not None:
            self.remaining_day = int(day)

    def get(self, path, params=None):
        params = params or {}
        rate_limited = server_errors = network_errors = 0
        while True:
            self._check_budget()
            self._throttle()
            try:
                resp = self.session.get(f"{self.base_url}{path}", params=params, timeout=60)
            except requests.RequestException as exc:
                network_errors += 1
                if network_errors > self.max_server_retries:
                    raise
                log.warning("%s failed (%s), retry %d", path, exc, network_errors)
                self._sleep(self.backoff_base ** network_errors)
                continue

            self.calls += 1
            self._read_limits(resp.headers)

            if resp.status_code == 429 and rate_limited < self.max_retries:
                rate_limited += 1
                log.warning("%s -> HTTP 429, cooling down %.0fs (retry %d)", path, self.rate_limit_cooldown, rate_limited)
                self._sleep(self.rate_limit_cooldown)
                continue
            if resp.status_code in SERVER_ERRORS and server_errors < self.max_server_retries:
                server_errors += 1
                log.warning("%s -> HTTP %d, retry %d", path, resp.status_code, server_errors)
                self._sleep(self.backoff_base ** server_errors)
                continue
            if resp.status_code != 200:
                raise ApiError(path, resp.status_code, resp.text)

            return ApiResponse(
                path=path, params=params, status=resp.status_code, data=resp.json(),
                fetched_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
                remaining_day=self.remaining_day,
                headers={k: v for k, v in resp.headers.items() if k.lower().startswith("x-rate-limit")},
            )

    def explorer(self, sql):
        """Run a read-only SQL query against OpenDota's database."""
        resp = self.get("/explorer", {"sql": sql})
        if resp.data.get("err"):
            raise ApiError("/explorer", resp.status, str(resp.data["err"]))
        return resp
