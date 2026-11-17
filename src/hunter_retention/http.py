"""Shared HTTP retry/rate-limit helper."""
from __future__ import annotations

import logging
import random
import threading
import time

import requests

log = logging.getLogger(__name__)

RETRY_STATUS = {408, 409, 425, 429, 500, 502, 503, 504}


class ApiError(Exception):
    def __init__(self, message: str, status: int | None = None, body: str | None = None):
        super().__init__(message)
        self.status = status
        self.body = body


class RateLimiter:
    """Minimum spacing between calls (simple, process-local)."""

    def __init__(self, per_second: float):
        self.interval = 1.0 / per_second if per_second > 0 else 0
        self._last = 0.0
        self._lock = threading.Lock()

    def wait(self) -> None:
        if not self.interval:
            return
        with self._lock:
            now = time.monotonic()
            delay = self._last + self.interval - now
            if delay > 0:
                time.sleep(delay)
            self._last = time.monotonic()


def request_with_retry(session: requests.Session, method: str, url: str, *, limiter: RateLimiter | None = None,
                       max_attempts: int = 5, base_delay: float = 1.0, timeout: float = 60,
                       sleep=time.sleep, **kwargs) -> requests.Response:
    last_exc: Exception | None = None
    for attempt in range(1, max_attempts + 1):
        if limiter:
            limiter.wait()
        try:
            resp = session.request(method, url, timeout=timeout, **kwargs)
        except (requests.ConnectionError, requests.Timeout) as exc:
            last_exc = exc
            log.warning("network error %s %s (attempt %s): %s", method, url, attempt, exc)
        else:
            if resp.status_code < 400:
                return resp
            if resp.status_code not in RETRY_STATUS:
                raise ApiError(f"{method} {url} -> {resp.status_code}", resp.status_code, resp.text[:2000])
            last_exc = ApiError(f"{method} {url} -> {resp.status_code}", resp.status_code, resp.text[:2000])
            retry_after = resp.headers.get("Retry-After")
            if retry_after and retry_after.isdigit():
                sleep(min(int(retry_after), 120))
                continue
        if attempt < max_attempts:
            sleep(min(base_delay * (2 ** (attempt - 1)) + random.uniform(0, 0.5), 60))
    if isinstance(last_exc, ApiError):
        raise last_exc
    raise ApiError(f"{method} {url} failed after {max_attempts} attempts: {last_exc}")
