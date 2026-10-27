"""Polite, robots-aware HTTP client.

* honours robots.txt (fetched once per host, RFC 9309 semantics) — disallowed URLs are never requested
* enforces a minimum delay between requests (``max(configured delay, Crawl-delay)``)
* retries network errors, 408/425/429 and 5xx with exponential backoff + jitter, honouring Retry-After
* never retries other 4xx responses; classifies every failure into a :class:`FetchError` kind
"""

from __future__ import annotations

import hashlib
import logging
import random
import time
from collections.abc import Callable
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from urllib.parse import urlsplit

import httpx

from .models import RawDocument
from .robots import RobotsPolicy

log = logging.getLogger(__name__)

RETRYABLE_STATUS = {408, 425, 429, 500, 502, 503, 504}


class FetchError(Exception):
    """A document could not be retrieved. ``kind`` is a stable, reportable error code."""

    def __init__(self, url: str, kind: str, message: str, status_code: int | None = None):
        super().__init__(f"{kind}: {message} ({url})")
        self.url = url
        self.kind = kind
        self.detail = message
        self.status_code = status_code


class PoliteClient:
    def __init__(
        self,
        *,
        user_agent: str,
        delay_seconds: float = 1.5,
        timeout_seconds: float = 30.0,
        max_retries: int = 4,
        backoff_base_seconds: float = 2.0,
        respect_robots: bool = True,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._client = httpx.Client(
            headers={
                "User-Agent": user_agent,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "de-DE,de;q=0.9,en;q=0.5",
            },
            timeout=httpx.Timeout(timeout_seconds),
            follow_redirects=True,
            transport=transport,
        )
        self._user_agent = user_agent
        self._delay = delay_seconds
        self._max_retries = max_retries
        self._backoff = backoff_base_seconds
        self._respect_robots = respect_robots
        self._robots: dict[str, RobotsPolicy] = {}
        self._last_request: dict[str, float] = {}
        self._sleep = sleep
        self._clock = clock
        self.requests_made = 0

    # -- lifecycle -------------------------------------------------------------------------
    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> PoliteClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- robots ----------------------------------------------------------------------------
    def robots_for(self, url: str) -> RobotsPolicy:
        parts = urlsplit(url)
        origin = f"{parts.scheme}://{parts.netloc}"
        if origin not in self._robots:
            try:
                resp = self._request(f"{origin}/robots.txt")
            except FetchError as exc:
                if exc.status_code is not None and 400 <= exc.status_code < 500:
                    policy = RobotsPolicy()  # RFC 9309 §2.3.1.3: 4xx → no restrictions
                else:
                    raise  # unreachable robots.txt → treat as full disallow by failing loudly
            else:
                policy = RobotsPolicy.parse(resp.text, self._user_agent)
            self._robots[origin] = policy
            log.info("robots.txt loaded", extra={"origin": origin, "rules": len(policy.rules)})
        return self._robots[origin]

    def allowed(self, url: str) -> bool:
        return not self._respect_robots or self.robots_for(url).can_fetch(url)

    # -- fetching --------------------------------------------------------------------------
    def fetch(self, url: str) -> RawDocument:
        if not self.allowed(url):
            raise FetchError(url, "robots_disallowed", "URL is disallowed by robots.txt")
        resp = self._request(url)
        text = resp.text
        if not text.strip():
            raise FetchError(url, "empty_response", "server returned an empty body", resp.status_code)
        return RawDocument(
            url=url,
            final_url=str(resp.url),
            status_code=resp.status_code,
            content=text,
            sha256=hashlib.sha256(resp.content).hexdigest(),
            fetched_at=datetime.now(UTC),
            content_type=resp.headers.get("content-type"),
        )

    def _throttle(self, host: str) -> None:
        delay = self._delay
        policy = self._robots.get(host)
        if policy and policy.crawl_delay:
            delay = max(delay, policy.crawl_delay)
        last = self._last_request.get(host)
        if last is not None:
            wait = delay - (self._clock() - last)
            if wait > 0:
                self._sleep(wait)
        self._last_request[host] = self._clock()

    def _request(self, url: str) -> httpx.Response:
        parts = urlsplit(url)
        host = f"{parts.scheme}://{parts.netloc}"
        attempt = 0
        while True:
            self._throttle(host)
            self.requests_made += 1
            try:
                resp = self._client.get(url)
            except (httpx.TimeoutException, httpx.NetworkError, httpx.RemoteProtocolError) as exc:
                kind = "timeout" if isinstance(exc, httpx.TimeoutException) else "network_error"
                if attempt >= self._max_retries:
                    raise FetchError(url, kind, str(exc) or exc.__class__.__name__) from exc
                self._backoff_sleep(attempt, None, url, kind)
                attempt += 1
                continue

            if resp.status_code < 400:
                return resp
            if resp.status_code in RETRYABLE_STATUS and attempt < self._max_retries:
                self._backoff_sleep(attempt, resp.headers.get("retry-after"), url, str(resp.status_code))
                attempt += 1
                continue
            if resp.status_code in {401, 403}:
                server = resp.headers.get("server", "origin")
                raise FetchError(
                    url,
                    "access_denied",
                    f"HTTP {resp.status_code} from {server} — access restriction respected, not bypassed",
                    resp.status_code,
                )
            kind = "not_found" if resp.status_code in {404, 410} else f"http_{resp.status_code}"
            raise FetchError(url, kind, f"HTTP {resp.status_code}", resp.status_code)

    def _backoff_sleep(self, attempt: int, retry_after: str | None, url: str, reason: str) -> None:
        delay = self._backoff * (2**attempt) + random.uniform(0, self._backoff)  # noqa: S311
        if retry_after:
            delay = max(delay, _parse_retry_after(retry_after))
        delay = min(delay, 300.0)
        log.warning("retrying", extra={"url": url, "reason": reason, "attempt": attempt + 1, "delay": round(delay, 1)})
        self._sleep(delay)


def _parse_retry_after(value: str) -> float:
    try:
        return max(0.0, float(value))
    except ValueError:
        pass
    try:
        when = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return 0.0
    return max(0.0, (when - datetime.now(UTC)).total_seconds())
